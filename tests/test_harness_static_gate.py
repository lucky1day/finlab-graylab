from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.operation import build_direct_operation, operation_scope_sha256
from scheduler.discovery import load_scheme_config


class HarnessStaticGateTests(unittest.TestCase):
    def test_native_static_gate_records_canonical_business_identity_snapshot(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'tenors: ["10Y"]',
                    'tenors: ["5Y", "10Y"]',
                ),
                encoding="utf-8",
            )

            result = StaticGate().run(
                GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    project_root=project_root,
                    report_dir=project_root / "reports",
                )
            )

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(
            _evidence_dict(result)["native_business_identity"],
            {
                "scheme_id": "demo_daily",
                "runtime_type": "native_adapter",
                "horizon": 1,
                "task_type": "T+1",
                "frequency": "daily",
                "tenors": ["10Y", "5Y"],
                "registry_scheme_ids": [
                    "demo_daily__h1__10Y",
                    "demo_daily__h1__5Y",
                ],
            },
        )

    def test_existing_schemes_pass_static_gate(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        project_root = Path(__file__).resolve().parents[1]
        schemes = (
            "t1_daily",
            "t5_daily",
        )

        for scheme_id in schemes:
            with self.subTest(scheme_id=scheme_id):
                result = StaticGate().run(
                    GateContext(
                        scheme_id=scheme_id,
                        predict_date="2026-06-08",
                        project_root=project_root,
                        report_dir=project_root / "reports" / "harness" / "test",
                    )
                )

                self.assertTrue(result.passed, result.errors)

    def test_core_sqlalchemy_import_fails_with_file_evidence(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            (scheme_dir / "core" / "model.py").write_text("import sqlalchemy\n", encoding="utf-8")

            result = StaticGate().run(
                GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    project_root=project_root,
                    report_dir=project_root / "reports",
                )
            )

        self.assertFalse(result.passed)
        self.assertTrue(any("sqlalchemy" in item for item in result.errors), result.errors)
        self.assertTrue(any("core/model.py" in item for item in result.errors), result.errors)
        self.assertIn("dangerous_core_imports", _evidence_keys(result))

    def test_predict_cross_scheme_import_fails_with_file_evidence(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            predict_path = scheme_dir / "predict.py"
            predict_path.write_text(
                "\n".join(
                    [
                        "from shared.input_artifacts import build_daily_input_artifact",
                        "from schemes.other_daily import predict",
                        'SCHEME_ID = "demo_daily"',
                        "def run(predict_date: str):",
                        "    return []",
                    ]
                ),
                encoding="utf-8",
            )

            result = StaticGate().run(
                GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    project_root=project_root,
                    report_dir=project_root / "reports",
                )
            )

        self.assertFalse(result.passed)
        self.assertTrue(any("other_daily" in item for item in result.errors), result.errors)
        self.assertTrue(any("predict.py" in item for item in result.errors), result.errors)
        self.assertIn("cross_scheme_imports", _evidence_keys(result))






class HarnessRuntimeGateTests(unittest.TestCase):
    def test_input_gate_uses_artifact_metadata_and_required_columns(self) -> None:
        from harness.context import GateContext
        from harness.gates.input_gate import InputGate

        artifact = SimpleNamespace(
            scheme_id="demo_daily",
            frequency="daily",
            path=Path("/tmp/daily_output.csv"),
            dataframe=None,
            source="shared_data_service_daily",
            data_version="shared_data_service_daily.v1",
            row_count=3,
            column_count=2,
            columns=["date", "TB0YWI0C"],
            date_coverage={"field": "date", "start": "2026-06-01", "end": "2026-06-03"},
            quality_flags={"missing_required_columns": []},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.input_gate.get_calendar", return_value=_fake_calendar()):
                with patch("harness.gates.input_gate.build_daily_input_artifact", return_value=artifact) as build:
                    result = InputGate().run(
                        GateContext(
                            scheme_id="demo_daily",
                            predict_date="2026-06-03",
                            project_root=project_root,
                            report_dir=project_root / "reports",
                            engine_factory=lambda: engine,
                        )
                )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["row_count"], 3)
        self.assertEqual(evidence["date_coverage"], {"field": "date", "start": "2026-06-01", "end": "2026-06-03"})
        self.assertEqual(evidence["missing_required_cols"], [])
        self.assertEqual(evidence["auxiliary_input_artifacts"], [])
        self.assertEqual(evidence["feature_date"], "2026-06-02")
        self.assertEqual(build.call_args.kwargs["scheme_id"], "demo_daily")
        self.assertEqual(build.call_args.kwargs["end_date"], "2026-06-02")



    def test_unit_gate_runs_scheme_selector_tests(self) -> None:
        from harness.context import GateContext
        from harness.gates.unit_gate import UnitGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_demo_daily.py").write_text(
                "import unittest\n\nclass DemoDailyTests(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            result = UnitGate().run(
                GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    project_root=project_root,
                    report_dir=project_root / "reports",
                )
            )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["tests_run"], 1)
        self.assertEqual(evidence["test_modules"], ["tests.test_demo_daily"])

    def test_dry_run_gate_fails_when_common_extra_key_missing(self) -> None:
        from harness.context import GateContext
        from harness.gates.dry_run_gate import DryRunGate
        from shared.models import PredictionRecord

        record = PredictionRecord(
            scheme_id="demo_daily",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-06-08",
            target_date="2026-06-09",
            predicted_direction=1,
            feature_date="2026-06-07",
            extra={"input_artifact_path": "/tmp/input.csv", "feature_date": "2026-06-07"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            with patch("harness.gates.dry_run_gate.run_scheme_subprocess", return_value=[record]):
                with patch("harness.gates.dry_run_gate.get_calendar", return_value=_fake_daily_semantics_calendar()):
                    with patch(
                        "harness.gates.dry_run_gate.snapshot_table_counts",
                        side_effect=[
                            {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_scheme_runs": 5},
                            {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_scheme_runs": 5},
                        ],
                    ):
                        result = DryRunGate().run(
                            GateContext(
                                scheme_id="demo_daily",
                                predict_date="2026-06-08",
                                project_root=project_root,
                                report_dir=project_root / "reports",
                            )
                        )

        self.assertFalse(result.passed)
        self.assertTrue(any("input_artifact_source" in error for error in result.errors), result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["predictions_table_delta"], 0)
        self.assertEqual(evidence["run_log_delta"], 0)




    def test_table_guard_diffs_snapshots(self) -> None:
        from harness.probes.table_guard import diff_snapshots

        diff = diff_snapshots(
            {"t_scheme_predictions": 10, "t_scheme_run_log": 5},
            {"t_scheme_predictions": 10, "t_scheme_run_log": 7},
        )

        self.assertEqual(diff, {"t_scheme_predictions": 0, "t_scheme_run_log": 2})



class HarnessLiveGateTests(unittest.TestCase):
    def test_live_gate_blocks_without_operation_and_does_not_execute(self) -> None:
        from harness.context import GateContext
        from harness.gates.live_gate import LiveGate
        from harness.result import GateStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            engine = SimpleNamespace(dispose=lambda: None)
            with patch(
                "harness.gates.live_gate.snapshot_table_counts",
                side_effect=[
                    {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "api_wind_daily": 30},
                    {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "api_wind_daily": 30},
                ],
            ):
                with patch("harness.gates.live_gate.execute_scheme") as execute:
                    result = LiveGate().run(
                        GateContext(
                            scheme_id="demo_daily",
                            predict_date="2026-06-08",
                            project_root=project_root,
                            report_dir=project_root / "reports" / "harness" / "demo_daily",
                            engine_factory=lambda: engine,
                        )
                    )

        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertFalse(result.passed)
        self.assertFalse(execute.called)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["protected_table_deltas"], {"api_wind_daily": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0})


    def test_live_gate_records_direct_operation_evidence(self) -> None:
        from harness.context import GateContext
        from harness.gates.live_gate import LiveGate
        from harness.result import GateStatus

        run_result = SimpleNamespace(status="success", records_written=1, error_msg=None)
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            config_path = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            operation = build_direct_operation(
                "demo_daily",
                "live_write",
                "2026-06-08",
                scheme_version=load_scheme_config(config_path / "config.yaml").scheme_version,
                issued_by="test-operator",
            )
            engine = SimpleNamespace(dispose=lambda: None)
            snapshots = [
                {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_scheme_runs": 5, "api_wind_daily": 30},
                {"t_scheme_predictions": 11, "t_scheme_run_log": 21, "t_scheme_runs": 6, "api_wind_daily": 30},
            ]
            scheme_snapshots = [
                {"t_scheme_predictions": 0, "t_scheme_run_log": 0, "t_scheme_runs": 0},
                {"t_scheme_predictions": 1, "t_scheme_run_log": 1, "t_scheme_runs": 1},
            ]
            with patch("harness.gates.live_gate.snapshot_table_counts", side_effect=snapshots):
                with patch("harness.gates.live_gate.snapshot_scheme_counts", side_effect=scheme_snapshots):
                    with patch("harness.gates.live_gate.execute_scheme", return_value=run_result) as execute:
                        first = LiveGate().run(
                            GateContext(
                                scheme_id="demo_daily",
                                predict_date="2026-06-08",
                                project_root=project_root,
                                report_dir=project_root / "reports" / "harness" / "demo_daily",
                                engine_factory=lambda: engine,
                                operation=operation,
                                prediction_phase="gray_live",
                            )
                        )
        self.assertEqual(first.status, GateStatus.PASSED)
        self.assertEqual(execute.call_args.kwargs["prediction_phase"], "gray_live")
        first_evidence = _evidence_dict(first)
        self.assertEqual(first_evidence["prediction_phase"], "gray_live")
        self.assertEqual(
            first_evidence["protected_table_deltas"],
            {"api_wind_daily": 0, "t_scheme_predictions": 1, "t_scheme_run_log": 1, "t_scheme_runs": 1},
        )
        self.assertEqual(
            first_evidence["operation_scheme_table_deltas"],
            {"t_scheme_predictions": 1, "t_scheme_run_log": 1, "t_scheme_runs": 1},
        )
        self.assertEqual(first_evidence["operator"], "test-operator")
        self.assertEqual(
            first_evidence["operation_scope_sha256"],
            operation_scope_sha256(operation),
        )



class HarnessBacktestApiOrchestratorTests(unittest.TestCase):
    def test_backtest_gate_records_success_without_business_writes(self) -> None:
        from harness.context import GateContext
        from harness.gates.backtest_gate import BacktestGate

        current = {
            "status": "success",
            "scheme_id": "demo_daily",
            "row_count": 2,
            "monthly_count": 1,
            "summary": {"by_tenor": {"10Y": {"samples": 2, "correct": 1}}},
            "elapsed_sec": 99.0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            config_path = project_root / "schemes" / "demo_daily" / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + "\nbacktest:\n"
                + "  runner: backtests.demo_daily_reproduction\n",
                encoding="utf-8",
            )
            engine = SimpleNamespace(dispose=lambda: None)
            snapshots = [
                {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_backtest_runs": 3},
                {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_backtest_runs": 3},
            ]
            with patch("harness.gates.backtest_gate.snapshot_table_counts", side_effect=snapshots):
                with patch("harness.gates.backtest_gate.run_backtest_no_persist", return_value=current):
                    result = BacktestGate().run(
                        GateContext(
                            scheme_id="demo_daily",
                            predict_date="2026-06-08",
                            project_root=project_root,
                            report_dir=project_root / "reports" / "harness" / "demo_daily",
                            engine_factory=lambda: engine,
                        )
                    )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["row_count"], 2)
        self.assertEqual(evidence["monthly_count"], 1)
        self.assertEqual(evidence["protected_table_deltas"], {"t_backtest_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0})

    def test_orchestrator_fail_fast_stops_after_first_failure(self) -> None:
        from harness.context import GateContext
        from harness.orchestrator import onboard
        from harness.result import Evidence, GateResult, GateStatus

        calls: list[str] = []

        class FakeGate:
            def __init__(self, name: str, status: GateStatus) -> None:
                self.name = name
                self.status = status

            def run(self, ctx: GateContext) -> GateResult:
                calls.append(self.name)
                return GateResult(
                    gate_name=self.name,
                    status=self.status,
                    passed=self.status == GateStatus.PASSED,
                    evidence=[Evidence("called", self.name)],
                    errors=[] if self.status == GateStatus.PASSED else ["boom"],
                    started_at="2026-06-08T00:00:00+00:00",
                    finished_at="2026-06-08T00:00:01+00:00",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=project_root,
                report_dir=project_root / "reports" / "harness" / "demo_daily",
            )
            with (
                patch(
                    "harness.orchestrator.persist_harness_run_start",
                    return_value=True,
                ),
                patch(
                    "harness.orchestrator.persist_harness_gate_result",
                    return_value=True,
                ),
                patch(
                    "harness.orchestrator.persist_harness_run_finish",
                    return_value=True,
                ),
            ):
                report = onboard(
                    ctx,
                    stage="all",
                    gates=[
                        FakeGate("static", GateStatus.PASSED),
                        FakeGate("input", GateStatus.FAILED),
                        FakeGate("unit", GateStatus.PASSED),
                    ],
                )

        self.assertFalse(report.overall_passed)
        self.assertEqual(calls, ["static", "input"])
        self.assertEqual([item.gate_name for item in report.results], ["static", "input"])


def _evidence_keys(result) -> set[str]:
    return {item.key for item in result.evidence}


def _evidence_dict(result) -> dict:
    return {item.key: item.value for item in result.evidence}


def _fake_calendar() -> SimpleNamespace:
    return SimpleNamespace(
        previous_trading_day=lambda _predict_date: "2026-06-02",
        week_id_for_date=lambda _feature_date: 202622,
    )


def _fake_daily_semantics_calendar() -> SimpleNamespace:
    return SimpleNamespace(
        previous_trading_day=lambda _predict_date: "2026-06-07",
        nth_trading_day_after=lambda _feature_date, _horizon: "2026-06-09",
    )


def _write_minimal_scheme(project_root: Path, *, scheme_id: str, extra_config_lines: list[str] | None = None) -> Path:
    _write_policy(project_root, scheme_id)
    scheme_dir = project_root / "schemes" / scheme_id
    (scheme_dir / "core").mkdir(parents=True)
    (scheme_dir / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "core" / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "core" / "model.py").write_text("def fit(df):\n    return df\n", encoding="utf-8")
    (scheme_dir / "predict.py").write_text(
        "\n".join(
            [
                "from shared.input_artifacts import build_daily_input_artifact",
                f'SCHEME_ID = "{scheme_id}"',
                "def run(predict_date: str):",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )
    config_lines = [
        f"scheme_id: {scheme_id}",
        'name: "Demo"',
        'description: "Demo scheme"',
        "horizon: 1",
        "task_type: T+1",
        'tenors: ["10Y"]',
        "frequency: daily",
        "schedule:",
        '  cron: "25 9 * * 1-5"',
        '  timezone: "Asia/Shanghai"',
        "entry_point: predict.run",
        "status: paused",
        "input_spec:",
        "  data_version: shared_data_service_daily.v1",
        '  required_columns: ["date", "TB0YWI0C"]',
    ]
    if extra_config_lines:
        config_lines.extend(extra_config_lines)
    (scheme_dir / "config.yaml").write_text("\n".join(config_lines), encoding="utf-8")
    return scheme_dir


def _write_policy(project_root: Path, scheme_id: str) -> None:
    path = project_root / "deploy" / "onboarding_policy_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "policy_version": "1.0",
                "new_scheme_runtime_type": "blackbox_v2",
                "native_v1_mode": "maintenance_only",
                "legacy_native_scheme_ids": [scheme_id],
            }
        ),
        encoding="utf-8",
    )
