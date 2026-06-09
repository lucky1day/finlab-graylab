from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class ExecutorRunIdTests(unittest.TestCase):
    def test_execute_scheme_appends_predictions_and_updates_serving_pointer(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(scheme_id="t1_daily", status="active", scheme_version="abc123")
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="5Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                predicted_direction=1,
            ),
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                predicted_direction=-1,
            ),
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor.create_scheme_run", return_value=101) as create_run:
                with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                    with patch("scheduler.executor.insert_run_predictions", return_value=2) as insert_predictions:
                        with patch("scheduler.executor.update_serving_pointer") as update_pointer:
                            with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                with patch("scheduler.executor.write_run_log") as write_run_log:
                                    with patch("scheduler.executor.upsert_predictions") as legacy_upsert:
                                        result = execute_scheme(cfg, "2026-06-05", algo_env="test_env")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.records_written, 2)
        self.assertEqual(result.run_id, 101)
        self.assertTrue(engine.disposed)
        create_run.assert_called_once_with(
            engine,
            scheme_id="t1_daily",
            predict_date="2026-06-05",
            scheme_version="abc123",
            run_type="active",
        )
        insert_predictions.assert_called_once_with(engine, 101, records, scheme_version="abc123")
        self.assertEqual(update_pointer.call_count, 2)
        self.assertEqual(
            [call.kwargs["target_tenor"] for call in update_pointer.call_args_list],
            ["5Y", "10Y"],
        )
        self.assertTrue(all(call.kwargs["run_id"] == 101 for call in update_pointer.call_args_list))
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["run_id"], 101)
        self.assertEqual(finish_run.call_args.kwargs["status"], "success")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 2)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 2)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.kwargs["run_id"], 101)
        legacy_upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
