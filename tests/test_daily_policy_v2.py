from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scheduler.blackbox_scheduler_admission import (
    DAILY_LEDGER,
    load_blackbox_scheduler_admission,
)
from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_V1_PATH = (
    PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
)
POLICY_V2_PATH = (
    PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v2.json"
)
POLICY_V1_SHA256 = (
    "e6b660a292ea07cd9b08637c8756df5c"
    "d787be3bd4fd45a9d550add088a92091"
)
FORMAL_V2_OFFSETS = {
    "one_y_t5_liq_excess_a_v1": 0,
    "one_y_t5_liq_excess_a_w252_l7_v1": 2,
    "one_y_t5_liq_excess_a_w350_l7_v1": 4,
    "one_y_t5_liq_excess_b_w252_l7_v1": 6,
}
GRAY_V2_OFFSETS = {
    "ten_y_t5_maj3_k3_ic_static_v1": 8,
    "ten_y_t5_maj4_k3_ic_static_v1": 10,
    "ten_y_t5_maj4_k3_ic_yearly_v1": 12,
    "ten_y_t5_say_k5_sharpe_static_v1": 14,
}
LIVE_SOURCE_0629 = {
    "daily_10y_lgbm_10y04_0629",
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
}


def _active_daily_schemes():
    return tuple(
        config
        for config in discover_schemes()
        if config.status == "active"
        and config.frequency == "daily"
    )


class DailyPolicyV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        from scheduler import daily_policy

        self.module = daily_policy
        self.active_daily = _active_daily_schemes()

    def test_v1_policy_bytes_remain_immutable(self) -> None:
        self.assertEqual(
            hashlib.sha256(POLICY_V1_PATH.read_bytes()).hexdigest(),
            POLICY_V1_SHA256,
        )
        self.assertEqual(
            self.module.DEFAULT_POLICY_PATH,
            POLICY_V1_PATH,
        )
        policy = self.module.load_daily_policy(
            discovered=self.active_daily,
        )
        self.assertEqual(policy.version, "daily-scheduler-policy-v1")
        self.assertEqual(
            (policy.expected_item_count, policy.expected_target_count),
            (21, 25),
        )

    def test_v2_covers_exact_daily_ledger_25_items_29_targets(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_V2_PATH,
            discovered=self.active_daily,
        )
        admission = load_blackbox_scheduler_admission()
        expected = {
            config.scheme_id
            for config in self.active_daily
            if admission.allows(config, plane=DAILY_LEDGER)
        }

        self.assertEqual(
            policy.version,
            "daily-scheduler-policy-v2",
        )
        self.assertEqual(policy.expected_item_count, 25)
        self.assertEqual(policy.expected_target_count, 29)
        self.assertEqual(set(policy.schemes), expected)
        self.assertEqual(len(policy.schemes), 25)
        self.assertEqual(
            sum(
                len(item.target_tenors)
                for item in policy.schemes.values()
            ),
            29,
        )
        self.assertEqual(
            sum(
                item.runtime_type == "native_adapter"
                for item in policy.schemes.values()
            ),
            17,
        )
        self.assertEqual(
            sum(
                item.runtime_type == "blackbox_v2"
                for item in policy.schemes.values()
            ),
            8,
        )

    def test_v2_freezes_exact_blackbox_release_sequence_and_pool_limits(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_V2_PATH,
            discovered=self.active_daily,
        )
        actual_offsets = {
            item.scheme_id: item.v2_release_offset_min
            for item in policy.schemes.values()
            if item.runtime_type == "blackbox_v2"
        }

        self.assertEqual(
            actual_offsets,
            {**FORMAL_V2_OFFSETS, **GRAY_V2_OFFSETS},
        )
        self.assertEqual(
            tuple(sorted(actual_offsets.values())),
            (0, 2, 4, 6, 8, 10, 12, 14),
        )
        self.assertEqual(policy.native_max_concurrency, 2)
        self.assertEqual(policy.v2_max_concurrency, 2)
        self.assertFalse(policy.native_auto_scale)
        self.assertEqual(policy.retry_max, 1)

    def test_v2_preserves_native_input_modes_and_blackbox_generation(
        self,
    ) -> None:
        policy = self.module.load_daily_policy(
            POLICY_V2_PATH,
            discovered=self.active_daily,
        )
        native = [
            item
            for item in policy.schemes.values()
            if item.runtime_type == "native_adapter"
        ]
        blackbox = [
            item
            for item in policy.schemes.values()
            if item.runtime_type == "blackbox_v2"
        ]

        self.assertEqual(
            {
                item.scheme_id
                for item in native
                if item.input_compatibility
                == "live_source_0629"
            },
            LIVE_SOURCE_0629,
        )
        self.assertEqual(
            sum(
                item.input_compatibility == "generation_v1"
                for item in native
            ),
            14,
        )
        self.assertEqual(
            sum(
                item.input_compatibility == "databridge_v1"
                for item in blackbox
            ),
            8,
        )

    def test_v2_rejects_release_identity_drift_and_third_native_lane(
        self,
    ) -> None:
        payload = json.loads(
            POLICY_V2_PATH.read_text(encoding="utf-8")
        )

        def swap_gray_offsets(value) -> None:
            first = next(
                item
                for item in value["schemes"]
                if item["scheme_id"]
                == "ten_y_t5_maj3_k3_ic_static_v1"
            )
            second = next(
                item
                for item in value["schemes"]
                if item["scheme_id"]
                == "ten_y_t5_maj4_k3_ic_static_v1"
            )
            (
                first["v2_release_offset_min"],
                second["v2_release_offset_min"],
            ) = (
                second["v2_release_offset_min"],
                first["v2_release_offset_min"],
            )

        mutations = (
            (
                "release offset mapping",
                swap_gray_offsets,
            ),
            (
                "native_max_concurrency",
                lambda value: value["pools"].update(
                    {"native_max_concurrency": 3}
                ),
            ),
        )
        for expected_error, mutate in mutations:
            candidate = copy.deepcopy(payload)
            mutate(candidate)
            with (
                self.subTest(expected_error=expected_error),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "policy.json"
                path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.module.DailyPolicyError,
                    expected_error,
                ):
                    self.module.load_daily_policy(
                        path,
                        discovered=self.active_daily,
                    )

    def test_policy_rejects_shadow_formal_or_gray_exact_version(
        self,
    ) -> None:
        cases = (
            (
                POLICY_V1_PATH,
                "one_y_t5_liq_excess_a_v1",
            ),
            (
                POLICY_V2_PATH,
                "ten_y_t5_maj3_k3_ic_static_v1",
            ),
        )
        for policy_path, scheme_id in cases:
            discovered = tuple(
                replace(config, version_status="shadow")
                if config.scheme_id == scheme_id
                else config
                for config in self.active_daily
            )
            with (
                self.subTest(scheme_id=scheme_id),
                self.assertRaisesRegex(
                    self.module.DailyPolicyError,
                    "version is not active",
                ),
            ):
                self.module.load_daily_policy(
                    policy_path,
                    discovered=discovered,
                )


if __name__ == "__main__":
    unittest.main()
