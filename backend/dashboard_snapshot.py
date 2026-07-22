from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Condition
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


@dataclass
class _Flight:
    """一次构建的完成状态，供同批 waiter 稳定观察。"""

    done: bool = False
    error: BaseException | None = None


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

    def get(self) -> SnapshotResult:
        """返回新鲜快照，或参与唯一构建并显式降级到 stale LKG。"""
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
                wait_started_at = _sample_wait_monotonic()
                wait_deadline = wait_started_at + self._wait_timeout_seconds
                if not math.isfinite(wait_deadline):
                    raise ValueError("wait monotonic deadline must be finite")
                while not flight.done:
                    remaining = wait_deadline - _sample_wait_monotonic()
                    if remaining <= 0.0:
                        break
                    self._condition.wait(timeout=remaining)

                resumed_at = _sample_wait_monotonic()
                observed_at = self._sample_monotonic()
                completed_in_time = flight.done and resumed_at <= wait_deadline
                if not completed_in_time:
                    return self._stale_or_raise(
                        observed_at=observed_at,
                        message="dashboard snapshot build timed out",
                    )

                # 恢复时先看全局缓存：旧失败 flight 的 waiter 可能已经错过一次
                # 后续成功 rebuild，不能把那个新鲜 payload 错标为 STALE。
                if self._is_fresh(observed_at):
                    return self._cached_result("HIT", observed_at)
                return self._stale_or_raise(
                    observed_at=observed_at,
                    message=(
                        "dashboard snapshot build failed"
                        if flight.error is not None
                        else "dashboard snapshot expired before waiter resumed"
                    ),
                    cause=flight.error,
                )

            flight = _Flight()
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
    ) -> SnapshotResult:
        snapshot = self._snapshot
        if snapshot is None:
            raise SnapshotUnavailable("dashboard snapshot is unavailable")
        payload, built_at = snapshot
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
    ) -> SnapshotResult:
        if self._snapshot is not None:
            return self._cached_result("STALE", observed_at)
        if cause is None:
            raise SnapshotUnavailable(message)
        raise SnapshotUnavailable(message) from cause

    def _publish_success(
        self,
        flight: _Flight,
        *,
        snapshot: tuple[dict, float],
    ) -> bool:
        """原子发布成功快照；临界区取消时回滚为原 LKG。"""
        with self._condition:
            if self._flight is not flight or flight.done:
                return False
            previous_snapshot = self._snapshot
            try:
                self._snapshot = snapshot
                flight.done = True
                self._flight = None
                self._condition.notify_all()
            except BaseException:  # noqa: BLE001 - 回滚半完成发布
                self._snapshot = previous_snapshot
                flight.error = None
                flight.done = False
                self._flight = flight
                raise
        return True

    def _finish_owner_failure(
        self,
        flight: _Flight,
        *,
        error: BaseException,
        build_started_at: float,
    ) -> SnapshotResult:
        cleanup_error = self._finalize_failed_flight(flight, error=error)

        # SystemExit/KeyboardInterrupt 等进程控制信号只在 owner 原样传播；
        # waiter 通过 flight.error 仍转换为 STALE/SnapshotUnavailable。
        if not isinstance(error, Exception):
            raise error
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error

        try:
            failed_at = self._sample_monotonic()
        except Exception:
            failed_at = build_started_at
        with self._condition:
            return self._stale_or_raise(
                observed_at=failed_at,
                message="dashboard snapshot build failed",
                cause=error,
            )

    def _finalize_failed_flight(
        self,
        flight: _Flight,
        *,
        error: BaseException,
    ) -> BaseException | None:
        """先发布 terminal 事实，再尽力 detach 并唤醒同批 waiter。"""
        # error 先写、done 后写：waiter 和后续请求只要观察到 terminal，
        # 就一定能看到完整错误，且不再被 zombie flight 阻塞。
        flight.error = error
        flight.done = True
        first_cleanup_error: BaseException | None = None
        for attempt in range(2):
            try:
                with self._condition:
                    if self._flight is not flight:
                        return first_cleanup_error
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
