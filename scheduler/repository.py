from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from scheduler.discovery import SchemeConfig, blackbox_deliveries, load_scheme_config
from scheduler.persistence.actuals import (
    ActualTailRepairPlan,
    ActualWriteStats,
    _actual_keys_after_source_watermark_conn,
    _assert_weekly_actuals_target_rule_unique_key,
    _delete_actual_keys_conn,
    _require_current_actual_tail_plan,
    _upsert_actuals_conn,
    _upsert_actuals_detailed_conn,
    actual_comparison_value as _actual_comparison_value,
    classify_actual_rows_conn as _classify_actual_rows_conn,
    delete_actuals_after_source_watermark,
    plan_actuals_tail_repair,
    repair_actuals_after_source_watermark,
    repair_actuals_after_source_watermark_detailed,
    upsert_actuals,
    upsert_actuals_detailed,
    upsert_monthly_actuals,
    upsert_monthly_actuals_detailed,
    upsert_period_average_actuals,
    upsert_period_average_actuals_detailed,
    upsert_weekly_actuals,
    upsert_weekly_actuals_detailed,
)
from scheduler.persistence.connections import (
    _set_mysql_session_utc_on_checkout,
    create_engine_from_env,
    dialect_name as _dialect_name,
)
from shared.blackbox_v2.contracts import REQUEST_FIELDS, request_from_mapping
from shared.models import PredictionRecord
from shared.one_shot_control_plane import SCHEDULED_ONE_SHOT_CONTROL_PLANES
from shared.prediction_context import LIVE_PREDICTION_PHASES
from shared.scheme_config_schema import (
    ALLOWED_RUNTIME_TYPES,
    ALLOWED_VERSION_STATUS,
    normalize_scheme_owner,
)


PREDICTION_KEYS_ALREADY_EXIST = "prediction_keys_already_exist"
PARTIAL_PREDICTION_KEY_CONFLICT = "partial_prediction_key_conflict"
_PredictionBusinessKey = tuple[str, str, int, str]
BLACKBOX_REGISTRY_STATUSES = {"active", "paused", "archived"}
_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC = 5.0
_BLACKBOX_BACKTEST_START_DATE = "2025-01-01"
_BLACKBOX_LIVE_TARGET_START_DATE = "2026-06-01"
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


def registry_scheme_id(base_scheme_id: str, horizon: int, target_tenor: str) -> str:
    """生成前端/业务层唯一方案 ID。"""
    return f"{base_scheme_id}__h{int(horizon)}__{target_tenor}"


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
            registry_params = {
                f"registry_id_{index}": registry_id
                for index, registry_id in enumerate(expected_registry_ids)
            }
            registry_placeholders = ", ".join(
                f":{key}" for key in registry_params
            )
            registry_rows = conn.execute(
                text(
                    "SELECT scheme_id, name, description, owner "
                    "FROM t_scheme_registry "
                    f"WHERE scheme_id IN ({registry_placeholders}){lock_clause}"
                ),
                registry_params,
            ).mappings().all()
            registry_by_id = {row["scheme_id"]: row for row in registry_rows}
            metadata_by_id = {
                registry_scheme_id(
                    cfg.scheme_id, cfg.horizon, delivery.metadata.target_tenor
                ): delivery.metadata
                for delivery in blackbox_deliveries(cfg)
            }
            for registry_id in expected_registry_ids:
                row = registry_by_id.get(registry_id)
                if row is None:
                    raise RuntimeError(
                        "Blackbox initial activation Registry display readback "
                        f"missing: {registry_id}"
                    )
                for field in ("name", "description", "owner"):
                    if row[field] != getattr(metadata_by_id[registry_id], field):
                        raise RuntimeError(
                            "Blackbox initial activation Registry display readback "
                            f"mismatch: {registry_id}.{field}"
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
            "target_date, label, predicted_direction, extra"
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
            "backtest_start_date": _BLACKBOX_BACKTEST_START_DATE,
            "target_date_before": _BLACKBOX_LIVE_TARGET_START_DATE,
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
             predicted_direction, backtest_actual_direction, model_version, extra)
        VALUES
            (:run_id, :backtest_run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :predicted_direction, :backtest_actual_direction, :model_version, {extra_expression})
        """.format(extra_expression=extra_expression)
    rows = [
        {key: value for key, value in row.items() if key != "_source_request"}
        for row in prediction_rows
    ]
    if not rows:
        return 0
    conn.execute(text(statement), rows)
    return len(rows)




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
