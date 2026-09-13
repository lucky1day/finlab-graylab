from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import tempfile
import threading
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Sequence

from scheduler.blackbox_v2_runner import (
    DEFAULT_RUNTIME_PROFILE,
    RuntimeProfile,
    run_blackbox_backtest,
)
from scheduler.discovery import SchemeConfig, blackbox_deliveries, discover_schemes
from scheduler.process_control import (
    ProcessGroupTerminationError,
    ProcessRegistrationCleanupError,
    ProcessStartGuard,
    capture_new_session_process_group,
    require_process_start_guard,
    terminate_process_group as _terminate_process_group,
)
from scheduler.repository import (
    attach_run_data_snapshot,
    active_native_identity_error,
    complete_active_native_run,
    complete_approved_blackbox_run,
    create_scheme_run,
    create_engine_from_env,
    fail_scheme_run_atomic,
    read_blackbox_execution_approval,
    write_run_log,
)
from shared.calendar_service import get_calendar
from shared.blackbox_v2.contracts import (
    BlackboxMetadata,
    BlackboxRequest,
)
from shared.blackbox_v2.requests import build_live_request, resolve_live_context
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.input_artifacts import (
    EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
    get_ready_blackbox_snapshot,
    open_blackbox_runtime_view,
    resolve_blackbox_input_cutoffs,
    validate_blackbox_request_calendar,
)
from shared.blackbox_v2.snapshot import BlackboxSnapshot
from shared.db_config import DATABASE_ENV_FILE_ENV
from shared.models import PredictionRecord
from shared.one_shot_control_plane import (
    LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    SCHEDULED_ONE_SHOT_CONTROL_PLANES,
    SYSTEMD_ONE_SHOT_CONTROL_PLANE,
)
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)
from shared.source_runtime_database import (
    SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV,
    SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV,
    SOURCE_RUNTIME_SCHEME_IDS,
    SourceRuntimeDatabaseConfig,
    frozen_source_runtime_database_config,
    load_source_runtime_database_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALGO_ENV = "forecast_env"
BLACKBOX_SNAPSHOT_MODE_FRESH = "fresh"
BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF = "historical_as_of_replay"
VALID_BLACKBOX_SNAPSHOT_MODES = {
    BLACKBOX_SNAPSHOT_MODE_FRESH,
    BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
}
TIMEOUT_OUTPUT_DRAIN_SEC = 1
logger = logging.getLogger(__name__)
PLATFORM_CONFIGURATION_ERROR_PREFIX = "platform configuration error:"
_SCHEDULED_EXECUTION_CONTEXTS = {
    LAUNCHD_ONE_SHOT_CONTROL_PLANE: object(),
    SYSTEMD_ONE_SHOT_CONTROL_PLANE: object(),
}
SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT = (
    "data_bridge_ready_timeout"
)

_ALGORITHM_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "TZ",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "GLOG_minloglevel",
        "ABSL_LOGGING_MIN_LEVEL",
        "NUMBA_CACHE_DIR",
        "MPLCONFIGDIR",
        "PYTHONDONTWRITEBYTECODE",
        "BFL_RUNTIME_ROOT",
        DATABASE_ENV_FILE_ENV,
        "DAILY_0629_SOURCE_CACHE_DISABLE",
        "DAILY_0629_SOURCE_CACHE_DIR",
        "DAILY_0629_SOURCE_TIMEOUT_SEC",
        "DAILY_0629_SOURCE_PYTHON",
        "DAILY_N_JOBS",
        "MONTHLY_SOURCE_CACHE_DISABLE",
        "MONTHLY_SOURCE_CACHE_DIR",
        "MONTHLY_SOURCE_PYTHON",
        "MONTHLY_SOURCE_TIMEOUT_SEC",
        "WEEKLY_AVERAGE_SOURCE_PYTHON",
        "WEEKLY_AVERAGE_SOURCE_TIMEOUT_SEC",
    }
)


@dataclass(frozen=True)
class SchemeRunResult:
    """方案执行结果。"""

    scheme_id: str
    status: str
    records_written: int = 0
    duration_sec: float | None = None
    error_msg: str | None = None
    run_id: int | None = None


def _launchd_scheduled_execution_context() -> object:
    """返回仅供 launchd one-shot runner 传递的进程内 capability。"""
    return _SCHEDULED_EXECUTION_CONTEXTS[
        LAUNCHD_ONE_SHOT_CONTROL_PLANE
    ]


def _systemd_scheduled_execution_context() -> object:
    """返回仅供 systemd one-shot runner 传递的进程内 capability。"""
    return _SCHEDULED_EXECUTION_CONTEXTS[
        SYSTEMD_ONE_SHOT_CONTROL_PLANE
    ]


def _is_scheduled_execution_context(
    control_plane: object,
    context: object,
) -> bool:
    """只接受控制面名称与其进程内 capability 的精确配对。"""
    if control_plane not in SCHEDULED_ONE_SHOT_CONTROL_PLANES:
        return False
    return context is _SCHEDULED_EXECUTION_CONTEXTS[control_plane]


def _record_from_payload(item: dict) -> PredictionRecord:
    return PredictionRecord(
        scheme_id=str(item["scheme_id"]),
        target_tenor=str(item["target_tenor"]),
        horizon=int(item["horizon"]),
        predict_date=str(item["predict_date"]),
        target_date=str(item["target_date"]),
        predicted_direction=int(item["predicted_direction"]),
        feature_date=str(item["feature_date"]) if item.get("feature_date") is not None else None,
        prediction_phase=str(item["prediction_phase"]) if item.get("prediction_phase") is not None else None,
        model_version=str(item["model_version"]) if item.get("model_version") is not None else None,
        extra=item.get("extra") or None,
        run_id=int(item["run_id"]) if item.get("run_id") is not None else None,
        scheme_version=str(item["scheme_version"]) if item.get("scheme_version") is not None else None,
    )


def run_scheme_subprocess(
    scheme_id: str,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
    source_database_config: (
        SourceRuntimeDatabaseConfig | None
    ) = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    ephemeral_native_runtime_root: str | Path | None = None,
    cancellation_event: threading.Event | None = None,
) -> list[PredictionRecord]:
    """通过 conda 子进程在算法环境中运行方案。"""
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    normalized_ephemeral_root = _normalize_ephemeral_native_runtime_root(
        ephemeral_native_runtime_root
    )
    env = _build_algorithm_environment()
    env.pop(SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV, None)
    env.pop(SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV, None)
    if scheme_id in SOURCE_RUNTIME_SCHEME_IDS:
        if source_database_config is None:
            source_database_config = (
                load_source_runtime_database_config()
            )
    elif source_database_config is not None:
        raise ValueError(
            "source database config is only valid for source schemes"
        )
    env.pop(EPHEMERAL_NATIVE_INPUT_ROOT_ENV, None)
    if normalized_ephemeral_root is not None:
        env[EPHEMERAL_NATIVE_INPUT_ROOT_ENV] = str(normalized_ephemeral_root)
    cmd = [
        "conda",
        "run",
        "--no-capture-output",
        "-n",
        algo_env,
        "python",
        "-m",
        "scheduler.scheme_runner",
        "--scheme-id",
        scheme_id,
        "--predict-date",
        predict_date,
    ]
    process_kwargs = {
        "cwd": PROJECT_ROOT,
        "env": env,
        "timeout": timeout_sec,
    }
    if process_started is not None:
        process_kwargs["process_started"] = process_started
    if process_fence is not None:
        process_kwargs["process_fence"] = process_fence
    if process_start_guard is not None:
        process_kwargs["process_start_guard"] = process_start_guard
    if cancellation_event is not None:
        process_kwargs["cancellation_event"] = cancellation_event
    source_database_context = (
        frozen_source_runtime_database_config(
            source_database_config
        )
        if source_database_config is not None
        else nullcontext(None)
    )
    with tempfile.TemporaryDirectory(
        prefix="bfl-native-pycache-"
    ) as pycache_root:
        os.chmod(pycache_root, 0o700)
        pycache_path = Path(pycache_root).resolve(strict=True)
        pycache_details = pycache_path.lstat()
        if (
            stat.S_ISLNK(pycache_details.st_mode)
            or not stat.S_ISDIR(pycache_details.st_mode)
            or pycache_details.st_uid != os.getuid()
            or stat.S_IMODE(pycache_details.st_mode) != 0o700
            or any(pycache_path.iterdir())
        ):
            raise RuntimeError("native pycache root is unsafe")
        env["PYTHONPYCACHEPREFIX"] = str(pycache_path)
        with source_database_context as source_database_path:
            if source_database_path is not None:
                env[SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV] = str(
                    source_database_path.parent
                )
                env[SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV] = str(
                    source_database_path
                )
            completed = _run_process_group(
                cmd,
                **process_kwargs,
            )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"scheme runner returned non-list payload for {scheme_id}")
    return [_record_from_payload(item) for item in payload]


def _normalize_ephemeral_native_runtime_root(
    value: str | Path | None,
) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("ephemeral_native_runtime_root must be absolute")
    return path


def _build_algorithm_environment() -> dict[str, str]:
    """仅向 Native 算法进程传递运行必需且不含凭据的环境变量。"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _ALGORITHM_ENVIRONMENT_ALLOWLIST
        or name.startswith("LC_")
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_configured_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    *,
    engine,
    algo_env: str,
    timeout_sec: int | None,
    blackbox_snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    expected_generation_id: str | None = None,
    expected_refresh_date: str | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    ephemeral_native_runtime_root: str | Path | None = None,
    cancellation_event: threading.Event | None = None,
) -> list[PredictionRecord]:
    """按显式 runtime_type 选择算法执行驱动。"""
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    if blackbox_snapshot_mode not in VALID_BLACKBOX_SNAPSHOT_MODES:
        raise ValueError(f"unsupported Blackbox snapshot mode: {blackbox_snapshot_mode}")
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    normalized_ephemeral_root = _normalize_ephemeral_native_runtime_root(
        ephemeral_native_runtime_root
    )
    if runtime_type != "native_adapter" and (
        normalized_ephemeral_root is not None
        or cancellation_event is not None
    ):
        raise ValueError(
            "Native execution contract is only valid for native_adapter"
        )
    if runtime_type == "native_adapter":
        if blackbox_snapshot_mode != BLACKBOX_SNAPSHOT_MODE_FRESH:
            raise ValueError(
                "historical Blackbox snapshot mode is not valid for native_adapter"
            )
        if timeout_sec is None:
            raise ValueError(
                "Native execution timeout must be resolved before dispatch"
            )
        native_kwargs = {}
        if normalized_ephemeral_root is not None:
            native_kwargs["ephemeral_native_runtime_root"] = (
                normalized_ephemeral_root
            )
        if cancellation_event is not None:
            native_kwargs["cancellation_event"] = cancellation_event
        if process_started is not None:
            native_kwargs["process_started"] = process_started
        if process_fence is not None:
            native_kwargs["process_fence"] = process_fence
        if process_start_guard is not None:
            native_kwargs["process_start_guard"] = process_start_guard
        return run_scheme_subprocess(
            cfg.scheme_id,
            predict_date,
            algo_env=algo_env,
            timeout_sec=timeout_sec,
            **native_kwargs,
        )
    if runtime_type == "blackbox_v2":
        if getattr(cfg, "input_source", None) != "data_bridge_current":
            raise ValueError(f"Blackbox V2 input_source must be data_bridge_current: {cfg.scheme_id}")
        blackbox_kwargs = {
            "engine": engine,
            "algo_env": algo_env,
            "timeout_sec": timeout_sec,
            "snapshot_mode": blackbox_snapshot_mode,
        }
        if blackbox_snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
            blackbox_kwargs["expected_generation_id"] = expected_generation_id
            blackbox_kwargs["expected_refresh_date"] = expected_refresh_date
        if process_started is not None:
            blackbox_kwargs["process_started"] = process_started
        if process_fence is not None:
            blackbox_kwargs["process_fence"] = process_fence
        if process_start_guard is not None:
            blackbox_kwargs["process_start_guard"] = (
                process_start_guard
            )
        return run_blackbox_scheme_subprocess(
            cfg,
            predict_date,
            **blackbox_kwargs,
        )
    raise ValueError(f"unsupported runtime_type for {cfg.scheme_id}: {runtime_type}")


def run_blackbox_scheme_subprocess(
    cfg: SchemeConfig,
    predict_date: str,
    *,
    engine,
    algo_env: str,
    timeout_sec: int | None,
    snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    expected_generation_id: str | None = None,
    expected_refresh_date: str | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    rebuild_state: bool = False,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> list[PredictionRecord]:
    """生成平台输入并通过 Blackbox V2 CLI 执行一个实盘 Request。"""
    from scheduler.blackbox_v2_runner import (
        run_blackbox_predict, state_binding_for_scheme,
    )

    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    if snapshot_mode not in VALID_BLACKBOX_SNAPSHOT_MODES:
        raise ValueError(f"unsupported Blackbox snapshot mode: {snapshot_mode}")
    if rebuild_state and (not getattr(cfg, "incremental_state", False)
                          or snapshot_mode != BLACKBOX_SNAPSHOT_MODE_FRESH):
        raise ValueError("state rebuild requires an incremental scheme and fresh trusted input")
    deliveries = blackbox_deliveries(cfg)
    require_fresh = snapshot_mode == BLACKBOX_SNAPSHOT_MODE_FRESH
    snapshot = get_ready_blackbox_snapshot(
        snapshot_date=predict_date,
        require_fresh=require_fresh,
        factor_input_mode=getattr(cfg, "factor_input_mode", None) or "legacy_v1",
    )

    metadata = deliveries[0].metadata
    if snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
        _validate_historical_snapshot(
            snapshot,
            predict_date=predict_date,
            expected_generation_id=expected_generation_id,
            expected_refresh_date=expected_refresh_date,
        )
    calendar = get_calendar(engine)
    if metadata.task_type in PERIOD_AVERAGE_TASK_TYPES:
        feature_date = resolve_live_context(
            metadata,
            predict_date=predict_date,
            calendar=calendar,
        ).feature_date
    elif metadata.frequency == "daily":
        feature_date = build_daily_live_context(
            calendar,
            predict_date,
            horizon=metadata.horizon,
        ).feature_date
    elif metadata.frequency == "weekly":
        feature_date = build_weekly_live_context(
            calendar,
            predict_date,
        ).feature_date
    else:
        feature_date = build_monthly_live_context(
            calendar,
            predict_date,
        ).feature_date
    cutoffs = resolve_blackbox_input_cutoffs(
        snapshot,
        feature_date=feature_date,
        engine=engine,
    )
    request = build_live_request(
        metadata,
        predict_date=predict_date,
        calendar=calendar,
        cutoffs=cutoffs,
    )
    input_bundle = compose_blackbox_input_bundle(
        snapshot,
        factor_input_mode=getattr(cfg, "factor_input_mode", None) or "legacy_v1",
    )
    blackbox_env = profile.conda_env if algo_env == DEFAULT_ALGO_ENV else algo_env
    profile = replace(
        profile,
        conda_env=blackbox_env,
    )
    records = []
    deadline = time.monotonic() + (timeout_sec or profile.predict_timeout_sec)
    with open_blackbox_runtime_view(input_bundle) as runtime_view:
        trusted_bundle = runtime_view.bundle
        predict_kwargs = {
            "metadata": metadata,
            "script_path": deliveries[0].script_path,
            "request": request,
            "data_dir": runtime_view.data_dir,
            "data_snapshot_id":
                trusted_bundle.combined_snapshot_id,
            "profile": profile,
        }
        state = state_binding_for_scheme(
            cfg, generation_id=snapshot.generation_id,
            persistent=snapshot_mode == BLACKBOX_SNAPSHOT_MODE_FRESH,
            rebuild=rebuild_state,
        )
        if state is not None:
            predict_kwargs["state"] = state
        if timeout_sec is not None:
            predict_kwargs["timeout_sec"] = timeout_sec
        if process_started is not None:
            predict_kwargs["process_started"] = process_started
        if process_fence is not None:
            predict_kwargs["process_fence"] = process_fence
        if process_start_guard is not None:
            predict_kwargs["process_start_guard"] = (
                process_start_guard
            )
        for delivery in deliveries:
            metadata = delivery.metadata
            target_request = request if len(deliveries) == 1 else build_live_request(
                metadata, predict_date=predict_date, calendar=calendar, cutoffs=cutoffs,
            )
            predict_kwargs.update(metadata=metadata, script_path=delivery.script_path, request=target_request)
            if len(deliveries) > 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("multi-target predict total deadline exceeded")
                predict_kwargs["timeout_sec"] = remaining
            try:
                record = run_blackbox_predict(**predict_kwargs)
            except ProcessGroupTerminationError:
                runtime_view.mark_termination_uncertain()
                raise
            if snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
                extra = dict(record.extra or {})
                extra.update(
                    {
                        "replay_semantics": "current_snapshot_as_of_not_historical_vintage",
                        "backfill_mode": "post_deployment_live_safe_replay",
                        "backfilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "data_generation_id": snapshot.generation_id,
                        "source_refresh_date": snapshot.refresh_date,
                        "daily_cutoff_key": cutoffs.daily_cutoff_key,
                        "weekly_cutoff_key": cutoffs.weekly_cutoff_key,
                        "monthly_cutoff_key": cutoffs.monthly_cutoff_key,
                    }
                )
                record = replace(record, extra=extra)
            records.append(_project_blackbox_fact_horizon(record, cfg=cfg, metadata=metadata))
    return records


def _project_blackbox_fact_horizon(
    record: PredictionRecord, *, cfg: SchemeConfig, metadata: BlackboxMetadata,
) -> PredictionRecord:
    """标准结果校验后只投影原事实键，保留 Request 日期和算法输出。"""
    from shared.scheme_config_schema import resolve_fact_horizon

    fact_horizon = getattr(cfg, "horizon", metadata.horizon)
    if fact_horizon == metadata.horizon:
        return record
    horizon = resolve_fact_horizon(
        metadata.scheme_id, metadata.task_type, metadata.horizon, fact_horizon,
    )
    return replace(record, horizon=horizon)


def _blackbox_metadata(cfg: SchemeConfig) -> BlackboxMetadata:
    deliveries = blackbox_deliveries(cfg)
    if len(deliveries) > 1:
        raise ValueError("multi-target scheme requires the complete delivery list")
    return deliveries[0].metadata


def run_blackbox_gray_replay_batch(
    cfg: SchemeConfig,
    *,
    requests: Sequence[BlackboxRequest],
    snapshot: BlackboxSnapshot,
    algo_env: str,
    timeout_sec: int,
    profile: RuntimeProfile = DEFAULT_RUNTIME_PROFILE,
) -> list[PredictionRecord]:
    """直接使用 producer-ready sealed snapshot 执行一个灰度批次。"""
    if not isinstance(snapshot, BlackboxSnapshot):
        raise TypeError("snapshot must be a BlackboxSnapshot")
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("gray replay batch requires runtime_type=blackbox_v2")
    if getattr(cfg, "input_source", None) != "data_bridge_current":
        raise ValueError(
            "gray replay batch requires input_source=data_bridge_current"
        )
    deliveries = blackbox_deliveries(cfg)
    if len(deliveries) > 1:
        raise ValueError("multi-target gray replay requires target-specific Requests")
    if (
        isinstance(timeout_sec, bool)
        or not isinstance(timeout_sec, int)
        or timeout_sec <= 0
    ):
        raise ValueError("gray replay batch timeout_sec must be positive")
    if not isinstance(profile, RuntimeProfile):
        raise TypeError("gray replay batch profile must be a RuntimeProfile")

    batch_requests = list(requests)
    if not batch_requests:
        raise ValueError("gray replay batch requires at least one Request")
    request_ids = [request.request_id for request in batch_requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("gray replay batch Request ids must be unique")
    for request in batch_requests:
        if not isinstance(request, BlackboxRequest):
            raise TypeError("gray replay batch requests must be BlackboxRequest")
        _validate_gray_replay_request_within_snapshot(request, snapshot)

    metadata = _blackbox_metadata(cfg)
    if metadata.scheme_id != cfg.scheme_id:
        raise ValueError(
            "Blackbox V2 metadata scheme_id does not match configured scheme"
        )
    configured_frequency = getattr(cfg, "frequency", None)
    if configured_frequency != metadata.frequency:
        raise ValueError(
            "Blackbox V2 metadata frequency does not match configured scheme"
        )

    input_bundle = compose_blackbox_input_bundle(
        snapshot,
        factor_input_mode=getattr(cfg, "factor_input_mode", None) or "legacy_v1",
    )
    blackbox_env = (
        profile.conda_env if algo_env == DEFAULT_ALGO_ENV else algo_env
    )
    effective_profile = replace(
        profile,
        conda_env=blackbox_env,
        backtest_timeout_sec=min(
            profile.backtest_timeout_sec,
            timeout_sec,
        ),
    )
    with open_blackbox_runtime_view(input_bundle) as runtime_view:
        trusted_bundle = runtime_view.bundle
        backtest_kwargs: dict[str, Any] = {
            "metadata": metadata,
            "script_path": cfg.delivery_script,
            "requests": batch_requests,
            "data_dir": runtime_view.data_dir,
            "data_snapshot_id": trusted_bundle.combined_snapshot_id,
            "profile": effective_profile,
        }
        from scheduler.blackbox_v2_runner import state_binding_for_scheme
        state = state_binding_for_scheme(cfg, generation_id=snapshot.generation_id)
        if state is not None:
            backtest_kwargs["state"] = state
        records = run_blackbox_backtest(**backtest_kwargs)
    records_by_request = _index_gray_replay_records(records, batch_requests)
    return [
        _with_gray_request_identity(
            _project_blackbox_fact_horizon(
                records_by_request[request.request_id], cfg=cfg, metadata=metadata,
            ),
            request=request,
            snapshot=snapshot,
        )
        for request in batch_requests
    ]


def _with_gray_request_identity(
    record: PredictionRecord,
    *,
    request: BlackboxRequest,
    snapshot: BlackboxSnapshot,
) -> PredictionRecord:
    """保留逐 Request 计算所需的稳定输入身份。"""
    extra = dict(record.extra or {})
    extra.update(
        {
            "data_generation_id": snapshot.generation_id,
            "source_refresh_date": snapshot.refresh_date,
            "daily_cutoff_key": request.daily_cutoff_key,
            "weekly_cutoff_key": request.weekly_cutoff_key,
            "monthly_cutoff_key": request.monthly_cutoff_key,
        }
    )
    return replace(record, extra=extra)


def _validate_gray_replay_request_within_snapshot(
    request: BlackboxRequest,
    snapshot: BlackboxSnapshot,
) -> None:
    if (
        request.daily_cutoff_key not in (snapshot.daily_cutoff_keys or ())
        or request.weekly_cutoff_key not in (snapshot.weekly_cutoff_keys or ())
        or request.monthly_cutoff_key not in (snapshot.monthly_cutoff_keys or ())
    ):
        raise ValueError("Request cutoff is absent from producer-ready snapshot")
    validate_blackbox_request_calendar(
        snapshot,
        daily_cutoff_key=request.daily_cutoff_key,
        weekly_cutoff_key=request.weekly_cutoff_key,
    )


def _index_gray_replay_records(
    records: Sequence[PredictionRecord],
    requests: Sequence[BlackboxRequest],
) -> dict[str, PredictionRecord]:
    expected_ids = {request.request_id for request in requests}
    indexed: dict[str, PredictionRecord] = {}
    for record in records:
        request_id = (record.extra or {}).get("request_id")
        if not isinstance(request_id, str) or request_id not in expected_ids:
            raise ValueError("gray replay batch returned an unexpected request_id")
        if request_id in indexed:
            raise ValueError("gray replay batch returned duplicate request_id")
        indexed[request_id] = record
    if set(indexed) != expected_ids:
        raise ValueError("gray replay batch returned missing request_id")
    return indexed


def _validate_historical_snapshot(
    snapshot,
    *,
    predict_date: str,
    expected_generation_id: str | None = None,
    expected_refresh_date: str | None = None,
) -> None:
    generation_id = getattr(snapshot, "generation_id", None)
    refresh_date = getattr(snapshot, "refresh_date", None)
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("historical Blackbox snapshot generation_id must be non-empty")
    if not isinstance(refresh_date, str) or not refresh_date.strip():
        raise ValueError("historical Blackbox snapshot refresh_date must be non-empty")
    try:
        canonical_predict_date = date.fromisoformat(predict_date).isoformat()
        canonical_refresh_date = date.fromisoformat(refresh_date).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "historical Blackbox snapshot dates must use canonical YYYY-MM-DD"
        ) from exc
    if canonical_predict_date != predict_date or canonical_refresh_date != refresh_date:
        raise ValueError(
            "historical Blackbox snapshot dates must use canonical YYYY-MM-DD"
        )
    if predict_date > refresh_date:
        raise ValueError(
            "historical Blackbox snapshot requires predict_date on or before "
            "refresh_date: "
            f"predict_date={predict_date}, refresh_date={refresh_date}"
        )
    if expected_generation_id is not None and generation_id != expected_generation_id:
        raise ValueError(
            "DataBridge current generation changed after historical replay "
            "preflight: "
            f"expected={expected_generation_id}, actual={generation_id}"
        )
    if expected_refresh_date is not None and refresh_date != expected_refresh_date:
        raise ValueError(
            "DataBridge current refresh_date changed after historical replay "
            "preflight: "
            f"expected={expected_refresh_date}, actual={refresh_date}"
        )


def _run_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ContextManager[object] | None = None,
    cancellation_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """启动独立进程组，timeout 时清理 conda wrapper 及其子进程。"""
    guard_context = (
        process_start_guard
        if process_start_guard is not None
        else nullcontext()
    )
    process_start_completed = False
    unconfirmed_cleanup_error: ProcessRegistrationCleanupError | None = None
    try:
        with guard_context:
            if cancellation_event is not None and cancellation_event.is_set():
                raise RuntimeError("Native process start was cancelled")
            if process_fence is not None:
                process_fence()
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            process_group_id: int | None = None
            try:
                process_group_id = capture_new_session_process_group(
                    process
                )
                if process_started is not None:
                    process_started(process.pid, process_group_id)
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()
                ):
                    raise RuntimeError("Native process was cancelled")
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
            "process_start_guard suppressed a process-start failure"
        )
    if process_group_id is None:
        raise AssertionError("process group was not captured")
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                raise RuntimeError("Native process was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.25, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException as exc:
        termination = _terminate_process_group(
            process,
            process_group_id=process_group_id,
        )
        if isinstance(exc, subprocess.TimeoutExpired):
            stdout, stderr = _drain_timed_out_process_output(process)
            if not termination.confirmed_gone:
                raise ProcessGroupTerminationError(
                    termination=termination,
                    context=f"Native process timed out after {timeout}s",
                ) from exc
            raise subprocess.TimeoutExpired(
                cmd,
                timeout,
                output=stdout,
                stderr=stderr,
            ) from exc
        if not termination.confirmed_gone:
            raise ProcessGroupTerminationError(
                termination=termination,
                context=(
                    "Native process interrupted: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        raise
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _drain_timed_out_process_output(process: subprocess.Popen[str]) -> tuple[str, str]:
    """timeout 清理后短暂收集输出，避免子进程持管道导致无界等待。"""
    try:
        return process.communicate(timeout=TIMEOUT_OUTPUT_DRAIN_SEC)
    except subprocess.TimeoutExpired as exc:
        _close_process_pipes(process)
        return _timeout_payload_to_text(exc.output), _timeout_payload_to_text(exc.stderr)


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _timeout_payload_to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _execution_error_message(exc: Exception) -> str:
    """返回可写入现有 run 错误列的有界子进程原因。"""
    if not isinstance(exc, subprocess.CalledProcessError):
        return str(exc)
    return f"algorithm process exited with status {exc.returncode}"


def execute_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int | None = None,
    *,
    prediction_phase: str,
    scheduled_control_plane: str | None = None,
    scheduled_execution_context: object | None = None,
    scheduled_preflight_failure: str | None = None,
    blackbox_snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    blackbox_expected_generation_id: str | None = None,
    blackbox_expected_refresh_date: str | None = None,
    blackbox_record_validator: Callable[[list[PredictionRecord]], None] | None = None,
    engine=None,
    canonical_config_trusted: bool = False,
    ephemeral_native_runtime_root: str | Path | None = None,
    cancellation_event: threading.Event | None = None,
    process_start_guard: ProcessStartGuard | None = None,
) -> SchemeRunResult:
    """执行单个方案并写入预测表和运行日志。

    执行前校验：
    1. Native 要求 config、Registry 与精确版本均 active
    2. Blackbox 只以数据库 exact active 版本、批准人与 composite Registry 为准
    """
    if prediction_phase != "scheduled_live":
        raise ValueError(
            "execute_scheme only supports scheduled_live; "
            "use signal-gap-fill for gray_live"
        )
    configuration_error = (
        scheduled_live_execution_configuration_error(
            cfg,
            scheduled_control_plane=scheduled_control_plane,
            scheduled_execution_context=scheduled_execution_context,
            canonical_config_trusted=canonical_config_trusted,
        )
    )
    if configuration_error is not None:
        return SchemeRunResult(
            cfg.scheme_id,
            "failed",
            0,
            0.0,
            configuration_error,
        )
    if scheduled_preflight_failure is not None:
        if (
            prediction_phase != "scheduled_live"
            or not _is_scheduled_execution_context(
                scheduled_control_plane,
                scheduled_execution_context,
            )
        ):
            raise ValueError(
                "scheduled_preflight_failure requires one-shot "
                "execution context"
            )
        if (
            scheduled_preflight_failure
            != SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
        ):
            raise ValueError("unsupported scheduled_preflight_failure")
    owns_engine = engine is None
    if engine is None:
        engine = create_engine_from_env()
    started = time.monotonic()
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type != "blackbox_v2" and cfg.status != "active":
        duration = time.monotonic() - started
        write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, f"status={cfg.status}")
        if owns_engine:
            engine.dispose()
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, f"status={cfg.status}")

    scheme_version = getattr(cfg, "scheme_version", None)
    if runtime_type == "blackbox_v2":
        approval = read_blackbox_execution_approval(engine, cfg)
        if not approval.executable:
            reason = f"Blackbox V2 version is not production-approved: {approval.reason}"
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            if owns_engine:
                engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
    else:
        ok, reason = _verify_scheme_activation(engine, cfg.scheme_id, scheme_version)
        if not ok:
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, reason)
            if owns_engine:
                engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, reason)
    active_targets = _active_registry_targets(engine, cfg.scheme_id)

    run_id: int | None = None
    records_returned: int | None = None
    records_written = 0
    try:
        creation_fence: dict[str, object] = {}
        if prediction_phase == "scheduled_live":
            frequency = getattr(cfg, "frequency", None)
            if _is_scheduled_execution_context(
                scheduled_control_plane,
                scheduled_execution_context,
            ):
                creation_fence[
                    "scheduled_control_plane"
                ] = scheduled_control_plane
        run_id = create_scheme_run(
            engine,
            scheme_id=cfg.scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            prediction_phase=prediction_phase,
            records_expected=len(active_targets),
            **creation_fence,
        )
        if not active_targets:
            raise ValueError(
                f"active registry targets empty for scheme {cfg.scheme_id}: "
                "missing=[], extra=[], duplicates=[]"
            )
        if scheduled_preflight_failure is not None:
            raise RuntimeError(scheduled_preflight_failure)
        effective_timeout_sec = _effective_timeout_sec(cfg, timeout_sec)
        run_kwargs = {
            "engine": engine,
            "algo_env": algo_env,
            "timeout_sec": effective_timeout_sec,
        }
        if runtime_type == "native_adapter":
            if ephemeral_native_runtime_root is not None:
                run_kwargs["ephemeral_native_runtime_root"] = (
                    ephemeral_native_runtime_root
                )
            if cancellation_event is not None:
                run_kwargs["cancellation_event"] = cancellation_event
            if process_start_guard is not None:
                run_kwargs["process_start_guard"] = process_start_guard
        if blackbox_snapshot_mode != BLACKBOX_SNAPSHOT_MODE_FRESH:
            run_kwargs["blackbox_snapshot_mode"] = blackbox_snapshot_mode
            run_kwargs["expected_generation_id"] = blackbox_expected_generation_id
            run_kwargs["expected_refresh_date"] = blackbox_expected_refresh_date
        records = run_configured_scheme(cfg, predict_date, **run_kwargs)
        records_returned = len(records)
        if getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2":
            snapshot_ids = {
                str((record.extra or {}).get("data_snapshot_id"))
                for record in records
                if (record.extra or {}).get("data_snapshot_id")
            }
            if len(snapshot_ids) != 1:
                raise ValueError(
                    f"Blackbox V2 run must return exactly one data_snapshot_id, got {sorted(snapshot_ids)}"
                )
            attach_run_data_snapshot(
                engine,
                run_id=run_id,
                data_snapshot_id=next(iter(snapshot_ids)),
            )
        if runtime_type == "native_adapter":
            records = [
                replace(
                    record,
                    extra=_strip_native_input_provenance(record.extra),
                )
                for record in records
            ]
        records = _normalize_live_records(records, prediction_phase=prediction_phase)
        _validate_live_record_dates(records, cfg=cfg, predict_date=predict_date, engine=engine)
        _validate_records_against_active_registry(records, cfg=cfg, active_targets=active_targets)
        if blackbox_record_validator is not None:
            blackbox_record_validator(records)
        if runtime_type == "blackbox_v2":
            expected = len(active_targets)
            if records_returned != expected:
                raise ValueError(
                    f"expected={expected}, returned={records_returned}, written=0"
                )
            duration = time.monotonic() - started
            status, records_written, error_msg = (
                complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=run_id,
                    records=records,
                    scheme_version=scheme_version,
                    records_returned=records_returned,
                    run_date=predict_date,
                    duration_sec=duration,
                )
            )
            return SchemeRunResult(
                cfg.scheme_id,
                status,
                records_written,
                duration,
                error_msg,
                run_id,
            )
        else:
            current_active_targets = _active_registry_targets(engine, cfg.scheme_id)
            if current_active_targets != active_targets:
                raise ValueError(
                    "active registry targets changed during run: "
                    f"initial={sorted(active_targets)}, current={sorted(current_active_targets)}"
                )
            duration = time.monotonic() - started
            status, records_written, error_msg = complete_active_native_run(
                engine,
                cfg,
                run_id=run_id,
                records=records,
                scheme_version=scheme_version,
                records_returned=records_returned,
                run_date=predict_date,
                duration_sec=duration,
            )
            return SchemeRunResult(
                cfg.scheme_id,
                status,
                records_written,
                duration,
                error_msg,
                run_id,
            )
    except Exception as exc:
        duration = time.monotonic() - started
        error_msg = _execution_error_message(exc)
        if run_id is not None:
            try:
                records_written = 0
                fail_scheme_run_atomic(
                    engine,
                    run_id=run_id,
                    scheme_id=cfg.scheme_id,
                    run_date=predict_date,
                    duration_sec=duration,
                    records_returned=records_returned,
                    error_message=error_msg,
                )
            except Exception as audit_exc:
                logger.exception("failed to finish failed scheme run_id=%s", run_id)
                error_msg = _append_audit_error(
                    error_msg,
                    "fail_scheme_run_atomic",
                    audit_exc,
                )
        if run_id is None:
            try:
                write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, error_msg, run_id=run_id)
            except Exception as audit_exc:
                logger.exception("failed to write run log for failed scheme run_id=%s", run_id)
                error_msg = _append_audit_error(error_msg, "write_run_log", audit_exc)
        return SchemeRunResult(
            cfg.scheme_id,
            "failed",
            records_written,
            duration,
            error_msg,
            run_id,
        )
    finally:
        if owns_engine:
            engine.dispose()


def _scheduled_scheme_identity_error(engine, cfg: SchemeConfig) -> str | None:
    """只读验证定向 scheduled one-shot 的当前执行身份。"""
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if getattr(cfg, "status", None) != "active":
        return f"status={getattr(cfg, 'status', None)}"
    if runtime_type == "blackbox_v2":
        approval = read_blackbox_execution_approval(engine, cfg)
        if not approval.executable:
            return (
                "Blackbox V2 version is not production-approved: "
                f"{approval.reason}"
            )
    else:
        return active_native_identity_error(engine, cfg)
    return None


def scheduled_live_execution_configuration_error(
    cfg: SchemeConfig,
    *,
    scheduled_control_plane: str | None,
    scheduled_execution_context: object | None = None,
    canonical_config_trusted: bool = False,
) -> str | None:
    """在任何数据库或子进程副作用前校验低层 scheduled_live 入口。"""
    if _is_scheduled_execution_context(
        scheduled_control_plane,
        scheduled_execution_context,
    ):
        if canonical_config_trusted:
            return None
        canonical_error = _scheduled_live_canonical_configuration_error(
            cfg
        )
        if canonical_error is not None:
            return canonical_error
        return None
    if canonical_config_trusted:
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} trusted canonical "
            f"configuration requires one-shot context: scheme_id={cfg.scheme_id}"
        )
    canonical_error = _scheduled_live_canonical_configuration_error(cfg)
    if canonical_error is not None:
        return canonical_error
    return (
        f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
        "scheduled_live requires one-shot execution context: "
        f"scheme_id={cfg.scheme_id}"
    )


def _scheduled_live_canonical_configuration_error(
    cfg: SchemeConfig,
) -> str | None:
    """拒绝调用方伪造或漂移的 scheduled config。"""
    scheme_id = str(getattr(cfg, "scheme_id", "")).strip()
    try:
        matches = [
            candidate
            for candidate in discover_schemes()
            if candidate.scheme_id == scheme_id
        ]
    except (OSError, RuntimeError, ValueError):
        matches = []
    if len(matches) != 1:
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            "scheduled_live canonical configuration is unavailable: "
            f"scheme_id={scheme_id}"
        )

    canonical = matches[0]
    if not _scheduled_config_matches_canonical(
        cfg,
        canonical,
    ):
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            "scheduled_live canonical configuration drift: "
            f"scheme_id={scheme_id}"
        )
    return None


def _scheduled_config_matches_canonical(
    config: object,
    canonical: object,
) -> bool:
    fields = [
        "scheme_id",
        "scheme_version",
        "runtime_type",
        "frequency",
        "task_type",
        "horizon",
    ]
    if getattr(config, "runtime_type", None) != "blackbox_v2":
        fields.extend(("status", "version_status"))
    return (
        all(
            getattr(config, field, None)
            == getattr(canonical, field, None)
            for field in fields
        )
        and tuple(
            str(tenor)
            for tenor in getattr(config, "tenors", ())
        )
        == tuple(
            str(tenor)
            for tenor in getattr(canonical, "tenors", ())
        )
    )


def _append_audit_error(error_msg: str, operation: str, exc: Exception) -> str:
    """保留原始错误并追加 best-effort 审计失败信息。"""
    return f"{error_msg}; {operation} audit failed: {exc}"


def _effective_timeout_sec(
    cfg: SchemeConfig,
    operation_timeout_sec: int | None,
) -> int:
    """解析方案预算与本次调用 deadline；Profile 上限由 runner 施加。"""
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    schedule = getattr(cfg, "schedule", None)
    configured = getattr(schedule, "timeout_sec", None)
    if configured is None:
        configured = getattr(cfg, "execution_timeout_sec", None)
    if runtime_type == "blackbox_v2":
        if configured is None:
            raise ValueError(
                f"scheme {cfg.scheme_id} timeout_sec must be configured"
            )
        timeout = int(configured)
        if operation_timeout_sec is not None:
            operation_timeout = int(operation_timeout_sec)
            if operation_timeout <= 0:
                raise ValueError(
                    f"scheme {cfg.scheme_id} timeout_sec must be positive, "
                    f"got {operation_timeout}"
                )
            timeout = min(timeout, operation_timeout)
    else:
        fallback = 600 if operation_timeout_sec is None else int(operation_timeout_sec)
        timeout = int(configured) if configured is not None else fallback
    if timeout <= 0:
        raise ValueError(f"scheme {cfg.scheme_id} timeout_sec must be positive, got {timeout}")
    return timeout


def _normalize_live_records(records: list[PredictionRecord], *, prediction_phase: str) -> list[PredictionRecord]:
    """补齐 feature_date；phase 只保留在 run/record 运行语义中。"""
    normalized: list[PredictionRecord] = []
    for record in records:
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(f"record {record.scheme_id}/{record.target_tenor} missing feature_date")
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                f"record {record.scheme_id}/{record.target_tenor} anchor_date={anchor_date} "
                f"does not equal feature_date={feature_date}"
            )
        extra["feature_date"] = str(feature_date)
        extra.pop("prediction_phase", None)
        normalized.append(
            replace(
                record,
                feature_date=str(feature_date),
                prediction_phase=prediction_phase,
                extra=extra,
            )
        )
    return normalized


_NATIVE_INPUT_PROVENANCE_FIELDS = frozenset(
    {
        "input_artifact_path",
        "input_artifact_source",
        "input_artifact_data_version",
        "input_artifact_watermark",
    }
)
_NATIVE_INPUT_PROVENANCE_PREFIXES = (
    "daily_input_artifact_",
    "weekly_input_artifact_",
    "monthly_input_artifact_",
)


def _strip_native_input_provenance(
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """预测结果不保存一次性输入路径及其重复描述。"""
    return {
        key: value
        for key, value in dict(extra or {}).items()
        if key not in _NATIVE_INPUT_PROVENANCE_FIELDS
        and not key.startswith(_NATIVE_INPUT_PROVENANCE_PREFIXES)
    }


def _active_registry_targets(engine, scheme_id: str) -> set[tuple[str, int]]:
    """读取 base 方案当前 active 的业务 target 集合。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT target_tenor, horizon
                FROM t_scheme_registry
                WHERE base_scheme_id = :scheme_id
                  AND status = 'active'
                """
            ),
            {"scheme_id": scheme_id},
        ).mappings().all()
    return {(str(row["target_tenor"]), int(row["horizon"])) for row in rows}


def _validate_live_record_dates(
    records: list[PredictionRecord],
    *,
    cfg: SchemeConfig,
    predict_date: str,
    engine,
) -> None:
    """校验实盘记录没有复用旧输入窗口或旧 target。"""
    if getattr(cfg, "frequency", None) != "daily":
        return
    calendar = get_calendar(engine=engine)
    expected_by_horizon: dict[int, object] = {}
    run_predict_date = str(predict_date)[:10]
    for record in records:
        horizon = int(record.horizon)
        expected = expected_by_horizon.get(horizon)
        if expected is None:
            expected = build_daily_live_context(calendar, run_predict_date, horizon=horizon)
            expected_by_horizon[horizon] = expected
        record_predict_date = str(record.predict_date)[:10]
        record_feature_date = str(record.feature_date)[:10] if record.feature_date is not None else None
        record_target_date = str(record.target_date)[:10]
        problems = []
        if record_predict_date != run_predict_date:
            problems.append(f"expected predict_date={run_predict_date}, got {record_predict_date}")
        if record_feature_date != expected.feature_date:
            problems.append(f"expected feature_date={expected.feature_date}, got {record_feature_date}")
        if record_target_date != expected.target_date:
            problems.append(f"expected target_date={expected.target_date}, got {record_target_date}")
        if problems:
            raise ValueError(
                f"daily live record {record.scheme_id}/{record.target_tenor}/h{horizon} has invalid dates: "
                + "; ".join(problems)
            )


def _validate_records_against_active_registry(
    records: list[PredictionRecord],
    *,
    cfg: SchemeConfig,
    active_targets: set[tuple[str, int]],
) -> None:
    """确保 live 返回 target multiset 与 active registry target set 一致。"""
    config_horizon = int(getattr(cfg, "horizon"))
    for record in records:
        if record.scheme_id != cfg.scheme_id:
            raise ValueError(
                f"record scheme_id={record.scheme_id} does not equal config scheme_id={cfg.scheme_id}"
            )
        if int(record.horizon) != config_horizon:
            raise ValueError(
                f"record {record.scheme_id}/{record.target_tenor} horizon={record.horizon} "
                f"does not equal config horizon={config_horizon}"
            )

    returned_targets = Counter((str(record.target_tenor), int(record.horizon)) for record in records)
    returned_target_set = set(returned_targets)
    missing = sorted(active_targets - returned_target_set)
    extra = sorted(returned_target_set - active_targets)
    duplicates = sorted(
        (target_tenor, horizon, count)
        for (target_tenor, horizon), count in returned_targets.items()
        if count > 1
    )
    if missing or extra or duplicates:
        raise ValueError(
            f"live target mismatch: missing={missing}, extra={extra}, duplicates={duplicates}"
        )


def _verify_scheme_activation(engine, scheme_id: str, scheme_version: str | None) -> tuple[bool, str]:
    """校验方案在 DB 注册与版本激活状态，返回 (通过, 原因)。"""
    from sqlalchemy import text

    if not isinstance(scheme_version, str) or not scheme_version.strip():
        return False, f"scheme {scheme_id} scheme_version is required for activation verification"

    with engine.begin() as conn:
        # 1. 校验 t_scheme_registry 中该 base 方案至少有一个 active 业务方案行。
        active_rows = conn.execute(
            text(
                """
                SELECT COUNT(*) AS active_count
                FROM t_scheme_registry
                WHERE base_scheme_id = :scheme_id
                  AND status = 'active'
                """
            ),
            {"scheme_id": scheme_id},
        ).scalar_one()
        if int(active_rows or 0) == 0:
            return False, f"scheme {scheme_id} not found in t_scheme_registry"

        # 2. 校验 t_scheme_versions 中当前精确版本仅允许 active。
        ver_row = conn.execute(
            text(
                "SELECT status FROM t_scheme_versions "
                "WHERE scheme_id = :scheme_id AND scheme_version = :scheme_version "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"scheme_id": scheme_id, "scheme_version": scheme_version},
        ).one_or_none()
        if ver_row is None:
            return False, (
                f"scheme {scheme_id} version {scheme_version} not found in t_scheme_versions; "
                "run 'python -m harness activate --scheme-id {id}' first"
            )
        if ver_row[0] != "active":
            return False, (
                f"scheme {scheme_id} version {scheme_version} status={ver_row[0]}, "
                "must be active"
            )

    return True, "ok"
