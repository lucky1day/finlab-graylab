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
        profile=RuntimeProfile.for_tests(),
        timeout_sec=3,
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
