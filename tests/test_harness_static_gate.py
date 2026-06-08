from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class HarnessStaticGateTests(unittest.TestCase):
    def test_existing_schemes_pass_static_gate(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        project_root = Path(__file__).resolve().parents[1]
        schemes = (
            "t1_daily",
            "t5_daily",
            "weekly_10y_d_overlay",
            "weekly_5y_direct_production",
            "weekly_7y_cross_d_overlay",
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

    def test_db_call_rules_do_not_flag_text_suffixes_or_context_calls(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            (scheme_dir / "core" / "model.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "def direction_text(value):",
                        "    return str(value)",
                        "def context(value):",
                        "    return value",
                        "def build():",
                        "    Path('/tmp/example').write_text(direction_text(context(1)))",
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

        self.assertTrue(result.passed, result.errors)

    def test_static_gate_does_not_import_predict_module(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            marker = project_root / "imported.txt"
            (scheme_dir / "predict.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        f"Path({str(marker)!r}).write_text('imported')",
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

        self.assertTrue(result.passed, result.errors)
        self.assertFalse(marker.exists())


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
            with patch("harness.gates.input_gate.build_daily_input_artifact", return_value=artifact) as build:
                result = InputGate().run(
                    GateContext(
                        scheme_id="demo_daily",
                        predict_date="2026-06-03",
                        project_root=project_root,
                        report_dir=project_root / "reports",
                    )
                )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["row_count"], 3)
        self.assertEqual(evidence["date_coverage"], {"field": "date", "start": "2026-06-01", "end": "2026-06-03"})
        self.assertEqual(evidence["missing_required_cols"], [])
        self.assertEqual(build.call_args.kwargs["scheme_id"], "demo_daily")

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
            extra={"input_artifact_path": "/tmp/input.csv", "feature_date": "2026-06-08"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            with patch("harness.gates.dry_run_gate.run_scheme_subprocess", return_value=[record]):
                with patch(
                    "harness.gates.dry_run_gate.snapshot_table_counts",
                    side_effect=[
                        {"t_scheme_predictions": 10, "t_scheme_run_log": 20},
                        {"t_scheme_predictions": 10, "t_scheme_run_log": 20},
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
    def test_live_gate_blocks_without_token_and_does_not_execute(self) -> None:
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

    def test_live_gate_blocks_scheme_mismatch_token(self) -> None:
        from harness.authorization import issue_token
        from harness.context import GateContext
        from harness.gates.live_gate import LiveGate
        from harness.result import GateStatus

        token = issue_token("other_daily", "live_write", "2026-06-08")
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.live_gate.snapshot_table_counts", return_value={"t_scheme_predictions": 10, "t_scheme_run_log": 20}):
                with patch("harness.gates.live_gate.execute_scheme") as execute:
                    result = LiveGate().run(
                        GateContext(
                            scheme_id="demo_daily",
                            predict_date="2026-06-08",
                            project_root=project_root,
                            report_dir=project_root / "reports" / "harness" / "demo_daily",
                            engine_factory=lambda: engine,
                            authorization=token,
                        )
                    )

        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertFalse(execute.called)
        self.assertTrue(any("scheme_id mismatch" in error for error in result.errors), result.errors)

    def test_live_gate_authorized_write_audits_and_consumes_token(self) -> None:
        from harness.authorization import issue_token
        from harness.context import GateContext
        from harness.gates.live_gate import LiveGate
        from harness.result import GateStatus

        token = issue_token("demo_daily", "live_write", "2026-06-08")
        run_result = SimpleNamespace(status="success", records_written=1, error_msg=None)
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            engine = SimpleNamespace(dispose=lambda: None)
            snapshots = [
                {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "api_wind_daily": 30},
                {"t_scheme_predictions": 11, "t_scheme_run_log": 21, "api_wind_daily": 30},
            ]
            scheme_snapshots = [
                {"t_scheme_predictions": 0, "t_scheme_run_log": 0},
                {"t_scheme_predictions": 1, "t_scheme_run_log": 1},
            ]
            with patch("harness.gates.live_gate.snapshot_table_counts", side_effect=snapshots):
                with patch("harness.gates.live_gate.snapshot_scheme_counts", side_effect=scheme_snapshots):
                    with patch("harness.gates.live_gate.execute_scheme", return_value=run_result):
                        first = LiveGate().run(
                            GateContext(
                                scheme_id="demo_daily",
                                predict_date="2026-06-08",
                                project_root=project_root,
                                report_dir=project_root / "reports" / "harness" / "demo_daily",
                                engine_factory=lambda: engine,
                                authorization=token,
                            )
                        )
            audit_path = _evidence_dict(first)["authorization_audit_path"]
            audit_exists = Path(audit_path).exists()

            with patch("harness.gates.live_gate.snapshot_table_counts", return_value={"t_scheme_predictions": 11, "t_scheme_run_log": 21}):
                second = LiveGate().run(
                    GateContext(
                        scheme_id="demo_daily",
                        predict_date="2026-06-08",
                        project_root=project_root,
                        report_dir=project_root / "reports" / "harness" / "demo_daily",
                        engine_factory=lambda: engine,
                        authorization=token,
                    )
                )

        self.assertEqual(first.status, GateStatus.PASSED)
        first_evidence = _evidence_dict(first)
        self.assertEqual(first_evidence["protected_table_deltas"], {"api_wind_daily": 0, "t_scheme_predictions": 1, "t_scheme_run_log": 1})
        self.assertEqual(first_evidence["authorized_scheme_table_deltas"], {"t_scheme_predictions": 1, "t_scheme_run_log": 1})
        self.assertTrue(audit_exists)
        self.assertEqual(second.status, GateStatus.BLOCKED)
        self.assertTrue(any("already used" in error for error in second.errors), second.errors)


def _evidence_keys(result) -> set[str]:
    return {item.key for item in result.evidence}


def _evidence_dict(result) -> dict:
    return {item.key: item.value for item in result.evidence}


def _write_minimal_scheme(project_root: Path, *, scheme_id: str) -> Path:
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
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                'name: "Demo"',
                'description: "Demo scheme"',
                "horizon: 1",
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
        ),
        encoding="utf-8",
    )
    return scheme_dir
