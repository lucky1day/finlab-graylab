from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "013_add_1y_target_registry.sql"


def _normalized_sql() -> str:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)
    return re.sub(r"\s+", " ", "\n".join(lines)).lower()


class Add1YTargetRegistryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _normalized_sql()

    def test_upserts_1y_active_treasury_target(self) -> None:
        self.assertIn("insert into t_target_registry", self.sql)
        self.assertIn("'1y'", self.sql)
        self.assertIn("'1y国债活跃'", self.sql)
        self.assertIn("'bond'", self.sql)
        self.assertIn("'active_treasury'", self.sql)
        self.assertIn("10", self.sql)
        self.assertIn("'active'", self.sql)
        self.assertIn("json_object('legacy_tenor', '1y')", self.sql)

    def test_is_idempotent_and_does_not_touch_predictions_or_backtests(self) -> None:
        self.assertIn("on duplicate key update", self.sql)
        self.assertIn("display_name = values(display_name)", self.sql)
        self.assertIn("sort_order = values(sort_order)", self.sql)
        self.assertNotRegex(self.sql, r"\binsert\s+into\s+t_scheme_predictions\b")
        self.assertNotRegex(self.sql, r"\binsert\s+into\s+t_backtest_")
        self.assertNotRegex(self.sql, r"\bupdate\s+t_scheme_predictions\b")
        self.assertNotRegex(self.sql, r"\bupdate\s+t_backtest_")


if __name__ == "__main__":
    unittest.main()
