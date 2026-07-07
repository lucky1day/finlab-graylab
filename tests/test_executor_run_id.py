from __future__ import annotations

import unittest
import inspect
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import patch


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class ExecutorRunIdTests(unittest.TestCase):
    def test_run_scheme_subprocess_timeout_kills_process_group(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        class FakeProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.communicate_calls = 0

            def communicate(self, timeout=None):
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout)
                return "", "timeout"

            def wait(self, timeout=None):
                self.returncode = -signal.SIGTERM
                return self.returncode

        fake_process = FakeProcess()
        with patch("scheduler.executor.subprocess.Popen", return_value=fake_process) as popen:
            with patch("scheduler.executor.os.getpgid", return_value=67890) as getpgid:
                with patch("scheduler.executor.os.killpg") as killpg:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        run_scheme_subprocess("demo", "2026-07-03", algo_env="test_env", timeout_sec=1)

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        getpgid.assert_called_once_with(12345)
        killpg.assert_called_once_with(67890, signal.SIGTERM)

    def test_run_scheme_subprocess_timeout_drain_is_bounded(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        class FakePipe:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.communicate_timeouts: list[int | None] = []
                self.stdout = FakePipe()
                self.stderr = FakePipe()

            def communicate(self, timeout=None):
                self.communicate_timeouts.append(timeout)
                if len(self.communicate_timeouts) == 1:
                    raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout)
                raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout, output="partial", stderr="err")

            def wait(self, timeout=None):
                self.returncode = -signal.SIGTERM
                return self.returncode

        fake_process = FakeProcess()
        with patch("scheduler.executor.subprocess.Popen", return_value=fake_process):
            with patch("scheduler.executor.os.getpgid", return_value=67890):
                with patch("scheduler.executor.os.killpg"):
                    with self.assertRaises(subprocess.TimeoutExpired) as ctx:
                        run_scheme_subprocess("demo", "2026-07-03", algo_env="test_env", timeout_sec=7)

        self.assertEqual(fake_process.communicate_timeouts, [7, 1])
        self.assertTrue(fake_process.stdout.closed)
        self.assertTrue(fake_process.stderr.closed)
        self.assertEqual(ctx.exception.output, "partial")
        self.assertEqual(ctx.exception.stderr, "err")

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

    def test_execute_scheme_rejects_daily_records_with_stale_live_context(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="t5_daily",
            status="active",
            scheme_version="abc123",
            horizon=5,
            frequency="daily",
        )
        records = [
            PredictionRecord(
                scheme_id="t5_daily",
                target_tenor="10Y",
                horizon=5,
                predict_date="2026-07-07",
                target_date="2026-07-10",
                feature_date="2026-07-03",
                predicted_direction=1,
                extra={"feature_date": "2026-07-03"},
            )
        ]
        calendar = SimpleNamespace(
            previous_trading_day=lambda value: "2026-07-06",
            nth_trading_day_after=lambda value, count: "2026-07-13",
        )

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor.get_calendar", return_value=calendar, create=True):
                with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                    with patch("scheduler.executor._active_registry_targets", return_value={("10Y", 5)}):
                        with patch("scheduler.executor.create_scheme_run", return_value=301):
                            with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                                with patch("scheduler.executor.insert_run_predictions") as insert_predictions:
                                    with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                        with patch("scheduler.executor.write_run_log") as write_run_log:
                                            result = execute_scheme(cfg, "2026-07-07", algo_env="test_env")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records_written, 0)
        self.assertIn("expected feature_date=2026-07-06", result.error_msg or "")
        self.assertIn("expected target_date=2026-07-13", result.error_msg or "")
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
