from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "007_backtest_immutable.sql"


def _sql_without_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


class BacktestImmutableMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.normalized = re.sub(r"\s+", " ", _sql_without_comments(self.sql)).lower()

    def test_migration_replaces_legacy_backtest_scope_unique_key(self) -> None:
        self.assertIn("information_schema.statistics", self.normalized)
        self.assertIn("alter table t_backtest_runs drop index uk_backtest_run_scope", self.normalized)
        self.assertIn("add unique key uk_backtest_run_id (backtest_run_id)", self.normalized)

    def test_migration_creates_latest_backtest_run_view(self) -> None:
        self.assertIn("create or replace view v_latest_backtest_run", self.normalized)
        self.assertIn("max(id) as id", self.normalized)
        self.assertIn("group by benchmark_id, scheme_id, data_source, start_date, end_date", self.normalized)

    def test_migration_does_not_touch_scheme_tables(self) -> None:
        self.assertNotIn("t_scheme_", self.normalized)
        self.assertNotRegex(self.normalized, r"\bdrop\s+(?:column|table)\b")
        self.assertNotRegex(self.normalized, r"\binsert\s+into\b")
        self.assertNotRegex(self.normalized, r"\bdelete\s+from\b")


if __name__ == "__main__":
    unittest.main()
