from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from experiments.t5_no_foreign_lgbm.policy import (
    FOREIGN_DAILY_CODES,
    FOREIGN_WEEKLY_CODES,
    FeatureAudit,
    feature_source,
    is_us_treasury_name,
    patch_core_feature_builders,
)


class ForeignPolicyTests(unittest.TestCase):
    def test_registry_covers_approved_daily_and_weekly_sources(self) -> None:
        self.assertEqual(
            {
                "S0031525",
                "M0000005",
                "M0000271",
                "USDCNH0C",
                "SX5EDF0C",
                "G0003892",
                "G0006352",
                "G0006353",
                "G0003956",
                "B2559386",
                "DRS00001",
                "DRS00002",
            },
            set(FOREIGN_DAILY_CODES),
        )
        self.assertEqual(
            {"HWW00001", "HWW00002", "HWW00003"},
            set(FOREIGN_WEEKLY_CODES),
        )

    def test_us_treasury_aliases_are_detected(self) -> None:
        aliases = (
            "美国10年期国债收益率",
            "美债2Y",
            "U.S. Treasury 10Y",
            "Treasury yield spread",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertTrue(is_us_treasury_name(alias))
        self.assertFalse(is_us_treasury_name("中国10年期国债收益率"))

    def test_feature_source_parses_all_supported_naming_styles(self) -> None:
        self.assertEqual("USDCNH0C", feature_source("mf_USDCNH0C_r20"))
        self.assertEqual("HWW00003", feature_source("wk_HWW00003_chg"))
        self.assertEqual("S0031525", feature_source("S0031525_ret10"))
        self.assertIsNone(feature_source("5Y_mom_sum20"))

    def test_patch_removes_foreign_features_before_screening_and_restores_module(self) -> None:
        def build_mf_features(_frame, _categories=None):
            values = pd.DataFrame(
                {
                    "mf_USDCNH0C_r1": [1.0],
                    "mf_S0031525_z20": [2.0],
                    "mf_DR007IBC_r1": [3.0],
                    "mf_SH000300_r5": [4.0],
                }
            )
            categories = {
                "mf_USDCNH0C_r1": "fx",
                "mf_S0031525_z20": "energy",
                "mf_DR007IBC_r1": "money_market",
                "mf_SH000300_r5": "equity",
            }
            return values, categories

        def build_wkmo_features(_weekly, _monthly):
            return pd.DataFrame(
                {
                    "wk_HWW00003_chg": [1.0],
                    "wk_S0114089_val": [2.0],
                    "mo_M0000545_val": [3.0],
                }
            )

        module = SimpleNamespace(
            build_mf_features=build_mf_features,
            build_wkmo_features=build_wkmo_features,
        )
        original_mf = module.build_mf_features
        original_wkmo = module.build_wkmo_features
        audit = FeatureAudit()

        with patch_core_feature_builders(module, audit):
            mf_frame, categories = module.build_mf_features(pd.DataFrame())
            wkmo_frame = module.build_wkmo_features(pd.DataFrame(), pd.DataFrame())
            self.assertEqual(
                ["mf_DR007IBC_r1", "mf_SH000300_r5"],
                list(mf_frame.columns),
            )
            self.assertEqual(set(mf_frame.columns), set(categories))
            self.assertEqual(
                ["wk_S0114089_val", "mo_M0000545_val"],
                list(wkmo_frame.columns),
            )

        self.assertIs(original_mf, module.build_mf_features)
        self.assertIs(original_wkmo, module.build_wkmo_features)
        self.assertEqual(
            {
                "mf_USDCNH0C_r1",
                "mf_S0031525_z20",
                "wk_HWW00003_chg",
            },
            set(audit.removed_columns),
        )
        self.assertEqual(
            {"USDCNH0C", "S0031525", "HWW00003"},
            audit.foreign_sources_seen,
        )
        self.assertTrue(
            {
                "mf_DR007IBC_r1",
                "mf_SH000300_r5",
                "wk_S0114089_val",
                "mo_M0000545_val",
            }.issubset(audit.retained_columns)
        )


if __name__ == "__main__":
    unittest.main()
