from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from backend import dashboard_snapshot as snapshot_module
from backend.dashboard_snapshot import (
    DashboardSnapshotStore,
    SnapshotResult,
    SnapshotUnavailable,
)


class ManualMonotonic:
    """线程安全的可推进单调时钟，并可卡住指定线程的一次读时钟。"""

    def __init__(self, initial: float = 0.0) -> None:
        self._value = initial
        self._lock = threading.Lock()
        self._gate_thread_name: str | None = None
        self._gate_entered: threading.Event | None = None
        self._gate_release: threading.Event | None = None

    def __call__(self) -> float:
        gate: tuple[threading.Event, threading.Event] | None = None
        with self._lock:
            if threading.current_thread().name == self._gate_thread_name:
                assert self._gate_entered is not None
                assert self._gate_release is not None
                gate = (self._gate_entered, self._gate_release)
                self._gate_thread_name = None

        if gate is not None:
            entered, release = gate
            entered.set()
            if not release.wait(timeout=3.0):
                raise RuntimeError("test monotonic gate was not released")

        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds

    def gate_next_call(
        self,
        thread_name: str,
    ) -> tuple[threading.Event, threading.Event]:
        entered = threading.Event()
        release = threading.Event()
        with self._lock:
            if self._gate_thread_name is not None:
                raise RuntimeError("a monotonic gate is already armed")
            self._gate_thread_name = thread_name
            self._gate_entered = entered
            self._gate_release = release
        return entered, release


class ControlledBuilder:
    """按调用次序返回结果，并用 Event 精确控制指定调用。"""

    def __init__(
        self,
        *responses: dict[str, Any] | BaseException,
        blocked_call: int | None = None,
    ) -> None:
        self._responses = responses
        self._blocked_call = blocked_call
        self._calls = 0
        self._lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def __call__(self) -> dict[str, Any]:
        with self._lock:
            self._calls += 1
            call_number = self._calls

        if call_number == self._blocked_call:
            self.started.set()
            if not self.release.wait(timeout=3.0):
                raise RuntimeError("test builder was not released")

        if call_number > len(self._responses):
            raise AssertionError(f"unexpected builder call {call_number}")
        response = self._responses[call_number - 1]
        if isinstance(response, BaseException):
            raise response
        return response


class ResumeGateCondition(threading.Condition):
    """在 waiter 被通知后暂时释放锁，精确控制其恢复时刻。"""

    def __init__(self) -> None:
        super().__init__()
        self._gate_lock = threading.Lock()
        self._gated_thread_name: str | None = None
        self.wait_started = threading.Event()
        self.resume_entered = threading.Event()
        self.resume_release = threading.Event()

    def gate_next_wait(self, thread_name: str) -> None:
        with self._gate_lock:
            if self._gated_thread_name is not None:
                raise RuntimeError("a waiter resume gate is already armed")
            self._gated_thread_name = thread_name

    def wait(self, timeout: float | None = None) -> bool:
        with self._gate_lock:
            should_gate = threading.current_thread().name == self._gated_thread_name
            if should_gate:
                self._gated_thread_name = None
                self.wait_started.set()

        notified = super().wait(timeout)
        if should_gate:
            self.release()
            try:
                self.resume_entered.set()
                if not self.resume_release.wait(timeout=3.0):
                    raise RuntimeError("test waiter resume gate was not released")
            finally:
                self.acquire()
        return notified


class CountingWaitCondition(threading.Condition):
    """记录指定 waiter 已真正进入 Condition.wait。"""

    def __init__(self, *, waiter_count: int) -> None:
        super().__init__()
        self._waiter_count = waiter_count
        self._waiter_names: set[str] = set()
        self._count_lock = threading.Lock()
        self.all_waiting = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        name = threading.current_thread().name
        if name.startswith("failure-waiter-"):
            with self._count_lock:
                self._waiter_names.add(name)
                if len(self._waiter_names) == self._waiter_count:
                    self.all_waiting.set()
        return super().wait(timeout)


class MutableMonotonic:
    """可在测试中切换为任意（含非法）返回值的时钟。"""

    def __init__(self, value: Any = 0.0) -> None:
        self.value = value

    def __call__(self) -> Any:
        return self.value


def _start_get(
    store: DashboardSnapshotStore,
    *,
    name: str,
    results: dict[str, SnapshotResult],
    errors: dict[str, BaseException],
) -> threading.Thread:
    def target() -> None:
        try:
            results[name] = store.get()
        except BaseException as exc:  # noqa: BLE001 - 测试必须收集线程中的所有异常
            errors[name] = exc

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread


def _join_all(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=3.0)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert not stuck, f"threads did not finish: {stuck}"


def test_fresh_snapshot_is_reused_within_one_second() -> None:
    clock = ManualMonotonic(100.0)
    calls = 0
    source_payload: dict[str, Any] = {
        "snapshot_id": "snapshot-1",
        "schemes": [{"rows": [["2026-07-22", None]]}],
    }

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        clock.advance(0.25)
        return source_payload

    store = DashboardSnapshotStore(builder, monotonic=clock)

    miss = store.prewarm()
    source_payload["snapshot_id"] = "mutated-after-build"
    source_payload["schemes"][0]["rows"].append(["2026-07-23", 1])
    clock.advance(0.75)
    hit = store.get()

    assert miss.cache_status == "MISS"
    assert miss.build_seconds == pytest.approx(0.25)
    assert hit.cache_status == "HIT"
    assert hit.age_seconds == pytest.approx(0.75)
    assert hit.build_seconds is None
    assert hit.payload is miss.payload
    assert calls == 1

    assert isinstance(hit.payload, dict)
    assert isinstance(hit.payload["schemes"], tuple)
    assert isinstance(hit.payload["schemes"][0]["rows"], tuple)
    assert json.loads(json.dumps(hit.payload)) == {
        "snapshot_id": "snapshot-1",
        "schemes": [{"rows": [["2026-07-22", None]]}],
    }
    with pytest.raises(TypeError):
        hit.payload["snapshot_id"] = "mutated"
    with pytest.raises(TypeError):
        hit.payload["schemes"][0]["new"] = "mutated"
    top_level_copy = dict(hit.payload)
    top_level_copy["stale"] = True
    assert "stale" not in hit.payload
    with pytest.raises(FrozenInstanceError):
        hit.cache_status = "STALE"  # type: ignore[misc]


def test_snapshot_expires_at_exact_ttl_boundary() -> None:
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "first"},
        {"snapshot_id": "second"},
    )
    store = DashboardSnapshotStore(builder, monotonic=clock)
    assert store.prewarm().payload["snapshot_id"] == "first"

    clock.advance(1.0)
    boundary = store.get()

    assert boundary.cache_status == "MISS"
    assert boundary.payload["snapshot_id"] == "second"
    assert builder.calls == 2


def test_only_one_builder_runs_under_concurrency() -> None:
    worker_count = 20
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(builder, monotonic=clock)
    assert store.prewarm().cache_status == "MISS"
    clock.advance(1.01)

    start = threading.Barrier(worker_count + 1)
    entered_lock = threading.Lock()
    all_entered = threading.Event()
    entered_count = 0
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    def worker(index: int) -> None:
        nonlocal entered_count
        name = f"worker-{index}"
        try:
            start.wait(timeout=3.0)
            with entered_lock:
                entered_count += 1
                if entered_count == worker_count:
                    all_entered.set()
            results[name] = store.get()
        except BaseException as exc:  # noqa: BLE001 - 收集 Barrier 和业务异常
            errors[name] = exc

    threads = [
        threading.Thread(target=worker, args=(index,), name=f"worker-{index}")
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()

    try:
        start.wait(timeout=3.0)
        assert all_entered.wait(timeout=3.0)
        assert builder.started.wait(timeout=3.0)
    finally:
        builder.release.set()
        _join_all(threads)

    assert errors == {}
    assert len(results) == worker_count
    assert builder.calls == 2
    assert [result.cache_status for result in results.values()].count("MISS") == 1
    assert {result.payload["snapshot_id"] for result in results.values()} == {"new"}


def test_waiter_receives_new_snapshot_within_deadline() -> None:
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=1.0,
        monotonic=clock,
    )
    store.prewarm()
    clock.advance(1.01)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_entered, waiter_release = clock.gate_next_call("waiter")
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    try:
        assert waiter_entered.wait(timeout=3.0)
    finally:
        waiter_release.set()
        builder.release.set()
        _join_all([build_thread, waiter_thread])

    assert errors == {}
    assert results["builder"].cache_status == "MISS"
    assert results["builder"].build_seconds == pytest.approx(0.0)
    assert results["waiter"].cache_status == "HIT"
    assert results["waiter"].build_seconds is None
    assert results["waiter"].payload["snapshot_id"] == "new"
    assert builder.calls == 2


def test_waiter_sees_stale_when_new_snapshot_expires_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    condition = ResumeGateCondition()
    condition.gate_next_wait("waiter")
    monkeypatch.setattr(snapshot_module, "Condition", lambda: condition)
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=10.0,
        monotonic=clock,
    )
    store.prewarm()
    clock.advance(1.01)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}
    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    assert condition.wait_started.wait(timeout=3.0)

    try:
        builder.release.set()
        assert condition.resume_entered.wait(timeout=3.0)
        clock.advance(1.25)
    finally:
        condition.resume_release.set()
        _join_all([build_thread, waiter_thread])

    assert errors == {}
    assert results["builder"].cache_status == "MISS"
    assert results["waiter"].cache_status == "STALE"
    assert results["waiter"].payload["snapshot_id"] == "new"
    assert results["waiter"].age_seconds == pytest.approx(1.25)


def test_waiter_past_real_deadline_cannot_return_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    condition = ResumeGateCondition()
    condition.gate_next_wait("waiter")
    monkeypatch.setattr(snapshot_module, "Condition", lambda: condition)
    wait_clock = ManualMonotonic()
    monkeypatch.setattr(snapshot_module.time, "monotonic", wait_clock)
    ttl_clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=1.0,
        monotonic=ttl_clock,
    )
    store.prewarm()
    ttl_clock.advance(1.01)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}
    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    assert condition.wait_started.wait(timeout=3.0)

    try:
        builder.release.set()
        assert condition.resume_entered.wait(timeout=3.0)
        wait_clock.advance(1.01)
    finally:
        condition.resume_release.set()
        _join_all([build_thread, waiter_thread])

    assert errors == {}
    assert results["waiter"].cache_status == "STALE"
    assert results["waiter"].payload["snapshot_id"] == "new"
    assert store.get().cache_status == "HIT"


def test_old_failed_flight_waiter_prefers_newer_fresh_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BuilderFailure(RuntimeError):
        pass

    condition = ResumeGateCondition()
    condition.gate_next_wait("waiter")
    monkeypatch.setattr(snapshot_module, "Condition", lambda: condition)
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        BuilderFailure("failed flight"),
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=10.0,
        monotonic=clock,
    )
    store.prewarm()
    clock.advance(1.01)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}
    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    assert condition.wait_started.wait(timeout=3.0)

    try:
        builder.release.set()
        assert condition.resume_entered.wait(timeout=3.0)
        _join_all([build_thread])
        recovered = store.get()
        assert recovered.cache_status == "MISS"
        assert recovered.payload["snapshot_id"] == "new"
    finally:
        condition.resume_release.set()
        _join_all([waiter_thread])

    assert errors == {}
    assert results["builder"].cache_status == "STALE"
    assert results["builder"].payload["snapshot_id"] == "old"
    assert results["waiter"].cache_status == "HIT"
    assert results["waiter"].payload["snapshot_id"] == "new"
    assert builder.calls == 3


def test_waiter_gets_visible_stale_lkg_after_deadline() -> None:
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=0.0,
        monotonic=clock,
    )
    store.prewarm()
    clock.advance(1.25)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)

    stale = store.get()

    assert stale.cache_status == "STALE"
    assert stale.payload["snapshot_id"] == "old"
    assert stale.age_seconds == pytest.approx(1.25)
    assert stale.build_seconds is None
    assert build_thread.is_alive()
    assert builder.calls == 2

    builder.release.set()
    _join_all([build_thread])
    assert errors == {}
    assert results["builder"].cache_status == "MISS"
    assert store.get().payload["snapshot_id"] == "new"


def test_builder_failure_returns_visible_stale_lkg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BuilderFailure(RuntimeError):
        pass

    waiter_count = 25
    condition = CountingWaitCondition(waiter_count=waiter_count)
    monkeypatch.setattr(snapshot_module, "Condition", lambda: condition)
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        BuilderFailure("database unavailable"),
        {"snapshot_id": "recovered"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(builder, monotonic=clock)
    store.prewarm()
    clock.advance(1.5)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_threads = [
        _start_get(
            store,
            name=f"failure-waiter-{index}",
            results=results,
            errors=errors,
        )
        for index in range(waiter_count)
    ]
    try:
        assert condition.all_waiting.wait(timeout=3.0)
    finally:
        builder.release.set()
        _join_all([build_thread, *waiter_threads])

    assert errors == {}
    assert builder.calls == 2
    assert {result.cache_status for result in results.values()} == {"STALE"}
    assert {result.payload["snapshot_id"] for result in results.values()} == {"old"}
    assert all(result.build_seconds is None for result in results.values())
    assert len(results) == waiter_count + 1
    assert store._flight is None

    recovered = store.get()
    assert recovered.cache_status == "MISS"
    assert recovered.payload["snapshot_id"] == "recovered"
    assert builder.calls == 3


def test_first_builder_failure_raises_without_fake_empty_snapshot() -> None:
    class BuilderFailure(RuntimeError):
        pass

    clock = ManualMonotonic()
    builder = ControlledBuilder(
        BuilderFailure("first snapshot failed"),
        blocked_call=1,
    )
    store = DashboardSnapshotStore(builder, monotonic=clock)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_entered, waiter_release = clock.gate_next_call("waiter")
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    try:
        assert waiter_entered.wait(timeout=3.0)
    finally:
        waiter_release.set()
        builder.release.set()
        _join_all([build_thread, waiter_thread])

    assert results == {}
    assert builder.calls == 1
    assert set(errors) == {"builder", "waiter"}
    assert all(isinstance(error, SnapshotUnavailable) for error in errors.values())
    assert all(
        isinstance(error.__cause__, BuilderFailure) for error in errors.values()
    )
    assert store._flight is None


def test_baseexception_owner_reraises_but_waiter_gets_safe_error() -> None:
    signal = SystemExit("owner cancelled")
    clock = ManualMonotonic()
    builder = ControlledBuilder(
        signal,
        {"snapshot_id": "retry"},
        blocked_call=1,
    )
    store = DashboardSnapshotStore(builder, monotonic=clock)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}
    owner_thread = _start_get(
        store,
        name="owner",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_entered, waiter_release = clock.gate_next_call("waiter")
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    try:
        assert waiter_entered.wait(timeout=3.0)
    finally:
        waiter_release.set()
        builder.release.set()
        _join_all([owner_thread, waiter_thread])

    assert results == {}
    assert errors["owner"] is signal
    assert isinstance(errors["waiter"], SnapshotUnavailable)
    assert errors["waiter"].__cause__ is signal
    assert store._flight is None

    retry = store.get()
    assert retry.cache_status == "MISS"
    assert retry.payload["snapshot_id"] == "retry"
    assert builder.calls == 2


@pytest.mark.parametrize("failure_point", ["freeze", "finish_clock"])
def test_baseexception_during_postbuild_reraises_and_releases_flight(
    failure_point: str,
) -> None:
    signal = KeyboardInterrupt(failure_point)
    calls = 0

    class ExplodingDict(dict):
        def items(self) -> Any:
            raise signal

    class FinishFailingMonotonic:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            if failure_point == "finish_clock" and self.calls == 2:
                raise signal
            return 0.0

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if failure_point == "freeze" and calls == 1:
            return ExplodingDict(snapshot_id="discarded")
        return {"snapshot_id": f"snapshot-{calls}"}

    store = DashboardSnapshotStore(builder, monotonic=FinishFailingMonotonic())

    with pytest.raises(BaseException) as caught:
        store.get()

    assert caught.value is signal
    assert store._flight is None
    assert store._payload is None
    retry = store.get()
    assert retry.cache_status == "MISS"
    assert retry.payload["snapshot_id"] == "snapshot-2"
    assert calls == 2


@pytest.mark.parametrize("invalid", [True, "not-a-number", math.nan, math.inf, -math.inf])
def test_invalid_initial_monotonic_sample_is_rejected(invalid: Any) -> None:
    clock = MutableMonotonic(invalid)
    builder = ControlledBuilder({"snapshot_id": "must-not-build"})
    store = DashboardSnapshotStore(builder, monotonic=clock)

    with pytest.raises(ValueError, match="monotonic"):
        store.get()

    assert builder.calls == 0
    assert store._payload is None
    assert store._flight is None


@pytest.mark.parametrize("invalid", [True, "not-a-number", math.nan, math.inf, -math.inf])
def test_invalid_finished_monotonic_sample_does_not_publish(invalid: Any) -> None:
    clock = MutableMonotonic(0.0)
    calls = 0

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.value = invalid
        return {"snapshot_id": f"snapshot-{calls}"}

    store = DashboardSnapshotStore(builder, monotonic=clock)

    with pytest.raises(SnapshotUnavailable) as caught:
        store.get()

    assert isinstance(caught.value.__cause__, ValueError)
    assert store._payload is None
    assert store._flight is None

    clock.value = 2.0
    retry = store.get()
    assert retry.cache_status == "MISS"
    assert retry.payload["snapshot_id"] == "snapshot-2"
    assert calls == 2


def test_invalid_waiter_monotonic_sample_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    condition = ResumeGateCondition()
    condition.gate_next_wait("waiter")
    monkeypatch.setattr(snapshot_module, "Condition", lambda: condition)
    clock = MutableMonotonic(0.0)
    builder = ControlledBuilder(
        {"snapshot_id": "old"},
        {"snapshot_id": "new"},
        blocked_call=2,
    )
    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=10.0,
        monotonic=clock,
    )
    store.prewarm()
    clock.value = 1.01
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}
    build_thread = _start_get(
        store,
        name="builder",
        results=results,
        errors=errors,
    )
    assert builder.started.wait(timeout=3.0)
    waiter_thread = _start_get(
        store,
        name="waiter",
        results=results,
        errors=errors,
    )
    assert condition.wait_started.wait(timeout=3.0)

    try:
        builder.release.set()
        assert condition.resume_entered.wait(timeout=3.0)
        clock.value = math.nan
    finally:
        condition.resume_release.set()
        _join_all([build_thread, waiter_thread])

    assert results["builder"].cache_status == "MISS"
    assert isinstance(errors["waiter"], ValueError)
    clock.value = 1.02
    assert store.get().cache_status == "HIT"


def test_wall_clock_change_does_not_change_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualMonotonic(500.0)
    calls = 0

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"snapshot_id": f"snapshot-{calls}"}

    store = DashboardSnapshotStore(builder, monotonic=clock)
    assert store.prewarm().payload["snapshot_id"] == "snapshot-1"

    monkeypatch.setattr(time, "time", lambda: 9_999_999_999.0)
    clock.advance(0.75)
    assert store.get().cache_status == "HIT"

    monkeypatch.setattr(time, "time", lambda: -9_999_999_999.0)
    clock.advance(0.26)
    rebuilt = store.get()

    assert rebuilt.cache_status == "MISS"
    assert rebuilt.payload["snapshot_id"] == "snapshot-2"
    assert calls == 2


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"ttl_seconds": 0}, ValueError),
        ({"ttl_seconds": -1}, ValueError),
        ({"ttl_seconds": math.inf}, ValueError),
        ({"ttl_seconds": math.nan}, ValueError),
        ({"wait_timeout_seconds": -0.1}, ValueError),
        ({"wait_timeout_seconds": math.inf}, ValueError),
        ({"monotonic": None}, TypeError),
    ],
)
def test_invalid_constructor_arguments_are_rejected(
    kwargs: dict[str, Any],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        DashboardSnapshotStore(lambda: {}, **kwargs)


def test_builder_must_be_callable() -> None:
    with pytest.raises(TypeError):
        DashboardSnapshotStore(None)  # type: ignore[arg-type]
