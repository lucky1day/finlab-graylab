"""scheduler.main 调度注册测试。"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scheduler.blackbox_scheduler_admission import (
    EXPECTED_EXACT_ADMISSIONS,
    BlackboxSchedulerAdmissionError,
)
from scheduler.executor import SchemeRunResult
from scheduler.v2_daily_gate import V2DailyGateBlocked


FORMAL_BLACKBOX_IDENTITIES = {
    "one_y_t5_liq_excess_a_v1": "8d583560c9f1",
    "one_y_t5_liq_excess_a_w252_l7_v1": "103c93bbc913",
    "one_y_t5_liq_excess_a_w350_l7_v1": "86b458c568a5",
    "one_y_t5_liq_excess_b_w252_l7_v1": "ba00891cd179",
    "weekly_10y_lgbm_point_v1": "0666a6989d6b",
}
GRAY_BLACKBOX_IDENTITIES = {
    "cgb_causal_wk_1y": "cba824c27f0e",
    "cgb_causal_wk_3y": "4b8db29b2f74",
    "cgb_a4_fundseason_1y": "04e7af163fb0",
    "cgb_a4_fundseason_3y": "89d31f8bcb95",
    "cgb_a4_fundseason_5y": "7d47e0328532",
    "cgb_a4_fundseason_7y": "ddba87ece7ae",
    "cgb_a4_fundseason_10y": "85a65700499b",
    "ten_y_t5_maj3_k3_ic_static_v1": "c54b90bcafa7",
    "ten_y_t5_maj4_k3_ic_static_v1": "6bdabf86b4a6",
    "ten_y_t5_maj4_k3_ic_yearly_v1": "af04567a19c3",
    "ten_y_t5_say_k5_sharpe_static_v1": "e8137af4b655",
}
WAVG_GAPFLIP_V5_IDENTITIES = {
    "wavg_1y_gapflip_v5": "68999585142a",
    "wavg_3y_gapflip_v5": "faba245acaef",
    "wavg_5y_gapflip_v5": "63ed1291f9d4",
    "wavg_7y_gapflip_v5": "21951d955f44",
    "wavg_10y_gapflip_v5": "c1e5a9db6097",
}


def _cfg(
    scheme_id: str,
    *,
    frequency: str | None = None,
    cron: str = "3 7 * * 1-5",
    status: str = "active",
    version_status: str = "active",
    runtime_type: str = "native_adapter",
    input_source: str | None = None,
    scheme_version: str = "version-1",
) -> SimpleNamespace:
    admission = EXPECTED_EXACT_ADMISSIONS.get(
        (scheme_id, scheme_version)
    )
    effective_frequency = (
        frequency
        or (
            admission.frequency
            if admission is not None
            else "daily"
        )
    )
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        status=status,
        version_status=version_status,
        frequency=effective_frequency,
        task_type=(
            admission.task_type
            if admission is not None
            else "T+1"
        ),
        horizon=(
            admission.horizon
            if admission is not None
            else 1
        ),
        runtime_type=runtime_type,
        input_source=input_source,
        tenors=[
            (
                admission.target_tenor
                if admission is not None
                else "5Y"
            )
        ],
        schedule=SimpleNamespace(cron=cron, timezone="Asia/Shanghai"),
    )


class RetiredDataBridgeCliTests(unittest.TestCase):
    def test_legacy_data_bridge_cli_is_absent_and_rejected_by_parser(self) -> None:
        """退役 CLI 不能再作为 scheduler.main 的兼容入口。"""
        from scheduler import main as scheduler_main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = scheduler_main.main(
                ["--run-once", "data-refresh", "--date", "2026-07-24"]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid choice", stderr.getvalue())
        for legacy_name in (
            "LegacySchedulerWriterRetired",
            "run_data_bridge_refresh_job",
            "data_bridge_refresh_is_current",
            "run_startup_tasks",
        ):
            self.assertFalse(hasattr(scheduler_main, legacy_name), legacy_name)


class SchedulerMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mode_patcher = patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
        )
        self._mode_patcher.start()
        self._capacity_patcher = patch(
            "scheduler.main.build_daily_direct_cache_authorities",
            return_value={
                "schema_version": "daily-direct-cache-authorities-v1",
                "storage_root": "/tmp/cache",
                "contract": {},
                "consumers": {},
            },
        )
        self._direct_authority_mock = self._capacity_patcher.start()
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

    def test_legacy_run_once_predictions_preflight_storage_before_work(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        events: list[str] = []
        with (
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
        ):
            code = scheduler_main.main(["--run-once", "predictions"])

        self.assertEqual(code, 0)
        self.assertEqual(events, ["preflight", "predictions"])

    def test_run_once_storage_failure_blocks_legacy_predictions(
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
                    "DAILY_STORAGE_PATH_UNSAFE",
                    label="daily_runtime",
                ),
            ),
            patch.object(
                scheduler_main,
                "run_all_prediction_jobs",
            ) as predictions,
        ):
            code = scheduler_main.main(["--run-once", "predictions"])

        self.assertEqual(code, 2)
        predictions.assert_not_called()

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
        self._direct_authority_mock.assert_called_once()
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

    def test_scheduler_never_registers_startup_catchup(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        for coordinator_mode in ("legacy", "ledger"):
            with self.subTest(coordinator_mode=coordinator_mode):
                with (
                    patch.dict(
                        os.environ,
                        {
                            "BOND_DAILY_COORDINATOR_MODE": coordinator_mode,
                            "BOND_SCHEDULER_STARTUP_CATCHUP": "true",
                        },
                    ),
                    patch.object(
                        scheduler_main,
                        "_daily_coordinator_mode",
                        return_value=coordinator_mode,
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
                    startup_jobs = [
                        job.id
                        for job in scheduler.get_jobs()
                        if job.id.startswith("startup:")
                    ]
                finally:
                    if scheduler.running:
                        scheduler.shutdown(wait=False)

                self.assertEqual(startup_jobs, [])

    def test_ledger_startup_catchup_runs_non_daily_even_when_daily_fails(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        with (
            patch.object(
                scheduler_main,
                "run_daily_coordinator_job",
                side_effect=RuntimeError("daily failed"),
            ) as daily,
            patch.object(
                scheduler_main,
                "run_startup_prediction_catchup",
                return_value=[],
            ) as non_daily,
            self.assertRaisesRegex(RuntimeError, "daily failed"),
        ):
            scheduler_main.run_ledger_startup_catchup(
                now=datetime(
                    2026,
                    7,
                    24,
                    7,
                    30,
                    tzinfo=scheduler_main.ASIA_SHANGHAI,
                ),
                algo_env="forecast_env",
            )

        daily.assert_called_once_with(
            run_date="2026-07-24",
            algo_env="forecast_env",
            trigger_origin="startup_catchup",
        )
        non_daily.assert_called_once()
        self.assertEqual(
            non_daily.call_args.kwargs["algo_env"],
            "forecast_env",
        )

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

    def test_scheduler_jobs_are_registered_once_per_base_scheme_not_per_tenor(self) -> None:
        from scheduler import main as scheduler_main

        cfg = SimpleNamespace(
            scheme_id="weekly_multi_tenor",
            status="active",
            frequency="weekly",
            tenors=["3Y", "5Y", "7Y", "10Y"],
            schedule=SimpleNamespace(cron="30 11 * * 6", timezone="Asia/Shanghai"),
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

        self.assertEqual(prediction_jobs, ["predict:weekly_multi_tenor"])

    def test_legacy_scheduler_does_not_mount_daily_jobs_owned_by_daily_gray_launchd(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        schemes = [
            _cfg("daily_native", frequency="daily"),
            _cfg(
                "weekly_native",
                frequency="weekly",
                cron="30 11 * * 6",
            ),
        ]
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
            prediction_jobs = {
                job.id
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            }
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual(prediction_jobs, {"predict:weekly_native"})

    def test_scheduler_keeps_active_formal_and_excludes_paused_onboarding(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from scheduler.discovery import discover_schemes

        selected_ids = (
            set(FORMAL_BLACKBOX_IDENTITIES)
            | set(GRAY_BLACKBOX_IDENTITIES)
        )
        schemes = [
            config
            for config in discover_schemes()
            if config.scheme_id in selected_ids
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
                "_sync_registry",
                return_value=None,
            ) as sync_registry,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_ids = {
                job.id.removeprefix("predict:")
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            }
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        paused_onboarding: set[str] = set()
        expected_recurring = {
            config.scheme_id
            for config in schemes
            if config.scheme_id in FORMAL_BLACKBOX_IDENTITIES
            and config.status == "active"
            and config.frequency != "daily"
        }
        self.assertEqual(
            prediction_ids,
            expected_recurring - paused_onboarding,
        )
        self.assertTrue(
            all(
                config.status == "paused"
                for config in schemes
                if config.scheme_id in paused_onboarding
            )
        )
        self.assertTrue(
            set(GRAY_BLACKBOX_IDENTITIES).isdisjoint(prediction_ids)
        )
        sync_registry.assert_called_once_with(schemes)

    def test_invalid_blackbox_policy_keeps_native_jobs(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        native = _cfg("native_demo", frequency="weekly")
        blackbox = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
        )
        schemes = [native, blackbox]
        policy_errors = (
            "admission not found",
            "admission is invalid JSON",
            "unsupported schema_version",
        )

        for policy_error in policy_errors:
            with self.subTest(policy_error=policy_error):
                with (
                    patch.dict(
                        os.environ,
                        {
                            "BOND_SCHEDULER_STARTUP_CATCHUP": (
                                "false"
                            )
                        },
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
                    ) as sync_registry,
                    patch.object(
                        scheduler_main,
                        "load_blackbox_scheduler_admission",
                        side_effect=(
                            BlackboxSchedulerAdmissionError(
                                policy_error
                            )
                        ),
                    ),
                    self.assertLogs(
                        scheduler_main.logger,
                        level=logging.CRITICAL,
                    ),
                ):
                    scheduler = scheduler_main.build_scheduler()

                try:
                    job_ids = {
                        job.id
                        for job in scheduler.get_jobs()
                    }
                finally:
                    if scheduler.running:
                        scheduler.shutdown(wait=False)

                self.assertIn("predict:native_demo", job_ids)
                self.assertNotIn(
                    "predict:one_y_t5_liq_excess_a_v1",
                    job_ids,
                )
                sync_registry.assert_called_once_with(schemes)

    def test_non_utf8_blackbox_policy_keeps_native_jobs(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from scheduler.blackbox_scheduler_admission import (
            load_blackbox_scheduler_admission,
        )

        native = _cfg("native_demo", frequency="weekly")
        blackbox = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
        )
        schemes = [native, blackbox]
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "admission.json"
            policy_path.write_bytes(b"\xff\xfe\x80")
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
                    "_sync_registry",
                    return_value=None,
                ) as sync_registry,
                patch.object(
                    scheduler_main,
                    "load_blackbox_scheduler_admission",
                    side_effect=lambda: (
                        load_blackbox_scheduler_admission(policy_path)
                    ),
                ),
                self.assertLogs(
                    scheduler_main.logger,
                    level=logging.CRITICAL,
                ),
            ):
                scheduler = scheduler_main.build_scheduler()

        try:
            job_ids = {
                job.id
                for job in scheduler.get_jobs()
            }
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertIn("predict:native_demo", job_ids)
        self.assertNotIn(
            "predict:one_y_t5_liq_excess_a_v1",
            job_ids,
        )
        sync_registry.assert_called_once_with(schemes)

    def test_build_rejects_reserved_runtime_and_version_drift(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        native = _cfg("native_demo", frequency="weekly")
        runtime_drift = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="native_adapter",
            scheme_version="04e7af163fb0",
        )
        version_drift = _cfg(
            "cgb_a4_fundseason_3y",
            runtime_type="blackbox_v2",
            scheme_version="version-drift",
        )
        schemes = [native, runtime_drift, version_drift]
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
                "_sync_registry",
                return_value=None,
            ) as sync_registry,
            self.assertLogs(
                scheduler_main.logger,
                level=logging.CRITICAL,
            ) as logs,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_ids = {
                job.id.removeprefix("predict:")
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            }
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertEqual(prediction_ids, {"native_demo"})
        self.assertTrue(
            any(
                "runtime_type drift" in message
                and runtime_drift.scheme_id in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any(
                "scheme_version drift" in message
                and version_drift.scheme_id in message
                for message in logs.output
            )
        )
        sync_registry.assert_called_once_with(schemes)

    def test_native_only_scheduler_does_not_load_blackbox_policy(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        native = _cfg("native_demo", frequency="weekly")
        with (
            patch.dict(
                os.environ,
                {"BOND_SCHEDULER_STARTUP_CATCHUP": "false"},
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[native],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=AssertionError(
                    "Native-only scheduler must not load Blackbox policy"
                ),
            ) as load_policy,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            self.assertIsNotNone(
                scheduler.get_job("predict:native_demo")
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        load_policy.assert_not_called()

    def test_gray_blackbox_does_not_consume_stagger_slot(self) -> None:
        from scheduler import main as scheduler_main

        gray = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="blackbox_v2",
            scheme_version="04e7af163fb0",
        )
        formal = _cfg(
            "weekly_10y_lgbm_point_v1",
            runtime_type="blackbox_v2",
            scheme_version="0666a6989d6b",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "BOND_SCHEDULER_STARTUP_CATCHUP": "false",
                    "BOND_SCHEDULER_STAGGER_MINUTES": "2",
                },
            ),
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[gray, formal],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            formal_job = scheduler.get_job(
                f"predict:{formal.scheme_id}"
            )
            gray_job = scheduler.get_job(
                f"predict:{gray.scheme_id}"
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertIsNotNone(formal_job)
        self.assertIn("minute='3'", str(formal_job.trigger))
        self.assertIsNone(gray_job)

    def test_scheduler_staggers_prediction_jobs_with_same_base_cron(self) -> None:
        from scheduler import main as scheduler_main

        schemes = [
            _cfg("scheme_c", frequency="weekly"),
            _cfg("scheme_a", frequency="weekly"),
            _cfg("scheme_b", frequency="weekly"),
            _cfg(
                "paused_scheme",
                frequency="weekly",
                status="paused",
            ),
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
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[
                    _cfg("scheme_a", frequency="weekly"),
                    _cfg("scheme_b", frequency="weekly"),
                ],
            ),
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
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
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
                cfg.scheme_id,
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

    def test_run_all_prediction_jobs_rejects_invalid_blackbox_admission(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        gray = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="blackbox_v2",
            scheme_version="04e7af163fb0",
            frequency="monthly",
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[gray],
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "invalid policy"
                ),
            ) as load_admission,
            patch.object(
                scheduler_main,
                "execute_scheme",
            ) as execute_scheme,
        ):
            results = scheduler_main.run_all_prediction_jobs(
                run_date="2026-07-27",
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].scheme_id, gray.scheme_id)
        self.assertEqual(results[0].status, "failed")
        self.assertIn(
            scheduler_main.PLATFORM_CONFIGURATION_ERROR_PREFIX,
            results[0].error_msg or "",
        )
        self.assertIn("invalid policy", results[0].error_msg or "")
        load_admission.assert_called_once_with()
        execute_scheme.assert_not_called()

    def test_invalid_admission_rejects_all_blackbox_but_native_continues(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        native_a = _cfg("native_a")
        formal = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
        )
        gray = _cfg(
            "ten_y_t5_maj3_k3_ic_static_v1",
            runtime_type="blackbox_v2",
            scheme_version="c54b90bcafa7",
        )
        native_b = _cfg("native_b")
        configs = [native_a, formal, gray, native_b]

        def execute_native(config, *_args, **_kwargs):
            self.assertEqual(config.runtime_type, "native_adapter")
            return SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=configs,
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "invalid policy"
                ),
            ) as load_admission,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                side_effect=execute_native,
            ) as execute,
        ):
            results = scheduler_main.run_all_prediction_jobs(
                run_date="2026-07-27",
            )

        self.assertEqual(
            [result.scheme_id for result in results],
            [config.scheme_id for config in configs],
        )
        self.assertEqual(
            [result.status for result in results],
            ["success", "failed", "failed", "success"],
        )
        for result in results[1:3]:
            self.assertIn(
                scheduler_main.PLATFORM_CONFIGURATION_ERROR_PREFIX,
                result.error_msg or "",
            )
        load_admission.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in execute.call_args_list],
            [native_a, native_b],
        )

    def test_legacy_run_once_predictions_rejects_invalid_admission(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        gray = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="blackbox_v2",
            scheme_version="04e7af163fb0",
            frequency="monthly",
        )
        stdout = io.StringIO()
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[gray],
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "invalid policy"
                ),
            ) as load_admission,
            patch.object(
                scheduler_main,
                "execute_scheme",
            ) as execute_scheme,
            patch.object(
                scheduler_main,
                "run_all_prediction_jobs",
                wraps=scheduler_main.run_all_prediction_jobs,
            ) as manual_aggregate,
            redirect_stdout(stdout),
        ):
            code = scheduler_main.main(
                [
                    "--run-once",
                    "predictions",
                    "--date",
                    "2026-07-27",
                ]
            )

        self.assertEqual(code, 2)
        manual_aggregate.assert_called_once_with(
            run_date="2026-07-27",
            algo_env=scheduler_main.DEFAULT_ALGO_ENV,
            force=False,
        )
        load_admission.assert_called_once_with()
        execute_scheme.assert_not_called()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "counts": {
                    "failed": 1,
                    "partial": 0,
                    "skipped": 0,
                    "success": 0,
                },
                "event": "prediction_run_summary",
                "exit_code": 2,
                "total": 1,
            },
        )

    def test_main_returns_one_for_failed_run_once_prediction(self) -> None:
        from scheduler import main as scheduler_main

        failed = SchemeRunResult("demo", "failed", 0, 0.1, "stale generation")
        with patch.object(scheduler_main, "run_prediction_job", return_value=failed):
            code = scheduler_main.main(
                ["--run-once", "predictions", "--scheme-id", "demo", "--force"]
            )

        self.assertEqual(code, 1)

    def test_legacy_run_once_single_rejects_daily_gray(self) -> None:
        from scheduler import main as scheduler_main

        gray = _cfg(
            "ten_y_t5_maj3_k3_ic_static_v1",
            runtime_type="blackbox_v2",
            scheme_version="c54b90bcafa7",
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[gray],
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
            ) as run_config,
            redirect_stdout(io.StringIO()),
        ):
            code = scheduler_main.main(
                [
                    "--run-once",
                    "predictions",
                    "--scheme-id",
                    gray.scheme_id,
                    "--date",
                    "2026-07-27",
                ]
            )

        self.assertEqual(code, 2)
        run_config.assert_not_called()

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

        cfg = _cfg("scheduled_demo", frequency="weekly")
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
                    with (
                        patch.object(
                            scheduler_main,
                            "discover_schemes",
                            return_value=[cfg],
                        ),
                        patch.object(
                            scheduler_main,
                            "_run_prediction_config",
                            return_value=result,
                        ),
                    ):
                        with self.assertRaisesRegex(RuntimeError, result.status):
                            job.func("scheduled_demo")
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def test_scheduled_prediction_wrapper_passes_through_success_and_skipped_results(self) -> None:
        from scheduler import main as scheduler_main

        config = _cfg("scheduled_demo")
        results = (
            SchemeRunResult("scheduled_demo", "success", 1, 0.1),
            SchemeRunResult("scheduled_demo", "skipped", 0, 0.0, "non-trading day"),
        )
        for result in results:
            with self.subTest(status=result.status):
                with (
                    patch.object(
                        scheduler_main,
                        "discover_schemes",
                        return_value=[config],
                    ),
                    patch.object(
                        scheduler_main,
                        "_run_prediction_config",
                        return_value=result,
                    ),
                ):
                    actual = scheduler_main.run_scheduled_prediction_job("scheduled_demo")

                self.assertIs(actual, result)

    def test_scheduled_wrapper_rejects_gray_and_version_drift_before_manual_path(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        denied_configs = (
            _cfg(
                "cgb_a4_fundseason_1y",
                runtime_type="blackbox_v2",
                scheme_version="04e7af163fb0",
            ),
            _cfg(
                "ten_y_t5_maj3_k3_ic_static_v1",
                runtime_type="blackbox_v2",
                scheme_version="c54b90bcafa7",
            ),
            _cfg(
                "weekly_10y_lgbm_point_v1",
                runtime_type="blackbox_v2",
                scheme_version="version-drift",
                frequency="weekly",
            ),
        )
        for config in denied_configs:
            with (
                self.subTest(scheme_id=config.scheme_id),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    scheduler_main,
                    "_run_prediction_config",
                ) as run_config,
                self.assertRaisesRegex(
                    BlackboxSchedulerAdmissionError,
                    "automatic scheduling denied",
                ),
            ):
                scheduler_main.run_scheduled_prediction_job(
                    config.scheme_id
                )

            run_config.assert_not_called()

    def test_scheduled_wrapper_allows_exact_formal_blackbox_identity(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        identities = (
            ("weekly_10y_lgbm_point_v1", "0666a6989d6b"),
        )
        for scheme_id, scheme_version in identities:
            config = _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
                frequency="weekly",
            )
            expected = SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )
            with (
                self.subTest(scheme_id=scheme_id),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    scheduler_main,
                    "_run_prediction_config",
                    return_value=expected,
                ) as run_config,
            ):
                actual = scheduler_main.run_scheduled_prediction_job(
                    config.scheme_id,
                    run_date="2026-07-27",
                )

                self.assertIs(actual, expected)
                run_config.assert_called_once_with(
                    config,
                    "2026-07-27",
                    algo_env=scheduler_main.DEFAULT_ALGO_ENV,
                    force=False,
                )

    def test_scheduled_wrapper_uses_one_discovery_and_same_admitted_config(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        admitted = _cfg(
            "weekly_10y_lgbm_point_v1",
            runtime_type="blackbox_v2",
            scheme_version="0666a6989d6b",
            frequency="weekly",
        )
        drifted = _cfg(
            admitted.scheme_id,
            runtime_type="blackbox_v2",
            scheme_version="version-drift",
            frequency="weekly",
        )
        expected = SchemeRunResult(
            admitted.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                side_effect=[[admitted], [drifted]],
            ) as discovery,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                return_value=expected,
            ) as run_config,
        ):
            actual = scheduler_main.run_scheduled_prediction_job(
                admitted.scheme_id,
                run_date="2026-07-27",
            )

        self.assertIs(actual, expected)
        discovery.assert_called_once_with()
        run_config.assert_called_once_with(
            admitted,
            "2026-07-27",
            algo_env=scheduler_main.DEFAULT_ALGO_ENV,
            force=False,
        )

    def test_scheduled_wrapper_rejects_reserved_runtime_drift(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        runtime_drift = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="native_adapter",
            scheme_version="04e7af163fb0",
            frequency="weekly",
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[runtime_drift],
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
            ) as run_config,
            self.assertLogs(
                scheduler_main.logger,
                level=logging.CRITICAL,
            ) as logs,
            self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "automatic scheduling denied",
            ),
        ):
            scheduler_main.run_scheduled_prediction_job(
                runtime_drift.scheme_id,
                run_date="2026-07-27",
            )

        run_config.assert_not_called()
        self.assertTrue(
            any(
                "runtime_type drift" in message
                for message in logs.output
            )
        )

    def test_native_scheduled_wrapper_does_not_load_blackbox_policy(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        native = _cfg(
            "native_demo",
            frequency="weekly",
        )
        expected = SchemeRunResult(
            native.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[native],
            ) as discovery,
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=AssertionError(
                    "Native wrapper must not load Blackbox policy"
                ),
            ) as load_policy,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                return_value=expected,
            ) as run_config,
        ):
            actual = scheduler_main.run_scheduled_prediction_job(
                native.scheme_id,
                run_date="2026-07-27",
            )

        self.assertIs(actual, expected)
        discovery.assert_called_once_with()
        load_policy.assert_not_called()
        run_config.assert_called_once_with(
            native,
            "2026-07-27",
            algo_env=scheduler_main.DEFAULT_ALGO_ENV,
            force=False,
        )

    def test_blackbox_scheduled_wrapper_rejects_invalid_policy(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        blackbox = _cfg(
            "weekly_10y_lgbm_point_v1",
            runtime_type="blackbox_v2",
            scheme_version="0666a6989d6b",
            frequency="weekly",
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[blackbox],
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "invalid policy"
                ),
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
            ) as run_config,
            self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "invalid policy",
            ),
        ):
            scheduler_main.run_scheduled_prediction_job(
                blackbox.scheme_id,
                run_date="2026-07-27",
            )

        run_config.assert_not_called()

    def test_direct_prediction_rejects_every_gray_blackbox_before_readiness(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        for scheme_id, scheme_version in (
            GRAY_BLACKBOX_IDENTITIES.items()
        ):
            config = _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
            )
            with (
                self.subTest(scheme_id=scheme_id),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    scheduler_main,
                    "_is_trading_day",
                ) as trading_day,
                patch.object(
                    scheduler_main,
                    "require_v2_daily_ready",
                ) as readiness,
                patch.object(
                    scheduler_main,
                    "_run_prediction_config",
                ) as run_config,
            ):
                actual = scheduler_main.run_prediction_job(
                    config.scheme_id,
                    run_date="2026-07-27",
                )

            self.assertEqual(actual.status, "failed")
            self.assertIn(
                scheduler_main.PLATFORM_CONFIGURATION_ERROR_PREFIX,
                actual.error_msg or "",
            )
            self.assertIn("direct_scheduled", actual.error_msg or "")
            trading_day.assert_not_called()
            readiness.assert_not_called()
            run_config.assert_not_called()

    def test_direct_prediction_allows_formal_daily_and_weekly(self) -> None:
        from scheduler import main as scheduler_main

        identities = (
            (
                "one_y_t5_liq_excess_a_v1",
                "8d583560c9f1",
            ),
            (
                "weekly_10y_lgbm_point_v1",
                "0666a6989d6b",
            ),
        )
        for scheme_id, scheme_version in identities:
            config = _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
            )
            expected = SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )
            with (
                self.subTest(scheme_id=scheme_id),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    scheduler_main,
                    "_run_prediction_config",
                    return_value=expected,
                ) as run_config,
            ):
                actual = scheduler_main.run_prediction_job(
                    config.scheme_id,
                    run_date="2026-07-27",
                )

            self.assertIs(actual, expected)
            run_config.assert_called_once_with(
                config,
                "2026-07-27",
                algo_env=scheduler_main.DEFAULT_ALGO_ENV,
                force=False,
            )

    def test_valid_policy_aggregate_executes_only_formal_blackbox(self) -> None:
        from scheduler import main as scheduler_main

        configs = [
            _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
            )
            for scheme_id, scheme_version in (
                FORMAL_BLACKBOX_IDENTITIES
                | GRAY_BLACKBOX_IDENTITIES
            ).items()
        ]

        def execute_formal(config, *_args, **_kwargs):
            self.assertIn(
                config.scheme_id,
                FORMAL_BLACKBOX_IDENTITIES,
            )
            return SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        stdout = io.StringIO()
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=configs,
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                side_effect=execute_formal,
            ) as execute,
            redirect_stdout(stdout),
        ):
            code = scheduler_main.main(
                [
                    "--run-once",
                    "predictions",
                    "--date",
                    "2026-07-27",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(
            [call.args[0].scheme_id for call in execute.call_args_list],
            list(FORMAL_BLACKBOX_IDENTITIES),
        )
        self.assertEqual(
            json.loads(stdout.getvalue())["counts"],
            {
                "failed": len(GRAY_BLACKBOX_IDENTITIES),
                "partial": 0,
                "skipped": 0,
                "success": len(FORMAL_BLACKBOX_IDENTITIES),
            },
        )

    def test_native_direct_paths_never_load_blackbox_policy(self) -> None:
        from scheduler import main as scheduler_main

        single = _cfg("native_single")
        aggregate = _cfg("native_aggregate", frequency="weekly")

        def execute_native(config, *_args, **_kwargs):
            return SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                side_effect=[[single], [aggregate]],
            ),
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=AssertionError(
                    "Native direct path must not load Blackbox policy"
                ),
            ) as load_policy,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                side_effect=execute_native,
            ) as execute,
        ):
            single_result = scheduler_main.run_prediction_job(
                single.scheme_id,
                run_date="2026-07-27",
            )
            aggregate_results = scheduler_main.run_all_prediction_jobs(
                run_date="2026-07-27",
            )

        self.assertEqual(single_result.status, "success")
        self.assertEqual(
            [result.status for result in aggregate_results],
            ["success"],
        )
        load_policy.assert_not_called()
        self.assertEqual(execute.call_count, 2)

    def test_direct_prediction_rejects_inactive_scheme_or_version(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        configs = (
            _cfg("paused_native", status="paused"),
            _cfg(
                "one_y_t5_liq_excess_a_v1",
                runtime_type="blackbox_v2",
                scheme_version="8d583560c9f1",
                version_status="shadow",
            ),
        )
        for config in configs:
            with (
                self.subTest(scheme_id=config.scheme_id),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    scheduler_main,
                    "load_blackbox_scheduler_admission",
                ) as load_policy,
                patch.object(
                    scheduler_main,
                    "_is_trading_day",
                ) as trading_day,
                patch.object(
                    scheduler_main,
                    "require_v2_daily_ready",
                ) as readiness,
                patch.object(
                    scheduler_main,
                    "_run_prediction_config",
                ) as run_config,
            ):
                actual = scheduler_main.run_prediction_job(
                    config.scheme_id,
                    run_date="2026-07-27",
                )

            self.assertEqual(actual.status, "failed")
            self.assertIn(
                scheduler_main.PLATFORM_CONFIGURATION_ERROR_PREFIX,
                actual.error_msg or "",
            )
            load_policy.assert_not_called()
            trading_day.assert_not_called()
            readiness.assert_not_called()
            run_config.assert_not_called()

    def test_ledger_aggregate_rejects_daily_and_runs_weekly_monthly(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        daily_native = _cfg("daily_native")
        weekly_native = _cfg("weekly_native", frequency="weekly")
        daily_blackbox = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
        )
        monthly_native = _cfg("monthly_native", frequency="monthly")
        configs = [
            daily_native,
            weekly_native,
            daily_blackbox,
            monthly_native,
        ]

        def execute_recurring(config, *_args, **_kwargs):
            self.assertIn(config.frequency, {"weekly", "monthly"})
            return SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=configs,
            ),
            patch.object(
                scheduler_main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                scheduler_main,
                "_is_trading_day",
            ) as trading_day,
            patch.object(
                scheduler_main,
                "require_v2_daily_ready",
            ) as readiness,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                side_effect=execute_recurring,
            ) as execute,
        ):
            results = scheduler_main.run_all_prediction_jobs(
                run_date="2026-07-27",
            )

        self.assertEqual(
            [result.scheme_id for result in results],
            [config.scheme_id for config in configs],
        )
        self.assertEqual(
            [result.status for result in results],
            ["failed", "success", "failed", "success"],
        )
        self.assertEqual(scheduler_main._prediction_exit_code(results), 2)
        self.assertEqual(
            [call.args[0] for call in execute.call_args_list],
            [weekly_native, monthly_native],
        )
        trading_day.assert_not_called()
        readiness.assert_not_called()

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
                "_run_prediction_config",
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

    def test_build_scheduler_never_registers_actuals_jobs(self) -> None:
        from scheduler import main as scheduler_main

        for coordinator_mode in ("legacy", "ledger"):
            with self.subTest(coordinator_mode=coordinator_mode):
                engine = SimpleNamespace(dispose=Mock())
                with (
                    patch.dict(
                        os.environ,
                        {"BOND_SCHEDULER_STARTUP_CATCHUP": "false"},
                    ),
                    patch.object(
                        scheduler_main,
                        "_daily_coordinator_mode",
                        return_value=coordinator_mode,
                    ),
                    patch.object(
                        scheduler_main,
                        "discover_schemes",
                        return_value=[],
                    ),
                    patch.object(
                        scheduler_main,
                        "_preflight_source_runtime_database",
                        return_value=None,
                    ),
                    patch.object(
                        scheduler_main,
                        "_sync_registry",
                        return_value=None,
                    ),
                    patch.object(
                        scheduler_main,
                        "create_engine_from_env",
                        return_value=engine,
                    ),
                    patch.object(
                        scheduler_main,
                        "build_daily_direct_cache_authorities",
                        return_value={},
                    ),
                ):
                    scheduler = scheduler_main.build_scheduler()

                try:
                    actuals_job_ids = sorted(
                        job.id
                        for job in scheduler.get_jobs()
                        if job.id.startswith("actuals:")
                    )
                finally:
                    if scheduler.running:
                        scheduler.shutdown(wait=False)

                self.assertEqual([], actuals_job_ids)

    def test_wavg_gapflip_v5_gray_identities_never_mount_automatic_jobs(
        self,
    ) -> None:
        """五个周均 gray exact identity 不获得自动或 recurring 调度。"""
        from scheduler import main as scheduler_main

        configs = [
            _cfg(
                scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=scheme_version,
                cron="30 11 * * 6",
            )
            for scheme_id, scheme_version in (
                WAVG_GAPFLIP_V5_IDENTITIES.items()
            )
        ]
        for identity in WAVG_GAPFLIP_V5_IDENTITIES.items():
            with self.subTest(identity=identity):
                self.assertIn(identity, EXPECTED_EXACT_ADMISSIONS)

        self.assertEqual(
            scheduler_main._automatic_prediction_schemes(configs),
            [],
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=configs,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            prediction_job_ids = {
                job.id
                for job in scheduler.get_jobs()
                if job.id.startswith("predict:")
            }
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

        self.assertTrue(
            prediction_job_ids.isdisjoint(
                {
                    f"predict:{scheme_id}"
                    for scheme_id in WAVG_GAPFLIP_V5_IDENTITIES
                }
            )
        )

    def test_run_once_actuals_is_rejected_without_business_dispatch(self) -> None:
        """actuals 仅可由独立 runner CLI 进入，主入口不得再兼容委托。"""
        from scheduler import actuals_runner
        from scheduler import main as scheduler_main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                actuals_runner,
                "run_actuals_job",
            ) as run_actuals_job,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = scheduler_main.main(
                [
                    "--run-once",
                    "actuals",
                    "--date",
                    "2026-08-01",
                    "--force",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid choice", stderr.getvalue())
        run_actuals_job.assert_not_called()

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
        schemes = [
            _cfg("scheme_a", frequency="weekly"),
            _cfg("scheme_b", frequency="weekly"),
        ]
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

    def test_startup_prediction_catchup_excludes_daily_gray_launchd_schemes(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        now = datetime(
            2026,
            7,
            9,
            7,
            10,
            tzinfo=scheduler_main.ASIA_SHANGHAI,
        )
        daily = _cfg("daily_native", frequency="daily")
        weekly = _cfg(
            "weekly_native",
            frequency="weekly",
            cron="3 7 * * 1-5",
        )
        success = SchemeRunResult(
            weekly.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=[daily, weekly],
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ),
            patch.object(
                scheduler_main,
                "_prediction_run_exists",
                return_value=False,
            ) as run_exists,
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                return_value=success,
            ) as execute,
            patch.object(
                scheduler_main,
                "create_engine_from_env",
            ),
            self.assertLogs(
                scheduler_main.logger,
                level=logging.WARNING,
            ),
        ):
            results = scheduler_main.run_startup_prediction_catchup(
                now=now,
                algo_env="forecast_env",
            )

        self.assertEqual(results, [success])
        run_exists.assert_called_once()
        execute.assert_called_once_with(
            weekly,
            "2026-07-09",
            algo_env="forecast_env",
            force=False,
        )

    def test_startup_prediction_catchup_excludes_gray_and_version_drift(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        now = datetime(
            2026,
            7,
            9,
            7,
            15,
            tzinfo=scheduler_main.ASIA_SHANGHAI,
        )
        formal = _cfg(
            "weekly_10y_lgbm_point_v1",
            runtime_type="blackbox_v2",
            scheme_version="0666a6989d6b",
        )
        gray = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="blackbox_v2",
            scheme_version="04e7af163fb0",
        )
        drift = _cfg(
            "cgb_a4_fundseason_3y",
            runtime_type="blackbox_v2",
            scheme_version="version-drift",
        )
        native = _cfg("native_demo", frequency="weekly")
        schemes = [formal, gray, drift, native]

        def run_config(config, *_args, **_kwargs):
            return SchemeRunResult(
                config.scheme_id,
                "success",
                1,
                0.1,
            )

        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ) as sync_registry,
            patch.object(
                scheduler_main,
                "_prediction_run_exists",
                return_value=False,
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                side_effect=run_config,
            ) as execute,
            patch.object(
                scheduler_main,
                "create_engine_from_env",
            ),
            self.assertLogs(
                scheduler_main.logger,
                level=logging.WARNING,
            ),
        ):
            results = scheduler_main.run_startup_prediction_catchup(
                now=now,
                algo_env="forecast_env",
            )

        sync_registry.assert_called_once_with(schemes)
        self.assertEqual(
            [call.args[0] for call in execute.call_args_list],
            [native, formal],
        )
        self.assertEqual(
            [result.scheme_id for result in results],
            ["native_demo", formal.scheme_id],
        )

    def test_startup_invalid_blackbox_policy_keeps_native_catchup(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        now = datetime(
            2026,
            7,
            9,
            7,
            15,
            tzinfo=scheduler_main.ASIA_SHANGHAI,
        )
        native = _cfg("native_demo", frequency="weekly")
        blackbox = _cfg(
            "one_y_t5_liq_excess_a_v1",
            runtime_type="blackbox_v2",
            scheme_version="8d583560c9f1",
        )
        schemes = [native, blackbox]
        expected = SchemeRunResult(
            native.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ) as sync_registry,
            patch.object(
                scheduler_main,
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "invalid policy"
                ),
            ),
            patch.object(
                scheduler_main,
                "_prediction_run_exists",
                return_value=False,
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                return_value=expected,
            ) as run_config,
            patch.object(
                scheduler_main,
                "create_engine_from_env",
            ),
            self.assertLogs(
                scheduler_main.logger,
                level=logging.CRITICAL,
            ),
        ):
            results = scheduler_main.run_startup_prediction_catchup(
                now=now,
                algo_env="forecast_env",
            )

        sync_registry.assert_called_once_with(schemes)
        run_config.assert_called_once_with(
            native,
            "2026-07-09",
            algo_env="forecast_env",
            force=False,
        )
        self.assertEqual(results, [expected])

    def test_startup_rejects_reserved_runtime_drift(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        now = datetime(
            2026,
            7,
            9,
            7,
            15,
            tzinfo=scheduler_main.ASIA_SHANGHAI,
        )
        runtime_drift = _cfg(
            "cgb_a4_fundseason_1y",
            runtime_type="native_adapter",
            scheme_version="04e7af163fb0",
        )
        native = _cfg("native_demo", frequency="weekly")
        schemes = [runtime_drift, native]
        expected = SchemeRunResult(
            native.scheme_id,
            "success",
            1,
            0.1,
        )
        with (
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
                return_value=None,
            ) as sync_registry,
            patch.object(
                scheduler_main,
                "_prediction_run_exists",
                return_value=False,
            ),
            patch.object(
                scheduler_main,
                "_run_prediction_config",
                return_value=expected,
            ) as run_config,
            patch.object(
                scheduler_main,
                "create_engine_from_env",
            ),
            self.assertLogs(
                scheduler_main.logger,
                level=logging.CRITICAL,
            ) as logs,
        ):
            results = scheduler_main.run_startup_prediction_catchup(
                now=now,
                algo_env="forecast_env",
            )

        sync_registry.assert_called_once_with(schemes)
        run_config.assert_called_once_with(
            native,
            "2026-07-09",
            algo_env="forecast_env",
            force=False,
        )
        self.assertEqual(results, [expected])
        self.assertTrue(
            any(
                "runtime_type drift" in message
                for message in logs.output
            )
        )

    def test_startup_prediction_catchup_raises_after_attempting_all_due_jobs(self) -> None:
        from scheduler import main as scheduler_main

        now = datetime(2026, 7, 9, 7, 10, tzinfo=scheduler_main.ASIA_SHANGHAI)
        schemes = [
            _cfg("scheme_a", frequency="weekly"),
            _cfg("scheme_b", frequency="weekly"),
            _cfg("scheme_c", frequency="weekly"),
        ]
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
