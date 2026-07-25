from __future__ import annotations

import os
import unittest
from contextlib import redirect_stdout
import io
from types import SimpleNamespace
from unittest.mock import Mock, patch


class SchedulerCapacityAdmissionTests(unittest.TestCase):
    def test_ledger_scheduler_checks_current_capacity_before_registry_mutation(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        engine = SimpleNamespace(dispose=Mock())
        schemes = [
            SimpleNamespace(
                scheme_id="daily_native",
                frequency="daily",
            )
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
                "require_current_capacity_admission",
                side_effect=RuntimeError("capacity blocked"),
                create=True,
            ) as admission,
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ) as discovery,
            patch.object(
                scheduler_main,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
            ) as sync_registry,
            self.assertRaisesRegex(RuntimeError, "capacity blocked"),
        ):
            scheduler_main.build_scheduler()

        discovery.assert_called_once_with()
        admission.assert_called_once_with(
            engine,
            discovered=schemes,
            algo_env="forecast_env",
        )
        engine.dispose.assert_called_once_with()
        sync_registry.assert_not_called()

    def test_legacy_scheduler_does_not_require_capacity_admission(
        self,
    ) -> None:
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
                "require_daily_capacity_admission",
                side_effect=AssertionError("must not be called"),
                create=True,
            ) as admission,
            patch.object(
                scheduler_main,
                "require_current_capacity_admission",
                side_effect=AssertionError("must not be called"),
                create=True,
            ) as current_admission,
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
            self.assertIsNotNone(scheduler)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        admission.assert_not_called()
        current_admission.assert_not_called()

    def test_admitted_ledger_scheduler_never_auto_syncs_registry(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        engine = SimpleNamespace(dispose=Mock())
        schemes: list[object] = []
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
                "require_current_capacity_admission",
                return_value={"status": "ADMITTED"},
            ) as admission,
            patch.object(
                scheduler_main,
                "discover_schemes",
                return_value=schemes,
            ),
            patch.object(
                scheduler_main,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                scheduler_main,
                "_sync_registry",
            ) as sync_registry,
        ):
            scheduler = scheduler_main.build_scheduler()

        try:
            self.assertIsNotNone(scheduler)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        admission.assert_called_once_with(
            engine,
            discovered=schemes,
            algo_env="forecast_env",
        )
        engine.dispose.assert_called_once_with()
        sync_registry.assert_not_called()

    def test_direct_coordinator_delegates_admission_to_inner_runtime(
        self,
    ) -> None:
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
            patch(
                "scheduler.daily_runtime.run_daily_occurrence",
                side_effect=RuntimeError("capacity blocked"),
            ) as runtime,
            self.assertRaisesRegex(RuntimeError, "capacity blocked"),
        ):
            scheduler_main.run_daily_coordinator_job(
                run_date="2026-07-24",
            )

        runtime.assert_called_once()

    def test_operator_recovery_delegates_admission_to_inner_runtime(
        self,
    ) -> None:
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
            patch(
                "scheduler.daily_runtime.run_operator_recovery",
                side_effect=RuntimeError("capacity blocked"),
            ) as runtime,
            self.assertRaisesRegex(RuntimeError, "capacity blocked"),
        ):
            scheduler_main.run_daily_operator_recovery_job(
                "scheme-a",
                run_date="2026-07-24",
            )

        runtime.assert_called_once()

    def test_ledger_entries_reject_legacy_mode_before_inner_runtime(
        self,
    ) -> None:
        from scheduler import main as scheduler_main

        entries = (
            lambda: scheduler_main.run_daily_coordinator_job(
                run_date="2026-07-24",
            ),
            lambda: scheduler_main.run_daily_operator_recovery_job(
                "scheme-a",
                run_date="2026-07-24",
            ),
            lambda: scheduler_main.run_daily_watchdog_job(
                "sla",
                run_date="2026-07-24",
            ),
            lambda: scheduler_main.run_daily_heartbeat_job(
                run_date="2026-07-24",
            ),
        )
        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
            ),
            patch(
                "scheduler.daily_runtime.run_daily_occurrence",
            ) as occurrence,
            patch(
                "scheduler.daily_runtime.run_operator_recovery",
            ) as recovery,
            patch(
                "scheduler.daily_runtime.run_daily_watchdog",
            ) as watchdog,
            patch(
                "scheduler.daily_runtime.run_scheduler_heartbeat",
            ) as heartbeat,
        ):
            for entry in entries:
                with self.subTest(entry=entry), self.assertRaisesRegex(
                    RuntimeError,
                    "ledger coordinator mode",
                ):
                    entry()

        occurrence.assert_not_called()
        recovery.assert_not_called()
        watchdog.assert_not_called()
        heartbeat.assert_not_called()

    def test_main_reports_blocked_admission_as_configuration_error(
        self,
    ) -> None:
        from scheduler import main as scheduler_main
        from scheduler.capacity_admission import CapacityAdmissionError

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
                "run_daily_coordinator_job",
                side_effect=CapacityAdmissionError("capacity blocked"),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = scheduler_main.main(
                ["--run-once", "predictions"],
            )

        self.assertEqual(exit_code, 2)
        self.assertIn('"exit_code": 2', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
