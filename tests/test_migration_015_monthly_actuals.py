from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "015_monthly_actuals.sql"


def _normalized_sql() -> str:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)
    return re.sub(r"\s+", " ", "\n".join(lines)).lower()


class MonthlyActualsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _normalized_sql()

    def test_creates_monthly_actuals_table_with_target_rule_key(self) -> None:
        self.assertIn("create table if not exists t_scheme_monthly_actuals", self.sql)
        self.assertIn("feature_month_id varchar(7) not null", self.sql)
        self.assertIn("target_month_id varchar(7) not null", self.sql)
        self.assertIn("direction_monthly tinyint not null", self.sql)
        self.assertIn("target_rule varchar(128) not null", self.sql)
        self.assertIn(
            "unique key uk_monthly_actual_predict_rule (tenor, predict_date, target_rule)",
            self.sql,
        )

    def test_adds_read_indexes_without_mutating_existing_business_rows(self) -> None:
        self.assertIn("idx_monthly_actual_target (tenor, target_date, target_rule)", self.sql)
        self.assertIn("idx_monthly_actual_month (feature_month_id, target_month_id)", self.sql)
        self.assertNotRegex(self.sql, r"\bdelete\s+from\b")
        self.assertNotRegex(self.sql, r"\btruncate\b")
        self.assertNotRegex(self.sql, r"\bupdate\s+t_scheme_")
        self.assertNotRegex(self.sql, r"\binsert\s+into\s+t_scheme_")


if __name__ == "__main__":
    unittest.main()
