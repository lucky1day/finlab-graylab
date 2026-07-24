from __future__ import annotations

import re
import unittest
from pathlib import Path

from scheduler.daily_ledger import SCHEDULE_FAILURE_CODES
from scripts.apply_migrations import split_sql_statements


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "017_daily_schedule_ledger.sql"
)


def _migration_sql() -> str:
    if not MIGRATION.exists():
        return ""
    return MIGRATION.read_text(encoding="utf-8")


def _without_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


class DailyScheduleLedgerMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _without_comments(_migration_sql())
        self.normalized = re.sub(r"\s+", " ", self.sql).lower()

    def test_creates_all_ledger_tables_idempotently(self) -> None:
        for table in (
            "t_input_generations",
            "t_schedule_occurrences",
            "t_schedule_items",
            "t_schedule_item_targets",
            "t_scheduler_heartbeat",
        ):
            with self.subTest(table=table):
                self.assertIn(
                    f"create table if not exists {table}",
                    self.normalized,
                )

    def test_requires_runner_session_guard_before_first_ddl(self) -> None:
        guard = self.normalized.index(
            "@bond_factor_lab_migration_runner_017"
        )
        first_ddl = self.normalized.index(
            "create table if not exists t_input_generations"
        )

        self.assertLess(guard, first_ddl)
        self.assertIn(
            "migration 017 requires scripts/apply_migrations.py",
            self.normalized,
        )

    def test_declares_frozen_identity_uniqueness_and_foreign_keys(self) -> None:
        required = (
            "unique key uk_schedule_occurrence (schedule_key, predict_date)",
            "unique key uk_schedule_item (occurrence_id, base_scheme_id)",
            "unique key uk_schedule_target_registry (occurrence_id, registry_scheme_id)",
            "unique key uk_schedule_target_item (item_id, target_tenor, horizon)",
            "foreign key (occurrence_id) references t_schedule_occurrences(occurrence_id)",
            "foreign key (item_id) references t_schedule_items(item_id)",
            "foreign key (input_generation_id) references t_input_generations(generation_id)",
            "foreign key (current_run_id) references t_scheme_runs(run_id)",
            "foreign key (accepted_run_id) references t_scheme_runs(run_id)",
            "foreign key (accepted_prediction_id) references t_scheme_predictions(id)",
            "foreign key (schedule_item_id) references t_schedule_items(item_id)",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.normalized)

    def test_generation_freezes_provenance_and_uses_approved_state_machine(
        self,
    ) -> None:
        generation_table = re.search(
            r"create table if not exists t_input_generations\s*\((.*?)\)\s*engine=",
            self.sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(generation_table)
        generation_sql = re.sub(
            r"\s+",
            " ",
            generation_table.group(1),
        ).lower()
        for field in (
            "generation_type",
            "business_date",
            "feature_date",
            "readiness_basis",
            "source_commit_token",
            "dataset_content_id",
            "schema_version",
            "exporter_version",
            "manifest_uri",
            "manifest_sha256",
            "native_generation_id",
            "native_manifest_sha256",
        ):
            with self.subTest(field=field):
                self.assertRegex(generation_sql, rf"\b{field}\b")
        self.assertIn(
            "state enum('building','sealed','invalidated')",
            generation_sql,
        )
        self.assertIn(
            "generation_type enum('native_source','databridge_v1')",
            generation_sql,
        )
        self.assertIn(
            "readiness_basis enum('upstream_seal','clock_contract')",
            generation_sql,
        )
        for opaque_id in (
            "source_commit_token",
            "dataset_content_id",
        ):
            self.assertRegex(generation_sql, rf"\b{opaque_id}\s+varchar\(128\)")
        self.assertRegex(generation_sql, r"\bmanifest_sha256\s+char\(64\)")
        self.assertIn(
            "foreign key (native_generation_id) "
            "references t_input_generations(generation_id)",
            generation_sql,
        )
        self.assertNotRegex(generation_sql, r"\bstate\b[^,]*'invalid'")

    def test_occurrence_freezes_policy_and_write_once_sla_contract(self) -> None:
        occurrence_table = re.search(
            r"create table if not exists t_schedule_occurrences\s*\((.*?)\)\s*engine=",
            self.sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(occurrence_table)
        occurrence_sql = re.sub(
            r"\s+",
            " ",
            occurrence_table.group(1),
        ).lower()
        for field in (
            "feature_date",
            "policy_version",
            "policy_sha256",
            "policy_json",
            "registry_digest",
            "sla_deadline_at",
            "recovery_cutoff_at",
            "completion_state",
            "sla_outcome",
            "sla_accepted_target_count",
            "failure_code",
            "failure_message",
        ):
            with self.subTest(field=field):
                self.assertRegex(occurrence_sql, rf"\b{field}\b")
        self.assertRegex(
            occurrence_sql,
            r"\bfeature_date\s+date\s+not null\b",
        )
        self.assertRegex(
            occurrence_sql,
            r"check\s*\(\s*feature_date\s*<\s*predict_date\s*\)",
        )
        self.assertIn(
            "sla_outcome enum('pending','met','breached')",
            occurrence_sql,
        )

    def test_item_and_target_rows_freeze_execution_and_acceptance_identity(
        self,
    ) -> None:
        item_table = re.search(
            r"create table if not exists t_schedule_items\s*\((.*?)\)\s*engine=",
            self.sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        target_table = re.search(
            r"create table if not exists t_schedule_item_targets\s*\((.*?)\)\s*engine=",
            self.sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(item_table)
        self.assertIsNotNone(target_table)
        item_sql = item_table.group(1).lower()
        target_sql = target_table.group(1).lower()
        for field in (
            "scheme_version",
            "code_sha256",
            "config_sha256",
            "cache_group",
            "input_generation_id",
            "resource_class",
            "internal_workers",
            "release_offset_minutes",
            "release_at",
            "deadline_at",
            "sla_status",
            "late_reason",
            "sla_evaluated_at",
        ):
            with self.subTest(field=field):
                self.assertRegex(item_sql, rf"\b{field}\b")
        self.assertRegex(target_sql, r"\btarget_date\b")
        self.assertRegex(target_sql, r"\baccepted_run_id\b")
        self.assertRegex(target_sql, r"\baccepted_prediction_id\b")
        self.assertRegex(target_sql, r"\bvisible_at\b")
        self.assertIn(
            "stateenum('pending','running','retry_wait','success',"
            "'failed_terminal','abandoned','expired')",
            re.sub(r"\s+", "", item_sql),
        )
        self.assertIn(
            "sla_statusenum('pending','on_time','late')",
            re.sub(r"\s+", "", item_sql),
        )
        for field, sql_type in (
            ("cache_group", r"varchar\(128\) not null"),
            ("resource_class", r"varchar\(64\) not null"),
            ("internal_workers", r"int not null"),
            ("release_offset_minutes", r"int not null"),
            ("release_at", r"datetime\(6\) not null"),
            ("deadline_at", r"datetime\(6\) not null"),
        ):
            with self.subTest(required_resource_field=field):
                self.assertRegex(
                    re.sub(r"\s+", " ", item_sql),
                    rf"\b{field}\s+{sql_type}",
                )

    def test_extends_scheme_runs_with_fencing_and_recovery_fields_idempotently(
        self,
    ) -> None:
        columns = (
            "schedule_item_id",
            "attempt_no",
            "trigger_origin",
            "queued_at",
            "failure_code",
            "execution_token",
            "process_id",
            "process_group_id",
        )
        for column in columns:
            with self.subTest(column=column):
                self.assertIn("information_schema.columns", self.normalized)
                self.assertIn("table_name = 't_scheme_runs'", self.normalized)
                self.assertIn(f"column_name = '{column}'", self.normalized)
                self.assertIn(
                    f"alter table t_scheme_runs add column {column}",
                    self.normalized,
                )
        self.assertIn("information_schema.statistics", self.normalized)
        self.assertIn("uk_scheme_runs_schedule_attempt", self.normalized)
        self.assertIn(
            "add unique index uk_scheme_runs_schedule_attempt "
            "(schedule_item_id, attempt_no)",
            self.normalized,
        )
        self.assertIn(
            "add index idx_schedule_target_identity "
            "(base_scheme_id, target_tenor, horizon, target_date)",
            self.normalized,
        )
        self.assertIn("information_schema.table_constraints", self.normalized)
        self.assertIn(
            "add column trigger_origin enum('apscheduler','startup_catchup',"
            "'auto_retry','operator_recovery')",
            self.normalized.replace("''", "'"),
        )

    def test_incrementally_adds_late_ledger_columns_idempotently(self) -> None:
        late_columns = (
            ("t_input_generations", "native_generation_id"),
            ("t_input_generations", "native_manifest_sha256"),
            ("t_schedule_occurrences", "feature_date"),
            ("t_schedule_occurrences", "sla_accepted_target_count"),
            ("t_schedule_occurrences", "failure_code"),
            ("t_schedule_occurrences", "failure_message"),
            ("t_schedule_items", "resource_class"),
            ("t_schedule_items", "internal_workers"),
            ("t_schedule_items", "release_offset_minutes"),
            ("t_schedule_item_targets", "visible_at"),
        )
        for table, column in late_columns:
            with self.subTest(table=table, column=column):
                self.assertRegex(
                    self.normalized,
                    rf"table_name\s*=\s*'{table}'.*?"
                    rf"column_name\s*=\s*'{column}'.*?"
                    rf"alter table {table} add column {column}",
                )

    def test_legacy_occurrence_feature_date_migration_fails_closed_without_backfill(
        self,
    ) -> None:
        self.assertIn(
            "alter table t_schedule_occurrences "
            "add column feature_date date null after predict_date",
            self.normalized,
        )
        self.assertIn(
            "alter table t_schedule_occurrences "
            "modify column feature_date date not null",
            self.normalized,
        )
        self.assertNotRegex(
            self.normalized,
            r"update\s+t_schedule_occurrences\s+set\s+feature_date",
        )
        self.assertNotRegex(
            self.normalized,
            r"feature_date\s*=\s*(?:date_sub|predict_date)",
        )
        self.assertNotIn("t_trade_calendar", self.normalized)

    def test_feature_date_upgrade_guards_legacy_rows_before_ddl(
        self,
    ) -> None:
        missing_guard = self.normalized.index(
            "nonempty t_schedule_occurrences is missing feature_date"
        )
        add_column = self.normalized.index(
            "alter table t_schedule_occurrences "
            "add column feature_date date null after predict_date"
        )
        invalid_guard = self.normalized.index(
            "t_schedule_occurrences contains invalid feature_date"
        )
        modify_column = self.normalized.index(
            "alter table t_schedule_occurrences "
            "modify column feature_date date not null"
        )

        self.assertLess(missing_guard, add_column)
        self.assertLess(add_column, invalid_guard)
        self.assertLess(invalid_guard, modify_column)
        self.assertIn("feature_date is null", self.normalized)
        self.assertIn(
            "cast(feature_date as char) = '0000-00-00'",
            self.normalized,
        )
        self.assertIn("feature_date >= predict_date", self.normalized)
        top_level_feature_modifies = [
            statement
            for statement in split_sql_statements(self.sql)
            if re.match(
                r"(?is)^alter\s+table\s+t_schedule_occurrences\s+"
                r"modify\s+column\s+feature_date",
                statement,
            )
        ]
        self.assertEqual(top_level_feature_modifies, [])

    def test_resource_identity_upgrade_never_guesses_legacy_values(
        self,
    ) -> None:
        fields = (
            ("resource_class", "varchar(64)"),
            ("internal_workers", "int"),
            ("release_offset_minutes", "int"),
        )
        for field, sql_type in fields:
            with self.subTest(field=field):
                missing_guard = self.normalized.index(
                    f"nonempty t_schedule_items is missing {field}"
                )
                add_column = self.normalized.index(
                    "alter table t_schedule_items "
                    f"add column {field} {sql_type} null"
                )
                null_guard = self.normalized.index(
                    f"t_schedule_items contains null {field}"
                )
                modify_column = self.normalized.index(
                    "alter table t_schedule_items "
                    f"modify column {field} {sql_type} not null"
                )
                self.assertLess(missing_guard, add_column)
                self.assertLess(add_column, null_guard)
                self.assertLess(null_guard, modify_column)
                self.assertNotRegex(
                    self.normalized,
                    rf"add column {field} {re.escape(sql_type)} "
                    r"not null(?:\s+default\b)?",
                )
                self.assertNotRegex(
                    self.normalized,
                    rf"add column {field}[^']*\bdefault\b",
                )
                self.assertRegex(
                    self.normalized,
                    rf"where `{field}` is null",
                )

        self.assertNotIn("default ''cpu_standard''", self.normalized)

    def test_dynamic_fail_closed_guards_are_mysql_preparable(
        self,
    ) -> None:
        guard_messages = (
            "nonempty t_schedule_occurrences is missing feature_date",
            "t_schedule_occurrences contains invalid feature_date",
            "nonempty t_schedule_items is missing resource_class",
            "t_schedule_items contains null resource_class",
            "nonempty t_schedule_items is missing internal_workers",
            "t_schedule_items contains null internal_workers",
            "nonempty t_schedule_items is missing release_offset_minutes",
            "t_schedule_items contains null release_offset_minutes",
        )
        self.assertNotIn("signal sqlstate", self.normalized)
        for message in guard_messages:
            with self.subTest(message=message):
                doubled = f"''{message}''"
                self.assertIn(
                    "select (select guard_message from "
                    f"(select {doubled} as guard_message union all "
                    f"select {doubled}) as migration_guard)",
                    self.normalized,
                )

    def test_target_rows_are_acceptance_only_and_migration_has_no_business_dml(
        self,
    ) -> None:
        target_table = re.search(
            r"create table if not exists t_schedule_item_targets\s*\((.*?)\)\s*engine=",
            self.sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(target_table)
        target_sql = target_table.group(1).lower()
        self.assertIn("accepted_run_id", target_sql)
        self.assertIn("accepted_prediction_id", target_sql)
        self.assertIn("accepted_at", target_sql)
        self.assertIn("visible_at", target_sql)
        self.assertNotIn("claim", target_sql)

        self.assertNotRegex(self.normalized, r"\binsert\s+into\b")
        self.assertNotRegex(self.normalized, r"(?:^|;)\s*update\s+\w+")
        self.assertNotRegex(self.normalized, r"\bdelete\s+from\b")
        self.assertNotRegex(self.normalized, r"\bdrop\s+(?:table|column|index|key)\b")

    def test_ledger_audit_defaults_are_explicit_utc(self) -> None:
        for table in (
            "t_input_generations",
            "t_schedule_occurrences",
            "t_schedule_items",
            "t_schedule_item_targets",
            "t_scheduler_heartbeat",
        ):
            with self.subTest(table=table):
                match = re.search(
                    rf"create table if not exists {table}\s*"
                    r"\((.*?)\)\s*engine=",
                    self.sql,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                self.assertIsNotNone(match)
                table_sql = re.sub(
                    r"\s+",
                    " ",
                    match.group(1),
                ).lower()
                self.assertNotRegex(
                    table_sql,
                    r"default\s+current_timestamp",
                )
                self.assertIn(
                    "default (utc_timestamp(6))",
                    table_sql,
                )

    def test_failure_code_checks_share_the_canonical_vocabulary(self) -> None:
        for constraint in (
            "ck_schedule_occurrence_failure_code",
            "ck_schedule_item_failure_code",
            "ck_scheme_run_schedule_failure_code",
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, self.normalized)
        for failure_code in SCHEDULE_FAILURE_CODES:
            with self.subTest(failure_code=failure_code):
                self.assertIn(f"'{failure_code.lower()}'", self.normalized)
        for legacy in (
            "data_error",
            "contract_error",
            "algorithm_error",
            "result_structure_error",
            "recovery_cutoff_reached",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(f"'{legacy}'", self.normalized)
        self.assertRegex(
            self.normalized,
            r"check\s*\(\s*schedule_item_id\s+is\s+null\s+or\s+"
            r"failure_code\s+is\s+null\s+or\s+failure_code\s+in",
        )


if __name__ == "__main__":
    unittest.main()
