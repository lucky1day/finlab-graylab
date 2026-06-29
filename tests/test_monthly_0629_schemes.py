from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

from harness.config_loader import load_config_raw
from shared.monthly_predict_adapter import INTERNAL_FIELDS
from shared.monthly_source_evidence import MONTHLY_SOURCE_ROLE, PLATFORM_CURRENT_MONTHLY_ROLE
from shared.prediction_context import MONTHLY_TARGET_RULE


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEME_IDS = (
    "monthly_1y_rf_top30_0629",
    "monthly_5y_knn_top20_0629",
    "monthly_10y_rf_top5_0629",
)
MONTHLY_COLUMNS = {
    "feature_month_id",
    "feature_date",
    "target_month_id",
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


class Monthly0629SchemeTests(unittest.TestCase):
    def test_configs_declare_monthly_source_original_contract(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
                self.assertEqual(config["scheme_id"], scheme_id)
                self.assertEqual(config["task_type"], "monthly")
                self.assertEqual(config["frequency"], "monthly")
                self.assertEqual(config["horizon"], 30)
                self.assertEqual(config["target_rule"], MONTHLY_TARGET_RULE)
                self.assertTrue(config["backtest"]["benchmark_required"])
                self.assertEqual(config["backtest"]["benchmark_id"], "monthly_0629")
                required = set(config["backtest"]["required_internal_fields"])
                self.assertTrue(required)
                self.assertTrue(required <= set(INTERNAL_FIELDS))

    def test_benchmark_files_have_monthly_schema_and_internal_fields(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
                required = set(config["backtest"]["required_internal_fields"])
                bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                for csv_name in ("original_predictions_sample.csv", "current_predictions_sample.csv"):
                    header, rows = _read_csv(bench_dir / csv_name)
                    self.assertGreater(len(rows), 0)
                    self.assertTrue(MONTHLY_COLUMNS <= set(header))
                    self.assertTrue(required <= set(header))
                    for row in rows:
                        self.assertEqual(row["target_rule"], MONTHLY_TARGET_RULE)
                        for field in MONTHLY_COLUMNS | required:
                            self.assertNotEqual(row.get(field), "", f"{scheme_id} {csv_name} missing {field}")

    def test_benchmark_provenance_is_not_point_or_weekly_backed(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                expected_roles = {
                    "original_backtest_summary.json": MONTHLY_SOURCE_ROLE,
                    "current_backtest_summary.json": PLATFORM_CURRENT_MONTHLY_ROLE,
                }
                for summary_name, expected_role in expected_roles.items():
                    summary = json.loads((bench_dir / summary_name).read_text(encoding="utf-8"))
                    provenance = summary["benchmark_provenance"]
                    self.assertEqual(provenance["source_role"], expected_role)
                    rendered = json.dumps(provenance, ensure_ascii=False).lower()
                    self.assertNotIn("point_runner", rendered)
                    self.assertNotIn("point_scheme_id", rendered)
                    self.assertNotIn("weekly_point", rendered)
                    self.assertFalse(provenance["cross_frequency_reuse"])


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    return list(reader.fieldnames or []), rows


if __name__ == "__main__":
    unittest.main()
