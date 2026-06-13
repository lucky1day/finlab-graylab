"""scheduler.main 调度注册测试。"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class SchedulerMainTests(unittest.TestCase):
    def test_scheduler_jobs_are_registered_once_per_base_scheme_not_per_tenor(self) -> None:
        from scheduler import main as scheduler_main

        cfg = SimpleNamespace(
            scheme_id="t5_daily",
            status="active",
            frequency="daily",
            tenors=["3Y", "5Y", "7Y", "10Y"],
            schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
        )
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_jobs = [job.id for job in scheduler.get_jobs() if job.id.startswith("predict:")]
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual(prediction_jobs, ["predict:t5_daily"])

    def test_actuals_refresh_registers_morning_and_evening_jobs(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
            self.assertLogs(scheduler_main.logger, level=logging.INFO) as logs,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            actual_jobs = sorted(
                (job.id, str(job.trigger))
                for job in scheduler.get_jobs()
                if job.id.startswith("actuals")
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual(len(actual_jobs), 2)
        self.assertEqual([job_id for job_id, _ in actual_jobs], ["actuals:0830", "actuals:1900"])
        self.assertTrue(any("hour='8'" in trigger and "minute='30'" in trigger for _, trigger in actual_jobs))
        self.assertTrue(any("hour='19'" in trigger and "minute='0'" in trigger for _, trigger in actual_jobs))
        self.assertFalse(any("day_of_week='mon-fri'" in trigger for _, trigger in actual_jobs))
        self.assertTrue(
            any("Scheduled actuals refresh at 08:30 and 19:00 Asia/Shanghai" in msg for msg in logs.output)
        )
        self.assertFalse(any("16:00" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()
