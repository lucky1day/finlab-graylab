from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.models import PredictionRecord


def _blackbox_config(
    scheme_id: str,
    *,
    frequency: str = "weekly",
    status: str = "active",
    task_type: str = "weekly_point",
    legacy_mode: str = "gray",
    capabilities: frozenset[str] = frozenset(),
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
        legacy_mode=legacy_mode,
        capabilities=capabilities,
    )


class _WeeklyCalendar:
    """周频到期判定需要真实交易日；2026-08-01 是关闭了新 feature 周的周六。"""

    def covers(self, value: str) -> bool:
        return value == "2026-08-01"

    def previous_trading_day(self, value: str) -> str:
        return "2026-07-31"

    def is_trading_day(self, value: str) -> bool:
        return True


class LaunchdPredictionRunnerTests(unittest.TestCase):
    def test_blackbox_completion_propagates_duplicate_skip(self) -> None:
        from scheduler import executor

        cfg = _blackbox_config("duplicate_completion")
        engine = Mock()
        record = PredictionRecord(
            scheme_id=cfg.scheme_id,
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-08-01",
            feature_date="2026-07-31",
            target_date="2026-08-07",
            predicted_direction=1,
            extra={"data_snapshot_id": "snapshot-1"},
        )
        with (
            patch.object(
                executor,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                executor,
                "read_blackbox_execution_approval",
                return_value=SimpleNamespace(executable=True),
            ),
            patch.object(
                executor,
                "_active_registry_targets",
                return_value={("10Y", 1)},
            ),
            patch.object(executor, "create_scheme_run", return_value=101),
            patch.object(
                executor,
                "run_configured_scheme",
                return_value=[record],
            ),
            patch.object(executor, "attach_run_data_snapshot"),
            patch.object(
                executor,
                "complete_approved_blackbox_run",
                return_value=(
                    "skipped",
                    0,
                    "prediction_keys_already_exist",
                ),
            ) as complete_run,
            patch(
                "shared.blackbox_v2.lifecycle.assert_lifecycle_clear"
            ),
        ):
            result = executor.execute_scheme(
                cfg,
                "2026-08-01",
                prediction_phase="gray_live",
            )

        self.assertEqual(result.scheme_id, cfg.scheme_id)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.records_written, 0)
        self.assertEqual(
            result.error_msg,
            "prediction_keys_already_exist",
        )
        self.assertEqual(result.run_id, 101)
        complete_run.assert_called_once()
        self.assertNotIn(
            "insert_only_predictions",
            complete_run.call_args.kwargs,
        )

    def test_duplicate_prediction_skip_is_benign_but_visible(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        summary = runner.LaunchdPredictionSummary("daily", "2026-08-20")
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
        runner._finalize(summary, configuration_error=False)

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
        from scheduler import launchd_prediction_runner as runner

        cases = (
            (
                "activation failed: scheme_version=secret-version "
                "db_status=paused",
                "execution_skipped",
            ),
            (None, "execution_skipped"),
        )
        for error_msg, expected_code in cases:
            with self.subTest(error_msg=error_msg):
                summary = runner.LaunchdPredictionSummary(
                    "daily",
                    "2026-08-20",
                )
                cfg = _blackbox_config("not_approved")
                result = SimpleNamespace(
                    scheme_id=cfg.scheme_id,
                    status="skipped",
                    records_written=0,
                    error_msg=error_msg,
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
                runner._finalize(summary, configuration_error=False)

                self.assertEqual(
                    summary.skipped,
                    [
                        {
                            "scheme_id": "not_approved",
                            "code": expected_code,
                        }
                    ],
                )
                self.assertEqual(
                    (summary.outcome, summary.exit_code),
                    ("partial", 1),
                )

    def test_success_with_duplicate_skip_remains_successful(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        summary = runner.LaunchdPredictionSummary("daily", "2026-08-20")
        executed = _blackbox_config("executed")
        duplicate = _blackbox_config("duplicate")
        results = [
            SimpleNamespace(
                scheme_id=executed.scheme_id,
                status="success",
                records_written=1,
                error_msg=None,
                run_id=103,
            ),
            SimpleNamespace(
                scheme_id=duplicate.scheme_id,
                status="skipped",
                records_written=0,
                error_msg="prediction_keys_already_exist",
                run_id=104,
            ),
        ]
        with patch.object(runner, "execute_scheme", side_effect=results):
            for cfg in (executed, duplicate):
                runner._execute_candidate(
                    summary,
                    cfg,
                    predict_date="2026-08-20",
                    algo_env="forecast_env",
                    scheduled_control_plane="launchd_one_shot",
                    scheduled_execution_context=object(),
                )
        runner._finalize(summary, configuration_error=False)

        self.assertEqual(len(summary.executed), 1)
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual((summary.outcome, summary.exit_code), ("success", 0))

    def test_global_runner_lock_waits_until_current_cadence_releases(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        contender_attempting = threading.Event()
        contender_entered = threading.Event()
        failures: list[BaseException] = []

        with tempfile.TemporaryDirectory() as runtime_root:
            config = SimpleNamespace(runtime_root=Path(runtime_root))
            real_flock = runner.fcntl.flock
            contender: threading.Thread

            def observed_flock(file_descriptor: int, operation: int) -> None:
                if (
                    threading.current_thread() is contender
                    and operation & runner.fcntl.LOCK_EX
                ):
                    contender_attempting.set()
                real_flock(file_descriptor, operation)

            def wait_for_lock() -> None:
                try:
                    with runner._runner_lock(config):
                        contender_entered.set()
                except BaseException as exc:  # pragma: no cover - thread bridge
                    failures.append(exc)
                    contender_entered.set()

            contender = threading.Thread(target=wait_for_lock)
            with runner._runner_lock(config):
                with patch.object(runner.fcntl, "flock", new=observed_flock):
                    contender.start()
                    self.assertTrue(contender_attempting.wait(timeout=3))
                    entered_before_release = contender_entered.wait(timeout=0.2)

            self.assertTrue(contender_entered.wait(timeout=3))
            contender.join(timeout=3)

        self.assertFalse(contender.is_alive())
        if failures:
            raise failures[0]
        self.assertFalse(entered_before_release)
        self.assertTrue(contender_entered.is_set())

    def test_legacy_admission_module_is_retired(self) -> None:
        self.assertIsNone(
            importlib.util.find_spec(
                "scheduler.blackbox_scheduler_admission"
            )
        )

    def test_executor_requires_explicit_prediction_phase(self) -> None:
        from scheduler import executor

        parameter = inspect.signature(
            executor.execute_scheme
        ).parameters["prediction_phase"]
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_one_shot_requires_matching_target_before_runtime_access(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner

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
                    runner.LaunchdPredictionConfigurationError,
                    "deployment target does not match one-shot control plane",
                ):
                    runner.run(
                        "weekly",
                        predict_date="2026-08-01",
                        algo_env="forecast_env",
                    )

                data_bridge_config.assert_not_called()
                create_engine.assert_not_called()

    def test_gray_live_rejects_scheduled_control_plane_before_db_access(
        self,
    ) -> None:
        from scheduler import executor

        cfg = _blackbox_config("gray_live")
        with patch.object(executor, "create_engine_from_env") as create_engine:
            with self.assertRaisesRegex(
                ValueError,
                "scheduled control plane requires scheduled_live",
            ):
                executor.execute_scheme(
                    cfg,
                    "2026-08-08",
                    prediction_phase="gray_live",
                    scheduled_control_plane="launchd_one_shot",
                )

        create_engine.assert_not_called()

    def test_active_weekly_blackboxes_run_regardless_of_legacy_admission(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner

        formal = _blackbox_config(
            "formal_active",
            legacy_mode="formal",
            capabilities=frozenset({"launchd_one_shot"}),
        )
        gray = _blackbox_config(
            "gray_active",
            legacy_mode="gray",
            capabilities=frozenset(),
        )
        paused = _blackbox_config("paused", status="paused")
        monthly = _blackbox_config(
            "monthly",
            frequency="monthly",
            task_type="monthly",
        )

        def execute(config, *_args, **_kwargs):
            return SimpleNamespace(
                scheme_id=config.scheme_id,
                status="success",
                records_written=1,
                run_id=100,
            )

        engine = Mock()
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
                return_value=[formal, gray, paused, monthly],
            ),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=_WeeklyCalendar()),
            patch.object(runner, "execute_scheme", side_effect=execute) as execute_one,
        ):
            summary = runner.run(
                "weekly",
                predict_date="2026-08-01",
                algo_env="forecast_env",
            )

        self.assertEqual(
            [args.args[0] for args in execute_one.call_args_list],
            [formal, gray],
        )
        for args in execute_one.call_args_list:
            self.assertEqual(args.args[1], "2026-08-01")
            self.assertEqual(args.kwargs["algo_env"], "forecast_env")
            self.assertEqual(args.kwargs["prediction_phase"], "scheduled_live")
            self.assertEqual(
                args.kwargs["scheduled_control_plane"],
                "launchd_one_shot",
            )
        self.assertEqual(summary.discovered, 2)
        self.assertEqual(summary.excluded, [])
        self.assertEqual(summary.blocked, [])
        self.assertEqual(summary.outcome, "success")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(
            summary.to_payload()["event"],
            "launchd_prediction_run",
        )
