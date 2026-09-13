from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from scheduler.discovery import SchemeConfig, SchemeSchedule, load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(scheme_id: str, task_type: str, frequency: str) -> SchemeConfig:
    return SchemeConfig(
        scheme_id=scheme_id,
        name=scheme_id,
        description="",
        horizon=1,
        tenors=["10Y"],
        schedule=SchemeSchedule(cron="0 18 * * *"),
        status="active",
        task_type=task_type,
        frequency=frequency,
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        path=PROJECT_ROOT / "schemes" / scheme_id,
        code_hash="c" * 64,
        config_hash="f" * 64,
        manifest_hash=None,
        scheme_version="test-exact-version",
        version_status="active",
        algorithm_version="1.0.0",
        contract_version="1.0",
        runtime_profile="blackbox-v2-v1",
        data_schema_version="data-bridge-v1",
        target_rule=None,
        delivery_script=None,
        delivery_metadata=None,
        environment_fingerprint=None,
        data_snapshot_id=None,
    )


def _lifecycle_engine(*configs: SchemeConfig) -> MagicMock:
    engine = MagicMock()
    engine.version_rows = [
        {
            "scheme_id": cfg.scheme_id,
            "scheme_version": cfg.scheme_version,
            "runtime_type": cfg.runtime_type,
            "status": "active",
            "approved_by": "test-operator",
            "approved_at": datetime(2024, 1, 1),
        }
        for cfg in configs
    ]
    engine.registry_rows = [
        {
            "scheme_id": f"{cfg.scheme_id}__h{cfg.horizon}__{tenor}",
            "base_scheme_id": cfg.scheme_id,
            "runtime_type": cfg.runtime_type,
            "status": "active",
            "task_type": cfg.task_type,
            "target_tenor": tenor,
            "horizon": cfg.horizon,
        }
        for cfg in configs
        for tenor in cfg.tenors
    ]

    def execute(statement, params):
        sql = str(statement)
        assert sql.lstrip().startswith("SELECT")
        if "FROM t_scheme_versions" in sql:
            rows, key = engine.version_rows, "scheme_id"
        else:
            assert "FROM t_scheme_registry" in sql
            rows, key = engine.registry_rows, "base_scheme_id"
        result = Mock()
        result.mappings.return_value.all.return_value = [
            row for row in rows if row[key] in params.values()
        ]
        return result

    engine.begin.return_value.__enter__.return_value.execute.side_effect = execute
    return engine


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


def test_trading_day_15_refreshes_once_then_runs_monthly_and_period() -> None:
    from scripts import run_close_predictions as runner

    monthly = _config("monthly_point", "monthly", "monthly")
    monthly_average = _config(
        "monthly_average",
        "monthly_average",
        "monthly",
    )
    engine = _lifecycle_engine(monthly, monthly_average)
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

    quarterly = _config("quarterly", "quarterly_average", "quarterly")
    engine = _lifecycle_engine(quarterly)
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

    monthly = _config("monthly_point", "monthly", "monthly")
    engine = _lifecycle_engine(monthly)
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


@pytest.mark.parametrize(
    ("control_plane", "deployment_target"),
    [("launchd", "mac3-production"), ("systemd", "aliyun-gray")],
)
@pytest.mark.parametrize(
    ("task_type", "frequency", "predict_date", "cadence"),
    [
        ("monthly", "monthly", "2024-03-15", "monthly"),
        ("quarterly_average", "quarterly", "2024-03-29", "period_average"),
    ],
)
@pytest.mark.parametrize("database_status", ["active", "retired", "missing"])
def test_close_job_uses_host_lifecycle_before_refresh_and_dispatch(
    control_plane: str,
    deployment_target: str,
    task_type: str,
    frequency: str,
    predict_date: str,
    cadence: str,
    database_status: str,
) -> None:
    from scripts import run_close_predictions as runner

    cfg = replace(
        _config("close_task", task_type, frequency),
        status="paused" if database_status == "active" else "active",
        version_status="draft" if database_status == "active" else "active",
    )
    daily = _config("unrelated_daily", "T+1", "daily")
    engine = _lifecycle_engine(cfg, daily)
    # 不相关 cadence 的冲突身份不能阻塞 close 入口。
    engine.version_rows[1]["runtime_type"] = "native_adapter"
    if database_status == "missing":
        engine.version_rows.pop(0)
    else:
        engine.version_rows[0]["status"] = database_status
    run_prediction = Mock(return_value=_summary(cadence))
    with (
        patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": deployment_target}),
        patch.object(runner, "discover_schemes", return_value=[cfg, daily]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner, "get_calendar", return_value=_Calendar("2024-01-01", "2024-06-30")
        ),
        patch.object(runner, "_is_current_ready", return_value=False) as ready,
        patch.object(
            runner, "refresh_data_bridge", return_value=(0, {"status": "ok"})
        ) as refresh,
        patch.object(
            runner, "_runner_for_control_plane", return_value=run_prediction
        ) as select_runner,
    ):
        result = runner.run_close_job(
            control_plane=control_plane,
            predict_date=predict_date,
            algo_env="forecast_env",
        )

    if database_status == "active":
        assert result.outcome == "success"
        assert result.refresh_required
        refresh.assert_called_once_with(
            "publish", refresh_date=predict_date, expected_feature_date=predict_date
        )
        select_runner.assert_called_once_with(control_plane)
        run_prediction.assert_called_once_with(
            cadence, predict_date=predict_date, algo_env="forecast_env"
        )
    else:
        assert result.outcome == "not_applicable"
        assert not result.refresh_required
        ready.assert_not_called()
        refresh.assert_not_called()
        select_runner.assert_not_called()
    assert cfg.status == ("paused" if database_status == "active" else "active")
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("failure", ["registry_identity", "version_read", "registry_read"])
def test_close_lifecycle_failure_blocks_side_effects_and_reports_error(
    failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import run_close_predictions as runner

    monthly = _config("monthly", "monthly", "monthly")
    period = replace(
        _config("period", "monthly_average", "monthly"),
        status="paused",
        version_status="draft",
    )
    engine = _lifecycle_engine(monthly, period)
    if failure == "registry_identity":
        engine.registry_rows[1]["task_type"] = "T+1"
    else:
        execute = engine.begin.return_value.__enter__.return_value.execute
        read = execute.side_effect
        failed_table = (
            "t_scheme_versions" if failure == "version_read" else "t_scheme_registry"
        )

        def fail_read(statement, params):
            if failed_table in str(statement):
                raise RuntimeError("database read failed")
            return read(statement, params)

        execute.side_effect = fail_read
    with (
        patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": "mac3-production"}),
        patch.object(runner, "discover_schemes", return_value=[monthly, period]),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner, "get_calendar", return_value=_Calendar("2024-01-01", "2024-06-30")
        ),
        patch.object(runner, "_is_current_ready") as ready,
        patch.object(runner, "refresh_data_bridge") as refresh,
        patch.object(runner, "_runner_for_control_plane") as select_runner,
    ):
        assert runner.main(
            ["--control-plane", "launchd", "--predict-date", "2024-03-15"]
        ) == 2

    engine.dispose.assert_called_once()
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "configuration_error"
    assert payload["cadences"] == []
    ready.assert_not_called()
    refresh.assert_not_called()
    select_runner.assert_not_called()


@pytest.mark.parametrize("status", ["active", "paused"])
def test_close_job_preserves_w4_native_config_lifecycle(status: str) -> None:
    from scripts import run_close_predictions as runner

    policy = json.loads((PROJECT_ROOT / "deploy/onboarding_policy_v1.json").read_text())
    configs = [
        load_scheme_config(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
        for scheme_id in policy["legacy_native_scheme_ids"]
    ]
    monthly = [
        replace(cfg, status=status) for cfg in configs if cfg.frequency == "monthly"
    ]
    assert monthly
    assert all(cfg.runtime_type == "native_adapter" for cfg in monthly)
    engine = _lifecycle_engine()
    engine.begin.side_effect = AssertionError("Native lifecycle must use config")
    run_prediction = Mock(return_value=_summary("monthly"))
    with (
        patch.dict(os.environ, {"BFL_DEPLOYMENT_TARGET": "mac3-production"}),
        patch.object(runner, "discover_schemes", return_value=monthly),
        patch.object(runner, "create_engine_from_env", return_value=engine),
        patch.object(
            runner, "get_calendar", return_value=_Calendar("2024-01-01", "2024-06-30")
        ),
        patch.object(runner, "_is_current_ready") as ready,
        patch.object(runner, "refresh_data_bridge") as refresh,
        patch.object(
            runner, "_runner_for_control_plane", return_value=run_prediction
        ),
    ):
        result = runner.run_close_job(
            control_plane="launchd",
            predict_date="2024-03-15",
            algo_env="forecast_env",
        )

    assert result.outcome == ("success" if status == "active" else "not_applicable")
    assert not result.refresh_required
    if status == "active":
        run_prediction.assert_called_once_with(
            "monthly", predict_date="2024-03-15", algo_env="forecast_env"
        )
    else:
        run_prediction.assert_not_called()
    ready.assert_not_called()
    refresh.assert_not_called()
    engine.dispose.assert_called_once()
