from __future__ import annotations

import importlib
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
LIVE_SOURCE_0629 = {
    "daily_10y_lgbm_10y04_0629",
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
}
V2_IDS = (
    "one_y_t5_liq_excess_a_v1",
    "one_y_t5_liq_excess_a_w252_l7_v1",
    "one_y_t5_liq_excess_a_w350_l7_v1",
    "one_y_t5_liq_excess_b_w252_l7_v1",
)
NATIVE_GENERATION_ID = "native-source-20260724-g1"
DATABRIDGE_GENERATION_ID = "databridge-v1-20260724-g1"


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None


class DailyCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy_module = _module("scheduler.daily_policy")
        self.coordinator = _module("scheduler.daily_coordinator")
        if self.policy_module is not None:
            active_daily = tuple(
                config
                for config in discover_schemes()
                if config.status == "active" and config.frequency == "daily"
            )
            self.policy = self.policy_module.load_daily_policy(
                POLICY_PATH,
                discovered=active_daily,
            )

    def _v2_release_map(
        self,
        sealed_at: datetime,
    ) -> dict[str, datetime]:
        return {
            release.scheme_id: release.release_at
            for release in self.coordinator.compute_v2_releases(
                self.policy,
                sealed_at=sealed_at,
            )
        }

    def test_v2_releases_are_sealed_at_plus_zero_two_four_six(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        sealed_at = datetime(2026, 7, 24, 6, 50, tzinfo=SHANGHAI)

        releases = self.coordinator.compute_v2_releases(
            self.policy,
            sealed_at=sealed_at,
        )

        self.assertEqual(
            [(release.scheme_id, release.release_at.strftime("%H:%M")) for release in releases],
            list(zip(V2_IDS, ("06:50", "06:52", "06:54", "06:56"))),
        )

    def test_later_v2_release_does_not_depend_on_previous_v2_success(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states[V2_IDS[0]] = replace(
            states[V2_IDS[0]],
            state="FAILED_TERMINAL",
            failure_code="ALGORITHM",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 6, 52, 30, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=False,
            databridge_generation_id=DATABRIDGE_GENERATION_ID,
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
            databridge_generation_business_date=date(2026, 7, 24),
            v2_release_at_by_scheme=self._v2_release_map(
                datetime(
                    2026,
                    7,
                    24,
                    6,
                    50,
                    tzinfo=SHANGHAI,
                )
            ),
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id[V2_IDS[0]].action, "TERMINAL")
        self.assertEqual(by_id[V2_IDS[1]].action, "DISPATCH")
        self.assertEqual(by_id[V2_IDS[2]].action, "WAIT_RELEASE")

    def test_shared_native_consumer_waits_for_cache_prerequisite_success(
        self,
    ) -> None:
        prerequisite = "liwei_0616_10y01_full_oos_k3_div_k10"
        consumer = "liwei_0616_10y02_cons_say_k3_div_k5"
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        kwargs = {
            "policy": self.policy,
            "business_date": date(2026, 7, 24),
            "now": datetime(
                2026,
                7,
                24,
                6,
                30,
                tzinfo=SHANGHAI,
            ),
            "item_states": states,
            "native_generation_sealed": True,
            "native_generation_business_date": date(2026, 7, 24),
            "native_generation_id": NATIVE_GENERATION_ID,
            "expected_native_generation_id": NATIVE_GENERATION_ID,
        }

        pending = self.coordinator.plan_dispatches(**kwargs)
        pending_by_id = {
            decision.scheme_id: decision for decision in pending
        }
        self.assertEqual(
            pending_by_id[consumer].action,
            "WAIT_CACHE_PREREQUISITE",
        )
        self.assertEqual(
            pending_by_id[consumer].reason,
            f"CACHE_PREREQUISITE_NOT_SUCCESS:{prerequisite}",
        )

        states[prerequisite] = replace(
            states[prerequisite],
            state="SUCCESS",
        )
        released = self.coordinator.plan_dispatches(**kwargs)
        released_by_id = {
            decision.scheme_id: decision for decision in released
        }
        self.assertNotEqual(
            released_by_id[consumer].action,
            "WAIT_CACHE_PREREQUISITE",
        )

    def test_v2_release_is_not_cache_prerequisite_gated(self) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        sealed_at = datetime(
            2026,
            7,
            24,
            6,
            50,
            tzinfo=SHANGHAI,
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=sealed_at,
            item_states=states,
            native_generation_sealed=False,
            databridge_generation_business_date=date(2026, 7, 24),
            databridge_generation_id=DATABRIDGE_GENERATION_ID,
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
            v2_release_at_by_scheme=self._v2_release_map(sealed_at),
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertNotEqual(
            by_id[V2_IDS[0]].action,
            "WAIT_CACHE_PREREQUISITE",
        )

    def test_unpersistable_cancelled_state_is_rejected_as_unknown(self) -> None:
        """planner 的状态机必须与 migration/repository 可持久化枚举一致。"""
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = replace(
            states["t1_daily"],
            state="CANCELLED",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "TERMINAL")
        self.assertEqual(by_id["t1_daily"].reason, "UNKNOWN_ITEM_STATE")
        self.assertEqual(
            by_id["t1_daily"].failure_code,
            "INVALID_ITEM_STATE",
        )

    def test_planner_refuses_old_databridge_generation(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 6, 52, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=False,
            databridge_generation_business_date=date(2026, 7, 23),
            databridge_generation_id=DATABRIDGE_GENERATION_ID,
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertTrue(
            all(by_id[scheme_id].action == "WAIT_GENERATION" for scheme_id in V2_IDS)
        )
        self.assertTrue(
            all(
                by_id[scheme_id].reason == "DATABRIDGE_GENERATION_NOT_CURRENT"
                for scheme_id in V2_IDS
            )
        )

    def test_planner_requires_current_native_generation_identity(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        kwargs = {
            "policy": self.policy,
            "business_date": date(2026, 7, 24),
            "now": datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
            "item_states": states,
            "native_generation_sealed": True,
        }

        missing_identity = self.coordinator.plan_dispatches(**kwargs)
        old_identity = self.coordinator.plan_dispatches(
            **kwargs,
            native_generation_business_date=date(2026, 7, 23),
        )
        mismatched_identity = self.coordinator.plan_dispatches(
            **kwargs,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id="native-source-20260724-old",
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        missing_by_id = {
            decision.scheme_id: decision for decision in missing_identity
        }
        old_by_id = {decision.scheme_id: decision for decision in old_identity}
        mismatch_by_id = {
            decision.scheme_id: decision
            for decision in mismatched_identity
        }

        self.assertEqual(
            missing_by_id["t1_daily"].reason,
            "NATIVE_GENERATION_IDENTITY_MISSING",
        )
        self.assertEqual(
            old_by_id["t1_daily"].reason,
            "NATIVE_GENERATION_NOT_CURRENT",
        )
        self.assertEqual(
            mismatch_by_id["t1_daily"].reason,
            "NATIVE_GENERATION_MISMATCH",
        )
        self.assertEqual(missing_by_id["t1_daily"].action, "WAIT_GENERATION")
        self.assertEqual(old_by_id["t1_daily"].action, "WAIT_GENERATION")
        self.assertEqual(mismatch_by_id["t1_daily"].action, "WAIT_GENERATION")

    def test_planner_requires_matching_databridge_generation_identity(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 6, 52, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=False,
            databridge_generation_business_date=date(2026, 7, 24),
            v2_release_at_by_scheme=self._v2_release_map(
                datetime(
                    2026,
                    7,
                    24,
                    6,
                    50,
                    tzinfo=SHANGHAI,
                )
            ),
            databridge_generation_id="databridge-v1-20260724-old",
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertTrue(
            all(by_id[scheme_id].action == "WAIT_GENERATION" for scheme_id in V2_IDS)
        )
        self.assertTrue(
            all(
                by_id[scheme_id].reason == "DATABRIDGE_GENERATION_MISMATCH"
                for scheme_id in V2_IDS
            )
        )

    def test_unstarted_v2_is_late_at_guardrail_but_still_dispatches(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 45, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=False,
            databridge_generation_id=DATABRIDGE_GENERATION_ID,
            expected_databridge_generation_id=DATABRIDGE_GENERATION_ID,
            databridge_generation_business_date=date(2026, 7, 24),
            v2_release_at_by_scheme=self._v2_release_map(
                datetime(
                    2026,
                    7,
                    24,
                    7,
                    44,
                    tzinfo=SHANGHAI,
                )
            ),
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id[V2_IDS[0]].action, "DISPATCH")
        self.assertEqual(by_id[V2_IDS[0]].sla_status, "late")
        self.assertEqual(by_id[V2_IDS[1]].action, "WAIT_RELEASE")
        self.assertEqual(by_id[V2_IDS[1]].sla_status, "late")

    def test_dispatch_order_is_deadline_then_cache_prerequisite_then_lpt(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        ids = (
            "t1_daily",
            "liwei_0616_10y01_full_oos_k3_div_k10",
            "liwei_0616_5y01_full_oos_k3_div_k10",
            "liwei_0616_5y_auc_static_all_k3_div_k10",
        )
        schemes = dict(self.policy.schemes)
        schemes[ids[0]] = replace(
            schemes[ids[0]],
            absolute_deadline=datetime.strptime("07:40", "%H:%M").time(),
            cache_prerequisite=False,
            estimated_cold_sec=10,
        )
        schemes[ids[1]] = replace(
            schemes[ids[1]],
            absolute_deadline=datetime.strptime("07:55", "%H:%M").time(),
            cache_prerequisite=True,
            estimated_cold_sec=5,
        )
        schemes[ids[2]] = replace(
            schemes[ids[2]],
            absolute_deadline=datetime.strptime("07:55", "%H:%M").time(),
            cache_prerequisite=False,
            estimated_cold_sec=100,
        )
        schemes[ids[3]] = replace(
            schemes[ids[3]],
            absolute_deadline=datetime.strptime("07:55", "%H:%M").time(),
            cache_prerequisite=False,
            estimated_cold_sec=50,
        )
        policy = replace(self.policy, schemes=schemes)

        ordered = self.coordinator.dispatch_order(policy, ids)

        self.assertEqual(ordered, ids)

    def test_resource_governor_rejects_unapproved_pair_and_honors_pool_isolation(
        self,
    ) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        governor = self.coordinator.ResourceGovernor(self.policy)
        heavy_a = "liwei_0616_10y01_full_oos_k3_div_k10"
        heavy_b = "liwei_0616_5y01_full_oos_k3_div_k10"

        rejected = governor.can_start(
            running_scheme_ids=(heavy_a,),
            candidate_scheme_id=heavy_b,
        )
        native_while_v2_full = governor.can_start(
            running_scheme_ids=V2_IDS[:2],
            candidate_scheme_id="t1_daily",
        )
        third_v2 = governor.can_start(
            running_scheme_ids=V2_IDS[:2],
            candidate_scheme_id=V2_IDS[2],
        )

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.reason, "UNAPPROVED_RESOURCE_COMBINATION")
        self.assertTrue(native_while_v2_full.allowed)
        self.assertFalse(third_v2.allowed)
        self.assertEqual(third_v2.reason, "V2_POOL_LIMIT")

    def test_resource_governor_accounts_for_active_input_pipeline_load(
        self,
    ) -> None:
        governor = self.coordinator.ResourceGovernor(self.policy)

        approved = governor.can_start(
            running_scheme_ids=(),
            running_resource_classes=("databridge_refresh",),
            candidate_scheme_id="t1_daily",
        )
        unknown = governor.can_start(
            running_scheme_ids=(),
            running_resource_classes=("untracked_input_io",),
            candidate_scheme_id="t1_daily",
        )

        self.assertTrue(approved.allowed)
        self.assertFalse(unknown.allowed)
        self.assertEqual(
            unknown.reason,
            "UNAPPROVED_RESOURCE_COMBINATION",
        )

    def test_only_transient_infra_gets_one_retry_after_first_round(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        now = datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI)

        allowed = self.coordinator.decide_retry(
            self.policy,
            scheme_id="t1_daily",
            failure_code="TRANSIENT_INFRA",
            attempt_no=1,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=now,
        )
        before_first_round = self.coordinator.decide_retry(
            self.policy,
            scheme_id="t1_daily",
            failure_code="TRANSIENT_INFRA",
            attempt_no=1,
            all_first_attempts_covered=False,
            business_date=date(2026, 7, 24),
            now=now,
        )
        timeout = self.coordinator.decide_retry(
            self.policy,
            scheme_id="t1_daily",
            failure_code="TIMEOUT",
            attempt_no=1,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=now,
        )
        exhausted = self.coordinator.decide_retry(
            self.policy,
            scheme_id="t1_daily",
            failure_code="TRANSIENT_INFRA",
            attempt_no=2,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=now,
        )

        self.assertEqual(allowed.action, "RETRY")
        self.assertEqual(before_first_round.action, "WAIT_FIRST_ROUND")
        self.assertEqual(timeout.action, "TERMINAL")
        self.assertEqual(exhausted.action, "TERMINAL")

    def test_retry_policy_uses_the_shared_canonical_failure_vocabulary(
        self,
    ) -> None:
        self.assertEqual(
            self.coordinator.TRANSIENT_FAILURE_CODE,
            "TRANSIENT_INFRA",
        )
        self.assertEqual(
            self.coordinator.TERMINAL_FAILURE_CODES,
            frozenset({"TIMEOUT", "DATA", "CONTRACT", "ALGORITHM", "RESULT"}),
        )
        self.assertNotIn(
            "DATA_ERROR",
            self.coordinator.TERMINAL_FAILURE_CODES,
        )

    def test_planner_rejects_legacy_failure_code_in_snapshot(self) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = replace(
            states["t1_daily"],
            state="FAILED_TERMINAL",
            failure_code="DATA_ERROR",
        )

        with self.assertRaisesRegex(
            self.coordinator.DailyCoordinatorError,
            "unknown schedule failure_code",
        ):
            self.coordinator.plan_dispatches(
                self.policy,
                business_date=date(2026, 7, 24),
                now=datetime(
                    2026,
                    7,
                    24,
                    7,
                    0,
                    tzinfo=SHANGHAI,
                ),
                item_states=states,
                native_generation_sealed=False,
            )

    def test_planner_derives_first_round_coverage_from_item_snapshot(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = replace(
            states["t1_daily"],
            state="RETRY_WAIT",
            attempt_no=1,
            failure_code="TRANSIENT_INFRA",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
            all_first_attempts_covered=True,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "WAIT_FIRST_ROUND")

    def test_abandoned_second_attempt_waits_for_first_round_coverage(
        self,
    ) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = replace(
            states["t1_daily"],
            state="ABANDONED",
            attempt_no=1,
            failure_code="ABANDONED_ORPHAN_CLEANUP",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
            all_first_attempts_covered=True,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "WAIT_FIRST_ROUND")

    def test_abandoned_second_attempt_requires_remaining_budget(
        self,
    ) -> None:
        target = "liwei_0616_10y01_full_oos_k3_div_k10"
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state=(
                    "PENDING"
                    if item.input_compatibility == "unsupported"
                    else "FAILED_TERMINAL"
                ),
                failure_code=(
                    None
                    if item.input_compatibility == "unsupported"
                    else "DATA"
                ),
            )
            for scheme_id, item in self.policy.schemes.items()
        }
        states[target] = self.coordinator.ItemControlState(
            scheme_id=target,
            state="ABANDONED",
            attempt_no=1,
            failure_code="ABANDONED_ORPHAN_CLEANUP",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 8, 20, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id[target].action, "TERMINAL")
        self.assertEqual(
            by_id[target].reason,
            "RETRY_BUDGET_EXHAUSTED",
        )

    def test_abandoned_attempt_requires_confirmed_orphan_cleanup(
        self,
    ) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="FAILED_TERMINAL",
                failure_code="DATA",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = self.coordinator.ItemControlState(
            scheme_id="t1_daily",
            state="ABANDONED",
            attempt_no=1,
            failure_code="ABANDONED_FENCE_PENDING_CLEANUP",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(
            by_id["t1_daily"].action,
            "WAIT_ORPHAN_CLEANUP",
        )
        self.assertEqual(
            by_id["t1_daily"].reason,
            "ORPHAN_CLEANUP_NOT_CONFIRMED",
        )

    def test_abandoned_attempt_cannot_exceed_retry_limit(self) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="FAILED_TERMINAL",
                failure_code="DATA",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = self.coordinator.ItemControlState(
            scheme_id="t1_daily",
            state="ABANDONED",
            attempt_no=2,
            failure_code="ABANDONED_ORPHAN_CLEANUP",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "TERMINAL")
        self.assertEqual(by_id["t1_daily"].reason, "RETRY_EXHAUSTED")

    def test_first_attempt_coverage_treats_terminal_without_attempt_as_covered(
        self,
    ) -> None:
        self.assertTrue(
            self.coordinator.is_first_attempt_covered(
                state="FAILED_TERMINAL",
                attempt_no=0,
            )
        )
        self.assertTrue(
            self.coordinator.is_first_attempt_covered(
                state="PENDING",
                attempt_no=0,
                input_unsupported=True,
            )
        )
        self.assertFalse(
            self.coordinator.is_first_attempt_covered(
                state="PENDING",
                attempt_no=0,
            )
        )

    def test_terminal_first_round_items_do_not_block_transient_retry(
        self,
    ) -> None:
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state=(
                    "PENDING"
                    if item.input_compatibility == "unsupported"
                    else "FAILED_TERMINAL"
                ),
                failure_code=(
                    None
                    if item.input_compatibility == "unsupported"
                    else "DATA"
                ),
            )
            for scheme_id, item in self.policy.schemes.items()
        }
        states["t1_daily"] = self.coordinator.ItemControlState(
            scheme_id="t1_daily",
            state="RETRY_WAIT",
            attempt_no=1,
            failure_code="TRANSIENT_INFRA",
        )

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "DISPATCH")

    def test_retry_requires_budget_before_recovery_cutoff(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")

        decision = self.coordinator.decide_retry(
            self.policy,
            scheme_id="liwei_0616_10y01_full_oos_k3_div_k10",
            failure_code="TRANSIENT_INFRA",
            attempt_no=1,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 8, 20, tzinfo=SHANGHAI),
        )

        self.assertEqual(decision.action, "TERMINAL")
        self.assertEqual(decision.reason, "RETRY_BUDGET_EXHAUSTED")

    def test_retry_at_0829_uses_hard_timeout_not_30_second_estimate(
        self,
    ) -> None:
        scheme_id = "liwei_0616_10y02_cons_say_k3_div_k5"

        decision = self.coordinator.decide_retry(
            self.policy,
            scheme_id=scheme_id,
            failure_code="TRANSIENT_INFRA",
            attempt_no=1,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 8, 29, tzinfo=SHANGHAI),
        )

        self.assertEqual(
            self.policy.schemes[scheme_id].estimated_cold_sec,
            30,
        )
        self.assertEqual(
            self.policy.schemes[
                scheme_id
            ].admitted_hard_runtime_sec,
            3600,
        )
        self.assertEqual(decision.action, "TERMINAL")
        self.assertEqual(decision.reason, "RETRY_BUDGET_EXHAUSTED")

    def test_retry_budget_equal_to_cutoff_is_rejected(self) -> None:
        scheme_id = "t1_daily"
        item = self.policy.schemes[scheme_id]
        cutoff = datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI)
        exactly_consumes_budget = cutoff - timedelta(
            seconds=(
                item.admitted_hard_runtime_sec
                + self.policy.retry_cleanup_commit_margin_sec
            )
        )

        decision = self.coordinator.decide_retry(
            self.policy,
            scheme_id=scheme_id,
            failure_code="TRANSIENT_INFRA",
            attempt_no=1,
            all_first_attempts_covered=True,
            business_date=date(2026, 7, 24),
            now=exactly_consumes_budget,
        )

        self.assertEqual(decision.action, "TERMINAL")
        self.assertEqual(decision.reason, "RETRY_BUDGET_EXHAUSTED")

    def test_recovery_requires_cleanup_and_never_crosses_day_or_generation(
        self,
    ) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        before_cutoff = datetime(2026, 7, 24, 8, 20, tzinfo=SHANGHAI)
        generation_identity = {
            "bound_generation_id": NATIVE_GENERATION_ID,
            "occurrence_generation_id": NATIVE_GENERATION_ID,
            "generation_state": "SEALED",
            "generation_digest_valid": True,
        }

        success = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="SUCCESS",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            now=before_cutoff,
            **generation_identity,
        )
        running = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="RUNNING",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            now=before_cutoff,
            orphan_cleanup_confirmed=False,
            **generation_identity,
        )
        cleaned = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="RUNNING",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            now=before_cutoff,
            orphan_cleanup_confirmed=True,
            **generation_identity,
        )
        old_occurrence = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="PENDING",
            occurrence_date=date(2026, 7, 23),
            generation_business_date=date(2026, 7, 23),
            now=before_cutoff,
            **generation_identity,
        )
        wrong_generation = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="PENDING",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 23),
            now=before_cutoff,
            **generation_identity,
        )
        same_day_old_generation = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="PENDING",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            bound_generation_id="native-source-20260724-g0",
            occurrence_generation_id=NATIVE_GENERATION_ID,
            generation_state="SEALED",
            generation_digest_valid=True,
            now=before_cutoff,
        )

        self.assertEqual(success.action, "SKIP_SUCCESS")
        self.assertEqual(running.action, "REQUIRE_ABANDON_ORPHAN_CLEANUP")
        self.assertEqual(cleaned.action, "REQUEUE_AFTER_CLEANUP")
        self.assertEqual(old_occurrence.action, "NO_CROSS_DAY")
        self.assertEqual(wrong_generation.action, "GENERATION_MISMATCH")
        self.assertEqual(same_day_old_generation.action, "GENERATION_MISMATCH")

    def test_recovery_rejects_missing_invalid_or_tampered_generation(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        kwargs = {
            "policy": self.policy,
            "scheme_id": "t1_daily",
            "item_state": "PENDING",
            "occurrence_date": date(2026, 7, 24),
            "generation_business_date": date(2026, 7, 24),
            "now": datetime(2026, 7, 24, 8, 20, tzinfo=SHANGHAI),
        }

        missing = self.coordinator.decide_recovery(
            **kwargs,
            bound_generation_id=None,
            occurrence_generation_id=NATIVE_GENERATION_ID,
            generation_state="SEALED",
            generation_digest_valid=True,
        )
        invalidated = self.coordinator.decide_recovery(
            **kwargs,
            bound_generation_id=NATIVE_GENERATION_ID,
            occurrence_generation_id=NATIVE_GENERATION_ID,
            generation_state="INVALIDATED",
            generation_digest_valid=True,
        )
        tampered = self.coordinator.decide_recovery(
            **kwargs,
            bound_generation_id=NATIVE_GENERATION_ID,
            occurrence_generation_id=NATIVE_GENERATION_ID,
            generation_state="SEALED",
            generation_digest_valid=False,
        )

        self.assertEqual(missing.action, "GENERATION_IDENTITY_MISSING")
        self.assertEqual(invalidated.action, "GENERATION_INVALID")
        self.assertEqual(tampered.action, "GENERATION_HASH_MISMATCH")

    def test_pending_and_retry_items_expire_at_0830(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        generation_identity = {
            "bound_generation_id": NATIVE_GENERATION_ID,
            "occurrence_generation_id": NATIVE_GENERATION_ID,
            "generation_state": "SEALED",
            "generation_digest_valid": True,
        }

        before = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="PENDING",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 8, 29, 59, tzinfo=SHANGHAI),
            **generation_identity,
        )
        at_cutoff = self.coordinator.decide_recovery(
            self.policy,
            scheme_id="t1_daily",
            item_state="RETRY_WAIT",
            occurrence_date=date(2026, 7, 24),
            generation_business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
            **generation_identity,
        )

        self.assertEqual(before.action, "REQUEUE")
        self.assertEqual(at_cutoff.action, "EXPIRED")

    def test_planner_never_carries_running_attempt_across_day(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }
        states["t1_daily"] = replace(states["t1_daily"], state="RUNNING", attempt_no=1)

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 23),
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 23),
            native_generation_id="native-source-20260723-g1",
            expected_native_generation_id="native-source-20260723-g1",
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertEqual(by_id["t1_daily"].action, "TERMINAL")
        self.assertEqual(by_id["t1_daily"].reason, "NO_CROSS_DAY")

    def test_approved_0629_live_source_items_are_dispatchable(
        self,
    ) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        states = {
            scheme_id: self.coordinator.ItemControlState(
                scheme_id=scheme_id,
                state="PENDING",
            )
            for scheme_id in self.policy.schemes
        }

        decisions = self.coordinator.plan_dispatches(
            self.policy,
            business_date=date(2026, 7, 24),
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
            item_states=states,
            native_generation_sealed=True,
            native_generation_business_date=date(2026, 7, 24),
            native_generation_id=NATIVE_GENERATION_ID,
            expected_native_generation_id=NATIVE_GENERATION_ID,
        )
        by_id = {decision.scheme_id: decision for decision in decisions}

        self.assertTrue(
            all(
                by_id[scheme_id].failure_code is None
                for scheme_id in LIVE_SOURCE_0629
            )
        )
        self.assertTrue(
            any(
                by_id[scheme_id].action == "DISPATCH"
                for scheme_id in LIVE_SOURCE_0629
            )
        )
        self.assertTrue(
            all(
                by_id[scheme_id].action
                in {"DISPATCH", "WAIT_POOL", "WAIT_RESOURCE"}
                for scheme_id in LIVE_SOURCE_0629
            )
        )

    def test_occurrence_file_lock_is_nonblocking_and_not_unlinked(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "daily-20260724.lock"
            first = self.coordinator.OccurrenceFileLock(path)
            second = self.coordinator.OccurrenceFileLock(path)

            with first:
                with self.assertRaises(self.coordinator.OccurrenceLockUnavailable):
                    second.acquire()
            self.assertTrue(path.exists())
            with second:
                self.assertTrue(second.acquired)
            self.assertFalse(second.acquired)

    def test_occurrence_file_lock_rejects_relative_or_non_lock_paths(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")

        with self.assertRaises(ValueError):
            self.coordinator.OccurrenceFileLock(Path("relative.lock"))
        with self.assertRaises(ValueError):
            self.coordinator.OccurrenceFileLock(Path("/tmp/not-a-lock-file"))

    def test_occurrence_file_lock_rejects_parent_inode_replacement(self) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock_directory = root / "locks"
            replacement_directory = root / "replacement"
            original_directory = root / "locks-original"
            lock_directory.mkdir()
            replacement_directory.mkdir()
            path = lock_directory / "daily-20260724.lock"
            owner = self.coordinator.OccurrenceFileLock(path)

            lock_directory.rename(original_directory)
            replacement_directory.rename(lock_directory)

            with self.assertRaises(
                self.coordinator.OccurrenceLockPathChanged,
            ):
                owner.acquire()
            self.assertFalse(path.exists())
            self.assertFalse(owner.acquired)

    def test_occurrence_file_lock_rechecks_parent_permissions_on_acquire(
        self,
    ) -> None:
        self.assertIsNotNone(self.coordinator, "scheduler.daily_coordinator is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock_directory = root / "locks"
            lock_directory.mkdir(mode=0o700)
            path = lock_directory / "daily-20260724.lock"
            owner = self.coordinator.OccurrenceFileLock(path)

            lock_directory.chmod(0o777)
            try:
                with self.assertRaises(
                    self.coordinator.OccurrenceLockPathChanged,
                ):
                    owner.acquire()
            finally:
                owner.release()
                lock_directory.chmod(0o700)
            self.assertFalse(owner.acquired)


if __name__ == "__main__":
    unittest.main()
