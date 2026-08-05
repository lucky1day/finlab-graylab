from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, ContextManager, Sequence

from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
)
from scheduler.process_control import (
    ProcessGroupTerminationError,
    ProcessGroupTerminationResult,
    ProcessRegistrationCleanupError,
    ProcessStartGuard,
    capture_new_session_process_group,
    require_process_start_guard,
    terminate_process_group,
)
from scheduler.repository import (
    attach_run_data_snapshot,
    complete_active_native_run,
    complete_approved_blackbox_run,
    create_scheme_run,
    create_engine_from_env,
    fail_scheme_run_atomic,
    read_blackbox_execution_approval,
    sync_scheme_registry,
    write_run_log,
)
from shared.calendar_service import get_calendar
from shared.blackbox_v2.contracts import load_metadata
from shared.blackbox_v2.requests import build_live_request
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.input_artifacts import (
    BLACKBOX_SCHEMA_PATH,
    LIVE_SOURCE_FEATURE_DATE_ENV,
    LIVE_SOURCE_FENCE_GENERATION_ID_ENV,
    LIVE_SOURCE_INPUT_MODE,
    LIVE_SOURCE_INPUT_MODE_ENV,
    LIVE_SOURCE_PACKAGE_SHA256_ENV,
    NATIVE_BUSINESS_DATE_ENV,
    NATIVE_FEATURE_DATE_ENV,
    NATIVE_GENERATION_ID_ENV,
    NATIVE_INPUT_MODE,
    NATIVE_INPUT_MODE_ENV,
    NATIVE_MANIFEST_PATH_ENV,
    NATIVE_MANIFEST_SHA256_ENV,
    capture_blackbox_platform_inputs_from_connection,
    capture_blackbox_platform_inputs_from_native_generation,
    open_blackbox_input_snapshot,
    open_blackbox_runtime_view,
    resolve_blackbox_input_cutoffs,
)
from shared.databridge_input_generation import (
    DataBridgeGenerationContext,
    open_databridge_generation,
)
from shared.daily_coordinator_mode import (
    DAILY_COORDINATOR_MODE_ENV,
    DailyCoordinatorModeMissingError,
    bootstrap_deployment_daily_coordinator_mode,
    require_daily_coordinator_mode,
)
from shared.models import PredictionRecord
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    NativeGenerationContext,
    open_native_generation,
)
from shared.liwei_0616_cache_contract import (
    CACHE_MUTATION_POLICY_ENV,
    CACHE_MUTATION_POLICY_HIT_ONLY,
    CACHE_USE_QUALIFICATION_ENV,
    DIRECT_CACHE_RUNTIME_CONTEXT_ENV,
    canonical_json_bytes as canonical_cache_contract_json_bytes,
    validate_direct_cache_runtime_context,
    validate_trusted_cache_use_qualification,
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
NATIVE_EXECUTION_MODE_SCHEDULED = "scheduled"
NATIVE_EXECUTION_MODE_SIGNAL_GAP_CURRENT_SNAPSHOT = (
    "signal_gap_current_snapshot"
)
NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED = "signal_gap_archived"
_NATIVE_EXECUTION_MODES = frozenset(
    {
        NATIVE_EXECUTION_MODE_SCHEDULED,
        NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED,
        NATIVE_EXECUTION_MODE_SIGNAL_GAP_CURRENT_SNAPSHOT,
    }
)
VALID_PREDICTION_PHASES = {"gray_live", "scheduled_live"}
BLACKBOX_SNAPSHOT_MODE_FRESH = "fresh"
BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF = "historical_as_of_replay"
VALID_BLACKBOX_SNAPSHOT_MODES = {
    BLACKBOX_SNAPSHOT_MODE_FRESH,
    BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
}
TIMEOUT_OUTPUT_DRAIN_SEC = 1
SCHEDULE_EXECUTION_TOKEN_ENV = "BOND_SCHEDULE_EXECUTION_TOKEN"
_SAFE_EXECUTION_TOKEN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
logger = logging.getLogger(__name__)
PLATFORM_CONFIGURATION_ERROR_PREFIX = "platform configuration error:"

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
        "MPLCONFIGDIR",
        "PYTHONDONTWRITEBYTECODE",
        DAILY_COORDINATOR_MODE_ENV,
        "LIWEI_0616_PHASE_A_CACHE_ROOT",
        CACHE_MUTATION_POLICY_ENV,
        "DAILY_0629_SOURCE_CACHE_DISABLE",
        "DAILY_0629_SOURCE_CACHE_DIR",
        "DAILY_0629_SOURCE_TIMEOUT_SEC",
        "DAILY_0629_SOURCE_PYTHON",
        "DAILY_N_JOBS",
        "DAILY_BACKTEST_WORKERS",
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
        confidence=float(item["confidence"]) if item.get("confidence") is not None else None,
        model_version=str(item["model_version"]) if item.get("model_version") is not None else None,
        extra=item.get("extra") or None,
        run_id=int(item["run_id"]) if item.get("run_id") is not None else None,
        scheme_version=str(item["scheme_version"]) if item.get("scheme_version") is not None else None,
    )


def _validated_execution_token(
    execution_token: str | None,
) -> str | None:
    """校验调度 attempt token，禁止作为任意环境变量载荷。"""
    if execution_token is None:
        return None
    if (
        not isinstance(execution_token, str)
        or not execution_token
        or len(execution_token) > 128
        or any(
            char not in _SAFE_EXECUTION_TOKEN_CHARACTERS
            for char in execution_token
        )
    ):
        raise ValueError("execution_token is unsafe")
    return execution_token


def _validated_live_source_package_sha256(
    value: str | None,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("live source package sha256 is invalid")
    return value


def run_scheme_subprocess(
    scheme_id: str,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
    native_generation: NativeGenerationContext | None = None,
    live_source_compatibility: bool = False,
    live_source_package_sha256: str | None = None,
    cache_use_qualification: dict[str, object] | None = None,
    direct_cache_runtime_context: dict[str, object] | None = None,
    execution_token: str | None = None,
    source_database_config: (
        SourceRuntimeDatabaseConfig | None
    ) = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    native_execution_mode: str = NATIVE_EXECUTION_MODE_SCHEDULED,
    expected_native_feature_date: str | None = None,
    strip_daily_coordinator_mode: bool = False,
) -> list[PredictionRecord]:
    """通过 conda 子进程在算法环境中运行方案。"""
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    if native_execution_mode not in _NATIVE_EXECUTION_MODES:
        raise ValueError(
            "unsupported Native execution mode: "
            f"{native_execution_mode}"
        )
    is_signal_gap_execution = (
        native_execution_mode != NATIVE_EXECUTION_MODE_SCHEDULED
    )
    if is_signal_gap_execution and native_generation is None:
        raise ValueError(
            "signal-gap Native execution requires native_generation"
        )
    if (
        native_execution_mode == NATIVE_EXECUTION_MODE_SCHEDULED
        and expected_native_feature_date is not None
    ):
        raise ValueError(
            "expected_native_feature_date is only valid for signal-gap "
            "Native execution"
        )
    env = _build_algorithm_environment()
    if strip_daily_coordinator_mode:
        env.pop(DAILY_COORDINATOR_MODE_ENV, None)
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
    native_environment_names = (
        NATIVE_INPUT_MODE_ENV,
        NATIVE_MANIFEST_PATH_ENV,
        NATIVE_GENERATION_ID_ENV,
        NATIVE_MANIFEST_SHA256_ENV,
        NATIVE_BUSINESS_DATE_ENV,
        NATIVE_FEATURE_DATE_ENV,
    )
    for name in native_environment_names:
        env.pop(name, None)
    live_source_environment_names = (
        LIVE_SOURCE_INPUT_MODE_ENV,
        LIVE_SOURCE_FENCE_GENERATION_ID_ENV,
        LIVE_SOURCE_FEATURE_DATE_ENV,
        LIVE_SOURCE_PACKAGE_SHA256_ENV,
    )
    for name in live_source_environment_names:
        env.pop(name, None)
    env.pop(CACHE_USE_QUALIFICATION_ENV, None)
    env.pop(DIRECT_CACHE_RUNTIME_CONTEXT_ENV, None)
    env.pop(CACHE_MUTATION_POLICY_ENV, None)
    env.pop(SCHEDULE_EXECUTION_TOKEN_ENV, None)
    if is_signal_gap_execution:
        env[CACHE_MUTATION_POLICY_ENV] = (
            CACHE_MUTATION_POLICY_HIT_ONLY
        )
    validated_execution_token = _validated_execution_token(
        execution_token
    )
    if validated_execution_token is not None:
        env[SCHEDULE_EXECUTION_TOKEN_ENV] = validated_execution_token
    if live_source_compatibility and native_generation is None:
        raise ValueError(
            "live source compatibility requires a frozen Native fence"
        )
    if live_source_compatibility and (
        scheme_id not in APPROVED_0629_LIVE_SOURCE_SCHEMES
    ):
        raise ValueError(
            f"live source compatibility is not approved for {scheme_id}"
        )
    validated_source_package_sha256 = (
        _validated_live_source_package_sha256(
            live_source_package_sha256
        )
    )
    if (
        live_source_compatibility
        and validated_source_package_sha256 is None
    ):
        raise ValueError(
            "live source compatibility requires a frozen source package "
            "sha256"
        )
    if (
        not live_source_compatibility
        and validated_source_package_sha256 is not None
    ):
        raise ValueError(
            "live source package sha256 is only valid for compatibility "
            "mode"
        )
    if native_generation is not None:
        if not isinstance(native_generation, NativeGenerationContext):
            raise TypeError(
                "native_generation must be a validated "
                "NativeGenerationContext"
            )
        canonical_predict_date = date.fromisoformat(
            predict_date
        ).isoformat()
        if native_execution_mode == NATIVE_EXECUTION_MODE_SCHEDULED:
            if (
                native_generation.exporter_version
                == SIGNAL_GAP_NATIVE_EXPORTER_VERSION
            ):
                raise ValueError(
                    "signal-gap Native exporter requires explicit "
                    "signal-gap execution mode"
                )
            if native_generation.business_date != canonical_predict_date:
                raise ValueError(
                    "Native generation business_date does not match "
                    f"predict_date: {native_generation.business_date} != "
                    f"{canonical_predict_date}"
                )
        elif (
            native_execution_mode
            == NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED
        ):
            if (
                native_generation.exporter_version
                != NATIVE_GENERATION_EXPORTER_VERSION
            ):
                raise ValueError(
                    "archived signal-gap Native generation "
                    "exporter_version is invalid"
                )
            if (
                native_generation.business_date
                != canonical_predict_date
            ):
                raise ValueError(
                    "archived signal-gap Native generation "
                    "business_date does not match predict_date"
                )
            canonical_expected_feature_date = (
                _canonical_signal_gap_feature_date(
                    expected_native_feature_date
                )
            )
            if (
                native_generation.feature_date
                != canonical_expected_feature_date
            ):
                raise ValueError(
                    "signal-gap Native feature_date does not match "
                    "expected feature_date"
                )
        else:
            if (
                native_generation.exporter_version
                != SIGNAL_GAP_NATIVE_EXPORTER_VERSION
            ):
                raise ValueError(
                    "signal-gap Native generation exporter_version "
                    "is invalid"
                )
            if native_generation.business_date <= canonical_predict_date:
                raise ValueError(
                    "signal-gap Native capture date must be after "
                    "predict_date"
                )
            canonical_expected_feature_date = (
                _canonical_signal_gap_feature_date(
                    expected_native_feature_date
                )
            )
            if (
                native_generation.feature_date
                != canonical_expected_feature_date
            ):
                raise ValueError(
                    "signal-gap Native feature_date does not match "
                    "expected feature_date"
                )
        if not native_generation.manifest_path.is_absolute():
            raise ValueError(
                "Native generation manifest path must be absolute"
            )
        if live_source_compatibility:
            env.update(
                {
                    LIVE_SOURCE_INPUT_MODE_ENV:
                        LIVE_SOURCE_INPUT_MODE,
                    LIVE_SOURCE_FENCE_GENERATION_ID_ENV:
                        native_generation.generation_id,
                    LIVE_SOURCE_FEATURE_DATE_ENV:
                        native_generation.feature_date,
                    LIVE_SOURCE_PACKAGE_SHA256_ENV:
                        validated_source_package_sha256,
                    # 兼容桥每个 attempt 使用独立 source runtime，避免
                    # 多进程竞争旧 runner 的共享临时 cache 文件。
                    "DAILY_0629_SOURCE_CACHE_DISABLE": "1",
                }
            )
        else:
            env.update(
                {
                    NATIVE_INPUT_MODE_ENV: NATIVE_INPUT_MODE,
                    NATIVE_MANIFEST_PATH_ENV:
                        str(native_generation.manifest_path),
                    NATIVE_GENERATION_ID_ENV:
                        native_generation.generation_id,
                    NATIVE_MANIFEST_SHA256_ENV:
                        native_generation.manifest_sha256,
                    NATIVE_BUSINESS_DATE_ENV:
                        native_generation.business_date,
                    NATIVE_FEATURE_DATE_ENV:
                        native_generation.feature_date,
                }
            )
    if cache_use_qualification is not None:
        if live_source_compatibility:
            raise ValueError(
                "live source compatibility cannot use frozen cache "
                "qualification"
            )
        try:
            trusted_cache_qualification = (
                validate_trusted_cache_use_qualification(
                    cache_use_qualification,
                    expected_base_scheme_id=scheme_id,
                )
            )
        except ValueError as exc:
            raise ValueError(
                "cache_use_qualification is invalid"
            ) from exc
        if native_generation is None:
            raise ValueError(
                "cache_use_qualification requires Native generation"
            )
        env[CACHE_USE_QUALIFICATION_ENV] = (
            canonical_cache_contract_json_bytes(
                trusted_cache_qualification
            ).decode("ascii")
        )
    if direct_cache_runtime_context is not None:
        if cache_use_qualification is not None:
            raise ValueError(
                "legacy cache qualification and direct cache runtime "
                "context are mutually exclusive"
            )
        if live_source_compatibility:
            raise ValueError(
                "live source compatibility cannot use direct cache "
                "runtime context"
            )
        try:
            validated_direct_context = (
                validate_direct_cache_runtime_context(
                    direct_cache_runtime_context
                )
            )
        except ValueError as exc:
            raise ValueError(
                "direct_cache_runtime_context is invalid"
            ) from exc
        if (
            native_generation is None
            or validated_direct_context["consumer"][
                "base_scheme_id"
            ] != scheme_id
        ):
            raise ValueError(
                "direct_cache_runtime_context requires the exact Native "
                "scheme and generation"
            )
        env[DIRECT_CACHE_RUNTIME_CONTEXT_ENV] = (
            canonical_cache_contract_json_bytes(
                validated_direct_context
            ).decode("ascii")
        )
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


def _canonical_signal_gap_feature_date(value: str | None) -> str:
    if value is None:
        raise ValueError(
            "expected_native_feature_date is required for "
            "signal-gap Native execution"
        )
    try:
        canonical = date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "expected_native_feature_date must be a canonical "
            "YYYY-MM-DD date"
        ) from exc
    if canonical != value:
        raise ValueError(
            "expected_native_feature_date must be a canonical "
            "YYYY-MM-DD date"
        )
    return canonical


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
    timeout_sec: int,
    blackbox_snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    expected_generation_id: str | None = None,
    expected_refresh_date: str | None = None,
    native_generation: NativeGenerationContext | None = None,
    live_source_compatibility: bool = False,
    live_source_package_sha256: str | None = None,
    cache_use_qualification: dict[str, object] | None = None,
    direct_cache_runtime_context: dict[str, object] | None = None,
    databridge_generation: DataBridgeGenerationContext | None = None,
    calendar_generation: NativeGenerationContext | None = None,
    execution_token: str | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    native_execution_mode: str = NATIVE_EXECUTION_MODE_SCHEDULED,
    expected_native_feature_date: str | None = None,
    strip_daily_coordinator_mode: bool = False,
) -> list[PredictionRecord]:
    """按显式 runtime_type 选择算法执行驱动。"""
    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    validated_execution_token = _validated_execution_token(
        execution_token
    )
    if blackbox_snapshot_mode not in VALID_BLACKBOX_SNAPSHOT_MODES:
        raise ValueError(f"unsupported Blackbox snapshot mode: {blackbox_snapshot_mode}")
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type != "native_adapter" and (
        native_execution_mode != NATIVE_EXECUTION_MODE_SCHEDULED
        or expected_native_feature_date is not None
    ):
        raise ValueError(
            "Native execution contract is only valid for native_adapter"
        )
    if runtime_type == "native_adapter":
        if (
            databridge_generation is not None
            or calendar_generation is not None
        ):
            raise ValueError(
                "DataBridge/calendar generation is not valid for "
                "native_adapter"
            )
        if blackbox_snapshot_mode != BLACKBOX_SNAPSHOT_MODE_FRESH:
            raise ValueError(
                "historical Blackbox snapshot mode is not valid for native_adapter"
            )
        native_kwargs = (
            {"native_generation": native_generation}
            if native_generation is not None
            else {}
        )
        if native_execution_mode != NATIVE_EXECUTION_MODE_SCHEDULED:
            native_kwargs["native_execution_mode"] = (
                native_execution_mode
            )
        if expected_native_feature_date is not None:
            native_kwargs["expected_native_feature_date"] = (
                expected_native_feature_date
            )
        if strip_daily_coordinator_mode:
            native_kwargs["strip_daily_coordinator_mode"] = True
        if cache_use_qualification is not None:
            native_kwargs["cache_use_qualification"] = (
                cache_use_qualification
            )
        if direct_cache_runtime_context is not None:
            native_kwargs["direct_cache_runtime_context"] = (
                direct_cache_runtime_context
            )
        if live_source_compatibility:
            native_kwargs["live_source_compatibility"] = True
            native_kwargs["live_source_package_sha256"] = (
                live_source_package_sha256
            )
        elif live_source_package_sha256 is not None:
            raise ValueError(
                "live source package sha256 is only valid for "
                "compatibility mode"
            )
        if validated_execution_token is not None:
            native_kwargs["execution_token"] = validated_execution_token
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
        if live_source_compatibility:
            raise ValueError(
                "live source compatibility is not valid for blackbox_v2"
            )
        if live_source_package_sha256 is not None:
            raise ValueError(
                "live source package sha256 is not valid for blackbox_v2"
            )
        if cache_use_qualification is not None:
            raise ValueError(
                "cache_use_qualification is not valid for blackbox_v2"
            )
        if direct_cache_runtime_context is not None:
            raise ValueError(
                "direct_cache_runtime_context is not valid for blackbox_v2"
            )
        if native_generation is not None:
            raise ValueError(
                "Native generation is not valid for blackbox_v2"
            )
        if getattr(cfg, "input_source", None) != "data_bridge_current":
            raise ValueError(f"Blackbox V2 input_source must be data_bridge_current: {cfg.scheme_id}")
        blackbox_kwargs = {
            "engine": engine,
            "algo_env": algo_env,
            "timeout_sec": timeout_sec,
            "snapshot_mode": blackbox_snapshot_mode,
        }
        if databridge_generation is not None:
            blackbox_kwargs["databridge_generation"] = (
                databridge_generation
            )
            blackbox_kwargs["calendar_generation"] = calendar_generation
        elif calendar_generation is not None:
            raise ValueError(
                "calendar_generation requires databridge_generation"
            )
        if blackbox_snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
            blackbox_kwargs["expected_generation_id"] = expected_generation_id
            blackbox_kwargs["expected_refresh_date"] = expected_refresh_date
        if validated_execution_token is not None:
            blackbox_kwargs["execution_token"] = (
                validated_execution_token
            )
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
    timeout_sec: int,
    snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    expected_generation_id: str | None = None,
    expected_refresh_date: str | None = None,
    databridge_generation: DataBridgeGenerationContext | None = None,
    calendar_generation: NativeGenerationContext | None = None,
    execution_token: str | None = None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    process_start_guard: ProcessStartGuard | None = None,
) -> list[PredictionRecord]:
    """生成平台输入并通过 Blackbox V2 CLI 执行一个实盘 Request。"""
    from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, run_blackbox_predict

    process_start_guard = require_process_start_guard(
        process_start_guard
    )
    validated_execution_token = _validated_execution_token(
        execution_token
    )
    if snapshot_mode not in VALID_BLACKBOX_SNAPSHOT_MODES:
        raise ValueError(f"unsupported Blackbox snapshot mode: {snapshot_mode}")
    if cfg.delivery_script is None or cfg.delivery_metadata is None:
        raise ValueError(f"Blackbox V2 delivery paths missing for {cfg.scheme_id}")
    bound_databridge: DataBridgeGenerationContext | None = None
    if databridge_generation is not None:
        if snapshot_mode != BLACKBOX_SNAPSHOT_MODE_FRESH:
            raise ValueError(
                "bound DataBridge generation only supports fresh live mode"
            )
        if not isinstance(
            databridge_generation,
            DataBridgeGenerationContext,
        ):
            raise TypeError(
                "databridge_generation must be a validated context"
            )
        if not isinstance(calendar_generation, NativeGenerationContext):
            raise ValueError(
                "bound DataBridge generation requires a validated "
                "calendar Native generation"
            )
        if databridge_generation.business_date != predict_date:
            raise ValueError(
                "DataBridge generation business_date does not match "
                f"predict_date: {databridge_generation.business_date} != "
                f"{predict_date}"
            )
        if (
            calendar_generation.generation_id
            != databridge_generation.native_generation_id
        ):
            raise ValueError(
                "DataBridge generation linked Native generation mismatch"
            )
        if (
            calendar_generation.manifest_sha256
            != databridge_generation.native_manifest_sha256
        ):
            raise ValueError(
                "DataBridge generation linked Native manifest mismatch"
            )
        verified_calendar = open_native_generation(
            calendar_generation.manifest_path,
            expected_generation_id=calendar_generation.generation_id,
            expected_manifest_sha256=calendar_generation.manifest_sha256,
            expected_business_date=predict_date,
            expected_feature_date=databridge_generation.feature_date,
        )
        bound_databridge = open_databridge_generation(
            databridge_generation.manifest_path,
            expected_generation_id=databridge_generation.generation_id,
            expected_manifest_sha256=(
                databridge_generation.manifest_sha256
            ),
            expected_business_date=predict_date,
            expected_feature_date=databridge_generation.feature_date,
            schema_path=BLACKBOX_SCHEMA_PATH,
        )
        snapshot_context = nullcontext(bound_databridge.snapshot)
        calendar_source = verified_calendar
    else:
        if calendar_generation is not None:
            raise ValueError(
                "calendar_generation requires databridge_generation"
            )
        require_fresh = snapshot_mode == BLACKBOX_SNAPSHOT_MODE_FRESH
        snapshot_context = open_blackbox_input_snapshot(
            snapshot_date=predict_date,
            require_fresh=require_fresh,
        )
        calendar_source = engine

    metadata = load_metadata(cfg.delivery_metadata)
    with snapshot_context as snapshot:
        if snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
            _validate_historical_snapshot(
                snapshot,
                predict_date=predict_date,
                expected_generation_id=expected_generation_id,
                expected_refresh_date=expected_refresh_date,
            )
        calendar = get_calendar(calendar_source)
        if metadata.frequency == "daily":
            feature_date = build_daily_live_context(
                calendar,
                predict_date,
                horizon=metadata.horizon,
            ).feature_date
        elif metadata.frequency == "weekly":
            feature_date = build_weekly_live_context(calendar, predict_date).feature_date
        else:
            feature_date = build_monthly_live_context(calendar, predict_date).feature_date
        if bound_databridge is not None:
            if feature_date != bound_databridge.feature_date:
                raise ValueError(
                    "DataBridge generation feature_date does not match "
                    f"calendar context: {bound_databridge.feature_date} != "
                    f"{feature_date}"
                )
            cutoffs = bound_databridge.cutoffs
        else:
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
        platform_input_ids = tuple(
            getattr(cfg, "platform_inputs", ()) or ()
        )
        if platform_input_ids and bound_databridge is not None:
            platform_input_artifacts = (
                capture_blackbox_platform_inputs_from_native_generation(
                    platform_input_ids,
                    native_generation=verified_calendar,
                    weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                )
            )
        elif platform_input_ids:
            platform_input_artifacts = (
                _capture_blackbox_platform_inputs_from_engine(
                    engine,
                    platform_input_ids=platform_input_ids,
                    weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                )
            )
        else:
            platform_input_artifacts = ()
        input_bundle = compose_blackbox_input_bundle(
            snapshot,
            platform_input_ids=platform_input_ids,
            platform_input_artifacts=platform_input_artifacts,
        )
        blackbox_env = DEFAULT_RUNTIME_PROFILE.conda_env if algo_env == DEFAULT_ALGO_ENV else algo_env
        profile = replace(
            DEFAULT_RUNTIME_PROFILE,
            conda_env=blackbox_env,
            predict_timeout_sec=timeout_sec,
        )
        with open_blackbox_runtime_view(input_bundle) as runtime_view:
            trusted_bundle = runtime_view.bundle
            predict_kwargs = {
                "metadata": metadata,
                "script_path": cfg.delivery_script,
                "request": request,
                "data_dir": runtime_view.data_dir,
                "data_snapshot_id":
                    trusted_bundle.combined_snapshot_id,
                "platform_input_ids":
                    trusted_bundle.platform_input_ids,
                "profile": profile,
            }
            if trusted_bundle.platform_input_ids:
                predict_kwargs.update(
                    {
                        "parent_data_snapshot_id":
                            trusted_bundle.parent_snapshot_id,
                        "input_identity_manifest":
                            trusted_bundle.identity_manifest,
                        "input_audit_manifest":
                            trusted_bundle.audit_manifest,
                    }
                )
            if validated_execution_token is not None:
                predict_kwargs["execution_token"] = (
                    validated_execution_token
                )
            if process_started is not None:
                predict_kwargs["process_started"] = process_started
            if process_fence is not None:
                predict_kwargs["process_fence"] = process_fence
            if process_start_guard is not None:
                predict_kwargs["process_start_guard"] = (
                    process_start_guard
                )
            try:
                record = run_blackbox_predict(**predict_kwargs)
            except ProcessGroupTerminationError:
                runtime_view.mark_termination_uncertain()
                raise
        if bound_databridge is not None:
            extra = dict(record.extra or {})
            extra.update(
                {
                    "data_generation_id":
                        bound_databridge.generation_id,
                    "data_generation_manifest_sha256":
                        bound_databridge.manifest_sha256,
                    "source_refresh_date":
                        bound_databridge.business_date,
                    "upstream_data_generation_id":
                        bound_databridge.upstream_generation_id,
                    "native_generation_id":
                        bound_databridge.native_generation_id,
                    "daily_cutoff_key": cutoffs.daily_cutoff_key,
                    "weekly_cutoff_key": cutoffs.weekly_cutoff_key,
                    "monthly_cutoff_key": cutoffs.monthly_cutoff_key,
                }
            )
            record = replace(record, extra=extra)
        if snapshot_mode == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF:
            extra = dict(record.extra or {})
            extra.update(
                {
                    "replay_semantics": (
                        "current_snapshot_as_of_not_historical_vintage"
                    ),
                    "backfill_mode": "post_deployment_live_safe_replay",
                    "backfilled_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "data_generation_id": snapshot.generation_id,
                    "source_refresh_date": snapshot.refresh_date,
                    "daily_cutoff_key": cutoffs.daily_cutoff_key,
                    "weekly_cutoff_key": cutoffs.weekly_cutoff_key,
                    "monthly_cutoff_key": cutoffs.monthly_cutoff_key,
                }
            )
            record = replace(record, extra=extra)
    return [record]


def _capture_blackbox_platform_inputs_from_engine(
    engine,
    *,
    platform_input_ids: tuple[str, ...],
    weekly_cutoff_key: str,
):
    """从显式只读一致性事务捕获非 scheduled 平台输入。"""
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            return capture_blackbox_platform_inputs_from_connection(
                platform_input_ids,
                connection=connection,
                weekly_cutoff_key=weekly_cutoff_key,
            )
        finally:
            connection.rollback()


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
    if predict_date >= refresh_date:
        raise ValueError(
            "historical Blackbox snapshot requires predict_date before refresh_date: "
            f"predict_date={predict_date}, refresh_date={refresh_date}"
        )
    if expected_generation_id is not None and generation_id != expected_generation_id:
        raise ValueError(
            "DataBridge current generation changed after gray-backfill preflight: "
            f"expected={expected_generation_id}, actual={generation_id}"
        )
    if expected_refresh_date is not None and refresh_date != expected_refresh_date:
        raise ValueError(
            "DataBridge current refresh_date changed after gray-backfill preflight: "
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
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        termination = _terminate_process_group(
            process,
            process_group_id=process_group_id,
        )
        stdout, stderr = _drain_timed_out_process_output(process)
        if not termination.confirmed_gone:
            raise ProcessGroupTerminationError(
                termination=termination,
                context=f"Native process timed out after {timeout}s",
            ) from exc
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from exc
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    process_group_id: int | None = None,
) -> ProcessGroupTerminationResult:
    """终止进程组并返回“整个组已消失”的明确证据。"""
    return terminate_process_group(
        process,
        process_group_id=process_group_id,
    )


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


def execute_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
    prediction_phase: str = "scheduled_live",
    scheduled_control_plane: str = "direct_scheduled",
    blackbox_precommit_validator: Callable[[object], None] | None = None,
    blackbox_snapshot_mode: str = BLACKBOX_SNAPSHOT_MODE_FRESH,
    blackbox_expected_generation_id: str | None = None,
    blackbox_expected_refresh_date: str | None = None,
    blackbox_record_validator: Callable[[list[PredictionRecord]], None] | None = None,
) -> SchemeRunResult:
    """执行单个方案并写入预测表和运行日志。

    执行前校验：
    1. config.yaml status == 'active'（本地配置）
    2. Native 要求 Registry active 且精确版本状态仅为 active
    3. Blackbox 要求 exact active 版本、批准人与 composite Registry 身份全部一致
    """
    if prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}")
    if prediction_phase == "scheduled_live":
        configuration_error = (
            _scheduled_live_execution_configuration_error(
                cfg,
                scheduled_control_plane=scheduled_control_plane,
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
    engine = create_engine_from_env()
    started = time.monotonic()
    if cfg.status != "active":
        duration = time.monotonic() - started
        write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, f"status={cfg.status}")
        engine.dispose()
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, f"status={cfg.status}")

    scheme_version = getattr(cfg, "scheme_version", None)
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type == "blackbox_v2":
        config_version_status = getattr(cfg, "version_status", None)
        if config_version_status != "active":
            reason = (
                "Blackbox V2 config version_status is "
                f"{config_version_status}, expected active"
            )
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
        try:
            from shared.blackbox_v2.lifecycle import assert_lifecycle_clear

            cfg_path = getattr(cfg, "path", None)
            project_root = Path(cfg_path).parents[1] if cfg_path is not None else Path(__file__).resolve().parents[1]
            assert_lifecycle_clear(project_root, cfg.scheme_id)
        except RuntimeError as exc:
            reason = str(exc)
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
        approval = read_blackbox_execution_approval(engine, cfg)
        if not approval.executable:
            reason = f"Blackbox V2 version is not production-approved: {approval.reason}"
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
    else:
        ok, reason = _verify_scheme_activation(engine, cfg.scheme_id, scheme_version)
        if not ok:
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, reason)
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
            if scheduled_control_plane == "launchd_one_shot":
                creation_fence[
                    "scheduled_control_plane"
                ] = scheduled_control_plane
            elif frequency not in {"weekly", "monthly"}:
                coordinator_mode = require_daily_coordinator_mode()
                if coordinator_mode == "ledger":
                    creation_fence["schedule_frequency"] = frequency
                    creation_fence[
                        "enforce_scheduled_live_ledger"
                    ] = True
        run_id = create_scheme_run(
            engine,
            scheme_id=cfg.scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            run_type="active",
            prediction_phase=prediction_phase,
            records_expected=len(active_targets),
            **creation_fence,
        )
        if not active_targets:
            raise ValueError(
                f"active registry targets empty for scheme {cfg.scheme_id}: "
                "missing=[], extra=[], duplicates=[]"
            )
        effective_timeout_sec = _effective_timeout_sec(cfg, timeout_sec)
        run_kwargs = {
            "engine": engine,
            "algo_env": algo_env,
            "timeout_sec": effective_timeout_sec,
        }
        if (
            prediction_phase == "scheduled_live"
            and runtime_type == "native_adapter"
            and scheduled_control_plane == "launchd_one_shot"
        ):
            run_kwargs["strip_daily_coordinator_mode"] = True
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
            records_written = complete_approved_blackbox_run(
                engine,
                cfg,
                run_id=run_id,
                records=records,
                scheme_version=scheme_version,
                records_returned=records_returned,
                run_date=predict_date,
                duration_sec=duration,
                precommit_validator=blackbox_precommit_validator,
                insert_only_predictions=(
                    blackbox_snapshot_mode
                    == BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF
                ),
            )
            return SchemeRunResult(
                cfg.scheme_id,
                "success",
                records_written,
                duration,
                None,
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
        error_msg = str(exc)
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
        return SchemeRunResult(cfg.scheme_id, "failed", records_written, duration, error_msg, run_id)
    finally:
        engine.dispose()


def _scheduled_live_execution_configuration_error(
    cfg: SchemeConfig,
    *,
    scheduled_control_plane: str,
) -> str | None:
    """在任何数据库或子进程副作用前校验低层 scheduled_live 入口。"""
    from scheduler.blackbox_scheduler_admission import (
        DIRECT_SCHEDULED,
        LAUNCHD_ONE_SHOT,
        ScheduledPredictionConfigurationError,
        ScheduledPredictionControlPlaneDenied,
        require_scheduled_prediction_control_plane,
    )

    canonical_error = _scheduled_live_canonical_configuration_error(
        cfg
    )
    if canonical_error is not None:
        return canonical_error

    try:
        require_scheduled_prediction_control_plane(
            cfg,
            plane=scheduled_control_plane,
        )
    except ScheduledPredictionControlPlaneDenied:
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            "scheduled_live direct control plane denied: "
            f"scheme_id={cfg.scheme_id}"
        )
    except ScheduledPredictionConfigurationError:
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            "scheduled_live admission configuration is invalid: "
            f"scheme_id={cfg.scheme_id}"
        )

    if scheduled_control_plane == LAUNCHD_ONE_SHOT:
        return None
    if scheduled_control_plane != DIRECT_SCHEDULED:
        return (
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            "scheduled_live control plane is invalid: "
            f"scheme_id={cfg.scheme_id}"
        )
    return (
        f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
        "scheduled_live without schedule_item_id requires "
        f"launchd_one_shot: scheme_id={cfg.scheme_id}"
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
    fields = (
        "scheme_id",
        "scheme_version",
        "runtime_type",
        "frequency",
        "task_type",
        "horizon",
        "status",
        "version_status",
    )
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


def _effective_timeout_sec(cfg: SchemeConfig, default_timeout_sec: int) -> int:
    """读取执行 timeout；Blackbox 的调用方 policy 值是不可放宽上限。"""
    schedule = getattr(cfg, "schedule", None)
    configured = getattr(schedule, "timeout_sec", None)
    if configured is None:
        configured = getattr(cfg, "execution_timeout_sec", None)
    timeout = int(configured) if configured is not None else int(default_timeout_sec)
    if getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2":
        timeout = min(timeout, int(default_timeout_sec))
    if timeout <= 0:
        raise ValueError(f"scheme {cfg.scheme_id} timeout_sec must be positive, got {timeout}")
    return timeout


def execute_all(
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    include_paused: bool = False,
    prediction_phase: str = "scheduled_live",
    scheduled_control_plane: str = "direct_scheduled",
) -> list[SchemeRunResult]:
    """执行全部方案；正式调度先完成零写入 admission 快照。"""
    if prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(
            "prediction_phase must be one of "
            f"{sorted(VALID_PREDICTION_PHASES)}, got "
            f"{prediction_phase}"
        )
    schemes = discover_schemes()
    runnable = (
        schemes
        if include_paused
        else [cfg for cfg in schemes if cfg.status == "active"]
    )
    if prediction_phase == "scheduled_live":
        return _execute_all_scheduled(
            runnable,
            predict_date=predict_date,
            algo_env=algo_env,
            scheduled_control_plane=scheduled_control_plane,
        )

    engine = create_engine_from_env()
    try:
        sync_scheme_registry(engine, schemes)
    finally:
        engine.dispose()

    return [
        execute_scheme(
            cfg,
            predict_date,
            algo_env=algo_env,
            prediction_phase=prediction_phase,
        )
        for cfg in runnable
    ]


def _execute_all_scheduled(
    runnable: list[SchemeConfig],
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
) -> list[SchemeRunResult]:
    """冻结指定 scheduled 控制面 policy 后按发现顺序执行或拒绝。"""
    from scheduler.blackbox_scheduler_admission import (
        DIRECT_SCHEDULED,
        BlackboxSchedulerAdmissionError,
        ScheduledPredictionConfigurationError,
        ScheduledPredictionControlPlaneDenied,
        load_blackbox_scheduler_admission,
        require_scheduled_prediction_control_plane_with_policy_snapshot,
        uses_blackbox_scheduler_admission,
    )

    canonical_snapshot_invalid = False
    try:
        canonical_schemes = discover_schemes()
    except (OSError, RuntimeError, ValueError):
        canonical_schemes = []
        canonical_snapshot_invalid = True
    canonical_by_scheme_id: dict[str, list[SchemeConfig]] = {}
    for canonical in canonical_schemes:
        canonical_by_scheme_id.setdefault(
            canonical.scheme_id,
            [],
        ).append(canonical)

    controlled = [
        config
        for config in runnable
        if uses_blackbox_scheduler_admission(config)
    ]
    policy = None
    policy_invalid = False
    if controlled:
        try:
            policy = load_blackbox_scheduler_admission()
        except BlackboxSchedulerAdmissionError:
            policy_invalid = True

    daily_like_present = any(
        getattr(config, "frequency", None)
        not in {"weekly", "monthly"}
        for config in runnable
    )
    coordinator_mode: str | None = None
    coordinator_mode_invalid = False
    if (
        daily_like_present
        and scheduled_control_plane == DIRECT_SCHEDULED
    ):
        try:
            coordinator_mode = (
                bootstrap_deployment_daily_coordinator_mode()
            )
        except (
            DailyCoordinatorModeMissingError,
            RuntimeError,
            ValueError,
        ):
            coordinator_mode_invalid = True

    plan: list[
        tuple[SchemeConfig, SchemeRunResult | None]
    ] = []
    for config in runnable:
        failure: SchemeRunResult | None = None
        canonical_matches = canonical_by_scheme_id.get(
            config.scheme_id,
            [],
        )
        if (
            canonical_snapshot_invalid
            or len(canonical_matches) != 1
            or not _scheduled_config_matches_canonical(
                config,
                canonical_matches[0],
            )
        ):
            failure = (
                _scheduled_aggregate_configuration_failure(
                    config,
                    detail=(
                        "canonical configuration is unavailable "
                        "or drifted"
                    ),
                )
            )
        elif uses_blackbox_scheduler_admission(config):
            if policy_invalid or policy is None:
                failure = (
                    _scheduled_aggregate_configuration_failure(
                        config,
                        detail=(
                            "admission configuration is invalid"
                        ),
                    )
                )
            else:
                try:
                    require_scheduled_prediction_control_plane_with_policy_snapshot(
                        config,
                        plane=scheduled_control_plane,
                        policy=policy,
                    )
                except ScheduledPredictionControlPlaneDenied:
                    failure = (
                        _scheduled_aggregate_configuration_failure(
                            config,
                            detail="scheduled control plane denied",
                        )
                    )
                except ScheduledPredictionConfigurationError:
                    failure = (
                        _scheduled_aggregate_configuration_failure(
                            config,
                            detail=(
                                "canonical identity, lifecycle, or "
                                "execution metadata drift"
                            ),
                        )
                    )
        if (
            failure is None
            and scheduled_control_plane == DIRECT_SCHEDULED
            and getattr(config, "frequency", None)
            not in {"weekly", "monthly"}
        ):
            if coordinator_mode_invalid:
                failure = (
                    _scheduled_aggregate_configuration_failure(
                        config,
                        detail=(
                            "daily coordinator mode is unavailable"
                        ),
                    )
                )
            elif coordinator_mode == "ledger":
                failure = (
                    _scheduled_aggregate_configuration_failure(
                        config,
                        detail=(
                            "direct daily execution is disabled in "
                            "ledger mode"
                        ),
                    )
                )
        plan.append((config, failure))

    results: list[SchemeRunResult] = []
    for config, failure in plan:
        if failure is not None:
            results.append(failure)
        else:
            results.append(
                execute_scheme(
                    config,
                    predict_date,
                    algo_env=algo_env,
                    prediction_phase="scheduled_live",
                    scheduled_control_plane=scheduled_control_plane,
                )
            )
    return results


def _scheduled_aggregate_configuration_failure(
    config: SchemeConfig,
    *,
    detail: str,
) -> SchemeRunResult:
    return SchemeRunResult(
        config.scheme_id,
        "failed",
        0,
        0.0,
        f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
        f"scheduled_live {detail}: scheme_id={config.scheme_id}",
    )


def _normalize_live_records(records: list[PredictionRecord], *, prediction_phase: str) -> list[PredictionRecord]:
    """补齐平台级 feature_date / prediction_phase，一处统一控制灰度和正式实盘语义。"""
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
        extra["prediction_phase"] = prediction_phase
        normalized.append(
            replace(
                record,
                feature_date=str(feature_date),
                prediction_phase=prediction_phase,
                extra=extra,
            )
        )
    return normalized


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


def main(
    argv: Sequence[str] | None = None,
) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Execute Bond Factor Lab schemes.")
    parser.add_argument("predict_date", help="Prediction date in YYYY-MM-DD format")
    parser.add_argument("--scheme-id", default=None, help="Only execute one scheme")
    parser.add_argument("--algo-env", default=os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV))
    parser.add_argument("--include-paused", action="store_true", help="Run/skip paused schemes and log skipped rows")
    parser.add_argument(
        "--prediction-phase",
        choices=sorted(VALID_PREDICTION_PHASES),
        default="scheduled_live",
        help="Live prediction phase; gray backfill must pass gray_live explicitly.",
    )
    args = parser.parse_args(argv)

    if args.scheme_id:
        schemes = {cfg.scheme_id: cfg for cfg in discover_schemes()}
        config = schemes.get(args.scheme_id)
        if config is None:
            print(
                f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
                f"scheme not found: {args.scheme_id}",
                file=sys.stderr,
            )
            return 2
        result = execute_scheme(
            config,
            args.predict_date,
            algo_env=args.algo_env,
            prediction_phase=args.prediction_phase,
        )
        print(result)
        return _executor_exit_code([result])

    results = execute_all(
        args.predict_date,
        algo_env=args.algo_env,
        include_paused=args.include_paused,
        prediction_phase=args.prediction_phase,
    )
    for result in results:
        print(result)
    return _executor_exit_code(results)


def _executor_exit_code(
    results: list[SchemeRunResult],
) -> int:
    if any(
        (result.error_msg or "").startswith(
            PLATFORM_CONFIGURATION_ERROR_PREFIX
        )
        for result in results
    ):
        return 2
    if any(
        result.status not in {"success", "skipped"}
        for result in results
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
