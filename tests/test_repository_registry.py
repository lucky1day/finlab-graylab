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


class _Result:
    lastrowid = 101


class _RunConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params=None) -> _Result:
        self._store.setdefault("calls", []).append((str(sql), params))
        self._store["sql"] = str(sql)
        self._store["params"] = params
        return _Result()


class _RunBegin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _RunConnection:
        return _RunConnection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _RunEngine:
    def __init__(self) -> None:
        self.store: dict = {}

    def begin(self) -> _RunBegin:
        return _RunBegin(self.store)


class ImmutablePredictionRepositoryTests(unittest.TestCase):
    def test_create_scheme_run_inserts_running_row_and_returns_run_id(self) -> None:
        from scheduler.repository import create_scheme_run

        engine = _RunEngine()
        run_id = create_scheme_run(
            engine,
            scheme_id="t1_daily",
            predict_date="2026-06-05",
            scheme_version="abc123",
            run_type="active",
            input_artifact_id="artifact-1",
        )

        self.assertEqual(run_id, 101)
        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_runs", sql)
        self.assertEqual(params["scheme_id"], "t1_daily")
        self.assertEqual(params["scheme_version"], "abc123")
        self.assertEqual(params["predict_date"], "2026-06-05")
        self.assertEqual(params["run_type"], "active")
        self.assertEqual(params["status"], "running")
        self.assertEqual(params["input_artifact_id"], "artifact-1")

    def test_insert_run_predictions_uses_run_id_without_legacy_upsert(self) -> None:
        from scheduler.repository import insert_run_predictions
        from shared.models import PredictionRecord

        engine = _RunEngine()
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                predicted_direction=1,
                confidence=0.8,
            )
        ]

        written = insert_run_predictions(engine, 101, records, scheme_version="abc123")

        self.assertEqual(written, 1)
        sql = engine.store["sql"]
        rows = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_predictions", sql)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(rows[0]["run_id"], 101)
        self.assertEqual(rows[0]["scheme_version"], "abc123")

    def test_update_serving_pointer_upserts_latest_run(self) -> None:
        from scheduler.repository import update_serving_pointer

        engine = _RunEngine()
        update_serving_pointer(
            engine,
            scheme_id="t1_daily",
            target_tenor="10Y",
            predict_date="2026-06-05",
            run_id=101,
            status="approved",
        )

        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_serving_pointer", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(params["serving_run_id"], 101)
        self.assertEqual(params["serving_status"], "approved")


if __name__ == "__main__":
    unittest.main()
