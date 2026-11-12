"""统一输入入口；内部实现只向下依赖，不回指此入口。"""

from __future__ import annotations

from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxInputBundle,
    BlackboxSnapshot,
    CutoffKeys,
    compose_blackbox_input_bundle,
    create_snapshot_from_frames,
    normalize_daily_key,
    normalize_period_key,
)
from shared.data_bridge.validation import (
    ValidatedDataBridgeDataset,
)
from shared.data_bridge.validation import (
    LEGACY_FOUR_FILENAMES,
)
from shared._input_artifacts.contract import (
    DEFAULT_OUTPUT_ROOT,
    BLACKBOX_SNAPSHOT_ROOT,
    BLACKBOX_GENERATION_SNAPSHOT_ROOT,
    BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
    LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
    BLACKBOX_RUNTIME_VIEW_ROOT,
    BLACKBOX_SCHEMA_PATH,
    BLACKBOX_SCHEMA_VERSION,
    BLACKBOX_SCHEMA_CONTRACT_SHA256,
    LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
    DAILY_DATA_VERSION,
    WEEKLY_DATA_VERSION,
    MONTHLY_DATA_VERSION,
    EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
    InputArtifact,
)
from shared._input_artifacts.native import (
    create_input_engine,
    input_artifact_path,
    build_daily_input_artifact,
    build_weekly_input_artifact,
    build_monthly_input_artifact,
)
from shared._input_artifacts.cutoffs import (
    resolve_blackbox_input_cutoffs,
    resolve_blackbox_input_cutoffs_bulk,
    validate_blackbox_request_calendar,
)
from shared._input_artifacts.generation import (
    prepare_blackbox_generation_snapshot,
    invalidate_ready_blackbox_snapshot,
    get_ready_blackbox_snapshot,
    _generation_snapshot_cache_key,
)
from shared._input_artifacts.raw_wind import (
    RawWindUnavailableError,
    get_ready_raw_wind_artifact,
    invalidate_raw_wind_sidecar,
    prepare_raw_wind_sidecar,
    raw_wind_for_profile,
)
from shared._input_artifacts.runtime_view import (
    BlackboxRuntimeView,
    open_blackbox_runtime_view,
    cleanup_blackbox_runtime_debris,
)
from shared._input_artifacts.files import (
    _snapshot_file_fingerprints,
)

__all__ = [
    'SNAPSHOT_FILENAMES',
    'BlackboxInputBundle',
    'BlackboxSnapshot',
    'CutoffKeys',
    'compose_blackbox_input_bundle',
    'create_snapshot_from_frames',
    'normalize_daily_key',
    'normalize_period_key',
    'ValidatedDataBridgeDataset',
    'LEGACY_FOUR_FILENAMES',
    'DEFAULT_OUTPUT_ROOT',
    'BLACKBOX_SNAPSHOT_ROOT',
    'BLACKBOX_GENERATION_SNAPSHOT_ROOT',
    'BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION',
    'LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION',
    'BLACKBOX_RUNTIME_VIEW_ROOT',
    'BLACKBOX_SCHEMA_PATH',
    'BLACKBOX_SCHEMA_VERSION',
    'BLACKBOX_SCHEMA_CONTRACT_SHA256',
    'LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256',
    'DAILY_DATA_VERSION',
    'WEEKLY_DATA_VERSION',
    'MONTHLY_DATA_VERSION',
    'EPHEMERAL_NATIVE_INPUT_ROOT_ENV',
    'InputArtifact',
    'create_input_engine',
    'input_artifact_path',
    'build_daily_input_artifact',
    'build_weekly_input_artifact',
    'build_monthly_input_artifact',
    'resolve_blackbox_input_cutoffs',
    'resolve_blackbox_input_cutoffs_bulk',
    'validate_blackbox_request_calendar',
    'prepare_blackbox_generation_snapshot',
    'invalidate_ready_blackbox_snapshot',
    'get_ready_blackbox_snapshot',
    'RawWindUnavailableError',
    'get_ready_raw_wind_artifact',
    'invalidate_raw_wind_sidecar',
    'prepare_raw_wind_sidecar',
    'raw_wind_for_profile',
    'BlackboxRuntimeView',
    'open_blackbox_runtime_view',
    'cleanup_blackbox_runtime_debris',
]
