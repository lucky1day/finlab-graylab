from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from harness.context import GateContext
from harness.gates.api_readiness_gate import ApiReadinessGate


def _evidence_dict(result) -> dict:
    return {item.key: item.value for item in result.evidence}


class ApiReadinessGateTest(unittest.TestCase):
    def test_passes_for_paused_registry_backtest_and_hidden_public_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine)
            _insert_successful_backtest(engine, prediction_count=2)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[
                    {"schemes": []},
                    RuntimeError("HTTP Error 404: Not Found"),
                ],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["registry_ids"], ["demo_daily__h1__10Y"])
        self.assertEqual(evidence["latest_backtest_run_id"], 120)
        self.assertEqual(evidence["latest_backtest_prediction_count"], 2)
        self.assertFalse(evidence["public_factor_lab_visible"])
        self.assertFalse(evidence["public_metrics_visible"])

    def test_fails_when_registry_row_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_successful_backtest(engine, prediction_count=2)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[{"schemes": []}, RuntimeError("HTTP Error 404: Not Found")],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        self.assertTrue(any("registry row missing" in error for error in result.errors), result.errors)

    def test_fails_when_registry_fields_do_not_match_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine, name="Wrong Name")
            _insert_successful_backtest(engine, prediction_count=2)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[{"schemes": []}, RuntimeError("HTTP Error 404: Not Found")],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        self.assertTrue(any("name mismatch" in error for error in result.errors), result.errors)

    def test_fails_when_latest_backtest_has_no_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine)
            _insert_successful_backtest(engine, prediction_count=0)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[{"schemes": []}, RuntimeError("HTTP Error 404: Not Found")],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        self.assertTrue(any("latest successful backtest has no prediction rows" in error for error in result.errors), result.errors)

    def test_fails_when_paused_scheme_leaks_to_factor_lab_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine)
            _insert_successful_backtest(engine, prediction_count=2)

            payload = {
                "schemes": [
                    {
                        "scheme_id": "demo_daily__h1__10Y",
                        "target_tenor": "10Y",
                        "monthly_metrics": [],
                    }
                ]
            }
            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[payload, RuntimeError("HTTP Error 404: Not Found")],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        self.assertTrue(any("visible in /api/backtests/factor-lab" in error for error in result.errors), result.errors)

    def test_fails_when_paused_scheme_leaks_to_metrics_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine)
            _insert_successful_backtest(engine, prediction_count=2)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[
                    {"schemes": []},
                    {"monthly_metrics": [{"month": "2026-05", "samples": 1}]},
                ],
            ):
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        self.assertTrue(any("visible in /api/metrics" in error for error in result.errors), result.errors)


def _ctx(project_root: Path, engine) -> GateContext:
    return GateContext(
        scheme_id="demo_daily",
        predict_date="2026-06-08",
        project_root=project_root,
        report_dir=project_root / "reports",
        engine_factory=lambda: engine,
    )


def _write_scheme(project_root: Path) -> None:
    scheme_dir = project_root / "schemes" / "demo_daily"
    scheme_dir.mkdir(parents=True)
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                "scheme_id: demo_daily",
                'name: "Demo"',
                'description: "Demo scheme"',
                "horizon: 1",
                "task_type: T+1",
                'tenors: ["10Y"]',
                "frequency: daily",
                "status: paused",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
            ]
        ),
        encoding="utf-8",
    )


def _make_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT,
                    name TEXT,
                    horizon INTEGER,
                    task_type TEXT,
                    tenors TEXT,
                    frequency TEXT,
                    target_tenor TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER PRIMARY KEY,
                    scheme_id TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
    return engine


def _insert_registry(engine, *, name: str = "Demo") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, horizon, task_type, tenors, frequency, target_tenor, status)
                VALUES
                    (:scheme_id, :base_scheme_id, :name, :horizon, :task_type, :tenors, :frequency, :target_tenor, :status)
                """
            ),
            {
                "scheme_id": "demo_daily__h1__10Y",
                "base_scheme_id": "demo_daily",
                "name": name,
                "horizon": 1,
                "task_type": "T+1",
                "tenors": '["10Y"]',
                "frequency": "daily",
                "target_tenor": "10Y",
                "status": "paused",
            },
        )


def _insert_successful_backtest(engine, *, prediction_count: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO t_backtest_runs (id, scheme_id, status) VALUES (120, 'demo_daily', 'success')")
        )
        for _ in range(prediction_count):
            conn.execute(
                text("INSERT INTO t_backtest_predictions (run_id, scheme_id) VALUES (120, 'demo_daily')")
            )


if __name__ == "__main__":
    unittest.main()
