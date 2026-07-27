from __future__ import annotations

import csv
import importlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from backtests.weekly_base_runner import WeeklyAverageLabel
from harness.config_loader import load_config_raw
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE
from shared.weekly_average_source_evidence import is_point_backed_weekly_average_provenance


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEME_SPECS = {
    "weekly_avg_1y_lgbm_0529": {
        "target_tenor": "1Y",
        "runner": "backtests.weekly_avg_1y_lgbm_0529_reproduction",
        "required_internal_fields": [
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ],
    },
    "weekly_avg_5y_lgbm_0529": {
        "target_tenor": "5Y",
        "runner": "backtests.weekly_avg_5y_lgbm_0529_reproduction",
        "required_internal_fields": [
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ],
    },
    "weekly_avg_10y_lgbm_0529": {
        "target_tenor": "10Y",
        "runner": "backtests.weekly_avg_10y_lgbm_0529_reproduction",
        "required_internal_fields": [
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ],
    },
}


class WeeklyAverage0529SchemeTests(unittest.TestCase):
    def test_superseded_point_backed_identities_are_not_discoverable(self) -> None:
        from scheduler.discovery import discover_schemes

        discovered_ids = {
            config.scheme_id
            for config in discover_schemes(PROJECT_ROOT / "schemes", strict=True)
        }

        self.assertTrue(
            {
                "weekly_avg_5y_direct_0529",
                "weekly_avg_7y_cross_d_overlay_0529",
                "weekly_avg_10y_d_overlay_0529",
            }.isdisjoint(discovered_ids)
        )

    def test_weekly_average_0529_scheme_set_is_1y_5y_10y_only(self) -> None:
        from scripts import rebuild_weekly_average_0529_benchmarks as rebuild

        self.assertEqual(set(SCHEME_SPECS), set(rebuild.SPECS))
        self.assertEqual(
            {spec["target_tenor"] for spec in SCHEME_SPECS.values()},
            {"1Y", "5Y", "10Y"},
        )
        self.assertNotIn("weekly_avg_7y_cross_d_overlay_0529", rebuild.SPECS)

    def test_configs_are_active_weekly_average_schemes_after_activation(self) -> None:
        for scheme_id, spec in SCHEME_SPECS.items():
            with self.subTest(scheme_id=scheme_id):
                config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")

                self.assertEqual(config["scheme_id"], scheme_id)
                self.assertEqual(config["task_type"], "weekly_average")
                self.assertEqual(config["frequency"], "weekly")
                self.assertEqual(config["status"], "active")
                self.assertEqual(config["tenors"], [spec["target_tenor"]])
                self.assertEqual(config["target_rule"], WEEKLY_AVERAGE_TARGET_RULE)
                self.assertEqual(config["backtest"]["runner"], spec["runner"])
                self.assertEqual(config["backtest"]["required_internal_fields"], spec["required_internal_fields"])

    def test_predict_adapters_emit_weekly_average_identity(self) -> None:
        for scheme_id, spec in SCHEME_SPECS.items():
            with self.subTest(scheme_id=scheme_id):
                predict = importlib.import_module(f"schemes.{scheme_id}.predict")

                self.assertEqual(predict.SCHEME_ID, scheme_id)
                self.assertEqual(predict.TARGET_TENOR, spec["target_tenor"])
                self.assertEqual(predict.TARGET_RULE, WEEKLY_AVERAGE_TARGET_RULE)

    def test_benchmarks_are_weekly_average_and_have_independent_provenance(self) -> None:
        required_columns = {
            "feature_week_id",
            "feature_date",
            "target_week_id",
            "target_date",
            "target_tenor",
            "horizon",
            "target_rule",
            "benchmark_role",
            "direction",
            "confidence",
            "label",
            "is_correct",
        }
        for scheme_id, spec in SCHEME_SPECS.items():
            with self.subTest(scheme_id=scheme_id):
                bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                current_csv = bench_dir / "current_predictions_sample.csv"
                original_summary = json.loads((bench_dir / "original_backtest_summary.json").read_text())
                current_summary = json.loads((bench_dir / "current_backtest_summary.json").read_text())
                with current_csv.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    current_header = set(rows[0].keys())
                with (bench_dir / "original_predictions_sample.csv").open(encoding="utf-8", newline="") as handle:
                    original_rows = list(csv.DictReader(handle))
                    original_header = set(original_rows[0].keys())

                self.assertEqual(required_columns - original_header, set())
                self.assertEqual(required_columns - current_header, set())
                for field in spec["required_internal_fields"]:
                    self.assertIn(field, original_header)
                    self.assertIn(field, current_header)
                self.assertEqual({row["target_rule"] for row in rows}, {WEEKLY_AVERAGE_TARGET_RULE})
                self.assertEqual({row["target_rule"] for row in original_rows}, {WEEKLY_AVERAGE_TARGET_RULE})
                self.assertEqual(len(rows), len({_strict_key(row) for row in rows}))
                self.assertEqual(len(original_rows), len({_strict_key(row) for row in original_rows}))
                self.assertEqual(current_summary["target_rule"], WEEKLY_AVERAGE_TARGET_RULE)
                self.assertEqual(original_summary["target_rule"], WEEKLY_AVERAGE_TARGET_RULE)
                self.assertEqual(
                    current_summary["original_benchmark_validation"]["benchmark_rows"],
                    len(rows),
                )
                original_provenance = original_summary["benchmark_provenance"]
                current_provenance = current_summary["benchmark_provenance"]
                self.assertFalse(
                    is_point_backed_weekly_average_provenance(original_provenance, scheme_id=scheme_id),
                    original_provenance,
                )
                self.assertFalse(
                    is_point_backed_weekly_average_provenance(current_provenance, scheme_id=scheme_id),
                    current_provenance,
                )
                self.assertNotEqual(
                    original_provenance.get("bootstrap_source"),
                    "weekly_average_framework_db_aligned",
                )
                self.assertNotEqual(original_provenance["generator"], current_provenance["generator"])
                self.assertGreaterEqual(original_provenance["strict_source_original_rows"], 0)
                self.assertGreaterEqual(original_provenance["source_compatible_extension_rows"], 0)

    def test_platform_rows_collapse_duplicate_source_effective_week(self) -> None:
        from backtests.weekly_avg_lgbm_0529_reproduction import _platform_rows_from_source

        evidence = SimpleNamespace(
            frequency="W1Y",
            target_tenor="1Y",
            model_id="WEEKLY-1Y-LGBM-01",
            source_package_hash="hash",
        )
        label = WeeklyAverageLabel(
            feature_week_id=202607,
            target_week_id=202608,
            feature_date="2026-02-27",
            target_date="2026-03-06",
            feature_yield=1.30375,
            target_yield=1.2793,
            future_return=-0.018753595397890763,
            label=-1,
        )
        source_rows = [
            {
                "frequency": "W1Y",
                "effective_week_id": 202607,
                "source_output_date": "2026-02-21",
                "rdate": "2026-02-21",
                "pred_label": -1,
                "prob_up": 0.4685368578863234,
                "threshold_used": 0.51,
                "model_margin": -0.0414631421136766,
                "training_rows": 401,
                "calibration_rows": 114,
                "feature_count": 366,
            },
            {
                "frequency": "W1Y",
                "effective_week_id": 202607,
                "source_output_date": "2026-02-28",
                "rdate": "2026-02-28",
                "pred_label": -1,
                "prob_up": 0.4685368578863234,
                "threshold_used": 0.51,
                "model_margin": -0.0414631421136766,
                "training_rows": 401,
                "calibration_rows": 114,
                "feature_count": 366,
            },
        ]

        rows = _platform_rows_from_source(
            "weekly_avg_1y_lgbm_0529",
            evidence,
            source_rows,
            {202607: label},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["extra"]["source_output_date"], "2026-02-28")

    def test_platform_rows_fail_closed_on_conflicting_duplicate_source_week(self) -> None:
        from backtests.weekly_avg_lgbm_0529_reproduction import _platform_rows_from_source

        evidence = SimpleNamespace(
            frequency="W1Y",
            target_tenor="1Y",
            model_id="WEEKLY-1Y-LGBM-01",
            source_package_hash="hash",
        )
        label = WeeklyAverageLabel(
            feature_week_id=202607,
            target_week_id=202608,
            feature_date="2026-02-27",
            target_date="2026-03-06",
            feature_yield=1.30375,
            target_yield=1.2793,
            future_return=-0.018753595397890763,
            label=-1,
        )
        source_rows = [
            {
                "frequency": "W1Y",
                "effective_week_id": 202607,
                "source_output_date": "2026-02-21",
                "rdate": "2026-02-21",
                "pred_label": -1,
                "prob_up": 0.4685368578863234,
                "threshold_used": 0.51,
                "model_margin": -0.0414631421136766,
                "training_rows": 401,
                "calibration_rows": 114,
                "feature_count": 366,
            },
            {
                "frequency": "W1Y",
                "effective_week_id": 202607,
                "source_output_date": "2026-02-28",
                "rdate": "2026-02-28",
                "pred_label": 1,
                "prob_up": 0.5685368578863234,
                "threshold_used": 0.51,
                "model_margin": 0.0585368578863234,
                "training_rows": 401,
                "calibration_rows": 114,
                "feature_count": 366,
            },
        ]

        with self.assertRaisesRegex(RuntimeError, "conflicting duplicate source weekly-average rows"):
            _platform_rows_from_source(
                "weekly_avg_1y_lgbm_0529",
                evidence,
                source_rows,
                {202607: label},
            )

def _strict_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row["feature_week_id"],
        row["feature_date"],
        row["target_date"],
        row["target_tenor"],
        row["horizon"],
    )


if __name__ == "__main__":
    unittest.main()
