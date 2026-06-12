from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "010_prediction_semantics.sql"


def _normalized_sql() -> str:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)
    return re.sub(r"\s+", " ", "\n".join(lines)).lower().replace("''", "'")


class PredictionSemanticsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _normalized_sql()

    def test_adds_prediction_semantics_columns_idempotently(self) -> None:
        self.assertIn("t_scheme_predictions", self.sql)
        self.assertIn("column_name = 'feature_date'", self.sql)
        self.assertIn("alter table t_scheme_predictions add column feature_date date null", self.sql)
        self.assertIn("column_name = 'prediction_phase'", self.sql)
        self.assertIn(
            "alter table t_scheme_predictions add column prediction_phase enum('gray_live','scheduled_live') null",
            self.sql,
        )
        self.assertIn("alter table t_scheme_runs add column prediction_phase enum('gray_live','scheduled_live') null", self.sql)

    def test_adds_read_indexes_without_mutating_existing_rows(self) -> None:
        self.assertIn("idx_scheme_predictions_phase", self.sql)
        self.assertIn("idx_scheme_runs_phase", self.sql)
        self.assertNotRegex(self.sql, r"\bdelete\s+from\b")
        self.assertNotRegex(self.sql, r"\btruncate\b")
        self.assertNotRegex(self.sql, r"\bupdate\s+t_scheme_predictions\b")
        self.assertNotRegex(self.sql, r"\binsert\s+into\s+t_scheme_predictions\b")


if __name__ == "__main__":
    unittest.main()
