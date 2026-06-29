from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "014_weekly_average_actuals.sql"


def _normalized_sql() -> str:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)
    return re.sub(r"\s+", " ", "\n".join(lines)).lower()


class WeeklyAverageActualsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _normalized_sql()

    def test_rekeys_weekly_actuals_by_target_rule(self) -> None:
        self.assertIn("information_schema.statistics", self.sql)
        self.assertIn("alter table t_scheme_weekly_actuals drop index uk_weekly_actual_predict", self.sql)
        self.assertIn(
            "alter table t_scheme_weekly_actuals add unique key uk_weekly_actual_predict_rule "
            "(tenor, predict_date, target_rule)",
            self.sql,
        )

    def test_adds_target_rule_read_index_without_mutating_rows(self) -> None:
        self.assertIn("idx_weekly_actual_target_rule", self.sql)
        self.assertIn("(tenor, target_date, target_rule)", self.sql)
        self.assertNotRegex(self.sql, r"\bdelete\s+from\b")
        self.assertNotRegex(self.sql, r"\btruncate\b")
        self.assertNotRegex(self.sql, r"\bupdate\s+t_scheme_weekly_actuals\b")
        self.assertNotRegex(self.sql, r"\binsert\s+into\s+t_scheme_weekly_actuals\b")


if __name__ == "__main__":
    unittest.main()
