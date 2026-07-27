"""Blackbox 自动调度 admission 的精确身份与 fail-closed 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scheduler import blackbox_scheduler_admission as admission_module
from scheduler.blackbox_scheduler_admission import (
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

        self.assertEqual(
            dict(admission_module.EXPECTED_EXACT_ADMISSIONS),
            EXPECTED_MODES,
        )
        self.assertEqual(
            admission_module.RESERVED_BLACKBOX_SCHEME_IDS,
            {
                scheme_id
                for scheme_id, _scheme_version in EXPECTED_MODES
            },
        )
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
        policy = load_blackbox_scheduler_admission()

        self.assertTrue(
            policy.is_scheduled(
                _config(
                    "native_demo",
                    "not-listed",
                    runtime_type="native_adapter",
                )
            )
        )

    def test_reserved_blackbox_id_cannot_reclassify_as_native(
        self,
    ) -> None:
        policy = load_blackbox_scheduler_admission()
        reclassified = _config(
            "cgb_a4_fundseason_1y",
            "04e7af163fb0",
            runtime_type="native_adapter",
        )

        self.assertFalse(policy.is_scheduled(reclassified))

    def test_unknown_and_version_drift_are_not_scheduled(self) -> None:
        config = _config(
            "one_y_t5_liq_excess_a_v1",
            "8d583560c9f1",
        )
        policy = load_blackbox_scheduler_admission()

        self.assertTrue(policy.is_scheduled(config))
        self.assertFalse(
            policy.is_scheduled(
                _config("unknown_demo", "version-1")
            )
        )
        self.assertFalse(
            policy.is_scheduled(
                _config(config.scheme_id, "version-drift")
            )
        )

    def test_gray_identity_is_not_scheduled(self) -> None:
        config = _config(
            "cgb_a4_fundseason_1y",
            "04e7af163fb0",
        )
        policy = load_blackbox_scheduler_admission()

        self.assertEqual(policy.mode(config), "gray")
        self.assertFalse(policy.is_scheduled(config))

    def test_empty_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(self._payload())

    def test_unknown_formal_identity_is_rejected(self) -> None:
        payload = self._expected_payload()
        payload["schemes"].append(
            {
                "scheme_id": "unknown_formal",
                "scheme_version": "unknown-version",
                "mode": "formal",
            }
        )

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(payload)

    def test_missing_exact_identity_is_rejected(self) -> None:
        payload = self._expected_payload()
        payload["schemes"].pop()

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(payload)

    def test_mode_drift_is_rejected(self) -> None:
        payload = self._expected_payload()
        formal_row = next(
            row
            for row in payload["schemes"]
            if row["mode"] == "formal"
        )
        formal_row["mode"] = "gray"

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(payload)

    def test_version_drift_is_rejected(self) -> None:
        payload = self._expected_payload()
        payload["schemes"][0]["scheme_version"] = "version-drift"

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(payload)

    def test_malformed_json_is_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "invalid JSON",
            ):
                load_blackbox_scheduler_admission(path)

    def test_non_utf8_policy_is_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_bytes(b"\xff\xfe\x80")

            with self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "UTF-8",
            ):
                load_blackbox_scheduler_admission(path)

    def test_missing_policy_is_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-admission.json"

            with self.assertRaisesRegex(
                BlackboxSchedulerAdmissionError,
                "not found",
            ):
                load_blackbox_scheduler_admission(path)

    def test_schema_drift_is_configuration_error(self) -> None:
        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "schema_version",
        ):
            self._load(
                {
                    "schema_version": "future-version",
                    "schemes": [],
                }
            )

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
    def _expected_payload() -> dict[str, object]:
        rows = [
            {
                "scheme_id": scheme_id,
                "scheme_version": scheme_version,
                "mode": mode,
            }
            for (scheme_id, scheme_version), mode in sorted(
                EXPECTED_MODES.items()
            )
        ]
        return BlackboxSchedulerAdmissionTests._payload(*rows)

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
