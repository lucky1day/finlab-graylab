from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pandas as pd

from experiments.t5_no_foreign_lgbm.metrics import (
    MetricCounts,
    align_branches,
    monthly_comparison,
)
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
from experiments.t5_no_foreign_lgbm.report import render_report


APPROVED_SCHEME_IDS = (
    "daily_5y_2_v28__h5__5Y",
    "daily_7y_1_v28__h5__7Y",
    "liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y",
    "liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y",
    "liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y",
    "liwei_0616_5y_auc_static_all_k3_div_k10__h5__5Y",
    "liwei_0616_5y_auc_yearly_all_k3_div_k10__h5__5Y",
    "liwei_0616_5y_ic_yearly_all_k3_div_k10__h5__5Y",
    "liwei_0616_5y01_full_oos_k3_div_k10__h5__5Y",
    "liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y",
    "liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y",
    "liwei_0616_cons_sda_k3_div_k10__h5__5Y",
    "one_y_t5_liq_excess_a_v1__h5__1Y",
    "one_y_t5_liq_excess_a_w252_l7_v1__h5__1Y",
    "one_y_t5_liq_excess_a_w350_l7_v1__h5__1Y",
    "one_y_t5_liq_excess_b_w252_l7_v1__h5__1Y",
    "t5_daily__h5__10Y",
    "t5_daily__h5__3Y",
    "t5_daily__h5__5Y",
    "t5_daily__h5__7Y",
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


class MetricAndReportTests(unittest.TestCase):
    def _rows(self, directions: list[int]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "scheme_id": ["demo__h5__5Y"] * 5,
                "target_tenor": ["5Y"] * 5,
                "feature_date": [
                    "2026-03-25",
                    "2026-03-26",
                    "2026-04-24",
                    "2026-04-27",
                    "2026-05-25",
                ],
                "target_date": [
                    "2026-04-01",
                    "2026-04-02",
                    "2026-05-06",
                    "2026-05-07",
                    "2026-06-01",
                ],
                "label": [1, -1, -1, -1, 1],
                "direction": directions,
            }
        )

    def test_align_branches_rejects_sample_or_label_mismatch(self) -> None:
        baseline = self._rows([1, 0, 1, -1, 0])
        changed_label = self._rows([1, 0, 1, -1, 0])
        changed_label.loc[0, "label"] = -1
        with self.assertRaisesRegex(RuntimeError, "sample keys or labels"):
            align_branches(baseline, changed_label)
        changed_date = self._rows([1, 0, 1, -1, 0])
        changed_date.loc[0, "target_date"] = "2026-04-03"
        with self.assertRaisesRegex(RuntimeError, "sample keys or labels"):
            align_branches(baseline, changed_date)

    def test_monthly_and_aggregate_metrics_use_counts(self) -> None:
        baseline = self._rows([1, 0, 1, -1, 0])
        ablation = self._rows([1, -1, 0, -1, 1])
        rows = monthly_comparison(baseline, ablation)
        by_period = {row["period"]: row for row in rows}
        self.assertEqual(
            MetricCounts(eligible=2, trades=1, correct=1),
            by_period["2026-04"]["baseline"],
        )
        self.assertEqual(
            MetricCounts(eligible=1, trades=0, correct=0),
            by_period["2026-06"]["baseline"],
        )
        self.assertIsNone(by_period["2026-06"]["baseline"].accuracy)
        aggregate = by_period["2026-04..06"]
        self.assertEqual(
            MetricCounts(eligible=5, trades=3, correct=2),
            aggregate["baseline"],
        )
        self.assertAlmostEqual(2 / 3, aggregate["baseline"].accuracy)
        self.assertAlmostEqual(3 / 5, aggregate["baseline"].trade_rate)
        self.assertEqual(
            MetricCounts(eligible=5, trades=4, correct=4),
            aggregate["ablation"],
        )
        self.assertAlmostEqual(1 / 3, aggregate["accuracy_delta"])
        self.assertAlmostEqual(1 / 5, aggregate["trade_rate_delta"])

    def test_report_renders_twenty_configs_counts_audits_and_failures(self) -> None:
        comparison = monthly_comparison(
            self._rows([1, 0, 1, -1, 0]),
            self._rows([1, -1, 0, -1, 1]),
        )
        schemes = []
        for index, scheme_id in enumerate(APPROVED_SCHEME_IDS):
            status = "success"
            if index == 12:
                status = "not_applicable"
            if index == 19:
                status = "failed"
            schemes.append(
                {
                    "scheme_id": scheme_id,
                    "status": status,
                    "reason": "synthetic failure" if status == "failed" else "",
                    "comparisons": comparison if status != "failed" else [],
                    "factor_audit": {
                        "removed_sources": ["USDCNH0C", "S0031525"],
                        "removed_columns": ["mf_USDCNH0C_r1"],
                        "retained_count": 10,
                        "candidate_count": 11,
                        "ust_sources_used": [],
                    },
                    "changed_target_dates": ["2026-04-02"],
                }
            )
        bundle = {
            "metadata": {
                "code_commit": "abc123",
                "python_version": "3.12",
                "lightgbm_version": "4.6.0",
                "source_hashes": {"daily_output.csv": "deadbeef"},
                "source_unchanged": True,
            },
            "manifest_order": list(APPROVED_SCHEME_IDS),
            "schemes": schemes,
        }
        rendered = render_report(bundle)
        self.assertIn("未修改灰度实验室", rendered)
        self.assertIn("正确/出手/有效", rendered)
        self.assertIn("准确率变化", rendered)
        self.assertIn("出手率变化", rendered)
        self.assertIn("USDCNH0C", rendered)
        self.assertIn("美国国债收益率实际使用数：0", rendered)
        self.assertIn("2026-04-02", rendered)
        self.assertIn("deadbeef", rendered)
        self.assertIn("LightGBM 4.6.0", rendered)
        self.assertIn("N/A", rendered)
        self.assertIn("synthetic failure", rendered)
        overview = rendered.split("## 配置明细", 1)[0]
        for scheme_id in APPROVED_SCHEME_IDS:
            self.assertEqual(1, overview.count(scheme_id))
        failed_section = rendered.split(
            f"### {APPROVED_SCHEME_IDS[-1]}", 1
        )[1]
        self.assertNotIn("synthetic failure |", failed_section)


if __name__ == "__main__":
    unittest.main()
