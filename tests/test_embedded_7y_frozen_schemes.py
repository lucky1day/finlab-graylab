from dataclasses import FrozenInstanceError
import unittest

import tools.embedded_7y_blackbox.frozen_schemes as frozen_schemes
from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES, get_scheme


class FrozenSchemeTests(unittest.TestCase):
    def test_exact_approved_scheme_identities(self) -> None:
        self.assertEqual(
            set(SCHEMES),
            {
                "seven_y_t1_cfc_0084_embedded_v1",
                "seven_y_t1_cfc_0156_embedded_v1",
            },
        )
        self.assertEqual(
            get_scheme("seven_y_t1_cfc_0084_embedded_v1").candidate_hash,
            "69bf3a432784639d",
        )
        self.assertEqual(
            get_scheme("seven_y_t1_cfc_0156_embedded_v1").candidate_hash,
            "ad0d94dc15febd8a",
        )
        self.assertFalse(hasattr(frozen_schemes, "_SCHEMES"))

    def test_only_member_difference_is_frozen_explicitly(self) -> None:
        fast = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
        standard = get_scheme("seven_y_t1_cfc_0156_embedded_v1")
        self.assertEqual(fast.curve_member_id, "7y-fcco-08756")
        self.assertEqual(fast.candidate_id, "7y-cfc-0084")
        self.assertEqual(fast.curve_family, "fast_causal_curve_orientation")
        self.assertEqual(fast.curve_member_hash, "e80445063865243d")
        self.assertEqual(fast.edge_minimum_history, 21)
        self.assertEqual(standard.curve_member_id, "7y-cco-03620")
        self.assertEqual(standard.candidate_id, "7y-cfc-0156")
        self.assertEqual(standard.curve_family, "causal_curve_orientation")
        self.assertEqual(standard.curve_member_hash, "182131092906070b")
        self.assertEqual(standard.edge_minimum_history, 14)
        self.assertEqual(fast.member_weights, (1, 2))
        self.assertEqual(standard.member_weights, (1, 2))
        self.assertEqual(fast.hfas_member_id, "7y-hfas-10173")
        self.assertEqual(standard.hfas_member_id, "7y-hfas-10173")
        self.assertEqual(fast.hfas_member_hash, "a8cbf01f35327bb7")
        self.assertEqual(standard.hfas_member_hash, "a8cbf01f35327bb7")
        self.assertEqual(fast.threshold, 0.0)
        self.assertEqual(standard.threshold, 0.0)
        self.assertEqual(fast.tie_rule, "abstain")
        self.assertEqual(standard.tie_rule, "abstain")

    def test_common_constants_and_anchor_identities_are_exact(self) -> None:
        self.assertEqual(
            frozen_schemes.DIRECT_YIELD_COLUMNS,
            ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C"),
        )
        self.assertEqual(frozen_schemes.FAIR_VALUE_WEIGHTS, (0.6, 0.4))
        self.assertEqual(frozen_schemes.EDGE_WINDOW, 42)
        self.assertEqual(frozen_schemes.GAP_Z_MINIMUM_HISTORY, 42)
        self.assertEqual(frozen_schemes.GAP_Z_WINDOW, 84)
        self.assertEqual(frozen_schemes.CURVE_THRESHOLD_WINDOW, 252)
        self.assertEqual(frozen_schemes.CURVE_ACTIVE_RATE, 0.55)
        self.assertEqual(frozen_schemes.HFAS_ACTIVE_RATE, 0.60)
        self.assertEqual(frozen_schemes.HFAS_THRESHOLD_WINDOW, 42)
        self.assertEqual(frozen_schemes.HFAS_STATE_Z_WINDOW, 84)
        self.assertEqual(frozen_schemes.ANCHOR_WEIGHTS, (1.0, -1.0))
        self.assertEqual(
            frozen_schemes.FIVE_Y10_ANCHOR,
            frozen_schemes.AnchorIdentity(
                label="5Y10",
                source_scheme_id="daily_5y_lgbm_5y10_0629",
                config_sha256="0bd80068ff2c287b0c679ed555bab1dc1720815fd386a4aeb77a0a9f81190e62",
                runner_sha256="695f4d6365311827e732b02c6e6a2387d4f297d8e929d209818402c36b17f1c1",
            ),
        )
        self.assertEqual(
            frozen_schemes.TEN_Y04_ANCHOR,
            frozen_schemes.AnchorIdentity(
                label="10Y04",
                source_scheme_id="daily_10y_lgbm_10y04_0629",
                config_sha256="587fda91c9ca85a3805c9832ad99e4f6cb80e3f39c3f1057161cf944ac7b9712",
                runner_sha256="c29c87600e017c4609b9a75a04b9310788f5bd6c113532e54861383619d8a77f",
            ),
        )

    def test_scheme_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            get_scheme("seven_y_t1_cfc_0084_embedded_v1").threshold = 1.0
