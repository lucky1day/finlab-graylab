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


CONTROL_PLANES = (
    "legacy_automatic",
    "daily_ledger",
    "direct_scheduled",
    "launchd_one_shot",
)
FORMAL_DAILY_CAPABILITIES = frozenset(
    (
        "legacy_automatic",
        "daily_ledger",
        "direct_scheduled",
        "launchd_one_shot",
    )
)
FORMAL_WEEKLY_CAPABILITIES = frozenset(
    (
        "legacy_automatic",
        "direct_scheduled",
        "launchd_one_shot",
    )
)
DAILY_GRAY_CAPABILITIES = frozenset(("daily_ledger",))
LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES = frozenset(
    ("launchd_one_shot",)
)
NO_CAPABILITIES = frozenset()
G31_LAUNCHD_ONE_SHOT_IDENTITIES = frozenset(
    {
        ("one_y_t1_quote_state_hv_v1", "1fd56dfcc264"),
        ("three_y_adyn_lb1_k3_v1", "98233f0cb9ef"),
        ("three_y_adyn_lb2_k1_v1", "47c7c1776db0"),
        ("seven_y_current55_lgbm_001_v2", "cd0624ef3ead"),
        ("seven_y_current55_lgbm_002_v2", "57e956513471"),
    }
)


def _expected_admission(
    *,
    mode: str,
    frequency: str,
    task_type: str,
    horizon: int,
    target_tenor: str,
    capabilities: frozenset[str],
) -> dict[str, object]:
    return {
        "mode": mode,
        "runtime_type": "blackbox_v2",
        "frequency": frequency,
        "task_type": task_type,
        "horizon": horizon,
        "target_tenor": target_tenor,
        "capabilities": capabilities,
    }


EXPECTED_ADMISSIONS = {
    (
        "one_y_t5_liq_excess_a_v1",
        "8d583560c9f1",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="1Y",
        capabilities=FORMAL_DAILY_CAPABILITIES,
    ),
    (
        "one_y_t5_liq_excess_a_w252_l7_v1",
        "103c93bbc913",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="1Y",
        capabilities=FORMAL_DAILY_CAPABILITIES,
    ),
    (
        "one_y_t5_liq_excess_a_w350_l7_v1",
        "86b458c568a5",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="1Y",
        capabilities=FORMAL_DAILY_CAPABILITIES,
    ),
    (
        "one_y_t5_liq_excess_b_w252_l7_v1",
        "ba00891cd179",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="1Y",
        capabilities=FORMAL_DAILY_CAPABILITIES,
    ),
    (
        "weekly_10y_lgbm_point_v1",
        "0666a6989d6b",
    ): _expected_admission(
        mode="formal",
        frequency="weekly",
        task_type="weekly_point",
        horizon=1,
        target_tenor="10Y",
        capabilities=FORMAL_WEEKLY_CAPABILITIES,
    ),
    (
        "cgb_causal_wk_1y",
        "cba824c27f0e",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_point",
        horizon=1,
        target_tenor="1Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_causal_wk_3y",
        "4b8db29b2f74",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_point",
        horizon=1,
        target_tenor="3Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "wavg_1y_gapflip_v5",
        "68999585142a",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_average",
        horizon=1,
        target_tenor="1Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "wavg_3y_gapflip_v5",
        "faba245acaef",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_average",
        horizon=1,
        target_tenor="3Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "wavg_5y_gapflip_v5",
        "63ed1291f9d4",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_average",
        horizon=1,
        target_tenor="5Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "wavg_7y_gapflip_v5",
        "21951d955f44",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_average",
        horizon=1,
        target_tenor="7Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "wavg_10y_gapflip_v5",
        "c1e5a9db6097",
    ): _expected_admission(
        mode="gray",
        frequency="weekly",
        task_type="weekly_average",
        horizon=1,
        target_tenor="10Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_a4_fundseason_1y",
        "04e7af163fb0",
    ): _expected_admission(
        mode="gray",
        frequency="monthly",
        task_type="monthly",
        horizon=1,
        target_tenor="1Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_a4_fundseason_3y",
        "89d31f8bcb95",
    ): _expected_admission(
        mode="gray",
        frequency="monthly",
        task_type="monthly",
        horizon=1,
        target_tenor="3Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_a4_fundseason_5y",
        "7d47e0328532",
    ): _expected_admission(
        mode="gray",
        frequency="monthly",
        task_type="monthly",
        horizon=1,
        target_tenor="5Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_a4_fundseason_7y",
        "ddba87ece7ae",
    ): _expected_admission(
        mode="gray",
        frequency="monthly",
        task_type="monthly",
        horizon=1,
        target_tenor="7Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "cgb_a4_fundseason_10y",
        "85a65700499b",
    ): _expected_admission(
        mode="gray",
        frequency="monthly",
        task_type="monthly",
        horizon=1,
        target_tenor="10Y",
        capabilities=NO_CAPABILITIES,
    ),
    (
        "one_y_t1_quote_state_hv_v1",
        "1fd56dfcc264",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+1",
        horizon=1,
        target_tenor="1Y",
        capabilities=LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES,
    ),
    (
        "seven_y_current55_lgbm_001_v2",
        "cd0624ef3ead",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+1",
        horizon=1,
        target_tenor="7Y",
        capabilities=LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES,
    ),
    (
        "seven_y_current55_lgbm_002_v2",
        "57e956513471",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+1",
        horizon=1,
        target_tenor="7Y",
        capabilities=LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES,
    ),
    (
        "three_y_adyn_lb2_k1_v1",
        "47c7c1776db0",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+1",
        horizon=1,
        target_tenor="3Y",
        capabilities=LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES,
    ),
    (
        "three_y_adyn_lb1_k3_v1",
        "98233f0cb9ef",
    ): _expected_admission(
        mode="formal",
        frequency="daily",
        task_type="T+1",
        horizon=1,
        target_tenor="3Y",
        capabilities=LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES,
    ),
    (
        "ten_y_t5_maj3_k3_ic_static_v1",
        "c54b90bcafa7",
    ): _expected_admission(
        mode="gray",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="10Y",
        capabilities=DAILY_GRAY_CAPABILITIES,
    ),
    (
        "ten_y_t5_maj4_k3_ic_static_v1",
        "6bdabf86b4a6",
    ): _expected_admission(
        mode="gray",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="10Y",
        capabilities=DAILY_GRAY_CAPABILITIES,
    ),
    (
        "ten_y_t5_maj4_k3_ic_yearly_v1",
        "af04567a19c3",
    ): _expected_admission(
        mode="gray",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="10Y",
        capabilities=DAILY_GRAY_CAPABILITIES,
    ),
    (
        "ten_y_t5_say_k5_sharpe_static_v1",
        "e8137af4b655",
    ): _expected_admission(
        mode="gray",
        frequency="daily",
        task_type="T+5",
        horizon=5,
        target_tenor="10Y",
        capabilities=DAILY_GRAY_CAPABILITIES,
    ),
}
EXPECTED_MODES = {
    identity: str(row["mode"])
    for identity, row in EXPECTED_ADMISSIONS.items()
}


def _config(
    scheme_id: str,
    scheme_version: str,
    *,
    runtime_type: str = "blackbox_v2",
    frequency: str | None = None,
    task_type: str | None = None,
    horizon: int | None = None,
    target_tenor: str | None = None,
) -> SimpleNamespace:
    expected = EXPECTED_ADMISSIONS.get(
        (scheme_id, scheme_version),
        {},
    )
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        runtime_type=runtime_type,
        frequency=frequency or expected.get("frequency", "daily"),
        task_type=task_type or expected.get("task_type", "T+5"),
        horizon=(
            horizon
            if horizon is not None
            else expected.get("horizon", 5)
        ),
        tenors=[
            target_tenor
            or str(expected.get("target_tenor", "1Y"))
        ],
    )


class BlackboxSchedulerAdmissionTests(unittest.TestCase):
    def test_recurring_control_plane_is_retired(self) -> None:
        """weekly exact identity 仅移除 recurring，保留其余冻结能力。"""
        policy = load_blackbox_scheduler_admission()
        config = _config(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )

        self.assertFalse(hasattr(admission_module, "RECURRING"))
        self.assertNotIn(
            "recurring",
            admission_module.VALID_CONTROL_PLANES,
        )
        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "unknown Blackbox scheduler control plane: recurring",
        ):
            policy.allows(config, plane="recurring")
        self.assertTrue(
            policy.allows(config, plane="legacy_automatic")
        )
        self.assertTrue(
            policy.allows(config, plane="direct_scheduled")
        )
        self.assertTrue(
            policy.allows(config, plane="launchd_one_shot")
        )

    def test_exact_control_plane_permission_matrix(self) -> None:
        """每个冻结 Blackbox 身份只获得明确列出的控制面能力。"""
        policy = load_blackbox_scheduler_admission()
        discovered = {
            config.scheme_id: config
            for config in discover_schemes()
            if config.runtime_type == "blackbox_v2"
        }

        self.assertEqual(
            set(EXPECTED_ADMISSIONS),
            set(EXPECTED_MODES),
        )
        for identity, expected in EXPECTED_ADMISSIONS.items():
            scheme_id, scheme_version = identity
            config = discovered[scheme_id]
            self.assertEqual(config.scheme_version, scheme_version)
            self.assertEqual(config.runtime_type, expected["runtime_type"])
            self.assertEqual(config.frequency, expected["frequency"])
            self.assertEqual(config.task_type, expected["task_type"])
            self.assertEqual(config.horizon, expected["horizon"])
            self.assertEqual(config.tenors, [expected["target_tenor"]])
            for plane in CONTROL_PLANES:
                with self.subTest(identity=identity, plane=plane):
                    self.assertEqual(
                        policy.allows(config, plane=plane),
                        plane in expected["capabilities"],
                    )

    def test_control_plane_matrix_fails_closed_for_identity_drift(
        self,
    ) -> None:
        """未知、版本漂移、runtime 重分类在所有控制面均拒绝。"""
        policy = load_blackbox_scheduler_admission()
        candidates = [
            _config("unknown_demo", "version-1"),
            _config(
                "one_y_t5_liq_excess_a_v1",
                "version-drift",
            ),
        ]
        candidates.extend(
            _config(
                scheme_id,
                scheme_version,
                runtime_type="native_adapter",
            )
            for scheme_id, scheme_version in EXPECTED_ADMISSIONS
        )

        for config in candidates:
            for plane in CONTROL_PLANES:
                with self.subTest(
                    scheme_id=config.scheme_id,
                    plane=plane,
                ):
                    self.assertFalse(
                        policy.allows(config, plane=plane)
                    )

    def test_control_plane_matrix_rejects_execution_metadata_drift(
        self,
    ) -> None:
        """频率、任务、horizon、tenor 任一漂移均不得获得能力。"""
        policy = load_blackbox_scheduler_admission()
        identity = (
            "one_y_t5_liq_excess_a_v1",
            "8d583560c9f1",
        )
        drifted = (
            _config(*identity, frequency="weekly"),
            _config(*identity, task_type="T+1"),
            _config(*identity, horizon=1),
            _config(*identity, target_tenor="10Y"),
        )

        for config in drifted:
            for plane in CONTROL_PLANES:
                with self.subTest(config=config, plane=plane):
                    self.assertFalse(
                        policy.allows(
                            config,
                            plane=plane,
                        )
                    )

    def test_non_reserved_native_remains_outside_control_planes(
        self,
    ) -> None:
        """非保留 Native 不依赖 Blackbox admission。"""
        policy = load_blackbox_scheduler_admission()
        native = _config(
            "native_demo",
            "not-listed",
            runtime_type="native_adapter",
        )

        for plane in CONTROL_PLANES:
            with self.subTest(plane=plane):
                self.assertTrue(policy.allows(native, plane=plane))

    def test_deployed_policy_declares_exact_capabilities(self) -> None:
        """部署 policy 是 exact capability 的唯一配置来源。"""
        payload = json.loads(
            admission_module.DEFAULT_ADMISSION_PATH.read_text(
                encoding="utf-8"
            )
        )
        actual = {
            (row["scheme_id"], row["scheme_version"]): {
                "runtime_type": row["runtime_type"],
                "frequency": row["frequency"],
                "task_type": row["task_type"],
                "horizon": row["horizon"],
                "target_tenor": row["target_tenor"],
                "mode": row["mode"],
                "capabilities": frozenset(row["capabilities"]),
            }
            for row in payload["schemes"]
        }

        self.assertEqual(
            actual,
            {
                identity: expected
                for identity, expected in EXPECTED_ADMISSIONS.items()
            },
        )

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
            set(admission_module.EXPECTED_EXACT_ADMISSIONS),
            set(EXPECTED_ADMISSIONS),
        )
        self.assertEqual(
            admission_module.RESERVED_BLACKBOX_SCHEME_IDS,
            {
                scheme_id
                for scheme_id, _scheme_version in EXPECTED_MODES
            },
        )
        self.assertEqual(set(policy.entries), set(EXPECTED_ADMISSIONS))
        for identity, mode in EXPECTED_MODES.items():
            with self.subTest(identity=identity):
                scheme_id, scheme_version = identity
                config = discovered[scheme_id]
                self.assertEqual(config.scheme_version, scheme_version)
                self.assertEqual(policy.mode(config), mode)
                self.assertEqual(
                    policy.allows(
                        config,
                        plane="legacy_automatic",
                    ),
                    "legacy_automatic"
                    in EXPECTED_ADMISSIONS[identity]["capabilities"],
                )

    def test_g31_exact_identities_are_launchd_one_shot_only(self) -> None:
        """G3.1 formal 身份不继承 legacy、ledger 或 direct 能力。"""
        policy = load_blackbox_scheduler_admission()

        self.assertEqual(
            G31_LAUNCHD_ONE_SHOT_IDENTITIES,
            {
                identity
                for identity, expected in EXPECTED_ADMISSIONS.items()
                if expected["capabilities"]
                == LAUNCHD_ONE_SHOT_ONLY_CAPABILITIES
            },
        )
        for identity in G31_LAUNCHD_ONE_SHOT_IDENTITIES:
            config = _config(*identity)
            with self.subTest(identity=identity):
                self.assertEqual(policy.mode(config), "formal")
                for plane in CONTROL_PLANES:
                    self.assertEqual(
                        policy.allows(config, plane=plane),
                        plane == "launchd_one_shot",
                    )

    def test_native_is_scheduled_without_blackbox_policy_identity(
        self,
    ) -> None:
        policy = load_blackbox_scheduler_admission()

        self.assertTrue(
            policy.allows(
                _config(
                    "native_demo",
                    "not-listed",
                    runtime_type="native_adapter",
                ),
                plane="legacy_automatic",
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

        self.assertFalse(
            policy.allows(
                reclassified,
                plane="legacy_automatic",
            )
        )

    def test_unknown_and_version_drift_are_not_scheduled(self) -> None:
        config = _config(
            "one_y_t5_liq_excess_a_v1",
            "8d583560c9f1",
        )
        policy = load_blackbox_scheduler_admission()

        self.assertTrue(
            policy.allows(
                config,
                plane="legacy_automatic",
            )
        )
        self.assertFalse(
            policy.allows(
                _config("unknown_demo", "version-1"),
                plane="legacy_automatic",
            )
        )
        self.assertFalse(
            policy.allows(
                _config(config.scheme_id, "version-drift"),
                plane="legacy_automatic",
            )
        )

    def test_gray_identity_is_not_scheduled(self) -> None:
        config = _config(
            "cgb_a4_fundseason_1y",
            "04e7af163fb0",
        )
        policy = load_blackbox_scheduler_admission()

        self.assertEqual(policy.mode(config), "gray")
        self.assertFalse(
            policy.allows(
                config,
                plane="legacy_automatic",
            )
        )

    def test_ten_year_t5_gray_identities_are_not_scheduled(self) -> None:
        policy = load_blackbox_scheduler_admission()
        identities = {
            identity
            for identity, mode in EXPECTED_MODES.items()
            if identity[0].startswith("ten_y_t5_")
            and mode == "gray"
        }

        self.assertEqual(len(identities), 4)
        for scheme_id, scheme_version in identities:
            with self.subTest(scheme_id=scheme_id):
                config = _config(scheme_id, scheme_version)
                self.assertEqual(policy.mode(config), "gray")
                self.assertFalse(
                    policy.allows(
                        config,
                        plane="legacy_automatic",
                    )
                )
                self.assertFalse(
                    policy.allows(
                        _config(
                            scheme_id,
                            scheme_version,
                            runtime_type="native_adapter",
                        ),
                        plane="legacy_automatic",
                    )
                )

    def test_launchd_one_shot_is_exactly_capability_listed(self) -> None:
        """one-shot 只由 frozen capability 决定，不从 mode 或 tenor 推断。"""
        policy = load_blackbox_scheduler_admission()
        for identity, expected in EXPECTED_ADMISSIONS.items():
            with self.subTest(identity=identity):
                self.assertEqual(
                    policy.allows(
                        _config(*identity),
                        plane="launchd_one_shot",
                    ),
                    "launchd_one_shot"
                    in expected["capabilities"],
                )

    def test_empty_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "exact frozen admissions",
        ):
            self._load(self._payload())

    def test_unknown_formal_identity_is_rejected(self) -> None:
        payload = self._expected_payload()
        unknown = dict(payload["schemes"][0])
        unknown["scheme_id"] = "unknown_formal"
        unknown["scheme_version"] = "unknown-version"
        payload["schemes"].append(unknown)

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

    def test_duplicate_root_json_key_is_rejected(self) -> None:
        payload = (
            '{"schema_version":"blackbox-scheduler-admission-v1",'
            '"schema_version":"blackbox-scheduler-admission-v1",'
            '"schemes":[]}'
        )

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate JSON key: schema_version",
        ):
            self._load_text(payload)

    def test_duplicate_entry_json_key_is_rejected(self) -> None:
        payload = (
            admission_module.DEFAULT_ADMISSION_PATH.read_text(
                encoding="utf-8"
            ).replace(
                '"runtime_type": "blackbox_v2",',
                '"runtime_type": "blackbox_v2",'
                '\n      "runtime_type": "blackbox_v2",',
                1,
            )
        )

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate JSON key: runtime_type",
        ):
            self._load_text(payload)

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
        row = self._expected_payload()["schemes"][0]

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate",
        ):
            self._load(self._payload(row, dict(row)))

    def test_policy_rejects_duplicate_base_scheme_id(self) -> None:
        payload = self._expected_payload()
        duplicate = dict(payload["schemes"][0])
        duplicate["scheme_version"] = "different-version"
        payload["schemes"].append(duplicate)

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate scheme_id",
        ):
            self._load(payload)

    def test_policy_rejects_duplicate_capability(self) -> None:
        payload = self._expected_payload()
        row = next(
            item
            for item in payload["schemes"]
            if item["capabilities"]
        )
        row["capabilities"].append(row["capabilities"][0])

        with self.assertRaisesRegex(
            BlackboxSchedulerAdmissionError,
            "duplicate capability",
        ):
            self._load(payload)

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
                "runtime_type": expected["runtime_type"],
                "frequency": expected["frequency"],
                "task_type": expected["task_type"],
                "horizon": expected["horizon"],
                "target_tenor": expected["target_tenor"],
                "mode": expected["mode"],
                "capabilities": sorted(expected["capabilities"]),
            }
            for (scheme_id, scheme_version), expected in sorted(
                EXPECTED_ADMISSIONS.items()
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

    @staticmethod
    def _load_text(payload: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admission.json"
            path.write_text(payload, encoding="utf-8")
            return load_blackbox_scheduler_admission(path)


if __name__ == "__main__":
    unittest.main()
