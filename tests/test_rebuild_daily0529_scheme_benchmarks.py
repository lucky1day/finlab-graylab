from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class RebuildDaily0529SchemeBenchmarksTests(unittest.TestCase):
    def test_benchmark_row_uses_strict_feature_target_key_fields(self) -> None:
        from scripts.rebuild_daily0529_scheme_benchmarks import BENCHMARK_FIELDNAMES, benchmark_row

        row = benchmark_row(
            {
                "feature_date": "2025-01-02",
                "target_date": "2025-01-09",
                "target_tenor": "5Y",
                "horizon": 5,
                "predicted_direction": -1,
                "confidence": 0.42,
                "label": -1,
            }
        )

        self.assertEqual(
            BENCHMARK_FIELDNAMES,
            [
                "feature_date",
                "target_date",
                "target_tenor",
                "horizon",
                "direction",
                "confidence",
                "label",
                "is_correct",
            ],
        )
        self.assertEqual(row["feature_date"], "2025-01-02")
        self.assertEqual(row["target_date"], "2025-01-09")
        self.assertEqual(row["target_tenor"], "5Y")
        self.assertEqual(row["horizon"], "5")
        self.assertEqual(row["direction"], "-1")
        self.assertEqual(row["confidence"], "0.42")
        self.assertEqual(row["label"], "-1")
        self.assertEqual(row["is_correct"], "true")

    def test_summary_for_export_removes_runner_diagnostics(self) -> None:
        from scripts.rebuild_daily0529_scheme_benchmarks import summary_for_export

        summary = summary_for_export(
            {
                "row_count": 2,
                "accuracy": 0.5,
                "comparison": {"mismatch_count": 0},
            }
        )

        self.assertEqual(summary, {"row_count": 2, "accuracy": 0.5})

    def test_write_predictions_csv_uses_lf_line_endings(self) -> None:
        from scripts.rebuild_daily0529_scheme_benchmarks import _write_predictions_csv

        rows = [
            {
                "feature_date": "2025-01-02",
                "target_date": "2025-01-03",
                "target_tenor": "5Y",
                "horizon": "1",
                "direction": "1",
                "confidence": "0.5",
                "label": "1",
                "is_correct": "true",
            }
        ]
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.csv"
            _write_predictions_csv(path, rows)
            raw = path.read_bytes()

        self.assertNotIn(b"\r\n", raw)
        self.assertIn(b"\n", raw)


if __name__ == "__main__":
    unittest.main()
