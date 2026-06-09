from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "005_lifecycle.sql"


def _sql_without_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


class LifecycleMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.clean_sql = _sql_without_comments(self.sql)
        self.normalized = re.sub(r"\s+", " ", self.clean_sql).lower()

    def test_migration_creates_required_lifecycle_tables_idempotently(self) -> None:
        required_tables = [
            "t_scheme_versions",
            "t_harness_runs",
            "t_harness_gate_results",
            "t_input_artifacts",
            "t_scheme_runs",
            "t_scheme_serving_pointer",
        ]

        for table in required_tables:
            with self.subTest(table=table):
                self.assertIn(f"create table if not exists {table}", self.normalized)
                pattern = re.compile(
                    rf"create table if not exists {table} .*?\) "
                    r"engine=innodb default charset=utf8mb4 collate=utf8mb4_0900_ai_ci",
                    re.IGNORECASE | re.DOTALL,
                )
                self.assertRegex(self.clean_sql, pattern)

    def test_migration_declares_required_unique_keys_and_foreign_keys(self) -> None:
        required_fragments = [
            "unique key uk_scheme_version (scheme_id, scheme_version)",
            "foreign key (harness_run_id) references t_harness_runs(harness_run_id)",
            "unique key uk_input_artifact (scheme_id, predict_date, content_hash)",
            "unique key uk_serving_pointer (scheme_id, target_tenor, predict_date)",
            "foreign key (serving_run_id) references t_scheme_runs(run_id)",
        ]

        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.normalized)

    def test_migration_adds_existing_table_columns_without_removing_rollback_path(self) -> None:
        required_adds = [
            ("t_scheme_predictions", "run_id", "alter table t_scheme_predictions add column run_id"),
            ("t_scheme_predictions", "scheme_version", "alter table t_scheme_predictions add column scheme_version"),
            ("t_backtest_runs", "backtest_run_id", "alter table t_backtest_runs add column backtest_run_id"),
            ("t_backtest_runs", "code_hash", "alter table t_backtest_runs add column code_hash"),
            ("t_backtest_runs", "config_hash", "alter table t_backtest_runs add column config_hash"),
            (
                "t_backtest_runs",
                "input_artifact_hash",
                "alter table t_backtest_runs add column input_artifact_hash",
            ),
            ("t_backtest_runs", "run_mode", "alter table t_backtest_runs add column run_mode"),
        ]

        for table, column, ddl_fragment in required_adds:
            with self.subTest(table=table, column=column):
                self.assertIn("information_schema.columns", self.normalized)
                self.assertIn(f"table_name = '{table}'", self.normalized)
                self.assertIn(f"column_name = '{column}'", self.normalized)
                self.assertIn(ddl_fragment, self.normalized)

        self.assertNotRegex(self.normalized, r"\bdrop\s+(?:index|key|column|table)\b")
        self.assertNotRegex(self.normalized, r"\binsert\s+into\b")
        self.assertNotRegex(self.normalized, r"(?:^|;)\s*update\s+\w+")
        self.assertNotRegex(self.normalized, r"\bdelete\s+from\b")


if __name__ == "__main__":
    unittest.main()
