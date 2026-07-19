from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Mapping

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, URL

from scheduler.discovery import SchemeConfig
from shared.db_config import DatabaseConfig
from shared.input_artifacts import InputArtifact
from shared.models import ActualRecord, MonthlyActualRecord, PredictionRecord, WeeklyActualRecord


VALID_PREDICTION_PHASES = {"gray_live", "scheduled_live"}
VERSION_STATUSES = {"draft", "validated", "shadow", "active", "paused", "retired"}
BLACKBOX_REGISTRY_STATUSES = {"active", "paused", "archived"}
BLACKBOX_IMMUTABLE_VERSION_FIELDS = (
    "runtime_type",
    "algorithm_version",
    "contract_version",
    "runtime_profile",
    "environment_fingerprint",
    "data_snapshot_id",
    "code_hash",
    "config_hash",
    "manifest_hash",
)


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
    return create_engine(url, future=True)


def sync_scheme_registry(engine: Engine, schemes: Iterable[SchemeConfig]) -> None:
    """将配置文件中的方案元数据同步到 t_scheme_registry。"""
    scheme_list = list(schemes)
    if not scheme_list:
        return
    with engine.begin() as conn:
        effective_statuses: dict[str, str] = {}
        for cfg in scheme_list:
            runtime_type = getattr(cfg, "runtime_type", "native_adapter")
            version_row = None
            if getattr(cfg, "scheme_version", None):
                version_row = _upsert_discovered_scheme_version_conn(conn, cfg)
            if runtime_type == "blackbox_v2":
                approved = bool(
                    version_row is not None
                    and version_row.get("scheme_id") == cfg.scheme_id
                    and version_row.get("scheme_version") == cfg.scheme_version
                    and version_row.get("runtime_type") == "blackbox_v2"
                    and version_row.get("status") == "active"
                    and isinstance(version_row.get("approved_by"), str)
                    and bool(str(version_row.get("approved_by")).strip())
                    and isinstance(version_row.get("approved_at"), datetime)
                )
                effective_status = "active" if cfg.status == "active" and approved else "paused"
            else:
                effective_status = cfg.status
            for target_tenor in cfg.tenors:
                effective_statuses[
                    registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
                ] = effective_status
        _sync_scheme_registry_conn(conn, scheme_list, effective_statuses=effective_statuses)


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
            (scheme_id, base_scheme_id, name, description, horizon, task_type, runtime_type, tenors, frequency, target_tenor,
             schedule_cron, schedule_timezone, status, deployed_at)
        VALUES
            (:scheme_id, :base_scheme_id, :name, :description, :horizon, :task_type, :runtime_type, CAST(:tenors AS JSON), :frequency,
             :target_tenor, :schedule_cron, :schedule_timezone, :status,
             IF(:status = 'active', CURRENT_DATE, NULL))
        ON DUPLICATE KEY UPDATE
            updated_at = IF(
                NOT (
                    base_scheme_id <=> VALUES(base_scheme_id)
                    AND
                    name <=> VALUES(name)
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
    rows = []
    registry_ids_by_base: dict[str, list[str]] = {}
    for cfg in scheme_list:
        registry_ids: list[str] = []
        for target_tenor in cfg.tenors:
            row_scheme_id = registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
            effective_status = effective_statuses[row_scheme_id]
            registry_ids.append(row_scheme_id)
            rows.append(
                {
                    "scheme_id": row_scheme_id,
                    "base_scheme_id": cfg.scheme_id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "horizon": cfg.horizon,
                    "task_type": cfg.task_type,
                    "runtime_type": getattr(cfg, "runtime_type", "native_adapter"),
                    "tenors": json.dumps([target_tenor], ensure_ascii=False),
                    "frequency": cfg.frequency,
                    "target_tenor": target_tenor,
                    "schedule_cron": cfg.schedule.cron,
                    "schedule_timezone": cfg.schedule.timezone,
                    "status": effective_status,
                }
            )
        registry_ids_by_base[cfg.scheme_id] = registry_ids
    if not rows:
        return
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


def upsert_scheme_version(engine: Engine, cfg: SchemeConfig) -> str:
    """将发现到的方案版本写入 t_scheme_versions，保持幂等。"""
    with engine.begin() as conn:
        _upsert_discovered_scheme_version_conn(conn, cfg)
    return cfg.scheme_version


def _upsert_discovered_scheme_version_conn(
    conn: Connection,
    cfg: SchemeConfig,
) -> Mapping[str, object] | None:
    """锁定并同步发现版本；Blackbox 只允许插入 draft 或原样保留。"""
    existing = _read_scheme_version_conn(conn, cfg, for_update=True)
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if existing is not None and (
        runtime_type == "blackbox_v2" or existing.get("runtime_type") == "blackbox_v2"
    ):
        _raise_on_blackbox_version_metadata_conflict(existing, cfg)
    _upsert_scheme_version_conn(
        conn,
        cfg,
        trusted_status=None,
        approved_by=None,
        approved_at=None,
    )
    return existing


def _raise_on_blackbox_version_metadata_conflict(
    existing: Mapping[str, object],
    cfg: SchemeConfig,
) -> None:
    conflicts = []
    for field in BLACKBOX_IMMUTABLE_VERSION_FIELDS:
        stored = existing.get(field)
        discovered = getattr(cfg, field, None)
        if discovered is None or stored is None or stored == discovered:
            continue
        conflicts.append(f"{field}: stored={stored!r}, discovered={discovered!r}")
    if conflicts:
        raise ValueError(
            f"immutable Blackbox version metadata mismatch for {cfg.scheme_id}/{cfg.scheme_version}: "
            + "; ".join(conflicts)
        )


def _upsert_scheme_version_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    trusted_status: str | None,
    approved_by: str | None,
    approved_at: datetime | None,
) -> str:
    """在调用方事务中写入精确版本；只有 trusted_status 可提升 Blackbox 生命周期。"""
    if trusted_status is not None and trusted_status not in VERSION_STATUSES:
        raise ValueError(f"invalid trusted scheme version status: {trusted_status}")
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    configured_status = getattr(cfg, "version_status", cfg.status)
    if trusted_status is not None:
        status = trusted_status
    elif runtime_type == "blackbox_v2":
        status = "draft"
    else:
        status = configured_status if configured_status in VERSION_STATUSES else "draft"
    preserve_lifecycle = runtime_type == "blackbox_v2" and trusted_status is None
    preserve_blackbox_evidence = preserve_lifecycle
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
    lock_clause = " FOR UPDATE" if for_update else ""
    return (
        conn.execute(
            text(
                "SELECT scheme_id, scheme_version, runtime_type, algorithm_version, contract_version, "
                "runtime_profile, environment_fingerprint, data_snapshot_id, code_hash, config_hash, "
                "manifest_hash, git_commit, status, approved_by, approved_at "
                "FROM t_scheme_versions "
                "WHERE scheme_id = :scheme_id AND scheme_version = :scheme_version "
                f"LIMIT 1{lock_clause}"
            ),
            {"scheme_id": cfg.scheme_id, "scheme_version": cfg.scheme_version},
        )
        .mappings()
        .one_or_none()
    )


def read_blackbox_execution_approval(engine: Engine, cfg: SchemeConfig) -> BlackboxExecutionApproval:
    """读取并验证 Blackbox 精确版本及 composite Registry 的生产批准。"""
    with engine.begin() as conn:
        return _read_blackbox_execution_approval_conn(conn, cfg, for_update=False)


def read_blackbox_lifecycle_state(engine: Engine, cfg: SchemeConfig) -> BlackboxLifecycleState:
    """独立读取 Blackbox 精确版本和 composite Registry 生命周期状态。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("Blackbox lifecycle read requires runtime_type=blackbox_v2")
    expected_tenors, expected_registry_ids = _expected_blackbox_registry_identity(cfg)
    with engine.begin() as conn:
        version_row = _read_scheme_version_conn(conn, cfg, for_update=False)
        if version_row is None:
            raise RuntimeError(
                f"exact version not found: scheme_id={cfg.scheme_id} scheme_version={cfg.scheme_version}"
            )
        registry_rows = _read_blackbox_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=False,
        )
    statuses = {str(row.get("status")) for row in registry_rows}
    if len(statuses) != 1:
        raise RuntimeError(f"Blackbox Registry lifecycle statuses are inconsistent: {sorted(statuses)}")
    registry_status = next(iter(statuses))
    registry_error = _blackbox_registry_identity_error(
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
    if getattr(cfg, "status", None) != "active":
        return denied(f"config status is {getattr(cfg, 'status', None)}, expected active")
    if not base_scheme_id:
        return denied("config scheme_id is empty")
    if not scheme_version:
        return denied(f"config scheme_version is empty for {base_scheme_id}")

    try:
        expected_tenors, expected_registry_ids = _expected_blackbox_registry_identity(cfg)
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

    registry_rows = _read_blackbox_registry_rows_conn(
        conn,
        cfg,
        expected_registry_ids,
        for_update=for_update,
    )
    registry_ids = tuple(str(row.get("scheme_id")) for row in registry_rows)
    registry_error = _blackbox_registry_identity_error(
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


def _expected_blackbox_registry_identity(
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


def _read_blackbox_registry_rows_conn(
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
    lock_clause = " FOR UPDATE" if for_update else ""
    return (
        conn.execute(
            text(
                "SELECT scheme_id, base_scheme_id, runtime_type, status, task_type, target_tenor, horizon "
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


def _blackbox_registry_identity_error(
    cfg: SchemeConfig,
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
    *,
    expected_status: str,
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
        if row.get("runtime_type") != "blackbox_v2":
            return (
                f"registry {expected_id} runtime_type is {row.get('runtime_type')}, "
                "expected blackbox_v2"
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


def apply_blackbox_lifecycle_state(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    version_status: str,
    registry_status: str,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
) -> BlackboxLifecycleState:
    """在单一事务中可信更新 Blackbox 精确版本和 composite Registry。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("trusted Blackbox lifecycle update requires runtime_type=blackbox_v2")
    if version_status not in VERSION_STATUSES:
        raise ValueError(f"invalid trusted scheme version status: {version_status}")
    if registry_status not in BLACKBOX_REGISTRY_STATUSES:
        raise ValueError(f"invalid trusted Registry status: {registry_status}")
    if registry_status == "active" and version_status != "active":
        raise ValueError("active Blackbox Registry requires active version status")
    if approved_by is not None and (not isinstance(approved_by, str) or not approved_by.strip()):
        raise ValueError("approved_by must be a non-empty string when provided")
    if approved_at is not None and not isinstance(approved_at, datetime):
        raise ValueError("approved_at must be datetime when provided")
    if version_status == "active" and (approved_by is None or approved_at is None):
        raise ValueError("active Blackbox version requires approved_by and approved_at")

    expected_tenors, expected_registry_ids = _expected_blackbox_registry_identity(cfg)
    effective_statuses = {
        registry_id: registry_status for registry_id in expected_registry_ids
    }
    with engine.begin() as conn:
        _upsert_scheme_version_conn(
            conn,
            cfg,
            trusted_status=version_status,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        _sync_scheme_registry_conn(
            conn,
            [cfg],
            effective_statuses=effective_statuses,
        )
        version_row = _read_scheme_version_conn(conn, cfg, for_update=True)
        if version_row is None:
            raise RuntimeError(
                f"trusted lifecycle version readback missing: {cfg.scheme_id}/{cfg.scheme_version}"
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
            "status": version_status,
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
        mismatches = [
            f"{field}: expected={expected!r}, got={version_row.get(field)!r}"
            for field, expected in expected_version_values.items()
            if version_row.get(field) != expected
        ]
        if mismatches:
            raise RuntimeError("trusted lifecycle version readback mismatch: " + "; ".join(mismatches))
        registry_rows = _read_blackbox_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=True,
        )
        registry_error = _blackbox_registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status=registry_status,
        )
        if registry_error is not None:
            raise RuntimeError(f"trusted lifecycle Registry readback mismatch: {registry_error}")

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
    )


def create_scheme_run(
    engine: Engine,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    runtime_type: str = "native_adapter",
    run_type: str = "active",
    prediction_phase: str | None = None,
    status: str = "running",
    harness_run_id: str | None = None,
    input_artifact_id: str | None = None,
    data_snapshot_id: str | None = None,
    records_expected: int | None = None,
) -> int:
    """创建一次不可变预测运行记录，返回 run_id。"""
    if prediction_phase is not None and prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}")
    sql = text(
        """
        INSERT INTO t_scheme_runs
            (scheme_id, scheme_version, runtime_type, run_type, prediction_phase, predict_date, status,
             harness_run_id, input_artifact_id, data_snapshot_id, records_expected)
        VALUES
            (:scheme_id, :scheme_version, :runtime_type, :run_type, :prediction_phase, :predict_date, :status,
             :harness_run_id, :input_artifact_id, :data_snapshot_id, :records_expected)
        """
    )
    params = {
        "scheme_id": scheme_id,
        "scheme_version": scheme_version,
        "runtime_type": runtime_type,
        "run_type": run_type,
        "prediction_phase": prediction_phase,
        "predict_date": predict_date,
        "status": status,
        "harness_run_id": harness_run_id,
        "input_artifact_id": input_artifact_id,
        "data_snapshot_id": data_snapshot_id,
        "records_expected": records_expected,
    }
    with engine.begin() as conn:
        result = conn.execute(sql, params)
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


def finish_scheme_run(
    engine: Engine,
    *,
    run_id: int,
    status: str,
    records_returned: int | None = None,
    records_written: int | None = None,
    error_message: str | None = None,
) -> None:
    """标记预测运行结束。"""
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
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "run_id": run_id,
                "status": status,
                "records_returned": records_returned,
                "records_written": records_written,
                "error_message": error_message,
            },
        )


def insert_run_predictions(
    engine: Engine,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None = None,
) -> int:
    """UPSERT 预测记录，按 UK (scheme_id, target_tenor, horizon, target_date) 覆盖。"""
    with engine.begin() as conn:
        return _insert_run_predictions_conn(
            conn,
            run_id,
            records,
            scheme_version=scheme_version,
        )


def insert_approved_blackbox_predictions(
    engine: Engine,
    cfg: SchemeConfig,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None = None,
) -> int:
    """锁定并复核 Blackbox 批准状态后，在同一事务写入预测。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("approved Blackbox insert requires runtime_type=blackbox_v2")
    if scheme_version is not None and scheme_version != cfg.scheme_version:
        raise ValueError(
            f"prediction scheme_version={scheme_version} does not match config {cfg.scheme_version}"
        )
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
    with engine.begin() as conn:
        approval = _read_blackbox_execution_approval_conn(conn, cfg, for_update=True)
        if not approval.executable:
            raise RuntimeError(
                f"Blackbox V2 version is not production-approved: {approval.reason}"
            )
        return _insert_run_predictions_conn(
            conn,
            run_id,
            record_list,
            scheme_version=exact_scheme_version,
        )


def _insert_run_predictions_conn(
    conn: Connection,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> int:
    """在调用方事务中 UPSERT 预测记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_predictions
            (run_id, scheme_version, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
             prediction_phase, predicted_direction, confidence, model_version, extra)
        VALUES
            (:run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :prediction_phase, :predicted_direction, :confidence, :model_version, CAST(:extra AS JSON))
        ON DUPLICATE KEY UPDATE
            run_id = VALUES(run_id),
            scheme_version = VALUES(scheme_version),
            predict_date = VALUES(predict_date),
            feature_date = VALUES(feature_date),
            prediction_phase = VALUES(prediction_phase),
            predicted_direction = VALUES(predicted_direction),
            confidence = VALUES(confidence),
            model_version = VALUES(model_version),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = []
    for record in records:
        row = asdict(record)
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(f"feature_date is required for prediction record {record.scheme_id}/{record.target_tenor}")
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                f"anchor_date must equal feature_date for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        phase = record.prediction_phase or extra.get("prediction_phase")
        if phase not in VALID_PREDICTION_PHASES:
            raise ValueError(
                f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)} "
                f"for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        extra["feature_date"] = str(feature_date)
        extra["prediction_phase"] = str(phase)
        row["run_id"] = record.run_id if record.run_id is not None else run_id
        row["scheme_version"] = record.scheme_version if record.scheme_version is not None else scheme_version
        row["feature_date"] = str(feature_date)
        row["prediction_phase"] = str(phase)
        row["extra"] = json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0
    conn.execute(sql, rows)
    return len(rows)


def upsert_input_artifact(engine: Engine, artifact: InputArtifact) -> str:
    """UPSERT 输入产物指纹，返回稳定 artifact_id。"""
    predict_date = artifact.metadata.get("predict_date")
    if not predict_date:
        raise ValueError("InputArtifact.metadata must include predict_date")

    coverage = artifact.date_coverage or {}
    if coverage.get("field") == "date":
        min_date = coverage.get("start")
        max_date = coverage.get("end")
    else:
        min_date = None
        max_date = None

    sql = text(
        """
        INSERT INTO t_input_artifacts
            (artifact_id, scheme_id, scheme_version, predict_date, frequency,
             data_version, artifact_uri, content_hash, schema_hash, source_watermark,
             row_count, min_date, max_date)
        VALUES
            (:artifact_id, :scheme_id, :scheme_version, :predict_date, :frequency,
             :data_version, :artifact_uri, :content_hash, :schema_hash, :source_watermark,
             :row_count, :min_date, :max_date)
        ON DUPLICATE KEY UPDATE
            scheme_version = VALUES(scheme_version),
            data_version = VALUES(data_version),
            artifact_uri = VALUES(artifact_uri),
            schema_hash = VALUES(schema_hash),
            source_watermark = VALUES(source_watermark),
            row_count = VALUES(row_count),
            min_date = VALUES(min_date),
            max_date = VALUES(max_date)
        """
    )
    params = {
        "artifact_id": artifact.artifact_id,
        "scheme_id": artifact.scheme_id,
        "scheme_version": artifact.metadata.get("scheme_version"),
        "predict_date": str(predict_date),
        "frequency": artifact.frequency,
        "data_version": artifact.data_version,
        "artifact_uri": str(artifact.path),
        "content_hash": artifact.content_hash,
        "schema_hash": artifact.schema_hash,
        "source_watermark": artifact.source_watermark,
        "row_count": artifact.row_count,
        "min_date": min_date,
        "max_date": max_date,
    }
    with engine.begin() as conn:
        conn.execute(sql, params)
    return artifact.artifact_id


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
    sql = text(
        """
        INSERT INTO t_scheme_run_log (run_id, scheme_id, run_date, status, duration_sec, error_msg)
        VALUES (:run_id, :scheme_id, :run_date, :status, :duration_sec, :error_msg)
        """
    )
    with engine.begin() as conn:
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
