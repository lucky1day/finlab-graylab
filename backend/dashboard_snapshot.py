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
        self._payload: dict | None = None
        self._built_at: float | None = None
        self._building = False
        self._flight: _Flight | None = None

    def get(self) -> SnapshotResult:
        """返回新鲜快照，或参与唯一构建并显式降级到 stale LKG。"""
        with self._condition:
            observed_at = self._sample_monotonic()
            if self._is_fresh(observed_at):
                return self._cached_result("HIT", observed_at)

            if self._building:
                flight = self._flight
                if flight is None:
                    raise RuntimeError("snapshot build state is inconsistent")
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
            self._building = True
            build_started_at = observed_at

        try:
            payload = self._builder()
            if not isinstance(payload, dict):
                raise TypeError("dashboard snapshot builder must return a dict")
            frozen_payload = _freeze(payload)
            build_finished_at = self._sample_monotonic()
            build_seconds = _elapsed(build_finished_at, build_started_at)
        except BaseException as error:  # noqa: BLE001 - 必须释放所有 flight
            with self._condition:
                flight.error = error
                flight.done = True
                self._building = False
                if self._flight is flight:
                    self._flight = None
                self._condition.notify_all()

            # SystemExit/KeyboardInterrupt 等进程控制信号只在 owner 原样传播；
            # waiter 通过 flight.error 仍转换为 STALE/SnapshotUnavailable。
            if not isinstance(error, Exception):
                raise

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

        with self._condition:
            self._payload = frozen_payload
            self._built_at = build_finished_at
            flight.done = True
            self._building = False
            if self._flight is flight:
                self._flight = None
            self._condition.notify_all()
            return SnapshotResult(
                payload=frozen_payload,
                cache_status="MISS",
                age_seconds=0.0,
                build_seconds=build_seconds,
            )

    def prewarm(self) -> SnapshotResult:
        """按与请求相同的规则预构建或复用快照。"""
        return self.get()

    def _is_fresh(self, observed_at: float) -> bool:
        return (
            self._payload is not None
            and self._built_at is not None
            and _elapsed(observed_at, self._built_at) < self._ttl_seconds
        )

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
        if self._payload is None or self._built_at is None:
            raise SnapshotUnavailable("dashboard snapshot is unavailable")
        return SnapshotResult(
            payload=self._payload,
            cache_status=cache_status,
            age_seconds=_elapsed(observed_at, self._built_at),
            build_seconds=None,
        )

    def _stale_or_raise(
        self,
        *,
        observed_at: float,
        message: str,
        cause: BaseException | None = None,
    ) -> SnapshotResult:
        if self._payload is not None:
            return self._cached_result("STALE", observed_at)
        if cause is None:
            raise SnapshotUnavailable(message)
        raise SnapshotUnavailable(message) from cause


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
