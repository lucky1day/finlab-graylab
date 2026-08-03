from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


def _cfg(
    scheme_id: str,
    *,
    frequency: str = "daily",
    status: str = "active",
    runtime_type: str = "native_adapter",
    input_source: str = "legacy_db",
    version_status: str = "active",
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-version",
        frequency=frequency,
        status=status,
        runtime_type=runtime_type,
        input_source=input_source,
        version_status=version_status,
        task_type="T+1" if frequency == "daily" else "weekly_point",
        horizon=1,
        tenors=["7Y"],
    )


class _Calendar:
    def __init__(self, *, trading: bool) -> None:
        self.trading = trading
        self.is_trading_day = Mock(return_value=trading)
        self.previous_trading_day = Mock(return_value="2026-07-31")


class LaunchdPredictionRunnerTests(unittest.TestCase):
    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(runtime_root=root / "runtime")

    def _engine(self) -> SimpleNamespace:
        return SimpleNamespace(dispose=Mock())

    def test_discovers_active_matching_cadence_and_runs_with_launchd_plane(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner

        native = _cfg("native_daily")
        blackbox = _cfg(
            "formal_blackbox",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        paused = _cfg("paused_daily", status="paused")
        weekly = _cfg("weekly_native", frequency="weekly")
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=True)

        def execute(config_item, *_args, **_kwargs):
            return SimpleNamespace(
                scheme_id=config_item.scheme_id,
                status="success",
                records_written=1,
                error_msg=None,
                run_id=100 if config_item is native else 101,
            )

        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(
                runner,
                "discover_schemes",
                return_value=[native, blackbox, paused, weekly],
            ) as discover,
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "load_blackbox_scheduler_admission", return_value=object()) as load_policy,
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
            ) as admit,
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(runner, "execute_scheme", side_effect=execute) as execute_one,
        ):
            summary = runner.run(
                "daily",
                predict_date="2026-08-03",
                algo_env="forecast_env",
            )

        discover.assert_called_once_with(strict=True)
        load_policy.assert_called_once_with()
        admit.assert_called_once()
        gate.assert_called_once_with(config, "2026-08-03", "2026-07-31")
        self.assertEqual(
            execute_one.call_args_list,
            [
                call(
                    native,
                    "2026-08-03",
                    algo_env="forecast_env",
                    prediction_phase="scheduled_live",
                    scheduled_control_plane="launchd_one_shot",
                ),
                call(
                    blackbox,
                    "2026-08-03",
                    algo_env="forecast_env",
                    prediction_phase="scheduled_live",
                    scheduled_control_plane="launchd_one_shot",
                ),
            ],
        )
        self.assertEqual(summary.discovered, 2)
        self.assertEqual(summary.outcome, "success")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(
            [item["scheme_id"] for item in summary.executed],
            ["formal_blackbox", "native_daily"],
        )
        engine.dispose.assert_called_once_with()

    def test_unadmitted_7y_is_denied_without_gate_or_execution(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner
        from scheduler.blackbox_scheduler_admission import (
            ScheduledPredictionControlPlaneDenied,
        )

        native = _cfg("native_daily")
        seven_y = _cfg(
            "seven_y_current55_lgbm_001_v2",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=True)

        def admit(candidate, **_kwargs):
            if candidate is seven_y:
                raise ScheduledPredictionControlPlaneDenied("denied")

        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[native, seven_y]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "load_blackbox_scheduler_admission", return_value=object()),
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
                side_effect=admit,
            ),
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id=native.scheme_id,
                    status="success",
                    records_written=1,
                    error_msg=None,
                    run_id=7,
                ),
            ) as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-03")

        self.assertEqual(summary.outcome, "partial")
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(
            summary.denied,
            [{"scheme_id": seven_y.scheme_id, "code": "control_plane_denied"}],
        )
        gate.assert_not_called()
        execute_one.assert_called_once_with(
            native,
            "2026-08-03",
            algo_env=runner.DEFAULT_ALGO_ENV,
            prediction_phase="scheduled_live",
            scheduled_control_plane="launchd_one_shot",
        )

    def test_global_lock_conflict_has_no_discovery_or_execution(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        config = self._config(Path(tempfile.mkdtemp()))
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(False)),
            patch.object(runner, "discover_schemes") as discover,
            patch.object(runner, "execute_scheme") as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-03")

        self.assertEqual(summary.outcome, "lock_conflict")
        self.assertEqual(summary.exit_code, 1)
        discover.assert_not_called()
        execute_one.assert_not_called()

    def test_unadmitted_blackbox_never_opens_calendar_engine(self) -> None:
        from scheduler import launchd_prediction_runner as runner
        from scheduler.blackbox_scheduler_admission import (
            ScheduledPredictionControlPlaneDenied,
        )

        blackbox = _cfg(
            "seven_y_current55_lgbm_001_v2",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        config = self._config(Path(tempfile.mkdtemp()))
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[blackbox]),
            patch.object(runner, "load_blackbox_scheduler_admission", return_value=object()),
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
                side_effect=ScheduledPredictionControlPlaneDenied("denied"),
            ),
            patch.object(runner, "create_engine_from_env") as create_engine,
            patch.object(runner, "get_calendar") as get_calendar,
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(runner, "execute_scheme") as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-03")

        self.assertEqual(summary.outcome, "partial")
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(
            summary.denied,
            [{"scheme_id": blackbox.scheme_id, "code": "control_plane_denied"}],
        )
        create_engine.assert_not_called()
        get_calendar.assert_not_called()
        gate.assert_not_called()
        execute_one.assert_not_called()

    def test_daily_non_trading_day_is_not_applicable_without_gate_or_execution(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner

        blackbox = _cfg(
            "formal_blackbox",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=False)
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[blackbox]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(
                runner,
                "load_blackbox_scheduler_admission",
                return_value=object(),
            ) as policy,
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
            ) as admit,
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(runner, "execute_scheme") as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-02")

        self.assertEqual(summary.outcome, "not_applicable")
        self.assertEqual(summary.exit_code, 0)
        policy.assert_called_once_with()
        admit.assert_called_once()
        gate.assert_not_called()
        execute_one.assert_not_called()

    def test_native_data_bridge_input_does_not_consume_blackbox_gate(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        native = _cfg("native_daily", input_source="data_bridge_current")
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=True)
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[native]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id=native.scheme_id,
                    status="success",
                    records_written=1,
                    error_msg=None,
                    run_id=12,
                ),
            ) as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-03")

        self.assertEqual(summary.exit_code, 0)
        gate.assert_not_called()
        execute_one.assert_called_once()

    def test_weekly_saturday_uses_same_day_gate_and_previous_trading_day(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner

        blackbox = _cfg(
            "weekly_10y_lgbm_point_v1",
            frequency="weekly",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=False)
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[blackbox]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "load_blackbox_scheduler_admission", return_value=object()),
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
            ),
            patch.object(runner, "require_v2_daily_ready") as gate,
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id=blackbox.scheme_id,
                    status="success",
                    records_written=1,
                    error_msg=None,
                    run_id=9,
                ),
            ),
        ):
            summary = runner.run("weekly", predict_date="2026-08-01")

        self.assertEqual(summary.exit_code, 0)
        gate.assert_called_once_with(config, "2026-08-01", "2026-07-31")
        calendar.previous_trading_day.assert_called_once_with("2026-08-01")

    def test_gate_blocked_candidate_does_not_prevent_native_execution(
        self,
    ) -> None:
        from scheduler import launchd_prediction_runner as runner
        from scheduler.v2_daily_gate import V2DailyGateBlocked

        native = _cfg("native_daily")
        blackbox = _cfg(
            "formal_blackbox",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        config = self._config(Path(tempfile.mkdtemp()))
        engine = self._engine()
        calendar = _Calendar(trading=True)
        with (
            patch.object(runner.DataBridgeRefreshConfig, "from_env", return_value=config),
            patch.object(runner, "_runner_lock", return_value=nullcontext(True)),
            patch.object(runner, "discover_schemes", return_value=[native, blackbox]),
            patch.object(runner, "create_engine_from_env", return_value=engine),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "load_blackbox_scheduler_admission", return_value=object()),
            patch.object(
                runner,
                "require_scheduled_prediction_control_plane_with_policy_snapshot",
            ),
            patch.object(
                runner,
                "require_v2_daily_ready",
                side_effect=V2DailyGateBlocked("gate missing"),
            ),
            patch.object(
                runner,
                "execute_scheme",
                return_value=SimpleNamespace(
                    scheme_id=native.scheme_id,
                    status="success",
                    records_written=1,
                    error_msg=None,
                    run_id=11,
                ),
            ) as execute_one,
        ):
            summary = runner.run("daily", predict_date="2026-08-03")

        self.assertEqual(summary.outcome, "partial")
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(
            summary.blocked,
            [{"scheme_id": blackbox.scheme_id, "code": "v2_gate_blocked"}],
        )
        execute_one.assert_called_once()

    def test_monthly_non_15th_is_structured_configuration_error(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = runner.main(
                ["--cadence", "monthly", "--predict-date", "2026-08-14"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["outcome"], "configuration_error")
        self.assertEqual(payload["cadence"], "monthly")
        self.assertEqual(payload["predict_date"], "2026-08-14")


if __name__ == "__main__":
    unittest.main()
