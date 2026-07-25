"""真实日频算法的隔离 21/25 功能联跑边界。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import event
from sqlalchemy.engine import Connection

from scheduler.daily_runtime import _policy_payload
from scheduler.repository import (
    create_schedule_occurrence as _repository_create_schedule_occurrence,
)
from scheduler.repository import (
    register_seal_and_bind_schedule_occurrence_generation
    as _repository_register_generation,
)
from shared.calendar_service import FrozenCalendarService
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
REAL_REPLAY_DATABASE_PREFIX = "bfl_real_replay_"
EXPECTED_ITEM_COUNT = 21
EXPECTED_TARGET_COUNT = 25
EXPECTED_NATIVE_GENERATION_COUNT = 14
EXPECTED_NATIVE_COMPATIBILITY_COUNT = 3
EXPECTED_V2_COUNT = 4
REPLAY_SLA_WINDOW = timedelta(hours=23)
REPLAY_RECOVERY_WINDOW = timedelta(hours=24)
_MYSQL_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_REPLAY_DATABASE_RE = re.compile(
    rf"{REAL_REPLAY_DATABASE_PREFIX}[a-z0-9_]{{1,45}}\Z"
)


class DailyRealReplayError(RuntimeError):
    """真实日频隔离联跑的输入或冻结策略不满足契约。"""


@dataclass(frozen=True)
class DailyRealReplayInputs:
    """一组已重新打开并交叉验证的真实联跑 generation。"""

    native_generation: NativeGenerationContext
    databridge_generation: DataBridgeGenerationContext
    business_date: str
    feature_date: str


@dataclass(frozen=True)
class DailyRealReplayDatabaseIdentity:
    """写入前已验证、且会重新核验的隔离 MySQL 身份。"""

    version: str
    database_name: str
    server_uuid: str
    session_time_zone: str
    storage_engine: str
    sql_mode: str
    isolation_level: str
    bind_address: str
    port: int
    socket_path: str
    datadir: str
    secure_file_priv: str
    foreign_key_checks: int
    log_bin: int
    local_infile: int
    engine_identity: int
    connection_guard: Callable[[Connection], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def verify_real_replay_database(
    engine: Any,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
    expected_port: int,
    expected_private_root: str | Path,
) -> DailyRealReplayDatabaseIdentity:
    """验证并保护真实联跑专用的 loopback 临时 MySQL Engine。"""
    expected_database = str(expected_database_name).strip()
    expected_uuid = str(expected_server_uuid).strip().lower()
    expected_root = Path(expected_private_root).resolve()
    if _REPLAY_DATABASE_RE.fullmatch(expected_database) is None:
        raise DailyRealReplayError(
            "real replay database must use bfl_real_replay_* namespace"
        )
    if _MYSQL_UUID_RE.fullmatch(expected_uuid) is None:
        raise DailyRealReplayError(
            "real replay expected server_uuid is invalid"
        )
    expected_paths = {
        "socket_path": str(expected_root / "mysql.sock"),
        "datadir": str(expected_root / "data"),
        "secure_file_priv": str(expected_root / "secure"),
    }
    identity = _read_real_replay_database_identity(engine)
    _validate_real_replay_database_identity(identity)
    expected_fields = {
        "database_name": expected_database,
        "server_uuid": expected_uuid,
        "port": int(expected_port),
        **expected_paths,
    }
    drift = _database_identity_drift(identity, expected_fields)
    if drift:
        raise DailyRealReplayError(
            "real replay database differs from expected private MySQL: "
            + ",".join(drift)
        )
    protected: DailyRealReplayDatabaseIdentity

    def connection_guard(conn: Connection) -> None:
        actual = _read_real_replay_database_identity_conn(conn)
        _validate_real_replay_database_identity(actual)
        actual_drift = _database_identity_drift(
            actual,
            _database_identity_fields(protected),
        )
        if actual_drift:
            raise DailyRealReplayError(
                "real replay database identity drift: "
                + ",".join(actual_drift)
            )
        conn.info[REAL_REPLAY_SCHEMA_VERSION] = protected.server_uuid

    protected = replace(
        identity,
        engine_identity=id(engine),
        connection_guard=connection_guard,
    )
    event.listen(engine, "engine_connect", connection_guard)
    setattr(engine, "_bfl_real_replay_isolation", protected)
    return protected


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
    item_deadline = normalized_opened_at + REPLAY_RECOVERY_WINDOW

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
            "deadline_at": item_deadline,
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
    expected_modes = {
        "generation_v1": EXPECTED_NATIVE_GENERATION_COUNT,
        "live_source_0629": EXPECTED_NATIVE_COMPATIBILITY_COUNT,
        "databridge_v1": EXPECTED_V2_COUNT,
    }
    if input_modes != expected_modes:
        raise DailyRealReplayError(
            "real replay input compatibility matrix drifted: "
            f"{input_modes}"
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
        "sla_deadline_at":
            normalized_opened_at + REPLAY_SLA_WINDOW,
        "recovery_cutoff_at":
            normalized_opened_at + REPLAY_RECOVERY_WINDOW,
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


def _read_real_replay_database_identity(
    engine: Any,
) -> DailyRealReplayDatabaseIdentity:
    try:
        with engine.connect() as conn:
            return _read_real_replay_database_identity_conn(conn)
    except Exception as exc:
        if isinstance(exc, DailyRealReplayError):
            raise
        raise DailyRealReplayError(
            "real replay database identity could not be inspected"
        ) from exc


def _read_real_replay_database_identity_conn(
    conn: Connection,
) -> DailyRealReplayDatabaseIdentity:
    try:
        driver_connection = conn.connection.driver_connection
        cursor = driver_connection.cursor()
        try:
            cursor.execute(
                """
                SELECT VERSION() AS version,
                       DATABASE() AS database_name,
                       @@server_uuid AS server_uuid,
                       @@session.time_zone AS session_time_zone,
                       @@default_storage_engine AS storage_engine,
                       @@session.sql_mode AS sql_mode,
                       @@session.transaction_isolation AS isolation_level,
                       @@bind_address AS bind_address,
                       @@port AS port,
                       @@socket AS socket_path,
                       @@datadir AS datadir,
                       @@secure_file_priv AS secure_file_priv,
                       @@foreign_key_checks AS foreign_key_checks,
                       @@global.log_bin AS log_bin,
                       @@global.local_infile AS local_infile
                """
            )
            raw = cursor.fetchone()
            description = tuple(cursor.description or ())
        finally:
            cursor.close()
    except Exception as exc:
        raise DailyRealReplayError(
            "real replay DBAPI identity query failed"
        ) from exc
    if raw is None or len(description) != len(raw):
        raise DailyRealReplayError(
            "real replay DBAPI identity result is invalid"
        )
    row = {
        str(column[0]): value
        for column, value in zip(description, raw, strict=True)
    }
    required_fields = {
        "version",
        "database_name",
        "server_uuid",
        "session_time_zone",
        "storage_engine",
        "sql_mode",
        "isolation_level",
        "bind_address",
        "port",
        "socket_path",
        "datadir",
        "secure_file_priv",
        "foreign_key_checks",
        "log_bin",
        "local_infile",
    }
    if set(row) != required_fields:
        raise DailyRealReplayError(
            "real replay DBAPI identity columns drifted"
        )
    return DailyRealReplayDatabaseIdentity(
        version=str(row["version"] or "").strip(),
        database_name=str(row["database_name"] or "").strip(),
        server_uuid=str(row["server_uuid"] or "").strip().lower(),
        session_time_zone=str(row["session_time_zone"] or "").strip(),
        storage_engine=str(row["storage_engine"] or "").strip(),
        sql_mode=str(row["sql_mode"] or "").strip(),
        isolation_level=str(row["isolation_level"] or "").strip(),
        bind_address=str(row["bind_address"] or "").strip(),
        port=int(row["port"]),
        socket_path=str(Path(str(row["socket_path"] or "")).resolve()),
        datadir=str(Path(str(row["datadir"] or "")).resolve()),
        secure_file_priv=str(
            Path(str(row["secure_file_priv"] or "")).resolve()
        ),
        foreign_key_checks=int(row["foreign_key_checks"]),
        log_bin=int(row["log_bin"]),
        local_infile=int(row["local_infile"]),
        engine_identity=0,
    )


def _validate_real_replay_database_identity(
    identity: DailyRealReplayDatabaseIdentity,
) -> None:
    if not identity.version.startswith("8.0.45"):
        raise DailyRealReplayError(
            "real replay MySQL version must be 8.0.45"
        )
    if _REPLAY_DATABASE_RE.fullmatch(identity.database_name) is None:
        raise DailyRealReplayError(
            "real replay database is not an isolated replay schema"
        )
    if _MYSQL_UUID_RE.fullmatch(identity.server_uuid) is None:
        raise DailyRealReplayError(
            "real replay server_uuid is invalid"
        )
    if identity.session_time_zone != "+00:00":
        raise DailyRealReplayError(
            "real replay MySQL session must use UTC"
        )
    if identity.storage_engine.lower() != "innodb":
        raise DailyRealReplayError(
            "real replay MySQL default engine must be InnoDB"
        )
    sql_modes = {
        value.strip().upper()
        for value in identity.sql_mode.split(",")
        if value.strip()
    }
    if not sql_modes.intersection(
        {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"}
    ):
        raise DailyRealReplayError(
            "real replay MySQL strict SQL mode is disabled"
        )
    if identity.isolation_level.upper() != "REPEATABLE-READ":
        raise DailyRealReplayError(
            "real replay MySQL isolation must be REPEATABLE-READ"
        )
    if identity.bind_address != "127.0.0.1":
        raise DailyRealReplayError(
            "real replay MySQL is not bound to loopback"
        )
    if identity.port <= 0 or identity.port > 65535 or identity.port == 3306:
        raise DailyRealReplayError(
            "real replay MySQL must use a non-production random port"
        )
    if (
        not identity.socket_path
        or not identity.datadir
        or not identity.secure_file_priv
    ):
        raise DailyRealReplayError(
            "real replay MySQL paths are unavailable"
        )
    if identity.foreign_key_checks != 1:
        raise DailyRealReplayError(
            "real replay MySQL foreign_key_checks is disabled"
        )
    if identity.log_bin != 0 or identity.local_infile != 0:
        raise DailyRealReplayError(
            "real replay MySQL unsafe global options are enabled"
        )


def _recheck_real_replay_database(
    engine: Any,
    isolation: DailyRealReplayDatabaseIdentity,
) -> None:
    if not isinstance(isolation, DailyRealReplayDatabaseIdentity):
        raise DailyRealReplayError(
            "real replay database isolation capability is required"
        )
    if isolation.engine_identity != id(engine):
        raise DailyRealReplayError(
            "real replay database engine identity drift"
        )
    if getattr(engine, "_bfl_real_replay_isolation", None) is not isolation:
        raise DailyRealReplayError(
            "real replay database engine guard drift"
        )
    guard = isolation.connection_guard
    if guard is None or not event.contains(
        engine,
        "engine_connect",
        guard,
    ):
        raise DailyRealReplayError(
            "real replay database connection guard is unavailable"
        )
    actual = _read_real_replay_database_identity(engine)
    drift = _database_identity_drift(
        actual,
        _database_identity_fields(isolation),
    )
    if drift:
        raise DailyRealReplayError(
            "real replay database identity drift: " + ",".join(drift)
        )


def _database_identity_fields(
    identity: DailyRealReplayDatabaseIdentity,
) -> dict[str, object]:
    return {
        name: getattr(identity, name)
        for name in (
            "version",
            "database_name",
            "server_uuid",
            "session_time_zone",
            "storage_engine",
            "sql_mode",
            "isolation_level",
            "bind_address",
            "port",
            "socket_path",
            "datadir",
            "secure_file_priv",
            "foreign_key_checks",
            "log_bin",
            "local_infile",
        )
    }


def _database_identity_drift(
    actual: DailyRealReplayDatabaseIdentity,
    expected: Mapping[str, object],
) -> list[str]:
    return sorted(
        name
        for name, expected_value in expected.items()
        if getattr(actual, name) != expected_value
    )


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
