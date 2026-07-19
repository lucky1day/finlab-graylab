from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from shared.blackbox_v2.contracts import (
    BlackboxMetadata,
    BlackboxRequest,
    BlackboxResult,
    load_backtest_results,
    load_prediction_result,
)
from shared.blackbox_v2.requests import write_request, write_requests
from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES
from shared.models import PredictionRecord


@dataclass(frozen=True)
class RuntimeProfile:
    name: str = "blackbox-v2-v1"
    conda_env: str | None = "forecast_env_blackbox_v1"
    predict_timeout_sec: int = 3600
    backtest_timeout_sec: int = 14400
    max_batch_requests: int = 100
    cpu_threads: int = 8
    memory_limit_bytes: int = 64 * 1024**3
    max_output_bytes: int = 50 * 1024**2
    max_log_bytes: int = 5 * 1024**2
    sandbox_enabled: bool = True

    @classmethod
    def for_tests(cls, **overrides) -> RuntimeProfile:
        return replace(cls(conda_env=None, sandbox_enabled=False), **overrides)


DEFAULT_RUNTIME_PROFILE = RuntimeProfile()


class BlackboxExecutionError(RuntimeError):
    pass


def probe_blackbox_help(
    script_path: str | Path,
    *,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> str:
    """验证交付脚本公开 predict 和 backtest 两种 CLI 模式。"""
    script = Path(script_path).resolve()
    if not script.is_file() or script.is_symlink() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")

    with tempfile.TemporaryDirectory(prefix="blackbox-v2-help-") as tmpdir:
        writable_dir = Path(tmpdir)
        command = _python_command(profile) + [str(script), "--help"]
        if profile.sandbox_enabled:
            command = _sandbox_command(command, writable_dir)
        completed = _run_process(
            command,
            cwd=writable_dir,
            env=_runtime_environment(profile, writable_dir),
            timeout=min(profile.predict_timeout_sec, 60),
            memory_limit_bytes=profile.memory_limit_bytes,
            max_capture_bytes=profile.max_log_bytes,
        )

    if completed.returncode != 0:
        raise BlackboxExecutionError(
            f"Blackbox V2 --help exited {completed.returncode}: {_bounded(completed.stderr)}"
        )
    help_text = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    missing = [mode for mode in ("predict", "backtest") if mode not in help_text]
    if missing:
        raise BlackboxExecutionError(
            f"Blackbox V2 --help must expose predict and backtest; missing={missing}"
        )
    return help_text


def execute_blackbox_cli(
    *,
    script_path: str | Path,
    mode: str,
    input_path: str | Path,
    data_dir: str | Path,
    output_path: str | Path,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> subprocess.CompletedProcess[str]:
    """执行一次 Blackbox V2 CLI；仅进程成功且 Output 合法存在才返回。"""
    if mode not in {"predict", "backtest"}:
        raise ValueError(f"unsupported Blackbox V2 mode: {mode}")
    script = Path(script_path).resolve()
    input_file = Path(input_path).resolve()
    data = Path(data_dir).resolve()
    output = Path(output_path).resolve()
    if not script.is_file() or script.is_symlink() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")
    if not input_file.is_file() or input_file.is_symlink():
        raise ValueError(f"Blackbox V2 input must be one regular file: {input_file}")
    _validate_data_dir(data)
    if output.exists():
        raise ValueError(f"platform must provide a fresh output path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    input_flag = "--request" if mode == "predict" else "--requests"
    command = _python_command(profile) + [
        str(script),
        mode,
        input_flag,
        str(input_file),
        "--data-dir",
        str(data),
        "--output",
        str(output),
    ]
    if profile.sandbox_enabled:
        command = _sandbox_command(command, output.parent)
    env = _runtime_environment(profile, output.parent)
    timeout = profile.predict_timeout_sec if mode == "predict" else profile.backtest_timeout_sec
    try:
        completed = _run_process(
            command,
            cwd=output.parent,
            env=env,
            timeout=timeout,
            memory_limit_bytes=profile.memory_limit_bytes,
            max_capture_bytes=profile.max_log_bytes,
        )
        if completed.returncode != 0:
            raise BlackboxExecutionError(
                f"Blackbox V2 {mode} exited {completed.returncode}: {_bounded(completed.stderr)}"
            )
        if completed.stdout.strip():
            raise BlackboxExecutionError(
                f"Blackbox V2 {mode} must not write business output to stdout: "
                f"{_bounded(completed.stdout)}"
            )
        if not output.is_file() or output.is_symlink():
            raise BlackboxExecutionError(f"Blackbox V2 {mode} did not create a regular output file")
        if output.stat().st_size > profile.max_output_bytes:
            raise BlackboxExecutionError(
                f"Blackbox V2 output exceeds {profile.max_output_bytes} bytes"
            )
        return completed
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def run_blackbox_predict(
    *,
    metadata: BlackboxMetadata,
    script_path: str | Path,
    request: BlackboxRequest,
    data_dir: str | Path,
    data_snapshot_id: str,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> PredictionRecord:
    with tempfile.TemporaryDirectory(prefix="blackbox-v2-predict-") as tmpdir:
        root = Path(tmpdir)
        request_path = write_request(request, root / "request.json")
        output_path = root / "output" / "prediction.json"
        execute_blackbox_cli(
            script_path=script_path,
            mode="predict",
            input_path=request_path,
            data_dir=data_dir,
            output_path=output_path,
            profile=profile,
        )
        result = load_prediction_result(output_path, request)
    return _to_prediction_record(metadata, result, data_snapshot_id, profile)


def run_blackbox_backtest(
    *,
    metadata: BlackboxMetadata,
    script_path: str | Path,
    requests: Sequence[BlackboxRequest],
    data_dir: str | Path,
    data_snapshot_id: str,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> list[PredictionRecord]:
    if not requests:
        raise ValueError("Blackbox V2 backtest requires at least one Request")
    if profile.max_batch_requests <= 0:
        raise ValueError("max_batch_requests must be positive")
    request_ids = [request.request_id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Blackbox V2 backtest Request ids must be unique")

    records: list[PredictionRecord] = []
    for batch_index, start in enumerate(range(0, len(requests), profile.max_batch_requests)):
        batch = list(requests[start : start + profile.max_batch_requests])
        with tempfile.TemporaryDirectory(prefix=f"blackbox-v2-backtest-{batch_index:04d}-") as tmpdir:
            root = Path(tmpdir)
            requests_path = write_requests(batch, root / "requests.csv")
            output_path = root / "output" / "backtest.csv"
            execute_blackbox_cli(
                script_path=script_path,
                mode="backtest",
                input_path=requests_path,
                data_dir=data_dir,
                output_path=output_path,
                profile=profile,
            )
            results = load_backtest_results(output_path, batch)
        records.extend(
            _to_prediction_record(metadata, result, data_snapshot_id, profile)
            for result in results
        )
    return records


def _to_prediction_record(
    metadata: BlackboxMetadata,
    result: BlackboxResult,
    data_snapshot_id: str,
    profile: RuntimeProfile,
) -> PredictionRecord:
    return PredictionRecord(
        scheme_id=metadata.scheme_id,
        target_tenor=metadata.target_tenor,
        horizon=metadata.horizon,
        predict_date=result.predict_date,
        feature_date=result.feature_date,
        target_date=result.target_date,
        predicted_direction=result.predicted_direction,
        model_version=metadata.algorithm_version,
        extra={
            "runtime_type": "blackbox_v2",
            "request_id": result.request_id,
            "task_type": metadata.task_type,
            "target_rule": metadata.target_rule,
            "contract_version": metadata.schema_version,
            "runtime_profile": profile.name,
            "data_snapshot_id": data_snapshot_id,
        },
    )


def _validate_data_dir(data_dir: Path) -> None:
    if not data_dir.is_dir() or data_dir.is_symlink():
        raise ValueError(f"Blackbox V2 data-dir must be one regular directory: {data_dir}")
    entries = {path.name for path in data_dir.iterdir()}
    if entries != set(SNAPSHOT_FILENAMES):
        raise ValueError(f"Blackbox V2 data-dir files must be exactly {list(SNAPSHOT_FILENAMES)}")
    for filename in SNAPSHOT_FILENAMES:
        path = data_dir / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Blackbox V2 data file must be regular and not a symlink: {path}")


def _python_command(profile: RuntimeProfile) -> list[str]:
    if profile.conda_env:
        return [_conda_python(profile.conda_env)]
    return [sys.executable]


@lru_cache(maxsize=8)
def _conda_python(conda_env: str) -> str:
    completed = subprocess.run(
        [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            conda_env,
            "python",
            "-c",
            "import sys; print(sys.executable)",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    executable = completed.stdout.strip().splitlines()[-1]
    path = Path(executable)
    if not path.is_file():
        raise RuntimeError(f"conda environment Python executable not found: {path}")
    return str(path)


def _sandbox_command(command: list[str], writable_dir: Path) -> list[str]:
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        raise RuntimeError("runtime profile requires sandbox-exec, but it is unavailable")
    quoted = str(writable_dir).replace('"', '\\"')
    policy = "\n".join(
        [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow file-read*)",
            "(allow mach-lookup)",
            "(allow signal)",
            "(allow sysctl-read)",
            f'(allow file-write* (subpath "{quoted}"))',
            "(deny network*)",
        ]
    )
    return [sandbox_exec, "-p", policy, *command]


def _runtime_environment(profile: RuntimeProfile, writable_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": str(profile.cpu_threads),
            "OPENBLAS_NUM_THREADS": str(profile.cpu_threads),
            "MKL_NUM_THREADS": str(profile.cpu_threads),
            "NUMEXPR_NUM_THREADS": str(profile.cpu_threads),
            "VECLIB_MAXIMUM_THREADS": str(profile.cpu_threads),
            "TMPDIR": str(writable_dir),
            "HOME": str(writable_dir),
            "XDG_CACHE_HOME": str(writable_dir / ".cache"),
            "MPLCONFIGDIR": str(writable_dir / ".matplotlib"),
        }
    )
    return env


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    memory_limit_bytes: int,
    max_capture_bytes: int,
) -> subprocess.CompletedProcess[str]:
    if max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be positive")
    with tempfile.TemporaryFile(mode="w+b", dir=cwd) as stdout_file:
        with tempfile.TemporaryFile(mode="w+b", dir=cwd) as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            failure: str | None = None
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    failure = f"Blackbox V2 process timed out after {timeout}s"
                    break
                captured_bytes = _capture_size(stdout_file) + _capture_size(stderr_file)
                if captured_bytes > max_capture_bytes:
                    failure = (
                        "Blackbox V2 process log output exceeded limit: "
                        f"captured={captured_bytes}, limit={max_capture_bytes}"
                    )
                    break
                rss_bytes = _process_group_rss_bytes(process.pid)
                if memory_limit_bytes > 0 and rss_bytes > memory_limit_bytes:
                    failure = (
                        f"Blackbox V2 process exceeded memory limit: "
                        f"rss={rss_bytes}, limit={memory_limit_bytes}"
                    )
                    break
                time.sleep(0.25)

            captured_bytes = _capture_size(stdout_file) + _capture_size(stderr_file)
            if failure is None and captured_bytes > max_capture_bytes:
                failure = (
                    "Blackbox V2 process log output exceeded limit: "
                    f"captured={captured_bytes}, limit={max_capture_bytes}"
                )
            if failure is not None:
                _terminate_process_group(process)
            stdout = _read_capture(stdout_file, max_capture_bytes)
            stderr = _read_capture(stderr_file, max_capture_bytes)
            if failure is not None:
                raise BlackboxExecutionError(f"{failure}: {_bounded(stderr)}")
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _capture_size(handle) -> int:
    handle.flush()
    return int(os.fstat(handle.fileno()).st_size)


def _read_capture(handle, limit: int) -> str:
    handle.flush()
    handle.seek(0)
    return handle.read(limit).decode("utf-8", errors="replace")


def _process_group_rss_bytes(pid: int) -> int:
    try:
        process_group = os.getpgid(pid)
        completed = subprocess.run(
            ["ps", "-axo", "pgid=,rss="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        return 0
    total_kib = 0
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            row_group, rss_kib = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if row_group == process_group:
            total_kib += rss_kib
    return total_kib * 1024


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group, signal.SIGKILL)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            return


def _bounded(value: str, limit: int = 4000) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
