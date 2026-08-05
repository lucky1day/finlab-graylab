"""actuals 一次性 runner 的独立行为测试。"""
from __future__ import annotations

import importlib
import json
import logging
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ActualsRunnerTests(unittest.TestCase):
    @staticmethod
    def _runner():
        return importlib.import_module("scheduler.actuals_runner")

    def test_trading_date_updates_all_actuals_at_target_date(self) -> None:
        runner = self._runner()
        no_database = Mock(side_effect=AssertionError("runner must not open a database in this test"))
        with (
            patch.object(runner, "create_engine_from_env", no_database),
            patch.object(runner, "_is_trading_day", return_value=True),
            patch.object(runner, "update_actuals", return_value=5) as daily_update,
            patch.object(runner, "update_weekly_actuals", return_value=3) as weekly_update,
            patch.object(runner, "update_monthly_actuals", return_value=2) as monthly_update,
            self.assertLogs(runner.logger, level=logging.INFO) as logs,
        ):
            runner.run_actuals_job(date(2026, 8, 5))

        daily_update.assert_called_once_with(end_date="2026-08-05")
        weekly_update.assert_called_once_with(end_date="2026-08-05")
        monthly_update.assert_called_once_with(end_date="2026-08-05")
        no_database.assert_not_called()
        self.assertTrue(
            any(
                "Actuals refresh finished: date=2026-08-05 "
                "daily_weekly_end_date=2026-08-05 daily_records=5 "
                "weekly_records=3 monthly_records=2" in message
                for message in logs.output
            )
        )

    def test_non_trading_date_uses_previous_day_for_daily_and_weekly_only(self) -> None:
        runner = self._runner()
        no_database = Mock(side_effect=AssertionError("runner must not open a database in this test"))
        with (
            patch.object(runner, "create_engine_from_env", no_database),
            patch.object(runner, "_is_trading_day", return_value=False),
            patch.object(runner, "_previous_trading_day", return_value="2026-08-14") as previous_day,
            patch.object(runner, "update_actuals", return_value=5) as daily_update,
            patch.object(runner, "update_weekly_actuals", return_value=3) as weekly_update,
            patch.object(runner, "update_monthly_actuals", return_value=2) as monthly_update,
            self.assertLogs(runner.logger, level=logging.INFO) as logs,
        ):
            runner.run_actuals_job("2026-08-15")

        previous_day.assert_called_once_with("2026-08-15")
        daily_update.assert_called_once_with(end_date="2026-08-14")
        weekly_update.assert_called_once_with(end_date="2026-08-14")
        monthly_update.assert_called_once_with(end_date="2026-08-15")
        no_database.assert_not_called()
        self.assertTrue(
            any(
                "Refresh daily/weekly actuals to previous trading day "
                "2026-08-14 on non-trading day 2026-08-15" in message
                for message in logs.output
            )
        )

    def test_cli_accepts_date_and_force(self) -> None:
        runner = self._runner()
        with patch.object(runner, "run_actuals_job") as run_actuals_job:
            exit_code = runner.main(["--date", "2026-08-15", "--force"])

        self.assertEqual(exit_code, 0)
        run_actuals_job.assert_called_once_with(
            run_date="2026-08-15",
            force=True,
        )

    def test_cli_returns_two_for_invalid_date_value_error(self) -> None:
        runner = self._runner()
        no_database = Mock(side_effect=AssertionError("CLI must reject the date before database access"))
        daily_update = Mock(side_effect=AssertionError("CLI must reject the date before daily update"))
        weekly_update = Mock(side_effect=AssertionError("CLI must reject the date before weekly update"))
        monthly_update = Mock(side_effect=AssertionError("CLI must reject the date before monthly update"))
        with patch.object(
            runner,
            "create_engine_from_env",
            no_database,
        ), patch.object(
            runner,
            "update_actuals",
            daily_update,
        ), patch.object(
            runner,
            "update_weekly_actuals",
            weekly_update,
        ), patch.object(
            runner,
            "update_monthly_actuals",
            monthly_update,
        ):
            exit_code = runner.main(["--date", "not-a-date"])

        self.assertEqual(exit_code, 2)
        no_database.assert_not_called()
        daily_update.assert_not_called()
        weekly_update.assert_not_called()
        monthly_update.assert_not_called()

    def test_clean_import_does_not_load_legacy_scheduler_or_apscheduler(self) -> None:
        script = "\n".join(
            (
                "import importlib",
                "import json",
                "import sys",
                f"sys.path.insert(0, {str(PROJECT_ROOT)!r})",
                'importlib.import_module("scheduler.actuals_runner")',
                "print(json.dumps(sorted(name for name in sys.modules if "
                'name == "scheduler.main" or name.startswith("apscheduler"))))',
            )
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
