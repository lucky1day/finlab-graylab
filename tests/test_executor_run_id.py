from __future__ import annotations

import unittest
import inspect
from types import SimpleNamespace
from unittest.mock import patch


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class ExecutorRunIdTests(unittest.TestCase):
    def test_execute_scheme_appends_predictions_without_serving_pointer(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(scheme_id="t1_daily", status="active", scheme_version="abc123", horizon=1)
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="5Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=1,
                extra={"feature_date": "2026-06-04"},
            ),
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=-1,
                extra={"feature_date": "2026-06-04"},
            ),
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("5Y", 1), ("10Y", 1)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=101) as create_run:
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                            with patch("scheduler.executor.insert_run_predictions", return_value=2) as insert_predictions:
                                with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                    with patch("scheduler.executor.write_run_log") as write_run_log:
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
            prediction_phase="scheduled_live",
        )
        written_records = insert_predictions.call_args.args[2]
        self.assertEqual([record.prediction_phase for record in written_records], ["scheduled_live", "scheduled_live"])
        self.assertEqual([record.extra["prediction_phase"] for record in written_records], ["scheduled_live", "scheduled_live"])
        insert_predictions.assert_called_once()
        self.assertEqual(insert_predictions.call_args.args[:2], (engine, 101))
        self.assertEqual(insert_predictions.call_args.kwargs["scheme_version"], "abc123")
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["run_id"], 101)
        self.assertEqual(finish_run.call_args.kwargs["status"], "success")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 2)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 2)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.kwargs["run_id"], 101)

    def test_execute_scheme_uses_per_scheme_schedule_timeout(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="slow_daily",
            status="active",
            scheme_version="abc123",
            horizon=5,
            schedule=SimpleNamespace(timeout_sec=1800),
        )
        records = [
            PredictionRecord(
                scheme_id="slow_daily",
                target_tenor="10Y",
                horizon=5,
                predict_date="2026-07-03",
                target_date="2026-07-10",
                feature_date="2026-07-02",
                predicted_direction=1,
                extra={"feature_date": "2026-07-02"},
            )
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("10Y", 5)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=201):
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records) as runner:
                            with patch("scheduler.executor.insert_run_predictions", return_value=1):
                                with patch("scheduler.executor.finish_scheme_run"):
                                    with patch("scheduler.executor.write_run_log"):
                                        result = execute_scheme(cfg, "2026-07-03", algo_env="test_env")

        self.assertEqual(result.status, "success")
        runner.assert_called_once_with(
            "slow_daily",
            "2026-07-03",
            algo_env="test_env",
            timeout_sec=1800,
        )

    def test_execute_scheme_rejects_records_outside_active_registry_targets(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(scheme_id="t1_daily", status="active", scheme_version="abc123", horizon=1)
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="7Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=1,
                extra={"feature_date": "2026-06-04"},
            )
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("5Y", 1)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=102):
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                            with patch("scheduler.executor.insert_run_predictions") as insert_predictions:
                                with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                    with patch("scheduler.executor.write_run_log") as write_run_log:
                                        result = execute_scheme(cfg, "2026-06-05", algo_env="test_env")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records_written, 0)
        self.assertIn("not active in t_scheme_registry", result.error_msg or "")
        insert_predictions.assert_not_called()
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["status"], "failed")
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 0)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.args[3], "failed")

    def test_executor_does_not_write_serving_pointer(self) -> None:
        import scheduler.executor as executor

        self.assertNotIn("update_serving_pointer", inspect.getsource(executor))


if __name__ == "__main__":
    unittest.main()
