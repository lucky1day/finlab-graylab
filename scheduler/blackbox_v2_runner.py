from __future__ import annotations

import csv
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Sequence

from scheduler.process_control import (
    ProcessGroupTerminationError,
    ProcessRegistrationCleanupError,
    ProcessStartGuard,
    capture_new_session_process_group,
    require_process_start_guard,
    terminate_process_group as _terminate_process_group,
)
from shared.blackbox_v2.contracts import (
    BlackboxMetadata,
    BlackboxRequest,
    BlackboxResult,
    load_backtest_results,
    load_prediction_result,
    load_request,
    load_requests,
)
from shared.blackbox_v2.platform_input_registry import (
    PLATFORM_INPUT_REGISTRY,
)
from shared.blackbox_v2.requests import write_request, write_requests
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    compute_blackbox_combined_snapshot_id,
)
from shared.models import PredictionRecord


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_PROFILE_PATH = _PROJECT_ROOT / "deploy" / "blackbox_v2" / "runtime_profile_v1.json"
_SAFE_ENVIRONMENT_KEYS = frozenset({"LANG", "LC_ALL", "TZ"})
_API_WIND_DATE_PLATFORM_INPUT_ID = "api-wind-date-v1"
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
        "environment_allowlist",
        "environment_defaults",
        "network_access",
        "database_access",
    }
)


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
    environment_allowlist: tuple[str, ...]
    environment_defaults: tuple[tuple[str, str], ...]
    network_access: bool
    database_access: bool

    @classmethod
    def for_tests(cls, **overrides) -> RuntimeProfile:
        changes = {"conda_env": None, **overrides}
        profile = replace(DEFAULT_RUNTIME_PROFILE, **changes)
        _validate_runtime_profile(profile, source="test runtime profile")
        return profile


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
    for field in ("network_access", "database_access"):
        if type(raw[field]) is not bool:
            raise ValueError(f"Blackbox V2 runtime profile field {field} has wrong type")
    if raw["network_access"] or raw["database_access"]:
        raise ValueError("Blackbox V2 runtime profile must deny network and database access")
    if raw["max_output_bytes"] > raw["max_run_dir_bytes"]:
        raise ValueError("Blackbox V2 max_output_bytes must not exceed max_run_dir_bytes")

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


def execute_blackbox_cli(
    *,
    script_path: str | Path,
    mode: str,
    input_path: str | Path,
    data_dir: str | Path,
    output_path: str | Path,
    platform_input_ids: Sequence[str] = (),
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
    timeout_sec: float | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行一次 Blackbox V2 CLI；仅进程成功且 Output 合法存在才返回。"""
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    _validate_runtime_profile(profile, source="runtime profile")
    if mode not in {"predict", "backtest"}:
        raise ValueError(f"unsupported Blackbox V2 mode: {mode}")
    if process_start_guard is not None and mode != "predict":
        raise ValueError(
            "process_start_guard is only valid for Blackbox predict"
        )
    script = _resolve_controlled_path(script_path, label="script")
    input_file = _resolve_controlled_path(input_path, label="input")
    data = _resolve_controlled_path(data_dir, label="data-dir")
    output = _resolve_controlled_path(output_path, label="output", allow_missing=True)
    if not script.is_file() or script.suffix != ".py":
        raise ValueError(f"Blackbox V2 script must be one regular .py file: {script}")
    if not input_file.is_file():
        raise ValueError(f"Blackbox V2 input must be one regular file: {input_file}")
    normalized_platform_input_ids = _normalize_platform_input_ids(
        platform_input_ids
    )
    expected_filenames = _expected_data_filenames(
        normalized_platform_input_ids
    )
    initial_data_state = _validate_data_dir(
        data,
        platform_input_ids=normalized_platform_input_ids,
    )
    if output.exists():
        raise ValueError(f"platform must provide a fresh output path: {output}")
    runtime = _python_runtime(profile)
    data_files = tuple(data / filename for filename in expected_filenames)
    _prepare_output_directory(
        output.parent,
        controls=(
            ("script", script),
            ("input", input_file),
            ("data-dir", data),
            *((f"data file {path.name}", path) for path in data_files),
            ("runtime prefix", runtime.prefix),
        ),
    )
    if (
        _validate_data_dir(
            data,
            platform_input_ids=normalized_platform_input_ids,
        )
        != initial_data_state
    ):
        raise ValueError(
            "Blackbox V2 data-dir changed before process start"
        )

    try:
        _validate_api_wind_date_request_mappings(
            mode=mode,
            input_path=input_file,
            data_dir=data,
            platform_input_ids=normalized_platform_input_ids,
        )
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
        process_kwargs = {
            "cwd": output.parent,
            "env": env,
            "timeout": timeout,
            "memory_limit_bytes": profile.memory_limit_bytes,
            "max_capture_bytes": profile.max_log_bytes,
            "max_run_dir_bytes": profile.max_run_dir_bytes,
            "max_run_dir_entries": profile.max_run_dir_entries,
        }
        if process_started is not None:
            process_kwargs["process_started"] = process_started
        if process_fence is not None:
            process_kwargs["process_fence"] = process_fence
        if process_start_guard is not None:
            process_kwargs["process_start_guard"] = (
                process_start_guard
            )
        completed = _run_process(command, **process_kwargs)
        # 运行后复验输入目录指纹。平台不再依赖 OS 级 sandbox 阻止算法写入
        # data-dir，因此必须在子进程结束后证明输入未被改写；任何变化都视为
        # 本次执行不可信，直接失败并触发 run 目录清理。
        if (
            _validate_data_dir(
                data,
                platform_input_ids=normalized_platform_input_ids,
            )
            != initial_data_state
        ):
            raise BlackboxExecutionError(
                "Blackbox V2 data-dir changed during execution"
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
            if isinstance(
                execution_error,
                ProcessGroupTerminationError,
            ):
                execution_error.add_note(
                    "Blackbox V2 run directory cleanup failed: "
                    f"{cleanup_error}"
                )
                raise execution_error from cleanup_error
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
    platform_input_ids: Sequence[str] = (),
    parent_data_snapshot_id: str | None = None,
    input_identity_manifest: Mapping[str, Any] | None = None,
    input_audit_manifest: Mapping[str, Any] | None = None,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
    timeout_sec: float | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
) -> PredictionRecord:
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    (
        normalized_platform_input_ids,
        validated_parent_snapshot_id,
        validated_identity_manifest,
        validated_audit_manifest,
    ) = _validate_platform_input_evidence(
        data_snapshot_id=data_snapshot_id,
        platform_input_ids=platform_input_ids,
        parent_data_snapshot_id=parent_data_snapshot_id,
        input_identity_manifest=input_identity_manifest,
        input_audit_manifest=input_audit_manifest,
    )
    with tempfile.TemporaryDirectory(prefix="blackbox-v2-predict-") as tmpdir:
        root = Path(tmpdir)
        request_path = write_request(request, root / "request.json")
        output_path = root / "output" / "prediction.json"
        execute_kwargs = {
            "script_path": script_path,
            "mode": "predict",
            "input_path": request_path,
            "data_dir": data_dir,
            "output_path": output_path,
            "profile": profile,
            "platform_input_ids": normalized_platform_input_ids,
        }
        if timeout_sec is not None:
            execute_kwargs["timeout_sec"] = timeout_sec
        if process_started is not None:
            execute_kwargs["process_started"] = process_started
        if process_fence is not None:
            execute_kwargs["process_fence"] = process_fence
        if process_start_guard is not None:
            execute_kwargs["process_start_guard"] = (
                process_start_guard
            )
        execute_blackbox_cli(**execute_kwargs)
        result = load_prediction_result(output_path, request)
    return _to_prediction_record(
        metadata,
        result,
        data_snapshot_id,
        profile,
        platform_input_ids=normalized_platform_input_ids,
        parent_data_snapshot_id=validated_parent_snapshot_id,
        input_identity_manifest=validated_identity_manifest,
        input_audit_manifest=validated_audit_manifest,
    )


def run_blackbox_backtest(
    *,
    metadata: BlackboxMetadata,
    script_path: str | Path,
    requests: Sequence[BlackboxRequest],
    data_dir: str | Path,
    data_snapshot_id: str,
    platform_input_ids: Sequence[str] = (),
    parent_data_snapshot_id: str | None = None,
    input_identity_manifest: Mapping[str, Any] | None = None,
    input_audit_manifest: Mapping[str, Any] | None = None,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
    budget: BacktestExecutionBudget | None = None,
) -> list[PredictionRecord]:
    (
        normalized_platform_input_ids,
        validated_parent_snapshot_id,
        validated_identity_manifest,
        validated_audit_manifest,
    ) = _validate_platform_input_evidence(
        data_snapshot_id=data_snapshot_id,
        platform_input_ids=platform_input_ids,
        parent_data_snapshot_id=parent_data_snapshot_id,
        input_identity_manifest=input_identity_manifest,
        input_audit_manifest=input_audit_manifest,
    )
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
                    platform_input_ids=normalized_platform_input_ids,
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
            _to_prediction_record(
                metadata,
                result,
                data_snapshot_id,
                profile,
                platform_input_ids=normalized_platform_input_ids,
                parent_data_snapshot_id=validated_parent_snapshot_id,
                input_identity_manifest=validated_identity_manifest,
                input_audit_manifest=validated_audit_manifest,
            )
            for result in results
        )
    return records


def _to_prediction_record(
    metadata: BlackboxMetadata,
    result: BlackboxResult,
    data_snapshot_id: str,
    profile: RuntimeProfile,
    *,
    platform_input_ids: tuple[str, ...] = (),
    parent_data_snapshot_id: str | None = None,
    input_identity_manifest: Mapping[str, Any] | None = None,
    input_audit_manifest: Mapping[str, Any] | None = None,
) -> PredictionRecord:
    extra: dict[str, Any] = {
        "runtime_type": "blackbox_v2",
        "request_id": result.request_id,
        "task_type": metadata.task_type,
        "target_rule": metadata.target_rule,
        "contract_version": metadata.schema_version,
        "runtime_profile": profile.name,
        "data_snapshot_id": data_snapshot_id,
    }
    if platform_input_ids:
        extra.update(
            {
                "parent_data_snapshot_id": parent_data_snapshot_id,
                "platform_input_ids": list(
                    platform_input_ids
                ),
                "platform_input_identity_manifest":
                    _copy_manifest(
                        input_identity_manifest,
                        "input_identity_manifest",
                    ),
                "platform_input_audit_manifest":
                    _copy_manifest(
                        input_audit_manifest,
                        "input_audit_manifest",
                    ),
            }
        )
    return PredictionRecord(
        scheme_id=metadata.scheme_id,
        target_tenor=metadata.target_tenor,
        horizon=metadata.horizon,
        predict_date=result.predict_date,
        feature_date=result.feature_date,
        target_date=result.target_date,
        predicted_direction=result.predicted_direction,
        model_version=metadata.algorithm_version,
        extra=extra,
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


def _normalize_platform_input_ids(
    platform_input_ids: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(platform_input_ids, (str, bytes)):
        raise ValueError(
            "platform_input_ids must be a sequence of registered IDs"
        )
    values = tuple(platform_input_ids)
    if not values:
        return ()
    normalized = PLATFORM_INPUT_REGISTRY.normalize_ids(values)
    if values != normalized:
        raise ValueError(
            "platform_input_ids must be unique and sorted"
        )
    return normalized


def _expected_data_filenames(
    platform_input_ids: Sequence[str],
) -> tuple[str, ...]:
    normalized = _normalize_platform_input_ids(platform_input_ids)
    return SNAPSHOT_FILENAMES + tuple(
        PLATFORM_INPUT_REGISTRY.get(artifact_id).filename
        for artifact_id in normalized
    )


def _copy_manifest(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{field} must be an object")
    return copied


def _validate_platform_input_evidence(
    *,
    data_snapshot_id: str,
    platform_input_ids: Sequence[str],
    parent_data_snapshot_id: str | None,
    input_identity_manifest: Mapping[str, Any] | None,
    input_audit_manifest: Mapping[str, Any] | None,
) -> tuple[
    tuple[str, ...],
    str | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    normalized = _normalize_platform_input_ids(platform_input_ids)
    if normalized:
        if (
            not isinstance(parent_data_snapshot_id, str)
            or not parent_data_snapshot_id
            or input_identity_manifest is None
            or input_audit_manifest is None
        ):
            raise ValueError(
                "declared platform inputs require parent snapshot and "
                "identity/audit manifests"
            )
        identity = _copy_manifest(
            input_identity_manifest,
            "input_identity_manifest",
        )
        audit = _copy_manifest(
            input_audit_manifest,
            "input_audit_manifest",
        )
        expected_combined_snapshot_id = (
            compute_blackbox_combined_snapshot_id(identity)
        )
        if expected_combined_snapshot_id != data_snapshot_id:
            raise ValueError(
                "combined snapshot ID does not match identity manifest"
            )
        if identity["parent_snapshot_id"] != parent_data_snapshot_id:
            raise ValueError(
                "identity manifest parent_snapshot_id does not match "
                "runner evidence"
            )
        artifact_ids = tuple(
            artifact["artifact_id"]
            for artifact in identity["platform_inputs"]
        )
        if artifact_ids != normalized:
            raise ValueError(
                "identity manifest platform inputs do not match "
                "runner declaration"
            )
        _validate_platform_input_audit_manifest(
            audit,
            identity_manifest=identity,
            parent_snapshot_id=parent_data_snapshot_id,
        )
        return (
            normalized,
            parent_data_snapshot_id,
            identity,
            audit,
        )
    if any(
        value is not None
        for value in (
            parent_data_snapshot_id,
            input_identity_manifest,
            input_audit_manifest,
        )
    ):
        raise ValueError(
            "platform input manifests require declared platform inputs"
        )
    return (), None, None, None


def _validate_platform_input_audit_manifest(
    audit_manifest: Mapping[str, Any],
    *,
    identity_manifest: Mapping[str, Any],
    parent_snapshot_id: str,
) -> None:
    if set(audit_manifest) != {
        "identity",
        "base_snapshot",
        "platform_inputs",
    }:
        raise ValueError("platform input audit manifest fields are invalid")
    if audit_manifest["identity"] != identity_manifest:
        raise ValueError(
            "platform input audit manifest identity does not match "
            "identity manifest"
        )
    base_snapshot = audit_manifest["base_snapshot"]
    if (
        not isinstance(base_snapshot, Mapping)
        or set(base_snapshot)
        != {
            "snapshot_id",
            "schema_version",
            "generation_id",
            "refresh_date",
        }
        or base_snapshot["snapshot_id"] != parent_snapshot_id
    ):
        raise ValueError(
            "platform input audit manifest base snapshot is invalid"
        )
    audit_artifacts = audit_manifest["platform_inputs"]
    identity_artifacts = identity_manifest["platform_inputs"]
    if (
        not isinstance(audit_artifacts, list)
        or len(audit_artifacts) != len(identity_artifacts)
    ):
        raise ValueError(
            "platform input audit manifest artifacts are invalid"
        )
    for audit_artifact, identity_artifact in zip(
        audit_artifacts,
        identity_artifacts,
    ):
        if (
            not isinstance(audit_artifact, Mapping)
            or set(audit_artifact) != {"identity", "provenance"}
            or audit_artifact["identity"] != identity_artifact
            or not isinstance(audit_artifact["provenance"], Mapping)
        ):
            raise ValueError(
                "platform input audit manifest artifact is invalid"
            )


def _validate_data_dir(
    data_dir: Path,
    *,
    platform_input_ids: Sequence[str] = (),
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    try:
        directory_before = data_dir.lstat()
    except OSError as exc:
        raise ValueError(
            f"Blackbox V2 data-dir is unavailable: {data_dir}"
        ) from exc
    if (
        stat.S_ISLNK(directory_before.st_mode)
        or not stat.S_ISDIR(directory_before.st_mode)
    ):
        raise ValueError(f"Blackbox V2 data-dir must be one regular directory: {data_dir}")
    expected_filenames = _expected_data_filenames(platform_input_ids)
    entries = {path.name for path in data_dir.iterdir()}
    if entries != set(expected_filenames):
        raise ValueError(
            "Blackbox V2 data-dir files must be exactly "
            f"{list(expected_filenames)}"
        )
    states: list[tuple[str, tuple[int, ...]]] = []
    for filename in expected_filenames:
        path = data_dir / filename
        try:
            before = path.lstat()
        except OSError as exc:
            raise ValueError(
                f"Blackbox V2 data file is unavailable: {path}"
            ) from exc
        if stat.S_ISLNK(before.st_mode):
            raise ValueError(f"Blackbox V2 data file must be regular and not a symlink: {path}")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                f"Blackbox V2 data file must be regular: {path}"
            )
        if before.st_nlink != 1:
            raise ValueError(
                f"Blackbox V2 data file must not be a hardlink: {path}"
            )
        before_fingerprint = _data_path_fingerprint(before)
        after = path.stat(follow_symlinks=False)
        if _data_path_fingerprint(after) != before_fingerprint:
            raise ValueError(
                f"Blackbox V2 data file changed during validation: {path}"
            )
        states.append((filename, before_fingerprint))
    if {path.name for path in data_dir.iterdir()} != entries:
        raise ValueError(
            "Blackbox V2 data-dir changed during validation"
        )
    directory_after = data_dir.lstat()
    if (
        directory_after.st_dev != directory_before.st_dev
        or directory_after.st_ino != directory_before.st_ino
        or directory_after.st_mtime_ns != directory_before.st_mtime_ns
        or directory_after.st_ctime_ns != directory_before.st_ctime_ns
    ):
        raise ValueError(
            "Blackbox V2 data-dir changed during validation"
        )
    for filename, expected_state in states:
        current = (data_dir / filename).stat(follow_symlinks=False)
        if _data_path_fingerprint(current) != expected_state:
            raise ValueError(
                "Blackbox V2 data file changed during validation: "
                f"{data_dir / filename}"
            )
    return tuple(states)


def _validate_api_wind_date_request_mappings(
    *,
    mode: str,
    input_path: Path,
    data_dir: Path,
    platform_input_ids: Sequence[str],
) -> None:
    """在启动上游进程前校验每条 Request 的冻结周历映射。"""
    if _API_WIND_DATE_PLATFORM_INPUT_ID not in platform_input_ids:
        return

    try:
        requests = (
            (load_request(input_path),)
            if mode == "predict"
            else tuple(load_requests(input_path))
        )
    except ValueError:
        # UnitGate 要求上游脚本本身拒绝故意构造的非法 raw Request；
        # 只有已通过 Contract 解析的 Request 才进入本平台周历映射校验。
        return
    calendar_filename = PLATFORM_INPUT_REGISTRY.get(
        _API_WIND_DATE_PLATFORM_INPUT_ID
    ).filename
    calendar_path = data_dir / calendar_filename
    week_ids_by_rdate = _load_api_wind_date_week_ids(calendar_path)
    for request in requests:
        week_ids = week_ids_by_rdate.get(request.daily_cutoff_key, ())
        if len(week_ids) != 1:
            raise ValueError(
                "Blackbox V2 calendar mapping "
                f"request_id={request.request_id} must contain exactly one row "
                f"for daily_cutoff_key={request.daily_cutoff_key}; "
                f"found={len(week_ids)}"
            )
        actual_week_id = week_ids[0]
        if actual_week_id != request.weekly_cutoff_key:
            raise ValueError(
                "Blackbox V2 calendar mapping "
                f"request_id={request.request_id} weekly_cutoff_key mismatch: "
                f"daily_cutoff_key={request.daily_cutoff_key}, "
                f"expected={request.weekly_cutoff_key}, got={actual_week_id}"
            )


def _load_api_wind_date_week_ids(
    calendar_path: Path,
) -> dict[str, list[str]]:
    """从已冻结的 api_wind_date.csv 读取原样的规范化周历键。"""
    expected_columns = ("rdate", "week_id")
    week_ids_by_rdate: dict[str, list[str]] = {}
    try:
        with calendar_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != expected_columns:
                raise ValueError(
                    "Blackbox V2 api_wind_date.csv columns must be "
                    f"{list(expected_columns)}"
                )
            for row in reader:
                if set(row) != set(expected_columns):
                    raise ValueError(
                        "Blackbox V2 api_wind_date.csv row fields are invalid"
                    )
                rdate = row["rdate"]
                week_id = row["week_id"]
                if (
                    not isinstance(rdate, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rdate)
                    or not isinstance(week_id, str)
                    or not re.fullmatch(r"\d{6}", week_id)
                ):
                    raise ValueError(
                        "Blackbox V2 api_wind_date.csv contains a "
                        "non-canonical calendar row"
                    )
                week_ids_by_rdate.setdefault(rdate, []).append(week_id)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(
            f"Blackbox V2 api_wind_date.csv is invalid: {calendar_path}: {exc}"
        ) from exc
    return week_ids_by_rdate


def _data_path_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


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


_RUNTIME_BOOTSTRAP = r'''
import resource
import runpy
import sys

max_file_bytes = int(sys.argv[1])
script_argv = sys.argv[2:]
if hasattr(resource, "RLIMIT_FSIZE"):
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
sys.argv = script_argv
runpy.run_path(script_argv[0], run_name="__main__")
'''


def _bootstrap_command(
    command: list[str],
    max_file_bytes: int,
) -> list[str]:
    """把算法命令包装成受控引导：python -I 隔离模式 + RLIMIT_FSIZE 写入上限。"""
    return [
        command[0],
        "-I",
        "-c",
        _RUNTIME_BOOTSTRAP,
        str(max_file_bytes),
        *command[1:],
    ]


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
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ContextManager[object] | None = None,
) -> subprocess.CompletedProcess[str]:
    if max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be positive")
    if max_run_dir_bytes <= 0:
        raise ValueError("max_run_dir_bytes must be positive")
    if max_run_dir_entries <= 0:
        raise ValueError("max_run_dir_entries must be positive")
    with tempfile.TemporaryFile(mode="w+b", dir=cwd) as stdout_file:
        with tempfile.TemporaryFile(mode="w+b", dir=cwd) as stderr_file:
            guard_context = (
                process_start_guard
                if process_start_guard is not None
                else nullcontext()
            )
            process_start_completed = False
            unconfirmed_cleanup_error: (
                ProcessRegistrationCleanupError | None
            ) = None
            try:
                with guard_context:
                    if process_fence is not None:
                        process_fence()
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                    )
                    process_group_id: int | None = None
                    try:
                        process_group_id = (
                            capture_new_session_process_group(
                                process
                            )
                        )
                        if process_started is not None:
                            process_started(
                                process.pid,
                                process_group_id,
                            )
                        if process_fence is not None:
                            process_fence()
                    except BaseException as registration_error:
                        termination = _terminate_process_group(
                            process,
                            process_group_id=process_group_id,
                        )
                        if not termination.confirmed_gone:
                            unconfirmed_cleanup_error = (
                                ProcessRegistrationCleanupError(
                                    registration_error=registration_error,
                                    termination=termination,
                                )
                            )
                            raise unconfirmed_cleanup_error from registration_error
                        raise
                    process_start_completed = True
            except BaseException as guard_exit_error:
                if (
                    unconfirmed_cleanup_error is not None
                    and guard_exit_error is not unconfirmed_cleanup_error
                ):
                    raise unconfirmed_cleanup_error from guard_exit_error
                if process_start_completed:
                    termination = _terminate_process_group(
                        process,
                        process_group_id=process_group_id,
                    )
                    if not termination.confirmed_gone:
                        raise ProcessRegistrationCleanupError(
                            registration_error=guard_exit_error,
                            termination=termination,
                        ) from guard_exit_error
                raise
            if not process_start_completed:
                if unconfirmed_cleanup_error is not None:
                    raise unconfirmed_cleanup_error
                raise RuntimeError(
                    "process_start_guard suppressed a process-start "
                    "failure"
                )
            if process_group_id is None:
                raise AssertionError("process group was not captured")
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
                termination = _terminate_process_group(
                    process,
                    process_group_id=process_group_id,
                )
                if not termination.confirmed_gone:
                    raise ProcessGroupTerminationError(
                        termination=termination,
                        context=failure,
                    )
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


def _bounded(value: str) -> str:
    text = value.strip()
    if len(text) <= 4000:
        return text
    return text[:4000] + "..."
