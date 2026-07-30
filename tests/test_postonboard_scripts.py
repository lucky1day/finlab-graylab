from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PostOnboardScriptTests(unittest.TestCase):
    def test_run_baseline_reads_per_scheme_static_benchmark(self) -> None:
        from scripts.run_baseline import run_baseline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme_config(root, "demo_daily")
            benchmark = root / "schemes" / "demo_daily" / "benchmarks"
            benchmark.mkdir(parents=True)
            (benchmark / "original_predictions_sample.csv").write_text(
                "feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct\n"
                "2026-06-04,2026-06-05,10Y,1,1,0.7,1,1\n",
                encoding="utf-8",
            )
            output_dir = root / "reports" / "demo_daily"

            payload, exit_code = run_baseline("demo_daily", output_dir=output_dir, project_root=root)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["evidence"]["morphology"], "static_benchmark_csv")
            self.assertEqual(payload["evidence"]["sample_count"], 1)
            output_path = Path(payload["evidence"]["output_path"])
            self.assertTrue(output_path.exists())
            rows = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(rows[0]["feature_date"], "2026-06-04")
            self.assertEqual(rows[0]["target_date"], "2026-06-05")
            self.assertEqual(rows[0]["target_tenor"], "10Y")
            self.assertEqual(rows[0]["predicted_direction"], 1)
            self.assertEqual(rows[0]["confidence"], 0.7)

    def test_run_baseline_rejects_source_evidence_batch_as_static_benchmark(self) -> None:
        from scripts.run_baseline import run_baseline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme_config(root, "demo_daily")
            source_evidence = root / "source_evidence" / "benchmark_batches" / "model_muti_0529"
            source_evidence.mkdir(parents=True)
            (source_evidence / "daily_output.csv").write_text(
                "date,TB0YWI0C\n2026-06-05,1.7\n",
                encoding="utf-8",
            )

            payload, exit_code = run_baseline("demo_daily", output_dir=root / "reports", project_root=root)

            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(any("missing per-scheme benchmark CSV" in item for item in payload["errors"]))

    def test_run_baseline_rejects_static_benchmark_missing_required_fields(self) -> None:
        from scripts.run_baseline import run_baseline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme_config(root, "demo_daily")
            benchmark = root / "schemes" / "demo_daily" / "benchmarks"
            benchmark.mkdir(parents=True)
            (benchmark / "original_predictions_sample.csv").write_text(
                "date,TB0YWI0C\n2026-06-05,1.7\n",
                encoding="utf-8",
            )

            payload, exit_code = run_baseline("demo_daily", output_dir=root / "reports", project_root=root)

            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(any("missing required columns" in item for item in payload["errors"]))

    def test_run_framework_repro_extracts_matching_framework_run(self) -> None:
        from scripts.run_framework_repro import run_framework_repro

        runner_payload = {
            "runs": [
                {"scheme_id": "demo_daily", "data_source": "baseline_original_csv", "rows": 1, "summary": {}},
                {
                    "scheme_id": "demo_daily",
                    "data_source": "framework_db_aligned",
                    "rows": 2,
                    "summary": {"row_count": 2, "by_tenor": {"10Y": {"samples": 2}}},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme_config(root, "demo_daily")
            (root / "backtests").mkdir()
            (root / "backtests" / "demo_daily_reproduction.py").write_text("", encoding="utf-8")
            output_dir = root / "reports" / "demo_daily"

            with patch("scripts.run_framework_repro._run_backtest_runner", return_value=runner_payload):
                payload, exit_code = run_framework_repro(
                    "demo_daily",
                    output_dir=output_dir,
                    project_root=root,
                    algo_env="forecast_env",
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["evidence"]["sample_count"], 2)
            self.assertEqual(payload["evidence"]["input_artifact_source"], "shared_data_service_daily")
            self.assertEqual(payload["evidence"]["data_version"], "shared_data_service_daily.v1")
            self.assertTrue(Path(payload["evidence"]["output_path"]).exists())

    def test_verify_frontend_db_detects_no_mismatches(self) -> None:
        from scripts.verify_frontend_db import compare_frontend_db_cells

        api_payload = {
            "schemes": [
                {
                    "scheme_id": "demo_daily__h1__10Y",
                    "base_scheme_id": "demo_daily",
                    "run_id": 7,
                    "target_tenor": "10Y",
                    "monthly_metrics": [
                        {"month": "2026-06", "samples": 2, "correct": 1, "accuracy": 50.0},
                    ],
                }
            ]
        }
        db_rows = [
            {
                "target_tenor": "10Y",
                "target_date": "2026-06-05",
                "predict_date": "2026-06-01",
                "label": 1,
                "predicted_direction": 1,
            },
            {
                "target_tenor": "10Y",
                "target_date": "2026-06-06",
                "predict_date": "2026-06-02",
                "label": -1,
                "predicted_direction": 1,
            }
        ]

        evidence = compare_frontend_db_cells(api_payload, db_rows, scheme_id="demo_daily", run_id=7)

        self.assertEqual(evidence["tenors_checked"], ["10Y"])
        self.assertEqual(evidence["total_cells_checked"], 1)
        self.assertEqual(evidence["mismatch_count"], 0)
        self.assertEqual(evidence["mismatches"], [])
        self.assertEqual(evidence["frontend_only_cells"], 0)
        self.assertEqual(evidence["database_only_cells"], 0)

    def test_verify_frontend_db_fails_when_api_missing_db_cells(self) -> None:
        from scripts.verify_frontend_db import compare_frontend_db_cells

        evidence = compare_frontend_db_cells(
            {"schemes": []},
            [
                {
                    "target_tenor": "10Y",
                    "target_date": "2026-06-05",
                    "predict_date": "2026-06-01",
                    "label": 1,
                    "predicted_direction": 1,
                }
            ],
            scheme_id="demo_daily",
            run_id=7,
        )

        self.assertEqual(evidence["total_cells_checked"], 1)
        self.assertEqual(evidence["mismatch_count"], 1)
        self.assertEqual(evidence["database_only_cells"], 1)
        self.assertEqual(evidence["mismatches"][0]["kind"], "missing_cell")

    def test_verify_frontend_db_uses_selected_run_data_source(self) -> None:
        from scripts.verify_frontend_db import _factor_lab_url

        self.assertEqual(
            _factor_lab_url(
                "http://127.0.0.1:8100/",
                "blackbox_v2_current_snapshot_as_of",
            ),
            "http://127.0.0.1:8100/api/backtests/factor-lab?"
            "data_source=blackbox_v2_current_snapshot_as_of",
        )

    def test_verify_scheduler_mount_evaluates_config_registry_and_log(self) -> None:
        from scripts.verify_scheduler_mount import evaluate_scheduler_mount

        evidence, errors, status = evaluate_scheduler_mount(
            scheme_id="demo_daily",
            config_status="active",
            config_cron="25 9 * * 1-5",
            registry_status="active",
            registry_cron="25 9 * * 1-5",
            scheduler_running=True,
            log_text="INFO Scheduled scheme demo_daily at 25 9 * * 1-5",
        )

        self.assertEqual(status, "pass")
        self.assertEqual(errors, [])
        self.assertTrue(evidence["cron_registered"])


def _write_scheme_config(root: Path, scheme_id: str) -> None:
    scheme_dir = root / "schemes" / scheme_id
    scheme_dir.mkdir(parents=True)
    (scheme_dir / "core").mkdir()
    (scheme_dir / "config.yaml").write_text(
        f"""
scheme_id: {scheme_id}
name: Demo
description: Demo
horizon: 1
tenors: ["10Y"]
frequency: daily
schedule:
  cron: "25 9 * * 1-5"
  timezone: "Asia/Shanghai"
entry_point: predict.run
status: active
input_spec:
  data_version: shared_data_service_daily.v1
backtest:
  runner: backtests.{scheme_id}_reproduction
""".strip(),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
