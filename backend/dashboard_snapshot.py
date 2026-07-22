from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Condition, local
from typing import Any, Callable, Literal, NoReturn


class _FrozenDict(dict):
    """保留标准 JSON dict 兼容性，并阻止调用方的常规写操作。"""

    # dict subclass 必然可被 dict.__setitem__(obj, ...) 强制绕过；这里按契约
    # 防止 route/普通调用方误改，同时保留标准 json.dumps 的原生编码能力。

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("dashboard snapshot payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


@dataclass(frozen=True)
class SnapshotResult:
    """一次 dashboard 快照读取结果。"""

    payload: dict
    cache_status: Literal["HIT", "MISS", "STALE"]
    age_seconds: float
    build_seconds: float | None


class SnapshotUnavailable(RuntimeError):
    """首份 dashboard 快照不可用。"""


@dataclass(frozen=True)
class _FlightOutcome:
    """一次 flight 的不可变 terminal 结果。"""

    error: BaseException | None
    attempt_id: int
    attempt_status: Literal["success", "failed"]
    attempt_seconds: float | None


@dataclass
class _Flight:
    """一次构建状态；单一 outcome 引用是唯一 terminal 事实。"""

    attempt_id: int = 0
    outcome: _FlightOutcome | None = None
    waiter_count: int = 0

    @property
    def done(self) -> bool:
        return self.outcome is not None

    @property
    def error(self) -> BaseException | None:
        outcome = self.outcome
        return None if outcome is None else outcome.error


class DashboardSnapshotStore:
    """进程内 dashboard TTL、single-flight 与 LKG 快照存储。"""

    def __init__(
        self,
        builder: Callable[[], dict[str, Any]],
        *,
        ttl_seconds: float = 1.0,
        wait_timeout_seconds: float = 0.6,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(builder):
            raise TypeError("builder must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._ttl_seconds = _validated_duration(
            "ttl_seconds",
            ttl_seconds,
            allow_zero=False,
        )
        self._wait_timeout_seconds = _validated_duration(
            "wait_timeout_seconds",
            wait_timeout_seconds,
            allow_zero=True,
        )
        self._builder = builder
        self._monotonic = monotonic
        self._condition = Condition()
        self._snapshot: tuple[dict, float] | None = None
        self._flight: _Flight | None = None
        self._request_local = local()
        self._observation_revision = 0
        self._next_attempt_id = 0
        self._snapshot_generation = 0
        self._snapshot_origin_waiter_count = 0
        self._snapshot_origin_build_seconds: float | None = None
        self._waiter_registrations: dict[object, _Flight] = {}
        self._last_waiter_count = 0
        self._last_build_seconds: float | None = None
        self._last_attempt_id: int | None = None
        self._last_attempt_status: Literal["success", "failed"] | None = None
        self._last_attempt_seconds: float | None = None

    def get(self) -> SnapshotResult:
        """返回新鲜快照，或参与唯一构建并显式降级到 stale LKG。"""
        self._request_local.diagnostics = {
            "observation_revision": None,
            "request_waited": False,
            "build_waiter_count": 0,
            "snapshot_generation": None,
            "cache_status": None,
            "attempt_status": None,
            "attempt_seconds": None,
            "attempt_id": None,
        }
        with self._condition:
            flight_at_entry = self._flight
            joined_active_flight = (
                flight_at_entry is not None and not flight_at_entry.done
            )
            observed_at = self._sample_monotonic()
            if self._is_fresh(observed_at):
                return self._cached_result("HIT", observed_at)

            if joined_active_flight:
                flight = flight_at_entry
                if flight is None:  # pragma: no cover - narrowed above
                    raise RuntimeError("snapshot flight capture is inconsistent")
                registration = object()
                try:
                    # waiter_count 统计所有已选择加入该 flight 的请求；
                    # store 级 registry token 是唯一 active waiter 事实。
                    flight.waiter_count += 1
                    self._waiter_registrations[registration] = flight
                    self._request_local.diagnostics["request_waited"] = True
                    wait_started_at = _sample_wait_monotonic()
                    wait_deadline = (
                        wait_started_at + self._wait_timeout_seconds
                    )
                    if not math.isfinite(wait_deadline):
                        raise ValueError(
                            "wait monotonic deadline must be finite"
                        )
                    while not flight.done:
                        remaining = wait_deadline - _sample_wait_monotonic()
                        if remaining <= 0.0:
                            break
                        self._condition.wait(timeout=remaining)

                    resumed_at = _sample_wait_monotonic()
                    observed_at = self._sample_monotonic()
                    completed_in_time = (
                        flight.done and resumed_at <= wait_deadline
                    )
                    if not completed_in_time:
                        return self._stale_or_raise(
                            observed_at=observed_at,
                            message="dashboard snapshot build timed out",
                            flight=flight,
                            attempt_status="timeout",
                            attempt_seconds=max(
                                0.0,
                                resumed_at - wait_started_at,
                            ),
                        )

                    # 恢复时先看全局缓存：旧失败 flight 的 waiter 可能已经错过一次
                    # 后续成功 rebuild，不能把那个新鲜 payload 错标为 STALE。
                    if self._is_fresh(observed_at):
                        return self._cached_result(
                            "HIT",
                            observed_at,
                            flight=flight,
                        )
                    return self._stale_or_raise(
                        observed_at=observed_at,
                        message=(
                            "dashboard snapshot build failed"
                            if flight.error is not None
                            else (
                                "dashboard snapshot expired before waiter "
                                "resumed"
                            )
                        ),
                        cause=flight.error,
                        flight=flight,
                    )
                finally:
                    self._waiter_registrations.pop(registration, None)

            self._next_attempt_id += 1
            flight = _Flight()
            flight.attempt_id = self._next_attempt_id
            self._flight = flight
            build_started_at = observed_at

        try:
            payload = self._builder()
            if not isinstance(payload, dict):
                raise TypeError("dashboard snapshot builder must return a dict")
            frozen_payload = _freeze(payload)
            build_finished_at = self._sample_monotonic()
            build_seconds = _elapsed(build_finished_at, build_started_at)
            result = SnapshotResult(
                payload=frozen_payload,
                cache_status="MISS",
                age_seconds=0.0,
                build_seconds=build_seconds,
            )
        except BaseException as error:  # noqa: BLE001 - 必须释放所有 flight
            return self._finish_owner_failure(
                flight,
                error=error,
                build_started_at=build_started_at,
            )

        try:
            published = self._publish_success(
                flight,
                snapshot=(frozen_payload, build_finished_at),
                build_seconds=build_seconds,
            )
        except BaseException as error:  # noqa: BLE001 - 发布取消也必须完成 flight
            return self._finish_owner_failure(
                flight,
                error=error,
                build_started_at=build_started_at,
            )
        if not published:
            observed_at = self._sample_monotonic()
            with self._condition:
                if self._is_fresh(observed_at):
                    return self._cached_result("HIT", observed_at)
                return self._stale_or_raise(
                    observed_at=observed_at,
                    message="dashboard snapshot owner lost its flight",
                )
        return result

    def prewarm(self) -> SnapshotResult:
        """按与请求相同的规则预构建或复用快照。"""
        return self.get()

    def diagnostics(self) -> dict[str, Any]:
        """返回 single-flight 的只读计数和最近一次构建耗时。"""
        with self._condition:
            return {
                "active_waiter_count": len(self._waiter_registrations),
                "last_waiter_count": self._last_waiter_count,
                "last_build_seconds": self._last_build_seconds,
                "snapshot_origin_build_seconds": (
                    self._snapshot_origin_build_seconds
                ),
                "last_attempt_id": self._last_attempt_id,
                "last_attempt_status": self._last_attempt_status,
                "last_attempt_seconds": self._last_attempt_seconds,
            }

    def request_diagnostics(self) -> dict[str, Any]:
        """返回当前线程最近一次 get/prewarm 的观察诊断副本。"""
        diagnostics = getattr(self._request_local, "diagnostics", None)
        return {} if diagnostics is None else dict(diagnostics)

    def _is_fresh(self, observed_at: float) -> bool:
        snapshot = self._snapshot
        return snapshot is not None and _elapsed(
            observed_at,
            snapshot[1],
        ) < self._ttl_seconds

    @property
    def _payload(self) -> dict | None:
        snapshot = self._snapshot
        return None if snapshot is None else snapshot[0]

    @property
    def _built_at(self) -> float | None:
        snapshot = self._snapshot
        return None if snapshot is None else snapshot[1]

    @property
    def _building(self) -> bool:
        flight = self._flight
        return flight is not None and not flight.done

    def _sample_monotonic(self) -> float:
        return _finite_monotonic_sample(
            self._monotonic(),
            name="monotonic",
        )

    def _cached_result(
        self,
        cache_status: Literal["HIT", "STALE"],
        observed_at: float,
        *,
        flight: _Flight | None = None,
        attempt_status: Literal[
            "success", "failed", "timeout"
        ] | None = None,
        attempt_seconds: float | None = None,
    ) -> SnapshotResult:
        snapshot = self._snapshot
        if snapshot is None:
            raise SnapshotUnavailable("dashboard snapshot is unavailable")
        payload, built_at = snapshot
        self._record_observation(
            cache_status,
            flight=flight,
            attempt_status=attempt_status,
            attempt_seconds=attempt_seconds,
        )
        return SnapshotResult(
            payload=payload,
            cache_status=cache_status,
            age_seconds=_elapsed(observed_at, built_at),
            build_seconds=None,
        )

    def _stale_or_raise(
        self,
        *,
        observed_at: float,
        message: str,
        cause: BaseException | None = None,
        flight: _Flight | None = None,
        attempt_status: Literal[
            "success", "failed", "timeout"
        ] | None = None,
        attempt_seconds: float | None = None,
    ) -> SnapshotResult:
        if self._snapshot is not None:
            return self._cached_result(
                "STALE",
                observed_at,
                flight=flight,
                attempt_status=attempt_status,
                attempt_seconds=attempt_seconds,
            )
        self._record_observation(
            "UNAVAILABLE",
            flight=flight,
            attempt_status=attempt_status,
            attempt_seconds=attempt_seconds,
        )
        if cause is None:
            raise SnapshotUnavailable(message)
        raise SnapshotUnavailable(message) from cause

    def _publish_success(
        self,
        flight: _Flight,
        *,
        snapshot: tuple[dict, float],
        build_seconds: float = 0.0,
    ) -> bool:
        """原子发布成功快照；临界区取消时回滚为原 LKG。"""
        terminal_error: BaseException | None = None
        with self._condition:
            if self._flight is not flight or flight.done:
                return False
            previous_snapshot = self._snapshot
            previous_snapshot_generation = self._snapshot_generation
            previous_snapshot_origin_waiter_count = (
                self._snapshot_origin_waiter_count
            )
            previous_snapshot_origin_build_seconds = (
                self._snapshot_origin_build_seconds
            )
            previous_waiter_count = self._last_waiter_count
            previous_build_seconds = self._last_build_seconds
            try:
                self._snapshot = snapshot
                self._snapshot_generation += 1
                self._snapshot_origin_waiter_count = flight.waiter_count
                self._snapshot_origin_build_seconds = build_seconds
                self._last_waiter_count = flight.waiter_count
                self._last_build_seconds = build_seconds
                terminal_error = _publish_flight_outcome(
                    flight,
                    _FlightOutcome(
                        error=None,
                        attempt_id=flight.attempt_id,
                        attempt_status="success",
                        attempt_seconds=build_seconds,
                    ),
                )
                self._record_terminal_attempt(flight.outcome)
                self._flight = None
                self._record_observation("MISS", flight=flight)
                self._condition.notify_all()
            except BaseException:  # noqa: BLE001 - 回滚半完成发布
                if flight.outcome is None:
                    self._snapshot = previous_snapshot
                    self._snapshot_generation = previous_snapshot_generation
                    self._snapshot_origin_waiter_count = (
                        previous_snapshot_origin_waiter_count
                    )
                    self._snapshot_origin_build_seconds = (
                        previous_snapshot_origin_build_seconds
                    )
                    self._last_waiter_count = previous_waiter_count
                    self._last_build_seconds = previous_build_seconds
                    self._flight = flight
                else:
                    # outcome 已 terminal 时绝不能回退为 active；snapshot tuple
                    # 已单次发布，可安全保留并仅做 identity-guarded detach。
                    if self._flight is flight:
                        self._flight = None
                raise
        if terminal_error is not None:
            raise terminal_error
        return True

    def _finish_owner_failure(
        self,
        flight: _Flight,
        *,
        error: BaseException,
        build_started_at: float,
    ) -> SnapshotResult:
        try:
            failed_at = self._sample_monotonic()
        except BaseException as timing_error:  # noqa: BLE001 - 先释放 flight
            cleanup_error = self._finalize_failed_flight(
                flight,
                error=error,
                attempt_seconds=None,
            )
            if not isinstance(timing_error, Exception):
                raise timing_error
            failed_at = build_started_at
        else:
            cleanup_error = self._finalize_failed_flight(
                flight,
                error=error,
                attempt_seconds=_elapsed(failed_at, build_started_at),
            )

        # SystemExit/KeyboardInterrupt 等进程控制信号只在 owner 原样传播；
        # waiter 通过 flight.error 仍转换为 STALE/SnapshotUnavailable。
        if not isinstance(error, Exception):
            raise error
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error

        with self._condition:
            try:
                observed_at = self._sample_monotonic()
            except Exception:
                observed_at = failed_at
            if self._is_fresh(observed_at):
                return self._cached_result(
                    "HIT",
                    observed_at,
                    flight=flight,
                )
            return self._stale_or_raise(
                observed_at=observed_at,
                message="dashboard snapshot build failed",
                cause=error,
                flight=flight,
            )

    def _record_observation(
        self,
        cache_status: Literal["HIT", "MISS", "STALE", "UNAVAILABLE"],
        *,
        flight: _Flight | None,
        attempt_status: Literal[
            "success", "failed", "timeout"
        ] | None = None,
        attempt_seconds: float | None = None,
    ) -> None:
        """在结果已确定的 condition 临界区发布单调观察序号。"""
        outcome = None if flight is None else flight.outcome
        if attempt_status is None and outcome is not None:
            attempt_status = outcome.attempt_status
            attempt_seconds = outcome.attempt_seconds
        if outcome is not None:
            self._record_terminal_attempt(outcome)
        self._observation_revision += 1
        current = getattr(self._request_local, "diagnostics", {})
        build_waiter_count = (
            flight.waiter_count
            if flight is not None
            else self._snapshot_origin_waiter_count
        )
        self._request_local.diagnostics = {
            **current,
            "observation_revision": self._observation_revision,
            "build_waiter_count": build_waiter_count,
            "snapshot_generation": (
                self._snapshot_generation
                if self._snapshot is not None
                else None
            ),
            "cache_status": cache_status,
            "attempt_status": attempt_status,
            "attempt_seconds": attempt_seconds,
            "attempt_id": (
                flight.attempt_id
                if flight is not None and flight.attempt_id > 0
                else None
            ),
        }

    def _record_terminal_attempt(
        self,
        outcome: _FlightOutcome | None,
    ) -> None:
        """只允许同代或更新 flight 更新全局 terminal attempt。"""
        if outcome is None or outcome.attempt_id <= 0:
            return
        if (
            self._last_attempt_id is not None
            and outcome.attempt_id < self._last_attempt_id
        ):
            return
        self._last_attempt_id = outcome.attempt_id
        self._last_attempt_status = outcome.attempt_status
        self._last_attempt_seconds = outcome.attempt_seconds

    def _finalize_failed_flight(
        self,
        flight: _Flight,
        *,
        error: BaseException,
        attempt_seconds: float | None,
    ) -> BaseException | None:
        """先发布 terminal 事实，再尽力 detach 并唤醒同批 waiter。"""
        first_cleanup_error = _publish_flight_outcome(
            flight,
            _FlightOutcome(
                error=error,
                attempt_id=flight.attempt_id,
                attempt_status="failed",
                attempt_seconds=attempt_seconds,
            ),
        )
        for attempt in range(2):
            try:
                with self._condition:
                    self._record_terminal_attempt(flight.outcome)
                    if self._flight is not flight:
                        return first_cleanup_error
                    self._last_waiter_count = flight.waiter_count
                    self._flight = None
                    self._condition.notify_all()
                return first_cleanup_error
            except BaseException as cleanup_error:  # noqa: BLE001 - 有限清理重试
                if first_cleanup_error is None:
                    first_cleanup_error = cleanup_error
                if attempt == 1:
                    if not isinstance(error, Exception):
                        raise error
                    raise cleanup_error
        raise RuntimeError("unreachable failed-flight finalization state")


def _publish_flight_outcome(
    flight: _Flight,
    outcome: _FlightOutcome,
) -> BaseException | None:
    """以一次引用写发布 terminal；有限取消前后均可恢复。"""
    first_cancellation: BaseException | None = None
    for _ in range(8):
        if flight.outcome is not None:
            break
        try:
            flight.outcome = outcome
        except BaseException as cancellation:  # noqa: BLE001 - terminal 必须可恢复
            if first_cancellation is None:
                first_cancellation = cancellation
            # 赋值后取消时 outcome 已可见，循环自然结束；赋值前取消则重试。
    if flight.outcome is None:
        # 避开自定义 __setattr__ 的持续取消钩子，做最后一次内建引用写；
        # 真实异步异常若连此处也持续打断则向上传播，绝不无限吞取消。
        object.__setattr__(flight, "outcome", outcome)
    return first_cancellation


def _validated_duration(
    name: str,
    value: float,
    *,
    allow_zero: bool,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        duration = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    minimum_is_valid = duration >= 0.0 if allow_zero else duration > 0.0
    if not math.isfinite(duration) or not minimum_is_valid:
        comparator = ">=" if allow_zero else ">"
        raise ValueError(f"{name} must be finite and {comparator} 0")
    return duration


def _elapsed(observed_at: float, built_at: float) -> float:
    return max(0.0, observed_at - built_at)


def _sample_wait_monotonic() -> float:
    return _finite_monotonic_sample(
        time.monotonic(),
        name="wait monotonic",
    )


def _finite_monotonic_sample(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} sample must be a finite number")
    try:
        sample = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} sample must be a finite number") from error
    if not math.isfinite(sample):
        raise ValueError(f"{name} sample must be a finite number")
    return sample


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
