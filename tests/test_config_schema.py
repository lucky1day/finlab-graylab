from __future__ import annotations

import unittest

from shared.scheme_config_schema import validate_config


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


def _base_blackbox_config() -> dict:
    return {
        "scheme_id": "demo_blackbox",
        "runtime_type": "blackbox_v2",
        "input_source": "data_bridge_current",
        "runtime_profile": "blackbox-v2-v1",
        "data_schema_version": "data-bridge-v1",
        "status": "paused",
        "version_status": "draft",
        "schedule": {
            "cron": "3 7 * * 1-5",
            "timezone": "Asia/Shanghai",
            "timeout_sec": 3600,
        },
        "delivery": {
            "script": "delivery/demo_blackbox.py",
            "metadata": "delivery/demo_blackbox.json",
        },
    }


class ConfigSchemaAuxiliaryInputTests(unittest.TestCase):
    def test_incremental_state_is_a_blackbox_true_only_opt_in(self) -> None:
        for value in (True, False, None, "true", "false", 0, 1, {}, []):
            with self.subTest(runtime="blackbox_v2", value=value):
                config = _base_blackbox_config()
                config["incremental_state"] = value
                errors = validate_config(config, "demo_blackbox")
                if value is True:
                    self.assertEqual(errors, [])
                else:
                    self.assertIn(
                        "incremental_state must be literal true when present",
                        errors,
                    )
            with self.subTest(runtime="native_adapter", value=value):
                config = _base_config()
                config["incremental_state"] = value
                self.assertIn(
                    "incremental_state is only supported for Blackbox V2",
                    validate_config(config, "demo_daily"),
                )

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

    def test_blackbox_schedule_timeout_sec_is_required_and_positive(self) -> None:
        for timeout_sec in (None, 0):
            with self.subTest(timeout_sec=timeout_sec):
                config = _base_blackbox_config()
                if timeout_sec is None:
                    del config["schedule"]["timeout_sec"]
                else:
                    config["schedule"]["timeout_sec"] = timeout_sec

                errors = validate_config(config, dirname="demo_blackbox")

                self.assertIn(
                    "Blackbox V2 schedule.timeout_sec must be a positive integer",
                    errors,
                )


class ConfigSchemaTaskTests(unittest.TestCase):


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
