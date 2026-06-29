from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

from harness.config_loader import load_config_raw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"

REQUIRED_BENCHMARK_FILES = (
    "original_predictions_sample.csv",
    "current_predictions_sample.csv",
    "original_backtest_summary.json",
    "current_backtest_summary.json",
)
STRICT_PREDICTION_COLUMNS = {
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "direction",
    "confidence",
    "label",
    "is_correct",
}
WEEKLY_AVERAGE_COLUMNS = {
    "feature_week_id",
    "target_week_id",
    "target_rule",
    "benchmark_role",
}
LEGACY_ONLY_COLUMNS = {"predict_date", "tenor", "framework_feature_date", "framework_target_date"}
LIWEI_SOURCE_SCHEME_IDS = {
    "liwei_0616_10y01_cons_say_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5",
    "liwei_0616_7y01_cons_say_k3_div_k10",
    "liwei_0616_7y03_cons_all_k3_div_k8",
    "liwei_0616_cons_sda_k3_div_k10",
}


class BenchmarkParadigmTests(unittest.TestCase):
    """所有 active benchmark 方案必须使用逐方案严格 benchmark 基线。"""

    def test_active_required_benchmarks_use_strict_prediction_schema(self) -> None:
        failures: list[str] = []

        for config_path in sorted(SCHEMES_ROOT.glob("*/config.yaml")):
            config = load_config_raw(config_path)
            if config.get("status") != "active":
                continue
            backtest = config.get("backtest")
            if not isinstance(backtest, dict) or not backtest.get("benchmark_required"):
                continue

            scheme_id = str(config.get("scheme_id") or config_path.parent.name)
            bench_dir = config_path.parent / "benchmarks"
            missing_files = [name for name in REQUIRED_BENCHMARK_FILES if not (bench_dir / name).exists()]
            if missing_files:
                failures.append(f"{scheme_id}: missing benchmark files {missing_files}")
                continue

            original_header, original_rows = _read_csv_header_and_count(
                bench_dir / "original_predictions_sample.csv"
            )
            current_header, current_rows = _read_csv_header_and_count(
                bench_dir / "current_predictions_sample.csv"
            )
            for label, header, rows in (
                ("original", original_header, original_rows),
                ("current", current_header, current_rows),
            ):
                missing_columns = sorted(STRICT_PREDICTION_COLUMNS - set(header))
                if missing_columns:
                    failures.append(f"{scheme_id}: {label} missing strict columns {missing_columns}")
                if rows <= 0:
                    failures.append(f"{scheme_id}: {label} benchmark CSV is empty")

            if original_header != current_header:
                failures.append(f"{scheme_id}: original/current benchmark headers differ")

            original_columns = set(original_header)
            task_type = str(config.get("task_type") or "")
            if task_type == "weekly_average":
                for label, header in (("original", original_header), ("current", current_header)):
                    missing_weekly = sorted(WEEKLY_AVERAGE_COLUMNS - set(header))
                    if missing_weekly:
                        failures.append(f"{scheme_id}: {label} missing weekly average columns {missing_weekly}")
                required_internal = backtest.get("required_internal_fields")
                if not isinstance(required_internal, list) or not required_internal:
                    failures.append(f"{scheme_id}: weekly average benchmark missing backtest.required_internal_fields")
                    required_internal = []
                for field in required_internal:
                    if field not in original_columns:
                        failures.append(f"{scheme_id}: original missing required internal field {field}")
                    if field not in set(current_header):
                        failures.append(f"{scheme_id}: current missing required internal field {field}")
                for summary_name in ("original_backtest_summary.json", "current_backtest_summary.json"):
                    summary = json.loads((bench_dir / summary_name).read_text(encoding="utf-8"))
                    provenance = summary.get("benchmark_provenance")
                    if not isinstance(provenance, dict):
                        failures.append(f"{scheme_id}: {summary_name} missing benchmark_provenance")

            if LEGACY_ONLY_COLUMNS & original_columns and not STRICT_PREDICTION_COLUMNS <= original_columns:
                failures.append(
                    f"{scheme_id}: legacy columns present without full strict schema "
                    f"{sorted(LEGACY_ONLY_COLUMNS & original_columns)}"
                )
            if scheme_id in LIWEI_SOURCE_SCHEME_IDS:
                internal_columns = [
                    column
                    for column in original_header
                    if column == "vote_score" or column.endswith(("_score", "_vs", "_dir", "_sign"))
                ]
                if not internal_columns:
                    failures.append(f"{scheme_id}: source benchmark missing internal score/dir columns")
                elif "vote_score" not in internal_columns:
                    failures.append(f"{scheme_id}: source benchmark missing vote_score column")

        self.assertEqual(failures, [])


def _read_csv_header_and_count(path: Path) -> tuple[list[str], int]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = sum(1 for _ in reader)
    return header, rows


if __name__ == "__main__":
    unittest.main()
