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
    read_roots: tuple[str, ...] = (
        "/opt/homebrew/opt/libomp/lib",
    )
    environment_allowlist: tuple[str, ...] = ("LANG", "LC_ALL", "TZ")
    environment_defaults: tuple[tuple[str, str], ...] = (
        ("LANG", "C.UTF-8"),
        ("TZ", "Asia/Shanghai"),
    )

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
    script = _resolve_controlled_path(script_path, label="script")
    if not script.is_file() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")

    with tempfile.TemporaryDirectory(prefix="blackbox-v2-help-") as tmpdir:
        writable_dir = Path(tmpdir).resolve()
        python_executable = _python_command(profile)[0]
        command = [python_executable, str(script), "--help"]
        if profile.sandbox_enabled:
            command = _sandbox_command(
                command,
                writable_dir,
                profile=profile,
                script_path=script,
            )
        completed = _run_process(
            command,
            cwd=writable_dir,
            env=_runtime_environment(
                profile,
                writable_dir,
                python_executable=python_executable,
            ),
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
    script = _resolve_controlled_path(script_path, label="script")
    input_file = _resolve_controlled_path(input_path, label="input")
    data = _resolve_controlled_path(data_dir, label="data-dir")
    output = _resolve_controlled_path(output_path, label="output", allow_missing=True)
    if not script.is_file() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")
    if not input_file.is_file():
        raise ValueError(f"Blackbox V2 input must be one regular file: {input_file}")
    _validate_data_dir(data)
    if output.exists():
        raise ValueError(f"platform must provide a fresh output path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    input_flag = "--request" if mode == "predict" else "--requests"
    python_executable = _python_command(profile)[0]
    command = [
        python_executable,
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
        command = _sandbox_command(
            command,
            output.parent,
            profile=profile,
            script_path=script,
            input_path=input_file,
            data_dir=data,
        )
    env = _runtime_environment(
        profile,
        output.parent,
        python_executable=python_executable,
    )
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


_SYSTEM_SYMLINK_ALIASES = {
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


def _resolve_controlled_path(
    path: str | Path,
    *,
    label: str,
    allow_missing: bool = False,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for candidate in (absolute, *absolute.parents):
        if not candidate.is_symlink():
            continue
        allowed_target = _SYSTEM_SYMLINK_ALIASES.get(candidate)
        if allowed_target is not None and candidate.resolve(strict=True) == allowed_target:
            continue
        raise ValueError(f"Blackbox V2 {label} path must not traverse a symlink: {candidate}")
    try:
        return absolute.resolve(strict=not allow_missing)
    except FileNotFoundError as exc:
        raise ValueError(f"Blackbox V2 {label} path does not exist: {absolute}") from exc


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


def _sandbox_command(
    command: list[str],
    writable_dir: Path,
    *,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
    script_path: Path | None = None,
    input_path: Path | None = None,
    data_dir: Path | None = None,
) -> list[str]:
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        raise RuntimeError("runtime profile requires sandbox-exec, but it is unavailable")

    writable = writable_dir.resolve(strict=True)
    executable = Path(command[0]).resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError(f"Blackbox V2 Python executable is not a regular file: {executable}")
    python_prefix = executable.parent.parent
    runtime_directories = [python_prefix]
    read_aliases: list[Path] = []
    for configured_root in profile.read_roots:
        configured = Path(os.path.abspath(configured_root))
        root = configured.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError(f"Blackbox V2 runtime read root is not a directory: {root}")
        runtime_directories.append(root)
        if configured != root:
            read_aliases.append(configured)
            read_aliases.extend(
                candidate for candidate in configured.parents if candidate.is_symlink()
            )

    read_files = [path.resolve(strict=True) for path in (script_path, input_path) if path]
    resolved_data_dir: Path | None = None
    if data_dir is not None:
        resolved_data_dir = data_dir.resolve(strict=True)
        read_files.extend(resolved_data_dir / filename for filename in SNAPSHOT_FILENAMES)

    read_directories = [*runtime_directories, writable]
    read_filters = _sandbox_path_filters(read_directories, include_children=True)
    read_filters.extend(_sandbox_path_filters(read_files, include_children=False))
    if resolved_data_dir is not None:
        read_filters.extend(_sandbox_path_filters([resolved_data_dir], include_children=False))

    process_filters = _sandbox_path_filters(
        [Path(os.path.abspath(command[0])), executable],
        include_children=False,
    )
    executable_filters = _sandbox_path_filters(
        runtime_directories,
        include_children=True,
    )
    metadata_filters = _sandbox_metadata_filters(
        [*read_directories, *read_files, *read_aliases, executable]
        + ([resolved_data_dir] if resolved_data_dir is not None else [])
    )
    alias_filters = _sandbox_path_filters(read_aliases, include_children=True)
    device_filters = _sandbox_path_filters(
        [
            Path(path)
            for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
        ],
        include_children=False,
    )
    write_filters = _sandbox_path_filters([writable], include_children=True)
    policy = "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec",
            *(f"  {item}" for item in process_filters),
            ")",
            "(allow syscall-unix",
            "  (syscall-number SYS___mac_syscall)",
            "  (syscall-number SYS_getfsstat SYS_getfsstat64)",
            "  (syscall-number SYS_map_with_linking_np)",
            "  (syscall-number SYS_open SYS_openat)",
            "  (syscall-number SYS_fstatat SYS_fstatat64)",
            "  (syscall-number SYS_dup)",
            ")",
            "(allow sysctl-read",
            '  (sysctl-name "kern.ostype" "kern.hostname" "kern.osrelease"',
            '               "kern.version" "hw.machine" "hw.ncpu")',
            ")",
            "(allow system-fcntl",
            "  (fcntl-command F_ADDFILESIGS_RETURN F_CHECK_LV F_GETPATH)",
            ")",
            '(with-filter (mac-policy-name "Sandbox")',
            "  (allow system-mac-syscall (mac-syscall-number 2))",
            ")",
            '(allow file-read-data file-test-existence (literal "/"))',
            "(allow file-read* file-test-existence",
            *(f"  {item}" for item in read_filters),
            ")",
            "(allow file-map-executable",
            *(f"  {item}" for item in executable_filters),
            ")",
            "(allow file-read-metadata file-test-existence",
            *(f"  {item}" for item in metadata_filters),
            *(f"  {item}" for item in alias_filters),
            ")",
            "(allow file-read* file-test-existence",
            *(f"  {item}" for item in device_filters),
            ")",
            "(allow file-write-data",
            *(
                f"  {item}"
                for item in _sandbox_path_filters(
                    [Path("/dev/null"), Path("/dev/zero")],
                    include_children=False,
                )
            ),
            ")",
            "(allow file-write*",
            *(f"  {item}" for item in write_filters),
            ")",
            *(
                [
                    "(deny file-write*",
                    *(
                        f"  {item}"
                        for item in _sandbox_path_filters(
                            [resolved_data_dir],
                            include_children=True,
                        )
                    ),
                    ")",
                ]
                if resolved_data_dir is not None
                else []
            ),
            "(deny network*)",
        ]
    )
    return [sandbox_exec, "-p", policy, *command]


def _sandbox_path_filters(paths: Sequence[Path], *, include_children: bool) -> list[str]:
    filters: list[str] = []
    for path in dict.fromkeys(paths):
        quoted = _sandbox_quote(path)
        filters.append(f'(literal "{quoted}")')
        if include_children:
            filters.append(f'(subpath "{quoted}")')
    return filters


def _sandbox_metadata_filters(paths: Sequence[Path]) -> list[str]:
    metadata_paths: list[Path] = []
    for path in paths:
        metadata_paths.extend((path, *path.parents))
    return _sandbox_path_filters(metadata_paths, include_children=False)


def _sandbox_quote(path: Path) -> str:
    value = str(path)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError(f"Blackbox V2 sandbox path contains a control character: {path!r}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _runtime_environment(
    profile: RuntimeProfile,
    writable_dir: Path,
    *,
    python_executable: str | Path | None = None,
) -> dict[str, str]:
    allowed = set(profile.environment_allowlist)
    defaults = dict(profile.environment_defaults)
    unexpected_defaults = defaults.keys() - allowed
    if unexpected_defaults:
        raise ValueError(
            "environment_defaults keys must be explicitly allowlisted: "
            f"{sorted(unexpected_defaults)}"
        )

    env = defaults
    for key in profile.environment_allowlist:
        if key in os.environ:
            env[key] = os.environ[key]

    executable = Path(python_executable or _python_command(profile)[0]).resolve(strict=True)
    run_dir = str(writable_dir)
    env.update(
        {
            "PATH": str(executable.parent),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": str(profile.cpu_threads),
            "OPENBLAS_NUM_THREADS": str(profile.cpu_threads),
            "MKL_NUM_THREADS": str(profile.cpu_threads),
            "NUMEXPR_NUM_THREADS": str(profile.cpu_threads),
            "VECLIB_MAXIMUM_THREADS": str(profile.cpu_threads),
            "TMPDIR": run_dir,
            "HOME": run_dir,
            "XDG_CACHE_HOME": str(writable_dir / ".cache"),
            "XDG_CONFIG_HOME": str(writable_dir / ".config"),
            "XDG_DATA_HOME": str(writable_dir / ".local" / "share"),
            "MPLCONFIGDIR": str(writable_dir / ".matplotlib"),
            "NUMBA_CACHE_DIR": str(writable_dir / ".cache" / "numba"),
            "JOBLIB_TEMP_FOLDER": str(writable_dir / ".tmp" / "joblib"),
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
