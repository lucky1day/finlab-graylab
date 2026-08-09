from __future__ import annotations

import importlib.util
import inspect
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
        legacy_mode=legacy_mode,
        capabilities=capabilities,
    )


class LaunchdPredictionRunnerTests(unittest.TestCase):
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
            patch.object(
                runner.DataBridgeRefreshConfig,
                "from_env",
                return_value=object(),
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[formal, gray, paused, monthly],
            ),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=Mock()),
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
