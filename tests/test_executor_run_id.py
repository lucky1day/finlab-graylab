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

    def test_run_scheme_subprocess_disables_conda_output_capture(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        class FakeProcess:
            pid = 12345
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                return "[]", ""

        with patch("scheduler.executor.subprocess.Popen", return_value=FakeProcess()) as popen:
            records = run_scheme_subprocess("demo", "2026-07-03", algo_env="test_env", timeout_sec=7)

        self.assertEqual(records, [])
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[:4], ["conda", "run", "--no-capture-output", "-n"])

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
            records_expected=2,
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
        self.assertEqual(
            result.error_msg,
            "live target mismatch: missing=[('5Y', 1)], extra=[('7Y', 1)], duplicates=[]",
        )
        insert_predictions.assert_not_called()
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["status"], "failed")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 1)
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
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 1)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 0)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.args[3], "failed")

    def test_executor_does_not_write_serving_pointer(self) -> None:
        import scheduler.executor as executor

        self.assertNotIn("update_serving_pointer", inspect.getsource(executor))


class ExecutorTargetCompletenessTests(unittest.TestCase):
    @staticmethod
    def _record(
        target_tenor: str,
        *,
        scheme_id: str = "target_complete",
        horizon: int = 1,
        predicted_direction: int = 1,
        feature_date: str | None = "2026-07-02",
    ):
        from shared.models import PredictionRecord

        extra = {"feature_date": feature_date} if feature_date is not None else None
        return PredictionRecord(
            scheme_id=scheme_id,
            target_tenor=target_tenor,
            horizon=horizon,
            predict_date="2026-07-03",
            target_date="2026-07-06",
            feature_date=feature_date,
            predicted_direction=predicted_direction,
            extra=extra,
        )

    def _execute(
        self,
        records,
        *,
        active_targets: set[tuple[str, int]],
        current_active_targets: set[tuple[str, int]] | None = None,
        records_written: int | None = None,
        finish_side_effect=None,
        write_log_side_effect=None,
    ):
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="target_complete",
            status="active",
            scheme_version="version-1",
            horizon=1,
        )
        written = len(records) if records_written is None else records_written
        current_targets = active_targets if current_active_targets is None else current_active_targets
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")),
            patch(
                "scheduler.executor._active_registry_targets",
                side_effect=[active_targets, current_targets],
            ) as active_targets_reader,
            patch("scheduler.executor.create_scheme_run", return_value=401) as create_run,
            patch("scheduler.executor.run_scheme_subprocess", return_value=records) as runner,
            patch("scheduler.executor.insert_run_predictions", return_value=written) as insert_predictions,
            patch("scheduler.executor.finish_scheme_run", side_effect=finish_side_effect) as finish_run,
            patch("scheduler.executor.write_run_log", side_effect=write_log_side_effect) as write_run_log,
            patch("scheduler.executor.logger.exception") as logger_exception,
        ):
            result = execute_scheme(cfg, "2026-07-03", algo_env="test_env")
        return SimpleNamespace(
            result=result,
            create_run=create_run,
            active_targets_reader=active_targets_reader,
            runner=runner,
            insert_predictions=insert_predictions,
            finish_run=finish_run,
            write_run_log=write_run_log,
            logger_exception=logger_exception,
        )

    def test_execute_scheme_rejects_empty_return_for_active_target(self) -> None:
        execution = self._execute([], active_targets={("5Y", 1)})

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[('5Y', 1)], extra=[], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 0)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 0)

    def test_execute_scheme_rejects_missing_target(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1), ("10Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[('10Y', 1)], extra=[], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)

    def test_execute_scheme_rejects_duplicate_target(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("5Y")],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[], extra=[], duplicates=[('5Y', 1, 2)]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)

    def test_execute_scheme_rejects_extra_target(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("7Y")],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[], extra=[('7Y', 1)], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)

    def test_execute_scheme_rejects_unexpected_empty_active_targets(self) -> None:
        execution = self._execute([], active_targets=set())

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets empty for scheme target_complete: missing=[], extra=[], duplicates=[]",
        )
        execution.runner.assert_not_called()
        execution.insert_predictions.assert_not_called()
        execution.create_run.assert_called_once_with(
            execution.create_run.call_args.args[0],
            scheme_id="target_complete",
            predict_date="2026-07-03",
            scheme_version="version-1",
            run_type="active",
            prediction_phase="scheduled_live",
            records_expected=0,
        )
        self.assertIsNone(execution.finish_run.call_args.kwargs["records_returned"])

    def test_execute_scheme_records_raw_count_when_normalization_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y", feature_date=None)],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertIn("missing feature_date", execution.result.error_msg or "")
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)

    def test_execute_scheme_rejects_target_paused_during_run(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            current_active_targets=set(),
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets changed during run: initial=[('5Y', 1)], current=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.active_targets_reader.call_count, 2)
        self.assertEqual(execution.create_run.call_args.kwargs["records_expected"], 1)

    def test_execute_scheme_rejects_target_added_during_run(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            current_active_targets={("5Y", 1), ("10Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets changed during run: "
            "initial=[('5Y', 1)], current=[('10Y', 1), ('5Y', 1)]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.active_targets_reader.call_count, 2)
        self.assertEqual(execution.create_run.call_args.kwargs["records_expected"], 1)

    def test_execute_scheme_accepts_complete_flat_record(self) -> None:
        execution = self._execute(
            [self._record("5Y", predicted_direction=0)],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "success")
        self.assertEqual(execution.result.records_written, 1)
        inserted = execution.insert_predictions.call_args.args[2]
        self.assertEqual([record.predicted_direction for record in inserted], [0])
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 1)

    def test_execute_scheme_preserves_written_count_when_first_finish_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            finish_side_effect=[RuntimeError("initial finish failed"), None],
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(execution.result.records_written, 1)
        self.assertEqual(execution.result.error_msg, "initial finish failed")
        execution.insert_predictions.assert_called_once()
        self.assertEqual(execution.finish_run.call_count, 2)
        first_finish = execution.finish_run.call_args_list[0].kwargs
        second_finish = execution.finish_run.call_args_list[1].kwargs
        self.assertEqual(first_finish["status"], "success")
        self.assertEqual(first_finish["records_written"], 1)
        self.assertEqual(second_finish["status"], "failed")
        self.assertEqual(second_finish["records_written"], 1)
        self.assertEqual(second_finish["error_message"], "initial finish failed")

    def test_execute_scheme_keeps_success_when_run_log_write_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            write_log_side_effect=[RuntimeError("run log failed"), None],
        )

        self.assertEqual(execution.result.status, "success")
        self.assertEqual(execution.result.records_written, 1)
        self.assertIsNone(execution.result.error_msg)
        self.assertEqual(execution.finish_run.call_count, 1)
        self.assertEqual(execution.finish_run.call_args.kwargs["status"], "success")
        self.assertEqual(execution.write_run_log.call_count, 1)
        execution.logger_exception.assert_called_once()

    def test_execute_scheme_preserves_business_error_when_failure_audits_fail(self) -> None:
        try:
            execution = self._execute(
                [self._record("5Y", feature_date=None)],
                active_targets={("5Y", 1)},
                finish_side_effect=RuntimeError("finish audit failed"),
                write_log_side_effect=RuntimeError("log audit failed"),
            )
        except RuntimeError as exc:
            self.fail(f"audit exception escaped instead of preserving business error: {exc}")

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(execution.result.records_written, 0)
        self.assertTrue(
            (execution.result.error_msg or "").startswith(
                "record target_complete/5Y missing feature_date"
            )
        )
        self.assertIn(
            "finish_scheme_run audit failed: finish audit failed",
            execution.result.error_msg or "",
        )
        self.assertIn(
            "write_run_log audit failed: log audit failed",
            execution.result.error_msg or "",
        )
        execution.finish_run.assert_called_once()
        execution.write_run_log.assert_called_once()
        self.assertEqual(execution.logger_exception.call_count, 2)

    def test_execute_scheme_marks_partial_with_all_three_counts(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("10Y")],
            active_targets={("5Y", 1), ("10Y", 1)},
            records_written=1,
        )

        self.assertEqual(execution.result.status, "partial")
        self.assertEqual(execution.result.records_written, 1)
        self.assertEqual(execution.result.error_msg, "expected=2, returned=2, written=1")
        self.assertEqual(execution.finish_run.call_args.kwargs["status"], "partial")
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 1)
        self.assertEqual(
            execution.finish_run.call_args.kwargs["error_message"],
            "expected=2, returned=2, written=1",
        )


if __name__ == "__main__":
    unittest.main()
