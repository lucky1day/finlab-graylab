from __future__ import annotations

import threading
import unittest
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scheduler.daily_coordinator import (
    ItemControlState,
    compute_v2_releases,
    plan_dispatches,
)
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
SEALED_AT = datetime(2026, 7, 24, 6, 50, tzinfo=SHANGHAI)
NATIVE_GENERATION_ID = "native-source-20260724-step7"
DATABRIDGE_GENERATION_ID = "databridge-v1-20260724-step7"
V2_IDS = (
    "one_y_t5_liq_excess_a_v1",
    "one_y_t5_liq_excess_a_w252_l7_v1",
    "one_y_t5_liq_excess_a_w350_l7_v1",
    "one_y_t5_liq_excess_b_w252_l7_v1",
)


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


def _v2_release_map(policy) -> dict[str, datetime]:
    return {
        release.scheme_id: release.release_at
        for release in compute_v2_releases(
            policy,
            sealed_at=SEALED_AT,
        )
    }


def _control_states(policy) -> dict[str, ItemControlState]:
    return {
        scheme_id: ItemControlState(
            scheme_id=scheme_id,
            state=(
                "PENDING"
                if item.runtime_type == "blackbox_v2"
                else "SUCCESS"
            ),
        )
        for scheme_id, item in policy.schemes.items()
    }


def _plan_v2(
    policy,
    *,
    now: datetime,
    states: dict[str, ItemControlState] | None = None,
    databridge_business_date: date | None = BUSINESS_DATE,
    databridge_generation_id: str | None = DATABRIDGE_GENERATION_ID,
    expected_generation_id: str | None = DATABRIDGE_GENERATION_ID,
):
    return plan_dispatches(
        policy,
        business_date=BUSINESS_DATE,
        now=now,
        item_states=states or _control_states(policy),
        native_generation_sealed=True,
        native_generation_business_date=BUSINESS_DATE,
        native_generation_id=NATIVE_GENERATION_ID,
        expected_native_generation_id=NATIVE_GENERATION_ID,
        databridge_generation_business_date=databridge_business_date,
        databridge_generation_id=databridge_generation_id,
        expected_databridge_generation_id=expected_generation_id,
        v2_release_at_by_scheme=_v2_release_map(policy),
    )


class _V2MatrixServices:
    """用真实 policy/planner 驱动四个 V2 的受控执行替身。"""

    def __init__(
        self,
        *,
        failure_scheme_id: str | None = None,
        current_time: datetime | None = None,
        synchronize_first_wave: bool = True,
    ) -> None:
        self.policy = _real_daily_policy()
        self.failure_scheme_id = failure_scheme_id
        self.synchronize_first_wave = synchronize_first_wave
        self.current_time = current_time or SEALED_AT + timedelta(minutes=6)
        self.releases = _v2_release_map(self.policy)
        self.states = {
            scheme_id: (
                "PENDING"
                if item.runtime_type == "blackbox_v2"
                else "SUCCESS"
            )
            for scheme_id, item in self.policy.schemes.items()
        }
        self.failure_codes: dict[str, str | None] = {
            scheme_id: None for scheme_id in self.policy.schemes
        }
        self.sla_statuses: dict[str, str] = {
            scheme_id: "PENDING" for scheme_id in self.policy.schemes
        }
        self.attempts: Counter[str] = Counter()
        self.generation_ids: dict[str, str] = {}
        self.execution_start_times: dict[str, datetime] = {}
        self.alerts: list[dict[str, object]] = []
        self.heartbeats: list[dict[str, object]] = []
        self.observed_resource_signatures: set[
            tuple[str, ...]
        ] = set()
        self._lock = threading.Lock()
        self._first_wave_barrier = threading.Barrier(2)
        self._started_total = 0
        self._active_v2 = 0
        self.max_active_v2 = 0

    def now(self) -> datetime:
        return self.current_time

    def read_snapshot(self, occurrence_id: int):
        if occurrence_id != 42:
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
                            sla_status=self.sla_statuses[scheme_id],
                            attempt_no=self.attempts[scheme_id],
                            failure_code=self.failure_codes[scheme_id],
                            input_generation_id=(
                                DATABRIDGE_GENERATION_ID
                                if policy_item.runtime_type == "blackbox_v2"
                                else NATIVE_GENERATION_ID
                            ),
                            release_at=self.releases.get(scheme_id),
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
                feature_date="2026-07-23",
                expected_item_count=self.policy.expected_item_count,
                expected_target_count=self.policy.expected_target_count,
                sla_outcome="PENDING",
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
        if business_date != BUSINESS_DATE:
            raise AssertionError(f"unexpected business date: {business_date}")
        if native_generation.generation_id != NATIVE_GENERATION_ID:
            raise AssertionError("Native generation drifted")
        if (
            databridge_generation.generation_id
            != DATABRIDGE_GENERATION_ID
        ):
            raise AssertionError("DataBridge generation drifted")
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
            native_generation_sealed=True,
            native_generation_business_date=BUSINESS_DATE,
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
            databridge_generation_business_date=BUSINESS_DATE,
            databridge_generation_id=DATABRIDGE_GENERATION_ID,
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
            v2_release_at_by_scheme=self.releases,
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
        if policy_item.runtime_type != "blackbox_v2":
            raise AssertionError(
                f"Step 7 must not execute Native: {scheme_id}"
            )
        with self._lock:
            self.attempts[scheme_id] += 1
            self.generation_ids[scheme_id] = DATABRIDGE_GENERATION_ID
            self.execution_start_times[scheme_id] = self.current_time
            self._started_total += 1
            first_wave = self._started_total <= 2
            self._active_v2 += 1
            self.max_active_v2 = max(
                self.max_active_v2,
                self._active_v2,
            )
        try:
            if first_wave and self.synchronize_first_wave:
                self._first_wave_barrier.wait(timeout=3)
            with self._lock:
                if scheme_id == self.failure_scheme_id:
                    self.states[scheme_id] = "FAILED_TERMINAL"
                    self.failure_codes[scheme_id] = "ALGORITHM"
                    status = "failed"
                else:
                    self.states[scheme_id] = "SUCCESS"
                    status = "success"
        finally:
            with self._lock:
                self._active_v2 -= 1
        return SimpleNamespace(
            status=status,
            scheme_id=scheme_id,
            failure_code=(
                "ALGORITHM" if status == "failed" else None
            ),
            error_message=(
                "step7 injected independent failure"
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

    def find_occurrence_id(self, business_date: date) -> int | None:
        return 42 if business_date == BUSINESS_DATE else None

    def validate_occurrence_epoch(self, *, snapshot) -> None:
        if snapshot.occurrence.occurrence_id != 42:
            raise AssertionError("unexpected occurrence epoch")

    def load_policy(self):
        return self.policy

    def load_current_audit_policy(self):
        return self.policy

    def validate_occurrence_policy(self, *, snapshot, policy) -> None:
        if (
            snapshot.actual_item_count != policy.expected_item_count
            or snapshot.actual_target_count != policy.expected_target_count
        ):
            raise AssertionError("frozen occurrence cardinality drifted")

    @staticmethod
    def find_unbound_building_generations(
        *,
        business_date: date,
    ) -> tuple[object, ...]:
        del business_date
        return ()

    @staticmethod
    def detect_late_writes(
        *,
        business_date: date,
        feature_date: str,
    ) -> tuple[object, ...]:
        del business_date, feature_date
        return ()

    def evaluate_v2_start(
        self,
        *,
        item_id: int,
        evaluated_at: datetime,
    ):
        del evaluated_at
        scheme_id = tuple(self.policy.schemes)[item_id - 1]
        if self.policy.schemes[scheme_id].runtime_type != "blackbox_v2":
            return SimpleNamespace(
                status="ON_TIME",
                reason=None,
                newly_persisted=False,
            )
        newly_persisted = self.sla_statuses[scheme_id] == "PENDING"
        self.sla_statuses[scheme_id] = "LATE"
        return SimpleNamespace(
            status="LATE",
            reason="V2_NOT_STARTED_BY_0745",
            newly_persisted=newly_persisted,
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

    def wait_for_progress(self, seconds: float) -> None:
        del seconds
        with self._lock:
            self.current_time += timedelta(minutes=1)
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
            databridge_generation=SimpleNamespace(
                generation_id=DATABRIDGE_GENERATION_ID,
                business_date=BUSINESS_DATE.isoformat(),
            ),
        )
    )
    return availability


class DailyV2CoordinatorMatrixTests(unittest.TestCase):
    def test_real_policy_freezes_exact_independent_release_matrix(
        self,
    ) -> None:
        policy = _real_daily_policy()
        v2_items = tuple(
            item
            for item in policy.schemes.values()
            if item.runtime_type == "blackbox_v2"
        )

        self.assertEqual(
            tuple(item.scheme_id for item in v2_items),
            V2_IDS,
        )
        self.assertEqual(
            tuple(item.v2_release_offset_min for item in v2_items),
            (0, 2, 4, 6),
        )
        self.assertEqual(
            tuple(
                release.release_at
                for release in compute_v2_releases(
                    policy,
                    sealed_at=SEALED_AT,
                )
            ),
            tuple(
                SEALED_AT + timedelta(minutes=offset)
                for offset in (0, 2, 4, 6)
            ),
        )

        states = _control_states(policy)
        states[V2_IDS[0]] = replace(
            states[V2_IDS[0]],
            state="FAILED_TERMINAL",
            failure_code="ALGORITHM",
        )
        by_id = {
            decision.scheme_id: decision
            for decision in _plan_v2(
                policy,
                now=SEALED_AT + timedelta(minutes=2),
                states=states,
            )
        }

        self.assertEqual(by_id[V2_IDS[0]].action, "TERMINAL")
        self.assertEqual(by_id[V2_IDS[1]].action, "DISPATCH")
        self.assertEqual(by_id[V2_IDS[2]].action, "WAIT_RELEASE")

    def test_old_mismatched_and_unbound_databridge_never_release_v2(
        self,
    ) -> None:
        policy = _real_daily_policy()
        cases = (
            (
                {
                    "databridge_business_date": BUSINESS_DATE
                    - timedelta(days=1),
                },
                "DATABRIDGE_GENERATION_NOT_CURRENT",
            ),
            (
                {
                    "databridge_generation_id":
                        "databridge-v1-20260724-old",
                },
                "DATABRIDGE_GENERATION_MISMATCH",
            ),
            (
                {
                    "databridge_generation_id": None,
                    "expected_generation_id": None,
                },
                "DATABRIDGE_GENERATION_IDENTITY_MISSING",
            ),
        )

        for kwargs, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                by_id = {
                    decision.scheme_id: decision
                    for decision in _plan_v2(
                        policy,
                        now=SEALED_AT + timedelta(minutes=6),
                        **kwargs,
                    )
                }
                self.assertTrue(
                    all(
                        by_id[scheme_id].action == "WAIT_GENERATION"
                        for scheme_id in V2_IDS
                    )
                )
                self.assertTrue(
                    all(
                        by_id[scheme_id].reason == expected_reason
                        for scheme_id in V2_IDS
                    )
                )

    def test_real_v2_matrix_dispatches_once_with_two_lanes_and_reentry(
        self,
    ) -> None:
        services = _V2MatrixServices()
        runtime = DailyRuntime(services)
        availability = _finished_generation_availability(
            services.policy
        )

        first = runtime._drive_items(
            occurrence_id=42,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=availability,
            trigger_origin="apscheduler",
        )
        attempts_after_first = services.attempts.copy()
        second = runtime._drive_items(
            occurrence_id=42,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=availability,
            trigger_origin="startup_catchup",
        )

        self.assertEqual(
            set(first.dispatched_scheme_ids),
            set(V2_IDS),
        )
        self.assertEqual(second.dispatched_scheme_ids, ())
        self.assertEqual(services.attempts, attempts_after_first)
        self.assertEqual(
            services.attempts,
            Counter({scheme_id: 1 for scheme_id in V2_IDS}),
        )
        self.assertEqual(services.max_active_v2, 2)
        self.assertEqual(
            services.generation_ids,
            {
                scheme_id: DATABRIDGE_GENERATION_ID
                for scheme_id in V2_IDS
            },
        )
        self.assertTrue(services.observed_resource_signatures)
        self.assertTrue(
            services.observed_resource_signatures.issubset(
                services.policy.allowed_resource_combinations
            )
        )

    def test_runtime_starts_each_v2_at_its_own_release_time(
        self,
    ) -> None:
        services = _V2MatrixServices(
            current_time=SEALED_AT,
            synchronize_first_wave=False,
        )

        result = DailyRuntime(services)._drive_items(
            occurrence_id=42,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=(
                _finished_generation_availability(services.policy)
            ),
            trigger_origin="apscheduler",
        )

        self.assertEqual(
            set(result.dispatched_scheme_ids),
            set(V2_IDS),
        )
        self.assertEqual(
            tuple(
                services.execution_start_times[scheme_id]
                for scheme_id in V2_IDS
            ),
            tuple(
                SEALED_AT + timedelta(minutes=offset)
                for offset in (0, 2, 4, 6)
            ),
        )

    def test_v2_algorithm_failure_does_not_block_other_releases(
        self,
    ) -> None:
        services = _V2MatrixServices(
            failure_scheme_id=V2_IDS[0],
        )
        runtime = DailyRuntime(services)

        result = runtime._drive_items(
            occurrence_id=42,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=(
                _finished_generation_availability(services.policy)
            ),
            trigger_origin="apscheduler",
        )

        self.assertEqual(result.status, "finished")
        self.assertEqual(
            services.attempts,
            Counter({scheme_id: 1 for scheme_id in V2_IDS}),
        )
        self.assertEqual(
            services.states[V2_IDS[0]],
            "FAILED_TERMINAL",
        )
        self.assertTrue(
            all(
                services.states[scheme_id] == "SUCCESS"
                for scheme_id in V2_IDS[1:]
            )
        )
        terminal_alerts = [
            alert
            for alert in services.alerts
            if alert["code"] == "ITEM_TERMINAL_FAILURE"
        ]
        self.assertEqual(len(terminal_alerts), 1)
        self.assertEqual(
            terminal_alerts[0]["scheme_id"],
            V2_IDS[0],
        )
        self.assertEqual(
            terminal_alerts[0]["details"]["failure_code"],
            "ALGORITHM",
        )

    def test_0745_alerts_late_then_still_executes_all_due_v2(
        self,
    ) -> None:
        services = _V2MatrixServices(
            current_time=datetime(
                2026,
                7,
                24,
                7,
                45,
                tzinfo=SHANGHAI,
            )
        )
        runtime = DailyRuntime(services)

        watchdog = runtime.run_watchdog(
            stage="v2_start_guardrail",
            run_date=BUSINESS_DATE,
        )
        execution = runtime._drive_items(
            occurrence_id=42,
            business_date=BUSINESS_DATE,
            policy=services.policy,
            generation_availability=(
                _finished_generation_availability(services.policy)
            ),
            trigger_origin="apscheduler",
        )

        self.assertEqual(watchdog.status, "late")
        self.assertEqual(
            {
                alert["scheme_id"]
                for alert in services.alerts
                if alert["code"] == "V2_START_LATE"
            },
            set(V2_IDS),
        )
        self.assertEqual(
            set(execution.dispatched_scheme_ids),
            set(V2_IDS),
        )
        self.assertEqual(
            services.attempts,
            Counter({scheme_id: 1 for scheme_id in V2_IDS}),
        )


if __name__ == "__main__":
    unittest.main()
