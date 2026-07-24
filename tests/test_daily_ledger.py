from __future__ import annotations

import importlib
import inspect
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime


def _ledger_module():
    try:
        return importlib.import_module("scheduler.daily_ledger")
    except ModuleNotFoundError:
        return None


class DailyFailureCodeVocabularyTests(unittest.TestCase):
    def test_failure_codes_are_one_stable_canonical_vocabulary(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        expected = {
            "TRANSIENT_INFRA",
            "TIMEOUT",
            "DATA",
            "CONTRACT",
            "ALGORITHM",
            "RESULT",
            "NATIVE_GENERATION_UNSUPPORTED",
            "GENERATION_BUILD_FAILED",
            "GENERATION_HASH_MISMATCH",
            "GENERATION_INVALIDATED",
            "ABANDONED_FENCE_PENDING_CLEANUP",
            "ABANDONED_ORPHAN_CLEANUP",
            "RECOVERY_CUTOFF_EXPIRED",
            "STALE_ATTEMPT",
            "NO_CROSS_DAY",
            "INVALID_ITEM_STATE",
        }

        self.assertEqual(ledger.SCHEDULE_FAILURE_CODES, frozenset(expected))
        self.assertEqual(
            ledger.TERMINAL_EXECUTION_FAILURE_CODES,
            frozenset({"TIMEOUT", "DATA", "CONTRACT", "ALGORITHM", "RESULT"}),
        )
        self.assertEqual(
            ledger.NONEXECUTED_FAILURE_CODES,
            frozenset(
                {
                    "NATIVE_GENERATION_UNSUPPORTED",
                    "GENERATION_BUILD_FAILED",
                    "GENERATION_HASH_MISMATCH",
                    "GENERATION_INVALIDATED",
                }
            ),
        )
        for legacy in (
            "DATA_ERROR",
            "CONTRACT_ERROR",
            "ALGORITHM_ERROR",
            "RESULT_STRUCTURE_ERROR",
            "RECOVERY_CUTOFF_REACHED",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, ledger.SCHEDULE_FAILURE_CODES)


class DailyRegistrySnapshotTests(unittest.TestCase):
    def test_freezes_active_daily_rows_into_base_items_and_targets(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        rows = [
            {
                "scheme_id": "alpha__h1__5Y",
                "base_scheme_id": "alpha",
                "runtime_type": "native_adapter",
                "status": "active",
                "frequency": "daily",
                "task_type": "T+1",
                "target_tenor": "5Y",
                "horizon": 1,
                "target_date": "2026-07-27",
                "scheme_version": "alpha-v1",
                "code_sha256": "a" * 64,
                "config_sha256": "b" * 64,
                "cache_group": "alpha-cache",
                "resource_class": "cpu_standard",
                "internal_workers": 2,
                "release_offset_minutes": 0,
                "release_at": "2026-07-23 23:00:00",
                "deadline_at": "2026-07-24 00:30:00",
            },
            {
                "scheme_id": "alpha__h1__10Y",
                "base_scheme_id": "alpha",
                "runtime_type": "native_adapter",
                "status": "active",
                "frequency": "daily",
                "task_type": "T+1",
                "target_tenor": "10Y",
                "horizon": 1,
                "target_date": "2026-07-27",
                "scheme_version": "alpha-v1",
                "code_sha256": "a" * 64,
                "config_sha256": "b" * 64,
                "cache_group": "alpha-cache",
                "resource_class": "cpu_standard",
                "internal_workers": 2,
                "release_offset_minutes": 0,
                "release_at": "2026-07-23 23:00:00",
                "deadline_at": "2026-07-24 00:30:00",
            },
            {
                "scheme_id": "beta__h5__1Y",
                "base_scheme_id": "beta",
                "runtime_type": "blackbox_v2",
                "status": "active",
                "frequency": "daily",
                "task_type": "T+5",
                "target_tenor": "1Y",
                "horizon": 5,
                "target_date": "2026-07-31",
                "scheme_version": "beta-v1",
                "code_sha256": "c" * 64,
                "config_sha256": "d" * 64,
                "cache_group": "beta-cache",
                "resource_class": "cpu_heavy",
                "internal_workers": 4,
                "release_offset_minutes": 4,
                "release_at": "2026-07-23 23:15:00",
                "deadline_at": "2026-07-24 00:30:00",
            },
            {
                "scheme_id": "paused__h1__7Y",
                "base_scheme_id": "paused",
                "runtime_type": "native_adapter",
                "status": "paused",
                "frequency": "daily",
                "task_type": "T+1",
                "target_tenor": "7Y",
                "horizon": 1,
            },
            {
                "scheme_id": "weekly__h6__10Y",
                "base_scheme_id": "weekly",
                "runtime_type": "native_adapter",
                "status": "active",
                "frequency": "weekly",
                "task_type": "weekly_point",
                "target_tenor": "10Y",
                "horizon": 6,
            },
        ]

        snapshot = ledger.freeze_active_daily_registry(rows)

        self.assertEqual(snapshot.expected_item_count, 2)
        self.assertEqual(snapshot.expected_target_count, 3)
        self.assertRegex(
            getattr(snapshot, "registry_digest", ""),
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            [item.base_scheme_id for item in snapshot.items],
            ["alpha", "beta"],
        )
        self.assertEqual(
            [
                target.registry_scheme_id
                for item in snapshot.items
                for target in item.targets
            ],
            ["alpha__h1__10Y", "alpha__h1__5Y", "beta__h5__1Y"],
        )
        self.assertEqual(
            getattr(snapshot.items[0], "scheme_version", None),
            "alpha-v1",
        )
        self.assertEqual(getattr(snapshot.items[0], "code_sha256", None), "a" * 64)
        self.assertEqual(
            getattr(snapshot.items[0], "config_sha256", None),
            "b" * 64,
        )
        self.assertEqual(
            getattr(snapshot.items[0], "cache_group", None),
            "alpha-cache",
        )
        self.assertEqual(
            getattr(snapshot.items[1], "release_offset_minutes", None),
            4,
        )
        self.assertEqual(
            getattr(snapshot.items[0].targets[0], "target_date", None),
            "2026-07-27",
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.items[0].base_scheme_id = "changed"

    def test_validates_occurrence_counts_from_snapshot_not_fixed_baseline(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        snapshot = ledger.freeze_active_daily_registry(
            [
                {
                    "scheme_id": "only__h1__3Y",
                    "base_scheme_id": "only",
                    "runtime_type": "native_adapter",
                    "status": "active",
                    "frequency": "daily",
                    "task_type": "T+1",
                    "target_tenor": "3Y",
                    "horizon": 1,
                    "target_date": "2026-07-27",
                    "scheme_version": "only-v1",
                    "code_sha256": "a" * 64,
                    "config_sha256": "b" * 64,
                    "cache_group": "only-cache",
                    "resource_class": "cpu_standard",
                    "internal_workers": 1,
                    "release_offset_minutes": 0,
                    "release_at": "2026-07-23 23:00:00",
                    "deadline_at": "2026-07-24 00:30:00",
                }
            ]
        )

        ledger.validate_snapshot_cardinality(
            snapshot,
            actual_item_count=1,
            actual_target_count=1,
        )
        with self.assertRaisesRegex(
            ValueError,
            "expected items=1 targets=1, got items=1 targets=0",
        ):
            ledger.validate_snapshot_cardinality(
                snapshot,
                actual_item_count=1,
                actual_target_count=0,
            )

    def test_rejects_duplicate_target_identity_in_registry_snapshot(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        duplicate = {
            "scheme_id": "alpha__h1__5Y",
            "base_scheme_id": "alpha",
            "runtime_type": "native_adapter",
            "status": "active",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "5Y",
            "horizon": 1,
            "target_date": "2026-07-27",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "cache_group": "alpha-cache",
            "resource_class": "cpu_standard",
            "internal_workers": 2,
            "release_offset_minutes": 0,
            "release_at": "2026-07-23 23:00:00",
            "deadline_at": "2026-07-24 00:30:00",
        }

        with self.assertRaisesRegex(ValueError, "duplicate Registry scheme_id"):
            ledger.freeze_active_daily_registry([duplicate, dict(duplicate)])

    def test_rejects_task_horizon_and_composite_registry_identity_drift(
        self,
    ) -> None:
        ledger = _ledger_module()
        row = {
            "scheme_id": "alpha__h1__5Y",
            "base_scheme_id": "alpha",
            "runtime_type": "native_adapter",
            "status": "active",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "5Y",
            "horizon": 1,
            "target_date": "2026-07-27",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "cache_group": "alpha-cache",
            "resource_class": "cpu_standard",
            "internal_workers": 2,
            "release_offset_minutes": 0,
            "release_at": "2026-07-23 23:00:00",
            "deadline_at": "2026-07-24 00:30:00",
        }

        with self.assertRaisesRegex(ValueError, "task_type/horizon mismatch"):
            ledger.freeze_active_daily_registry(
                [{**row, "task_type": "T+5"}]
            )
        with self.assertRaisesRegex(ValueError, "composite Registry scheme_id"):
            ledger.freeze_active_daily_registry(
                [{**row, "scheme_id": "wrong"}]
            )

    def test_rejects_missing_daily_resource_policy(self) -> None:
        ledger = _ledger_module()
        row = {
            "scheme_id": "alpha__h1__5Y",
            "base_scheme_id": "alpha",
            "runtime_type": "native_adapter",
            "status": "active",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "5Y",
            "horizon": 1,
            "target_date": "2026-07-27",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "cache_group": "alpha-cache",
        }

        with self.assertRaisesRegex(ValueError, "resource_class"):
            ledger.freeze_active_daily_registry([row])

    def test_rejects_missing_or_negative_release_offset(self) -> None:
        ledger = _ledger_module()
        row = {
            "scheme_id": "alpha__h1__5Y",
            "base_scheme_id": "alpha",
            "runtime_type": "native_adapter",
            "status": "active",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "5Y",
            "horizon": 1,
            "target_date": "2026-07-27",
            "scheme_version": "alpha-v1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "cache_group": "alpha-cache",
            "resource_class": "cpu_standard",
            "internal_workers": 2,
            "release_at": "2026-07-23 23:00:00",
            "deadline_at": "2026-07-24 00:30:00",
        }

        with self.assertRaisesRegex(ValueError, "release_offset_minutes"):
            ledger.freeze_active_daily_registry([row])
        with self.assertRaisesRegex(ValueError, "release_offset_minutes"):
            ledger.freeze_active_daily_registry(
                [{**row, "release_offset_minutes": -1}]
            )

    def test_generation_state_constant_uses_invalidated_terminal_name(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")

        self.assertEqual(
            getattr(ledger, "GENERATION_INVALIDATED", None),
            "INVALIDATED",
        )
        self.assertFalse(hasattr(ledger, "GENERATION_INVALID"))

    def test_item_state_constants_distinguish_retry_terminal_and_expired(
        self,
    ) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")

        self.assertEqual(
            {
                getattr(ledger, "ITEM_PENDING", None),
                getattr(ledger, "ITEM_RUNNING", None),
                getattr(ledger, "ITEM_RETRY_WAIT", None),
                getattr(ledger, "ITEM_SUCCESS", None),
                getattr(ledger, "ITEM_FAILED_TERMINAL", None),
                getattr(ledger, "ITEM_ABANDONED", None),
                getattr(ledger, "ITEM_EXPIRED", None),
            },
            {
                "PENDING",
                "RUNNING",
                "RETRY_WAIT",
                "SUCCESS",
                "FAILED_TERMINAL",
                "ABANDONED",
                "EXPIRED",
            },
        )


class DailySlaProjectionTests(unittest.TestCase):
    def test_predeadline_sla_waits_for_commit_visible_receipts(self) -> None:
        ledger = _ledger_module()

        projection = ledger.project_target_availability_sla(
            current=None,
            visible_target_count=24,
            accepted_by_deadline_count=24,
            expected_target_count=25,
            evaluated_at=datetime(2026, 7, 24, 7, 59),
            deadline_at=datetime(2026, 7, 24, 8, 0),
        )

        self.assertEqual(projection.status, ledger.SLA_PENDING)
        self.assertEqual(projection.accepted_target_count, 24)

    def test_target_sla_breach_is_write_once_after_deadline(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        deadline = datetime(2026, 7, 24, 8, 0)

        at_deadline = ledger.project_target_availability_sla(
            current=None,
            visible_target_count=25,
            accepted_by_deadline_count=24,
            expected_target_count=25,
            evaluated_at=datetime(2026, 7, 24, 8, 5),
            deadline_at=deadline,
        )
        after_fill = ledger.project_target_availability_sla(
            current=at_deadline,
            visible_target_count=25,
            accepted_by_deadline_count=24,
            expected_target_count=25,
            evaluated_at=datetime(2026, 7, 24, 8, 10),
            deadline_at=deadline,
        )

        self.assertEqual(at_deadline.status, ledger.SLA_BREACHED)
        self.assertEqual(at_deadline.reason, "TARGETS_INCOMPLETE_AT_DEADLINE")
        self.assertEqual(at_deadline.accepted_target_count, 25)
        self.assertEqual(at_deadline.accepted_by_deadline_count, 24)
        self.assertEqual(after_fill, at_deadline)

    def test_target_sla_is_met_when_all_frozen_targets_are_available(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        projection = ledger.project_target_availability_sla(
            current=None,
            visible_target_count=3,
            accepted_by_deadline_count=3,
            expected_target_count=3,
            evaluated_at=datetime(2026, 7, 24, 7, 59),
            deadline_at=datetime(2026, 7, 24, 8, 0),
        )

        self.assertEqual(projection.status, ledger.SLA_MET)
        self.assertIsNone(projection.reason)

    def test_terminal_target_sla_refreshes_current_health_count_only(self) -> None:
        ledger = _ledger_module()
        deadline = datetime(2026, 7, 24, 8, 0)
        breached = ledger.project_target_availability_sla(
            current=None,
            visible_target_count=24,
            accepted_by_deadline_count=24,
            expected_target_count=25,
            evaluated_at=deadline,
            deadline_at=deadline,
        )

        refreshed = ledger.project_target_availability_sla(
            current=breached,
            visible_target_count=25,
            accepted_by_deadline_count=24,
            expected_target_count=25,
            evaluated_at=datetime(2026, 7, 24, 8, 5),
            deadline_at=deadline,
        )

        self.assertEqual(refreshed.status, ledger.SLA_BREACHED)
        self.assertEqual(refreshed.evaluated_at, breached.evaluated_at)
        self.assertEqual(refreshed.reason, breached.reason)
        self.assertEqual(refreshed.accepted_target_count, 25)
        self.assertEqual(refreshed.accepted_by_deadline_count, 24)

    def test_v2_start_guardrail_projects_late_only_while_not_started(self) -> None:
        ledger = _ledger_module()
        self.assertIsNotNone(ledger, "scheduler.daily_ledger is missing")
        self.assertIn(
            "current",
            inspect.signature(ledger.project_v2_start_guardrail).parameters,
        )
        deadline = datetime(2026, 7, 24, 7, 45)

        late = ledger.project_v2_start_guardrail(
            current=None,
            runtime_type="blackbox_v2",
            started_at=None,
            evaluated_at=deadline,
            deadline_at=deadline,
        )
        after_late_start = ledger.project_v2_start_guardrail(
            current=late,
            runtime_type="blackbox_v2",
            started_at=datetime(2026, 7, 24, 7, 50),
            evaluated_at=datetime(2026, 7, 24, 8, 0),
            deadline_at=deadline,
        )
        started_on_time = ledger.project_v2_start_guardrail(
            current=None,
            runtime_type="blackbox_v2",
            started_at=datetime(2026, 7, 24, 7, 44),
            evaluated_at=datetime(2026, 7, 24, 8, 0),
            deadline_at=deadline,
        )
        native = ledger.project_v2_start_guardrail(
            current=None,
            runtime_type="native_adapter",
            started_at=None,
            evaluated_at=datetime(2026, 7, 24, 8, 0),
            deadline_at=deadline,
        )

        self.assertEqual(late.status, ledger.ITEM_SLA_LATE)
        self.assertEqual(late.reason, "V2_NOT_STARTED_BY_0745")
        self.assertEqual(after_late_start, late)
        self.assertEqual(started_on_time.status, ledger.ITEM_SLA_ON_TIME)
        self.assertIsNone(started_on_time.reason)
        self.assertEqual(native.status, ledger.GUARDRAIL_NOT_APPLICABLE)


if __name__ == "__main__":
    unittest.main()
