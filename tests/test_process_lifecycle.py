"""公开执行入口必须在返回或异常退出前收口其完整进程组。"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
from typing import Iterator

import pytest

from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


@contextmanager
def _registered_groups() -> Iterator[list[tuple[int, int]]]:
    """即使回归失败，测试创建的进程也不得留到用例外。"""
    groups: list[tuple[int, int]] = []
    try:
        yield groups
    finally:
        for pid, pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def _blackbox_job(tmp_path: Path, source: str, **kwargs):
    root = tmp_path.resolve()
    script = root / "delivery.py"
    script.write_text(source, encoding="utf-8")
    request = root / "request.json"
    request.write_text("{}", encoding="utf-8")
    data = root / "data"
    data.mkdir(exist_ok=True)
    for name in (
        "daily_output.csv", "weekly_output.csv", "monthly_output.csv",
        "api_wind_date.csv",
    ):
        (data / name).write_text("key,value\n1,1\n", encoding="utf-8")
    return execute_blackbox_cli(
        script_path=script,
        mode="predict",
        input_path=request,
        data_dir=data,
        output_path=root / "run" / "result.json",
        profile=kwargs.pop("profile", RuntimeProfile.for_tests()),
        timeout_sec=kwargs.pop("timeout_sec", 3),
        **kwargs,
    )


@pytest.mark.parametrize("failure", [KeyboardInterrupt("cancel"), RuntimeError("monitor failed")])
def test_blackbox_monitor_failure_stops_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    """监控系统异常或取消不得遗留仍在运行的算法进程。"""
    system_run = subprocess.run

    def inspect_processes(command, **kwargs):
        if command[0] == "/bin/ps":
            raise failure
        return system_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", inspect_processes)
    with _registered_groups() as groups:
        with pytest.raises(type(failure)) as error:
            _blackbox_job(
                tmp_path,
                "import time; time.sleep(30)",
                process_started=lambda pid, pgid: groups.append((pid, pgid)),
            )
        assert error.value is failure
        assert len(groups) == 1
        assert not _group_exists(groups[0][1])


def _native_job(tmp_path: Path, source: str, monkeypatch, **kwargs):
    from scheduler.executor import run_scheme_subprocess
    import sys

    script = tmp_path / "native.py"
    script.write_text(source, encoding="utf-8")
    system_popen = subprocess.Popen

    def launch(command, **options):
        if "scheduler.scheme_runner" in command:
            command = [sys.executable, str(script)]
        return system_popen(command, **options)

    monkeypatch.setattr(subprocess, "Popen", launch)
    return run_scheme_subprocess("process_contract_trial", "2026-11-06", **kwargs)


@pytest.mark.parametrize("driver", ["blackbox", "native"])
def test_successful_execution_stops_remaining_group_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str,
) -> None:
    """主进程已完成结果时，同组后代仍归本次执行清理。"""
    source = '''
import pathlib
import subprocess
import sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
if "--output" in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index("--output") + 1]).write_text("{}")
else:
    print("[]", flush=True)
'''
    with _registered_groups() as groups:
        options = {"process_started": lambda pid, pgid: groups.append((pid, pgid))}
        if driver == "blackbox":
            result = _blackbox_job(tmp_path, source, **options)
            assert result.returncode == 0
        else:
            assert _native_job(tmp_path, source, monkeypatch, **options) == []
        assert len(groups) == 1
        assert not _group_exists(groups[0][1])


@pytest.mark.parametrize("driver,outcome,drain_failure", [
    (driver, outcome, None)
    for driver in ("blackbox", "native")
    for outcome in ("finished", "timed_out", "interrupted")
] + [
    pytest.param("native", "timed_out", failure, id=label)
    for label, failure in (
        ("drain-oserror", OSError("drain failed")),
        ("drain-valueerror", ValueError("drain closed")),
        ("drain-cancel", KeyboardInterrupt("second cancellation")),
    )
])
def test_unconfirmed_runtime_cleanup_blocks_subsequent_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str, outcome: str,
    drain_failure: BaseException | None,
) -> None:
    """终止权限失效后，同一批次的启动能力必须持续失效。"""
    from itertools import count
    from types import SimpleNamespace
    import time

    from scheduler.process_control import (
        ProcessGroupTerminationError, ProcessStartGuard,
        ProcessStartGuardPoisonedError,
    )

    guard = ProcessStartGuard()
    returncode = 0 if outcome == "finished" else None
    cancelling = outcome == "interrupted"
    cleanup_clock = not cancelling
    cancellation = KeyboardInterrupt("runtime cancelled")

    def interrupted(*_args, **_kwargs):
        nonlocal cleanup_clock
        cleanup_clock = True
        raise cancellation

    def communicate(**kwargs):
        if cancelling:
            return interrupted(**kwargs)
        if drain_failure is not None:
            raise drain_failure
        return "[]", ""

    process = SimpleNamespace(
        pid=4321, returncode=returncode,
        poll=lambda: returncode,
        communicate=communicate,
    )
    if cancelling:
        monkeypatch.setattr(subprocess, "run", interrupted)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 4321)

    def cannot_signal(_pgid, _signal):
        raise PermissionError("process-group permission unavailable")

    monkeypatch.setattr(os, "killpg", cannot_signal)
    ticks = count(step=1000)
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks) if cleanup_clock else 0)

    def run():
        if driver == "blackbox":
            return _blackbox_job(tmp_path, "pass", process_start_guard=guard)
        return _native_job(tmp_path, "pass", monkeypatch, process_start_guard=guard)

    with pytest.raises(BaseException) as failed:
        run()
    assert isinstance(failed.value, ProcessGroupTerminationError)
    assert not failed.value.termination.confirmed_gone
    if cancelling:
        assert failed.value.__cause__ is cancellation
    if drain_failure is not None:
        assert isinstance(failed.value.__cause__, subprocess.TimeoutExpired)

    def forbidden_start(*_args, **_kwargs):
        raise AssertionError("poisoned batch started another process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_start)
    with pytest.raises(ProcessStartGuardPoisonedError) as blocked:
        run()
    assert blocked.value.poison_error is failed.value


@pytest.mark.parametrize("driver", ["blackbox", "native"])
@pytest.mark.parametrize("outcome", ["nonzero", "timeout"])
def test_model_failure_keeps_exit_semantics_and_stops_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str, outcome: str,
) -> None:
    """收口进程组不改变模型非零退出和执行预算失败的分类。"""
    from scheduler.blackbox_v2_runner import BlackboxExecutionError

    source = "import sys; sys.exit(7)" if outcome == "nonzero" else "import time; time.sleep(30)"
    failure_type = (
        BlackboxExecutionError if driver == "blackbox"
        else subprocess.CalledProcessError if outcome == "nonzero"
        else subprocess.TimeoutExpired
    )
    with _registered_groups() as groups:
        options = {
            "process_started": lambda pid, pgid: groups.append((pid, pgid)),
            "timeout_sec": 0.2,
        }
        with pytest.raises(failure_type) as failed:
            if driver == "blackbox":
                _blackbox_job(tmp_path, source, **options)
            else:
                _native_job(tmp_path, source, monkeypatch, **options)
        if outcome == "nonzero":
            if driver == "native":
                assert failed.value.returncode == 7
            else:
                assert "exited 7" in str(failed.value)
        else:
            assert "timed out" in str(failed.value)
        assert len(groups) == 1
        assert not _group_exists(groups[0][1])


def test_blackbox_log_limit_stops_process_group(tmp_path: Path) -> None:
    """输出超限仍以既有预算分类失败，并终止完整进程组。"""
    from scheduler.blackbox_v2_runner import BlackboxExecutionError

    with _registered_groups() as groups:
        with pytest.raises(BlackboxExecutionError, match="log output exceeded limit"):
            _blackbox_job(
                tmp_path,
                "import time; print('x' * 4096, flush=True); time.sleep(30)",
                profile=RuntimeProfile.for_tests(max_log_bytes=64),
                process_started=lambda pid, pgid: groups.append((pid, pgid)),
            )
        assert len(groups) == 1
        assert not _group_exists(groups[0][1])


def test_confirmed_native_timeout_preserves_partial_process_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已确认终止后仍有界收集超时前的输出，保持原超时合同。"""
    source = '''
import sys
import time
print("partial-stdout", flush=True)
print("partial-stderr", file=sys.stderr, flush=True)
time.sleep(30)
'''
    with _registered_groups() as groups:
        with pytest.raises(subprocess.TimeoutExpired) as failed:
            _native_job(
                tmp_path, source, monkeypatch, timeout_sec=0.5,
                process_started=lambda pid, pgid: groups.append((pid, pgid)),
            )
        assert failed.value.output == "partial-stdout\n"
        assert failed.value.stderr == "partial-stderr\n"
        assert not _group_exists(groups[0][1])
