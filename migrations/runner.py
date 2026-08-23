from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
RELEASE_MIGRATION_MANIFEST_PATH = MIGRATIONS_DIR / "release_manifest.json"
RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION = 1


class MigrationSQLParseError(ValueError):
    """迁移 SQL 无法被安全拆分时抛出的错误。"""


class MigrationPreflightError(RuntimeError):
    """迁移开始前发现无法安全执行的 MySQL 前提。"""


class MigrationPartialApplyError(RuntimeError):
    """MySQL DDL 隐式提交后，迁移可能只完成了一部分。"""


class MigrationHistoryError(MigrationPreflightError):
    """迁移历史不连续、漂移或处于未决状态。"""


@dataclass(frozen=True)
class PreparedMigration:
    """已解析且带内容校验和的 migration 文件。"""

    version: int
    path: Path
    sha256: str
    statements: tuple[str, ...]


DAILY_LEDGER_NEW_TABLES = frozenset(
    {
        "t_input_generations",
        "t_schedule_occurrences",
        "t_schedule_items",
        "t_schedule_item_targets",
        "t_scheduler_heartbeat",
    }
)


def prepare_migration_files(
    paths: Iterable[Path],
) -> list[PreparedMigration]:
    """一次性解析 migration 清单并验证连续版本与内容摘要。"""
    prepared: list[PreparedMigration] = []
    seen_versions: set[int] = set()
    seen_names: set[str] = set()
    for raw_path in sorted(paths, key=lambda candidate: candidate.name):
        path = Path(raw_path)
        match = re.fullmatch(r"(\d{3})_[A-Za-z0-9_.-]+\.sql", path.name)
        if match is None:
            raise MigrationHistoryError(
                f"invalid migration filename: {path.name}"
            )
        version = int(match.group(1))
        if version in seen_versions or path.name in seen_names:
            raise MigrationHistoryError(
                f"duplicate migration version or filename: {path.name}"
            )
        raw_sql = path.read_bytes()
        try:
            statements = split_sql_statements(
                raw_sql.decode("utf-8")
            )
        except (UnicodeDecodeError, MigrationSQLParseError) as exc:
            raise MigrationSQLParseError(
                f"{path.name}: {exc}"
            ) from exc
        if not statements:
            raise MigrationSQLParseError(
                f"{path.name}: migration contains no statements"
            )
        prepared.append(
            PreparedMigration(
                version=version,
                path=path,
                sha256=hashlib.sha256(raw_sql).hexdigest(),
                statements=tuple(statements),
            )
        )
        seen_versions.add(version)
        seen_names.add(path.name)

    versions = [migration.version for migration in prepared]
    if versions and versions != list(range(1, max(versions) + 1)):
        raise MigrationHistoryError(
            "migration files must form a contiguous prefix starting at 001; "
            f"found {versions}"
        )
    return prepared


def validate_release_migration_manifest(
    paths: Iterable[Path],
    *,
    manifest_path: Path | None = None,
) -> list[PreparedMigration]:
    """验证 release SQL 集合与受版本控制的 checksum 清单完全一致。"""
    path = Path(manifest_path or RELEASE_MIGRATION_MANIFEST_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationHistoryError(
            f"release migration manifest cannot be read: {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "migrations",
    }:
        raise MigrationHistoryError(
            "release migration manifest must contain exactly "
            "schema_version and migrations"
        )
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION
    ):
        raise MigrationHistoryError(
            "release migration manifest schema_version mismatch: "
            f"{schema_version!r}"
        )
    raw_entries = payload["migrations"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise MigrationHistoryError(
            "release migration manifest migrations must be a nonempty list"
        )

    expected: list[tuple[int, str, str]] = []
    for offset, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "version",
            "filename",
            "sha256",
        }:
            raise MigrationHistoryError(
                "release migration manifest entry must contain exactly "
                f"version, filename and sha256: offset={offset}"
            )
        version = raw_entry["version"]
        filename = raw_entry["filename"]
        sha256 = raw_entry["sha256"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            or not isinstance(filename, str)
            or re.fullmatch(
                rf"{version:03d}_[A-Za-z0-9_.-]+\.sql",
                filename,
            )
            is None
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise MigrationHistoryError(
                "release migration manifest entry is invalid: "
                f"offset={offset}"
            )
        expected.append((version, filename, sha256))

    expected_versions = list(range(1, len(expected) + 1))
    if [version for version, _filename, _sha256 in expected] != (
        expected_versions
    ):
        raise MigrationHistoryError(
            "release migration manifest versions must be the exact "
            f"contiguous 001..{len(expected):03d} prefix"
        )
    expected_names = [filename for _version, filename, _sha256 in expected]
    raw_paths = sorted(
        (Path(raw_path) for raw_path in paths),
        key=lambda migration_path: migration_path.name,
    )
    observed_names = [migration_path.name for migration_path in raw_paths]
    if observed_names != expected_names:
        missing = sorted(set(expected_names) - set(observed_names))
        unknown = sorted(set(observed_names) - set(expected_names))
        raise MigrationHistoryError(
            "release migration manifest file set mismatch: "
            f"missing={missing} unknown={unknown}"
        )

    prepared = prepare_migration_files(raw_paths)
    observed = [
        (migration.version, migration.path.name, migration.sha256)
        for migration in prepared
    ]
    if observed != expected:
        drift = [
            {
                "version": expected_row[0],
                "filename": expected_row[1],
                "expected_sha256": expected_row[2],
                "observed_sha256": observed_row[2],
            }
            for expected_row, observed_row in zip(expected, observed)
            if expected_row != observed_row
        ]
        raise MigrationHistoryError(
            f"release migration manifest checksum drift: {drift}"
        )
    return prepared


def select_pending_migrations(
    manifest: Iterable[PreparedMigration],
    history_rows: Iterable[Mapping[str, object]],
) -> list[PreparedMigration]:
    """验证历史 checksum/连续性并只返回真正 pending 的后缀。"""
    migrations = list(manifest)
    by_version = {migration.version: migration for migration in migrations}
    history = sorted(
        history_rows,
        key=lambda row: int(row["version"]),
    )
    seen_versions: set[int] = set()
    applied_versions: list[int] = []
    for row in history:
        version = int(row["version"])
        if version in seen_versions:
            raise MigrationHistoryError(
                f"duplicate migration history version: {version:03d}"
            )
        seen_versions.add(version)
        migration = by_version.get(version)
        if migration is None:
            raise MigrationHistoryError(
                f"unknown migration history version: {version:03d}"
            )
        filename = str(row.get("filename") or "")
        if filename != migration.path.name:
            raise MigrationHistoryError(
                "migration history filename drift for "
                f"{version:03d}: {filename!r}"
            )
        checksum = str(row.get("sha256") or "").lower()
        if checksum != migration.sha256:
            raise MigrationHistoryError(
                "migration checksum drift for "
                f"{migration.path.name}: expected {migration.sha256}, "
                f"found {checksum}"
            )
        state = str(row.get("state") or "").upper()
        if state == "APPLYING":
            raise MigrationHistoryError(
                "migration history contains APPLYING row; implicit-commit "
                f"state requires inspection before retry: {filename}"
            )
        if state != "APPLIED":
            raise MigrationHistoryError(
                f"invalid migration history state {state!r}: {filename}"
            )
        applied_versions.append(version)

    expected_prefix = list(range(1, len(applied_versions) + 1))
    if applied_versions != expected_prefix:
        raise MigrationHistoryError(
            "applied migration history must be a contiguous prefix; "
            f"found {applied_versions}"
        )
    return migrations[len(applied_versions) :]


def _canonical_legacy_view_definition(
    value: object,
    *,
    database_name: str | None = None,
) -> str:
    """规范化 MySQL 对 009 view SELECT 的无语义格式重写。"""
    normalized = str(value or "").lower().replace("`", "")
    if database_name:
        normalized = re.sub(
            rf"\b{re.escape(database_name.lower())}\.",
            "",
            normalized,
        )
    normalized = re.sub(
        r"_(?:latin1|utf8mb4|utf8|binary)(?=')",
        "",
        normalized,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\s*([(),=])\s*", r"\1", normalized)
    return normalized


def expected_legacy_016_baseline() -> dict[str, object]:
    """返回无 history 的既有生产库可登记到 016 的完整终态。"""

    def column_spec(
        name: str,
        column_type: str,
        nullable: str,
        default: str | None = None,
        extra: str = "",
        comment: str = "",
    ) -> tuple[str, str, str, str | None, str, str]:
        return name, column_type, nullable, default, extra, comment

    column_groups = {
        "t_scheme_predictions": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("run_id", "bigint", "yes"),
            column_spec("scheme_version", "varchar(64)", "yes"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("target_tenor", "varchar(64)", "no"),
            column_spec("horizon", "int", "no"),
            column_spec("predict_date", "date", "no"),
            column_spec("target_date", "date", "no"),
            column_spec("feature_date", "date", "yes"),
            column_spec(
                "prediction_phase",
                "enum('gray_live','scheduled_live')",
                "yes",
            ),
            column_spec(
                "predicted_direction",
                "tinyint",
                "no",
                comment="1=涨, -1=跌, 0=平",
            ),
            column_spec("confidence", "float", "yes"),
            column_spec("model_version", "varchar(64)", "yes"),
            column_spec("extra", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_actuals": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("tenor", "varchar(64)", "no"),
            column_spec("trade_date", "date", "no"),
            column_spec("close_yield", "double", "no"),
            column_spec(
                "direction_1d",
                "tinyint",
                "yes",
                comment="1=涨, -1=跌, 0=平",
            ),
            column_spec(
                "direction_5d",
                "tinyint",
                "yes",
                comment="1=涨, -1=跌, 0=平",
            ),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_registry": (
            column_spec("id", "int", "no", extra="auto_increment"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("base_scheme_id", "varchar(64)", "no"),
            column_spec("name", "varchar(128)", "no"),
            column_spec("description", "text", "yes"),
            column_spec("horizon", "int", "no"),
            column_spec("task_type", "varchar(32)", "yes"),
            column_spec(
                "runtime_type",
                "varchar(32)",
                "no",
                "native_adapter",
            ),
            column_spec("tenors", "json", "no"),
            column_spec("frequency", "varchar(32)", "no", "daily"),
            column_spec("target_tenor", "varchar(16)", "no"),
            column_spec("schedule_cron", "varchar(64)", "no"),
            column_spec(
                "schedule_timezone",
                "varchar(64)",
                "no",
                "Asia/Shanghai",
            ),
            column_spec(
                "status",
                "enum('active','paused','archived')",
                "no",
                "active",
            ),
            column_spec("deployed_at", "date", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_run_log": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("run_id", "bigint", "yes"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("run_date", "date", "no"),
            column_spec(
                "status",
                "enum('success','failed','skipped','partial')",
                "no",
            ),
            column_spec("duration_sec", "float", "yes"),
            column_spec("error_msg", "text", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
        ),
        "t_backtest_runs": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("backtest_run_id", "bigint", "yes"),
            column_spec("benchmark_id", "varchar(128)", "no"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("data_source", "varchar(64)", "no"),
            column_spec("start_date", "date", "no"),
            column_spec("end_date", "date", "no"),
            column_spec(
                "status",
                "enum('running','success','failed','partial')",
                "no",
                "running",
            ),
            column_spec("summary", "json", "yes"),
            column_spec("report_path", "varchar(512)", "yes"),
            column_spec("code_hash", "varchar(64)", "yes"),
            column_spec("config_hash", "varchar(64)", "yes"),
            column_spec("input_artifact_hash", "varchar(64)", "yes"),
            column_spec(
                "run_mode",
                "enum('no_persist','persist','reproduction','comparison')",
                "yes",
            ),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_backtest_predictions": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("run_id", "bigint", "no"),
            column_spec("benchmark_id", "varchar(128)", "no"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("target_tenor", "varchar(64)", "no"),
            column_spec("horizon", "int", "no"),
            column_spec("predict_date", "date", "no"),
            column_spec("feature_date", "date", "yes"),
            column_spec("target_date", "date", "yes"),
            column_spec("label", "tinyint", "yes"),
            column_spec("predicted_direction", "tinyint", "yes"),
            column_spec("model_pred", "tinyint", "yes"),
            column_spec("confidence", "double", "yes"),
            column_spec("source_row", "json", "yes"),
            column_spec("extra", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_backtest_monthly_metrics": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("run_id", "bigint", "no"),
            column_spec("benchmark_id", "varchar(128)", "no"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("target_tenor", "varchar(64)", "no"),
            column_spec("horizon", "int", "no"),
            column_spec("month", "char(7)", "no"),
            column_spec("sample_count", "int", "no", "0"),
            column_spec("correct_count", "int", "no", "0"),
            column_spec("accuracy", "double", "yes"),
            column_spec("up_precision", "double", "yes"),
            column_spec("up_recall", "double", "yes"),
            column_spec("down_precision", "double", "yes"),
            column_spec("down_recall", "double", "yes"),
            column_spec("actual_dist", "json", "yes"),
            column_spec("predicted_dist", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_backtest_reproduction_checks": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("benchmark_id", "varchar(128)", "no"),
            column_spec("check_name", "varchar(128)", "no"),
            column_spec(
                "status",
                "enum('success','failed','partial')",
                "no",
            ),
            column_spec("source_path", "varchar(512)", "yes"),
            column_spec("row_count_csv", "int", "yes"),
            column_spec("row_count_db", "int", "yes"),
            column_spec("col_count_csv", "int", "yes"),
            column_spec("col_count_db", "int", "yes"),
            column_spec("date_min_csv", "date", "yes"),
            column_spec("date_max_csv", "date", "yes"),
            column_spec("date_min_db", "date", "yes"),
            column_spec("date_max_db", "date", "yes"),
            column_spec("csv_only_columns", "json", "yes"),
            column_spec("db_only_columns", "json", "yes"),
            column_spec("target_max_abs_diff", "json", "yes"),
            column_spec("overall_max_abs_diff", "double", "yes"),
            column_spec("missing_diff_count", "bigint", "yes"),
            column_spec("first_diff", "json", "yes"),
            column_spec("report", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
        ),
        "t_target_registry": (
            column_spec("target_code", "varchar(64)", "no"),
            column_spec("display_name", "varchar(128)", "no"),
            column_spec("asset_class", "varchar(64)", "no", "bond"),
            column_spec(
                "target_type",
                "varchar(64)",
                "no",
                "active_treasury",
            ),
            column_spec("sort_order", "int", "no", "0"),
            column_spec(
                "status",
                "enum('active','paused','archived')",
                "no",
                "active",
            ),
            column_spec("extra", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_weekly_actuals": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("tenor", "varchar(64)", "no"),
            column_spec("feature_week_id", "int", "no"),
            column_spec("target_week_id", "int", "no"),
            column_spec("predict_date", "date", "no"),
            column_spec("feature_date", "date", "no"),
            column_spec("target_date", "date", "no"),
            column_spec("feature_yield", "double", "no"),
            column_spec("target_yield", "double", "no"),
            column_spec(
                "direction_weekly",
                "tinyint",
                "no",
                comment=(
                    "收益率方向: 1=上行/价格空, "
                    "-1=下行/价格多, 0=平"
                ),
            ),
            column_spec(
                "price_signal",
                "varchar(8)",
                "no",
                comment="价格视角: 空/多/平",
            ),
            column_spec("target_rule", "varchar(128)", "no"),
            column_spec("extra", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_versions": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("scheme_version", "varchar(64)", "no"),
            column_spec(
                "runtime_type",
                "varchar(32)",
                "no",
                "native_adapter",
            ),
            column_spec("algorithm_version", "varchar(64)", "yes"),
            column_spec("contract_version", "varchar(32)", "yes"),
            column_spec("runtime_profile", "varchar(64)", "yes"),
            column_spec(
                "environment_fingerprint",
                "varchar(64)",
                "yes",
            ),
            column_spec("data_snapshot_id", "varchar(128)", "yes"),
            column_spec("code_hash", "varchar(64)", "no"),
            column_spec("config_hash", "varchar(64)", "yes"),
            column_spec("manifest_hash", "varchar(64)", "yes"),
            column_spec("git_commit", "varchar(64)", "yes"),
            column_spec(
                "status",
                "enum('draft','validated','shadow','active','paused',"
                "'retired')",
                "no",
                "draft",
            ),
            column_spec("created_by", "varchar(128)", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec("approved_by", "varchar(128)", "yes"),
            column_spec("approved_at", "datetime", "yes"),
        ),
        "t_harness_runs": (
            column_spec("harness_run_id", "varchar(128)", "no"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("scheme_version", "varchar(64)", "yes"),
            column_spec("stage", "varchar(64)", "no"),
            column_spec(
                "status",
                "enum('running','passed','failed','blocked','error',"
                "'skipped')",
                "no",
                "running",
            ),
            column_spec(
                "started_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec("finished_at", "datetime", "yes"),
            column_spec("triggered_by", "varchar(128)", "yes"),
            column_spec("project_root", "varchar(512)", "yes"),
            column_spec("git_commit", "varchar(64)", "yes"),
            column_spec("code_hash", "varchar(64)", "yes"),
            column_spec("config_hash", "varchar(64)", "yes"),
            column_spec("report_uri", "varchar(512)", "yes"),
        ),
        "t_harness_gate_results": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("harness_run_id", "varchar(128)", "no"),
            column_spec("gate_name", "varchar(128)", "no"),
            column_spec(
                "status",
                "enum('passed','failed','blocked','error','skipped')",
                "no",
            ),
            column_spec("started_at", "datetime", "no"),
            column_spec("finished_at", "datetime", "yes"),
            column_spec("summary_json", "json", "yes"),
            column_spec("report_uri", "varchar(512)", "yes"),
        ),
        "t_input_artifacts": (
            column_spec("artifact_id", "varchar(128)", "no"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("scheme_version", "varchar(64)", "yes"),
            column_spec("predict_date", "date", "no"),
            column_spec("frequency", "varchar(32)", "no"),
            column_spec("data_version", "varchar(64)", "yes"),
            column_spec("artifact_uri", "varchar(512)", "no"),
            column_spec("content_hash", "varchar(64)", "no"),
            column_spec("schema_hash", "varchar(64)", "no"),
            column_spec("source_watermark", "varchar(128)", "yes"),
            column_spec("row_count", "int", "yes"),
            column_spec("min_date", "date", "yes"),
            column_spec("max_date", "date", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
        ),
        "t_scheme_runs": (
            column_spec("run_id", "bigint", "no", extra="auto_increment"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("scheme_version", "varchar(64)", "yes"),
            column_spec(
                "runtime_type",
                "varchar(32)",
                "no",
                "native_adapter",
            ),
            column_spec(
                "run_type",
                "enum('dry_run','shadow','active','manual')",
                "no",
                "active",
            ),
            column_spec(
                "prediction_phase",
                "enum('gray_live','scheduled_live')",
                "yes",
            ),
            column_spec("predict_date", "date", "no"),
            column_spec(
                "status",
                "enum('running','success','failed','partial','skipped')",
                "no",
                "running",
            ),
            column_spec("harness_run_id", "varchar(128)", "yes"),
            column_spec("input_artifact_id", "varchar(128)", "yes"),
            column_spec("data_snapshot_id", "varchar(128)", "yes"),
            column_spec(
                "started_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec("finished_at", "datetime", "yes"),
            column_spec("records_expected", "int", "yes"),
            column_spec("records_returned", "int", "yes"),
            column_spec("records_written", "int", "yes"),
            column_spec("error_message", "text", "yes"),
        ),
        "t_scheme_serving_pointer": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("scheme_id", "varchar(64)", "no"),
            column_spec("target_tenor", "varchar(64)", "no"),
            column_spec("predict_date", "date", "no"),
            column_spec("serving_run_id", "bigint", "no"),
            column_spec(
                "serving_status",
                "enum('approved','hidden','deprecated')",
                "no",
                "approved",
            ),
            column_spec("updated_by", "varchar(128)", "yes"),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
        "t_scheme_monthly_actuals": (
            column_spec("id", "bigint", "no", extra="auto_increment"),
            column_spec("tenor", "varchar(64)", "no"),
            column_spec("feature_month_id", "varchar(7)", "no"),
            column_spec("target_month_id", "varchar(7)", "no"),
            column_spec("predict_date", "date", "no"),
            column_spec("feature_date", "date", "no"),
            column_spec("target_date", "date", "no"),
            column_spec("feature_yield", "double", "no"),
            column_spec("target_yield", "double", "no"),
            column_spec(
                "direction_monthly",
                "tinyint",
                "no",
                comment=(
                    "收益率方向: 1=上行/价格空, "
                    "-1=下行/价格多, 0=平"
                ),
            ),
            column_spec(
                "price_signal",
                "varchar(8)",
                "no",
                comment="价格视角: 空/多/平",
            ),
            column_spec("target_rule", "varchar(128)", "no"),
            column_spec("extra", "json", "yes"),
            column_spec(
                "created_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            column_spec(
                "updated_at",
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        ),
    }

    def is_textual(column_type: str) -> bool:
        return column_type.startswith(
            ("char(", "varchar(", "text", "enum(")
        )

    columns: dict[str, dict[str, object]] = {}
    for table_name, specs in column_groups.items():
        column_names = [spec[0] for spec in specs]
        duplicate_names = sorted(
            {
                column_name
                for column_name in column_names
                if column_names.count(column_name) > 1
            }
        )
        if duplicate_names:
            raise AssertionError(
                "legacy 016 manifest contains duplicate columns for "
                f"{table_name}: {duplicate_names}"
            )
        for ordinal, (
            name,
            column_type,
            nullable,
            default,
            extra,
            comment,
        ) in enumerate(specs, start=1):
            textual = is_textual(column_type)
            columns[f"{table_name}.{name}"] = {
                "column_type": column_type,
                "nullable": nullable,
                "default": default,
                "extra": extra,
                "comment": comment,
                "character_set": "utf8mb4" if textual else None,
                "collation": (
                    "utf8mb4_0900_ai_ci" if textual else None
                ),
                "ordinal": ordinal,
            }

    def index(
        table_name: str,
        index_name: str,
        *column_names: str,
        unique: bool,
    ) -> tuple[str, dict[str, object]]:
        return (
            f"{table_name}.{index_name}",
            {
                "unique": unique,
                "columns": tuple(column_names),
                "sub_parts": (None,) * len(column_names),
                "orders": ("a",) * len(column_names),
                "index_type": "btree",
                "visible": True,
                "expressions": (None,) * len(column_names),
            },
        )

    index_specs = (
        index("t_scheme_predictions", "PRIMARY", "id", unique=True),
        index(
            "t_scheme_predictions",
            "uk_scheme_tenor_target",
            "scheme_id",
            "target_tenor",
            "horizon",
            "target_date",
            unique=True,
        ),
        index(
            "t_scheme_predictions",
            "idx_scheme_predict_date",
            "scheme_id",
            "predict_date",
            unique=False,
        ),
        index(
            "t_scheme_predictions",
            "idx_target_date",
            "target_date",
            unique=False,
        ),
        index(
            "t_scheme_predictions",
            "idx_tenor_target_date",
            "target_tenor",
            "target_date",
            unique=False,
        ),
        index(
            "t_scheme_predictions",
            "idx_scheme_predictions_phase",
            "scheme_id",
            "prediction_phase",
            "predict_date",
            unique=False,
        ),
        index("t_scheme_actuals", "PRIMARY", "id", unique=True),
        index(
            "t_scheme_actuals",
            "uk_tenor_date",
            "tenor",
            "trade_date",
            unique=True,
        ),
        index(
            "t_scheme_actuals",
            "idx_date",
            "trade_date",
            unique=False,
        ),
        index("t_scheme_registry", "PRIMARY", "id", unique=True),
        index(
            "t_scheme_registry",
            "scheme_id",
            "scheme_id",
            unique=True,
        ),
        index(
            "t_scheme_registry",
            "idx_status",
            "status",
            unique=False,
        ),
        index(
            "t_scheme_registry",
            "idx_scheme_registry_base",
            "base_scheme_id",
            "status",
            unique=False,
        ),
        index("t_scheme_run_log", "PRIMARY", "id", unique=True),
        index(
            "t_scheme_run_log",
            "idx_scheme_run",
            "scheme_id",
            "run_date",
            unique=False,
        ),
        index(
            "t_scheme_run_log",
            "idx_created_at",
            "created_at",
            unique=False,
        ),
        index(
            "t_scheme_run_log",
            "idx_scheme_run_log_run_id",
            "run_id",
            unique=False,
        ),
        index("t_backtest_runs", "PRIMARY", "id", unique=True),
        index(
            "t_backtest_runs",
            "idx_backtest_runs_scheme",
            "scheme_id",
            "data_source",
            unique=False,
        ),
        index(
            "t_backtest_runs",
            "idx_backtest_runs_created_at",
            "created_at",
            unique=False,
        ),
        index(
            "t_backtest_runs",
            "uk_backtest_run_id",
            "backtest_run_id",
            unique=True,
        ),
        index(
            "t_backtest_runs",
            "idx_backtest_runs_latest_scope",
            "benchmark_id",
            "scheme_id",
            "data_source",
            "start_date",
            "end_date",
            "id",
            unique=False,
        ),
        index(
            "t_backtest_runs",
            "idx_backtest_runs_latest_success",
            "benchmark_id",
            "scheme_id",
            "data_source",
            "status",
            "updated_at",
            "id",
            unique=False,
        ),
        index("t_backtest_predictions", "PRIMARY", "id", unique=True),
        index(
            "t_backtest_predictions",
            "uk_backtest_prediction",
            "run_id",
            "target_tenor",
            "predict_date",
            unique=True,
        ),
        index(
            "t_backtest_predictions",
            "idx_backtest_predictions_scope",
            "scheme_id",
            "target_tenor",
            "predict_date",
            unique=False,
        ),
        index(
            "t_backtest_predictions",
            "idx_backtest_predictions_run",
            "run_id",
            unique=False,
        ),
        index(
            "t_backtest_monthly_metrics",
            "PRIMARY",
            "id",
            unique=True,
        ),
        index(
            "t_backtest_monthly_metrics",
            "uk_backtest_monthly",
            "run_id",
            "target_tenor",
            "month",
            unique=True,
        ),
        index(
            "t_backtest_monthly_metrics",
            "idx_backtest_monthly_scope",
            "scheme_id",
            "target_tenor",
            "month",
            unique=False,
        ),
        index(
            "t_backtest_reproduction_checks",
            "PRIMARY",
            "id",
            unique=True,
        ),
        index(
            "t_backtest_reproduction_checks",
            "uk_reproduction_check",
            "benchmark_id",
            "check_name",
            "created_at",
            unique=True,
        ),
        index(
            "t_backtest_reproduction_checks",
            "idx_reproduction_check_name",
            "benchmark_id",
            "check_name",
            unique=False,
        ),
        index(
            "t_backtest_reproduction_checks",
            "idx_reproduction_check_created_at",
            "created_at",
            unique=False,
        ),
        index(
            "t_target_registry",
            "PRIMARY",
            "target_code",
            unique=True,
        ),
        index(
            "t_target_registry",
            "idx_target_registry_status_sort",
            "status",
            "sort_order",
            unique=False,
        ),
        index(
            "t_scheme_weekly_actuals",
            "PRIMARY",
            "id",
            unique=True,
        ),
        index(
            "t_scheme_weekly_actuals",
            "idx_weekly_actual_target",
            "tenor",
            "target_date",
            unique=False,
        ),
        index(
            "t_scheme_weekly_actuals",
            "idx_weekly_actual_week",
            "feature_week_id",
            "target_week_id",
            unique=False,
        ),
        index(
            "t_scheme_weekly_actuals",
            "uk_weekly_actual_predict_rule",
            "tenor",
            "predict_date",
            "target_rule",
            unique=True,
        ),
        index(
            "t_scheme_weekly_actuals",
            "idx_weekly_actual_target_rule",
            "tenor",
            "target_date",
            "target_rule",
            unique=False,
        ),
        index("t_scheme_versions", "PRIMARY", "id", unique=True),
        index(
            "t_scheme_versions",
            "uk_scheme_version",
            "scheme_id",
            "scheme_version",
            unique=True,
        ),
        index(
            "t_scheme_versions",
            "idx_scheme_versions_status",
            "status",
            unique=False,
        ),
        index(
            "t_scheme_versions",
            "idx_scheme_versions_created_at",
            "created_at",
            unique=False,
        ),
        index(
            "t_harness_runs",
            "PRIMARY",
            "harness_run_id",
            unique=True,
        ),
        index(
            "t_harness_runs",
            "idx_harness_runs_scheme",
            "scheme_id",
            "started_at",
            unique=False,
        ),
        index(
            "t_harness_runs",
            "idx_harness_runs_status",
            "status",
            unique=False,
        ),
        index("t_harness_gate_results", "PRIMARY", "id", unique=True),
        index(
            "t_harness_gate_results",
            "idx_gate_results_run",
            "harness_run_id",
            unique=False,
        ),
        index(
            "t_harness_gate_results",
            "idx_gate_results_gate",
            "gate_name",
            "status",
            unique=False,
        ),
        index(
            "t_input_artifacts",
            "PRIMARY",
            "artifact_id",
            unique=True,
        ),
        index(
            "t_input_artifacts",
            "uk_input_artifact",
            "scheme_id",
            "predict_date",
            "content_hash",
            unique=True,
        ),
        index(
            "t_input_artifacts",
            "idx_input_artifacts_scheme_date",
            "scheme_id",
            "predict_date",
            unique=False,
        ),
        index(
            "t_input_artifacts",
            "idx_input_artifacts_hash",
            "content_hash",
            unique=False,
        ),
        index("t_scheme_runs", "PRIMARY", "run_id", unique=True),
        index(
            "t_scheme_runs",
            "idx_scheme_runs_scheme_date",
            "scheme_id",
            "predict_date",
            unique=False,
        ),
        index(
            "t_scheme_runs",
            "idx_scheme_runs_status",
            "status",
            unique=False,
        ),
        index(
            "t_scheme_runs",
            "idx_scheme_runs_harness",
            "harness_run_id",
            unique=False,
        ),
        index(
            "t_scheme_runs",
            "idx_scheme_runs_artifact",
            "input_artifact_id",
            unique=False,
        ),
        index(
            "t_scheme_runs",
            "idx_scheme_runs_phase",
            "scheme_id",
            "prediction_phase",
            "predict_date",
            unique=False,
        ),
        index(
            "t_scheme_serving_pointer",
            "PRIMARY",
            "id",
            unique=True,
        ),
        index(
            "t_scheme_serving_pointer",
            "uk_serving_pointer",
            "scheme_id",
            "target_tenor",
            "predict_date",
            unique=True,
        ),
        index(
            "t_scheme_serving_pointer",
            "idx_serving_pointer_run",
            "serving_run_id",
            unique=False,
        ),
        index(
            "t_scheme_serving_pointer",
            "idx_serving_pointer_status",
            "serving_status",
            unique=False,
        ),
        index(
            "t_scheme_monthly_actuals",
            "PRIMARY",
            "id",
            unique=True,
        ),
        index(
            "t_scheme_monthly_actuals",
            "uk_monthly_actual_predict_rule",
            "tenor",
            "predict_date",
            "target_rule",
            unique=True,
        ),
        index(
            "t_scheme_monthly_actuals",
            "idx_monthly_actual_target",
            "tenor",
            "target_date",
            "target_rule",
            unique=False,
        ),
        index(
            "t_scheme_monthly_actuals",
            "idx_monthly_actual_month",
            "feature_month_id",
            "target_month_id",
            unique=False,
        ),
    )

    def foreign_key(
        table_name: str,
        columns_: tuple[str, ...],
        referenced_table: str,
        referenced_columns: tuple[str, ...],
        delete_rule: str,
    ) -> dict[str, object]:
        return {
            "table": table_name,
            "columns": columns_,
            "referenced_table": referenced_table,
            "referenced_columns": referenced_columns,
            "delete_rule": delete_rule,
            "update_rule": "no action",
        }

    expected_view = _canonical_legacy_view_definition(
        """
        select ranked.id AS id,ranked.backtest_run_id AS backtest_run_id,
        ranked.benchmark_id AS benchmark_id,ranked.scheme_id AS scheme_id,
        ranked.data_source AS data_source,ranked.start_date AS start_date,
        ranked.end_date AS end_date,ranked.status AS status,
        ranked.summary AS summary,ranked.report_path AS report_path,
        ranked.code_hash AS code_hash,ranked.config_hash AS config_hash,
        ranked.input_artifact_hash AS input_artifact_hash,
        ranked.run_mode AS run_mode,ranked.created_at AS created_at,
        ranked.updated_at AS updated_at
        from (
            select r.id AS id,r.backtest_run_id AS backtest_run_id,
            r.benchmark_id AS benchmark_id,r.scheme_id AS scheme_id,
            r.data_source AS data_source,r.start_date AS start_date,
            r.end_date AS end_date,r.status AS status,
            r.summary AS summary,r.report_path AS report_path,
            r.code_hash AS code_hash,r.config_hash AS config_hash,
            r.input_artifact_hash AS input_artifact_hash,
            r.run_mode AS run_mode,r.created_at AS created_at,
            r.updated_at AS updated_at,
            row_number() OVER (
                PARTITION BY r.benchmark_id,r.scheme_id,r.data_source
                ORDER BY r.updated_at desc,r.id desc
            ) AS latest_rank
            from t_backtest_runs r
            where (r.status = 'success')
        ) ranked
        where (ranked.latest_rank = 1)
        """
    )
    tables = set(column_groups)
    return {
        "tables": tables,
        "table_definitions": {
            table_name: {
                "table_type": "base table",
                "engine": "innodb",
                "collation": "utf8mb4_0900_ai_ci",
            }
            for table_name in tables
        },
        "columns": columns,
        "indexes": dict(index_specs),
        "foreign_keys": {
            "fk_backtest_predictions_run": foreign_key(
                "t_backtest_predictions",
                ("run_id",),
                "t_backtest_runs",
                ("id",),
                "cascade",
            ),
            "fk_backtest_monthly_run": foreign_key(
                "t_backtest_monthly_metrics",
                ("run_id",),
                "t_backtest_runs",
                ("id",),
                "cascade",
            ),
            "fk_gate_results_harness_run": foreign_key(
                "t_harness_gate_results",
                ("harness_run_id",),
                "t_harness_runs",
                ("harness_run_id",),
                "cascade",
            ),
            "fk_scheme_runs_harness_run": foreign_key(
                "t_scheme_runs",
                ("harness_run_id",),
                "t_harness_runs",
                ("harness_run_id",),
                "set null",
            ),
            "fk_scheme_runs_input_artifact": foreign_key(
                "t_scheme_runs",
                ("input_artifact_id",),
                "t_input_artifacts",
                ("artifact_id",),
                "set null",
            ),
            "fk_serving_pointer_run": foreign_key(
                "t_scheme_serving_pointer",
                ("serving_run_id",),
                "t_scheme_runs",
                ("run_id",),
                "restrict",
            ),
        },
        "checks": {},
        "views": {
            "v_latest_backtest_run": {
                "definition": expected_view,
                "columns": (
                    "id",
                    "backtest_run_id",
                    "benchmark_id",
                    "scheme_id",
                    "data_source",
                    "start_date",
                    "end_date",
                    "status",
                    "summary",
                    "report_path",
                    "code_hash",
                    "config_hash",
                    "input_artifact_hash",
                    "run_mode",
                    "created_at",
                    "updated_at",
                ),
                "check_option": "none",
                "security_type": "definer",
                "is_updatable": "yes",
            }
        },
        "violations": {},
    }


def validate_legacy_016_baseline(
    state: Mapping[str, object],
) -> None:
    """校验无 migration history 的库确实处于 016 终态。"""
    expected = expected_legacy_016_baseline()
    observed_tables = set(state.get("tables", ()))
    required_tables = set(expected["tables"])
    if observed_tables != required_tables:
        raise MigrationHistoryError(
            "legacy 016 baseline tables mismatch: "
            f"missing={sorted(required_tables - observed_tables)} "
            f"unexpected={sorted(observed_tables - required_tables)}"
        )
    for category in (
        "table_definitions",
        "columns",
        "indexes",
        "foreign_keys",
        "checks",
        "views",
    ):
        observed_objects = state.get(category, {})
        if not isinstance(observed_objects, Mapping):
            raise MigrationHistoryError(
                f"legacy 016 baseline {category} is invalid"
            )
        required_objects = expected[category]
        assert isinstance(required_objects, Mapping)
        missing = set(required_objects) - set(observed_objects)
        unexpected = set(observed_objects) - set(required_objects)
        drift = {
            name: {
                "expected": definition,
                "observed": observed_objects.get(name),
            }
            for name, definition in required_objects.items()
            if name in observed_objects
            and observed_objects[name] != definition
        }
        if missing or unexpected or drift:
            raise MigrationHistoryError(
                f"legacy 016 baseline {category} mismatch: "
                f"missing={sorted(missing)} "
                f"unexpected={sorted(unexpected)} drift={drift}"
            )
    violations = state.get("violations", {})
    if not isinstance(violations, Mapping):
        raise MigrationHistoryError(
            "legacy 016 baseline violations are invalid"
        )
    failed = {
        str(name): int(count)
        for name, count in violations.items()
        if int(count) > 0
    }
    if failed:
        raise MigrationHistoryError(
            f"legacy 016 baseline data violations: {failed}"
        )


def legacy_016_data_violation_queries() -> dict[str, str]:
    """返回 003/007/011/012/013/016 数据变更的终态反例探针。"""
    target_seed_rows = """
        SELECT '1Y' AS target_code, '1Y国债活跃' AS display_name,
               10 AS sort_order
        UNION ALL
        SELECT '3Y', '3Y国债活跃', 30
        UNION ALL
        SELECT '5Y', '5Y国债活跃', 50
        UNION ALL
        SELECT '7Y', '7Y国债活跃', 70
        UNION ALL
        SELECT '10Y', '10Y国债活跃', 100
    """
    queries = {
        "backtest_run_identity_drift": (
            "COUNT(*) FROM t_backtest_runs "
            "WHERE backtest_run_id IS NULL OR backtest_run_id <> id"
        ),
        "registry_composite_identity_drift": (
            "COUNT(*) FROM t_scheme_registry "
            "WHERE base_scheme_id = '' OR target_tenor = '' "
            "OR scheme_id <> CONCAT("
            "base_scheme_id, '__h', horizon, '__', target_tenor)"
        ),
        "registry_tenors_snapshot_drift": (
            "COUNT(*) FROM t_scheme_registry "
            "WHERE JSON_TYPE(tenors) <> 'ARRAY' "
            "OR JSON_LENGTH(tenors) <> 1 "
            "OR NOT ("
            "JSON_UNQUOTE(JSON_EXTRACT(tenors, '$[0]')) "
            "<=> target_tenor)"
        ),
        "registry_deployed_at_missing": (
            "COUNT(*) FROM t_scheme_registry "
            "WHERE deployed_at IS NULL"
        ),
        "registry_task_contract_drift": (
            "COUNT(*) FROM t_scheme_registry "
            "WHERE task_type IS NULL OR task_type = '' OR NOT ("
            "(frequency = 'daily' AND horizon = 1 "
            "AND task_type = 'T+1') OR "
            "(frequency = 'daily' AND horizon = 5 "
            "AND task_type = 'T+5') OR "
            "(frequency = 'weekly' AND horizon = 6 "
            "AND task_type IN ('weekly_point','weekly_average')) OR "
            "(frequency = 'monthly' AND task_type = 'monthly'))"
        ),
        "t_scheme_registry_runtime_type_invalid": (
            "COUNT(*) FROM t_scheme_registry "
            "WHERE runtime_type IS NULL OR runtime_type = '' "
            "OR runtime_type NOT IN ('native_adapter','blackbox_v2')"
        ),
        "t_scheme_versions_runtime_type_invalid": (
            "COUNT(*) FROM t_scheme_versions "
            "WHERE runtime_type IS NULL OR runtime_type = '' "
            "OR runtime_type NOT IN ('native_adapter','blackbox_v2')"
        ),
        "t_scheme_runs_runtime_type_invalid": (
            "COUNT(*) FROM t_scheme_runs "
            "WHERE runtime_type IS NULL OR runtime_type = '' "
            "OR runtime_type NOT IN ('native_adapter','blackbox_v2')"
        ),
        "target_registry_seed_drift": (
            "COUNT(*) FROM ("
            f"{target_seed_rows}"
            ") AS target_seed "
            "LEFT JOIN t_target_registry AS actual "
            "ON actual.target_code = target_seed.target_code "
            "WHERE actual.target_code IS NULL "
            "OR actual.display_name <> target_seed.display_name "
            "OR actual.asset_class <> 'bond' "
            "OR actual.target_type <> 'active_treasury' "
            "OR actual.sort_order <> target_seed.sort_order "
            "OR actual.status <> 'active' "
            "OR COALESCE(JSON_LENGTH(actual.extra), -1) <> 1 "
            "OR NOT (JSON_UNQUOTE(JSON_EXTRACT("
            "actual.extra, '$.legacy_tenor')) "
            "<=> target_seed.target_code)"
        ),
    }
    return {
        name: " ".join(query.split())
        for name, query in queries.items()
    }


def read_legacy_016_baseline(
    connection: object,
) -> dict[str, object]:
    """读取既有库的 016 终态；结构不符时不执行任何数据探针。"""
    expected = expected_legacy_016_baseline()
    required_tables = sorted(expected["tables"])
    table_sql = ",".join(f"'{name}'" for name in required_tables)
    table_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_type = 'BASE TABLE'
              AND table_name IN ({table_sql})
            ORDER BY table_name
            """
        )
    ).mappings().all()
    tables = {
        str(row["table_name"]).lower() for row in table_rows
    }
    table_definitions = {
        str(row["table_name"]).lower(): {
            "table_type": str(row["table_type"]).lower(),
            "engine": str(row.get("engine") or "").lower(),
            "collation": str(
                row.get("table_collation") or ""
            ).lower(),
        }
        for row in table_rows
    }

    column_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra,
                   column_comment AS column_comment,
                   character_set_name AS character_set_name,
                   collation_name AS collation_name,
                   ordinal_position AS ordinal_position
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_sql})
            ORDER BY table_name, ordinal_position
            """
        )
    ).mappings().all()
    columns: dict[str, dict[str, object]] = {}
    for row in column_rows:
        identity = (
            f"{str(row['table_name']).lower()}."
            f"{str(row['column_name']).lower()}"
        )
        column_type = str(row["column_type"]).lower()
        textual = column_type.startswith(
            ("char(", "varchar(", "text", "enum(")
        )
        columns[identity] = {
            "column_type": column_type,
            "nullable": str(row["is_nullable"]).lower(),
            "default": (
                None
                if row["column_default"] is None
                else (
                    str(row["column_default"])
                    if textual
                    else str(row["column_default"]).lower()
                )
            ),
            "extra": " ".join(
                str(row.get("extra") or "").lower().split()
            ),
            "comment": str(row.get("column_comment") or ""),
            "character_set": (
                None
                if row.get("character_set_name") is None
                else str(row["character_set_name"]).lower()
            ),
            "collation": (
                None
                if row.get("collation_name") is None
                else str(row["collation_name"]).lower()
            ),
            "ordinal": int(row["ordinal_position"]),
        }

    index_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part,
                   collation AS collation,
                   index_type AS index_type,
                   is_visible AS is_visible,
                   expression AS expression
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_sql})
            ORDER BY table_name, index_name, seq_in_index
            """
        )
    ).mappings().all()
    grouped_indexes: dict[str, list[Mapping[str, object]]] = {}
    for row in index_rows:
        identity = (
            f"{str(row['table_name']).lower()}."
            f"{str(row['index_name'])}"
        )
        grouped_indexes.setdefault(identity, []).append(row)
    indexes = {
        identity: {
            "unique": int(rows[0]["non_unique"]) == 0,
            "columns": tuple(
                (
                    None
                    if row.get("column_name") is None
                    else str(row["column_name"]).lower()
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "sub_parts": tuple(
                (
                    None
                    if row.get("sub_part") is None
                    else int(row["sub_part"])
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "orders": tuple(
                str(row.get("collation") or "").lower()
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "index_type": str(
                rows[0].get("index_type") or ""
            ).lower(),
            "visible": str(
                rows[0].get("is_visible") or ""
            ).upper() == "YES",
            "expressions": tuple(
                (
                    None
                    if row.get("expression") is None
                    else str(row["expression"]).lower()
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
        }
        for identity, rows in grouped_indexes.items()
    }

    foreign_key_rows = connection.execute(
        text(
            f"""
            SELECT k.constraint_name AS constraint_name,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.ordinal_position AS ordinal_position,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.update_rule AS update_rule,
                   r.delete_rule AS delete_rule
            FROM information_schema.key_column_usage AS k
            JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.constraint_schema = DATABASE()
              AND k.table_name IN ({table_sql})
              AND k.referenced_table_name IS NOT NULL
            ORDER BY k.constraint_name, k.ordinal_position
            """
        )
    ).mappings().all()
    grouped_foreign_keys: dict[
        str,
        list[Mapping[str, object]],
    ] = {}
    for row in foreign_key_rows:
        grouped_foreign_keys.setdefault(
            str(row["constraint_name"]),
            [],
        ).append(row)
    foreign_keys: dict[str, dict[str, object]] = {}
    for name, rows in grouped_foreign_keys.items():
        ordered = sorted(
            rows,
            key=lambda item: int(item["ordinal_position"]),
        )
        foreign_keys[name] = {
            "table": str(ordered[0]["table_name"]).lower(),
            "columns": tuple(
                str(row["column_name"]).lower() for row in ordered
            ),
            "referenced_table": str(
                ordered[0]["referenced_table_name"]
            ).lower(),
            "referenced_columns": tuple(
                str(row["referenced_column_name"]).lower()
                for row in ordered
            ),
            "delete_rule": str(
                ordered[0]["delete_rule"]
            ).lower(),
            "update_rule": str(
                ordered[0]["update_rule"]
            ).lower(),
        }

    check_rows = connection.execute(
        text(
            f"""
            SELECT t.constraint_name AS constraint_name,
                   t.table_name AS table_name,
                   t.enforced AS enforced,
                   c.check_clause AS check_clause
            FROM information_schema.table_constraints AS t
            JOIN information_schema.check_constraints AS c
              ON BINARY c.constraint_schema =
                 BINARY t.constraint_schema
             AND BINARY c.constraint_name =
                 BINARY t.constraint_name
            WHERE t.constraint_schema = DATABASE()
              AND t.table_name IN ({table_sql})
              AND t.constraint_type = 'CHECK'
            ORDER BY t.constraint_name
            """
        )
    ).mappings().all()
    checks = {
        str(row["constraint_name"]): {
            "table": str(row["table_name"]).lower(),
            "clause": _canonical_check_clause(row["check_clause"]),
            "enforced": str(row["enforced"]).upper() == "YES",
        }
        for row in check_rows
    }

    database_name = str(
        connection.execute(text("SELECT DATABASE()")).scalar_one()
    )
    view_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   view_definition AS view_definition,
                   check_option AS check_option,
                   security_type AS security_type,
                   is_updatable AS is_updatable
            FROM information_schema.views
            WHERE table_schema = DATABASE()
              AND table_name = 'v_latest_backtest_run'
            """
        )
    ).mappings().all()
    view_column_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   column_name AS column_name,
                   ordinal_position AS ordinal_position
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'v_latest_backtest_run'
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    view_columns: dict[str, list[Mapping[str, object]]] = {}
    for row in view_column_rows:
        view_columns.setdefault(
            str(row["table_name"]).lower(),
            [],
        ).append(row)
    views = {
        str(row["table_name"]).lower(): {
            "definition": _canonical_legacy_view_definition(
                row["view_definition"],
                database_name=database_name,
            ),
            "columns": tuple(
                str(column["column_name"]).lower()
                for column in sorted(
                    view_columns.get(
                        str(row["table_name"]).lower(),
                        [],
                    ),
                    key=lambda item: int(item["ordinal_position"]),
                )
            ),
            "check_option": str(row["check_option"]).lower(),
            "security_type": str(row["security_type"]).lower(),
            "is_updatable": str(row["is_updatable"]).lower(),
        }
        for row in view_rows
    }
    state: dict[str, object] = {
        "tables": tables,
        "table_definitions": table_definitions,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
        "views": views,
        "violations": {},
    }
    validate_legacy_016_baseline(state)

    violations: dict[str, int] = {}

    def record(name: str, sql: str) -> None:
        count = int(
            connection.execute(
                text(f"SELECT /* {name} */ {sql}")
            ).scalar_one()
        )
        if count > 0:
            violations[name] = count

    for name, sql in legacy_016_data_violation_queries().items():
        record(name, sql)
    state["violations"] = violations
    validate_legacy_016_baseline(state)
    return state


def validate_mysql_session_contract(
    facts: Mapping[str, object],
    *,
    require_reviewed_constraint_namespace: bool = False,
) -> None:
    """校验 migration 所需的 MySQL session 安全前提。

    只有 migration 017 的约束命名空间实现依赖
    ``lower_case_table_names=2``；其余 migration 的 canonical 标识符均为
    小写，可在 MySQL 支持的 0/1/2 三种模式下安全核验与执行。
    """
    server_version = str(facts.get("server_version") or "").strip()
    version_comment = str(facts.get("version_comment") or "").strip()
    if "mariadb" in f"{server_version} {version_comment}".lower():
        raise MigrationPreflightError(
            "migration requires Oracle MySQL, not MariaDB"
        )
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", server_version)
    if match is None:
        raise MigrationPreflightError(
            f"cannot parse MySQL server version: {server_version!r}"
        )
    version = tuple(int(part) for part in match.groups())
    if version < (8, 0, 16):
        raise MigrationPreflightError(
            "migration requires MySQL >= 8.0.16 for enforced CHECK "
            f"constraints; found {server_version}"
        )

    sql_modes = {
        mode.strip().upper()
        for mode in str(facts.get("sql_mode") or "").split(",")
        if mode.strip()
    }
    has_strict_mode = bool(
        {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"} & sql_modes
    )
    required_modes = {"NO_ZERO_DATE", "NO_ZERO_IN_DATE"}
    if not has_strict_mode or not required_modes.issubset(sql_modes):
        raise MigrationPreflightError(
            "unsafe MySQL sql_mode for fail-closed migration: "
            f"{sorted(sql_modes)}"
        )

    session_timezone = str(
        facts.get("session_time_zone") or ""
    ).strip().upper()
    if session_timezone not in {"+00:00", "UTC"}:
        raise MigrationPreflightError(
            "migration MySQL session must use UTC (+00:00); "
            f"found {session_timezone!r}"
        )
    if int(facts.get("foreign_key_checks") or 0) != 1:
        raise MigrationPreflightError(
            "migration requires foreign_key_checks=1"
        )
    try:
        lower_case_table_names = int(
            facts.get("lower_case_table_names")
        )
    except (TypeError, ValueError):
        lower_case_table_names = -1
    if lower_case_table_names not in {0, 1, 2}:
        raise MigrationPreflightError(
            "migration found unsupported lower_case_table_names="
            f"{lower_case_table_names}"
        )
    if (
        require_reviewed_constraint_namespace
        and lower_case_table_names != 2
    ):
        raise MigrationPreflightError(
            "migration 017 requires lower_case_table_names=2 for the "
            "reviewed constraint namespace semantics"
        )


def preflight_migration_session(
    connection: object,
    *,
    require_reviewed_constraint_namespace: bool = False,
) -> None:
    """在任何 migration DDL 前校验连接级安全契约。"""
    if getattr(connection.dialect, "name", None) != "mysql":
        return
    facts = connection.execute(
        text(
            """
            SELECT VERSION() AS server_version,
                   @@version_comment AS version_comment,
                   @@SESSION.sql_mode AS sql_mode,
                   @@SESSION.time_zone AS session_time_zone,
                   @@SESSION.foreign_key_checks AS foreign_key_checks,
                   @@lower_case_table_names AS lower_case_table_names
            """
        )
    ).mappings().one()
    validate_mysql_session_contract(
        facts,
        require_reviewed_constraint_namespace=(
            require_reviewed_constraint_namespace
        ),
    )


def _canonical_check_clause(value: object) -> str:
    """消除 MySQL SHOW/I_S 为 CHECK 注入的非语义格式。"""
    normalized = str(value or "").lower().replace("`", "")
    normalized = normalized.replace("\\'", "'")
    normalized = re.sub(
        r"_(?:latin1|utf8mb4|utf8|binary)(?=')",
        "",
        normalized,
    )
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.replace("(", "").replace(")", "")


def expected_daily_ledger_schema_fingerprint() -> dict[str, object]:
    """返回 migration 017 完成后的关键 schema 定义。"""
    failure_codes = (
        "'transient_infra','timeout','data','contract','algorithm',"
        "'result','native_generation_unsupported',"
        "'generation_build_failed','generation_hash_mismatch',"
        "'generation_invalidated','abandoned_fence_pending_cleanup',"
        "'abandoned_orphan_cleanup','recovery_cutoff_expired',"
        "'stale_attempt','no_cross_day','invalid_item_state'"
    )

    def column(
        column_type: str,
        nullable: str,
        default: str | None = None,
        extra: str = "",
        character_set: str | None = None,
        collation: str | None = None,
    ) -> dict[str, object]:
        return {
            "column_type": column_type,
            "nullable": nullable,
            "default": default,
            "extra": extra,
            "character_set": character_set,
            "collation": collation,
        }

    def text_column(
        column_type: str,
        nullable: str,
        default: str | None = None,
        extra: str = "",
    ) -> dict[str, object]:
        return column(
            column_type,
            nullable,
            default,
            extra,
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        )

    created_at = column(
        "datetime(6)",
        "no",
        "utc_timestamp(6)",
        "default_generated",
    )
    updated_at = column(
        "datetime(6)",
        "no",
        "utc_timestamp(6)",
        "default_generated on update current_timestamp(6)",
    )
    tables = {
        table_name: {
            "table_type": "base table",
            "engine": "innodb",
            "collation": "utf8mb4_0900_ai_ci",
        }
        for table_name in (
            "t_input_generations",
            "t_schedule_occurrences",
            "t_schedule_items",
            "t_schedule_item_targets",
            "t_scheduler_heartbeat",
        )
    }
    columns = {
        "t_input_generations.generation_id": text_column(
            "varchar(128)",
            "no",
        ),
        "t_input_generations.generation_type": text_column(
            "enum('native_source','databridge_v1')",
            "no",
        ),
        "t_input_generations.business_date": column("date", "no"),
        "t_input_generations.feature_date": column("date", "no"),
        "t_input_generations.readiness_basis": text_column(
            "enum('upstream_seal','clock_contract')",
            "no",
        ),
        "t_input_generations.source_commit_token": text_column(
            "varchar(128)",
            "no",
        ),
        "t_input_generations.dataset_content_id": text_column(
            "varchar(128)",
            "no",
        ),
        "t_input_generations.schema_version": text_column(
            "varchar(64)",
            "no",
        ),
        "t_input_generations.exporter_version": text_column(
            "varchar(64)",
            "no",
        ),
        "t_input_generations.manifest_uri": text_column(
            "varchar(512)",
            "no",
        ),
        "t_input_generations.manifest_sha256": text_column(
            "char(64)",
            "no",
        ),
        "t_input_generations.native_generation_id": text_column(
            "varchar(128)",
            "yes",
        ),
        "t_input_generations.native_manifest_sha256": text_column(
            "char(64)",
            "yes",
        ),
        "t_input_generations.state": text_column(
            "enum('building','sealed','invalidated')",
            "no",
            "building",
        ),
        "t_input_generations.sealed_at": column("datetime(6)", "yes"),
        "t_input_generations.invalidated_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_input_generations.invalid_reason": text_column(
            "varchar(512)",
            "yes",
        ),
        "t_schedule_occurrences.occurrence_id": column(
            "bigint",
            "no",
            extra="auto_increment",
        ),
        "t_schedule_occurrences.schedule_key": text_column(
            "varchar(128)",
            "no",
        ),
        "t_schedule_occurrences.predict_date": column("date", "no"),
        "t_schedule_occurrences.feature_date": column("date", "no"),
        "t_schedule_occurrences.policy_version": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_occurrences.policy_sha256": text_column(
            "char(64)",
            "no",
        ),
        "t_schedule_occurrences.policy_json": column("json", "no"),
        "t_schedule_occurrences.registry_digest": text_column(
            "char(64)",
            "no",
        ),
        "t_schedule_occurrences.completion_state": text_column(
            "enum('pending','running','success','failed')",
            "no",
            "pending",
        ),
        "t_schedule_occurrences.expected_item_count": column(
            "int",
            "no",
        ),
        "t_schedule_occurrences.expected_target_count": column(
            "int",
            "no",
        ),
        "t_schedule_occurrences.accepted_target_count": column(
            "int",
            "no",
            "0",
        ),
        "t_schedule_occurrences.sla_accepted_target_count": column(
            "int",
            "yes",
        ),
        "t_schedule_occurrences.sla_deadline_at": column(
            "datetime(6)",
            "no",
        ),
        "t_schedule_occurrences.recovery_cutoff_at": column(
            "datetime(6)",
            "no",
        ),
        "t_schedule_occurrences.sla_outcome": text_column(
            "enum('pending','met','breached')",
            "no",
            "pending",
        ),
        "t_schedule_occurrences.sla_evaluated_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_schedule_occurrences.sla_reason": text_column(
            "varchar(128)",
            "yes",
        ),
        "t_schedule_occurrences.failure_code": text_column(
            "varchar(64)",
            "yes",
        ),
        "t_schedule_occurrences.failure_message": text_column(
            "text",
            "yes",
        ),
        "t_schedule_occurrences.started_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_schedule_occurrences.completed_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_schedule_items.item_id": column(
            "bigint",
            "no",
            extra="auto_increment",
        ),
        "t_schedule_items.occurrence_id": column("bigint", "no"),
        "t_schedule_items.base_scheme_id": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_items.runtime_type": text_column(
            "varchar(32)",
            "no",
        ),
        "t_schedule_items.scheme_version": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_items.code_sha256": text_column("char(64)", "no"),
        "t_schedule_items.config_sha256": text_column("char(64)", "no"),
        "t_schedule_items.cache_group": text_column(
            "varchar(128)",
            "no",
        ),
        "t_schedule_items.input_generation_id": text_column(
            "varchar(128)",
            "yes",
        ),
        "t_schedule_items.resource_class": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_items.internal_workers": column("int", "no"),
        "t_schedule_items.release_offset_minutes": column("int", "no"),
        "t_schedule_items.release_at": column("datetime(6)", "no"),
        "t_schedule_items.deadline_at": column("datetime(6)", "no"),
        "t_schedule_items.state": text_column(
            "enum('pending','running','retry_wait','success',"
            "'failed_terminal','abandoned','expired')",
            "no",
            "pending",
        ),
        "t_schedule_items.sla_status": text_column(
            "enum('pending','on_time','late')",
            "no",
            "pending",
        ),
        "t_schedule_items.late_reason": text_column(
            "varchar(128)",
            "yes",
        ),
        "t_schedule_items.sla_evaluated_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_schedule_items.attempt_no": column("int", "no", "0"),
        "t_schedule_items.current_run_id": column("bigint", "yes"),
        "t_schedule_items.started_at": column("datetime(6)", "yes"),
        "t_schedule_items.completed_at": column("datetime(6)", "yes"),
        "t_schedule_items.failure_code": text_column(
            "varchar(64)",
            "yes",
        ),
        "t_schedule_items.failure_message": text_column("text", "yes"),
        "t_schedule_item_targets.target_id": column(
            "bigint",
            "no",
            extra="auto_increment",
        ),
        "t_schedule_item_targets.occurrence_id": column("bigint", "no"),
        "t_schedule_item_targets.item_id": column("bigint", "no"),
        "t_schedule_item_targets.registry_scheme_id": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_item_targets.base_scheme_id": text_column(
            "varchar(64)",
            "no",
        ),
        "t_schedule_item_targets.runtime_type": text_column(
            "varchar(32)",
            "no",
        ),
        "t_schedule_item_targets.task_type": text_column(
            "varchar(32)",
            "no",
        ),
        "t_schedule_item_targets.target_tenor": text_column(
            "varchar(16)",
            "no",
        ),
        "t_schedule_item_targets.horizon": column("int", "no"),
        "t_schedule_item_targets.target_date": column("date", "no"),
        "t_schedule_item_targets.status": text_column(
            "enum('pending','accepted')",
            "no",
            "pending",
        ),
        "t_schedule_item_targets.accepted_run_id": column(
            "bigint",
            "yes",
        ),
        "t_schedule_item_targets.accepted_prediction_id": column(
            "bigint",
            "yes",
        ),
        "t_schedule_item_targets.accepted_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_schedule_item_targets.visible_at": column(
            "datetime(6)",
            "yes",
        ),
        "t_scheduler_heartbeat.service_name": text_column(
            "varchar(64)",
            "no",
        ),
        "t_scheduler_heartbeat.process_id": column("bigint", "no"),
        "t_scheduler_heartbeat.host_name": text_column(
            "varchar(255)",
            "no",
        ),
        "t_scheduler_heartbeat.state": text_column(
            "varchar(32)",
            "no",
        ),
        "t_scheduler_heartbeat.occurrence_id": column("bigint", "yes"),
        "t_scheduler_heartbeat.heartbeat_at": column(
            "datetime(6)",
            "no",
        ),
        "t_scheduler_heartbeat.details_json": column("json", "no"),
        "t_scheme_runs.schedule_item_id": column("bigint", "yes"),
        "t_scheme_runs.attempt_no": column("int", "yes"),
        "t_scheme_runs.trigger_origin": text_column(
            "enum('apscheduler','startup_catchup','auto_retry',"
            "'operator_recovery')",
            "yes",
        ),
        "t_scheme_runs.queued_at": column("datetime(6)", "yes"),
        "t_scheme_runs.failure_code": text_column("varchar(64)", "yes"),
        "t_scheme_runs.execution_token": text_column(
            "varchar(128)",
            "yes",
        ),
        "t_scheme_runs.process_id": column("bigint", "yes"),
        "t_scheme_runs.process_group_id": column("bigint", "yes"),
    }
    for table_name in (
        "t_input_generations",
        "t_schedule_occurrences",
        "t_schedule_items",
        "t_scheduler_heartbeat",
    ):
        columns[f"{table_name}.created_at"] = dict(created_at)
        columns[f"{table_name}.updated_at"] = dict(updated_at)
    columns["t_schedule_item_targets.created_at"] = dict(created_at)

    def index(
        *column_names: str,
        unique: bool,
    ) -> dict[str, object]:
        return {
            "unique": unique,
            "columns": tuple(column_names),
            "sub_parts": (None,) * len(column_names),
            "orders": ("a",) * len(column_names),
            "index_type": "btree",
            "visible": True,
            "expressions": (None,) * len(column_names),
        }

    def unique_index(*column_names: str) -> dict[str, object]:
        return index(*column_names, unique=True)

    def nonunique_index(*column_names: str) -> dict[str, object]:
        return index(*column_names, unique=False)

    indexes = {
        "t_input_generations.PRIMARY":
            unique_index("generation_id"),
        "t_input_generations.idx_input_generation_date_state":
            nonunique_index(
                "business_date",
                "generation_type",
                "state",
            ),
        "t_input_generations.idx_input_generation_dataset":
            nonunique_index("dataset_content_id"),
        "t_input_generations.idx_input_generation_native":
            nonunique_index("native_generation_id"),
        "t_schedule_occurrences.PRIMARY":
            unique_index("occurrence_id"),
        "t_schedule_occurrences.uk_schedule_occurrence":
            unique_index("schedule_key", "predict_date"),
        "t_schedule_occurrences.idx_schedule_occurrence_date_state":
            nonunique_index("predict_date", "completion_state"),
        "t_schedule_occurrences.idx_schedule_occurrence_sla":
            nonunique_index("sla_outcome", "sla_deadline_at"),
        "t_schedule_items.PRIMARY": unique_index("item_id"),
        "t_schedule_items.uk_schedule_item":
            unique_index("occurrence_id", "base_scheme_id"),
        "t_schedule_items.idx_schedule_item_occurrence_state":
            nonunique_index("occurrence_id", "state"),
        "t_schedule_items.idx_schedule_item_current_run":
            nonunique_index("current_run_id"),
        "t_schedule_items.idx_schedule_item_generation":
            nonunique_index("input_generation_id"),
        "t_schedule_items.idx_schedule_item_release":
            nonunique_index("state", "release_at", "deadline_at"),
        "t_schedule_item_targets.PRIMARY": unique_index("target_id"),
        "t_schedule_item_targets.uk_schedule_target_registry":
            unique_index("occurrence_id", "registry_scheme_id"),
        "t_schedule_item_targets.uk_schedule_target_item":
            unique_index("item_id", "target_tenor", "horizon"),
        "t_schedule_item_targets.idx_schedule_target_occurrence_status":
            nonunique_index("occurrence_id", "status"),
        "t_schedule_item_targets.idx_schedule_target_identity":
            nonunique_index(
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ),
        "t_schedule_item_targets.idx_schedule_target_run":
            nonunique_index("accepted_run_id"),
        "t_schedule_item_targets.idx_schedule_target_prediction":
            nonunique_index("accepted_prediction_id"),
        "t_scheduler_heartbeat.PRIMARY":
            unique_index("service_name"),
        "t_scheduler_heartbeat.idx_scheduler_heartbeat_at":
            nonunique_index("heartbeat_at"),
        "t_scheduler_heartbeat.fk_scheduler_heartbeat_occurrence":
            nonunique_index("occurrence_id"),
        "t_scheme_runs.uk_scheme_runs_schedule_attempt":
            unique_index("schedule_item_id", "attempt_no"),
        "t_scheme_runs.uk_scheme_runs_execution_token":
            unique_index("execution_token"),
    }

    def foreign_key(
        table: str,
        columns_: tuple[str, ...],
        referenced_table: str,
        referenced_columns: tuple[str, ...],
        delete_rule: str,
        update_rule: str = "no action",
    ) -> dict[str, object]:
        return {
            "table": table,
            "columns": columns_,
            "referenced_table": referenced_table,
            "referenced_columns": referenced_columns,
            "delete_rule": delete_rule,
            "update_rule": update_rule,
        }

    foreign_keys = {
        "fk_input_generation_native": foreign_key(
            "t_input_generations",
            ("native_generation_id",),
            "t_input_generations",
            ("generation_id",),
            "restrict",
        ),
        "fk_schedule_item_occurrence": foreign_key(
            "t_schedule_items",
            ("occurrence_id",),
            "t_schedule_occurrences",
            ("occurrence_id",),
            "cascade",
        ),
        "fk_schedule_item_generation": foreign_key(
            "t_schedule_items",
            ("input_generation_id",),
            "t_input_generations",
            ("generation_id",),
            "restrict",
        ),
        "fk_schedule_item_current_run": foreign_key(
            "t_schedule_items",
            ("current_run_id",),
            "t_scheme_runs",
            ("run_id",),
            "restrict",
        ),
        "fk_schedule_target_occurrence": foreign_key(
            "t_schedule_item_targets",
            ("occurrence_id",),
            "t_schedule_occurrences",
            ("occurrence_id",),
            "cascade",
        ),
        "fk_schedule_target_item": foreign_key(
            "t_schedule_item_targets",
            ("item_id",),
            "t_schedule_items",
            ("item_id",),
            "cascade",
        ),
        "fk_schedule_target_run": foreign_key(
            "t_schedule_item_targets",
            ("accepted_run_id",),
            "t_scheme_runs",
            ("run_id",),
            "restrict",
        ),
        "fk_schedule_target_prediction": foreign_key(
            "t_schedule_item_targets",
            ("accepted_prediction_id",),
            "t_scheme_predictions",
            ("id",),
            "restrict",
        ),
        "fk_scheduler_heartbeat_occurrence": foreign_key(
            "t_scheduler_heartbeat",
            ("occurrence_id",),
            "t_schedule_occurrences",
            ("occurrence_id",),
            "set null",
        ),
        "fk_scheme_run_schedule_item": foreign_key(
            "t_scheme_runs",
            ("schedule_item_id",),
            "t_schedule_items",
            ("item_id",),
            "restrict",
        ),
    }
    checks = {
        "ck_schedule_occurrence_feature_date": {
            "table": "t_schedule_occurrences",
            "clause": _canonical_check_clause(
                "feature_date < predict_date"
            ),
            "enforced": True,
        },
        "ck_schedule_occurrence_failure_code": {
            "table": "t_schedule_occurrences",
            "clause": _canonical_check_clause(
                "failure_code IS NULL OR failure_code IN ("
                f"{failure_codes})"
            ),
            "enforced": True,
        },
        "ck_schedule_item_failure_code": {
            "table": "t_schedule_items",
            "clause": _canonical_check_clause(
                "failure_code IS NULL OR failure_code IN ("
                f"{failure_codes})"
            ),
            "enforced": True,
        },
        "ck_scheme_run_schedule_failure_code": {
            "table": "t_scheme_runs",
            "clause": _canonical_check_clause(
                "schedule_item_id IS NULL OR failure_code IS NULL OR "
                f"failure_code IN ({failure_codes})"
            ),
            "enforced": True,
        },
    }
    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def validate_daily_ledger_schema_fingerprint(
    fingerprint: Mapping[str, object],
    *,
    allow_missing: bool,
    allow_nullable_transitions: bool = False,
) -> None:
    """校验 migration 017 的闭世界新表及 legacy 扩展定义。"""
    expected = expected_daily_ledger_schema_fingerprint()
    nullable_transitions = (
        daily_ledger_nullable_transition_definitions()
        if allow_nullable_transitions
        else {}
    )
    missing: list[str] = []
    drift: dict[str, dict[str, object]] = {}
    for category, expected_objects in expected.items():
        observed_objects = fingerprint.get(category, {})
        if not isinstance(observed_objects, Mapping):
            raise MigrationPreflightError(
                f"daily ledger schema fingerprint {category} is invalid"
            )
        for name, expected_definition in expected_objects.items():
            if name not in observed_objects:
                if not allow_missing:
                    missing.append(f"{category}:{name}")
                continue
            observed_definition = observed_objects[name]
            if observed_definition != expected_definition:
                if (
                    category == "columns"
                    and nullable_transitions.get(name)
                    == observed_definition
                ):
                    continue
                drift[f"{category}:{name}"] = {
                    "expected": expected_definition,
                    "observed": observed_definition,
                }

    unexpected: list[str] = []
    for category, observed_objects in fingerprint.items():
        if category not in expected:
            unexpected.append(f"{category}:*")
            continue
        if not isinstance(observed_objects, Mapping):
            continue
        expected_objects = expected[category]
        assert isinstance(expected_objects, Mapping)
        for name, definition in observed_objects.items():
            if name in expected_objects:
                continue
            if category == "tables":
                object_table = str(name)
            elif category in {"columns", "indexes"}:
                object_table = str(name).split(".", 1)[0]
            elif isinstance(definition, Mapping):
                object_table = str(definition.get("table") or "")
            else:
                object_table = ""
            if object_table in DAILY_LEDGER_NEW_TABLES:
                unexpected.append(f"{category}:{name}")
    if missing:
        raise MigrationPreflightError(
            "daily ledger schema fingerprint missing objects: "
            + ", ".join(sorted(missing))
        )
    if drift:
        raise MigrationPreflightError(
            f"daily ledger schema definition drift: {drift}"
        )
    if unexpected:
        raise MigrationPreflightError(
            "daily ledger schema has unexpected closed-world objects: "
            + ", ".join(sorted(unexpected))
        )


def daily_ledger_nullable_transition_definitions(
) -> dict[str, dict[str, object]]:
    """返回 017 SQL 中四个 ADD NULL → MODIFY NOT NULL 精确过渡态。"""
    expected_columns = (
        expected_daily_ledger_schema_fingerprint()["columns"]
    )
    transition_columns = (
        "t_schedule_occurrences.feature_date",
        "t_schedule_items.resource_class",
        "t_schedule_items.internal_workers",
        "t_schedule_items.release_offset_minutes",
    )
    transitions: dict[str, dict[str, object]] = {}
    for identity in transition_columns:
        definition = dict(expected_columns[identity])
        definition["nullable"] = "yes"
        transitions[identity] = definition
    return transitions


def read_daily_ledger_schema_fingerprint(
    connection: object,
) -> dict[str, object]:
    """从 MySQL information_schema 读取 migration 017 关键定义。"""
    table_names = (
        "'t_input_generations','t_schedule_occurrences',"
        "'t_schedule_items','t_schedule_item_targets',"
        "'t_scheduler_heartbeat','t_scheme_runs'"
    )
    table_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_names})
            ORDER BY table_name
            """
        )
    ).mappings().all()
    column_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra,
                   character_set_name AS character_set_name,
                   collation_name AS collation_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_names})
            ORDER BY table_name, ordinal_position
            """
        )
    ).mappings().all()
    index_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part,
                   collation AS collation,
                   index_type AS index_type,
                   is_visible AS is_visible,
                   expression AS expression
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_names})
            ORDER BY table_name, index_name, seq_in_index
            """
        )
    ).mappings().all()
    foreign_key_rows = connection.execute(
        text(
            f"""
            SELECT k.constraint_name AS constraint_name,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.ordinal_position AS ordinal_position,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.update_rule AS update_rule,
                   r.delete_rule AS delete_rule
            FROM information_schema.key_column_usage AS k
            JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.constraint_schema = DATABASE()
              AND k.table_name IN ({table_names})
              AND k.referenced_table_name IS NOT NULL
            ORDER BY k.constraint_name, k.ordinal_position
            """
        )
    ).mappings().all()
    check_rows = connection.execute(
        text(
            f"""
            SELECT t.constraint_name AS constraint_name,
                   t.table_name AS table_name,
                   t.enforced AS enforced,
                   c.check_clause AS check_clause
            FROM information_schema.table_constraints AS t
            JOIN information_schema.check_constraints AS c
              ON BINARY c.constraint_schema =
                 BINARY t.constraint_schema
             AND BINARY c.constraint_name =
                 BINARY t.constraint_name
            WHERE t.constraint_schema = DATABASE()
              AND t.table_name IN ({table_names})
              AND t.constraint_type = 'CHECK'
            ORDER BY t.constraint_name
            """
        )
    ).mappings().all()

    tables = {
        str(row["table_name"]).lower(): {
            "table_type": str(row["table_type"]).lower(),
            "engine": str(row.get("engine") or "").lower(),
            "collation": str(
                row.get("table_collation") or ""
            ).lower(),
        }
        for row in table_rows
    }

    columns: dict[str, dict[str, object]] = {}
    for row in column_rows:
        identity = f"{row['table_name']}.{row['column_name']}"
        columns[identity] = {
            "column_type": str(row["column_type"]).lower(),
            "nullable": str(row["is_nullable"]).lower(),
            "default": (
                None
                if row["column_default"] is None
                else str(row["column_default"]).lower()
            ),
            "extra": " ".join(
                str(row.get("extra") or "").lower().split()
            ),
            "character_set": (
                None
                if row.get("character_set_name") is None
                else str(row["character_set_name"]).lower()
            ),
            "collation": (
                None
                if row.get("collation_name") is None
                else str(row["collation_name"]).lower()
            ),
        }

    grouped_indexes: dict[str, list[Mapping[str, object]]] = {}
    for row in index_rows:
        identity = f"{row['table_name']}.{row['index_name']}"
        grouped_indexes.setdefault(identity, []).append(row)
    indexes = {
        identity: {
            "unique": int(rows[0]["non_unique"]) == 0,
            "columns": tuple(
                (
                    None
                    if row.get("column_name") is None
                    else str(row["column_name"]).lower()
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "sub_parts": tuple(
                (
                    None
                    if row.get("sub_part") is None
                    else int(row["sub_part"])
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "orders": tuple(
                str(row.get("collation") or "").lower()
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "index_type": str(
                rows[0].get("index_type") or ""
            ).lower(),
            "visible": str(rows[0].get("is_visible") or "").upper()
            == "YES",
            "expressions": tuple(
                (
                    None
                    if row.get("expression") is None
                    else str(row["expression"]).lower()
                )
                for row in sorted(
                    rows,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
        }
        for identity, rows in grouped_indexes.items()
    }

    grouped_foreign_keys: dict[
        str,
        list[Mapping[str, object]],
    ] = {}
    for row in foreign_key_rows:
        grouped_foreign_keys.setdefault(
            str(row["constraint_name"]),
            [],
        ).append(row)
    foreign_keys: dict[str, dict[str, object]] = {}
    for name, rows in grouped_foreign_keys.items():
        ordered = sorted(
            rows,
            key=lambda item: int(item["ordinal_position"]),
        )
        foreign_keys[name] = {
            "table": str(ordered[0]["table_name"]).lower(),
            "columns": tuple(
                str(row["column_name"]).lower() for row in ordered
            ),
            "referenced_table": str(
                ordered[0]["referenced_table_name"]
            ).lower(),
            "referenced_columns": tuple(
                str(row["referenced_column_name"]).lower()
                for row in ordered
            ),
            "delete_rule": str(
                ordered[0]["delete_rule"]
            ).lower(),
            "update_rule": str(
                ordered[0]["update_rule"]
            ).lower(),
        }
    checks = {
        str(row["constraint_name"]): {
            "table": str(row["table_name"]).lower(),
            "clause": _canonical_check_clause(row["check_clause"]),
            "enforced": str(row["enforced"]).upper() == "YES",
        }
        for row in check_rows
    }
    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _daily_ledger_reserved_constraint_names(
    constraint_type: str,
) -> tuple[str, ...]:
    """从 017 期望定义派生指定类型的 schema 级保留名称。"""
    expected = expected_daily_ledger_schema_fingerprint()
    category = {
        "foreign_key": "foreign_keys",
        "check": "checks",
    }.get(constraint_type)
    if category is None:
        raise ValueError(
            f"unsupported daily ledger constraint type: {constraint_type}"
        )
    definitions = expected[category]
    assert isinstance(definitions, Mapping)
    return tuple(sorted(str(name) for name in definitions))


def read_daily_ledger_schema_global_constraints(
    connection: object,
) -> tuple[dict[str, object], ...]:
    """读取 migration 017 保留名称在整个 schema 中的占用。"""
    foreign_key_names = ",".join(
        f"'{name}'"
        for name in _daily_ledger_reserved_constraint_names("foreign_key")
    )
    check_names = ",".join(
        f"'{name}'"
        for name in _daily_ledger_reserved_constraint_names("check")
    )
    foreign_key_rows = connection.execute(
        text(
            f"""
            SELECT k.constraint_name AS constraint_name,
                   k.constraint_schema AS constraint_schema,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.ordinal_position AS ordinal_position,
                   k.referenced_table_schema
                       AS referenced_table_schema,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name
                       AS referenced_column_name,
                   r.update_rule AS update_rule,
                   r.delete_rule AS delete_rule
            FROM information_schema.key_column_usage AS k
            JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.constraint_schema = DATABASE()
              AND LOWER(k.constraint_name) IN ({foreign_key_names})
              AND k.referenced_table_name IS NOT NULL
            ORDER BY k.constraint_schema,
                     k.constraint_name,
                     k.table_name,
                     k.ordinal_position
            """
        )
    ).mappings().all()
    check_rows = connection.execute(
        text(
            f"""
            SELECT t.constraint_name AS constraint_name,
                   t.constraint_schema AS constraint_schema,
                   t.table_name AS table_name,
                   t.enforced AS enforced,
                   c.check_clause AS check_clause
            FROM information_schema.table_constraints AS t
            JOIN information_schema.check_constraints AS c
              ON BINARY c.constraint_schema =
                 BINARY t.constraint_schema
             AND BINARY c.constraint_name =
                 BINARY t.constraint_name
            WHERE t.constraint_schema = DATABASE()
              AND CONVERT(t.constraint_name USING utf8mb4)
                  COLLATE utf8mb4_0900_as_ci IN ({check_names})
              AND t.constraint_type = 'CHECK'
            ORDER BY t.constraint_schema,
                     t.constraint_name,
                     t.table_name
            """
        )
    ).mappings().all()

    grouped_foreign_keys: dict[
        tuple[str, str, str],
        list[Mapping[str, object]],
    ] = {}
    for row in foreign_key_rows:
        identity = (
            str(row["constraint_schema"]),
            str(row["constraint_name"]),
            str(row["table_name"]).lower(),
        )
        grouped_foreign_keys.setdefault(identity, []).append(row)

    constraints: list[dict[str, object]] = []
    for (schema, name, table_name), rows in (
        grouped_foreign_keys.items()
    ):
        ordered = sorted(
            rows,
            key=lambda item: int(item["ordinal_position"]),
        )
        constraints.append(
            {
                "constraint_name": name,
                "constraint_type": "foreign_key",
                "schema": schema,
                "table": table_name,
                "columns": tuple(
                    str(row["column_name"]).lower()
                    for row in ordered
                ),
                "referenced_schema": str(
                    ordered[0]["referenced_table_schema"]
                ),
                "referenced_table": str(
                    ordered[0]["referenced_table_name"]
                ).lower(),
                "referenced_columns": tuple(
                    str(row["referenced_column_name"]).lower()
                    for row in ordered
                ),
                "update_rule": str(
                    ordered[0]["update_rule"]
                ).lower(),
                "delete_rule": str(
                    ordered[0]["delete_rule"]
                ).lower(),
            }
        )
    for row in check_rows:
        constraints.append(
            {
                "constraint_name": str(row["constraint_name"]),
                "constraint_type": "check",
                "schema": str(row["constraint_schema"]),
                "table": str(row["table_name"]).lower(),
                "clause": _canonical_check_clause(
                    row["check_clause"]
                ),
                "enforced": (
                    str(row["enforced"]).upper() == "YES"
                ),
            }
        )
    return tuple(
        sorted(
            constraints,
            key=lambda item: (
                str(item["constraint_name"]),
                str(item["constraint_type"]),
                str(item["schema"]),
                str(item["table"]),
            ),
        )
    )


def read_daily_ledger_upgrade_state(
    connection: object,
) -> dict[str, object]:
    """读取 migration 017 的 legacy schema/data preflight 状态。"""
    relevant_tables = (
        "t_input_generations",
        "t_schedule_occurrences",
        "t_schedule_items",
        "t_schedule_item_targets",
        "t_scheduler_heartbeat",
        "t_scheme_runs",
        "t_scheme_predictions",
    )
    table_list = ",".join(f"'{name}'" for name in relevant_tables)
    table_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_list})
            ORDER BY table_name
            """
        )
    ).mappings().all()
    existing_tables = {
        str(row["table_name"]).lower() for row in table_rows
    }
    column_rows = connection.execute(
        text(
            f"""
            SELECT table_name AS table_name,
                   column_name AS column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name IN ({table_list})
            ORDER BY table_name, ordinal_position
            """
        )
    ).mappings().all()
    columns_by_table = {
        table_name: set() for table_name in existing_tables
    }
    for row in column_rows:
        columns_by_table.setdefault(
            str(row["table_name"]).lower(),
            set(),
        ).add(str(row["column_name"]).lower())
    tables: dict[str, dict[str, object]] = {
        table_name: {
            "row_count": 0,
            "columns": columns_by_table.get(table_name, set()),
        }
        for table_name in existing_tables
    }
    for counted_table in relevant_tables:
        if counted_table in tables:
            tables[counted_table]["row_count"] = int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM `{counted_table}`")
                ).scalar_one()
            )
    violations: dict[str, int] = {}

    def record_violation(name: str, query: str) -> None:
        count = int(
            connection.execute(
                text(f"SELECT /* {name} */ COUNT(*) {query}")
            ).scalar_one()
        )
        if count > 0:
            violations[name] = count

    occurrence_columns = columns_by_table.get(
        "t_schedule_occurrences",
        set(),
    )
    if {
        "feature_date",
        "predict_date",
    }.issubset(occurrence_columns):
        record_violation(
            "occurrence_feature_date_null_or_zero",
            """
            FROM `t_schedule_occurrences`
            WHERE feature_date IS NULL
               OR CAST(feature_date AS CHAR) = '0000-00-00'
            """,
        )
        record_violation(
            "occurrence_feature_date_order",
            """
            FROM `t_schedule_occurrences`
            WHERE feature_date >= predict_date
            """,
        )

    item_columns = columns_by_table.get("t_schedule_items", set())
    for resource_column in (
        "resource_class",
        "internal_workers",
        "release_offset_minutes",
    ):
        if resource_column in item_columns:
            record_violation(
                f"item_{resource_column}_null",
                "FROM `t_schedule_items` "
                f"WHERE `{resource_column}` IS NULL",
            )

    run_columns = columns_by_table.get("t_scheme_runs", set())
    if {"schedule_item_id", "attempt_no"}.issubset(run_columns):
        record_violation(
            "duplicate_schedule_attempt",
            """
            FROM (
                SELECT schedule_item_id, attempt_no
                FROM `t_scheme_runs`
                WHERE schedule_item_id IS NOT NULL
                  AND attempt_no IS NOT NULL
                GROUP BY schedule_item_id, attempt_no
                HAVING COUNT(*) > 1
            ) AS duplicate_schedule_attempts
            """,
        )
    if "execution_token" in run_columns:
        record_violation(
            "duplicate_execution_token",
            """
            FROM (
                SELECT execution_token
                FROM `t_scheme_runs`
                WHERE execution_token IS NOT NULL
                GROUP BY execution_token
                HAVING COUNT(*) > 1
            ) AS duplicate_execution_tokens
            """,
        )

    def record_orphan(
        name: str,
        *,
        child_table: str,
        child_column: str,
        parent_table: str,
        parent_column: str,
    ) -> None:
        if (
            child_table not in existing_tables
            or parent_table not in existing_tables
            or child_column
            not in columns_by_table.get(child_table, set())
            or parent_column
            not in columns_by_table.get(parent_table, set())
        ):
            return
        record_violation(
            name,
            f"""
            FROM `{child_table}` AS child
            LEFT JOIN `{parent_table}` AS parent
              ON parent.`{parent_column}` = child.`{child_column}`
            WHERE child.`{child_column}` IS NOT NULL
              AND parent.`{parent_column}` IS NULL
            """,
        )

    orphan_relations = (
        (
            "orphan_input_native_generation",
            "t_input_generations",
            "native_generation_id",
            "t_input_generations",
            "generation_id",
        ),
        (
            "orphan_item_occurrence",
            "t_schedule_items",
            "occurrence_id",
            "t_schedule_occurrences",
            "occurrence_id",
        ),
        (
            "orphan_item_generation",
            "t_schedule_items",
            "input_generation_id",
            "t_input_generations",
            "generation_id",
        ),
        (
            "orphan_item_current_run",
            "t_schedule_items",
            "current_run_id",
            "t_scheme_runs",
            "run_id",
        ),
        (
            "orphan_target_occurrence",
            "t_schedule_item_targets",
            "occurrence_id",
            "t_schedule_occurrences",
            "occurrence_id",
        ),
        (
            "orphan_target_item",
            "t_schedule_item_targets",
            "item_id",
            "t_schedule_items",
            "item_id",
        ),
        (
            "orphan_target_run",
            "t_schedule_item_targets",
            "accepted_run_id",
            "t_scheme_runs",
            "run_id",
        ),
        (
            "orphan_target_prediction",
            "t_schedule_item_targets",
            "accepted_prediction_id",
            "t_scheme_predictions",
            "id",
        ),
        (
            "orphan_heartbeat_occurrence",
            "t_scheduler_heartbeat",
            "occurrence_id",
            "t_schedule_occurrences",
            "occurrence_id",
        ),
        (
            "orphan_schedule_item",
            "t_scheme_runs",
            "schedule_item_id",
            "t_schedule_items",
            "item_id",
        ),
    )
    for (
        name,
        child_table,
        child_column,
        parent_table,
        parent_column,
    ) in orphan_relations:
        record_orphan(
            name,
            child_table=child_table,
            child_column=child_column,
            parent_table=parent_table,
            parent_column=parent_column,
        )

    failure_codes = (
        "'TRANSIENT_INFRA','TIMEOUT','DATA','CONTRACT','ALGORITHM',"
        "'RESULT','NATIVE_GENERATION_UNSUPPORTED',"
        "'GENERATION_BUILD_FAILED','GENERATION_HASH_MISMATCH',"
        "'GENERATION_INVALIDATED','ABANDONED_FENCE_PENDING_CLEANUP',"
        "'ABANDONED_ORPHAN_CLEANUP','RECOVERY_CUTOFF_EXPIRED',"
        "'STALE_ATTEMPT','NO_CROSS_DAY','INVALID_ITEM_STATE'"
    )
    invalid_failure_queries: list[str] = []
    for table_name in (
        "t_schedule_occurrences",
        "t_schedule_items",
    ):
        if "failure_code" in columns_by_table.get(table_name, set()):
            invalid_failure_queries.append(
                f"(SELECT COUNT(*) FROM `{table_name}` "
                "WHERE failure_code IS NOT NULL "
                f"AND failure_code NOT IN ({failure_codes}))"
            )
    if {
        "failure_code",
        "schedule_item_id",
    }.issubset(run_columns):
        invalid_failure_queries.append(
            "(SELECT COUNT(*) FROM `t_scheme_runs` "
            "WHERE schedule_item_id IS NOT NULL "
            "AND failure_code IS NOT NULL "
            f"AND failure_code NOT IN ({failure_codes}))"
        )
    if invalid_failure_queries:
        invalid_failure_count = int(
            connection.execute(
                text(
                    "SELECT /* invalid_failure_code */ "
                    + " + ".join(invalid_failure_queries)
                )
            ).scalar_one()
        )
        if invalid_failure_count > 0:
            violations["invalid_failure_code"] = (
                invalid_failure_count
            )
    return {
        "tables": tables,
        "violations": violations,
        "fingerprint": read_daily_ledger_schema_fingerprint(
            connection
        ),
        "schema_global_constraints": (
            read_daily_ledger_schema_global_constraints(connection)
        ),
    }


def preflight_daily_ledger_upgrade(connection: object) -> None:
    """在 migration 017 任一 DDL 前校验 legacy schema/data。"""
    if getattr(connection.dialect, "name", None) != "mysql":
        return
    state = read_daily_ledger_upgrade_state(connection)
    validate_daily_ledger_upgrade_state(state)


def _validate_daily_ledger_constraint_namespace(
    fingerprint: Mapping[str, object],
    constraints: object,
) -> None:
    """约束全域占用必须与目标表 fingerprint 精确对应。"""
    if not isinstance(constraints, (list, tuple)):
        raise MigrationPreflightError(
            "daily ledger constraint namespace state is invalid"
        )
    foreign_keys = fingerprint.get("foreign_keys", {})
    checks = fingerprint.get("checks", {})
    if not isinstance(foreign_keys, Mapping):
        raise MigrationPreflightError(
            "daily ledger constraint namespace foreign keys are invalid"
        )
    if not isinstance(checks, Mapping):
        raise MigrationPreflightError(
            "daily ledger constraint namespace checks are invalid"
        )

    expected_names = {
        "foreign_key": set(
            _daily_ledger_reserved_constraint_names("foreign_key")
        ),
        "check": set(
            _daily_ledger_reserved_constraint_names("check")
        ),
    }
    observed_identities: set[tuple[str, str]] = set()
    for raw_constraint in constraints:
        if not isinstance(raw_constraint, Mapping):
            raise MigrationPreflightError(
                "daily ledger constraint namespace entry is invalid"
            )
        name = str(
            raw_constraint.get("constraint_name") or ""
        )
        constraint_type = str(
            raw_constraint.get("constraint_type") or ""
        )
        identity = (constraint_type, name)
        if (
            constraint_type not in expected_names
            or name not in expected_names[constraint_type]
            or identity in observed_identities
        ):
            raise MigrationPreflightError(
                "daily ledger constraint namespace has an unexpected "
                f"occupancy: {dict(raw_constraint)}"
            )
        observed_identities.add(identity)
        schema = str(raw_constraint.get("schema") or "")
        if not schema:
            raise MigrationPreflightError(
                "daily ledger constraint namespace schema is missing"
            )

        if constraint_type == "foreign_key":
            referenced_schema = str(
                raw_constraint.get("referenced_schema") or ""
            )
            raw_columns = raw_constraint.get("columns")
            raw_referenced_columns = raw_constraint.get(
                "referenced_columns"
            )
            if (
                not isinstance(raw_columns, (list, tuple))
                or not isinstance(
                    raw_referenced_columns,
                    (list, tuple),
                )
            ):
                raise MigrationPreflightError(
                    "daily ledger constraint namespace foreign key "
                    "columns are invalid"
                )
            observed_definition = {
                "table": str(raw_constraint.get("table") or ""),
                "columns": tuple(str(item) for item in raw_columns),
                "referenced_table": str(
                    raw_constraint.get("referenced_table") or ""
                ),
                "referenced_columns": tuple(
                    str(item) for item in raw_referenced_columns
                ),
                "delete_rule": str(
                    raw_constraint.get("delete_rule") or ""
                ),
                "update_rule": str(
                    raw_constraint.get("update_rule") or ""
                ),
            }
            expected_definition = foreign_keys.get(name)
            schema_is_valid = (
                bool(referenced_schema)
                and referenced_schema == schema
            )
        else:
            observed_definition = {
                "table": str(raw_constraint.get("table") or ""),
                "clause": str(raw_constraint.get("clause") or ""),
                "enforced": raw_constraint.get("enforced"),
            }
            expected_definition = checks.get(name)
            schema_is_valid = isinstance(
                raw_constraint.get("enforced"),
                bool,
            )
        if (
            not schema_is_valid
            or not isinstance(expected_definition, Mapping)
            or observed_definition != dict(expected_definition)
        ):
            raise MigrationPreflightError(
                "daily ledger constraint namespace conflicts with "
                f"migration 017: {dict(raw_constraint)}"
            )

    fingerprint_identities = {
        ("foreign_key", str(name))
        for name in foreign_keys
        if str(name) in expected_names["foreign_key"]
    } | {
        ("check", str(name))
        for name in checks
        if str(name) in expected_names["check"]
    }
    if observed_identities != fingerprint_identities:
        raise MigrationPreflightError(
            "daily ledger constraint namespace does not match the "
            "observed migration 017 fingerprint"
        )


def validate_daily_ledger_upgrade_state(
    state: Mapping[str, object],
) -> None:
    """校验从 legacy schema/data 读取的 017 升级前状态。"""
    tables = state.get("tables", {})
    violations = state.get("violations", {})
    fingerprint = state.get("fingerprint", {})
    if not isinstance(tables, Mapping):
        raise MigrationPreflightError(
            "daily ledger preflight tables are invalid"
        )
    if not isinstance(violations, Mapping):
        raise MigrationPreflightError(
            "daily ledger preflight violations are invalid"
        )
    if not isinstance(fingerprint, Mapping):
        raise MigrationPreflightError(
            "daily ledger preflight fingerprint is invalid"
        )
    validate_daily_ledger_schema_fingerprint(
        fingerprint,
        allow_missing=True,
        allow_nullable_transitions=True,
    )
    _validate_daily_ledger_constraint_namespace(
        fingerprint,
        state.get("schema_global_constraints"),
    )
    for required_table in (
        "t_scheme_runs",
        "t_scheme_predictions",
    ):
        if required_table not in tables:
            raise MigrationPreflightError(
                "migration 017 requires base table "
                f"{required_table}"
            )

    expected = expected_daily_ledger_schema_fingerprint()
    upgradable_missing = {
        "tables": set(),
        "columns": {
            "t_input_generations.native_generation_id",
            "t_input_generations.native_manifest_sha256",
            "t_schedule_occurrences.feature_date",
            "t_schedule_occurrences.sla_accepted_target_count",
            "t_schedule_occurrences.failure_code",
            "t_schedule_occurrences.failure_message",
            "t_schedule_items.resource_class",
            "t_schedule_items.internal_workers",
            "t_schedule_items.release_offset_minutes",
            "t_schedule_item_targets.visible_at",
            "t_scheme_runs.schedule_item_id",
            "t_scheme_runs.attempt_no",
            "t_scheme_runs.trigger_origin",
            "t_scheme_runs.queued_at",
            "t_scheme_runs.failure_code",
            "t_scheme_runs.execution_token",
            "t_scheme_runs.process_id",
            "t_scheme_runs.process_group_id",
        },
        "indexes": {
            "t_input_generations.idx_input_generation_native",
            "t_schedule_item_targets.idx_schedule_target_identity",
            "t_scheme_runs.uk_scheme_runs_schedule_attempt",
            "t_scheme_runs.uk_scheme_runs_execution_token",
        },
        "foreign_keys": {
            "fk_input_generation_native",
            "fk_scheme_run_schedule_item",
        },
        "checks": {
            "ck_schedule_occurrence_feature_date",
            "ck_schedule_occurrence_failure_code",
            "ck_schedule_item_failure_code",
            "ck_scheme_run_schedule_failure_code",
        },
    }
    for category, expected_objects in expected.items():
        observed_objects = fingerprint.get(category, {})
        assert isinstance(observed_objects, Mapping)
        for identity, definition in expected_objects.items():
            if identity in observed_objects:
                continue
            if category == "tables":
                object_table = identity
            elif category in {"columns", "indexes"}:
                object_table = identity.split(".", 1)[0]
            else:
                assert isinstance(definition, Mapping)
                object_table = str(definition["table"])
            if object_table not in tables:
                continue
            if identity in upgradable_missing[category]:
                continue
            raise MigrationPreflightError(
                "existing daily ledger schema is missing required "
                f"{category} object {identity}"
            )

    occurrence = tables.get("t_schedule_occurrences")
    if isinstance(occurrence, Mapping):
        occurrence_columns = set(occurrence.get("columns", ()))
        if (
            int(occurrence.get("row_count") or 0) > 0
            and "feature_date" not in occurrence_columns
        ):
            raise MigrationPreflightError(
                "nonempty t_schedule_occurrences is missing "
                "feature_date; explicit reconciliation is required"
            )

    items = tables.get("t_schedule_items")
    if isinstance(items, Mapping):
        item_columns = set(items.get("columns", ()))
        if int(items.get("row_count") or 0) > 0:
            for required_column in (
                "resource_class",
                "internal_workers",
                "release_offset_minutes",
            ):
                if required_column not in item_columns:
                    raise MigrationPreflightError(
                        "nonempty t_schedule_items is missing "
                        f"{required_column}; policy identity cannot "
                        "be guessed"
                    )

    failed_violations = {
        str(name): int(count)
        for name, count in violations.items()
        if int(count) > 0
    }
    if failed_violations:
        raise MigrationPreflightError(
            "daily ledger preflight data violations: "
            f"{failed_violations}"
        )


def _canonical_migration_state_value(value: object) -> object:
    """把 inspect 状态转为跨进程稳定的 JSON 值。"""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_migration_state_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (set, frozenset)):
        return sorted(
            _canonical_migration_state_value(item)
            for item in value
        )
    if isinstance(value, (list, tuple)):
        return [
            _canonical_migration_state_value(item)
            for item in value
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _applying_017_state_digest(state: Mapping[str, object]) -> str:
    """计算 inspect canonical safety state 的 SHA-256。"""
    payload = json.dumps(
        _canonical_migration_state_value(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_applying_017_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..016 APPLIED + 017 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= DAILY_LEDGER_MIGRATION_VERSION
    ]
    if [migration.version for migration in migrations] != list(
        range(1, DAILY_LEDGER_MIGRATION_VERSION + 1)
    ):
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..017"
        )
    target = migrations[-1]
    if (
        target.path.name != DAILY_LEDGER_MIGRATION_FILENAME
        or target.sha256 != DAILY_LEDGER_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 017 recovery identity/checksum is not the "
            "reviewed daily ledger migration"
        )

    rows = sorted(history, key=lambda row: int(row["version"]))
    versions = [int(row["version"]) for row in rows]
    if versions != list(range(1, DAILY_LEDGER_MIGRATION_VERSION + 1)):
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            f"001..017 prefix; found {versions}"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == DAILY_LEDGER_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 017 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 017 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_017_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    upgrade_state: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 017 三态检查及 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= DAILY_LEDGER_MIGRATION_VERSION
        ],
        "history": history_rows,
        "upgrade_state": dict(upgrade_state),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_017_history(
            migrations,
            history_rows,
        )
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        validate_daily_ledger_upgrade_state(upgrade_state)
        fingerprint = upgrade_state.get("fingerprint", {})
        assert isinstance(fingerprint, Mapping)
        try:
            validate_daily_ledger_schema_fingerprint(
                fingerprint,
                allow_missing=False,
            )
        except MigrationPreflightError:
            classification = "COMPATIBLE_PARTIAL"
        else:
            classification = "COMPLETE"
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _schedule_run_started_at_shape(
    row: Mapping[str, object] | None,
) -> dict[str, object]:
    """规范化 018 唯一变更列的闭世界定义。"""
    if row is None:
        return {"exists": False}
    return {
        "exists": True,
        "column_type": str(row.get("column_type") or "").lower(),
        "is_nullable": str(row.get("is_nullable") or "").lower(),
        "column_default": str(
            row.get("column_default") or ""
        ).lower(),
        "extra": str(row.get("extra") or "").lower(),
    }


def _classify_schedule_run_started_at_shape(
    shape: Mapping[str, object],
) -> str:
    """只接受 018 审核过的 source 或 target 定义。"""
    source = {
        "exists": True,
        "column_type": "datetime",
        "is_nullable": "no",
        "column_default": "current_timestamp",
        "extra": "default_generated",
    }
    target = {
        "exists": True,
        "column_type": "datetime(6)",
        "is_nullable": "yes",
        "column_default": "current_timestamp(6)",
        "extra": "default_generated",
    }
    normalized = dict(shape)
    if normalized == source:
        return "COMPATIBLE_PARTIAL"
    if normalized == target:
        return "COMPLETE"
    raise MigrationPreflightError(
        "unexpected t_scheme_runs.started_at definition: "
        f"{normalized}"
    )


def _validate_applying_018_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..017 APPLIED + 018 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
    ]
    expected_versions = list(
        range(1, SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION + 1)
    )
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..018"
        )
    target = migrations[-1]
    if (
        target.path.name != SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME
        or target.sha256 != SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 018 recovery identity/checksum is not the "
            "reviewed schedule-run timestamp migration"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            "001..018 prefix"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 018 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0)
        for row in rows
    ]
    fresh_history = [0] * 18
    legacy_bootstrap_history = [1] * 16 + [0, 0]
    if tuple(bootstrap_flags) not in {
        tuple(fresh_history),
        tuple(legacy_bootstrap_history),
    }:
        raise MigrationHistoryError(
            "migration 018 APPLYING history has an invalid baseline "
            f"bootstrap pattern: {bootstrap_flags}"
        )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 018 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_018_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    started_at_shape: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 018 两态检查及 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version
            <= SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
        ],
        "history": history_rows,
        "started_at_shape": dict(started_at_shape),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_018_history(
            migrations,
            history_rows,
        )
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = _classify_schedule_run_started_at_shape(
            started_at_shape
        )
    except (
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _error_location(sql: str, offset: int) -> str:
    """返回零偏移量对应的一基行列位置。"""
    line = sql.count("\n", 0, offset) + 1
    previous_newline = sql.rfind("\n", 0, offset)
    column = offset - previous_newline
    return f"line {line}, column {column}"


def _append_comment_separator(buffer: list[str]) -> None:
    """删除注释时保留 token 间隔，避免前后 SQL 单词粘连。"""
    if buffer and not buffer[-1].isspace():
        buffer.append(" ")


def _is_dash_comment_start(sql: str, offset: int) -> bool:
    """按 MySQL 语义识别要求后随空白的 ``--`` 行注释。"""
    if not sql.startswith("--", offset):
        return False
    following = offset + 2
    return following >= len(sql) or sql[following].isspace()


def _is_delimiter_directive(sql: str, offset: int) -> bool:
    """识别当前 statement 起点处不受支持的 DELIMITER 指令。"""
    keyword = "DELIMITER"
    if sql[offset : offset + len(keyword)].upper() != keyword:
        return False
    following = offset + len(keyword)
    return following >= len(sql) or sql[following].isspace()


def split_sql_statements(sql: str) -> list[str]:
    """安全拆分 MySQL migration statement。

    分号只有在引号、反引号和注释之外才是 statement 边界。普通注释会被
    移除，并在必要时保留 token 间隔。当前 migrations 不使用客户端
    ``DELIMITER`` 指令，因此遇到该指令或未闭合结构时直接 fail-closed。
    """
    statements: list[str] = []
    buffer: list[str] = []
    state = "normal"
    opened_at: int | None = None
    has_executable_content = False
    offset = 0

    def emit_statement() -> None:
        nonlocal has_executable_content
        statement = "".join(buffer).strip()
        if has_executable_content:
            if not statement:
                raise MigrationSQLParseError(
                    "statement contains no executable SQL"
                )
            statements.append(statement)
        buffer.clear()
        has_executable_content = False

    while offset < len(sql):
        char = sql[offset]

        if state == "line_comment":
            if char == "\n":
                buffer.append("\n")
                state = "normal"
                opened_at = None
            offset += 1
            continue

        if state == "block_comment":
            if sql.startswith("*/", offset):
                _append_comment_separator(buffer)
                state = "normal"
                opened_at = None
                offset += 2
            else:
                offset += 1
            continue

        if state in {"single_quote", "double_quote", "backtick"}:
            buffer.append(char)
            quote = {
                "single_quote": "'",
                "double_quote": '"',
                "backtick": "`",
            }[state]
            if char == "\\" and offset + 1 < len(sql):
                buffer.append(sql[offset + 1])
                offset += 2
                continue
            if char == quote:
                if offset + 1 < len(sql) and sql[offset + 1] == quote:
                    buffer.append(sql[offset + 1])
                    offset += 2
                    continue
                state = "normal"
                opened_at = None
            offset += 1
            continue

        if (
            not has_executable_content
            and char.isalpha()
            and _is_delimiter_directive(sql, offset)
        ):
            raise MigrationSQLParseError(
                "DELIMITER directives are not supported "
                f"({_error_location(sql, offset)})"
            )
        if _is_dash_comment_start(sql, offset):
            _append_comment_separator(buffer)
            state = "line_comment"
            opened_at = offset
            offset += 2
            continue
        if char == "#":
            _append_comment_separator(buffer)
            state = "line_comment"
            opened_at = offset
            offset += 1
            continue
        if sql.startswith("/*", offset):
            _append_comment_separator(buffer)
            state = "block_comment"
            opened_at = offset
            offset += 2
            continue
        if char in {"'", '"', "`"}:
            state = {
                "'": "single_quote",
                '"': "double_quote",
                "`": "backtick",
            }[char]
            opened_at = offset
            buffer.append(char)
            has_executable_content = True
            offset += 1
            continue
        if char == ";":
            emit_statement()
            offset += 1
            continue

        buffer.append(char)
        if not char.isspace():
            has_executable_content = True
        offset += 1

    if state == "line_comment":
        state = "normal"
        opened_at = None
    if state != "normal":
        assert opened_at is not None
        raise MigrationSQLParseError(
            f"unterminated {state.replace('_', ' ')} "
            f"({_error_location(sql, opened_at)})"
        )
    emit_statement()
    return statements


def _is_ddl_statement(statement: str) -> bool:
    """保守识别会触发 MySQL 隐式提交的常见 DDL。"""
    first_word = re.match(r"[A-Za-z]+", statement.lstrip())
    return bool(
        first_word
        and first_word.group(0).upper()
        in {
            "ALTER",
            "CREATE",
            "DROP",
            "RENAME",
            "TRUNCATE",
        }
    )


def _is_daily_ledger_migration(path: Path) -> bool:
    """migration 017 需要额外的数据与 schema 定义门禁。"""
    return path.name.startswith("017_")


def _is_schedule_run_started_at_migration(path: Path) -> bool:
    """migration 018 需要精确列定义 postcondition。"""
    return path.name == SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME


def _is_serving_pointer_retirement_migration(path: Path) -> bool:
    """migration 019 需要精确 serving-pointer source/target 门禁。"""
    return path.name == SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME


def _is_period_average_actuals_migration(path: Path) -> bool:
    """migration 020 只允许缺表或精确完整目标表。"""
    return path.name == PERIOD_AVERAGE_ACTUALS_MIGRATION_FILENAME


def _partial_apply_error(
    path: Path,
    *,
    execution_error: BaseException | None,
    postcondition_error: BaseException | None,
) -> MigrationPartialApplyError:
    """构造不暗示 rollback 成功的 MySQL DDL 错误。"""
    details: list[str] = []
    if execution_error is not None:
        details.append(f"execution_error={execution_error!r}")
    if postcondition_error is not None:
        details.append(
            f"postcondition_error={postcondition_error!r}"
        )
    suffix = "; ".join(details) if details else "unknown schema state"
    return MigrationPartialApplyError(
        "MySQL DDL uses implicit commit; migration "
        f"{path.name} may be partially applied and cannot be assumed "
        f"rolled back. Inspect the schema before rerunning. {suffix}"
    )


MIGRATION_HISTORY_TABLE = "t_schema_migrations"
MIGRATION_HISTORY_LOCK = "bond_factor_lab_schema_migrations"
DAILY_LEDGER_RUNNER_GUARD = "daily-ledger-017-v1"
DAILY_LEDGER_MIGRATION_VERSION = 17
DAILY_LEDGER_MIGRATION_FILENAME = "017_daily_schedule_ledger.sql"
DAILY_LEDGER_MIGRATION_SHA256 = (
    "a405ba82a857fc36252256c91fa9719e5"
    "7b63007830bd9acf734f5f8e0c743fb"
)
SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION = 18
SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME = (
    "018_schedule_run_started_at_nullable.sql"
)
SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256 = (
    "320cdf0877618330b8dbd52bb091e956"
    "987e916447cb41fc4f8d7a567b5b3ba1"
)
SCHEDULE_RUN_STARTED_AT_TARGET_DEFINITION = (
    "datetime(6) NULL DEFAULT CURRENT_TIMESTAMP(6)"
)
SERVING_POINTER_RETIREMENT_MIGRATION_VERSION = 19
SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME = (
    "019_retire_scheme_serving_pointer.sql"
)
SERVING_POINTER_RETIREMENT_MIGRATION_SHA256 = (
    "c5713935b4c33c492cac54f8cf85725b"
    "2353fc33079129b762a7ce868785b002"
)
PERIOD_AVERAGE_ACTUALS_MIGRATION_FILENAME = (
    "020_period_average_actuals.sql"
)


def _expected_period_average_actuals_schema() -> dict[str, object]:
    """返回 migration 020 新表的精确闭世界定义。"""
    return {
        "table": {
            "table_type": "base table",
            "engine": "innodb",
            "collation": "utf8mb4_0900_ai_ci",
        },
        "columns": {
            "id": ("bigint", "no", None, "auto_increment"),
            "tenor": ("varchar(64)", "no", None, ""),
            "predict_date": ("date", "no", None, ""),
            "feature_date": ("date", "no", None, ""),
            "target_date": ("date", "no", None, ""),
            "feature_yield": ("double", "no", None, ""),
            "target_yield": ("double", "no", None, ""),
            "actual_direction": ("tinyint", "no", None, ""),
            "price_signal": ("varchar(8)", "no", None, ""),
            "target_rule": ("varchar(128)", "no", None, ""),
            "extra": ("json", "yes", None, ""),
            "created_at": (
                "datetime",
                "no",
                "current_timestamp",
                "default_generated",
            ),
            "updated_at": (
                "datetime",
                "no",
                "current_timestamp",
                "default_generated on update current_timestamp",
            ),
        },
        "indexes": {
            "PRIMARY": {
                "unique": True,
                "columns": ("id",),
                "sub_parts": (None,),
            },
            "uk_period_average_actual_predict_rule": {
                "unique": True,
                "columns": ("tenor", "predict_date", "target_rule"),
                "sub_parts": (None, None, None),
            },
            "idx_period_average_actual_target": {
                "unique": False,
                "columns": ("tenor", "target_date", "target_rule"),
                "sub_parts": (None, None, None),
            },
        },
    }


def _read_period_average_actuals_schema(
    connection: object,
) -> dict[str, object]:
    """只读 migration 020 目标表定义；缺表是唯一允许的 source 状态。"""
    table_row = connection.execute(
        text(
            """
            SELECT table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_period_average_actuals'
            """
        )
    ).mappings().one_or_none()
    if table_row is None:
        return {"exists": False}
    column_rows = connection.execute(
        text(
            """
            SELECT column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_period_average_actuals'
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    index_rows = connection.execute(
        text(
            """
            SELECT index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_period_average_actuals'
            ORDER BY index_name, seq_in_index
            """
        )
    ).mappings().all()
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in index_rows:
        grouped.setdefault(str(row["index_name"]), []).append(row)
    return {
        "exists": True,
        "table": {
            "table_type": str(table_row["table_type"]).lower(),
            "engine": str(table_row.get("engine") or "").lower(),
            "collation": str(table_row.get("table_collation") or "").lower(),
        },
        "columns": {
            str(row["column_name"]).lower(): (
                str(row["column_type"]).lower(),
                str(row["is_nullable"]).lower(),
                (
                    None
                    if row["column_default"] is None
                    else str(row["column_default"]).lower()
                ),
                " ".join(str(row.get("extra") or "").lower().split()),
            )
            for row in column_rows
        },
        "indexes": {
            name: {
                "unique": int(rows[0]["non_unique"]) == 0,
                "columns": tuple(
                    str(row["column_name"]).lower()
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
                "sub_parts": tuple(
                    None if row.get("sub_part") is None else int(row["sub_part"])
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
            }
            for name, rows in grouped.items()
        },
    }


def _validate_period_average_actuals_schema(
    schema: Mapping[str, object],
    *,
    allow_missing: bool,
) -> None:
    """只接受缺表或 migration 020 完整终态，拒绝部分/漂移定义。"""
    if schema == {"exists": False}:
        if allow_missing:
            return
        raise MigrationPreflightError(
            "t_scheme_period_average_actuals is missing after migration 020"
        )
    expected = {"exists": True, **_expected_period_average_actuals_schema()}
    if dict(schema) != expected:
        raise MigrationPreflightError(
            "unexpected t_scheme_period_average_actuals definition: "
            f"{dict(schema)}"
        )


def _expected_migration_history_schema() -> dict[str, object]:
    """返回 runner 自身 history 表的闭世界定义。"""
    return {
        "table": {
            "table_type": "base table",
            "engine": "innodb",
            "collation": "utf8mb4_0900_ai_ci",
        },
        "columns": {
            "version": ("int", "no", None, ""),
            "filename": ("varchar(255)", "no", None, ""),
            "sha256": ("char(64)", "no", None, ""),
            "state": (
                "enum('applying','applied')",
                "no",
                None,
                "",
            ),
            "baseline_bootstrap": ("tinyint(1)", "no", "0", ""),
            "started_at": (
                "datetime(6)",
                "no",
                "utc_timestamp(6)",
                "default_generated",
            ),
            "applied_at": ("datetime(6)", "yes", None, ""),
        },
        "indexes": {
            "PRIMARY": {
                "unique": True,
                "columns": ("version",),
                "sub_parts": (None,),
            },
            "uk_schema_migration_filename": {
                "unique": True,
                "columns": ("filename",),
                "sub_parts": (None,),
            },
        },
    }


def _migration_history_table_exists(connection: object) -> bool:
    count = int(
        connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 't_schema_migrations'
                  AND table_type = 'BASE TABLE'
                """
            )
        ).scalar_one()
    )
    return count == 1


def _read_migration_history_schema(
    connection: object,
) -> dict[str, object]:
    table_row = connection.execute(
        text(
            """
            SELECT table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            """
        )
    ).mappings().one()
    column_rows = connection.execute(
        text(
            """
            SELECT column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    index_rows = connection.execute(
        text(
            """
            SELECT index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            ORDER BY index_name, seq_in_index
            """
        )
    ).mappings().all()
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in index_rows:
        grouped.setdefault(str(row["index_name"]), []).append(row)
    return {
        "table": {
            "table_type": str(table_row["table_type"]).lower(),
            "engine": str(table_row.get("engine") or "").lower(),
            "collation": str(
                table_row.get("table_collation") or ""
            ).lower(),
        },
        "columns": {
            str(row["column_name"]).lower(): (
                str(row["column_type"]).lower(),
                str(row["is_nullable"]).lower(),
                (
                    None
                    if row["column_default"] is None
                    else str(row["column_default"]).lower()
                ),
                " ".join(
                    str(row.get("extra") or "").lower().split()
                ),
            )
            for row in column_rows
        },
        "indexes": {
            name: {
                "unique": int(rows[0]["non_unique"]) == 0,
                "columns": tuple(
                    str(row["column_name"]).lower()
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
                "sub_parts": tuple(
                    (
                        None
                        if row.get("sub_part") is None
                        else int(row["sub_part"])
                    )
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
            }
            for name, rows in grouped.items()
        },
    }


def _validate_migration_history_schema(
    schema: Mapping[str, object],
) -> None:
    expected = _expected_migration_history_schema()
    if schema != expected:
        raise MigrationHistoryError(
            "migration history schema definition drift: "
            f"expected={expected} observed={dict(schema)}"
        )


def _create_migration_history_table(connection: object) -> None:
    preflight_migration_session(connection)
    connection.execute(
        text(
            """
            CREATE TABLE t_schema_migrations (
                version INT NOT NULL,
                filename VARCHAR(255) NOT NULL,
                sha256 CHAR(64) NOT NULL,
                state ENUM('APPLYING','APPLIED') NOT NULL,
                baseline_bootstrap TINYINT(1) NOT NULL DEFAULT 0,
                started_at DATETIME(6) NOT NULL
                    DEFAULT (UTC_TIMESTAMP(6)),
                applied_at DATETIME(6) DEFAULT NULL,
                PRIMARY KEY (version),
                UNIQUE KEY uk_schema_migration_filename (filename)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            """
        )
    )


def _read_migration_history(
    connection: object,
) -> list[Mapping[str, object]]:
    return list(
        connection.execute(
            text(
                """
                SELECT version AS version,
                       filename AS filename,
                       sha256 AS sha256,
                       state AS state,
                       baseline_bootstrap AS baseline_bootstrap
                FROM t_schema_migrations
                ORDER BY version
                """
            )
        ).mappings().all()
    )


@contextmanager
def _migration_owner_connection(engine: object):
    """持有进程无关的 MySQL migration owner lock。"""
    with engine.connect() as connection:
        if getattr(connection.dialect, "name", None) != "mysql":
            raise MigrationHistoryError(
                "APPLYING migration inspection/recovery requires MySQL"
            )
        preflight_migration_session(connection)
        acquired = int(
            connection.execute(
                text("SELECT GET_LOCK(:lock_name, 5)"),
                {"lock_name": MIGRATION_HISTORY_LOCK},
            ).scalar_one()
            or 0
        )
        if acquired != 1:
            raise MigrationHistoryError(
                "could not acquire migration owner lock"
            )
        try:
            yield connection
        finally:
            connection.execute(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": MIGRATION_HISTORY_LOCK},
            )


def _unsafe_applying_017_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    database_identity: Mapping[str, object],
    reason: BaseException,
) -> dict[str, object]:
    """在 history/schema 无法完整读取时仍返回稳定 UNSAFE 结果。"""
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in manifest
            if migration.version <= DAILY_LEDGER_MIGRATION_VERSION
        ],
        "inspection_error": {
            "type": type(reason).__name__,
            "message": str(reason),
        },
    }
    return {
        "classification": "UNSAFE",
        "state_digest": _applying_017_state_digest(canonical_state),
        "database_identity": dict(database_identity),
        "migration": None,
        "reason": str(reason),
    }


def _read_applying_017_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 017 状态。"""
    identity_row = connection.execute(
        text(
            """
            SELECT DATABASE() AS database_name,
                   @@server_uuid AS server_uuid
            """
        )
    ).mappings().one()
    database_identity = {
        "database_name": str(identity_row["database_name"] or ""),
        "server_uuid": str(identity_row["server_uuid"] or ""),
    }
    try:
        if not _migration_history_table_exists(connection):
            raise MigrationHistoryError(
                "migration history table does not exist"
            )
        _validate_migration_history_schema(
            _read_migration_history_schema(connection)
        )
        history = _read_migration_history(connection)
        upgrade_state = read_daily_ledger_upgrade_state(connection)
        return build_applying_017_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            upgrade_state=upgrade_state,
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_017_inspection(
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_017(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查，不执行 DDL/DML。"""
    manifest = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        return _read_applying_017_inspection(
            owner_connection,
            manifest,
        )


def _daily_ledger_recovery_target(
    manifest: Iterable[PreparedMigration],
) -> PreparedMigration:
    """从受校验 manifest 中取得唯一允许恢复的 migration 017。"""
    candidates = [
        migration
        for migration in manifest
        if migration.version == DAILY_LEDGER_MIGRATION_VERSION
    ]
    if len(candidates) != 1:
        raise MigrationHistoryError(
            "recovery requires exactly one migration 017 file"
        )
    target = candidates[0]
    if (
        target.path.name != DAILY_LEDGER_MIGRATION_FILENAME
        or target.sha256 != DAILY_LEDGER_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "recovery target is not the reviewed migration 017"
        )
    return target


def recover_applying_migration_017(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 017。"""
    if re.fullmatch(r"[0-9a-f]{64}", expected_state_digest) is None:
        raise MigrationHistoryError(
            "expected state digest must be 64 lowercase hex characters"
        )
    manifest = validate_release_migration_manifest(paths)
    target = _daily_ledger_recovery_target(manifest)
    with _migration_owner_connection(engine) as owner_connection:
        initial = _read_applying_017_inspection(
            owner_connection,
            manifest,
        )
        observed_digest = str(initial.get("state_digest") or "")
        if not hmac.compare_digest(
            observed_digest,
            expected_state_digest,
        ):
            raise MigrationHistoryError(
                "APPLYING 017 state digest changed; run a new read-only "
                "inspection before recovery"
            )
        classification = str(
            initial.get("classification") or ""
        )
        if classification == "UNSAFE":
            raise MigrationHistoryError(
                "APPLYING 017 recovery refused unsafe state: "
                f"{initial.get('reason')}"
            )
        if classification == "COMPATIBLE_PARTIAL":
            rollback = getattr(owner_connection, "rollback", None)
            if callable(rollback):
                rollback()
            _execute_prepared_migration_files(
                engine,
                [(target.path, target.statements)],
            )
            if callable(rollback):
                rollback()
            completed = _read_applying_017_inspection(
                owner_connection,
                manifest,
            )
            if completed.get("classification") != "COMPLETE":
                raise MigrationPartialApplyError(
                    "migration 017 replay did not reach COMPLETE; "
                    "history remains APPLYING. "
                    f"inspection={completed}"
                )
        elif classification != "COMPLETE":
            raise MigrationHistoryError(
                "unknown APPLYING 017 inspection classification: "
                f"{classification!r}"
            )
        _mark_migration_applied(engine, target)
        return {
            "recovery_outcome": "APPLIED",
            "initial_classification": classification,
            "initial_state_digest": observed_digest,
            "migration": {
                "version": target.version,
                "filename": target.path.name,
                "sha256": target.sha256,
            },
        }


def _read_schedule_run_started_at_shape(
    connection: object,
) -> dict[str, object]:
    """从 information_schema 读取 018 唯一变更列。"""
    rows = connection.execute(
        text(
            """
            SELECT column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_runs'
              AND column_name = 'started_at'
            """
        )
    ).mappings().all()
    if len(rows) > 1:
        raise MigrationHistoryError(
            "duplicate t_scheme_runs.started_at metadata rows"
        )
    return _schedule_run_started_at_shape(
        dict(rows[0]) if rows else None
    )


def _schedule_run_started_at_definition(
    shape: Mapping[str, object],
) -> str:
    """把归一化列形态渲染成运维可读的 DDL 片段。"""
    if not shape.get("exists"):
        return "<column is missing>"
    nullable = (
        "NULL"
        if str(shape.get("is_nullable") or "") == "yes"
        else "NOT NULL"
    )
    default = str(shape.get("column_default") or "").upper()
    definition = f"{shape.get('column_type')} {nullable}"
    if default:
        definition = f"{definition} DEFAULT {default}"
    return definition


def preflight_schedule_run_started_at_nullable(
    engine: object,
) -> None:
    """ledger 启动前只读确认 018 已应用，否则立即 fail-closed。

    ledger claim 会显式写 ``t_scheme_runs.started_at = NULL``；017 形态下
    该写入要到当天日批中段才失败，因此启动即拒绝。
    """
    with engine.connect() as connection:
        shape = _read_schedule_run_started_at_shape(connection)
        try:
            complete = (
                _classify_schedule_run_started_at_shape(shape)
                == "COMPLETE"
            )
        except MigrationPreflightError:
            complete = False
        if complete:
            return
    raise MigrationPreflightError(
        "t_scheme_runs.started_at is not migration "
        f"{SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION} shape; the ledger "
        "claim path writes started_at = NULL and would fail mid-run. "
        f"current={_schedule_run_started_at_definition(shape)}; "
        f"expected={SCHEDULE_RUN_STARTED_AT_TARGET_DEFINITION}; "
        "apply it first with: python scripts/apply_migrations.py --apply "
        "--expected-database-name <database-name> "
        "--expected-server-uuid <server-uuid>; obtain the expected identity "
        "from a controlled read-only inspect or identity query"
    )


def _unsafe_applying_018_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    database_identity: Mapping[str, object],
    reason: BaseException,
) -> dict[str, object]:
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in manifest
            if migration.version
            <= SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
        ],
        "inspection_error": {
            "type": type(reason).__name__,
            "message": str(reason),
        },
    }
    return {
        "classification": "UNSAFE",
        "state_digest": _applying_017_state_digest(canonical_state),
        "database_identity": dict(database_identity),
        "migration": None,
        "reason": str(reason),
    }


def _read_applying_018_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 018 状态。"""
    identity_row = connection.execute(
        text(
            """
            SELECT DATABASE() AS database_name,
                   @@server_uuid AS server_uuid
            """
        )
    ).mappings().one()
    database_identity = {
        "database_name": str(identity_row["database_name"] or ""),
        "server_uuid": str(identity_row["server_uuid"] or ""),
    }
    try:
        if not _migration_history_table_exists(connection):
            raise MigrationHistoryError(
                "migration history table does not exist"
            )
        _validate_migration_history_schema(
            _read_migration_history_schema(connection)
        )
        return build_applying_018_inspection(
            manifest=manifest,
            history=_read_migration_history(connection),
            database_identity=database_identity,
            started_at_shape=_read_schedule_run_started_at_shape(
                connection
            ),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_018_inspection(
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_018(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 018，不执行 DDL/DML。"""
    manifest = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        return _read_applying_018_inspection(
            owner_connection,
            manifest,
        )


def _schedule_run_started_at_recovery_target(
    manifest: Iterable[PreparedMigration],
) -> PreparedMigration:
    candidates = [
        migration
        for migration in manifest
        if migration.version
        == SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
    ]
    if len(candidates) != 1:
        raise MigrationHistoryError(
            "recovery requires exactly one migration 018 file"
        )
    target = candidates[0]
    if (
        target.path.name != SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME
        or target.sha256 != SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "recovery target is not the reviewed migration 018"
        )
    return target


def recover_applying_migration_018(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 018。"""
    if re.fullmatch(r"[0-9a-f]{64}", expected_state_digest) is None:
        raise MigrationHistoryError(
            "expected state digest must be 64 lowercase hex characters"
        )
    manifest = validate_release_migration_manifest(paths)
    target = _schedule_run_started_at_recovery_target(manifest)
    with _migration_owner_connection(engine) as owner_connection:
        initial = _read_applying_018_inspection(
            owner_connection,
            manifest,
        )
        observed_digest = str(initial.get("state_digest") or "")
        if not hmac.compare_digest(
            observed_digest,
            expected_state_digest,
        ):
            raise MigrationHistoryError(
                "APPLYING 018 state digest changed; run a new read-only "
                "inspection before recovery"
            )
        classification = str(initial.get("classification") or "")
        if classification == "UNSAFE":
            raise MigrationHistoryError(
                "APPLYING 018 recovery refused unsafe state: "
                f"{initial.get('reason')}"
            )
        if classification == "COMPATIBLE_PARTIAL":
            rollback = getattr(owner_connection, "rollback", None)
            if callable(rollback):
                rollback()
            _execute_prepared_migration_files(
                engine,
                [(target.path, target.statements)],
            )
            if callable(rollback):
                rollback()
            completed = _read_applying_018_inspection(
                owner_connection,
                manifest,
            )
            if completed.get("classification") != "COMPLETE":
                raise MigrationPartialApplyError(
                    "migration 018 replay did not reach COMPLETE; "
                    "history remains APPLYING. "
                    f"inspection={completed}"
                )
        elif classification != "COMPLETE":
            raise MigrationHistoryError(
                "unknown APPLYING 018 inspection classification: "
                f"{classification!r}"
            )
        _mark_migration_applied(engine, target)
        return {
            "recovery_outcome": "APPLIED",
            "initial_classification": classification,
            "initial_state_digest": observed_digest,
            "migration": {
                "version": target.version,
                "filename": target.path.name,
                "sha256": target.sha256,
            },
        }


def _expected_serving_pointer_retirement_state() -> dict[str, object]:
    """返回 019 可重放前唯一允许存在的 serving-pointer 定义。"""
    expected = expected_legacy_016_baseline()
    table_name = "t_scheme_serving_pointer"
    prefix = f"{table_name}."
    table_definitions = expected["table_definitions"]
    columns = expected["columns"]
    indexes = expected["indexes"]
    foreign_keys = expected["foreign_keys"]
    checks = expected["checks"]
    assert isinstance(table_definitions, Mapping)
    assert isinstance(columns, Mapping)
    assert isinstance(indexes, Mapping)
    assert isinstance(foreign_keys, Mapping)
    assert isinstance(checks, Mapping)
    return {
        "exists": True,
        "table_definition": dict(table_definitions[table_name]),
        "columns": {
            name: dict(spec)
            for name, spec in columns.items()
            if str(name).startswith(prefix)
        },
        "indexes": {
            name: dict(spec)
            for name, spec in indexes.items()
            if str(name).startswith(prefix)
        },
        "foreign_keys": {
            name: dict(spec)
            for name, spec in foreign_keys.items()
            if str(spec.get("table") if isinstance(spec, Mapping) else "")
            == table_name
        },
        "checks": {
            name: dict(spec)
            for name, spec in checks.items()
            if str(spec.get("table") if isinstance(spec, Mapping) else "")
            == table_name
        },
        "inbound_foreign_keys": {},
        "dependent_objects": {
            "views": (),
            "triggers": (),
            "routines": (),
            "events": (),
        },
        "schedule_run_started_at_shape": {
            "exists": True,
            "column_type": "datetime(6)",
            "is_nullable": "yes",
            "column_default": "current_timestamp(6)",
            "extra": "default_generated",
        },
    }


def _expected_serving_pointer_retirement_target_state() -> dict[str, object]:
    """返回 019 已删表后仍必须保持的闭世界 target。"""
    source = _expected_serving_pointer_retirement_state()
    return {
        "exists": False,
        "dependent_objects": dict(source["dependent_objects"]),
        "schedule_run_started_at_shape": dict(
            source["schedule_run_started_at_shape"]
        ),
    }


def _normalize_serving_pointer_column(
    row: Mapping[str, object],
) -> dict[str, object]:
    column_type = str(row["column_type"]).lower()
    textual = column_type.startswith(("char(", "varchar(", "text", "enum("))
    return {
        "column_type": column_type,
        "nullable": str(row["is_nullable"]).lower(),
        "default": (
            None
            if row["column_default"] is None
            else (
                str(row["column_default"])
                if textual
                else str(row["column_default"]).lower()
            )
        ),
        "extra": " ".join(str(row.get("extra") or "").lower().split()),
        "comment": str(row.get("column_comment") or ""),
        "character_set": (
            None
            if row.get("character_set_name") is None
            else str(row["character_set_name"]).lower()
        ),
        "collation": (
            None
            if row.get("collation_name") is None
            else str(row["collation_name"]).lower()
        ),
        "ordinal": int(row["ordinal_position"]),
    }


def _normalize_serving_pointer_indexes(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        identity = (
            f"{str(row['table_name']).lower()}."
            f"{str(row['index_name'])}"
        )
        grouped.setdefault(identity, []).append(row)
    return {
        identity: {
            "unique": int(group[0]["non_unique"]) == 0,
            "columns": tuple(
                None
                if row.get("column_name") is None
                else str(row["column_name"]).lower()
                for row in sorted(
                    group,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "sub_parts": tuple(
                None
                if row.get("sub_part") is None
                else int(row["sub_part"])
                for row in sorted(
                    group,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "orders": tuple(
                str(row.get("collation") or "").lower()
                for row in sorted(
                    group,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
            "index_type": str(group[0].get("index_type") or "").lower(),
            "visible": str(group[0].get("is_visible") or "").upper()
            == "YES",
            "expressions": tuple(
                None
                if row.get("expression") is None
                else str(row["expression"]).lower()
                for row in sorted(
                    group,
                    key=lambda item: int(item["seq_in_index"]),
                )
            ),
        }
        for identity, group in grouped.items()
    }


def _normalize_serving_pointer_foreign_keys(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["constraint_name"]), []).append(row)
    return {
        name: {
            "table": str(group[0]["table_name"]).lower(),
            "columns": tuple(
                str(row["column_name"]).lower()
                for row in sorted(
                    group,
                    key=lambda item: int(item["ordinal_position"]),
                )
            ),
            "referenced_table": str(
                group[0]["referenced_table_name"]
            ).lower(),
            "referenced_columns": tuple(
                str(row["referenced_column_name"]).lower()
                for row in sorted(
                    group,
                    key=lambda item: int(item["ordinal_position"]),
                )
            ),
            "delete_rule": str(group[0]["delete_rule"]).lower(),
            "update_rule": str(group[0]["update_rule"]).lower(),
        }
        for name, group in grouped.items()
    }


def _read_serving_pointer_dependent_objects(
    connection: object,
) -> dict[str, tuple[str, ...]]:
    """只读枚举会使 019 DROP TABLE 不安全的外部对象。"""
    view_rows = connection.execute(
        text(
            """
            SELECT table_schema AS object_schema,
                   table_name AS object_name
            FROM information_schema.views
            WHERE view_definition IS NULL
               OR LOWER(view_definition) LIKE '%t_scheme_serving_pointer%'
            ORDER BY table_schema, table_name
            """
        )
    ).mappings().all()
    trigger_rows = connection.execute(
        text(
            """
            SELECT trigger_schema AS object_schema,
                   trigger_name AS object_name
            FROM information_schema.triggers
            WHERE (
                event_object_schema = DATABASE()
                AND LOWER(event_object_table) = 't_scheme_serving_pointer'
            )
               OR action_statement IS NULL
               OR LOWER(action_statement) LIKE '%t_scheme_serving_pointer%'
            ORDER BY trigger_schema, trigger_name
            """
        )
    ).mappings().all()
    routine_rows = connection.execute(
        text(
            """
            SELECT routine_schema AS object_schema,
                   routine_type AS routine_type,
                   routine_name AS routine_name
            FROM information_schema.routines
            WHERE routine_definition IS NULL
               OR LOWER(routine_definition) LIKE '%t_scheme_serving_pointer%'
            ORDER BY routine_schema, routine_type, routine_name
            """
        )
    ).mappings().all()
    event_rows = connection.execute(
        text(
            """
            SELECT event_schema AS object_schema,
                   event_name AS object_name
            FROM information_schema.events
            WHERE event_definition IS NULL
               OR LOWER(event_definition) LIKE '%t_scheme_serving_pointer%'
            ORDER BY event_schema, event_name
            """
        )
    ).mappings().all()
    return {
        "views": tuple(
            sorted(
                f"{str(row['object_schema']).lower()}."
                f"{str(row['object_name']).lower()}"
                for row in view_rows
            )
        ),
        "triggers": tuple(
            sorted(
                f"{str(row['object_schema']).lower()}."
                f"{str(row['object_name']).lower()}"
                for row in trigger_rows
            )
        ),
        "routines": tuple(
            sorted(
                f"{str(row['object_schema']).lower()}."
                f"{str(row['routine_type']).lower()}:"
                f"{str(row['routine_name']).lower()}"
                for row in routine_rows
            )
        ),
        "events": tuple(
            sorted(
                f"{str(row['object_schema']).lower()}."
                f"{str(row['object_name']).lower()}"
                for row in event_rows
            )
        ),
    }


def _read_serving_pointer_retirement_state(
    connection: object,
) -> dict[str, object]:
    """读取 019 物理删表前的闭世界 source 或已删 target。"""
    table_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_serving_pointer'
            """
        )
    ).mappings().all()
    if not table_rows:
        return {
            "exists": False,
            "dependent_objects": _read_serving_pointer_dependent_objects(
                connection
            ),
            "schedule_run_started_at_shape": (
                _read_schedule_run_started_at_shape(connection)
            ),
        }
    if len(table_rows) != 1:
        raise MigrationHistoryError(
            "duplicate t_scheme_serving_pointer table metadata rows"
        )
    table_row = table_rows[0]
    if str(table_row["table_name"]).lower() != "t_scheme_serving_pointer":
        raise MigrationHistoryError("unexpected serving-pointer table name")

    column_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra,
                   column_comment AS column_comment,
                   character_set_name AS character_set_name,
                   collation_name AS collation_name,
                   ordinal_position AS ordinal_position
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_serving_pointer'
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    columns = {
        f"{str(row['table_name']).lower()}."
        f"{str(row['column_name']).lower()}": _normalize_serving_pointer_column(
            row
        )
        for row in column_rows
    }

    index_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part,
                   collation AS collation,
                   index_type AS index_type,
                   is_visible AS is_visible,
                   expression AS expression
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_serving_pointer'
            ORDER BY index_name, seq_in_index
            """
        )
    ).mappings().all()

    outgoing_fk_rows = connection.execute(
        text(
            """
            SELECT k.constraint_name AS constraint_name,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.ordinal_position AS ordinal_position,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.update_rule AS update_rule,
                   r.delete_rule AS delete_rule
            FROM information_schema.key_column_usage AS k
            JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.constraint_schema = DATABASE()
              AND k.table_name = 't_scheme_serving_pointer'
              AND k.referenced_table_name IS NOT NULL
            ORDER BY k.constraint_name, k.ordinal_position
            """
        )
    ).mappings().all()
    inbound_fk_rows = connection.execute(
        text(
            """
            SELECT k.constraint_name AS constraint_name,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.ordinal_position AS ordinal_position,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.update_rule AS update_rule,
                   r.delete_rule AS delete_rule
            FROM information_schema.key_column_usage AS k
            JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.referenced_table_schema = DATABASE()
              AND k.referenced_table_name = 't_scheme_serving_pointer'
            ORDER BY k.constraint_name, k.ordinal_position
            """
        )
    ).mappings().all()
    check_rows = connection.execute(
        text(
            """
            SELECT t.constraint_name AS constraint_name,
                   t.table_name AS table_name,
                   t.enforced AS enforced,
                   c.check_clause AS check_clause
            FROM information_schema.table_constraints AS t
            JOIN information_schema.check_constraints AS c
              ON BINARY c.constraint_schema = BINARY t.constraint_schema
             AND BINARY c.constraint_name = BINARY t.constraint_name
            WHERE t.constraint_schema = DATABASE()
              AND t.table_name = 't_scheme_serving_pointer'
              AND t.constraint_type = 'CHECK'
            ORDER BY t.constraint_name
            """
        )
    ).mappings().all()
    row_count = int(
        connection.execute(
            text("SELECT COUNT(*) FROM t_scheme_serving_pointer")
        ).scalar_one()
    )
    return {
        "exists": True,
        "table_definition": {
            "table_type": str(table_row["table_type"]).lower(),
            "engine": str(table_row.get("engine") or "").lower(),
            "collation": str(
                table_row.get("table_collation") or ""
            ).lower(),
        },
        "columns": columns,
        "indexes": _normalize_serving_pointer_indexes(index_rows),
        "foreign_keys": _normalize_serving_pointer_foreign_keys(
            outgoing_fk_rows
        ),
        "checks": {
            str(row["constraint_name"]): {
                "table": str(row["table_name"]).lower(),
                "clause": _canonical_check_clause(row["check_clause"]),
                "enforced": str(row["enforced"]).upper() == "YES",
            }
            for row in check_rows
        },
        "inbound_foreign_keys": _normalize_serving_pointer_foreign_keys(
            inbound_fk_rows
        ),
        "dependent_objects": _read_serving_pointer_dependent_objects(
            connection
        ),
        "schedule_run_started_at_shape": (
            _read_schedule_run_started_at_shape(connection)
        ),
        "row_count": row_count,
    }


def _classify_serving_pointer_retirement_state(
    state: Mapping[str, object],
) -> str:
    """019 只接受完整旧表或已删除表，拒绝全部中间/漂移状态。"""
    normalized = dict(state)
    expected_target = _expected_serving_pointer_retirement_target_state()
    if normalized == expected_target:
        return "COMPLETE"
    if normalized.get("exists") is False:
        raise MigrationPreflightError(
            "unexpected completed t_scheme_serving_pointer retirement "
            f"state: {normalized}"
        )
    row_count = normalized.pop("row_count", None)
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise MigrationPreflightError(
            "t_scheme_serving_pointer retirement row_count is invalid"
        )
    if row_count < 0:
        raise MigrationPreflightError(
            "t_scheme_serving_pointer retirement row_count is negative"
        )
    if normalized.get("inbound_foreign_keys"):
        raise MigrationPreflightError(
            "t_scheme_serving_pointer has unexpected inbound foreign keys"
        )
    expected = _expected_serving_pointer_retirement_state()
    dependent_objects = normalized.get("dependent_objects")
    if not isinstance(dependent_objects, Mapping):
        raise MigrationPreflightError(
            "t_scheme_serving_pointer dependent objects state is invalid"
        )
    if dict(dependent_objects) != expected["dependent_objects"]:
        raise MigrationPreflightError(
            "t_scheme_serving_pointer has unexpected dependent objects"
        )
    started_at_shape = normalized.get("schedule_run_started_at_shape")
    if not isinstance(started_at_shape, Mapping):
        raise MigrationPreflightError(
            "t_scheme_runs.started_at state is invalid before migration 019"
        )
    if _classify_schedule_run_started_at_shape(started_at_shape) != "COMPLETE":
        raise MigrationPreflightError(
            "t_scheme_runs.started_at must be the migration 018 target "
            "before migration 019"
        )
    if normalized != expected:
        raise MigrationPreflightError(
            "unexpected t_scheme_serving_pointer retirement definition: "
            f"{normalized}"
        )
    return "COMPATIBLE_PARTIAL"


def _validate_applying_019_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..018 APPLIED + 019 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
    ]
    expected_versions = list(
        range(1, SERVING_POINTER_RETIREMENT_MIGRATION_VERSION + 1)
    )
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..019"
        )
    target = migrations[-1]
    if (
        target.path.name != SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME
        or target.sha256 != SERVING_POINTER_RETIREMENT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 019 recovery identity/checksum is not the "
            "reviewed serving-pointer retirement migration"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            "001..019 prefix"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 019 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0) for row in rows
    ]
    fresh_history = [0] * SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
    legacy_bootstrap_history = [1] * 16 + [0, 0, 0]
    if tuple(bootstrap_flags) not in {
        tuple(fresh_history),
        tuple(legacy_bootstrap_history),
    }:
        raise MigrationHistoryError(
            "migration 019 APPLYING history has an invalid baseline "
            f"bootstrap pattern: {bootstrap_flags}"
        )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 019 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_019_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    pointer_state: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 019 source/target/unsafe 检查及 digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
        ],
        "history": history_rows,
        "pointer_state": dict(pointer_state),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_019_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = _classify_serving_pointer_retirement_state(
            pointer_state
        )
    except (
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _unsafe_applying_019_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    database_identity: Mapping[str, object],
    reason: BaseException,
) -> dict[str, object]:
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in manifest
            if migration.version <= SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
        ],
        "inspection_error": {
            "type": type(reason).__name__,
            "message": str(reason),
        },
    }
    return {
        "classification": "UNSAFE",
        "state_digest": _applying_017_state_digest(canonical_state),
        "database_identity": dict(database_identity),
        "migration": None,
        "reason": str(reason),
    }


def _read_applying_019_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 019 状态。"""
    identity_row = connection.execute(
        text(
            """
            SELECT DATABASE() AS database_name,
                   @@server_uuid AS server_uuid
            """
        )
    ).mappings().one()
    database_identity = {
        "database_name": str(identity_row["database_name"] or ""),
        "server_uuid": str(identity_row["server_uuid"] or ""),
    }
    try:
        if not _migration_history_table_exists(connection):
            raise MigrationHistoryError(
                "migration history table does not exist"
            )
        _validate_migration_history_schema(
            _read_migration_history_schema(connection)
        )
        return build_applying_019_inspection(
            manifest=manifest,
            history=_read_migration_history(connection),
            database_identity=database_identity,
            pointer_state=_read_serving_pointer_retirement_state(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_019_inspection(
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_019(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 019，不执行 DDL/DML。"""
    manifest = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        return _read_applying_019_inspection(owner_connection, manifest)


def _serving_pointer_retirement_recovery_target(
    manifest: Iterable[PreparedMigration],
) -> PreparedMigration:
    candidates = [
        migration
        for migration in manifest
        if migration.version == SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
    ]
    if len(candidates) != 1:
        raise MigrationHistoryError(
            "recovery requires exactly one migration 019 file"
        )
    target = candidates[0]
    if (
        target.path.name != SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME
        or target.sha256 != SERVING_POINTER_RETIREMENT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "recovery target is not the reviewed migration 019"
        )
    return target


def recover_applying_migration_019(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 019。"""
    if re.fullmatch(r"[0-9a-f]{64}", expected_state_digest) is None:
        raise MigrationHistoryError(
            "expected state digest must be 64 lowercase hex characters"
        )
    manifest = validate_release_migration_manifest(paths)
    target = _serving_pointer_retirement_recovery_target(manifest)
    with _migration_owner_connection(engine) as owner_connection:
        initial = _read_applying_019_inspection(owner_connection, manifest)
        observed_digest = str(initial.get("state_digest") or "")
        if not hmac.compare_digest(observed_digest, expected_state_digest):
            raise MigrationHistoryError(
                "APPLYING 019 state digest changed; run a new read-only "
                "inspection before recovery"
            )
        classification = str(initial.get("classification") or "")
        if classification == "UNSAFE":
            raise MigrationHistoryError(
                "APPLYING 019 recovery refused unsafe state: "
                f"{initial.get('reason')}"
            )
        if classification == "COMPATIBLE_PARTIAL":
            rollback = getattr(owner_connection, "rollback", None)
            if callable(rollback):
                rollback()
            _execute_prepared_migration_files(
                engine,
                [(target.path, target.statements)],
            )
            if callable(rollback):
                rollback()
            completed = _read_applying_019_inspection(
                owner_connection,
                manifest,
            )
            if completed.get("classification") != "COMPLETE":
                raise MigrationPartialApplyError(
                    "migration 019 replay did not reach COMPLETE; "
                    "history remains APPLYING. "
                    f"inspection={completed}"
                )
        elif classification != "COMPLETE":
            raise MigrationHistoryError(
                "unknown APPLYING 019 inspection classification: "
                f"{classification!r}"
            )
        _mark_migration_applied(engine, target)
        return {
            "recovery_outcome": "APPLIED",
            "initial_classification": classification,
            "initial_state_digest": observed_digest,
            "migration": {
                "version": target.version,
                "filename": target.path.name,
                "sha256": target.sha256,
            },
        }


def _legacy_domain_table_count(connection: object) -> int:
    required = sorted(expected_legacy_016_baseline()["tables"])
    table_sql = ",".join(f"'{name}'" for name in required)
    return int(
        connection.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_type = 'BASE TABLE'
                  AND table_name IN ({table_sql})
                """
            )
        ).scalar_one()
    )


def _bootstrap_legacy_migration_history(
    engine: object,
    manifest: list[PreparedMigration],
) -> list[Mapping[str, object]]:
    baseline = [
        migration for migration in manifest if migration.version <= 16
    ]
    if [migration.version for migration in baseline] != list(
        range(1, 17)
    ):
        raise MigrationHistoryError(
            "legacy baseline bootstrap requires exact migrations 001..016"
        )
    with engine.begin() as connection:
        preflight_migration_session(connection)
        for migration in baseline:
            connection.execute(
                text(
                    """
                    INSERT INTO t_schema_migrations (
                        version,
                        filename,
                        sha256,
                        state,
                        baseline_bootstrap,
                        started_at,
                        applied_at
                    ) VALUES (
                        :version,
                        :filename,
                        :sha256,
                        'APPLIED',
                        1,
                        UTC_TIMESTAMP(6),
                        UTC_TIMESTAMP(6)
                    )
                    """
                ),
                {
                    "version": migration.version,
                    "filename": migration.path.name,
                    "sha256": migration.sha256,
                },
            )
    return [
        {
            "version": migration.version,
            "filename": migration.path.name,
            "sha256": migration.sha256,
            "state": "APPLIED",
            "baseline_bootstrap": 1,
        }
        for migration in baseline
    ]


def _load_or_bootstrap_migration_history(
    engine: object,
    owner_connection: object,
    manifest: list[PreparedMigration],
) -> list[Mapping[str, object]]:
    history_exists = _migration_history_table_exists(owner_connection)
    history: list[Mapping[str, object]] = []
    if history_exists:
        _validate_migration_history_schema(
            _read_migration_history_schema(owner_connection)
        )
        history = _read_migration_history(owner_connection)
        if history:
            return history

    domain_table_count = _legacy_domain_table_count(owner_connection)
    if domain_table_count > 0:
        read_legacy_016_baseline(owner_connection)
        if not history_exists:
            _create_migration_history_table(owner_connection)
            _validate_migration_history_schema(
                _read_migration_history_schema(owner_connection)
            )
        return _bootstrap_legacy_migration_history(engine, manifest)

    if not history_exists:
        _create_migration_history_table(owner_connection)
        _validate_migration_history_schema(
            _read_migration_history_schema(owner_connection)
        )
    return []


def _mark_migration_applying(
    engine: object,
    migration: PreparedMigration,
) -> None:
    with engine.begin() as connection:
        preflight_migration_session(connection)
        connection.execute(
            text(
                """
                INSERT INTO t_schema_migrations (
                    version,
                    filename,
                    sha256,
                    state,
                    baseline_bootstrap,
                    started_at,
                    applied_at
                ) VALUES (
                    :version,
                    :filename,
                    :sha256,
                    'APPLYING',
                    0,
                    UTC_TIMESTAMP(6),
                    NULL
                )
                """
            ),
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            },
        )


def _mark_migration_applied(
    engine: object,
    migration: PreparedMigration,
) -> None:
    with engine.begin() as connection:
        preflight_migration_session(connection)
        result = connection.execute(
            text(
                """
                UPDATE t_schema_migrations
                SET state = 'APPLIED',
                    applied_at = UTC_TIMESTAMP(6)
                WHERE version = :version
                  AND filename = :filename
                  AND sha256 = :sha256
                  AND state = 'APPLYING'
                """
            ),
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            },
        )
        if int(result.rowcount) != 1:
            raise MigrationHistoryError(
                "migration APPLYING completion fence rejected: "
                f"{migration.path.name}"
            )


def _execute_prepared_migration_files(
    engine: object,
    prepared: Iterable[tuple[Path, Iterable[str]]],
) -> None:
    """执行已解析文件；只由 history runner 或 parser 测试入口调用。"""
    for path, raw_statements in prepared:
        statements = list(raw_statements)
        daily_ledger = _is_daily_ledger_migration(path)
        schedule_run_started_at = (
            _is_schedule_run_started_at_migration(path)
        )
        serving_pointer_retirement = (
            _is_serving_pointer_retirement_migration(path)
        )
        period_average_actuals = _is_period_average_actuals_migration(path)
        mysql_session = False
        execution_started = False
        ddl_attempted = False
        execution_error: BaseException | None = None
        try:
            with engine.begin() as connection:
                mysql_session = (
                    getattr(connection.dialect, "name", None)
                    == "mysql"
                )
                preflight_migration_session(
                    connection,
                    require_reviewed_constraint_namespace=daily_ledger,
                )
                if daily_ledger and mysql_session:
                    preflight_daily_ledger_upgrade(connection)
                    connection.execute(
                        text(
                            "SET @bond_factor_lab_migration_runner_017 = "
                            ":runner_guard"
                        ),
                        {"runner_guard": DAILY_LEDGER_RUNNER_GUARD},
                    )
                if serving_pointer_retirement and mysql_session:
                    classification = (
                        _classify_serving_pointer_retirement_state(
                            _read_serving_pointer_retirement_state(
                                connection
                            )
                        )
                    )
                    if classification != "COMPATIBLE_PARTIAL":
                        raise MigrationPreflightError(
                            "migration 019 requires the complete "
                            "serving-pointer source definition before "
                            "DROP TABLE"
                        )
                if period_average_actuals and mysql_session:
                    _validate_period_average_actuals_schema(
                        _read_period_average_actuals_schema(connection),
                        allow_missing=True,
                    )
                for statement in statements:
                    execution_started = True
                    if mysql_session and _is_ddl_statement(statement):
                        ddl_attempted = True
                    connection.execute(text(statement))
        except BaseException as exc:
            execution_error = exc

        postcondition_error: BaseException | None = None
        if daily_ledger and mysql_session and execution_started:
            try:
                with engine.begin() as connection:
                    preflight_migration_session(
                        connection,
                        require_reviewed_constraint_namespace=True,
                    )
                    fingerprint = (
                        read_daily_ledger_schema_fingerprint(connection)
                    )
                    validate_daily_ledger_schema_fingerprint(
                        fingerprint,
                        allow_missing=False,
                    )
            except BaseException as exc:
                postcondition_error = exc
        if (
            schedule_run_started_at
            and mysql_session
            and execution_started
        ):
            try:
                with engine.begin() as connection:
                    preflight_migration_session(connection)
                    classification = (
                        _classify_schedule_run_started_at_shape(
                            _read_schedule_run_started_at_shape(
                                connection
                            )
                        )
                    )
                    if classification != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 018 did not reach target definition"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if (
            serving_pointer_retirement
            and mysql_session
            and execution_started
        ):
            try:
                with engine.begin() as connection:
                    preflight_migration_session(connection)
                    classification = (
                        _classify_serving_pointer_retirement_state(
                            _read_serving_pointer_retirement_state(
                                connection
                            )
                        )
                    )
                    if classification != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 019 did not remove "
                            "t_scheme_serving_pointer"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if period_average_actuals and mysql_session and execution_started:
            try:
                with engine.begin() as connection:
                    preflight_migration_session(connection)
                    _validate_period_average_actuals_schema(
                        _read_period_average_actuals_schema(connection),
                        allow_missing=False,
                    )
            except BaseException as exc:
                postcondition_error = exc

        if execution_error is not None:
            if ddl_attempted:
                raise _partial_apply_error(
                    path,
                    execution_error=execution_error,
                    postcondition_error=postcondition_error,
                ) from execution_error
            raise execution_error
        if postcondition_error is not None:
            raise _partial_apply_error(
                path,
                execution_error=None,
                postcondition_error=postcondition_error,
            ) from postcondition_error
        print(f"applied {path.name}")


def _apply_migration_files_without_history(
    engine: object,
    paths: Iterable[Path],
) -> None:
    """按文件名执行迁移，并对 MySQL 017 实施 fail-closed 门禁。

    所有文件会先完整解析，避免较早 DDL 已隐式提交后才发现后续 SQL
    无法拆分。每个 migration 使用其实际执行 session 做契约检查。
    migration 017 一旦开始执行，无论 statement 成功或失败，都会再用
    一个受检连接读取 schema definition postcondition。
    """
    prepared: list[tuple[Path, list[str]]] = []
    for raw_path in sorted(paths, key=lambda candidate: candidate.name):
        path = Path(raw_path)
        try:
            statements = split_sql_statements(
                path.read_text(encoding="utf-8")
            )
        except MigrationSQLParseError as exc:
            raise MigrationSQLParseError(
                f"{path.name}: {exc}"
            ) from exc
        prepared.append((path, statements))

    _execute_prepared_migration_files(engine, prepared)


def apply_pending_migration_files(
    engine: object,
    paths: Iterable[Path],
) -> None:
    """持有 MySQL owner lock，以 checksum history 只执行 pending 后缀。"""
    manifest = validate_release_migration_manifest(paths)
    with engine.connect() as owner_connection:
        if getattr(owner_connection.dialect, "name", None) != "mysql":
            _execute_prepared_migration_files(
                engine,
                [
                    (migration.path, migration.statements)
                    for migration in manifest
                ],
            )
            return
        preflight_migration_session(owner_connection)
        acquired = int(
            owner_connection.execute(
                text(
                    "SELECT GET_LOCK(:lock_name, 5)"
                ),
                {"lock_name": MIGRATION_HISTORY_LOCK},
            ).scalar_one()
            or 0
        )
        if acquired != 1:
            raise MigrationHistoryError(
                "could not acquire migration owner lock"
            )
        try:
            history = _load_or_bootstrap_migration_history(
                engine,
                owner_connection,
                manifest,
            )
            pending = select_pending_migrations(manifest, history)
            for migration in pending:
                _mark_migration_applying(engine, migration)
                _execute_prepared_migration_files(
                    engine,
                    [(migration.path, migration.statements)],
                )
                _mark_migration_applied(engine, migration)
        finally:
            owner_connection.execute(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": MIGRATION_HISTORY_LOCK},
            )


def apply_migration_files(
    engine: object,
    paths: Iterable[Path],
) -> None:
    """公开入口始终经过 checksum history，不能重放已应用迁移。"""
    apply_pending_migration_files(engine, paths)
