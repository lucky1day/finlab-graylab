from __future__ import annotations

import unittest

from harness.contracts.config_schema import validate_config


def _base_config() -> dict:
    return {
        "scheme_id": "demo_daily",
        "name": "Demo",
        "description": "Demo scheme",
        "horizon": 5,
        "task_type": "T+5",
        "tenors": ["10Y"],
        "frequency": "daily",
        "schedule": {"cron": "25 9 * * 1-5", "timezone": "Asia/Shanghai"},
        "entry_point": "predict.run",
        "status": "paused",
        "input_spec": {
            "data_version": "shared_data_service_daily.v1",
            "required_columns": ["date", "TB0YWI0C"],
        },
    }


class ConfigSchemaAuxiliaryInputTests(unittest.TestCase):
    def test_config_without_auxiliary_inputs_remains_valid(self) -> None:
        config = _base_config()

        errors = validate_config(config, dirname="demo_daily")

        self.assertEqual(errors, [])

    def test_config_with_weekly_and_monthly_auxiliary_inputs_is_valid(self) -> None:
        config = _base_config()
        config["input_spec"]["auxiliary_inputs"] = [
            {
                "frequency": "weekly",
                "data_version": "shared_data_service_weekly.v1",
                "required_columns": ["week_id", "TB0YWI3C"],
            },
            {
                "frequency": "monthly",
                "data_version": "shared_data_service_monthly.v1",
                "required_columns": ["month_id", "M0000001"],
            },
        ]

        errors = validate_config(config, dirname="demo_daily")

        self.assertEqual(errors, [])

    def test_auxiliary_inputs_must_be_non_empty_list_when_present(self) -> None:
        config = _base_config()
        config["input_spec"]["auxiliary_inputs"] = []

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("input_spec.auxiliary_inputs must be a non-empty list when present", errors)

    def test_auxiliary_input_rejects_duplicate_frequency(self) -> None:
        config = _base_config()
        config["input_spec"]["auxiliary_inputs"] = [
            {
                "frequency": "weekly",
                "data_version": "shared_data_service_weekly.v1",
                "required_columns": ["week_id"],
            },
            {
                "frequency": "weekly",
                "data_version": "shared_data_service_weekly.v1",
                "required_columns": ["week_id"],
            },
        ]

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("input_spec.auxiliary_inputs contain duplicate frequency: weekly", errors)

    def test_auxiliary_input_rejects_scheme_frequency(self) -> None:
        config = _base_config()
        config["input_spec"]["auxiliary_inputs"] = [
            {
                "frequency": "daily",
                "data_version": "shared_data_service_daily.v1",
                "required_columns": ["date"],
            }
        ]

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("input_spec.auxiliary_inputs[0].frequency must differ from scheme frequency", errors)

    def test_auxiliary_input_rejects_invalid_fields(self) -> None:
        config = _base_config()
        config["input_spec"]["auxiliary_inputs"] = [
            {
                "frequency": "quarterly",
                "data_version": "",
                "required_columns": [],
            },
            "not-a-mapping",
        ]

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("input_spec.auxiliary_inputs[0].frequency must be one of daily, weekly, monthly", errors)
        self.assertIn("input_spec.auxiliary_inputs[0].data_version must be a non-empty string", errors)
        self.assertIn(
            "input_spec.auxiliary_inputs[0].required_columns must be a non-empty list of strings",
            errors,
        )
        self.assertIn("input_spec.auxiliary_inputs[1] must be a mapping", errors)


class ConfigSchemaScheduleTests(unittest.TestCase):
    def test_schedule_timeout_sec_accepts_positive_integer(self) -> None:
        config = _base_config()
        config["schedule"]["timeout_sec"] = 1800

        errors = validate_config(config, dirname="demo_daily")

        self.assertEqual(errors, [])

    def test_schedule_timeout_sec_rejects_non_positive_integer(self) -> None:
        config = _base_config()
        config["schedule"]["timeout_sec"] = 0

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("schedule.timeout_sec must be a positive integer when present", errors)


class ConfigSchemaBacktestStartTests(unittest.TestCase):
    def test_daily_backtest_requires_start_date_2025_01_01(self) -> None:
        config = _base_config()
        config["backtest"] = {"runner": "backtests.demo"}

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("backtest.start_date must be 2025-01-01 for daily/monthly backtests", errors)

    def test_daily_backtest_start_date_2025_01_01_is_valid(self) -> None:
        config = _base_config()
        config["backtest"] = {
            "runner": "backtests.demo",
            "start_date": "2025-01-01",
            "benchmark_required": True,
        }

        errors = validate_config(config, dirname="demo_daily")

        self.assertEqual(errors, [])

    def test_daily_backtest_runner_args_are_validated(self) -> None:
        config = _base_config()
        config["backtest"] = {
            "runner": "backtests.demo",
            "runner_args": ["--batch-mode", "monthly"],
            "start_date": "2025-01-01",
        }

        errors = validate_config(config, dirname="demo_daily")

        self.assertEqual(errors, [])

    def test_daily_backtest_runner_args_cannot_override_persist_semantics(self) -> None:
        config = _base_config()
        config["backtest"] = {
            "runner": "backtests.demo",
            "runner_args": ["--no-persist"],
            "start_date": "2025-01-01",
        }

        errors = validate_config(config, dirname="demo_daily")

        self.assertIn("backtest.runner_args must not include --no-persist", errors)

    def test_weekly_backtest_requires_predict_start_date_2025_01_01(self) -> None:
        config = _base_config()
        config["scheme_id"] = "demo_weekly"
        config["frequency"] = "weekly"
        config["horizon"] = 6
        config["task_type"] = "weekly_point"
        config["target_rule"] = "next_week_last_trading_day_vs_current_week_last_trading_day"
        config["input_spec"] = {
            "data_version": "shared_data_service_weekly.v1",
            "required_columns": ["week_id", "TB5YWI3C"],
            "weekly_variant": "unified",
        }
        config["backtest"] = {
            "runner": "backtests.demo_weekly",
            "start_week": 200901,
            "end_week": 202622,
        }

        errors = validate_config(config, dirname="demo_weekly")

        self.assertIn("backtest.predict_start_date must be 2025-01-01 for weekly backtests", errors)

    def test_weekly_backtest_predict_start_date_keeps_historical_start_week_valid(self) -> None:
        config = _base_config()
        config["scheme_id"] = "demo_weekly"
        config["frequency"] = "weekly"
        config["horizon"] = 6
        config["task_type"] = "weekly_point"
        config["target_rule"] = "next_week_last_trading_day_vs_current_week_last_trading_day"
        config["input_spec"] = {
            "data_version": "shared_data_service_weekly.v1",
            "required_columns": ["week_id", "TB5YWI3C"],
            "weekly_variant": "unified",
        }
        config["backtest"] = {
            "runner": "backtests.demo_weekly",
            "predict_start_date": "2025-01-01",
            "start_week": 200901,
            "end_week": 202622,
        }

        errors = validate_config(config, dirname="demo_weekly")

        self.assertEqual(errors, [])

    def test_monthly_scheme_requires_target_rule(self) -> None:
        config = _base_config()
        config["scheme_id"] = "demo_monthly"
        config["frequency"] = "monthly"
        config["task_type"] = "monthly"
        config["horizon"] = 30
        config["input_spec"] = {
            "data_version": "shared_data_service_monthly.v1",
            "required_columns": ["month_id"],
        }
        config["backtest"] = {
            "runner": "backtests.demo_monthly",
            "start_date": "2025-01-01",
        }

        errors = validate_config(config, dirname="demo_monthly")

        self.assertIn("target_rule is required for weekly/monthly schemes", errors)


if __name__ == "__main__":
    unittest.main()
