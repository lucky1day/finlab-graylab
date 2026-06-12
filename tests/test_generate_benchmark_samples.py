from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text


def _create_engine_with_backtest_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER,
                    backtest_run_id INTEGER,
                    benchmark_id TEXT,
                    scheme_id TEXT,
                    data_source TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT,
                    summary TEXT,
                    report_path TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE VIEW v_latest_backtest_run AS
                SELECT *
                FROM (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY benchmark_id, scheme_id, data_source
                               ORDER BY updated_at DESC, id DESC
                           ) AS rn
                    FROM t_backtest_runs r
                    WHERE status = 'success'
                ) ranked
                WHERE rn = 1
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    predict_date TEXT,
                    predicted_direction INTEGER,
                    confidence REAL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_monthly_metrics (
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    month TEXT,
                    sample_count INTEGER,
                    correct_count INTEGER,
                    accuracy REAL,
                    up_precision REAL,
                    up_recall REAL,
                    down_precision REAL,
                    down_recall REAL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, backtest_run_id, benchmark_id, scheme_id, data_source,
                     start_date, end_date, status, summary, report_path, created_at, updated_at)
                VALUES
                    (92, 92, 'v28_daily_5y_2', 'daily_5y_2_v28', 'framework_db_aligned',
                     '2024-07-01', '2026-04-23', 'success', '{}', NULL, NULL, '2026-06-12T02:31:03'),
                    (93, 93, 'v28_daily_5y_2', 'daily_5y_2_v28', 'framework_db_aligned',
                     '2025-01-02', '2026-05-22', 'success', '{}', NULL, NULL, '2026-06-12T13:56:12'),
                    (94, 94, 'v28_daily_5y_2', 'daily_5y_2_v28', 'framework_db_aligned',
                     '2025-01-02', '2026-05-23', 'failed', '{}', NULL, NULL, '2026-06-12T14:56:12'),
                    (84, 84, 'model_muti_0529', 't5_daily', 'framework_db_aligned',
                     '2025-01-01', '2026-05-31', 'success', '{}', NULL, NULL, '2026-06-10T10:00:00'),
                    (83, 83, 'model_muti_0529', 't5_daily', 'framework_original_csv',
                     '2025-01-01', '2026-05-31', 'success', '{}', NULL, NULL, '2026-06-10T09:00:00')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (run_id, scheme_id, target_tenor, predict_date, predicted_direction, confidence)
                VALUES
                    (92, 'daily_5y_2_v28', '5Y', '2026-04-23', -1, 1.0),
                    (93, 'daily_5y_2_v28', '5Y', '2026-05-22', 1, 0.8),
                    (84, 't5_daily', '5Y', '2026-05-22', -1, 0.5),
                    (83, 't5_daily', '5Y', '2026-05-22', 1, 0.6)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_monthly_metrics
                    (run_id, scheme_id, target_tenor, month, sample_count, correct_count,
                     accuracy, up_precision, up_recall, down_precision, down_recall)
                VALUES
                    (92, 'daily_5y_2_v28', '5Y', '2026-04', 21, 11, 0.524, NULL, NULL, NULL, NULL),
                    (93, 'daily_5y_2_v28', '5Y', '2026-05', 13, 10, 0.769, NULL, NULL, NULL, NULL),
                    (84, 't5_daily', '5Y', '2026-05', 20, 10, 0.5, NULL, NULL, NULL, NULL),
                    (83, 't5_daily', '5Y', '2026-05', 20, 9, 0.45, NULL, NULL, NULL, NULL)
                """
            )
        )
    return engine


class GenerateBenchmarkSamplesTests(unittest.TestCase):
    def test_main_requires_run_id_or_scheme_id_and_benchmark_id(self) -> None:
        from scripts import generate_benchmark_samples

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                generate_benchmark_samples.main([])

        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("provide either --run-id", stderr.getvalue())

    def test_generate_selects_single_canonical_latest_run(self) -> None:
        from scripts import generate_benchmark_samples

        engine = _create_engine_with_backtest_rows()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(generate_benchmark_samples, "SCHEMES_ROOT", Path(tmpdir)):
                with patch.object(generate_benchmark_samples, "create_engine_from_env", return_value=engine):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        generate_benchmark_samples.main([
                            "--scheme-id",
                            "daily_5y_2_v28",
                            "--benchmark-id",
                            "v28_daily_5y_2",
                            "--data-source",
                            "framework_db_aligned",
                        ])

            bench_dir = Path(tmpdir) / "daily_5y_2_v28" / "benchmarks"
            predictions = (bench_dir / "current_predictions_sample.csv").read_text(encoding="utf-8")
            summary = (bench_dir / "current_backtest_summary.json").read_text(encoding="utf-8")

        self.assertIn("selected run_id=93", output.getvalue())
        self.assertIn("2026-05-22", predictions)
        self.assertNotIn("2026-04-23", predictions)
        self.assertIn('"2026-05"', summary)
        self.assertNotIn('"2026-04"', summary)

    def test_generate_run_id_selection_rejects_scheme_mismatch(self) -> None:
        from scripts import generate_benchmark_samples

        engine = _create_engine_with_backtest_rows()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(generate_benchmark_samples, "SCHEMES_ROOT", Path(tmpdir)):
                with patch.object(generate_benchmark_samples, "create_engine_from_env", return_value=engine):
                    with self.assertRaises(SystemExit) as ctx:
                        generate_benchmark_samples.main([
                            "--run-id",
                            "93",
                            "--scheme-id",
                            "t5_daily",
                        ])

        self.assertNotEqual(ctx.exception.code, 0)

    def test_generate_explicit_run_id_selects_only_that_run(self) -> None:
        from scripts import generate_benchmark_samples

        engine = _create_engine_with_backtest_rows()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(generate_benchmark_samples, "SCHEMES_ROOT", Path(tmpdir)):
                with patch.object(generate_benchmark_samples, "create_engine_from_env", return_value=engine):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        generate_benchmark_samples.main(["--run-id", "84"])

            bench_dir = Path(tmpdir) / "t5_daily" / "benchmarks"
            predictions = (bench_dir / "current_predictions_sample.csv").read_text(encoding="utf-8")
            summary = (bench_dir / "current_backtest_summary.json").read_text(encoding="utf-8")

        self.assertIn("selected run_id=84", output.getvalue())
        self.assertIn("2026-05-22", predictions)
        self.assertIn("-1", predictions)
        self.assertNotIn("0.6", predictions)
        self.assertIn('"2026-05"', summary)
