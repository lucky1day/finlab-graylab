from __future__ import annotations

import unittest
from types import SimpleNamespace


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, rows) -> None:
        self._store.setdefault("calls", []).append((str(sql), rows))
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
    def test_registry_scheme_id_is_composite_for_every_tenor(self) -> None:
        from scheduler.repository import registry_scheme_id

        self.assertEqual(registry_scheme_id("t1_daily", 1, "5Y"), "t1_daily__h1__5Y")
        self.assertEqual(registry_scheme_id("daily_5y_2_v28", 5, "5Y"), "daily_5y_2_v28__h5__5Y")

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
            scheme_version="abc123def456",
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
        )

        sync_scheme_registry(engine, [scheme])

        sql = engine.store["calls"][0][0]
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        self.assertIn("updated_at = IF(", update_clause)
        self.assertLess(update_clause.index("updated_at = IF("), update_clause.index("name = VALUES(name)"))
        self.assertNotIn("updated_at = CURRENT_TIMESTAMP", update_clause)
        self.assertIn("deployed_at = IF(deployed_at IS NULL AND VALUES(status) = 'active'", update_clause)

    def test_registry_sync_writes_one_registry_row_per_target_tenor(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()
        scheme = SimpleNamespace(
            scheme_id="t5_daily",
            name="T5 Daily",
            description="multi tenor daily scheme",
            horizon=5,
            tenors=["3Y", "5Y", "7Y", "10Y"],
            frequency="daily",
            schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
            status="active",
            scheme_version=None,
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
        )

        sync_scheme_registry(engine, [scheme])

        sql = engine.store["calls"][0][0]
        rows = engine.store["calls"][0][1]
        self.assertNotIn("base_scheme_id, frequency, horizon, target_tenor", sql)
        self.assertEqual(
            [row["scheme_id"] for row in rows],
            [
                "t5_daily__h5__3Y",
                "t5_daily__h5__5Y",
                "t5_daily__h5__7Y",
                "t5_daily__h5__10Y",
            ],
        )
        self.assertEqual({row["base_scheme_id"] for row in rows}, {"t5_daily"})
        self.assertEqual([row["target_tenor"] for row in rows], ["3Y", "5Y", "7Y", "10Y"])
        self.assertEqual([row["tenors"] for row in rows], ['["3Y"]', '["5Y"]', '["7Y"]', '["10Y"]'])


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
            prediction_phase="scheduled_live",
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
        self.assertEqual(params["prediction_phase"], "scheduled_live")
        self.assertEqual(params["status"], "running")
        self.assertEqual(params["input_artifact_id"], "artifact-1")

    def test_insert_run_predictions_upserts_prediction_semantics(self) -> None:
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
                feature_date="2026-06-04",
                prediction_phase="scheduled_live",
                predicted_direction=1,
                confidence=0.8,
                extra={"feature_date": "2026-06-04"},
            )
        ]

        written = insert_run_predictions(engine, 101, records, scheme_version="abc123")

        self.assertEqual(written, 1)
        sql = engine.store["sql"]
        rows = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_predictions", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("run_id = VALUES(run_id)", sql)
        self.assertIn("scheme_version = VALUES(scheme_version)", sql)
        self.assertIn("predict_date = VALUES(predict_date)", sql)
        self.assertIn("feature_date = VALUES(feature_date)", sql)
        self.assertIn("prediction_phase = VALUES(prediction_phase)", sql)
        self.assertEqual(rows[0]["run_id"], 101)
        self.assertEqual(rows[0]["scheme_version"], "abc123")
        self.assertEqual(rows[0]["feature_date"], "2026-06-04")
        self.assertEqual(rows[0]["prediction_phase"], "scheduled_live")

    def test_insert_run_predictions_requires_feature_date_and_phase(self) -> None:
        from scheduler.repository import insert_run_predictions
        from shared.models import PredictionRecord

        engine = _RunEngine()
        record = PredictionRecord(
            scheme_id="t1_daily",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-06-05",
            target_date="2026-06-06",
            predicted_direction=1,
        )

        with self.assertRaisesRegex(ValueError, "feature_date"):
            insert_run_predictions(engine, 101, [record], scheme_version="abc123")

    def test_upsert_scheme_version_writes_version_hashes(self) -> None:
        from scheduler.repository import upsert_scheme_version

        engine = _RunEngine()
        cfg = SimpleNamespace(
            scheme_id="t1_daily",
            scheme_version="abc123def456",
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
            status="active",
        )

        version = upsert_scheme_version(engine, cfg)

        self.assertEqual(version, "abc123def456")
        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_versions", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(params["scheme_id"], "t1_daily")
        self.assertEqual(params["scheme_version"], "abc123def456")
        self.assertEqual(params["code_hash"], "c" * 64)
        self.assertEqual(params["config_hash"], "f" * 64)
        self.assertIsNone(params["manifest_hash"])
        self.assertEqual(params["status"], "active")


if __name__ == "__main__":
    unittest.main()
