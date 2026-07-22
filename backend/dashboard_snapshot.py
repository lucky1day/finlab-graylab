from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Condition
from typing import Any, Callable, Literal, NoReturn


class _FrozenDict(dict):
    """保留标准 JSON dict 兼容性的不可变字典。"""

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
            observed_at = self._monotonic()
            if self._is_fresh(observed_at):
                return self._cached_result("HIT", observed_at)

            if self._building:
                flight = self._flight
                if flight is None:
                    raise RuntimeError("snapshot build state is inconsistent")
                completed = self._condition.wait_for(
                    lambda: flight.done,
                    timeout=self._wait_timeout_seconds,
                )
                if not completed:
                    return self._stale_or_raise(
                        observed_at=self._monotonic(),
                        message="dashboard snapshot build timed out",
                    )
                if flight.error is not None:
                    return self._stale_or_raise(
                        observed_at=self._monotonic(),
                        message="dashboard snapshot build failed",
                        cause=flight.error,
                    )
                return self._cached_result("HIT", self._monotonic())

            flight = _Flight()
            self._flight = flight
            self._building = True
            build_started_at = observed_at

        try:
            payload = self._builder()
            if not isinstance(payload, dict):
                raise TypeError("dashboard snapshot builder must return a dict")
            frozen_payload = _freeze(payload)
            build_finished_at = self._monotonic()
        except BaseException as error:  # noqa: BLE001 - 必须释放所有 flight
            try:
                failed_at = self._monotonic()
            except BaseException:  # noqa: BLE001 - 时钟异常也不能泄漏 flight
                failed_at = build_started_at
            with self._condition:
                flight.error = error
                flight.done = True
                self._building = False
                self._condition.notify_all()
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
            self._condition.notify_all()
            return SnapshotResult(
                payload=frozen_payload,
                cache_status="MISS",
                age_seconds=0.0,
                build_seconds=max(0.0, build_finished_at - build_started_at),
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


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
