from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pandas as pd

from experiments.t5_no_foreign_lgbm.phase_cache import (
    splice_phase_cache,
    target_map,
    write_json_checkpoint,
    write_pickle_checkpoint,
)
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


class PhaseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = pd.DataFrame(
            {
                "rdate": [
                    "2026-03-24",
                    "2026-03-25",
                    "2026-03-26",
                    "2026-03-27",
                    "2026-03-30",
                    "2026-03-31",
                    "2026-04-01",
                    "2026-04-02",
                    "2026-04-03",
                    "2026-04-07",
                ]
            }
        )
        self.mapping = target_map(self.calendar, horizon=5)
        self.configs = ({"window": 200}, {"window": 350})
        self.baseline = {
            "test_dates": ["2026-03-24", "2026-03-25", "2026-03-26"],
            "results": [
                {
                    "config": self.configs[0],
                    "preds": np.array([1, 1, 1], dtype=np.int32),
                    "probs": np.array([0.10, 0.20, 0.30]),
                },
                {
                    "config": self.configs[1],
                    "preds": np.array([-1, -1, -1], dtype=np.int32),
                    "probs": np.array([0.60, 0.70, 0.80]),
                },
            ],
        }
        self.ablation = {
            "test_dates": ["2026-03-25", "2026-03-26"],
            "results": [
                {
                    "config": self.configs[0],
                    "preds": np.array([-1, -1], dtype=np.int32),
                    "probs": np.array([0.41, 0.42]),
                },
                {
                    "config": self.configs[1],
                    "preds": np.array([1, 1], dtype=np.int32),
                    "probs": np.array([0.51, 0.52]),
                },
            ],
        }

    def test_target_map_uses_fifth_later_trading_date(self) -> None:
        self.assertEqual("2026-03-31", self.mapping["2026-03-24"])
        self.assertEqual("2026-04-01", self.mapping["2026-03-25"])

    def test_splice_keeps_march_target_and_replaces_from_april_target(self) -> None:
        merged = splice_phase_cache(
            self.baseline,
            self.ablation,
            feature_to_target=self.mapping,
            target_start="2026-04-01",
            target_end="2026-06-30",
        )
        np.testing.assert_array_equal(
            np.array([1, -1, -1], dtype=np.int32),
            merged["results"][0]["preds"],
        )
        np.testing.assert_allclose(
            np.array([0.10, 0.41, 0.42]),
            merged["results"][0]["probs"],
        )
        np.testing.assert_array_equal(
            np.array([-1, 1, 1], dtype=np.int32),
            merged["results"][1]["preds"],
        )
        self.assertIsNot(
            self.baseline["results"][0]["preds"],
            merged["results"][0]["preds"],
        )
        np.testing.assert_array_equal(
            np.array([1, 1, 1], dtype=np.int32),
            self.baseline["results"][0]["preds"],
        )

    def test_splice_fails_when_replacement_date_is_missing(self) -> None:
        bad = dict(self.ablation)
        bad["test_dates"] = ["2026-03-25"]
        bad["results"] = [
            {
                **result,
                "preds": result["preds"][:1],
                "probs": result["probs"][:1],
            }
            for result in self.ablation["results"]
        ]
        with self.assertRaisesRegex(RuntimeError, "missing ablation dates"):
            splice_phase_cache(
                self.baseline,
                bad,
                feature_to_target=self.mapping,
                target_start="2026-04-01",
                target_end="2026-06-30",
            )

    def test_splice_fails_on_duplicate_dates_or_config_mismatch(self) -> None:
        duplicate = {**self.baseline, "test_dates": ["2026-03-24"] * 3}
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            splice_phase_cache(
                duplicate,
                self.ablation,
                feature_to_target=self.mapping,
                target_start="2026-04-01",
                target_end="2026-06-30",
            )
        mismatch = {
            **self.ablation,
            "results": [
                {**self.ablation["results"][0], "config": {"window": 201}},
                self.ablation["results"][1],
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "config"):
            splice_phase_cache(
                self.baseline,
                mismatch,
                feature_to_target=self.mapping,
                target_start="2026-04-01",
                target_end="2026-06-30",
            )

    def test_splice_fails_on_non_finite_or_length_mismatch(self) -> None:
        non_finite = {
            **self.ablation,
            "results": [
                {**self.ablation["results"][0], "probs": np.array([np.nan, 0.4])},
                self.ablation["results"][1],
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "finite"):
            splice_phase_cache(
                self.baseline,
                non_finite,
                feature_to_target=self.mapping,
                target_start="2026-04-01",
                target_end="2026-06-30",
            )
        length_mismatch = {
            **self.ablation,
            "results": [
                {**self.ablation["results"][0], "preds": np.array([-1])},
                self.ablation["results"][1],
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "length"):
            splice_phase_cache(
                self.baseline,
                length_mismatch,
                feature_to_target=self.mapping,
                target_start="2026-04-01",
                target_end="2026-06-30",
            )

    def test_checkpoint_writers_are_atomic_and_confined(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            json_path = write_json_checkpoint(root, "state/run.json", {"ok": True})
            pickle_path = write_pickle_checkpoint(root, "cache/phase.pkl", {"ok": True})
            self.assertEqual('{"ok":true}\n', json_path.read_text(encoding="utf-8"))
            self.assertTrue(pickle_path.is_file())
            with self.assertRaisesRegex(ValueError, "outside experiment root"):
                write_json_checkpoint(root, "../escape.json", {"ok": False})


if __name__ == "__main__":
    unittest.main()
