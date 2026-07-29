from dataclasses import FrozenInstanceError
import unittest

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

    def test_only_member_difference_is_frozen_explicitly(self) -> None:
        fast = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
        standard = get_scheme("seven_y_t1_cfc_0156_embedded_v1")
        self.assertEqual(fast.curve_member_id, "7y-fcco-08756")
        self.assertEqual(fast.edge_minimum_history, 21)
        self.assertEqual(standard.curve_member_id, "7y-cco-03620")
        self.assertEqual(standard.edge_minimum_history, 14)
        self.assertEqual(fast.member_weights, (1, 2))
        self.assertEqual(standard.member_weights, (1, 2))

    def test_scheme_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            get_scheme("seven_y_t1_cfc_0084_embedded_v1").threshold = 1.0
