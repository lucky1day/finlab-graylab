from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
from sqlalchemy import create_engine, text


class BlackboxApiGateTests(unittest.TestCase):
    _INSTANCE = {
        "fingerprint_version": "2",
        "fingerprint": "f" * 64,
    }

    def test_blackbox_passed_all_uses_harness_id_tiebreaker_for_same_finished_second(self) -> None:
        from harness.blackbox_v2.gates import _verify_passed_all

        required = (
            "static",
            "input",
            "unit",
            "dry-run",
            "compare",
            "backtest",
            "api-readiness",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
            reports = {}
            for harness_run_id in ("hr_a", "hr_z"):
                report_dir = root / harness_run_id
                state_dir = report_dir / "blackbox_v2"
                state_dir.mkdir(parents=True)
                (state_dir / "input_state.json").write_text(
                    json.dumps(
                        {
                            "snapshot_id": f"snapshot-{harness_run_id}",
                            "generation_id": f"generation-{harness_run_id}",
                            "runtime_profile": "blackbox-v2-v1",
                            "environment_fingerprint": "e" * 64,
                        }
                    ),
                    encoding="utf-8",
                )
                reports[harness_run_id] = report_dir
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE t_harness_runs (harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, stage TEXT, status TEXT, finished_at TEXT, report_uri TEXT)"))
                connection.execute(text("CREATE TABLE t_harness_gate_results (harness_run_id TEXT, gate_name TEXT, status TEXT)"))
                for harness_run_id in ("hr_a", "hr_z"):
                    connection.execute(
                        text("INSERT INTO t_harness_runs VALUES (:run_id, :scheme, :version, 'all', 'passed', '2026-07-20T10:00:00', :report_uri)"),
                        {
                            "run_id": harness_run_id,
                            "scheme": cfg.scheme_id,
                            "version": cfg.scheme_version,
                            "report_uri": str(reports[harness_run_id]),
                        },
                    )
                    connection.execute(
                        text("INSERT INTO t_harness_gate_results VALUES (:run_id, :gate_name, 'passed')"),
                        [
                            {"run_id": harness_run_id, "gate_name": gate_name}
                            for gate_name in required
                        ],
                    )

            passed = _verify_passed_all(engine, cfg)

        self.assertEqual(passed.harness_run_id, "hr_z")
        self.assertEqual(passed.data_snapshot_id, "snapshot-hr_z")

    def test_passed_all_requires_exactly_one_of_each_canonical_gate(self) -> None:
        from harness.blackbox_v2.gates import _verify_passed_all

        required = (
            "static",
            "input",
            "unit",
            "dry-run",
            "compare",
            "backtest",
            "api-readiness",
        )
        variants = {
            "duplicate": [*required, "static"],
            "extra": [*required, "unexpected"],
            "missing": list(required[:-1]),
            "non_passed": [
                *[(name, "passed") for name in required[:-1]],
                ("api-readiness", "skipped"),
            ],
        }
        for label, variant in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                cfg = self._scaffold(root)
                report_dir = root / "report"
                state_dir = report_dir / "blackbox_v2"
                state_dir.mkdir(parents=True)
                (state_dir / "input_state.json").write_text(
                    json.dumps(
                        {
                            "snapshot_id": "snapshot",
                            "environment_fingerprint": "e" * 64,
                        }
                    ),
                    encoding="utf-8",
                )
                engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE t_harness_runs "
                            "(harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
                            "stage TEXT, status TEXT, finished_at TEXT, report_uri TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE TABLE t_harness_gate_results "
                            "(harness_run_id TEXT, gate_name TEXT, status TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO t_harness_runs VALUES "
                            "('hr_exact', :scheme, :version, 'all', 'passed', "
                            "'2026-07-20T10:00:00', :report_uri)"
                        ),
                        {
                            "scheme": cfg.scheme_id,
                            "version": cfg.scheme_version,
                            "report_uri": str(report_dir),
                        },
                    )
                    rows = [
                        (
                            {"gate_name": item[0], "status": item[1]}
                            if isinstance(item, tuple)
                            else {"gate_name": item, "status": "passed"}
                        )
                        for item in variant
                    ]
                    connection.execute(
                        text(
                            "INSERT INTO t_harness_gate_results VALUES "
                            "('hr_exact', :gate_name, :status)"
                        ),
                        rows,
                    )

                with self.assertRaisesRegex(
                    ValueError,
                    "exact persisted Blackbox V2 gate set",
                ):
                    _verify_passed_all(engine, cfg)

    def test_certification_evidence_selects_exact_latest_harness_backtest_and_live_run(self) -> None:
        from harness.blackbox_v2.api_gate import (
            _read_expected_backtest_evidence,
            _read_expected_live_evidence,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE t_harness_runs (harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, stage TEXT, status TEXT, finished_at TEXT)"))
                connection.execute(text("CREATE TABLE t_backtest_runs (id INTEGER, benchmark_id TEXT, scheme_id TEXT, data_source TEXT, status TEXT, summary TEXT, updated_at TEXT)"))
                connection.execute(text("CREATE TABLE t_scheme_runs (run_id INTEGER, scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, prediction_phase TEXT, status TEXT, data_snapshot_id TEXT, finished_at TEXT)"))
                connection.execute(text("CREATE TABLE t_scheme_predictions (id INTEGER, run_id INTEGER, scheme_id TEXT, target_tenor TEXT, horizon INTEGER, scheme_version TEXT, prediction_phase TEXT, predict_date TEXT, feature_date TEXT, target_date TEXT, extra TEXT)"))
                connection.execute(
                    text("INSERT INTO t_harness_runs VALUES ('hr_a', :scheme, :version, 'all', 'passed', '2026-07-20T10:00:00'), ('hr_z', :scheme, :version, 'all', 'passed', '2026-07-20T10:00:00')"),
                    {"scheme": cfg.scheme_id, "version": cfg.scheme_version},
                )
                for run_id, harness_id, snapshot_id, updated_at in (
                    (100, "hr_a", "snapshot-old", "2026-07-20T09:01:00"),
                    (101, "hr_z", "snapshot-current", "2026-07-20T10:01:00"),
                ):
                    connection.execute(
                        text("INSERT INTO t_backtest_runs VALUES (:id, :benchmark, :scheme, 'blackbox_v2_current_snapshot_as_of', 'success', :summary, :updated_at)"),
                        {
                            "id": run_id,
                            "benchmark": f"bbv2-{cfg.scheme_id}-{harness_id}",
                            "scheme": cfg.scheme_id,
                            "summary": json.dumps(
                                {
                                    "scheme_version": cfg.scheme_version,
                                    "harness_run_id": harness_id,
                                    "data_snapshot_id": snapshot_id,
                                }
                            ),
                            "updated_at": updated_at,
                        },
                    )
                connection.execute(
                    text("INSERT INTO t_scheme_runs VALUES (201, :scheme, :version, 'blackbox_v2', 'scheduled_live', 'success', 'snapshot-live-old', '2026-07-20T10:00:00'), (202, :scheme, :version, 'blackbox_v2', 'scheduled_live', 'success', 'snapshot-live-current', '2026-07-20T11:00:00')"),
                    {"scheme": cfg.scheme_id, "version": cfg.scheme_version},
                )
                connection.execute(
                    text("INSERT INTO t_scheme_predictions VALUES (999, 201, :scheme, '10Y', 1, :version, 'scheduled_live', '2026-07-13', '2026-07-10', '2026-07-17', :old_extra), (301, 202, :scheme, '10Y', 1, :version, 'scheduled_live', '2026-07-20', '2026-07-17', '2026-07-24', :current_extra)"),
                    {
                        "scheme": cfg.scheme_id,
                        "version": cfg.scheme_version,
                        "old_extra": json.dumps(
                            {
                                "request_id": f"{cfg.scheme_id}:2026-07-13:2026-07-10:2026-07-17",
                                "data_snapshot_id": "snapshot-live-old",
                            }
                        ),
                        "current_extra": json.dumps(
                            {
                                "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                                "data_snapshot_id": "snapshot-live-current",
                            }
                        ),
                    },
                )

            backtest = _read_expected_backtest_evidence(engine, cfg)
            live = _read_expected_live_evidence(
                engine,
                cfg,
                target_tenor="10Y",
                horizon=1,
                prediction_phase="scheduled_live",
            )

        self.assertEqual(backtest["run_id"], 101)
        self.assertEqual(backtest["benchmark_id"], f"bbv2-{cfg.scheme_id}-hr_z")
        self.assertEqual(backtest["data_snapshot_id"], "snapshot-current")
        self.assertEqual(live["request_id"], f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24")
        self.assertEqual(live["data_snapshot_id"], "snapshot-live-current")
        self.assertEqual(live["run_id"], 202)

    def test_live_evidence_rejects_run_and_prediction_snapshot_mismatch(self) -> None:
        from harness.blackbox_v2.api_gate import _read_expected_live_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE t_scheme_runs (run_id INTEGER, scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, prediction_phase TEXT, status TEXT, data_snapshot_id TEXT, finished_at TEXT)"))
                connection.execute(text("CREATE TABLE t_scheme_predictions (id INTEGER, run_id INTEGER, scheme_id TEXT, target_tenor TEXT, horizon INTEGER, scheme_version TEXT, prediction_phase TEXT, predict_date TEXT, feature_date TEXT, target_date TEXT, extra TEXT)"))
                connection.execute(
                    text("INSERT INTO t_scheme_runs VALUES (202, :scheme, :version, 'blackbox_v2', 'scheduled_live', 'success', 'snapshot-run', '2026-07-20T11:00:00')"),
                    {"scheme": cfg.scheme_id, "version": cfg.scheme_version},
                )
                connection.execute(
                    text("INSERT INTO t_scheme_predictions VALUES (301, 202, :scheme, '10Y', 1, :version, 'scheduled_live', '2026-07-20', '2026-07-17', '2026-07-24', :extra)"),
                    {
                        "scheme": cfg.scheme_id,
                        "version": cfg.scheme_version,
                        "extra": json.dumps(
                            {
                                "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                                "data_snapshot_id": "snapshot-prediction",
                            }
                        ),
                    },
                )

            with self.assertRaisesRegex(ValueError, "snapshot"):
                _read_expected_live_evidence(
                    engine,
                    cfg,
                    target_tenor="10Y",
                    horizon=1,
                    prediction_phase="scheduled_live",
                )

    def test_metrics_contract_allows_mixed_history_but_requires_exact_current_live_row(self) -> None:
        from harness.blackbox_v2.api_gate import _metrics_contract_errors

        registry_id = "weekly_trial__h1__10Y"
        payload = {
            "scheme_id": registry_id,
            "base_scheme_id": "weekly_trial",
            "target_tenor": "10Y",
            "task_type": "weekly_point",
            "summary": {"metric_samples": 1},
            "monthly_metrics": [{"month": "2026-06", "metric_samples": 1}],
            "daily_rows": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "weekly_trial",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-06-15",
                    "feature_date": "2026-06-12",
                    "target_date": "2026-06-19",
                    "prediction_phase": "gray_live",
                    "run_id": 11,
                    "scheme_version": "old-version",
                    "request_id": "weekly_trial:2026-06-15:2026-06-12:2026-06-19",
                    "data_snapshot_id": "snapshot-old",
                    "predicted_direction": -1,
                    "actual_direction": -1,
                    "is_correct": True,
                },
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "weekly_trial",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-07-20",
                    "feature_date": "2026-07-17",
                    "target_date": "2026-07-24",
                    "prediction_phase": "scheduled_live",
                    "run_id": 22,
                    "scheme_version": "current-version",
                    "request_id": "weekly_trial:2026-07-20:2026-07-17:2026-07-24",
                    "data_snapshot_id": "snapshot-current",
                    "predicted_direction": 1,
                    "actual_direction": None,
                    "is_correct": None,
                },
            ],
        }

        errors, counts = _metrics_contract_errors(
            payload,
            registry_id=registry_id,
            base_scheme_id="weekly_trial",
            target_tenor="10Y",
            task_type="weekly_point",
            horizon=1,
            scheme_version="current-version",
            expected_phase="scheduled_live",
            expected_live={
                "predict_date": "2026-07-20",
                "feature_date": "2026-07-17",
                "target_date": "2026-07-24",
                "request_id": "weekly_trial:2026-07-20:2026-07-17:2026-07-24",
                "data_snapshot_id": "snapshot-current",
                "run_id": 22,
            },
        )

        self.assertEqual(errors, [])
        self.assertEqual(counts["exact_current_live_rows"], 1)
        self.assertEqual(counts["matched_actual_rows"], 1)
        self.assertEqual(counts["pending_actual_rows"], 1)

    def test_metrics_contract_rejects_payload_with_only_old_live_rows(self) -> None:
        from harness.blackbox_v2.api_gate import _metrics_contract_errors

        registry_id = "weekly_trial__h1__10Y"
        payload = {
            "scheme_id": registry_id,
            "base_scheme_id": "weekly_trial",
            "target_tenor": "10Y",
            "task_type": "weekly_point",
            "summary": {"metric_samples": 1},
            "monthly_metrics": [{"month": "2026-06", "metric_samples": 1}],
            "daily_rows": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "weekly_trial",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "predict_date": "2026-06-15",
                    "feature_date": "2026-06-12",
                    "target_date": "2026-06-19",
                    "prediction_phase": "gray_live",
                    "run_id": 11,
                    "scheme_version": "old-version",
                    "request_id": "weekly_trial:2026-06-15:2026-06-12:2026-06-19",
                    "data_snapshot_id": "snapshot-old",
                    "predicted_direction": -1,
                    "actual_direction": -1,
                    "is_correct": True,
                }
            ],
        }

        errors, counts = _metrics_contract_errors(
            payload,
            registry_id=registry_id,
            base_scheme_id="weekly_trial",
            target_tenor="10Y",
            task_type="weekly_point",
            horizon=1,
            scheme_version="current-version",
            expected_phase="scheduled_live",
            expected_live={
                "predict_date": "2026-07-20",
                "feature_date": "2026-07-17",
                "target_date": "2026-07-24",
                "request_id": "weekly_trial:2026-07-20:2026-07-17:2026-07-24",
                "data_snapshot_id": "snapshot-current",
                "run_id": 22,
            },
        )

        self.assertEqual(counts["exact_current_live_rows"], 0)
        self.assertTrue(any("exact current live row" in error for error in errors), errors)

    def test_metrics_contract_rejects_current_provenance_from_wrong_run(self) -> None:
        from harness.blackbox_v2.api_gate import _metrics_contract_errors

        registry_id = "weekly_trial__h1__10Y"
        request_id = "weekly_trial:2026-07-20:2026-07-17:2026-07-24"
        payload = {
            "scheme_id": registry_id,
            "base_scheme_id": "weekly_trial",
            "target_tenor": "10Y",
            "task_type": "weekly_point",
            "summary": {"metric_samples": 1},
            "monthly_metrics": [{"month": "2026-07", "metric_samples": 1}],
            "daily_rows": [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": "weekly_trial",
                    "target_tenor": "10Y",
                    "horizon": 1,
                    "run_id": 21,
                    "predict_date": "2026-07-20",
                    "feature_date": "2026-07-17",
                    "target_date": "2026-07-24",
                    "prediction_phase": "scheduled_live",
                    "scheme_version": "current-version",
                    "request_id": request_id,
                    "data_snapshot_id": "snapshot-current",
                    "predicted_direction": 1,
                    "actual_direction": 1,
                    "is_correct": True,
                }
            ],
        }

        errors, counts = _metrics_contract_errors(
            payload,
            registry_id=registry_id,
            base_scheme_id="weekly_trial",
            target_tenor="10Y",
            task_type="weekly_point",
            horizon=1,
            scheme_version="current-version",
            expected_phase="scheduled_live",
            expected_live={
                "run_id": 22,
                "predict_date": "2026-07-20",
                "feature_date": "2026-07-17",
                "target_date": "2026-07-24",
                "request_id": request_id,
                "data_snapshot_id": "snapshot-current",
            },
        )

        self.assertEqual(counts["exact_current_live_rows"], 0)
        self.assertTrue(any("exact current live row" in error for error in errors), errors)

    def test_backtest_contract_rejects_old_run_impersonating_current_certification(self) -> None:
        from harness.blackbox_v2.api_gate import _backtest_contract_errors

        errors = _backtest_contract_errors(
            {
                "run_id": 99,
                "benchmark_id": "bbv2-weekly_trial-hr_old",
                "base_scheme_id": "weekly_trial",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
                "data_source": "blackbox_v2_current_snapshot_as_of",
                "runtime_type": "blackbox_v2",
                "scheme_version": "old-version",
                "data_snapshot_id": "snapshot-old",
                "harness_run_id": "hr_old",
                "daily_rows": [{"predicted_direction": 1, "actual_direction": 1}],
                "monthly_metrics": [{"metric_samples": 1}],
            },
            base_scheme_id="weekly_trial",
            target_tenor="10Y",
            task_type="weekly_point",
            horizon=1,
            expected_run_id=101,
            expected_benchmark_id="bbv2-weekly_trial-hr_current",
            expected_scheme_version="current-version",
            expected_snapshot_id="snapshot-current",
            expected_harness_run_id="hr_current",
        )

        joined = "\n".join(errors)
        self.assertIn("run_id", joined)
        self.assertIn("benchmark_id", joined)
        self.assertIn("scheme_version", joined)
        self.assertIn("data_snapshot_id", joined)
        self.assertIn("harness_run_id", joined)

    def test_backtest_payload_rejects_duplicate_cards_for_same_registry(self) -> None:
        from harness.blackbox_v2.api_gate import _find_unique_factor_lab_cell

        registry_id = "weekly_trial__h1__10Y"
        row, errors = _find_unique_factor_lab_cell(
            {
                "schemes": [
                    {"scheme_id": registry_id, "run_id": 101},
                    {"scheme_id": registry_id, "run_id": 100},
                ]
            },
            registry_id,
        )

        self.assertIsNone(row)
        self.assertTrue(any("exactly one latest" in error for error in errors), errors)

    def _scaffold(self, root: Path):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = root / "incoming"
        delivery.mkdir()
        (delivery / "weekly_trial.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "weekly_trial.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "weekly_trial",
                    "name": "Weekly Trial",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "weekly_point",
                    "horizon": 1,
                    "target_rule": "target_week_end_yield_vs_feature_week_end_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=root / "schemes")
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("status: paused", "status: active")
            .replace("version_status: draft", "version_status: active"),
            encoding="utf-8",
        )
        return load_scheme_config(config_path)

    def test_registry_dispatches_active_blackbox_to_blackbox_api_gate(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate
        from harness.registry import gate_for_name

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
            )

            gate = gate_for_name("api", ctx=ctx)

        self.assertIsInstance(gate, BlackboxApiGate)

    def test_blackbox_api_gate_requires_registry_schemes_metrics_and_backtest_probes(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_base_url="http://127.0.0.1:18100",
                api_instance_nonce="cert-instance-1",
                prediction_phase="gray_live",
            )
            expected_backtest = {
                "run_id": 301,
                "benchmark_id": f"bbv2-{cfg.scheme_id}-hr_current",
                "scheme_version": cfg.scheme_version,
                "harness_run_id": "hr_current",
                "data_snapshot_id": "snapshot-live-1",
            }
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }
            schemes_row = dict(registry_row)
            schemes_row.pop("runtime_type")
            schemes_payload = {"schemes": [schemes_row]}
            metrics_payload = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "summary": {"total": 1, "metric_samples": 1, "accuracy": 100.0},
                "monthly_metrics": [
                    {"month": "2026-07", "samples": 1, "metric_samples": 1, "accuracy": 100.0}
                ],
                "daily_rows": [
                    {
                        "scheme_id": registry_id,
                        "base_scheme_id": cfg.scheme_id,
                        "target_tenor": "10Y",
                        "horizon": 1,
                        "run_id": 401,
                        "predict_date": "2026-07-20",
                        "feature_date": "2026-07-17",
                        "target_date": "2026-07-24",
                        "prediction_phase": "gray_live",
                        "scheme_version": cfg.scheme_version,
                        "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                        "data_snapshot_id": "snapshot-live-1",
                        "predicted_direction": 1,
                        "actual_direction": 1,
                        "is_correct": True,
                    }
                ],
            }
            backtest_payload = {
                "data_source": "blackbox_v2_current_snapshot_as_of",
                "schemes": [
                    {
                        "scheme_id": registry_id,
                        "base_scheme_id": cfg.scheme_id,
                        "target_tenor": "10Y",
                        "task_type": "weekly_point",
                        "horizon": 1,
                        "data_source": "blackbox_v2_current_snapshot_as_of",
                        "runtime_type": "blackbox_v2",
                        "run_id": 301,
                        "benchmark_id": f"bbv2-{cfg.scheme_id}-hr_current",
                        "scheme_version": cfg.scheme_version,
                        "data_snapshot_id": "snapshot-live-1",
                        "harness_run_id": "hr_current",
                        "monthly_metrics": [
                            {"month": "2026-07", "samples": 1, "metric_samples": 1, "accuracy": 100.0}
                        ],
                        "daily_rows": [
                            {"predicted_direction": 1, "actual_direction": 1, "is_correct": True}
                        ],
                    }
                ],
            }

            def fetch(url: str, timeout_sec: int, max_response_bytes: int):
                if url.endswith("/api/health"):
                    return {"status": "ok", "service_instance": self._INSTANCE}, 200
                if url.endswith("/api/schemes"):
                    return schemes_payload, 200
                if url.endswith(f"/api/metrics/{registry_id}"):
                    return metrics_payload, 200
                if "data_source=blackbox_v2_current_snapshot_as_of" in url:
                    return backtest_payload, 200
                raise AssertionError(f"unexpected URL: {url}")

            with (
                patch.dict(
                    os.environ,
                    {"BOND_FACTOR_LAB_API_BASE_URL": "http://wrong-environment:9999"},
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=registry_row,
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    return_value=expected_backtest,
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value={
                        "run_id": 401,
                        "predict_date": "2026-07-20",
                        "feature_date": "2026-07-17",
                        "target_date": "2026-07-24",
                        "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                        "data_snapshot_id": "snapshot-live-1",
                    },
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value="formal-shared-secret-for-tests",
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
                patch("harness.blackbox_v2.api_gate.fetch_json", side_effect=fetch) as fetch_mock,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(fetch_mock.call_count, 4)
        self.assertTrue(
            all(
                call.args[0].startswith("http://127.0.0.1:18100/")
                for call in fetch_mock.call_args_list
            )
        )
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["target_tenor"], "10Y")
        self.assertEqual(evidence["task_type"], "weekly_point")
        self.assertEqual(evidence["horizon"], 1)
        self.assertTrue(evidence["registry_active"])
        self.assertTrue(evidence["schemes_visible"])
        self.assertTrue(evidence["metrics_visible"])
        self.assertTrue(evidence["backtest_visible"])
        self.assertEqual(evidence["effective_api_base_url"], "http://127.0.0.1:18100")
        self.assertEqual(evidence["expected_service_fingerprint"], "f" * 64)
        self.assertEqual(evidence["actual_service_fingerprint"], "f" * 64)
        self.assertEqual(evidence["live_rows"], 1)
        self.assertEqual(evidence["matched_actual_rows"], 1)
        self.assertEqual(evidence["expected_live_run_id"], 401)

    def test_blackbox_api_gate_fails_closed_when_any_probe_is_missing(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_instance_nonce="cert-instance-1",
                prediction_phase="gray_live",
            )
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }

            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=registry_row,
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    return_value={
                        "run_id": 301,
                        "benchmark_id": f"bbv2-{cfg.scheme_id}-hr_current",
                        "scheme_version": cfg.scheme_version,
                        "harness_run_id": "hr_current",
                        "data_snapshot_id": "snapshot-live-1",
                    },
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value={
                        "run_id": 401,
                        "predict_date": "2026-07-20",
                        "feature_date": "2026-07-17",
                        "target_date": "2026-07-24",
                        "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                        "data_snapshot_id": "snapshot-live-1",
                    },
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value="formal-shared-secret-for-tests",
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=[
                        ({"status": "ok", "service_instance": self._INSTANCE}, 200),
                        ({"schemes": [registry_row]}, 200),
                        ({"scheme_id": registry_id, "daily_rows": [], "monthly_metrics": []}, 200),
                        ({"schemes": []}, 200),
                    ],
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        errors = "\n".join(result.errors).lower()
        self.assertIn("backtest", errors)
        self.assertIn("daily_rows", errors)
        self.assertIn("monthly_metrics", errors)

    def test_blackbox_api_gate_rejects_wrong_service_instance_and_missing_live_contract(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_instance_nonce="cert-instance-1",
                prediction_phase="gray_live",
            )
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }
            wrong_instance = {**self._INSTANCE, "code_commit": "b" * 40}
            metrics = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "target_tenor": "10Y",
                "monthly_metrics": [{"month": "2026-07", "samples": 1, "metric_samples": 1}],
                "daily_rows": [
                    {
                        "target_tenor": "10Y",
                        "horizon": 1,
                        "prediction_phase": "gray_live",
                        "scheme_version": cfg.scheme_version,
                        "request_id": "",
                        "data_snapshot_id": "",
                        "predicted_direction": 1,
                        "actual_direction": None,
                    }
                ],
                "summary": {"total": 1, "metric_samples": 0},
            }
            backtest = {
                "schemes": [
                    {
                        **registry_row,
                        "data_source": "blackbox_v2_current_snapshot_as_of",
                        "monthly_metrics": [{"month": "2026-07", "samples": 1, "metric_samples": 1}],
                        "daily_rows": [{"actual_direction": 1}],
                    }
                ]
            }
            with (
                patch("harness.blackbox_v2.api_gate._read_active_registry", return_value=registry_row),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    return_value={
                        "run_id": 301,
                        "benchmark_id": f"bbv2-{cfg.scheme_id}-hr_current",
                        "scheme_version": cfg.scheme_version,
                        "harness_run_id": "hr_current",
                        "data_snapshot_id": "snapshot-live-1",
                    },
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value={
                        "run_id": 401,
                        "predict_date": "2026-07-20",
                        "feature_date": "2026-07-17",
                        "target_date": "2026-07-24",
                        "request_id": f"{cfg.scheme_id}:2026-07-20:2026-07-17:2026-07-24",
                        "data_snapshot_id": "snapshot-live-1",
                    },
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value="formal-shared-secret-for-tests",
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=[
                        ({"status": "ok", "service_instance": wrong_instance}, 200),
                        ({"schemes": [registry_row]}, 200),
                        (metrics, 200),
                        (backtest, 200),
                    ],
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        errors = "\n".join(result.errors)
        self.assertIn("service instance health payload", errors)
        self.assertIn("request_id", errors)
        self.assertIn("data_snapshot_id", errors)
        self.assertIn("actual", errors)

    def test_blackbox_api_gate_fails_closed_without_authenticated_fingerprint_secret(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_instance_nonce="cert-instance-1",
                prediction_phase="gray_live",
            )
            with (
                patch("harness.blackbox_v2.api_gate.service_fingerprint_secret", return_value=None),
                patch("harness.blackbox_v2.api_gate._read_active_registry", return_value=None),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    side_effect=ValueError("no persisted run"),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    side_effect=ValueError("no live row"),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=[
                        ({"status": "ok", "service_instance": {"fingerprint_version": "2", "fingerprint": None}}, 200),
                        ({"schemes": []}, 200),
                        ({"daily_rows": [], "monthly_metrics": []}, 200),
                        ({"schemes": []}, 200),
                    ],
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("HARNESS_AUTH_SECRET", "\n".join(result.errors))

    def test_fetch_json_rejects_response_larger_than_limit(self) -> None:
        from harness.probes.api_probe import ApiProbeError, fetch_json

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size: int):
                return b"x" * size

        with (
            patch("harness.probes.api_probe.urlopen", return_value=Response()),
            self.assertRaisesRegex(ApiProbeError, "exceeds"),
        ):
            fetch_json("http://127.0.0.1/api/health", max_response_bytes=32)

    def test_fetch_preserves_http_status_and_bounded_error_summary(self) -> None:
        from harness.blackbox_v2.api_gate import _fetch
        from harness.probes.api_probe import ApiProbeError

        with patch(
            "harness.blackbox_v2.api_gate.fetch_json",
            side_effect=ApiProbeError(
                "service unavailable",
                status_code=503,
                error_summary="x" * 900,
            ),
        ):
            payload, status, summary = _fetch(
                "http://127.0.0.1/api/health",
                timeout_sec=1,
            )

        self.assertIsNone(payload)
        self.assertEqual(status, 503)
        self.assertEqual(len(summary or ""), 512)


if __name__ == "__main__":
    unittest.main()
