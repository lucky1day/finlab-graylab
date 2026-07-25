"""真实日频算法的隔离 21/25 功能联跑边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from scheduler.daily_runtime import _policy_payload
from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
    EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME,
)
from scheduler.repository import (
    create_schedule_occurrence as _repository_create_schedule_occurrence,
)
from scheduler.repository import (
    register_seal_and_bind_schedule_occurrence_generation
    as _repository_register_generation,
)
from shared.calendar_service import FrozenCalendarService
from shared.daily_coordinator_mode import (
    DailyCoordinatorEpochIdentity,
    VerifiedIsolatedDailyDatabase,
    bind_isolated_daily_coordinator_epoch,
    recheck_verified_isolated_daily_database,
    verify_and_register_isolated_daily_database,
)
from shared.databridge_input_generation import (
    DataBridgeGenerationContext,
    open_databridge_generation,
)
from shared.input_artifacts import BLACKBOX_SCHEMA_PATH
from shared.native_input_generation import (
    NativeGenerationContext,
    open_native_generation,
)


REAL_REPLAY_SCHEMA_VERSION = "daily-real-replay-v1"
REAL_REPLAY_SCHEDULE_PREFIX = "isolated-real-replay-v1-"
EXPECTED_ITEM_COUNT = 21
EXPECTED_TARGET_COUNT = 25
EXPECTED_NATIVE_COUNT = 17
EXPECTED_V2_COUNT = 4
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyRealReplayError(RuntimeError):
    """真实日频隔离联跑的输入或冻结策略不满足契约。"""


@dataclass(frozen=True)
class DailyRealReplayInputs:
    """一组已重新打开并交叉验证的真实联跑 generation。"""

    native_generation: NativeGenerationContext
    databridge_generation: DataBridgeGenerationContext
    business_date: str
    feature_date: str


DailyRealReplayDatabaseIdentity = VerifiedIsolatedDailyDatabase


def verify_real_replay_database(
    engine: Any,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
    expected_port: int,
    expected_private_root: str | Path,
) -> DailyRealReplayDatabaseIdentity:
    """验证并保护真实联跑专用的 loopback 临时 MySQL Engine。"""
    try:
        return verify_and_register_isolated_daily_database(
            engine,
            expected_database_name=expected_database_name,
            expected_server_uuid=expected_server_uuid,
            expected_port=expected_port,
            expected_private_root=expected_private_root,
            connection_marker_key=REAL_REPLAY_SCHEMA_VERSION,
        )
    except RuntimeError as exc:
        raise DailyRealReplayError(str(exc)) from exc


def open_real_replay_generations(
    *,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
    databridge_schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> DailyRealReplayInputs:
    """重新校验两份不可变 manifest，并验证 DataBridge 的 Native 父关系。"""
    native = open_native_generation(Path(native_manifest).resolve())
    databridge = open_databridge_generation(
        Path(databridge_manifest).resolve(),
        schema_path=Path(databridge_schema_path).resolve(),
    )
    drift: list[str] = []
    if databridge.native_generation_id != native.generation_id:
        drift.append("generation_id")
    if databridge.native_manifest_sha256 != native.manifest_sha256:
        drift.append("manifest_sha256")
    if databridge.business_date != native.business_date:
        drift.append("business_date")
    if databridge.feature_date != native.feature_date:
        drift.append("feature_date")
    if drift:
        raise DailyRealReplayError(
            "DataBridge Native parent mismatch: " + ", ".join(drift)
        )
    return DailyRealReplayInputs(
        native_generation=native,
        databridge_generation=databridge,
        business_date=native.business_date,
        feature_date=native.feature_date,
    )


def bind_real_replay_epoch(
    engine: Any,
    *,
    isolation: DailyRealReplayDatabaseIdentity,
    epoch_payload: Mapping[str, object],
) -> DailyCoordinatorEpochIdentity:
    """把 replay epoch 仅绑定到当前受保护临时 MySQL Engine。"""
    _recheck_real_replay_database(engine, isolation)
    return bind_isolated_daily_coordinator_epoch(
        engine,
        frozen=dict(epoch_payload),
        database_name=isolation.database_name,
        server_uuid=isolation.server_uuid,
        isolation=isolation,
        connection_marker_key=REAL_REPLAY_SCHEMA_VERSION,
    )


def create_real_replay_occurrence(
    engine: Any,
    *,
    isolation: DailyRealReplayDatabaseIdentity,
    policy: Any,
    configs: Mapping[str, Any],
    inputs: DailyRealReplayInputs,
    schedule_key: str,
    opened_at: datetime,
    epoch_payload: Mapping[str, object],
) -> int:
    """只经受保护 Engine 创建不冒充 SLA/容量证据的 occurrence。"""
    _recheck_real_replay_database(engine, isolation)
    return int(
        _repository_create_schedule_occurrence(
            engine,
            **_build_real_replay_occurrence_args(
                policy=policy,
                configs=configs,
                inputs=inputs,
                schedule_key=schedule_key,
                opened_at=opened_at,
                epoch_payload=epoch_payload,
            ),
        )
    )


def _build_real_replay_occurrence_args(
    *,
    policy: Any,
    configs: Mapping[str, Any],
    inputs: DailyRealReplayInputs,
    schedule_key: str,
    opened_at: datetime,
    epoch_payload: Mapping[str, object],
) -> dict[str, object]:
    normalized_opened_at = _aware_utc(opened_at)
    normalized_schedule_key = (
        schedule_key.strip() if isinstance(schedule_key, str) else ""
    )
    if not normalized_schedule_key.startswith(
        REAL_REPLAY_SCHEDULE_PREFIX
    ):
        raise DailyRealReplayError(
            "real replay schedule namespace must use "
            f"{REAL_REPLAY_SCHEDULE_PREFIX}*"
        )
    if set(configs) != set(policy.schemes):
        raise DailyRealReplayError(
            "real replay config identities differ from policy"
        )
    if len(policy.schemes) != EXPECTED_ITEM_COUNT:
        raise DailyRealReplayError(
            "real replay policy does not contain 21 items"
        )

    calendar = FrozenCalendarService(inputs.native_generation)
    if (
        calendar.previous_trading_day(inputs.business_date)
        != inputs.feature_date
    ):
        raise DailyRealReplayError(
            "real replay business/feature date is not a trading-day pair"
        )

    target_dates: dict[str, str] = {}
    item_policy_by_base: dict[str, dict[str, object]] = {}
    release_offsets = [
        int(item.v2_release_offset_min or 0)
        for item in policy.schemes.values()
        if item.runtime_type == "blackbox_v2"
    ]
    if sorted(release_offsets) != [0, 2, 4, 6]:
        raise DailyRealReplayError(
            "real replay V2 release offsets differ from 0/2/4/6"
        )
    release_base = normalized_opened_at - timedelta(
        minutes=max(release_offsets)
    )
    replay_cutoff = _next_shanghai_midnight(normalized_opened_at)

    for scheme_id, scheme_policy in policy.schemes.items():
        config = configs[scheme_id]
        target_date = calendar.nth_trading_day_after(
            inputs.feature_date,
            int(scheme_policy.horizon),
        )
        for tenor in scheme_policy.target_tenors:
            registry_id = (
                f"{scheme_id}__h{int(scheme_policy.horizon)}__{tenor}"
            )
            target_dates[registry_id] = target_date
        release_offset = int(
            scheme_policy.v2_release_offset_min or 0
        )
        release_at = (
            release_base + timedelta(minutes=release_offset)
            if scheme_policy.runtime_type == "blackbox_v2"
            else normalized_opened_at
        )
        item_policy_by_base[scheme_id] = {
            "scheme_version": config.scheme_version,
            "code_sha256": config.code_hash,
            "config_sha256": config.config_hash,
            "cache_group": scheme_policy.cache_group,
            "resource_class": scheme_policy.resource_class,
            "internal_workers": scheme_policy.internal_workers,
            "release_offset_minutes": release_offset,
            "release_at": release_at,
            "deadline_at": replay_cutoff,
        }

    if len(target_dates) != EXPECTED_TARGET_COUNT:
        raise DailyRealReplayError(
            "real replay policy does not contain 25 targets"
        )
    policy_json = _policy_payload(
        policy,
        daily_coordinator_epoch=epoch_payload,
    )
    input_modes = _input_mode_counts(policy_json)
    if (
        set(input_modes)
        - {
            "generation_v1",
            "live_source_0629",
            "databridge_v1",
        }
        or input_modes.get("databridge_v1", 0) != EXPECTED_V2_COUNT
        or (
            input_modes.get("generation_v1", 0)
            + input_modes.get("live_source_0629", 0)
        )
        != EXPECTED_NATIVE_COUNT
    ):
        raise DailyRealReplayError(
            "real replay input compatibility matrix drifted: "
            f"{input_modes}"
        )
    _assert_input_compatibility_identities(
        policy_json,
        expected_policy_schemes=policy.schemes,
        expected_configs=configs,
    )
    cache_scheme_ids = sorted(
        str(row["scheme_id"])
        for row in policy_json["schemes"]
        if row.get("cache_spec_fingerprint") is not None
    )
    for row in policy_json["schemes"]:
        row["cache_spec_fingerprint"] = None
    policy_json["real_replay_projection"] = {
        "schema_version": REAL_REPLAY_SCHEMA_VERSION,
        "purpose": "isolated_21_item_25_target_function_replay",
        "algorithm_execution":
            "real_17_native_plus_4_blackbox_v2",
        "persistence": "isolated_mysql_only",
        "capacity_qualification": "EXCLUDED",
        "sla_qualification": "EXCLUDED",
        "cache_completion_qualification": "EXCLUDED",
        "exclusion_reason": "FUNCTION_REPLAY_NOT_PRODUCTION_ADMISSION",
        "excluded_cache_scheme_ids": cache_scheme_ids,
        "native_generation_id":
            inputs.native_generation.generation_id,
        "native_manifest_sha256":
            inputs.native_generation.manifest_sha256,
        "databridge_generation_id":
            inputs.databridge_generation.generation_id,
        "databridge_manifest_sha256":
            inputs.databridge_generation.manifest_sha256,
    }
    return {
        "schedule_key": normalized_schedule_key,
        "predict_date": inputs.business_date,
        "feature_date": inputs.feature_date,
        "target_dates": target_dates,
        "item_policy_by_base": item_policy_by_base,
        "policy_version": policy.version,
        "policy_json": policy_json,
        "sla_deadline_at": replay_cutoff,
        "recovery_cutoff_at": replay_cutoff,
    }


def register_real_replay_generations(
    engine: Any,
    *,
    occurrence_id: int,
    inputs: DailyRealReplayInputs,
    isolation: DailyRealReplayDatabaseIdentity,
) -> dict[str, tuple[str, int]]:
    """按 Native 父、DataBridge 子顺序登记并绑定真实 manifest。"""
    _recheck_real_replay_database(engine, isolation)
    native = inputs.native_generation
    databridge = inputs.databridge_generation
    native_binding = _repository_register_generation(
        engine,
        occurrence_id=int(occurrence_id),
        generation_id=native.generation_id,
        generation_type=native.generation_type,
        business_date=native.business_date,
        feature_date=native.feature_date,
        readiness_basis=native.readiness_basis,
        source_commit_token=native.source_commit_token,
        dataset_content_id=native.dataset_content_id,
        schema_version=native.schema_version,
        exporter_version=native.exporter_version,
        manifest_uri=str(native.manifest_path),
        manifest_sha256=native.manifest_sha256,
        expected_feature_date=inputs.feature_date,
    )
    databridge_binding = _repository_register_generation(
        engine,
        occurrence_id=int(occurrence_id),
        generation_id=databridge.generation_id,
        generation_type=databridge.generation_type,
        business_date=databridge.business_date,
        feature_date=databridge.feature_date,
        readiness_basis=databridge.readiness_basis,
        source_commit_token=databridge.source_commit_token,
        dataset_content_id=databridge.dataset_content_id,
        schema_version=databridge.schema_version,
        exporter_version=databridge.exporter_version,
        manifest_uri=str(databridge.manifest_path),
        manifest_sha256=databridge.manifest_sha256,
        native_generation_id=native.generation_id,
        native_manifest_sha256=native.manifest_sha256,
        expected_feature_date=inputs.feature_date,
    )
    expected = {
        "native_source": (native.generation_id, 17),
        "databridge_v1": (databridge.generation_id, 4),
    }
    actual = {
        "native_source": native_binding,
        "databridge_v1": databridge_binding,
    }
    if actual != expected:
        raise DailyRealReplayError(
            "real replay generation binding cardinality drifted: "
            f"{actual}"
        )
    return actual


def _recheck_real_replay_database(
    engine: Any,
    isolation: DailyRealReplayDatabaseIdentity,
) -> None:
    try:
        recheck_verified_isolated_daily_database(engine, isolation)
    except RuntimeError as exc:
        raise DailyRealReplayError(str(exc)) from exc


def _input_mode_counts(
    policy_json: Mapping[str, object],
) -> dict[str, int]:
    rows = policy_json.get("schemes")
    if not isinstance(rows, list):
        raise DailyRealReplayError(
            "real replay policy schemes are unavailable"
        )
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise DailyRealReplayError(
                "real replay policy scheme row is invalid"
            )
        mode = row.get("input_compatibility")
        if not isinstance(mode, str) or not mode:
            raise DailyRealReplayError(
                "real replay input compatibility is invalid"
            )
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _assert_input_compatibility_identities(
    policy_json: Mapping[str, object],
    *,
    expected_policy_schemes: Mapping[str, Any],
    expected_configs: Mapping[str, Any],
) -> None:
    """要求 17 个 Native 的动态模式和四个 V2 身份完全一致。"""
    rows = policy_json.get("schemes")
    if not isinstance(rows, list):
        raise DailyRealReplayError(
            "real replay policy schemes are unavailable"
        )
    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    if len(normalized_rows) != len(rows):
        raise DailyRealReplayError(
            "real replay input compatibility identities are invalid"
        )
    row_ids = [row.get("scheme_id") for row in normalized_rows]
    if any(
        not isinstance(scheme_id, str)
        or not scheme_id
        or scheme_id != scheme_id.strip()
        for scheme_id in row_ids
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities are invalid"
        )
    canonical_ids = [str(scheme_id) for scheme_id in row_ids]
    expected_ids = set(expected_policy_schemes)
    if (
        any(
            not isinstance(scheme_id, str)
            or not scheme_id
            or scheme_id != scheme_id.strip()
            for scheme_id in (
                *expected_policy_schemes.keys(),
                *expected_configs.keys(),
            )
        )
        or len(canonical_ids) != len(set(canonical_ids))
        or set(canonical_ids) != expected_ids
        or expected_ids != set(expected_configs)
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    rows_by_id = {
        scheme_id: row
        for scheme_id, row in zip(
            canonical_ids,
            normalized_rows,
            strict=True,
        )
    }
    expected_v2_offsets = dict(
        EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME
    )
    expected_v2_ids = set(expected_v2_offsets)
    if not expected_v2_ids < expected_ids:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    for scheme_id in sorted(expected_ids):
        row = rows_by_id[scheme_id]
        policy_item = expected_policy_schemes[scheme_id]
        config = expected_configs[scheme_id]
        if (
            getattr(policy_item, "scheme_id", None) != scheme_id
            or getattr(config, "scheme_id", None) != scheme_id
            or row.get("runtime_type")
            != getattr(policy_item, "runtime_type", None)
            or row.get("runtime_type")
            != getattr(config, "runtime_type", None)
            or row.get("input_compatibility")
            != getattr(policy_item, "input_compatibility", None)
            or row.get("source_package_sha256")
            != getattr(policy_item, "source_package_sha256", None)
        ):
            raise DailyRealReplayError(
                "real replay input compatibility identities drifted"
            )
    actual_v2_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("runtime_type") == "blackbox_v2"
    }
    if actual_v2_ids != expected_v2_ids:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    offset_drift = False
    for scheme_id, expected_offset in expected_v2_offsets.items():
        observed_offset = rows_by_id[scheme_id].get(
            "v2_release_offset_min"
        )
        if (
            not isinstance(observed_offset, int)
            or isinstance(observed_offset, bool)
            or observed_offset != expected_offset
        ):
            offset_drift = True
    if offset_drift:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    actual_live_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "live_source_0629"
    }
    expected_native_ids = expected_ids - expected_v2_ids
    actual_native_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("runtime_type") == "native_adapter"
    }
    actual_generation_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "generation_v1"
    }
    actual_databridge_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "databridge_v1"
    }
    if (
        actual_native_ids != expected_native_ids
        or not (
            actual_live_ids
            <= set(APPROVED_0629_LIVE_SOURCE_SCHEMES)
        )
        or not (actual_live_ids <= expected_native_ids)
        or actual_generation_ids
        != expected_native_ids - actual_live_ids
        or actual_databridge_ids != expected_v2_ids
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    for row in rows_by_id.values():
        mode = row.get("input_compatibility")
        source_sha256 = row.get("source_package_sha256")
        if mode == "live_source_0629":
            if (
                not isinstance(source_sha256, str)
                or len(source_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in source_sha256
                )
            ):
                raise DailyRealReplayError(
                    "real replay input compatibility identities drifted"
                )
        elif source_sha256 is not None:
            raise DailyRealReplayError(
                "real replay input compatibility identities drifted"
            )


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DailyRealReplayError(
            "real replay opened_at must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _next_shanghai_midnight(value: datetime) -> datetime:
    """返回给定时刻之后的下一个上海自然日零点（UTC）。"""
    local_date = value.astimezone(SHANGHAI_TIMEZONE).date()
    next_date = local_date + timedelta(days=1)
    return datetime.combine(
        next_date,
        time.min,
        tzinfo=SHANGHAI_TIMEZONE,
    ).astimezone(timezone.utc)
