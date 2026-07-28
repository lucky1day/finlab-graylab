from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scheduler.blackbox_scheduler_admission import (
    LEGACY_AUTOMATIC,
    load_blackbox_scheduler_admission,
)
from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
LIVE_SOURCE_0629 = {
    "daily_10y_lgbm_10y04_0629",
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
}
DAILY_0629_SOURCE_PACKAGE_SHA256 = (
    "de63375f51810962ad10162444f93f7b"
    "2fbde6236b7f4b9921734ae6b8fad1e3"
)


def _policy_module():
    try:
        return importlib.import_module("scheduler.daily_policy")
    except ModuleNotFoundError:
        return None


def _active_daily_schemes():
    return tuple(
        config
        for config in discover_schemes()
        if config.status == "active" and config.frequency == "daily"
    )


class DailyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _policy_module()
        self.active_daily = _active_daily_schemes()

    def test_versioned_policy_covers_current_21_items_and_25_targets(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")

        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )

        self.assertEqual(policy.version, "daily-scheduler-policy-v1")
        self.assertEqual(policy.evidence_version, "mysql-run-log-2026-07-22")
        self.assertEqual(policy.timezone, "Asia/Shanghai")
        self.assertEqual(policy.expected_item_count, 21)
        self.assertEqual(policy.expected_target_count, 25)
        self.assertEqual(len(policy.schemes), 21)
        scheduler_admission = load_blackbox_scheduler_admission()
        self.assertEqual(
            set(policy.schemes),
            {
                config.scheme_id
                for config in self.active_daily
                if scheduler_admission.allows(
                    config,
                    plane=LEGACY_AUTOMATIC,
                )
            }
        )
        self.assertEqual(
            sum(len(item.target_tenors) for item in policy.schemes.values()),
            25,
        )

    def test_gray_blackbox_schemes_are_excluded_from_formal_daily_policy(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )
        daily_gray_ids = {
            "ten_y_t5_maj3_k3_ic_static_v1",
            "ten_y_t5_maj4_k3_ic_static_v1",
            "ten_y_t5_maj4_k3_ic_yearly_v1",
            "ten_y_t5_say_k5_sharpe_static_v1",
        }

        self.assertTrue(daily_gray_ids.issubset(
            {config.scheme_id for config in self.active_daily}
        ))
        self.assertTrue(daily_gray_ids.isdisjoint(policy.schemes))
        self.assertEqual(len(policy.schemes), 21)

    def test_policy_freezes_approved_times_limits_and_no_auto_three(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")

        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )

        self.assertEqual(policy.not_before.strftime("%H:%M"), "06:30")
        self.assertEqual(
            policy.native_capture_deadline.strftime("%H:%M"),
            "08:30",
        )
        self.assertEqual(
            policy.databridge_readiness_guardrail.strftime("%H:%M"),
            "06:55",
        )
        self.assertEqual(policy.watchdog.strftime("%H:%M"), "07:00")
        self.assertEqual(policy.v2_start_guardrail.strftime("%H:%M"), "07:45")
        self.assertEqual(policy.target_ready.strftime("%H:%M"), "07:55")
        self.assertEqual(policy.sla_deadline.strftime("%H:%M"), "08:00")
        self.assertEqual(policy.recovery_cutoff.strftime("%H:%M"), "08:30")
        self.assertEqual(policy.native_max_concurrency, 2)
        self.assertEqual(policy.v2_max_concurrency, 2)
        self.assertEqual(policy.v2_timeout_sec, 120)
        self.assertEqual(policy.retry_max, 1)
        self.assertEqual(
            policy.retry_cleanup_commit_margin_sec,
            60,
        )
        self.assertFalse(policy.native_auto_scale)
        self.assertEqual(policy.generation_max_count, 64)
        self.assertEqual(
            policy.generation_max_total_bytes,
            50 * 1024**3,
        )
        self.assertEqual(
            policy.generation_min_free_bytes,
            2 * 1024**3,
        )

    def test_policy_rejects_native_capture_deadline_drift(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["times"]["native_capture_deadline"] = "08:29"

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "native_capture_deadline",
        ):
            self._load_payload(payload)

    def test_input_startup_pair_is_explicit_and_cannot_be_removed(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )
        required = {
            ("native_export",),
            ("databridge_refresh",),
            ("databridge_pack",),
            ("databridge_refresh", "native_export"),
        }
        self.assertTrue(
            required.issubset(policy.allowed_resource_combinations)
        )
        for signature in required:
            with self.subTest(signature=signature):
                payload = self._payload()
                payload["resource_governor"][
                    "allowed_combinations"
                ] = [
                    row
                    for row in payload["resource_governor"][
                        "allowed_combinations"
                    ]
                    if tuple(sorted(row)) != signature
                ]
                with self.assertRaisesRegex(
                    self.module.DailyPolicyError,
                    "mandatory input startup combination",
                ):
                    self._load_payload(payload)

    def test_policy_rejects_databridge_readiness_guardrail_drift(
        self,
    ) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["times"]["databridge_readiness_guardrail"] = "07:00"

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "databridge_readiness_guardrail",
        ):
            self._load_payload(payload)

    def test_every_item_has_explicit_capacity_and_input_contract(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")

        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )

        for scheme_id, item in policy.schemes.items():
            with self.subTest(scheme_id=scheme_id):
                self.assertIn(item.runtime_type, {"native_adapter", "blackbox_v2"})
                self.assertTrue(item.resource_class)
                self.assertGreater(item.internal_workers, 0)
                self.assertTrue(item.cache_group)
                self.assertGreater(item.estimated_cold_sec, 0)
                self.assertGreater(
                    item.admitted_hard_runtime_sec,
                    0,
                )
                self.assertIsNotNone(item.absolute_deadline)
                self.assertIn(
                    item.input_compatibility,
                    {
                        "generation_v1",
                        "databridge_v1",
                        "live_source_0629",
                    },
                )
                self.assertIn(item.task_type, {"T+1", "T+5"})
                self.assertTrue(item.target_tenors)

    def test_shared_native_cache_groups_have_one_prerequisite_and_one_spec(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )
        native_groups: dict[str, list[object]] = {}
        for item in policy.schemes.values():
            if item.runtime_type != "native_adapter":
                continue
            native_groups.setdefault(item.cache_group, []).append(item)

        for cache_group, members in native_groups.items():
            if len(members) < 2:
                continue
            with self.subTest(cache_group=cache_group):
                self.assertEqual(
                    sum(item.cache_prerequisite for item in members),
                    1,
                )
                self.assertEqual(
                    len(
                        {
                            item.cache_spec_fingerprint
                            for item in members
                        }
                    ),
                    1,
                )
                self.assertIsNotNone(
                    members[0].cache_spec_fingerprint
                )

    def test_policy_rejects_shared_cache_spec_or_prerequisite_drift(
        self,
    ) -> None:
        payload = self._payload()
        members = [
            row
            for row in payload["schemes"]
            if row["cache_group"]
            == "liwei_0616_10y_v61:10Y"
        ]
        self.assertEqual(len(members), 3)

        drifted_spec = copy.deepcopy(payload)
        drifted_member = next(
            row
            for row in drifted_spec["schemes"]
            if row["scheme_id"] == members[0]["scheme_id"]
        )
        drifted_member["cache_spec_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "cache spec fingerprint",
        ):
            self._load_payload(drifted_spec)

        duplicate_prerequisite = copy.deepcopy(payload)
        duplicate_members = [
            row
            for row in duplicate_prerequisite["schemes"]
            if row["cache_group"]
            == "liwei_0616_10y_v61:10Y"
        ]
        for row in duplicate_members[:2]:
            row["cache_prerequisite"] = True
        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "exactly one cache prerequisite",
        ):
            self._load_payload(duplicate_prerequisite)

    def test_incompatible_7y_cache_specs_are_split(self) -> None:
        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )
        first = policy.schemes[
            "liwei_0616_7y01_cons_say_k3_div_k10"
        ]
        second = policy.schemes[
            "liwei_0616_7y03_cons_all_k3_div_k8"
        ]

        self.assertNotEqual(first.cache_group, second.cache_group)
        self.assertNotEqual(
            first.cache_spec_fingerprint,
            second.cache_spec_fingerprint,
        )

    def test_admitted_hard_runtime_is_bound_to_effective_scheme_timeout(
        self,
    ) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")

        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )
        config_by_id = {
            config.scheme_id: config
            for config in self.active_daily
        }

        for scheme_id, item in policy.schemes.items():
            config = config_by_id[scheme_id]
            expected = (
                120
                if item.runtime_type == "blackbox_v2"
                else (config.schedule.timeout_sec or 600)
            )
            with self.subTest(scheme_id=scheme_id):
                self.assertEqual(
                    item.admitted_hard_runtime_sec,
                    expected,
                )

    def test_policy_rejects_hard_runtime_below_config_timeout(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        row = next(
            row
            for row in payload["schemes"]
            if row["scheme_id"]
            == "liwei_0616_10y02_cons_say_k3_div_k5"
        )
        self.assertEqual(row["estimated_cold_sec"], 30)
        row["admitted_hard_runtime_sec"] = 30

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "admitted_hard_runtime_sec",
        ):
            self._load_payload(payload)

    def test_policy_rejects_scheme_timeout_drift_from_frozen_bound(
        self,
    ) -> None:
        scheme_id = "liwei_0616_10y02_cons_say_k3_div_k5"
        drifted_discovery = tuple(
            (
                replace(
                    config,
                    schedule=replace(
                        config.schedule,
                        timeout_sec=30,
                    ),
                )
                if config.scheme_id == scheme_id
                else config
            )
            for config in self.active_daily
        )

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "admitted_hard_runtime_sec",
        ):
            self._load_payload(
                self._payload(),
                discovered=drifted_discovery,
            )

    def test_only_three_0629_items_use_live_source_compatibility(self) -> None:
        from shared.daily_0629_source_evidence import (
            require_daily_0629_source_evidence,
        )

        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")

        policy = self.module.load_daily_policy(
            POLICY_PATH,
            discovered=self.active_daily,
        )

        live_source = {
            item.scheme_id
            for item in policy.schemes.values()
            if item.input_compatibility == "live_source_0629"
        }
        self.assertEqual(live_source, LIVE_SOURCE_0629)
        self.assertEqual(
            {
                item.source_package_sha256
                for item in policy.schemes.values()
                if item.scheme_id in LIVE_SOURCE_0629
            },
            {DAILY_0629_SOURCE_PACKAGE_SHA256},
        )
        self.assertEqual(
            {
                require_daily_0629_source_evidence(
                    scheme_id
                ).source_package_hash
                for scheme_id in LIVE_SOURCE_0629
            },
            {DAILY_0629_SOURCE_PACKAGE_SHA256},
        )
        self.assertFalse(
            any(
                item.input_compatibility == "unsupported"
                for item in policy.schemes.values()
            )
        )
        self.assertTrue(
            all(
                item.input_compatibility == "generation_v1"
                for item in policy.schemes.values()
                if item.runtime_type == "native_adapter"
                and item.scheme_id not in LIVE_SOURCE_0629
            )
        )
        self.assertTrue(
            all(
                item.input_compatibility == "databridge_v1"
                for item in policy.schemes.values()
                if item.runtime_type == "blackbox_v2"
            )
        )

    def test_policy_rejects_live_source_mode_on_other_native(self) -> None:
        payload = self._payload()
        target = next(
            row
            for row in payload["schemes"]
            if row["scheme_id"] == "t1_daily"
        )
        target["input_compatibility"] = "live_source_0629"

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "live source compatibility",
        ):
            self._load_payload(payload)

    def test_policy_rejects_missing_live_source_package_hash(
        self,
    ) -> None:
        payload = self._payload()
        target = next(
            row
            for row in payload["schemes"]
            if row["scheme_id"] == "daily_1y_xgb_1y13_0629"
        )
        target.pop("source_package_sha256")

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "source_package_sha256",
        ):
            self._load_payload(payload)

    def test_policy_rejects_unknown_scheme(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"][0]["scheme_id"] = "unknown_daily_scheme"

        with self.assertRaisesRegex(self.module.DailyPolicyError, "unknown"):
            self._load_payload(payload)

    def test_policy_rejects_missing_active_daily_scheme(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"].pop()

        with self.assertRaisesRegex(self.module.DailyPolicyError, "missing"):
            self._load_payload(payload)

    def test_policy_rejects_duplicate_scheme(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"].append(copy.deepcopy(payload["schemes"][0]))

        with self.assertRaisesRegex(self.module.DailyPolicyError, "duplicate"):
            self._load_payload(payload)

    def test_policy_rejects_illegal_daily_task_type(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"][0]["task_type"] = "weekly_point"

        with self.assertRaisesRegex(self.module.DailyPolicyError, "task_type"):
            self._load_payload(payload)

    def test_policy_rejects_native_concurrency_above_two(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["pools"]["native_max_concurrency"] = 3

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "native_max_concurrency",
        ):
            self._load_payload(payload)

    def test_policy_requires_exact_v2_release_offsets(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        v2_item = next(
            item
            for item in payload["schemes"]
            if item["runtime_type"] == "blackbox_v2"
        )
        v2_item["v2_release_offset_min"] = 8

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "release offsets",
        ):
            self._load_payload(payload)

    def test_policy_rejects_swapped_v2_release_identity(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        v2_items = [
            item
            for item in payload["schemes"]
            if item["runtime_type"] == "blackbox_v2"
        ]
        v2_items[0]["v2_release_offset_min"], v2_items[1][
            "v2_release_offset_min"
        ] = (
            v2_items[1]["v2_release_offset_min"],
            v2_items[0]["v2_release_offset_min"],
        )

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "release offset mapping",
        ):
            self._load_payload(payload)

    def test_policy_rejects_malformed_scheme_entry(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"][0] = "not-an-object"

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "entries must be objects",
        ):
            self._load_payload(payload)

    def test_policy_rejects_timezone_bearing_clock_text(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["times"]["not_before"] = "06:30+08:00"

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "HH:MM",
        ):
            self._load_payload(payload)

    def test_native_items_cannot_declare_v2_release_offsets(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        payload["schemes"][0]["v2_release_offset_min"] = 0

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "Native cannot declare",
        ):
            self._load_payload(payload)

    def test_v1_cardinality_rejects_paired_policy_and_discovery_drift(self) -> None:
        self.assertIsNotNone(self.module, "scheduler.daily_policy is missing")
        payload = self._payload()
        template_row = next(
            row
            for row in payload["schemes"]
            if row["scheme_id"] == "t1_daily"
        )
        extra_row = copy.deepcopy(template_row)
        extra_row["scheme_id"] = "unadmitted_daily_scheme"
        extra_row["cache_group"] = "native:unadmitted"
        payload["schemes"].append(extra_row)
        payload["expected_item_count"] = 22
        payload["expected_target_count"] = 27
        template_config = next(
            config
            for config in self.active_daily
            if config.scheme_id == "t1_daily"
        )
        drifted_discovery = (
            *self.active_daily,
            replace(
                template_config,
                scheme_id="unadmitted_daily_scheme",
            ),
        )

        with self.assertRaisesRegex(
            self.module.DailyPolicyError,
            "v1 cardinality",
        ):
            self._load_payload(
                payload,
                discovered=drifted_discovery,
            )

    @staticmethod
    def _payload() -> dict[str, object]:
        return json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def _load_payload(
        self,
        payload: dict[str, object],
        *,
        discovered=None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            return self.module.load_daily_policy(
                path,
                discovered=(
                    self.active_daily
                    if discovered is None
                    else discovered
                ),
            )


if __name__ == "__main__":
    unittest.main()
