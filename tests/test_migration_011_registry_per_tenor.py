from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "011_registry_per_tenor.sql"


def _normalized_sql() -> str:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)
    return re.sub(r"\s+", " ", "\n".join(lines)).lower().replace("''", "'")


class RegistryPerTenorMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = _normalized_sql()

    def test_adds_registry_identity_columns(self) -> None:
        self.assertIn("alter table t_scheme_registry add column base_scheme_id varchar(64)", self.sql)
        self.assertIn("alter table t_scheme_registry add column target_tenor varchar(16)", self.sql)
        self.assertIn("alter table t_scheme_registry add column deployed_at date null", self.sql)

    def test_uses_composite_scheme_id_for_all_registry_rows(self) -> None:
        self.assertIn("concat(", self.sql)
        self.assertIn("__h", self.sql)
        self.assertIn("target_tenor", self.sql)
        self.assertIn("json_table", self.sql)
        self.assertIn("json_array(", self.sql)

    def test_keeps_only_scheme_id_as_unique_identity(self) -> None:
        self.assertIn("scheme_id", self.sql)
        self.assertNotIn("base_scheme_id, frequency, horizon, target_tenor", self.sql)
        self.assertNotIn("uk_base", self.sql)
        self.assertNotIn("uk_registry_task", self.sql)

    def test_backfills_deployed_at_from_created_at_and_is_idempotent(self) -> None:
        self.assertIn("date(created_at)", self.sql)
        self.assertIn("information_schema.columns", self.sql)
        self.assertNotRegex(self.sql, r"\btruncate\b")


if __name__ == "__main__":
    unittest.main()
