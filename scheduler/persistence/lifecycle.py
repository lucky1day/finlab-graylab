"""方案版本、Registry 与激活生命周期的内部持久化实现。"""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Iterator, Mapping
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from scheduler.discovery import SchemeConfig, blackbox_deliveries
from scheduler.persistence.connections import dialect_name as _dialect_name
from shared.scheme_config_schema import (
    ALLOWED_RUNTIME_TYPES,
    ALLOWED_VERSION_STATUS,
    normalize_scheme_owner,
)
from scheduler.persistence.prediction_rows import (
    _insert_run_predictions_conn,
    _mysql_utc_datetime,
    _prepare_blackbox_backtest_fact_rows_conn,
)


BLACKBOX_REGISTRY_STATUSES = {"active", "paused", "archived"}

_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC = 5.0

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
