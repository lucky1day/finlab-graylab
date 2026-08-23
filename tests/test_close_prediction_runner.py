from __future__ import annotations

import argparse
from datetime import date, timedelta
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(scheme_id: str, task_type: str, frequency: str) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        status="active",
        task_type=task_type,
        frequency=frequency,
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
    )


class _Calendar:
    def __init__(self, start: str, end: str) -> None:
        current = date.fromisoformat(start)
        final = date.fromisoformat(end)
        self.rows = []
        while current <= final:
            self.rows.append(
                {"rdate": current.isoformat(), "trade_flag": "1"}
            )
            current += timedelta(days=1)

    def covers(self, value: str) -> bool:
        return self.rows[0]["rdate"] <= value <= self.rows[-1]["rdate"]

    def period_calendar_rows(self):
        return tuple(self.rows)

    def is_trading_day(self, value: str) -> bool:
        return date.fromisoformat(value).weekday() < 5

    def previous_trading_day(self, value: str) -> str:
        current = date.fromisoformat(value) - timedelta(days=1)
        while current.weekday() >= 5:
            current -= timedelta(days=1)
        return current.isoformat()


def _summary(cadence: str, exit_code: int = 0):
    return SimpleNamespace(
        to_payload=lambda: {
            "cadence": cadence,
            "exit_code": exit_code,
            "outcome": "success" if exit_code == 0 else "partial",
        }
    )


def test_deployed_script_entrypoint_resolves_project_packages() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_close_predictions.py"),
            "--help",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_close_job_does_nothing_when_no_task_is_due() -> None:
    from scripts import run_close_predictions as runner

    engine = Mock()
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ),
        patch.object(runner, "discover_schemes", return_value=[]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner,
            "get_calendar",
            return_value=_Calendar("2024-01-01", "2024-06-30"),
        ),
        patch.object(runner, "refresh_data_bridge") as refresh,
        patch.object(runner, "_runner_for_control_plane") as prediction_runner,
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-28",
            algo_env="forecast_env",
        )

    assert result.outcome == "not_applicable"
    assert not result.refresh_required
    refresh.assert_not_called()
    prediction_runner.assert_not_called()
    engine.dispose.assert_called_once()


def test_close_job_refreshes_once_then_runs_due_period_task() -> None:
    from scripts import run_close_predictions as runner

    engine = Mock()
    run_prediction = Mock(return_value=_summary("period_average"))
    quarterly = _config("quarterly", "quarterly_average", "quarterly")
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ),
        patch.object(runner, "discover_schemes", return_value=[quarterly]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner,
            "get_calendar",
            return_value=_Calendar("2024-01-01", "2024-06-30"),
        ),
        patch.object(
            runner,
            "refresh_data_bridge",
            return_value=(0, {"status": "ok"}),
        ) as refresh,
        patch.object(runner, "_is_current_ready", return_value=False),
        patch.object(
            runner,
            "_runner_for_control_plane",
            return_value=run_prediction,
        ),
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-29",
            algo_env="forecast_env",
            refresh_start="18:00",
            refresh_deadline="18:55",
        )

    refresh.assert_called_once_with(
        "publish",
        refresh_date="2024-03-29",
        expected_feature_date="2024-03-29",
        refresh_start="18:00",
        refresh_deadline="18:55",
    )
    run_prediction.assert_called_once_with(
        "period_average",
        predict_date="2024-03-29",
        algo_env="forecast_env",
    )
    assert result.outcome == "success"


def test_close_job_rejects_partial_refresh_window_before_runtime_access() -> None:
    from scripts import run_close_predictions as runner

    with (
        patch.object(runner, "discover_schemes") as discover,
        pytest.raises(ValueError, match="requires both start and deadline"),
    ):
        runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-29",
            algo_env="forecast_env",
            refresh_start="18:00",
        )

    discover.assert_not_called()


@pytest.mark.parametrize("value", ["6:30", "24:00", "18:55:00", "bad"])
def test_refresh_clock_requires_strict_hour_and_minute(value: str) -> None:
    from scripts import run_close_predictions as runner

    with pytest.raises(argparse.ArgumentTypeError):
        runner._refresh_clock(value)


def test_trading_day_15_refreshes_once_then_runs_monthly_and_period() -> None:
    from scripts import run_close_predictions as runner

    engine = Mock()
    monthly = _config("monthly_point", "monthly", "monthly")
    monthly_average = _config(
        "monthly_average",
        "monthly_average",
        "monthly",
    )
    run_prediction = Mock(
        side_effect=[_summary("monthly"), _summary("period_average")]
    )
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ),
        patch.object(
            runner,
            "discover_schemes",
            return_value=[monthly, monthly_average],
        ),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner,
            "get_calendar",
            return_value=_Calendar("2024-01-01", "2024-04-30"),
        ),
        patch.object(
            runner,
            "refresh_data_bridge",
            return_value=(0, {"status": "ok"}),
        ) as refresh,
        patch.object(runner, "_is_current_ready", return_value=False),
        patch.object(
            runner,
            "_runner_for_control_plane",
            return_value=run_prediction,
        ),
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-15",
            algo_env="forecast_env",
        )

    refresh.assert_called_once()
    assert [call.args[0] for call in run_prediction.call_args_list] == [
        "monthly",
        "period_average",
    ]
    assert len(result.cadences) == 2


def test_close_job_does_not_predict_after_refresh_failure() -> None:
    from scripts import run_close_predictions as runner

    engine = Mock()
    quarterly = _config("quarterly", "quarterly_average", "quarterly")
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ),
        patch.object(runner, "discover_schemes", return_value=[quarterly]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner,
            "get_calendar",
            return_value=_Calendar("2024-01-01", "2024-06-30"),
        ),
        patch.object(
            runner,
            "refresh_data_bridge",
            return_value=(1, {"status": "failed"}),
        ),
        patch.object(runner, "_is_current_ready", return_value=False),
        patch.object(runner, "_runner_for_control_plane") as prediction_runner,
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-29",
            algo_env="forecast_env",
        )

    assert result.outcome == "refresh_failed"
    assert result.exit_code == 1
    prediction_runner.assert_not_called()


def test_close_job_reuses_matching_ready_snapshot_without_refresh() -> None:
    from scripts import run_close_predictions as runner

    engine = Mock()
    monthly = _config("monthly_point", "monthly", "monthly")
    run_prediction = Mock(return_value=_summary("monthly"))
    with (
        patch.dict(
            os.environ,
            {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
            clear=False,
        ),
        patch.object(runner, "discover_schemes", return_value=[monthly]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner,
            "get_calendar",
            return_value=_Calendar("2024-01-01", "2024-03-31"),
        ),
        patch.object(runner, "_is_current_ready", return_value=True),
        patch.object(runner, "refresh_data_bridge") as refresh,
        patch.object(
            runner,
            "_runner_for_control_plane",
            return_value=run_prediction,
        ),
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-02-15",
            algo_env="forecast_env",
        )

    refresh.assert_not_called()
    run_prediction.assert_called_once()
    assert result.refresh_status == "already_ready"
