"""scheduler.main 调度注册测试。"""

from __future__ import annotations

import io
import json
import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from scheduler.executor import SchemeRunResult
from scheduler.v2_daily_gate import V2DailyGateBlocked


def _cfg(
    scheme_id: str,
    *,
    frequency: str = "daily",
    cron: str = "3 7 * * 1-5",
    status: str = "active",
    runtime_type: str = "native_adapter",
    input_source: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        status=status,
        frequency=frequency,
        runtime_type=runtime_type,
        input_source=input_source,
        tenors=["5Y"],
        schedule=SimpleNamespace(cron=cron, timezone="Asia/Shanghai"),
    )


class SchedulerMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mode_patcher = patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
        )
        self._mode_patcher.start()
        self._capacity_patcher = patch(
            "scheduler.main.require_current_capacity_admission",
            return_value={"status": "ADMITTED"},
        )
        self._capacity_patcher.start()
        self._storage_patcher = patch(
            "scheduler.main.preflight_daily_storage",
        )
        self._storage_patcher.start()

    def tearDown(self) -> None:
        self._storage_patcher.stop()
        self._capacity_patcher.stop()
        self._mode_patcher.stop()

    def test_source_database_preflight_runs_before_registry_sync(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        events: list[str] = []
        database_config = object()

        def record_preflight(config):
            self.assertIs(config, database_config)
            events.append("source-preflight")

        def record_sync(_schemes):
            events.append("registry-sync")

        schemes = [
            _cfg("daily_1y_xgb_1y13_0629"),
            _cfg("daily_native"),
        ]
        with (
            patch.dict(
                os.environ,
                {"BOND_SCHEDULER_STARTUP_CATCHUP": "false"},
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "load_source_runtime_database_config",
                return_value=database_config,
            ),
            patch.object(
                scheduler_main,
                "preflight_source_runtime_database_access",
                side_effect=record_preflight,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                side_effect=record_sync,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            self.assertEqual(
                events[:2],
                ["source-preflight", "registry-sync"],
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def test_source_database_preflight_failure_prevents_registry_sync(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        schemes = [_cfg("daily_1y_xgb_1y13_0629")]
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "load_source_runtime_database_config",
                return_value=object(),
            ),
            patch.object(
                scheduler_main,
                "preflight_source_runtime_database_access",
                side_effect=RuntimeError("SOURCE_DB_GRANT_UNSAFE"),
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
            ) as sync_registry,
            self.assertRaisesRegex(
                RuntimeError,
                "SOURCE_DB_GRANT_UNSAFE",
            ),
        ):
            scheduler_main.build_scheduler()

        sync_registry.assert_not_called()

    def test_source_database_config_failure_is_stable_and_redacted(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from shared.source_runtime_database import (
            SourceRuntimeDatabasePreflightError,
        )

        schemes = [_cfg("daily_1y_xgb_1y13_0629")]
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "load_source_runtime_database_config",
                side_effect=RuntimeError(
                    "source-test-secret must not escape"
                ),
            ),
            patch.object(
                scheduler_main,
                "preflight_source_runtime_database_access",
            ) as preflight,
            patch.object(
                scheduler_main,
                "_sync_registry",
            ) as sync_registry,
            self.assertRaises(
                SourceRuntimeDatabasePreflightError
            ) as caught,
        ):
            scheduler_main.build_scheduler()

        self.assertEqual(
            caught.exception.code,
            "SOURCE_DB_CONFIG_INVALID",
        )
        self.assertNotIn(
            "source-test-secret",
            str(caught.exception),
        )
        preflight.assert_not_called()
        sync_registry.assert_not_called()

    def test_main_preflights_storage_before_scheduler_construction(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        events: list[str] = []

        class _Scheduler:
            def start(self):
                events.append("start")

        with (
            patch.object(
                scheduler_main,
                "preflight_daily_storage",
                side_effect=lambda: events.append("storage-preflight"),
            ),
            patch.object(
                scheduler_main,
                "build_scheduler",
                side_effect=lambda **_kwargs: (
                    events.append("build") or _Scheduler()
                ),
            ),
        ):
            code = scheduler_main.main([])

        self.assertEqual(code, 0)
        self.assertEqual(
            events,
            ["storage-preflight", "build", "start"],
        )

    def test_storage_preflight_failure_prevents_scheduler_construction(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
        )

        with (
            patch.object(
                scheduler_main,
                "preflight_daily_storage",
                side_effect=DailyStoragePreflightError(
                    "DAILY_STORAGE_ROOT_NOT_PRIVATE",
                    label="liwei_cache",
                ),
            ),
            patch.object(
                scheduler_main,
                "build_scheduler",
            ) as build_scheduler,
        ):
            code = scheduler_main.main([])

        self.assertEqual(code, 2)
        build_scheduler.assert_not_called()

    def test_legacy_run_once_writers_preflight_storage_before_work(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        for run_once in ("predictions", "data-refresh"):
            events: list[str] = []
            with (
                self.subTest(run_once=run_once),
                patch.object(
                    scheduler_main,
                    "preflight_daily_storage",
                    side_effect=lambda: events.append("preflight"),
                ),
                patch.object(
                    scheduler_main,
                    "run_all_prediction_jobs",
                    side_effect=lambda **_kwargs: (
                        events.append("predictions") or []
                    ),
                ),
                patch.object(
                    scheduler_main,
                    "run_data_bridge_refresh_job",
                    side_effect=lambda **_kwargs: events.append(
                        "data-refresh"
                    ),
                ),
            ):
                code = scheduler_main.main(
                    ["--run-once", run_once]
                )

            self.assertEqual(code, 0)
            self.assertEqual(events, ["preflight", run_once])

    def test_run_once_storage_failure_blocks_legacy_writers(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
        )

        for run_once in ("predictions", "data-refresh"):
            with (
                self.subTest(run_once=run_once),
                patch.object(
                    scheduler_main,
                    "preflight_daily_storage",
                    side_effect=DailyStoragePreflightError(
                        "DAILY_STORAGE_PATH_UNSAFE",
                        label="daily_runtime",
                    ),
                ),
                patch.object(
                    scheduler_main,
                    "run_all_prediction_jobs",
                ) as predictions,
                patch.object(
                    scheduler_main,
                    "run_data_bridge_refresh_job",
                ) as data_refresh,
            ):
                code = scheduler_main.main(
                    ["--run-once", run_once]
                )

            self.assertEqual(code, 2)
            predictions.assert_not_called()
            data_refresh.assert_not_called()

    def test_ledger_mode_registers_one_daily_coordinator_and_isolates_pools(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        schemes = [
            _cfg("daily_native", frequency="daily"),
            _cfg("weekly_native", frequency="weekly"),
            _cfg("monthly_native", frequency="monthly"),
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "BOND_DAILY_COORDINATOR_MODE": "ledger",
                    "BOND_SCHEDULER_STARTUP_CATCHUP": "false",
                },
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            jobs = {job.id: job for job in scheduler.get_jobs()}
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertIn("daily:coordinator", jobs)
        self.assertEqual(
            jobs["daily:coordinator"].func.__name__,
            "run_daily_coordinator_job",
        )
        self.assertEqual(jobs["daily:coordinator"].executor, "daily_control")
        self.assertIn("hour='6'", str(jobs["daily:coordinator"].trigger))
        self.assertIn("minute='30'", str(jobs["daily:coordinator"].trigger))
        self.assertNotIn("predict:daily_native", jobs)
        self.assertEqual(
            jobs["predict:weekly_native"].executor,
            "recurring_predictions",
        )
        self.assertEqual(
            jobs["predict:monthly_native"].executor,
            "recurring_predictions",
        )
        for job_id in (
            "daily:databridge-readiness:0655",
            "daily:watchdog:0700",
            "daily:v2-guardrail:0745",
        ):
            self.assertIn(job_id, jobs)
            self.assertEqual(
                jobs[job_id].executor,
                "daily_soft_watchdog",
            )
        self.assertEqual(
            jobs["daily:sla:0800"].executor,
            "daily_sla",
        )
        self.assertEqual(
            jobs["daily:recovery-cutoff:0830"].executor,
            "daily_cutoff",
        )
        self.assertIn("daily:heartbeat", jobs)
        self.assertEqual(
            jobs["daily:heartbeat"].func.__name__,
            "run_daily_heartbeat_job",
        )
        self.assertEqual(
            jobs["daily:heartbeat"].executor,
            "daily_heartbeat",
        )
        self.assertIn("0:00:30", str(jobs["daily:heartbeat"].trigger))
        recovery_loop = jobs["daily:recovery-loop"]
        self.assertEqual(
            recovery_loop.func.__name__,
            "run_daily_recovery_tick_job",
        )
        self.assertEqual(recovery_loop.executor, "daily_recovery")
        self.assertEqual(
            len(
                {
                    jobs["daily:heartbeat"].executor,
                    jobs["daily:sla:0800"].executor,
                    jobs["daily:recovery-cutoff:0830"].executor,
                    recovery_loop.executor,
                    jobs["daily:coordinator"].executor,
                }
            ),
            5,
        )
        self.assertEqual(
            recovery_loop.kwargs["algo_env"],
            "forecast_env",
        )
        self.assertIn("hour='6-8'", str(recovery_loop.trigger))
        self.assertIn("minute='1-59/2'", str(recovery_loop.trigger))
        for job_id in ("actuals:0830", "actuals:1900", "actuals:2345"):
            self.assertEqual(jobs[job_id].executor, "actuals")

    def test_recovery_tick_reenters_same_coordinator_only_inside_window(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from zoneinfo import ZoneInfo

        shanghai = ZoneInfo("Asia/Shanghai")
        with patch.object(
            scheduler_main,
            "run_daily_coordinator_job",
            return_value="resumed",
        ) as coordinator, patch.object(
            scheduler_main,
            "run_daily_watchdog_job",
            return_value="cutoff-reconciled",
        ) as watchdog:
            before = scheduler_main.run_daily_recovery_tick_job(
                run_date="2026-07-24",
                now=datetime(2026, 7, 24, 6, 29, tzinfo=shanghai),
            )
            active = scheduler_main.run_daily_recovery_tick_job(
                run_date="2026-07-24",
                now=datetime(2026, 7, 24, 6, 31, tzinfo=shanghai),
            )
            cutoff = scheduler_main.run_daily_recovery_tick_job(
                run_date="2026-07-24",
                now=datetime(2026, 7, 24, 8, 30, tzinfo=shanghai),
            )
            delayed = scheduler_main.run_daily_recovery_tick_job(
                run_date="2026-07-24",
                now=datetime(2026, 7, 24, 8, 31, tzinfo=shanghai),
            )

        self.assertEqual(before["status"], "outside_recovery_window")
        self.assertEqual(active, "resumed")
        self.assertEqual(cutoff, "cutoff-reconciled")
        self.assertEqual(delayed, "cutoff-reconciled")
        coordinator.assert_called_once_with(
            run_date="2026-07-24",
            algo_env="forecast_env",
            trigger_origin="startup_catchup",
        )
        self.assertEqual(watchdog.call_count, 2)
        watchdog.assert_called_with(
            "recovery_cutoff",
            run_date="2026-07-24",
        )

    def test_recovery_tick_does_not_swallow_coordinator_failure(self) -> None:
        from scheduler import main as scheduler_main
        from zoneinfo import ZoneInfo

        with (
            patch.object(
                scheduler_main,
                "run_daily_coordinator_job",
                side_effect=RuntimeError("coordinator failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "coordinator failed"),
        ):
            scheduler_main.run_daily_recovery_tick_job(
                run_date="2026-07-24",
                now=datetime(
                    2026,
                    7,
                    24,
                    7,
                    1,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

    def test_legacy_mode_does_not_register_ledger_heartbeat(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(
                os.environ,
                {
                    "BOND_DAILY_COORDINATOR_MODE": "legacy",
                    "BOND_SCHEDULER_STARTUP_CATCHUP": "false",
                },
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            self.assertIsNone(scheduler.get_job("daily:heartbeat"))
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def test_ledger_mode_startup_catchup_routes_only_through_coordinator(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(
                os.environ,
                {
                    "BOND_DAILY_COORDINATOR_MODE": "ledger",
                    "BOND_SCHEDULER_STARTUP_CATCHUP": "true",
                },
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[_cfg("daily_native")],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            startup = scheduler.get_job(
                "startup:daily-occurrence-catchup"
            )
            legacy = scheduler.get_job(
                "startup:data-refresh-and-prediction-catchup"
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertIsNotNone(startup)
        self.assertEqual(
            startup.func.__name__,
            "run_daily_coordinator_job",
        )
        self.assertEqual(startup.kwargs["trigger_origin"], "startup_catchup")
        self.assertIsNone(legacy)

    def test_coordinator_entry_does_not_grant_process_wide_publish_scope(
        self,
    ) -> None:
        from scheduler import daily_runtime
        from scheduler import main as scheduler_main
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            _require_publish_authority,
        )

        def fake_occurrence(**_kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(DataBridgeRefreshError):
                    pool.submit(
                        _require_publish_authority,
                        publish=True,
                    ).result()
            return "delegated"

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                daily_runtime,
                "run_daily_occurrence",
                side_effect=fake_occurrence,
            ),
        ):
            result = scheduler_main.run_daily_coordinator_job(
                run_date="2026-07-24",
            )
            with self.assertRaises(DataBridgeRefreshError):
                _require_publish_authority(publish=True)

        self.assertEqual(result, "delegated")

    def test_operator_recovery_never_receives_databridge_publish_scope(
        self,
    ) -> None:
        from scheduler import daily_runtime
        from scheduler import main as scheduler_main
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            _require_publish_authority,
        )

        def fake_recovery(**_kwargs):
            with self.assertRaises(DataBridgeRefreshError):
                _require_publish_authority(publish=True)
            return "recovered-without-publication"

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                daily_runtime,
                "run_operator_recovery",
                side_effect=fake_recovery,
            ),
        ):
            result = scheduler_main.run_daily_operator_recovery_job(
                "daily_alpha",
                run_date="2026-07-24",
            )

        self.assertEqual(result, "recovered-without-publication")

    def test_scheduler_rejects_unknown_daily_coordinator_mode(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "both"},
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "BOND_DAILY_COORDINATOR_MODE",
            ):
                scheduler_main.build_scheduler()

    def test_scheduler_rejects_missing_mode_when_rollout_bootstrap_fails(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                scheduler_main,
                "bootstrap_deployment_daily_coordinator_mode",
                side_effect=ValueError(
                    "BOND_DAILY_COORDINATOR_MODE must be explicitly set"
                ),
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
            self.assertRaisesRegex(ValueError, "must be explicitly set"),
        ):
            scheduler_main.build_scheduler()

    def test_scheduler_does_not_own_daily_data_bridge_refresh(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
                clear=True,
            ),
            patch.object(scheduler_main, "discover_schemes", return_value=[]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            job = scheduler.get_job("data-bridge-refresh")
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertIsNone(job)

    def test_ledger_run_once_data_refresh_is_check_only(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "data_bridge_refresh_is_current",
                return_value=True,
            ) as check_current,
            patch.object(
                scheduler_main,
                "run_data_bridge_refresh_job",
            ) as publish_refresh,
        ):
            exit_code = scheduler_main.main(
                ["--run-once", "data-refresh", "--date", "2026-07-24"]
            )

        self.assertEqual(exit_code, 0)
        check_current.assert_called_once_with("2026-07-24")
        publish_refresh.assert_not_called()

    def test_startup_tasks_refresh_before_prediction_catchup(self) -> None:
        from scheduler import main as scheduler_main

        calls: list[str] = []
        now = datetime(2026, 7, 19, 7, 30, tzinfo=scheduler_main.ASIA_SHANGHAI)
        with (
            patch.object(
                scheduler_main,
                "data_bridge_refresh_is_current",
                side_effect=lambda *_args, **_kwargs: calls.append("check") or False,
            ),
            patch.object(
                scheduler_main,
                "run_data_bridge_refresh_job",
                side_effect=lambda *_args, **_kwargs: calls.append("refresh"),
            ),
            patch.object(
                scheduler_main,
                "run_startup_prediction_catchup",
                side_effect=lambda *_args, **_kwargs: calls.append("predictions"),
            ),
        ):
            scheduler_main.run_startup_tasks(now=now, algo_env="forecast_env")

        self.assertEqual(calls, ["check", "refresh", "predictions"])

    def test_startup_tasks_refresh_when_current_check_raises(self) -> None:
        from scheduler import main as scheduler_main
        from shared.data_bridge.refresh import DataBridgeRefreshError

        calls: list[str] = []
        now = datetime(2026, 7, 19, 7, 30, tzinfo=scheduler_main.ASIA_SHANGHAI)
        with (
            patch.object(
                scheduler_main,
                "data_bridge_refresh_is_current",
                side_effect=DataBridgeRefreshError("stale"),
            ),
            patch.object(
                scheduler_main,
                "run_data_bridge_refresh_job",
                side_effect=lambda *_args, **_kwargs: calls.append("refresh"),
            ),
            patch.object(
                scheduler_main,
                "run_startup_prediction_catchup",
                side_effect=lambda *_args, **_kwargs: calls.append("predictions"),
            ),
        ):
            scheduler_main.run_startup_tasks(now=now, algo_env="forecast_env")

        self.assertEqual(calls, ["refresh", "predictions"])

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
            result = scheduler_main.run_prediction_job("daily_demo", run_date="2026-06-15")

        trading_day.assert_called_once_with("2026-06-15")
        execute_scheme.assert_not_called()
        self.assertEqual(
            result,
            SchemeRunResult("daily_demo", "skipped", 0, 0.0, "non-trading day"),
        )
        self.assertTrue(any("Skip daily_demo on non-trading day 2026-06-15" in msg for msg in logs.output))

    def test_run_prediction_job_returns_executor_result(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("daily_demo")
        expected = SchemeRunResult("daily_demo", "success", 1, 0.1, run_id=7)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "execute_scheme", return_value=expected),
        ):
            result = scheduler_main.run_prediction_job("daily_demo", run_date="2026-06-16")

        self.assertEqual(result, expected)

    def test_ledger_cutover_rejects_direct_legacy_daily_entry(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("daily_demo", frequency="daily")
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[cfg],
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
            ) as legacy_run,
            self.assertRaisesRegex(
                RuntimeError,
                "direct daily prediction.*ledger coordinator",
            ),
        ):
            scheduler_main.run_prediction_job(
                "daily_demo",
                run_date="2026-07-24",
            )

        legacy_run.assert_not_called()

    def test_v2_scheduled_job_requires_daily_ready_gate(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg(
            "blackbox_demo",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
        )
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "_previous_trading_day", return_value="2026-07-21"),
            patch.object(
                scheduler_main,
                "require_v2_daily_ready",
                side_effect=V2DailyGateBlocked("blocked"),
            ) as gate,
            patch.object(scheduler_main, "execute_scheme") as execute_scheme,
            self.assertLogs(scheduler_main.logger, level=logging.ERROR),
        ):
            result = scheduler_main.run_prediction_job(
                "blackbox_demo",
                run_date="2026-07-22",
            )

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.error_msg, "blocked")
        execute_scheme.assert_not_called()
        gate.assert_called_once()

    def test_native_v1_does_not_read_v2_daily_gate(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("native_demo", runtime_type="native_adapter")
        expected = SchemeRunResult("native_demo", "success", 1, 0.1, run_id=8)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "require_v2_daily_ready") as gate,
            patch.object(scheduler_main, "execute_scheme", return_value=expected),
        ):
            result = scheduler_main.run_prediction_job(
                "native_demo",
                run_date="2026-07-22",
            )

        self.assertEqual(result, expected)
        gate.assert_not_called()

    def test_single_prediction_job_does_not_sync_registry(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("daily_demo")
        expected = SchemeRunResult("daily_demo", "success", 1, 0.1, run_id=9)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_sync_registry") as sync_registry,
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "execute_scheme", return_value=expected),
        ):
            result = scheduler_main.run_prediction_job(
                "daily_demo",
                run_date="2026-07-22",
            )

        self.assertEqual(result, expected)
        sync_registry.assert_not_called()

    def test_run_prediction_job_returns_configuration_failure_for_unknown_scheme(self) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(scheduler_main, "discover_schemes", return_value=[]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
        ):
            result = scheduler_main.run_prediction_job("missing", run_date="2026-06-16")

        self.assertIsInstance(result, SchemeRunResult)
        self.assertEqual(result.status, "failed")
        self.assertIn("platform configuration error", result.error_msg or "")
        self.assertIn("scheme not found", result.error_msg or "")

    def test_run_all_prediction_jobs_retains_mixed_results(self) -> None:
        from scheduler import main as scheduler_main

        schemes = [_cfg("scheme_a"), _cfg("scheme_b"), _cfg("paused", status="paused")]
        success = SchemeRunResult("scheme_a", "success", 1, 0.1, run_id=1)
        failed = SchemeRunResult("scheme_b", "failed", 0, 0.2, "stale generation", 2)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=schemes) as discover_schemes,
            patch.object(scheduler_main, "_sync_registry", return_value=None) as sync_registry,
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(
                scheduler_main,
                "execute_scheme",
                side_effect=[success, failed],
            ) as execute_scheme,
        ):
            results = scheduler_main.run_all_prediction_jobs(run_date="2026-06-16")

        self.assertEqual(results, [success, failed])
        discover_schemes.assert_called_once_with()
        sync_registry.assert_not_called()
        self.assertEqual(execute_scheme.call_count, 2)

    def test_main_returns_one_for_failed_run_once_prediction(self) -> None:
        from scheduler import main as scheduler_main

        failed = SchemeRunResult("demo", "failed", 0, 0.1, "stale generation")
        with patch.object(scheduler_main, "run_prediction_job", return_value=failed):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "demo", "--force"]
            )

        self.assertEqual(code, 1)

    def test_main_returns_one_for_partial_run_once_prediction(self) -> None:
        from scheduler import main as scheduler_main

        partial = SchemeRunResult("demo", "partial", 1, 0.1, "one tenor failed")
        with patch.object(scheduler_main, "run_prediction_job", return_value=partial):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "demo"]
            )

        self.assertEqual(code, 1)

    def test_main_returns_zero_for_successful_run_once_prediction(self) -> None:
        from scheduler import main as scheduler_main

        success = SchemeRunResult("demo", "success", 1, 0.1, run_id=3)
        with patch.object(scheduler_main, "run_prediction_job", return_value=success):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "demo"]
            )

        self.assertEqual(code, 0)

    def test_ledger_run_once_single_scheme_routes_operator_recovery(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        result = SimpleNamespace(
            business_date="2026-07-24",
            occurrence_id=42,
            status="complete",
            dispatched_scheme_ids=("daily_alpha",),
        )
        stdout = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "run_daily_operator_recovery_job",
                return_value=result,
            ) as recovery,
            patch.object(
                scheduler_main,
                "run_prediction_job",
            ) as legacy,
            redirect_stdout(stdout),
        ):
            code = scheduler_main.main(
                [
                    "--run-once",
                    "predictions",
                    "--scheme-id",
                    "daily_alpha",
                    "--date",
                    "2026-07-24",
                ]
            )

        self.assertEqual(code, 0)
        recovery.assert_called_once_with(
            "daily_alpha",
            run_date="2026-07-24",
            algo_env="forecast_env",
        )
        legacy.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["event"], "daily_occurrence_run")
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["occurrence_id"], 42)

    def test_ledger_run_once_all_routes_single_daily_coordinator(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        result = SimpleNamespace(
            business_date="2026-07-24",
            occurrence_id=43,
            status="incomplete",
            dispatched_scheme_ids=("daily_alpha", "daily_beta"),
        )
        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "run_daily_coordinator_job",
                return_value=result,
            ) as coordinator,
            patch.object(
                scheduler_main,
                "run_all_prediction_jobs",
            ) as legacy,
            redirect_stdout(io.StringIO()),
        ):
            code = scheduler_main.main(
                [
                    "--run-once",
                    "predictions",
                    "--date",
                    "2026-07-24",
                ]
            )

        self.assertEqual(code, 1)
        coordinator.assert_called_once_with(
            run_date="2026-07-24",
            algo_env="forecast_env",
            trigger_origin="operator_recovery",
        )
        legacy.assert_not_called()

    def test_main_returns_zero_for_expected_skipped_prediction(self) -> None:
        from scheduler import main as scheduler_main

        skipped = SchemeRunResult("demo", "skipped", 0, 0.0, "non-trading day")
        with patch.object(scheduler_main, "run_prediction_job", return_value=skipped):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "demo"]
            )

        self.assertEqual(code, 0)

    def test_main_returns_two_for_unknown_explicit_scheme(self) -> None:
        from scheduler import main as scheduler_main

        unknown = SchemeRunResult(
            "missing",
            "failed",
            0,
            0.0,
            "platform configuration error: scheme not found: missing",
        )
        with patch.object(scheduler_main, "run_prediction_job", return_value=unknown):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "missing"]
            )

        self.assertEqual(code, 2)

    def test_main_returns_two_for_argument_or_platform_configuration_error(self) -> None:
        from scheduler import main as scheduler_main

        self.assertEqual(scheduler_main.main(["--run-once", "invalid"]), 2)
        with patch.object(
            scheduler_main,
            "run_all_prediction_jobs",
            side_effect=ValueError("invalid scheduler configuration"),
        ):
            code = scheduler_main.main(["--run-once", "predictions"])

        self.assertEqual(code, 2)

    def test_main_empty_prediction_aggregate_emits_summary_and_returns_zero(self) -> None:
        from scheduler import main as scheduler_main

        stdout = io.StringIO()
        with (
            patch.object(scheduler_main, "run_all_prediction_jobs", return_value=[]),
            redirect_stdout(stdout),
        ):
            code = scheduler_main.main(["--run-once", "predictions"])

        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {
                "counts": {"failed": 0, "partial": 0, "skipped": 0, "success": 0},
                "event": "prediction_run_summary",
                "exit_code": 0,
                "total": 0,
            },
        )

    def test_main_mixed_prediction_aggregate_emits_summary_and_returns_one(self) -> None:
        from scheduler import main as scheduler_main

        results = [
            SchemeRunResult("success", "success", 1, 0.1),
            SchemeRunResult("failed", "failed", 0, 0.2, "failed"),
            SchemeRunResult("partial", "partial", 1, 0.3, "partial"),
            SchemeRunResult("skipped", "skipped", 0, 0.0, "non-trading day"),
        ]
        stdout = io.StringIO()
        with (
            patch.object(scheduler_main, "run_all_prediction_jobs", return_value=results),
            redirect_stdout(stdout),
        ):
            code = scheduler_main.main(["--run-once", "predictions"])

        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {
                "counts": {"failed": 1, "partial": 1, "skipped": 1, "success": 1},
                "event": "prediction_run_summary",
                "exit_code": 1,
                "total": 4,
            },
        )

    def test_scheduled_prediction_wrapper_raises_for_failed_and_partial_results(self) -> None:
        from scheduler import main as scheduler_main

        cfg = _cfg("scheduled_demo")
        with (
            patch.dict(os.environ, {"BOND_SCHEDULER_STARTUP_CATCHUP": "false"}),
            patch.object(scheduler_main, "discover_schemes", return_value=[cfg]),
            patch.object(scheduler_main, "_sync_registry", return_value=None),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            job = scheduler.get_job("predict:scheduled_demo")
            self.assertIsNotNone(job)
            self.assertEqual(job.func.__name__, "run_scheduled_prediction_job")
            results = (
                SchemeRunResult("scheduled_demo", "failed", 0, 0.1, "failed"),
                SchemeRunResult("scheduled_demo", "partial", 1, 0.1, "partial"),
            )
            for result in results:
                with self.subTest(status=result.status):
                    with patch.object(scheduler_main, "run_prediction_job", return_value=result):
                        with self.assertRaisesRegex(RuntimeError, result.status):
                            job.func("scheduled_demo")
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def test_scheduled_prediction_wrapper_passes_through_success_and_skipped_results(self) -> None:
        from scheduler import main as scheduler_main

        results = (
            SchemeRunResult("scheduled_demo", "success", 1, 0.1),
            SchemeRunResult("scheduled_demo", "skipped", 0, 0.0, "non-trading day"),
        )
        for result in results:
            with self.subTest(status=result.status):
                with patch.object(scheduler_main, "run_prediction_job", return_value=result):
                    actual = scheduler_main.run_scheduled_prediction_job("scheduled_demo")

                self.assertIs(actual, result)

    def test_ledger_cutover_rejects_already_registered_legacy_daily_job(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[_cfg("daily_demo", frequency="daily")],
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "run_prediction_job",
                return_value=SchemeRunResult(
                    "daily_demo",
                    "success",
                    1,
                    0.1,
                ),
            ) as legacy_run,
            self.assertRaisesRegex(
                RuntimeError,
                "legacy scheduled daily prediction.*ledger",
            ),
        ):
            scheduler_main.run_scheduled_prediction_job("daily_demo")

        legacy_run.assert_not_called()

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
        success = SchemeRunResult("scheme_b", "success", 1, 0.1)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=schemes) as discover_schemes,
            patch.object(scheduler_main, "_sync_registry", return_value=None) as sync_registry,
            patch.object(scheduler_main, "_prediction_run_exists", side_effect=[True, False]) as run_exists,
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(scheduler_main, "execute_scheme", return_value=success) as execute_scheme,
            patch.object(scheduler_main, "create_engine_from_env") as create_engine,
            self.assertLogs(scheduler_main.logger, level=logging.WARNING),
        ):
            engine = create_engine.return_value
            results = scheduler_main.run_startup_prediction_catchup(now=now, algo_env="forecast_env")

        run_exists.assert_any_call(engine, "scheme_a", "2026-07-09")
        run_exists.assert_any_call(engine, "scheme_b", "2026-07-09")
        discover_schemes.assert_called_once_with()
        sync_registry.assert_called_once_with(schemes)
        execute_scheme.assert_called_once_with(schemes[1], "2026-07-09", algo_env="forecast_env")
        self.assertEqual(
            results,
            [
                SchemeRunResult("scheme_a", "skipped", 0, 0.0, "existing run"),
                success,
            ],
        )
        engine.dispose.assert_called_once()

    def test_startup_prediction_catchup_raises_after_attempting_all_due_jobs(self) -> None:
        from scheduler import main as scheduler_main

        now = datetime(2026, 7, 9, 7, 10, tzinfo=scheduler_main.ASIA_SHANGHAI)
        schemes = [_cfg("scheme_a"), _cfg("scheme_b"), _cfg("scheme_c")]
        failed = SchemeRunResult("scheme_a", "failed", 0, 0.1, "failed")
        partial = SchemeRunResult("scheme_b", "partial", 1, 0.2, "partial")
        success = SchemeRunResult("scheme_c", "success", 1, 0.3)
        with (
            patch.object(scheduler_main, "discover_schemes", return_value=schemes) as discover_schemes,
            patch.object(scheduler_main, "_sync_registry", return_value=None) as sync_registry,
            patch.object(scheduler_main, "_prediction_run_exists", return_value=False),
            patch.object(scheduler_main, "_is_trading_day", return_value=True),
            patch.object(
                scheduler_main,
                "execute_scheme",
                side_effect=[failed, partial, success],
            ) as execute_scheme,
            patch.object(scheduler_main, "create_engine_from_env"),
            self.assertLogs(scheduler_main.logger, level=logging.WARNING),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"Startup prediction catchup failed.*scheme_a=failed.*scheme_b=partial",
            ):
                scheduler_main.run_startup_prediction_catchup(now=now, algo_env="forecast_env")

        discover_schemes.assert_called_once_with()
        sync_registry.assert_called_once_with(schemes)
        self.assertEqual(execute_scheme.call_count, 3)


if __name__ == "__main__":
    unittest.main()
