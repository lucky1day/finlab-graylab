from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "006_predictions_runid_uk.sql"


def _sql_without_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


class PredictionsRunIdUniqueKeyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.normalized = re.sub(r"\s+", " ", _sql_without_comments(self.sql)).lower()

    def test_migration_replaces_legacy_prediction_unique_key_with_run_id_key(self) -> None:
        self.assertIn("information_schema.statistics", self.normalized)
        self.assertIn("alter table t_scheme_predictions drop index uk_scheme_tenor_predict", self.normalized)
        self.assertIn(
            "add unique key uk_scheme_tenor_predict_run (scheme_id, target_tenor, predict_date, run_id)",
            self.normalized,
        )

    def test_migration_adds_run_id_to_run_log_for_traceability(self) -> None:
        self.assertIn("information_schema.columns", self.normalized)
        self.assertIn("table_name = 't_scheme_run_log'", self.normalized)
        self.assertIn("column_name = 'run_id'", self.normalized)
        self.assertIn("alter table t_scheme_run_log add column run_id bigint null", self.normalized)

    def test_migration_keeps_tables_and_columns_for_rollback_channel(self) -> None:
        self.assertNotRegex(self.normalized, r"\bdrop\s+(?:column|table)\b")
        self.assertNotRegex(self.normalized, r"\binsert\s+into\b")
        self.assertNotRegex(self.normalized, r"(?:^|;)\s*update\s+\w+")
        self.assertNotRegex(self.normalized, r"\bdelete\s+from\b")


if __name__ == "__main__":
    unittest.main()
