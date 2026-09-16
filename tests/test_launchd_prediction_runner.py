from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _run_one_shot(runner, cadence: str, **kwargs):
    from scheduler.executor import _launchd_scheduled_execution_context
    from shared.one_shot_control_plane import LAUNCHD_ONE_SHOT_CONTROL_PLANE

    return runner.run_one_shot(
        cadence,
        scheduled_control_plane=LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        scheduled_execution_context=_launchd_scheduled_execution_context(),
        event="launchd_prediction_run",
        algo_env=kwargs.pop("algo_env", "forecast_env"),
        **kwargs,
    )


def _blackbox_config(
    scheme_id: str,
    *,
    frequency: str = "weekly",
    status: str = "active",
    task_type: str = "weekly_point",
) -> SimpleNamespace:
    """构造不依赖真实 admission 身份的 Blackbox config。"""
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-version",
        frequency=frequency,
        status=status,
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        version_status="active",
        task_type=task_type,
        horizon=1,
        tenors=["10Y"],
        execution_timeout_sec=600,
    )


def _native_config(scheme_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-version",
        frequency="daily",
        status="active",
        runtime_type="native_adapter",
        version_status="active",
        task_type="T+1",
        horizon=1,
        tenors=["10Y"],
    )


class _PeriodCalendar:
    def __init__(self, start: str, end: str, closures: set[str] | None = None) -> None:
        current = date.fromisoformat(start)
        final = date.fromisoformat(end)
        closed = closures or set()
        self.rows = []
        while current <= final:
            self.rows.append(
                {
                    "rdate": current.isoformat(),
                    "trade_flag": (
                        "0" if current.isoformat() in closed else "1"
                    ),
                }
            )
            current += timedelta(days=1)

    def covers(self, value: str) -> bool:
        return self.rows[0]["rdate"] <= value <= self.rows[-1]["rdate"]

    def is_trading_day(self, value: str) -> bool:
        return date.fromisoformat(value).weekday() < 5

    def period_calendar_rows(self):
        return tuple(self.rows)


class LaunchdPredictionRunnerTests(unittest.TestCase):
    def test_native_wave_uses_at_most_two_workers(self) -> None:
        from scheduler import one_shot_prediction_runner as runner
        from scheduler.process_control import ProcessStartGuard

        summary = runner.OneShotPredictionSummary("daily", "2026-08-20")
        candidates = [
            SimpleNamespace(scheme_id=f"native-{index}")
            for index in range(4)
        ]
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = 0
        maximum = 0

        def execute(local_summary, cfg, **_kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=2)
            local_summary.executed.append(
                {"scheme_id": cfg.scheme_id, "status": "success"}
            )
            with lock:
                active -= 1

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                runner,
                "_execute_candidate",
                side_effect=execute,
            ):
                runner._execute_native_wave(
                    summary,
                    candidates,
                    predict_date="2026-08-20",
                    algo_env="forecast_env",
                    scheduled_control_plane="launchd_one_shot",
                    scheduled_execution_context=object(),
                    engine=object(),
                    input_root=Path(tmpdir),
                    cancellation_event=threading.Event(),
                    process_start_guard=ProcessStartGuard(),
                )

        self.assertEqual(maximum, 2)
        self.assertEqual(len(summary.executed), 4)

    def test_requested_scheme_filter_runs_only_exact_active_set(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        selected = _blackbox_config("selected")
        ignored = _blackbox_config("ignored")
        engine = Mock()
        calendar = _PeriodCalendar("2026-08-01", "2026-08-31")
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[selected, ignored],
            ),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(selected, ignored),
            ),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "is_weekly_signal_date", return_value=True),
            patch.object(
                runner,
                "_scheduled_scheme_identity_error",
                return_value=None,
            ),
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id="selected",
                    status="success",
                    records_written=1,
                    run_id=100,
                ),
            ),
        ):
            summary = _run_one_shot(runner,
                "weekly",
                predict_date="2026-08-15",
                scheme_ids=["selected", "selected"],
            )

        self.assertEqual(summary.discovered, 1)
        self.assertEqual(
            [item["scheme_id"] for item in summary.executed],
            ["selected"],
        )

    def test_requested_scheme_filter_rejects_unknown_before_execution(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(runner, "discover_schemes", return_value=[]),
            patch.object(runner, "create_engine_from_env") as create_engine,
        ):
            with self.assertRaisesRegex(
                runner.OneShotPredictionConfigurationError,
                "not available",
            ):
                _run_one_shot(runner,
                    "daily",
                    predict_date="2026-08-28",
                    scheme_ids=["unknown"],
                )

        create_engine.assert_not_called()

    def test_requested_scheme_identity_failure_blocks_entire_batch(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        valid = _native_config("valid-native")
        drifted = _native_config("drifted-native")
        engine = Mock()

        def identity_error(_engine, cfg):
            return (
                "registry task_type mismatch"
                if cfg.scheme_id == "drifted-native"
                else None
            )

        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[valid, drifted],
            ),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(valid, drifted),
            ),
            patch.object(
                runner,
                "_scheduled_scheme_identity_error",
                side_effect=identity_error,
            ),
            patch.object(runner, "_execute_native_wave") as execute_wave,
        ):
            with self.assertRaisesRegex(
                runner.OneShotPredictionConfigurationError,
                "identity is not executable",
            ):
                _run_one_shot(runner,
                    "daily",
                    predict_date="2026-08-28",
                    scheme_ids=["valid-native", "drifted-native"],
                )

        execute_wave.assert_not_called()

    def test_period_average_uses_task_type_and_same_day_ready_gate(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        quarterly = _blackbox_config(
            "quarterly",
            frequency="quarterly",
            task_type="quarterly_average",
        )
        monthly = _blackbox_config(
            "monthly_average",
            frequency="monthly",
            task_type="monthly_average",
        )
        engine = Mock()
        calendar = _PeriodCalendar("2024-01-01", "2024-06-30")
        data_bridge_config = object()
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=data_bridge_config,
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[quarterly, monthly],
            ),
            patch.object(
                runner,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(quarterly, monthly),
            ),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(
                runner,
                "_wait_for_v2_daily_ready",
                return_value=None,
            ) as ready,
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id="quarterly",
                    status="success",
                    records_written=1,
                    run_id=100,
                ),
            ) as execute,
        ):
            summary = _run_one_shot(runner,
                "period_average",
                predict_date="2024-03-29",
                algo_env="forecast_env",
            )

        self.assertEqual(
            [item["scheme_id"] for item in summary.executed],
            ["quarterly"],
        )
        self.assertEqual((summary.outcome, summary.exit_code), ("success", 0))
        ready.assert_called_once_with(
            data_bridge_config,
            run_date="2024-03-29",
            expected_daily_date="2024-03-29",
        )
        self.assertIs(execute.call_args.kwargs["engine"], engine)
        self.assertTrue(
            execute.call_args.kwargs["canonical_config_trusted"]
        )

    def test_period_calendar_failure_is_isolated_by_task_type(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        monthly = _blackbox_config(
            "monthly_average",
            frequency="monthly",
            task_type="monthly_average",
        )
        annual = _blackbox_config(
            "annual_average",
            frequency="annual",
            task_type="annual_average",
        )
        engine = Mock()
        calendar = _PeriodCalendar("2024-01-01", "2024-03-31")
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[monthly, annual],
            ),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(monthly, annual),
            ),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "_wait_for_v2_daily_ready", return_value=None),
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id="monthly_average",
                    status="success",
                    records_written=1,
                    run_id=101,
                ),
            ),
        ):
            summary = _run_one_shot(runner,
                "period_average",
                predict_date="2024-02-15",
                algo_env="forecast_env",
            )

        self.assertEqual(
            [item["scheme_id"] for item in summary.executed],
            ["monthly_average"],
        )
        self.assertEqual(
            summary.blocked,
            [
                {
                    "scheme_id": "annual_average",
                    "code": "period_calendar_invalid",
                }
            ],
        )
        self.assertEqual((summary.outcome, summary.exit_code), ("partial", 1))

    def test_monthly_cadence_does_not_select_monthly_average(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        cfg = _blackbox_config(
            "monthly_average",
            frequency="monthly",
            task_type="monthly_average",
        )
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(runner, "discover_schemes", return_value=[cfg]),
            patch.object(runner, "create_engine_from_env") as create_engine,
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(cfg,),
            ),
        ):
            summary = _run_one_shot(runner,
                "monthly",
                predict_date="2024-02-15",
                algo_env="forecast_env",
            )

        self.assertEqual(summary.discovered, 0)
        self.assertEqual((summary.outcome, summary.exit_code), ("success", 0))
        create_engine.assert_not_called()


    def test_duplicate_prediction_skip_is_benign_but_visible(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        summary = runner.OneShotPredictionSummary("daily", "2026-08-20")
        cfg = _blackbox_config("duplicate")
        result = SimpleNamespace(
            scheme_id=cfg.scheme_id,
            status="skipped",
            records_written=0,
            error_msg="prediction_keys_already_exist",
            run_id=101,
        )
        with patch.object(runner, "execute_scheme", return_value=result):
            runner._execute_candidate(
                summary,
                cfg,
                predict_date="2026-08-20",
                algo_env="forecast_env",
                scheduled_control_plane="launchd_one_shot",
                scheduled_execution_context=object(),
            )
        runner._finalize(summary)

        self.assertEqual(
            summary.skipped,
            [
                {
                    "scheme_id": "duplicate",
                    "code": "prediction_keys_already_exist",
                }
            ],
        )
        self.assertEqual((summary.outcome, summary.exit_code), ("success", 0))

    def test_other_prediction_skips_remain_partial(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        summary = runner.OneShotPredictionSummary(
            "daily",
            "2026-08-20",
        )
        cfg = _blackbox_config("not_approved")
        result = SimpleNamespace(
            scheme_id=cfg.scheme_id,
            status="skipped",
            records_written=0,
            error_msg=(
                "activation failed: scheme_version=secret-version "
                "db_status=paused"
            ),
            run_id=102,
        )
        with patch.object(
            runner,
            "execute_scheme",
            return_value=result,
        ):
            runner._execute_candidate(
                summary,
                cfg,
                predict_date="2026-08-20",
                algo_env="forecast_env",
                scheduled_control_plane="launchd_one_shot",
                scheduled_execution_context=object(),
            )
        runner._finalize(summary)

        self.assertEqual(
            summary.skipped,
            [
                {
                    "scheme_id": "not_approved",
                    "code": "execution_skipped",
                }
            ],
        )
        self.assertEqual(
            (summary.outcome, summary.exit_code),
            ("partial", 1),
        )

    def test_failed_candidate_keeps_run_and_write_evidence(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        summary = runner.OneShotPredictionSummary("daily", "2026-08-20")
        cfg = _blackbox_config("failed")
        result = SimpleNamespace(
            scheme_id=cfg.scheme_id,
            status="failed",
            records_written=3,
            error_msg="secret detail",
            run_id=103,
        )
        with patch.object(runner, "execute_scheme", return_value=result):
            runner._execute_candidate(
                summary,
                cfg,
                predict_date="2026-08-20",
                algo_env="forecast_env",
                scheduled_control_plane="launchd_one_shot",
                scheduled_execution_context=object(),
            )

        self.assertEqual(
            summary.failed,
            [
                {
                    "scheme_id": "failed",
                    "run_id": 103,
                    "stage": "execute_scheme",
                    "code": "execution_failed",
                    "records_written": 3,
                }
            ],
        )
        self.assertNotIn("secret detail", repr(summary.failed))


    def test_global_runner_lock_times_out_then_recovers_after_release(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        with tempfile.TemporaryDirectory() as runtime_root:
            config = SimpleNamespace(runtime_root=Path(runtime_root))
            with runner._runner_lock(config, lock_timeout_sec=1):
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "\n".join(
                            (
                                "from pathlib import Path",
                                "from types import SimpleNamespace",
                                "import sys",
                                "from scheduler import "
                                "one_shot_prediction_runner as runner",
                                "config = SimpleNamespace("
                                "runtime_root=Path(sys.argv[1]))",
                                "try:",
                                "    with runner._runner_lock("
                                "config, lock_timeout_sec=0.05):",
                                "        print('unexpected_acquisition')",
                                "except runner.OneShotPredictionLockTimeout as exc:",
                                "    print(exc.code)",
                            )
                        ),
                        runtime_root,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    check=True,
                    text=True,
                    timeout=3,
                )

            with runner._runner_lock(config, lock_timeout_sec=0.2):
                recovered = True

        self.assertEqual(contender.stdout.strip(), "lock_wait_timeout")
        self.assertEqual(contender.stderr, "")
        self.assertTrue(recovered)

    def test_lock_timeout_summary_has_dedicated_failure_code(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        timeout = runner.OneShotPredictionLockTimeout("busy")
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", side_effect=timeout),
        ):
            with self.assertRaises(runner.OneShotPredictionLockTimeout) as caught:
                _run_one_shot(
                    runner,
                    "daily",
                    predict_date="2026-08-28",
                    lock_timeout_sec=0.01,
                )

        self.assertIs(caught.exception.summary, timeout.summary)
        self.assertEqual(
            caught.exception.summary.failed,
            [
                {
                    "scheme_id": "",
                    "run_id": None,
                    "stage": "runner_lock",
                    "code": "lock_wait_timeout",
                    "records_written": 0,
                }
            ],
        )
        self.assertEqual(caught.exception.summary.exit_code, 1)

    def test_lock_rechecks_deadline_after_late_acquisition(self) -> None:
        from scheduler import one_shot_prediction_runner as runner

        with tempfile.TemporaryDirectory() as runtime_root:
            config = SimpleNamespace(runtime_root=Path(runtime_root))
            with (
                patch.object(
                    runner.time,
                    "monotonic",
                    side_effect=[10.0, 10.01, 10.06],
                ),
                patch.object(runner.time, "sleep"),
                patch.object(
                    runner.fcntl,
                    "flock",
                    side_effect=[
                        BlockingIOError(errno.EAGAIN, "busy"),
                        None,
                        None,
                    ],
                ),
            ):
                with self.assertRaises(
                    runner.OneShotPredictionLockTimeout
                ) as caught:
                    with runner._runner_lock(
                        config,
                        lock_timeout_sec=0.05,
                    ):
                        self.fail("expired acquisition must not execute")

        self.assertEqual(caught.exception.code, "lock_wait_timeout")
        self.assertEqual(caught.exception.stage, "runner_lock")

    def test_partial_batch_and_cleanup_errors_preserve_completed_summary(self) -> None:
        from scheduler import launchd_prediction_runner as entry
        from scheduler import one_shot_prediction_runner as runner

        direct = _blackbox_config("completed")
        direct.input_source = "database"
        engine = Mock()
        engine.dispose.side_effect = RuntimeError("dispose failed")
        calendar = _PeriodCalendar("2026-08-01", "2026-08-31")
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(runner, "discover_schemes", return_value=[direct]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(direct,),
            ),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "is_weekly_signal_date", return_value=True),
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id="completed",
                    status="success",
                    records_written=2,
                    run_id=314,
                ),
            ) as execute,
            patch.object(
                runner,
                "_execute_data_bridge_wave",
                side_effect=RuntimeError("dependency failed"),
            ),
        ):
            with self.assertRaises(runner.OneShotPredictionRunError) as caught:
                entry.run(
                    "weekly",
                    predict_date="2026-08-15",
                    algo_env="forecast_env",
                )

        summary = caught.exception.summary
        self.assertEqual(
            summary.executed,
            [
                {
                    "scheme_id": "completed",
                    "status": "success",
                    "records_written": 2,
                    "run_id": 314,
                }
            ],
        )
        self.assertEqual(
            {item["stage"] for item in summary.failed},
            {"batch_dependency", "engine_dispose"},
        )
        self.assertTrue(
            all(
                item["code"] == "dependency_unavailable"
                for item in summary.failed
            )
        )
        self.assertEqual((summary.outcome, summary.exit_code), ("partial", 1))
        execute.assert_called_once()

    def test_host_mains_serialize_carried_partial_summary(self) -> None:
        from scheduler import launchd_prediction_runner
        from scheduler import one_shot_prediction_runner as runner
        from scheduler import systemd_prediction_runner

        summary = runner.OneShotPredictionSummary(
            "daily",
            "2026-08-28",
            executed=[
                {
                    "scheme_id": "completed",
                    "status": "success",
                    "records_written": 1,
                    "run_id": 22,
                }
            ],
            failed=[
                {
                    "scheme_id": "",
                    "run_id": None,
                    "stage": "batch_dependency",
                    "code": "dependency_unavailable",
                    "records_written": 0,
                }
            ],
            outcome="partial",
            exit_code=1,
        )
        error = runner.OneShotPredictionRunError(
            "failed",
            summary=summary,
            code="dependency_unavailable",
            stage="batch_dependency",
        )
        for entry in (launchd_prediction_runner, systemd_prediction_runner):
            with (
                self.subTest(entry=entry.__name__),
                patch.object(entry, "run", side_effect=error),
                patch("builtins.print") as output,
            ):
                exit_code = entry.main(
                    ["--cadence", "daily", "--predict-date", "2026-08-28"]
                )

                payload = json.loads(output.call_args.args[0])
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["executed"][0]["run_id"], 22)
                self.assertEqual(
                    payload["failed"][0]["code"],
                    "dependency_unavailable",
                )


    def test_one_shot_requires_matching_target_before_runtime_access(
        self,
    ) -> None:
        from scheduler import one_shot_prediction_runner as runner

        cases = ({}, {"BFL_DEPLOYMENT_TARGET": "aliyun-gray"})
        for environment in cases:
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                patch.object(
                    runner.DataBridgeRefreshConfig,
                    "from_env",
                ) as data_bridge_config,
                patch.object(
                    runner,
                    "create_engine_from_env",
                ) as create_engine,
            ):
                with self.assertRaisesRegex(
                    runner.OneShotPredictionConfigurationError,
                    "deployment target does not match one-shot control plane",
                ):
                    _run_one_shot(runner,
                        "weekly",
                        predict_date="2026-08-01",
                        algo_env="forecast_env",
                    )

                data_bridge_config.assert_not_called()
                create_engine.assert_not_called()

    def test_executor_rejects_gray_live_before_db_access(
        self,
    ) -> None:
        from scheduler import executor

        cfg = _blackbox_config("gray_live")
        with patch.object(executor, "create_engine_from_env") as create_engine:
            with self.assertRaisesRegex(
                ValueError,
                "execute_scheme only supports scheduled_live",
            ):
                executor.execute_scheme(
                    cfg,
                    "2026-08-08",
                    prediction_phase="gray_live",
                    scheduled_control_plane="launchd_one_shot",
                )

        create_engine.assert_not_called()
