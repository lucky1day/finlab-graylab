"""scheduler.main 调度注册测试。"""

from __future__ import annotations

import logging
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


def _cfg(
    scheme_id: str,
    *,
    frequency: str = "daily",
    cron: str = "3 7 * * 1-5",
    status: str = "active",
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        status=status,
        frequency=frequency,
        tenors=["5Y"],
        schedule=SimpleNamespace(cron=cron, timezone="Asia/Shanghai"),
    )


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

    def test_scheduler_staggers_prediction_jobs_with_same_base_cron(self) -> None:
        from scheduler import main as scheduler_main

        schemes = [
            _cfg("scheme_c"),
            _cfg("scheme_a"),
            _cfg("scheme_b"),
            _cfg("paused_scheme", status="paused"),
        ]
        with (
            patch.dict(os.environ, {"BOND_SCHEDULER_STAGGER_MINUTES": "2"}),
            patch.object(scheduler_main, "discover_schemes", return_value=schemes),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
            self.assertLogs(scheduler_main.logger, level=logging.INFO) as logs,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_jobs = sorted(
                (job.id, str(job.trigger))
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual([job_id for job_id, _ in prediction_jobs], [
            "predict:scheme_a",
            "predict:scheme_b",
            "predict:scheme_c",
        ])
        self.assertTrue(any("hour='7'" in trigger and "minute='3'" in trigger for _, trigger in prediction_jobs))
        self.assertTrue(any("hour='7'" in trigger and "minute='5'" in trigger for _, trigger in prediction_jobs))
        self.assertTrue(any("hour='7'" in trigger and "minute='7'" in trigger for _, trigger in prediction_jobs))
        self.assertTrue(
            any("Scheduled scheme scheme_b at 3 7 * * 1-5 -> 5 7 * * 1-5" in msg for msg in logs.output)
        )

    def test_zero_stagger_keeps_original_prediction_cron(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(os.environ, {"BOND_SCHEDULER_STAGGER_MINUTES": "0"}),
            patch.object(scheduler_main, "discover_schemes", return_value=[_cfg("scheme_a"), _cfg("scheme_b")]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_triggers = [
                str(job.trigger)
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            ]
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual(len(prediction_triggers), 2)
        self.assertTrue(all("hour='7'" in trigger and "minute='3'" in trigger for trigger in prediction_triggers))

    def test_cron_offset_rolls_minutes_without_crossing_day(self) -> None:
        from scheduler import main as scheduler_main

        self.assertEqual(scheduler_main._offset_cron_expr("58 7 * * 1-5", 5), "3 8 * * 1-5")
        with self.assertRaisesRegex(ValueError, "crosses a natural day"):
            scheduler_main._offset_cron_expr("59 23 * * *", 1)
        with self.assertRaisesRegex(ValueError, "numeric minute and hour"):
            scheduler_main._offset_cron_expr("*/5 7 * * 1-5", 2)

    def test_scheduler_env_int_fails_closed(self) -> None:
        from scheduler import main as scheduler_main

        with patch.dict(os.environ, {"BOND_SCHEDULER_STAGGER_MINUTES": "abc"}):
            with self.assertRaisesRegex(ValueError, "BOND_SCHEDULER_STAGGER_MINUTES"):
                scheduler_main._env_int("BOND_SCHEDULER_STAGGER_MINUTES", 2, min_value=0)
        with patch.dict(os.environ, {"BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY": "0"}):
            with self.assertRaisesRegex(ValueError, "BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY"):
                scheduler_main._env_int("BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY", 1, min_value=1)

    def test_daily_prediction_skips_non_trading_day(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("daily_demo", frequency="daily")
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
            patch.object(scheduler_main, "_is_trading_day", return_value=False) as trading_day,
            patch.object(scheduler_main, "execute_scheme") as execute_scheme,
            self.assertLogs(scheduler_main.logger, level=logging.INFO) as logs,
        ):
            scheduler_main.run_prediction_job("daily_demo", run_date="2026-06-15")

        trading_day.assert_called_once_with("2026-06-15")
        execute_scheme.assert_not_called()
        self.assertTrue(any("Skip daily_demo on non-trading day 2026-06-15" in msg for msg in logs.output))

    def test_weekly_and_monthly_predictions_run_on_non_trading_day(self) -> None:
        from scheduler import main as scheduler_main

        for frequency in ("weekly", "monthly"):
            with self.subTest(frequency=frequency):
                cfg = _cfg(f"{frequency}_demo", frequency=frequency)
                with (
                    patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
                    patch.object(scheduler_main, "_sync_registry", return_value=None),
                    patch.object(scheduler_main, "_is_trading_day", return_value=False) as trading_day,
                    patch.object(scheduler_main, "execute_scheme", return_value="ok") as execute_scheme,
                ):
                    scheduler_main.run_prediction_job(f"{frequency}_demo", run_date="2026-06-15")

                trading_day.assert_not_called()
                execute_scheme.assert_called_once_with(cfg, "2026-06-15", algo_env=scheduler_main.DEFAULT_ALGO_ENV)

    def test_actuals_refresh_registers_morning_evening_and_late_jobs(self) -> None:
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

        self.assertEqual(len(actual_jobs), 3)
        self.assertEqual([job_id for job_id, _ in actual_jobs], ["actuals:0830", "actuals:1900", "actuals:2345"])
        self.assertTrue(any("hour='8'" in trigger and "minute='30'" in trigger for _, trigger in actual_jobs))
        self.assertTrue(any("hour='19'" in trigger and "minute='0'" in trigger for _, trigger in actual_jobs))
        self.assertTrue(any("hour='23'" in trigger and "minute='45'" in trigger for _, trigger in actual_jobs))
        self.assertFalse(any("day_of_week='mon-fri'" in trigger for _, trigger in actual_jobs))
        self.assertTrue(
            any("Scheduled actuals refresh at 08:30, 19:00, 23:45 Asia/Shanghai" in msg for msg in logs.output)
        )
        self.assertFalse(any("16:00" in msg for msg in logs.output))

    def test_actuals_job_refreshes_daily_weekly_and_monthly_actuals(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "update_actuals", return_value=5) as daily_update,
            patch.object(scheduler_main, "update_weekly_actuals", return_value=3, create=True) as weekly_update,
            patch.object(scheduler_main, "update_monthly_actuals", return_value=2, create=True) as monthly_update,
            self.assertLogs(scheduler_main.logger, level=logging.INFO) as logs,
        ):
            scheduler_main.run_actuals_job("2026-06-22")

        daily_update.assert_called_once_with(end_date="2026-06-22")
        weekly_update.assert_called_once_with(end_date="2026-06-22")
        monthly_update.assert_called_once_with(end_date="2026-06-22")
        self.assertTrue(
            any(
                "Actuals refresh finished: date=2026-06-22 daily_weekly_end_date=2026-06-22 "
                "daily_records=5 weekly_records=3 monthly_records=2" in msg
                for msg in logs.output
            )
        )

    def test_actuals_job_refreshes_daily_weekly_to_previous_trading_day_on_non_trading_day(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(scheduler_main, "_is_trading_day", return_value=False),
            patch.object(scheduler_main, "_previous_trading_day", return_value="2026-08-14") as previous_trading_day,
            patch.object(scheduler_main, "update_actuals", return_value=5) as daily_update,
            patch.object(scheduler_main, "update_weekly_actuals", return_value=3, create=True) as weekly_update,
            patch.object(scheduler_main, "update_monthly_actuals", return_value=2, create=True) as monthly_update,
            self.assertLogs(scheduler_main.logger, level=logging.INFO) as logs,
        ):
            scheduler_main.run_actuals_job("2026-08-15")

        previous_trading_day.assert_called_once_with("2026-08-15")
        daily_update.assert_called_once_with(end_date="2026-08-14")
        weekly_update.assert_called_once_with(end_date="2026-08-14")
        monthly_update.assert_called_once_with(end_date="2026-08-15")
        self.assertTrue(
            any(
                "Refresh daily/weekly actuals to previous trading day 2026-08-14 on non-trading day 2026-08-15"
                in msg
                for msg in logs.output
            )
        )

    def test_startup_prediction_catchup_detects_due_staggered_jobs(self) -> None:
        from scheduler import main as scheduler_main

        now = datetime(2026, 7, 9, 7, 4, tzinfo=scheduler_main.ASIA_SHANGHAI)
        jobs = scheduler_main._startup_prediction_catchup_due_jobs(
            [_cfg("scheme_a"), _cfg("scheme_b")],
            now=now,
            interval_minutes=2,
        )

        self.assertEqual([job.cfg.scheme_id for job in jobs], ["scheme_a"])

    def test_startup_prediction_catchup_ignores_non_matching_cron_date(self) -> None:
        from scheduler import main as scheduler_main

        saturday = datetime(2026, 7, 11, 9, 0, tzinfo=scheduler_main.ASIA_SHANGHAI)
        jobs = scheduler_main._startup_prediction_catchup_due_jobs(
            [_cfg("daily_demo", cron="3 7 * * 1-5")],
            now=saturday,
            interval_minutes=0,
        )

        self.assertEqual(jobs, [])

    def test_startup_prediction_catchup_runs_only_missing_due_jobs(self) -> None:
        from scheduler import main as scheduler_main

        now = datetime(2026, 7, 9, 7, 6, tzinfo=scheduler_main.ASIA_SHANGHAI)
        schemes = [_cfg("scheme_a"), _cfg("scheme_b")]
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=schemes),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
            patch.object(scheduler_main, "_prediction_run_exists", side_effect=[True, False]) as run_exists,
            patch.object(scheduler_main, "run_prediction_job") as run_prediction_job,
            patch.object(scheduler_main, "create_engine_from_env") as create_engine,
            self.assertLogs(scheduler_main.logger, level=logging.WARNING),
        ):
            engine = create_engine.return_value
            scheduler_main.run_startup_prediction_catchup(now=now, algo_env="forecast_env")

        run_exists.assert_any_call(engine, "scheme_a", "2026-07-09")
        run_exists.assert_any_call(engine, "scheme_b", "2026-07-09")
        run_prediction_job.assert_called_once_with("scheme_b", run_date="2026-07-09", algo_env="forecast_env")
        engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
