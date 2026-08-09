from __future__ import annotations

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
    def test_active_weekly_blackboxes_run_regardless_of_legacy_admission(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner
        from scheduler import blackbox_scheduler_admission as legacy_admission

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
            patch.object(
                legacy_admission,
                "load_blackbox_scheduler_admission",
                side_effect=AssertionError("launchd runner queried legacy admission"),
            ) as load_legacy_admission,
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
        load_legacy_admission.assert_not_called()
        self.assertEqual(summary.discovered, 2)
        self.assertEqual(summary.excluded, [])
        self.assertEqual(summary.blocked, [])
        self.assertEqual(summary.outcome, "success")
        self.assertEqual(summary.exit_code, 0)
