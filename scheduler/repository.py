from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
from collections import Counter
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence, cast

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, URL

from scheduler.discovery import SchemeConfig, load_scheme_config
from shared.blackbox_v2.contracts import REQUEST_FIELDS, request_from_mapping
from shared.db_config import DatabaseConfig
from shared.models import (
    ActualRecord,
    MonthlyActualRecord,
    PeriodAverageActualRecord,
    PredictionRecord,
    WeeklyActualRecord,
)
from shared.one_shot_control_plane import SCHEDULED_ONE_SHOT_CONTROL_PLANES
from shared.prediction_context import LIVE_PREDICTION_PHASES
from shared.scheme_config_schema import (
    ALLOWED_RUNTIME_TYPES,
    ALLOWED_VERSION_STATUS,
    normalize_scheme_owner,
)
from shared.task_specs import TASK_COMBINATIONS


PREDICTION_KEYS_ALREADY_EXIST = "prediction_keys_already_exist"
PARTIAL_PREDICTION_KEY_CONFLICT = "partial_prediction_key_conflict"
_PredictionBusinessKey = tuple[str, str, int, str]
BLACKBOX_REGISTRY_STATUSES = {"active", "paused", "archived"}
_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC = 5.0
_NATIVE_SUCCESSOR_REQUIRED_SCHEMA_VERSION = 24
_NATIVE_SUCCESSOR_BACKTEST_START_DATE = "2025-01-01"
_NATIVE_SUCCESSOR_LIVE_TARGET_START_DATE = "2026-06-01"
_SAME_ID_W3B_NATIVE_HISTORY_IDS = frozenset({
    "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_10y01_cons_say_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5",
})
_SAME_ID_RECLAIM_WAVES = {
    "W1B": frozenset({"weekly_5y_direct_0529", "weekly_7y_cross_d_overlay_0529", "weekly_10y_d_overlay_0529"}),
    "W2": frozenset({"daily_5y_2_v28", "daily_7y_1_v28"}),
    "W3A": frozenset({"liwei_0616_cons_sda_k3_div_k10", "liwei_0616_5y01_full_oos_k3_div_k10"}),
}
_PRESERVED_W2_COMPARE_SHA256 = "d40db697478c656cbc1ed714cdee73bbdbe295005a595e5778083ac6745c0285"
_NATIVE_SUCCESSOR_REQUEST_IDENTITY_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
)
_LOGGER = logging.getLogger(__name__)


class BlackboxActivationLockTimeout(RuntimeError):
    """Blackbox 激活未能取得方案级互斥锁。"""


class BlackboxLifecycleIdentityAbsent(RuntimeError):
    """精确 Blackbox version 与 Registry 身份均明确不存在。"""


@dataclass(frozen=True)
class BlackboxExecutionApproval:
    """Blackbox V2 精确版本与 Registry 的执行批准证据。"""

    executable: bool
    reason: str
    version_status: str | None
    approved_by: str | None
    approved_at: datetime | None
    base_scheme_id: str | None = None
    scheme_version: str | None = None
    version_runtime_type: str | None = None
    registry_scheme_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlackboxLifecycleState:
    """Blackbox V2 生命周期事务提交后的数据库证据。"""

    scheme_id: str
    scheme_version: str
    runtime_type: str
    version_status: str
    registry_status: str
    environment_fingerprint: str | None
    data_snapshot_id: str | None
    code_hash: str
    config_hash: str | None
    manifest_hash: str | None
    approved_by: str | None
    approved_at: datetime | None
    registry_scheme_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlackboxRevisionActivationPreflight:
    """同一 Blackbox 业务身份修订切换前的锁定前置证据。"""

    prior_scheme_version: str
    pending_scheme_versions: tuple[str, ...]
    registry_scheme_ids: tuple[str, ...]


@dataclass(frozen=True)
class NativeSuccessorTarget:
    """一次 Native 到 Blackbox 迁移中的单个业务格子。"""

    old_base_scheme_id: str
    new_base_scheme_id: str
    task_type: str
    target_tenor: str
    target_rule: str | None
    old_horizon: int
    new_horizon: int


@dataclass(frozen=True)
class NativeSuccessorBacktestEvidence:
    """successor 持久化回测的精确身份。"""

    backtest_run_id: int
    benchmark_id: str
    data_snapshot_id: str
    generation_id: str
    runtime_profile: str
    environment_fingerprint: str
    code_hash: str
    config_hash: str
    manifest_hash: str
    validator_policy_digest: str


def registry_scheme_id(base_scheme_id: str, horizon: int, target_tenor: str) -> str:
    """生成前端/业务层唯一方案 ID。"""
    return f"{base_scheme_id}__h{int(horizon)}__{target_tenor}"


def create_engine_from_env() -> Engine:
    """创建 SQLAlchemy Engine。"""
    cfg = DatabaseConfig.from_env()
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        query={"charset": cfg.charset},
    )
    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args={
            "init_command": "SET SESSION time_zone = '+00:00'",
        },
    )
    event.listen(
        engine,
        "checkout",
        _set_mysql_session_utc_on_checkout,
    )
    return engine


def _set_mysql_session_utc_on_checkout(
    dbapi_connection: object,
    _connection_record: object,
    _connection_proxy: object,
) -> None:
    """每次连接池 checkout 都修复 MySQL session 时区为 UTC。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()


@contextmanager
def _blackbox_activation_advisory_lock(
    engine: Engine,
    *,
    scheme_id: str,
) -> Iterator[None]:
    """用 scheme-scoped MySQL advisory lock 消除首次 absent-row 竞争。"""
    scheme_digest = hashlib.sha256(scheme_id.encode("utf-8")).hexdigest()[:32]
    lock_name = f"bfl:bbv2-draft:{scheme_digest}"
    with engine.connect() as lock_conn:
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout_sec)"),
            {
                "lock_name": lock_name,
                "timeout_sec": _BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC,
            },
        ).scalar_one()
        if int(acquired or 0) != 1:
            raise BlackboxActivationLockTimeout(
                "timed out waiting for Blackbox activation advisory lock: "
                f"scheme_hash={scheme_digest} "
                f"timeout_sec={_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC:g}"
            )
        try:
            yield
        finally:
            active_error = sys.exc_info()[1]
            try:
                released = lock_conn.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                ).scalar_one()
                if int(released or 0) != 1:
                    raise RuntimeError(
                        "failed to release Blackbox draft registration advisory lock: "
                        f"scheme_hash={scheme_digest}"
                    )
            except BaseException as release_error:
                if active_error is None:
                    raise
                if hasattr(active_error, "add_note"):
                    active_error.add_note(
                        f"activation advisory lock release failed: {release_error}"
                    )


def _normalize_registry_tenors(value: object) -> object:
    """规范化 MySQL JSON driver 或测试替身返回的 tenors。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return value


def _sync_scheme_registry_conn(
    conn: Connection,
    schemes: Iterable[SchemeConfig],
    *,
    effective_statuses: Mapping[str, str],
) -> None:
    """在调用方事务中同步 composite Registry 行。"""
    scheme_list = list(schemes)
    sql = text(
        """
        INSERT INTO t_scheme_registry
            (scheme_id, base_scheme_id, name, owner, description, horizon, task_type, runtime_type, tenors, frequency, target_tenor,
             schedule_cron, schedule_timezone, status, deployed_at)
        VALUES
            (:scheme_id, :base_scheme_id, :name, :owner, :description, :horizon, :task_type, :runtime_type, CAST(:tenors AS JSON), :frequency,
             :target_tenor, :schedule_cron, :schedule_timezone, :status,
             IF(:status = 'active', CURRENT_DATE, NULL))
        ON DUPLICATE KEY UPDATE
            updated_at = IF(
                NOT (
                    base_scheme_id <=> VALUES(base_scheme_id)
                    AND
                    name <=> VALUES(name)
                    AND owner <=> VALUES(owner)
                    AND description <=> VALUES(description)
                    AND horizon <=> VALUES(horizon)
                    AND task_type <=> VALUES(task_type)
                    AND runtime_type <=> VALUES(runtime_type)
                    AND CAST(tenors AS CHAR) <=> CAST(VALUES(tenors) AS CHAR)
                    AND frequency <=> VALUES(frequency)
                    AND target_tenor <=> VALUES(target_tenor)
                    AND schedule_cron <=> VALUES(schedule_cron)
                    AND schedule_timezone <=> VALUES(schedule_timezone)
                    AND status <=> VALUES(status)
                ),
                CURRENT_TIMESTAMP,
                updated_at
            ),
            base_scheme_id = VALUES(base_scheme_id),
            name = VALUES(name),
            owner = VALUES(owner),
            description = VALUES(description),
            horizon = VALUES(horizon),
            task_type = VALUES(task_type),
            runtime_type = VALUES(runtime_type),
            tenors = VALUES(tenors),
            frequency = VALUES(frequency),
            target_tenor = VALUES(target_tenor),
            schedule_cron = VALUES(schedule_cron),
            schedule_timezone = VALUES(schedule_timezone),
            status = VALUES(status),
            deployed_at = IF(deployed_at IS NULL AND VALUES(status) = 'active', CURRENT_DATE, deployed_at)
        """
    )
    row_specs: list[tuple[SchemeConfig, str, str]] = []
    registry_ids_by_base: dict[str, list[str]] = {}
    for cfg in scheme_list:
        registry_ids: list[str] = []
        for target_tenor in cfg.tenors:
            row_scheme_id = registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
            effective_status = effective_statuses[row_scheme_id]
            registry_ids.append(row_scheme_id)
            row_specs.append((cfg, target_tenor, effective_status))
        registry_ids_by_base[cfg.scheme_id] = registry_ids
    if not row_specs:
        return

    expected_registry_ids = [
        registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
        for cfg, target_tenor, _effective_status in row_specs
    ]
    owner_placeholders = ", ".join(
        f":owner_scheme_id_{index}"
        for index, _scheme_id in enumerate(expected_registry_ids)
    )
    owner_params = {
        f"owner_scheme_id_{index}": scheme_id
        for index, scheme_id in enumerate(expected_registry_ids)
    }
    lock_clause = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    existing_owner_rows = conn.execute(
        text(
            "SELECT scheme_id, owner FROM t_scheme_registry "
            f"WHERE scheme_id IN ({owner_placeholders}){lock_clause}"
        ),
        owner_params,
    ).mappings().all()
    existing_owners = {
        str(row["scheme_id"]): row.get("owner")
        for row in existing_owner_rows
    }

    rows = []
    for cfg, target_tenor, effective_status in row_specs:
        row_scheme_id = registry_scheme_id(
            cfg.scheme_id,
            cfg.horizon,
            target_tenor,
        )
        declared_owner = getattr(cfg, "owner", None)
        if declared_owner is not None:
            owner = normalize_scheme_owner(declared_owner)
        else:
            try:
                owner = normalize_scheme_owner(existing_owners.get(row_scheme_id))
            except ValueError as exc:
                raise ValueError(
                    "Registry owner is required when canonical scheme metadata "
                    f"does not declare one: {row_scheme_id}"
                ) from exc
        rows.append(
            {
                "scheme_id": row_scheme_id,
                "base_scheme_id": cfg.scheme_id,
                "name": cfg.name,
                "owner": owner,
                "description": cfg.description,
                "horizon": cfg.horizon,
                "task_type": cfg.task_type,
                "runtime_type": getattr(
                    cfg,
                    "runtime_type",
                    "native_adapter",
                ),
                "tenors": json.dumps([target_tenor], ensure_ascii=False),
                "frequency": cfg.frequency,
                "target_tenor": target_tenor,
                "schedule_cron": cfg.schedule.cron,
                "schedule_timezone": cfg.schedule.timezone,
                "status": effective_status,
            }
        )
    conn.execute(sql, rows)
    for base_scheme_id, registry_ids in registry_ids_by_base.items():
        placeholders = ", ".join(f":scheme_id_{index}" for index, _ in enumerate(registry_ids))
        params = {"base_scheme_id": base_scheme_id}
        for index, scheme_id in enumerate(registry_ids):
            params[f"scheme_id_{index}"] = scheme_id
        conn.execute(
            text(
                f"""
                UPDATE t_scheme_registry
                SET status = 'archived',
                    updated_at = CURRENT_TIMESTAMP
                WHERE base_scheme_id = :base_scheme_id
                  AND scheme_id NOT IN ({placeholders})
                  AND status <> 'archived'
                """
            ),
            params,
        )


def _upsert_scheme_version_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    trusted_status: str | None,
    approved_by: str | None,
    approved_at: datetime | None,
) -> str:
    """在调用方事务中写入精确版本；只有 trusted_status 可提升生命周期。"""
    if trusted_status is not None and trusted_status not in ALLOWED_VERSION_STATUS:
        raise ValueError(f"invalid trusted scheme version status: {trusted_status}")
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if trusted_status is not None:
        status = trusted_status
    else:
        status = "draft"
    preserve_lifecycle = (
        runtime_type in ALLOWED_RUNTIME_TYPES
        and trusted_status is None
    )
    preserve_blackbox_evidence = (
        runtime_type == "blackbox_v2" and trusted_status is None
    )
    trusted_lifecycle = trusted_status is not None
    sql = text(
        """
        INSERT INTO t_scheme_versions
            (scheme_id, scheme_version, runtime_type, algorithm_version, contract_version, runtime_profile,
             environment_fingerprint, data_snapshot_id,
             code_hash, config_hash, manifest_hash, git_commit, status, created_by, approved_by, approved_at)
        VALUES
            (:scheme_id, :scheme_version, :runtime_type, :algorithm_version, :contract_version, :runtime_profile,
             :environment_fingerprint, :data_snapshot_id,
             :code_hash, :config_hash, :manifest_hash, :git_commit, :status, :created_by, :approved_by, :approved_at)
        ON DUPLICATE KEY UPDATE
            code_hash = IF(:preserve_blackbox_evidence, t_scheme_versions.code_hash, VALUES(code_hash)),
            runtime_type = IF(:preserve_lifecycle, t_scheme_versions.runtime_type, VALUES(runtime_type)),
            algorithm_version = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.algorithm_version,
                VALUES(algorithm_version)
            ),
            contract_version = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.contract_version,
                VALUES(contract_version)
            ),
            runtime_profile = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.runtime_profile,
                VALUES(runtime_profile)
            ),
            environment_fingerprint = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.environment_fingerprint,
                VALUES(environment_fingerprint)
            ),
            data_snapshot_id = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.data_snapshot_id,
                VALUES(data_snapshot_id)
            ),
            config_hash = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.config_hash,
                VALUES(config_hash)
            ),
            manifest_hash = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.manifest_hash,
                VALUES(manifest_hash)
            ),
            git_commit = IF(
                :preserve_blackbox_evidence,
                t_scheme_versions.git_commit,
                VALUES(git_commit)
            ),
            status = IF(:preserve_lifecycle, t_scheme_versions.status, VALUES(status)),
            approved_by = IF(:trusted_lifecycle, VALUES(approved_by), t_scheme_versions.approved_by),
            approved_at = IF(:trusted_lifecycle, VALUES(approved_at), t_scheme_versions.approved_at)
        """
    )
    params = {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "runtime_type": runtime_type,
        "algorithm_version": getattr(cfg, "algorithm_version", None),
        "contract_version": getattr(cfg, "contract_version", None),
        "runtime_profile": getattr(cfg, "runtime_profile", None),
        "environment_fingerprint": getattr(cfg, "environment_fingerprint", None),
        "data_snapshot_id": getattr(cfg, "data_snapshot_id", None),
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
        "git_commit": None,
        "status": status,
        "created_by": "scheduler.discovery",
        "approved_by": approved_by,
        "approved_at": approved_at,
        "preserve_lifecycle": preserve_lifecycle,
        "preserve_blackbox_evidence": preserve_blackbox_evidence,
        "trusted_lifecycle": trusted_lifecycle,
    }
    conn.execute(sql, params)
    return cfg.scheme_version


def _read_scheme_version_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    for_update: bool = False,
) -> Mapping[str, object] | None:
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    return (
        conn.execute(
            text(
                "SELECT scheme_id, scheme_version, runtime_type, algorithm_version, contract_version, "
                "runtime_profile, environment_fingerprint, data_snapshot_id, code_hash, config_hash, "
                "manifest_hash, git_commit, status, created_by, approved_by, approved_at "
                "FROM t_scheme_versions "
                "WHERE scheme_id = :scheme_id AND scheme_version = :scheme_version "
                f"LIMIT 1{lock_clause}"
            ),
            {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version},
        )
        .mappings()
        .one_or_none()
    )


def _read_scheme_version_rows_for_base_conn(
    conn: Connection,
    scheme_id: str,
    *,
    for_update: bool,
) -> list[Mapping[str, object]]:
    """读取一个 base scheme 的全部 exact version，供受控版本切换锁定。"""
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    return (
        conn.execute(
            text(
                "SELECT scheme_id, scheme_version, runtime_type, algorithm_version, "
                "contract_version, runtime_profile, environment_fingerprint, "
                "data_snapshot_id, code_hash, config_hash, manifest_hash, git_commit, "
                "status, created_by, approved_by, approved_at "
                "FROM t_scheme_versions WHERE scheme_id = :scheme_id "
                f"ORDER BY scheme_version{lock_clause}"
            ),
            {"scheme_id": scheme_id},
        )
        .mappings()
        .all()
    )


def read_blackbox_execution_approval(engine: Engine, cfg: SchemeConfig) -> BlackboxExecutionApproval:
    """读取并验证 Blackbox 精确版本及 composite Registry 的生产批准。"""
    with engine.begin() as conn:
        return _read_blackbox_execution_approval_conn(conn, cfg, for_update=False)


def activate_blackbox_initial(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    backtest_run_id: int,
    backtest_benchmark_id: str,
    backtest_data_snapshot_id: str,
    backtest_generation_id: str,
    backtest_runtime_profile: str,
    backtest_environment_fingerprint: str,
    backtest_code_hash: str,
    backtest_config_hash: str,
    backtest_manifest_hash: str,
    backtest_validator_policy_digest: str,
    approved_by: str,
    approved_at: datetime,
) -> BlackboxLifecycleState:
    """在一个事务中建立并激活全新 Blackbox version 与 Registry。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("Blackbox initial activation requires runtime_type=blackbox_v2")
    if not str(getattr(cfg, "environment_fingerprint", "") or "").strip():
        raise ValueError("Blackbox initial activation requires environment_fingerprint")
    if not str(getattr(cfg, "data_snapshot_id", "") or "").strip():
        raise ValueError("Blackbox initial activation requires data_snapshot_id")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("Blackbox initial activation requires non-empty approved_by")
    if not isinstance(approved_at, datetime):
        raise ValueError("Blackbox initial activation requires approved_at datetime")
    if isinstance(backtest_run_id, bool) or not isinstance(backtest_run_id, int) or backtest_run_id <= 0:
        raise ValueError("Blackbox initial activation requires positive backtest_run_id")

    normalized_approver = approved_by.strip()
    mysql_approved_at = _mysql_utc_datetime(approved_at)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    identity_ids = (cfg.scheme_id, *expected_registry_ids)
    placeholders = ", ".join(
        f":identity_id_{index}" for index, _ in enumerate(identity_ids)
    )
    identity_params = {
        f"identity_id_{index}": identity_id
        for index, identity_id in enumerate(identity_ids)
    }
    with _blackbox_activation_advisory_lock(engine, scheme_id=cfg.scheme_id):
        with engine.begin() as conn:
            lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
            version_conflicts = conn.execute(
                text(
                    "SELECT scheme_id, scheme_version FROM t_scheme_versions "
                    f"WHERE scheme_id IN ({placeholders}){lock_clause}"
                ),
                identity_params,
            ).mappings().all()
            registry_conflicts = conn.execute(
                text(
                    "SELECT scheme_id, base_scheme_id FROM t_scheme_registry "
                    f"WHERE scheme_id IN ({placeholders}) "
                    f"OR base_scheme_id IN ({placeholders}){lock_clause}"
                ),
                identity_params,
            ).mappings().all()
            if version_conflicts or registry_conflicts:
                raise ValueError(
                    "Blackbox initial activation identity conflict: "
                    f"versions={len(version_conflicts)}, "
                    f"registry={len(registry_conflicts)}"
                )

            fact_rows = _prepare_blackbox_backtest_fact_rows_conn(
                conn,
                cfg,
                backtest_run_id=backtest_run_id,
                backtest_benchmark_id=backtest_benchmark_id,
                backtest_data_snapshot_id=backtest_data_snapshot_id,
                backtest_generation_id=backtest_generation_id,
                backtest_runtime_profile=backtest_runtime_profile,
                backtest_environment_fingerprint=(
                    backtest_environment_fingerprint
                ),
                backtest_code_hash=backtest_code_hash,
                backtest_config_hash=backtest_config_hash,
                backtest_manifest_hash=backtest_manifest_hash,
                backtest_validator_policy_digest=(
                    backtest_validator_policy_digest
                ),
            )
            existing_fact_count = int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM t_scheme_predictions "
                        "WHERE scheme_id = :scheme_id"
                    ),
                    {"scheme_id": cfg.scheme_id},
                ).scalar_one()
            )
            if existing_fact_count:
                raise ValueError(
                    "Blackbox initial activation found pre-existing product facts: "
                    f"scheme_id={cfg.scheme_id} count={existing_fact_count}"
                )
            inserted_facts = _insert_run_predictions_conn(conn, fact_rows)
            if inserted_facts != len(fact_rows):
                raise RuntimeError("Blackbox initial activation fact insert count mismatch")

            _upsert_scheme_version_conn(
                conn,
                cfg,
                trusted_status="active",
                approved_by=normalized_approver,
                approved_at=mysql_approved_at,
            )
            _sync_scheme_registry_conn(
                conn,
                [cfg],
                effective_statuses={
                    registry_id: "active" for registry_id in expected_registry_ids
                },
            )
            approval = _read_blackbox_execution_approval_conn(
                conn,
                cfg,
                for_update=True,
            )
            if not approval.executable:
                raise RuntimeError(
                    "Blackbox initial activation readback failed: "
                    f"{approval.reason}"
                )
            version_row = _read_scheme_version_conn(conn, cfg, for_update=True)
            if version_row is None:
                raise RuntimeError("Blackbox initial activation version readback missing")
            published_count = int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM t_scheme_predictions "
                        "WHERE scheme_id = :scheme_id "
                        "AND run_id IS NULL "
                        "AND backtest_run_id = :backtest_run_id"
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "backtest_run_id": backtest_run_id,
                    },
                ).scalar_one()
            )
            if published_count != len(fact_rows):
                raise RuntimeError("Blackbox initial activation fact readback mismatch")

    return BlackboxLifecycleState(
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        runtime_type="blackbox_v2",
        version_status="active",
        registry_status="active",
        environment_fingerprint=str(cfg.environment_fingerprint),
        data_snapshot_id=str(cfg.data_snapshot_id),
        code_hash=cfg.code_hash,
        config_hash=cfg.config_hash,
        manifest_hash=cfg.manifest_hash,
        approved_by=normalized_approver,
        approved_at=mysql_approved_at,
        registry_scheme_ids=expected_registry_ids,
    )


def resolve_database_lifecycle(
    engine: Engine,
    configs: Iterable[SchemeConfig],
) -> tuple[SchemeConfig, ...]:
    """一次读取数据库，把 Blackbox 配置解析为当前主机的生效状态。

    Native 仍使用 immutable config 中的存量状态。Blackbox 的 config 状态只是
    Intake 初始声明；是否可执行唯一由本机 exact version 与 composite Registry 决定。
    """
    resolved = tuple(configs)
    blackbox = tuple(
        cfg for cfg in resolved if getattr(cfg, "runtime_type", None) == "blackbox_v2"
    )
    if not blackbox:
        return resolved

    base_ids = tuple(dict.fromkeys(cfg.scheme_id for cfg in blackbox))
    placeholders = ", ".join(f":base_id_{index}" for index, _ in enumerate(base_ids))
    params = {
        f"base_id_{index}": scheme_id
        for index, scheme_id in enumerate(base_ids)
    }
    with engine.begin() as conn:
        version_rows = conn.execute(
            text(
                "SELECT scheme_id, scheme_version, runtime_type, status, "
                "approved_by, approved_at FROM t_scheme_versions "
                f"WHERE scheme_id IN ({placeholders})"
            ),
            params,
        ).mappings().all()
        registry_rows = conn.execute(
            text(
                "SELECT scheme_id, base_scheme_id, name, description, horizon, "
                "task_type, runtime_type, tenors, frequency, target_tenor, "
                "schedule_cron, schedule_timezone, status, deployed_at "
                "FROM t_scheme_registry "
                f"WHERE base_scheme_id IN ({placeholders})"
            ),
            params,
        ).mappings().all()

    versions_by_identity = {
        (str(row["scheme_id"]), str(row["scheme_version"])): dict(row)
        for row in version_rows
    }
    registries_by_base: dict[str, list[Mapping[str, object]]] = {}
    for row in registry_rows:
        registries_by_base.setdefault(str(row["base_scheme_id"]), []).append(
            dict(row)
        )

    effective: list[SchemeConfig] = []
    for cfg in resolved:
        if getattr(cfg, "runtime_type", None) != "blackbox_v2":
            effective.append(cfg)
            continue
        version = versions_by_identity.get((cfg.scheme_id, cfg.scheme_version))
        if version is None:
            effective.append(replace(cfg, status="paused", version_status="draft"))
            continue
        version_status = str(version.get("status") or "")
        if version_status != "active":
            effective.append(
                replace(cfg, status="paused", version_status=version_status or "draft")
            )
            continue
        if version.get("runtime_type") != "blackbox_v2":
            raise RuntimeError(
                f"active exact version runtime_type mismatch for {cfg.scheme_id}"
            )
        if not isinstance(version.get("approved_by"), str) or not str(
            version.get("approved_by")
        ).strip() or not isinstance(version.get("approved_at"), datetime):
            raise RuntimeError(
                f"active exact version approval evidence is incomplete for {cfg.scheme_id}"
            )
        expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
        expected_ids = set(expected_registry_ids)
        rows = [
            row
            for row in registries_by_base.get(cfg.scheme_id, ())
            if str(row.get("scheme_id")) in expected_ids
            or str(row.get("status")) == "active"
        ]
        registry_error = _registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            rows,
            expected_status="active",
        )
        if registry_error is not None:
            raise RuntimeError(
                f"active Blackbox lifecycle is inconsistent for {cfg.scheme_id}: "
                f"{registry_error}"
            )
        effective.append(replace(cfg, status="active", version_status="active"))
    return tuple(effective)


def read_blackbox_revision_activation_preflight(
    engine: Engine,
    cfg: SchemeConfig,
) -> BlackboxRevisionActivationPreflight:
    """只读确认 active Blackbox 同身份修订可进行原子切换。"""
    _validate_blackbox_revision_candidate(cfg, require_evidence=False)
    with engine.begin() as conn:
        return _read_blackbox_revision_activation_preflight_conn(
            conn,
            cfg,
            for_update=False,
        )


def activate_blackbox_revision(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    prior_scheme_version: str,
    pending_scheme_versions: tuple[str, ...],
    approved_by: str,
    approved_at: datetime,
) -> BlackboxLifecycleState:
    """原子替换同一业务身份的唯一 active Blackbox exact version。"""
    _validate_blackbox_revision_candidate(cfg, require_evidence=True)
    if not isinstance(prior_scheme_version, str) or not prior_scheme_version.strip():
        raise ValueError("Blackbox revision activation requires prior_scheme_version")
    if prior_scheme_version == cfg.scheme_version:
        raise ValueError("Blackbox revision activation prior version must differ from candidate")
    if not isinstance(pending_scheme_versions, tuple) or any(
        not isinstance(version, str) or not version.strip()
        for version in pending_scheme_versions
    ):
        raise ValueError(
            "pending_scheme_versions must be a tuple of non-empty strings"
        )
    expected_pending_versions = tuple(sorted(pending_scheme_versions))
    if (
        len(expected_pending_versions) != len(set(expected_pending_versions))
        or {cfg.scheme_version, prior_scheme_version}.intersection(
            expected_pending_versions
        )
    ):
        raise ValueError(
            "pending_scheme_versions must be unique and exclude candidate/prior"
        )
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("Blackbox revision activation requires non-empty approved_by")
    if not isinstance(approved_at, datetime):
        raise ValueError("Blackbox revision activation requires approved_at datetime")

    normalized_approver = approved_by.strip()
    mysql_approved_at = _mysql_utc_datetime(approved_at)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    with _blackbox_activation_advisory_lock(
        engine,
        scheme_id=cfg.scheme_id,
    ):
        with engine.begin() as conn:
            lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
            preflight = _read_blackbox_revision_activation_preflight_conn(
                conn,
                cfg,
                for_update=True,
            )
            if preflight.prior_scheme_version != prior_scheme_version:
                raise RuntimeError(
                    "Blackbox revision activation prior version changed before commit: "
                    f"expected={prior_scheme_version}, "
                    f"actual={preflight.prior_scheme_version}"
                )
            if preflight.pending_scheme_versions != expected_pending_versions:
                raise RuntimeError(
                    "Blackbox revision activation pending versions changed before "
                    "commit: "
                    f"expected={list(expected_pending_versions)}, "
                    f"actual={list(preflight.pending_scheme_versions)}"
                )

            _upsert_scheme_version_conn(
                conn,
                cfg,
                trusted_status="active",
                approved_by=normalized_approver,
                approved_at=mysql_approved_at,
            )
            retired = conn.execute(
                text(
                    "UPDATE t_scheme_versions SET status = 'retired' "
                    "WHERE scheme_id = :scheme_id "
                    "AND scheme_version = :prior_scheme_version "
                    "AND runtime_type = 'blackbox_v2' AND status = 'active'"
                ),
                {
                    "scheme_id": cfg.scheme_id,
                    "prior_scheme_version": prior_scheme_version,
                },
            )
            if retired.rowcount != 1:
                raise RuntimeError(
                    "Blackbox revision activation could not retire exactly one "
                    f"prior active version: rowcount={retired.rowcount}"
                )
            if expected_pending_versions:
                retired_pending = conn.execute(
                    text(
                        "UPDATE t_scheme_versions SET status = 'retired' "
                        "WHERE scheme_id = :scheme_id "
                        "AND runtime_type = 'blackbox_v2' "
                        "AND status IN ('draft', 'validated', 'shadow', 'paused')"
                    ),
                    {"scheme_id": cfg.scheme_id},
                )
                if retired_pending.rowcount != len(expected_pending_versions):
                    raise RuntimeError(
                        "Blackbox revision activation could not retire every locked "
                        "pending version: "
                        f"expected={len(expected_pending_versions)}, "
                        f"rowcount={retired_pending.rowcount}"
                    )

            version_rows = _read_scheme_version_rows_for_base_conn(
                conn,
                cfg.scheme_id,
                for_update=True,
            )
            candidate_rows = [
                row
                for row in version_rows
                if row.get("scheme_version") == cfg.scheme_version
            ]
            if len(candidate_rows) != 1:
                raise RuntimeError(
                    "Blackbox revision activation candidate readback is not unique"
                )
            version_row = candidate_rows[0]
            actual_version_values = dict(version_row)
            if isinstance(actual_version_values.get("approved_at"), datetime):
                actual_version_values["approved_at"] = _mysql_utc_datetime(
                    actual_version_values["approved_at"]
                )
            expected_version_values = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "algorithm_version": getattr(cfg, "algorithm_version", None),
                "contract_version": getattr(cfg, "contract_version", None),
                "runtime_profile": getattr(cfg, "runtime_profile", None),
                "environment_fingerprint": getattr(cfg, "environment_fingerprint", None),
                "data_snapshot_id": getattr(cfg, "data_snapshot_id", None),
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
                "manifest_hash": cfg.manifest_hash,
                "status": "active",
                "approved_by": normalized_approver,
                "approved_at": mysql_approved_at,
            }
            mismatches = [
                f"{field}: expected={expected!r}, got={actual_version_values.get(field)!r}"
                for field, expected in expected_version_values.items()
                if actual_version_values.get(field) != expected
            ]
            if mismatches:
                raise RuntimeError(
                    "Blackbox revision activation candidate readback mismatch: "
                    + "; ".join(mismatches)
                )

            active_versions = sorted(
                str(row.get("scheme_version"))
                for row in version_rows
                if row.get("runtime_type") == "blackbox_v2"
                and row.get("status") == "active"
            )
            if active_versions != [cfg.scheme_version]:
                raise RuntimeError(
                    "Blackbox revision activation must leave exactly one active "
                    f"version: got={active_versions}"
                )
            prior_rows = [
                row
                for row in version_rows
                if row.get("scheme_version") == prior_scheme_version
            ]
            if len(prior_rows) != 1 or prior_rows[0].get("status") != "retired":
                raise RuntimeError(
                    "Blackbox revision activation prior version readback is not retired"
                )
            non_retired_versions = sorted(
                str(row.get("scheme_version"))
                for row in version_rows
                if row.get("runtime_type") == "blackbox_v2"
                and row.get("scheme_version") != cfg.scheme_version
                and row.get("status") != "retired"
            )
            if non_retired_versions:
                raise RuntimeError(
                    "Blackbox revision activation must retire every prior exact "
                    f"version: got={non_retired_versions}"
                )
            registry_rows = _read_scheme_registry_rows_conn(
                conn,
                cfg,
                expected_registry_ids,
                for_update=True,
            )
            registry_error = _blackbox_revision_registry_identity_error(
                cfg,
                expected_tenors,
                expected_registry_ids,
                registry_rows,
            )
            if registry_error is not None:
                raise RuntimeError(
                    "Blackbox revision activation Registry changed unexpectedly: "
                    f"{registry_error}"
                )

    return BlackboxLifecycleState(
        scheme_id=str(version_row["scheme_id"]),
        scheme_version=str(version_row["scheme_version"]),
        runtime_type=str(version_row["runtime_type"]),
        version_status=str(version_row["status"]),
        registry_status="active",
        environment_fingerprint=(
            str(version_row["environment_fingerprint"])
            if version_row.get("environment_fingerprint") is not None
            else None
        ),
        data_snapshot_id=(
            str(version_row["data_snapshot_id"])
            if version_row.get("data_snapshot_id") is not None
            else None
        ),
        code_hash=str(version_row["code_hash"]),
        config_hash=(
            str(version_row["config_hash"])
            if version_row.get("config_hash") is not None
            else None
        ),
        manifest_hash=(
            str(version_row["manifest_hash"])
            if version_row.get("manifest_hash") is not None
            else None
        ),
        approved_by=(
            str(version_row["approved_by"])
            if version_row.get("approved_by") is not None
            else None
        ),
        approved_at=(
            actual_version_values["approved_at"]
            if isinstance(actual_version_values.get("approved_at"), datetime)
            else None
        ),
        registry_scheme_ids=expected_registry_ids,
    )


def _validate_blackbox_revision_candidate(
    cfg: SchemeConfig,
    *,
    require_evidence: bool,
) -> None:
    """校验同身份 Blackbox revision 的不可变候选边界。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError(
            "Blackbox revision activation requires runtime_type=blackbox_v2"
        )
    scheme_version = getattr(cfg, "scheme_version", None)
    if not isinstance(scheme_version, str) or not scheme_version.strip():
        raise ValueError("Blackbox revision activation requires non-empty scheme_version")
    if require_evidence:
        for field in ("environment_fingerprint", "data_snapshot_id"):
            value = getattr(cfg, field, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "Blackbox revision activation requires successful backtest "
                    f"{field}"
                )


def _read_blackbox_revision_activation_preflight_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    for_update: bool,
) -> BlackboxRevisionActivationPreflight:
    """在当前事务内锁定并验证同身份 revision 的替换前状态。"""
    _validate_blackbox_revision_candidate(cfg, require_evidence=False)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    version_rows = _read_scheme_version_rows_for_base_conn(
        conn,
        cfg.scheme_id,
        for_update=for_update,
    )
    candidate_rows = [
        row for row in version_rows if row.get("scheme_version") == cfg.scheme_version
    ]
    if candidate_rows:
        raise ValueError("exact candidate version already exists")
    non_blackbox_versions = [
        str(row.get("scheme_version"))
        for row in version_rows
        if row.get("runtime_type") != "blackbox_v2"
        and not (
            row.get("runtime_type") == "native_adapter"
            and row.get("status") == "retired"
        )
    ]
    if non_blackbox_versions:
        raise ValueError(
            "Blackbox revision identity has non-Blackbox versions: "
            f"{sorted(non_blackbox_versions)}"
        )
    pending_versions = sorted(
        str(row.get("scheme_version"))
        for row in version_rows
        if row.get("status") in {"draft", "validated", "shadow", "paused"}
    )
    active_rows = [
        row
        for row in version_rows
        if row.get("runtime_type") == "blackbox_v2" and row.get("status") == "active"
    ]
    if len(active_rows) != 1:
        active_versions = sorted(
            str(row.get("scheme_version")) for row in active_rows
        )
        raise ValueError(
            "Blackbox revision activation requires exactly one prior active "
            f"version: got={active_versions}"
        )
    prior = active_rows[0]
    prior_version = str(prior.get("scheme_version"))
    if not isinstance(prior.get("approved_by"), str) or not str(
        prior.get("approved_by")
    ).strip() or not isinstance(prior.get("approved_at"), datetime):
        raise ValueError("prior active Blackbox version has incomplete approval evidence")
    registry_rows = _read_scheme_registry_rows_conn(
        conn,
        cfg,
        expected_registry_ids,
        for_update=for_update,
    )
    registry_error = _blackbox_revision_registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
    )
    if registry_error is not None:
        raise ValueError(
            "Blackbox revision activation Registry identity is not active/exact: "
            f"{registry_error}"
        )
    return BlackboxRevisionActivationPreflight(
        prior_scheme_version=prior_version,
        pending_scheme_versions=tuple(pending_versions),
        registry_scheme_ids=expected_registry_ids,
    )


def _blackbox_revision_registry_identity_error(
    cfg: SchemeConfig,
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
) -> str | None:
    """补充 revision 需要锁定的 cadence 与单 target Registry 身份。"""
    error = _registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
        expected_status="active",
    )
    if error is not None:
        return error
    rows_by_id = {str(row.get("scheme_id")): row for row in registry_rows}
    for target_tenor, registry_id in zip(expected_tenors, expected_registry_ids):
        row = rows_by_id[registry_id]
        if row.get("frequency") != cfg.frequency:
            return (
                f"registry {registry_id} frequency is {row.get('frequency')!r}, "
                f"expected {cfg.frequency!r}"
            )
        if _normalize_registry_tenors(row.get("tenors")) != [target_tenor]:
            return (
                f"registry {registry_id} tenors are "
                f"{_normalize_registry_tenors(row.get('tenors'))!r}, "
                f"expected {[target_tenor]!r}"
            )
    return None


def read_blackbox_lifecycle_state(engine: Engine, cfg: SchemeConfig) -> BlackboxLifecycleState:
    """独立读取 Blackbox 精确版本和 composite Registry 生命周期状态。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("Blackbox lifecycle read requires runtime_type=blackbox_v2")
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    with engine.begin() as conn:
        version_row = _read_scheme_version_conn(conn, cfg, for_update=False)
        registry_rows = _read_scheme_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=False,
        )
    if version_row is None:
        if not registry_rows:
            raise BlackboxLifecycleIdentityAbsent(
                "exact Blackbox lifecycle identity is absent: "
                f"scheme_id={cfg.scheme_id} "
                f"scheme_version={cfg.scheme_version}"
            )
        raise RuntimeError(
            "partial Blackbox lifecycle identity found without exact version: "
            f"scheme_id={cfg.scheme_id} "
            f"scheme_version={cfg.scheme_version}"
        )
    statuses = {str(row.get("status")) for row in registry_rows}
    if len(statuses) != 1:
        raise RuntimeError(f"Blackbox Registry lifecycle statuses are inconsistent: {sorted(statuses)}")
    registry_status = next(iter(statuses))
    registry_error = _registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
        expected_status=registry_status,
    )
    if registry_error is not None:
        raise RuntimeError(f"Blackbox lifecycle Registry read failed: {registry_error}")
    return BlackboxLifecycleState(
        scheme_id=str(version_row["scheme_id"]),
        scheme_version=str(version_row["scheme_version"]),
        runtime_type=str(version_row["runtime_type"]),
        version_status=str(version_row["status"]),
        registry_status=registry_status,
        environment_fingerprint=(
            str(version_row["environment_fingerprint"])
            if version_row.get("environment_fingerprint") is not None
            else None
        ),
        data_snapshot_id=(
            str(version_row["data_snapshot_id"])
            if version_row.get("data_snapshot_id") is not None
            else None
        ),
        code_hash=str(version_row["code_hash"]),
        config_hash=(
            str(version_row["config_hash"]) if version_row.get("config_hash") is not None else None
        ),
        manifest_hash=(
            str(version_row["manifest_hash"]) if version_row.get("manifest_hash") is not None else None
        ),
        approved_by=(
            str(version_row["approved_by"]) if version_row.get("approved_by") is not None else None
        ),
        approved_at=(
            version_row["approved_at"] if isinstance(version_row.get("approved_at"), datetime) else None
        ),
        registry_scheme_ids=expected_registry_ids,
    )


def _read_blackbox_execution_approval_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    for_update: bool,
) -> BlackboxExecutionApproval:
    base_scheme_id = str(getattr(cfg, "scheme_id", ""))
    scheme_version = getattr(cfg, "scheme_version", None)
    version_status: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    version_runtime_type: str | None = None
    registry_ids: tuple[str, ...] = ()

    def denied(reason: str) -> BlackboxExecutionApproval:
        return BlackboxExecutionApproval(
            executable=False,
            reason=reason,
            version_status=version_status,
            approved_by=approved_by,
            approved_at=approved_at,
            base_scheme_id=base_scheme_id or None,
            scheme_version=str(scheme_version) if scheme_version is not None else None,
            version_runtime_type=version_runtime_type,
            registry_scheme_ids=registry_ids,
        )

    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        return denied(
            f"config runtime_type is {getattr(cfg, 'runtime_type', None)}, expected blackbox_v2"
        )
    if not base_scheme_id:
        return denied("config scheme_id is empty")
    if not scheme_version:
        return denied(f"config scheme_version is empty for {base_scheme_id}")

    try:
        expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    except ValueError as exc:
        return denied(str(exc))

    version_row = _read_scheme_version_conn(conn, cfg, for_update=for_update)
    if version_row is None:
        return denied(
            f"exact version not found: scheme_id={base_scheme_id} scheme_version={scheme_version}"
        )

    actual_scheme_id = version_row.get("scheme_id")
    actual_scheme_version = version_row.get("scheme_version")
    version_status = str(version_row.get("status")) if version_row.get("status") is not None else None
    approved_by_value = version_row.get("approved_by")
    approved_by = str(approved_by_value) if approved_by_value is not None else None
    approved_at_value = version_row.get("approved_at")
    approved_at = approved_at_value if isinstance(approved_at_value, datetime) else None
    version_runtime_type = (
        str(version_row.get("runtime_type")) if version_row.get("runtime_type") is not None else None
    )
    if actual_scheme_id != base_scheme_id or actual_scheme_version != scheme_version:
        return denied(
            "version identity mismatch: "
            f"expected {base_scheme_id}/{scheme_version}, got {actual_scheme_id}/{actual_scheme_version}"
        )
    if version_runtime_type != "blackbox_v2":
        return denied(f"version runtime_type is {version_runtime_type}, expected blackbox_v2")
    if version_status != "active":
        return denied(f"version status is {version_status}, expected active")
    if approved_by_value is None:
        return denied("version approved_by is null")
    if not isinstance(approved_by_value, str) or not approved_by_value.strip():
        return denied("version approved_by is empty")
    if approved_at_value is None:
        return denied("version approved_at is null")
    if not isinstance(approved_at_value, datetime):
        return denied("version approved_at must be datetime")

    registry_rows = _read_scheme_registry_rows_conn(
        conn,
        cfg,
        expected_registry_ids,
        for_update=for_update,
    )
    registry_ids = tuple(str(row.get("scheme_id")) for row in registry_rows)
    registry_error = _registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
        expected_status="active",
    )
    if registry_error is not None:
        return denied(registry_error)

    return BlackboxExecutionApproval(
        executable=True,
        reason="approved",
        version_status=version_status,
        approved_by=approved_by,
        approved_at=approved_at,
        base_scheme_id=base_scheme_id,
        scheme_version=str(scheme_version),
        version_runtime_type=version_runtime_type,
        registry_scheme_ids=registry_ids,
    )


def _expected_registry_identity(
    cfg: SchemeConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tenors = tuple(str(target_tenor) for target_tenor in cfg.tenors)
    if not tenors:
        raise ValueError(f"config tenors are empty for {cfg.scheme_id}")
    registry_ids = tuple(
        registry_scheme_id(cfg.scheme_id, int(cfg.horizon), target_tenor)
        for target_tenor in tenors
    )
    duplicate_tenors = sorted({item for item in tenors if tenors.count(item) > 1})
    duplicate_registry_ids = sorted(
        {item for item in registry_ids if registry_ids.count(item) > 1}
    )
    if duplicate_tenors or duplicate_registry_ids:
        raise ValueError(
            "duplicate expected tenors/Registry ids: "
            f"tenors={duplicate_tenors}, registry_ids={duplicate_registry_ids}"
        )
    return tenors, registry_ids


def _read_scheme_registry_rows_conn(
    conn: Connection,
    cfg: SchemeConfig,
    expected_registry_ids: tuple[str, ...],
    *,
    for_update: bool,
) -> list[Mapping[str, object]]:
    placeholders = ", ".join(
        f":registry_scheme_id_{index}" for index, _ in enumerate(expected_registry_ids)
    )
    params: dict[str, object] = {"base_scheme_id": cfg.scheme_id}
    for index, expected_id in enumerate(expected_registry_ids):
        params[f"registry_scheme_id_{index}"] = expected_id
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    return (
        conn.execute(
            text(
                "SELECT scheme_id, base_scheme_id, name, description, horizon, "
                "task_type, runtime_type, tenors, frequency, target_tenor, "
                "schedule_cron, schedule_timezone, status, deployed_at "
                "FROM t_scheme_registry "
                f"WHERE scheme_id IN ({placeholders}) "
                "OR (base_scheme_id = :base_scheme_id AND status = 'active')"
                f"{lock_clause}"
            ),
            params,
        )
        .mappings()
        .all()
    )


def _registry_identity_error(
    cfg: SchemeConfig,
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
    *,
    expected_status: str,
    expected_runtime_type: str = "blackbox_v2",
) -> str | None:
    registry_ids = tuple(str(row.get("scheme_id")) for row in registry_rows)
    if not registry_rows:
        return f"registry row missing: {expected_registry_ids[0]}"
    if len(registry_ids) != len(set(registry_ids)):
        return f"duplicate registry rows returned: {sorted(registry_ids)}"
    if set(registry_ids) != set(expected_registry_ids):
        if len(registry_ids) == len(expected_registry_ids) == 1:
            return (
                f"registry scheme_id mismatch: expected {expected_registry_ids[0]}, "
                f"got {registry_ids[0]}"
            )
        return (
            f"registry identity mismatch: expected {sorted(expected_registry_ids)}, "
            f"got {sorted(registry_ids)}"
        )

    rows_by_id = {str(row.get("scheme_id")): row for row in registry_rows}
    for target_tenor, expected_id in zip(expected_tenors, expected_registry_ids):
        row = rows_by_id[expected_id]
        if row.get("base_scheme_id") != cfg.scheme_id:
            return (
                f"registry base_scheme_id mismatch for {expected_id}: "
                f"expected {cfg.scheme_id}, got {row.get('base_scheme_id')}"
            )
        if row.get("status") != expected_status:
            return (
                f"registry {expected_id} status is {row.get('status')}, "
                f"expected {expected_status}"
            )
        if row.get("runtime_type") != expected_runtime_type:
            return (
                f"registry {expected_id} runtime_type is {row.get('runtime_type')}, "
                f"expected {expected_runtime_type}"
            )
        if row.get("task_type") != cfg.task_type:
            return (
                f"registry {expected_id} task_type is {row.get('task_type')}, "
                f"expected {cfg.task_type}"
            )
        if row.get("target_tenor") != target_tenor:
            return (
                f"registry {expected_id} target_tenor is {row.get('target_tenor')}, "
                f"expected {target_tenor}"
            )
        try:
            actual_horizon = int(row.get("horizon"))
        except (TypeError, ValueError):
            actual_horizon = None
        if actual_horizon != int(cfg.horizon):
            return (
                f"registry {expected_id} horizon is {row.get('horizon')}, "
                f"expected {cfg.horizon}"
            )
    return None


def active_native_identity_error(
    engine: Engine,
    cfg: SchemeConfig,
) -> str | None:
    """只读核验 scheduled Native 的精确版本与完整 Registry 身份。"""
    if getattr(cfg, "runtime_type", None) != "native_adapter":
        return "config runtime_type is not native_adapter"
    if getattr(cfg, "status", None) != "active":
        return f"config status is {getattr(cfg, 'status', None)}, expected active"
    exact_scheme_version = getattr(cfg, "scheme_version", None)
    if (
        not isinstance(exact_scheme_version, str)
        or not exact_scheme_version.strip()
    ):
        return "config scheme_version is empty"
    try:
        expected_tenors, expected_registry_ids = (
            _expected_registry_identity(cfg)
        )
        with engine.begin() as conn:
            version_row = _read_scheme_version_conn(
                conn,
                cfg,
                for_update=False,
            )
            if (
                version_row is None
                or version_row.get("scheme_id") != cfg.scheme_id
                or version_row.get("scheme_version")
                != exact_scheme_version
                or version_row.get("runtime_type") != "native_adapter"
                or version_row.get("status") != "active"
            ):
                return "exact active Native version identity mismatch"
            registry_rows = _read_scheme_registry_rows_conn(
                conn,
                cfg,
                expected_registry_ids,
                for_update=False,
            )
            return _registry_identity_error(
                cfg,
                expected_tenors,
                expected_registry_ids,
                registry_rows,
                expected_status="active",
                expected_runtime_type="native_adapter",
            )
    except (TypeError, ValueError) as exc:
        return str(exc)


def apply_native_activation_state(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    approved_by: str,
    approved_at: datetime,
) -> str:
    """在单一事务中可信激活一个 Native 精确版本及其目标 Registry。"""
    if getattr(cfg, "runtime_type", None) != "native_adapter":
        raise ValueError(
            "trusted Native activation requires runtime_type=native_adapter"
        )
    if getattr(cfg, "status", None) != "active":
        raise ValueError("trusted Native activation requires active config")
    scheme_version = getattr(cfg, "scheme_version", None)
    if not isinstance(scheme_version, str) or not scheme_version.strip():
        raise ValueError("trusted Native activation requires non-empty scheme_version")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("approved_by must be a non-empty string")
    if not isinstance(approved_at, datetime):
        raise ValueError("approved_at must be datetime")

    normalized_approver = approved_by.strip()
    mysql_approved_at = _mysql_utc_datetime(approved_at)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    effective_statuses = {
        registry_id: "active" for registry_id in expected_registry_ids
    }
    with engine.begin() as conn:
        _upsert_scheme_version_conn(
            conn,
            cfg,
            trusted_status="active",
            approved_by=normalized_approver,
            approved_at=mysql_approved_at,
        )
        _sync_scheme_registry_conn(
            conn,
            [cfg],
            effective_statuses=effective_statuses,
        )
        version_row = _read_scheme_version_conn(conn, cfg, for_update=True)
        if version_row is None:
            raise RuntimeError(
                "trusted Native activation version readback missing: "
                f"{cfg.scheme_id}/{scheme_version}"
            )
        actual_approved_at = version_row.get("approved_at")
        if isinstance(actual_approved_at, datetime):
            actual_approved_at = _mysql_utc_datetime(actual_approved_at)
        expected_version_values = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": scheme_version,
            "runtime_type": "native_adapter",
            "code_hash": cfg.code_hash,
            "config_hash": cfg.config_hash,
            "manifest_hash": cfg.manifest_hash,
            "status": "active",
            "approved_by": normalized_approver,
            "approved_at": mysql_approved_at,
        }
        actual_version_values = dict(version_row)
        actual_version_values["approved_at"] = actual_approved_at
        mismatches = [
            f"{field}: expected={expected!r}, got={actual_version_values.get(field)!r}"
            for field, expected in expected_version_values.items()
            if actual_version_values.get(field) != expected
        ]
        if mismatches:
            raise RuntimeError(
                "trusted Native activation version readback mismatch: "
                + "; ".join(mismatches)
            )
        registry_rows = _read_scheme_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=True,
        )
        registry_error = _registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status="active",
            expected_runtime_type="native_adapter",
        )
        if registry_error is not None:
            raise RuntimeError(
                f"trusted Native activation Registry readback mismatch: {registry_error}"
            )
    return scheme_version


def _mysql_utc_datetime(value: datetime | None) -> datetime | None:
    """将批准时刻统一为 MySQL DATETIME(0) 使用的无时区 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _require_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_sha256(value: str, field: str) -> str:
    normalized = _require_nonempty(value, field).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field} must be a 64-character SHA256 hex digest")
    return normalized


def _dialect_name(conn: Connection) -> str:
    return str(getattr(getattr(conn, "dialect", None), "name", "mysql"))


def _select_mapping_one_or_none(
    conn: Connection,
    sql: str,
    params: Mapping[str, object],
) -> Mapping[str, object] | None:
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    return (
        conn.execute(text(sql + lock), dict(params))
        .mappings()
        .one_or_none()
    )


def _read_scheme_run_conn(
    conn: Connection,
    *,
    run_id: int,
) -> Mapping[str, object] | None:
    """读取普通 run；遗留 schedule linkage 不属于新的运行契约。"""
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT run_id, scheme_id, scheme_version, runtime_type, run_type,
               prediction_phase, predict_date, status, records_expected,
               records_returned, records_written, data_snapshot_id,
               started_at, finished_at
        FROM t_scheme_runs
        WHERE run_id = :run_id
        """,
        {"run_id": int(run_id)},
    )


def _require_rowcount(result: object, operation: str) -> None:
    actual = int(getattr(result, "rowcount", 0) or 0)
    if actual != 1:
        raise RuntimeError(
            f"{operation} affected {actual} rows, expected 1"
        )


def _stored_iso_date(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise RuntimeError(f"invalid stored {field}: {value!r}") from exc
    raise RuntimeError(f"invalid stored {field}: {value!r}")


def _lock_existing_prediction_business_keys(
    conn: Connection,
    keys: Sequence[_PredictionBusinessKey],
) -> frozenset[_PredictionBusinessKey]:
    """用一次锁定查询读取已存在的预测业务键。"""
    unique_keys = tuple(dict.fromkeys(keys))
    if len(unique_keys) != len(keys):
        raise RuntimeError(
            "prediction business-key lock input contains duplicate keys"
        )
    if not unique_keys:
        return frozenset()

    placeholders = []
    params: dict[str, object] = {}
    for index, key in enumerate(unique_keys):
        placeholders.append(
            f"(:scheme_id_{index}, :target_tenor_{index}, "
            f":horizon_{index}, :target_date_{index})"
        )
        params.update(
            {
                f"scheme_id_{index}": key[0],
                f"target_tenor_{index}": key[1],
                f"horizon_{index}": key[2],
                f"target_date_{index}": key[3],
            }
        )
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    rows = (
        conn.execute(
            text(
                "SELECT scheme_id, target_tenor, horizon, target_date "
                "FROM t_scheme_predictions "
                "WHERE (scheme_id, target_tenor, horizon, target_date) IN ("
                + ", ".join(placeholders)
                + ")"
                + lock
            ),
            params,
        )
        .mappings()
        .all()
    )

    requested = frozenset(unique_keys)
    existing: set[_PredictionBusinessKey] = set()
    for row in rows:
        try:
            key = (
                str(row["scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
                _stored_iso_date(row, "target_date"),
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                "prediction business-key lock readback is malformed"
            ) from exc
        if key not in requested:
            raise RuntimeError(
                "prediction business-key lock readback returned an unexpected key"
            )
        if key in existing:
            raise RuntimeError(
                "prediction business-key lock readback returned a duplicate key"
            )
        existing.add(key)
    return frozenset(existing)


def create_scheme_run(
    engine: Engine,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    runtime_type: str = "native_adapter",
    prediction_phase: str | None = None,
    records_expected: int | None = None,
    scheduled_control_plane: str | None = None,
) -> int:
    """创建正常预测运行；自然写入只接受已安装的一次性控制面。"""
    if (
        prediction_phase is not None
        and prediction_phase not in LIVE_PREDICTION_PHASES
    ):
        raise ValueError(
            "prediction_phase must be one of "
            f"{sorted(LIVE_PREDICTION_PHASES)}, got {prediction_phase}"
        )
    if (
        scheduled_control_plane is not None
        and scheduled_control_plane not in SCHEDULED_ONE_SHOT_CONTROL_PLANES
    ):
        raise ValueError(
            "scheduled_control_plane must be an installed one-shot control plane"
        )
    if scheduled_control_plane is not None:
        if prediction_phase != "scheduled_live":
            raise ValueError(
                "scheduled_control_plane requires scheduled_live"
            )
    if (
        prediction_phase == "scheduled_live"
        and scheduled_control_plane not in SCHEDULED_ONE_SHOT_CONTROL_PLANES
    ):
        raise RuntimeError(
            "scheduled_live requires an installed one-shot control plane"
        )
    with engine.begin() as conn:
        return _create_scheme_run_conn(
            conn,
            scheme_id=scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            prediction_phase=prediction_phase,
            records_expected=records_expected,
        )


def _create_scheme_run_conn(
    conn: Connection,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    runtime_type: str = "native_adapter",
    prediction_phase: str | None = None,
    records_expected: int | None = None,
) -> int:
    """在调用方事务内创建不带遗留 schedule linkage 的运行。"""
    result = conn.execute(
        text(
            """
            INSERT INTO t_scheme_runs
                (scheme_id, scheme_version, runtime_type, run_type,
                 prediction_phase, predict_date, status, records_expected)
            VALUES
                (:scheme_id, :scheme_version, :runtime_type, :run_type,
                 :prediction_phase, :predict_date, :status, :records_expected)
            """
        ),
        {
            "scheme_id": scheme_id,
            "scheme_version": scheme_version,
            "runtime_type": runtime_type,
            "run_type": "active",
            "prediction_phase": prediction_phase,
            "predict_date": predict_date,
            "status": "running",
            "records_expected": records_expected,
        },
    )
    run_id = getattr(result, "lastrowid", None)
    if run_id is None:
        run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
    return int(run_id)


def attach_run_data_snapshot(engine: Engine, *, run_id: int, data_snapshot_id: str) -> None:
    """将 Blackbox V2 输入快照关联到既有运行审计行。"""
    if not str(data_snapshot_id).strip():
        raise ValueError("data_snapshot_id must be non-empty")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET data_snapshot_id = :data_snapshot_id
                WHERE run_id = :run_id
                """
            ),
            {"run_id": int(run_id), "data_snapshot_id": str(data_snapshot_id)},
        )


def _finish_scheme_run_conn(
    conn: Connection,
    *,
    run_id: int,
    status: str,
    records_returned: int | None = None,
    records_written: int | None = None,
    error_message: str | None = None,
) -> None:
    """在调用方事务中标记预测运行结束。"""
    sql = text(
        """
        UPDATE t_scheme_runs
        SET status = :status,
            finished_at = CURRENT_TIMESTAMP,
            records_returned = :records_returned,
            records_written = :records_written,
            error_message = :error_message
        WHERE run_id = :run_id
        """
    )
    result = conn.execute(
        sql,
        {
            "run_id": run_id,
            "status": status,
            "records_returned": records_returned,
            "records_written": records_written,
            "error_message": error_message,
        },
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        raise RuntimeError(
            "scheme run update affected "
            f"{int(getattr(result, 'rowcount', 0) or 0)} rows for run_id={run_id}"
        )


def _prediction_write_decision_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> tuple[str, int, str | None]:
    """锁定已验证的实盘业务键并决定整组提交语义。"""
    keys = sorted(
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in prediction_rows
    )
    existing_keys = _lock_existing_prediction_business_keys(conn, keys)
    existing = [key for key in keys if key in existing_keys]
    missing = [key for key in keys if key not in existing_keys]

    if not existing:
        return "success", len(keys), None
    if not missing:
        return "skipped", 0, PREDICTION_KEYS_ALREADY_EXIST

    def format_keys(values: list[tuple[str, str, int, str]]) -> str:
        return "[" + ", ".join(
            f"{scheme_id}/{target_tenor}/h{horizon}/{target_date}"
            for scheme_id, target_tenor, horizon, target_date in values
        ) + "]"

    return (
        "failed",
        0,
        f"{PARTIAL_PREDICTION_KEY_CONFLICT}: "
        f"existing={format_keys(existing)}; missing={format_keys(missing)}",
    )


def _validate_running_run_identity(
    run: Mapping[str, object] | None,
    *,
    expected_identity: Mapping[str, object],
    error_prefix: str,
) -> str:
    """纯校验 running run 身份，并返回其 scheduled_live phase。"""
    errors = []
    if run is None:
        errors.append("run missing")
    else:
        for field, expected_value in expected_identity.items():
            actual_value = run.get(field)
            if field == "predict_date":
                actual_value = _stored_iso_date(run, field)
            if field == "records_expected" and actual_value is not None:
                try:
                    actual_value = int(actual_value)
                except (TypeError, ValueError):
                    pass
            if actual_value != expected_value:
                errors.append(
                    f"{field}: expected={expected_value!r}, "
                    f"got={actual_value!r}"
                )
    if errors:
        raise RuntimeError(f"{error_prefix}: " + "; ".join(errors))
    assert run is not None
    run_phase = str(run.get("prediction_phase") or "")
    if run_phase != "scheduled_live":
        raise RuntimeError(
            f"{error_prefix}: ordinary completion requires scheduled_live, "
            f"got prediction_phase={run.get('prediction_phase')!r}"
        )
    return run_phase


def _validate_completion_records(
    records: Sequence[PredictionRecord],
    *,
    records_returned: int,
    expected_targets: set[tuple[str, int]],
    scheme_id: str,
    scheme_version: str,
    predict_date: str,
    prediction_phase: str,
    runtime_label: str,
) -> None:
    """纯校验 completion records 的计数、业务身份与目标集合。"""
    if records_returned != len(records):
        raise RuntimeError(
            f"{runtime_label} completion records_returned mismatch: "
            f"returned={records_returned}, records={len(records)}"
        )
    returned_targets = Counter(
        (str(record.target_tenor), int(record.horizon))
        for record in records
    )
    duplicate_targets = sorted(
        (target_tenor, horizon, count)
        for (target_tenor, horizon), count in returned_targets.items()
        if count != 1
    )
    errors = []
    if set(returned_targets) != expected_targets or duplicate_targets:
        errors.append(
            "target set does not match active Registry: "
            f"expected={sorted(expected_targets)}, "
            f"returned={sorted(returned_targets)}, "
            f"duplicates={duplicate_targets}"
        )
    for record in records:
        if record.scheme_id != scheme_id:
            errors.append(f"record scheme_id={record.scheme_id!r}")
        if record.scheme_version not in (None, scheme_version):
            errors.append(f"record scheme_version={record.scheme_version!r}")
        if str(record.predict_date) != predict_date:
            errors.append(f"record predict_date={record.predict_date!r}")
        record_phase = record.prediction_phase or (
            record.extra or {}
        ).get("prediction_phase")
        if record_phase != prediction_phase:
            errors.append(f"record prediction_phase={record_phase!r}")
    if errors:
        raise RuntimeError(
            f"{runtime_label} completion records revalidation failed: "
            + "; ".join(errors)
        )


def complete_approved_blackbox_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    scheme_version: str | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> tuple[str, int, str | None]:
    """原子提交 Blackbox prediction、终态 run 与运行日志。"""
    normalized_run_date = _require_iso_date(run_date, "run_date")
    expected_tenors, _expected_registry_ids = _expected_registry_identity(cfg)
    expected_targets = {
        (target_tenor, int(cfg.horizon))
        for target_tenor in expected_tenors
    }
    with _approved_blackbox_write_transaction(
        engine,
        cfg,
        records,
        scheme_version=scheme_version,
    ) as (conn, record_list, exact_scheme_version):
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        expected_run_identity = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": exact_scheme_version,
            "runtime_type": "blackbox_v2",
            "run_type": "active",
            "predict_date": normalized_run_date,
            "status": "running",
            "records_expected": len(expected_targets),
        }
        run_phase = _validate_running_run_identity(
            run,
            expected_identity=expected_run_identity,
            error_prefix=(
                "Blackbox completion running run identity revalidation failed"
            ),
        )
        _validate_completion_records(
            record_list,
            records_returned=records_returned,
            expected_targets=expected_targets,
            scheme_id=cfg.scheme_id,
            scheme_version=exact_scheme_version,
            predict_date=normalized_run_date,
            prediction_phase=run_phase,
            runtime_label="Blackbox",
        )
        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != records_returned:
                raise RuntimeError(
                    "Blackbox completion records_written mismatch: "
                    f"returned={records_returned}, written={records_written}"
                )
        _finish_scheme_run_conn(
            conn,
            run_id=run_id,
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            cfg.scheme_id,
            normalized_run_date,
            status,
            duration_sec,
            error_message,
            run_id,
        )
        return status, records_written, error_message


def complete_active_native_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    scheme_version: str | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> tuple[str, int, str | None]:
    """原子提交 Native prediction、终态 run 与运行日志。"""
    if getattr(cfg, "runtime_type", None) != "native_adapter":
        raise ValueError(
            "active Native completion requires runtime_type=native_adapter"
        )
    if getattr(cfg, "status", None) != "active":
        raise ValueError("active Native completion requires active config")
    exact_scheme_version = getattr(cfg, "scheme_version", None)
    if (
        not isinstance(exact_scheme_version, str)
        or not exact_scheme_version.strip()
    ):
        raise ValueError(
            "active Native completion requires non-empty scheme_version"
        )
    if scheme_version != exact_scheme_version:
        raise ValueError(
            "Native completion scheme_version does not match config: "
            f"{scheme_version!r} != {exact_scheme_version!r}"
        )
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    expected_targets = {
        (target_tenor, int(cfg.horizon))
        for target_tenor in expected_tenors
    }
    record_list = list(records)
    normalized_run_date = _require_iso_date(run_date, "run_date")

    with engine.begin() as conn:
        version_row = _read_scheme_version_conn(
            conn,
            cfg,
            for_update=True,
        )
        if (
            version_row is None
            or version_row.get("scheme_id") != cfg.scheme_id
            or version_row.get("scheme_version") != exact_scheme_version
            or version_row.get("runtime_type") != "native_adapter"
            or version_row.get("status") != "active"
        ):
            raise RuntimeError(
                "exact active Native version revalidation failed: "
                f"scheme_id={cfg.scheme_id} "
                f"scheme_version={exact_scheme_version}"
            )
        registry_rows = _read_scheme_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=True,
        )
        registry_error = _registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status="active",
            expected_runtime_type="native_adapter",
        )
        if registry_error is not None:
            raise RuntimeError(
                f"Native completion Registry revalidation failed: {registry_error}"
            )
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        expected_run_identity = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": exact_scheme_version,
            "runtime_type": "native_adapter",
            "run_type": "active",
            "predict_date": normalized_run_date,
            "status": "running",
            "records_expected": len(expected_targets),
        }
        run_phase = _validate_running_run_identity(
            run,
            expected_identity=expected_run_identity,
            error_prefix="running run identity revalidation failed",
        )
        _validate_completion_records(
            record_list,
            records_returned=records_returned,
            expected_targets=expected_targets,
            scheme_id=cfg.scheme_id,
            scheme_version=exact_scheme_version,
            predict_date=normalized_run_date,
            prediction_phase=run_phase,
            runtime_label="Native",
        )

        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != len(expected_targets):
                raise RuntimeError(
                    "Native completion records_written mismatch: "
                    f"expected={len(expected_targets)}, "
                    f"written={records_written}"
                )
        _finish_scheme_run_conn(
            conn,
            run_id=int(run_id),
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            cfg.scheme_id,
            normalized_run_date,
            status,
            duration_sec,
            error_message,
            int(run_id),
        )
        return status, records_written, error_message


def complete_gray_gap_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    expected_target_keys: list[Mapping[str, object]],
    source_authority: Mapping[str, object] | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> int:
    """原子提交一个调度日的 ``gray_live`` 历史信号缺口。"""
    written = complete_gray_gap_runs_atomic(
        engine,
        [
            {
                "cfg": cfg,
                "run_id": run_id,
                "records": list(records),
                "expected_target_keys": expected_target_keys,
                "source_authority": source_authority,
                "records_returned": records_returned,
                "run_date": run_date,
                "duration_sec": duration_sec,
            }
        ],
    )
    return written[int(run_id)]


def complete_gray_gap_runs_atomic(
    engine: Engine,
    items: Iterable[Mapping[str, object]],
) -> dict[int, int]:
    """在一个事务中提交多个调度日的 gray-live run 与 prediction。"""
    prepared: list[dict[str, object]] = []
    all_business_keys: set[tuple[object, ...]] = set()
    for item in items:
        cfg = cast(SchemeConfig, item["cfg"])
        run_id = int(item["run_id"])
        records = list(cast(Iterable[PredictionRecord], item["records"]))
        records_returned = int(item["records_returned"])
        expected_targets = _normalize_gray_gap_target_keys(
            cfg,
            list(
                cast(
                    Iterable[Mapping[str, object]],
                    item["expected_target_keys"],
                )
            ),
        )
        if records_returned != len(records):
            raise RuntimeError(
                "gray gap records_returned mismatch: "
                f"returned={records_returned}, records={len(records)}"
            )
        run_date = _require_iso_date(item["run_date"], "run_date")
        duration_sec = float(item["duration_sec"])
        if not math.isfinite(duration_sec) or duration_sec < 0:
            raise ValueError("duration_sec must be a finite non-negative number")
        execution_dates = _gray_gap_execution_dates(expected_targets)
        if run_date != execution_dates["predict_date"]:
            raise ValueError("run_date must equal gray gap execution predict_date")
        normalized_authority = _normalize_gray_gap_source_authority(
            cast(Mapping[str, object] | None, item.get("source_authority")),
            runtime_type=str(cfg.runtime_type),
            feature_date=execution_dates["feature_date"],
            predict_date=execution_dates["predict_date"],
        )
        _validate_gray_gap_records(
            cfg,
            records=records,
            expected_targets=expected_targets,
        )
        enriched_records = _enrich_gray_gap_records(
            cfg,
            records=records,
            source_authority=normalized_authority,
        )
        snapshot_id = _blackbox_gray_gap_snapshot_id(
            cfg,
            records=enriched_records,
        )
        for target in expected_targets:
            key = (
                target["base_scheme_id"],
                target["target_tenor"],
                target["horizon"],
                target["target_date"],
            )
            if key in all_business_keys:
                raise RuntimeError("gray gap range contains duplicate business keys")
            all_business_keys.add(key)
        prepared.append(
            {
                "cfg": cfg,
                "run_id": run_id,
                "records_returned": records_returned,
                "run_date": run_date,
                "duration_sec": duration_sec,
                "expected_targets": expected_targets,
                "records": enriched_records,
                "snapshot_id": snapshot_id,
            }
        )
    if not prepared:
        raise ValueError("gray gap atomic completion requires at least one run")
    run_ids = [int(item["run_id"]) for item in prepared]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("gray gap atomic completion contains duplicate run ids")

    written_by_run: dict[int, int] = {}
    with engine.begin() as conn:
        validated_configs: set[tuple[str, str]] = set()
        all_targets: list[Mapping[str, object]] = []
        for item in prepared:
            cfg = cast(SchemeConfig, item["cfg"])
            run_id = int(item["run_id"])
            targets = cast(list[Mapping[str, object]], item["expected_targets"])
            run = _read_scheme_run_conn(conn, run_id=run_id)
            _validate_gray_gap_run(
                cfg,
                run_id=run_id,
                run=run,
                expected_count=len(targets),
                predict_date=str(item["run_date"]),
            )
            config_identity = (str(cfg.scheme_id), str(cfg.scheme_version))
            if config_identity not in validated_configs:
                _validate_gray_gap_active_version(conn, cfg)
                _validate_gray_gap_active_registry(conn, cfg)
                validated_configs.add(config_identity)
            snapshot_id = item["snapshot_id"]
            if snapshot_id is not None:
                _bind_blackbox_gray_gap_snapshot_conn(
                    conn,
                    run_id=run_id,
                    run=run,
                    data_snapshot_id=str(snapshot_id),
                )
            all_targets.extend(targets)

        _assert_gray_gap_business_keys_absent(conn, all_targets)

        for item in prepared:
            cfg = cast(SchemeConfig, item["cfg"])
            run_id = int(item["run_id"])
            targets = cast(list[Mapping[str, object]], item["expected_targets"])
            prediction_rows = _prepare_run_prediction_rows(
                run_id,
                cast(Iterable[PredictionRecord], item["records"]),
                scheme_version=str(cfg.scheme_version),
            )
            records_written = _insert_run_predictions_conn(conn, prediction_rows)
            if records_written != len(targets):
                raise RuntimeError(
                    "gray gap records_written mismatch: "
                    f"expected={len(targets)}, written={records_written}"
                )
            finished = conn.execute(
                text(
                    """
                    UPDATE t_scheme_runs
                    SET status = 'success',
                        finished_at = CURRENT_TIMESTAMP,
                        records_returned = :records_returned,
                        records_written = :records_written,
                        error_message = NULL
                    WHERE run_id = :run_id
                      AND status = 'running'
                      AND run_type = 'active'
                      AND prediction_phase = 'gray_live'
                    """
                ),
                {
                    "run_id": run_id,
                    "records_returned": int(item["records_returned"]),
                    "records_written": records_written,
                },
            )
            _require_rowcount(finished, "gray gap run finish")
            _write_run_log_conn(
                conn,
                str(cfg.scheme_id),
                str(item["run_date"]),
                "success",
                float(item["duration_sec"]),
                None,
                run_id,
            )
            written_by_run[run_id] = records_written
    return written_by_run


def _blackbox_gray_gap_snapshot_id(
    cfg: SchemeConfig,
    *,
    records: Iterable[PredictionRecord],
) -> str | None:
    """提取并核验 Blackbox gray-gap records 的唯一运行输入快照。"""
    if str(cfg.runtime_type) != "blackbox_v2":
        return None
    snapshot_ids: set[str] = set()
    for record in records:
        snapshot_ids.add(
            _blackbox_gray_gap_record_snapshot_id(
                cfg,
                scheme_id=record.scheme_id,
                predict_date=record.predict_date,
                feature_date=record.feature_date,
                target_date=record.target_date,
                extra=record.extra,
            )
        )
    if len(snapshot_ids) != 1:
        raise RuntimeError(
            "Blackbox gray gap records must carry exactly one non-empty "
            "data_snapshot_id"
        )
    return next(iter(snapshot_ids))


def _bind_blackbox_gray_gap_snapshot_conn(
    conn: Connection,
    *,
    run_id: int,
    run: Mapping[str, object] | None,
    data_snapshot_id: str,
) -> None:
    """在同一 completion 事务中将已核验 snapshot 固定到 running run。"""
    if run is None:
        raise RuntimeError("Blackbox gray gap run is missing before snapshot bind")
    if run.get("data_snapshot_id") is not None:
        raise RuntimeError(
            "Blackbox gray gap run already has data_snapshot_id; refusing overwrite"
        )
    result = conn.execute(
        text(
            """
            UPDATE t_scheme_runs
            SET data_snapshot_id = :data_snapshot_id
            WHERE run_id = :run_id
              AND data_snapshot_id IS NULL
              AND scheme_id = :scheme_id
              AND scheme_version = :scheme_version
              AND runtime_type = 'blackbox_v2'
              AND status = 'running'
              AND run_type = 'active'
              AND prediction_phase = 'gray_live'
            """
        ),
        {
            "run_id": int(run_id),
            "data_snapshot_id": data_snapshot_id,
            "scheme_id": str(run["scheme_id"]),
            "scheme_version": str(run["scheme_version"]),
        },
    )
    _require_rowcount(result, "Blackbox gray gap run data snapshot bind")


def _blackbox_gray_gap_record_snapshot_id(
    cfg: SchemeConfig,
    *,
    scheme_id: object,
    predict_date: object,
    feature_date: object,
    target_date: object,
    extra: object,
) -> str:
    """验证单条 Blackbox gray-gap record 的 request/snapshot provenance。"""
    if str(scheme_id) != str(cfg.scheme_id):
        raise RuntimeError(
            "Blackbox gray gap record scheme_id provenance does not match config"
        )
    if not isinstance(extra, Mapping):
        raise RuntimeError(
            "Blackbox gray gap record provenance requires extra object"
        )
    try:
        snapshot_id = _require_nonempty(
            extra.get("data_snapshot_id"),
            "Blackbox gray gap record data_snapshot_id",
        )
        normalized_feature_date = _require_iso_date(
            feature_date or extra.get("feature_date"),
            "Blackbox gray gap record feature_date",
        )
        normalized_predict_date = _require_iso_date(
            predict_date,
            "Blackbox gray gap record predict_date",
        )
        normalized_target_date = _require_iso_date(
            target_date,
            "Blackbox gray gap record target_date",
        )
        request_id = _require_nonempty(
            extra.get("request_id"),
            "Blackbox gray gap record request_id",
        )
    except ValueError as exc:
        raise RuntimeError(
            "Blackbox gray gap record provenance is invalid"
        ) from exc
    canonical_request_id = (
        f"{cfg.scheme_id}:{normalized_predict_date}:{normalized_feature_date}:"
        f"{normalized_target_date}"
    )
    if request_id != canonical_request_id:
        raise RuntimeError(
            "Blackbox gray gap record request_id provenance is not canonical"
        )
    return snapshot_id


_GRAY_GAP_TARGET_FIELDS = frozenset(
    {
        "registry_scheme_id",
        "base_scheme_id",
        "target_tenor",
        "horizon",
        "task_type",
        "predict_date",
        "feature_date",
        "target_date",
        "prediction_phase",
    }
)
_GRAY_GAP_DATABRIDGE_AUTHORITY_FIELDS = frozenset(
    {
        "authority_type",
        "generation_id",
        "manifest_sha256",
        "refresh_date",
        "cutoff_date",
        "replay_mode",
        "vintage_disclaimer",
    }
)
_GRAY_GAP_VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)
_RETIRED_GRAY_GAP_EXTRA_FIELDS = frozenset(
    {
        "signal_gap_plan_sha256",
        "execution_group_identity",
        "execution_group_identity_sha256",
        "backfilled_at",
        "backfilled_by",
    }
)
_GRAY_GAP_FIXED_ATOMIC_TARGETS = {
    "t1_daily": frozenset({"5Y", "10Y"}),
    "t5_daily": frozenset({"3Y", "5Y", "7Y", "10Y"}),
}


def _require_lower_sha256(value: object) -> str:
    field = "source_authority.manifest_sha256"
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a lowercase SHA256 digest")
    normalized = _require_sha256(value, field)
    if normalized != value:
        raise ValueError(f"{field} must be a lowercase SHA256 digest")
    return normalized


def _require_iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if normalized != value:
        raise ValueError(f"{field} must be an ISO date")
    return normalized


def _normalize_gray_gap_target_keys(
    cfg: SchemeConfig,
    expected_target_keys: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(expected_target_keys, list) or not expected_target_keys:
        raise ValueError(
            "expected_target_keys must be a non-empty list of mappings"
        )
    if (
        getattr(cfg, "runtime_type", None) != "blackbox_v2"
        and (
            getattr(cfg, "status", None) != "active"
            or getattr(cfg, "version_status", None) != "active"
        )
    ):
        raise RuntimeError(
            "gray gap config must be active with active version_status"
        )
    base_scheme_id = _require_nonempty(
        getattr(cfg, "scheme_id", None),
        "cfg.scheme_id",
    )
    _require_nonempty(
        getattr(cfg, "scheme_version", None),
        "cfg.scheme_version",
    )
    runtime_type = getattr(cfg, "runtime_type", None)
    if runtime_type not in ALLOWED_RUNTIME_TYPES:
        raise ValueError(
            "gray gap config runtime_type must be native_adapter or "
            "blackbox_v2"
        )
    cfg_horizon = getattr(cfg, "horizon", None)
    if isinstance(cfg_horizon, bool) or not isinstance(cfg_horizon, int):
        raise ValueError("cfg.horizon must be an integer")
    cfg_task_type = _require_nonempty(
        getattr(cfg, "task_type", None),
        "cfg.task_type",
    )
    _require_nonempty(
        getattr(cfg, "frequency", None),
        "cfg.frequency",
    )
    raw_tenors = getattr(cfg, "tenors", None)
    if not isinstance(raw_tenors, list) or not raw_tenors:
        raise ValueError("cfg.tenors must be a non-empty list")
    cfg_tenors = [
        _require_nonempty(value, "cfg.tenors item")
        for value in raw_tenors
    ]
    if len(cfg_tenors) != len(set(cfg_tenors)):
        raise ValueError("cfg.tenors must not contain duplicates")
    fixed_targets = _GRAY_GAP_FIXED_ATOMIC_TARGETS.get(base_scheme_id)
    if (
        fixed_targets is not None
        and frozenset(cfg_tenors) != fixed_targets
    ):
        raise RuntimeError(
            f"{base_scheme_id} fixed atomic target multiset mismatch: "
            f"expected={sorted(fixed_targets)}, actual={sorted(cfg_tenors)}"
        )

    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(expected_target_keys):
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"expected_target_keys[{index}] must be a mapping"
            )
        if set(raw) != _GRAY_GAP_TARGET_FIELDS:
            raise ValueError(
                f"expected_target_keys[{index}] must contain exact fields "
                f"{sorted(_GRAY_GAP_TARGET_FIELDS)}"
            )
        horizon = raw["horizon"]
        if isinstance(horizon, bool) or not isinstance(horizon, int):
            raise ValueError(
                f"expected_target_keys[{index}].horizon must be an integer"
            )
        target_tenor = _require_nonempty(
            raw["target_tenor"],
            f"expected_target_keys[{index}].target_tenor",
        )
        row = {
            "registry_scheme_id": _require_nonempty(
                raw["registry_scheme_id"],
                f"expected_target_keys[{index}].registry_scheme_id",
            ),
            "base_scheme_id": _require_nonempty(
                raw["base_scheme_id"],
                f"expected_target_keys[{index}].base_scheme_id",
            ),
            "target_tenor": target_tenor,
            "horizon": horizon,
            "task_type": _require_nonempty(
                raw["task_type"],
                f"expected_target_keys[{index}].task_type",
            ),
            "predict_date": _require_iso_date(
                raw["predict_date"],
                f"expected_target_keys[{index}].predict_date",
            ),
            "feature_date": _require_iso_date(
                raw["feature_date"],
                f"expected_target_keys[{index}].feature_date",
            ),
            "target_date": _require_iso_date(
                raw["target_date"],
                f"expected_target_keys[{index}].target_date",
            ),
            "prediction_phase": _require_nonempty(
                raw["prediction_phase"],
                f"expected_target_keys[{index}].prediction_phase",
            ),
        }
        expected_registry_id = registry_scheme_id(
            base_scheme_id,
            cfg_horizon,
            target_tenor,
        )
        exact_identity = {
            "registry_scheme_id": expected_registry_id,
            "base_scheme_id": base_scheme_id,
            "horizon": cfg_horizon,
            "task_type": cfg_task_type,
            "prediction_phase": "gray_live",
        }
        mismatches = [
            field
            for field, expected in exact_identity.items()
            if row[field] != expected
        ]
        if mismatches:
            raise RuntimeError(
                "expected target identity does not match exact config: "
                + ",".join(mismatches)
            )
        normalized.append(row)

    actual_tenors = Counter(str(row["target_tenor"]) for row in normalized)
    unknown_tenors = sorted(set(actual_tenors) - set(cfg_tenors))
    if unknown_tenors:
        raise RuntimeError(
            "expected target subset does not match exact config: "
            f"unknown={unknown_tenors}, configured={sorted(cfg_tenors)}"
        )
    identities = [
        tuple(row[field] for field in sorted(_GRAY_GAP_TARGET_FIELDS))
        for row in normalized
    ]
    if len(identities) != len(set(identities)):
        raise RuntimeError("expected target multiset contains duplicates")
    _gray_gap_execution_dates(normalized)
    return sorted(
        normalized,
        key=lambda row: (
            str(row["registry_scheme_id"]),
            str(row["target_tenor"]),
        ),
    )


def _gray_gap_execution_dates(
    expected_targets: list[Mapping[str, object]],
) -> dict[str, str]:
    fields = ("predict_date", "feature_date", "target_date")
    values = {
        field: {str(row[field]) for row in expected_targets}
        for field in fields
    }
    inconsistent = [
        field for field, observed in values.items() if len(observed) != 1
    ]
    if inconsistent:
        raise RuntimeError(
            "gray gap execution group dates are inconsistent: "
            + ",".join(inconsistent)
        )
    return {
        field: next(iter(values[field]))
        for field in fields
    }


def _normalize_gray_gap_source_authority(
    source_authority: Mapping[str, object] | None,
    *,
    runtime_type: str,
    feature_date: str,
    predict_date: str,
) -> dict[str, object] | None:
    if runtime_type == "native_adapter":
        if source_authority is not None:
            raise ValueError("Native source_authority must be None")
        return None
    if runtime_type != "blackbox_v2":
        raise ValueError(
            f"unsupported gray gap runtime_type={runtime_type}"
        )
    if not isinstance(source_authority, Mapping):
        raise ValueError("Blackbox DataBridge source_authority is required")
    authority_type = source_authority.get("authority_type")
    if authority_type != "databridge_current_generation":
        raise ValueError(
            "Blackbox source_authority must be a DataBridge authority"
        )
    expected_fields = _GRAY_GAP_DATABRIDGE_AUTHORITY_FIELDS
    if set(source_authority) != expected_fields:
        raise ValueError(
            "source_authority must contain exact fields "
            f"{sorted(expected_fields)}"
        )
    normalized = dict(source_authority)
    normalized["manifest_sha256"] = _require_lower_sha256(
        source_authority["manifest_sha256"],
    )
    cutoff_date = _require_iso_date(
        source_authority["cutoff_date"],
        "source_authority.cutoff_date",
    )
    if cutoff_date != feature_date:
        raise ValueError(
            "source_authority cutoff_date must equal execution feature_date"
        )
    if (
        source_authority["vintage_disclaimer"]
        != _GRAY_GAP_VINTAGE_DISCLAIMER
    ):
        raise ValueError("source_authority vintage_disclaimer is invalid")
    normalized["generation_id"] = _require_nonempty(
        source_authority["generation_id"],
        "source_authority.generation_id",
    )
    refresh_date = _require_iso_date(
        source_authority["refresh_date"],
        "source_authority.refresh_date",
    )
    if refresh_date < predict_date:
        raise ValueError(
            "source_authority refresh_date must be on or after historical "
            "predict_date"
        )
    if source_authority["replay_mode"] != "historical_as_of_replay":
        raise ValueError("source_authority replay_mode is invalid")
    return normalized


def _validate_gray_gap_records(
    cfg: SchemeConfig,
    *,
    records: list[PredictionRecord],
    expected_targets: list[Mapping[str, object]],
) -> None:
    expected = Counter(
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["predict_date"]),
            str(row["feature_date"]),
            str(row["target_date"]),
            str(row["prediction_phase"]),
        )
        for row in expected_targets
    )
    actual: Counter[tuple[object, ...]] = Counter()
    for record in records:
        phase = record.prediction_phase or (
            (record.extra or {}).get("prediction_phase")
            if isinstance(record.extra, Mapping)
            else None
        )
        feature_date = record.feature_date or (
            (record.extra or {}).get("feature_date")
            if isinstance(record.extra, Mapping)
            else None
        )
        try:
            identity = (
                str(record.scheme_id),
                str(record.target_tenor),
                int(record.horizon),
                _require_iso_date(record.predict_date, "record.predict_date"),
                _require_iso_date(feature_date, "record.feature_date"),
                _require_iso_date(record.target_date, "record.target_date"),
                str(phase),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "gray gap record identity mismatch"
            ) from exc
        if record.run_id is not None:
            raise RuntimeError(
                "gray gap records must not carry a pre-bound run_id"
            )
        if (
            record.scheme_version is not None
            and record.scheme_version != str(cfg.scheme_version)
        ):
            raise RuntimeError(
                "gray gap record scheme_version identity mismatch"
            )
        actual[identity] += 1
    if actual != expected:
        raise RuntimeError(
            "gray gap record target multiset or identity mismatch: "
            f"expected={dict(expected)}, actual={dict(actual)}"
        )


def _enrich_gray_gap_records(
    cfg: SchemeConfig,
    *,
    records: list[PredictionRecord],
    source_authority: Mapping[str, object] | None,
) -> list[PredictionRecord]:
    common_extra: dict[str, object] = {
        "backfill_mode": "signal_gap_fill",
    }
    if source_authority is not None:
        common_extra.update(
            {
                "source_authority": dict(source_authority),
                "replay_semantics": _GRAY_GAP_VINTAGE_DISCLAIMER,
                "source_generation_id": source_authority["generation_id"],
                "source_generation_manifest_sha256":
                    source_authority["manifest_sha256"],
                "source_refresh_date": source_authority["refresh_date"],
                "source_cutoff_date": source_authority["cutoff_date"],
            }
        )
    return [
        replace(
            record,
            prediction_phase="gray_live",
            scheme_version=str(cfg.scheme_version),
            extra={
                **{
                    key: value
                    for key, value in dict(record.extra or {}).items()
                    if key not in _RETIRED_GRAY_GAP_EXTRA_FIELDS
                },
                **common_extra,
            },
        )
        for record in records
    ]


def _validate_gray_gap_run(
    cfg: SchemeConfig,
    *,
    run_id: int,
    run: Mapping[str, object] | None,
    expected_count: int,
    predict_date: str,
) -> None:
    if run is None:
        raise RuntimeError(f"gray gap scheme run not found: {run_id}")
    expected_fields = {
        "scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": str(cfg.runtime_type),
        "run_type": "active",
        "prediction_phase": "gray_live",
        "predict_date": predict_date,
        "status": "running",
    }
    identity_matches = all(
        str(run.get(field)) == expected
        for field, expected in expected_fields.items()
    )
    if not identity_matches:
        raise RuntimeError(
            "gray gap completion requires a gray_live running run with exact "
            f"config identity: run_id={run_id}"
        )
    if int(run.get("records_expected") or 0) != expected_count:
        raise RuntimeError(
            "gray gap run records_expected mismatch: "
            f"expected={expected_count}, actual={run.get('records_expected')}"
        )


def _validate_gray_gap_active_version(
    conn: Connection,
    cfg: SchemeConfig,
) -> None:
    version = _read_scheme_version_conn(conn, cfg, for_update=True)
    expected = {
        "scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": str(cfg.runtime_type),
        "status": "active",
    }
    if version is None or any(
        str(version.get(field) or "") != value
        for field, value in expected.items()
    ):
        raise RuntimeError(
            "gray gap completion requires the exact active version"
        )


def _validate_gray_gap_active_registry(
    conn: Connection,
    cfg: SchemeConfig,
) -> None:
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    rows = list(
        (
            conn.execute(
                text(
                    """
                    SELECT scheme_id, base_scheme_id, runtime_type, frequency,
                           task_type, target_tenor, horizon
                    FROM t_scheme_registry
                    WHERE base_scheme_id = :base_scheme_id
                      AND status = 'active'
                    ORDER BY scheme_id
                    """
                    + lock
                ),
                {"base_scheme_id": str(cfg.scheme_id)},
            )
            .mappings()
            .all()
        )
    )
    actual = Counter(
        (
            str(row["scheme_id"]),
            str(row["base_scheme_id"]),
            str(row["runtime_type"]),
            str(row["frequency"]),
            str(row["task_type"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in rows
    )
    expected = Counter(
        (
            registry_scheme_id(
                str(cfg.scheme_id),
                int(cfg.horizon),
                str(target_tenor),
            ),
            str(cfg.scheme_id),
            str(cfg.runtime_type),
            str(cfg.frequency),
            str(cfg.task_type),
            str(target_tenor),
            int(cfg.horizon),
        )
        for target_tenor in cfg.tenors
    )
    if actual != expected:
        raise RuntimeError(
            "gray gap active Registry target multiset mismatch: "
            f"expected={dict(expected)}, actual={dict(actual)}"
        )


def _assert_gray_gap_business_keys_absent(
    conn: Connection,
    expected_targets: list[Mapping[str, object]],
) -> None:
    sorted_rows = sorted(
        expected_targets,
        key=lambda item: (
            str(item["base_scheme_id"]),
            str(item["target_tenor"]),
            int(item["horizon"]),
            str(item["target_date"]),
        ),
    )
    keys = tuple(
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in sorted_rows
    )
    existing = _lock_existing_prediction_business_keys(conn, keys)
    for row, key in zip(sorted_rows, keys):
        if key in existing:
            raise RuntimeError(
                "gray gap business key already exists; entire group "
                "rejected: "
                f"{row['base_scheme_id']}/{row['target_tenor']}/"
                f"h{row['horizon']}/{row['target_date']}"
            )


def fail_scheme_run_atomic(
    engine: Engine,
    *,
    run_id: int,
    scheme_id: str,
    run_date: str,
    duration_sec: float,
    records_returned: int | None,
    error_message: str,
) -> None:
    """以独立事务同时写入 failed run 状态与失败日志。"""
    normalized_run_date = _require_iso_date(run_date, "run_date")
    with engine.begin() as conn:
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        identity_errors = []
        if run is None:
            identity_errors.append("run missing")
        else:
            if run.get("scheme_id") != scheme_id:
                identity_errors.append(
                    "scheme_id: "
                    f"expected={scheme_id!r}, got={run.get('scheme_id')!r}"
                )
            actual_run_date = _stored_iso_date(run, "predict_date")
            if actual_run_date != normalized_run_date:
                identity_errors.append(
                    "predict_date: "
                    f"expected={normalized_run_date!r}, got={actual_run_date!r}"
                )
            if run.get("status") != "running":
                identity_errors.append(
                    "status: expected='running', "
                    f"got={run.get('status')!r}"
                )
        if identity_errors:
            raise RuntimeError(
                "failure run identity revalidation failed: "
                + "; ".join(identity_errors)
            )
        _finish_scheme_run_conn(
            conn,
            run_id=run_id,
            status="failed",
            records_returned=records_returned,
            records_written=0,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            scheme_id,
            normalized_run_date,
            "failed",
            duration_sec,
            error_message,
            run_id,
        )


@contextmanager
def _approved_blackbox_write_transaction(
    engine: Engine,
    cfg: SchemeConfig,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> Iterator[tuple[Connection, list[PredictionRecord], str]]:
    """统一 Blackbox 最终写入的生命周期锁、配置与 DB 复核边界。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("approved Blackbox insert requires runtime_type=blackbox_v2")
    if scheme_version is not None and scheme_version != cfg.scheme_version:
        raise ValueError(
            f"prediction scheme_version={scheme_version} does not match config {cfg.scheme_version}"
        )
    scheme_path_value = getattr(cfg, "path", None)
    if scheme_path_value is None:
        raise RuntimeError("Blackbox canonical config path is required for final write")
    scheme_path = Path(scheme_path_value)
    if scheme_path.name != cfg.scheme_id or scheme_path.parent.name != "schemes":
        raise RuntimeError(
            "Blackbox canonical config path is invalid for final write: "
            f"scheme_id={cfg.scheme_id} path={scheme_path}"
        )
    config_path = scheme_path / "config.yaml"
    exact_scheme_version = cfg.scheme_version
    record_list = list(records)
    mismatched_record_versions = sorted(
        {
            str(record.scheme_version)
            for record in record_list
            if record.scheme_version is not None and record.scheme_version != exact_scheme_version
        }
    )
    if mismatched_record_versions:
        raise ValueError(
            "prediction records contain scheme versions that do not match approved config "
            f"{exact_scheme_version}: {mismatched_record_versions}"
        )

    current_cfg = load_scheme_config(config_path)
    if current_cfg.scheme_version != cfg.scheme_version:
        raise RuntimeError(
            "Blackbox canonical config changed before final write: "
            f"expected={cfg.scheme_version}, current={current_cfg.scheme_version}"
        )
    cfg = current_cfg
    with engine.begin() as conn:
        approval = _read_blackbox_execution_approval_conn(conn, cfg, for_update=True)
        if not approval.executable:
            raise RuntimeError(
                f"Blackbox V2 version is not production-approved: {approval.reason}"
            )
        yield conn, record_list, exact_scheme_version


def _prepare_run_prediction_rows(
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> list[dict[str, object]]:
    """将 prediction records 规范化并验证为最终 SQL rows。"""
    rows: list[dict[str, object]] = []
    for record in records:
        row = asdict(record)
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(
                "feature_date is required for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                "anchor_date must equal feature_date for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        phase = record.prediction_phase or extra.get("prediction_phase")
        if phase not in LIVE_PREDICTION_PHASES:
            raise ValueError(
                f"prediction_phase must be one of {sorted(LIVE_PREDICTION_PHASES)} "
                f"for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        extra["feature_date"] = str(feature_date)
        extra.pop("prediction_phase", None)
        row["run_id"] = record.run_id if record.run_id is not None else run_id
        if int(row["run_id"]) != int(run_id):
            raise RuntimeError(
                "prediction record run_id must match the committing run: "
                f"{row['run_id']} != {run_id}"
            )
        row["scheme_version"] = (
            record.scheme_version
            if record.scheme_version is not None
            else scheme_version
        )
        row["backtest_run_id"] = None
        row["backtest_actual_direction"] = None
        if not str(row["scheme_version"] or "").strip():
            raise ValueError(
                "scheme_version is required for live prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        row["feature_date"] = str(feature_date)
        row.pop("prediction_phase", None)
        row["extra"] = json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    return rows


def _prepare_blackbox_backtest_fact_rows_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    backtest_run_id: int,
    backtest_benchmark_id: str,
    backtest_data_snapshot_id: str,
    backtest_generation_id: str,
    backtest_runtime_profile: str,
    backtest_environment_fingerprint: str,
    backtest_code_hash: str,
    backtest_config_hash: str,
    backtest_manifest_hash: str,
    backtest_validator_policy_digest: str,
    for_update: bool = True,
    require_source_request: bool = False,
) -> list[dict[str, object]]:
    """锁定并把首次激活的 exact-version 回测转换为产品事实 rows。"""
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    run = conn.execute(
        text(
            "SELECT id, benchmark_id, scheme_id, data_source, status, run_mode, "
            "code_hash, config_hash, input_artifact_hash, summary "
            "FROM t_backtest_runs WHERE id = :backtest_run_id"
            + lock_clause
        ),
        {"backtest_run_id": backtest_run_id},
    ).mappings().one_or_none()
    if run is None:
        raise ValueError("Blackbox activation backtest run does not exist")
    summary = _json_mapping(run.get("summary"))
    expected = {
        "scheme_id": cfg.scheme_id,
        "data_source": "blackbox_v2_current_snapshot_as_of",
        "status": "success",
        "run_mode": "persist",
        "benchmark_id": backtest_benchmark_id,
        "code_hash": backtest_code_hash,
        "config_hash": backtest_config_hash,
        "input_artifact_hash": backtest_data_snapshot_id,
        "scheme_version": cfg.scheme_version,
        "manifest_hash": backtest_manifest_hash,
        "script_validator_policy_digest": backtest_validator_policy_digest,
        "data_snapshot_id": backtest_data_snapshot_id,
        "generation_id": backtest_generation_id,
        "runtime_profile": backtest_runtime_profile,
        "environment_fingerprint": backtest_environment_fingerprint,
    }
    actual = {
        "scheme_id": str(run.get("scheme_id") or ""),
        "data_source": str(run.get("data_source") or ""),
        "status": str(run.get("status") or ""),
        "run_mode": str(run.get("run_mode") or ""),
        "benchmark_id": str(run.get("benchmark_id") or ""),
        "code_hash": str(run.get("code_hash") or ""),
        "config_hash": str(run.get("config_hash") or ""),
        "input_artifact_hash": str(run.get("input_artifact_hash") or ""),
        "scheme_version": str(summary.get("scheme_version") or ""),
        "manifest_hash": str(summary.get("manifest_hash") or ""),
        "script_validator_policy_digest": str(
            summary.get("script_validator_policy_digest") or ""
        ),
        "data_snapshot_id": str(summary.get("data_snapshot_id") or ""),
        "generation_id": str(summary.get("generation_id") or ""),
        "runtime_profile": str(summary.get("runtime_profile") or ""),
        "environment_fingerprint": str(
            summary.get("environment_fingerprint") or ""
        ),
    }
    if actual != expected:
        raise ValueError(
            "Blackbox activation backtest identity mismatch: "
            f"expected={expected!r} actual={actual!r}"
        )
    source_request_column = ", source_row" if require_source_request else ""
    source_rows = conn.execute(
        text(
            "SELECT scheme_id, target_tenor, horizon, predict_date, feature_date, "
            "target_date, label, predicted_direction, confidence, extra"
            + source_request_column
            + " "
            "FROM t_backtest_predictions "
            "WHERE run_id = :backtest_run_id "
            "ORDER BY target_tenor, horizon, target_date, predict_date, id"
            + lock_clause
        ),
        {"backtest_run_id": backtest_run_id},
    ).mappings().all()
    if not source_rows:
        raise ValueError("Blackbox activation backtest contains no predictions")
    if require_source_request:
        predict_dates = [str(row.get("predict_date")) for row in source_rows]
        target_dates = [str(row.get("target_date")) for row in source_rows]
        expected_coverage = {
            "backtest_start_date": _NATIVE_SUCCESSOR_BACKTEST_START_DATE,
            "target_date_before": _NATIVE_SUCCESSOR_LIVE_TARGET_START_DATE,
            "request_count": len(source_rows),
            "actual_predict_date_min": min(predict_dates),
            "actual_predict_date_max": max(predict_dates),
            "actual_target_date_min": min(target_dates),
            "actual_target_date_max": max(target_dates),
        }
        actual_coverage = {
            "backtest_start_date": str(summary.get("backtest_start_date") or ""),
            "target_date_before": str(summary.get("target_date_before") or ""),
            "request_count": summary.get("request_count"),
            "actual_predict_date_min": str(
                summary.get("actual_predict_date_min") or ""
            ),
            "actual_predict_date_max": str(
                summary.get("actual_predict_date_max") or ""
            ),
            "actual_target_date_min": str(
                summary.get("actual_target_date_min") or ""
            ),
            "actual_target_date_max": str(
                summary.get("actual_target_date_max") or ""
            ),
        }
        if actual_coverage != expected_coverage:
            raise ValueError(
                "Native successor backtest coverage summary mismatch: "
                f"expected={expected_coverage!r} actual={actual_coverage!r}"
            )
    expected_tenors = set(str(value) for value in cfg.tenors)
    rows: list[dict[str, object]] = []
    seen: set[_PredictionBusinessKey] = set()
    for source in source_rows:
        if str(source.get("scheme_id") or "") != cfg.scheme_id:
            raise ValueError("Blackbox activation backtest scheme identity mismatch")
        tenor = str(source.get("target_tenor") or "")
        horizon = int(source.get("horizon") or 0)
        predict_date = source.get("predict_date")
        feature_date = source.get("feature_date")
        target_date = source.get("target_date")
        direction = source.get("predicted_direction")
        actual_direction = source.get("label")
        if tenor not in expected_tenors or horizon != int(cfg.horizon):
            raise ValueError("Blackbox activation backtest target scope mismatch")
        if predict_date is None or feature_date is None or target_date is None:
            raise ValueError("Blackbox activation backtest date lineage is incomplete")
        if direction not in {-1, 0, 1}:
            raise ValueError("Blackbox activation backtest direction is invalid")
        if actual_direction not in {-1, 0, 1}:
            raise ValueError("Blackbox activation backtest actual direction is invalid")
        key = (cfg.scheme_id, tenor, horizon, str(target_date))
        if key in seen:
            raise ValueError("Blackbox activation backtest has duplicate business key")
        seen.add(key)
        extra = _json_mapping(source.get("extra"))
        extra.pop("prediction_phase", None)
        row: dict[str, object] = {
            "run_id": None,
            "backtest_run_id": backtest_run_id,
            "scheme_version": cfg.scheme_version,
            "scheme_id": cfg.scheme_id,
            "target_tenor": tenor,
            "horizon": horizon,
            "predict_date": str(predict_date),
            "feature_date": str(feature_date),
            "target_date": str(target_date),
            "predicted_direction": int(direction),
            "backtest_actual_direction": int(actual_direction),
            "confidence": source.get("confidence"),
            "model_version": cfg.scheme_version,
            "extra": json.dumps(extra, ensure_ascii=False),
        }
        if require_source_request:
            raw_source_row = _json_mapping(source.get("source_row"))
            if set(raw_source_row) != {*REQUEST_FIELDS, "actual"}:
                raise ValueError(
                    "Blackbox activation backtest source_row fields are invalid"
                )
            source_request = request_from_mapping(
                {field: raw_source_row.get(field) for field in REQUEST_FIELDS}
            )
            expected_request_id = (
                f"{cfg.scheme_id}:{predict_date}:{feature_date}:{target_date}"
            )
            if (
                source_request.request_id != expected_request_id
                or source_request.predict_date != str(predict_date)
                or source_request.feature_date != str(feature_date)
                or source_request.target_date != str(target_date)
            ):
                raise ValueError(
                    "Blackbox activation backtest source_row Request identity mismatch"
                )
            row["_source_request"] = asdict(source_request)
        rows.append(row)
    return rows


def _json_mapping(value: object) -> dict[str, object]:
    """把 MySQL JSON 或 SQLite 文本收敛为对象。"""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, Mapping):
            return dict(loaded)
    raise ValueError("JSON value must be an object")


def _insert_run_predictions_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> int:
    """在调用方事务中用唯一产品事实 INSERT 写入已验证 rows。"""
    sqlite = _dialect_name(conn) == "sqlite"
    extra_expression = ":extra" if sqlite else "CAST(:extra AS JSON)"
    statement = """
        INSERT INTO t_scheme_predictions
            (run_id, backtest_run_id, scheme_version, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
             predicted_direction, backtest_actual_direction, confidence, model_version, extra)
        VALUES
            (:run_id, :backtest_run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :predicted_direction, :backtest_actual_direction, :confidence, :model_version, {extra_expression})
        """.format(extra_expression=extra_expression)
    rows = [
        {key: value for key, value in row.items() if key != "_source_request"}
        for row in prediction_rows
    ]
    if not rows:
        return 0
    conn.execute(text(statement), rows)
    return len(rows)


def _same_id_rows_conn(
    conn: Connection, table: str, where: str, params: Mapping[str, object],
    *, order: str, for_update: bool,
) -> list[dict[str, object]]:
    """读取迁移内部固定表名和条件下的完整行，避免数量摘要漏掉内容漂移。"""
    lock = " FOR UPDATE" if for_update and _dialect_name(conn) == "mysql" else ""
    return [dict(row) for row in conn.execute(
        text(f"SELECT * FROM {table} WHERE {where} ORDER BY {order}{lock}"),
        params,
    ).mappings()]


def _same_id_fact_snapshot_conn(
    conn: Connection, scheme_ids: Sequence[str], *, for_update: bool,
) -> dict[str, object]:
    """摘要完整事实、输入与 Harness 行；不改写任何历史内容。"""
    params = {f"id_{index}": value for index, value in enumerate(scheme_ids)}
    values = ",".join(f":{key}" for key in params)
    scope = f"scheme_id IN ({values})"
    tables = (
        ("t_scheme_predictions", "id", scope),
        ("t_scheme_runs", "run_id", scope),
        ("t_backtest_runs", "id", scope),
        ("t_backtest_predictions", "id", scope),
        ("t_backtest_monthly_metrics", "id", scope),
        ("t_input_artifacts", "artifact_id", scope),
        ("t_harness_runs", "harness_run_id", scope),
        ("t_harness_gate_results", "id", "harness_run_id IN "
         f"(SELECT harness_run_id FROM t_harness_runs WHERE {scope})"),
        ("t_backtest_reproduction_checks", "id", "benchmark_id IN "
         f"(SELECT benchmark_id FROM t_backtest_runs WHERE {scope})"),
    )
    result = {}
    for table, order, where in tables:
        rows = _same_id_rows_conn(
            conn, table, where, params, order=order, for_update=for_update,
        )
        result[table] = {"count": len(rows), "sha256": native_successor_plan_sha256(
            {"rows": rows}
        )}
    return result


def _same_id_evidence_conn(
    conn: Connection, old: SchemeConfig, new: SchemeConfig, run_id: str,
    control: Mapping[str, object], *, for_update: bool,
) -> dict[str, object]:
    """只接受数据库中真实完成且绑定 exact 身份的专用迁移证据。"""
    runs = _same_id_rows_conn(
        conn, "t_harness_runs", "harness_run_id = :run", {"run": run_id},
        order="harness_run_id", for_update=for_update,
    )
    expected_run = {
        "scheme_id": new.scheme_id, "scheme_version": new.scheme_version,
        "code_hash": new.code_hash, "config_hash": new.config_hash,
        "stage": "native-runtime-upgrade", "status": "passed",
    }
    if len(runs) != 1 or not runs[0].get("finished_at") or any(
        runs[0].get(key) != value for key, value in expected_run.items()
    ):
        raise RuntimeError("same-ID upgrade Harness run identity/status mismatch")
    gates = _same_id_rows_conn(
        conn, "t_harness_gate_results",
        "harness_run_id = :run AND gate_name = 'native-runtime-upgrade'",
        {"run": run_id}, order="id", for_update=for_update,
    )
    if len(gates) != 1 or gates[0].get("status") != "passed" or not gates[0].get("finished_at"):
        raise RuntimeError("same-ID upgrade requires exactly one passed migration gate")
    summary = gates[0].get("summary_json")
    if isinstance(summary, str):
        summary = json.loads(summary)
    if (
        not isinstance(summary, Mapping)
        or set(summary) != {"passed", "evidence", "errors"}
        or summary["passed"] is not True or summary["errors"] != []
        or not isinstance(summary["evidence"], list)
        or len(summary["evidence"]) != 1
        or not isinstance(summary["evidence"][0], Mapping)
        or set(summary["evidence"][0]) != {"key", "value"}
        or summary["evidence"][0]["key"] != "runtime_upgrade"
    ):
        raise RuntimeError("same-ID upgrade requires one unambiguous runtime_upgrade evidence")
    summary = summary["evidence"][0]["value"]
    identity_fields = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")
    databridge = control.get("databridge")
    if not isinstance(databridge, Mapping):
        raise ValueError("same-ID upgrade requires current DataBridge identity")
    expected = {
        "schema_version": "same-id-runtime-upgrade-evidence-v1",
        "scheme_id": new.scheme_id,
        "old_identity": {key: getattr(old, key) for key in identity_fields},
        "new_identity": {key: getattr(new, key) for key in identity_fields},
        "environment_fingerprint": new.environment_fingerprint,
        "data_snapshot_id": new.data_snapshot_id,
        "generation_id": databridge.get("generation_id"),
    }
    if (
        not isinstance(summary, Mapping)
        or not new.environment_fingerprint or not new.data_snapshot_id
        or not expected["generation_id"]
        or control.get("blackbox_environment_fingerprint") != new.environment_fingerprint
        or new.data_snapshot_id != databridge.get("data_snapshot_id")
        or set(summary) != set(expected) | {"equivalence_sha256", "local_execution_sha256"}
        or any(summary.get(key) != value for key, value in expected.items())
    ):
        raise RuntimeError("same-ID upgrade migration evidence identity mismatch")
    for key in ("equivalence_sha256", "local_execution_sha256"):
        value = summary[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise RuntimeError(f"invalid same-ID migration evidence digest: {key}")
    if control.get("wave") == "W3A" and control.get("w3a_readiness") is not None:
        readiness = control["w3a_readiness"]
        if (not isinstance(readiness, Mapping)
                or summary["local_execution_sha256"] != readiness.get("local_execution_sha256", {}).get(new.scheme_id)
                or summary["equivalence_sha256"] != readiness.get("equivalence_sha256", {}).get(new.scheme_id)
                or readiness.get("harness_run_ids", {}).get(new.scheme_id) != run_id):
            raise RuntimeError("W3A Gate differs from read-back preparation readiness")
    return {"harness_run_id": run_id, "sha256": native_successor_plan_sha256(
        {"run": runs[0], "gate": gates[0]}
    )}


def _same_id_runtime_upgrade_plan_conn(
    conn: Connection, *, old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig], harness_run_ids: Mapping[str, str],
    action: str, control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str, for_update: bool,
) -> dict[str, object]:
    """锁定同 ID 整批升级的事实、唯一 Writer 和证据前提。"""
    if action not in {"cutover", "rollback"}:
        raise ValueError("same-ID upgrade action must be cutover or rollback")
    ids = sorted(old_configs)
    if not ids or set(ids) != set(new_configs) or set(ids) != set(harness_run_ids):
        raise ValueError("same-ID upgrade requires identical nonempty config/evidence identities")
    if not isinstance(control_plane_evidence, Mapping) or not control_plane_evidence:
        raise ValueError("same-ID upgrade requires verified control-plane evidence")
    if _dialect_name(conn) == "mysql":
        _require_nonempty(expected_database_name, "expected_database_name")
        _require_nonempty(expected_server_uuid, "expected_server_uuid")
        identity = dict(conn.execute(text(
            "SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid"
        )).mappings().one())
        if identity != {"database_name": expected_database_name, "server_uuid": expected_server_uuid}:
            raise RuntimeError("same-ID upgrade database identity mismatch")
    elif _dialect_name(conn) == "sqlite":
        identity = {"isolated_test": "sqlite"}
    else:
        raise RuntimeError("same-ID upgrade supports only MySQL or isolated SQLite tests")
    migrations = _same_id_rows_conn(
        conn, "t_schema_migrations", "1=1", {}, order="version", for_update=for_update,
    )
    if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(
        row["state"] != "APPLIED" for row in migrations
    ):
        raise RuntimeError("same-ID upgrade requires fully applied schema migration 024")
    params = {f"id_{i}": value for i, value in enumerate(ids)}
    values = ",".join(f":{key}" for key in params)
    versions = _same_id_rows_conn(
        conn, "t_scheme_versions", f"scheme_id IN ({values})", params,
        order="scheme_id, scheme_version", for_update=for_update,
    )
    registry = _same_id_rows_conn(
        conn, "t_scheme_registry", f"base_scheme_id IN ({values})", params,
        order="scheme_id", for_update=for_update,
    )
    for table, order in (("t_scheme_runs", "run_id"), ("t_backtest_runs", "id"), ("t_harness_runs", "harness_run_id")):
        if _same_id_rows_conn(conn, table, f"scheme_id IN ({values}) AND status = 'running'", params,
                             order=order, for_update=for_update):
            raise RuntimeError(f"same-ID upgrade requires zero running rows: {table}")
    evidence = {}
    # 仅完整 W3B 原 ID 批次可收口旧 Native lifecycle 历史；实际 Writer 来自
    # 只读核实的 canonical exact 与已围栏 one-shot，不能由历史 active 行推断。
    scheduler_control = control_plane_evidence.get("scheduler", {})
    canonical_selection = control_plane_evidence.get("native_canonical_selection")
    historical_native_allowed = (
        set(ids) == _SAME_ID_W3B_NATIVE_HISTORY_IDS
        and control_plane_evidence.get("schema_version") == "same-id-w3b-control-plane-v1"
        and control_plane_evidence.get("wave") == "W3B"
        and control_plane_evidence.get("deployment_target") == "aliyun-gray"
        and isinstance(scheduler_control, Mapping)
        and scheduler_control.get("control_plane") == "systemd_one_shot"
        and scheduler_control.get("timer_fenced") is True
        and scheduler_control.get("unique_writer") is True
        and canonical_selection == {
            key: {field: getattr(old_configs[key], field) for field in (
                "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash",
            )} for key in ids
        }
    )
    historical_native_versions = []
    historical_native_versions_to_retire = []
    for scheme_id in ids:
        old, new = old_configs[scheme_id], new_configs[scheme_id]
        _validate_blackbox_revision_candidate(new, require_evidence=True)
        if old.scheme_id != scheme_id or new.scheme_id != scheme_id or old.runtime_type != "native_adapter" or new.runtime_type != "blackbox_v2" or old.scheme_version == new.scheme_version:
            raise ValueError("same-ID upgrade config runtime/exact identity mismatch")
        for field in ("horizon", "task_type", "frequency"):
            if getattr(old, field) != getattr(new, field):
                raise ValueError(f"same-ID upgrade changed business field: {field}")
        old_rule = old.target_rule
        if old_rule is None and old.task_type in {"T+1", "T+5"}:
            old_rule = TASK_COMBINATIONS[old.task_type][1]
        if old_rule != new.target_rule:
            raise ValueError("same-ID upgrade changed business field: target_rule")
        if sorted(old.tenors) != sorted(new.tenors) or len(set(old.tenors)) != len(old.tenors) or not old.tenors or any(
            getattr(old.schedule, key) != getattr(new.schedule, key) for key in ("cron", "timezone")
        ):
            raise ValueError("same-ID upgrade changed targets/schedule")
        scoped = [row for row in versions if row["scheme_id"] == scheme_id]
        old_row = next((row for row in scoped if row["scheme_version"] == old.scheme_version), None)
        new_row = next((row for row in scoped if row["scheme_version"] == new.scheme_version), None)
        if old_row is None:
            raise RuntimeError("same-ID upgrade Native exact version is absent")
        _assert_migration_version_identity(old, old_row)
        if new_row is not None:
            _assert_migration_version_identity(new, new_row)
            if any(new_row.get(key) != getattr(new, key) for key in (
                "environment_fingerprint", "data_snapshot_id", "algorithm_version",
                "contract_version", "runtime_profile",
            )):
                raise RuntimeError("same-ID upgrade candidate environment/input changed")
        expected_active = old if action == "cutover" else new
        if action == "cutover":
            valid = old_row["status"] == "active" and (new_row is None or new_row["status"] == "retired")
        else:
            valid = old_row["status"] == "retired" and new_row is not None and new_row["status"] == "active"
        if not valid:
            raise RuntimeError("same-ID upgrade mixed/unsupported lifecycle state")
        for row in scoped:
            if row.get("runtime_type") not in {"native_adapter", "blackbox_v2"}:
                raise RuntimeError("same-ID upgrade unknown historical runtime")
            if row["scheme_version"] in {old.scheme_version, new.scheme_version}:
                continue
            historical_native = historical_native_allowed and row["runtime_type"] == "native_adapter"
            allowed_statuses = {"retired", "paused"} if historical_native else {"retired"}
            if historical_native and action == "cutover":
                allowed_statuses.add("active")
            if row.get("status") not in allowed_statuses:
                raise RuntimeError("same-ID upgrade unexpected pending/active version (second Writer)")
            if historical_native:
                recorded = {key: row[key] for key in ("scheme_id", "scheme_version", "status")}
                historical_native_versions.append(recorded)
                if row["status"] == "active":
                    historical_native_versions_to_retire.append(recorded | {"expected_rowcount": 1})
        rows = [row for row in registry if row["base_scheme_id"] == scheme_id]
        expected_ids = {registry_scheme_id(scheme_id, old.horizon, tenor): tenor for tenor in old.tenors}
        if {row["scheme_id"] for row in rows} != set(expected_ids):
            raise RuntimeError("same-ID upgrade Registry target coverage mismatch")
        for row in rows:
            tenor = expected_ids[row["scheme_id"]]
            wanted = {"runtime_type": expected_active.runtime_type, "status": "active", "horizon": old.horizon,
                      "target_tenor": tenor, "task_type": old.task_type, "frequency": old.frequency,
                      "schedule_cron": old.schedule.cron, "schedule_timezone": old.schedule.timezone}
            if any(row.get(key) != value for key, value in wanted.items()) or _normalize_registry_tenors(row.get("tenors")) != [tenor]:
                raise RuntimeError("same-ID upgrade Registry identity/status mismatch")
        evidence[scheme_id] = _same_id_evidence_conn(conn, old, new, harness_run_ids[scheme_id],
                                                  control_plane_evidence, for_update=for_update)
    unaffected = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id NOT IN ({values})", params,
                                   order="scheme_id", for_update=for_update)
    return {
        "schema_version": "same-id-runtime-upgrade-plan-v1", "action": action,
        "database_identity_sha256": native_successor_plan_sha256(identity), "schema_migration_version": 24,
        "scheme_ids": ids, "versions": versions, "registry": registry, "evidence": evidence,
        "historical_native_versions": historical_native_versions,
        "historical_native_versions_to_retire": historical_native_versions_to_retire,
        "candidate_versions": [
            {field: getattr(new_configs[scheme_id], field) for field in (
                "scheme_id", "scheme_version", "runtime_type", "code_hash",
                "config_hash", "manifest_hash", "algorithm_version",
                "contract_version", "runtime_profile", "environment_fingerprint",
                "data_snapshot_id",
            )}
            for scheme_id in ids
        ],
        "control_plane": dict(control_plane_evidence),
        "facts": _same_id_fact_snapshot_conn(conn, ids, for_update=for_update),
        "unaffected_registry_sha256": native_successor_plan_sha256({"rows": unaffected}),
    }


def read_same_id_runtime_upgrade_plan(
    engine: Engine, *, old_configs: Mapping[str, SchemeConfig], new_configs: Mapping[str, SchemeConfig],
    harness_run_ids: Mapping[str, str], action: str, control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str,
) -> dict[str, object]:
    """以一致只读快照生成同 ID 升级计划，不写事实或生命周期。"""
    kwargs = dict(old_configs=old_configs, new_configs=new_configs, harness_run_ids=harness_run_ids,
                  action=action, control_plane_evidence=control_plane_evidence,
                  expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
                  for_update=False)
    with engine.connect() as conn:
        if engine.dialect.name == "mysql":
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            return _same_id_runtime_upgrade_plan_conn(conn, **kwargs)
        finally:
            conn.rollback()


def apply_same_id_runtime_upgrade(
    engine: Engine, *, old_configs: Mapping[str, SchemeConfig], new_configs: Mapping[str, SchemeConfig],
    harness_run_ids: Mapping[str, str], action: str, expected_plan_sha256: str, approved_by: str,
    approved_at: datetime, control_plane_evidence_reader: Callable[[], Mapping[str, object]],
    expected_database_name: str, expected_server_uuid: str,
    additional_lifecycle_lock_scheme_ids: Sequence[str] = (),
) -> dict[str, object]:
    """复用正常 activation 互斥锁，整批升级/回滚；历史事实始终只读。"""
    operator = _require_nonempty(approved_by, "approved_by")
    if not isinstance(approved_at, datetime):
        raise ValueError("same-ID upgrade requires approved_at datetime")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in expected_plan_sha256
    ):
        raise ValueError("expected_plan_sha256 must be lowercase SHA-256 hex")
    extra_ids = tuple(additional_lifecycle_lock_scheme_ids)
    if extra_ids and (
        len(extra_ids) != len(set(extra_ids))
        or set(extra_ids) != {scheme_id + "_bbv2" for scheme_id in old_configs}
    ):
        raise ValueError("additional lifecycle locks must cover exactly the temporary successor IDs")
    with ExitStack() as locks:
        if engine.dialect.name != "sqlite":
            for scheme_id in sorted(set(old_configs) | set(extra_ids)):
                locks.enter_context(_blackbox_activation_advisory_lock(engine, scheme_id=scheme_id))
        with engine.begin() as conn:
            control = control_plane_evidence_reader()
            if extra_ids and sorted(control.get("temporary_writer_check", {}).get("scheme_ids", [])) != sorted(extra_ids):
                raise ValueError("temporary lifecycle lock scope differs from captured control plane")
            kwargs = dict(old_configs=old_configs, new_configs=new_configs, harness_run_ids=harness_run_ids,
                          control_plane_evidence=control, expected_database_name=expected_database_name,
                          expected_server_uuid=expected_server_uuid, for_update=True)
            plan = _same_id_runtime_upgrade_plan_conn(conn, action=action, **kwargs)
            if native_successor_plan_sha256(plan) != expected_plan_sha256:
                raise RuntimeError("same-ID upgrade plan changed before commit")
            for scheme_id in sorted(old_configs):
                old, new = old_configs[scheme_id], new_configs[scheme_id]
                retiring, activating = (old, new) if action == "cutover" else (new, old)
                changed = conn.execute(text("UPDATE t_scheme_versions SET status = 'retired' "
                    "WHERE scheme_id = :id AND scheme_version = :version AND status = 'active'"),
                    {"id": scheme_id, "version": retiring.scheme_version})
                if changed.rowcount != 1:
                    raise RuntimeError("same-ID upgrade did not retire exactly one active version")
                if action == "cutover":
                    for historical in plan["historical_native_versions_to_retire"]:
                        if historical["scheme_id"] != scheme_id:
                            continue
                        changed = conn.execute(text("UPDATE t_scheme_versions SET status = 'retired' "
                            "WHERE scheme_id = :id AND scheme_version = :version "
                            "AND runtime_type = 'native_adapter' AND status = 'active'"),
                            {"id": scheme_id, "version": historical["scheme_version"]})
                        if changed.rowcount != historical["expected_rowcount"]:
                            raise RuntimeError("same-ID upgrade historical Native retirement count mismatch")
                    _upsert_scheme_version_conn(conn, new, trusted_status="active", approved_by=operator,
                                                approved_at=_mysql_utc_datetime(approved_at))
                else:
                    changed = conn.execute(text("UPDATE t_scheme_versions SET status = 'active' "
                        "WHERE scheme_id = :id AND scheme_version = :version AND status = 'retired'"),
                        {"id": scheme_id, "version": old.scheme_version})
                    if changed.rowcount != 1:
                        raise RuntimeError("same-ID upgrade could not restore Native exact version")
                changed = conn.execute(text("UPDATE t_scheme_registry SET runtime_type = :runtime "
                    "WHERE base_scheme_id = :id AND status = 'active' AND runtime_type = :prior"),
                    {"id": scheme_id, "runtime": activating.runtime_type, "prior": retiring.runtime_type})
                if changed.rowcount != len(old.tenors):
                    raise RuntimeError("same-ID upgrade Registry update count mismatch")
            post = _same_id_runtime_upgrade_plan_conn(conn, action="rollback" if action == "cutover" else "cutover", **kwargs)
            if post["facts"] != plan["facts"] or post["unaffected_registry_sha256"] != plan["unaffected_registry_sha256"]:
                raise RuntimeError("same-ID upgrade changed facts/unaffected Registry")
            post_versions = {(row["scheme_id"], row["scheme_version"]): row for row in post["versions"]}
            retired_history = {(row["scheme_id"], row["scheme_version"])
                               for row in plan["historical_native_versions_to_retire"]}
            for before in plan["versions"]:
                key = before["scheme_id"], before["scheme_version"]
                if key[1] in {old_configs[key[0]].scheme_version, new_configs[key[0]].scheme_version}:
                    continue
                expected = before | {"status": "retired"} if key in retired_history else before
                ignored = {"updated_at"} if key in retired_history else set()
                if {k: v for k, v in post_versions[key].items() if k not in ignored} != {
                    k: v for k, v in expected.items() if k not in ignored
                }:
                    raise RuntimeError("same-ID upgrade changed historical version identity or paused status")
            # updated_at 是 MySQL 自动更新时间；业务展示字段必须逐字段不变。
            for before, after in zip(plan["registry"], post["registry"]):
                if {k: v for k, v in before.items() if k not in {"runtime_type", "updated_at"}} != {
                    k: v for k, v in after.items() if k not in {"runtime_type", "updated_at"}
                }:
                    raise RuntimeError("same-ID upgrade changed Registry business metadata")
            if native_successor_plan_sha256({"control_plane": control_plane_evidence_reader()}) != native_successor_plan_sha256({"control_plane": control}):
                raise RuntimeError("same-ID upgrade control plane changed during transaction")
    return {"schema_version": "same-id-runtime-upgrade-result-v1", "action": action,
            "scheme_ids": sorted(old_configs), "plan_sha256": expected_plan_sha256, "approved_by": operator}


def _same_id_reclaim_identity(cfg: SchemeConfig) -> dict[str, object]:
    """绑定三种 canonical 身份，不用历史 active 行推断 Native Writer。"""
    return {key: getattr(cfg, key) for key in (
        "scheme_id", "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash",
    )}


def _is_preserved_w2_historical_compare(
    conn: Connection, row: Mapping[str, object], *, for_update: bool = False,
) -> bool:
    """只识别已核实的一条旧 Mac compare 审计；不修改其 running 状态。"""
    expected = {
        "harness_run_id": "hr_20260614T091820Z_126af53720a9", "scheme_id": "daily_5y_2_v28",
        "scheme_version": "accdea99c5c9", "stage": "compare", "status": "running",
    }
    if any(row.get(key) != value for key, value in expected.items()):
        return False
    gates = _same_id_rows_conn(conn, "t_harness_gate_results", "harness_run_id=:run",
                               {"run": expected["harness_run_id"]}, order="id", for_update=for_update)
    return native_successor_plan_sha256({"run": dict(row), "gates": gates}) == _PRESERVED_W2_COMPARE_SHA256


def _reviewed_w3a_reclaim_revision(source: SchemeConfig, new: SchemeConfig, control: Mapping[str, object]) -> bool:
    """仅放行固定 Full 修订，严格绑定已认证转换及本机状态/回滚回执。"""
    key = "liwei_0616_5y01_full_oos_k3_div_k10"
    source_code = "ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103"
    candidate_code = "0fa034ca6fcd9ad358895ccd4c6617a43191cffaff1176823f8dd0ad7b8cbc39"
    source_metadata = "96d46ee4b2fb16f3b7da0c5485808f52f8f7ef14607721fbe00d26bc55d74982"
    candidate_metadata = "ae7bfee67c9eae88c10b57cb32901c7ad206febfebde5435f5ba3c4346c6da69"
    expected = {
        "schema_version": "w3a-weekly-revision-delivery-conversion-v1",
        "source_scheme_id": key + "_bbv2", "scheme_id": key, "target_tenor": "5Y",
        "task_type": "T+5", "execution_horizon": 5, "fact_horizon": 5,
        "source_code_sha256": source_code, "candidate_code_sha256": candidate_code,
        "source_metadata_sha256": source_metadata, "candidate_metadata_sha256": candidate_metadata,
        "algorithm_executions": 0,
        "permitted_algorithm_change": "weekly_raw_prefix_to_feature_suffix_invalidation",
        "weekly_length_guard_preserved": True, "requires_independent_suffix_evidence": True,
    }
    release = control.get("release", {})
    readiness = control.get("w3a_readiness", {})
    return (control.get("wave") == "W3A" and new.scheme_id == key and source.scheme_id == key + "_bbv2"
            and source.scheme_version == "3ee3dd2334fd" and new.scheme_version == "be34f35f233b"
            and (source.code_hash, new.code_hash, source.manifest_hash, new.manifest_hash)
                == (source_code, candidate_code, source_metadata, candidate_metadata)
            and isinstance(release, Mapping) and release.get("identity_conversions", {}).get(key) == expected
            and isinstance(readiness, Mapping) and readiness.get("status") == "ready"
            and readiness.get("source_and_candidate_current_input_verified") is True
            and set(readiness.get("local_execution_sha256", {})) == _SAME_ID_RECLAIM_WAVES["W3A"])


def _same_id_writer_reclaim_plan_conn(
    conn: Connection, *, wave: str, old_configs: Mapping[str, SchemeConfig],
    source_configs: Mapping[str, SchemeConfig], new_configs: Mapping[str, SchemeConfig],
    harness_run_ids: Mapping[str, str], action: str, control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str, for_update: bool,
) -> dict[str, object]:
    """仅批准的完整批次回收临时身份 Writer；预测及回测事实全部只读。"""
    ids = sorted(old_configs)
    if (action not in {"cutover", "rollback"} or wave not in _SAME_ID_RECLAIM_WAVES
            or set(ids) != _SAME_ID_RECLAIM_WAVES[wave]
            or any(set(group) != set(ids) for group in (source_configs, new_configs, harness_run_ids))):
        raise ValueError("same-ID reclaim requires one complete approved wave")
    control = control_plane_evidence
    scheduler = control.get("scheduler", {}) if isinstance(control, Mapping) else {}
    if (not isinstance(control, Mapping)
            or control.get("schema_version") != "same-id-writer-reclaim-control-plane-v1"
            or control.get("wave") != wave or control.get("deployment_target") != "aliyun-gray"
            or not isinstance(scheduler, Mapping) or scheduler.get("control_plane") != "systemd_one_shot"
            or scheduler.get("timer_fenced") is not True or scheduler.get("unique_writer") is not True
            or control.get("native_canonical_selection") != {
                key: _same_id_reclaim_identity(old_configs[key]) for key in ids}
            or control.get("source_canonical_selection") != {
                key: _same_id_reclaim_identity(source_configs[key]) for key in ids}):
        raise RuntimeError("same-ID reclaim requires fenced canonical control-plane evidence")
    if _dialect_name(conn) == "mysql":
        identity = dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
        if identity != {"database_name": _require_nonempty(expected_database_name, "expected_database_name"),
                        "server_uuid": _require_nonempty(expected_server_uuid, "expected_server_uuid")}:
            raise RuntimeError("same-ID reclaim database identity mismatch")
    elif _dialect_name(conn) == "sqlite":
        identity = {"isolated_test": "sqlite"}
    else:
        raise RuntimeError("same-ID reclaim supports MySQL or isolated SQLite only")
    if wave == "W3A" and control.get("w3a_readiness") is not None:
        if control["w3a_readiness"].get("prepare_database_identity_sha256") != native_successor_plan_sha256(identity):
            raise RuntimeError("W3A readiness originated from another database")
    migrations = _same_id_rows_conn(conn, "t_schema_migrations", "1=1", {}, order="version", for_update=for_update)
    if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(row["state"] != "APPLIED" for row in migrations):
        raise RuntimeError("same-ID reclaim requires fully applied schema migration 024")
    all_ids = sorted(ids + [key + "_bbv2" for key in ids])
    params = {f"id_{i}": key for i, key in enumerate(all_ids)}
    placeholders = ",".join(":" + key for key in params)
    versions = _same_id_rows_conn(conn, "t_scheme_versions", f"scheme_id IN ({placeholders})", params,
                                  order="scheme_id,scheme_version", for_update=for_update)
    registry = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id IN ({placeholders})", params,
                                  order="scheme_id", for_update=for_update)
    preserved_harness = []
    for table, order in (("t_scheme_runs", "run_id"), ("t_backtest_runs", "id"), ("t_harness_runs", "harness_run_id")):
        running = _same_id_rows_conn(conn, table, f"scheme_id IN ({placeholders}) AND status='running'", params,
                                    order=order, for_update=for_update)
        for row in running:
            if (wave == "W2" and table == "t_harness_runs"
                    and _is_preserved_w2_historical_compare(conn, row, for_update=for_update)):
                preserved_harness.append({"harness_run_id": row["harness_run_id"],
                                          "sha256": _PRESERVED_W2_COMPARE_SHA256})
            else:
                raise RuntimeError(f"same-ID reclaim requires zero running rows: {table}")
    historical, retiring, evidence, backtests = [], [], {}, {}
    for key in ids:
        old, source, new = old_configs[key], source_configs[key], new_configs[key]
        if (old.scheme_id != key or new.scheme_id != key or source.scheme_id != key + "_bbv2"
                or old.runtime_type != "native_adapter" or source.runtime_type != "blackbox_v2"
                or old.scheme_version == new.scheme_version
                or (source.code_hash != new.code_hash and not _reviewed_w3a_reclaim_revision(source, new, control))
                or any(getattr(source, field) != getattr(new, field) for field in
                       ("algorithm_version", "contract_version", "runtime_profile"))):
            raise ValueError("same-ID reclaim config/code identity mismatch")
        _validate_blackbox_revision_candidate(new, require_evidence=True)
        for cfg in (source, new):
            expected_horizon = 1 if wave == "W1B" and cfg is source else old.horizon
            if (cfg.horizon != expected_horizon
                    or any(getattr(cfg, field) != getattr(old, field) for field in ("task_type", "frequency"))
                    or sorted(cfg.tenors) != sorted(old.tenors) or len(set(old.tenors)) != len(old.tenors)
                    or not old.tenors or any(getattr(cfg.schedule, field) != getattr(old.schedule, field)
                                           for field in ("cron", "timezone"))):
                raise ValueError("same-ID reclaim business dimensions differ")
            implicit_rule = old.task_type in {"T+1", "T+5"} or (wave == "W1B" and old.task_type == "weekly_point")
            old_rule = old.target_rule or (TASK_COMBINATIONS[old.task_type][1] if implicit_rule else None)
            if cfg.target_rule != old_rule:
                raise ValueError("same-ID reclaim target_rule differs")
        if wave == "W1B":
            from shared.scheme_config_schema import resolve_fact_horizon

            if (old.task_type != "weekly_point" or old.frequency != "weekly" or old.horizon != 6
                    or new.horizon != resolve_fact_horizon(key, "weekly_point", source.horizon, old.horizon)
                    or scheduler.get("cadence") != "weekly"):
                raise ValueError("weekly reclaim requires exact execution/fact horizon and weekly fence")
        by_key = {(row["scheme_id"], row["scheme_version"]): row for row in versions}
        old_row = by_key.get((key, old.scheme_version))
        source_row = by_key.get((source.scheme_id, source.scheme_version))
        new_row = by_key.get((key, new.scheme_version))
        if old_row is None or source_row is None:
            raise RuntimeError("same-ID reclaim canonical Native/source exact is absent")
        for cfg, row in ((old, old_row), (source, source_row), (new, new_row)):
            if row is not None:
                _assert_migration_version_identity(cfg, row)
                if cfg.runtime_type == "blackbox_v2" and any(
                    row.get(field) != getattr(cfg, field) for field in
                    ("algorithm_version", "contract_version", "runtime_profile")
                ):
                    raise RuntimeError("same-ID reclaim Blackbox runtime identity differs")
        if new_row is not None and any(new_row.get(field) != getattr(new, field)
                                       for field in ("environment_fingerprint", "data_snapshot_id")):
            raise RuntimeError("same-ID reclaim candidate environment/input differs")
        if (old_row["status"] != "retired"
                or source_row["status"] != ("active" if action == "cutover" else "retired")
                or (action == "cutover" and new_row is not None and new_row["status"] != "retired")
                or (action == "rollback" and (new_row is None or new_row["status"] != "active"))):
            raise RuntimeError("same-ID reclaim lifecycle state differs")
        for row in versions:
            if row["scheme_id"] not in {key, source.scheme_id} or row in (old_row, source_row, new_row):
                continue
            native = row["scheme_id"] == key and row["runtime_type"] == "native_adapter"
            allowed = {"retired", "paused"} if native else {"retired"}
            if native and action == "cutover":
                allowed.add("active")
            if row["runtime_type"] not in {"native_adapter", "blackbox_v2"} or row["status"] not in allowed:
                raise RuntimeError("same-ID reclaim unexpected historical second Writer")
            if native:
                entry = {field: row[field] for field in ("scheme_id", "scheme_version", "status")}
                historical.append(entry)
                if row["status"] == "active":
                    retiring.append(entry | {"expected_rowcount": 1})
        for cfg, status, runtime in (
            (old, "archived" if action == "cutover" else "active", "native_adapter" if action == "cutover" else "blackbox_v2"),
            (source, "active" if action == "cutover" else "archived", "blackbox_v2"),
        ):
            rows = [row for row in registry if row["base_scheme_id"] == cfg.scheme_id]
            expected_ids = {registry_scheme_id(cfg.scheme_id, cfg.horizon, tenor): tenor for tenor in cfg.tenors}
            if {row["scheme_id"] for row in rows} != set(expected_ids):
                raise RuntimeError("same-ID reclaim Registry target coverage differs")
            for row in rows:
                tenor = expected_ids[row["scheme_id"]]
                wanted = {"status": status, "runtime_type": runtime, "target_tenor": tenor,
                          "horizon": cfg.horizon, "task_type": cfg.task_type, "frequency": cfg.frequency,
                          "schedule_cron": cfg.schedule.cron, "schedule_timezone": cfg.schedule.timezone}
                if any(row.get(field) != value for field, value in wanted.items()) or _normalize_registry_tenors(row.get("tenors")) != [tenor]:
                    raise RuntimeError("same-ID reclaim Registry business/status differs")
        evidence[key] = _same_id_evidence_conn(conn, old, new, harness_run_ids[key], control, for_update=for_update)
        runs = _same_id_rows_conn(conn, "t_backtest_runs", "scheme_id=:id AND status='success' AND run_mode='persist'",
                                  {"id": source.scheme_id}, order="id", for_update=for_update)
        matches = []
        for run in runs:
            summary = _json_mapping(run.get("summary"))
            if (run.get("data_source") == "blackbox_v2_current_snapshot_as_of"
                    and run.get("code_hash") == source.code_hash and run.get("config_hash") == source.config_hash
                    and summary.get("scheme_version") == source.scheme_version
                    and summary.get("manifest_hash") == source.manifest_hash
                    and summary.get("runtime_profile") == source.runtime_profile
                    and all(summary.get(field) for field in ("environment_fingerprint", "data_snapshot_id", "generation_id"))):
                matches.append(run)
        if not matches:
            raise RuntimeError("same-ID reclaim requires a successful exact source Blackbox backtest")
        selected = matches[-1]
        rows = _same_id_rows_conn(conn, "t_backtest_predictions", "run_id=:run", {"run": selected["id"]},
                                  order="id", for_update=for_update)
        if (not rows or {row["target_tenor"] for row in rows} != set(source.tenors)
                or any(row["scheme_id"] != source.scheme_id or row["horizon"] != source.horizon for row in rows)):
            raise RuntimeError("same-ID reclaim source backtest has no complete persisted target coverage")
        backtests[key] = {"backtest_run_id": selected["id"], "benchmark_id": selected["benchmark_id"],
                         "sha256": native_successor_plan_sha256({"run": selected, "predictions": rows})}
    unaffected = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id NOT IN ({placeholders})", params,
                                    order="scheme_id", for_update=for_update)
    return {
        "schema_version": "same-id-writer-reclaim-plan-v1", "wave": wave, "action": action,
        "scheme_ids": ids, "source_scheme_ids": [key + "_bbv2" for key in ids],
        "database_identity_sha256": native_successor_plan_sha256(identity), "schema_migration_version": 24,
        "versions": versions, "registry": registry, "evidence": evidence, "source_backtests": backtests,
        "historical_native_versions": historical, "historical_native_versions_to_retire": retiring,
        "preserved_historical_harness_runs": preserved_harness,
        "candidate_versions": [{**_same_id_reclaim_identity(new_configs[key]), **{
            field: getattr(new_configs[key], field) for field in
            ("algorithm_version", "contract_version", "runtime_profile", "environment_fingerprint", "data_snapshot_id")}}
            for key in ids],
        "control_plane": dict(control), "facts": _same_id_fact_snapshot_conn(conn, all_ids, for_update=for_update),
        "unaffected_registry_sha256": native_successor_plan_sha256({"rows": unaffected}),
        "prediction_preservation": "read-only; missing alias facts require a separate authorized operation",
    }


def read_same_id_writer_reclaim_plan(
    engine: Engine, *, wave: str, old_configs: Mapping[str, SchemeConfig],
    source_configs: Mapping[str, SchemeConfig], new_configs: Mapping[str, SchemeConfig],
    harness_run_ids: Mapping[str, str], action: str, control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str,
) -> dict[str, object]:
    """只读生成 W2/W3A Writer 回收或回滚计划，不补入历史事实。"""
    with engine.connect() as conn:
        if engine.dialect.name == "mysql":
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            return _same_id_writer_reclaim_plan_conn(conn, wave=wave, old_configs=old_configs,
                source_configs=source_configs, new_configs=new_configs, harness_run_ids=harness_run_ids,
                action=action, control_plane_evidence=control_plane_evidence,
                expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid, for_update=False)
        finally:
            conn.rollback()


def apply_same_id_writer_reclaim(
    engine: Engine, *, wave: str, old_configs: Mapping[str, SchemeConfig],
    source_configs: Mapping[str, SchemeConfig], new_configs: Mapping[str, SchemeConfig],
    harness_run_ids: Mapping[str, str], action: str, expected_plan_sha256: str,
    approved_by: str, approved_at: datetime, control_plane_evidence_reader: Callable[[], Mapping[str, object]],
    expected_database_name: str, expected_server_uuid: str,
) -> dict[str, object]:
    """持有原/临时 ID 正常 lifecycle 锁，整组切换唯一 Writer；所有事实不变。"""
    operator = _require_nonempty(approved_by, "approved_by")
    if not isinstance(approved_at, datetime) or not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_plan_sha256):
        raise ValueError("same-ID reclaim requires approved_at and exact plan SHA-256")
    kwargs = dict(wave=wave, old_configs=old_configs, source_configs=source_configs, new_configs=new_configs,
                  harness_run_ids=harness_run_ids, expected_database_name=expected_database_name,
                  expected_server_uuid=expected_server_uuid, for_update=True)
    with ExitStack() as locks:
        if engine.dialect.name != "sqlite":
            for key in sorted(set(old_configs) | {key + "_bbv2" for key in old_configs}):
                locks.enter_context(_blackbox_activation_advisory_lock(engine, scheme_id=key))
        with engine.begin() as conn:
            control = control_plane_evidence_reader()
            plan = _same_id_writer_reclaim_plan_conn(conn, action=action, control_plane_evidence=control, **kwargs)
            if native_successor_plan_sha256(plan) != expected_plan_sha256:
                raise RuntimeError("same-ID reclaim plan changed before commit")
            def change_version(scheme_id, version, prior, status):
                changed = conn.execute(text("UPDATE t_scheme_versions SET status=:status "
                    "WHERE scheme_id=:id AND scheme_version=:version AND status=:prior"),
                    {"id": scheme_id, "version": version, "prior": prior, "status": status})
                if changed.rowcount != 1:
                    raise RuntimeError("same-ID reclaim exact version update count mismatch")
            for key in plan["scheme_ids"]:
                old, source, new = old_configs[key], source_configs[key], new_configs[key]
                if action == "cutover":
                    change_version(source.scheme_id, source.scheme_version, "active", "retired")
                    for row in plan["historical_native_versions_to_retire"]:
                        if row["scheme_id"] == key:
                            change_version(key, row["scheme_version"], "active", "retired")
                    _upsert_scheme_version_conn(conn, new, trusted_status="active", approved_by=operator,
                                                approved_at=_mysql_utc_datetime(approved_at))
                else:
                    change_version(key, new.scheme_version, "active", "retired")
                    change_version(source.scheme_id, source.scheme_version, "retired", "active")
                for cfg, prior_status, status, prior_runtime, runtime in (
                    (old, "archived" if action == "cutover" else "active", "active" if action == "cutover" else "archived",
                     "native_adapter" if action == "cutover" else "blackbox_v2", "blackbox_v2" if action == "cutover" else "native_adapter"),
                    (source, "active" if action == "cutover" else "archived", "archived" if action == "cutover" else "active",
                     "blackbox_v2", "blackbox_v2"),
                ):
                    changed = conn.execute(text("UPDATE t_scheme_registry SET status=:status,runtime_type=:runtime "
                        "WHERE base_scheme_id=:id AND status=:prior_status AND runtime_type=:prior_runtime"),
                        {"id": cfg.scheme_id, "status": status, "runtime": runtime,
                         "prior_status": prior_status, "prior_runtime": prior_runtime})
                    if changed.rowcount != len(cfg.tenors):
                        raise RuntimeError("same-ID reclaim Registry update count mismatch")
            post = _same_id_writer_reclaim_plan_conn(conn, action="rollback" if action == "cutover" else "cutover",
                                                      control_plane_evidence=control, **kwargs)
            if post["facts"] != plan["facts"] or post["unaffected_registry_sha256"] != plan["unaffected_registry_sha256"]:
                raise RuntimeError("same-ID reclaim changed facts/unaffected Registry")
            for before, after in zip(plan["registry"], post["registry"]):
                if {k: v for k, v in before.items() if k not in {"status", "runtime_type", "updated_at"}} != {
                    k: v for k, v in after.items() if k not in {"status", "runtime_type", "updated_at"}}:
                    raise RuntimeError("same-ID reclaim changed Registry business metadata")
            post_versions = {(row["scheme_id"], row["scheme_version"]): row for row in post["versions"]}
            changed_status = {(row["scheme_id"], row["scheme_version"]): "retired"
                              for row in plan["historical_native_versions_to_retire"]}
            for key, cfg in source_configs.items():
                changed_status[cfg.scheme_id, cfg.scheme_version] = "retired" if action == "cutover" else "active"
            for before in plan["versions"]:
                key = before["scheme_id"], before["scheme_version"]
                if key[0] in new_configs and key[1] == new_configs[key[0]].scheme_version:
                    continue
                expected = before | {"status": changed_status[key]} if key in changed_status else before
                ignored = {"updated_at"} if key in changed_status else set()
                if {k: v for k, v in post_versions[key].items() if k not in ignored} != {
                    k: v for k, v in expected.items() if k not in ignored}:
                    raise RuntimeError("same-ID reclaim changed historical version metadata/status")
            if native_successor_plan_sha256({"control_plane": control_plane_evidence_reader()}) != native_successor_plan_sha256({"control_plane": control}):
                raise RuntimeError("same-ID reclaim control plane changed during transaction")
    return {"schema_version": "same-id-writer-reclaim-result-v1", "wave": wave, "action": action,
            "scheme_ids": sorted(old_configs), "plan_sha256": expected_plan_sha256, "approved_by": operator,
            "prediction_written": False, "historical_facts_changed": False}


def _same_id_live_preservation_plan_conn(
    conn: Connection, *, source_configs: Mapping[str, SchemeConfig], target_configs: Mapping[str, SchemeConfig],
    backup_evidence: Mapping[str, object], control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str, for_update: bool,
) -> dict[str, object]:
    """只读核实固定 W2 四条 alias-only live 来源；不接受调用方提供预测值。"""
    ids = sorted(_SAME_ID_RECLAIM_WAVES["W2"])
    if set(source_configs) != set(ids) or set(target_configs) != set(ids):
        raise ValueError("live preservation requires exactly the two W2 original IDs")
    control = deepcopy(control_plane_evidence)
    scheduler = control.get("scheduler", {}) if isinstance(control, Mapping) else {}
    if (not isinstance(control, Mapping) or control.get("schema_version") != "same-id-live-preservation-control-plane-v1"
            or control.get("wave") != "W2" or control.get("deployment_target") != "aliyun-gray"
            or not isinstance(scheduler, Mapping) or scheduler.get("control_plane") != "systemd_one_shot"
            or scheduler.get("timer_fenced") is not True or scheduler.get("unique_writer") is not True
            or control.get("source_canonical_selection") != {key: _same_id_reclaim_identity(source_configs[key]) for key in ids}
            or control.get("target_canonical_selection") != {key: _same_id_reclaim_identity(target_configs[key]) for key in ids}):
        raise RuntimeError("live preservation requires fenced canonical control evidence")
    if _dialect_name(conn) == "mysql":
        identity = dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
        if identity != {"database_name": _require_nonempty(expected_database_name, "expected_database_name"),
                        "server_uuid": _require_nonempty(expected_server_uuid, "expected_server_uuid")}:
            raise RuntimeError("live preservation database identity mismatch")
    elif _dialect_name(conn) == "sqlite":
        identity = {"isolated_test": "sqlite"}
    else:
        raise RuntimeError("live preservation requires MySQL or isolated SQLite")
    migrations = _same_id_rows_conn(conn, "t_schema_migrations", "1=1", {}, order="version", for_update=for_update)
    if not migrations or max(int(row["version"]) for row in migrations) != 24 or any(row["state"] != "APPLIED" for row in migrations):
        raise RuntimeError("live preservation requires applied schema 024")
    all_ids = sorted(ids + [key + "_bbv2" for key in ids])
    params = {f"id_{i}": key for i, key in enumerate(all_ids)}
    scope = ",".join(":" + key for key in params)
    versions = _same_id_rows_conn(conn, "t_scheme_versions", f"scheme_id IN ({scope})", params,
                                  order="scheme_id,scheme_version", for_update=for_update)
    registry = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id IN ({scope})", params,
                                  order="scheme_id", for_update=for_update)
    for table, order in (("t_scheme_runs", "run_id"), ("t_backtest_runs", "id"), ("t_harness_runs", "harness_run_id")):
        running = _same_id_rows_conn(conn, table, f"scheme_id IN ({scope}) AND status='running'", params,
                                     order=order, for_update=for_update)
        if any(table != "t_harness_runs" or not _is_preserved_w2_historical_compare(conn, row, for_update=for_update) for row in running):
            raise RuntimeError("live preservation found a running execution")
    conversions, inputs = control.get("identity_conversions"), control.get("source_inputs")
    if not isinstance(conversions, Mapping) or set(conversions) != set(ids) or not isinstance(inputs, Mapping):
        raise ValueError("live preservation requires conversion and historical snapshot proofs")
    sources, keys = [], []
    for key in ids:
        source, target = source_configs[key], target_configs[key]
        tenor = "5Y" if key == "daily_5y_2_v28" else "7Y"
        if source.scheme_id != key + "_bbv2" or target.scheme_id != key or source.code_hash != target.code_hash:
            raise ValueError("live preservation source/target code or ID mismatch")
        _validate_blackbox_revision_candidate(target, require_evidence=True)
        for cfg, status in ((source, "retired"), (target, "active")):
            if (cfg.runtime_type != "blackbox_v2" or cfg.horizon != 5 or cfg.task_type != "T+5"
                    or cfg.tenors != [tenor] or cfg.target_rule != TASK_COMBINATIONS["T+5"][1]):
                raise ValueError("live preservation W2 business dimensions differ")
            exact = [row for row in versions if row["scheme_id"] == cfg.scheme_id and row["scheme_version"] == cfg.scheme_version]
            if len(exact) != 1 or exact[0]["status"] != status:
                raise RuntimeError("live preservation requires completed original-ID takeover")
            _assert_migration_version_identity(cfg, exact[0])
            others = [row for row in versions if row["scheme_id"] == cfg.scheme_id and row not in exact]
            if any(row["status"] not in {"retired", "paused"} or row["runtime_type"] not in {"native_adapter", "blackbox_v2"}
                   or (row["runtime_type"] == "blackbox_v2" and row["status"] != "retired") for row in others):
                raise RuntimeError("live preservation has another eligible version")
            rows = [row for row in registry if row["base_scheme_id"] == cfg.scheme_id]
            wanted = {"scheme_id": registry_scheme_id(cfg.scheme_id, 5, tenor), "horizon": 5,
                      "target_tenor": tenor, "task_type": "T+5", "runtime_type": "blackbox_v2",
                      "status": "active" if status == "active" else "archived"}
            if len(rows) != 1 or any(rows[0].get(field) != value for field, value in wanted.items()):
                raise RuntimeError("live preservation Registry identity/status differs")
        conversion = conversions[key]
        wanted_conversion = {
            "schema_version": "native-runtime-identity-conversion-v1", "source_scheme_id": source.scheme_id,
            "scheme_id": key, "source_code_sha256": source.code_hash, "candidate_code_sha256": target.code_hash,
            "source_metadata_sha256": source.manifest_hash, "candidate_metadata_sha256": target.manifest_hash,
            "algorithm_executions": 0,
        }
        if not isinstance(conversion, Mapping) or any(conversion.get(field) != value for field, value in wanted_conversion.items()):
            raise ValueError("live preservation identity-only conversion proof differs")
        for target_date in ("2026-09-16", "2026-09-17"):
            keys.append((key, tenor, 5, target_date))
            predictions = _same_id_rows_conn(conn, "t_scheme_predictions",
                "scheme_id=:id AND target_tenor=:tenor AND horizon=5 AND target_date=:target",
                {"id": source.scheme_id, "tenor": tenor, "target": target_date}, order="id", for_update=for_update)
            if len(predictions) != 1:
                raise RuntimeError("live preservation requires one unique source per fixed target")
            prediction = predictions[0]
            if (prediction.get("run_id") is None or prediction.get("backtest_run_id") is not None
                    or prediction.get("backtest_actual_direction") is not None or prediction.get("scheme_version") != source.scheme_version
                    or prediction.get("predicted_direction") not in {-1, 0, 1}):
                raise RuntimeError("live preservation source is not an exact live fact")
            runs = _same_id_rows_conn(conn, "t_scheme_runs", "run_id=:run", {"run": prediction["run_id"]},
                                      order="run_id", for_update=for_update)
            if len(runs) != 1:
                raise RuntimeError("live preservation source run is missing")
            run = runs[0]
            expected = {"scheme_id": source.scheme_id, "scheme_version": source.scheme_version,
                        "runtime_type": "blackbox_v2", "run_type": "active", "prediction_phase": "scheduled_live",
                        "status": "success", "records_expected": 1, "records_returned": 1, "records_written": 1,
                        "input_artifact_id": None}
            if (any(run.get(field) != value for field, value in expected.items()) or not run.get("finished_at")
                    or _stored_iso_date(run, "predict_date") != _stored_iso_date(prediction, "predict_date")):
                raise RuntimeError("live preservation source run identity/completion differs")
            extra = _json_mapping(prediction.get("extra"))
            snapshot = _require_nonempty(run.get("data_snapshot_id"), "source snapshot")
            request_id = ":".join([source.scheme_id] + [_stored_iso_date(prediction, field)
                                                    for field in ("predict_date", "feature_date", "target_date")])
            if extra.get("request_id") != request_id or extra.get("data_snapshot_id") != snapshot or "migration_import" in extra:
                raise RuntimeError("live preservation source Request/snapshot provenance differs")
            input_proof = inputs.get(snapshot)
            input_fields = {"snapshot_id", "generation_id", "manifest_sha256", "files"}
            filenames = {"daily_output.csv", "weekly_output.csv", "monthly_output.csv", "factor_catalog.csv", "api_wind_date.csv"}
            if (not isinstance(input_proof, Mapping) or set(input_proof) != input_fields
                    or input_proof["snapshot_id"] != snapshot or not isinstance(input_proof["files"], Mapping)
                    or set(input_proof["files"]) != filenames or not input_proof["generation_id"]
                    or (extra.get("data_generation_id") is not None and extra["data_generation_id"] != input_proof["generation_id"])):
                raise ValueError("live preservation historical input proof differs")
            for digest in [input_proof["manifest_sha256"], *input_proof["files"].values()]:
                _require_sha256(digest, "historical input SHA-256")
            sources.append({"prediction": prediction, "run": run})
    existing = (_lock_existing_prediction_business_keys(conn, keys) if for_update else {
        (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]), _stored_iso_date(row, "target_date"))
        for row in _same_id_rows_conn(conn, "t_scheme_predictions", f"scheme_id IN ({scope})", params,
                                      order="id", for_update=False)
    }.intersection(keys))
    if existing:
        raise RuntimeError("live preservation rejects any existing target key, including complete duplicates")
    facts = _same_id_fact_snapshot_conn(conn, all_ids, for_update=for_update)
    source_sha = native_successor_plan_sha256({"sources": sources})
    history_sha = native_successor_plan_sha256({"facts": facts, "versions": versions, "registry": registry})
    backup_fields = {"schema_version", "backup_uri", "backup_sha256", "restoration_verified",
                     "restore_receipt_sha256", "source_rows_sha256", "existing_facts_sha256",
                     "source_database_identity_sha256"}
    if (not isinstance(backup_evidence, Mapping) or set(backup_evidence) != backup_fields
            or backup_evidence["schema_version"] != "same-id-live-preservation-backup-v1"
            or backup_evidence["restoration_verified"] is not True
            or not isinstance(backup_evidence["backup_uri"], str) or not Path(backup_evidence["backup_uri"]).is_absolute()
            or backup_evidence["source_database_identity_sha256"] != native_successor_plan_sha256(identity)
            or backup_evidence["source_rows_sha256"] != source_sha or backup_evidence["existing_facts_sha256"] != history_sha):
        raise ValueError("live preservation backup/restore evidence differs from complete source/history")
    for field in ("backup_sha256", "restore_receipt_sha256", "source_rows_sha256", "existing_facts_sha256", "source_database_identity_sha256"):
        _require_sha256(backup_evidence[field], field)
    return {"schema_version": "same-id-live-preservation-plan-v1", "scheme_ids": ids, "all_scheme_ids": all_ids,
            "target_keys": keys, "sources": sources, "source_rows_sha256": source_sha, "existing_facts_sha256": history_sha,
            "facts": facts, "versions": versions, "registry": registry, "backup_evidence": dict(backup_evidence),
            "control_plane": dict(control), "database_identity_sha256": native_successor_plan_sha256(identity),
            "materialization": {"run_type": "manual", "prediction_phase": None, "algorithm_executions": 0}}


def read_same_id_live_preservation_plan(
    engine: Engine, *, source_configs: Mapping[str, SchemeConfig], target_configs: Mapping[str, SchemeConfig],
    backup_evidence: Mapping[str, object], control_plane_evidence: Mapping[str, object],
    expected_database_name: str, expected_server_uuid: str,
) -> dict[str, object]:
    """固定四条 W2 历史结果物化的只读批准计划。"""
    with engine.connect() as conn:
        if engine.dialect.name == "mysql":
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            return _same_id_live_preservation_plan_conn(conn, source_configs=source_configs, target_configs=target_configs,
                backup_evidence=backup_evidence, control_plane_evidence=control_plane_evidence,
                expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid, for_update=False)
        finally:
            conn.rollback()


def apply_same_id_live_preservation(
    engine: Engine, *, source_configs: Mapping[str, SchemeConfig], target_configs: Mapping[str, SchemeConfig],
    backup_evidence: Mapping[str, object], control_plane_evidence_reader: Callable[[], Mapping[str, object]],
    expected_database_name: str, expected_server_uuid: str, expected_plan_sha256: str,
    approved_by: str, approved_at: datetime,
) -> dict[str, object]:
    """一次事务新建四条 manual 物化 run 和预测；全部旧事实、版本及 Registry 不变。"""
    operator = _require_nonempty(approved_by, "approved_by")
    _require_sha256(expected_plan_sha256, "expected_plan_sha256")
    if not isinstance(approved_at, datetime) or set(source_configs) != _SAME_ID_RECLAIM_WAVES["W2"] or set(target_configs) != set(source_configs):
        raise ValueError("live preservation requires W2 scope and approved_at datetime")
    all_ids = sorted(list(source_configs) + [key + "_bbv2" for key in source_configs])
    kwargs = dict(source_configs=source_configs, target_configs=target_configs, backup_evidence=backup_evidence,
                  expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid)
    mappings, new_runs, new_predictions = [], [], []
    inserted_run_rows, inserted_prediction_rows = [], []
    with ExitStack() as locks:
        if engine.dialect.name != "sqlite":
            for key in all_ids:
                locks.enter_context(_blackbox_activation_advisory_lock(engine, scheme_id=key))
        with engine.begin() as conn:
            control = deepcopy(control_plane_evidence_reader())
            plan = _same_id_live_preservation_plan_conn(conn, **kwargs, control_plane_evidence=control, for_update=True)
            if native_successor_plan_sha256(plan) != expected_plan_sha256:
                raise RuntimeError("live preservation plan changed before commit")
            stamp = _mysql_utc_datetime(approved_at)
            for item in plan["sources"]:
                source, source_run = item["prediction"], item["run"]
                key = str(source["scheme_id"]).removesuffix("_bbv2")
                cfg = target_configs[key]
                result = conn.execute(text("INSERT INTO t_scheme_runs "
                    "(scheme_id,scheme_version,runtime_type,run_type,prediction_phase,predict_date,status,"
                    "data_snapshot_id,input_artifact_id,started_at,finished_at,records_expected,records_returned,records_written) "
                    "VALUES (:id,:version,'blackbox_v2','manual',NULL,:predict,'success',:snapshot,NULL,:stamp,:stamp,1,1,1)"),
                    {"id": key, "version": cfg.scheme_version, "predict": source["predict_date"],
                     "snapshot": source_run["data_snapshot_id"], "stamp": stamp})
                run_id = int(result.lastrowid)
                new_runs.append(run_id)
                manual = _same_id_rows_conn(conn, "t_scheme_runs", "run_id=:run", {"run": run_id},
                                            order="run_id", for_update=True)
                expected_run = {"scheme_id": key, "scheme_version": cfg.scheme_version, "runtime_type": "blackbox_v2",
                                "run_type": "manual", "prediction_phase": None, "status": "success",
                                "data_snapshot_id": source_run["data_snapshot_id"], "input_artifact_id": None,
                                "records_expected": 1, "records_returned": 1, "records_written": 1}
                if len(manual) != 1 or any(manual[0].get(field) != value for field, value in expected_run.items()):
                    raise RuntimeError("live preservation manual run readback differs")
                inserted_run_rows.append(manual[0])
                extra = _json_mapping(source.get("extra"))
                extra["migration_import"] = {
                    "schema_version": "same-id-live-materialization-v1", "operation": "history-materialization",
                    "source_scheme_id": source["scheme_id"], "source_scheme_version": source["scheme_version"],
                    "source_prediction_id": source["id"], "source_run_id": source["run_id"],
                    "source_request_id": extra["request_id"], "source_prediction_sha256": native_successor_plan_sha256(source),
                    "source_run_sha256": native_successor_plan_sha256(source_run),
                    "source_input": control["source_inputs"][source_run["data_snapshot_id"]],
                    "backup_evidence": dict(backup_evidence), "identity_conversion": control["identity_conversions"][key],
                    "plan_sha256": expected_plan_sha256, "approved_by": operator, "materialized_at": approved_at.isoformat(),
                    "algorithm_executions": 0,
                }
                row = {field: source.get(field) for field in ("target_tenor", "horizon", "predict_date", "feature_date",
                        "target_date", "predicted_direction", "confidence", "model_version")}
                row.update(scheme_id=key, scheme_version=cfg.scheme_version, run_id=run_id, backtest_run_id=None,
                           backtest_actual_direction=None, extra=json.dumps(extra, ensure_ascii=False, default=str))
                if _insert_run_predictions_conn(conn, [row]) != 1:
                    raise RuntimeError("live preservation prediction insert count mismatch")
                inserted = _same_id_rows_conn(conn, "t_scheme_predictions", "run_id=:run", {"run": run_id}, order="id", for_update=True)
                if len(inserted) != 1:
                    raise RuntimeError("live preservation inserted prediction readback differs")
                actual = dict(inserted[0])
                for field, value in row.items():
                    found = _json_mapping(actual[field]) if field == "extra" else str(actual[field]) if field.endswith("date") else actual[field]
                    wanted = json.loads(value) if field == "extra" else str(value) if field.endswith("date") else value
                    if found != wanted:
                        raise RuntimeError("live preservation inserted prediction fields differ")
                new_predictions.append(actual["id"])
                inserted_prediction_rows.append(actual)
                mappings.append({"source_prediction_id": source["id"], "source_run_id": source["run_id"],
                                 "prediction_id": actual["id"], "run_id": run_id})
            params = {f"id_{i}": key for i, key in enumerate(all_ids)}
            scope = ",".join(":" + key for key in params)
            post = _same_id_fact_snapshot_conn(conn, all_ids, for_update=True)
            for table, primary, inserted_ids, expected_rows in (
                ("t_scheme_runs", "run_id", new_runs, inserted_run_rows),
                ("t_scheme_predictions", "id", new_predictions, inserted_prediction_rows),
            ):
                rows = _same_id_rows_conn(conn, table, f"scheme_id IN ({scope})", params, order=primary, for_update=True)
                original_rows = [row for row in rows if row[primary] not in inserted_ids]
                if [row for row in rows if row[primary] in inserted_ids] != expected_rows:
                    raise RuntimeError("live preservation new materialization rows changed before commit")
                post[table] = {"count": len(original_rows), "sha256": native_successor_plan_sha256({"rows": original_rows})}
            versions = _same_id_rows_conn(conn, "t_scheme_versions", f"scheme_id IN ({scope})", params,
                                          order="scheme_id,scheme_version", for_update=True)
            registry = _same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id IN ({scope})", params,
                                          order="scheme_id", for_update=True)
            if native_successor_plan_sha256({"facts": post, "versions": versions, "registry": registry}) != plan["existing_facts_sha256"]:
                raise RuntimeError("live preservation changed preexisting facts or lifecycle rows")
            if native_successor_plan_sha256({"control_plane": control_plane_evidence_reader()}) != native_successor_plan_sha256({"control_plane": control}):
                raise RuntimeError("live preservation control plane changed during transaction")
    return {"schema_version": "same-id-live-preservation-result-v1", "plan_sha256": expected_plan_sha256,
            "approved_by": operator, "records_written": 4, "manual_runs_created": 4, "algorithm_executions": 0,
            "imports": mappings, "source_rows_sha256": plan["source_rows_sha256"], "existing_facts_sha256": plan["existing_facts_sha256"]}


def canonical_native_successor_plan(plan: Mapping[str, object]) -> str:
    """把迁移计划编码为稳定 JSON，供人工授权绑定。"""
    normalized = json.loads(json.dumps(plan, ensure_ascii=True, default=str))
    control_plane = normalized.get("control_plane")
    if isinstance(control_plane, dict):
        control_plane.pop("captured_at", None)
        control_plane.pop("_capture_sha256", None)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def native_successor_plan_sha256(plan: Mapping[str, object]) -> str:
    """计算规范迁移计划摘要。"""
    return hashlib.sha256(
        canonical_native_successor_plan(plan).encode("utf-8")
    ).hexdigest()


@contextmanager
def _native_successor_advisory_locks(
    engine: Engine,
    *,
    scheme_ids: Sequence[str],
) -> Iterator[None]:
    """按稳定顺序持有一次跨 base 迁移涉及的全部 MySQL 互斥锁。"""
    normalized = tuple(sorted(set(scheme_ids)))
    if not normalized:
        raise ValueError("Native successor migration requires scheme identities")
    if engine.dialect.name == "sqlite":
        yield
        return
    lock_names = ["bfl:native-successor:global"] + [
        "bfl:native-successor:"
        + hashlib.sha256(scheme_id.encode("utf-8")).hexdigest()[:32]
        for scheme_id in normalized
    ]
    with engine.connect() as lock_conn:
        acquired: list[str] = []
        try:
            for lock_name in lock_names:
                value = lock_conn.execute(
                    text("SELECT GET_LOCK(:lock_name, :timeout_sec)"),
                    {
                        "lock_name": lock_name,
                        "timeout_sec": _BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC,
                    },
                ).scalar_one()
                if int(value or 0) != 1:
                    raise BlackboxActivationLockTimeout(
                        "timed out waiting for Native successor migration lock: "
                        f"lock_name={lock_name}"
                    )
                acquired.append(lock_name)
            yield
        finally:
            active_error = sys.exc_info()[1]
            release_errors: list[str] = []
            for lock_name in reversed(acquired):
                try:
                    released = lock_conn.execute(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": lock_name},
                    ).scalar_one()
                    if int(released or 0) != 1:
                        release_errors.append(lock_name)
                except BaseException:  # noqa: BLE001
                    release_errors.append(lock_name)
            if release_errors:
                message = (
                    "failed to release Native successor migration locks: "
                    + ",".join(release_errors)
                )
                if active_error is None:
                    raise RuntimeError(message)
                if hasattr(active_error, "add_note"):
                    active_error.add_note(message)


def read_native_successor_migration_plan(
    engine: Engine,
    *,
    wave: str,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    backtests: Mapping[str, NativeSuccessorBacktestEvidence],
    equivalence_evidence: Mapping[str, object],
    control_plane_evidence: Mapping[str, object],
    expected_database_name: str | None = None,
    expected_server_uuid: str | None = None,
    expected_action: str | None = None,
) -> dict[str, object]:
    """只读生成当前权威状态下的 Native successor 迁移计划。"""
    if engine.dialect.name == "mysql":
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            conn.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
            try:
                return _native_successor_migration_plan_conn(
                    conn,
                    wave=wave,
                    targets=targets,
                    old_configs=old_configs,
                    new_configs=new_configs,
                    backtests=backtests,
                    equivalence_evidence=equivalence_evidence,
                    control_plane_evidence=control_plane_evidence,
                    expected_database_name=expected_database_name,
                    expected_server_uuid=expected_server_uuid,
                    expected_action=expected_action,
                    for_update=False,
                )
            finally:
                conn.rollback()
    with engine.begin() as conn:
        return _native_successor_migration_plan_conn(
            conn,
            wave=wave,
            targets=targets,
            old_configs=old_configs,
            new_configs=new_configs,
            backtests=backtests,
            equivalence_evidence=equivalence_evidence,
            control_plane_evidence=control_plane_evidence,
            expected_database_name=expected_database_name,
            expected_server_uuid=expected_server_uuid,
            expected_action=expected_action,
            for_update=False,
        )


def apply_native_successor_migration(
    engine: Engine,
    *,
    wave: str,
    action: str,
    expected_plan_sha256: str,
    approved_by: str,
    approved_at: datetime,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    backtests: Mapping[str, NativeSuccessorBacktestEvidence],
    equivalence_evidence: Mapping[str, object],
    control_plane_evidence_reader: Callable[[], Mapping[str, object]],
    expected_database_name: str | None = None,
    expected_server_uuid: str | None = None,
) -> dict[str, object]:
    """按预检摘要在一个事务中执行跨 base 切换或回滚。"""
    if action not in {"cutover", "rollback"}:
        raise ValueError("Native successor migration action must be cutover or rollback")
    if not isinstance(expected_plan_sha256, str) or len(expected_plan_sha256) != 64:
        raise ValueError("expected_plan_sha256 must be a SHA-256 hex digest")
    try:
        int(expected_plan_sha256, 16)
    except ValueError as exc:
        raise ValueError("expected_plan_sha256 must be a SHA-256 hex digest") from exc
    operator = _require_nonempty(approved_by, "approved_by")
    if operator != approved_by:
        raise ValueError("approved_by must not contain surrounding whitespace")
    if not isinstance(approved_at, datetime):
        raise ValueError("approved_at must be datetime")
    all_ids = tuple(old_configs) + tuple(new_configs)
    with _native_successor_advisory_locks(engine, scheme_ids=all_ids):
        with engine.begin() as conn:
            control_plane_evidence = control_plane_evidence_reader()
            if not isinstance(control_plane_evidence, Mapping):
                raise RuntimeError("controlled control-plane recapture is invalid")
            plan = _native_successor_migration_plan_conn(
                conn,
                wave=wave,
                targets=targets,
                old_configs=old_configs,
                new_configs=new_configs,
                backtests=backtests,
                equivalence_evidence=equivalence_evidence,
                control_plane_evidence=control_plane_evidence,
                expected_database_name=expected_database_name,
                expected_server_uuid=expected_server_uuid,
                expected_action=action,
                for_update=True,
            )
            actual_sha256 = native_successor_plan_sha256(plan)
            if actual_sha256 != expected_plan_sha256.lower():
                raise RuntimeError(
                    "Native successor migration plan changed before commit: "
                    f"expected={expected_plan_sha256.lower()} actual={actual_sha256}"
                )
            mysql_approved_at = _mysql_utc_datetime(approved_at)
            if action == "cutover":
                _cutover_native_successors_conn(
                    conn,
                    targets=targets,
                    old_configs=old_configs,
                    new_configs=new_configs,
                    backtests=backtests,
                    approved_by=operator,
                    approved_at=cast(datetime, mysql_approved_at),
                )
            else:
                _rollback_native_successors_conn(
                    conn,
                    targets=targets,
                    old_configs=old_configs,
                    new_configs=new_configs,
                    approved_by=operator,
                    approved_at=cast(datetime, mysql_approved_at),
                )
            _assert_native_successor_post_state_conn(
                conn,
                targets=targets,
                old_configs=old_configs,
                new_configs=new_configs,
                action=action,
            )
            if _read_native_migration_fact_summaries_conn(
                conn,
                scheme_ids=sorted(old_configs),
                for_update=True,
            ) != plan["old_fact_summaries"]:
                raise RuntimeError("Native historical facts changed during migration")
            if _read_unaffected_active_registry_summary_conn(
                conn,
                excluded_base_ids=all_ids,
                for_update=True,
            ) != plan["unaffected_active_registry"]:
                raise RuntimeError(
                    "unaffected active Registry set changed during migration"
                )
    return {
        "schema_version": "native-successor-migration-result-v1",
        "wave": wave,
        "action": action,
        "approved_by": operator,
        "plan_sha256": expected_plan_sha256.lower(),
        "old_base_scheme_ids": sorted(old_configs),
        "new_base_scheme_ids": sorted(new_configs),
    }


def _native_successor_migration_plan_conn(
    conn: Connection,
    *,
    wave: str,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    backtests: Mapping[str, NativeSuccessorBacktestEvidence],
    equivalence_evidence: Mapping[str, object],
    control_plane_evidence: Mapping[str, object],
    expected_database_name: str | None,
    expected_server_uuid: str | None,
    expected_action: str | None,
    for_update: bool,
) -> dict[str, object]:
    wave = _require_nonempty(wave, "wave")
    if not isinstance(control_plane_evidence, Mapping) or not control_plane_evidence:
        raise ValueError(
            "verified control-plane evidence is required for migration planning"
        )
    ordered_targets = tuple(
        sorted(targets, key=lambda item: (
            item.old_base_scheme_id,
            item.task_type,
            item.target_tenor,
            item.new_base_scheme_id,
        ))
    )
    if not ordered_targets or len(ordered_targets) != len(set(ordered_targets)):
        raise ValueError("migration targets must be non-empty and unique")
    old_ids = sorted({item.old_base_scheme_id for item in ordered_targets})
    new_ids = sorted({item.new_base_scheme_id for item in ordered_targets})
    if set(old_ids) != set(old_configs) or set(new_ids) != set(new_configs):
        raise ValueError("migration config identities do not match target mapping")
    if set(new_ids) != set(backtests) or set(old_ids).intersection(new_ids):
        raise ValueError("migration backtest identities are incomplete or overlap Native")
    control_databridge = control_plane_evidence.get("databridge")
    if not isinstance(control_databridge, Mapping):
        raise ValueError("migration control-plane DataBridge evidence is incomplete")
    control_generation_id = str(
        control_databridge.get("generation_id") or ""
    ).strip()
    control_data_snapshot_id = str(
        control_databridge.get("data_snapshot_id") or ""
    ).strip()
    if not control_generation_id or not control_data_snapshot_id:
        raise ValueError("migration control-plane DataBridge identity is incomplete")
    for scheme_id, cfg in old_configs.items():
        if cfg.scheme_id != scheme_id or cfg.runtime_type != "native_adapter":
            raise ValueError(f"invalid Native config identity: {scheme_id}")
    for scheme_id, cfg in new_configs.items():
        if cfg.scheme_id != scheme_id or cfg.runtime_type != "blackbox_v2":
            raise ValueError(f"invalid Blackbox successor config identity: {scheme_id}")
    _validate_native_successor_target_configs(
        ordered_targets,
        old_configs=old_configs,
        new_configs=new_configs,
    )
    all_ids = tuple(old_ids + new_ids)
    placeholders = ", ".join(f":scheme_id_{index}" for index, _ in enumerate(all_ids))
    params = {f"scheme_id_{index}": value for index, value in enumerate(all_ids)}
    lock_clause = " FOR UPDATE" if for_update and _dialect_name(conn) != "sqlite" else ""
    if _dialect_name(conn) == "mysql":
        expected_database = _require_nonempty(
            expected_database_name,
            "expected_database_name",
        )
        expected_uuid = _require_nonempty(
            expected_server_uuid,
            "expected_server_uuid",
        )
        raw_database_identity = dict(
            conn.execute(
                text(
                    "SELECT DATABASE() AS database_name, "
                    "@@server_uuid AS server_uuid"
                )
            ).mappings().one()
        )
        if not raw_database_identity.get(
            "database_name"
        ) or not raw_database_identity.get(
            "server_uuid"
        ):
            raise RuntimeError("migration database identity is incomplete")
        if (
            raw_database_identity["database_name"] != expected_database
            or raw_database_identity["server_uuid"] != expected_uuid
        ):
            raise RuntimeError("migration database identity mismatch")
        database_identity = {
            "database_identity_sha256": hashlib.sha256(
                canonical_native_successor_plan(
                    {
                        "database_name": raw_database_identity["database_name"],
                        "server_uuid": raw_database_identity["server_uuid"],
                    }
                ).encode("utf-8")
            ).hexdigest()
        }
    else:
        database_identity = {
            "database_identity_sha256": hashlib.sha256(
                b"isolated-test:sqlite"
            ).hexdigest(),
        }
    migration_rows = conn.execute(
        text(
            "SELECT version, state FROM t_schema_migrations "
            f"ORDER BY version{lock_clause}"
        )
    ).mappings().all()
    applying_versions = [
        int(row["version"])
        for row in migration_rows
        if row.get("state") != "APPLIED"
    ]
    current_migration_version = (
        max(int(row["version"]) for row in migration_rows)
        if migration_rows
        else None
    )
    if applying_versions or (
        current_migration_version != _NATIVE_SUCCESSOR_REQUIRED_SCHEMA_VERSION
    ):
        raise RuntimeError(
            "Native successor migration requires fully applied schema migration 024: "
            f"current={current_migration_version} applying={applying_versions}"
        )
    database_identity["schema_migration_version"] = current_migration_version
    version_rows = conn.execute(
        text(
            "SELECT scheme_id, scheme_version, runtime_type, status, code_hash, "
            "config_hash, manifest_hash, environment_fingerprint, data_snapshot_id "
            f"FROM t_scheme_versions WHERE scheme_id IN ({placeholders}) "
            f"ORDER BY scheme_id, scheme_version{lock_clause}"
        ),
        params,
    ).mappings().all()
    registry_rows = conn.execute(
        text(
            "SELECT scheme_id, base_scheme_id, name, owner, description, horizon, "
            "task_type, runtime_type, tenors, frequency, target_tenor, "
            "schedule_cron, schedule_timezone, status, deployed_at "
            f"FROM t_scheme_registry WHERE base_scheme_id IN ({placeholders}) "
            f"ORDER BY base_scheme_id, scheme_id{lock_clause}"
        ),
        params,
    ).mappings().all()
    running = conn.execute(
        text(
            "SELECT scheme_id, run_id FROM t_scheme_runs "
            f"WHERE scheme_id IN ({placeholders}) AND status = 'running' "
            f"ORDER BY scheme_id, run_id{lock_clause}"
        ),
        params,
    ).mappings().all()
    if running:
        raise RuntimeError("Native successor migration requires zero running scheme runs")

    old_fact_summaries = _read_native_migration_fact_summaries_conn(
        conn,
        scheme_ids=old_ids,
        for_update=for_update,
    )
    unaffected_active_registry = _read_unaffected_active_registry_summary_conn(
        conn,
        excluded_base_ids=all_ids,
        for_update=for_update,
    )

    exact_versions: dict[str, Mapping[str, object] | None] = {}
    for scheme_id, cfg in {**old_configs, **new_configs}.items():
        matches = [
            row for row in version_rows
            if row.get("scheme_id") == scheme_id
            and row.get("scheme_version") == cfg.scheme_version
        ]
        if len(matches) > 1:
            raise RuntimeError(f"duplicate exact version rows: {scheme_id}")
        exact_versions[scheme_id] = matches[0] if matches else None
    for scheme_id, cfg in old_configs.items():
        row = exact_versions[scheme_id]
        if row is None:
            raise RuntimeError(f"Native exact version row is missing: {scheme_id}")
        _assert_migration_version_identity(cfg, row)
    for scheme_id, cfg in new_configs.items():
        row = exact_versions[scheme_id]
        if row is not None:
            _assert_migration_version_identity(cfg, row)
    # Native V1 历史入库并未维护“同 base 唯一 active version”不变量；现场可能
    # 保留多个旧 active 行。迁移只切换仓库当前 config 对应的 exact version，
    # 因而不能改写或用这些历史行阻断 predecessor。successor 则必须继续满足
    # 唯一 active version，避免新运行时出现第二个可执行身份。
    for scheme_id in new_ids:
        other_active = [
            row for row in version_rows
            if row.get("scheme_id") == scheme_id
            and row.get("scheme_version")
            != ({**old_configs, **new_configs}[scheme_id]).scheme_version
            and row.get("status") == "active"
        ]
        if other_active:
            raise RuntimeError(
                f"unexpected active exact version exists for migration base: {scheme_id}"
            )
    old_registry = [row for row in registry_rows if row.get("base_scheme_id") in old_ids]
    new_registry = [row for row in registry_rows if row.get("base_scheme_id") in new_ids]
    inferred_action = _infer_native_successor_action(
        ordered_targets,
        old_configs=old_configs,
        new_configs=new_configs,
        exact_versions=exact_versions,
        old_registry=old_registry,
        new_registry=new_registry,
    )
    if expected_action is not None and inferred_action != expected_action:
        raise RuntimeError(
            "Native successor migration state does not permit requested action: "
            f"requested={expected_action} current={inferred_action}"
        )
    first_cutover = inferred_action == "cutover" and all(
        exact_versions[scheme_id] is None for scheme_id in new_ids
    )
    if first_cutover:
        mismatched_backtests = sorted(
            scheme_id
            for scheme_id, evidence in backtests.items()
            if evidence.generation_id != control_generation_id
            or evidence.data_snapshot_id != control_data_snapshot_id
        )
        if mismatched_backtests:
            raise RuntimeError(
                "successor backtest DataBridge identity differs from current "
                "control plane: " + ",".join(mismatched_backtests)
            )

    backtest_plans: list[dict[str, object]] = []
    successor_fact_rows: dict[str, list[dict[str, object]]] = {}
    for scheme_id in new_ids:
        cfg = new_configs[scheme_id]
        evidence = backtests[scheme_id]
        rows = _prepare_blackbox_backtest_fact_rows_conn(
            conn,
            cfg,
            backtest_run_id=evidence.backtest_run_id,
            backtest_benchmark_id=evidence.benchmark_id,
            backtest_data_snapshot_id=evidence.data_snapshot_id,
            backtest_generation_id=evidence.generation_id,
            backtest_runtime_profile=evidence.runtime_profile,
            backtest_environment_fingerprint=evidence.environment_fingerprint,
            backtest_code_hash=evidence.code_hash,
            backtest_config_hash=evidence.config_hash,
            backtest_manifest_hash=evidence.manifest_hash,
            backtest_validator_policy_digest=evidence.validator_policy_digest,
            for_update=for_update,
            require_source_request=True,
        )
        _assert_native_successor_backtest_boundary(rows)
        successor_fact_rows[scheme_id] = rows
        backtest_plans.append(
            {
                "scheme_id": scheme_id,
                "scheme_version": cfg.scheme_version,
                "backtest_run_id": evidence.backtest_run_id,
                "benchmark_id": evidence.benchmark_id,
                "generation_id": evidence.generation_id,
                "data_snapshot_id": evidence.data_snapshot_id,
                "runtime_profile": evidence.runtime_profile,
                "environment_fingerprint": evidence.environment_fingerprint,
                "code_hash": evidence.code_hash,
                "config_hash": evidence.config_hash,
                "manifest_hash": evidence.manifest_hash,
                "validator_policy_digest": evidence.validator_policy_digest,
                "fact_count": len(rows),
                "fact_sha256": _native_successor_fact_rows_sha256(rows),
                "request_artifact_sha256": hashlib.sha256(
                    canonical_native_successor_plan(
                        {"requests": [row["_source_request"] for row in rows]}
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    normalized_equivalence = _validate_native_successor_equivalence_evidence(
        equivalence_evidence,
        wave=wave,
        targets=ordered_targets,
        old_configs=old_configs,
        new_configs=new_configs,
        backtests=backtests,
        successor_fact_rows=successor_fact_rows,
        control_plane_evidence=control_plane_evidence,
        require_current_databridge=first_cutover,
    )
    grid_coverage = _validate_native_successor_historical_grid_coverage_conn(
        conn,
        targets=ordered_targets,
        successor_fact_rows=successor_fact_rows,
        for_update=for_update,
    )
    return {
        "schema_version": "native-successor-migration-plan-v1",
        "wave": wave,
        "action": inferred_action,
        "targets": [asdict(item) for item in ordered_targets],
        "old_versions": [
            _migration_version_plan(old_configs[scheme_id], exact_versions[scheme_id])
            for scheme_id in old_ids
        ],
        "new_versions": [
            _migration_version_plan(new_configs[scheme_id], exact_versions[scheme_id])
            for scheme_id in new_ids
        ],
        "old_registry": [_migration_registry_plan(row) for row in old_registry],
        "new_registry": [_migration_registry_plan(row) for row in new_registry],
        "old_fact_summaries": old_fact_summaries,
        "unaffected_active_registry": unaffected_active_registry,
        "backtests": backtest_plans,
        "equivalence": normalized_equivalence,
        "grid_coverage": grid_coverage,
        "control_plane": dict(control_plane_evidence),
        "database": database_identity,
        "running_runs": [],
    }


def _validate_native_successor_equivalence_evidence(
    evidence: Mapping[str, object],
    *,
    wave: str,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    backtests: Mapping[str, NativeSuccessorBacktestEvidence],
    successor_fact_rows: Mapping[str, Sequence[Mapping[str, object]]],
    control_plane_evidence: Mapping[str, object],
    require_current_databridge: bool,
) -> dict[str, object]:
    """算法等价绑定自身冻结输入；正式回测独立绑定入库输入。"""
    top_fields = {
        "schema_version",
        "wave",
        "producer",
        "runtime_environment_fingerprint",
        "targets",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != top_fields:
        raise RuntimeError("Native successor equivalence evidence fields are invalid")
    if (
        evidence.get("schema_version")
        != "native-successor-equivalence-v2"
        or evidence.get("wave") != wave
    ):
        raise RuntimeError("Native successor equivalence evidence identity mismatch")
    producer = evidence.get("producer")
    release = control_plane_evidence.get("release")
    if not isinstance(producer, Mapping) or set(producer) != {
        "tool",
        "tool_version",
        "comparator_source_sha256",
        "generated_at",
    }:
        raise RuntimeError("equivalence evidence producer is invalid")
    if not isinstance(release, Mapping) or (
        producer.get("tool") != "native-successor-controlled-comparator"
        or producer.get("tool_version") != "2"
        or producer.get("comparator_source_sha256")
        != release.get("comparator_source_sha256")
        or not str(producer.get("generated_at") or "").strip()
    ):
        raise RuntimeError("equivalence evidence producer identity mismatch")
    runtime_fingerprint = _require_sha256(
        str(evidence.get("runtime_environment_fingerprint") or ""),
        "equivalence runtime_environment_fingerprint",
    )
    required_files = {
        "daily_output.csv",
        "weekly_output.csv",
        "monthly_output.csv",
        "api_wind_date.csv",
        "factor_catalog.csv",
    }
    raw_targets = evidence.get("targets")
    if not isinstance(raw_targets, list):
        raise RuntimeError("equivalence evidence targets must be a list")
    target_fields = {
        "old_base_scheme_id",
        "new_base_scheme_id",
        "task_type",
        "target_tenor",
        "old_horizon",
        "new_horizon",
        "old_code_hash",
        "new_code_hash",
        "input_identity",
        "native_runtime_profile",
        "native_runtime_environment_fingerprint",
        "request_artifact_sha256",
        "request_sha256",
        "request_count",
        "native_result_sha256",
        "successor_result_sha256",
        "request_id_mismatch_count",
        "predict_date_mismatch_count",
        "feature_date_mismatch_count",
        "target_date_mismatch_count",
        "direction_mismatch_count",
    }
    by_identity: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for item in raw_targets:
        if not isinstance(item, Mapping) or set(item) != target_fields:
            raise RuntimeError("equivalence target fields are invalid")
        identity = (
            str(item.get("old_base_scheme_id") or ""),
            str(item.get("new_base_scheme_id") or ""),
            str(item.get("target_tenor") or ""),
        )
        if identity in by_identity:
            raise RuntimeError("equivalence evidence contains duplicate targets")
        by_identity[identity] = item
    expected_identities = {
        (
            target.old_base_scheme_id,
            target.new_base_scheme_id,
            target.target_tenor,
        )
        for target in targets
    }
    if set(by_identity) != expected_identities:
        raise RuntimeError("equivalence evidence target coverage mismatch")
    if any(
        item.get("native_runtime_profile") == "blackbox-v2-v1"
        for item in raw_targets
    ):
        reviewed_family = {
            NativeSuccessorTarget(
                old_base_scheme_id=base_id,
                new_base_scheme_id=f"{base_id}_bbv2",
                task_type="T+5",
                target_tenor="5Y",
                target_rule=None,
                old_horizon=5,
                new_horizon=5,
            )
            for base_id in (
                "liwei_0616_cons_sda_k3_div_k10",
                "liwei_0616_5y01_full_oos_k3_div_k10",
            )
        }
        if wave != "W3A" or len(targets) != 2 or set(targets) != reviewed_family:
            raise RuntimeError(
                "equivalence Blackbox Native reference requires the complete locked W3A family"
            )

    normalized_targets: list[dict[str, object]] = []
    native_runtime = control_plane_evidence.get("native_runtime")
    if not isinstance(native_runtime, Mapping):
        raise RuntimeError("control plane Native runtime evidence is missing")
    current_native_runtime_fingerprint = _require_sha256(
        str(native_runtime.get("environment_fingerprint") or ""),
        "control plane Native runtime environment_fingerprint",
    )
    mismatch_fields = (
        "request_id_mismatch_count",
        "predict_date_mismatch_count",
        "feature_date_mismatch_count",
        "target_date_mismatch_count",
        "direction_mismatch_count",
    )
    for target in targets:
        identity = (
            target.old_base_scheme_id,
            target.new_base_scheme_id,
            target.target_tenor,
        )
        item = by_identity[identity]
        input_identity = item.get("input_identity")
        if not isinstance(input_identity, Mapping) or set(input_identity) != {
            "generation_id", "data_snapshot_id", "data_files_sha256",
        }:
            raise RuntimeError("equivalence target input_identity fields are invalid")
        generation_id = _require_nonempty(
            str(input_identity.get("generation_id") or ""),
            "equivalence generation_id",
        )
        data_snapshot_id = _require_nonempty(
            str(input_identity.get("data_snapshot_id") or ""),
            "equivalence data_snapshot_id",
        )
        file_hashes = input_identity.get("data_files_sha256")
        if not isinstance(file_hashes, Mapping) or set(file_hashes) != required_files:
            raise RuntimeError("equivalence evidence must bind all five DataBridge files")
        normalized_files = {
            filename: _require_sha256(
                str(file_hashes.get(filename) or ""),
                f"equivalence {filename}",
            )
            for filename in sorted(required_files)
        }
        backtest = backtests[target.new_base_scheme_id]
        same_input = (
            backtest.generation_id == generation_id
            and backtest.data_snapshot_id == data_snapshot_id
        )
        expected_identity = {
            "old_base_scheme_id": target.old_base_scheme_id,
            "new_base_scheme_id": target.new_base_scheme_id,
            "task_type": target.task_type,
            "target_tenor": target.target_tenor,
            "old_horizon": target.old_horizon,
            "new_horizon": target.new_horizon,
            "old_code_hash": old_configs[target.old_base_scheme_id].code_hash,
            "new_code_hash": new_configs[target.new_base_scheme_id].code_hash,
        }
        if any(item.get(key) != value for key, value in expected_identity.items()):
            raise RuntimeError("equivalence target identity or code hash mismatch")
        request_count = item.get("request_count")
        if (
            isinstance(request_count, bool)
            or not isinstance(request_count, int)
            or request_count <= 0
        ):
            raise RuntimeError("equivalence request_count must be positive")
        rows = sorted(
            (
                row
                for row in successor_fact_rows[target.new_base_scheme_id]
                if row.get("target_tenor") == target.target_tenor
                and int(row.get("horizon") or 0) == target.new_horizon
            ),
            key=lambda row: (
                str(row.get("target_date")),
                str(row.get("predict_date")),
                str(row.get("feature_date")),
            ),
        )
        request_projection = [
            {
                "request_id": (
                    f"{target.new_base_scheme_id}:{row.get('predict_date')}:"
                    f"{row.get('feature_date')}:{row.get('target_date')}"
                ),
                "predict_date": str(row.get("predict_date")),
                "feature_date": str(row.get("feature_date")),
                "target_date": str(row.get("target_date")),
            }
            for row in rows
        ]
        full_request_projection: list[dict[str, object]] = []
        for request, row in zip(request_projection, rows, strict=True):
            source_request = row.get("_source_request")
            if not isinstance(source_request, Mapping) or set(
                source_request
            ) != set(REQUEST_FIELDS):
                raise RuntimeError(
                    "persisted backtest source_row lacks the complete Request"
                )
            normalized_source_request = {
                field: str(source_request.get(field)) for field in REQUEST_FIELDS
            }
            if any(
                normalized_source_request[field] != request[field]
                for field in _NATIVE_SUCCESSOR_REQUEST_IDENTITY_FIELDS
            ):
                raise RuntimeError(
                    "persisted backtest source_row differs from fact identity"
                )
            full_request_projection.append(normalized_source_request)
        result_projection = [
            {
                **request,
                "predicted_direction": int(row.get("predicted_direction")),
            }
            for request, row in zip(request_projection, rows, strict=True)
        ]
        expected_request_sha256 = hashlib.sha256(
            canonical_native_successor_plan(
                {"requests": request_projection}
            ).encode("utf-8")
        ).hexdigest()
        expected_request_artifact_sha256 = hashlib.sha256(
            canonical_native_successor_plan(
                {"requests": full_request_projection}
            ).encode("utf-8")
        ).hexdigest()
        expected_result_sha256 = hashlib.sha256(
            canonical_native_successor_plan(
                {"results": result_projection}
            ).encode("utf-8")
        ).hexdigest()
        if request_count != len(rows):
            raise RuntimeError(
                "equivalence request_count differs from persisted backtest facts"
            )
        if any(
            type(item.get(field)) is not int or item.get(field) != 0
            for field in mismatch_fields
        ):
            raise RuntimeError("Native/successor equivalence mismatch count is non-zero")
        request_sha256 = _require_sha256(
            str(item.get("request_sha256") or ""),
            "equivalence request_sha256",
        )
        request_artifact_sha256 = _require_sha256(
            str(item.get("request_artifact_sha256") or ""),
            "equivalence request_artifact_sha256",
        )
        native_result_sha256 = _require_sha256(
            str(item.get("native_result_sha256") or ""),
            "equivalence native_result_sha256",
        )
        successor_result_sha256 = _require_sha256(
            str(item.get("successor_result_sha256") or ""),
            "equivalence successor_result_sha256",
        )
        if request_sha256 != expected_request_sha256:
            raise RuntimeError(
                "equivalence Request digest differs from persisted backtest facts"
            )
        # 不同输入可能修订 cutoff 或方向；完整日期覆盖仍必须一致。
        # 输入相同时保留逐字段交叉核验，不能用分离证据掩盖同输入差异。
        if same_input and request_artifact_sha256 != expected_request_artifact_sha256:
            raise RuntimeError(
                "equivalence complete Request digest differs from persisted "
                "backtest source_row"
            )
        if (
            native_result_sha256 != successor_result_sha256
            or (same_input and successor_result_sha256 != expected_result_sha256)
        ):
            raise RuntimeError(
                "Native/successor standardized result digests differ from "
                "persisted backtest facts"
            )
        native_runtime_fingerprint = _require_sha256(
            str(item.get("native_runtime_environment_fingerprint") or ""),
            "equivalence native_runtime_environment_fingerprint",
        )
        native_runtime_profile = item.get("native_runtime_profile")
        if native_runtime_profile not in ("native", "blackbox-v2-v1"):
            raise RuntimeError("equivalence Native runtime profile is invalid")
        if native_runtime_profile == "native" and (
            native_runtime_fingerprint != current_native_runtime_fingerprint
        ):
            raise RuntimeError(
                "equivalence Native runtime differs from current control plane"
            )
        if native_runtime_profile == "blackbox-v2-v1" and (
            native_runtime_fingerprint != runtime_fingerprint
            or new_configs[target.new_base_scheme_id].runtime_profile
            != native_runtime_profile
        ):
            raise RuntimeError(
                "equivalence Native runtime differs from successor runtime profile"
            )
        if require_current_databridge and same_input:
            current = control_plane_evidence.get("databridge")
            if not isinstance(current, Mapping) or (
                current.get("generation_id") != generation_id
                or current.get("data_snapshot_id") != data_snapshot_id
                or current.get("files") != normalized_files
            ):
                raise RuntimeError(
                    "first cutover equivalence evidence differs from current DataBridge"
                )
        normalized_targets.append(
            {
                **expected_identity,
                "input_identity": {
                    "generation_id": generation_id,
                    "data_snapshot_id": data_snapshot_id,
                    "data_files_sha256": normalized_files,
                },
                "native_runtime_profile": native_runtime_profile,
                "native_runtime_environment_fingerprint": (
                    native_runtime_fingerprint
                ),
                "request_sha256": request_sha256,
                "request_artifact_sha256": request_artifact_sha256,
                "request_count": request_count,
                "native_result_sha256": native_result_sha256,
                "successor_result_sha256": successor_result_sha256,
                **{field: 0 for field in mismatch_fields},
            }
        )

    if any(
        backtest.environment_fingerprint != runtime_fingerprint
        for backtest in backtests.values()
    ):
        raise RuntimeError("equivalence evidence differs from persisted backtest runtime")
    if any(
        config.environment_fingerprint != runtime_fingerprint
        for config in new_configs.values()
    ):
        raise RuntimeError("equivalence evidence differs from successor runtime")
    return {
        "schema_version": "native-successor-equivalence-v2",
        "wave": wave,
        "producer": dict(producer),
        "runtime_environment_fingerprint": runtime_fingerprint,
        "targets": sorted(
            normalized_targets,
            key=lambda item: (
                str(item["old_base_scheme_id"]),
                str(item["new_base_scheme_id"]),
                str(item["target_tenor"]),
            ),
        ),
    }


def _validate_native_successor_target_configs(
    targets: Sequence[NativeSuccessorTarget],
    *,
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
) -> None:
    for target in targets:
        old = old_configs[target.old_base_scheme_id]
        new = new_configs[target.new_base_scheme_id]
        try:
            canonical_horizon, canonical_target_rule, canonical_frequency = (
                TASK_COMBINATIONS[target.task_type]
            )
        except KeyError as exc:
            raise ValueError(
                f"unsupported migration task_type: {target.task_type}"
            ) from exc
        expected_old = (
            old.task_type,
            target.target_tenor,
            old.target_rule,
            int(old.horizon),
            old.frequency,
        )
        declared_old = (
            target.task_type,
            target.target_tenor,
            target.target_rule,
            target.old_horizon,
            canonical_frequency,
        )
        expected_new = (
            new.task_type,
            target.target_tenor,
            new.target_rule,
            int(new.horizon),
            new.frequency,
        )
        declared_new = (
            target.task_type,
            target.target_tenor,
            canonical_target_rule,
            target.new_horizon,
            canonical_frequency,
        )
        if declared_old != expected_old or target.target_tenor not in old.tenors:
            raise ValueError(f"Native target mapping does not match config: {target}")
        if (
            declared_new != expected_new
            or target.new_horizon != canonical_horizon
            or tuple(new.tenors) != (target.target_tenor,)
        ):
            raise ValueError(f"successor target mapping does not match config: {target}")
    for old_id, cfg in old_configs.items():
        mapped = {
            target.target_tenor for target in targets
            if target.old_base_scheme_id == old_id
        }
        if mapped != set(cfg.tenors):
            raise ValueError(f"wave does not cover every Native target: {old_id}")


def _infer_native_successor_action(
    targets: Sequence[NativeSuccessorTarget],
    *,
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    exact_versions: Mapping[str, Mapping[str, object] | None],
    old_registry: Sequence[Mapping[str, object]],
    new_registry: Sequence[Mapping[str, object]],
) -> str:
    expected_old = {
        registry_scheme_id(item.old_base_scheme_id, item.old_horizon, item.target_tenor)
        for item in targets
    }
    expected_new = {
        registry_scheme_id(item.new_base_scheme_id, item.new_horizon, item.target_tenor)
        for item in targets
    }
    old_by_id = {str(row.get("scheme_id")): row for row in old_registry}
    new_by_id = {str(row.get("scheme_id")): row for row in new_registry}
    if set(old_by_id) != expected_old or set(new_by_id).difference(expected_new):
        raise RuntimeError("Registry target coverage differs from migration mapping")
    for target in targets:
        old_id = registry_scheme_id(
            target.old_base_scheme_id,
            target.old_horizon,
            target.target_tenor,
        )
        old_row = old_by_id[old_id]
        old_cfg = old_configs[target.old_base_scheme_id]
        if (
            old_row.get("base_scheme_id") != target.old_base_scheme_id
            or old_row.get("runtime_type") != "native_adapter"
            or old_row.get("task_type") != target.task_type
            or old_row.get("target_tenor") != target.target_tenor
            or int(old_row.get("horizon") or 0) != target.old_horizon
            or _normalize_registry_tenors(old_row.get("tenors"))
            != [target.target_tenor]
            or old_row.get("frequency") != old_cfg.frequency
            or old_row.get("schedule_cron") != old_cfg.schedule.cron
            or old_row.get("schedule_timezone") != old_cfg.schedule.timezone
        ):
            raise RuntimeError(f"Native Registry identity mismatch: {old_id}")
        new_id = registry_scheme_id(
            target.new_base_scheme_id,
            target.new_horizon,
            target.target_tenor,
        )
        new_row = new_by_id.get(new_id)
        if new_row is not None and (
            new_row.get("base_scheme_id") != target.new_base_scheme_id
            or new_row.get("runtime_type") != "blackbox_v2"
            or new_row.get("task_type") != target.task_type
            or new_row.get("target_tenor") != target.target_tenor
            or int(new_row.get("horizon") or 0) != target.new_horizon
            or _normalize_registry_tenors(new_row.get("tenors"))
            != [target.target_tenor]
            or new_row.get("frequency") != old_row.get("frequency")
            or new_row.get("schedule_cron") != old_row.get("schedule_cron")
            or new_row.get("schedule_timezone")
            != old_row.get("schedule_timezone")
        ):
            raise RuntimeError(f"successor Registry identity mismatch: {new_id}")
    old_active = all(
        exact_versions[scheme_id] is not None
        and exact_versions[scheme_id].get("runtime_type") == "native_adapter"
        and exact_versions[scheme_id].get("status") == "active"
        for scheme_id in old_configs
    ) and all(row.get("status") == "active" for row in old_by_id.values())
    old_retired = all(
        exact_versions[scheme_id] is not None
        and exact_versions[scheme_id].get("runtime_type") == "native_adapter"
        and exact_versions[scheme_id].get("status") == "retired"
        for scheme_id in old_configs
    ) and all(row.get("status") == "archived" for row in old_by_id.values())
    new_versions_absent = all(
        exact_versions[scheme_id] is None for scheme_id in new_configs
    )
    new_versions_retired = all(
        exact_versions[scheme_id] is not None
        and (
            exact_versions[scheme_id].get("runtime_type") == "blackbox_v2"
            and exact_versions[scheme_id].get("status") == "retired"
        )
        for scheme_id in new_configs
    )
    new_registry_absent = not new_by_id
    new_registry_archived = set(new_by_id) == expected_new and all(
        row.get("status") == "archived" for row in new_by_id.values()
    )
    new_cutover_ready = (
        new_versions_absent and new_registry_absent
    ) or (
        new_versions_retired and new_registry_archived
    )
    new_active = set(new_by_id) == expected_new and all(
        exact_versions[scheme_id] is not None
        and exact_versions[scheme_id].get("runtime_type") == "blackbox_v2"
        and exact_versions[scheme_id].get("status") == "active"
        for scheme_id in new_configs
    ) and all(row.get("status") == "active" for row in new_by_id.values())
    if old_active and new_cutover_ready:
        return "cutover"
    if old_retired and new_active:
        return "rollback"
    raise RuntimeError("Native/successor lifecycle state is mixed or unsupported")


def _assert_migration_version_identity(
    cfg: SchemeConfig,
    row: Mapping[str, object],
) -> None:
    expected = {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "runtime_type": cfg.runtime_type,
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
    }
    mismatches = [
        f"{field}: expected={value!r}, actual={row.get(field)!r}"
        for field, value in expected.items()
        if row.get(field) != value
    ]
    if mismatches:
        raise RuntimeError(
            "migration exact version identity mismatch for "
            f"{cfg.scheme_id}: " + "; ".join(mismatches)
        )


def _migration_version_plan(
    cfg: SchemeConfig,
    row: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "runtime_type": cfg.runtime_type,
        "status": str(row.get("status")) if row is not None else "absent",
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
        "database_code_hash": str(row.get("code_hash")) if row is not None else None,
        "database_config_hash": str(row.get("config_hash")) if row is not None else None,
        "database_manifest_hash": str(row.get("manifest_hash")) if row is not None else None,
    }


def _migration_registry_plan(row: Mapping[str, object]) -> dict[str, object]:
    return {
        key: row.get(key)
        for key in (
            "scheme_id",
            "base_scheme_id",
            "name",
            "owner",
            "description",
            "horizon",
            "task_type",
            "runtime_type",
            "tenors",
            "frequency",
            "target_tenor",
            "schedule_cron",
            "schedule_timezone",
            "status",
            "deployed_at",
        )
    }


def _read_native_migration_fact_summaries_conn(
    conn: Connection,
    *,
    scheme_ids: Sequence[str],
    for_update: bool,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for scheme_id in sorted(scheme_ids):
        if for_update and _dialect_name(conn) != "sqlite":
            for table, identity_column in (
                ("t_scheme_predictions", "id"),
                ("t_scheme_runs", "run_id"),
                ("t_backtest_runs", "id"),
                ("t_backtest_predictions", "id"),
            ):
                conn.execute(
                    text(
                        f"SELECT {identity_column} FROM {table} "
                        "WHERE scheme_id = :scheme_id "
                        f"ORDER BY {identity_column} FOR UPDATE"
                    ),
                    {"scheme_id": scheme_id},
                ).all()
        prediction = conn.execute(
            text(
                "SELECT COUNT(*) AS row_count, MIN(target_date) AS min_target_date, "
                "MAX(target_date) AS max_target_date, "
                "SUM(CASE WHEN predicted_direction = -1 THEN 1 ELSE 0 END) AS down_count, "
                "SUM(CASE WHEN predicted_direction = 0 THEN 1 ELSE 0 END) AS flat_count, "
                "SUM(CASE WHEN predicted_direction = 1 THEN 1 ELSE 0 END) AS up_count "
                "FROM t_scheme_predictions WHERE scheme_id = :scheme_id"
            ),
            {"scheme_id": scheme_id},
        ).mappings().one()
        runs = conn.execute(
            text(
                "SELECT COUNT(*) AS row_count, MIN(predict_date) AS min_predict_date, "
                "MAX(predict_date) AS max_predict_date, "
                "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count "
                "FROM t_scheme_runs WHERE scheme_id = :scheme_id"
            ),
            {"scheme_id": scheme_id},
        ).mappings().one()
        backtest_runs = conn.execute(
            text(
                "SELECT COUNT(*) AS row_count FROM t_backtest_runs "
                "WHERE scheme_id = :scheme_id"
            ),
            {"scheme_id": scheme_id},
        ).mappings().one()
        backtest_predictions = conn.execute(
            text(
                "SELECT COUNT(*) AS row_count, MIN(target_date) AS min_target_date, "
                "MAX(target_date) AS max_target_date, "
                "SUM(CASE WHEN predicted_direction = -1 THEN 1 ELSE 0 END) AS down_count, "
                "SUM(CASE WHEN predicted_direction = 0 THEN 1 ELSE 0 END) AS flat_count, "
                "SUM(CASE WHEN predicted_direction = 1 THEN 1 ELSE 0 END) AS up_count "
                "FROM t_backtest_predictions WHERE scheme_id = :scheme_id"
            ),
            {"scheme_id": scheme_id},
        ).mappings().one()
        summaries.append(
            {
                "scheme_id": scheme_id,
                "predictions": _normalize_migration_summary(prediction),
                "runs": _normalize_migration_summary(runs),
                "backtest_runs": _normalize_migration_summary(backtest_runs),
                "backtest_predictions": _normalize_migration_summary(
                    backtest_predictions
                ),
            }
        )
    return summaries


def _normalize_migration_summary(
    summary: Mapping[str, object],
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in summary.items():
        if key.endswith("_count") or key == "row_count":
            normalized[key] = int(value or 0)
        elif key.endswith("_date"):
            normalized[key] = str(value) if value is not None else None
        else:
            normalized[key] = value
    return normalized


def _read_unaffected_active_registry_summary_conn(
    conn: Connection,
    *,
    excluded_base_ids: Sequence[str],
    for_update: bool,
) -> dict[str, object]:
    placeholders = ", ".join(
        f":excluded_base_id_{index}"
        for index, _ in enumerate(excluded_base_ids)
    )
    params = {
        f"excluded_base_id_{index}": value
        for index, value in enumerate(excluded_base_ids)
    }
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    rows = conn.execute(
        text(
            "SELECT scheme_id FROM t_scheme_registry WHERE status = 'active' "
            f"AND base_scheme_id NOT IN ({placeholders}) ORDER BY scheme_id"
            + lock_clause
        ),
        params,
    ).scalars().all()
    ids = [str(value) for value in rows]
    return {
        "count": len(ids),
        "scheme_ids_sha256": hashlib.sha256(
            canonical_native_successor_plan({"scheme_ids": ids}).encode("utf-8")
        ).hexdigest(),
    }


def _native_successor_fact_rows_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    normalized = []
    for source in rows:
        row = {
            "run_id": source.get("run_id"),
            "backtest_run_id": source.get("backtest_run_id"),
            "scheme_version": source.get("scheme_version"),
            "scheme_id": source.get("scheme_id"),
            "target_tenor": source.get("target_tenor"),
            "horizon": int(source.get("horizon") or 0),
            "predict_date": str(source.get("predict_date")),
            "feature_date": str(source.get("feature_date")),
            "target_date": str(source.get("target_date")),
            "predicted_direction": source.get("predicted_direction"),
            "backtest_actual_direction": source.get("backtest_actual_direction"),
            "confidence": (
                float(source["confidence"])
                if source.get("confidence") is not None
                else None
            ),
            "model_version": source.get("model_version"),
            "extra": _json_mapping(source.get("extra")),
        }
        normalized.append(row)
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _assert_native_successor_backtest_boundary(
    rows: Sequence[Mapping[str, object]],
) -> None:
    crossing = sorted(
        {
            str(row.get("target_date"))
            for row in rows
            if str(row.get("target_date"))
            >= _NATIVE_SUCCESSOR_LIVE_TARGET_START_DATE
        }
    )
    if crossing:
        raise RuntimeError(
            "successor historical backtest crosses gray-live target boundary: "
            f"first={crossing[0]} boundary={_NATIVE_SUCCESSOR_LIVE_TARGET_START_DATE}"
        )


def _validate_native_successor_historical_grid_coverage_conn(
    conn: Connection,
    *,
    targets: Sequence[NativeSuccessorTarget],
    successor_fact_rows: Mapping[str, Sequence[Mapping[str, object]]],
    for_update: bool,
) -> list[dict[str, object]]:
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    result: list[dict[str, object]] = []
    for target in targets:
        old_rows = conn.execute(
            text(
                "SELECT predict_date, feature_date, target_date "
                "FROM t_scheme_predictions "
                "WHERE scheme_id = :scheme_id "
                "AND target_tenor = :target_tenor AND horizon = :horizon "
                "AND backtest_run_id IS NOT NULL "
                "AND target_date < :live_target_start "
                "ORDER BY target_date, predict_date, feature_date"
                + lock_clause
            ),
            {
                "scheme_id": target.old_base_scheme_id,
                "target_tenor": target.target_tenor,
                "horizon": target.old_horizon,
                "live_target_start": _NATIVE_SUCCESSOR_LIVE_TARGET_START_DATE,
            },
        ).mappings().all()
        old_dates = [
            (
                str(row.get("predict_date")),
                str(row.get("feature_date")),
                str(row.get("target_date")),
            )
            for row in old_rows
        ]
        new_dates = sorted(
            (
                str(row.get("predict_date")),
                str(row.get("feature_date")),
                str(row.get("target_date")),
            )
            for row in successor_fact_rows[target.new_base_scheme_id]
            if row.get("target_tenor") == target.target_tenor
            and int(row.get("horizon") or 0) == target.new_horizon
        )
        old_dates = sorted(old_dates)
        if not old_dates or len(old_dates) != len(set(old_dates)):
            raise RuntimeError(
                "Native historical grid is empty or contains duplicate dates: "
                f"{target.old_base_scheme_id}/{target.target_tenor}"
            )
        if not new_dates or len(new_dates) != len(set(new_dates)):
            raise RuntimeError(
                "successor historical grid is empty or contains duplicate dates: "
                f"{target.new_base_scheme_id}/{target.target_tenor}"
            )
        old_digest = hashlib.sha256(
            canonical_native_successor_plan(
                {"dates": old_dates}
            ).encode("utf-8")
        ).hexdigest()
        new_digest = hashlib.sha256(
            canonical_native_successor_plan(
                {"dates": new_dates}
            ).encode("utf-8")
        ).hexdigest()
        result.append(
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "target_tenor": target.target_tenor,
                "old_horizon": target.old_horizon,
                "new_horizon": target.new_horizon,
                "old_date_count": len(old_dates),
                "new_date_count": len(new_dates),
                "old_date_identity_sha256": old_digest,
                "new_date_identity_sha256": new_digest,
                "data_vintage_drift": old_digest != new_digest,
            }
        )
    return result


def _cutover_native_successors_conn(
    conn: Connection,
    *,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    backtests: Mapping[str, NativeSuccessorBacktestEvidence],
    approved_by: str,
    approved_at: datetime,
) -> None:
    old_registry_rows = _migration_registry_rows_by_id_conn(conn, targets, old=True)
    for scheme_id in sorted(new_configs):
        cfg = new_configs[scheme_id]
        evidence = backtests[scheme_id]
        rows = _prepare_blackbox_backtest_fact_rows_conn(
            conn,
            cfg,
            backtest_run_id=evidence.backtest_run_id,
            backtest_benchmark_id=evidence.benchmark_id,
            backtest_data_snapshot_id=evidence.data_snapshot_id,
            backtest_generation_id=evidence.generation_id,
            backtest_runtime_profile=evidence.runtime_profile,
            backtest_environment_fingerprint=evidence.environment_fingerprint,
            backtest_code_hash=evidence.code_hash,
            backtest_config_hash=evidence.config_hash,
            backtest_manifest_hash=evidence.manifest_hash,
            backtest_validator_policy_digest=evidence.validator_policy_digest,
            require_source_request=True,
        )
        _assert_native_successor_backtest_boundary(rows)
        _publish_or_reuse_successor_facts_conn(conn, cfg, rows)
        _upsert_scheme_version_conn(
            conn,
            cfg,
            trusted_status="active",
            approved_by=approved_by,
            approved_at=approved_at,
        )
    for target in sorted(targets, key=lambda item: item.new_base_scheme_id):
        old_id = registry_scheme_id(
            target.old_base_scheme_id,
            target.old_horizon,
            target.target_tenor,
        )
        _upsert_successor_registry_from_old_conn(
            conn,
            target=target,
            old_row=old_registry_rows[old_id],
        )
    _update_migration_registry_status_conn(conn, targets, old=True, status="archived")
    _update_migration_version_status_conn(
        conn,
        old_configs,
        from_status="active",
        to_status="retired",
        runtime_type="native_adapter",
        approved_by=approved_by,
        approved_at=approved_at,
    )


def _rollback_native_successors_conn(
    conn: Connection,
    *,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    approved_by: str,
    approved_at: datetime,
) -> None:
    _update_migration_registry_status_conn(conn, targets, old=False, status="archived")
    _update_migration_version_status_conn(
        conn,
        new_configs,
        from_status="active",
        to_status="retired",
        runtime_type="blackbox_v2",
        approved_by=approved_by,
        approved_at=approved_at,
    )
    _update_migration_version_status_conn(
        conn,
        old_configs,
        from_status="retired",
        to_status="active",
        runtime_type="native_adapter",
        approved_by=approved_by,
        approved_at=approved_at,
    )
    _update_migration_registry_status_conn(conn, targets, old=True, status="active")


def _migration_registry_rows_by_id_conn(
    conn: Connection,
    targets: Sequence[NativeSuccessorTarget],
    *,
    old: bool,
) -> dict[str, Mapping[str, object]]:
    ids = [
        registry_scheme_id(
            target.old_base_scheme_id if old else target.new_base_scheme_id,
            target.old_horizon if old else target.new_horizon,
            target.target_tenor,
        )
        for target in targets
    ]
    placeholders = ", ".join(f":registry_id_{index}" for index, _ in enumerate(ids))
    params = {f"registry_id_{index}": value for index, value in enumerate(ids)}
    lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
    rows = conn.execute(
        text(
            "SELECT scheme_id, base_scheme_id, name, owner, description, horizon, "
            "task_type, runtime_type, tenors, frequency, target_tenor, "
            "schedule_cron, schedule_timezone, status, deployed_at "
            f"FROM t_scheme_registry WHERE scheme_id IN ({placeholders})"
            f"{lock_clause}"
        ),
        params,
    ).mappings().all()
    result = {str(row["scheme_id"]): row for row in rows}
    if set(result) != set(ids):
        raise RuntimeError("migration Registry rows changed inside transaction")
    return result


def _upsert_successor_registry_from_old_conn(
    conn: Connection,
    *,
    target: NativeSuccessorTarget,
    old_row: Mapping[str, object],
) -> None:
    new_id = registry_scheme_id(
        target.new_base_scheme_id,
        target.new_horizon,
        target.target_tenor,
    )
    params = {
        "scheme_id": new_id,
        "base_scheme_id": target.new_base_scheme_id,
        "name": old_row.get("name"),
        "owner": old_row.get("owner"),
        "description": old_row.get("description"),
        "horizon": target.new_horizon,
        "task_type": target.task_type,
        "runtime_type": "blackbox_v2",
        "tenors": json.dumps([target.target_tenor], ensure_ascii=False),
        "frequency": old_row.get("frequency"),
        "target_tenor": target.target_tenor,
        "schedule_cron": old_row.get("schedule_cron"),
        "schedule_timezone": old_row.get("schedule_timezone"),
    }
    if _dialect_name(conn) == "sqlite":
        statement = """
            INSERT INTO t_scheme_registry
                (scheme_id, base_scheme_id, name, owner, description, horizon,
                 task_type, runtime_type, tenors, frequency, target_tenor,
                 schedule_cron, schedule_timezone, status, deployed_at)
            VALUES
                (:scheme_id, :base_scheme_id, :name, :owner, :description, :horizon,
                 :task_type, :runtime_type, :tenors, :frequency, :target_tenor,
                 :schedule_cron, :schedule_timezone, 'active', CURRENT_DATE)
            ON CONFLICT(scheme_id) DO UPDATE SET
                base_scheme_id=excluded.base_scheme_id, name=excluded.name,
                owner=excluded.owner, description=excluded.description,
                horizon=excluded.horizon, task_type=excluded.task_type,
                runtime_type=excluded.runtime_type, tenors=excluded.tenors,
                frequency=excluded.frequency, target_tenor=excluded.target_tenor,
                schedule_cron=excluded.schedule_cron,
                schedule_timezone=excluded.schedule_timezone, status='active'
        """
    else:
        statement = """
            INSERT INTO t_scheme_registry
                (scheme_id, base_scheme_id, name, owner, description, horizon,
                 task_type, runtime_type, tenors, frequency, target_tenor,
                 schedule_cron, schedule_timezone, status, deployed_at)
            VALUES
                (:scheme_id, :base_scheme_id, :name, :owner, :description, :horizon,
                 :task_type, :runtime_type, CAST(:tenors AS JSON), :frequency,
                 :target_tenor, :schedule_cron, :schedule_timezone, 'active', CURRENT_DATE)
            ON DUPLICATE KEY UPDATE
                base_scheme_id=VALUES(base_scheme_id), name=VALUES(name),
                owner=VALUES(owner), description=VALUES(description),
                horizon=VALUES(horizon), task_type=VALUES(task_type),
                runtime_type=VALUES(runtime_type), tenors=VALUES(tenors),
                frequency=VALUES(frequency), target_tenor=VALUES(target_tenor),
                schedule_cron=VALUES(schedule_cron),
                schedule_timezone=VALUES(schedule_timezone), status='active'
        """
    conn.execute(text(statement), params)


def _update_migration_registry_status_conn(
    conn: Connection,
    targets: Sequence[NativeSuccessorTarget],
    *,
    old: bool,
    status: str,
) -> None:
    ids = [
        registry_scheme_id(
            target.old_base_scheme_id if old else target.new_base_scheme_id,
            target.old_horizon if old else target.new_horizon,
            target.target_tenor,
        )
        for target in targets
    ]
    placeholders = ", ".join(f":registry_id_{index}" for index, _ in enumerate(ids))
    params: dict[str, object] = {
        f"registry_id_{index}": value for index, value in enumerate(ids)
    }
    params["status"] = status
    result = conn.execute(
        text(
            f"UPDATE t_scheme_registry SET status = :status "
            f"WHERE scheme_id IN ({placeholders})"
        ),
        params,
    )
    if result.rowcount != len(ids):
        raise RuntimeError(
            "migration Registry update count mismatch: "
            f"expected={len(ids)} actual={result.rowcount}"
        )


def _update_migration_version_status_conn(
    conn: Connection,
    configs: Mapping[str, SchemeConfig],
    *,
    from_status: str,
    to_status: str,
    runtime_type: str,
    approved_by: str,
    approved_at: datetime,
) -> None:
    for scheme_id in sorted(configs):
        cfg = configs[scheme_id]
        result = conn.execute(
            text(
                "UPDATE t_scheme_versions SET status = :to_status, "
                "approved_by = :approved_by, approved_at = :approved_at "
                "WHERE scheme_id = :scheme_id AND scheme_version = :scheme_version "
                "AND runtime_type = :runtime_type AND status = :from_status"
            ),
            {
                "to_status": to_status,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "scheme_id": scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": runtime_type,
                "from_status": from_status,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "migration exact version update count mismatch: "
                f"{scheme_id}/{cfg.scheme_version} rowcount={result.rowcount}"
            )


def _publish_or_reuse_successor_facts_conn(
    conn: Connection,
    cfg: SchemeConfig,
    expected_rows: Sequence[Mapping[str, object]],
) -> None:
    lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
    existing = conn.execute(
        text(
            "SELECT run_id, backtest_run_id, scheme_version, scheme_id, "
            "target_tenor, horizon, predict_date, feature_date, target_date, "
            "predicted_direction, backtest_actual_direction, confidence, "
            "model_version, extra FROM t_scheme_predictions "
            "WHERE scheme_id = :scheme_id AND backtest_run_id IS NOT NULL "
            "ORDER BY target_tenor, horizon, target_date, predict_date"
            + lock_clause
        ),
        {"scheme_id": cfg.scheme_id},
    ).mappings().all()
    if not existing:
        inserted = _insert_run_predictions_conn(conn, expected_rows)
        if inserted != len(expected_rows):
            raise RuntimeError("successor backtest fact insert count mismatch")
        return
    if _native_successor_fact_rows_sha256(existing) != _native_successor_fact_rows_sha256(
        expected_rows
    ):
        raise RuntimeError(
            "existing successor backtest facts differ from exact persisted backtest"
        )


def _assert_native_successor_post_state_conn(
    conn: Connection,
    *,
    targets: Sequence[NativeSuccessorTarget],
    old_configs: Mapping[str, SchemeConfig],
    new_configs: Mapping[str, SchemeConfig],
    action: str,
) -> None:
    expected_old_version = "retired" if action == "cutover" else "active"
    expected_new_version = "active" if action == "cutover" else "retired"
    expected_old_registry = "archived" if action == "cutover" else "active"
    expected_new_registry = "active" if action == "cutover" else "archived"
    for configs, expected_status in (
        (old_configs, expected_old_version),
        (new_configs, expected_new_version),
    ):
        for scheme_id, cfg in configs.items():
            status = conn.execute(
                text(
                    "SELECT status FROM t_scheme_versions WHERE scheme_id = :scheme_id "
                    "AND scheme_version = :scheme_version"
                ),
                {"scheme_id": scheme_id, "scheme_version": cfg.scheme_version},
            ).scalar_one_or_none()
            if status != expected_status:
                raise RuntimeError(f"migration version readback mismatch: {scheme_id}")
    for old, expected_status in (
        (True, expected_old_registry),
        (False, expected_new_registry),
    ):
        rows = _migration_registry_rows_by_id_conn(conn, targets, old=old)
        if any(row.get("status") != expected_status for row in rows.values()):
            raise RuntimeError("migration Registry readback status mismatch")



def upsert_actuals(engine: Engine, records: Iterable[ActualRecord]) -> int:
    """UPSERT 实际方向记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_actuals
            (tenor, trade_date, close_yield, direction_1d, direction_5d)
        VALUES
            (:tenor, :trade_date, :close_yield, :direction_1d, :direction_5d)
        ON DUPLICATE KEY UPDATE
            close_yield = VALUES(close_yield),
            direction_1d = VALUES(direction_1d),
            direction_5d = VALUES(direction_5d),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = [asdict(record) for record in records]
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def delete_actuals_after_source_watermark(
    engine: Engine,
    source_watermarks: Mapping[str, str],
    end_date: str | None = None,
) -> int:
    """删除晚于当前源表水位的日频 actual 尾部脏数据。

    只清理每个 tenor 的 tail，不处理源表中间缺口，避免上游短暂缺行时误删历史验证。
    """
    rows = [
        {"tenor": str(tenor), "source_max_date": str(source_max_date), "end_date": end_date}
        for tenor, source_max_date in source_watermarks.items()
        if source_max_date
    ]
    if not rows:
        return 0
    end_filter = "AND trade_date <= :end_date" if end_date else ""
    sql = text(
        f"""
        DELETE FROM t_scheme_actuals
        WHERE tenor = :tenor
          AND trade_date > :source_max_date
          {end_filter}
        """
    )
    deleted = 0
    with engine.begin() as conn:
        for row in rows:
            result = conn.execute(sql, row)
            deleted += int(result.rowcount or 0)
    return deleted


def upsert_weekly_actuals(engine: Engine, records: Iterable[WeeklyActualRecord]) -> int:
    """UPSERT 周度实际方向记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_weekly_actuals
            (tenor, feature_week_id, target_week_id, predict_date, feature_date, target_date,
             feature_yield, target_yield, direction_weekly, price_signal, target_rule, extra)
        VALUES
            (:tenor, :feature_week_id, :target_week_id, :predict_date, :feature_date, :target_date,
             :feature_yield, :target_yield, :direction_weekly, :price_signal, :target_rule, CAST(:extra AS JSON))
        ON DUPLICATE KEY UPDATE
            feature_week_id = VALUES(feature_week_id),
            target_week_id = VALUES(target_week_id),
            feature_date = VALUES(feature_date),
            target_date = VALUES(target_date),
            feature_yield = VALUES(feature_yield),
            target_yield = VALUES(target_yield),
            direction_weekly = VALUES(direction_weekly),
            price_signal = VALUES(price_signal),
            target_rule = VALUES(target_rule),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0
    with engine.begin() as conn:
        _assert_weekly_actuals_target_rule_unique_key(conn)
        conn.execute(sql, rows)
    return len(rows)


def upsert_monthly_actuals(engine: Engine, records: Iterable[MonthlyActualRecord]) -> int:
    """UPSERT 月度实际方向记录。"""
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0

    if engine.dialect.name == "sqlite":
        sql = text(
            """
            INSERT INTO t_scheme_monthly_actuals
                (tenor, feature_month_id, target_month_id, predict_date, feature_date, target_date,
                 feature_yield, target_yield, direction_monthly, price_signal, target_rule, extra)
            VALUES
                (:tenor, :feature_month_id, :target_month_id, :predict_date, :feature_date, :target_date,
                 :feature_yield, :target_yield, :direction_monthly, :price_signal, :target_rule, :extra)
            """
        )
    else:
        sql = text(
            """
            INSERT INTO t_scheme_monthly_actuals
                (tenor, feature_month_id, target_month_id, predict_date, feature_date, target_date,
                 feature_yield, target_yield, direction_monthly, price_signal, target_rule, extra)
            VALUES
                (:tenor, :feature_month_id, :target_month_id, :predict_date, :feature_date, :target_date,
                 :feature_yield, :target_yield, :direction_monthly, :price_signal, :target_rule, CAST(:extra AS JSON))
            ON DUPLICATE KEY UPDATE
                feature_month_id = VALUES(feature_month_id),
                target_month_id = VALUES(target_month_id),
                feature_date = VALUES(feature_date),
                target_date = VALUES(target_date),
                feature_yield = VALUES(feature_yield),
                target_yield = VALUES(target_yield),
                direction_monthly = VALUES(direction_monthly),
                price_signal = VALUES(price_signal),
                target_rule = VALUES(target_rule),
                extra = VALUES(extra),
                updated_at = CURRENT_TIMESTAMP
            """
        )
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def upsert_period_average_actuals(
    engine: Engine,
    records: Iterable[PeriodAverageActualRecord],
) -> int:
    """UPSERT MID/CQ/SF 共用的周期均值实际方向记录。"""
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0

    json_value = ":extra" if engine.dialect.name == "sqlite" else "CAST(:extra AS JSON)"
    duplicate = "" if engine.dialect.name == "sqlite" else """
        ON DUPLICATE KEY UPDATE
            feature_date = VALUES(feature_date),
            target_date = VALUES(target_date),
            feature_yield = VALUES(feature_yield),
            target_yield = VALUES(target_yield),
            actual_direction = VALUES(actual_direction),
            price_signal = VALUES(price_signal),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
    """
    sql = text(
        f"""
        INSERT INTO t_scheme_period_average_actuals
            (tenor, predict_date, feature_date, target_date,
             feature_yield, target_yield, actual_direction,
             price_signal, target_rule, extra)
        VALUES
            (:tenor, :predict_date, :feature_date, :target_date,
             :feature_yield, :target_yield, :actual_direction,
             :price_signal, :target_rule, {json_value})
        {duplicate}
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def _assert_weekly_actuals_target_rule_unique_key(conn) -> None:
    """确认周度 actual 唯一键包含 target_rule，避免 point/average 互相覆盖。"""
    expected = ("tenor", "predict_date", "target_rule")
    legacy = ("tenor", "predict_date")
    indexes = inspect(conn).get_indexes("t_scheme_weekly_actuals")
    unique_columns = [
        tuple(index.get("column_names") or ())
        for index in indexes
        if bool(index.get("unique"))
    ]
    if expected not in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals missing unique key "
            "uk_weekly_actual_predict_rule(tenor,predict_date,target_rule); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )
    if legacy in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals still has legacy unique key on (tenor,predict_date); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )


def write_run_log(
    engine: Engine,
    scheme_id: str,
    run_date: str,
    status: str,
    duration_sec: float | None = None,
    error_msg: str | None = None,
    run_id: int | None = None,
) -> None:
    """写入方案运行日志。"""
    with engine.begin() as conn:
        _write_run_log_conn(
            conn,
            scheme_id,
            run_date,
            status,
            duration_sec,
            error_msg,
            run_id,
        )


def _write_run_log_conn(
    conn: Connection,
    scheme_id: str,
    run_date: str,
    status: str,
    duration_sec: float | None = None,
    error_msg: str | None = None,
    run_id: int | None = None,
) -> None:
    """在调用方事务中写入方案运行日志。"""
    sql = text(
        """
        INSERT INTO t_scheme_run_log (run_id, scheme_id, run_date, status, duration_sec, error_msg)
        VALUES (:run_id, :scheme_id, :run_date, :status, :duration_sec, :error_msg)
        """
    )
    conn.execute(
        sql,
        {
            "run_id": run_id,
            "scheme_id": scheme_id,
            "run_date": run_date,
            "status": status,
            "duration_sec": duration_sec,
            "error_msg": error_msg,
        },
    )
