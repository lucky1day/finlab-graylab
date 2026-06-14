from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import pandas as pd


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

    def test_daily0529_runner_keeps_may_last_target_week_rows(self) -> None:
        from backtests.daily_0529_reproduction import make_run_output

        output = make_run_output(
            scheme_id="t5_daily",
            data_source="framework_db_aligned",
            start_date="2025-01-01",
            end_date="2026-05-31",
            rows=[
                {
                    "scheme_id": "t5_daily",
                    "target_tenor": "5Y",
                    "horizon": 5,
                    "predict_date": "2026-05-18",
                    "feature_date": "2026-05-18",
                    "target_date": "2026-05-25",
                    "label": -1,
                    "predicted_direction": 1,
                    "confidence": 0.38,
                }
            ],
        )

        self.assertEqual(len(output.rows), 1)
        self.assertEqual(output.summary["raw_row_count"], 1)
        self.assertEqual(output.summary["excluded_row_count"], 0)
        self.assertEqual(output.rows[0]["target_date"], "2026-05-25")

    def test_daily0529_benchmark_frame_appends_required_may29_db_row(self) -> None:
        from backtests.daily_0529_reproduction import build_daily0529_benchmark_frame

        csv_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-28"]),
                "TB5YWI0C": [1.4175],
            }
        )

        def fake_artifact_builder(**kwargs):
            self.assertEqual(kwargs["end_date"], "2026-05-29")
            return SimpleNamespace(
                dataframe=pd.DataFrame(
                    {
                        "date": pd.to_datetime(["2026-05-28", "2026-05-29"]),
                        "TB1YWI0C": [1.1410, 1.1425],
                        "TB3YWI0C": [1.2625, 1.2700],
                        "TB5YWI0C": [1.4175, 1.4100],
                        "TB7YWI0C": [1.5600, 1.5475],
                        "TB0YWI0C": [1.7150, 1.7070],
                    }
                )
            )

        frame = build_daily0529_benchmark_frame(csv_df, artifact_builder=fake_artifact_builder)

        self.assertEqual(frame["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-05-28", "2026-05-29"])
        self.assertEqual(float(frame.iloc[-1]["TB5YWI0C"]), 1.4100)


if __name__ == "__main__":
    unittest.main()
