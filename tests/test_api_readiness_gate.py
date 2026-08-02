from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from harness.context import GateContext
from harness.gates.api_readiness_gate import (
    ApiReadinessGate,
    _fetch_latest_successful_backtest,
)


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
            ) as fetch_json:
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["registry_ids"], ["demo_daily__h1__10Y"])
        self.assertEqual(evidence["latest_backtest_run_id"], 120)
        self.assertEqual(evidence["latest_backtest_benchmark_id"], "demo-benchmark")
        self.assertEqual(evidence["latest_backtest_prediction_count"], 2)
        self.assertEqual(
            fetch_json.call_args_list[0].args[0],
            (
                "http://127.0.0.1:8100/api/backtests/factor-lab"
                "?benchmark_id=demo-benchmark"
            ),
        )
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

    def test_falls_back_to_unfiltered_factor_lab_url_when_backtest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme(project_root)
            engine = _make_engine()
            self.addCleanup(engine.dispose)
            _insert_registry(engine)

            with patch(
                "harness.gates.api_readiness_gate.fetch_json",
                side_effect=[{"schemes": []}, RuntimeError("HTTP Error 404: Not Found")],
            ) as fetch_json:
                result = ApiReadinessGate().run(_ctx(project_root, engine))

        self.assertFalse(result.passed)
        evidence = _evidence_dict(result)
        self.assertIsNone(evidence["latest_backtest_benchmark_id"])
        self.assertEqual(
            fetch_json.call_args_list[0].args[0],
            "http://127.0.0.1:8100/api/backtests/factor-lab",
        )

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

    def test_native_latest_ignores_higher_id_non_default_source(self) -> None:
        engine = _make_engine()
        self.addCleanup(engine.dispose)
        _insert_backtest(
            engine,
            run_id=120,
            benchmark_id="native-default",
            data_source="framework_db_aligned",
            updated_at="2026-07-30 08:00:00",
        )
        _insert_backtest(
            engine,
            run_id=121,
            benchmark_id="native-original",
            data_source="framework_original_csv",
            updated_at="2026-07-30 09:00:00",
        )

        latest = _fetch_latest_successful_backtest(
            engine,
            "demo_daily",
            runtime_type="native_adapter",
        )

        self.assertEqual(latest["run_id"], 120)
        self.assertEqual(latest["benchmark_id"], "native-default")

    def test_latest_default_source_uses_updated_at_before_id(self) -> None:
        engine = _make_engine()
        self.addCleanup(engine.dispose)
        _insert_backtest(
            engine,
            run_id=120,
            benchmark_id="completed-later",
            data_source="framework_db_aligned",
            updated_at="2026-07-30 10:00:00",
        )
        _insert_backtest(
            engine,
            run_id=121,
            benchmark_id="higher-id",
            data_source="framework_db_aligned",
            updated_at="2026-07-30 09:00:00",
        )

        latest = _fetch_latest_successful_backtest(
            engine,
            "demo_daily",
            runtime_type="native_adapter",
        )

        self.assertEqual(latest["run_id"], 120)
        self.assertEqual(latest["benchmark_id"], "completed-later")

    def test_blackbox_latest_uses_blackbox_default_source(self) -> None:
        engine = _make_engine()
        self.addCleanup(engine.dispose)
        _insert_backtest(
            engine,
            run_id=120,
            benchmark_id="blackbox-default",
            data_source="blackbox_v2_current_snapshot_as_of",
            updated_at="2026-07-30 08:00:00",
        )
        _insert_backtest(
            engine,
            run_id=121,
            benchmark_id="blackbox-other",
            data_source="framework_db_aligned",
            updated_at="2026-07-30 09:00:00",
        )

        latest = _fetch_latest_successful_backtest(
            engine,
            "demo_daily",
            runtime_type="blackbox_v2",
        )

        self.assertEqual(latest["run_id"], 120)
        self.assertEqual(latest["benchmark_id"], "blackbox-default")

    def test_latest_default_source_rejects_blank_benchmark_id(self) -> None:
        engine = _make_engine()
        self.addCleanup(engine.dispose)
        _insert_backtest(
            engine,
            run_id=120,
            benchmark_id="   ",
            data_source="framework_db_aligned",
            updated_at="2026-07-30 08:00:00",
        )

        with self.assertRaisesRegex(ValueError, "benchmark_id must be non-empty"):
            _fetch_latest_successful_backtest(
                engine,
                "demo_daily",
                runtime_type="native_adapter",
            )


def _ctx(project_root: Path, engine) -> GateContext:
    return GateContext(
        scheme_id="demo_daily",
        predict_date="2026-06-08",
        project_root=project_root,
        report_dir=project_root / "reports",
        engine_factory=lambda: engine,
    )


def _write_scheme(
    project_root: Path,
    *,
    runtime_type: str = "native_adapter",
) -> None:
    scheme_dir = project_root / "schemes" / "demo_daily"
    scheme_dir.mkdir(parents=True)
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                "scheme_id: demo_daily",
                f"runtime_type: {runtime_type}",
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
                    benchmark_id TEXT,
                    data_source TEXT,
                    updated_at TEXT,
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
    _insert_backtest(
        engine,
        run_id=120,
        benchmark_id="demo-benchmark",
        data_source="framework_db_aligned",
        updated_at="2026-07-30 08:00:00",
        prediction_count=prediction_count,
    )


def _insert_backtest(
    engine,
    *,
    run_id: int,
    benchmark_id: str,
    data_source: str,
    updated_at: str,
    prediction_count: int = 1,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_backtest_runs "
                "(id, scheme_id, benchmark_id, data_source, updated_at, status) "
                "VALUES (:run_id, 'demo_daily', :benchmark_id, :data_source, "
                ":updated_at, 'success')"
            ),
            {
                "run_id": run_id,
                "benchmark_id": benchmark_id,
                "data_source": data_source,
                "updated_at": updated_at,
            },
        )
        for _ in range(prediction_count):
            conn.execute(
                text(
                    "INSERT INTO t_backtest_predictions (run_id, scheme_id) "
                    "VALUES (:run_id, 'demo_daily')"
                ),
                {"run_id": run_id},
            )


if __name__ == "__main__":
    unittest.main()
