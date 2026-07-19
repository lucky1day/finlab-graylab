from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "016_dual_runtime.sql"


class DualRuntimeMigrationTests(unittest.TestCase):
    def test_adds_runtime_type_to_lifecycle_tables_with_native_default(self) -> None:
        sql = re.sub(r"\s+", " ", MIGRATION.read_text(encoding="utf-8")).lower()

        for table in ("t_scheme_registry", "t_scheme_versions", "t_scheme_runs"):
            with self.subTest(table=table):
                self.assertIn(f"table_name = '{table}'", sql)
                self.assertIn("column_name = 'runtime_type'", sql)
                self.assertIn(
                    f"alter table {table} add column runtime_type varchar(32) not null default ''native_adapter''",
                    sql,
                )

        self.assertNotRegex(sql, r"\bdrop\s+(?:column|table|index|key)\b")

    def test_adds_blackbox_version_and_snapshot_audit_fields(self) -> None:
        sql = re.sub(r"\s+", " ", MIGRATION.read_text(encoding="utf-8")).lower()

        self.assertIn("t_scheme_versions' and column_name = 'environment_fingerprint'", sql)
        self.assertIn("t_scheme_versions' and column_name = 'data_snapshot_id'", sql)
        self.assertIn("t_scheme_runs' and column_name = 'data_snapshot_id'", sql)
        self.assertLess(
            sql.index("t_scheme_versions' and column_name = 'runtime_profile'"),
            sql.index("t_scheme_versions' and column_name = 'environment_fingerprint'"),
        )


if __name__ == "__main__":
    unittest.main()
