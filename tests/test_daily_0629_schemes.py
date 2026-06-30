from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

from harness.config_loader import load_config_raw
from shared.daily_0629_predict_adapter import DAILY_0629_INTERNAL_FIELDS
from shared.daily_0629_source_evidence import DAILY_0629_SOURCE_ROLE, PLATFORM_CURRENT_DAILY_0629_ROLE


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEME_IDS = (
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
    "daily_10y_lgbm_10y04_0629",
)
DAILY_COLUMNS = {
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "direction",
    "confidence",
    "label",
    "is_correct",
    "benchmark_role",
}


class Daily0629SchemeTests(unittest.TestCase):
    def test_configs_declare_daily_t1_source_original_contract(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
                self.assertEqual(config["scheme_id"], scheme_id)
                self.assertEqual(config["task_type"], "T+1")
                self.assertEqual(config["frequency"], "daily")
                self.assertEqual(config["horizon"], 1)
                self.assertTrue(config["backtest"]["benchmark_required"])
                self.assertEqual(config["backtest"]["benchmark_id"], "daily_0629")
                self.assertEqual(config["backtest"]["data_source"], "framework_db_aligned")
                required = set(config["backtest"]["required_internal_fields"])
                self.assertTrue(required)
                self.assertTrue(required <= set(DAILY_0629_INTERNAL_FIELDS))

    def test_benchmark_files_have_daily_schema_and_internal_fields(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
                required = set(config["backtest"]["required_internal_fields"])
                bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                for csv_name in ("original_predictions_sample.csv", "current_predictions_sample.csv"):
                    header, rows = _read_csv(bench_dir / csv_name)
                    self.assertGreater(len(rows), 0)
                    self.assertTrue(DAILY_COLUMNS <= set(header))
                    self.assertTrue(required <= set(header))
                    for row in rows:
                        self.assertEqual(row["horizon"], "1")
                        self.assertLess(row["target_date"], "2026-06-01")
                        for field in DAILY_COLUMNS | required:
                            self.assertNotEqual(row.get(field), "", f"{scheme_id} {csv_name} missing {field}")

    def test_benchmark_provenance_is_source_original_daily_runner(self) -> None:
        for scheme_id in SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                expected_roles = {
                    "original_backtest_summary.json": (
                        DAILY_0629_SOURCE_ROLE,
                        "source_original_daily_0629_binary_runner",
                    ),
                    "current_backtest_summary.json": (
                        PLATFORM_CURRENT_DAILY_0629_ROLE,
                        "framework_db_aligned",
                    ),
                }
                for summary_name, (expected_role, expected_data_source) in expected_roles.items():
                    summary = json.loads((bench_dir / summary_name).read_text(encoding="utf-8"))
                    self.assertEqual(summary["data_source"], expected_data_source)
                    self.assertGreater(summary["row_count"], 0)
                    provenance = summary["benchmark_provenance"]
                    self.assertEqual(provenance["source_role"], expected_role)
                    self.assertEqual(
                        provenance["source_original_data_source"],
                        "source_original_daily_0629_binary_runner",
                    )
                    rendered = json.dumps(provenance, ensure_ascii=False).lower()
                    self.assertNotIn("model_muti_0529", rendered)
                    self.assertNotIn("t1_daily", rendered)
                    self.assertFalse(provenance["cross_frequency_reuse"])


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    return list(reader.fieldnames or []), rows


if __name__ == "__main__":
    unittest.main()
