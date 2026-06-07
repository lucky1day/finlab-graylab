from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class SchedulerWeeklyForceTests(unittest.TestCase):
    def test_weekly_jobs_force_calendar_check_but_daily_jobs_do_not(self) -> None:
        from scheduler.main import build_scheduler

        def scheme(scheme_id: str, frequency: str, cron: str) -> SimpleNamespace:
            return SimpleNamespace(
                scheme_id=scheme_id,
                status="active",
                frequency=frequency,
                schedule=SimpleNamespace(cron=cron, timezone="Asia/Shanghai"),
            )

        schemes = [
            scheme("t1_daily", "daily", "25 9 * * 1-5"),
            scheme("weekly_10y_d_overlay", "weekly", "30 11 * * 6"),
        ]

        with patch("scheduler.main.discover_schemes", return_value=schemes), patch("scheduler.main._sync_registry"):
            scheduler = build_scheduler()

        pending_jobs = {job.id: job.kwargs for job, _, _ in scheduler._pending_jobs}
        self.assertFalse(pending_jobs["predict:t1_daily"].get("force", False))
        self.assertTrue(pending_jobs["predict:weekly_10y_d_overlay"]["force"])


if __name__ == "__main__":
    unittest.main()
