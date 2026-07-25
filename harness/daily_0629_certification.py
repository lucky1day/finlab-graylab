"""0629 日频兼容方案的显式 no-persist 真实执行认证。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from scheduler.executor import run_scheme_subprocess
from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from shared.daily_0629_predict_adapter import (
    DAILY_0629_INTERNAL_FIELDS,
)
from shared.daily_0629_source_evidence import (
    DAILY_0629_SOURCE_ROLE,
    require_daily_0629_source_evidence,
    source_package_tree_sha256,
)
from shared.data_contract import (
    capture_source_commit_evidence,
    inspect_native_input_readiness,
)
from shared.input_artifacts import create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import build_daily_live_context
from shared.source_runtime_database import (
    load_source_runtime_database_config,
    preflight_source_runtime_database_access,
)


CERTIFICATION_SCHEMA_VERSION = "daily-0629-certification-v1"
REAL_RUN_OPT_IN_ENV = "BFL_DAILY_0629_CERTIFY_REAL"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ALGO_ENV = "forecast_env"
_GIT_PATH = Path("/usr/bin/git")
_CONTROL_ISOLATION_ENV = "BFL_DAILY_0629_CERT_CONTROL_ISOLATION"
_CANDIDATE_EXECUTABLE_ROOTS = (
    "backend",
    "backtests",
    "harness",
    "scheduler",
    "schemes",
    "scripts",
    "shared",
    "tests",
)
_CANDIDATE_IMPORTABLE_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyi", ".so", ".dylib", ".pyd"}
)
_CANDIDATE_RUNTIME_ROOTS = frozenset(
    {
        ".claude",
        ".git",
        ".pytest_cache",
        "backtest_artifacts",
        "dist",
        "logs",
        "outputs",
        "reports",
    }
)
_ADMITTED_SPECS = {
    "daily_1y_xgb_1y13_0629": {
        "target_tenor": "1Y",
        "horizon": 1,
        "model_version": "1Y13",
        "frequency": "D1Y",
        "target_col": "TB1YWI0C",
        "timeout_sec": 600,
        "required_internal_fields": (
            "frequency",
            "final_select_id",
            "candidate_id",
            "target_col",
            "prediction_mode",
            "candidate_pred",
            "raw_direction",
            "prob_up",
            "active_score",
            "threshold",
        ),
    },
}
_BUSINESS_WRITE_GUARD_TABLES = (
    "t_schema_migrations",
    "t_target_registry",
    "t_scheme_registry",
    "t_scheme_versions",
    "t_scheme_runs",
    "t_scheme_predictions",
    "t_scheme_run_log",
    "t_scheme_serving_pointer",
    "t_scheme_actuals",
    "t_scheme_weekly_actuals",
    "t_scheme_monthly_actuals",
    "t_backtest_runs",
    "t_backtest_predictions",
    "t_backtest_monthly_metrics",
    "t_backtest_reproduction_checks",
    "t_harness_runs",
    "t_harness_gate_results",
    "t_input_artifacts",
    "t_input_generations",
    "t_schedule_occurrences",
    "t_schedule_items",
    "t_schedule_item_targets",
    "t_scheduler_heartbeat",
    "t_pre_market_forecast",
    "t_shap",
)


class Daily0629CertificationError(RuntimeError):
    """0629 真实认证的稳定、脱敏错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Daily0629CandidateIdentity:
    """认证期间必须保持不变的完整非忽略工作树身份。"""

    git_head: str
    tree_sha256: str
    file_count: int
    worktree_clean: bool
    git_path: Path
    git_sha256: str


@dataclass(frozen=True)
class Daily0629RuntimeIdentity:
    """0629 认证唯一允许的 conda/source Python 身份。"""

    algo_env: str
    conda_path: Path
    conda_sha256: str
    conda_runtime_tree_sha256: str
    conda_runtime_tree_file_count: int
    conda_runtime_tree_bytes: int
    conda_configuration_sha256: str
    source_python_path: Path
    source_python_sha256: str
    conda_explicit_sha256: str
    conda_explicit_package_count: int
    python_packages_sha256: str
    python_package_count: int
    runtime_tree_sha256: str
    runtime_tree_file_count: int
    runtime_tree_bytes: int
    control_python_path: Path
    control_python_sha256: str
    control_runtime_tree_sha256: str
    control_runtime_tree_file_count: int
    control_runtime_tree_bytes: int


@dataclass(frozen=True)
class _ReportDestination:
    path: Path
    parent_device: int
    parent_inode: int


Runner = Callable[..., list[PredictionRecord]]


def _require_control_process_isolation() -> None:
    """确认空 pycache prefix 已在当前解释器启动前生效。"""
    raw_prefix = os.environ.get("PYTHONPYCACHEPREFIX", "").strip()
    raw_identity = os.environ.get(_CONTROL_ISOLATION_ENV, "").strip()
    if (
        not raw_prefix
        or not raw_identity
        or sys.pycache_prefix != raw_prefix
        or not sys.dont_write_bytecode
        or not sys.flags.no_user_site
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONTROL_ISOLATION_REQUIRED"
        )
    prefix = Path(raw_prefix)
    try:
        if prefix.resolve(strict=True) != prefix:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CONTROL_ISOLATION_INVALID"
            )
        details = prefix.lstat()
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONTROL_ISOLATION_INVALID"
        ) from exc
    expected_identity = f"{details.st_dev}:{details.st_ino}"
    if (
        raw_identity != expected_identity
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONTROL_ISOLATION_INVALID"
        )
    try:
        if any(prefix.iterdir()):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CONTROL_ISOLATION_DIRTY"
            )
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONTROL_ISOLATION_INVALID"
        ) from exc


def certify_daily_0629_source_execution(
    *,
    scheme_id: str,
    predict_date: str,
    report_path: str | Path,
    runner: Runner = run_scheme_subprocess,
) -> dict[str, object]:
    """在当前只读水位连续执行两次，并生成私有 no-persist 证据。"""
    spec = _ADMITTED_SPECS.get(str(scheme_id))
    if spec is None:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_SCHEME_NOT_ADMITTED"
        )
    normalized_predict_date = _canonical_date(
        predict_date,
        "DAILY_0629_CERT_PREDICT_DATE_INVALID",
    )
    destination = _require_private_new_report_path(report_path)
    _require_control_process_isolation()
    candidate = _freeze_candidate_identity()
    runtime = _freeze_runtime_identity()
    try:
        database_config = load_source_runtime_database_config()
        preflight = preflight_source_runtime_database_access(
            database_config
        )
    except Exception as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_SOURCE_DATABASE_PREFLIGHT_FAILED"
        ) from exc
    engine = create_input_engine(database_config=database_config)
    guard_engine = None
    try:
        guard_engine = create_engine_from_env()
        calendar = get_calendar(engine)
        if not calendar.is_trading_day(normalized_predict_date):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_PREDICT_DATE_NOT_TRADING_DAY"
            )
        context = build_daily_live_context(
            calendar,
            normalized_predict_date,
            horizon=int(spec["horizon"]),
        )
        readiness = inspect_native_input_readiness(
            engine,
            feature_date=context.feature_date,
        )
        if not readiness.ready:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_INPUT_NOT_READY"
            )

        source = require_daily_0629_source_evidence(scheme_id)
        package_sha_before = source_package_tree_sha256(
            source.source_package_path
        )
        if package_sha_before != source.source_package_hash:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_SOURCE_PACKAGE_DRIFT"
            )
        source_before = capture_source_commit_evidence(
            engine,
            feature_date=context.feature_date,
        )
        tables_before = _snapshot_business_table_state(
            guard_engine,
        )

        records_by_run: list[list[PredictionRecord]] = []
        durations: list[float] = []
        failure_codes: list[str] = []
        try:
            with _certification_environment(runtime):
                for _ in range(2):
                    preflight_codes = _collect_identity_guard_failures(
                        candidate,
                        runtime,
                        source_database_config=database_config,
                    )
                    failure_codes.extend(preflight_codes)
                    if preflight_codes:
                        break
                    started = time.monotonic()
                    try:
                        records = runner(
                            scheme_id,
                            normalized_predict_date,
                        algo_env=_ALGO_ENV,
                        timeout_sec=int(spec["timeout_sec"]),
                        source_database_config=database_config,
                    )
                        records_list = list(records)
                    except Exception:
                        failure_codes.append(
                            "DAILY_0629_CERT_RUN_FAILED"
                        )
                        break
                    durations.append(time.monotonic() - started)
                    records_by_run.append(records_list)
                    post_run_codes = _collect_identity_guard_failures(
                        candidate,
                        runtime,
                        source_database_config=database_config,
                    )
                    failure_codes.extend(post_run_codes)
                    if post_run_codes:
                        break
        except Exception:
            failure_codes.append(
                "DAILY_0629_CERT_CONTROL_ENVIRONMENT_FAILED"
            )

        postflight = _collect_postflight(
            source_package_path=source.source_package_path,
            source_engine=engine,
            feature_date=context.feature_date,
            guard_engine=guard_engine,
            candidate=candidate,
            runtime=runtime,
            source_database_config=database_config,
        )
        failure_codes.extend(postflight["failure_codes"])
        package_sha_after = postflight["package_sha256"]
        source_after = postflight["source_evidence"]
        tables_after = postflight["business_table_state"]
        if (
            tables_after is not None
            and tables_before != tables_after
        ):
            failure_codes.append(
                "DAILY_0629_CERT_BUSINESS_TABLE_CHANGED"
            )
        if (
            package_sha_after is not None
            and (
                package_sha_before != package_sha_after
                or package_sha_after != source.source_package_hash
            )
        ):
            failure_codes.append(
                "DAILY_0629_CERT_SOURCE_PACKAGE_DRIFT"
            )
        if (
            source_after is not None
            and (
                source_before.feature_date != source_after.feature_date
                or source_before.source_commit_token
                != source_after.source_commit_token
            )
        ):
            failure_codes.append(
                "DAILY_0629_CERT_SOURCE_WATERMARK_DRIFT"
            )
        if len(records_by_run) != 2:
            failure_codes.append("DAILY_0629_CERT_RUN_FAILED")
        selected_failure = _select_failure_code(failure_codes)
        if selected_failure is not None:
            raise Daily0629CertificationError(
                selected_failure
            )
        assert source_after is not None
        assert tables_after is not None

        canonical_runs = [
            _validate_and_canonicalize_records(
                records,
                scheme_id=scheme_id,
                predict_date=normalized_predict_date,
                feature_date=context.feature_date,
                target_date=context.target_date,
                spec=spec,
                source=source,
            )
            for records in records_by_run
        ]
        if canonical_runs[0] != canonical_runs[1]:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_NONDETERMINISTIC_OUTPUT"
            )
        record_bytes = _canonical_json_bytes(canonical_runs[0])
        report: dict[str, object] = {
            "schema_version": CERTIFICATION_SCHEMA_VERSION,
            "status": "PASSED",
            "scope":
                "live_source_no_persist_observed_watermark",
            "scheme_id": scheme_id,
            "predict_date": normalized_predict_date,
            "feature_date": context.feature_date,
            "target_date": context.target_date,
            "target_count": len(canonical_runs[0]),
            "run_count": 2,
            "run_duration_sec": [
                round(value, 6)
                for value in durations
            ],
            "full_record_deterministic": True,
            "canonical_record_sha256": hashlib.sha256(
                record_bytes
            ).hexdigest(),
            "canonical_records": canonical_runs[0],
            "source_package_sha256": source.source_package_hash,
            "source_package_unchanged": True,
            "source_commit_token":
                source_before.source_commit_token,
            "source_watermark_unchanged": True,
            "business_table_state_sha256":
                _business_table_state_sha256(tables_before),
            "business_table_count":
                len(_BUSINESS_WRITE_GUARD_TABLES),
            "business_tables_unchanged": True,
            "business_table_guard_scope":
                "all_platform_write_tables_full_content",
            "source_database_identity_sha256":
                database_config.cache_identity,
            "source_database_table_count": len(preflight.tables),
            "source_database_read_only_preflight": True,
            "candidate": {
                "git_head": candidate.git_head,
                "tree_sha256": candidate.tree_sha256,
                "file_count": candidate.file_count,
                "worktree_clean": candidate.worktree_clean,
                "git_path": str(candidate.git_path),
                "git_sha256": candidate.git_sha256,
                "binding":
                    "all_git_tracked_plus_all_executable_root_bytes_and_modes",
            },
            "runtime": {
                "algo_env": runtime.algo_env,
                "conda_path": str(runtime.conda_path),
                "conda_sha256": runtime.conda_sha256,
                "conda_runtime_tree_sha256":
                    runtime.conda_runtime_tree_sha256,
                "conda_runtime_tree_file_count":
                    runtime.conda_runtime_tree_file_count,
                "conda_runtime_tree_bytes":
                    runtime.conda_runtime_tree_bytes,
                "conda_configuration_sha256":
                    runtime.conda_configuration_sha256,
                "source_python_path":
                    str(runtime.source_python_path),
                "source_python_sha256":
                    runtime.source_python_sha256,
                "conda_explicit_sha256":
                    runtime.conda_explicit_sha256,
                "conda_explicit_package_count":
                    runtime.conda_explicit_package_count,
                "python_packages_sha256":
                    runtime.python_packages_sha256,
                "python_package_count":
                    runtime.python_package_count,
                "runtime_tree_sha256":
                    runtime.runtime_tree_sha256,
                "runtime_tree_file_count":
                    runtime.runtime_tree_file_count,
                "runtime_tree_bytes":
                    runtime.runtime_tree_bytes,
                "control_python_path":
                    str(runtime.control_python_path),
                "control_python_sha256":
                    runtime.control_python_sha256,
                "control_runtime_tree_sha256":
                    runtime.control_runtime_tree_sha256,
                "control_runtime_tree_file_count":
                    runtime.control_runtime_tree_file_count,
                "control_runtime_tree_bytes":
                    runtime.control_runtime_tree_bytes,
            },
            # 本阶段证明当前 live-source adapter；真实 occurrence fence
            # 仍由 scheduled executor / coordinator 集成门禁覆盖。
            "scheduled_compatibility_fence_exercised": False,
            "completed_at": datetime.now(
                timezone.utc
            ).isoformat(timespec="microseconds"),
        }
        final_identity_failures = _collect_identity_guard_failures(
            candidate,
            runtime,
            source_database_config=database_config,
        )
        final_failure = _select_failure_code(
            final_identity_failures
        )
        if final_failure is not None:
            raise Daily0629CertificationError(final_failure)
        _write_report_atomic(destination, report)
        return report
    finally:
        if guard_engine is not None:
            guard_engine.dispose()
        engine.dispose()


def _collect_identity_guard_failures(
    candidate: Daily0629CandidateIdentity,
    runtime: Daily0629RuntimeIdentity,
    *,
    source_database_config: object | None = None,
) -> list[str]:
    """独立执行代码与运行时身份检查，单项失败不跳过另一项。"""
    failures: list[str] = []
    try:
        _verify_candidate_identity(candidate)
    except Daily0629CertificationError as exc:
        failures.append(exc.code)
    except Exception:
        failures.append(
            "DAILY_0629_CERT_CANDIDATE_GUARD_FAILED"
        )
    try:
        _verify_runtime_identity(runtime)
    except Daily0629CertificationError as exc:
        failures.append(exc.code)
    except Exception:
        failures.append(
            "DAILY_0629_CERT_RUNTIME_GUARD_FAILED"
        )
    if source_database_config is not None:
        try:
            current_database_config = (
                load_source_runtime_database_config()
            )
            if current_database_config != source_database_config:
                failures.append(
                    "DAILY_0629_CERT_SOURCE_DATABASE_CONFIG_DRIFT"
                )
        except Exception:
            failures.append(
                "DAILY_0629_CERT_SOURCE_DATABASE_CONFIG_GUARD_FAILED"
            )
    return failures


def _collect_postflight(
    *,
    source_package_path: Path,
    source_engine,
    feature_date: str,
    guard_engine,
    candidate: Daily0629CandidateIdentity,
    runtime: Daily0629RuntimeIdentity,
    source_database_config: object,
) -> dict[str, object]:
    """执行全部 postflight；任何一项失败都不能短路其余安全检查。"""
    failures: list[str] = []
    package_sha256: str | None = None
    source_evidence: object | None = None
    business_table_state: dict[str, object] | None = None
    try:
        package_sha256 = source_package_tree_sha256(
            source_package_path
        )
    except Exception:
        failures.append(
            "DAILY_0629_CERT_SOURCE_PACKAGE_GUARD_FAILED"
        )
    try:
        source_evidence = capture_source_commit_evidence(
            source_engine,
            feature_date=feature_date,
        )
    except Exception:
        failures.append(
            "DAILY_0629_CERT_SOURCE_WATERMARK_GUARD_FAILED"
        )
    try:
        business_table_state = _snapshot_business_table_state(
            guard_engine
        )
    except Exception:
        failures.append(
            "DAILY_0629_CERT_BUSINESS_TABLE_GUARD_FAILED"
        )
    failures.extend(
        _collect_identity_guard_failures(
            candidate,
            runtime,
            source_database_config=source_database_config,
        )
    )
    return {
        "package_sha256": package_sha256,
        "source_evidence": source_evidence,
        "business_table_state": business_table_state,
        "failure_codes": failures,
    }


def _select_failure_code(
    failure_codes: Sequence[str],
) -> str | None:
    """按副作用风险优先返回稳定错误码。"""
    if not failure_codes:
        return None
    priorities = (
        "DAILY_0629_CERT_BUSINESS_TABLE_CHANGED",
        "DAILY_0629_CERT_BUSINESS_TABLE_GUARD_FAILED",
        "DAILY_0629_CERT_SOURCE_PACKAGE_DRIFT",
        "DAILY_0629_CERT_SOURCE_PACKAGE_GUARD_FAILED",
        "DAILY_0629_CERT_SOURCE_WATERMARK_DRIFT",
        "DAILY_0629_CERT_SOURCE_WATERMARK_GUARD_FAILED",
        "DAILY_0629_CERT_SOURCE_DATABASE_CONFIG_DRIFT",
        "DAILY_0629_CERT_SOURCE_DATABASE_CONFIG_GUARD_FAILED",
        "DAILY_0629_CERT_CANDIDATE_DRIFT",
        "DAILY_0629_CERT_CANDIDATE_GUARD_FAILED",
        "DAILY_0629_CERT_RUNTIME_DRIFT",
        "DAILY_0629_CERT_RUNTIME_GUARD_FAILED",
        "DAILY_0629_CERT_CONTROL_ENVIRONMENT_FAILED",
        "DAILY_0629_CERT_RUN_FAILED",
    )
    for code in priorities:
        if code in failure_codes:
            return code
    return str(failure_codes[0])


def _snapshot_business_table_state(
    engine,
) -> dict[str, object]:
    """在单一只读一致性快照中绑定所有平台可写表的结构与内容。"""
    from sqlalchemy import text

    snapshot: dict[str, object] = {}
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            for table_name in _BUSINESS_WRITE_GUARD_TABLES:
                table_row = connection.execute(
                    text(
                        """
                        SELECT
                            TABLE_TYPE,
                            ENGINE,
                            TABLE_COLLATION,
                            AUTO_INCREMENT,
                            CREATE_OPTIONS
                        FROM information_schema.tables
                        WHERE table_schema = DATABASE()
                          AND table_name = :table_name
                        """
                    ),
                    {"table_name": table_name},
                ).mappings().first()
                if table_row is None:
                    snapshot[table_name] = {
                        "exists": False,
                        "row_count": 0,
                        "schema_sha256":
                            hashlib.sha256(b"missing").hexdigest(),
                        "content_sha256":
                            hashlib.sha256(b"").hexdigest(),
                    }
                    continue
                columns = list(
                    connection.execute(
                        text(
                            """
                            SELECT
                                COLUMN_NAME,
                                COLUMN_TYPE,
                                IS_NULLABLE,
                                COLUMN_DEFAULT,
                                EXTRA,
                                COLLATION_NAME,
                                ORDINAL_POSITION
                            FROM information_schema.columns
                            WHERE table_schema = DATABASE()
                              AND table_name = :table_name
                            ORDER BY ORDINAL_POSITION
                            """
                        ),
                        {"table_name": table_name},
                    ).mappings()
                )
                if not columns:
                    raise Daily0629CertificationError(
                        "DAILY_0629_CERT_BUSINESS_TABLE_GUARD_FAILED"
                    )
                schema_payload = {
                    "table_type": _canonical_database_value(
                        table_row["TABLE_TYPE"]
                    ),
                    "engine": table_row["ENGINE"],
                    "table_collation": table_row[
                        "TABLE_COLLATION"
                    ],
                    "auto_increment": _canonical_database_value(
                        table_row["AUTO_INCREMENT"]
                    ),
                    "create_options": _canonical_database_value(
                        table_row["CREATE_OPTIONS"]
                    ),
                    "columns": [
                        {
                            key.lower(): _canonical_database_value(
                                row[key]
                            )
                            for key in (
                                "COLUMN_NAME",
                                "COLUMN_TYPE",
                                "IS_NULLABLE",
                                "COLUMN_DEFAULT",
                                "EXTRA",
                                "COLLATION_NAME",
                                "ORDINAL_POSITION",
                            )
                        }
                        for row in columns
                    ],
                    "indexes": _canonical_database_rows(
                        connection.execute(
                            text(
                                """
                                SELECT
                                    INDEX_NAME,
                                    NON_UNIQUE,
                                    SEQ_IN_INDEX,
                                    COLUMN_NAME,
                                    COLLATION,
                                    SUB_PART,
                                    NULLABLE,
                                    INDEX_TYPE,
                                    COMMENT,
                                    INDEX_COMMENT,
                                    IS_VISIBLE,
                                    EXPRESSION
                                FROM information_schema.statistics
                                WHERE table_schema = DATABASE()
                                  AND table_name = :table_name
                                ORDER BY
                                    INDEX_NAME,
                                    SEQ_IN_INDEX
                                """
                            ),
                            {"table_name": table_name},
                        ).mappings(),
                        (
                            "INDEX_NAME",
                            "NON_UNIQUE",
                            "SEQ_IN_INDEX",
                            "COLUMN_NAME",
                            "COLLATION",
                            "SUB_PART",
                            "NULLABLE",
                            "INDEX_TYPE",
                            "COMMENT",
                            "INDEX_COMMENT",
                            "IS_VISIBLE",
                            "EXPRESSION",
                        ),
                    ),
                    "constraints": _canonical_database_rows(
                        connection.execute(
                            text(
                                """
                                SELECT
                                    tc.CONSTRAINT_NAME,
                                    tc.CONSTRAINT_TYPE,
                                    tc.ENFORCED,
                                    kcu.ORDINAL_POSITION,
                                    kcu.COLUMN_NAME,
                                    kcu.REFERENCED_TABLE_SCHEMA,
                                    kcu.REFERENCED_TABLE_NAME,
                                    kcu.REFERENCED_COLUMN_NAME,
                                    rc.MATCH_OPTION,
                                    rc.UPDATE_RULE,
                                    rc.DELETE_RULE,
                                    cc.CHECK_CLAUSE
                                FROM information_schema.table_constraints tc
                                LEFT JOIN information_schema.key_column_usage kcu
                                  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                                 AND kcu.TABLE_NAME = tc.TABLE_NAME
                                 AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                                LEFT JOIN information_schema.referential_constraints rc
                                  ON rc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                                 AND rc.TABLE_NAME = tc.TABLE_NAME
                                 AND rc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                                LEFT JOIN information_schema.check_constraints cc
                                  ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                                 AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                                WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
                                  AND tc.TABLE_NAME = :table_name
                                ORDER BY
                                    tc.CONSTRAINT_NAME,
                                    kcu.ORDINAL_POSITION
                                """
                            ),
                            {"table_name": table_name},
                        ).mappings(),
                        (
                            "CONSTRAINT_NAME",
                            "CONSTRAINT_TYPE",
                            "ENFORCED",
                            "ORDINAL_POSITION",
                            "COLUMN_NAME",
                            "REFERENCED_TABLE_SCHEMA",
                            "REFERENCED_TABLE_NAME",
                            "REFERENCED_COLUMN_NAME",
                            "MATCH_OPTION",
                            "UPDATE_RULE",
                            "DELETE_RULE",
                            "CHECK_CLAUSE",
                        ),
                    ),
                    "triggers": _canonical_database_rows(
                        connection.execute(
                            text(
                                """
                                SELECT
                                    TRIGGER_NAME,
                                    EVENT_MANIPULATION,
                                    ACTION_ORDER,
                                    ACTION_CONDITION,
                                    ACTION_STATEMENT,
                                    ACTION_ORIENTATION,
                                    ACTION_TIMING,
                                    SQL_MODE,
                                    DEFINER,
                                    CHARACTER_SET_CLIENT,
                                    COLLATION_CONNECTION,
                                    DATABASE_COLLATION
                                FROM information_schema.triggers
                                WHERE trigger_schema = DATABASE()
                                  AND event_object_table = :table_name
                                ORDER BY
                                    TRIGGER_NAME,
                                    EVENT_MANIPULATION,
                                    ACTION_TIMING
                                """
                            ),
                            {"table_name": table_name},
                        ).mappings(),
                        (
                            "TRIGGER_NAME",
                            "EVENT_MANIPULATION",
                            "ACTION_ORDER",
                            "ACTION_CONDITION",
                            "ACTION_STATEMENT",
                            "ACTION_ORIENTATION",
                            "ACTION_TIMING",
                            "SQL_MODE",
                            "DEFINER",
                            "CHARACTER_SET_CLIENT",
                            "COLLATION_CONNECTION",
                            "DATABASE_COLLATION",
                        ),
                    ),
                }
                column_names = [
                    str(row["COLUMN_NAME"])
                    for row in columns
                ]
                select_list = ", ".join(
                    f"`{name.replace('`', '``')}`"
                    for name in column_names
                )
                result = connection.exec_driver_sql(
                    f"SELECT {select_list} FROM `{table_name}`",
                    execution_options={"stream_results": True},
                )
                row_hashes: list[str] = []
                while True:
                    batch = result.fetchmany(1000)
                    if not batch:
                        break
                    for row in batch:
                        row_payload = [
                            _canonical_database_value(value)
                            for value in tuple(row)
                        ]
                        row_hashes.append(
                            hashlib.sha256(
                                _canonical_json_bytes(row_payload)
                            ).hexdigest()
                        )
                row_hashes.sort()
                snapshot[table_name] = {
                    "exists": True,
                    "row_count": len(row_hashes),
                    "schema_sha256": hashlib.sha256(
                        _canonical_json_bytes(schema_payload)
                    ).hexdigest(),
                    "content_sha256": hashlib.sha256(
                        "\n".join(row_hashes).encode("ascii")
                    ).hexdigest(),
                }
        finally:
            connection.rollback()
    return snapshot


def _canonical_database_rows(
    rows,
    keys: Sequence[str],
) -> list[dict[str, object]]:
    return [
        {
            key.lower(): _canonical_database_value(row[key])
            for key in keys
        }
        for row in rows
    ]


def _canonical_database_value(value: object) -> object:
    """为 MySQL scalar 生成无歧义、无 NaN 的类型化表示。"""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            return ["float", repr(value)]
        return ["float", value.hex()]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, datetime_time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return [
            "bytes",
            base64.b64encode(bytes(value)).decode("ascii"),
        ]
    if isinstance(value, str):
        return ["str", value]
    raise Daily0629CertificationError(
        "DAILY_0629_CERT_BUSINESS_TABLE_GUARD_FAILED"
    )


def _business_table_state_sha256(
    snapshot: Mapping[str, object],
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(snapshot)
    ).hexdigest()


def _validate_and_canonicalize_records(
    records: Sequence[PredictionRecord],
    *,
    scheme_id: str,
    predict_date: str,
    feature_date: str,
    target_date: str,
    spec: Mapping[str, object],
    source: object,
) -> list[dict[str, object]]:
    if len(records) != 1:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_TARGET_COUNT_INVALID"
        )
    record = records[0]
    if not isinstance(record, PredictionRecord):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RECORD_TYPE_INVALID"
        )
    expected = (
        scheme_id,
        str(spec["target_tenor"]),
        int(spec["horizon"]),
        predict_date,
        feature_date,
        target_date,
        str(spec["model_version"]),
    )
    actual = (
        record.scheme_id,
        record.target_tenor,
        record.horizon,
        record.predict_date,
        record.feature_date,
        record.target_date,
        record.model_version,
    )
    if actual != expected or record.predicted_direction not in {-1, 0, 1}:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RECORD_CONTRACT_INVALID"
        )
    extra = record.extra
    if not isinstance(extra, dict):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_EXTRA_INVALID"
        )
    required = tuple(spec["required_internal_fields"])
    if (
        any(field not in extra for field in DAILY_0629_INTERNAL_FIELDS)
        or any(
            extra.get(field) is None
            or (
                isinstance(extra.get(field), str)
                and not str(extra.get(field)).strip()
            )
            for field in required
        )
        or any(field not in DAILY_0629_INTERNAL_FIELDS for field in required)
        or extra.get("source_role") != DAILY_0629_SOURCE_ROLE
        or extra.get("source_model_id")
        != getattr(source, "model_id", None)
        or extra.get("frequency") != spec["frequency"]
        or extra.get("final_select_id") != spec["model_version"]
        or extra.get("candidate_id")
        != getattr(source, "candidate_id", None)
        or extra.get("target_col") != spec["target_col"]
        or extra.get("source_package_hash")
        != getattr(source, "source_package_hash", None)
        or extra.get("source_output_date") != predict_date
        or extra.get("source_prediction_date") != feature_date
        or extra.get("input_cutoff_date") != feature_date
        or extra.get("feature_date") != feature_date
        or extra.get("source_rdate") != target_date
        or extra.get("target_date") != target_date
        or extra.get("input_artifact_source")
        != "shared_data_service_daily"
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_EXTRA_CONTRACT_INVALID"
        )
    artifact_path = extra.get("input_artifact_path")
    if (
        not isinstance(artifact_path, str)
        or not Path(artifact_path).is_absolute()
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_ARTIFACT_PATH_INVALID"
        )
    payload = asdict(record)
    _canonical_json_bytes(payload)
    return [payload]


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RESULT_NOT_CANONICAL"
        ) from exc


def _canonical_date(value: object, code: str) -> str:
    try:
        normalized = date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise Daily0629CertificationError(code) from exc
    if normalized != value:
        raise Daily0629CertificationError(code)
    return normalized


def _freeze_candidate_identity() -> Daily0629CandidateIdentity:
    """绑定当前全部 tracked 与非忽略 untracked 文件的实际 bytes。"""
    try:
        git_stat = _GIT_PATH.lstat()
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_GIT_IDENTITY_UNAVAILABLE"
        ) from exc
    if (
        not stat.S_ISREG(git_stat.st_mode)
        or git_stat.st_uid != 0
        or stat.S_IMODE(git_stat.st_mode) & 0o022
        or not os.access(_GIT_PATH, os.X_OK)
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_GIT_IDENTITY_UNSAFE"
        )
    git_sha256 = _file_sha256(
        _GIT_PATH,
        error_code="DAILY_0629_CERT_GIT_IDENTITY_UNAVAILABLE",
    )
    head = _git_output(("rev-parse", "HEAD")).decode("ascii").strip()
    if (
        len(head) != 40
        or any(character not in "0123456789abcdef" for character in head)
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CANDIDATE_HEAD_INVALID"
        )
    relative_paths, has_untracked_executable = (
        _candidate_relative_paths()
    )
    digest = hashlib.sha256()
    for raw_relative in relative_paths:
        relative = raw_relative.decode(
            "utf-8",
            errors="surrogateescape",
        )
        path = _PROJECT_ROOT / relative
        try:
            file_stat = path.lstat()
        except OSError as exc:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CANDIDATE_FILE_UNAVAILABLE"
            ) from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
            )
        digest.update(raw_relative)
        digest.update(b"\0")
        digest.update(
            f"{stat.S_IMODE(file_stat.st_mode):04o}".encode("ascii")
        )
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(
                    lambda: handle.read(1024 * 1024),
                    b"",
                ):
                    digest.update(chunk)
        except OSError as exc:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CANDIDATE_FILE_UNREADABLE"
            ) from exc
        digest.update(b"\0")
    dirty = has_untracked_executable or bool(
        _git_output(
            (
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
        )
    )
    return Daily0629CandidateIdentity(
        git_head=head,
        tree_sha256=digest.hexdigest(),
        file_count=len(relative_paths),
        worktree_clean=not dirty,
        git_path=_GIT_PATH,
        git_sha256=git_sha256,
    )


def _verify_candidate_identity(
    expected: Daily0629CandidateIdentity,
) -> None:
    current = _freeze_candidate_identity()
    if (
        current.git_head != expected.git_head
        or current.tree_sha256 != expected.tree_sha256
        or current.file_count != expected.file_count
        or current.worktree_clean != expected.worktree_clean
        or current.git_path != expected.git_path
        or current.git_sha256 != expected.git_sha256
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CANDIDATE_DRIFT"
        )


def _git_output(arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            [
                str(_GIT_PATH),
                "-c",
                "core.excludesFile=/dev/null",
                "-C",
                str(_PROJECT_ROOT),
                *arguments,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/var/empty",
                "LANG": "C",
                "PATH": "/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_GIT_IDENTITY_UNAVAILABLE"
        ) from exc
    return bytes(completed.stdout)


def _candidate_relative_paths() -> tuple[list[bytes], bool]:
    """绑定 tracked 文件及所有可执行源码根，拒绝 Git ignore 绕过。"""
    tracked = _git_output(("ls-files", "-z", "--cached"))
    tracked_paths = {
        item
        for item in tracked.split(b"\0")
        if item
    }
    relative_paths = set(tracked_paths)
    executable_paths: set[bytes] = set()
    git_control_paths = (
        _PROJECT_ROOT / ".git" / "config",
        _PROJECT_ROOT / ".git" / "info" / "exclude",
    )
    for path in git_control_paths:
        if path.is_file() and not path.is_symlink():
            relative_paths.add(
                os.fsencode(path.relative_to(_PROJECT_ROOT))
            )
    for path in _PROJECT_ROOT.iterdir():
        if (
            path.is_file()
            and not path.is_symlink()
            and path.name != ".DS_Store"
        ):
            relative_paths.add(
                os.fsencode(path.relative_to(_PROJECT_ROOT))
            )
    for root_name in _CANDIDATE_EXECUTABLE_ROOTS:
        root = _PROJECT_ROOT / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
            )
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            for name in directory_names:
                if (Path(current) / name).is_symlink():
                    raise Daily0629CertificationError(
                        "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
                    )
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name != "__pycache__"
            )
            for name in sorted(file_names):
                if name == ".DS_Store" or name.endswith(".pyc"):
                    continue
                path = Path(current) / name
                encoded = os.fsencode(
                    path.relative_to(_PROJECT_ROOT)
                )
                executable_paths.add(encoded)
                relative_paths.add(encoded)
    for current, directory_names, file_names in os.walk(
        _PROJECT_ROOT,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in directory_names:
            if (current_path / name).is_symlink():
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
                )
        if current_path == _PROJECT_ROOT:
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _CANDIDATE_RUNTIME_ROOTS
            )
        else:
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name != "__pycache__"
            )
        for name in file_names:
            path = current_path / name
            if path.suffix.lower() not in _CANDIDATE_IMPORTABLE_SUFFIXES:
                continue
            if (
                path.suffix.lower() == ".pyc"
                and "__pycache__" not in path.parts
            ):
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
                )
            if path.is_symlink() or not path.is_file():
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE"
                )
            encoded = os.fsencode(path.relative_to(_PROJECT_ROOT))
            executable_paths.add(encoded)
            relative_paths.add(encoded)
    return (
        sorted(relative_paths),
        bool(executable_paths - tracked_paths),
    )


def _freeze_runtime_identity() -> Daily0629RuntimeIdentity:
    expected_conda = (
        Path.home() / "miniconda3" / "bin" / "conda"
    )
    expected_source_python = (
        Path.home()
        / "miniconda3"
        / "envs"
        / _ALGO_ENV
        / "bin"
        / "python"
    )
    expected_control_python = (
        Path.home()
        / "miniconda3"
        / "envs"
        / "bond_factor_lab_service"
        / "bin"
        / "python"
    )
    try:
        conda = expected_conda.resolve(strict=True)
        source_python = expected_source_python.resolve(strict=True)
        control_python = expected_control_python.resolve(strict=True)
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_UNAVAILABLE"
        ) from exc
    if (
        Path(sys.executable).resolve() != control_python
        or not os.access(conda, os.X_OK)
        or not os.access(source_python, os.X_OK)
        or not os.access(control_python, os.X_OK)
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_INVALID"
        )
    conda_prefix = conda.parents[1]
    (
        conda_runtime_tree_sha256,
        conda_runtime_tree_file_count,
        conda_runtime_tree_bytes,
    ) = _runtime_tree_identity(
        conda_prefix,
        excluded_root_names=frozenset({"envs", "pkgs"}),
    )
    conda_configuration_sha256 = (
        _conda_configuration_identity(conda)
    )
    _probe_conda_runtime_resolution(
        conda,
        algo_env=_ALGO_ENV,
        expected_python=source_python,
    )
    conda_explicit_sha256, conda_package_count = (
        _conda_explicit_inventory(conda, source_python.parents[1])
    )
    python_packages_sha256, python_package_count = (
        _python_package_inventory(source_python)
    )
    (
        runtime_tree_sha256,
        runtime_tree_file_count,
        runtime_tree_bytes,
    ) = _runtime_tree_identity(source_python.parents[1])
    (
        control_runtime_tree_sha256,
        control_runtime_tree_file_count,
        control_runtime_tree_bytes,
    ) = _runtime_tree_identity(control_python.parents[1])
    return Daily0629RuntimeIdentity(
        algo_env=_ALGO_ENV,
        conda_path=conda,
        conda_sha256=_file_sha256(conda),
        conda_runtime_tree_sha256=conda_runtime_tree_sha256,
        conda_runtime_tree_file_count=(
            conda_runtime_tree_file_count
        ),
        conda_runtime_tree_bytes=conda_runtime_tree_bytes,
        conda_configuration_sha256=(
            conda_configuration_sha256
        ),
        source_python_path=source_python,
        source_python_sha256=_file_sha256(source_python),
        conda_explicit_sha256=conda_explicit_sha256,
        conda_explicit_package_count=conda_package_count,
        python_packages_sha256=python_packages_sha256,
        python_package_count=python_package_count,
        runtime_tree_sha256=runtime_tree_sha256,
        runtime_tree_file_count=runtime_tree_file_count,
        runtime_tree_bytes=runtime_tree_bytes,
        control_python_path=control_python,
        control_python_sha256=_file_sha256(control_python),
        control_runtime_tree_sha256=control_runtime_tree_sha256,
        control_runtime_tree_file_count=(
            control_runtime_tree_file_count
        ),
        control_runtime_tree_bytes=control_runtime_tree_bytes,
    )


def _verify_runtime_identity(
    expected: Daily0629RuntimeIdentity,
) -> None:
    current = _freeze_runtime_identity()
    if current != expected:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_DRIFT"
        )


def _conda_explicit_inventory(
    conda: Path,
    environment_prefix: Path,
) -> tuple[str, int]:
    try:
        completed = subprocess.run(
            [
                str(conda),
                "list",
                "--explicit",
                "--prefix",
                str(environment_prefix),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            env=_conda_process_environment(conda),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_INVENTORY_UNAVAILABLE"
        ) from exc
    lines = sorted(
        line.strip()
        for line in completed.stdout.decode(
            "utf-8",
            errors="strict",
        ).splitlines()
        if (
            line.strip()
            and not line.lstrip().startswith("#")
            and line.strip() != "@EXPLICIT"
        )
    )
    payload = _canonical_json_bytes(lines)
    return hashlib.sha256(payload).hexdigest(), len(lines)


def _conda_process_environment(conda: Path) -> dict[str, str]:
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.pathsep.join(
            (
                str(conda.parent),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return environment


def _conda_configuration_identity(conda: Path) -> str:
    try:
        completed = subprocess.run(
            [
                str(conda),
                "config",
                "--show-sources",
                "--json",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            env=_conda_process_environment(conda),
        )
        payload = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONDA_CONFIG_UNAVAILABLE"
        ) from exc
    if not isinstance(payload, dict):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONDA_CONFIG_INVALID"
        )
    return hashlib.sha256(
        _canonical_json_bytes(payload)
    ).hexdigest()


def _probe_conda_runtime_resolution(
    conda: Path,
    *,
    algo_env: str,
    expected_python: Path,
) -> None:
    script = (
        "import os,sys;"
        "print(os.path.realpath(sys.executable))"
    )
    try:
        completed = subprocess.run(
            [
                str(conda),
                "run",
                "--no-capture-output",
                "-n",
                algo_env,
                "python",
                "-I",
                "-B",
                "-c",
                script,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            env=_conda_process_environment(conda),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONDA_RESOLUTION_FAILED"
        ) from exc
    resolved = completed.stdout.decode(
        "utf-8",
        errors="strict",
    ).strip()
    if resolved != str(expected_python):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_CONDA_RESOLUTION_DRIFT"
        )


def _python_package_inventory(
    source_python: Path,
) -> tuple[str, int]:
    script = (
        "import importlib.metadata as m,json,re\n"
        "rows=[]\n"
        "for d in m.distributions():\n"
        " n=d.metadata.get('Name')\n"
        " if n:\n"
        "  rows.append((re.sub(r'[-_.]+','-',n).lower(),d.version))\n"
        "rows.sort()\n"
        "print(json.dumps(rows,separators=(',',':'),ensure_ascii=True))\n"
    )
    try:
        completed = subprocess.run(
            [str(source_python), "-I", "-B", "-c", script],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            env={
                "HOME": str(Path.home()),
                "PATH": os.pathsep.join(
                    (
                        str(source_python.parent),
                        "/usr/bin",
                        "/bin",
                    )
                ),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        parsed = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_INVENTORY_UNAVAILABLE"
        ) from exc
    if (
        not isinstance(parsed, list)
        or any(
            not isinstance(row, list)
            or len(row) != 2
            or not all(
                isinstance(value, str) and value
                for value in row
            )
            for row in parsed
        )
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_INVENTORY_INVALID"
        )
    payload = _canonical_json_bytes(parsed)
    return hashlib.sha256(payload).hexdigest(), len(parsed)


def _runtime_tree_identity(
    root_value: Path,
    *,
    excluded_root_names: frozenset[str] = frozenset(),
) -> tuple[str, int, int]:
    """绑定实际环境树中所有目录、文件和内部 symlink 的 bytes/mode。"""
    try:
        root = root_value.resolve(strict=True)
        root_details = root.lstat()
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_TREE_UNAVAILABLE"
        ) from exc
    if root.is_symlink() or not stat.S_ISDIR(root_details.st_mode):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_TREE_UNSAFE"
        )
    entries: list[Path] = []
    try:
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            if current_path == root:
                directory_names[:] = [
                    name
                    for name in directory_names
                    if name not in excluded_root_names
                ]
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                path = current_path / name
                if path.is_symlink():
                    entries.append(path)
                else:
                    retained_directories.append(name)
                    entries.append(path)
            directory_names[:] = retained_directories
            entries.extend(
                current_path / name
                for name in sorted(file_names)
            )
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_RUNTIME_TREE_UNAVAILABLE"
        ) from exc
    entries.sort(
        key=lambda path: os.fsencode(path.relative_to(root))
    )
    digest = hashlib.sha256()
    digest.update(b"root\0")
    digest.update(
        f"{stat.S_IMODE(root_details.st_mode):04o}".encode("ascii")
    )
    digest.update(b"\0")
    file_count = 0
    total_bytes = 0
    for path in entries:
        relative = os.fsencode(path.relative_to(root))
        try:
            details = path.lstat()
        except OSError as exc:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_RUNTIME_TREE_UNAVAILABLE"
            ) from exc
        digest.update(relative)
        digest.update(b"\0")
        digest.update(
            f"{stat.S_IMODE(details.st_mode):04o}".encode("ascii")
        )
        digest.update(b"\0")
        if stat.S_ISDIR(details.st_mode):
            digest.update(b"directory\0")
            continue
        file_count += 1
        if stat.S_ISLNK(details.st_mode):
            try:
                target_text = os.readlink(path)
                resolved_target = path.resolve(strict=True)
            except OSError as exc:
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_RUNTIME_TREE_UNSAFE"
                ) from exc
            if not resolved_target.is_relative_to(root):
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_RUNTIME_TREE_UNSAFE"
                )
            digest.update(b"symlink\0")
            digest.update(os.fsencode(target_text))
            digest.update(b"\0")
            continue
        if not stat.S_ISREG(details.st_mode):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_RUNTIME_TREE_UNSAFE"
            )
        flags = os.O_RDONLY
        for name in ("O_CLOEXEC", "O_NOFOLLOW"):
            flags |= int(getattr(os, name, 0))
        file_fd: int | None = None
        file_digest = hashlib.sha256()
        try:
            file_fd = os.open(path, flags)
            opened = os.fstat(file_fd)
            if (
                opened.st_dev != details.st_dev
                or opened.st_ino != details.st_ino
                or opened.st_size != details.st_size
                or stat.S_IMODE(opened.st_mode)
                != stat.S_IMODE(details.st_mode)
            ):
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_RUNTIME_TREE_DRIFT"
                )
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                file_digest.update(chunk)
            finished = os.fstat(file_fd)
            if (
                finished.st_dev != opened.st_dev
                or finished.st_ino != opened.st_ino
                or finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
            ):
                raise Daily0629CertificationError(
                    "DAILY_0629_CERT_RUNTIME_TREE_DRIFT"
                )
        except Daily0629CertificationError:
            raise
        except OSError as exc:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_RUNTIME_TREE_UNAVAILABLE"
            ) from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
        total_bytes += int(details.st_size)
        digest.update(b"file\0")
        digest.update(str(details.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
        digest.update(b"\0")
    return digest.hexdigest(), file_count, total_bytes


def _file_sha256(
    path: Path,
    *,
    error_code: str = "DAILY_0629_CERT_RUNTIME_UNREADABLE",
) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(
                lambda: handle.read(1024 * 1024),
                b"",
            ):
                digest.update(chunk)
    except OSError as exc:
        raise Daily0629CertificationError(error_code) from exc
    return digest.hexdigest()


def _require_private_new_report_path(
    value: str | Path,
) -> _ReportDestination:
    path = Path(value)
    if not path.is_absolute():
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_PATH_UNSAFE"
        )
    if path.is_relative_to(_PROJECT_ROOT.resolve()):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_PATH_UNSAFE"
        )
    parent = path.parent
    try:
        if parent.resolve(strict=True) != parent:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_REPORT_ROOT_UNSAFE"
            )
        parent_stat = parent.lstat()
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_ROOT_UNAVAILABLE"
        ) from exc
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_ROOT_UNSAFE"
        )
    for ancestor in parent.parents:
        try:
            ancestor_stat = ancestor.lstat()
        except OSError as exc:
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_REPORT_ROOT_UNAVAILABLE"
            ) from exc
        if (
            stat.S_ISLNK(ancestor_stat.st_mode)
            or not stat.S_ISDIR(ancestor_stat.st_mode)
            or ancestor_stat.st_uid not in {0, os.getuid()}
        ):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_REPORT_ROOT_UNSAFE"
            )
        mode = stat.S_IMODE(ancestor_stat.st_mode)
        if (
            mode & 0o022
            and not (
                ancestor_stat.st_uid == 0
                and mode & stat.S_ISVTX
            )
        ):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_REPORT_ROOT_UNSAFE"
            )
    if os.path.lexists(path):
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_ALREADY_EXISTS"
        )
    return _ReportDestination(
        path=path,
        parent_device=parent_stat.st_dev,
        parent_inode=parent_stat.st_ino,
    )


def _write_report_atomic(
    destination: _ReportDestination,
    report: Mapping[str, object],
) -> None:
    """通过已冻结目录 fd 原子发布唯一证据，拒绝路径替换。"""
    parent = destination.path.parent
    flags = os.O_RDONLY
    for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    directory_fd: int | None = None
    temporary_name = (
        f".{destination.path.name}."
        f"{secrets.token_hex(12)}.tmp"
    )
    file_fd: int | None = None
    try:
        directory_fd = os.open(parent, flags)
        current = os.fstat(directory_fd)
        if (
            current.st_dev != destination.parent_device
            or current.st_ino != destination.parent_inode
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise Daily0629CertificationError(
                "DAILY_0629_CERT_REPORT_ROOT_DRIFT"
            )
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for name in ("O_CLOEXEC", "O_NOFOLLOW"):
            file_flags |= int(getattr(os, name, 0))
        file_fd = os.open(
            temporary_name,
            file_flags,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(file_fd, 0o600)
        payload = _canonical_json_bytes(report) + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.link(
            temporary_name,
            destination.path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Daily0629CertificationError:
        raise
    except OSError as exc:
        raise Daily0629CertificationError(
            "DAILY_0629_CERT_REPORT_WRITE_FAILED"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory_fd)


@contextmanager
def _certification_environment(
    runtime: Daily0629RuntimeIdentity,
) -> Iterator[None]:
    with tempfile.TemporaryDirectory(
        prefix="bfl-daily-0629-cert-pycache-"
    ) as pycache_root:
        os.chmod(pycache_root, 0o700)
        updates = {
            "DAILY_0629_SOURCE_CACHE_DISABLE": "1",
            "DAILY_N_JOBS": "1",
            "DAILY_BACKTEST_WORKERS": "1",
            "DAILY_0629_SOURCE_PYTHON":
                str(runtime.source_python_path),
            "PYTHONPYCACHEPREFIX": pycache_root,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": os.pathsep.join(
                (
                    str(runtime.conda_path.parent),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            ),
        }
        previous = {
            name: os.environ.get(name)
            for name in updates
        }
        try:
            os.environ.update(updates)
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one admitted daily 0629 source scheme twice without "
            "business persistence."
        )
    )
    parser.add_argument(
        "--scheme-id",
        required=True,
        choices=tuple(sorted(_ADMITTED_SPECS)),
    )
    parser.add_argument("--predict-date", required=True)
    parser.add_argument(
        "--report-path",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--authorize-real-run",
        action="store_true",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        not args.authorize_real_run
        or os.environ.get(REAL_RUN_OPT_IN_ENV) != "1"
    ):
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "code":
                        "DAILY_0629_CERT_REAL_RUN_NOT_AUTHORIZED",
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        report = certify_daily_0629_source_execution(
            scheme_id=args.scheme_id,
            predict_date=args.predict_date,
            report_path=args.report_path,
        )
    except Daily0629CertificationError as exc:
        print(
            json.dumps(
                {"status": "FAILED", "code": exc.code},
                sort_keys=True,
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "code": "DAILY_0629_CERT_INTERNAL_ERROR",
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "scheme_id": report["scheme_id"],
                "predict_date": report["predict_date"],
                "canonical_record_sha256":
                    report["canonical_record_sha256"],
                "report_path": str(args.report_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
