from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


class BaseRunnerMetricsTests(unittest.TestCase):
    def test_make_run_output_filters_excluded_target_ranges_and_uses_spec(self) -> None:
        from backtests._base_runner import BacktestSpec, BaseDailyBacktestRunner

        spec = BacktestSpec(
            benchmark_id="demo_benchmark",
            scheme_id="demo_daily",
            canonical_csv=None,
            target_columns=("TB0YWI0C",),
            start_date="2026-01-01",
            end_date="2026-01-31",
            excluded_target_ranges=(
                {
                    "label": "exclude one target",
                    "start": "2026-01-10",
                    "end": "2026-01-10",
                    "reason": "test",
                },
            ),
        )
        runner = BaseDailyBacktestRunner(spec)

        output = runner.make_run_output(
            data_source="framework_db_aligned",
            rows=[
                {
                    "scheme_id": "demo_daily",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-01-09",
                    "target_date": "2026-01-10",
                    "label": 1,
                    "predicted_direction": 1,
                },
                {
                    "scheme_id": "demo_daily",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-01-12",
                    "target_date": "2026-01-13",
                    "label": -1,
                    "predicted_direction": -1,
                },
            ],
        )

        self.assertEqual(output.scheme_id, "demo_daily")
        self.assertEqual(output.start_date, "2026-01-01")
        self.assertEqual(output.end_date, "2026-01-31")
        self.assertEqual(len(output.rows), 1)
        self.assertEqual(output.summary["raw_row_count"], 2)
        self.assertEqual(output.summary["excluded_row_count"], 1)
        self.assertEqual(output.monthly_metrics[0]["benchmark_id"], "demo_benchmark")
        self.assertEqual(output.monthly_metrics[0]["accuracy"], 1.0)

    def test_weekly_rows_are_grouped_by_target_month(self) -> None:
        from backtests._base_runner import build_monthly_metrics

        rows = [
            {
                "scheme_id": "weekly_demo",
                "target_tenor": "10Y",
                "horizon": 6,
                "predict_date": "2025-10-25",
                "feature_date": "2025-10-31",
                "target_date": "2025-11-07",
                "label": 1,
                "predicted_direction": 1,
                "extra": {"frequency": "weekly"},
            }
        ]

        metrics = build_monthly_metrics(rows, benchmark_id="weekly_benchmark")

        self.assertEqual(metrics[0]["month"], "2025-11")
        self.assertEqual(metrics[0]["sample_count"], 1)
        self.assertEqual(metrics[0]["benchmark_id"], "weekly_benchmark")

    def test_daily_rows_are_grouped_by_target_month(self) -> None:
        from backtests._base_runner import build_monthly_metrics

        rows = [
            {
                "scheme_id": "demo_daily",
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2025-12-29",
                "target_date": "2026-01-05",
                "label": 1,
                "predicted_direction": 1,
            }
        ]

        metrics = build_monthly_metrics(rows, benchmark_id="demo_benchmark")

        self.assertEqual(metrics[0]["month"], "2026-01")
        self.assertEqual(metrics[0]["sample_count"], 1)

    def test_flat_predictions_count_as_samples_but_not_metric_denominator(self) -> None:
        from backtests._base_runner import build_monthly_metrics

        rows = [
            {
                "scheme_id": "demo_daily",
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": f"2026-06-{day:02d}",
                "target_date": f"2026-06-{day + 5:02d}",
                "label": label,
                "predicted_direction": predicted,
            }
            for day, label, predicted in [
                (1, 1, 1),
                (2, -1, -1),
                (3, 1, 1),
                (4, -1, 1),
                (5, 1, -1),
                (6, -1, 1),
                (7, 1, -1),
                (8, 0, 0),
            ]
        ]

        metrics = build_monthly_metrics(rows, benchmark_id="demo_benchmark")

        self.assertEqual(metrics[0]["sample_count"], 8)
        self.assertEqual(metrics[0]["metric_sample_count"], 7)
        self.assertEqual(metrics[0]["correct_count"], 3)
        self.assertAlmostEqual(metrics[0]["accuracy"], 3 / 7)
        self.assertEqual(metrics[0]["predicted_dist"]["flat"], 1)


class BaseRunnerComparisonTests(unittest.TestCase):
    def test_compare_prediction_rows_normalizes_scalars_and_respects_float_tolerance(self) -> None:
        from backtests._base_runner import compare_prediction_rows

        baseline = [
            {
                "target_tenor": "10Y",
                "predict_date": "2026-01-02",
                "label": 1.0,
                "confidence": 0.5000000001,
            }
        ]
        candidate = [
            {
                "target_tenor": "10Y",
                "predict_date": "2026-01-02",
                "label": 1,
                "confidence": 0.5000000002,
            },
            {
                "target_tenor": "5Y",
                "predict_date": "2026-01-02",
                "label": -1,
                "confidence": 0.4,
            },
        ]

        comparison = compare_prediction_rows(
            baseline,
            candidate,
            fields=("label",),
            float_fields=("confidence",),
            float_tolerance=1e-9,
        )

        self.assertEqual(comparison["matched_rows"], 1)
        self.assertEqual(comparison["mismatch_count"], 1)
        self.assertEqual(comparison["candidate_only"], [{"target_tenor": "5Y", "predict_date": "2026-01-02"}])
        self.assertEqual(comparison["mismatch_rows"], [])


class BaseRunnerInputArtifactTests(unittest.TestCase):
    def test_build_db_aligned_daily_uses_supplied_artifact_builder(self) -> None:
        from backtests._base_runner import build_db_aligned_daily

        canonical = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.1],
                "legacy_only": [7.0],
            }
        )
        generated = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.1],
            }
        )
        calls: list[dict[str, object]] = []

        def fake_build_daily_input_artifact(**kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(dataframe=generated, path=Path("/tmp/demo.csv"), source="test")

        _, aligned = build_db_aligned_daily(
            csv_df=canonical,
            engine=object(),
            benchmark_id="demo_benchmark",
            canonical_csv=None,
            artifact_scheme_id="demo_daily",
            artifact_builder=fake_build_daily_input_artifact,
        )

        self.assertEqual(aligned.columns.tolist(), ["date", "TB0YWI0C", "legacy_only"])
        self.assertTrue(pd.isna(aligned.loc[0, "legacy_only"]))
        self.assertEqual(calls[0]["scheme_id"], "demo_daily")
        self.assertEqual(calls[0]["predict_date"], "2026-01-02")
        self.assertEqual(calls[0]["start_date"], "2026-01-02")
        self.assertEqual(calls[0]["end_date"], "2026-01-02")


class BaseRunnerTemplateTests(unittest.TestCase):
    def test_run_framework_db_aligned_builds_input_predicts_and_returns_output(self) -> None:
        from backtests._base_runner import BacktestSpec, BaseDailyBacktestRunner

        class DemoRunner(BaseDailyBacktestRunner):
            def __init__(self, spec: BacktestSpec) -> None:
                super().__init__(spec)
                self.seen_daily_columns: list[str] = []
                self.seen_n_jobs: int | None = None

            def predict_rows(self, daily_df: pd.DataFrame, *, n_jobs: int = 4) -> list[dict[str, object]]:
                self.seen_daily_columns = daily_df.columns.tolist()
                self.seen_n_jobs = n_jobs
                return [
                    {
                        "scheme_id": "demo_daily",
                        "target_tenor": "10Y",
                        "horizon": 1,
                        "predict_date": "2026-01-02",
                        "target_date": "2026-01-05",
                        "label": 1,
                        "predicted_direction": 1,
                    }
                ]

        spec = BacktestSpec(
            benchmark_id="demo_benchmark",
            scheme_id="demo_daily",
            canonical_csv=None,
            target_columns=("TB0YWI0C",),
            start_date="2026-01-02",
            end_date="2026-01-05",
        )
        canonical = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.1],
                "legacy_only": [7.0],
            }
        )
        generated = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.1],
            }
        )

        def fake_build_daily_input_artifact(**_: object) -> SimpleNamespace:
            return SimpleNamespace(dataframe=generated, path=Path("/tmp/demo.csv"), source="test")

        runner = DemoRunner(spec)

        output = runner.run_framework_db_aligned(
            csv_df=canonical,
            engine=object(),
            n_jobs=2,
            persist=False,
            artifact_builder=fake_build_daily_input_artifact,
        )

        self.assertEqual(runner.seen_daily_columns, ["date", "TB0YWI0C", "legacy_only"])
        self.assertEqual(runner.seen_n_jobs, 2)
        self.assertEqual(output.scheme_id, "demo_daily")
        self.assertEqual(output.data_source, "framework_db_aligned")
        self.assertEqual(output.summary["row_count"], 1)
        self.assertEqual(output.monthly_metrics[0]["benchmark_id"], "demo_benchmark")

    def test_persist_run_output_uses_append_backtest_run(self) -> None:
        from backtests import _base_runner

        output = _base_runner.RunOutput(
            scheme_id="demo_daily",
            data_source="framework_db_aligned",
            start_date="2026-01-01",
            end_date="2026-01-31",
            rows=[
                {
                    "benchmark_id": "demo_benchmark",
                    "scheme_id": "demo_daily",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-01-02",
                }
            ],
            monthly_metrics=[
                {
                    "benchmark_id": "demo_benchmark",
                    "scheme_id": "demo_daily",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "month": "2026-01",
                    "sample_count": 1,
                    "correct_count": 1,
                }
            ],
            summary={"row_count": 1},
            report_path="/tmp/report.json",
        )

        engine = object()
        with patch.object(_base_runner, "create_backtest_run", return_value=201) as create_run:
            with patch.object(_base_runner, "replace_backtest_predictions", return_value=1) as replace_predictions:
                with patch.object(_base_runner, "update_backtest_run_summary") as update_summary:
                    run_id = _base_runner.persist_run_output(engine, output, benchmark_id="demo_benchmark")

        self.assertEqual(run_id, 201)
        create_run.assert_called_once()
        self.assertEqual(create_run.call_args.kwargs["run_mode"], "persist")
        replace_predictions.assert_called_once_with(engine, 201, output.rows)
        update_summary.assert_called_once()
        self.assertEqual(update_summary.call_args.kwargs["run_id"], 201)
        self.assertEqual(output.summary["run_id"], 201)


if __name__ == "__main__":
    unittest.main()
