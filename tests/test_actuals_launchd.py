"""独立 actuals LaunchAgent 配置测试。"""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLIST_PATH = (
    PROJECT_ROOT
    / "deploy"
    / "launchd"
    / "com.bond-factor-lab.actuals.plist"
)


class ActualsLaunchdTests(unittest.TestCase):
    def test_actuals_job_runs_existing_one_shot_at_three_refresh_times(
        self,
    ) -> None:
        self.assertTrue(
            PLIST_PATH.is_file(),
            "独立 actuals LaunchAgent 配置必须存在",
        )
        with PLIST_PATH.open("rb") as handle:
            config = plistlib.load(handle)

        self.assertEqual(config["Label"], "com.bond-factor-lab.actuals")
        self.assertEqual(
            config["WorkingDirectory"],
            "/Users/macstudio0/bond-factor-lab",
        )
        self.assertEqual(
            config["ProgramArguments"],
            [
                "/Users/macstudio0/miniconda3/bin/conda",
                "run",
                "--no-capture-output",
                "-n",
                "bond_factor_lab_service",
                "python",
                "-m",
                "scheduler.actuals_runner",
            ],
        )
        self.assertEqual(
            {
                (item["Hour"], item["Minute"])
                for item in config["StartCalendarInterval"]
            },
            {(8, 30), (19, 0), (23, 45)},
        )
        self.assertFalse(config["RunAtLoad"])
        self.assertNotIn("KeepAlive", config)
        self.assertEqual(
            config["EnvironmentVariables"],
            {
                "PYTHONNOUSERSITE": "1",
            },
        )

    def test_actuals_one_writer_template_has_not_been_retimed_or_disabled(self) -> None:
        with PLIST_PATH.open("rb") as handle:
            config = plistlib.load(handle)

        self.assertNotIn("Disabled", config)
        self.assertEqual(
            {
                (item["Hour"], item["Minute"])
                for item in config["StartCalendarInterval"]
            },
            {(8, 30), (19, 0), (23, 45)},
        )


if __name__ == "__main__":
    unittest.main()
