from __future__ import annotations

import ast
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, text

from backtests._base_runner import RunOutput, make_run_output
from scheduler.blackbox_v2_runner import RuntimeProfile
from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest
from shared.blackbox_v2.history import HistoricalCase
from shared.blackbox_v2.snapshot import BlackboxSnapshot
from shared.models import PredictionRecord


def _metadata() -> BlackboxMetadata:
    return BlackboxMetadata(
        schema_version="1.0",
        scheme_id="trial_history",
        name="Trial History",
        algorithm_version="1.0.0",
        target_tenor="10Y",
        task_type="T+1",
        horizon=1,
        target_rule="target_date_yield_vs_feature_date_yield",
        frequency="daily",
    )


def _cases(count: int) -> list[HistoricalCase]:
    start = date(2025, 1, 1)
    result = []
    for index in range(count):
        feature = (start + timedelta(days=index * 2)).isoformat()
        target = (start + timedelta(days=index * 2 + 1)).isoformat()
        result.append(
            HistoricalCase(
                request=BlackboxRequest(
                    request_id=f"trial_history:{feature}:{feature}:{target}",
                    predict_date=feature,
                    feature_date=feature,
                    target_date=target,
                    daily_cutoff_key=feature,
                    weekly_cutoff_key=f"2025{index % 52 + 1:02d}",
                    monthly_cutoff_key=f"2025{index % 12 + 1:02d}",
                ),
                label=1 if index % 2 == 0 else -1,
                actual_extra={
                    "actual_fact_key": ["10Y", target],
                    "replay_semantics": "current_snapshot_as_of_not_historical_vintage",
                },
            )
        )
    return result


def _records(cases: list[HistoricalCase]) -> list[PredictionRecord]:
    return [
        PredictionRecord(
            scheme_id="trial_history",
            target_tenor="10Y",
            horizon=1,
            predict_date=case.request.predict_date,
            feature_date=case.request.feature_date,
            target_date=case.request.target_date,
            predicted_direction=case.label,
            model_version="1.0.0",
            extra={
                "request_id": case.request.request_id,
                "runtime_type": "blackbox_v2",
                "target_rule": "target_date_yield_vs_feature_date_yield",
                "data_snapshot_id": "snapshot-test",
            },
        )
        for case in cases
    ]


def _snapshot(root: Path) -> BlackboxSnapshot:
    data = root / "data"
    data.mkdir(parents=True)
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return BlackboxSnapshot("snapshot-test", root, data, manifest, "data-bridge-v1")


def _output(count: int = 100) -> RunOutput:
    rows = []
    start = date(2025, 1, 1)
    for index in range(count):
        predict_date = (start + timedelta(days=index)).isoformat()
        target_date = (start + timedelta(days=index + 1)).isoformat()
        direction = 1 if index % 2 == 0 else -1
        rows.append(
            {
                "benchmark_id": "bbv2-history",
                "scheme_id": "trial_history",
                "target_tenor": "10Y",
                "horizon": 1,
                "predict_date": predict_date,
                "feature_date": predict_date,
                "target_date": target_date,
                "label": direction,
                "predicted_direction": direction,
                "model_pred": direction,
                "confidence": None,
                "source_row": {"request_id": f"request-{index}"},
                "extra": {"runtime_type": "blackbox_v2"},
            }
        )
    return make_run_output(
        scheme_id="trial_history",
        data_source="blackbox_v2_current_snapshot_as_of",
        start_date=rows[0]["predict_date"],
        end_date=rows[-1]["predict_date"],
        rows=rows,
        benchmark_id="bbv2-history",
    )


def _database():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE t_backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_run_id INTEGER,
                benchmark_id TEXT NOT NULL,
                scheme_id TEXT NOT NULL,
                data_source TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT,
                report_path TEXT,
                code_hash TEXT,
                config_hash TEXT,
                input_artifact_hash TEXT,
                run_mode TEXT,
                updated_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE t_backtest_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                benchmark_id TEXT NOT NULL,
                scheme_id TEXT NOT NULL,
                target_tenor TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                predict_date TEXT NOT NULL,
                feature_date TEXT,
                target_date TEXT,
                label INTEGER,
                predicted_direction INTEGER,
                model_pred INTEGER,
                confidence REAL,
                source_row TEXT,
                extra TEXT,
                UNIQUE(run_id, target_tenor, predict_date)
            )
        """))
        conn.execute(text("""
            CREATE TABLE t_backtest_monthly_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                benchmark_id TEXT NOT NULL,
                scheme_id TEXT NOT NULL,
                target_tenor TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                month TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                correct_count INTEGER NOT NULL,
                accuracy REAL,
                up_precision REAL,
                up_recall REAL,
                down_precision REAL,
                down_recall REAL,
                actual_dist TEXT,
                predicted_dist TEXT,
                UNIQUE(run_id, target_tenor, month)
            )
        """))
    return engine


def _counts(engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return tuple(
            int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in ("t_backtest_runs", "t_backtest_predictions", "t_backtest_monthly_metrics")
        )


class BlackboxV2BacktestConversionTests(unittest.TestCase):
    def test_backtest_converter_has_no_scheduler_dependency(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "backtests" / "blackbox_v2.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertFalse(
            any(module == "scheduler" or module.startswith("scheduler.") for module in imports),
            imports,
        )

    def test_unchanged_delivery_converts_real_cases_to_standard_run_output(self) -> None:
        from backtests.blackbox_v2 import run_blackbox_historical_backtest

        cases = _cases(100)
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir))
            output = run_blackbox_historical_backtest(
                metadata=_metadata(),
                script_path=Path(tmpdir) / "trial_history.py",
                cases=cases,
                snapshot=snapshot,
                scheme_version="version-test",
                generation_id="generation-test",
                benchmark_id="bbv2-history",
                harness_run_id="hr-history-current",
                run_delivery=lambda **_kwargs: _records(cases),
                profile=RuntimeProfile.for_tests(),
            )

        self.assertEqual(len(output.rows), 100)
        self.assertGreater(len(output.monthly_metrics), 0)
        row = output.rows[0]
        self.assertEqual(row["model_pred"], row["predicted_direction"])
        self.assertEqual(row["source_row"]["request_id"], cases[0].request.request_id)
        self.assertEqual(row["extra"]["scheme_version"], "version-test")
        self.assertEqual(row["extra"]["generation_id"], "generation-test")
        self.assertEqual(row["extra"]["data_snapshot_id"], "snapshot-test")
        self.assertEqual(output.summary["scheme_version"], "version-test")
        self.assertEqual(output.summary["data_snapshot_id"], "snapshot-test")
        self.assertEqual(output.summary["harness_run_id"], "hr-history-current")
        self.assertEqual(output.data_source, "blackbox_v2_current_snapshot_as_of")

    def test_batch_sizes_are_conversion_invariant(self) -> None:
        from backtests.blackbox_v2 import run_blackbox_historical_backtest

        for count in (100, 101, 500, 1000):
            cases = _cases(count)
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmpdir:
                snapshot = _snapshot(Path(tmpdir))
                output = run_blackbox_historical_backtest(
                    metadata=_metadata(), script_path=Path(tmpdir) / "delivery.py",
                    cases=cases, snapshot=snapshot, scheme_version="version-test",
                    generation_id="generation-test", benchmark_id=f"benchmark-{count}",
                    harness_run_id=f"hr-{count}",
                    run_delivery=lambda **_kwargs: _records(cases),
                    profile=RuntimeProfile.for_tests(),
                )
            self.assertEqual(len(output.rows), count)
            self.assertEqual([row["request_id"] for row in (item["extra"] for item in output.rows)], [
                case.request.request_id for case in cases
            ])

    def test_converter_forwards_one_budget_and_records_batch_evidence(self) -> None:
        from backtests.blackbox_v2 import run_blackbox_historical_backtest

        cases = _cases(205)
        budget = object()
        observed: dict[str, object] = {}

        def run_delivery(**kwargs):
            observed["budget"] = kwargs.get("budget")
            return _records(cases)

        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir))
            output = run_blackbox_historical_backtest(
                metadata=_metadata(),
                script_path=Path(tmpdir) / "delivery.py",
                cases=cases,
                snapshot=snapshot,
                scheme_version="version-test",
                generation_id="generation-test",
                benchmark_id="benchmark-205",
                harness_run_id="hr-205",
                run_delivery=run_delivery,
                profile=RuntimeProfile.for_tests(max_batch_requests=100),
                budget=budget,
            )

        self.assertIs(observed["budget"], budget)
        self.assertEqual(output.summary["batch_count"], 3)
        self.assertEqual(output.summary["batch_sizes"], [100, 100, 5])
        self.assertEqual(output.summary["max_batch_requests"], 100)

    def test_result_order_echo_and_unique_predict_date_fail_closed(self) -> None:
        from backtests.blackbox_v2 import run_blackbox_historical_backtest

        cases = _cases(2)
        bad_records = list(reversed(_records(cases)))
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = _snapshot(Path(tmpdir))
            with self.assertRaisesRegex(ValueError, "order or echo"):
                run_blackbox_historical_backtest(
                    metadata=_metadata(), script_path=Path(tmpdir) / "delivery.py",
                    cases=cases, snapshot=snapshot, scheme_version="v",
                    generation_id="g", benchmark_id="b",
                    harness_run_id="hr-b",
                    run_delivery=lambda **_kwargs: bad_records,
                    profile=RuntimeProfile.for_tests(),
                )
            duplicate = [cases[0], replace(cases[1], request=replace(
                cases[1].request, predict_date=cases[0].request.predict_date
            ))]
            with self.assertRaisesRegex(ValueError, "duplicate predict_date"):
                run_blackbox_historical_backtest(
                    metadata=_metadata(), script_path=Path(tmpdir) / "delivery.py",
                    cases=duplicate, snapshot=snapshot, scheme_version="v",
                    generation_id="g", benchmark_id="b",
                    harness_run_id="hr-b",
                    run_delivery=lambda **_kwargs: _records(duplicate),
                    profile=RuntimeProfile.for_tests(),
                )


class BlackboxV2AtomicPersistenceTests(unittest.TestCase):
    def test_scope_counts_ignore_concurrent_other_benchmarks(self) -> None:
        from backtests.repository import snapshot_backtest_scope_counts

        engine = _database()
        from backtests.repository import persist_backtest_output_atomic

        persist_backtest_output_atomic(engine, _output(), benchmark_id="bbv2-history")
        other = _output()
        for row in other.rows:
            row["benchmark_id"] = "other-benchmark"
        for metric in other.monthly_metrics:
            metric["benchmark_id"] = "other-benchmark"
        persist_backtest_output_atomic(engine, other, benchmark_id="other-benchmark")

        counts = snapshot_backtest_scope_counts(engine, "bbv2-history")
        self.assertEqual(counts, {
            "t_backtest_runs": 1,
            "t_backtest_predictions": 100,
            "t_backtest_monthly_metrics": len(_output().monthly_metrics),
        })
        engine.dispose()

    def test_atomic_persistence_writes_one_run_all_predictions_and_metrics(self) -> None:
        from backtests.repository import persist_backtest_output_atomic

        engine = _database()
        output = _output()
        run_id = persist_backtest_output_atomic(engine, output, benchmark_id="bbv2-history")
        self.assertGreater(run_id, 0)
        self.assertEqual(_counts(engine), (1, 100, len(output.monthly_metrics)))
        with engine.connect() as conn:
            status = conn.execute(text("SELECT status FROM t_backtest_runs")).scalar_one()
        self.assertEqual(status, "success")
        engine.dispose()

    def test_failure_after_run_insert_rolls_back_every_table(self) -> None:
        from backtests.repository import persist_backtest_output_atomic

        engine = _database()

        def fail_predictions(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "INSERT INTO t_backtest_predictions" in statement:
                raise RuntimeError("injected after run")

        event.listen(engine, "before_cursor_execute", fail_predictions)
        with self.assertRaisesRegex(RuntimeError, "injected after run"):
            persist_backtest_output_atomic(engine, _output(), benchmark_id="bbv2-history")
        self.assertEqual(_counts(engine), (0, 0, 0))
        engine.dispose()

    def test_failure_after_predictions_rolls_back_every_table(self) -> None:
        from backtests.repository import persist_backtest_output_atomic

        engine = _database()

        def fail_metrics(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "INSERT INTO t_backtest_monthly_metrics" in statement:
                raise RuntimeError("injected after predictions")

        event.listen(engine, "before_cursor_execute", fail_metrics)
        with self.assertRaisesRegex(RuntimeError, "injected after predictions"):
            persist_backtest_output_atomic(engine, _output(), benchmark_id="bbv2-history")
        self.assertEqual(_counts(engine), (0, 0, 0))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
