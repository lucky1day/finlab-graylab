from __future__ import annotations

import threading
import unittest
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scheduler.daily_coordinator import ItemControlState, plan_dispatches
from scheduler.daily_runtime import (
    DailyRuntime,
    GenerationBuildOutcome,
    _GenerationAvailability,
)
from scheduler.daily_policy import load_daily_policy
from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
BUSINESS_DATE = date(2026, 7, 24)
NATIVE_GENERATION_ID = "native-source-20260724-step6"
INDEPENDENT_FAILURE_SCHEME = "daily_1y_xgb_1y13_0629"
LIVE_SOURCE_0629 = {
    "daily_10y_lgbm_10y04_0629",
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
}


def _real_daily_policy():
    active_daily = tuple(
        config
        for config in discover_schemes(strict=True)
        if config.status == "active" and config.frequency == "daily"
    )
    return load_daily_policy(
        POLICY_PATH,
        discovered=active_daily,
    )


class _NativeMatrixServices:
    """用真实 policy/planner 验证完整 Native 队列的受控执行替身。"""

    def __init__(
        self,
        *,
        failure_scheme_id: str | None = None,
    ) -> None:
        self.policy = _real_daily_policy()
        self.failure_scheme_id = failure_scheme_id
        self.current_time = datetime(
            2026,
            7,
            24,
            6,
            36,
            tzinfo=SHANGHAI,
        )
        self.states = {
            scheme_id: (
                "PENDING"
                if item.runtime_type == "native_adapter"
                else "SUCCESS"
            )
            for scheme_id, item in self.policy.schemes.items()
        }
        self.failure_codes: dict[str, str | None] = {
            scheme_id: None for scheme_id in self.policy.schemes
        }
        self.attempts: Counter[str] = Counter()
        self.execution_modes: dict[str, str] = {}
        self.execution_events: list[tuple[str, str]] = []
        self.alerts: list[dict[str, object]] = []
        self.heartbeats: list[dict[str, object]] = []
        self.observed_resource_signatures: set[
            tuple[str, ...]
        ] = set()
        self._lock = threading.Lock()
        self._first_wave_barrier = threading.Barrier(2)
        self._started_total = 0
        self._active_native = 0
        self.max_active_native = 0

    @property
    def native_ids(self) -> set[str]:
        return {
            scheme_id
            for scheme_id, item in self.policy.schemes.items()
            if item.runtime_type == "native_adapter"
        }

    def now(self) -> datetime:
        return self.current_time

    def read_snapshot(self, occurrence_id: int):
        if occurrence_id != 41:
            raise AssertionError(
                f"unexpected occurrence_id: {occurrence_id}"
            )
        with self._lock:
            summaries = []
            accepted_target_count = 0
            for item_id, scheme_id in enumerate(
                self.policy.schemes,
                start=1,
            ):
                policy_item = self.policy.schemes[scheme_id]
                state = self.states[scheme_id]
                target_count = len(policy_item.target_tenors)
                accepted = target_count if state == "SUCCESS" else 0
                accepted_target_count += accepted
                summaries.append(
                    SimpleNamespace(
                        item=SimpleNamespace(
                            item_id=item_id,
                            base_scheme_id=scheme_id,
                            runtime_type=policy_item.runtime_type,
                            state=state,
                            sla_status="PENDING",
                            attempt_no=self.attempts[scheme_id],
                            failure_code=self.failure_codes[scheme_id],
                            input_generation_id=(
                                NATIVE_GENERATION_ID
                                if policy_item.runtime_type
                                == "native_adapter"
                                else "databridge-step6"
                            ),
                            current_run_id=None,
                        ),
                        target_count=target_count,
                        accepted_target_count=accepted,
                    )
                )
        return SimpleNamespace(
            occurrence=SimpleNamespace(
                occurrence_id=occurrence_id,
                predict_date=BUSINESS_DATE.isoformat(),
                expected_item_count=self.policy.expected_item_count,
                expected_target_count=self.policy.expected_target_count,
                completion_state=(
                    "SUCCESS"
                    if accepted_target_count
                    == self.policy.expected_target_count
                    else "RUNNING"
                ),
            ),
            items=tuple(summaries),
            actual_item_count=len(summaries),
            actual_target_count=sum(
                summary.target_count for summary in summaries
            ),
            actual_accepted_target_count=accepted_target_count,
        )

    def dispatch_decisions(
        self,
        *,
        business_date: date,
        snapshot,
        native_generation,
        databridge_generation,
        running_scheme_ids: tuple[str, ...],
        running_resource_classes: tuple[str, ...],
        now: datetime,
    ):
        del databridge_generation
        states = {
            summary.item.base_scheme_id: ItemControlState(
                scheme_id=summary.item.base_scheme_id,
                state=summary.item.state,
                attempt_no=summary.item.attempt_no,
                failure_code=summary.item.failure_code,
            )
            for summary in snapshot.items
        }
        decisions = plan_dispatches(
            self.policy,
            business_date=business_date,
            now=now,
            item_states=states,
            native_generation_sealed=native_generation is not None,
            native_generation_business_date=(
                date.fromisoformat(native_generation.business_date)
                if native_generation is not None
                else None
            ),
            native_generation_id=(
                native_generation.generation_id
                if native_generation is not None
                else None
            ),
            expected_native_generation_id=NATIVE_GENERATION_ID,
            running_scheme_ids=running_scheme_ids,
            running_resource_classes=running_resource_classes,
        )
        selected = list(running_scheme_ids)
        for decision in decisions:
            if decision.action != "DISPATCH":
                continue
            selected.append(decision.scheme_id)
            signature = tuple(
                sorted(
                    (
                        *(
                            self.policy.schemes[scheme_id].resource_class
                            for scheme_id in selected
                        ),
                        *running_resource_classes,
                    )
                )
            )
            self.observed_resource_signatures.add(signature)
        return decisions

    def execute_item(
        self,
        *,
        item_id: int,
        trigger_origin: str,
    ):
        del trigger_origin
        scheme_id = tuple(self.policy.schemes)[item_id - 1]
        policy_item = self.policy.schemes[scheme_id]
        if policy_item.runtime_type != "native_adapter":
            raise AssertionError(
                f"Step 6 must not execute V2: {scheme_id}"
            )
        with self._lock:
            self.attempts[scheme_id] += 1
            self.execution_modes[scheme_id] = (
                policy_item.input_compatibility
            )
            self.execution_events.append(("start", scheme_id))
            self._started_total += 1
            first_wave = self._started_total <= 2
            self._active_native += 1
            self.max_active_native = max(
                self.max_active_native,
                self._active_native,
            )
        try:
            if first_wave:
                self._first_wave_barrier.wait(timeout=3)
            with self._lock:
                if scheme_id == self.failure_scheme_id:
                    self.states[scheme_id] = "FAILED_TERMINAL"
                    self.failure_codes[scheme_id] = "ALGORITHM"
                    status = "failed"
                else:
                    self.states[scheme_id] = "SUCCESS"
                    status = "success"
                self.execution_events.append(("finish", scheme_id))
        finally:
            with self._lock:
                self._active_native -= 1
        return SimpleNamespace(
            status=status,
            scheme_id=scheme_id,
            failure_code=(
                "ALGORITHM" if status == "failed" else None
            ),
            error_message=(
                "step6 injected independent failure"
                if status == "failed"
                else None
            ),
        )

    def heartbeat(
        self,
        *,
        occurrence_id: int | None,
        state: str,
        details,
    ) -> None:
        self.heartbeats.append(
            {
                "occurrence_id": occurrence_id,
                "state": state,
                "details": details,
            }
        )

    def alert(
        self,
        *,
        code: str,
        severity: str,
        business_date: date,
        occurrence_id: int | None,
        message: str,
        scheme_id: str | None = None,
        details=None,
    ) -> None:
        self.alerts.append(
            {
                "code": code,
                "severity": severity,
                "business_date": business_date,
                "occurrence_id": occurrence_id,
                "message": message,
                "scheme_id": scheme_id,
                "details": details,
            }
        )

    def expire_items(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ) -> int:
        del occurrence_id, evaluated_at
        return 0

    @staticmethod
    def wait_for_progress(seconds: float) -> None:
        del seconds
        threading.Event().wait(0.001)


def _finished_generation_availability(policy):
    availability = _GenerationAvailability(
        policy.allowed_resource_combinations
    )
    availability.finish(
        GenerationBuildOutcome(
            native_generation=SimpleNamespace(
                generation_id=NATIVE_GENERATION_ID,
                business_date=BUSINESS_DATE.isoformat(),
            ),
            databridge_generation=None,
        )
    )
    return availability


class DailyNativeCoordinatorMatrixTests(unittest.TestCase):
    def test_real_native_matrix_dispatches_once_with_two_lanes_and_reentry(
        self,
    ) -> None:
        services = _NativeMatrixServices()
        runtime = DailyRuntime(services)
        availability = _finished_generation_availability(
            services.policy
        )

        first = runtime._drive_items(
            occurrence_id=41,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=availability,
            trigger_origin="apscheduler",
        )
        attempts_after_first = services.attempts.copy()
        second = runtime._drive_items(
            occurrence_id=41,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=availability,
            trigger_origin="startup_catchup",
        )
        final_snapshot = services.read_snapshot(41)

        self.assertEqual(set(first.dispatched_scheme_ids), services.native_ids)
        self.assertEqual(len(first.dispatched_scheme_ids), 17)
        self.assertEqual(second.dispatched_scheme_ids, ())
        self.assertEqual(services.attempts, attempts_after_first)
        self.assertEqual(
            services.attempts,
            Counter({scheme_id: 1 for scheme_id in services.native_ids}),
        )
        self.assertEqual(services.max_active_native, 2)
        self.assertEqual(
            {
                summary.item.input_generation_id
                for summary in final_snapshot.items
                if summary.item.runtime_type == "native_adapter"
            },
            {NATIVE_GENERATION_ID},
        )
        self.assertEqual(
            {
                scheme_id
                for scheme_id, mode in services.execution_modes.items()
                if mode == "live_source_0629"
            },
            LIVE_SOURCE_0629,
        )
        self.assertEqual(
            sum(
                mode == "generation_v1"
                for mode in services.execution_modes.values()
            ),
            14,
        )
        self.assertTrue(services.observed_resource_signatures)
        self.assertTrue(
            services.observed_resource_signatures.issubset(
                services.policy.allowed_resource_combinations
            )
        )
        event_positions = {
            event: index
            for index, event in enumerate(services.execution_events)
        }
        for cache_group in {
            item.cache_group
            for item in services.policy.schemes.values()
            if (
                item.runtime_type == "native_adapter"
                and item.cache_prerequisite
            )
        }:
            members = [
                item
                for item in services.policy.schemes.values()
                if (
                    item.runtime_type == "native_adapter"
                    and item.cache_group == cache_group
                )
            ]
            if len(members) <= 1:
                continue
            prerequisite = next(
                item for item in members if item.cache_prerequisite
            )
            for consumer in members:
                if consumer.scheme_id == prerequisite.scheme_id:
                    continue
                with self.subTest(
                    cache_group=cache_group,
                    consumer=consumer.scheme_id,
                ):
                    self.assertLess(
                        event_positions[
                            ("finish", prerequisite.scheme_id)
                        ],
                        event_positions[("start", consumer.scheme_id)],
                    )

    def test_independent_terminal_failure_does_not_block_other_native_items(
        self,
    ) -> None:
        services = _NativeMatrixServices(
            failure_scheme_id=INDEPENDENT_FAILURE_SCHEME,
        )
        runtime = DailyRuntime(services)

        result = runtime._drive_items(
            occurrence_id=41,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=(
                _finished_generation_availability(services.policy)
            ),
            trigger_origin="apscheduler",
        )
        snapshot = services.read_snapshot(41)

        self.assertEqual(result.status, "finished")
        self.assertEqual(set(result.dispatched_scheme_ids), services.native_ids)
        self.assertEqual(
            services.attempts[INDEPENDENT_FAILURE_SCHEME],
            1,
        )
        self.assertEqual(
            {
                scheme_id
                for scheme_id in services.native_ids
                if services.states[scheme_id] == "SUCCESS"
            },
            services.native_ids - {INDEPENDENT_FAILURE_SCHEME},
        )
        self.assertEqual(
            sum(services.attempts.values()),
            17,
        )
        self.assertLess(
            snapshot.actual_accepted_target_count,
            snapshot.occurrence.expected_target_count,
        )
        terminal_alerts = [
            alert
            for alert in services.alerts
            if alert["code"] == "ITEM_TERMINAL_FAILURE"
        ]
        self.assertEqual(len(terminal_alerts), 1)
        self.assertEqual(
            terminal_alerts[0]["scheme_id"],
            INDEPENDENT_FAILURE_SCHEME,
        )
        self.assertEqual(
            terminal_alerts[0]["details"]["failure_code"],
            "ALGORITHM",
        )


if __name__ == "__main__":
    unittest.main()
