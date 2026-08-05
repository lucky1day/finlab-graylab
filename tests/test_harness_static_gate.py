from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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

    def test_parent_relative_cross_scheme_import_fails_with_resolved_module(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root, scheme_id="demo_daily")
            (scheme_dir / "predict.py").write_text(
                "\n".join(
                    [
                        "from shared.input_artifacts import build_daily_input_artifact",
                        "from .core import model",
                        "from ..other_daily.core import model as other_model",
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
        self.assertTrue(
            any(
                "cross-scheme import: schemes.other_daily.core" in item
                for item in result.errors
            ),
            result.errors,
        )
        self.assertFalse(
            any("cross-scheme import: schemes.demo_daily.core" in item for item in result.errors),
            result.errors,
        )
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
                        "    return direction_text(context(1))",
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

    def test_active_backtest_runner_cannot_use_root_benchmarks_as_runtime_input(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'CANONICAL_CSV = Path("benchmarks/demo/daily_output.csv")',
                        "def main():",
                        "    build_daily_input_artifact",
                        "    return CANONICAL_CSV",
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
        self.assertTrue(any("root benchmarks path is source-evidence only" in item for item in result.errors), result.errors)
        self.assertTrue(any("backtests/demo_daily_reproduction.py" in item for item in result.errors), result.errors)

    def test_active_backtest_runner_allows_explicit_source_evidence_archive_path(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'SOURCE_EVIDENCE_CSV = Path("benchmarks/demo/daily_output.csv")',
                        "def build_default_input():",
                        "    return build_daily_input_artifact",
                        "def read_source_evidence():",
                        "    return SOURCE_EVIDENCE_CSV",
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

    def test_active_backtest_runner_cannot_use_source_evidence_as_runtime_input(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'CANONICAL_CSV = Path("source_evidence/benchmark_batches/demo/daily_output.csv")',
                        "def main():",
                        "    build_daily_input_artifact",
                        "    return CANONICAL_CSV",
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
        self.assertTrue(any("source_evidence path is source-evidence only" in item for item in result.errors), result.errors)

    def test_active_backtest_runner_allows_explicit_source_evidence_named_path(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'SOURCE_EVIDENCE_CSV = Path("source_evidence/benchmark_batches/demo/daily_output.csv")',
                        "def build_default_input():",
                        "    return build_daily_input_artifact",
                        "def read_source_evidence():",
                        "    return SOURCE_EVIDENCE_CSV",
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

    def test_active_backtest_runner_cannot_use_source_evidence_helper_as_runtime_input(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from shared.artifact_paths import benchmark_source_evidence_root",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'CANONICAL_CSV = benchmark_source_evidence_root("demo") / "daily_output.csv"',
                        "def main():",
                        "    build_daily_input_artifact",
                        "    return CANONICAL_CSV",
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
        self.assertTrue(any("source_evidence path is source-evidence only" in item for item in result.errors), result.errors)

    def test_active_backtest_runner_allows_source_evidence_helper_with_explicit_name(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from shared.artifact_paths import benchmark_source_evidence_root",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'SOURCE_EVIDENCE_CSV = benchmark_source_evidence_root("demo") / "daily_output.csv"',
                        "def build_default_input():",
                        "    return build_daily_input_artifact",
                        "def read_source_evidence():",
                        "    return SOURCE_EVIDENCE_CSV",
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

    def test_active_backtest_runner_cannot_return_source_evidence_variable_from_default_path(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "status: active",
                    "backtest:",
                    "  runner: backtests.demo_daily_reproduction",
                    "  benchmark_required: true",
                    "  benchmark_id: demo_benchmark",
                    "  data_source: framework_db_aligned",
                    '  start_date: "2025-01-01"',
                ],
            )
            runner_path = project_root / "backtests" / "demo_daily_reproduction.py"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from shared.input_artifacts import build_daily_input_artifact",
                        'SOURCE_EVIDENCE_CSV = Path("source_evidence/benchmark_batches/demo/daily_output.csv")',
                        "def main():",
                        "    build_daily_input_artifact",
                        "    return SOURCE_EVIDENCE_CSV",
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
        self.assertTrue(any("source_evidence path is source-evidence only" in item for item in result.errors), result.errors)


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

    def test_input_gate_builds_auxiliary_inputs_with_evidence(self) -> None:
        from harness.context import GateContext
        from harness.gates.input_gate import InputGate

        primary = SimpleNamespace(
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
        weekly = SimpleNamespace(
            scheme_id="demo_daily",
            frequency="weekly",
            path=Path("/tmp/weekly_output.csv"),
            dataframe=None,
            source="shared_data_service_weekly",
            data_version="shared_data_service_weekly.v1",
            row_count=2,
            column_count=2,
            columns=["week_id", "TB0YWI3C"],
            date_coverage={"field": "week_id", "start": 202621, "end": 202622},
            quality_flags={"missing_required_columns": []},
        )
        monthly = SimpleNamespace(
            scheme_id="demo_daily",
            frequency="monthly",
            path=Path("/tmp/monthly_output.csv"),
            dataframe=None,
            source="shared_data_service_monthly",
            data_version="shared_data_service_monthly.v1",
            row_count=2,
            column_count=2,
            columns=["month_id", "M0000001"],
            date_coverage={"field": "month_id", "start": "202504", "end": "202505"},
            quality_flags={"missing_required_columns": []},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "  auxiliary_inputs:",
                    "    - frequency: weekly",
                    "      data_version: shared_data_service_weekly.v1",
                    '      required_columns: ["week_id", "TB0YWI3C"]',
                    "    - frequency: monthly",
                    "      data_version: shared_data_service_monthly.v1",
                    '      required_columns: ["month_id", "M0000001"]',
                ],
            )
            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.input_gate.get_calendar", return_value=_fake_calendar()):
                with patch("harness.gates.input_gate.build_daily_input_artifact", return_value=primary):
                    with patch("harness.gates.input_gate.build_weekly_input_artifact", return_value=weekly) as build_weekly:
                        with patch(
                            "harness.gates.input_gate.build_monthly_input_artifact",
                            return_value=monthly,
                        ) as build_monthly:
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
        self.assertEqual(len(evidence["auxiliary_input_artifacts"]), 2)
        self.assertEqual(evidence["auxiliary_input_artifacts"][0]["frequency"], "weekly")
        self.assertEqual(evidence["auxiliary_input_artifacts"][1]["frequency"], "monthly")
        self.assertEqual(build_weekly.call_args.kwargs["end_week"], 202622)
        self.assertEqual(build_weekly.call_args.kwargs["as_of_date"], "2026-06-02")
        expected_start = (datetime.strptime("2026-06-02", "%Y-%m-%d") - timedelta(days=8 * 365)).strftime("%Y-%m-%d")
        self.assertEqual(build_monthly.call_args.kwargs["start_date"], expected_start)
        self.assertEqual(build_monthly.call_args.kwargs["end_date"], "2026-06-02")

    def test_input_gate_merges_all_auxiliary_validation_failures(self) -> None:
        from harness.context import GateContext
        from harness.gates.input_gate import InputGate

        primary = SimpleNamespace(
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
        weekly = SimpleNamespace(
            scheme_id="demo_daily",
            frequency="weekly",
            path=Path("/tmp/weekly_output.csv"),
            dataframe=None,
            source="shared_data_service_weekly",
            data_version="wrong_weekly.v1",
            row_count=2,
            column_count=2,
            columns=["week_id", "TB0YWI3C"],
            date_coverage={"field": "week_id", "start": 202621, "end": 202622},
            quality_flags={"missing_required_columns": []},
        )
        monthly = SimpleNamespace(
            scheme_id="demo_daily",
            frequency="monthly",
            path=Path("/tmp/monthly_output.csv"),
            dataframe=None,
            source="shared_data_service_monthly",
            data_version="shared_data_service_monthly.v1",
            row_count=2,
            column_count=1,
            columns=["month_id"],
            date_coverage={"field": "month_id", "start": "202504", "end": "202505"},
            quality_flags={"missing_required_columns": []},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
                extra_config_lines=[
                    "  auxiliary_inputs:",
                    "    - frequency: weekly",
                    "      data_version: shared_data_service_weekly.v1",
                    '      required_columns: ["week_id", "TB0YWI3C"]',
                    "    - frequency: monthly",
                    "      data_version: shared_data_service_monthly.v1",
                    '      required_columns: ["month_id", "M0000001"]',
                ],
            )
            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.input_gate.get_calendar", return_value=_fake_calendar()):
                with patch("harness.gates.input_gate.build_daily_input_artifact", return_value=primary):
                    with patch("harness.gates.input_gate.build_weekly_input_artifact", return_value=weekly):
                        with patch("harness.gates.input_gate.build_monthly_input_artifact", return_value=monthly):
                            result = InputGate().run(
                                GateContext(
                                    scheme_id="demo_daily",
                                    predict_date="2026-06-03",
                                    project_root=project_root,
                                    report_dir=project_root / "reports",
                                    engine_factory=lambda: engine,
                                )
                            )

        self.assertFalse(result.passed)
        joined = "\n".join(result.errors)
        self.assertIn(
            "auxiliary input (weekly) input artifact data_version mismatch: "
            "expected shared_data_service_weekly.v1, got wrong_weekly.v1",
            joined,
        )
        self.assertIn("auxiliary input (monthly) missing required input columns: ['M0000001']", joined)

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

    def test_unit_selector_excludes_retired_daily_control_tests_for_native_preactivation(self) -> None:
        from harness.gates.unit_gate import _select_test_modules

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            for name in (
                "test_t1_daily_shap_retirement",
                "test_daily_policy",
                "test_daily_runtime",
            ):
                (tests_dir / f"{name}.py").write_text(
                    "# t1_daily\n",
                    encoding="utf-8",
                )

            active = _select_test_modules(
                project_root,
                "t1_daily",
                native_preactivation=False,
            )
            preactivation = _select_test_modules(
                project_root,
                "t1_daily",
                native_preactivation=True,
            )

        self.assertEqual(
            active,
            [
                "tests.test_daily_policy",
                "tests.test_daily_runtime",
                "tests.test_t1_daily_shap_retirement",
            ],
        )
        self.assertEqual(
            preactivation,
            ["tests.test_t1_daily_shap_retirement"],
        )

    def test_unit_gate_keeps_retired_daily_control_tests_for_other_paused_native(self) -> None:
        from harness.context import GateContext
        from harness.gates.unit_gate import UnitGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            for name in (
                "test_daily_policy",
                "test_daily_runtime",
            ):
                (tests_dir / f"{name}.py").write_text(
                    "# other_native\n",
                    encoding="utf-8",
                )
            (tests_dir / "test_other_native.py").write_text(
                "import unittest\n\n"
                "class OtherNativeTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            result = UnitGate().run(
                GateContext(
                    scheme_id="other_native",
                    predict_date="2026-06-08",
                    project_root=project_root,
                    report_dir=project_root / "reports",
                    config=SimpleNamespace(
                        runtime_type="native_adapter",
                        status="paused",
                    ),
                )
            )

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(
            _evidence_dict(result)["test_modules"],
            [
                "tests.test_daily_policy",
                "tests.test_daily_runtime",
                "tests.test_other_native",
            ],
        )

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

    def test_dry_run_gate_fails_invalid_prediction_dates(self) -> None:
        from harness.context import GateContext
        from harness.gates.dry_run_gate import DryRunGate
        from shared.models import PredictionRecord

        record = PredictionRecord(
            scheme_id="demo_daily",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-06-08",
            feature_date="2026-06-08",
            target_date="2026-06-09",
            predicted_direction=1,
            extra={
                "input_artifact_path": "/tmp/input.csv",
                "input_artifact_source": "shared_data_service_daily",
                "feature_date": "2026-06-08",
            },
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
        self.assertTrue(any("feature_date must be before predict_date" in error for error in result.errors), result.errors)

    def test_dry_run_gate_fails_daily_target_date_not_matching_horizon(self) -> None:
        from harness.context import GateContext
        from harness.gates.dry_run_gate import DryRunGate
        from shared.models import PredictionRecord

        record = PredictionRecord(
            scheme_id="demo_daily",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-06-08",
            feature_date="2026-06-07",
            target_date="2026-06-10",
            predicted_direction=1,
            extra={
                "input_artifact_path": "/tmp/input.csv",
                "input_artifact_source": "shared_data_service_daily",
                "feature_date": "2026-06-07",
            },
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
        self.assertTrue(any("target_date expected 2026-06-09" in error for error in result.errors), result.errors)

    def test_dry_run_gate_fails_monthly_required_extra_missing(self) -> None:
        from harness.context import GateContext
        from harness.gates.dry_run_gate import DryRunGate
        from shared.models import PredictionRecord
        from shared.prediction_context import MONTHLY_TARGET_RULE

        record = PredictionRecord(
            scheme_id="demo_monthly",
            target_tenor="10Y",
            horizon=30,
            predict_date="2026-04-15",
            feature_date="2026-04-15",
            target_date="2026-05-15",
            predicted_direction=1,
            extra={
                "input_artifact_path": "/tmp/monthly.csv",
                "input_artifact_source": "shared_data_service_monthly",
                "feature_date": "2026-04-15",
                "target_date": "2026-05-15",
                "feature_month_id": "2026-04",
                "target_rule": MONTHLY_TARGET_RULE,
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_monthly_scheme(project_root, scheme_id="demo_monthly")
            with patch("harness.gates.dry_run_gate.run_scheme_subprocess", return_value=[record]):
                with patch("harness.gates.dry_run_gate.get_calendar", return_value=_fake_monthly_semantics_calendar()):
                    with patch(
                        "harness.gates.dry_run_gate.snapshot_table_counts",
                        side_effect=[
                            {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_scheme_runs": 5},
                            {"t_scheme_predictions": 10, "t_scheme_run_log": 20, "t_scheme_runs": 5},
                        ],
                    ):
                        result = DryRunGate().run(
                            GateContext(
                                scheme_id="demo_monthly",
                                predict_date="2026-04-15",
                                project_root=project_root,
                                report_dir=project_root / "reports",
                            )
                        )

        self.assertFalse(result.passed)
        self.assertTrue(any("target_month_id" in error for error in result.errors), result.errors)

    def test_table_guard_diffs_snapshots(self) -> None:
        from harness.probes.table_guard import diff_snapshots

        diff = diff_snapshots(
            {"t_scheme_predictions": 10, "t_scheme_run_log": 5},
            {"t_scheme_predictions": 10, "t_scheme_run_log": 7},
        )

        self.assertEqual(diff, {"t_scheme_predictions": 0, "t_scheme_run_log": 2})

    def test_table_guard_tracks_scheme_runs_for_live_and_no_persist_boundaries(self) -> None:
        from harness.probes import table_guard

        self.assertIn("t_scheme_runs", table_guard.DRY_RUN_GUARD_TABLES)
        self.assertIn("t_scheme_runs", table_guard.PROTECTED_TABLES)
        self.assertIn("t_scheme_monthly_actuals", table_guard.DRY_RUN_GUARD_TABLES)
        self.assertIn("t_scheme_monthly_actuals", table_guard.PROTECTED_TABLES)
        self.assertIn("t_scheme_runs", table_guard.LIVE_WRITE_ALLOWED_TABLES)
        for source_table in (
            "api_wind_date",
            "api_wind_daily",
            "api_wind_derivative_daily",
            "api_wind_weekly",
            "api_wind_derivative_weekly",
            "api_wind_monthly",
            "api_wind_derivative_monthly",
        ):
            with self.subTest(source_table=source_table):
                self.assertIn(source_table, table_guard.DRY_RUN_GUARD_TABLES)
                self.assertIn(source_table, table_guard.PROTECTED_TABLES)


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
                                authorization=token,
                                prediction_phase="gray_live",
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
        self.assertEqual(execute.call_args.kwargs["prediction_phase"], "gray_live")
        first_evidence = _evidence_dict(first)
        self.assertEqual(first_evidence["prediction_phase"], "gray_live")
        self.assertEqual(
            first_evidence["protected_table_deltas"],
            {"api_wind_daily": 0, "t_scheme_predictions": 1, "t_scheme_run_log": 1, "t_scheme_runs": 1},
        )
        self.assertEqual(
            first_evidence["authorized_scheme_table_deltas"],
            {"t_scheme_predictions": 1, "t_scheme_run_log": 1, "t_scheme_runs": 1},
        )
        self.assertTrue(audit_exists)
        self.assertEqual(second.status, GateStatus.BLOCKED)
        self.assertTrue(any("already used" in error for error in second.errors), second.errors)

    def test_live_gate_requires_explicit_prediction_phase(self) -> None:
        from harness.authorization import issue_token
        from harness.context import GateContext
        from harness.gates.live_gate import LiveGate
        from harness.result import GateStatus

        token = issue_token("demo_daily", "live_write", "2026-06-08")
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.live_gate.snapshot_table_counts", return_value={"t_scheme_predictions": 10, "t_scheme_run_log": 20}):
                with patch("harness.gates.live_gate.snapshot_scheme_counts", return_value={"t_scheme_predictions": 0, "t_scheme_run_log": 0}):
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
        self.assertTrue(any("prediction_phase" in error for error in result.errors), result.errors)


class HarnessBacktestApiOrchestratorTests(unittest.TestCase):
    def test_backtest_gate_compares_baseline_ignoring_elapsed_sec(self) -> None:
        from harness.context import GateContext
        from harness.gates.backtest_gate import BacktestGate

        baseline = {
            "status": "success",
            "scheme_id": "demo_daily",
            "row_count": 2,
            "monthly_count": 1,
            "summary": {"by_tenor": {"10Y": {"samples": 2, "correct": 1}}},
            "elapsed_sec": 1.0,
        }
        current = dict(baseline)
        current["elapsed_sec"] = 99.0
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
            baseline_dir = project_root / "reports" / "refactor_baseline" / "demo_daily"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "backtest_no_persist.json").write_text(
                json.dumps(baseline),
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
        self.assertEqual(evidence["diff_count"], 0)
        self.assertEqual(evidence["row_count"], 2)
        self.assertEqual(evidence["monthly_count"], 1)
        self.assertEqual(evidence["protected_table_deltas"], {"t_backtest_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0})

    def test_api_gate_passes_when_factor_lab_cell_exists(self) -> None:
        from harness.context import GateContext
        from harness.gates.api_gate import ApiGate

        payload = {
            "schemes": [
                {
                    "scheme_id": "demo_daily__h1__10Y",
                    "target_tenor": "10Y",
                    "monthly_metrics": [{"month": "2026-05", "samples": 2}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            with patch("harness.gates.api_gate.fetch_json", return_value=payload):
                result = ApiGate().run(
                    GateContext(
                        scheme_id="demo_daily",
                        predict_date="2026-06-08",
                        project_root=project_root,
                        report_dir=project_root / "reports" / "harness" / "demo_daily",
                    )
                )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertTrue(evidence["matrix_cell_present"])
        self.assertEqual(evidence["matched_tenor"], "10Y")

    def test_registry_all_excludes_live_and_orders_auto_gates(self) -> None:
        from harness.registry import sequence_for_stage

        self.assertEqual(
            sequence_for_stage("all"),
            ["static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness"],
        )
        self.assertNotIn("api", sequence_for_stage("all"))
        self.assertNotIn("live", sequence_for_stage("all"))

    def test_activation_history_requires_api_readiness_not_active_api(self) -> None:
        from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES

        self.assertIn("api-readiness", REQUIRED_ACTIVATE_GATES)
        self.assertNotIn("api", REQUIRED_ACTIVATE_GATES)

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

    def test_cli_exit_codes_for_pass_fail_and_blocked(self) -> None:
        from harness.cli import main
        from harness.result import GateResult, GateStatus, OnboardReport

        def make_result(status: GateStatus) -> GateResult:
            return GateResult(
                gate_name="static",
                status=status,
                passed=status == GateStatus.PASSED,
                evidence=[],
                errors=[] if status == GateStatus.PASSED else ["stop"],
                started_at="2026-06-08T00:00:00+00:00",
                finished_at="2026-06-08T00:00:01+00:00",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir) / "reports"
            pass_report = OnboardReport(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                stage_requested="all",
                results=[make_result(GateStatus.PASSED)],
                overall_passed=True,
                report_dir=report_dir,
            )
            fail_report = OnboardReport(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                stage_requested="all",
                results=[make_result(GateStatus.FAILED)],
                overall_passed=False,
                report_dir=report_dir,
            )
            output = io.StringIO()
            with patch("harness.cli.run_onboard", return_value=pass_report):
                with contextlib.redirect_stdout(output):
                    pass_code = main(["onboard", "demo_daily", "--predict-date", "2026-06-08", "--stage", "all"])
            with patch("harness.cli.run_onboard", return_value=fail_report):
                with contextlib.redirect_stdout(output):
                    fail_code = main(["onboard", "demo_daily", "--predict-date", "2026-06-08", "--stage", "all"])

        with contextlib.redirect_stdout(output):
            blocked_code = main(["activate", "--scheme-id", "demo_daily"])

        self.assertEqual(pass_code, 0)
        self.assertEqual(fail_code, 1)
        self.assertEqual(blocked_code, 2)

    def test_cli_onboard_defaults_to_local_backend_port(self) -> None:
        from harness.cli import main
        from harness.result import OnboardReport

        captured = {}

        def fake_onboard(ctx, stage: str, *, check_only: bool) -> OnboardReport:
            captured["api_base_url"] = ctx.api_base_url
            captured["prediction_phase"] = ctx.prediction_phase
            captured["check_only"] = check_only
            return OnboardReport(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                stage_requested=stage,
                results=[],
                overall_passed=True,
                report_dir=ctx.report_dir,
            )

        output = io.StringIO()
        with patch("harness.cli.run_onboard", side_effect=fake_onboard):
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "onboard",
                        "demo_daily",
                        "--predict-date",
                        "2026-06-08",
                        "--stage",
                        "all",
                        "--prediction-phase",
                        "gray_live",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(captured["api_base_url"], "http://127.0.0.1:8100")
        self.assertEqual(captured["prediction_phase"], "gray_live")
        self.assertFalse(captured["check_only"])


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


def _fake_monthly_semantics_calendar() -> SimpleNamespace:
    return SimpleNamespace(
        is_trading_day=lambda day: day in {"2026-04-15", "2026-05-15"},
        previous_trading_day=lambda day: {"2026-04-16": "2026-04-15", "2026-05-16": "2026-05-15"}[day],
        next_trading_days=lambda day, count: {
            "2026-04-14": ["2026-04-15"],
            "2026-05-14": ["2026-05-15"],
        }.get(day, [])[:count],
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


def _write_monthly_scheme(project_root: Path, *, scheme_id: str) -> Path:
    scheme_dir = project_root / "schemes" / scheme_id
    scheme_dir.mkdir(parents=True)
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                'name: "Demo Monthly"',
                'description: "Demo monthly scheme"',
                "horizon: 30",
                "task_type: monthly",
                'tenors: ["10Y"]',
                "frequency: monthly",
                "target_rule: next_month_observation_yield_vs_feature_month_observation_yield",
                "schedule:",
                '  cron: "0 18 15 * *"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "status: paused",
                "input_spec:",
                "  data_version: shared_data_service_monthly.v1",
                '  required_columns: ["month_id", "M0000001"]',
            ]
        ),
        encoding="utf-8",
    )
    return scheme_dir
