from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.exc import NoInspectionAvailable

from scheduler.daily_coordinator import is_first_attempt_covered
from scheduler.daily_ledger import (
    FAILURE_ABANDONED_FENCE_PENDING_CLEANUP,
    FAILURE_ABANDONED_ORPHAN_CLEANUP,
    FAILURE_RECOVERY_CUTOFF_EXPIRED,
    FAILURE_RESULT,
    FAILURE_TRANSIENT_INFRA,
    GENERATION_BUILDING,
    GENERATION_INVALIDATED,
    GENERATION_SEALED,
    GUARDRAIL_NOT_APPLICABLE,
    ITEM_ABANDONED,
    ITEM_EXPIRED,
    ITEM_FAILED_TERMINAL,
    ITEM_PENDING,
    ITEM_RETRY_WAIT,
    ITEM_RUNNING,
    ITEM_SLA_LATE,
    ITEM_SLA_ON_TIME,
    ITEM_SUCCESS,
    OCCURRENCE_FAILED,
    OCCURRENCE_PENDING,
    OCCURRENCE_RUNNING,
    OCCURRENCE_SUCCESS,
    NONEXECUTED_FAILURE_CODES,
    SCHEDULE_FAILURE_CODES,
    SLA_BREACHED,
    SLA_MET,
    SLA_PENDING,
    TARGET_ACCEPTED,
    TERMINAL_EXECUTION_FAILURE_CODES,
    StartGuardrailProjection,
    TargetSlaProjection,
    freeze_active_daily_registry,
    project_target_availability_sla,
    project_v2_start_guardrail,
    validate_snapshot_cardinality,
)
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.blackbox_scheduler_admission import LAUNCHD_ONE_SHOT
from shared.blackbox_v2.lifecycle import assert_lifecycle_clear, lifecycle_operation_lock
from shared.daily_coordinator_mode import (
    assert_daily_coordinator_epoch_payload_matches_current,
)
from shared.db_config import DatabaseConfig
from shared.input_artifacts import InputArtifact
from shared.liwei_0616_cache_contract import (
    validate_prediction_cache_audit,
    validate_trusted_cache_use_qualification,
)
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
    "t_schedule_occurrences",
    "t_schedule_items",
    "t_schedule_item_targets",
    "t_scheduler_heartbeat",
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
_MAX_SCHEDULE_ATTEMPTS = 2
_ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEDULE_TRIGGER_ORIGINS = {
    "apscheduler",
    "startup_catchup",
    "auto_retry",
    "operator_recovery",
}
_SECOND_ATTEMPT_TRIGGER_ORIGINS = {"auto_retry", "operator_recovery"}
_ABANDONED_TRIGGER_ORIGINS = {"startup_catchup", "operator_recovery"}
_TERMINAL_SCHEDULE_FAILURE_CODES = frozenset(
    {
        *TERMINAL_EXECUTION_FAILURE_CODES,
        FAILURE_RECOVERY_CUTOFF_EXPIRED,
    }
)
_NONEXECUTED_SCHEDULE_FAILURE_CODES = NONEXECUTED_FAILURE_CODES
_ABANDONED_FENCE_PENDING_CLEANUP = (
    FAILURE_ABANDONED_FENCE_PENDING_CLEANUP
)
_ABANDONED_ORPHAN_CLEANUP = FAILURE_ABANDONED_ORPHAN_CLEANUP
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
class ScheduledAttempt:
    """日批 item 当前一次带 fence 的执行尝试。"""

    item_id: int
    run_id: int
    attempt_no: int
    execution_token: str


@dataclass(frozen=True)
class CurrentReplayAttemptProcess:
    """当前隔离 replay occurrence 可允许的已登记进程组。"""

    item_id: int
    run_id: int
    execution_token: str
    process_id: int
    process_group_id: int
    base_scheme_id: str
    input_compatibility: str


@dataclass(frozen=True)
class FencedScheduleAttempt:
    """已先行失效、等待孤儿进程清理确认的 attempt 身份。"""

    item_id: int
    run_id: int
    execution_token: str
    process_id: int | None
    process_group_id: int | None
    fenced_at: datetime


@dataclass(frozen=True)
class ScheduledCompletionEvidence:
    """算法返回前由执行器在同一输入 generation 上观测的完整性证据。"""

    observed_generation_id: str
    manifest_sha256: str
    generation_dataset_content_id: str
    generation_schema_version: str
    generation_exporter_version: str
    feature_date: str
    scheme_version: str
    code_sha256: str
    config_sha256: str
    native_generation_id: str | None = None
    native_manifest_sha256: str | None = None
    native_dataset_content_id: str | None = None
    native_schema_version: str | None = None
    native_exporter_version: str | None = None
    native_feature_date: str | None = None


@dataclass(frozen=True)
class ScheduledCompletionExpectation:
    """受信 verifier 在事务外重开并复核文件所需的冻结期望。"""

    run_id: int
    item_id: int
    occurrence_id: int
    base_scheme_id: str
    runtime_type: str
    generation_id: str
    manifest_uri: str
    manifest_sha256: str
    generation_dataset_content_id: str
    generation_schema_version: str
    generation_exporter_version: str
    feature_date: str
    business_date: str
    scheme_version: str
    code_sha256: str
    config_sha256: str
    native_generation_id: str | None = None
    native_manifest_uri: str | None = None
    native_manifest_sha256: str | None = None
    native_dataset_content_id: str | None = None
    native_schema_version: str | None = None
    native_exporter_version: str | None = None
    native_feature_date: str | None = None
    native_business_date: str | None = None


class ScheduledCompletionVerifier(Protocol):
    """事务外执行实际 rehash，并返回强类型观测证据。"""

    def verify(
        self,
        expectation: ScheduledCompletionExpectation,
    ) -> ScheduledCompletionEvidence:
        """返回基于实际文件/代码复核得到的证据。"""


@dataclass(frozen=True)
class ScheduleOccurrenceEnvelope:
    """只由 occurrence 冻结行构造的执行上下文。"""

    occurrence_id: int
    schedule_key: str
    predict_date: str
    feature_date: str
    policy_version: str
    policy_sha256: str
    policy_json: Mapping[str, object]
    registry_digest: str
    completion_state: str
    expected_item_count: int
    expected_target_count: int
    accepted_target_count: int
    sla_accepted_target_count: int | None
    sla_deadline_at: datetime
    recovery_cutoff_at: datetime
    sla_outcome: str
    sla_evaluated_at: datetime | None
    sla_reason: str | None
    failure_code: str | None
    failure_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class ScheduleItemEnvelope:
    """只由 item 冻结行及其 occurrence 时限构造的执行上下文。"""

    item_id: int
    occurrence_id: int
    base_scheme_id: str
    runtime_type: str
    scheme_version: str
    code_sha256: str
    config_sha256: str
    cache_group: str
    input_generation_id: str | None
    resource_class: str
    internal_workers: int
    release_offset_minutes: int
    release_at: datetime
    deadline_at: datetime
    recovery_cutoff_at: datetime
    occurrence_sla_deadline_at: datetime
    state: str
    sla_status: str
    late_reason: str | None
    sla_evaluated_at: datetime | None
    attempt_no: int
    current_run_id: int | None
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True)
class ScheduleTargetEnvelope:
    """冻结 target 身份与后续 acceptance 状态。"""

    target_id: int
    occurrence_id: int
    item_id: int
    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    task_type: str
    target_tenor: str
    horizon: int
    target_date: str
    status: str
    accepted_run_id: int | None
    accepted_prediction_id: int | None
    accepted_at: datetime | None
    visible_at: datetime | None
    accepted_linkage_valid: bool = False


@dataclass(frozen=True)
class ScheduleInputGenerationEnvelope:
    """冻结 input generation provenance。"""

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


@dataclass(frozen=True)
class ScheduleExecutionEnvelope:
    """一次 item 执行所需的完整冻结信封。"""

    occurrence: ScheduleOccurrenceEnvelope
    item: ScheduleItemEnvelope
    generation: ScheduleInputGenerationEnvelope
    calendar_generation: ScheduleInputGenerationEnvelope
    targets: tuple[ScheduleTargetEnvelope, ...]


@dataclass(frozen=True)
class ScheduleOccurrenceItemSummary:
    """Occurrence 详情中的 item 与 target 聚合。"""

    item: ScheduleItemEnvelope
    target_count: int
    accepted_target_count: int


@dataclass(frozen=True)
class ScheduleOccurrenceSnapshot:
    """Occurrence、全部 items 及一致性聚合的只读快照。"""

    occurrence: ScheduleOccurrenceEnvelope
    items: tuple[ScheduleOccurrenceItemSummary, ...]
    actual_item_count: int
    actual_target_count: int
    actual_accepted_target_count: int
    item_state_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SchedulerHeartbeat:
    """跨进程 scheduler 存活与当前工作状态。"""

    service_name: str
    process_id: int
    host_name: str
    state: str
    occurrence_id: int | None
    heartbeat_at: datetime
    details: Mapping[str, object]


class _LedgerClock(Protocol):
    """仅供内部测试 seam 使用的可信 UTC clock。"""

    def now_utc(self) -> datetime:
        """返回带时区的 UTC 当前时间。"""


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


def _lock_registration_native_parent_conn(
    conn: Connection,
    params: Mapping[str, object],
) -> Mapping[str, object] | None:
    """按父→子锁序锁定并验证 DataBridge 的 Native generation。"""
    if str(params.get("generation_type")) != "databridge_v1":
        return None
    native_generation_id = params.get("native_generation_id")
    if not native_generation_id:
        raise RuntimeError(
            "databridge generation is missing native_generation_id"
        )
    native_generation = _read_input_generation_conn(
        conn,
        str(native_generation_id),
        for_update=True,
    )
    if native_generation is None:
        raise RuntimeError(
            f"native generation not found: {native_generation_id}"
        )
    _validate_databridge_native_relation(
        databridge=params,
        native_generation=native_generation,
    )
    return native_generation


def _create_or_match_input_generation_conn(
    conn: Connection,
    params: Mapping[str, object],
    *,
    native_generation: Mapping[str, object] | None,
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
                "building": GENERATION_BUILDING,
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
                 :native_manifest_sha256, '{GENERATION_BUILDING}')
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
    if native_generation is not None:
        _validate_databridge_native_relation(
            databridge=row,
            native_generation=native_generation,
        )
    return row


def create_input_generation(
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
    native_generation_id: str | None = None,
    native_manifest_sha256: str | None = None,
) -> str:
    """创建不可变输入 generation；相同 ID 只允许完全相同的 provenance。"""
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
        native_generation_id=native_generation_id,
        native_manifest_sha256=native_manifest_sha256,
    )
    if (
        params["generation_type"] == "native_source"
        and params["exporter_version"]
        == SIGNAL_GAP_NATIVE_EXPORTER_VERSION
    ):
        raise ValueError(
            "daily ledger Native registration rejects the signal-gap "
            "special exporter_version"
        )
    with engine.begin() as conn:
        native_generation = _lock_registration_native_parent_conn(
            conn,
            params,
        )
        _create_or_match_input_generation_conn(
            conn,
            params,
            native_generation=native_generation,
        )
    return str(params["generation_id"])


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
    """原子登记 gray-gap current snapshot，不创建任何 ledger 对象。"""
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
    """原子登记当日已封存 Native generation，不创建 ledger 对象。"""
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
    """登记并原子封存一个不绑定 occurrence 的 Native authority。"""
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
                        "building": GENERATION_BUILDING,
                        "sealed": GENERATION_SEALED,
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
                native_generation=None,
            )
            state = str(row.get("state"))
            if state == GENERATION_SEALED:
                if row.get("sealed_at") is None:
                    raise RuntimeError(
                        f"SEALED {authority_label} Native authority has no "
                        "sealed_at"
                    )
                return str(params["generation_id"])
            if state != GENERATION_BUILDING:
                raise RuntimeError(
                    f"{authority_label} Native authority cannot be sealed from "
                    f"state={state}"
                )
            sealed_at, _trusted_now = _ledger_event_time_after_locks(
                conn,
                None,
                field="sealed_at",
                clock=None,
            )
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
                    "sealed": GENERATION_SEALED,
                    "sealed_at": sealed_at,
                    "generation_id": params["generation_id"],
                    "building": GENERATION_BUILDING,
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
) -> ScheduleInputGenerationEnvelope:
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
    if str(row.get("state")) != GENERATION_SEALED:
        raise RuntimeError(
            "input generation DB fence is not SEALED: "
            f"{normalized_generation_id}"
        )
    if row.get("sealed_at") is None:
        raise RuntimeError(
            "SEALED input generation DB fence has no sealed_at: "
            f"{normalized_generation_id}"
        )
    return _schedule_generation_envelope(row)


def resolve_reclaimable_generation_payloads(
    engine: Engine,
) -> tuple[ScheduleInputGenerationEnvelope, ...]:
    """由 DB 引用闭包解析可删除 payload；SEALED/历史绑定永不返回。"""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT g.generation_id, g.generation_type,
                           g.business_date, g.feature_date,
                           g.readiness_basis, g.source_commit_token,
                           g.dataset_content_id, g.schema_version,
                           g.exporter_version, g.manifest_uri,
                           g.manifest_sha256, g.native_generation_id,
                           g.native_manifest_sha256, g.state, g.sealed_at,
                           g.invalidated_at, g.invalid_reason
                    FROM t_input_generations AS g
                    WHERE g.state = :invalidated
                      AND NOT EXISTS (
                          SELECT 1
                          FROM t_schedule_items AS i
                          WHERE i.input_generation_id = g.generation_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM t_input_generations AS child
                          WHERE child.native_generation_id = g.generation_id
                      )
                    ORDER BY g.generation_type, g.generation_id
                    """
                ),
                {"invalidated": GENERATION_INVALIDATED},
            )
            .mappings()
            .all()
        )
    return tuple(_schedule_generation_envelope(row) for row in rows)


def finalize_reclaimed_generation_payload(
    engine: Engine,
    *,
    generation_id: str,
    expected_generation_type: str,
    expected_business_date: str,
    expected_feature_date: str,
    expected_manifest_uri: str,
    expected_manifest_sha256: str,
) -> bool:
    """payload 删除后重验 DB 引用闭包并移除 INVALIDATED tombstone 行。

    文件删除与 DB 删除无法组成同一事务。若进程在文件删除后崩溃，重启会再次
    解析同一候选；文件侧删除是幂等的，本函数随后完成 DB finalize。删除子
    DataBridge 行后，下一轮 resolver 才可能释放其 Native 父代。
    """
    normalized_generation_id = _require_nonempty(
        generation_id,
        "generation_id",
    )
    if expected_generation_type not in _GENERATION_TYPES:
        raise ValueError(
            "expected_generation_type must be one of "
            f"{sorted(_GENERATION_TYPES)}"
        )
    normalized_business_date = date.fromisoformat(
        str(expected_business_date)
    ).isoformat()
    normalized_feature_date = date.fromisoformat(
        str(expected_feature_date)
    ).isoformat()
    normalized_manifest_uri = _require_nonempty(
        expected_manifest_uri,
        "expected_manifest_uri",
    )
    normalized_manifest_sha256 = _require_sha256(
        expected_manifest_sha256,
        "expected_manifest_sha256",
    )
    with engine.begin() as conn:
        row = _read_input_generation_conn(
            conn,
            normalized_generation_id,
            for_update=True,
        )
        if row is None:
            return False
        expected_identity = {
            "generation_type": expected_generation_type,
            "business_date": normalized_business_date,
            "feature_date": normalized_feature_date,
            "manifest_uri": normalized_manifest_uri,
            "manifest_sha256": normalized_manifest_sha256,
            "state": GENERATION_INVALIDATED,
        }
        actual_identity = {
            "generation_type": str(row.get("generation_type")),
            "business_date": str(row.get("business_date")),
            "feature_date": str(row.get("feature_date")),
            "manifest_uri": str(row.get("manifest_uri")),
            "manifest_sha256": str(row.get("manifest_sha256")),
            "state": str(row.get("state")),
        }
        if actual_identity != expected_identity:
            raise RuntimeError(
                "reclaimed generation DB fence changed before finalize: "
                f"{normalized_generation_id}"
            )
        item_reference_count = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_schedule_items
                    WHERE input_generation_id = :generation_id
                    """
                ),
                {"generation_id": normalized_generation_id},
            ).scalar_one()
        )
        child_reference_count = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_input_generations
                    WHERE native_generation_id = :generation_id
                    """
                ),
                {"generation_id": normalized_generation_id},
            ).scalar_one()
        )
        if item_reference_count or child_reference_count:
            raise RuntimeError(
                "reclaimed generation gained a DB reference before "
                f"finalize: {normalized_generation_id}"
            )
        deleted = conn.execute(
            text(
                """
                DELETE FROM t_input_generations
                WHERE generation_id = :generation_id
                  AND state = :invalidated
                """
            ),
            {
                "generation_id": normalized_generation_id,
                "invalidated": GENERATION_INVALIDATED,
            },
        )
        if deleted.rowcount != 1:
            raise RuntimeError(
                "reclaimed generation DB finalize lost its locked row: "
                f"{normalized_generation_id}"
            )
    return True


def _validate_databridge_native_relation(
    *,
    databridge: Mapping[str, object],
    native_generation: Mapping[str, object],
) -> None:
    """验证 DataBridge 冻结的 calendar generation 关系。"""
    native_generation_id = str(native_generation["generation_id"])
    if str(native_generation.get("generation_type")) != "native_source":
        raise RuntimeError(
            "native generation relation must reference native_source: "
            f"{native_generation_id}"
        )
    if str(native_generation.get("state")) != GENERATION_SEALED:
        raise RuntimeError(
            "native generation relation is not SEALED: "
            f"{native_generation_id}"
        )
    for field in ("business_date", "feature_date"):
        if str(native_generation.get(field)) != str(databridge.get(field)):
            raise RuntimeError(
                "native generation relation date mismatch: "
                f"{field}={native_generation.get(field)} != "
                f"{databridge.get(field)}"
            )
    if str(native_generation.get("manifest_sha256")) != str(
        databridge.get("native_manifest_sha256")
    ):
        raise RuntimeError(
            "native generation manifest_sha256 mismatch: "
            f"{native_generation_id}"
        )


def _lock_input_generation_with_parent_conn(
    conn: Connection,
    generation_id: str,
) -> tuple[
    Mapping[str, object] | None,
    Mapping[str, object] | None,
]:
    """以 Native 父 generation→DataBridge 子 generation 的顺序加锁。"""
    preview = _read_input_generation_conn(
        conn,
        generation_id,
        for_update=False,
    )
    if preview is None:
        return None, None
    native_generation = None
    preview_native_id = preview.get("native_generation_id")
    if str(preview.get("generation_type")) == "databridge_v1":
        if not preview_native_id:
            raise RuntimeError(
                "databridge generation is missing native_generation_id"
            )
        native_generation = _read_input_generation_conn(
            conn,
            str(preview_native_id),
            for_update=True,
        )
        if native_generation is None:
            raise RuntimeError(
                f"native generation not found: {preview_native_id}"
            )
    row = _read_input_generation_conn(
        conn,
        generation_id,
        for_update=True,
    )
    if row is None:
        raise RuntimeError(
            f"input generation disappeared while locking: {generation_id}"
        )
    if (
        str(row.get("generation_type"))
        != str(preview.get("generation_type"))
        or str(row.get("native_generation_id"))
        != str(preview_native_id)
    ):
        raise RuntimeError(
            "input generation immutable parent relation changed while locking: "
            f"{generation_id}"
        )
    if native_generation is not None:
        _validate_databridge_native_relation(
            databridge=row,
            native_generation=native_generation,
        )
    return row, native_generation


def invalidate_input_generation(
    engine: Engine,
    *,
    generation_id: str,
    reason: str,
    invalidated_at: datetime | None = None,
) -> None:
    """将 generation 标为 INVALIDATED，并保留首次失效原因。"""
    normalized_reason = _require_nonempty(reason, "reason")
    effective_time = _utc_datetime6(invalidated_at)
    with engine.begin() as conn:
        row = _read_input_generation_conn(
            conn,
            generation_id,
            for_update=True,
        )
        if row is None:
            raise RuntimeError(f"input generation not found: {generation_id}")
        if row.get("state") == GENERATION_INVALIDATED:
            if row.get("invalid_reason") != normalized_reason:
                raise RuntimeError(
                    "input generation is already invalidated with another reason"
                )
            return
        result = conn.execute(
            text(
                """
                UPDATE t_input_generations
                SET state = :state,
                    invalidated_at = :invalidated_at,
                    invalid_reason = :invalid_reason,
                    updated_at = :invalidated_at
                WHERE generation_id = :generation_id
                  AND state IN (:building, :sealed)
                """
            ),
            {
                "state": GENERATION_INVALIDATED,
                "invalidated_at": effective_time,
                "invalid_reason": normalized_reason,
                "generation_id": generation_id,
                "building": GENERATION_BUILDING,
                "sealed": GENERATION_SEALED,
            },
        )
        _require_rowcount(result, 1, "input generation invalidate")


def create_schedule_occurrence(
    engine: Engine,
    *,
    schedule_key: str,
    predict_date: str,
    feature_date: str,
    target_dates: Mapping[str, str],
    item_policy_by_base: Mapping[str, Mapping[str, object]],
    policy_version: str = "daily-ledger-v1",
    policy_json: Mapping[str, object] | None = None,
    policy_sha256: str | None = None,
    sla_deadline_at: datetime | None = None,
    recovery_cutoff_at: datetime | None = None,
) -> int:
    """从 active daily Registry 原子冻结幂等 occurrence/items/targets。"""
    normalized_schedule_key = _require_nonempty(
        schedule_key,
        "schedule_key",
    )
    normalized_predict_date = date.fromisoformat(predict_date).isoformat()
    normalized_feature_date = date.fromisoformat(feature_date).isoformat()
    if normalized_feature_date >= normalized_predict_date:
        raise ValueError(
            "feature_date must be earlier than predict_date"
        )
    normalized_policy_version = _require_nonempty(
        policy_version,
        "policy_version",
    )
    policy_payload = dict(policy_json or {})
    _assert_deployed_epoch_payload(
        policy_payload.get("daily_coordinator_epoch"),
        label="new daily occurrence coordinator epoch",
        engine=engine,
    )
    policy_text = json.dumps(
        policy_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    computed_policy_sha256 = hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
    if (
        policy_sha256 is not None
        and _require_sha256(policy_sha256, "policy_sha256")
        != computed_policy_sha256
    ):
        raise ValueError("policy_sha256 does not match canonical policy_json")
    effective_sla_deadline = _utc_datetime6(
        sla_deadline_at
        or _business_time_utc(
            normalized_predict_date,
            time(8, 0),
        )
    )
    effective_recovery_cutoff = _utc_datetime6(
        recovery_cutoff_at
        or _business_time_utc(
            normalized_predict_date,
            time(8, 30),
        )
    )
    if effective_recovery_cutoff < effective_sla_deadline:
        raise ValueError("recovery_cutoff_at cannot be earlier than sla_deadline_at")

    with engine.begin() as conn:
        _assert_deployed_epoch_payload(
            policy_payload.get("daily_coordinator_epoch"),
            label="new daily occurrence coordinator epoch",
            engine=conn,
        )
        existing = _read_schedule_occurrence_conn(
            conn,
            schedule_key=normalized_schedule_key,
            predict_date=normalized_predict_date,
            for_update=True,
        )
        registry_rows = _read_active_daily_registry_snapshot_rows(
            conn,
            target_dates=target_dates,
            item_policy_by_base=item_policy_by_base,
        )
        snapshot = freeze_active_daily_registry(registry_rows)
        if existing is not None:
            _assert_occurrence_epoch_conn(existing, engine=conn)
            _validate_stored_occurrence_cardinality(conn, existing)
            expected_identity = {
                "feature_date": normalized_feature_date,
                "policy_version": normalized_policy_version,
                "policy_sha256": computed_policy_sha256,
                "registry_digest": snapshot.registry_digest,
                "expected_item_count": snapshot.expected_item_count,
                "expected_target_count": snapshot.expected_target_count,
                "sla_deadline_at": effective_sla_deadline,
                "recovery_cutoff_at": effective_recovery_cutoff,
            }
            drift = {
                field: (existing.get(field), expected)
                for field, expected in expected_identity.items()
                if (
                    _as_datetime(existing[field], field) != expected
                    if field in {
                        "sla_deadline_at",
                        "recovery_cutoff_at",
                    }
                    else str(existing.get(field)) != str(expected)
                )
            }
            if drift:
                raise RuntimeError(
                    "occurrence immutable snapshot mismatch: "
                    f"{drift}"
                )
            return int(existing["occurrence_id"])
        occurrence_params = {
            "schedule_key": normalized_schedule_key,
            "predict_date": normalized_predict_date,
            "feature_date": normalized_feature_date,
            "policy_version": normalized_policy_version,
            "policy_sha256": computed_policy_sha256,
            "policy_json": policy_text,
            "registry_digest": snapshot.registry_digest,
            "completion_state": OCCURRENCE_PENDING,
            "expected_item_count": snapshot.expected_item_count,
            "expected_target_count": snapshot.expected_target_count,
            "sla_deadline_at": effective_sla_deadline,
            "recovery_cutoff_at": effective_recovery_cutoff,
        }
        result = conn.execute(
            text(
                """
                INSERT INTO t_schedule_occurrences
                    (schedule_key, predict_date, feature_date, policy_version,
                     policy_sha256, policy_json, registry_digest, completion_state,
                     expected_item_count, expected_target_count,
                     accepted_target_count, sla_deadline_at,
                     recovery_cutoff_at, sla_outcome)
                VALUES
                    (:schedule_key, :predict_date, :feature_date,
                     :policy_version, :policy_sha256, :policy_json, :registry_digest,
                     :completion_state, :expected_item_count,
                     :expected_target_count, 0, :sla_deadline_at,
                     :recovery_cutoff_at, 'PENDING')
                """
            ),
            occurrence_params,
        )
        occurrence_id = _result_lastrowid(conn, result)
        for item in snapshot.items:
            item_result = conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_items
                        (occurrence_id, base_scheme_id, runtime_type,
                         scheme_version, code_sha256, config_sha256,
                         cache_group, input_generation_id, resource_class,
                         internal_workers, release_offset_minutes, release_at,
                         deadline_at, state, attempt_no)
                    VALUES
                        (:occurrence_id, :base_scheme_id, :runtime_type,
                         :scheme_version, :code_sha256, :config_sha256,
                         :cache_group, NULL, :resource_class,
                         :internal_workers, :release_offset_minutes,
                         :release_at, :deadline_at, 'PENDING', 0)
                    """
                ),
                {
                    "occurrence_id": occurrence_id,
                    "base_scheme_id": item.base_scheme_id,
                    "runtime_type": item.runtime_type,
                    "scheme_version": item.scheme_version,
                    "code_sha256": item.code_sha256,
                    "config_sha256": item.config_sha256,
                    "cache_group": item.cache_group,
                    "resource_class": item.resource_class,
                    "internal_workers": item.internal_workers,
                    "release_offset_minutes": (
                        item.release_offset_minutes
                    ),
                    "release_at": item.release_at,
                    "deadline_at": item.deadline_at,
                },
            )
            item_id = _result_lastrowid(conn, item_result)
            conn.execute(
                text(
                    """
                    INSERT INTO t_schedule_item_targets
                        (occurrence_id, item_id, registry_scheme_id,
                         base_scheme_id, runtime_type, task_type,
                         target_tenor, horizon, target_date, status)
                    VALUES
                        (:occurrence_id, :item_id, :registry_scheme_id,
                         :base_scheme_id, :runtime_type, :task_type,
                         :target_tenor, :horizon, :target_date, 'PENDING')
                    """
                ),
                [
                    {
                        "occurrence_id": occurrence_id,
                        "item_id": item_id,
                        "registry_scheme_id": target.registry_scheme_id,
                        "base_scheme_id": target.base_scheme_id,
                        "runtime_type": target.runtime_type,
                        "task_type": target.task_type,
                        "target_tenor": target.target_tenor,
                        "horizon": target.horizon,
                        "target_date": target.target_date,
                    }
                    for target in item.targets
                ],
            )
        actual_item_count = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_items "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        )
        actual_target_count = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_schedule_item_targets "
                    "WHERE occurrence_id = :occurrence_id"
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        )
        validate_snapshot_cardinality(
            snapshot,
            actual_item_count=actual_item_count,
            actual_target_count=actual_target_count,
        )
        return occurrence_id


def read_current_replay_attempt_processes(
    engine: Engine,
    *,
    occurrence_id: int,
    active_item_ids: Iterable[int],
) -> tuple[CurrentReplayAttemptProcess, ...]:
    """只返回当前隔离 replay active future 的 current running 进程组。"""
    normalized_occurrence_id = int(occurrence_id)
    if normalized_occurrence_id <= 0:
        raise ValueError("occurrence_id must be positive")
    normalized_item_ids = tuple(
        sorted({int(item_id) for item_id in active_item_ids})
    )
    if any(item_id <= 0 for item_id in normalized_item_ids):
        raise ValueError("active_item_ids must be positive")
    if not normalized_item_ids:
        return ()
    placeholders = ", ".join(
        f":active_item_id_{index}"
        for index in range(len(normalized_item_ids))
    )
    parameters: dict[str, object] = {
        "occurrence_id": normalized_occurrence_id,
        **{
            f"active_item_id_{index}": item_id
            for index, item_id in enumerate(normalized_item_ids)
        },
    }
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT i.item_id, r.run_id, r.execution_token,
                       r.process_id, r.process_group_id,
                       i.base_scheme_id, o.policy_json
                FROM t_schedule_occurrences o
                JOIN t_schedule_items i
                  ON i.occurrence_id = o.occurrence_id
                JOIN t_scheme_runs r
                  ON r.run_id = i.current_run_id
                 AND r.schedule_item_id = i.item_id
                 AND r.attempt_no = i.attempt_no
                WHERE o.occurrence_id = :occurrence_id
                  AND o.schedule_key LIKE
                      'isolated-real-replay-v1-%'
                  AND o.completion_state IN ('PENDING', 'RUNNING')
                  AND i.item_id IN ({placeholders})
                  AND i.state = 'RUNNING'
                  AND i.failure_code IS NULL
                  AND i.started_at IS NOT NULL
                  AND r.scheme_id = i.base_scheme_id
                  AND r.scheme_version = i.scheme_version
                  AND r.runtime_type = i.runtime_type
                  AND r.predict_date = o.predict_date
                  AND r.run_type = 'active'
                  AND r.prediction_phase = 'scheduled_live'
                  AND r.trigger_origin = 'operator_recovery'
                  AND r.status = 'running'
                  AND r.failure_code IS NULL
                  AND r.execution_token IS NOT NULL
                  AND r.execution_token <> ''
                  AND r.process_id > 1
                  AND r.process_group_id > 1
                  AND r.process_id = r.process_group_id
                  AND r.started_at = i.started_at
                  AND r.finished_at IS NULL
                ORDER BY i.item_id
                """
            ),
            parameters,
        ).mappings().all()
    processes: list[CurrentReplayAttemptProcess] = []
    for row in rows:
        base_scheme_id = str(row["base_scheme_id"])
        policy = _stored_json_mapping(row, "policy_json")
        raw_schemes = policy.get("schemes")
        matches = (
            [
                entry
                for entry in raw_schemes
                if (
                    isinstance(entry, Mapping)
                    and entry.get("scheme_id") == base_scheme_id
                )
            ]
            if isinstance(raw_schemes, list)
            else []
        )
        if len(matches) != 1:
            raise RuntimeError(
                "replay process frozen input mode is missing"
            )
        input_compatibility = matches[0].get(
            "input_compatibility"
        )
        if (
            not isinstance(input_compatibility, str)
            or not input_compatibility
        ):
            raise RuntimeError(
                "replay process frozen input mode is invalid"
            )
        processes.append(
            CurrentReplayAttemptProcess(
                item_id=int(row["item_id"]),
                run_id=int(row["run_id"]),
                execution_token=str(row["execution_token"]),
                process_id=int(row["process_id"]),
                process_group_id=int(row["process_group_id"]),
                base_scheme_id=base_scheme_id,
                input_compatibility=input_compatibility,
            )
        )
    return tuple(processes)


def read_schedule_execution_envelope(
    engine: Engine,
    *,
    item_id: int,
) -> ScheduleExecutionEnvelope:
    """单事务读取 item 执行所需的全部冻结 ledger 身份。"""
    with engine.begin() as conn:
        item = _read_schedule_item_conn(
            conn,
            item_id=int(item_id),
            for_update=False,
        )
        if item is None:
            raise RuntimeError(f"schedule item not found: {item_id}")
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(item["occurrence_id"]),
            for_update=False,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {item['occurrence_id']}"
            )
        generation_id = item.get("input_generation_id")
        if generation_id is None:
            raise RuntimeError(
                f"schedule item has no bound input generation: {item_id}"
            )
        generation = _read_input_generation_conn(
            conn,
            str(generation_id),
            for_update=False,
        )
        if generation is None:
            raise RuntimeError(
                f"bound input generation not found: {generation_id}"
            )
        if str(generation.get("state")) != GENERATION_SEALED:
            raise RuntimeError(
                f"bound input generation is not SEALED: {generation_id}"
            )
        generation_type = str(generation.get("generation_type"))
        if generation_type == "native_source":
            if (
                generation.get("native_generation_id") is not None
                or generation.get("native_manifest_sha256") is not None
            ):
                raise RuntimeError(
                    "native_source generation cannot reference another "
                    "native generation"
                )
            calendar_generation = generation
        elif generation_type == "databridge_v1":
            native_generation_id = generation.get(
                "native_generation_id"
            )
            if not native_generation_id:
                raise RuntimeError(
                    "databridge generation is missing native_generation_id"
                )
            calendar_generation = _read_input_generation_conn(
                conn,
                str(native_generation_id),
                for_update=False,
            )
            if calendar_generation is None:
                raise RuntimeError(
                    "native generation not found: "
                    f"{native_generation_id}"
                )
            _validate_databridge_native_relation(
                databridge=generation,
                native_generation=calendar_generation,
            )
        else:
            raise RuntimeError(
                f"unsupported stored generation_type: {generation_type}"
            )
        targets = _read_schedule_targets_conn(
            conn,
            item_id=int(item_id),
            for_update=False,
        )
        return ScheduleExecutionEnvelope(
            occurrence=_schedule_occurrence_envelope(occurrence),
            item=_schedule_item_envelope(item, occurrence),
            generation=_schedule_generation_envelope(generation),
            calendar_generation=_schedule_generation_envelope(
                calendar_generation
            ),
            targets=tuple(
                _schedule_target_envelope(target)
                for target in targets
            ),
        )


def read_schedule_occurrence_snapshot(
    engine: Engine,
    *,
    occurrence_id: int,
) -> ScheduleOccurrenceSnapshot:
    """单事务读取 occurrence、ordered items 与 target 聚合。"""
    normalized_occurrence_id = int(occurrence_id)
    with engine.begin() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=normalized_occurrence_id,
            for_update=False,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        items = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=normalized_occurrence_id,
            for_update=False,
        )
        targets = _read_schedule_targets_for_occurrence_conn(
            conn,
            occurrence_id=normalized_occurrence_id,
        )
        target_counts: Counter[int] = Counter()
        accepted_counts: Counter[int] = Counter()
        for target in targets:
            target_item_id = int(target["item_id"])
            target_counts[target_item_id] += 1
            if _is_valid_accepted_schedule_target(target):
                accepted_counts[target_item_id] += 1
        state_counts = Counter(str(item["state"]) for item in items)
        summaries = tuple(
            ScheduleOccurrenceItemSummary(
                item=_schedule_item_envelope(item, occurrence),
                target_count=target_counts[int(item["item_id"])],
                accepted_target_count=accepted_counts[
                    int(item["item_id"])
                ],
            )
            for item in items
        )
        return ScheduleOccurrenceSnapshot(
            occurrence=_schedule_occurrence_envelope(occurrence),
            items=summaries,
            actual_item_count=len(items),
            actual_target_count=len(targets),
            actual_accepted_target_count=sum(accepted_counts.values()),
            item_state_counts=tuple(sorted(state_counts.items())),
        )


def upsert_scheduler_heartbeat(
    engine: Engine,
    *,
    service_name: str,
    process_id: int,
    host_name: str,
    state: str,
    occurrence_id: int | None,
    details: Mapping[str, object] | None = None,
    _clock: _LedgerClock | None = None,
) -> SchedulerHeartbeat:
    """在独立服务行上 upsert 跨进程 heartbeat，不触碰 run/ledger 锁。"""
    normalized_service = _require_bounded_identifier(
        service_name,
        "service_name",
        max_length=64,
    )
    normalized_host = _require_nonempty(host_name, "host_name")
    if len(normalized_host) > 255:
        raise ValueError("host_name must be at most 255 characters")
    normalized_state = _require_bounded_identifier(
        state,
        "state",
        max_length=32,
    )
    normalized_process_id = int(process_id)
    if normalized_process_id <= 0:
        raise ValueError("process_id must be a positive integer")
    normalized_occurrence_id = (
        None if occurrence_id is None else int(occurrence_id)
    )
    if (
        normalized_occurrence_id is not None
        and normalized_occurrence_id <= 0
    ):
        raise ValueError("occurrence_id must be a positive integer")
    normalized_details = dict(details or {})
    if normalized_service == "daily-coordinator":
        _assert_deployed_epoch_payload(
            normalized_details.get("daily_coordinator_epoch"),
            label="daily coordinator heartbeat epoch",
            engine=engine,
        )
    details_text = json.dumps(
        normalized_details,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    effective_time = _trusted_ledger_now(_clock)
    params = {
        "service_name": normalized_service,
        "process_id": normalized_process_id,
        "host_name": normalized_host,
        "state": normalized_state,
        "occurrence_id": normalized_occurrence_id,
        "heartbeat_at": effective_time,
        "details_json": details_text,
    }
    with engine.begin() as conn:
        if normalized_service == "daily-coordinator":
            _assert_deployed_epoch_payload(
                normalized_details.get("daily_coordinator_epoch"),
                label="daily coordinator heartbeat epoch",
                engine=conn,
            )
            if normalized_occurrence_id is not None:
                occurrence = _read_schedule_occurrence_by_id_conn(
                    conn,
                    occurrence_id=normalized_occurrence_id,
                    for_update=False,
                )
                if occurrence is None:
                    raise RuntimeError(
                        "scheduler heartbeat occurrence is missing"
                    )
                _assert_occurrence_epoch_conn(occurrence, engine=conn)
        if _dialect_name(conn) == "sqlite":
            sql = text(
                """
                INSERT INTO t_scheduler_heartbeat
                    (service_name, process_id, host_name, state,
                     occurrence_id, heartbeat_at, details_json)
                VALUES
                    (:service_name, :process_id, :host_name, :state,
                     :occurrence_id, :heartbeat_at, :details_json)
                ON CONFLICT(service_name) DO UPDATE SET
                    process_id = excluded.process_id,
                    host_name = excluded.host_name,
                    state = excluded.state,
                    occurrence_id = excluded.occurrence_id,
                    heartbeat_at = excluded.heartbeat_at,
                    details_json = excluded.details_json,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
        else:
            sql = text(
                """
                INSERT INTO t_scheduler_heartbeat
                    (service_name, process_id, host_name, state,
                     occurrence_id, heartbeat_at, details_json)
                VALUES
                    (:service_name, :process_id, :host_name, :state,
                     :occurrence_id, :heartbeat_at,
                     CAST(:details_json AS JSON))
                ON DUPLICATE KEY UPDATE
                    process_id = VALUES(process_id),
                    host_name = VALUES(host_name),
                    state = VALUES(state),
                    occurrence_id = VALUES(occurrence_id),
                    heartbeat_at = VALUES(heartbeat_at),
                    details_json = VALUES(details_json),
                    updated_at = UTC_TIMESTAMP(6)
                """
            )
        conn.execute(sql, params)
        stored = _read_scheduler_heartbeat_conn(
            conn,
            service_name=normalized_service,
        )
        if stored is None:
            raise RuntimeError(
                f"scheduler heartbeat upsert was not visible: "
                f"{normalized_service}"
            )
        return _scheduler_heartbeat(stored)


def _occurrence_feature_date(
    occurrence: Mapping[str, object],
    *,
    expected_feature_date: str | None,
) -> str:
    """只信 occurrence 冻结日；调用方日期仅作额外一致性断言。"""
    stored = occurrence.get("feature_date")
    if stored is None:
        raise RuntimeError(
            "schedule occurrence feature_date is missing; "
            "legacy occurrence must be reconciled explicitly"
        )
    normalized = date.fromisoformat(str(stored)).isoformat()
    predict_date = date.fromisoformat(
        str(occurrence.get("predict_date"))
    ).isoformat()
    if normalized >= predict_date:
        raise RuntimeError(
            "schedule occurrence feature_date must be earlier than "
            f"predict_date: {normalized} >= {predict_date}"
        )
    if expected_feature_date is not None:
        expected = date.fromisoformat(
            expected_feature_date
        ).isoformat()
        if expected != normalized:
            raise RuntimeError(
                "caller expected_feature_date does not match occurrence "
                f"feature_date: {expected} != {normalized}"
            )
    return normalized


def _require_databridge_parent_bound_to_native_items(
    siblings: Iterable[Mapping[str, object]],
    *,
    native_generation_id: str,
) -> None:
    """DataBridge 只能复用同 occurrence 已冻结到 Native items 的父代。"""
    native_items = [
        item
        for item in siblings
        if str(item.get("runtime_type")) == "native_adapter"
    ]
    mismatched = [
        {
            "item_id": int(item["item_id"]),
            "input_generation_id": item.get("input_generation_id"),
        }
        for item in native_items
        if str(item.get("input_generation_id") or "")
        != native_generation_id
    ]
    if mismatched:
        raise RuntimeError(
            "all Native runtime items must be bound to the DataBridge "
            f"parent generation {native_generation_id}: {mismatched}"
        )


def _seal_and_bind_schedule_occurrence_generation_conn(
    conn: Connection,
    *,
    occurrence: Mapping[str, object],
    siblings: Iterable[Mapping[str, object]],
    generation: Mapping[str, object],
    native_generation: Mapping[str, object] | None,
    expected_feature_date: str | None,
    building_sealed_at: datetime,
    require_already_sealed: bool,
) -> tuple[str, int]:
    """在已取得 occurrence→items→父 generation→子 generation 锁后提交。"""
    normalized_generation_id = str(generation["generation_id"])
    sibling_rows = tuple(dict(item) for item in siblings)
    generation_state = str(generation.get("state"))
    if generation_state not in {
        GENERATION_BUILDING,
        GENERATION_SEALED,
    }:
        raise RuntimeError(
            "input generation cannot be sealed/bound from state="
            f"{generation_state}: {normalized_generation_id}"
        )
    if require_already_sealed and generation_state != GENERATION_SEALED:
        raise RuntimeError(
            f"input generation is not SEALED: {normalized_generation_id}"
        )
    generation_type = str(generation.get("generation_type"))
    if generation_type == "databridge_v1":
        if native_generation is None:
            raise RuntimeError(
                "databridge generation is missing locked native parent"
            )
        _validate_databridge_native_relation(
            databridge=generation,
            native_generation=native_generation,
        )
        _require_databridge_parent_bound_to_native_items(
            sibling_rows,
            native_generation_id=str(
                native_generation["generation_id"]
            ),
        )
    elif generation_type != "native_source":
        raise RuntimeError(
            f"unsupported stored generation_type: {generation_type}"
        )

    frozen_feature_date = _occurrence_feature_date(
        occurrence,
        expected_feature_date=expected_feature_date,
    )
    if str(generation.get("business_date")) != str(
        occurrence.get("predict_date")
    ):
        raise RuntimeError(
            "input generation business_date does not match occurrence "
            f"predict_date: {generation.get('business_date')} != "
            f"{occurrence.get('predict_date')}"
        )
    if str(generation.get("feature_date")) != frozen_feature_date:
        raise RuntimeError(
            "input generation feature_date does not match occurrence "
            f"feature_date: {generation.get('feature_date')} != "
            f"{frozen_feature_date}"
        )

    expected_runtime_type = (
        "blackbox_v2"
        if generation_type == "databridge_v1"
        else "native_adapter"
    )
    runtime_items = [
        item
        for item in sibling_rows
        if str(item.get("runtime_type")) == expected_runtime_type
    ]
    if not runtime_items:
        raise RuntimeError(
            "occurrence has no item for generation runtime: "
            f"{generation_type}"
        )
    conflicting = {
        str(item["input_generation_id"])
        for item in runtime_items
        if (
            item.get("input_generation_id") is not None
            and str(item["input_generation_id"])
            != normalized_generation_id
        )
    }
    if conflicting:
        raise RuntimeError(
            "same occurrence runtime generation must be unique: "
            f"existing={sorted(conflicting)} "
            f"requested={normalized_generation_id}"
        )

    effective_sealed_at = building_sealed_at
    if generation_state == GENERATION_SEALED:
        if generation.get("sealed_at") is None:
            raise RuntimeError(
                "SEALED input generation has no sealed_at: "
                f"{normalized_generation_id}"
            )
        effective_sealed_at = _as_datetime(
            generation["sealed_at"],
            "sealed_at",
        )
    finalized_releases: dict[int, datetime] = {}
    recovery_cutoff = _as_datetime(
        occurrence["recovery_cutoff_at"],
        "recovery_cutoff_at",
    )
    for item in runtime_items:
        release_at = _as_datetime(item["release_at"], "release_at")
        if expected_runtime_type == "blackbox_v2":
            release_at = max(
                release_at,
                effective_sealed_at
                + timedelta(
                    minutes=int(item["release_offset_minutes"])
                ),
            )
            if release_at >= recovery_cutoff:
                raise RuntimeError(
                    "finalized release_at must be earlier than "
                    "occurrence recovery_cutoff_at"
                )
        finalized_releases[int(item["item_id"])] = release_at

    if generation_state == GENERATION_BUILDING:
        sealed = conn.execute(
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
                "sealed": GENERATION_SEALED,
                "sealed_at": effective_sealed_at,
                "generation_id": normalized_generation_id,
                "building": GENERATION_BUILDING,
            },
        )
        _require_rowcount(
            sealed,
            1,
            "input generation atomic seal",
        )

    occurrence_id = int(occurrence["occurrence_id"])
    for item in runtime_items:
        if str(item.get("input_generation_id") or "") == (
            normalized_generation_id
        ):
            continue
        bound = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET input_generation_id = :generation_id,
                    release_at = :release_at
                WHERE item_id = :item_id
                  AND occurrence_id = :occurrence_id
                  AND input_generation_id IS NULL
                """
            ),
            {
                "generation_id": normalized_generation_id,
                "release_at": finalized_releases[
                    int(item["item_id"])
                ],
                "item_id": int(item["item_id"]),
                "occurrence_id": occurrence_id,
            },
        )
        _require_rowcount(
            bound,
            1,
            "schedule occurrence generation bind",
        )
    return normalized_generation_id, len(runtime_items)


def register_seal_and_bind_schedule_occurrence_generation(
    engine: Engine,
    *,
    occurrence_id: int,
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
    expected_feature_date: str | None = None,
    _clock: _LedgerClock | None = None,
) -> tuple[str, int]:
    """单事务创建/精确恢复、封存并绑定 occurrence generation。"""
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
        native_generation_id=native_generation_id,
        native_manifest_sha256=native_manifest_sha256,
    )
    if (
        params["generation_type"] == "native_source"
        and params["exporter_version"]
        == SIGNAL_GAP_NATIVE_EXPORTER_VERSION
    ):
        raise ValueError(
            "daily ledger Native registration rejects the signal-gap "
            "special exporter_version"
        )
    with engine.begin() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        frozen_feature_date = _occurrence_feature_date(
            occurrence,
            expected_feature_date=expected_feature_date,
        )
        if str(params["business_date"]) != str(
            occurrence["predict_date"]
        ):
            raise RuntimeError(
                "input generation business_date does not match occurrence "
                f"predict_date: {params['business_date']} != "
                f"{occurrence['predict_date']}"
            )
        if str(params["feature_date"]) != frozen_feature_date:
            raise RuntimeError(
                "input generation feature_date does not match occurrence "
                f"feature_date: {params['feature_date']} != "
                f"{frozen_feature_date}"
            )
        native_generation = _lock_registration_native_parent_conn(
            conn,
            params,
        )
        if native_generation is not None:
            _require_databridge_parent_bound_to_native_items(
                siblings,
                native_generation_id=str(
                    native_generation["generation_id"]
                ),
            )
        generation = _create_or_match_input_generation_conn(
            conn,
            params,
            native_generation=native_generation,
        )
        trusted_sealed_at, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                None,
                field="sealed_at",
                clock=_clock,
            )
        )
        return _seal_and_bind_schedule_occurrence_generation_conn(
            conn,
            occurrence=occurrence,
            siblings=siblings,
            generation=generation,
            native_generation=native_generation,
            expected_feature_date=expected_feature_date,
            building_sealed_at=trusted_sealed_at,
            require_already_sealed=False,
        )


def seal_and_bind_schedule_occurrence_generation(
    engine: Engine,
    *,
    occurrence_id: int,
    generation_id: str,
    expected_feature_date: str,
    sealed_at: datetime | None = None,
    _require_already_sealed: bool = False,
) -> tuple[str, int]:
    """兼容已登记 generation 的 occurrence 级原子封存/绑定入口。"""
    normalized_generation_id = _require_nonempty(
        generation_id,
        "generation_id",
    )
    normalized_feature_date = date.fromisoformat(
        expected_feature_date
    ).isoformat()
    requested_sealed_at = _utc_datetime6(sealed_at)
    with engine.begin() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        generation, native_generation = (
            _lock_input_generation_with_parent_conn(
                conn,
                normalized_generation_id,
            )
        )
        if generation is None:
            raise RuntimeError(
                f"input generation not found: {normalized_generation_id}"
            )
        return _seal_and_bind_schedule_occurrence_generation_conn(
            conn,
            occurrence=occurrence,
            siblings=siblings,
            generation=generation,
            native_generation=native_generation,
            expected_feature_date=normalized_feature_date,
            building_sealed_at=requested_sealed_at,
            require_already_sealed=_require_already_sealed,
        )


def start_schedule_attempt(
    engine: Engine,
    *,
    item_id: int,
    trigger_origin: str = "apscheduler",
    execution_token: str | None = None,
    queued_at: datetime | None = None,
    process_id: int | None = None,
    process_group_id: int | None = None,
    started_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> ScheduledAttempt:
    """在 item fence 下 claim 唯一 attempt；不伪造真实进程启动时刻。"""
    if process_id is not None or process_group_id is not None:
        raise ValueError(
            "process identity must be registered by the Popen callback"
        )
    normalized_origin = _require_schedule_trigger_origin(trigger_origin)
    normalized_token = _require_bounded_identifier(
        execution_token or uuid.uuid4().hex,
        "execution_token",
        max_length=128,
    )
    with engine.begin() as conn:
        locator = _read_schedule_item_locator_conn(
            conn,
            item_id=int(item_id),
        )
        if locator is None:
            raise RuntimeError(f"schedule item not found: {item_id}")
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(locator["occurrence_id"]),
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {locator['occurrence_id']}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(locator["occurrence_id"]),
            for_update=True,
        )
        stored_item = next(
            (
                row
                for row in siblings
                if int(row["item_id"]) == int(item_id)
            ),
            None,
        )
        if stored_item is None:
            raise RuntimeError(f"schedule item not found: {item_id}")
        item = {
            **dict(stored_item),
            "predict_date": occurrence["predict_date"],
            "recovery_cutoff_at": occurrence["recovery_cutoff_at"],
            "sla_deadline_at": occurrence["sla_deadline_at"],
        }
        effective_claimed_at, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                started_at,
                field="claimed_at",
                clock=_clock,
            )
        )
        if queued_at is None:
            effective_queued_at = effective_claimed_at
        else:
            effective_queued_at, _ = _ledger_event_time_after_locks(
                conn,
                queued_at,
                field="queued_at",
                clock=_clock,
            )
            if effective_queued_at > effective_claimed_at:
                raise RuntimeError(
                    "queued_at cannot be after claimed_at"
                )
        state = str(item["state"])
        allowed_states = {ITEM_PENDING, ITEM_RETRY_WAIT, ITEM_ABANDONED}
        if state not in allowed_states:
            raise RuntimeError(
                f"schedule item cannot start from state={state}: {item_id}"
            )
        if (
            state == ITEM_ABANDONED
            and str(item.get("failure_code") or "")
            != _ABANDONED_ORPHAN_CLEANUP
        ):
            raise RuntimeError(
                "orphan cleanup confirmation is required before recovery: "
                f"item_id={item_id}"
            )
        cutoff = _as_datetime(
            item["recovery_cutoff_at"],
            "recovery_cutoff_at",
        )
        if effective_claimed_at >= cutoff:
            raise RuntimeError(
                f"schedule item cannot start at/after recovery cutoff: {item_id}"
            )
        release_at = _as_datetime(item["release_at"], "release_at")
        if effective_claimed_at < release_at:
            raise RuntimeError(
                "schedule item cannot start before release_at: "
                f"{effective_claimed_at.isoformat()} < {release_at.isoformat()}"
            )
        generation_id = item.get("input_generation_id")
        if not generation_id:
            raise RuntimeError(
                f"schedule item has no bound input generation: {item_id}"
            )
        generation = _read_input_generation_conn(
            conn,
            str(generation_id),
            for_update=True,
        )
        if generation is None:
            raise RuntimeError(
                f"bound input generation not found: {generation_id}"
            )
        if generation.get("state") != GENERATION_SEALED:
            raise RuntimeError(
                f"bound input generation is not SEALED: {generation_id}"
            )
        attempt_no = int(item["attempt_no"]) + 1
        if attempt_no > _MAX_SCHEDULE_ATTEMPTS:
            raise RuntimeError(
                f"schedule item exhausted {_MAX_SCHEDULE_ATTEMPTS} attempts: "
                f"{item_id}"
            )
        if (
            attempt_no == 2
            and normalized_origin not in _SECOND_ATTEMPT_TRIGGER_ORIGINS
            and not (
                state == ITEM_ABANDONED
                and normalized_origin == "startup_catchup"
            )
        ):
            raise ValueError(
                "attempt 2 trigger_origin must be auto_retry or "
                "operator_recovery, except startup_catchup for ABANDONED"
            )
        if (
            state == ITEM_ABANDONED
            and normalized_origin not in _ABANDONED_TRIGGER_ORIGINS
        ):
            raise ValueError(
                "ABANDONED item trigger_origin must be startup_catchup or "
                "operator_recovery"
            )
        if attempt_no == 2:
            waiting_items = sum(
                1
                for sibling in siblings
                if not is_first_attempt_covered(
                    state=str(sibling["state"]),
                    attempt_no=int(sibling["attempt_no"]),
                )
            )
            if waiting_items:
                raise RuntimeError(
                    "schedule occurrence first-attempt barrier is not "
                    f"satisfied: waiting_items={waiting_items}"
                )

        current_run_id = item.get("current_run_id")
        if current_run_id is not None:
            previous = _read_schedule_run_conn(
                conn,
                run_id=int(current_run_id),
                for_update=True,
            )
            if previous is None:
                raise RuntimeError(
                    f"schedule item current run is missing: {current_run_id}"
                )
            if previous.get("status") == "running":
                raise RuntimeError(
                    "schedule item still has a RUNNING current run; "
                    "confirmed abandon is required"
                )

        targets = _read_schedule_targets_conn(
            conn,
            item_id=int(item_id),
            for_update=True,
        )
        expected_targets = len(targets)
        if expected_targets <= 0:
            raise RuntimeError(
                f"schedule item has no frozen targets: {item_id}"
            )
        run_id = _create_scheme_run_conn(
            conn,
            scheme_id=str(item["base_scheme_id"]),
            predict_date=str(item["predict_date"]),
            scheme_version=str(item["scheme_version"]),
            runtime_type=str(item["runtime_type"]),
            run_type="active",
            prediction_phase="scheduled_live",
            status="running",
            records_expected=expected_targets,
            schedule_item_id=int(item_id),
            attempt_no=attempt_no,
            trigger_origin=normalized_origin,
            queued_at=effective_queued_at,
            execution_token=normalized_token,
            process_id=None,
            process_group_id=None,
        )
        # t_scheme_runs 的 legacy DDL 对 started_at 有 CURRENT_TIMESTAMP
        # 默认值；ledger claim 必须显式清空，真实启动只由 Popen callback 写入。
        conn.execute(
            text(
                "UPDATE t_scheme_runs SET started_at = NULL "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :running,
                    attempt_no = :attempt_no,
                    current_run_id = :run_id,
                    started_at = NULL,
                    completed_at = NULL,
                    failure_code = NULL,
                    failure_message = NULL
                WHERE item_id = :item_id
                  AND attempt_no = :previous_attempt_no
                """
            ),
            {
                "running": ITEM_RUNNING,
                "attempt_no": attempt_no,
                "run_id": run_id,
                "item_id": int(item_id),
                "previous_attempt_no": attempt_no - 1,
            },
        )
        _require_rowcount(result, 1, "schedule attempt fence")
        conn.execute(
            text(
                """
                UPDATE t_schedule_occurrences
                SET completion_state = :running,
                    started_at = COALESCE(started_at, :started_at),
                    completed_at = NULL
                WHERE occurrence_id = :occurrence_id
                  AND (
                      completion_state IN (:pending, :already_running)
                      OR (
                          completion_state = :failed
                          AND NOT EXISTS (
                              SELECT 1
                              FROM t_schedule_items terminal_item
                              WHERE terminal_item.occurrence_id =
                                    t_schedule_occurrences.occurrence_id
                                AND terminal_item.state IN (
                                    :failed_terminal,
                                    :expired
                                )
                          )
                      )
                  )
                """
            ),
            {
                "running": OCCURRENCE_RUNNING,
                "started_at": effective_claimed_at,
                "occurrence_id": int(item["occurrence_id"]),
                "pending": OCCURRENCE_PENDING,
                "already_running": OCCURRENCE_RUNNING,
                "failed": OCCURRENCE_FAILED,
                "failed_terminal": ITEM_FAILED_TERMINAL,
                "expired": ITEM_EXPIRED,
            },
        )
    return ScheduledAttempt(
        item_id=int(item_id),
        run_id=run_id,
        attempt_no=attempt_no,
        execution_token=normalized_token,
    )


def register_schedule_attempt_process(
    engine: Engine,
    *,
    run_id: int,
    execution_token: str,
    process_id: int,
    process_group_id: int,
    started_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> tuple[int, int]:
    """在当前 attempt fence 内一次性登记真实子进程身份。

    Popen 成功后、进入 communicate 前必须调用本函数。同一身份重放是
    幂等的；token、PID 或 PGID 漂移一律 fail-closed。
    """
    normalized_token = _require_bounded_identifier(
        execution_token,
        "execution_token",
        max_length=128,
    )
    normalized_process_id = int(process_id)
    normalized_process_group_id = int(process_group_id)
    if normalized_process_id <= 0:
        raise ValueError("process_id must be a positive integer")
    if normalized_process_group_id <= 0:
        raise ValueError("process_group_id must be a positive integer")
    with engine.begin() as conn:
        item_id = _resolve_schedule_item_id_for_run(
            conn,
            run_id=int(run_id),
        )
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=item_id,
        )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != item_id
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")
        if (
            int(item.get("current_run_id") or 0) != int(run_id)
            or item.get("state") != ITEM_RUNNING
            or run.get("status") != "running"
        ):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        if str(run.get("execution_token") or "") != normalized_token:
            raise RuntimeError(
                f"scheduled run execution token mismatch: run_id={run_id}"
            )
        stored_process_id = run.get("process_id")
        stored_process_group_id = run.get("process_group_id")
        if (
            stored_process_id is not None
            or stored_process_group_id is not None
        ):
            if (
                stored_process_id is None
                or stored_process_group_id is None
                or int(stored_process_id) != normalized_process_id
                or int(stored_process_group_id)
                != normalized_process_group_id
            ):
                raise RuntimeError(
                    "scheduled run process identity drift rejected: "
                    f"run_id={run_id}"
                )
            if run.get("started_at") is not None:
                if (
                    item.get("started_at") is None
                    or _as_datetime(
                        item["started_at"],
                        "item started_at",
                    )
                    != _as_datetime(
                        run["started_at"],
                        "run started_at",
                    )
                ):
                    raise RuntimeError(
                        "scheduled run/item process start identity drift "
                        f"rejected: run_id={run_id}"
                    )
                return normalized_process_id, normalized_process_group_id

        effective_started_at, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                started_at,
                field="started_at",
                clock=_clock,
            )
        )
        cutoff = _as_datetime(
            item["recovery_cutoff_at"],
            "recovery_cutoff_at",
        )
        if effective_started_at >= cutoff:
            raise RuntimeError(
                "schedule process cannot start at/after recovery cutoff: "
                f"run_id={run_id}"
            )
        _require_not_before(
            effective_started_at,
            run.get("queued_at"),
            event_field="started_at",
            lower_field="run queued_at",
        )

        updated = conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET process_id = :process_id,
                    process_group_id = :process_group_id,
                    started_at = :started_at
                WHERE run_id = :run_id
                  AND status = 'running'
                  AND execution_token = :execution_token
                  AND (
                      (process_id IS NULL AND process_group_id IS NULL)
                      OR (
                          process_id = :process_id
                          AND process_group_id = :process_group_id
                      )
                  )
                  AND started_at IS NULL
                """
            ),
            {
                "process_id": normalized_process_id,
                "process_group_id": normalized_process_group_id,
                "started_at": effective_started_at,
                "run_id": int(run_id),
                "execution_token": normalized_token,
            },
        )
        _require_rowcount(updated, 1, "schedule process identity register")
        item_updated = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET started_at = :started_at
                WHERE item_id = :item_id
                  AND current_run_id = :run_id
                  AND state = :running
                  AND started_at IS NULL
                """
            ),
            {
                "started_at": effective_started_at,
                "item_id": item_id,
                "run_id": int(run_id),
                "running": ITEM_RUNNING,
            },
        )
        _require_rowcount(
            item_updated,
            1,
            "schedule item process start register",
        )
        _persist_v2_start_guardrail_conn(
            conn,
            item=item,
            started_at=effective_started_at,
            evaluated_at=effective_started_at,
        )
    return normalized_process_id, normalized_process_group_id


def fence_current_schedule_attempt(
    engine: Engine,
    *,
    item_id: int,
    fenced_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> FencedScheduleAttempt:
    """先在数据库拒绝旧 attempt 完成，再允许清理其进程组。

    fence 提交后 item 保持不可重排，直至
    ``confirm_schedule_attempt_orphan_cleanup`` 成功。这样即使进程清理与
    恢复动作之间崩溃，旧 attempt 也无法发布结果。
    """
    effective_time, _trusted_now = _ledger_event_time(
        fenced_at,
        field="fenced_at",
        clock=_clock,
    )
    with engine.begin() as conn:
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=int(item_id),
        )
        run_id = int(item.get("current_run_id") or 0)
        if run_id <= 0:
            raise RuntimeError(
                f"schedule item has no current run to fence: {item_id}"
            )
        run = _read_schedule_run_conn(
            conn,
            run_id=run_id,
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != int(item_id)
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")

        item_failure_code = str(item.get("failure_code") or "")
        run_failure_code = str(run.get("failure_code") or "")
        already_fenced = (
            item.get("state") == ITEM_ABANDONED
            and run.get("status") == "failed"
            and item_failure_code
            in {
                _ABANDONED_FENCE_PENDING_CLEANUP,
                _ABANDONED_ORPHAN_CLEANUP,
            }
            and run_failure_code == item_failure_code
        )
        if not already_fenced:
            if (
                item.get("state") != ITEM_RUNNING
                or run.get("status") != "running"
            ):
                raise RuntimeError(
                    f"schedule item is not RUNNING: {item_id}"
                )
            _require_run_lifecycle_time(
                effective_time,
                run,
                event_field="fenced_at",
            )
            run_result = conn.execute(
                text(
                    """
                    UPDATE t_scheme_runs
                    SET status = 'failed',
                        failure_code = :failure_code,
                        error_message = :failure_message,
                        finished_at = :finished_at
                    WHERE run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "failure_code": _ABANDONED_FENCE_PENDING_CLEANUP,
                    "failure_message": (
                        "attempt fenced before orphan cleanup confirmation"
                    ),
                    "finished_at": effective_time,
                    "run_id": run_id,
                },
            )
            _require_rowcount(run_result, 1, "schedule run recovery fence")
            item_result = conn.execute(
                text(
                    """
                    UPDATE t_schedule_items
                    SET state = :abandoned,
                        failure_code = :failure_code,
                        failure_message = :failure_message,
                        completed_at = :completed_at
                    WHERE item_id = :item_id
                      AND current_run_id = :run_id
                      AND state = :running
                    """
                ),
                {
                    "abandoned": ITEM_ABANDONED,
                    "failure_code": _ABANDONED_FENCE_PENDING_CLEANUP,
                    "failure_message": (
                        "attempt fenced before orphan cleanup confirmation"
                    ),
                    "completed_at": effective_time,
                    "item_id": int(item_id),
                    "run_id": run_id,
                    "running": ITEM_RUNNING,
                },
            )
            _require_rowcount(item_result, 1, "schedule item recovery fence")
            _mark_occurrence_failed_conn(
                conn,
                occurrence_id=int(item["occurrence_id"]),
                completed_at=effective_time,
            )
            result_fenced_at = effective_time
        else:
            result_fenced_at = _as_datetime(
                run.get("finished_at"),
                "run finished_at",
            )

        token = str(run.get("execution_token") or "")
        if not token:
            raise RuntimeError(
                f"scheduled run has no execution token: {run_id}"
            )
        process_id = (
            int(run["process_id"])
            if run.get("process_id") is not None
            else None
        )
        process_group_id = (
            int(run["process_group_id"])
            if run.get("process_group_id") is not None
            else None
        )
    return FencedScheduleAttempt(
        item_id=int(item_id),
        run_id=run_id,
        execution_token=token,
        process_id=process_id,
        process_group_id=process_group_id,
        fenced_at=result_fenced_at,
    )


def confirm_schedule_attempt_orphan_cleanup(
    engine: Engine,
    *,
    item_id: int,
    run_id: int,
    execution_token: str,
) -> str:
    """确认已 fence attempt 的进程组清理完成，随后才允许恢复启动。"""
    normalized_token = _require_bounded_identifier(
        execution_token,
        "execution_token",
        max_length=128,
    )
    with engine.begin() as conn:
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=int(item_id),
        )
        if int(item.get("current_run_id") or 0) != int(run_id):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != int(item_id)
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")
        if str(run.get("execution_token") or "") != normalized_token:
            raise RuntimeError(
                f"scheduled run execution token mismatch: run_id={run_id}"
            )
        if (
            item.get("state") != ITEM_ABANDONED
            or run.get("status") != "failed"
        ):
            raise RuntimeError(
                f"scheduled attempt is not fenced: run_id={run_id}"
            )
        item_failure_code = str(item.get("failure_code") or "")
        run_failure_code = str(run.get("failure_code") or "")
        if (
            item_failure_code == _ABANDONED_ORPHAN_CLEANUP
            and run_failure_code == _ABANDONED_ORPHAN_CLEANUP
        ):
            return ITEM_ABANDONED
        if (
            item_failure_code != _ABANDONED_FENCE_PENDING_CLEANUP
            or run_failure_code != _ABANDONED_FENCE_PENDING_CLEANUP
        ):
            raise RuntimeError(
                "orphan cleanup confirmation requires a pending recovery "
                f"fence: run_id={run_id}"
            )
        run_result = conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET failure_code = :confirmed_code,
                    error_message = :failure_message
                WHERE run_id = :run_id
                  AND status = 'failed'
                  AND failure_code = :pending_code
                  AND execution_token = :execution_token
                """
            ),
            {
                "confirmed_code": _ABANDONED_ORPHAN_CLEANUP,
                "failure_message": (
                    "orphan cleanup confirmed after recovery fence"
                ),
                "run_id": int(run_id),
                "pending_code": _ABANDONED_FENCE_PENDING_CLEANUP,
                "execution_token": normalized_token,
            },
        )
        _require_rowcount(run_result, 1, "schedule run cleanup confirm")
        item_result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET failure_code = :confirmed_code,
                    failure_message = :failure_message
                WHERE item_id = :item_id
                  AND current_run_id = :run_id
                  AND state = :abandoned
                  AND failure_code = :pending_code
                """
            ),
            {
                "confirmed_code": _ABANDONED_ORPHAN_CLEANUP,
                "failure_message": (
                    "orphan cleanup confirmed after recovery fence"
                ),
                "item_id": int(item_id),
                "run_id": int(run_id),
                "abandoned": ITEM_ABANDONED,
                "pending_code": _ABANDONED_FENCE_PENDING_CLEANUP,
            },
        )
        _require_rowcount(item_result, 1, "schedule item cleanup confirm")
    return ITEM_ABANDONED


def mark_schedule_attempt_retry_wait(
    engine: Engine,
    *,
    run_id: int,
    failure_code: str,
    error_message: str | None = None,
    failed_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> str:
    """结束当前失败 attempt；首败等待一次重试，第二次失败转终态。"""
    if failure_code != FAILURE_TRANSIENT_INFRA:
        raise ValueError(
            "retry-wait failure_code must be TRANSIENT_INFRA"
        )
    effective_failed_at, _trusted_now = _ledger_event_time(
        failed_at,
        field="failed_at",
        clock=_clock,
    )
    with engine.begin() as conn:
        item_id = _resolve_schedule_item_id_for_run(
            conn,
            run_id=int(run_id),
        )
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=item_id,
        )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != item_id
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")
        if int(item.get("current_run_id") or 0) != int(run_id):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        if run.get("status") != "running" or item.get("state") != ITEM_RUNNING:
            raise RuntimeError(
                f"scheduled run is not running: run_id={run_id}"
            )
        _require_run_lifecycle_time(
            effective_failed_at,
            run,
            event_field="failed_at",
        )
        next_state = (
            ITEM_RETRY_WAIT
            if int(run["attempt_no"]) < _MAX_SCHEDULE_ATTEMPTS
            else ITEM_FAILED_TERMINAL
        )
        run_result = conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET status = 'failed',
                    failure_code = :failure_code,
                    error_message = :error_message,
                    finished_at = :failed_at
                WHERE run_id = :run_id
                  AND status = 'running'
                """
            ),
            {
                "failure_code": FAILURE_TRANSIENT_INFRA,
                "error_message": error_message,
                "failed_at": effective_failed_at,
                "run_id": int(run_id),
            },
        )
        _require_rowcount(run_result, 1, "schedule run failure")
        item_result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :state,
                    failure_code = :failure_code,
                    failure_message = :failure_message,
                    completed_at = :completed_at
                WHERE item_id = :item_id
                  AND current_run_id = :run_id
                  AND state = :running
                """
            ),
            {
                "state": next_state,
                "failure_code": FAILURE_TRANSIENT_INFRA,
                "failure_message": error_message,
                "completed_at": (
                    effective_failed_at
                    if next_state == ITEM_FAILED_TERMINAL
                    else None
                ),
                "item_id": int(item["item_id"]),
                "run_id": int(run_id),
                "running": ITEM_RUNNING,
            },
        )
        _require_rowcount(item_result, 1, "schedule item failure")
        if next_state == ITEM_FAILED_TERMINAL:
            _mark_occurrence_failed_conn(
                conn,
                occurrence_id=int(item["occurrence_id"]),
                completed_at=effective_failed_at,
            )
    return next_state


def mark_schedule_attempt_terminal_failure(
    engine: Engine,
    *,
    run_id: int,
    failure_code: str,
    error_message: str | None = None,
    failed_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> str:
    """将执行终态或真实启动越过 recovery cutoff 原子收口。"""
    normalized_failure_code = _require_schedule_failure_code(failure_code)
    if normalized_failure_code not in _TERMINAL_SCHEDULE_FAILURE_CODES:
        raise ValueError(
            "terminal failure_code must be one of "
            f"{sorted(_TERMINAL_SCHEDULE_FAILURE_CODES)}"
        )
    effective_failed_at, _trusted_now = _ledger_event_time(
        failed_at,
        field="failed_at",
        clock=_clock,
    )
    with engine.begin() as conn:
        item_id = _resolve_schedule_item_id_for_run(
            conn,
            run_id=int(run_id),
        )
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=item_id,
        )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != item_id
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")
        if int(item.get("current_run_id") or 0) != int(run_id):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        if run.get("status") != "running" or item.get("state") != ITEM_RUNNING:
            raise RuntimeError(
                f"scheduled run is not running: run_id={run_id}"
            )
        _require_run_lifecycle_time(
            effective_failed_at,
            run,
            event_field="failed_at",
        )
        run_result = conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET status = 'failed',
                    failure_code = :failure_code,
                    error_message = :error_message,
                    finished_at = :failed_at
                WHERE run_id = :run_id
                  AND status = 'running'
                """
            ),
            {
                "failure_code": normalized_failure_code,
                "error_message": error_message,
                "failed_at": effective_failed_at,
                "run_id": int(run_id),
            },
        )
        _require_rowcount(run_result, 1, "terminal schedule run failure")
        terminal_item_state = (
            ITEM_EXPIRED
            if normalized_failure_code
            == FAILURE_RECOVERY_CUTOFF_EXPIRED
            else ITEM_FAILED_TERMINAL
        )
        item_result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :terminal_state,
                    failure_code = :failure_code,
                    failure_message = :failure_message,
                    completed_at = :completed_at
                WHERE item_id = :item_id
                  AND current_run_id = :run_id
                  AND state = :running
                """
            ),
            {
                "terminal_state": terminal_item_state,
                "failure_code": normalized_failure_code,
                "failure_message": error_message,
                "completed_at": effective_failed_at,
                "item_id": item_id,
                "run_id": int(run_id),
                "running": ITEM_RUNNING,
            },
        )
        _require_rowcount(item_result, 1, "terminal schedule item failure")
        _mark_occurrence_failed_conn(
            conn,
            occurrence_id=int(item["occurrence_id"]),
            completed_at=effective_failed_at,
        )
    return terminal_item_state


def fail_schedule_item_without_attempt(
    engine: Engine,
    *,
    item_id: int,
    failure_code: str,
    failure_message: str | None = None,
    _clock: _LedgerClock | None = None,
) -> str:
    """将尚未进入新 attempt 的 item 原子终止，并记录 occurrence 审计。"""
    normalized_failure_code = _require_schedule_failure_code(failure_code)
    if normalized_failure_code not in _NONEXECUTED_SCHEDULE_FAILURE_CODES:
        raise ValueError(
            "non-executed failure_code must be one of "
            f"{sorted(_NONEXECUTED_SCHEDULE_FAILURE_CODES)}"
        )
    normalized_failure_message = (
        str(failure_message).strip()
        if failure_message is not None
        else None
    )
    if failure_message is not None and not normalized_failure_message:
        raise ValueError("failure_message must be non-empty when provided")
    completed_at = _trusted_ledger_now(_clock)
    with engine.begin() as conn:
        locator = _select_mapping_one_or_none(
            conn,
            """
            SELECT item_id, occurrence_id
            FROM t_schedule_items
            WHERE item_id = :item_id
            """,
            {"item_id": int(item_id)},
            for_update=False,
        )
        if locator is None:
            raise RuntimeError(f"schedule item not found: {item_id}")
        occurrence_id = int(locator["occurrence_id"])
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=occurrence_id,
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=occurrence_id,
            for_update=True,
        )
        item = next(
            (
                row
                for row in siblings
                if int(row["item_id"]) == int(item_id)
            ),
            None,
        )
        if item is None:
            raise RuntimeError(f"schedule item not found: {item_id}")
        if str(item["state"]) == ITEM_FAILED_TERMINAL:
            if (
                str(item.get("failure_code"))
                == normalized_failure_code
                and _optional_stored_text(
                    item.get("failure_message")
                )
                == normalized_failure_message
            ):
                return ITEM_FAILED_TERMINAL
            raise RuntimeError(
                "schedule item is already terminal with different "
                f"failure audit: {item_id}"
            )
        allowed_states = {
            ITEM_PENDING,
            ITEM_RETRY_WAIT,
            ITEM_ABANDONED,
        }
        if str(item["state"]) not in allowed_states:
            raise RuntimeError(
                "schedule item cannot fail without an attempt from "
                f"state={item['state']}: {item_id}"
            )
        result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :failed_terminal,
                    failure_code = :failure_code,
                    failure_message = :failure_message,
                    completed_at = :completed_at
                WHERE item_id = :item_id
                  AND state IN (:pending, :retry_wait, :abandoned)
                """
            ),
            {
                "failed_terminal": ITEM_FAILED_TERMINAL,
                "failure_code": normalized_failure_code,
                "failure_message": normalized_failure_message,
                "completed_at": completed_at,
                "item_id": int(item_id),
                "pending": ITEM_PENDING,
                "retry_wait": ITEM_RETRY_WAIT,
                "abandoned": ITEM_ABANDONED,
            },
        )
        _require_rowcount(result, 1, "non-executed schedule item failure")
        _mark_occurrence_failed_conn(
            conn,
            occurrence_id=occurrence_id,
            completed_at=completed_at,
            failure_code=normalized_failure_code,
            failure_message=normalized_failure_message,
        )
    return ITEM_FAILED_TERMINAL


def expire_schedule_items(
    engine: Engine,
    *,
    occurrence_id: int,
    evaluated_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> int:
    """到 recovery cutoff 后，将尚未执行完成的 item 原子置为 EXPIRED。"""
    with engine.begin() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        effective_time, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                evaluated_at,
                field="evaluated_at",
                clock=_clock,
            )
        )
        if effective_time < _as_datetime(
            occurrence["recovery_cutoff_at"],
            "recovery_cutoff_at",
        ):
            return 0
        result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :expired,
                    failure_code = :failure_code,
                    failure_message = 'item did not start before recovery cutoff',
                    completed_at = :evaluated_at
                WHERE occurrence_id = :occurrence_id
                  AND state IN (:pending, :retry_wait, :abandoned)
                """
            ),
            {
                "expired": ITEM_EXPIRED,
                "failure_code": FAILURE_RECOVERY_CUTOFF_EXPIRED,
                "evaluated_at": effective_time,
                "occurrence_id": int(occurrence_id),
                "pending": ITEM_PENDING,
                "retry_wait": ITEM_RETRY_WAIT,
                "abandoned": ITEM_ABANDONED,
            },
        )
        expired_count = int(getattr(result, "rowcount", 0) or 0)
        if expired_count:
            _mark_occurrence_failed_conn(
                conn,
                occurrence_id=int(occurrence_id),
                completed_at=effective_time,
            )
        return expired_count


def evaluate_schedule_item_start_sla(
    engine: Engine,
    *,
    item_id: int,
    evaluated_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> StartGuardrailProjection:
    """求值并一次性落盘 Blackbox V2 的 07:45 启动 guardrail。"""
    with engine.begin() as conn:
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=int(item_id),
        )
        effective_time, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                evaluated_at,
                field="evaluated_at",
                clock=_clock,
            )
        )
        # 07:45 是固定业务 guardrail。DataBridge 晚封存时，动态
        # release_at 可能晚于 07:45；这恰好应记录 LATE，而不是拒绝求值。
        projection = _persist_v2_start_guardrail_conn(
            conn,
            item=item,
            started_at=(
                _as_datetime(item["started_at"], "started_at")
                if item.get("started_at") is not None
                else None
            ),
            evaluated_at=effective_time,
        )
    return projection


def complete_scheduled_attempt(
    engine: Engine,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    trusted_verifier: ScheduledCompletionVerifier,
    scheme_version: str | None = None,
    completed_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> int:
    """在完整性 evidence gate 后原子完成 daily ledger attempt。

    P0 集成边界：旧 generic ``scheduled_live`` 写入路径暂未全局关闭；
    daily production 协调器必须只调用本函数，后续由 mode fence 统一收口。
    """
    if trusted_verifier is None or not callable(
        getattr(trusted_verifier, "verify", None)
    ):
        raise TypeError(
            "trusted_verifier must implement "
            "ScheduledCompletionVerifier.verify"
        )
    expectation = _read_scheduled_completion_expectation(
        engine,
        run_id=int(run_id),
    )
    _assert_scheduled_completion_epoch(engine, expectation)
    evidence = trusted_verifier.verify(expectation)
    _assert_scheduled_completion_epoch(engine, expectation)
    if not isinstance(evidence, ScheduledCompletionEvidence):
        raise RuntimeError(
            "trusted_verifier must return ScheduledCompletionEvidence"
        )
    record_list = list(records)
    with engine.begin() as conn:
        item_id = _resolve_schedule_item_id_for_run(
            conn,
            run_id=int(run_id),
        )
        occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=item_id,
        )
        if int(item.get("current_run_id") or 0) != int(run_id):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        if not item.get("input_generation_id"):
            raise RuntimeError(
                f"schedule item has no bound input generation: {item['item_id']}"
            )
        generation = _read_input_generation_conn(
            conn,
            str(item["input_generation_id"]),
            for_update=True,
        )
        if generation is None:
            raise RuntimeError(
                "bound input generation not found: "
                f"{item['input_generation_id']}"
            )
        if generation.get("state") != GENERATION_SEALED:
            raise RuntimeError(
                "input generation is not SEALED: "
                f"{item['input_generation_id']}"
            )
        native_generation = None
        if str(generation.get("generation_type")) == "databridge_v1":
            native_generation_id = generation.get(
                "native_generation_id"
            )
            if not native_generation_id:
                raise RuntimeError(
                    "databridge generation is missing native_generation_id"
                )
            native_generation = _read_input_generation_conn(
                conn,
                str(native_generation_id),
                for_update=True,
            )
            if native_generation is None:
                raise RuntimeError(
                    "native generation relation not found: "
                    f"{native_generation_id}"
                )
            _validate_databridge_native_relation(
                databridge=generation,
                native_generation=native_generation,
            )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != item_id
        ):
            raise RuntimeError(f"scheduled run mapping changed: {run_id}")
        if (
            run.get("status") != "running"
            or item.get("state") != ITEM_RUNNING
        ):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        targets = _read_schedule_targets_conn(
            conn,
            item_id=int(item["item_id"]),
            for_update=True,
        )
        _validate_scheduled_completion_evidence(
            evidence=evidence,
            item=item,
            generation=generation,
            native_generation=native_generation,
        )
        _validate_cache_qualified_completion(
            occurrence=occurrence,
            item=item,
            generation=generation,
            records=record_list,
        )
        _validate_scheduled_records(
            run_id=int(run_id),
            run=run,
            item=item,
            occurrence=occurrence,
            targets=targets,
            records=record_list,
            scheme_version=scheme_version,
            generation_feature_date=str(generation["feature_date"]),
        )
        effective_time, _trusted_now = _ledger_event_time_after_locks(
            conn,
            completed_at,
            field="completed_at",
            clock=_clock,
        )
        _require_not_before(
            effective_time,
            run.get("started_at"),
            event_field="completed_at",
            lower_field="run started_at",
        )
        _require_not_before(
            effective_time,
            generation.get("sealed_at"),
            event_field="completed_at",
            lower_field="generation sealed_at",
        )
        exact_scheme_version = str(item["scheme_version"])
        written = _insert_run_predictions_conn(
            conn,
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
            insert_only=True,
        )
        predictions = (
            conn.execute(
                text(
                    """
                    SELECT id, run_id, scheme_id, target_tenor, horizon,
                           target_date
                    FROM t_scheme_predictions
                    WHERE run_id = :run_id
                    ORDER BY id
                    """
                ),
                {"run_id": int(run_id)},
            )
            .mappings()
            .all()
        )
        prediction_ids = {
            (
                str(row["target_tenor"]),
                int(row["horizon"]),
                str(row["target_date"]),
            ): int(row["id"])
            for row in predictions
            if (
                int(row["run_id"]) == int(run_id)
                and str(row["scheme_id"]) == str(item["base_scheme_id"])
            )
        }
        expected_keys = {
            (
                str(target["target_tenor"]),
                int(target["horizon"]),
                str(target["target_date"]),
            )
            for target in targets
        }
        if set(prediction_ids) != expected_keys or len(predictions) != len(targets):
            raise RuntimeError(
                "inserted prediction acceptance identity mismatch for "
                f"run_id={run_id}"
            )
        for target in targets:
            key = (
                str(target["target_tenor"]),
                int(target["horizon"]),
                str(target["target_date"]),
            )
            accepted = conn.execute(
                text(
                    """
                    UPDATE t_schedule_item_targets
                    SET status = :accepted,
                        accepted_run_id = :run_id,
                        accepted_prediction_id = :prediction_id,
                        accepted_at = :accepted_at
                    WHERE target_id = :target_id
                      AND item_id = :item_id
                      AND status = 'PENDING'
                      AND accepted_run_id IS NULL
                      AND accepted_prediction_id IS NULL
                    """
                ),
                {
                    "accepted": TARGET_ACCEPTED,
                    "run_id": int(run_id),
                    "prediction_id": prediction_ids[key],
                    "accepted_at": effective_time,
                    "target_id": int(target["target_id"]),
                    "item_id": int(item["item_id"]),
                },
            )
            _require_rowcount(accepted, 1, "schedule target accept")
        _finish_scheme_run_conn(
            conn,
            run_id=int(run_id),
            status="success",
            records_returned=len(record_list),
            records_written=written,
            error_message=None,
            finished_at=effective_time,
            require_exact_run=True,
        )
        item_result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET state = :success,
                    completed_at = :completed_at,
                    failure_code = NULL,
                    failure_message = NULL
                WHERE item_id = :item_id
                  AND current_run_id = :run_id
                  AND state = :running
                """
            ),
            {
                "success": ITEM_SUCCESS,
                "completed_at": effective_time,
                "item_id": int(item["item_id"]),
                "run_id": int(run_id),
                "running": ITEM_RUNNING,
            },
        )
        _require_rowcount(item_result, 1, "schedule item complete")
        accepted_count = _count_accepted_schedule_targets(
            conn,
            occurrence_id=int(item["occurrence_id"]),
            for_update=True,
        )
        current_siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(item["occurrence_id"]),
            for_update=True,
        )
        successful_items = sum(
            1
            for sibling in current_siblings
            if str(sibling["state"]) == ITEM_SUCCESS
        )
        occurrence_success = (
            accepted_count == int(occurrence["expected_target_count"])
            and successful_items == int(occurrence["expected_item_count"])
        )
        next_occurrence_state = (
            OCCURRENCE_SUCCESS
            if occurrence_success
            else (
                OCCURRENCE_FAILED
                if occurrence.get("completion_state") == OCCURRENCE_FAILED
                else OCCURRENCE_RUNNING
            )
        )
        next_completed_at = (
            effective_time
            if next_occurrence_state == OCCURRENCE_SUCCESS
            else (
                occurrence.get("completed_at")
                if next_occurrence_state == OCCURRENCE_FAILED
                else None
            )
        )
        occurrence_result = conn.execute(
            text(
                """
                UPDATE t_schedule_occurrences
                SET accepted_target_count = :accepted_target_count,
                    completion_state = :completion_state,
                    completed_at = :completed_at
                WHERE occurrence_id = :occurrence_id
                """
            ),
            {
                "accepted_target_count": accepted_count,
                "completion_state": next_occurrence_state,
                "completed_at": next_completed_at,
                "occurrence_id": int(item["occurrence_id"]),
            },
        )
        _require_rowcount(
            occurrence_result,
            1,
            "schedule occurrence aggregate",
        )
    try:
        record_schedule_attempt_visibility(
            engine,
            run_id=int(run_id),
            _clock=_clock,
        )
    except Exception:
        # 预测、run、item 与 target acceptance 已在上一事务提交。receipt
        # 失败不能把一个已提交的成功 attempt 伪装成算法失败；缺失 receipt
        # 会被 SLA/health 保守计为不可用，并由恢复循环显式补写。
        _LOGGER.exception(
            "schedule visibility receipt failed after committed success: "
            "run_id=%s",
            run_id,
        )
    return written


def record_schedule_attempt_visibility(
    engine: Engine,
    *,
    run_id: int,
    _clock: _LedgerClock | None = None,
) -> datetime:
    """在成功发布事务提交后记录 write-once 可见性 receipt。

    receipt 使用第二个事务中的数据库 UTC 时钟。该时刻是预测提交已可被
    新事务观测到的保守上界；重放只会在 receipt 缺失时使用重放当下时钟，
    永不回填为原 attempt 的 ``accepted_at``。
    """
    with engine.begin() as conn:
        item_id = _resolve_schedule_item_id_for_run(
            conn,
            run_id=int(run_id),
        )
        _occurrence, item, _siblings = _lock_schedule_item_context_conn(
            conn,
            item_id=item_id,
        )
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if (
            run is None
            or int(run.get("schedule_item_id") or 0) != item_id
            or int(item.get("current_run_id") or 0) != int(run_id)
        ):
            raise RuntimeError(
                f"stale schedule visibility receipt rejected: run_id={run_id}"
            )
        if run.get("status") != "success" or item.get("state") != ITEM_SUCCESS:
            raise RuntimeError(
                "schedule visibility receipt requires committed success: "
                f"run_id={run_id}"
            )
        targets = _read_schedule_targets_conn(
            conn,
            item_id=item_id,
            for_update=True,
        )
        if not targets:
            raise RuntimeError(
                f"schedule visibility receipt has no targets: run_id={run_id}"
            )
        relationship_targets = _read_schedule_targets_conn(
            conn,
            item_id=item_id,
            for_update=False,
        )
        if {
            int(target["target_id"]) for target in relationship_targets
        } != {int(target["target_id"]) for target in targets}:
            raise RuntimeError(
                "schedule visibility receipt target linkage invalid: "
                f"run_id={run_id}"
            )
        for target in relationship_targets:
            if (
                not _is_valid_accepted_schedule_target(target)
                or int(target.get("accepted_run_id") or 0) != int(run_id)
            ):
                raise RuntimeError(
                    "schedule visibility receipt target linkage invalid: "
                    f"run_id={run_id} target_id={target.get('target_id')}"
                )

        existing_receipts = [
            target.get("visible_at") for target in relationship_targets
        ]
        if any(value is not None for value in existing_receipts):
            if not all(value is not None for value in existing_receipts):
                raise RuntimeError(
                    "partial schedule visibility receipt rejected: "
                    f"run_id={run_id}"
                )
            normalized_receipts = [
                _as_datetime(value, "visible_at")
                for value in existing_receipts
            ]
            for target, receipt_at in zip(
                relationship_targets,
                normalized_receipts,
                strict=True,
            ):
                _require_not_before(
                    receipt_at,
                    target.get("accepted_at"),
                    event_field="visible_at",
                    lower_field="accepted_at",
                )
            return max(normalized_receipts)

        visible_at, _trusted_now = _ledger_event_time_after_locks(
            conn,
            None,
            field="visible_at",
            clock=_clock,
        )
        visible_at = _visibility_time_after_run_finish(
            visible_at,
            run.get("finished_at"),
        )
        for target in relationship_targets:
            _require_not_before(
                visible_at,
                target.get("accepted_at"),
                event_field="visible_at",
                lower_field="accepted_at",
            )
        result = conn.execute(
            text(
                """
                UPDATE t_schedule_item_targets
                SET visible_at = :visible_at
                WHERE item_id = :item_id
                  AND accepted_run_id = :run_id
                  AND status = :accepted
                  AND accepted_prediction_id IS NOT NULL
                  AND accepted_at IS NOT NULL
                  AND visible_at IS NULL
                """
            ),
            {
                "visible_at": visible_at,
                "item_id": item_id,
                "run_id": int(run_id),
                "accepted": TARGET_ACCEPTED,
            },
        )
        _require_rowcount(
            result,
            len(relationship_targets),
            "schedule visibility receipt",
        )
    return visible_at


def reconcile_schedule_occurrence_visibility_receipts(
    engine: Engine,
    *,
    occurrence_id: int,
    _clock: _LedgerClock | None = None,
) -> int:
    """补写 occurrence 内 crash-after-commit 遗留的 visibility receipts。

    每个成功 run 经独立短事务补写，便于中途崩溃后安全重放。返回本次发现
    并完成 reconcile 的 run 数；已存在的 write-once receipt 不会改写。
    """
    with engine.connect() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=False,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        candidates = list(
            conn.execute(
                text(
                    """
                    SELECT i.item_id, i.current_run_id
                    FROM t_schedule_items i
                    JOIN t_scheme_runs r
                      ON r.run_id = i.current_run_id
                    WHERE i.occurrence_id = :occurrence_id
                      AND i.state = :success
                      AND r.status = 'success'
                    ORDER BY i.current_run_id
                    """
                ),
                {
                    "occurrence_id": int(occurrence_id),
                    "success": ITEM_SUCCESS,
                },
            ).mappings()
        )
        run_ids = []
        for candidate in candidates:
            targets = _read_schedule_targets_conn(
                conn,
                item_id=int(candidate["item_id"]),
                for_update=False,
            )
            if (
                targets
                and all(
                    _is_valid_accepted_schedule_target(target)
                    for target in targets
                )
                and any(
                    target.get("visible_at") is None
                    for target in targets
                )
            ):
                run_ids.append(int(candidate["current_run_id"]))
    for missing_run_id in run_ids:
        record_schedule_attempt_visibility(
            engine,
            run_id=missing_run_id,
            _clock=_clock,
        )
    return len(run_ids)


def evaluate_schedule_occurrence_target_sla(
    engine: Engine,
    *,
    occurrence_id: int,
    evaluated_at: datetime | None = None,
    _clock: _LedgerClock | None = None,
) -> TargetSlaProjection:
    """求值并一次性落盘 08:00 target availability SLA。"""
    with engine.begin() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        if occurrence is None:
            raise RuntimeError(
                f"schedule occurrence not found: {occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        items = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=int(occurrence_id),
            for_update=True,
        )
        if not items:
            raise RuntimeError(
                f"schedule occurrence has no items: {occurrence_id}"
            )
        effective_time, _trusted_now = (
            _ledger_event_time_after_locks(
                conn,
                evaluated_at,
                field="evaluated_at",
                clock=_clock,
            )
        )
        earliest_release_at = min(
            _as_datetime(item["release_at"], "release_at")
            for item in items
        )
        _require_not_before(
            effective_time,
            earliest_release_at,
            event_field="evaluated_at",
            lower_field="release_at",
        )
        accepted_count, visible_count, accepted_by_deadline_count = (
            _count_schedule_target_availability(
                conn,
                occurrence_id=int(occurrence_id),
                deadline_at=_as_datetime(
                    occurrence["sla_deadline_at"],
                    "sla_deadline_at",
                ),
                for_update=True,
            )
        )
        deadline = _as_datetime(
            occurrence["sla_deadline_at"],
            "sla_deadline_at",
        )
        current = None
        if occurrence.get("sla_outcome") in {SLA_MET, SLA_BREACHED}:
            if occurrence.get("sla_accepted_target_count") is None:
                raise RuntimeError(
                    "terminal schedule occurrence is missing "
                    "sla_accepted_target_count"
                )
            current = TargetSlaProjection(
                status=str(occurrence["sla_outcome"]),
                evaluated_at=_as_datetime(
                    occurrence["sla_evaluated_at"],
                    "sla_evaluated_at",
                ),
                deadline_at=deadline,
                accepted_target_count=int(
                    occurrence["accepted_target_count"]
                ),
                accepted_by_deadline_count=int(
                    occurrence["sla_accepted_target_count"]
                ),
                expected_target_count=int(
                    occurrence["expected_target_count"]
                ),
                reason=(
                    str(occurrence["sla_reason"])
                    if occurrence.get("sla_reason") is not None
                    else None
                ),
            )
        projection = project_target_availability_sla(
            current=current,
            visible_target_count=visible_count,
            accepted_by_deadline_count=accepted_by_deadline_count,
            expected_target_count=int(occurrence["expected_target_count"]),
            evaluated_at=effective_time,
            deadline_at=deadline,
        )
        if current is None and projection.status in {SLA_MET, SLA_BREACHED}:
            result = conn.execute(
                text(
                    """
                    UPDATE t_schedule_occurrences
                    SET accepted_target_count = :accepted_target_count,
                        sla_accepted_target_count = :sla_accepted_target_count,
                        sla_outcome = :sla_outcome,
                        sla_evaluated_at = :sla_evaluated_at,
                        sla_reason = :sla_reason,
                        failure_code = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN COALESCE(failure_code, :failure_result)
                          ELSE failure_code
                        END,
                        failure_message = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN COALESCE(
                            failure_message,
                            :relationship_failure_message
                          )
                          ELSE failure_message
                        END,
                        completion_state = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN :occurrence_failed
                          ELSE completion_state
                        END
                    WHERE occurrence_id = :occurrence_id
                      AND sla_outcome = :pending
                    """
                ),
                {
                    "accepted_target_count": accepted_count,
                    "sla_accepted_target_count": (
                        accepted_by_deadline_count
                    ),
                    "sla_outcome": projection.status,
                    "sla_evaluated_at": projection.evaluated_at,
                    "sla_reason": projection.reason,
                    "occurrence_success": OCCURRENCE_SUCCESS,
                    "occurrence_failed": OCCURRENCE_FAILED,
                    "failure_result": FAILURE_RESULT,
                    "relationship_failure_message": (
                        "accepted target relationship evidence mismatch"
                    ),
                    "occurrence_id": int(occurrence_id),
                    "pending": SLA_PENDING,
                },
            )
            _require_rowcount(result, 1, "schedule occurrence SLA")
            projection = replace(
                projection,
                newly_persisted=True,
            )
        else:
            result = conn.execute(
                text(
                    """
                    UPDATE t_schedule_occurrences
                    SET accepted_target_count = :accepted_target_count,
                        failure_code = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN COALESCE(failure_code, :failure_result)
                          ELSE failure_code
                        END,
                        failure_message = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN COALESCE(
                            failure_message,
                            :relationship_failure_message
                          )
                          ELSE failure_message
                        END,
                        completion_state = CASE
                          WHEN completion_state = :occurrence_success
                           AND :accepted_target_count
                               <> expected_target_count
                          THEN :occurrence_failed
                          ELSE completion_state
                        END
                    WHERE occurrence_id = :occurrence_id
                    """
                ),
                {
                    "accepted_target_count": accepted_count,
                    "occurrence_success": OCCURRENCE_SUCCESS,
                    "occurrence_failed": OCCURRENCE_FAILED,
                    "failure_result": FAILURE_RESULT,
                    "relationship_failure_message": (
                        "accepted target relationship evidence mismatch"
                    ),
                    "occurrence_id": int(occurrence_id),
                },
            )
            _require_rowcount(
                result,
                1,
                "schedule occurrence accepted target refresh",
            )
    return projection


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


def _require_schedule_trigger_origin(value: str) -> str:
    normalized = _require_bounded_identifier(
        value,
        "trigger_origin",
        max_length=32,
    )
    if normalized not in _SCHEDULE_TRIGGER_ORIGINS:
        raise ValueError(
            "trigger_origin must be one of "
            f"{sorted(_SCHEDULE_TRIGGER_ORIGINS)}"
        )
    return normalized


def _require_schedule_failure_code(value: str) -> str:
    """只接受日批账本的稳定词表；旧别名必须由显式迁移处理。"""
    normalized = _require_bounded_identifier(
        value,
        "failure_code",
        max_length=64,
    )
    if normalized not in SCHEDULE_FAILURE_CODES:
        raise ValueError(
            "schedule failure_code must be one of "
            f"{sorted(SCHEDULE_FAILURE_CODES)}; "
            f"legacy/unknown value rejected: {normalized}"
        )
    return normalized


def _utc_datetime6(value: datetime | None) -> datetime:
    effective = value or datetime.now(timezone.utc)
    if effective.tzinfo is not None:
        effective = effective.astimezone(timezone.utc).replace(tzinfo=None)
    return effective


def _trusted_ledger_now(clock: _LedgerClock | None) -> datetime:
    if clock is None:
        return _utc_datetime6(datetime.now(timezone.utc))
    observed = clock.now_utc()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("internal _clock seam must return timezone-aware UTC")
    return _utc_datetime6(observed)


def _ledger_event_time(
    value: datetime | None,
    *,
    field: str,
    clock: _LedgerClock | None,
) -> tuple[datetime, datetime]:
    trusted_now = _trusted_ledger_now(clock)
    if value is None:
        return trusted_now, trusted_now
    if clock is None:
        raise ValueError(
            f"{field} override requires explicit internal _clock seam"
        )
    event_time = _utc_datetime6(value)
    if event_time > trusted_now:
        raise RuntimeError(
            f"{field} cannot be in the future relative to trusted clock"
        )
    return event_time, trusted_now


def _ledger_event_time_after_locks(
    conn: Connection,
    value: datetime | None,
    *,
    field: str,
    clock: _LedgerClock | None,
) -> tuple[datetime, datetime]:
    """锁定/验证后取完成时刻；生产 MySQL 使用数据库 UTC 时钟。"""
    if clock is not None:
        trusted_now = _trusted_ledger_now(clock)
    elif _dialect_name(conn) == "mysql":
        observed = conn.execute(
            text("SELECT UTC_TIMESTAMP(6)")
        ).scalar_one()
        trusted_now = _utc_datetime6(
            observed
            if isinstance(observed, datetime)
            else datetime.fromisoformat(str(observed))
        )
    else:
        trusted_now = _trusted_ledger_now(None)
    if value is None:
        return trusted_now, trusted_now
    if clock is None:
        raise ValueError(
            f"{field} override requires explicit internal _clock seam"
        )
    event_time = _utc_datetime6(value)
    if event_time > trusted_now:
        raise RuntimeError(
            f"{field} cannot be in the future relative to trusted clock"
        )
    return event_time, trusted_now


def _require_not_before(
    event_time: datetime,
    lower_bound: object,
    *,
    event_field: str,
    lower_field: str,
) -> None:
    if lower_bound is None:
        raise RuntimeError(f"{lower_field} is required")
    normalized_lower_bound = _as_datetime(lower_bound, lower_field)
    if event_time < normalized_lower_bound:
        raise RuntimeError(
            f"{event_field} cannot be before {lower_field}"
        )


def _visibility_time_after_run_finish(
    visible_at: datetime,
    finished_at: object,
) -> datetime:
    """桥接旧 ``DATETIME(0)`` 对完成时刻最多半秒的向上舍入。"""
    if finished_at is None:
        raise RuntimeError("run finished_at is required")
    normalized_finished_at = _as_datetime(
        finished_at,
        "run finished_at",
    )
    if visible_at >= normalized_finished_at:
        return visible_at
    if (
        normalized_finished_at.microsecond == 0
        and normalized_finished_at - visible_at
        <= timedelta(microseconds=500_000)
    ):
        return normalized_finished_at
    raise RuntimeError(
        "visible_at cannot be before run finished_at"
    )


def _require_run_lifecycle_time(
    event_time: datetime,
    run: Mapping[str, object],
    *,
    event_field: str,
) -> None:
    """进程未 Popen 时，失败/恢复事件以 claim 的 queued_at 为下界。"""
    started_at = run.get("started_at")
    _require_not_before(
        event_time,
        started_at if started_at is not None else run.get("queued_at"),
        event_field=event_field,
        lower_field=(
            "run started_at"
            if started_at is not None
            else "run queued_at"
        ),
    )


def _business_time_utc(business_date: str, local_time: time) -> datetime:
    local = datetime.combine(
        date.fromisoformat(business_date),
        local_time,
        tzinfo=_ASIA_SHANGHAI,
    )
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _dialect_name(conn: Connection) -> str:
    return str(getattr(getattr(conn, "dialect", None), "name", "mysql"))


def _select_mapping_one_or_none(
    conn: Connection,
    sql: str,
    params: Mapping[str, object],
    *,
    for_update: bool,
) -> Mapping[str, object] | None:
    lock = (
        ""
        if not for_update or _dialect_name(conn) == "sqlite"
        else " FOR UPDATE"
    )
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


def _read_schedule_occurrence_conn(
    conn: Connection,
    *,
    schedule_key: str,
    predict_date: str,
    for_update: bool,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT occurrence_id, schedule_key, predict_date, feature_date,
               policy_version, policy_sha256, policy_json, registry_digest,
               completion_state,
               expected_item_count, expected_target_count,
               accepted_target_count, sla_accepted_target_count,
               sla_deadline_at, recovery_cutoff_at,
               sla_outcome, sla_evaluated_at, sla_reason, failure_code,
               failure_message, started_at, completed_at
        FROM t_schedule_occurrences
        WHERE schedule_key = :schedule_key
          AND predict_date = :predict_date
        """,
        {"schedule_key": schedule_key, "predict_date": predict_date},
        for_update=for_update,
    )


def _read_schedule_occurrence_by_id_conn(
    conn: Connection,
    *,
    occurrence_id: int,
    for_update: bool,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT occurrence_id, schedule_key, predict_date, feature_date,
               policy_version, policy_sha256, policy_json, registry_digest,
               completion_state,
               expected_item_count, expected_target_count,
               accepted_target_count, sla_accepted_target_count,
               sla_deadline_at, recovery_cutoff_at,
               sla_outcome, sla_evaluated_at, sla_reason, failure_code,
               failure_message, started_at, completed_at
        FROM t_schedule_occurrences
        WHERE occurrence_id = :occurrence_id
        """,
        {"occurrence_id": int(occurrence_id)},
        for_update=for_update,
    )


def _read_schedule_item_locator_conn(
    conn: Connection,
    *,
    item_id: int,
) -> Mapping[str, object] | None:
    """无锁读取 immutable item→occurrence 映射，供统一锁序定位。"""
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT item_id, occurrence_id
        FROM t_schedule_items
        WHERE item_id = :item_id
        """,
        {"item_id": int(item_id)},
        for_update=False,
    )


def _lock_schedule_item_context_conn(
    conn: Connection,
    *,
    item_id: int,
) -> tuple[
    Mapping[str, object],
    Mapping[str, object],
    list[Mapping[str, object]],
]:
    """按 occurrence→ordered siblings 锁定 item 的统一事务上下文。"""
    locator = _read_schedule_item_locator_conn(
        conn,
        item_id=int(item_id),
    )
    if locator is None:
        raise RuntimeError(f"schedule item not found: {item_id}")
    occurrence = _read_schedule_occurrence_by_id_conn(
        conn,
        occurrence_id=int(locator["occurrence_id"]),
        for_update=True,
    )
    if occurrence is None:
        raise RuntimeError(
            f"schedule occurrence not found: {locator['occurrence_id']}"
        )
    _assert_occurrence_epoch_conn(occurrence, engine=conn)
    siblings = _read_schedule_items_for_occurrence_conn(
        conn,
        occurrence_id=int(locator["occurrence_id"]),
        for_update=True,
    )
    stored_item = next(
        (
            row
            for row in siblings
            if int(row["item_id"]) == int(item_id)
        ),
        None,
    )
    if stored_item is None:
        raise RuntimeError(f"schedule item not found: {item_id}")
    item = {
        **dict(stored_item),
        "predict_date": occurrence["predict_date"],
        "recovery_cutoff_at": occurrence["recovery_cutoff_at"],
        "sla_deadline_at": occurrence["sla_deadline_at"],
    }
    return occurrence, item, siblings


def _resolve_schedule_item_id_for_run(
    conn: Connection,
    *,
    run_id: int,
) -> int:
    """无锁读取 immutable run→item 映射，随后调用方必须按 item→run 加锁。"""
    row = _select_mapping_one_or_none(
        conn,
        """
        SELECT run_id, schedule_item_id
        FROM t_scheme_runs
        WHERE run_id = :run_id
        """,
        {"run_id": int(run_id)},
        for_update=False,
    )
    if row is None or row.get("schedule_item_id") is None:
        raise RuntimeError(f"scheduled run not found: {run_id}")
    return int(row["schedule_item_id"])


def _assert_run_not_ledger_bound_conn(
    conn: Connection,
    *,
    run_id: int,
    operation: str,
) -> None:
    """阻止 daily ledger run 绕过 ledger 专属完成/失败事务。"""
    row = _select_mapping_one_or_none(
        conn,
        """
        SELECT run_id, schedule_item_id
        FROM t_scheme_runs
        WHERE run_id = :run_id
        """,
        {"run_id": int(run_id)},
        for_update=False,
    )
    if row is not None and row.get("schedule_item_id") is not None:
        raise RuntimeError(
            f"{operation} rejected ledger-bound run_id={run_id}; "
            "use the daily ledger API"
        )


def _mark_occurrence_failed_conn(
    conn: Connection,
    *,
    occurrence_id: int,
    completed_at: datetime,
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> None:
    result = conn.execute(
        text(
            """
            UPDATE t_schedule_occurrences
            SET completion_state = :failed,
                completed_at = COALESCE(completed_at, :completed_at),
                failure_code = COALESCE(failure_code, :failure_code),
                failure_message = COALESCE(
                    failure_message,
                    :failure_message
                )
            WHERE occurrence_id = :occurrence_id
            """
        ),
        {
            "failed": OCCURRENCE_FAILED,
            "completed_at": completed_at,
            "failure_code": failure_code,
            "failure_message": failure_message,
            "occurrence_id": int(occurrence_id),
        },
    )
    _require_rowcount(result, 1, "schedule occurrence failure")


def _read_schedule_item_conn(
    conn: Connection,
    *,
    item_id: int,
    for_update: bool,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT i.item_id, i.occurrence_id, i.base_scheme_id, i.runtime_type,
               i.scheme_version, i.code_sha256, i.config_sha256,
               i.cache_group, i.input_generation_id, i.resource_class,
               i.internal_workers, i.release_offset_minutes, i.release_at,
               i.deadline_at, i.state,
               i.sla_status, i.late_reason, i.sla_evaluated_at,
               i.attempt_no, i.current_run_id, i.started_at, i.completed_at,
               i.failure_code, i.failure_message, o.predict_date,
               o.recovery_cutoff_at, o.sla_deadline_at
        FROM t_schedule_items i
        JOIN t_schedule_occurrences o
          ON o.occurrence_id = i.occurrence_id
        WHERE i.item_id = :item_id
        """,
        {"item_id": int(item_id)},
        for_update=for_update,
    )


def _read_schedule_items_for_occurrence_conn(
    conn: Connection,
    *,
    occurrence_id: int,
    for_update: bool,
) -> list[Mapping[str, object]]:
    lock = (
        ""
        if not for_update or _dialect_name(conn) == "sqlite"
        else " FOR UPDATE"
    )
    return list(
        (
            conn.execute(
                text(
                    """
                    SELECT item_id, occurrence_id, base_scheme_id,
                           runtime_type, scheme_version, code_sha256,
                           config_sha256, cache_group, input_generation_id,
                           resource_class, internal_workers,
                           release_offset_minutes, release_at, deadline_at,
                           state, sla_status, late_reason,
                           sla_evaluated_at, attempt_no, current_run_id,
                           started_at, completed_at, failure_code,
                           failure_message
                    FROM t_schedule_items
                    WHERE occurrence_id = :occurrence_id
                    ORDER BY item_id
                    """
                    + lock
                ),
                {"occurrence_id": int(occurrence_id)},
            )
            .mappings()
            .all()
        )
    )


def _read_schedule_run_conn(
    conn: Connection,
    *,
    run_id: int,
    for_update: bool,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT run_id, scheme_id, scheme_version, runtime_type, run_type,
               prediction_phase, predict_date, status, records_expected,
               records_returned, records_written, schedule_item_id,
               attempt_no, trigger_origin, queued_at, failure_code,
               execution_token, process_id, process_group_id, data_snapshot_id,
               started_at, finished_at
        FROM t_scheme_runs
        WHERE run_id = :run_id
        """,
        {"run_id": int(run_id)},
        for_update=for_update,
    )


_SCHEDULE_TARGET_RELATIONSHIP_CASE_SQL = """
CASE
  WHEN i.item_id IS NOT NULL
   AND o.occurrence_id IS NOT NULL
   AND i.occurrence_id = t.occurrence_id
   AND i.base_scheme_id = t.base_scheme_id
   AND i.runtime_type = t.runtime_type
   AND (
     (
       t.status = 'ACCEPTED'
       AND t.accepted_at IS NOT NULL
       AND (
         t.visible_at IS NULL
         OR t.visible_at >= t.accepted_at
       )
       AND i.current_run_id = t.accepted_run_id
       AND r.run_id IS NOT NULL
       AND r.schedule_item_id = t.item_id
       AND r.scheme_id = i.base_scheme_id
       AND r.scheme_version = i.scheme_version
       AND r.runtime_type = i.runtime_type
       AND r.run_type = 'active'
       AND r.prediction_phase = 'scheduled_live'
       AND r.predict_date = o.predict_date
       AND r.status = 'success'
       AND r.attempt_no = i.attempt_no
       AND p.id IS NOT NULL
       AND p.run_id = t.accepted_run_id
       AND p.scheme_id = t.base_scheme_id
       AND p.scheme_version = i.scheme_version
       AND p.target_tenor = t.target_tenor
       AND p.horizon = t.horizon
       AND p.predict_date = o.predict_date
       AND p.feature_date = o.feature_date
       AND p.target_date = t.target_date
       AND p.prediction_phase = 'scheduled_live'
     )
     OR (
       t.status = 'PENDING'
       AND t.accepted_run_id IS NULL
       AND t.accepted_prediction_id IS NULL
       AND t.accepted_at IS NULL
       AND t.visible_at IS NULL
     )
   )
  THEN 1
  ELSE 0
END
"""


def _read_schedule_target_relationships_conn(
    conn: Connection,
    *,
    item_id: int | None = None,
    occurrence_id: int | None = None,
    for_update: bool = False,
) -> list[Mapping[str, object]]:
    """读取 target 及其完整 item/run/prediction 关系判定。"""
    if (item_id is None) == (occurrence_id is None):
        raise ValueError(
            "exactly one of item_id or occurrence_id is required"
        )
    if item_id is not None:
        where_clause = "t.item_id = :identity"
        order_clause = "t.target_id"
        identity = int(item_id)
    else:
        where_clause = (
            "(t.occurrence_id = :identity "
            "OR i.occurrence_id = :identity)"
        )
        order_clause = "t.item_id, t.target_id"
        identity = int(occurrence_id)
    lock = (
        ""
        if not for_update or _dialect_name(conn) == "sqlite"
        else " FOR UPDATE"
    )
    return list(
        (
            conn.execute(
                text(
                    f"""
                    SELECT t.target_id, t.occurrence_id, t.item_id,
                           t.registry_scheme_id, t.base_scheme_id,
                           t.runtime_type, t.task_type, t.target_tenor,
                           t.horizon, t.target_date, t.status,
                           t.accepted_run_id, t.accepted_prediction_id,
                           t.accepted_at, t.visible_at,
                           {_SCHEDULE_TARGET_RELATIONSHIP_CASE_SQL}
                           AS accepted_linkage_valid
                    FROM t_schedule_item_targets t
                    LEFT JOIN t_schedule_items i
                      ON i.item_id = t.item_id
                    LEFT JOIN t_schedule_occurrences o
                      ON o.occurrence_id = t.occurrence_id
                    LEFT JOIN t_scheme_runs r
                      ON r.run_id = t.accepted_run_id
                    LEFT JOIN t_scheme_predictions p
                      ON p.id = t.accepted_prediction_id
                    WHERE {where_clause}
                    ORDER BY {order_clause}
                    {lock}
                    """
                ),
                {"identity": identity},
            )
            .mappings()
            .all()
        )
    )


def _read_schedule_targets_conn(
    conn: Connection,
    *,
    item_id: int,
    for_update: bool,
) -> list[Mapping[str, object]]:
    if not for_update:
        return _read_schedule_target_relationships_conn(
            conn,
            item_id=int(item_id),
        )
    lock = (
        ""
        if _dialect_name(conn) == "sqlite"
        else " FOR UPDATE"
    )
    return list(
        (
            conn.execute(
                text(
                    """
                    SELECT target_id, occurrence_id, item_id,
                           registry_scheme_id, base_scheme_id, runtime_type,
                           task_type, target_tenor, horizon, target_date,
                           status, accepted_run_id, accepted_prediction_id,
                           accepted_at, visible_at
                    FROM t_schedule_item_targets
                    WHERE item_id = :item_id
                    ORDER BY target_id
                    """
                    + lock
                ),
                {"item_id": int(item_id)},
            )
            .mappings()
            .all()
        )
    )


def _read_schedule_targets_for_occurrence_conn(
    conn: Connection,
    *,
    occurrence_id: int,
    for_update: bool = False,
) -> list[Mapping[str, object]]:
    return _read_schedule_target_relationships_conn(
        conn,
        occurrence_id=int(occurrence_id),
        for_update=for_update,
    )


def _read_scheduler_heartbeat_conn(
    conn: Connection,
    *,
    service_name: str,
) -> Mapping[str, object] | None:
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT service_name, process_id, host_name, state, occurrence_id,
               heartbeat_at, details_json
        FROM t_scheduler_heartbeat
        WHERE service_name = :service_name
        """,
        {"service_name": service_name},
        for_update=False,
    )


def _schedule_occurrence_envelope(
    row: Mapping[str, object],
) -> ScheduleOccurrenceEnvelope:
    return ScheduleOccurrenceEnvelope(
        occurrence_id=int(row["occurrence_id"]),
        schedule_key=_stored_text(row, "schedule_key"),
        predict_date=_stored_iso_date(row, "predict_date"),
        feature_date=_stored_iso_date(row, "feature_date"),
        policy_version=_stored_text(row, "policy_version"),
        policy_sha256=_stored_text(row, "policy_sha256"),
        policy_json=_stored_json_mapping(row, "policy_json"),
        registry_digest=_stored_text(row, "registry_digest"),
        completion_state=_stored_text(row, "completion_state"),
        expected_item_count=int(row["expected_item_count"]),
        expected_target_count=int(row["expected_target_count"]),
        accepted_target_count=int(row["accepted_target_count"]),
        sla_accepted_target_count=_optional_int(
            row.get("sla_accepted_target_count")
        ),
        sla_deadline_at=_as_datetime(
            row["sla_deadline_at"],
            "sla_deadline_at",
        ),
        recovery_cutoff_at=_as_datetime(
            row["recovery_cutoff_at"],
            "recovery_cutoff_at",
        ),
        sla_outcome=_stored_text(row, "sla_outcome"),
        sla_evaluated_at=_optional_stored_datetime(
            row.get("sla_evaluated_at"),
            "sla_evaluated_at",
        ),
        sla_reason=_optional_stored_text(row.get("sla_reason")),
        failure_code=_optional_stored_schedule_failure_code(
            row.get("failure_code")
        ),
        failure_message=_optional_stored_text(row.get("failure_message")),
        started_at=_optional_stored_datetime(
            row.get("started_at"),
            "started_at",
        ),
        completed_at=_optional_stored_datetime(
            row.get("completed_at"),
            "completed_at",
        ),
    )


def _schedule_item_envelope(
    row: Mapping[str, object],
    occurrence: Mapping[str, object],
) -> ScheduleItemEnvelope:
    return ScheduleItemEnvelope(
        item_id=int(row["item_id"]),
        occurrence_id=int(row["occurrence_id"]),
        base_scheme_id=_stored_text(row, "base_scheme_id"),
        runtime_type=_stored_text(row, "runtime_type"),
        scheme_version=_stored_text(row, "scheme_version"),
        code_sha256=_stored_text(row, "code_sha256"),
        config_sha256=_stored_text(row, "config_sha256"),
        cache_group=_stored_text(row, "cache_group"),
        input_generation_id=_optional_stored_text(
            row.get("input_generation_id")
        ),
        resource_class=_stored_text(row, "resource_class"),
        internal_workers=int(row["internal_workers"]),
        release_offset_minutes=int(row["release_offset_minutes"]),
        release_at=_as_datetime(row["release_at"], "release_at"),
        deadline_at=_as_datetime(row["deadline_at"], "deadline_at"),
        recovery_cutoff_at=_as_datetime(
            occurrence["recovery_cutoff_at"],
            "recovery_cutoff_at",
        ),
        occurrence_sla_deadline_at=_as_datetime(
            occurrence["sla_deadline_at"],
            "sla_deadline_at",
        ),
        state=_stored_text(row, "state"),
        sla_status=_stored_text(row, "sla_status"),
        late_reason=_optional_stored_text(row.get("late_reason")),
        sla_evaluated_at=_optional_stored_datetime(
            row.get("sla_evaluated_at"),
            "sla_evaluated_at",
        ),
        attempt_no=int(row["attempt_no"]),
        current_run_id=_optional_int(row.get("current_run_id")),
        started_at=_optional_stored_datetime(
            row.get("started_at"),
            "started_at",
        ),
        completed_at=_optional_stored_datetime(
            row.get("completed_at"),
            "completed_at",
        ),
        failure_code=_optional_stored_schedule_failure_code(
            row.get("failure_code")
        ),
        failure_message=_optional_stored_text(row.get("failure_message")),
    )


def _schedule_target_envelope(
    row: Mapping[str, object],
) -> ScheduleTargetEnvelope:
    return ScheduleTargetEnvelope(
        target_id=int(row["target_id"]),
        occurrence_id=int(row["occurrence_id"]),
        item_id=int(row["item_id"]),
        registry_scheme_id=_stored_text(row, "registry_scheme_id"),
        base_scheme_id=_stored_text(row, "base_scheme_id"),
        runtime_type=_stored_text(row, "runtime_type"),
        task_type=_stored_text(row, "task_type"),
        target_tenor=_stored_text(row, "target_tenor"),
        horizon=int(row["horizon"]),
        target_date=_stored_iso_date(row, "target_date"),
        status=_stored_text(row, "status"),
        accepted_run_id=_optional_int(row.get("accepted_run_id")),
        accepted_prediction_id=_optional_int(
            row.get("accepted_prediction_id")
        ),
        accepted_at=_optional_stored_datetime(
            row.get("accepted_at"),
            "accepted_at",
        ),
        visible_at=_optional_stored_datetime(
            row.get("visible_at"),
            "visible_at",
        ),
        accepted_linkage_valid=bool(
            int(row.get("accepted_linkage_valid", 0))
        ),
    )


def _schedule_generation_envelope(
    row: Mapping[str, object],
) -> ScheduleInputGenerationEnvelope:
    return ScheduleInputGenerationEnvelope(
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
        sealed_at=_optional_stored_datetime(
            row.get("sealed_at"),
            "sealed_at",
        ),
        invalidated_at=_optional_stored_datetime(
            row.get("invalidated_at"),
            "invalidated_at",
        ),
        invalid_reason=_optional_stored_text(row.get("invalid_reason")),
    )


def _scheduler_heartbeat(
    row: Mapping[str, object],
) -> SchedulerHeartbeat:
    return SchedulerHeartbeat(
        service_name=_stored_text(row, "service_name"),
        process_id=int(row["process_id"]),
        host_name=_stored_text(row, "host_name"),
        state=_stored_text(row, "state"),
        occurrence_id=_optional_int(row.get("occurrence_id")),
        heartbeat_at=_as_datetime(row["heartbeat_at"], "heartbeat_at"),
        details=_stored_json_mapping(row, "details_json"),
    )


def _count_accepted_schedule_targets(
    conn: Connection,
    *,
    occurrence_id: int,
    for_update: bool = False,
) -> int:
    if for_update:
        lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
        conn.execute(
            text(
                """
                SELECT target_id
                FROM t_schedule_item_targets
                WHERE occurrence_id = :occurrence_id
                ORDER BY target_id
                """
                + lock
            ),
            {"occurrence_id": int(occurrence_id)},
        ).all()
    rows = _read_schedule_targets_for_occurrence_conn(
        conn,
        occurrence_id=int(occurrence_id),
        for_update=for_update,
    )
    return sum(
        1 for row in rows if _is_valid_accepted_schedule_target(row)
    )


def _count_schedule_target_availability(
    conn: Connection,
    *,
    occurrence_id: int,
    deadline_at: datetime,
    for_update: bool,
) -> tuple[int, int, int]:
    """返回 ACCEPTED、当前可见及 deadline 前可见的 target 数。"""
    if for_update:
        lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
        conn.execute(
            text(
                """
                SELECT target_id
                FROM t_schedule_item_targets
                WHERE occurrence_id = :occurrence_id
                ORDER BY target_id
                """
                + lock
            ),
            {"occurrence_id": int(occurrence_id)},
        ).all()
    rows = _read_schedule_targets_for_occurrence_conn(
        conn,
        occurrence_id=int(occurrence_id),
        for_update=for_update,
    )
    accepted_count = 0
    visible_count = 0
    accepted_by_deadline_count = 0
    for row in rows:
        if not _is_valid_accepted_schedule_target(row):
            continue
        accepted_count += 1
        if row.get("visible_at") is None:
            continue
        visible_count += 1
        if _as_datetime(row["visible_at"], "visible_at") <= deadline_at:
            accepted_by_deadline_count += 1
    return accepted_count, visible_count, accepted_by_deadline_count


def _is_valid_accepted_schedule_target(
    row: Mapping[str, object],
) -> bool:
    """只有完整关系证据成立的 ACCEPTED target 才参与任何聚合。"""
    return (
        str(row.get("status")) == TARGET_ACCEPTED
        and bool(int(row.get("accepted_linkage_valid", 0)))
        and row.get("accepted_run_id") is not None
        and row.get("accepted_prediction_id") is not None
        and row.get("accepted_at") is not None
    )


def _persist_v2_start_guardrail_conn(
    conn: Connection,
    *,
    item: Mapping[str, object],
    started_at: datetime | None,
    evaluated_at: datetime,
) -> StartGuardrailProjection:
    deadline = _business_time_utc(
        str(item["predict_date"]),
        time(7, 45),
    )
    current = None
    if item.get("sla_status") in {ITEM_SLA_ON_TIME, ITEM_SLA_LATE}:
        current = StartGuardrailProjection(
            status=str(item["sla_status"]),
            evaluated_at=_as_datetime(
                item["sla_evaluated_at"],
                "sla_evaluated_at",
            ),
            deadline_at=deadline,
            reason=(
                str(item["late_reason"])
                if item.get("late_reason") is not None
                else None
            ),
        )
    projection = project_v2_start_guardrail(
        current=current,
        runtime_type=str(item["runtime_type"]),
        started_at=started_at,
        evaluated_at=evaluated_at,
        deadline_at=deadline,
    )
    if (
        current is None
        and str(item["runtime_type"]) == "blackbox_v2"
        and projection.status in {ITEM_SLA_ON_TIME, ITEM_SLA_LATE}
    ):
        result = conn.execute(
            text(
                """
                UPDATE t_schedule_items
                SET sla_status = :sla_status,
                    late_reason = :late_reason,
                    sla_evaluated_at = :sla_evaluated_at
                WHERE item_id = :item_id
                  AND sla_status = :pending
                """
            ),
            {
                "sla_status": projection.status,
                "late_reason": projection.reason,
                "sla_evaluated_at": projection.evaluated_at,
                "item_id": int(item["item_id"]),
                "pending": SLA_PENDING,
            },
        )
        _require_rowcount(result, 1, "schedule item start SLA")
        projection = replace(
            projection,
            newly_persisted=True,
        )
    return projection


def _validate_scheduled_records(
    *,
    run_id: int,
    run: Mapping[str, object],
    item: Mapping[str, object],
    occurrence: Mapping[str, object],
    targets: list[Mapping[str, object]],
    records: list[PredictionRecord],
    scheme_version: str | None,
    generation_feature_date: str,
) -> None:
    if not targets:
        raise RuntimeError(
            f"schedule item has no frozen targets: {item['item_id']}"
        )
    if int(run.get("attempt_no") or 0) != int(item["attempt_no"]):
        raise RuntimeError(
            f"stale scheduled run attempt mismatch: run_id={run_id}"
        )
    frozen_version = str(item["scheme_version"])
    if scheme_version is not None and scheme_version != frozen_version:
        raise RuntimeError(
            "scheduled scheme_version mismatch: "
            f"expected={frozen_version} actual={scheme_version}"
        )
    expected_run_fields = {
        "scheme_id": str(item["base_scheme_id"]),
        "scheme_version": frozen_version,
        "runtime_type": str(item["runtime_type"]),
        "predict_date": str(occurrence["predict_date"]),
        "prediction_phase": "scheduled_live",
    }
    for field, expected_value in expected_run_fields.items():
        if str(run.get(field)) != expected_value:
            raise RuntimeError(
                "scheduled run identity mismatch: "
                f"{field} expected={expected_value} actual={run.get(field)}"
            )
    if int(run.get("records_expected") or 0) != len(targets):
        raise RuntimeError(
            "scheduled run records_expected mismatch: "
            f"expected={len(targets)} actual={run.get('records_expected')}"
        )
    expected = Counter(
        (
            str(target["target_tenor"]),
            int(target["horizon"]),
            str(target["target_date"]),
        )
        for target in targets
    )
    actual = Counter(
        (
            str(record.target_tenor),
            int(record.horizon),
            date.fromisoformat(str(record.target_date)).isoformat(),
        )
        for record in records
    )
    if actual != expected:
        raise RuntimeError(
            "scheduled target multiset mismatch: "
            f"expected={dict(expected)} actual={dict(actual)}"
        )
    expected_predict_date = str(occurrence["predict_date"])
    for record in records:
        phase = record.prediction_phase or (record.extra or {}).get(
            "prediction_phase"
        )
        if record.scheme_id != str(item["base_scheme_id"]):
            raise RuntimeError(
                "scheduled prediction scheme_id mismatch: "
                f"{record.scheme_id} != {item['base_scheme_id']}"
            )
        if (
            date.fromisoformat(str(record.predict_date)).isoformat()
            != expected_predict_date
        ):
            raise RuntimeError(
                "scheduled prediction predict_date mismatch: "
                f"{record.predict_date} != {expected_predict_date}"
            )
        if phase != "scheduled_live":
            raise RuntimeError(
                "scheduled prediction_phase must be scheduled_live"
            )
        if record.run_id is not None and int(record.run_id) != int(run_id):
            raise RuntimeError(
                f"scheduled prediction run_id mismatch: {record.run_id}"
            )
        if (
            record.scheme_version is not None
            and record.scheme_version != frozen_version
        ):
            raise RuntimeError(
                "scheduled prediction scheme_version mismatch: "
                f"{record.scheme_version} != {frozen_version}"
            )
        extra_feature_date = (
            (record.extra or {}).get("feature_date")
            if isinstance(record.extra, Mapping)
            else None
        )
        if (
            record.feature_date is not None
            and extra_feature_date is not None
            and str(record.feature_date) != str(extra_feature_date)
        ):
            raise RuntimeError(
                "scheduled record feature_date fields disagree: "
                f"record={record.feature_date} extra={extra_feature_date}"
            )
        observed_feature_date = (
            record.feature_date
            if record.feature_date is not None
            else extra_feature_date
        )
        if observed_feature_date is None:
            raise RuntimeError(
                "scheduled record feature_date is required"
            )
        try:
            normalized_feature_date = date.fromisoformat(
                str(observed_feature_date)
            ).isoformat()
        except ValueError as exc:
            raise RuntimeError(
                "scheduled record feature_date must be an ISO date: "
                f"{observed_feature_date}"
            ) from exc
        if normalized_feature_date != generation_feature_date:
            raise RuntimeError(
                "scheduled record feature_date mismatch: "
                f"{normalized_feature_date} != {generation_feature_date}"
            )
    if any(str(target["status"]) != "PENDING" for target in targets):
        raise RuntimeError(
            f"schedule item targets were already accepted: {item['item_id']}"
        )


def _read_scheduled_completion_expectation(
    engine: Engine,
    *,
    run_id: int,
) -> ScheduledCompletionExpectation:
    """单事务读取 verifier 所需冻结期望，不锁表、不回查 Registry。"""
    with engine.begin() as conn:
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=False,
        )
        if run is None or run.get("schedule_item_id") is None:
            raise RuntimeError(f"scheduled run not found: {run_id}")
        item_id = int(run["schedule_item_id"])
        item = _read_schedule_item_conn(
            conn,
            item_id=item_id,
            for_update=False,
        )
        if item is None:
            raise RuntimeError(
                f"schedule item not found for run: {run_id}"
            )
        if int(item.get("current_run_id") or 0) != int(run_id):
            raise RuntimeError(
                f"stale scheduled run fence rejected run_id={run_id}"
            )
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(item["occurrence_id"]),
            for_update=False,
        )
        if occurrence is None:
            raise RuntimeError(
                "schedule occurrence not found for run: "
                f"{run_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        generation_id = item.get("input_generation_id")
        if not generation_id:
            raise RuntimeError(
                f"schedule item has no bound input generation: {item_id}"
            )
        generation = _read_input_generation_conn(
            conn,
            str(generation_id),
            for_update=False,
        )
        if generation is None:
            raise RuntimeError(
                f"bound input generation not found: {generation_id}"
            )
        native_generation = None
        if str(generation.get("generation_type")) == "databridge_v1":
            native_generation_id = generation.get(
                "native_generation_id"
            )
            if not native_generation_id:
                raise RuntimeError(
                    "databridge generation is missing native_generation_id"
                )
            native_generation = _read_input_generation_conn(
                conn,
                str(native_generation_id),
                for_update=False,
            )
            if native_generation is None:
                raise RuntimeError(
                    "native generation relation not found: "
                    f"{native_generation_id}"
                )
            _validate_databridge_native_relation(
                databridge=generation,
                native_generation=native_generation,
            )
        return ScheduledCompletionExpectation(
            run_id=int(run_id),
            item_id=item_id,
            occurrence_id=int(item["occurrence_id"]),
            base_scheme_id=str(item["base_scheme_id"]),
            runtime_type=str(item["runtime_type"]),
            generation_id=str(generation_id),
            manifest_uri=str(generation["manifest_uri"]),
            manifest_sha256=str(generation["manifest_sha256"]),
            generation_dataset_content_id=str(
                generation["dataset_content_id"]
            ),
            generation_schema_version=str(generation["schema_version"]),
            generation_exporter_version=str(
                generation["exporter_version"]
            ),
            feature_date=str(generation["feature_date"]),
            business_date=str(generation["business_date"]),
            scheme_version=str(item["scheme_version"]),
            code_sha256=str(item["code_sha256"]),
            config_sha256=str(item["config_sha256"]),
            native_generation_id=(
                str(native_generation["generation_id"])
                if native_generation is not None
                else None
            ),
            native_manifest_uri=(
                str(native_generation["manifest_uri"])
                if native_generation is not None
                else None
            ),
            native_manifest_sha256=(
                str(native_generation["manifest_sha256"])
                if native_generation is not None
                else None
            ),
            native_dataset_content_id=(
                str(native_generation["dataset_content_id"])
                if native_generation is not None
                else None
            ),
            native_schema_version=(
                str(native_generation["schema_version"])
                if native_generation is not None
                else None
            ),
            native_exporter_version=(
                str(native_generation["exporter_version"])
                if native_generation is not None
                else None
            ),
            native_feature_date=(
                str(native_generation["feature_date"])
                if native_generation is not None
                else None
            ),
            native_business_date=(
                str(native_generation["business_date"])
                if native_generation is not None
                else None
            ),
        )


def _assert_scheduled_completion_epoch(
    engine: Engine,
    expectation: ScheduledCompletionExpectation,
) -> None:
    """在 verifier 前后重读 occurrence 并校验 current exact epoch。"""
    with engine.connect() as conn:
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=int(expectation.occurrence_id),
            for_update=False,
        )
        if occurrence is None:
            raise RuntimeError(
                "schedule occurrence not found for completion: "
                f"{expectation.occurrence_id}"
            )
        _assert_occurrence_epoch_conn(occurrence, engine=conn)


def _validate_scheduled_completion_evidence(
    *,
    evidence: ScheduledCompletionEvidence,
    item: Mapping[str, object],
    generation: Mapping[str, object],
    native_generation: Mapping[str, object] | None = None,
) -> None:
    """逐字段核对 validator evidence 与冻结 item/generation 身份。"""
    expected = {
        "observed_generation_id": str(item["input_generation_id"]),
        "manifest_sha256": str(generation["manifest_sha256"]),
        "generation_dataset_content_id": str(
            generation["dataset_content_id"]
        ),
        "generation_schema_version": str(generation["schema_version"]),
        "generation_exporter_version": str(
            generation["exporter_version"]
        ),
        "feature_date": str(generation["feature_date"]),
        "scheme_version": str(item["scheme_version"]),
        "code_sha256": str(item["code_sha256"]),
        "config_sha256": str(item["config_sha256"]),
    }
    for field, expected_value in expected.items():
        actual_value = str(getattr(evidence, field))
        if actual_value != expected_value:
            raise RuntimeError(
                "scheduled completion evidence mismatch: "
                f"{field} expected={expected_value} actual={actual_value}"
            )
    if str(generation.get("generation_type")) == "databridge_v1":
        if native_generation is None:
            raise RuntimeError(
                "scheduled completion is missing linked native generation"
            )
        native_expected = {
            "native_generation_id": str(
                native_generation["generation_id"]
            ),
            "native_manifest_sha256": str(
                native_generation["manifest_sha256"]
            ),
            "native_dataset_content_id": str(
                native_generation["dataset_content_id"]
            ),
            "native_schema_version": str(
                native_generation["schema_version"]
            ),
            "native_exporter_version": str(
                native_generation["exporter_version"]
            ),
            "native_feature_date": str(
                native_generation["feature_date"]
            ),
        }
        for field, expected_value in native_expected.items():
            actual_value = getattr(evidence, field)
            if actual_value is None or str(actual_value) != expected_value:
                raise RuntimeError(
                    "scheduled completion evidence mismatch: "
                    f"{field} expected={expected_value} "
                    f"actual={actual_value}"
                )
    elif any(
        getattr(evidence, field) is not None
        for field in (
            "native_generation_id",
            "native_manifest_sha256",
            "native_dataset_content_id",
            "native_schema_version",
            "native_exporter_version",
            "native_feature_date",
        )
    ):
        raise RuntimeError(
            "native scheduled completion evidence cannot contain a linked "
            "native generation"
        )


def _validate_cache_qualified_completion(
    *,
    occurrence: Mapping[str, object],
    item: Mapping[str, object],
    generation: Mapping[str, object],
    records: Iterable[PredictionRecord],
) -> None:
    """在提交事务内二次复核冻结 qualification 与 cache acceptance 文件。"""
    policy = _stored_json_mapping(occurrence, "policy_json")
    if "direct_cache_authorities" in policy:
        _validate_direct_cache_authority_completion(
            policy=policy,
            item=item,
            generation=generation,
            records=records,
        )
        return
    scheme_id = str(item["base_scheme_id"])
    raw_qualifications = policy.get("cache_use_qualifications")
    raw_schemes = policy.get("schemes")
    expected_spec = None
    if isinstance(raw_schemes, list):
        matches = [
            row
            for row in raw_schemes
            if (
                isinstance(row, Mapping)
                and row.get("scheme_id") == scheme_id
            )
        ]
        if len(matches) == 1:
            expected_spec = matches[0].get(
                "cache_spec_fingerprint"
            )
    raw_qualification = (
        raw_qualifications.get(scheme_id)
        if isinstance(raw_qualifications, Mapping)
        else None
    )
    if raw_qualification is None:
        if expected_spec is not None:
            raise RuntimeError(
                "cache-qualified completion is missing frozen qualification"
            )
        return
    if not isinstance(raw_qualification, Mapping):
        raise RuntimeError(
            "frozen cache qualification must be an object"
        )
    if str(generation.get("generation_type")) != "native_source":
        raise RuntimeError(
            "cache qualification is only valid for Native generation"
        )
    candidate = policy.get("capacity_candidate_fingerprint")
    if not isinstance(candidate, str):
        raise RuntimeError(
            "cache qualification is missing capacity candidate"
        )
    try:
        trusted = validate_trusted_cache_use_qualification(
            raw_qualification,
            expected_base_scheme_id=scheme_id,
            expected_candidate_fingerprint=candidate,
        )
    except ValueError as exc:
        raise RuntimeError(
            "frozen cache qualification is invalid"
        ) from exc
    qualification = trusted["qualification"]
    expected_consumer = {
        "scheme_version": str(item["scheme_version"]),
        "cache_group": str(item["cache_group"]),
        "spec_fingerprint": expected_spec,
    }
    mismatches = [
        field
        for field, expected_value in expected_consumer.items()
        if qualification.get(field) != expected_value
    ]
    if mismatches:
        raise RuntimeError(
            "cache completion consumer identity mismatch: "
            + ",".join(sorted(mismatches))
        )
    expected_native_generation = {
        "generation_id": str(generation["generation_id"]),
        "manifest_sha256": str(generation["manifest_sha256"]),
        "dataset_content_id": str(generation["dataset_content_id"]),
        "business_date": str(generation["business_date"]),
        "feature_date": str(generation["feature_date"]),
        "schema_version": str(generation["schema_version"]),
        "exporter_version": str(generation["exporter_version"]),
    }
    # 延迟 import 避免通用 repository 启动路径无条件加载 pandas/numpy。
    from shared.liwei_0616_phase_a_cache import (
        verify_phase_a_cache_audit_files,
    )

    saw_record = False
    for record in records:
        saw_record = True
        try:
            validate_prediction_cache_audit(
                record.extra,
                expected_qualification=trusted,
                expected_native_generation=expected_native_generation,
            )
            verify_phase_a_cache_audit_files(
                record.extra or {},
                expected_qualification=trusted,
            )
        except ValueError as exc:
            raise RuntimeError(
                "cache-qualified completion audit verification failed"
            ) from exc
    if not saw_record:
        raise RuntimeError(
            "cache-qualified completion contains no records"
        )


_DIRECT_CACHE_AUTHORITY_FIELDS = frozenset(
    {"schema_version", "storage_root", "contract", "consumers"}
)
_DIRECT_CACHE_CONTRACT = {
    "manifest_schema_version": 3,
    "input_state_schema_version": 3,
    "cache_abi_version": "liwei_0616.phase_a.v1",
    "projection_schema_version":
        "liwei-0616-auxiliary-dependency-projection-v1",
    "native_generation_type": "native_source",
    "native_generation_schema_version": "native-generation-v1",
    "native_exporter_version": "native-generation-exporter-v1",
}
_DIRECT_CACHE_CONSUMER_FIELDS = frozenset(
    {
        "base_scheme_id",
        "cache_consumer_id",
        "scheme_version",
        "code_sha256",
        "config_sha256",
        "cache_group",
        "spec_fingerprint",
        "publisher_consumer_id",
        "cache_family",
        "tenor",
        "access_mode",
        "cache_adapter_sha256",
        "cache_core_sha256",
        "publisher_projection_proof_identity_sha256",
        "daily_dependency_lookback_rows",
        "daily_dependency_proof",
    }
)
_DIRECT_CACHE_PUBLISHER_BUILD_STATES = {
    "full": (
        "cold_build",
        frozenset(
            {
                "no_current_generation",
                "current_generation_invalid",
                "spec_changed",
                "baseline_set_changed",
                "input_revision",
                "weekly_input_revision_unmappable",
                "monthly_input_revision_unmappable",
                "weekly_input_append_unmappable",
                "monthly_input_append_unmappable",
                "effective_auxiliary_projection_missing_from_parent",
                "effective_auxiliary_projection_missing_current",
                "effective_auxiliary_proof_changed",
                "date_to_week_history_changed",
                "effective_auxiliary_projection_unknown",
                "effective_auxiliary_revision_unknown",
            }
        ),
    ),
    "suffix": (
        "extended",
        frozenset(
            {
                "proven_daily_input_revision",
                "combined_daily_effective_revision",
                "effective_auxiliary_revision",
            }
        ),
    ),
    "rebind": (
        "extended",
        frozenset({"native_generation_rebound"}),
    ),
    "append": (
        "extended",
        frozenset(
            {
                "append_only",
                "cache_complete",
                "effective_auxiliary_append",
            }
        ),
    ),
    "migration": (
        "extended",
        frozenset({"legacy_v1_migration"}),
    ),
    "qualification": (
        "hit",
        frozenset({"compare_gate_qualification"}),
    ),
}
_DIRECT_CACHE_PUBLISHER_HIT_REASONS = frozenset(
    {"cache_complete", "truncated_request_preserved"}
)


def _validate_direct_cache_authority_completion(
    *,
    policy: Mapping[str, object],
    item: Mapping[str, object],
    generation: Mapping[str, object],
    records: Iterable[PredictionRecord],
) -> None:
    """按 occurrence 冻结的 direct authority 安全重开 cache generation。"""
    raw_authority = policy.get("direct_cache_authorities")
    if (
        not isinstance(raw_authority, Mapping)
        or set(raw_authority) != _DIRECT_CACHE_AUTHORITY_FIELDS
        or raw_authority.get("schema_version")
        != "daily-direct-cache-authorities-v1"
    ):
        raise RuntimeError("direct cache authority envelope is invalid")
    raw_contract = raw_authority.get("contract")
    if (
        not isinstance(raw_contract, Mapping)
        or dict(raw_contract) != _DIRECT_CACHE_CONTRACT
    ):
        raise RuntimeError("direct cache authority contract is invalid")
    storage_root = _direct_cache_storage_root(
        raw_authority.get("storage_root")
    )
    raw_consumers = raw_authority.get("consumers")
    if not isinstance(raw_consumers, Mapping):
        raise RuntimeError("direct cache authority consumers are invalid")
    consumers = _normalize_direct_cache_consumers(raw_consumers)
    expected_consumers = _direct_cache_expected_consumer_ids(policy)
    if set(consumers) != expected_consumers:
        raise RuntimeError(
            "direct cache authority consumer set mismatch: "
            f"expected={sorted(expected_consumers)}, "
            f"actual={sorted(consumers)}"
        )
    _validate_direct_cache_publisher_graph(consumers)

    scheme_id = str(item["base_scheme_id"])
    consumer = consumers.get(scheme_id)
    if consumer is None:
        return
    expected_item = {
        "base_scheme_id": scheme_id,
        "scheme_version": str(item["scheme_version"]),
        "code_sha256": str(item["code_sha256"]),
        "config_sha256": str(item["config_sha256"]),
        "cache_group": str(item["cache_group"]),
    }
    item_drift = sorted(
        field
        for field, expected in expected_item.items()
        if consumer[field] != expected
    )
    expected_policy_identity = _direct_cache_policy_identity(
        policy,
        scheme_id,
    )
    for field, expected in expected_policy_identity.items():
        if consumer[field] != expected:
            item_drift.append(field)
    if item_drift:
        raise RuntimeError(
            "direct cache consumer identity mismatch: "
            + ",".join(sorted(set(item_drift)))
        )
    expected_native = _direct_cache_native_generation(
        generation,
        contract=raw_contract,
    )
    # 当前 frozen daily policy 中一个 cache consumer 只对应一个 target。
    # 事务锁内的 secure generation I/O 因而只允许执行一次；未来若放开
    # 多 target，必须先按 generation identity 去重后再重开。
    record_list = list(records)
    if len(record_list) != 1:
        raise RuntimeError(
            "direct cache completion requires exactly one record"
        )
    try:
        _verify_direct_cache_record(
            record_list[0],
            storage_root=storage_root,
            consumer=consumer,
            expected_native=expected_native,
            contract=raw_contract,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "direct cache completion audit verification failed: "
            f"{exc}"
        ) from exc


def _direct_cache_storage_root(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("direct cache storage_root is invalid")
    root = Path(value)
    if (
        not root.is_absolute()
        or str(root) != value
        or os.path.normpath(value) != value
        or ".." in root.parts
    ):
        raise RuntimeError(
            "direct cache storage_root must be absolute and normalized"
        )
    return root


def _direct_cache_expected_consumer_ids(
    policy: Mapping[str, object],
) -> set[str]:
    raw_schemes = policy.get("schemes")
    if not isinstance(raw_schemes, list):
        raise RuntimeError("direct cache frozen policy schemes are invalid")
    expected: set[str] = set()
    seen: set[str] = set()
    for index, row in enumerate(raw_schemes):
        if not isinstance(row, Mapping):
            raise RuntimeError(
                f"direct cache frozen policy scheme {index} is invalid"
            )
        scheme_id = row.get("scheme_id")
        if not isinstance(scheme_id, str) or not scheme_id:
            raise RuntimeError(
                f"direct cache frozen policy scheme {index} has no id"
            )
        if scheme_id in seen:
            raise RuntimeError(
                "direct cache frozen policy scheme ids are duplicated"
            )
        seen.add(scheme_id)
        if row.get("cache_spec_fingerprint") is not None:
            expected.add(scheme_id)
    return expected


def _normalize_direct_cache_consumers(
    raw_consumers: Mapping[object, object],
) -> dict[str, dict[str, object]]:
    consumers: dict[str, dict[str, object]] = {}
    for raw_key, raw in raw_consumers.items():
        if (
            not isinstance(raw_key, str)
            or not raw_key
            or not isinstance(raw, Mapping)
            or set(raw) != _DIRECT_CACHE_CONSUMER_FIELDS
        ):
            raise RuntimeError(
                "direct cache consumer authority is invalid"
            )
        normalized = dict(raw)
        for field in (
            "base_scheme_id",
            "cache_consumer_id",
            "scheme_version",
            "cache_group",
            "publisher_consumer_id",
            "cache_family",
            "tenor",
        ):
            normalized[field] = _require_nonempty(
                normalized[field],
                f"direct_cache_authorities.consumers.{raw_key}.{field}",
            )
        for field in (
            "code_sha256",
            "config_sha256",
            "spec_fingerprint",
            "cache_adapter_sha256",
            "cache_core_sha256",
            "publisher_projection_proof_identity_sha256",
        ):
            normalized[field] = _require_lower_sha256(
                normalized[field],
                f"direct_cache_authorities.consumers.{raw_key}.{field}",
            )
        lookback_rows = normalized["daily_dependency_lookback_rows"]
        dependency_proof = normalized["daily_dependency_proof"]
        if lookback_rows is None and dependency_proof is None:
            pass
        elif (
            isinstance(lookback_rows, bool)
            or not isinstance(lookback_rows, int)
            or lookback_rows < 0
            or not isinstance(dependency_proof, str)
            or not dependency_proof.strip()
        ):
            raise RuntimeError(
                "direct cache consumer dependency proof is invalid: "
                f"{raw_key}"
            )
        if (
            normalized["base_scheme_id"] != raw_key
            or normalized["cache_consumer_id"] != raw_key
            or normalized["access_mode"] not in {"publisher", "read_only"}
            or normalized["cache_group"]
            != (
                f"{normalized['cache_family']}:"
                f"{normalized['tenor']}"
            )
        ):
            raise RuntimeError(
                f"direct cache consumer identity is invalid: {raw_key}"
            )
        consumers[raw_key] = normalized
    return consumers


def _validate_direct_cache_publisher_graph(
    consumers: Mapping[str, Mapping[str, object]],
) -> None:
    groups: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    for scheme_id, consumer in consumers.items():
        groups.setdefault(str(consumer["cache_group"]), []).append(
            (scheme_id, consumer)
        )
    for cache_group, members in groups.items():
        self_publishers = [
            scheme_id
            for scheme_id, consumer in members
            if (
                consumer["access_mode"] == "publisher"
                and consumer["publisher_consumer_id"] == scheme_id
            )
        ]
        if len(self_publishers) != 1:
            raise RuntimeError(
                "direct cache group must have exactly one self-publisher: "
                f"{cache_group}"
            )
        publisher_id = self_publishers[0]
        publisher = consumers[publisher_id]
        shared_fields = (
            "cache_group",
            "cache_family",
            "tenor",
            "spec_fingerprint",
            "publisher_projection_proof_identity_sha256",
            "daily_dependency_lookback_rows",
            "daily_dependency_proof",
        )
        for scheme_id, consumer in members:
            expected_access = (
                "publisher"
                if scheme_id == publisher_id
                else "read_only"
            )
            if (
                consumer["publisher_consumer_id"] != publisher_id
                or consumer["access_mode"] != expected_access
                or any(
                    publisher[field] != consumer[field]
                    for field in shared_fields
                )
            ):
                raise RuntimeError(
                    "direct cache publisher graph is invalid: "
                    f"{scheme_id}"
                )


def _direct_cache_policy_identity(
    policy: Mapping[str, object],
    scheme_id: str,
) -> dict[str, str]:
    matches = [
        row
        for row in policy.get("schemes", [])
        if (
            isinstance(row, Mapping)
            and row.get("scheme_id") == scheme_id
        )
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"direct cache policy consumer is ambiguous: {scheme_id}"
        )
    row = matches[0]
    identity = {
        "cache_group": _require_nonempty(
            row.get("cache_group"),
            f"direct cache policy {scheme_id} cache_group",
        ),
        "spec_fingerprint": _require_lower_sha256(
            row.get("cache_spec_fingerprint"),
            f"direct cache policy {scheme_id} cache_spec_fingerprint",
        ),
        "cache_adapter_sha256": _require_lower_sha256(
            row.get("cache_adapter_sha256"),
            f"direct cache policy {scheme_id} cache_adapter_sha256",
        ),
        "cache_core_sha256": _require_lower_sha256(
            row.get("cache_core_sha256"),
            f"direct cache policy {scheme_id} cache_core_sha256",
        ),
    }
    return identity


def _direct_cache_native_generation(
    generation: Mapping[str, object],
    *,
    contract: Mapping[str, object],
) -> dict[str, object]:
    if (
        generation.get("state") != GENERATION_SEALED
        or generation.get("generation_type")
        != contract["native_generation_type"]
        or generation.get("schema_version")
        != contract["native_generation_schema_version"]
        or generation.get("exporter_version")
        != contract["native_exporter_version"]
    ):
        raise RuntimeError(
            "direct cache Native generation identity is invalid"
        )
    expected = {
        "generation_id": _require_nonempty(
            generation.get("generation_id"),
            "direct cache Native generation_id",
        ),
        "manifest_sha256": _require_lower_sha256(
            generation.get("manifest_sha256"),
            "direct cache Native manifest_sha256",
        ),
        "dataset_content_id": _require_lower_sha256(
            generation.get("dataset_content_id"),
            "direct cache Native dataset_content_id",
        ),
        "business_date": _stored_iso_date(
            generation,
            "business_date",
        ),
        "feature_date": _stored_iso_date(
            generation,
            "feature_date",
        ),
        "schema_version": str(generation["schema_version"]),
        "exporter_version": str(generation["exporter_version"]),
    }
    return expected


def _verify_direct_cache_record(
    record: PredictionRecord,
    *,
    storage_root: Path,
    consumer: Mapping[str, object],
    expected_native: Mapping[str, object],
    contract: Mapping[str, object],
) -> None:
    from shared.artifact_paths import safe_path_part
    from shared.liwei_0616_phase_a_cache import (
        _load_generation_directory,
        _validate_generation_acceptance_for_use,
        _verify_generation_acceptance_lineage,
        consumer_input_state_equivalence_sha256,
        validate_phase_a_cache_input_change_audit,
    )

    extra = record.extra
    if not isinstance(extra, Mapping):
        raise ValueError("direct cache prediction extra is missing")
    audit = extra.get("phase_a_cache")
    if not isinstance(audit, Mapping):
        raise ValueError("direct cache phase_a_cache audit is missing")
    generation_id = _require_nonempty(
        audit.get("generation_id"),
        "direct cache generation_id",
    )
    if safe_path_part(generation_id) != generation_id:
        raise ValueError("direct cache generation_id is unsafe")
    manifest_sha256 = _require_lower_sha256(
        audit.get("generation_manifest_sha256"),
        "direct cache generation_manifest_sha256",
    )
    expected_family_root = (
        storage_root
        / safe_path_part(str(consumer["cache_family"]))
        / safe_path_part(str(consumer["tenor"]).lower())
    )
    expected_generation_path = (
        expected_family_root / "generations" / generation_id
    )
    generation_path = Path(
        _require_nonempty(
            audit.get("generation_path"),
            "direct cache generation_path",
        )
    )
    if (
        generation_path != expected_generation_path
        or audit.get("family_root") != str(expected_family_root)
        or audit.get("current_pointer")
        != str(expected_family_root / "current.json")
        or audit.get("published") is not True
    ):
        raise ValueError("direct cache audit path is not canonical")
    if consumer["access_mode"] == "read_only" and (
        audit.get("status") != "hit"
        or audit.get("build_mode") != "hit"
        or audit.get("build_reason") != "consumer_validated_hit"
    ):
        raise ValueError(
            "direct cache read_only audit must be a "
            "consumer_validated_hit"
        )
    loaded = _load_generation_directory(
        generation_path,
        expected_generation_id=generation_id,
        expected_manifest_sha256=manifest_sha256,
        secure=True,
    )
    manifest = loaded.manifest
    expected_manifest_identity = {
        "schema_version": contract["manifest_schema_version"],
        "abi_version": contract["cache_abi_version"],
        "cache_family": consumer["cache_family"],
        "tenor": consumer["tenor"],
        "spec_fingerprint": consumer["spec_fingerprint"],
    }
    drift = [
        field
        for field, expected in expected_manifest_identity.items()
        if manifest.get(field) != expected
    ]
    input_state = manifest.get("input_state")
    effective_auxiliary = (
        input_state.get("effective_auxiliary")
        if isinstance(input_state, Mapping)
        else None
    )
    if (
        not isinstance(input_state, Mapping)
        or input_state.get("schema_version")
        != contract["input_state_schema_version"]
        or input_state.get("native_generation")
        != dict(expected_native)
        or not isinstance(effective_auxiliary, Mapping)
        or effective_auxiliary.get("schema_version")
        != contract["projection_schema_version"]
        or effective_auxiliary.get("proof_identity_sha256")
        != consumer["publisher_projection_proof_identity_sha256"]
    ):
        drift.append("input_state")
    acceptance = manifest.get("generation_acceptance_evidence")
    if (
        not isinstance(acceptance, Mapping)
        or acceptance.get("native_generation")
        != dict(expected_native)
        or audit.get("generation_acceptance") != acceptance
    ):
        drift.append("generation_acceptance")
    if drift:
        raise ValueError(
            "direct cache manifest identity mismatch: "
            + ",".join(sorted(set(drift)))
        )
    publisher_input_equivalence_sha256 = (
        consumer_input_state_equivalence_sha256(input_state)
    )
    _validate_direct_cache_audit_identity(
        audit,
        manifest=manifest,
        consumer=consumer,
        generation_id=str(loaded.generation_id),
        manifest_sha256=str(loaded.manifest_sha256),
        publisher_input_equivalence_sha256=(
            publisher_input_equivalence_sha256
        ),
        input_change_validator=(
            validate_phase_a_cache_input_change_audit
        ),
    )
    _validate_direct_cache_audit_state(
        audit,
        manifest=manifest,
        consumer=consumer,
    )
    lineage_qualification = {
        "qualification": {
            "cache_abi_version": contract["cache_abi_version"],
            "cache_family": consumer["cache_family"],
            "tenor": consumer["tenor"],
            "spec_fingerprint": consumer["spec_fingerprint"],
            "daily_dependency_lookback_rows":
                consumer["daily_dependency_lookback_rows"],
            "daily_dependency_proof":
                consumer["daily_dependency_proof"],
        }
    }
    _verify_generation_acceptance_lineage(
        loaded,
        trusted_qualification=lineage_qualification,
    )
    _validate_generation_acceptance_for_use(
        loaded,
        native_generation_binding=dict(expected_native),
    )
    _validate_direct_cache_monotonic_coverage(
        loaded,
        loader=_load_generation_directory,
    )


def _validate_direct_cache_audit_identity(
    audit: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    consumer: Mapping[str, object],
    generation_id: str,
    manifest_sha256: str,
    publisher_input_equivalence_sha256: str,
    input_change_validator: Callable[[object], dict[str, object]],
) -> None:
    input_state = manifest.get("input_state")
    if not isinstance(input_state, Mapping):
        raise ValueError("direct cache manifest input_state is missing")
    expected = {
        "version": manifest.get("abi_version"),
        "cache_family": consumer["cache_family"],
        "tenor": consumer["tenor"],
        "input_content_id": input_state.get("content_id"),
        "consumer_input_equivalence_sha256":
            publisher_input_equivalence_sha256,
    }
    drift = sorted(
        field
        for field, expected_value in expected.items()
        if audit.get(field) != expected_value
    )
    if drift:
        raise ValueError(
            "direct cache audit identity mismatch: "
            + ",".join(drift)
        )
    audit_input_change = input_change_validator(
        audit.get("input_change")
    )
    if (
        consumer["access_mode"] == "publisher"
        and audit.get("build_mode") != "hit"
        and audit_input_change
        != input_change_validator(manifest.get("input_change"))
    ):
        raise ValueError(
            "direct cache publisher build input-change does not match "
            "manifest"
        )
    manifest_compare_gate = manifest.get("compare_gate_evidence")
    if not isinstance(manifest_compare_gate, Mapping):
        raise ValueError(
            "direct cache manifest compare-gate evidence is missing"
        )
    expected_compare_gate = {
        **dict(manifest_compare_gate),
        "generation_id": generation_id,
        "generation_manifest_sha256": manifest_sha256,
    }
    if audit.get("compare_gate_evidence") != expected_compare_gate:
        raise ValueError(
            "direct cache compare-gate audit does not match manifest"
        )


def _validate_direct_cache_audit_state(
    audit: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    consumer: Mapping[str, object],
) -> None:
    status = audit.get("status")
    build_mode = audit.get("build_mode")
    build_reason = audit.get("build_reason")
    if consumer["access_mode"] == "read_only":
        expected = (
            "hit",
            "hit",
            "consumer_validated_hit",
        )
        if (status, build_mode, build_reason) != expected:
            raise ValueError(
                "direct cache audit state is invalid for read_only"
            )
        return
    if build_mode == "hit":
        if (
            status != "hit"
            or build_reason not in _DIRECT_CACHE_PUBLISHER_HIT_REASONS
        ):
            raise ValueError(
                "direct cache audit state is invalid for publisher hit"
            )
        return
    manifest_mode = manifest.get("build_mode")
    acceptance = manifest.get("generation_acceptance_evidence")
    acceptance_mode = (
        acceptance.get("build_mode")
        if isinstance(acceptance, Mapping)
        else None
    )
    expected_state = _DIRECT_CACHE_PUBLISHER_BUILD_STATES.get(
        str(build_mode)
    )
    if (
        build_mode != manifest_mode
        or build_mode != acceptance_mode
        or expected_state is None
        or status != expected_state[0]
        or build_reason not in expected_state[1]
    ):
        raise ValueError(
            "direct cache audit state does not match reopened generation"
        )


def _validate_direct_cache_monotonic_coverage(
    generation: object,
    *,
    loader: Callable[..., object],
) -> None:
    manifest = getattr(generation, "manifest")
    parent_record = manifest.get("generation_acceptance_evidence", {}).get(
        "parent"
    )
    if parent_record is None:
        return
    if not isinstance(parent_record, Mapping):
        raise ValueError("direct cache parent authority is invalid")
    parent_id = parent_record.get("generation_id")
    if (
        not isinstance(parent_id, str)
        or not parent_id
        or manifest.get("parent_generation_id") != parent_id
    ):
        raise ValueError("direct cache parent identity is invalid")
    parent = loader(
        getattr(generation, "path").parent / parent_id,
        expected_generation_id=parent_id,
        expected_manifest_sha256=parent_record.get("manifest_sha256"),
        secure=True,
    )
    current_caches = getattr(generation, "caches")
    parent_caches = getattr(parent, "caches")
    if set(current_caches) != set(parent_caches):
        raise ValueError("direct cache baseline set is not monotonic")
    for baseline, parent_cache in parent_caches.items():
        parent_dates = set(parent_cache["test_dates"])
        current_dates = set(current_caches[baseline]["test_dates"])
        if not parent_dates.issubset(current_dates):
            raise ValueError(
                f"direct cache {baseline} coverage is not monotonic"
            )


def _validate_stored_occurrence_cardinality(
    conn: Connection,
    occurrence: Mapping[str, object],
) -> None:
    occurrence_id = int(occurrence["occurrence_id"])
    item_count = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM t_schedule_items "
                "WHERE occurrence_id = :occurrence_id"
            ),
            {"occurrence_id": occurrence_id},
        ).scalar_one()
    )
    target_count = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM t_schedule_item_targets "
                "WHERE occurrence_id = :occurrence_id"
            ),
            {"occurrence_id": occurrence_id},
        ).scalar_one()
    )
    expected_items = int(occurrence["expected_item_count"])
    expected_targets = int(occurrence["expected_target_count"])
    if item_count != expected_items or target_count != expected_targets:
        raise RuntimeError(
            "stored daily occurrence cardinality mismatch: "
            f"expected items={expected_items} targets={expected_targets}, "
            f"got items={item_count} targets={target_count}"
        )


def _read_active_daily_registry_snapshot_rows(
    conn: Connection,
    *,
    target_dates: Mapping[str, str],
    item_policy_by_base: Mapping[str, Mapping[str, object]],
) -> list[Mapping[str, object]]:
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    rows = (
        conn.execute(
            text(
                """
                SELECT r.scheme_id, r.base_scheme_id, r.runtime_type,
                       r.status, r.frequency, r.task_type, r.target_tenor,
                       r.horizon
                FROM t_scheme_registry r
                WHERE r.status = 'active'
                  AND r.frequency = 'daily'
                ORDER BY r.base_scheme_id, r.scheme_id
                """
                + lock
            )
        )
        .mappings()
        .all()
    )
    registry_ids = {str(row["scheme_id"]) for row in rows}
    base_scheme_ids = {str(row["base_scheme_id"]) for row in rows}
    if set(item_policy_by_base) != base_scheme_ids:
        raise ValueError(
            "item policy must exactly match active daily base schemes: "
            f"missing={sorted(base_scheme_ids - set(item_policy_by_base))}, "
            f"extra={sorted(set(item_policy_by_base) - base_scheme_ids)}"
        )
    normalized_target_dates = {
        str(registry_id): date.fromisoformat(str(target_date)).isoformat()
        for registry_id, target_date in target_dates.items()
    }
    if set(normalized_target_dates) != registry_ids:
        raise ValueError(
            "target_dates must exactly match active daily Registry ids: "
            f"missing={sorted(registry_ids - set(normalized_target_dates))}, "
            f"extra={sorted(set(normalized_target_dates) - registry_ids)}"
        )
    version_by_base: dict[str, Mapping[str, object]] = {}
    for base_scheme_id in sorted(base_scheme_ids):
        policy = dict(item_policy_by_base[base_scheme_id])
        policy_scheme_version = _require_nonempty(
            str(policy.get("scheme_version") or ""),
            f"{base_scheme_id}.scheme_version",
        )
        version = _select_mapping_one_or_none(
            conn,
            """
            SELECT scheme_id, scheme_version, runtime_type,
                   code_hash AS code_sha256,
                   config_hash AS config_sha256, status
            FROM t_scheme_versions
            WHERE scheme_id = :scheme_id
              AND scheme_version = :scheme_version
              AND status = 'active'
            """,
            {
                "scheme_id": base_scheme_id,
                "scheme_version": policy_scheme_version,
            },
            for_update=True,
        )
        if version is None:
            raise ValueError(
                "active version not found for exact item policy "
                "(scheme_version): "
                f"{base_scheme_id}/{policy_scheme_version}"
            )
        version_by_base[base_scheme_id] = version
    frozen_rows: list[Mapping[str, object]] = []
    for source in rows:
        row = dict(source)
        base_scheme_id = str(row["base_scheme_id"])
        policy = dict(item_policy_by_base[base_scheme_id])
        version = version_by_base[base_scheme_id]
        if str(version["runtime_type"]) != str(row["runtime_type"]):
            raise ValueError(
                "Registry/version runtime_type mismatch for "
                f"{base_scheme_id}: registry={row['runtime_type']} "
                f"version={version['runtime_type']}"
            )
        row.update(
            {
                "scheme_version": version["scheme_version"],
                "code_sha256": version["code_sha256"],
                "config_sha256": version["config_sha256"],
            }
        )
        policy_scheme_version = _require_nonempty(
            str(policy.get("scheme_version") or ""),
            f"{base_scheme_id}.scheme_version",
        )
        policy_code_sha256 = _require_sha256(
            str(policy.get("code_sha256") or ""),
            f"{base_scheme_id}.code_sha256",
        )
        policy_config_sha256 = _require_sha256(
            str(policy.get("config_sha256") or ""),
            f"{base_scheme_id}.config_sha256",
        )
        identity_pairs = (
            ("scheme_version", policy_scheme_version),
            ("code_sha256", policy_code_sha256),
            ("config_sha256", policy_config_sha256),
        )
        for field, expected in identity_pairs:
            actual = str(row[field])
            if actual != expected:
                raise ValueError(
                    f"item policy {field} mismatch for {base_scheme_id}: "
                    f"expected={expected} active_db={actual}"
                )
        cache_group = _require_nonempty(
            str(policy.get("cache_group") or ""),
            f"{base_scheme_id}.cache_group",
        )
        resource_class = _require_nonempty(
            str(policy.get("resource_class") or ""),
            f"{base_scheme_id}.resource_class",
        )
        try:
            internal_workers = int(policy["internal_workers"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{base_scheme_id}.internal_workers must be a positive integer"
            ) from exc
        if internal_workers <= 0:
            raise ValueError(
                f"{base_scheme_id}.internal_workers must be a positive integer"
            )
        try:
            release_offset_minutes = int(
                policy["release_offset_minutes"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{base_scheme_id}.release_offset_minutes must be a "
                "non-negative integer"
            ) from exc
        if (
            isinstance(policy["release_offset_minutes"], bool)
            or release_offset_minutes < 0
        ):
            raise ValueError(
                f"{base_scheme_id}.release_offset_minutes must be a "
                "non-negative integer"
            )
        release_at = _require_policy_datetime(
            policy.get("release_at"),
            f"{base_scheme_id}.release_at",
        )
        deadline_at = _require_policy_datetime(
            policy.get("deadline_at"),
            f"{base_scheme_id}.deadline_at",
        )
        if _as_datetime(release_at, "release_at") >= _as_datetime(
            deadline_at,
            "deadline_at",
        ):
            raise ValueError(
                f"{base_scheme_id} release_at must be earlier than deadline_at"
            )
        row.update(
            {
                "target_date": normalized_target_dates[str(row["scheme_id"])],
                "cache_group": cache_group,
                "resource_class": resource_class,
                "internal_workers": internal_workers,
                "release_offset_minutes": release_offset_minutes,
                "release_at": release_at,
                "deadline_at": deadline_at,
            }
        )
        frozen_rows.append(row)
    return frozen_rows


def _datetime_text_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc_datetime6(value).isoformat(sep=" ")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("datetime policy values must be datetime or non-empty text")


def _require_policy_datetime(value: object, field: str) -> str:
    normalized = _datetime_text_or_none(value)
    if normalized is None:
        raise ValueError(f"{field} is required")
    try:
        parsed = _as_datetime(normalized, field)
    except RuntimeError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    return parsed.isoformat(sep=" ")


def _result_lastrowid(conn: Connection, result: object) -> int:
    value = getattr(result, "lastrowid", None)
    if value not in (None, 0):
        return int(value)
    if _dialect_name(conn) == "sqlite":
        return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    return int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one())


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


def _optional_stored_datetime(
    value: object,
    field: str,
) -> datetime | None:
    return None if value is None else _as_datetime(value, field)


def _stored_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or not str(value).strip():
        raise RuntimeError(f"invalid stored {field}: {value!r}")
    return str(value)


def _optional_stored_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_stored_schedule_failure_code(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if normalized not in SCHEDULE_FAILURE_CODES:
        raise RuntimeError(
            "non-canonical stored schedule failure_code; "
            f"explicit audit migration required: {normalized!r}"
        )
    return normalized


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _stored_iso_date(
    row: Mapping[str, object],
    field: str,
) -> str:
    value = row.get(field)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise RuntimeError(
                f"invalid stored {field}: {value!r}"
            ) from exc
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
            raise RuntimeError(
                f"invalid stored {field}: malformed JSON"
            ) from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(
            f"invalid stored {field}: expected JSON object"
        )
    return dict(value)


def _assert_deployed_epoch_payload(
    frozen: object,
    *,
    label: str,
    engine: Any | None = None,
) -> None:
    """所有 ledger 写入口都必须验证非零、exact epoch capability。"""
    assert_daily_coordinator_epoch_payload_matches_current(
        frozen,
        label=label,
        engine=engine,
    )


def _assert_occurrence_epoch_conn(
    occurrence: Mapping[str, object],
    *,
    engine: Any | None = None,
) -> None:
    policy = _stored_json_mapping(occurrence, "policy_json")
    _assert_deployed_epoch_payload(
        policy.get("daily_coordinator_epoch"),
        label="daily occurrence coordinator epoch",
        engine=engine,
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
    schedule_item_id: int | None = None,
    attempt_no: int | None = None,
    trigger_origin: str | None = None,
    queued_at: datetime | None = None,
    failure_code: str | None = None,
    execution_token: str | None = None,
    process_id: int | None = None,
    process_group_id: int | None = None,
    schedule_frequency: str | None = None,
    scheduled_control_plane: str | None = None,
    enforce_scheduled_live_ledger: bool = False,
    _clock: _LedgerClock | None = None,
) -> int:
    """创建一次不可变预测运行记录，返回 run_id。"""
    if prediction_phase is not None and prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}")
    if scheduled_control_plane not in {None, LAUNCHD_ONE_SHOT}:
        raise ValueError(
            "scheduled_control_plane must be launchd_one_shot when set"
        )
    if scheduled_control_plane is not None:
        if prediction_phase != "scheduled_live":
            raise ValueError(
                "scheduled_control_plane requires scheduled_live"
            )
        if schedule_item_id is not None:
            raise ValueError(
                "launchd_one_shot scheduled_live must not have schedule_item_id"
            )
    normalized_frequency = None
    if schedule_frequency is not None:
        normalized_frequency = _require_bounded_identifier(
            schedule_frequency,
            "schedule_frequency",
            max_length=16,
        )
        if normalized_frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError(
                "schedule_frequency must be daily, weekly, or monthly"
            )
    if (
        prediction_phase == "scheduled_live"
        and schedule_item_id is None
        and scheduled_control_plane != LAUNCHD_ONE_SHOT
    ):
        raise RuntimeError(
            "scheduled_live without schedule_item_id requires "
            "launchd_one_shot"
        )
    effective_queued_at = queued_at
    if queued_at is not None:
        effective_queued_at, _trusted_now = _ledger_event_time(
            queued_at,
            field="queued_at",
            clock=_clock,
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
            schedule_item_id=schedule_item_id,
            attempt_no=attempt_no,
            trigger_origin=trigger_origin,
            queued_at=effective_queued_at,
            failure_code=failure_code,
            execution_token=execution_token,
            process_id=process_id,
            process_group_id=process_group_id,
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
    schedule_item_id: int | None = None,
    attempt_no: int | None = None,
    trigger_origin: str | None = None,
    queued_at: datetime | None = None,
    failure_code: str | None = None,
    execution_token: str | None = None,
    process_id: int | None = None,
    process_group_id: int | None = None,
) -> int:
    """在调用方事务内创建 scheme run。"""
    normalized_trigger_origin = (
        _require_schedule_trigger_origin(trigger_origin)
        if trigger_origin is not None
        else None
    )
    normalized_failure_code = None
    if failure_code is not None:
        normalized_failure_code = (
            _require_schedule_failure_code(failure_code)
            if schedule_item_id is not None
            else _require_bounded_identifier(
                failure_code,
                "failure_code",
                max_length=64,
            )
        )
    sql = text(
        """
        INSERT INTO t_scheme_runs
            (scheme_id, scheme_version, runtime_type, run_type, prediction_phase, predict_date, status,
             harness_run_id, input_artifact_id, data_snapshot_id, records_expected,
             schedule_item_id, attempt_no, trigger_origin, queued_at, failure_code,
             execution_token, process_id, process_group_id)
        VALUES
            (:scheme_id, :scheme_version, :runtime_type, :run_type, :prediction_phase, :predict_date, :status,
             :harness_run_id, :input_artifact_id, :data_snapshot_id, :records_expected,
             :schedule_item_id, :attempt_no, :trigger_origin, :queued_at, :failure_code,
             :execution_token, :process_id, :process_group_id)
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
        "schedule_item_id": schedule_item_id,
        "attempt_no": attempt_no,
        "trigger_origin": normalized_trigger_origin,
        "queued_at": queued_at,
        "failure_code": normalized_failure_code,
        "execution_token": execution_token,
        "process_id": process_id,
        "process_group_id": process_group_id,
    }
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
        run = _read_schedule_run_conn(
            conn,
            run_id=int(run_id),
            for_update=True,
        )
        if run is not None and run.get("schedule_item_id") is not None:
            raise RuntimeError(
                "generic approved completion rejected ledger-bound "
                f"run_id={run_id}; use the daily ledger API"
            )
        expected_run_identity = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": exact_scheme_version,
            "runtime_type": "blackbox_v2",
            "run_type": "active",
            "predict_date": normalized_run_date,
            "status": "running",
            "records_expected": len(expected_targets),
            "attempt_no": None,
            "trigger_origin": None,
            "execution_token": None,
            "process_id": None,
            "process_group_id": None,
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
        _assert_run_not_ledger_bound_conn(
            conn,
            run_id=int(run_id),
            operation="generic active Native completion",
        )
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
        run = _read_schedule_run_conn(
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
            "schedule_item_id": None,
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

    该入口只接受未绑定 daily ledger 的普通 running run。目标集合、
    活跃 Registry、输入 authority、prediction、run 成功状态与成功日志
    在同一事务内复核或提交；任何一步失败都不保留部分结果。
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
        # Daily ledger 的锁序固定为 occurrence→ordered siblings→targets；
        # gray-gap 必须先复用同一 guard，再锁普通 run，不能让 insert-only
        # 绕过 migration 018 已冻结的 canonical prediction key。
        _assert_prediction_keys_not_frozen_by_daily_ledger_conn(
            conn,
            (asdict(record) for record in enriched_records),
        )
        run = _read_schedule_run_conn(
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
                  AND schedule_item_id IS NULL
                  AND attempt_no IS NULL
                  AND trigger_origin IS NULL
                  AND execution_token IS NULL
                  AND process_id IS NULL
                  AND process_group_id IS NULL
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
              AND schedule_item_id IS NULL
              AND attempt_no IS NULL
              AND trigger_origin IS NULL
              AND execution_token IS NULL
              AND process_id IS NULL
              AND process_group_id IS NULL
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
        run = _read_schedule_run_conn(
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
                  AND schedule_item_id IS NULL
                  AND attempt_no IS NULL
                  AND trigger_origin IS NULL
                  AND execution_token IS NULL
                  AND process_id IS NULL
                  AND process_group_id IS NULL
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
    ordinary_fields = (
        "schedule_item_id",
        "attempt_no",
        "trigger_origin",
        "execution_token",
        "process_id",
        "process_group_id",
    )
    unexpected = [
        field for field in ordinary_fields if run.get(field) is not None
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
    if mismatches or unexpected:
        details = mismatches + [f"non-ordinary fields={unexpected}" for _ in unexpected]
        raise RuntimeError(
            "Blackbox gray gap snapshot repair run identity mismatch: "
            + "; ".join(details)
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
        "state": GENERATION_SEALED,
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
        if refresh_date <= predict_date:
            raise ValueError(
                "source_authority refresh_date must be after historical "
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
    ordinary = all(
        str(run.get(field)) == expected
        for field, expected in expected_fields.items()
    ) and all(
        run.get(field) is None
        for field in (
            "schedule_item_id",
            "attempt_no",
            "trigger_origin",
            "execution_token",
            "process_id",
            "process_group_id",
        )
    )
    if not ordinary:
        raise RuntimeError(
            "gray gap completion requires an ordinary gray_live running run "
            f"with exact config identity: run_id={run_id}"
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
        _assert_run_not_ledger_bound_conn(
            conn,
            run_id=int(run_id),
            operation="generic failure",
        )
        run = _read_schedule_run_conn(
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
    if not insert_only:
        _assert_prediction_keys_not_frozen_by_daily_ledger_conn(
            conn,
            rows,
        )
    conn.execute(sql, rows)
    return len(rows)


def _assert_prediction_keys_not_frozen_by_daily_ledger_conn(
    conn: Connection,
    rows: Iterable[Mapping[str, object]],
) -> None:
    """阻止 generic UPSERT 改写 occurrence 已冻结的 canonical key。

    先按稳定 key 顺序锁 target，和 ledger completion 的 target→prediction
    顺序一致。迁移尚未启用的 legacy 数据库没有 ledger 表，此时保持旧
    写入行为，便于受控回滚。
    """
    if not _daily_schedule_ledger_schema_available_conn(conn):
        return
    keys = {
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in rows
    }
    candidate_items: dict[int, set[int]] = {}
    for scheme_id, tenor, horizon, target_date in sorted(keys):
        candidates = (
            conn.execute(
                text(
                    """
                    SELECT occurrence_id, item_id
                    FROM t_schedule_item_targets
                    WHERE base_scheme_id = :base_scheme_id
                      AND target_tenor = :target_tenor
                      AND horizon = :horizon
                      AND target_date = :target_date
                    ORDER BY occurrence_id, item_id, target_id
                    """
                ),
                {
                    "base_scheme_id": scheme_id,
                    "target_tenor": tenor,
                    "horizon": horizon,
                    "target_date": target_date,
                },
            )
            .mappings()
            .all()
        )
        for candidate in candidates:
            candidate_items.setdefault(
                int(candidate["occurrence_id"]),
                set(),
            ).add(int(candidate["item_id"]))
    if not candidate_items:
        return

    # 与 ledger completion 完全相同地先锁 occurrence→ordered siblings，
    # 再按 item/target_id 锁 target。不能按 tenor 逐行 FOR UPDATE，否则
    # 5Y/10Y 等逻辑顺序与 target_id 顺序相反时会制造死锁。
    for occurrence_id in sorted(candidate_items):
        occurrence = _read_schedule_occurrence_by_id_conn(
            conn,
            occurrence_id=occurrence_id,
            for_update=True,
        )
        if occurrence is None:
            continue
        _assert_occurrence_epoch_conn(occurrence, engine=conn)
        siblings = _read_schedule_items_for_occurrence_conn(
            conn,
            occurrence_id=occurrence_id,
            for_update=True,
        )
        sibling_ids = {
            int(item["item_id"])
            for item in siblings
        }
        frozen: list[Mapping[str, object]] = []
        for item_id in sorted(
            candidate_items[occurrence_id] & sibling_ids
        ):
            for target in _read_schedule_targets_conn(
                conn,
                item_id=item_id,
                for_update=True,
            ):
                identity = (
                    str(target["base_scheme_id"]),
                    str(target["target_tenor"]),
                    int(target["horizon"]),
                    str(target["target_date"]),
                )
                if identity in keys:
                    frozen.append(target)
        if not frozen:
            continue
        identities = [
            {
                "target_id": int(row["target_id"]),
                "occurrence_id": int(row["occurrence_id"]),
                "status": str(row["status"]),
                "accepted_run_id": _optional_int(
                    row.get("accepted_run_id")
                ),
                "accepted_prediction_id": _optional_int(
                    row.get("accepted_prediction_id")
                ),
            }
            for row in frozen
        ]
        first = frozen[0]
        raise RuntimeError(
            "generic prediction mutation rejected for frozen daily "
            "ledger target: "
            f"{first['base_scheme_id']}/{first['target_tenor']}/"
            f"h{first['horizon']}/{first['target_date']} "
            f"targets={identities}"
        )


def _daily_schedule_ledger_schema_available_conn(conn: Connection) -> bool:
    """确认 ledger Schema 是否存在；无法检查时必须有显式连接能力声明。"""
    try:
        return bool(inspect(conn).has_table("t_schedule_item_targets"))
    except NoInspectionAvailable as exc:
        capabilities = getattr(conn, "schema_capabilities", None)
        if (
            not isinstance(capabilities, Mapping)
            or "daily_schedule_ledger" not in capabilities
            or type(capabilities["daily_schedule_ledger"]) is not bool
        ):
            raise RuntimeError(
                "cannot determine daily ledger schema availability; "
                "connection is not SQLAlchemy-inspectable and has no explicit "
                "boolean daily_schedule_ledger capability"
            ) from exc
        return capabilities["daily_schedule_ledger"]


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
