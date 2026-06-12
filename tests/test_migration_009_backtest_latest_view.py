from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "009_backtest_latest_view.sql"


def _sql_without_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


class BacktestLatestViewMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.normalized = re.sub(r"\s+", " ", _sql_without_comments(self.sql)).lower()

    def test_migration_recreates_latest_view_with_canonical_frontend_semantics(self) -> None:
        self.assertIn("create or replace view v_latest_backtest_run", self.normalized)
        self.assertIn("row_number() over", self.normalized)
        self.assertIn("partition by benchmark_id, scheme_id, data_source", self.normalized)
        self.assertIn("order by updated_at desc, id desc", self.normalized)
        self.assertIn("where status = 'success'", self.normalized)

    def test_latest_view_does_not_partition_by_backtest_window(self) -> None:
        partition_match = re.search(r"partition by ([^)]+?) order by", self.normalized)
        self.assertIsNotNone(partition_match)
        partition_clause = partition_match.group(1)
        self.assertNotIn("start_date", partition_clause)
        self.assertNotIn("end_date", partition_clause)
        self.assertNotIn("group by benchmark_id, scheme_id, data_source, start_date, end_date", self.normalized)

    def test_migration_is_view_and_index_only(self) -> None:
        self.assertIn("idx_backtest_runs_latest_success", self.normalized)
        self.assertNotRegex(self.normalized, r"\bdelete\s+from\b")
        self.assertNotRegex(self.normalized, r"\binsert\s+into\b")
        self.assertNotRegex(self.normalized, r"\bupdate\s+t_backtest_")
        self.assertNotIn("t_scheme_", self.normalized)


if __name__ == "__main__":
    unittest.main()
