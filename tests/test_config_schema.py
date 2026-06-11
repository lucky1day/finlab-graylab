from __future__ import annotations

import unittest

from harness.contracts.config_schema import validate_config


def _base_config() -> dict:
    return {
        "scheme_id": "demo_daily",
        "name": "Demo",
        "description": "Demo scheme",
        "horizon": 5,
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


if __name__ == "__main__":
    unittest.main()
