from __future__ import annotations

import unittest
from types import SimpleNamespace


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, rows) -> None:
        self._store["sql"] = str(sql)
        self._store["rows"] = rows


class _CaptureBegin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _CaptureConnection:
        return _CaptureConnection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _CaptureEngine:
    def __init__(self) -> None:
        self.store: dict = {}

    def begin(self) -> _CaptureBegin:
        return _CaptureBegin(self.store)


class RegistrySyncTests(unittest.TestCase):
    def test_registry_sync_only_refreshes_updated_at_when_metadata_changes(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()
        scheme = SimpleNamespace(
            scheme_id="demo_weekly_scheme",
            name="Demo Weekly Scheme",
            description="weekly scheme",
            horizon=6,
            tenors=["10Y"],
            frequency="weekly",
            schedule=SimpleNamespace(cron="30 11 * * 6", timezone="Asia/Shanghai"),
            status="active",
        )

        sync_scheme_registry(engine, [scheme])

        sql = engine.store["sql"]
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        self.assertIn("updated_at = IF(", update_clause)
        self.assertLess(update_clause.index("updated_at = IF("), update_clause.index("name = VALUES(name)"))
        self.assertNotIn("updated_at = CURRENT_TIMESTAMP", update_clause)


if __name__ == "__main__":
    unittest.main()
