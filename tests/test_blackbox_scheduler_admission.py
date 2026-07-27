"""Blackbox 自动调度 admission 的精确身份与 fail-closed 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scheduler.blackbox_scheduler_admission import (
    DEFAULT_ADMISSION_PATH,
    BlackboxSchedulerAdmissionError,
    load_blackbox_scheduler_admission,
)
from scheduler.discovery import discover_schemes


EXPECTED_MODES = {
    ("one_y_t5_liq_excess_a_v1", "8d583560c9f1"): "formal",
    (
        "one_y_t5_liq_excess_a_w252_l7_v1",
        "103c93bbc913",
    ): "formal",
    (
        "one_y_t5_liq_excess_a_w350_l7_v1",
        "86b458c568a5",
    ): "formal",
    (
        "one_y_t5_liq_excess_b_w252_l7_v1",
        "ba00891cd179",
    ): "formal",
    ("weekly_10y_lgbm_point_v1", "0666a6989d6b"): "formal",
    ("cgb_a4_fundseason_1y", "04e7af163fb0"): "gray",
    ("cgb_a4_fundseason_3y", "89d31f8bcb95"): "gray",
    ("cgb_a4_fundseason_5y", "7d47e0328532"): "gray",
    ("cgb_a4_fundseason_7y", "ddba87ece7ae"): "gray",
    ("cgb_a4_fundseason_10y", "85a65700499b"): "gray",
}


def _config(
    scheme_id: str,
    scheme_version: str,
    *,
    runtime_type: str = "blackbox_v2",
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        runtime_type=runtime_type,
    )


class BlackboxSchedulerAdmissionTests(unittest.TestCase):
    def test_deployed_policy_exactly_freezes_formal_and_gray_identities(
        self,
    ) -> None:
        policy = load_blackbox_scheduler_admission()
        discovered = {
            config.scheme_id: config
            for config in discover_schemes()
            if config.runtime_type == "blackbox_v2"
        }

        self.assertEqual(dict(policy.entries), EXPECTED_MODES)
        for identity, mode in EXPECTED_MODES.items():
            with self.subTest(identity=identity):
                scheme_id, scheme_version = identity
                config = discovered[scheme_id]
                self.assertEqual(config.scheme_version, scheme_version)
                self.assertEqual(policy.mode(config), mode)
                self.assertEqual(
                    policy.is_scheduled(config),
                    mode == "formal",
                )

    def test_native_is_scheduled_without_blackbox_policy_identity(
        self,
    ) -> None:
        policy = self._load(
            {
                "schema_version": "blackbox-scheduler-admission-v1",
                "schemes": [],
            }
        )

        self.assertTrue(
            policy.is_scheduled(
                _config(
                    "native_demo",
                    "not-listed",
                    runtime_type="native_adapter",
                )
            )
        )

    def test_unknown_and_version_drift_are_not_scheduled(self) -> None:
        config = _config("formal_demo", "version-1")
        policy = self._load(
            self._payload(
                {
                    "scheme_id": config.scheme_id,
                    "scheme_version": config.scheme_version,
                    "mode": "formal",
                }
            )
        )

        self.assertTrue(policy.is_scheduled(config))
        self.assertFalse(
            policy.is_scheduled(
                _config("unknown_demo", "version-1")
            )
        )
        self.assertFalse(
            policy.is_scheduled(
                _config(config.scheme_id, "version-2")
            )
        )

    def test_gray_identity_is_not_scheduled(self) -> None:
        config = _config("gray_demo", "version-1")
        policy = self._load(
            self._payload(
                {
                    "scheme_id": config.scheme_id,
                    "scheme_version": config.scheme_version,
                    "mode": "gray",
                }
            )
        )

        self.assertEqual(policy.mode(config), "gray")
        self.assertFalse(policy.is_scheduled(config))

    def test_malformed_json_is_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "invalid JSON",
            ):
                load_blackbox_scheduler_admission(path)

    def test_policy_rejects_invalid_root_and_entry_shapes(self) -> None:
        invalid_payloads = (
            [],
            {
                "schema_version": "blackbox-scheduler-admission-v1",
                "schemes": {},
            },
            {
                "schema_version": "blackbox-scheduler-admission-v1",
                "schemes": ["not-an-object"],
            },
            {
                "schema_version": "wrong-version",
                "schemes": [],
            },
            {
                "schema_version": "blackbox-scheduler-admission-v1",
                "schemes": [],
                "unexpected": True,
            },
            self._payload(
                {
                    "scheme_id": "formal_demo",
                    "scheme_version": "version-1",
                    "mode": "formal",
                    "unexpected": True,
                }
            ),
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(
                    BlackboxSchedulerAdmissionError
                ):
                    self._load(payload)

    def test_policy_rejects_duplicate_exact_identity(self) -> None:
        row = {
            "scheme_id": "duplicate_demo",
            "scheme_version": "version-1",
            "mode": "formal",
        }

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate",
        ):
            self._load(self._payload(row, dict(row)))

    def test_policy_rejects_empty_identity_and_illegal_mode(self) -> None:
        invalid_rows = (
            {
                "scheme_id": "",
                "scheme_version": "version-1",
                "mode": "formal",
            },
            {
                "scheme_id": "formal_demo",
                "scheme_version": " ",
                "mode": "formal",
            },
            {
                "scheme_id": "formal_demo",
                "scheme_version": "version-1",
                "mode": "active",
            },
        )

        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(
                    BlackboxSchedulerAdmissionError
                ):
                    self._load(self._payload(row))

    @staticmethod
    def _payload(*rows: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "blackbox-scheduler-admission-v1",
            "schemes": list(rows),
        }

    @staticmethod
    def _load(payload: object):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            return load_blackbox_scheduler_admission(path)


if __name__ == "__main__":
    unittest.main()
