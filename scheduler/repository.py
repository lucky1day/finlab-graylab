from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, URL

from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.blackbox_scheduler_admission import LAUNCHD_ONE_SHOT
from shared.blackbox_v2.lifecycle import assert_lifecycle_clear, lifecycle_operation_lock
from shared.db_config import DatabaseConfig
from shared.input_artifacts import InputArtifact
from shared.models import ActualRecord, MonthlyActualRecord, PredictionRecord, WeeklyActualRecord
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
)


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
BLACKBOX_BOOTSTRAP_EMPTY_TABLES = (
    "t_scheme_registry",
    "t_scheme_versions",
    "t_scheme_runs",
    "t_input_artifacts",
    "t_scheme_predictions",
    "t_scheme_run_log",
    "t_scheme_actuals",
    "t_scheme_weekly_actuals",
    "t_scheme_monthly_actuals",
    "t_backtest_runs",
    "t_backtest_predictions",
    "t_backtest_monthly_metrics",
    "t_backtest_reproduction_checks",
    "t_harness_runs",
    "t_harness_gate_results",
    "t_input_generations",
)
_TARGET_REGISTRY_BASELINE_VERSION = "migrations-003-013"
_TARGET_REGISTRY_BUSINESS_FIELDS = (
    "target_code",
    "display_name",
    "asset_class",
    "target_type",
    "sort_order",
    "status",
    "extra",
)
_TARGET_REGISTRY_BASELINE = (
    ("1Y", "1Y国债活跃", "bond", "active_treasury", 10, "active", {"legacy_tenor": "1Y"}),
    ("3Y", "3Y国债活跃", "bond", "active_treasury", 30, "active", {"legacy_tenor": "3Y"}),
    ("5Y", "5Y国债活跃", "bond", "active_treasury", 50, "active", {"legacy_tenor": "5Y"}),
    ("7Y", "7Y国债活跃", "bond", "active_treasury", 70, "active", {"legacy_tenor": "7Y"}),
    ("10Y", "10Y国债活跃", "bond", "active_treasury", 100, "active", {"legacy_tenor": "10Y"}),
)
_BLACKBOX_CERTIFICATION_SCHEMA = re.compile(r"^bbv2_cert_[A-Za-z0-9_]+$")
_GENERATION_TYPES = {"native_source", "databridge_v1"}
_READINESS_BASES = {"UPSTREAM_SEAL", "CLOCK_CONTRACT"}
INPUT_GENERATION_BUILDING = "BUILDING"
INPUT_GENERATION_SEALED = "SEALED"
INPUT_GENERATION_INVALIDATED = "INVALIDATED"
_LOGGER = logging.getLogger(__name__)


class BlackboxTargetRegistryBaselineError(RuntimeError):
    """携带 target_registry 失败证据的 bootstrap 阻断。"""

    def __init__(self, summary: dict[str, object]) -> None:
        self.summary = summary
        super().__init__(
            "target_registry baseline mismatch: "
            + json.dumps(summary, ensure_ascii=False, sort_keys=True)
        )


class BlackboxBootstrapLockTimeout(RuntimeError):
    """隔离 Schema bootstrap 互斥锁未在限定时间内取得。"""


class BlackboxDraftRegisterLockTimeout(RuntimeError):
    """首次生产 draft 登记未能取得方案级互斥锁。"""


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
    registry_scheme_ids: tuple[str, ...]


@dataclass(frozen=True)
class BlackboxBootstrapState:
    """空隔离 Schema 初始化后的只读证据。"""

    schema_name: str
    scheme_id: str
    scheme_version: str
    version_status: str
    registry_status: str
    table_counts: dict[str, int]
    target_registry_baseline: dict[str, object]


@dataclass(frozen=True)
class InputGenerationEnvelope:
    """已封存输入 generation 的通用只读 provenance。"""

    generation_id: str
    generation_type: str
    business_date: str
    feature_date: str
    readiness_basis: str
    source_commit_token: str
    dataset_content_id: str
    schema_version: str
    exporter_version: str
    manifest_uri: str
    manifest_sha256: str
    native_generation_id: str | None
    native_manifest_sha256: str | None
    state: str
    sealed_at: datetime | None
    invalidated_at: datetime | None
    invalid_reason: str | None


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


def bootstrap_blackbox_control_plane(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    expected_schema: str,
    lock_timeout_sec: float = 5.0,
) -> BlackboxBootstrapState:
    """仅在全新认证 Schema 中原子建立 Blackbox draft/paused 身份。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("Blackbox bootstrap requires runtime_type=blackbox_v2")
    if cfg.status != "paused" or cfg.version_status != "draft":
        raise ValueError(
            "Blackbox bootstrap requires config paused+draft: "
            f"got={cfg.status}+{cfg.version_status}"
        )
    if not _BLACKBOX_CERTIFICATION_SCHEMA.fullmatch(str(expected_schema)):
        raise ValueError(
            "expected certification Schema must match bbv2_cert_[A-Za-z0-9_]+"
        )
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    effective_statuses = {scheme_id: "paused" for scheme_id in expected_registry_ids}

    with _blackbox_bootstrap_advisory_lock(
        engine,
        expected_schema=expected_schema,
        timeout_sec=lock_timeout_sec,
    ):
        with engine.begin() as conn:
            actual_schema = str(conn.execute(text("SELECT DATABASE()")).scalar_one() or "")
            if actual_schema != expected_schema:
                raise RuntimeError(
                    "database Schema mismatch for Blackbox bootstrap: "
                    f"expected={expected_schema}, actual={actual_schema}"
                )
            table_counts = {
                table: int(
                    conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
                )
                for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES
            }
            nonempty = {table: count for table, count in table_counts.items() if count != 0}
            if nonempty:
                raise RuntimeError(
                    "Blackbox bootstrap test Schema must be empty: "
                    + ", ".join(f"{table}={count}" for table, count in sorted(nonempty.items()))
                )
            target_registry_baseline = _validate_target_registry_baseline_conn(conn)

            _upsert_scheme_version_conn(
                conn,
                cfg,
                trusted_status="draft",
                approved_by=None,
                approved_at=None,
            )
            _sync_scheme_registry_conn(
                conn,
                [cfg],
                effective_statuses=effective_statuses,
            )
            version_row = _read_scheme_version_conn(conn, cfg, for_update=True)
            registry_rows = _read_scheme_registry_rows_conn(
                conn,
                cfg,
                expected_registry_ids,
                for_update=True,
            )
            if version_row is None:
                raise RuntimeError("Blackbox bootstrap version readback is missing")
            if (
                version_row.get("scheme_id") != cfg.scheme_id
                or version_row.get("scheme_version") != cfg.scheme_version
                or version_row.get("runtime_type") != "blackbox_v2"
                or version_row.get("status") != "draft"
                or version_row.get("approved_by") is not None
                or version_row.get("approved_at") is not None
            ):
                raise RuntimeError("Blackbox bootstrap version readback is not exact draft state")
            registry_error = _registry_identity_error(
                cfg,
                expected_tenors,
                expected_registry_ids,
                registry_rows,
                expected_status="paused",
            )
            if registry_error is not None:
                raise RuntimeError(f"Blackbox bootstrap Registry readback mismatch: {registry_error}")

    return BlackboxBootstrapState(
        schema_name=actual_schema,
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        version_status="draft",
        registry_status="paused",
        table_counts=table_counts,
        target_registry_baseline=target_registry_baseline,
    )


def register_blackbox_draft_identity(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    expected_harness_run_id: str,
    lock_timeout_sec: float = 5.0,
) -> BlackboxLifecycleState:
    """在非空生产 Schema 中 insert-only 登记全新 Blackbox draft 身份。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("Blackbox draft registration requires runtime_type=blackbox_v2")
    if cfg.status != "paused" or cfg.version_status != "draft":
        raise ValueError(
            "Blackbox draft registration requires config paused+draft: "
            f"got={cfg.status}+{cfg.version_status}"
        )
    if not str(getattr(cfg, "environment_fingerprint", "") or "").strip():
        raise ValueError("Blackbox draft registration requires environment_fingerprint")
    if not str(getattr(cfg, "data_snapshot_id", "") or "").strip():
        raise ValueError("Blackbox draft registration requires data_snapshot_id")
    if (
        not isinstance(expected_harness_run_id, str)
        or not expected_harness_run_id.strip()
    ):
        raise ValueError(
            "Blackbox draft registration requires expected_harness_run_id"
        )

    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    identity_ids = (cfg.scheme_id, *expected_registry_ids)
    identity_placeholders = ", ".join(
        f":identity_id_{index}" for index, _ in enumerate(identity_ids)
    )
    identity_params = {
        f"identity_id_{index}": identity_id
        for index, identity_id in enumerate(identity_ids)
    }

    with _blackbox_draft_register_advisory_lock(
        engine,
        scheme_id=cfg.scheme_id,
        timeout_sec=lock_timeout_sec,
    ):
        with engine.begin() as conn:
            latest_run = (
                conn.execute(
                    text(
                        """
                        /* draft registration latest passed all-stage fence */
                        SELECT harness_run_id
                        FROM t_harness_runs
                        WHERE scheme_id = :scheme_id
                          AND scheme_version = :scheme_version
                          AND stage = 'all'
                          AND status = 'passed'
                        ORDER BY finished_at DESC, harness_run_id DESC
                        LIMIT 1
                        FOR UPDATE
                        """
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "scheme_version": cfg.scheme_version,
                    },
                )
                .mappings()
                .one_or_none()
            )
            actual_harness_run_id = (
                str(latest_run["harness_run_id"])
                if latest_run is not None
                else None
            )
            if actual_harness_run_id != expected_harness_run_id:
                raise RuntimeError(
                    "latest passed all-stage harness run changed before draft "
                    "registration: "
                    f"expected={expected_harness_run_id}, "
                    f"actual={actual_harness_run_id}"
                )
            version_conflicts = (
                conn.execute(
                    text(
                        f"""
                        /* draft registration version identity conflicts */
                        SELECT scheme_id, scheme_version, runtime_type, status
                        FROM t_scheme_versions
                        WHERE scheme_id IN ({identity_placeholders})
                        FOR UPDATE
                        """
                    ),
                    identity_params,
                )
                .mappings()
                .all()
            )
            registry_conflicts = (
                conn.execute(
                    text(
                        f"""
                        /* draft registration Registry identity conflicts */
                        SELECT scheme_id, base_scheme_id, runtime_type, status
                        FROM t_scheme_registry
                        WHERE scheme_id IN ({identity_placeholders})
                           OR base_scheme_id IN ({identity_placeholders})
                        FOR UPDATE
                        """
                    ),
                    identity_params,
                )
                .mappings()
                .all()
            )
            if version_conflicts or registry_conflicts:
                raise ValueError(
                    "Blackbox draft registration identity conflict: "
                    f"versions={len(version_conflicts)}, registry={len(registry_conflicts)}"
                )

            version_params = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "algorithm_version": cfg.algorithm_version,
                "contract_version": cfg.contract_version,
                "runtime_profile": cfg.runtime_profile,
                "environment_fingerprint": cfg.environment_fingerprint,
                "data_snapshot_id": cfg.data_snapshot_id,
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
                "manifest_hash": cfg.manifest_hash,
                "git_commit": None,
                "status": "draft",
                "created_by": "harness.draft-register",
            }
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_versions
                        (scheme_id, scheme_version, runtime_type, algorithm_version,
                         contract_version, runtime_profile, environment_fingerprint,
                         data_snapshot_id, code_hash, config_hash, manifest_hash,
                         git_commit, status, created_by, approved_by, approved_at)
                    VALUES
                        (:scheme_id, :scheme_version, :runtime_type, :algorithm_version,
                         :contract_version, :runtime_profile, :environment_fingerprint,
                         :data_snapshot_id, :code_hash, :config_hash, :manifest_hash,
                         :git_commit, :status, :created_by, NULL, NULL)
                    """
                ),
                version_params,
            )
            registry_rows = [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": cfg.scheme_id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "horizon": cfg.horizon,
                    "task_type": cfg.task_type,
                    "runtime_type": "blackbox_v2",
                    "tenors": json.dumps([target_tenor], ensure_ascii=False),
                    "frequency": cfg.frequency,
                    "target_tenor": target_tenor,
                    "schedule_cron": cfg.schedule.cron,
                    "schedule_timezone": cfg.schedule.timezone,
                    "status": "paused",
                    "deployed_at": None,
                }
                for target_tenor, registry_id in zip(
                    expected_tenors,
                    expected_registry_ids,
                )
            ]
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon,
                         task_type, runtime_type, tenors, frequency, target_tenor,
                         schedule_cron, schedule_timezone, status, deployed_at)
                    VALUES
                        (:scheme_id, :base_scheme_id, :name, :description, :horizon,
                         :task_type, :runtime_type, CAST(:tenors AS JSON), :frequency,
                         :target_tenor, :schedule_cron, :schedule_timezone, :status, NULL)
                    """
                ),
                registry_rows,
            )

            version_row = _read_scheme_version_conn(conn, cfg, for_update=True)
            if version_row is None:
                raise RuntimeError("Blackbox draft registration version readback is missing")
            expected_version = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "algorithm_version": cfg.algorithm_version,
                "contract_version": cfg.contract_version,
                "runtime_profile": cfg.runtime_profile,
                "environment_fingerprint": cfg.environment_fingerprint,
                "data_snapshot_id": cfg.data_snapshot_id,
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
                "manifest_hash": cfg.manifest_hash,
                "git_commit": None,
                "status": "draft",
                "created_by": "harness.draft-register",
                "approved_by": None,
                "approved_at": None,
            }
            version_mismatches = [
                f"{field}: expected={expected!r}, got={version_row.get(field)!r}"
                for field, expected in expected_version.items()
                if version_row.get(field) != expected
            ]
            if version_mismatches:
                raise RuntimeError(
                    "Blackbox draft registration version readback mismatch: "
                    + "; ".join(version_mismatches)
                )
            readback_registry = _read_scheme_registry_rows_conn(
                conn,
                cfg,
                expected_registry_ids,
                for_update=True,
            )
            registry_error = _registry_identity_error(
                cfg,
                expected_tenors,
                expected_registry_ids,
                readback_registry,
                expected_status="paused",
            )
            if registry_error is not None:
                raise RuntimeError(
                    f"Blackbox draft registration Registry readback mismatch: {registry_error}"
                )
            registry_by_id = {
                str(row["scheme_id"]): row
                for row in readback_registry
            }
            registry_mismatches: list[str] = []
            for expected_row in registry_rows:
                actual_row = registry_by_id[expected_row["scheme_id"]]
                expected_values = {
                    **expected_row,
                    "tenors": [expected_row["target_tenor"]],
                }
                actual_values = {
                    **actual_row,
                    "tenors": _normalize_registry_tenors(
                        actual_row.get("tenors")
                    ),
                }
                registry_mismatches.extend(
                    f"{expected_row['scheme_id']}.{field}: "
                    f"expected={expected!r}, got={actual_values.get(field)!r}"
                    for field, expected in expected_values.items()
                    if actual_values.get(field) != expected
                )
            if registry_mismatches:
                raise RuntimeError(
                    "Blackbox draft registration Registry readback mismatch: "
                    + "; ".join(registry_mismatches)
                )

    return BlackboxLifecycleState(
        scheme_id=str(version_row["scheme_id"]),
        scheme_version=str(version_row["scheme_version"]),
        runtime_type=str(version_row["runtime_type"]),
        version_status=str(version_row["status"]),
        registry_status="paused",
        environment_fingerprint=str(version_row["environment_fingerprint"]),
        data_snapshot_id=str(version_row["data_snapshot_id"]),
        code_hash=str(version_row["code_hash"]),
        config_hash=str(version_row["config_hash"]),
        manifest_hash=str(version_row["manifest_hash"]),
        approved_by=None,
        approved_at=None,
        registry_scheme_ids=expected_registry_ids,
    )


@contextmanager
def _blackbox_draft_register_advisory_lock(
    engine: Engine,
    *,
    scheme_id: str,
    timeout_sec: float,
) -> Iterator[None]:
    """用 scheme-scoped MySQL advisory lock 消除首次 absent-row 竞争。"""
    if timeout_sec < 0:
        raise ValueError("draft register lock_timeout_sec must be non-negative")
    scheme_digest = hashlib.sha256(scheme_id.encode("utf-8")).hexdigest()[:32]
    lock_name = f"bfl:bbv2-draft:{scheme_digest}"
    with engine.connect() as lock_conn:
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout_sec)"),
            {"lock_name": lock_name, "timeout_sec": float(timeout_sec)},
        ).scalar_one()
        if int(acquired or 0) != 1:
            raise BlackboxDraftRegisterLockTimeout(
                "timed out waiting for Blackbox draft registration advisory lock: "
                f"scheme_hash={scheme_digest} timeout_sec={timeout_sec:g}"
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
                        f"draft registration advisory lock release failed: {release_error}"
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


@contextmanager
def _blackbox_bootstrap_advisory_lock(
    engine: Engine,
    *,
    expected_schema: str,
    timeout_sec: float,
) -> Iterator[None]:
    """用连接级 MySQL advisory lock 串行化认证 Schema 初始化。"""
    if timeout_sec < 0:
        raise ValueError("bootstrap lock_timeout_sec must be non-negative")
    schema_digest = hashlib.sha256(expected_schema.encode("utf-8")).hexdigest()[:32]
    lock_name = f"bfl:bbv2-bootstrap:{schema_digest}"
    with engine.connect() as lock_conn:
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout_sec)"),
            {"lock_name": lock_name, "timeout_sec": float(timeout_sec)},
        ).scalar_one()
        if int(acquired or 0) != 1:
            raise BlackboxBootstrapLockTimeout(
                "timed out waiting for Blackbox bootstrap advisory lock: "
                f"schema_hash={schema_digest} timeout_sec={timeout_sec:g}"
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
                        "failed to release Blackbox bootstrap advisory lock: "
                        f"schema_hash={schema_digest}"
                    )
            except BaseException as release_error:
                if active_error is None:
                    raise
                if hasattr(active_error, "add_note"):
                    active_error.add_note(f"bootstrap advisory lock release failed: {release_error}")


def _validate_target_registry_baseline_conn(conn: Connection) -> dict[str, object]:
    rows = (
        conn.execute(
            text(
                "SELECT target_code, display_name, asset_class, target_type, "
                "sort_order, status, extra "
                "FROM t_target_registry "
                "ORDER BY sort_order, target_code FOR UPDATE"
            )
        )
        .mappings()
        .all()
    )
    expected = [
        dict(zip(_TARGET_REGISTRY_BUSINESS_FIELDS, values))
        for values in _TARGET_REGISTRY_BASELINE
    ]
    try:
        actual = [_normalize_target_registry_row(row) for row in rows]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BlackboxTargetRegistryBaselineError(
            {
                "status": "failed",
                "table": "t_target_registry",
                "baseline_version": _TARGET_REGISTRY_BASELINE_VERSION,
                "business_fields": list(_TARGET_REGISTRY_BUSINESS_FIELDS),
                "expected_count": len(expected),
                "actual_count": len(rows),
                "error": f"malformed row: {exc}",
            }
        ) from exc
    if actual != expected:
        raise BlackboxTargetRegistryBaselineError(
            _target_registry_baseline_diff(expected, actual)
        )
    return {
        "status": "passed",
        "table": "t_target_registry",
        "baseline_version": _TARGET_REGISTRY_BASELINE_VERSION,
        "business_fields": list(_TARGET_REGISTRY_BUSINESS_FIELDS),
        "row_count": len(actual),
        "target_codes": [str(row["target_code"]) for row in actual],
        "sha256": _target_registry_digest(actual),
    }


def _normalize_target_registry_row(row: Mapping[str, object]) -> dict[str, object]:
    extra = row.get("extra")
    if isinstance(extra, bytes):
        extra = extra.decode("utf-8")
    if isinstance(extra, str):
        extra = json.loads(extra)
    if not isinstance(extra, dict):
        raise TypeError(f"extra must be a JSON object, got {type(extra).__name__}")
    sort_order = int(row.get("sort_order"))
    return {
        "target_code": row.get("target_code"),
        "display_name": row.get("display_name"),
        "asset_class": row.get("asset_class"),
        "target_type": row.get("target_type"),
        "sort_order": sort_order,
        "status": row.get("status"),
        "extra": extra,
    }


def _target_registry_digest(rows: list[dict[str, object]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _target_registry_baseline_diff(
    expected: list[dict[str, object]],
    actual: list[dict[str, object]],
) -> dict[str, object]:
    expected_by_code = {str(row["target_code"]): row for row in expected}
    actual_by_code = {str(row["target_code"]): row for row in actual}
    actual_codes = [str(row["target_code"]) for row in actual]
    duplicate_codes = sorted(
        {code for code in actual_codes if actual_codes.count(code) > 1}
    )
    shared_codes = sorted(set(expected_by_code) & set(actual_by_code))
    changed = {
        code: {
            field: {
                "expected": expected_by_code[code][field],
                "actual": actual_by_code[code][field],
            }
            for field in _TARGET_REGISTRY_BUSINESS_FIELDS
            if expected_by_code[code][field] != actual_by_code[code][field]
        }
        for code in shared_codes
    }
    return {
        "status": "failed",
        "table": "t_target_registry",
        "baseline_version": _TARGET_REGISTRY_BASELINE_VERSION,
        "business_fields": list(_TARGET_REGISTRY_BUSINESS_FIELDS),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "expected_sha256": _target_registry_digest(expected),
        "actual_sha256": _target_registry_digest(actual),
        "missing_target_codes": sorted(set(expected_by_code) - set(actual_by_code)),
        "extra_target_codes": sorted(set(actual_by_code) - set(expected_by_code)),
        "duplicate_target_codes": duplicate_codes,
        "changed": {code: fields for code, fields in changed.items() if fields},
    }


def sync_scheme_registry(engine: Engine, schemes: Iterable[SchemeConfig]) -> None:
    """将配置文件中的方案元数据同步到 t_scheme_registry。"""
    scheme_list = list(schemes)
    if not scheme_list:
        return
    with engine.begin() as conn:
        effective_statuses: dict[str, str] = {}
        syncable_schemes: list[SchemeConfig] = []
        for cfg in scheme_list:
            runtime_type = getattr(cfg, "runtime_type", "native_adapter")
            if (
                runtime_type == "blackbox_v2"
                and _blackbox_identity_requires_controlled_revision_conn(conn, cfg)
            ):
                # Registry 不存 exact version。若当前 active config 的 exact candidate
                # 尚未登记、但同一业务身份已经存在，通用 discovery 若继续 upsert 会把
                # 现有 active Registry 降为 paused。此处保留已上线身份，要求走
                # revision-activate 的受控单事务切换。
                continue
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
                effective_status = (
                    "active"
                    if _native_sync_can_grandfather_active(
                        conn,
                        cfg,
                        version_row,
                    )
                    else "paused"
                )
            for target_tenor in cfg.tenors:
                effective_statuses[
                    registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
                ] = effective_status
            syncable_schemes.append(cfg)
        if syncable_schemes:
            _sync_scheme_registry_conn(
                conn,
                syncable_schemes,
                effective_statuses=effective_statuses,
            )


def _blackbox_identity_requires_controlled_revision_conn(
    conn: Connection,
    cfg: SchemeConfig,
) -> bool:
    """识别不能由 discovery 自动登记的 Blackbox 同身份修订。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        return False
    scheme_version = getattr(cfg, "scheme_version", None)
    if not isinstance(scheme_version, str) or not scheme_version.strip():
        return False
    if _read_scheme_version_conn(conn, cfg, for_update=True) is not None:
        return False

    version_rows = _read_scheme_version_rows_for_base_conn(
        conn,
        cfg.scheme_id,
        for_update=True,
    )
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    del expected_tenors
    placeholders = ", ".join(
        f":registry_scheme_id_{index}"
        for index, _ in enumerate(expected_registry_ids)
    )
    registry_params: dict[str, object] = {"base_scheme_id": cfg.scheme_id}
    for index, registry_scheme_id_value in enumerate(expected_registry_ids):
        registry_params[f"registry_scheme_id_{index}"] = registry_scheme_id_value
    lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
    registry_rows = (
        conn.execute(
            text(
                "SELECT scheme_id FROM t_scheme_registry "
                "WHERE base_scheme_id = :base_scheme_id "
                f"OR scheme_id IN ({placeholders}){lock_clause}"
            ),
            registry_params,
        )
        .mappings()
        .all()
    )
    return bool(version_rows or registry_rows)


def _native_sync_can_grandfather_active(
    conn: Connection,
    cfg: SchemeConfig,
    version_row: Mapping[str, object] | None,
) -> bool:
    """仅在 pre-sync 精确版本和完整 Registry 均 active 时保留 Native active。"""
    if (
        cfg.status != "active"
        or version_row is None
        or version_row.get("scheme_id") != cfg.scheme_id
        or version_row.get("scheme_version") != cfg.scheme_version
        or version_row.get("runtime_type") != "native_adapter"
        or version_row.get("status") != "active"
    ):
        return False
    try:
        expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
        registry_rows = _read_scheme_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=True,
        )
    except (TypeError, ValueError):
        return False
    return (
        _registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status="active",
            expected_runtime_type="native_adapter",
        )
        is None
    )


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
    """锁定并同步发现版本；普通发现不得提升 Native/Blackbox 生命周期。"""
    existing = _read_scheme_version_conn(conn, cfg, for_update=True)
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if existing is not None and (
        runtime_type == "blackbox_v2" or existing.get("runtime_type") == "blackbox_v2"
    ):
        _raise_on_blackbox_version_metadata_conflict(existing, cfg)
    if (
        existing is not None
        and runtime_type == "native_adapter"
        and existing.get("runtime_type") != "native_adapter"
    ):
        raise ValueError(
            "Native version runtime_type mismatch for "
            f"{cfg.scheme_id}/{cfg.scheme_version}: "
            f"stored={existing.get('runtime_type')!r}"
        )
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
    """在调用方事务中写入精确版本；只有 trusted_status 可提升生命周期。"""
    if trusted_status is not None and trusted_status not in VERSION_STATUSES:
        raise ValueError(f"invalid trusted scheme version status: {trusted_status}")
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if trusted_status is not None:
        status = trusted_status
    else:
        status = "draft"
    preserve_lifecycle = (
        runtime_type in {"native_adapter", "blackbox_v2"}
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
    expected_harness_run_id: str,
    approved_by: str,
    approved_at: datetime,
    lock_timeout_sec: float = 5.0,
) -> BlackboxLifecycleState:
    """原子替换同一业务身份的唯一 active Blackbox exact version。"""
    _validate_blackbox_revision_candidate(cfg, require_evidence=True)
    if not isinstance(prior_scheme_version, str) or not prior_scheme_version.strip():
        raise ValueError("Blackbox revision activation requires prior_scheme_version")
    if prior_scheme_version == cfg.scheme_version:
        raise ValueError("Blackbox revision activation prior version must differ from candidate")
    if (
        not isinstance(expected_harness_run_id, str)
        or not expected_harness_run_id.strip()
    ):
        raise ValueError(
            "Blackbox revision activation requires expected_harness_run_id"
        )
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("Blackbox revision activation requires non-empty approved_by")
    if not isinstance(approved_at, datetime):
        raise ValueError("Blackbox revision activation requires approved_at datetime")

    normalized_approver = approved_by.strip()
    mysql_approved_at = _mysql_utc_datetime(approved_at)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    with _blackbox_draft_register_advisory_lock(
        engine,
        scheme_id=cfg.scheme_id,
        timeout_sec=lock_timeout_sec,
    ):
        with engine.begin() as conn:
            lock_clause = " FOR UPDATE" if _dialect_name(conn) != "sqlite" else ""
            latest_run = (
                conn.execute(
                    text(
                        "SELECT harness_run_id FROM t_harness_runs "
                        "WHERE scheme_id = :scheme_id "
                        "AND scheme_version = :scheme_version "
                        "AND stage = 'all' AND status = 'passed' "
                        "ORDER BY finished_at DESC, harness_run_id DESC "
                        f"LIMIT 1{lock_clause}"
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "scheme_version": cfg.scheme_version,
                    },
                )
                .mappings()
                .one_or_none()
            )
            actual_harness_run_id = (
                str(latest_run["harness_run_id"])
                if latest_run is not None
                else None
            )
            if actual_harness_run_id != expected_harness_run_id:
                raise RuntimeError(
                    "latest passed all-stage harness run changed before Blackbox "
                    "revision activation: "
                    f"expected={expected_harness_run_id}, "
                    f"actual={actual_harness_run_id}"
                )

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
                expected_status="active",
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
    if getattr(cfg, "status", None) != "active" or getattr(
        cfg, "version_status", None
    ) != "active":
        raise ValueError(
            "Blackbox revision activation requires config active+active: "
            f"got={getattr(cfg, 'status', None)}+{getattr(cfg, 'version_status', None)}"
        )
    scheme_version = getattr(cfg, "scheme_version", None)
    if not isinstance(scheme_version, str) or not scheme_version.strip():
        raise ValueError("Blackbox revision activation requires non-empty scheme_version")
    if require_evidence:
        for field in ("environment_fingerprint", "data_snapshot_id"):
            value = getattr(cfg, field, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "Blackbox revision activation requires passed all-stage "
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
    if pending_versions:
        raise ValueError(
            "Blackbox revision identity has pending exact versions: "
            f"{pending_versions}"
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
        expected_status="active",
    )
    if registry_error is not None:
        raise ValueError(
            "Blackbox revision activation Registry identity is not active/exact: "
            f"{registry_error}"
        )
    return BlackboxRevisionActivationPreflight(
        prior_scheme_version=prior_version,
        registry_scheme_ids=expected_registry_ids,
    )


def _blackbox_revision_registry_identity_error(
    cfg: SchemeConfig,
    expected_tenors: tuple[str, ...],
    expected_registry_ids: tuple[str, ...],
    registry_rows: list[Mapping[str, object]],
    *,
    expected_status: str,
) -> str | None:
    """补充 revision 需要锁定的 cadence 与单 target Registry 身份。"""
    error = _registry_identity_error(
        cfg,
        expected_tenors,
        expected_registry_ids,
        registry_rows,
        expected_status=expected_status,
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
    if getattr(cfg, "status", None) != "active":
        return denied(f"config status is {getattr(cfg, 'status', None)}, expected active")
    if getattr(cfg, "version_status", None) != "active":
        return denied(
            "config version_status is "
            f"{getattr(cfg, 'version_status', None)}, expected active"
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

    mysql_approved_at = _mysql_utc_datetime(approved_at)
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    effective_statuses = {
        registry_id: registry_status for registry_id in expected_registry_ids
    }
    with engine.begin() as conn:
        _upsert_scheme_version_conn(
            conn,
            cfg,
            trusted_status=version_status,
            approved_by=approved_by,
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
                f"trusted lifecycle version readback missing: {cfg.scheme_id}/{cfg.scheme_version}"
            )
        actual_version_values = dict(version_row)
        stored_approved_at = actual_version_values.get("approved_at")
        if isinstance(stored_approved_at, datetime):
            actual_version_values["approved_at"] = _mysql_utc_datetime(stored_approved_at)
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
            "approved_at": mysql_approved_at,
        }
        mismatches = [
            f"{field}: expected={expected!r}, got={actual_version_values.get(field)!r}"
            for field, expected in expected_version_values.items()
            if actual_version_values.get(field) != expected
        ]
        if mismatches:
            raise RuntimeError("trusted lifecycle version readback mismatch: " + "; ".join(mismatches))
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
            actual_version_values["approved_at"]
            if isinstance(actual_version_values.get("approved_at"), datetime)
            else None
        ),
    )


def _mysql_utc_datetime(value: datetime | None) -> datetime | None:
    """将批准时刻统一为 MySQL DATETIME(0) 使用的无时区 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _normalize_input_generation_registration(
    *,
    generation_id: str,
    generation_type: str,
    business_date: str,
    feature_date: str,
    readiness_basis: str,
    source_commit_token: str,
    dataset_content_id: str,
    schema_version: str,
    exporter_version: str,
    manifest_uri: str,
    manifest_sha256: str,
    native_generation_id: str | None = None,
    native_manifest_sha256: str | None = None,
) -> dict[str, object]:
    """规范化 generation 的不可变登记身份。"""
    if generation_type not in _GENERATION_TYPES:
        raise ValueError(
            f"generation_type must be one of {sorted(_GENERATION_TYPES)}"
        )
    if readiness_basis not in _READINESS_BASES:
        raise ValueError(
            f"readiness_basis must be one of {sorted(_READINESS_BASES)}"
        )
    if generation_type == "databridge_v1":
        if native_generation_id is None:
            raise ValueError(
                "native_generation_id is required for databridge_v1"
            )
        if native_manifest_sha256 is None:
            raise ValueError(
                "native_manifest_sha256 is required for databridge_v1"
            )
    elif (
        native_generation_id is not None
        or native_manifest_sha256 is not None
    ):
        raise ValueError(
            "native generation relation is only valid for databridge_v1"
        )
    return {
        "generation_id": _require_nonempty(generation_id, "generation_id"),
        "generation_type": _require_nonempty(
            generation_type,
            "generation_type",
        ),
        "business_date": date.fromisoformat(business_date).isoformat(),
        "feature_date": date.fromisoformat(feature_date).isoformat(),
        "readiness_basis": _require_nonempty(
            readiness_basis,
            "readiness_basis",
        ),
        "source_commit_token": _require_opaque_id(
            source_commit_token,
            "source_commit_token",
        ),
        "dataset_content_id": _require_opaque_id(
            dataset_content_id,
            "dataset_content_id",
        ),
        "schema_version": _require_nonempty(
            schema_version,
            "schema_version",
        ),
        "exporter_version": _require_nonempty(
            exporter_version,
            "exporter_version",
        ),
        "manifest_uri": _require_nonempty(manifest_uri, "manifest_uri"),
        "manifest_sha256": _require_sha256(
            manifest_sha256,
            "manifest_sha256",
        ),
        "native_generation_id": (
            _require_nonempty(
                native_generation_id,
                "native_generation_id",
            )
            if native_generation_id is not None
            else None
        ),
        "native_manifest_sha256": (
            _require_sha256(
                native_manifest_sha256,
                "native_manifest_sha256",
            )
            if native_manifest_sha256 is not None
            else None
        ),
    }


def _create_or_match_input_generation_conn(
    conn: Connection,
    params: Mapping[str, object],
) -> Mapping[str, object]:
    """在调用方事务内恢复同 ID BUILDING，拒绝任何 provenance 漂移。"""
    building_lock = (
        ""
        if _dialect_name(conn) == "sqlite"
        else " FOR UPDATE"
    )
    unfinished = (
        conn.execute(
            text(
                """
                SELECT generation_id
                FROM t_input_generations
                WHERE business_date = :business_date
                  AND generation_type = :generation_type
                  AND state = :building
                ORDER BY generation_id
                """
                + building_lock
            ),
            {
                "business_date": params["business_date"],
                "generation_type": params["generation_type"],
                "building": INPUT_GENERATION_BUILDING,
            },
        )
        .scalars()
        .all()
    )
    conflicting_unfinished = sorted(
        str(value)
        for value in unfinished
        if str(value) != str(params["generation_id"])
    )
    if conflicting_unfinished:
        raise RuntimeError(
            "unfinished input generation blocks same-day replacement: "
            f"type={params['generation_type']} "
            f"business_date={params['business_date']} "
            f"existing={conflicting_unfinished}"
        )
    insert_prefix = (
        "INSERT OR IGNORE"
        if _dialect_name(conn) == "sqlite"
        else "INSERT IGNORE"
    )
    conn.execute(
        text(
            f"""
            {insert_prefix} INTO t_input_generations
                (generation_id, generation_type, business_date, feature_date,
                 readiness_basis, source_commit_token, dataset_content_id,
                 schema_version, exporter_version, manifest_uri,
                 manifest_sha256, native_generation_id,
                 native_manifest_sha256, state)
            VALUES
                (:generation_id, :generation_type, :business_date,
                 :feature_date, :readiness_basis, :source_commit_token,
                 :dataset_content_id, :schema_version, :exporter_version,
                 :manifest_uri, :manifest_sha256, :native_generation_id,
                 :native_manifest_sha256, '{INPUT_GENERATION_BUILDING}')
            """
        ),
        dict(params),
    )
    row = _read_input_generation_conn(
        conn,
        str(params["generation_id"]),
        for_update=True,
    )
    if row is None:
        raise RuntimeError(
            "input generation insert/readback missing: "
            f"{params['generation_id']}"
        )
    mismatches = {
        field: (params[field], row.get(field))
        for field in params
        if str(row.get(field)) != str(params[field])
    }
    if mismatches:
        raise RuntimeError(
            f"input generation immutable provenance mismatch: {mismatches}"
        )
    return row


@contextmanager
def _gray_gap_native_registration_lock(
    engine: Engine,
    *,
    feature_date: str,
    timeout_sec: float,
) -> Iterator[None]:
    """序列化同 feature current-snapshot authority 的首次登记。"""
    if timeout_sec < 0:
        raise ValueError(
            "gray-gap Native registration lock timeout must be non-negative"
        )
    if engine.dialect.name != "mysql":
        yield
        return
    digest = hashlib.sha256(feature_date.encode("ascii")).hexdigest()[:32]
    lock_name = f"bfl:gray-native:{digest}"
    with engine.connect() as lock_conn:
        acquired = lock_conn.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout_sec)"),
            {
                "lock_name": lock_name,
                "timeout_sec": float(timeout_sec),
            },
        ).scalar_one()
        if int(acquired or 0) != 1:
            raise RuntimeError(
                "timed out waiting for gray-gap Native registration lock"
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
                        "failed to release gray-gap Native registration lock"
                    )
            except BaseException as release_error:
                if active_error is None:
                    raise
                if hasattr(active_error, "add_note"):
                    active_error.add_note(
                        "gray-gap Native registration lock release failed: "
                        f"{release_error}"
                    )


def register_sealed_gray_gap_native_generation(
    engine: Engine,
    *,
    generation_id: str,
    generation_type: str,
    business_date: str,
    feature_date: str,
    readiness_basis: str,
    source_commit_token: str,
    dataset_content_id: str,
    schema_version: str,
    exporter_version: str,
    manifest_uri: str,
    manifest_sha256: str,
    historical_predict_date: str,
    lock_timeout_sec: float = 5.0,
) -> str:
    """原子登记 gray-gap current snapshot，不创建任何调度对象。"""
    params = _normalize_input_generation_registration(
        generation_id=generation_id,
        generation_type=generation_type,
        business_date=business_date,
        feature_date=feature_date,
        readiness_basis=readiness_basis,
        source_commit_token=source_commit_token,
        dataset_content_id=dataset_content_id,
        schema_version=schema_version,
        exporter_version=exporter_version,
        manifest_uri=manifest_uri,
        manifest_sha256=manifest_sha256,
    )
    normalized_predict_date = date.fromisoformat(
        historical_predict_date
    ).isoformat()
    if params["generation_type"] != "native_source":
        raise ValueError(
            "gray-gap standalone registration only accepts native_source"
        )
    if params["readiness_basis"] != "CLOCK_CONTRACT":
        raise ValueError(
            "gray-gap Native readiness_basis must be CLOCK_CONTRACT"
        )
    if (
        params["exporter_version"]
        != SIGNAL_GAP_NATIVE_EXPORTER_VERSION
    ):
        raise ValueError(
            "gray-gap Native exporter_version is invalid"
        )
    if str(params["feature_date"]) >= normalized_predict_date:
        raise ValueError(
            "gray-gap Native feature_date must precede historical predict_date"
        )
    if str(params["business_date"]) <= normalized_predict_date:
        raise ValueError(
            "gray-gap Native capture date must be after historical predict_date"
        )
    return _register_sealed_standalone_native_generation(
        engine,
        params=params,
        lock_timeout_sec=lock_timeout_sec,
        authority_label="gray-gap",
    )


def register_sealed_archived_native_generation(
    engine: Engine,
    *,
    generation_id: str,
    generation_type: str,
    business_date: str,
    feature_date: str,
    readiness_basis: str,
    source_commit_token: str,
    dataset_content_id: str,
    schema_version: str,
    exporter_version: str,
    manifest_uri: str,
    manifest_sha256: str,
    historical_predict_date: str,
    lock_timeout_sec: float = 5.0,
) -> str:
    """原子登记当日已封存 Native generation，不创建任何调度对象。"""
    params = _normalize_input_generation_registration(
        generation_id=generation_id,
        generation_type=generation_type,
        business_date=business_date,
        feature_date=feature_date,
        readiness_basis=readiness_basis,
        source_commit_token=source_commit_token,
        dataset_content_id=dataset_content_id,
        schema_version=schema_version,
        exporter_version=exporter_version,
        manifest_uri=manifest_uri,
        manifest_sha256=manifest_sha256,
    )
    normalized_predict_date = date.fromisoformat(
        historical_predict_date
    ).isoformat()
    if params["generation_type"] != "native_source":
        raise ValueError(
            "archived standalone registration only accepts native_source"
        )
    if params["readiness_basis"] != "CLOCK_CONTRACT":
        raise ValueError(
            "archived Native readiness_basis must be CLOCK_CONTRACT"
        )
    if (
        params["exporter_version"]
        != NATIVE_GENERATION_EXPORTER_VERSION
    ):
        raise ValueError(
            "archived Native exporter_version is invalid"
        )
    if str(params["feature_date"]) >= normalized_predict_date:
        raise ValueError(
            "archived Native feature_date must precede historical predict_date"
        )
    if str(params["business_date"]) != normalized_predict_date:
        raise ValueError(
            "archived Native business_date must equal historical predict_date"
        )
    return _register_sealed_standalone_native_generation(
        engine,
        params=params,
        lock_timeout_sec=lock_timeout_sec,
        authority_label="archived",
    )


def _register_sealed_standalone_native_generation(
    engine: Engine,
    *,
    params: Mapping[str, object],
    lock_timeout_sec: float,
    authority_label: str,
) -> str:
    """登记并原子封存一个独立 Native authority。"""
    with _gray_gap_native_registration_lock(
        engine,
        feature_date=str(params["feature_date"]),
        timeout_sec=lock_timeout_sec,
    ):
        with engine.begin() as conn:
            suffix = (
                ""
                if _dialect_name(conn) == "sqlite"
                else " FOR UPDATE"
            )
            existing = (
                conn.execute(
                    text(
                        """
                        SELECT generation_id
                        FROM t_input_generations
                        WHERE generation_type = 'native_source'
                          AND feature_date = :feature_date
                          AND exporter_version = :exporter_version
                          AND state IN (:building, :sealed)
                        ORDER BY generation_id
                        """
                        + suffix
                    ),
                    {
                        "feature_date": params["feature_date"],
                        "exporter_version": params["exporter_version"],
                        "building": INPUT_GENERATION_BUILDING,
                        "sealed": INPUT_GENERATION_SEALED,
                    },
                )
                .scalars()
                .all()
            )
            conflicts = sorted(
                str(value)
                for value in existing
                if str(value) != str(params["generation_id"])
            )
            if conflicts:
                raise RuntimeError(
                    f"{authority_label} feature must have one unique "
                    "SEALED authority: "
                    f"feature_date={params['feature_date']} "
                    f"existing={conflicts}"
                )
            row = _create_or_match_input_generation_conn(
                conn,
                params,
            )
            state = str(row.get("state"))
            if state == INPUT_GENERATION_SEALED:
                if row.get("sealed_at") is None:
                    raise RuntimeError(
                        f"SEALED {authority_label} Native authority has no "
                        "sealed_at"
                    )
                return str(params["generation_id"])
            if state != INPUT_GENERATION_BUILDING:
                raise RuntimeError(
                    f"{authority_label} Native authority cannot be sealed from "
                    f"state={state}"
                )
            sealed_at = _database_or_local_utc_now(conn)
            result = conn.execute(
                text(
                    """
                    UPDATE t_input_generations
                    SET state = :sealed,
                        sealed_at = :sealed_at,
                        updated_at = :sealed_at
                    WHERE generation_id = :generation_id
                      AND state = :building
                    """
                ),
                {
                    "sealed": INPUT_GENERATION_SEALED,
                    "sealed_at": sealed_at,
                    "generation_id": params["generation_id"],
                    "building": INPUT_GENERATION_BUILDING,
                },
            )
            _require_rowcount(
                result,
                1,
                f"{authority_label} Native atomic seal",
            )
    return str(params["generation_id"])


def read_sealed_input_generation(
    engine: Engine,
    *,
    generation_id: str,
    expected_generation_type: str,
) -> InputGenerationEnvelope:
    """读取一个已 SEALED generation 的完整不可变 DB fence。"""
    if expected_generation_type not in _GENERATION_TYPES:
        raise ValueError(
            "expected_generation_type must be one of "
            f"{sorted(_GENERATION_TYPES)}"
        )
    normalized_generation_id = _require_nonempty(
        generation_id,
        "generation_id",
    )
    with engine.begin() as conn:
        row = _read_input_generation_conn(
            conn,
            normalized_generation_id,
            for_update=False,
        )
    if row is None:
        raise RuntimeError(
            f"input generation not found: {normalized_generation_id}"
        )
    if str(row.get("generation_type")) != expected_generation_type:
        raise RuntimeError(
            "input generation type differs from expected DB fence: "
            f"{normalized_generation_id}"
        )
    if str(row.get("state")) != INPUT_GENERATION_SEALED:
        raise RuntimeError(
            "input generation DB fence is not SEALED: "
            f"{normalized_generation_id}"
        )
    if row.get("sealed_at") is None:
        raise RuntimeError(
            "SEALED input generation DB fence has no sealed_at: "
            f"{normalized_generation_id}"
        )
    return _input_generation_envelope(row)


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


def _require_opaque_id(value: str, field: str) -> str:
    """校验上游提交或内容标识，不假定其一定是裸 SHA256。"""
    return _require_bounded_identifier(value, field, max_length=128)


def _require_bounded_identifier(
    value: str,
    field: str,
    *,
    max_length: int,
) -> str:
    normalized = _require_nonempty(value, field)
    if len(normalized) > max_length or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/@+,\-=]*",
        normalized,
    ) is None:
        raise ValueError(
            f"{field} must be at most {max_length} safe identifier characters"
        )
    return normalized


def _utc_datetime6(value: datetime | None) -> datetime:
    effective = value or datetime.now(timezone.utc)
    if effective.tzinfo is not None:
        effective = effective.astimezone(timezone.utc).replace(tzinfo=None)
    return effective


def _database_or_local_utc_now(conn: Connection) -> datetime:
    """封存时优先使用 MySQL UTC 时钟，SQLite 测试使用本地 UTC。"""
    if _dialect_name(conn) == "mysql":
        observed = conn.execute(text("SELECT UTC_TIMESTAMP(6)")).scalar_one()
        return _utc_datetime6(
            observed
            if isinstance(observed, datetime)
            else datetime.fromisoformat(str(observed))
        )
    return _utc_datetime6(None)


def _dialect_name(conn: Connection) -> str:
    return str(getattr(getattr(conn, "dialect", None), "name", "mysql"))


def _select_mapping_one_or_none(
    conn: Connection,
    sql: str,
    params: Mapping[str, object],
    *,
    for_update: bool,
) -> Mapping[str, object] | None:
    lock = "" if not for_update or _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    return (
        conn.execute(text(sql + lock), dict(params))
        .mappings()
        .one_or_none()
    )


def _read_input_generation_conn(
    conn: Connection,
    generation_id: str,
    *,
    for_update: bool,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT generation_id, generation_type, business_date, feature_date,
               readiness_basis, source_commit_token, dataset_content_id,
               schema_version, exporter_version, manifest_uri,
               manifest_sha256, native_generation_id,
               native_manifest_sha256, state, sealed_at,
               invalidated_at, invalid_reason
        FROM t_input_generations
        WHERE generation_id = :generation_id
        """,
        {"generation_id": generation_id},
        for_update=for_update,
    )


def _input_generation_envelope(
    row: Mapping[str, object],
) -> InputGenerationEnvelope:
    return InputGenerationEnvelope(
        generation_id=_stored_text(row, "generation_id"),
        generation_type=_stored_text(row, "generation_type"),
        business_date=_stored_iso_date(row, "business_date"),
        feature_date=_stored_iso_date(row, "feature_date"),
        readiness_basis=_stored_text(row, "readiness_basis"),
        source_commit_token=_stored_text(row, "source_commit_token"),
        dataset_content_id=_stored_text(row, "dataset_content_id"),
        schema_version=_stored_text(row, "schema_version"),
        exporter_version=_stored_text(row, "exporter_version"),
        manifest_uri=_stored_text(row, "manifest_uri"),
        manifest_sha256=_stored_text(row, "manifest_sha256"),
        native_generation_id=_optional_stored_text(
            row.get("native_generation_id")
        ),
        native_manifest_sha256=_optional_stored_text(
            row.get("native_manifest_sha256")
        ),
        state=_stored_text(row, "state"),
        sealed_at=_optional_stored_datetime(row.get("sealed_at"), "sealed_at"),
        invalidated_at=_optional_stored_datetime(
            row.get("invalidated_at"), "invalidated_at"
        ),
        invalid_reason=_optional_stored_text(row.get("invalid_reason")),
    )


def _read_scheme_run_conn(
    conn: Connection,
    *,
    run_id: int,
    for_update: bool,
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
        for_update=for_update,
    )


def _require_rowcount(result: object, expected: int, operation: str) -> None:
    actual = int(getattr(result, "rowcount", 0) or 0)
    if actual != expected:
        raise RuntimeError(
            f"{operation} affected {actual} rows, expected {expected}"
        )


def _as_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc_datetime6(value)
    if isinstance(value, str):
        try:
            return _utc_datetime6(datetime.fromisoformat(value))
        except ValueError as exc:
            raise RuntimeError(f"invalid stored {field}: {value!r}") from exc
    raise RuntimeError(f"invalid stored {field}: {value!r}")


def _optional_stored_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _as_datetime(value, field)


def _stored_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or not str(value).strip():
        raise RuntimeError(f"invalid stored {field}: {value!r}")
    return str(value)


def _optional_stored_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


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


def _stored_json_mapping(
    row: Mapping[str, object],
    field: str,
) -> Mapping[str, object]:
    value = row.get(field)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid stored {field}: malformed JSON") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"invalid stored {field}: expected JSON object")
    return dict(value)


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
    scheduled_control_plane: str | None = None,
) -> int:
    """创建正常预测运行；自然写入只接受 launchd one-shot 身份。"""
    if (
        prediction_phase is not None
        and prediction_phase not in VALID_PREDICTION_PHASES
    ):
        raise ValueError(
            "prediction_phase must be one of "
            f"{sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}"
        )
    if scheduled_control_plane not in {None, LAUNCHD_ONE_SHOT}:
        raise ValueError(
            "scheduled_control_plane must be launchd_one_shot when set"
        )
    if scheduled_control_plane is not None:
        if prediction_phase != "scheduled_live":
            raise ValueError(
                "scheduled_control_plane requires scheduled_live"
            )
    if (
        prediction_phase == "scheduled_live"
        and scheduled_control_plane != LAUNCHD_ONE_SHOT
    ):
        raise RuntimeError(
            "scheduled_live requires launchd_one_shot"
        )
    with engine.begin() as conn:
        return _create_scheme_run_conn(
            conn,
            scheme_id=scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            run_type=run_type,
            prediction_phase=prediction_phase,
            status=status,
            harness_run_id=harness_run_id,
            input_artifact_id=input_artifact_id,
            data_snapshot_id=data_snapshot_id,
            records_expected=records_expected,
        )


def _create_scheme_run_conn(
    conn: Connection,
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
    """在调用方事务内创建不带遗留 schedule linkage 的运行。"""
    result = conn.execute(
        text(
            """
            INSERT INTO t_scheme_runs
                (scheme_id, scheme_version, runtime_type, run_type,
                 prediction_phase, predict_date, status, harness_run_id,
                 input_artifact_id, data_snapshot_id, records_expected)
            VALUES
                (:scheme_id, :scheme_version, :runtime_type, :run_type,
                 :prediction_phase, :predict_date, :status, :harness_run_id,
                 :input_artifact_id, :data_snapshot_id, :records_expected)
            """
        ),
        {
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
    finished_at: datetime | None = None,
    require_exact_run: bool = False,
) -> None:
    """在调用方事务中标记预测运行结束。"""
    sql = text(
        """
        UPDATE t_scheme_runs
        SET status = :status,
            finished_at = COALESCE(:finished_at, CURRENT_TIMESTAMP),
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
            "finished_at": finished_at,
        },
    )
    if require_exact_run and int(getattr(result, "rowcount", 0) or 0) != 1:
        raise RuntimeError(
            "scheme run update affected "
            f"{int(getattr(result, 'rowcount', 0) or 0)} rows for run_id={run_id}"
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
    precommit_validator: Callable[[Connection], None] | None = None,
    insert_only_predictions: bool = False,
) -> int:
    """原子提交 Blackbox prediction、成功 run 状态与成功日志。"""
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
            for_update=True,
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
        run_identity_errors = []
        if run is None:
            run_identity_errors.append("run missing")
        else:
            for field, expected_value in expected_run_identity.items():
                actual_value = run.get(field)
                if field == "predict_date":
                    actual_value = _stored_iso_date(run, field)
                if field == "records_expected" and actual_value is not None:
                    try:
                        actual_value = int(actual_value)
                    except (TypeError, ValueError):
                        pass
                if actual_value != expected_value:
                    run_identity_errors.append(
                        f"{field}: expected={expected_value!r}, "
                        f"got={actual_value!r}"
                    )
        if run_identity_errors:
            raise RuntimeError(
                "Blackbox completion running run identity revalidation failed: "
                + "; ".join(run_identity_errors)
            )
        assert run is not None
        run_phase = run.get("prediction_phase")
        if run_phase not in VALID_PREDICTION_PHASES:
            raise RuntimeError(
                "Blackbox completion running run identity revalidation failed: "
                f"invalid prediction_phase={run_phase!r}"
            )
        if records_returned != len(record_list):
            raise RuntimeError(
                "Blackbox completion records_returned mismatch: "
                f"returned={records_returned}, records={len(record_list)}"
            )
        returned_targets = Counter(
            (str(record.target_tenor), int(record.horizon))
            for record in record_list
        )
        duplicate_targets = sorted(
            (target_tenor, horizon, count)
            for (target_tenor, horizon), count in returned_targets.items()
            if count != 1
        )
        record_errors = []
        if set(returned_targets) != expected_targets or duplicate_targets:
            record_errors.append(
                "target set does not match active Registry: "
                f"expected={sorted(expected_targets)}, "
                f"returned={sorted(returned_targets)}, "
                f"duplicates={duplicate_targets}"
            )
        for record in record_list:
            if record.scheme_id != cfg.scheme_id:
                record_errors.append(f"record scheme_id={record.scheme_id!r}")
            if record.scheme_version not in (None, exact_scheme_version):
                record_errors.append(
                    f"record scheme_version={record.scheme_version!r}"
                )
            if str(record.predict_date) != normalized_run_date:
                record_errors.append(
                    f"record predict_date={record.predict_date!r}"
                )
            record_phase = record.prediction_phase or (
                record.extra or {}
            ).get("prediction_phase")
            if record_phase != run_phase:
                record_errors.append(
                    f"record prediction_phase={record_phase!r}"
                )
        if record_errors:
            raise RuntimeError(
                "Blackbox completion records revalidation failed: "
                + "; ".join(record_errors)
            )
        records_written = _insert_run_predictions_conn(
            conn,
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
            insert_only=insert_only_predictions,
        )
        if records_written != records_returned:
            raise RuntimeError(
                "Blackbox completion records_written mismatch: "
                f"returned={records_returned}, written={records_written}"
            )
        _finish_scheme_run_conn(
            conn,
            run_id=run_id,
            status="success",
            records_returned=records_returned,
            records_written=records_written,
            error_message=None,
            require_exact_run=True,
        )
        _write_run_log_conn(
            conn,
            cfg.scheme_id,
            normalized_run_date,
            "success",
            duration_sec,
            None,
            run_id,
        )
        if precommit_validator is not None:
            precommit_validator(conn)
        return records_written


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
            for_update=True,
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
        run_identity_errors = []
        if run is None:
            run_identity_errors.append("run missing")
        else:
            for field, expected_value in expected_run_identity.items():
                actual_value = run.get(field)
                if field == "predict_date":
                    actual_value = _stored_iso_date(run, field)
                if field == "records_expected" and actual_value is not None:
                    try:
                        actual_value = int(actual_value)
                    except (TypeError, ValueError):
                        pass
                if actual_value != expected_value:
                    run_identity_errors.append(
                        f"{field}: expected={expected_value!r}, got={actual_value!r}"
                    )
        if run_identity_errors:
            raise RuntimeError(
                "running run identity revalidation failed: "
                + "; ".join(run_identity_errors)
            )
        assert run is not None
        run_phase = run.get("prediction_phase")
        if run_phase not in VALID_PREDICTION_PHASES:
            raise RuntimeError(
                "running run identity revalidation failed: invalid "
                f"prediction_phase={run_phase!r}"
            )
        returned_targets = Counter(
            (str(record.target_tenor), int(record.horizon))
            for record in record_list
        )
        duplicate_targets = sorted(
            (target_tenor, horizon, count)
            for (target_tenor, horizon), count in returned_targets.items()
            if count != 1
        )
        record_errors = []
        if set(returned_targets) != expected_targets or duplicate_targets:
            record_errors.append(
                "target set does not match active Registry: "
                f"expected={sorted(expected_targets)}, "
                f"returned={sorted(returned_targets)}, "
                f"duplicates={duplicate_targets}"
            )
        for record in record_list:
            if record.scheme_id != cfg.scheme_id:
                record_errors.append(
                    f"record scheme_id={record.scheme_id!r}"
                )
            if record.scheme_version not in (None, exact_scheme_version):
                record_errors.append(
                    f"record scheme_version={record.scheme_version!r}"
                )
            if str(record.predict_date) != normalized_run_date:
                record_errors.append(
                    f"record predict_date={record.predict_date!r}"
                )
            record_phase = record.prediction_phase or (
                record.extra or {}
            ).get("prediction_phase")
            if record_phase != run_phase:
                record_errors.append(
                    f"record prediction_phase={record_phase!r}"
                )
        if record_errors:
            raise RuntimeError(
                "Native completion records revalidation failed: "
                + "; ".join(record_errors)
            )

        records_written = _insert_run_predictions_conn(
            conn,
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        expected = len(expected_targets)
        status = (
            "success"
            if expected == records_returned == records_written
            else "partial"
        )
        error_message = (
            None
            if status == "success"
            else (
                f"expected={expected}, returned={records_returned}, "
                f"written={records_written}"
            )
        )
        _finish_scheme_run_conn(
            conn,
            run_id=int(run_id),
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_message,
            require_exact_run=True,
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
    plan_sha256: str,
    source_authority: Mapping[str, object],
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> int:
    """原子提交一组 insert-only 的 ``gray_live`` 历史信号缺口。

    目标集合、活跃 Registry、输入 authority、prediction、run 成功状态与
    成功日志在同一事务内复核或提交；任何一步失败都不保留部分结果。
    """
    normalized_plan_sha256 = _require_lower_sha256(
        plan_sha256,
        "plan_sha256",
    )
    normalized_targets = _normalize_gray_gap_target_keys(
        cfg,
        expected_target_keys,
    )
    record_list = list(records)
    if records_returned != len(record_list):
        raise RuntimeError(
            "gray gap records_returned mismatch: "
            f"returned={records_returned}, records={len(record_list)}"
        )
    normalized_run_date = _require_iso_date(run_date, "run_date")
    normalized_duration = float(duration_sec)
    if not math.isfinite(normalized_duration) or normalized_duration < 0:
        raise ValueError("duration_sec must be a finite non-negative number")
    execution_dates = _gray_gap_execution_dates(normalized_targets)
    if normalized_run_date != execution_dates["predict_date"]:
        raise ValueError(
            "run_date must equal gray gap execution predict_date"
        )
    normalized_authority = _normalize_gray_gap_source_authority(
        source_authority,
        runtime_type=str(cfg.runtime_type),
        feature_date=execution_dates["feature_date"],
        predict_date=execution_dates["predict_date"],
    )
    _validate_gray_gap_records(
        cfg,
        records=record_list,
        expected_targets=normalized_targets,
    )
    enriched_records = _enrich_gray_gap_records(
        cfg,
        records=record_list,
        expected_targets=normalized_targets,
        plan_sha256=normalized_plan_sha256,
        source_authority=normalized_authority,
    )
    blackbox_snapshot_id = _blackbox_gray_gap_snapshot_id(
        cfg,
        records=enriched_records,
    )

    with engine.begin() as conn:
        _validate_gray_gap_archived_generation_conn(
            conn,
            normalized_authority,
        )
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        _validate_gray_gap_run(
            cfg,
            run_id=int(run_id),
            run=run,
            expected_count=len(normalized_targets),
            predict_date=execution_dates["predict_date"],
        )
        if blackbox_snapshot_id is not None:
            _bind_blackbox_gray_gap_snapshot_conn(
                conn,
                run_id=int(run_id),
                run=run,
                data_snapshot_id=blackbox_snapshot_id,
            )
        _validate_gray_gap_active_registry(
            conn,
            cfg,
            normalized_targets,
        )
        _assert_gray_gap_business_keys_absent(
            conn,
            normalized_targets,
        )
        records_written = _insert_run_predictions_conn(
            conn,
            int(run_id),
            enriched_records,
            scheme_version=str(cfg.scheme_version),
            insert_only=True,
        )
        if records_written != len(normalized_targets):
            raise RuntimeError(
                "gray gap records_written mismatch: "
                f"expected={len(normalized_targets)}, "
                f"written={records_written}"
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
                "run_id": int(run_id),
                "records_returned": records_returned,
                "records_written": records_written,
            },
        )
        _require_rowcount(finished, 1, "gray gap run finish")
        _write_run_log_conn(
            conn,
            str(cfg.scheme_id),
            normalized_run_date,
            "success",
            normalized_duration,
            None,
            int(run_id),
        )
        return records_written


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
    _require_rowcount(result, 1, "Blackbox gray gap run data snapshot bind")


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


def repair_blackbox_gray_gap_run_snapshot_provenance(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
) -> str:
    """受控补回已成功 Blackbox gray-gap run 缺失的运行级 snapshot。

    仅接受 current active 的精确 Blackbox config、未绑定任何 snapshot 的
    普通成功 gray_live run，并从其已落库预测的 canonical provenance 推导唯一
    snapshot；不修改 prediction、日期或业务键。
    """
    if str(getattr(cfg, "runtime_type", "")) != "blackbox_v2":
        raise ValueError(
            "Blackbox gray gap snapshot repair requires runtime_type=blackbox_v2"
        )
    if (
        getattr(cfg, "status", None) != "active"
        or getattr(cfg, "version_status", None) != "active"
    ):
        raise ValueError(
            "Blackbox gray gap snapshot repair requires config active+active"
        )
    exact_scheme_version = str(cfg.scheme_version)
    with engine.begin() as conn:
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        _validate_blackbox_gray_gap_snapshot_repair_run(
            cfg,
            run_id=int(run_id),
            run=run,
        )
        lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
        prediction_rows = list(
            conn.execute(
                text(
                    """
                    SELECT scheme_id, scheme_version, predict_date, feature_date,
                           target_date, prediction_phase, extra
                    FROM t_scheme_predictions
                    WHERE run_id = :run_id
                    ORDER BY id
                    """
                    + lock
                ),
                {"run_id": int(run_id)},
            )
            .mappings()
            .all()
        )
        expected_count = int(run["records_written"])
        if len(prediction_rows) != expected_count:
            raise RuntimeError(
                "Blackbox gray gap snapshot repair prediction count mismatch: "
                f"expected={expected_count}, actual={len(prediction_rows)}"
            )
        expected_run_date = _stored_iso_date(run, "predict_date")
        snapshot_ids: set[str] = set()
        for row in prediction_rows:
            if row.get("scheme_version") != exact_scheme_version:
                raise RuntimeError(
                    "Blackbox gray gap snapshot repair prediction version mismatch"
                )
            if row.get("prediction_phase") != "gray_live":
                raise RuntimeError(
                    "Blackbox gray gap snapshot repair prediction phase mismatch"
                )
            prediction_date = _stored_iso_date(row, "predict_date")
            if prediction_date != expected_run_date:
                raise RuntimeError(
                    "Blackbox gray gap snapshot repair prediction predict_date "
                    "does not match run"
                )
            extra = _stored_json_mapping(row, "extra")
            if extra.get("backfill_mode") != "signal_gap_fill":
                raise RuntimeError(
                    "Blackbox gray gap snapshot repair requires signal_gap_fill "
                    "prediction provenance"
                )
            _require_lower_sha256(
                extra.get("signal_gap_plan_sha256"),
                "Blackbox gray gap snapshot repair signal_gap_plan_sha256",
            )
            snapshot_ids.add(
                _blackbox_gray_gap_record_snapshot_id(
                    cfg,
                    scheme_id=row.get("scheme_id"),
                    predict_date=prediction_date,
                    feature_date=_stored_iso_date(row, "feature_date"),
                    target_date=_stored_iso_date(row, "target_date"),
                    extra=extra,
                )
            )
        if len(snapshot_ids) != 1:
            raise RuntimeError(
                "Blackbox gray gap snapshot repair requires exactly one "
                "prediction data_snapshot_id"
            )
        snapshot_id = next(iter(snapshot_ids))
        repaired = conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET data_snapshot_id = :data_snapshot_id
                WHERE run_id = :run_id
                  AND data_snapshot_id IS NULL
                  AND scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                  AND runtime_type = 'blackbox_v2'
                  AND status = 'success'
                  AND run_type = 'active'
                  AND prediction_phase = 'gray_live'
                """
            ),
            {
                "run_id": int(run_id),
                "data_snapshot_id": snapshot_id,
                "scheme_id": str(cfg.scheme_id),
                "scheme_version": exact_scheme_version,
            },
        )
        _require_rowcount(
            repaired,
            1,
            "Blackbox gray gap run data snapshot provenance repair",
        )
        return snapshot_id


def _validate_blackbox_gray_gap_snapshot_repair_run(
    cfg: SchemeConfig,
    *,
    run_id: int,
    run: Mapping[str, object] | None,
) -> None:
    """为 provenance-only repair 严格限定历史 run 的不可变业务身份。"""
    if run is None:
        raise RuntimeError(f"Blackbox gray gap snapshot repair run missing: {run_id}")
    expected = {
        "scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": "blackbox_v2",
        "run_type": "active",
        "prediction_phase": "gray_live",
        "status": "success",
    }
    mismatches = [
        f"{field}: expected={value!r}, got={run.get(field)!r}"
        for field, value in expected.items()
        if run.get(field) != value
    ]
    if run.get("data_snapshot_id") is not None:
        mismatches.append("data_snapshot_id must be NULL before repair")
    records_expected = run.get("records_expected")
    records_returned = run.get("records_returned")
    records_written = run.get("records_written")
    if (
        isinstance(records_expected, bool)
        or isinstance(records_returned, bool)
        or isinstance(records_written, bool)
        or not isinstance(records_expected, int)
        or not isinstance(records_returned, int)
        or not isinstance(records_written, int)
        or records_expected <= 0
        or records_returned <= 0
        or records_expected != records_returned
        or records_returned != records_written
    ):
        mismatches.append(
            "records_expected/records_returned/records_written must be equal "
            "positive integers"
        )
    if mismatches:
        raise RuntimeError(
            "Blackbox gray gap snapshot repair run identity mismatch: "
            + "; ".join(mismatches)
        )


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
_GRAY_GAP_NATIVE_AUTHORITY_FIELDS = frozenset(
    {
        "authority_type",
        "artifact_id",
        "manifest_sha256",
        "feature_date",
        "cutoff_date",
        "vintage_disclaimer",
    }
)
_GRAY_GAP_ARCHIVED_NATIVE_AUTHORITY_FIELDS = frozenset(
    {
        "authority_type",
        "generation_id",
        "manifest_sha256",
        "business_date",
        "feature_date",
        "cutoff_date",
        "replay_mode",
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
_GRAY_GAP_ARCHIVED_REPLAY_MODE = (
    "historical_sealed_generation_replay"
)
_GRAY_GAP_FIXED_ATOMIC_TARGETS = {
    "t1_daily": frozenset({"5Y", "10Y"}),
    "t5_daily": frozenset({"3Y", "5Y", "7Y", "10Y"}),
}


def _validate_gray_gap_archived_generation_conn(
    conn: Connection,
    source_authority: Mapping[str, object],
) -> None:
    """事务内锁定并核对 archived Native provenance。"""
    if (
        source_authority.get("authority_type")
        != "native_archived_generation"
    ):
        return
    generation_id = str(source_authority["generation_id"])
    row = _read_input_generation_conn(
        conn,
        generation_id,
        for_update=True,
    )
    if row is None:
        raise RuntimeError(
            "archived Native generation does not exist: "
            f"{generation_id}"
        )
    expected = {
        "generation_id": generation_id,
        "generation_type": "native_source",
        "business_date": str(source_authority["business_date"]),
        "feature_date": str(source_authority["feature_date"]),
        "exporter_version": NATIVE_GENERATION_EXPORTER_VERSION,
        "manifest_sha256": str(source_authority["manifest_sha256"]),
        "state": INPUT_GENERATION_SEALED,
    }
    mismatches = {
        field: (expected_value, row.get(field))
        for field, expected_value in expected.items()
        if str(row.get(field)) != expected_value
    }
    if (
        row.get("sealed_at") is None
        or row.get("invalidated_at") is not None
    ):
        mismatches["sealed_fence"] = (
            "sealed and not invalidated",
            {
                "sealed_at": row.get("sealed_at"),
                "invalidated_at": row.get("invalidated_at"),
            },
        )
    if mismatches:
        raise RuntimeError(
            "archived Native generation authority drift: "
            f"{mismatches}"
        )


def _require_lower_sha256(value: object, field: str) -> str:
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
        getattr(cfg, "status", None) != "active"
        or getattr(cfg, "version_status", None) != "active"
    ):
        raise RuntimeError(
            "gray gap config must be active with active version_status"
        )
    base_scheme_id = _require_nonempty(
        getattr(cfg, "scheme_id", None),
        "cfg.scheme_id",
    )
    scheme_version = _require_nonempty(
        getattr(cfg, "scheme_version", None),
        "cfg.scheme_version",
    )
    del scheme_version
    runtime_type = getattr(cfg, "runtime_type", None)
    if runtime_type not in {"native_adapter", "blackbox_v2"}:
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
    cfg_frequency = _require_nonempty(
        getattr(cfg, "frequency", None),
        "cfg.frequency",
    )
    del cfg_frequency
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
    expected_tenors = Counter(cfg_tenors)
    if actual_tenors != expected_tenors:
        raise RuntimeError(
            "expected target multiset does not match exact config: "
            f"expected={dict(expected_tenors)}, actual={dict(actual_tenors)}"
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
    source_authority: Mapping[str, object],
    *,
    runtime_type: str,
    feature_date: str,
    predict_date: str,
) -> dict[str, object]:
    if not isinstance(source_authority, Mapping):
        raise ValueError("source_authority must be a mapping")
    authority_type = source_authority.get("authority_type")
    if authority_type == "native_current_snapshot_artifact":
        expected_fields = _GRAY_GAP_NATIVE_AUTHORITY_FIELDS
    elif authority_type == "native_archived_generation":
        expected_fields = _GRAY_GAP_ARCHIVED_NATIVE_AUTHORITY_FIELDS
    elif authority_type == "databridge_current_generation":
        expected_fields = _GRAY_GAP_DATABRIDGE_AUTHORITY_FIELDS
    else:
        raise ValueError("source_authority authority_type is invalid")
    expected_authority_types = {
        "native_adapter": {
            "native_archived_generation",
            "native_current_snapshot_artifact",
        },
        "blackbox_v2": {"databridge_current_generation"},
    }.get(runtime_type, set())
    if authority_type not in expected_authority_types:
        raise ValueError(
            "source_authority authority_type does not match cfg "
            f"runtime_type={runtime_type}"
        )
    if set(source_authority) != expected_fields:
        raise ValueError(
            "source_authority must contain exact fields "
            f"{sorted(expected_fields)}"
        )
    normalized = dict(source_authority)
    normalized["manifest_sha256"] = _require_lower_sha256(
        source_authority["manifest_sha256"],
        "source_authority.manifest_sha256",
    )
    cutoff_date = _require_iso_date(
        source_authority["cutoff_date"],
        "source_authority.cutoff_date",
    )
    if cutoff_date != feature_date:
        raise ValueError(
            "source_authority cutoff_date must equal execution feature_date"
        )
    if authority_type == "native_current_snapshot_artifact":
        if (
            source_authority["vintage_disclaimer"]
            != _GRAY_GAP_VINTAGE_DISCLAIMER
        ):
            raise ValueError(
                "source_authority vintage_disclaimer is invalid"
            )
        normalized["artifact_id"] = _require_nonempty(
            source_authority["artifact_id"],
            "source_authority.artifact_id",
        )
        authority_feature_date = _require_iso_date(
            source_authority["feature_date"],
            "source_authority.feature_date",
        )
        if authority_feature_date != feature_date:
            raise ValueError(
                "source_authority feature_date must equal execution "
                "feature_date"
            )
    elif authority_type == "native_archived_generation":
        normalized["generation_id"] = _require_nonempty(
            source_authority["generation_id"],
            "source_authority.generation_id",
        )
        authority_business_date = _require_iso_date(
            source_authority["business_date"],
            "source_authority.business_date",
        )
        authority_feature_date = _require_iso_date(
            source_authority["feature_date"],
            "source_authority.feature_date",
        )
        if authority_business_date != predict_date:
            raise ValueError(
                "source_authority business_date must equal execution "
                "predict_date"
            )
        if authority_feature_date != feature_date:
            raise ValueError(
                "source_authority feature_date must equal execution "
                "feature_date"
            )
        if (
            source_authority["replay_mode"]
            != _GRAY_GAP_ARCHIVED_REPLAY_MODE
        ):
            raise ValueError(
                "source_authority replay_mode is invalid"
            )
    else:
        if (
            source_authority["vintage_disclaimer"]
            != _GRAY_GAP_VINTAGE_DISCLAIMER
        ):
            raise ValueError(
                "source_authority vintage_disclaimer is invalid"
            )
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
    expected_targets: list[Mapping[str, object]],
    plan_sha256: str,
    source_authority: Mapping[str, object],
) -> list[PredictionRecord]:
    targets = [
        {
            "registry_scheme_id": row["registry_scheme_id"],
            "target_tenor": row["target_tenor"],
            "horizon": row["horizon"],
        }
        for row in expected_targets
    ]
    dates = _gray_gap_execution_dates(expected_targets)
    unsigned_group = {
        "base_scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": str(cfg.runtime_type),
        "task_type": str(cfg.task_type),
        "predict_date": dates["predict_date"],
        "feature_date": dates["feature_date"],
        "target_date": dates["target_date"],
        "prediction_phase": "gray_live",
        "record_count": len(targets),
        "targets": targets,
    }
    group_digest = hashlib.sha256(
        json.dumps(
            unsigned_group,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    execution_group_identity = {
        **unsigned_group,
        "identity_sha256": group_digest,
    }
    backfilled_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    common_extra: dict[str, object] = {
        "signal_gap_plan_sha256": plan_sha256,
        "backfill_mode": "signal_gap_fill",
        "backfilled_at": backfilled_at,
        "source_authority": dict(source_authority),
        "replay_semantics": (
            source_authority["replay_mode"]
            if source_authority["authority_type"]
            == "native_archived_generation"
            else _GRAY_GAP_VINTAGE_DISCLAIMER
        ),
        "execution_group_identity": execution_group_identity,
    }
    if source_authority["authority_type"] == (
        "native_current_snapshot_artifact"
    ):
        common_extra.update(
            {
                "source_artifact_id": source_authority["artifact_id"],
                "source_artifact_manifest_sha256":
                    source_authority["manifest_sha256"],
                "source_artifact_feature_date":
                    source_authority["feature_date"],
                "source_cutoff_date": source_authority["cutoff_date"],
            }
        )
    elif source_authority["authority_type"] == (
        "native_archived_generation"
    ):
        common_extra.update(
            {
                "source_generation_id":
                    source_authority["generation_id"],
                "source_generation_manifest_sha256":
                    source_authority["manifest_sha256"],
                "source_generation_business_date":
                    source_authority["business_date"],
                "source_generation_feature_date":
                    source_authority["feature_date"],
                "source_cutoff_date":
                    source_authority["cutoff_date"],
            }
        )
    else:
        common_extra.update(
            {
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
            extra={**dict(record.extra or {}), **common_extra},
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


def _validate_gray_gap_active_registry(
    conn: Connection,
    cfg: SchemeConfig,
    expected_targets: list[Mapping[str, object]],
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
            str(row["registry_scheme_id"]),
            str(row["base_scheme_id"]),
            str(cfg.runtime_type),
            str(cfg.frequency),
            str(row["task_type"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in expected_targets
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
    for row in sorted(
        expected_targets,
        key=lambda item: (
            str(item["base_scheme_id"]),
            str(item["target_tenor"]),
            int(item["horizon"]),
            str(item["target_date"]),
        ),
    ):
        existing = _select_mapping_one_or_none(
            conn,
            """
            SELECT id, run_id
            FROM t_scheme_predictions
            WHERE scheme_id = :scheme_id
              AND target_tenor = :target_tenor
              AND horizon = :horizon
              AND target_date = :target_date
            """,
            {
                "scheme_id": str(row["base_scheme_id"]),
                "target_tenor": str(row["target_tenor"]),
                "horizon": int(row["horizon"]),
                "target_date": str(row["target_date"]),
            },
            for_update=True,
        )
        if existing is not None:
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
            for_update=True,
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
            require_exact_run=True,
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
    project_root = scheme_path.parent.parent
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

    with lifecycle_operation_lock(project_root, cfg.scheme_id):
        assert_lifecycle_clear(project_root, cfg.scheme_id)
        current_cfg = load_scheme_config(config_path)
        if (
            current_cfg.scheme_version != cfg.scheme_version
            or current_cfg.status != "active"
            or current_cfg.version_status != "active"
        ):
            raise RuntimeError(
                "Blackbox canonical config changed before final write: "
                f"expected={cfg.scheme_version}/active/active, "
                f"current={current_cfg.scheme_version}/{current_cfg.status}/{current_cfg.version_status}"
            )
        cfg = current_cfg
        with engine.begin() as conn:
            approval = _read_blackbox_execution_approval_conn(conn, cfg, for_update=True)
            if not approval.executable:
                raise RuntimeError(
                    f"Blackbox V2 version is not production-approved: {approval.reason}"
                )
            yield conn, record_list, exact_scheme_version


def _insert_run_predictions_conn(
    conn: Connection,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
    insert_only: bool = False,
) -> int:
    """在调用方事务中写入预测；历史灰度补齐使用 insert-only。"""
    sqlite = _dialect_name(conn) == "sqlite"
    extra_expression = ":extra" if sqlite else "CAST(:extra AS JSON)"
    statement = """
        INSERT INTO t_scheme_predictions
            (run_id, scheme_version, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
             prediction_phase, predicted_direction, confidence, model_version, extra)
        VALUES
            (:run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :prediction_phase, :predicted_direction, :confidence, :model_version, {extra_expression})
        """.format(extra_expression=extra_expression)
    if not insert_only:
        if sqlite:
            statement += """
            ON CONFLICT(scheme_id, target_tenor, horizon, target_date)
            DO UPDATE SET
                run_id = excluded.run_id,
                scheme_version = excluded.scheme_version,
                predict_date = excluded.predict_date,
                feature_date = excluded.feature_date,
                prediction_phase = excluded.prediction_phase,
                predicted_direction = excluded.predicted_direction,
                confidence = excluded.confidence,
                model_version = excluded.model_version,
                extra = excluded.extra,
                updated_at = CURRENT_TIMESTAMP
            """
        else:
            statement += """
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
    sql = text(statement)
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
        if int(row["run_id"]) != int(run_id):
            raise RuntimeError(
                "prediction record run_id must match the committing run: "
                f"{row['run_id']} != {run_id}"
            )
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
