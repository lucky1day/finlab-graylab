"""launchd-only prediction and DataBridge repository template tests."""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ROOT = PROJECT_ROOT / "deploy" / "launchd"
SERVICE_COMMAND = [
    "/Users/macstudio0/miniconda3/bin/conda",
    "run",
    "--no-capture-output",
    "-n",
    "bond_factor_lab_service",
    "python",
]
WORKING_DIRECTORY = "/Users/macstudio0/bond-factor-lab"
FORBIDDEN_ENVIRONMENT_KEYS = {
    "BOND_DB_DSN",
    "BOND_DB_PASSWORD",
    "BOND_DB_USER",
    "SOURCE_DB_PASSWORD",
    "SOURCE_DB_USER",
    "BOND_ADMIN_TOKEN",
    "BOND_INSTANCE_NONCE",
}


def load_plist(filename: str) -> dict[str, object]:
    with (LAUNCHD_ROOT / filename).open("rb") as handle:
        return plistlib.load(handle)


class PredictionLaunchdTests(unittest.TestCase):
    def assert_one_shot_template(
        self,
        config: dict[str, object],
        *,
        label: str,
        arguments: list[str],
        calendar: object,
    ) -> None:
        self.assertEqual(config["Label"], label)
        self.assertEqual(config["WorkingDirectory"], WORKING_DIRECTORY)
        self.assertEqual(config["ProgramArguments"], SERVICE_COMMAND + arguments)
        self.assertEqual(config["StartCalendarInterval"], calendar)
        self.assertFalse(config["RunAtLoad"])
        self.assertNotIn("KeepAlive", config)
        self.assertIn("StandardOutPath", config)
        self.assertIn("StandardErrorPath", config)
        self.assertNotEqual(
            config["StandardOutPath"], config["StandardErrorPath"]
        )
        environment = config["EnvironmentVariables"]
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["BOND_ALGO_CONDA_ENV"], "forecast_env")
        self.assertTrue(FORBIDDEN_ENVIRONMENT_KEYS.isdisjoint(environment))

    def test_data_bridge_refresh_is_the_0630_launchd_one_shot_publisher(self) -> None:
        config = load_plist("com.bond-factor-lab.data-bridge-refresh.plist")
        self.assert_one_shot_template(
            config,
            label="com.bond-factor-lab.data-bridge-refresh",
            arguments=["scripts/refresh_data_bridge_current.py", "--publish"],
            calendar={"Hour": 6, "Minute": 30},
        )
        self.assertEqual(
            config["EnvironmentVariables"]["BFL_DATABRIDGE_PRODUCER"],
            "launchd-one-shot",
        )

    def test_prediction_cadence_templates_have_separate_natural_calendars(self) -> None:
        cases = (
            (
                "com.bond-factor-lab.daily-predictions.plist",
                "com.bond-factor-lab.daily-predictions",
                "daily",
                [
                    {"Weekday": weekday, "Hour": 7, "Minute": 3}
                    for weekday in range(1, 6)
                ],
            ),
            (
                "com.bond-factor-lab.weekly-predictions.plist",
                "com.bond-factor-lab.weekly-predictions",
                "weekly",
                {"Weekday": 6, "Hour": 11, "Minute": 30},
            ),
            (
                "com.bond-factor-lab.monthly-predictions.plist",
                "com.bond-factor-lab.monthly-predictions",
                "monthly",
                {"Day": 15, "Hour": 18, "Minute": 0},
            ),
        )
        for filename, label, cadence, calendar in cases:
            with self.subTest(cadence=cadence):
                self.assert_one_shot_template(
                    load_plist(filename),
                    label=label,
                    arguments=[
                        "-m",
                        "scheduler.launchd_prediction_runner",
                        "--cadence",
                        cadence,
                    ],
                    calendar=calendar,
                )

    def test_legacy_scheduler_templates_are_disabled_without_calendar_triggers(self) -> None:
        for filename, label in (
            ("com.bond-factor-lab.scheduler.plist", "com.bond-factor-lab.scheduler"),
            ("com.bond-factor-lab.daily-gray.plist", "com.bond-factor-lab.daily-gray"),
            ("com.bond-factor-lab.v2-preflight.plist", "com.bond-factor-lab.v2-preflight"),
        ):
            with self.subTest(label=label):
                config = load_plist(filename)
                self.assertEqual(config["Label"], label)
                self.assertTrue(config["Disabled"])
                self.assertNotIn("StartCalendarInterval", config)
                self.assertNotIn("RunAtLoad", config)
                self.assertNotIn("KeepAlive", config)


if __name__ == "__main__":
    unittest.main()
