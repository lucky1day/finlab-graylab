from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_gray_launchd_policy_v1.json"
PLIST_PATH = (
    PROJECT_ROOT
    / "deploy"
    / "launchd"
    / "com.bond-factor-lab.daily-gray.plist"
)
EXTRA_DAILY_GRAY_IDENTITIES = {
    "one_y_t1_quote_state_hv_v1",
    "three_y_adyn_lb1_k3_v1",
    "three_y_adyn_lb2_k1_v1",
}


def _active_daily_schemes():
    return tuple(
        config
        for config in discover_schemes(strict=True)
        if config.status == "active" and config.frequency == "daily"
    )


class DailyGrayLaunchdPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        from scheduler import daily_gray_launchd_policy

        self.module = daily_gray_launchd_policy
        self.active_daily = _active_daily_schemes()
        self.payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def _write_policy(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "policy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_raw_policy(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "policy.json"
        path.write_text(text, encoding="utf-8")
        return path

    def _load_payload(self, payload: object):
        return self.module.load_daily_gray_launchd_policy(
            self._write_policy(payload),
            discovered=self.active_daily,
        )

    def test_repository_policy_loads_exact_28_executions_and_32_targets(
        self,
    ) -> None:
        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )

        self.assertEqual(
            policy.schema_version,
            "daily-gray-launchd-policy-v1",
        )
        self.assertEqual(policy.expected_execution_count, 28)
        self.assertEqual(policy.expected_target_count, 32)
        self.assertEqual(len(policy.schemes), 28)
        self.assertEqual(
            sum(len(row.target_tenors) for row in policy.schemes.values()),
            32,
        )
        self.assertEqual(tuple(policy.schemes), tuple(sorted(policy.schemes)))

    def test_policy_contains_three_daily_gray_only_identities(self) -> None:
        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )

        self.assertTrue(EXTRA_DAILY_GRAY_IDENTITIES.issubset(policy.schemes))

    def test_t1_version_is_exact_shap_retired_version(self) -> None:
        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )

        self.assertEqual(
            policy.schemes["t1_daily"].scheme_version,
            "7898b9e47a9a",
        )

    def test_policy_matches_frozen_classification_and_dependencies(
        self,
    ) -> None:
        from scheduler.daily_gray_launchd_policy import (
            FROZEN_HEAVY_SCHEME_IDS,
            FROZEN_PUBLISHER_MAP,
        )

        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )
        heavy = {
            row.scheme_id
            for row in policy.schemes.values()
            if row.execution_class == "heavy"
        }
        dependencies = {
            row.scheme_id: row.publisher_scheme_id
            for row in policy.schemes.values()
            if row.publisher_scheme_id is not None
        }

        self.assertEqual(heavy, set(FROZEN_HEAVY_SCHEME_IDS))
        self.assertEqual(dependencies, dict(FROZEN_PUBLISHER_MAP))

    def test_policy_root_matches_launchd_label_and_entrypoint(self) -> None:
        from scheduler.daily_gray_runner import PREDICTION_PHASE

        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )
        with PLIST_PATH.open("rb") as handle:
            plist = plistlib.load(handle)
        arguments = plist["ProgramArguments"]

        self.assertEqual(policy.plist_label, plist["Label"])
        module_flag = arguments.index("-m")
        self.assertEqual(policy.entrypoint, arguments[module_flag + 1])
        self.assertEqual(policy.prediction_phase, PREDICTION_PHASE)

    def test_default_loader_uses_strict_discovery(self) -> None:
        with patch.object(
            self.module,
            "discover_schemes",
            return_value=list(self.active_daily),
        ) as discovery:
            self.module.load_daily_gray_launchd_policy()

        discovery.assert_called_once_with(strict=True)

    def test_default_loader_wraps_strict_discovery_failure(self) -> None:
        failure = RuntimeError("strict discovery exploded")
        with (
            patch.object(
                self.module,
                "discover_schemes",
                side_effect=failure,
            ),
            self.assertRaisesRegex(
                self.module.DailyGrayLaunchdPolicyError,
                "strict discovery failed",
            ) as captured,
        ):
            self.module.load_daily_gray_launchd_policy()

        self.assertIs(captured.exception.__cause__, failure)

    def test_loaded_policy_is_immutable(self) -> None:
        policy = self.module.load_daily_gray_launchd_policy(
            discovered=self.active_daily,
        )

        with self.assertRaises(TypeError):
            policy.schemes["new"] = policy.schemes["t1_daily"]
        with self.assertRaises(FrozenInstanceError):
            policy.schemes["t1_daily"].horizon = 99

    def test_rejects_added_or_missing_active_daily_identity(self) -> None:
        added = self.active_daily + (
            replace(
                self.active_daily[0],
                scheme_id="unexpected_active_daily",
            ),
        )
        missing = self.active_daily[1:]

        for expected, discovered in (
            ("missing active daily identities.*unexpected_active_daily", added),
            ("unknown policy identities", missing),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self.module.load_daily_gray_launchd_policy(
                    discovered=discovered,
                )

    def test_rejects_discovery_identity_field_drift(self) -> None:
        first = self.active_daily[0]
        mutations = (
            ("scheme_version drift", {"scheme_version": "version-drift"}),
            ("runtime_type drift", {"runtime_type": "blackbox_v2"}),
            ("task_type drift", {"task_type": "T+5"}),
            ("horizon drift", {"horizon": first.horizon + 1}),
            ("target_tenors drift", {"tenors": ["30Y"]}),
        )

        for expected, changes in mutations:
            discovered = tuple(
                replace(config, **changes)
                if config.scheme_id == first.scheme_id
                else config
                for config in self.active_daily
            )
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self.module.load_daily_gray_launchd_policy(
                    discovered=discovered,
                )

    def test_rejects_policy_frequency_and_execution_class_drift(self) -> None:
        cases = (
            ("frequency", "weekly", "frequency must be daily"),
            ("execution_class", "medium", "execution_class"),
        )
        for field, value, expected in cases:
            payload = copy.deepcopy(self.payload)
            payload["schemes"][0][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self._load_payload(payload)

    def test_rejects_invalid_dependency_graph(self) -> None:
        heavy = [
            row["scheme_id"]
            for row in self.payload["schemes"]
            if row["execution_class"] == "heavy"
        ]
        light = [
            row["scheme_id"]
            for row in self.payload["schemes"]
            if row["execution_class"] == "light"
            and "publisher_scheme_id" not in row
        ]

        cases: list[tuple[str, object]] = []

        missing = copy.deepcopy(self.payload)
        missing["schemes"][0]["publisher_scheme_id"] = "missing_publisher"
        cases.append(("publisher does not exist", missing))

        self_dependency = copy.deepcopy(self.payload)
        scheme_id = self_dependency["schemes"][0]["scheme_id"]
        self_dependency["schemes"][0]["publisher_scheme_id"] = scheme_id
        cases.append(("self dependency", self_dependency))

        light_publisher = copy.deepcopy(self.payload)
        consumer = next(
            row
            for row in light_publisher["schemes"]
            if "publisher_scheme_id" in row
        )
        consumer["publisher_scheme_id"] = light[0]
        cases.append(("publisher must be heavy", light_publisher))

        cycle = copy.deepcopy(self.payload)
        cycle_by_id = {row["scheme_id"]: row for row in cycle["schemes"]}
        cycle_by_id[heavy[0]]["publisher_scheme_id"] = heavy[1]
        cycle_by_id[heavy[1]]["publisher_scheme_id"] = heavy[0]
        cases.append(("dependency cycle", cycle))

        for expected, payload in cases:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self._load_payload(payload)

    def test_rejects_frozen_execution_class_and_publisher_map_drift(
        self,
    ) -> None:
        light_to_heavy = copy.deepcopy(self.payload)
        light_row = next(
            row
            for row in light_to_heavy["schemes"]
            if row["execution_class"] == "light"
            and "publisher_scheme_id" not in row
        )
        light_row["execution_class"] = "heavy"

        deleted_publisher = copy.deepcopy(self.payload)
        consumer = next(
            row
            for row in deleted_publisher["schemes"]
            if "publisher_scheme_id" in row
        )
        del consumer["publisher_scheme_id"]

        changed_publisher = copy.deepcopy(self.payload)
        heavy_ids = [
            row["scheme_id"]
            for row in changed_publisher["schemes"]
            if row["execution_class"] == "heavy"
        ]
        consumer = next(
            row
            for row in changed_publisher["schemes"]
            if "publisher_scheme_id" in row
        )
        consumer["publisher_scheme_id"] = next(
            scheme_id
            for scheme_id in heavy_ids
            if scheme_id != consumer["publisher_scheme_id"]
        )

        cases = (
            ("heavy scheme set drift", light_to_heavy),
            ("publisher map drift", deleted_publisher),
            ("publisher map drift", changed_publisher),
        )
        for expected, payload in cases:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self._load_payload(payload)

    def test_rejects_wrong_root_constants(self) -> None:
        cases = (
            ("schema_version", "wrong", "schema_version"),
            ("plist_label", "wrong", "plist_label"),
            ("entrypoint", "wrong", "entrypoint"),
            ("prediction_phase", "wrong", "prediction_phase"),
            ("expected_execution_count", 27, "expected_execution_count"),
            ("expected_target_count", 31, "expected_target_count"),
        )
        for field, value, expected in cases:
            payload = copy.deepcopy(self.payload)
            payload[field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self._load_payload(payload)

    def test_rejects_duplicate_or_unsorted_scheme_rows(self) -> None:
        duplicate = copy.deepcopy(self.payload)
        duplicate["schemes"][-1] = copy.deepcopy(duplicate["schemes"][0])
        unsorted = copy.deepcopy(self.payload)
        unsorted["schemes"][0], unsorted["schemes"][1] = (
            unsorted["schemes"][1],
            unsorted["schemes"][0],
        )

        for expected, payload in (
            ("duplicate scheme_id", duplicate),
            ("sorted by scheme_id", unsorted),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    expected,
                ),
            ):
                self._load_payload(payload)

    def test_rejects_duplicate_keys_at_every_json_object_level(self) -> None:
        original = POLICY_PATH.read_text(encoding="utf-8")
        root_duplicate = original.replace(
            "{\n",
            '{\n  "schema_version": "ignored-duplicate",\n',
            1,
        )
        row_duplicate = original.replace(
            '      "scheme_id": "daily_10y_lgbm_10y04_0629",',
            (
                '      "scheme_id": "ignored-duplicate",\n'
                '      "scheme_id": "daily_10y_lgbm_10y04_0629",'
            ),
            1,
        )

        for field, raw in (
            ("schema_version", root_duplicate),
            ("scheme_id", row_duplicate),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    self.module.DailyGrayLaunchdPolicyError,
                    f"duplicate JSON object key.*{field}",
                ),
            ):
                self.module.load_daily_gray_launchd_policy(
                    self._write_raw_policy(raw),
                    discovered=self.active_daily,
                )

    def test_rejects_non_utf8_policy_with_domain_error(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "policy.json"
        path.write_bytes(b"\xff")

        with self.assertRaisesRegex(
            self.module.DailyGrayLaunchdPolicyError,
            "not valid UTF-8",
        ) as captured:
            self.module.load_daily_gray_launchd_policy(
                path,
                discovered=self.active_daily,
            )

        self.assertIsInstance(captured.exception.__cause__, UnicodeDecodeError)

    def test_rejects_invalid_json_and_non_mapping_root(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        malformed = Path(directory.name) / "malformed.json"
        malformed.write_text("{", encoding="utf-8")

        with self.assertRaisesRegex(
            self.module.DailyGrayLaunchdPolicyError,
            "invalid JSON",
        ):
            self.module.load_daily_gray_launchd_policy(
                malformed,
                discovered=self.active_daily,
            )
        with self.assertRaisesRegex(
            self.module.DailyGrayLaunchdPolicyError,
            "root must be an object",
        ):
            self._load_payload([])


if __name__ == "__main__":
    unittest.main()
