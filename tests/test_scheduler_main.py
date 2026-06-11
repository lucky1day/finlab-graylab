"""scheduler.main 调度注册测试。"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import patch


class SchedulerMainTests(unittest.TestCase):
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
