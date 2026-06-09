from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params) -> None:
        self._store["execute_count"] = self._store.get("execute_count", 0) + 1
        self._store["sql"] = str(sql)
        self._store["params"] = params


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


class InputArtifactRepositoryTests(unittest.TestCase):
    def test_upsert_input_artifact_writes_only_input_artifact_table_and_returns_id(self) -> None:
        from scheduler.repository import upsert_input_artifact
        from shared.input_artifacts import InputArtifact

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "daily.csv"
            path.write_text("date,TB0YWI0C\n2026-06-05,2.1\n", encoding="utf-8")
            artifact = InputArtifact(
                scheme_id="t1_daily",
                frequency="daily",
                path=path,
                dataframe=pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.1]}),
                source="shared_data_service_daily",
                generated_at="2026-06-09T00:00:00+00:00",
                data_version="shared_data_service_daily.v1",
                artifact_id="artifact-123",
                content_hash="c" * 64,
                schema_hash="s" * 64,
                source_watermark="2026-06-05",
                row_count=1,
                column_count=2,
                columns=["date", "TB0YWI0C"],
                date_coverage={"field": "date", "start": "2026-06-05", "end": "2026-06-05"},
                quality_flags={},
                metadata={"predict_date": "2026-06-05", "scheme_version": "v1"},
            )

            engine = _CaptureEngine()
            artifact_id = upsert_input_artifact(engine, artifact)

        self.assertEqual(artifact_id, "artifact-123")
        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_input_artifacts", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("t_scheme_predictions", sql)
        self.assertNotIn("t_scheme_runs", sql)
        self.assertEqual(params["artifact_id"], "artifact-123")
        self.assertEqual(params["scheme_id"], "t1_daily")
        self.assertEqual(params["scheme_version"], "v1")
        self.assertEqual(params["predict_date"], "2026-06-05")
        self.assertEqual(params["frequency"], "daily")
        self.assertEqual(params["artifact_uri"], str(path))
        self.assertEqual(params["content_hash"], "c" * 64)
        self.assertEqual(params["schema_hash"], "s" * 64)
        self.assertEqual(params["source_watermark"], "2026-06-05")
        self.assertEqual(params["row_count"], 1)
        self.assertEqual(params["min_date"], "2026-06-05")
        self.assertEqual(params["max_date"], "2026-06-05")

    def test_upsert_input_artifact_returns_same_id_for_repeated_artifact(self) -> None:
        from scheduler.repository import upsert_input_artifact
        from shared.input_artifacts import InputArtifact

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "daily.csv"
            path.write_text("date,TB0YWI0C\n2026-06-05,2.1\n", encoding="utf-8")
            artifact = InputArtifact(
                scheme_id="t1_daily",
                frequency="daily",
                path=path,
                dataframe=pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.1]}),
                source="shared_data_service_daily",
                generated_at="2026-06-09T00:00:00+00:00",
                data_version="shared_data_service_daily.v1",
                artifact_id="artifact-123",
                content_hash="c" * 64,
                schema_hash="s" * 64,
                source_watermark="2026-06-05",
                row_count=1,
                column_count=2,
                columns=["date", "TB0YWI0C"],
                date_coverage={"field": "date", "start": "2026-06-05", "end": "2026-06-05"},
                quality_flags={},
                metadata={"predict_date": "2026-06-05"},
            )

            engine = _CaptureEngine()
            first_id = upsert_input_artifact(engine, artifact)
            second_id = upsert_input_artifact(engine, artifact)

        self.assertEqual(first_id, "artifact-123")
        self.assertEqual(second_id, "artifact-123")
        self.assertEqual(engine.store["execute_count"], 2)
        self.assertIn("ON DUPLICATE KEY UPDATE", engine.store["sql"])


if __name__ == "__main__":
    unittest.main()
