from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
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


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_PROFILE_PATH = _PROJECT_ROOT / "deploy" / "blackbox_v2" / "runtime_profile_v1.json"
_SAFE_ENVIRONMENT_KEYS = frozenset({"LANG", "LC_ALL", "TZ"})
_PROFILE_FIELDS = frozenset(
    {
        "profile_name",
        "conda_env",
        "data_schema_version",
        "contract_version",
        "cpu_threads",
        "memory_limit_bytes",
        "predict_timeout_sec",
        "backtest_timeout_sec",
        "max_batch_requests",
        "max_output_bytes",
        "max_log_bytes",
        "max_run_dir_bytes",
        "max_run_dir_entries",
        "sandbox_enabled",
        "read_roots",
        "environment_allowlist",
        "environment_defaults",
        "network_access",
        "database_access",
    }
)
_UNSAFE_PROFILE_ROOTS = frozenset(
    {
        Path("/"),
        Path("/Users"),
        Path.home().resolve(),
        _PROJECT_ROOT,
        Path("/etc"),
        Path("/private"),
        Path("/private/etc"),
        Path("/usr"),
        Path("/usr/share"),
        Path("/System"),
        Path("/Library"),
        Path("/opt"),
        Path("/opt/homebrew"),
        Path("/opt/homebrew/Cellar"),
    }
)


@dataclass(frozen=True)
class RuntimeReadRoot:
    path: str
    resolved_boundary: str


@dataclass
class BacktestExecutionBudget:
    """跨分批调用共享的总 deadline 与子进程数量预算。"""

    deadline_monotonic: float
    max_subprocesses: int
    subprocesses_started: int = 0

    def claim_subprocess(self) -> float:
        if self.max_subprocesses <= 0:
            raise ValueError("backtest max_subprocesses must be positive")
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise BlackboxExecutionError("Blackbox V2 backtest total deadline exceeded")
        if self.subprocesses_started >= self.max_subprocesses:
            raise BlackboxExecutionError(
                "Blackbox V2 backtest subprocess limit exceeded: "
                f"limit={self.max_subprocesses}"
            )
        self.subprocesses_started += 1
        return remaining


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    conda_env: str | None
    data_schema_version: str
    contract_version: str
    cpu_threads: int
    memory_limit_bytes: int
    predict_timeout_sec: int
    backtest_timeout_sec: int
    max_batch_requests: int
    max_output_bytes: int
    max_log_bytes: int
    max_run_dir_bytes: int
    max_run_dir_entries: int
    sandbox_enabled: bool
    read_roots: tuple[RuntimeReadRoot, ...]
    environment_allowlist: tuple[str, ...]
    environment_defaults: tuple[tuple[str, str], ...]
    network_access: bool
    database_access: bool

    @classmethod
    def for_tests(cls, **overrides) -> RuntimeProfile:
        changes = {"conda_env": None, "sandbox_enabled": False, **overrides}
        profile = replace(DEFAULT_RUNTIME_PROFILE, **changes)
        _validate_runtime_profile(profile, source="test runtime profile")
        return profile


@dataclass(frozen=True)
class _ResolvedReadRoot:
    configured: Path
    resolved: Path
    boundary: Path


@dataclass(frozen=True)
class _PythonRuntime:
    executable: Path
    prefix: Path


def _load_runtime_profile(path: str | Path) -> RuntimeProfile:
    profile_path = Path(path)
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Blackbox V2 runtime profile is invalid: {profile_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Blackbox V2 runtime profile must be a JSON object")
    fields = set(raw)
    unknown = sorted(fields - _PROFILE_FIELDS)
    missing = sorted(_PROFILE_FIELDS - fields)
    if unknown:
        raise ValueError(f"Blackbox V2 runtime profile has unknown fields: {unknown}")
    if missing:
        raise ValueError(f"Blackbox V2 runtime profile has missing fields: {missing}")

    string_fields = ("profile_name", "conda_env", "data_schema_version", "contract_version")
    for field in string_fields:
        if not isinstance(raw[field], str) or not raw[field]:
            raise ValueError(f"Blackbox V2 runtime profile field {field} has wrong type")
    integer_fields = (
        "cpu_threads",
        "memory_limit_bytes",
        "predict_timeout_sec",
        "backtest_timeout_sec",
        "max_batch_requests",
        "max_output_bytes",
        "max_log_bytes",
        "max_run_dir_bytes",
        "max_run_dir_entries",
    )
    for field in integer_fields:
        if type(raw[field]) is not int:
            raise ValueError(f"Blackbox V2 runtime profile field {field} has wrong type")
        if raw[field] <= 0:
            raise ValueError(f"Blackbox V2 runtime profile field {field} must be positive")
    for field in ("sandbox_enabled", "network_access", "database_access"):
        if type(raw[field]) is not bool:
            raise ValueError(f"Blackbox V2 runtime profile field {field} has wrong type")
    if not raw["sandbox_enabled"]:
        raise ValueError("Blackbox V2 runtime profile must enable sandboxing")
    if raw["network_access"] or raw["database_access"]:
        raise ValueError("Blackbox V2 runtime profile must deny network and database access")
    if raw["max_output_bytes"] > raw["max_run_dir_bytes"]:
        raise ValueError("Blackbox V2 max_output_bytes must not exceed max_run_dir_bytes")

    read_roots_raw = raw["read_roots"]
    if not isinstance(read_roots_raw, list) or not read_roots_raw:
        raise ValueError("Blackbox V2 runtime profile read_roots has wrong type")
    read_roots: list[RuntimeReadRoot] = []
    for index, item in enumerate(read_roots_raw):
        if not isinstance(item, dict) or set(item) != {"path", "resolved_boundary"}:
            raise ValueError(f"Blackbox V2 runtime profile read_roots[{index}] has wrong type")
        if any(not isinstance(item[key], str) or not item[key] for key in item):
            raise ValueError(f"Blackbox V2 runtime profile read_roots[{index}] has wrong type")
        configured = Path(item["path"])
        boundary = Path(item["resolved_boundary"])
        if not configured.is_absolute() or not boundary.is_absolute():
            raise ValueError(f"Blackbox V2 runtime profile read_roots[{index}] is unsafe")
        if configured in _UNSAFE_PROFILE_ROOTS or boundary in _UNSAFE_PROFILE_ROOTS:
            raise ValueError(f"Blackbox V2 runtime profile read_roots[{index}] is unsafe")
        read_roots.append(RuntimeReadRoot(str(configured), str(boundary)))

    allowlist_raw = raw["environment_allowlist"]
    if (
        not isinstance(allowlist_raw, list)
        or any(not isinstance(key, str) or not key for key in allowlist_raw)
        or len(allowlist_raw) != len(set(allowlist_raw))
    ):
        raise ValueError("Blackbox V2 runtime profile environment_allowlist has wrong type")
    if not set(allowlist_raw) <= _SAFE_ENVIRONMENT_KEYS:
        raise ValueError("Blackbox V2 runtime profile environment_allowlist contains unsafe keys")
    defaults_raw = raw["environment_defaults"]
    if not isinstance(defaults_raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in defaults_raw.items()
    ):
        raise ValueError("Blackbox V2 runtime profile environment_defaults has wrong type")
    if not set(defaults_raw) <= set(allowlist_raw):
        raise ValueError("Blackbox V2 environment_defaults keys must be explicitly allowlisted")

    profile = RuntimeProfile(
        name=raw["profile_name"],
        conda_env=raw["conda_env"],
        data_schema_version=raw["data_schema_version"],
        contract_version=raw["contract_version"],
        cpu_threads=raw["cpu_threads"],
        memory_limit_bytes=raw["memory_limit_bytes"],
        predict_timeout_sec=raw["predict_timeout_sec"],
        backtest_timeout_sec=raw["backtest_timeout_sec"],
        max_batch_requests=raw["max_batch_requests"],
        max_output_bytes=raw["max_output_bytes"],
        max_log_bytes=raw["max_log_bytes"],
        max_run_dir_bytes=raw["max_run_dir_bytes"],
        max_run_dir_entries=raw["max_run_dir_entries"],
        sandbox_enabled=raw["sandbox_enabled"],
        read_roots=tuple(read_roots),
        environment_allowlist=tuple(allowlist_raw),
        environment_defaults=tuple(defaults_raw.items()),
        network_access=raw["network_access"],
        database_access=raw["database_access"],
    )
    _validate_runtime_profile(profile, source=str(profile_path))
    return profile


def _validate_runtime_profile(profile: RuntimeProfile, *, source: str) -> None:
    integer_fields = (
        "cpu_threads",
        "memory_limit_bytes",
        "predict_timeout_sec",
        "backtest_timeout_sec",
        "max_batch_requests",
        "max_output_bytes",
        "max_log_bytes",
        "max_run_dir_bytes",
        "max_run_dir_entries",
    )
    for field in integer_fields:
        value = getattr(profile, field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"Blackbox V2 {source} field {field} must be a positive integer")
    if profile.max_output_bytes > profile.max_run_dir_bytes:
        raise ValueError(f"Blackbox V2 {source} max_output_bytes exceeds max_run_dir_bytes")
    if type(profile.network_access) is not bool or type(profile.database_access) is not bool:
        raise ValueError(f"Blackbox V2 {source} access flags have wrong type")
    if profile.network_access or profile.database_access:
        raise ValueError(f"Blackbox V2 {source} must deny network and database access")
    if type(profile.sandbox_enabled) is not bool:
        raise ValueError(f"Blackbox V2 {source} sandbox_enabled has wrong type")
    if profile.conda_env is not None and (
        not isinstance(profile.conda_env, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", profile.conda_env)
    ):
        raise ValueError(f"Blackbox V2 {source} conda_env is unsafe")
    if any(
        not isinstance(value, str) or not value
        for value in (profile.name, profile.data_schema_version, profile.contract_version)
    ):
        raise ValueError(f"Blackbox V2 {source} has an empty version field")
    if not profile.read_roots or any(not isinstance(item, RuntimeReadRoot) for item in profile.read_roots):
        raise ValueError(f"Blackbox V2 {source} read_roots has wrong type")
    for item in profile.read_roots:
        configured = Path(item.path)
        boundary = Path(item.resolved_boundary)
        if (
            not configured.is_absolute()
            or not boundary.is_absolute()
            or configured in _UNSAFE_PROFILE_ROOTS
            or boundary in _UNSAFE_PROFILE_ROOTS
            or any(character in item.path + item.resolved_boundary for character in "\n\r\0")
        ):
            raise ValueError(f"Blackbox V2 {source} read_roots contains an unsafe path")
    if (
        any(not isinstance(key, str) or not key for key in profile.environment_allowlist)
        or len(profile.environment_allowlist) != len(set(profile.environment_allowlist))
        or not set(profile.environment_allowlist) <= _SAFE_ENVIRONMENT_KEYS
    ):
        raise ValueError(f"Blackbox V2 {source} environment_allowlist contains unsafe keys")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in profile.environment_defaults
    ):
        raise ValueError(f"Blackbox V2 {source} environment_defaults has wrong type")
    defaults = dict(profile.environment_defaults)
    if len(defaults) != len(profile.environment_defaults):
        raise ValueError(f"Blackbox V2 {source} environment_defaults contains duplicate keys")
    if not set(defaults) <= set(profile.environment_allowlist):
        raise ValueError(f"Blackbox V2 {source} environment_defaults is not allowlisted")


DEFAULT_RUNTIME_PROFILE = _load_runtime_profile(_RUNTIME_PROFILE_PATH)


class BlackboxExecutionError(RuntimeError):
    pass


def probe_blackbox_help(
    script_path: str | Path,
    *,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> str:
    """验证交付脚本公开 predict 和 backtest 两种 CLI 模式。"""
    _validate_runtime_profile(profile, source="runtime profile")
    script = _resolve_controlled_path(script_path, label="script")
    if not script.is_file() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")
    runtime = _python_runtime(profile)
    read_roots = _resolve_runtime_read_roots(profile)

    with tempfile.TemporaryDirectory(prefix="blackbox-v2-help-") as tmpdir:
        writable_dir = Path(tmpdir).resolve()
        python_executable = str(runtime.executable)
        command = [python_executable, str(script), "--help"]
        if profile.sandbox_enabled:
            command = _sandbox_command(
                command,
                writable_dir,
                profile=profile,
                script_path=script,
                runtime=runtime,
                resolved_read_roots=read_roots,
            )
        else:
            command = _bootstrap_command(command, profile.max_run_dir_bytes)
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
            max_run_dir_bytes=profile.max_run_dir_bytes,
            max_run_dir_entries=profile.max_run_dir_entries,
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
    timeout_sec: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行一次 Blackbox V2 CLI；仅进程成功且 Output 合法存在才返回。"""
    _validate_runtime_profile(profile, source="runtime profile")
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
    runtime = _python_runtime(profile)
    read_roots = _resolve_runtime_read_roots(profile)
    data_files = tuple(data / filename for filename in SNAPSHOT_FILENAMES)
    _prepare_output_directory(
        output.parent,
        controls=(
            ("script", script),
            ("input", input_file),
            ("data-dir", data),
            *((f"data file {path.name}", path) for path in data_files),
            ("runtime prefix", runtime.prefix),
            *(("runtime read root", item.resolved) for item in read_roots),
        ),
    )

    try:
        input_flag = "--request" if mode == "predict" else "--requests"
        python_executable = str(runtime.executable)
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
                runtime=runtime,
                resolved_read_roots=read_roots,
            )
        else:
            command = _bootstrap_command(command, profile.max_run_dir_bytes)
        env = _runtime_environment(
            profile,
            output.parent,
            python_executable=python_executable,
        )
        profile_timeout = (
            profile.predict_timeout_sec if mode == "predict" else profile.backtest_timeout_sec
        )
        timeout = (
            min(float(profile_timeout), float(timeout_sec))
            if timeout_sec is not None
            else float(profile_timeout)
        )
        if timeout <= 0:
            raise BlackboxExecutionError(f"Blackbox V2 {mode} deadline expired before process start")
        completed = _run_process(
            command,
            cwd=output.parent,
            env=env,
            timeout=timeout,
            memory_limit_bytes=profile.memory_limit_bytes,
            max_capture_bytes=profile.max_log_bytes,
            max_run_dir_bytes=profile.max_run_dir_bytes,
            max_run_dir_entries=profile.max_run_dir_entries,
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
    except BaseException as execution_error:
        try:
            _cleanup_run_directory(output.parent)
        except Exception as cleanup_error:
            raise BlackboxExecutionError(
                f"{execution_error}; cleanup failed for {output.parent}: {cleanup_error}"
            ) from execution_error
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
    budget: BacktestExecutionBudget | None = None,
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
            remaining_timeout = budget.claim_subprocess() if budget is not None else None
            try:
                execute_blackbox_cli(
                    script_path=script_path,
                    mode="backtest",
                    input_path=requests_path,
                    data_dir=data_dir,
                    output_path=output_path,
                    profile=profile,
                    timeout_sec=remaining_timeout,
                )
            except BlackboxExecutionError as exc:
                if budget is not None and time.monotonic() >= budget.deadline_monotonic:
                    raise BlackboxExecutionError(
                        f"Blackbox V2 backtest total deadline exceeded: {exc}"
                    ) from exc
                raise
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


def _prepare_output_directory(
    output_dir: Path,
    *,
    controls: Sequence[tuple[str, Path]],
) -> None:
    for label, control in controls:
        if _paths_overlap(output_dir, control):
            raise ValueError(
                f"Blackbox V2 output directory must not overlap {label}: "
                f"{output_dir} vs {control}"
            )
    if output_dir.exists():
        if not output_dir.is_dir() or output_dir.is_symlink():
            raise ValueError(f"Blackbox V2 output parent must be a fresh empty directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ValueError(f"Blackbox V2 output parent must be a fresh empty directory: {output_dir}")
        return
    if not output_dir.parent.is_dir():
        raise ValueError(
            f"Blackbox V2 output parent must have an existing parent directory: {output_dir}"
        )
    output_dir.mkdir(mode=0o700)


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=True)
    if left.exists():
        try:
            if os.path.samefile(left, right):
                return True
        except OSError:
            pass
    return _path_contains(left, right) or _path_contains(right, left)


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _cleanup_run_directory(run_dir: Path) -> None:
    if not os.path.lexists(run_dir):
        return
    _clear_user_file_flags(run_dir)
    if not run_dir.is_dir() or run_dir.is_symlink():
        run_dir.unlink(missing_ok=True)
        if os.path.lexists(run_dir):
            raise OSError(f"Blackbox V2 cleanup left artifact: {run_dir}")
        return
    for current, directories, files in os.walk(run_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            _clear_user_file_flags(current_path / name)
        for name in files:
            _clear_user_file_flags(current_path / name)
    for current, directories, files in os.walk(run_dir, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in files:
            child = current_path / filename
            if not child.is_symlink():
                child.chmod(0o600, follow_symlinks=False)
        for directory in directories:
            child = current_path / directory
            if child.is_symlink():
                continue
            child.chmod(0o700)
    run_dir.chmod(0o700)
    shutil.rmtree(run_dir)
    if os.path.lexists(run_dir):
        raise OSError(f"Blackbox V2 cleanup left run directory: {run_dir}")


def _clear_user_file_flags(path: Path) -> None:
    if not hasattr(os, "chflags"):
        return
    flags = getattr(os.lstat(path), "st_flags", 0)
    if flags:
        os.chflags(path, 0, follow_symlinks=False)


def _resolve_runtime_read_roots(profile: RuntimeProfile) -> tuple[_ResolvedReadRoot, ...]:
    resolved_roots: list[_ResolvedReadRoot] = []
    for item in profile.read_roots:
        configured = Path(os.path.abspath(item.path))
        boundary_path = Path(os.path.abspath(item.resolved_boundary))
        try:
            boundary = boundary_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(
                f"Blackbox V2 runtime read boundary does not exist: {boundary_path}"
            ) from exc
        if not boundary.is_dir():
            raise ValueError(f"Blackbox V2 runtime read boundary is not a directory: {boundary}")
        if boundary != boundary_path:
            raise ValueError(
                f"Blackbox V2 runtime read boundary must already be resolved: {boundary_path}"
            )
        try:
            resolved = configured.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Blackbox V2 runtime read root does not exist: {configured}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Blackbox V2 runtime read root is not a directory: {resolved}")
        if not _path_contains(boundary, resolved):
            raise ValueError(
                f"Blackbox V2 runtime read root escapes approved boundary: "
                f"{configured} -> {resolved}, boundary={boundary}"
            )
        resolved_roots.append(_ResolvedReadRoot(configured, resolved, boundary))
    return tuple(resolved_roots)


def _python_command(profile: RuntimeProfile) -> list[str]:
    return [str(_python_runtime(profile).executable)]


@lru_cache(maxsize=8)
def _conda_runtime_info(conda_env: str) -> tuple[str, str]:
    conda_executable = shutil.which("conda")
    if conda_executable is None:
        raise ValueError("Blackbox V2 Python runtime requires conda, but it is unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="blackbox-v2-conda-") as tmpdir:
            conda_bin = str(Path(conda_executable).resolve().parent)
            completed = subprocess.run(
                [
                    conda_executable,
                    "run",
                    "--no-capture-output",
                    "-n",
                    conda_env,
                    "python",
                    "-I",
                    "-c",
                    "import json,sys; print(json.dumps([sys.executable, sys.prefix]))",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    "HOME": tmpdir,
                    "TMPDIR": tmpdir,
                    "LANG": "C.UTF-8",
                    "PATH": f"{conda_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "PYTHONNOUSERSITE": "1",
                },
            )
        executable, prefix = json.loads(completed.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Blackbox V2 Python runtime could not resolve conda environment {conda_env}: {exc}"
        ) from exc
    if not isinstance(executable, str) or not isinstance(prefix, str):
        raise ValueError(
            f"Blackbox V2 Python runtime returned invalid paths for conda environment {conda_env}"
        )
    return executable, prefix


def _python_runtime(profile: RuntimeProfile) -> _PythonRuntime:
    if profile.conda_env:
        executable, prefix = _conda_runtime_info(profile.conda_env)
        return _validate_python_runtime(executable, prefix, profile.conda_env)
    return _validate_python_runtime(sys.executable, sys.prefix, None)


def _validate_python_runtime(
    executable: str | Path,
    prefix: str | Path,
    requested_env: str | None,
) -> _PythonRuntime:
    executable_path = Path(executable)
    prefix_path = Path(prefix)
    try:
        resolved_executable = executable_path.resolve(strict=True)
        resolved_prefix = prefix_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Blackbox V2 Python runtime path does not exist: {exc.filename}") from exc
    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise ValueError(
            f"Blackbox V2 Python runtime executable is not an executable file: {resolved_executable}"
        )
    if not resolved_prefix.is_dir():
        raise ValueError(f"Blackbox V2 Python runtime prefix is not a directory: {resolved_prefix}")
    unsafe_prefixes = {Path("/"), Path("/usr"), Path.home().resolve()}
    if resolved_prefix in unsafe_prefixes:
        raise ValueError(f"Blackbox V2 Python runtime prefix is unsafe: {resolved_prefix}")
    if not _path_contains(resolved_prefix, resolved_executable):
        raise ValueError(
            f"Blackbox V2 Python runtime executable escapes prefix: "
            f"{resolved_executable} vs {resolved_prefix}"
        )
    if requested_env is not None:
        if resolved_prefix.parent.name != "envs" or resolved_prefix.name != requested_env:
            raise ValueError(
                f"Blackbox V2 Python runtime prefix does not match requested conda env "
                f"{requested_env}: {resolved_prefix}"
            )
    return _PythonRuntime(resolved_executable, resolved_prefix)


_SANDBOX_BOOTSTRAP = r'''
import ctypes
import resource
import runpy
import sys

policy = sys.argv[1]
max_file_bytes = int(sys.argv[2])
script_argv = sys.argv[3:]
if policy:
    error = ctypes.c_char_p()
    library = ctypes.CDLL("/usr/lib/libsandbox.1.dylib")
    library.sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    library.sandbox_init.restype = ctypes.c_int
    if library.sandbox_init(policy.encode("utf-8"), 0, ctypes.byref(error)) != 0:
        message = error.value.decode("utf-8", errors="replace") if error.value else "unknown error"
        raise RuntimeError(f"sandbox_init failed: {message}")
if hasattr(resource, "RLIMIT_FSIZE"):
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
sys.argv = script_argv
runpy.run_path(script_argv[0], run_name="__main__")
'''


def _bootstrap_command(
    command: list[str],
    max_file_bytes: int,
    *,
    policy: str = "",
) -> list[str]:
    return [
        command[0],
        "-I",
        "-c",
        _SANDBOX_BOOTSTRAP,
        policy,
        str(max_file_bytes),
        *command[1:],
    ]


def _sandbox_command(
    command: list[str],
    writable_dir: Path,
    *,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
    script_path: Path | None = None,
    input_path: Path | None = None,
    data_dir: Path | None = None,
    runtime: _PythonRuntime | None = None,
    resolved_read_roots: Sequence[_ResolvedReadRoot] | None = None,
) -> list[str]:
    if shutil.which("sandbox-exec") is None:
        raise RuntimeError("runtime profile requires sandbox-exec, but it is unavailable")
    writable = writable_dir.resolve(strict=True)
    selected_runtime = runtime or _python_runtime(profile)
    executable = Path(command[0]).resolve(strict=True)
    if executable != selected_runtime.executable:
        raise ValueError(
            f"Blackbox V2 command executable does not match selected Python runtime: {executable}"
        )
    runtime_roots = tuple(resolved_read_roots or _resolve_runtime_read_roots(profile))
    policy = _sandbox_policy(
        writable=writable,
        runtime=selected_runtime,
        read_roots=runtime_roots,
        script_path=script_path,
        input_path=input_path,
        data_dir=data_dir,
    )
    selected_command = [str(selected_runtime.executable), *command[1:]]
    return _bootstrap_command(selected_command, profile.max_run_dir_bytes, policy=policy)


def _sandbox_policy(
    *,
    writable: Path,
    runtime: _PythonRuntime,
    read_roots: Sequence[_ResolvedReadRoot],
    script_path: Path | None,
    input_path: Path | None,
    data_dir: Path | None,
) -> str:
    runtime_directories = [runtime.prefix, *(item.resolved for item in read_roots)]
    read_aliases: list[Path] = []
    for item in read_roots:
        if item.configured != item.resolved:
            read_aliases.append(item.configured)
            read_aliases.extend(
                candidate for candidate in item.configured.parents if candidate.is_symlink()
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

    executable_filters = _sandbox_path_filters(
        runtime_directories,
        include_children=True,
    )
    metadata_filters = _sandbox_metadata_filters(
        [*read_directories, *read_files, *read_aliases, runtime.executable]
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
    denied_write_files = [path for path in (script_path, input_path) if path is not None]
    denied_write_directories = [*runtime_directories, *read_aliases]
    if resolved_data_dir is not None:
        denied_write_directories.append(resolved_data_dir)
    policy = "\n".join(
        [
            "(version 1)",
            "(deny default)",
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
            "(deny file-write*",
            *(
                f"  {item}"
                for item in _sandbox_path_filters(
                    denied_write_directories,
                    include_children=True,
                )
            ),
            *(
                f"  {item}"
                for item in _sandbox_path_filters(
                    denied_write_files,
                    include_children=False,
                )
            ),
            ")",
            "(deny network*)",
        ]
    )
    return policy


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
    timeout: float,
    memory_limit_bytes: int,
    max_capture_bytes: int,
    max_run_dir_bytes: int,
    max_run_dir_entries: int,
) -> subprocess.CompletedProcess[str]:
    if max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be positive")
    if max_run_dir_bytes <= 0:
        raise ValueError("max_run_dir_bytes must be positive")
    if max_run_dir_entries <= 0:
        raise ValueError("max_run_dir_entries must be positive")
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
                try:
                    quota_failure = _run_directory_limit_failure(
                        cwd,
                        max_bytes=max_run_dir_bytes,
                        max_entries=max_run_dir_entries,
                    )
                except OSError as exc:
                    failure = f"Blackbox V2 could not inspect run directory quota: {exc}"
                    break
                if quota_failure is not None:
                    failure = quota_failure
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
            if failure is None:
                try:
                    quota_failure = _run_directory_limit_failure(
                        cwd,
                        max_bytes=max_run_dir_bytes,
                        max_entries=max_run_dir_entries,
                    )
                except OSError as exc:
                    failure = f"Blackbox V2 could not inspect run directory quota: {exc}"
                else:
                    if quota_failure is not None:
                        failure = quota_failure
            if failure is not None:
                _terminate_process_group(process)
            stdout = _read_capture(stdout_file, max_capture_bytes)
            stderr = _read_capture(stderr_file, max_capture_bytes)
            if failure is not None:
                raise BlackboxExecutionError(f"{failure}: {_bounded(stderr)}")
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_directory_limit_failure(
    root: Path,
    *,
    max_bytes: int,
    max_entries: int,
) -> str | None:
    entry_count = 0
    allocated_bytes = 0
    logical_bytes = 0
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > max_entries:
                    return (
                        "Blackbox V2 run directory entry limit exceeded: "
                        f"entries={entry_count}, limit={max_entries}"
                    )
                file_stat = entry.stat(follow_symlinks=False)
                allocated_bytes += max(0, getattr(file_stat, "st_blocks", 0)) * 512
                logical_bytes += max(0, file_stat.st_size)
                if allocated_bytes >= max_bytes:
                    return (
                        "Blackbox V2 run directory allocated byte limit exceeded: "
                        f"bytes={allocated_bytes}, limit={max_bytes}"
                    )
                if logical_bytes >= max_bytes:
                    return (
                        "Blackbox V2 run directory logical byte limit exceeded: "
                        f"bytes={logical_bytes}, limit={max_bytes}"
                    )
                if stat.S_ISDIR(file_stat.st_mode):
                    # Count before queuing children so traversal memory is capped by the entry quota.
                    pending.append(Path(entry.path))
    return None


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
            ["/bin/ps", "-axo", "pgid=,rss="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={"LANG": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
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
