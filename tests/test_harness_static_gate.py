from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
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
                )
            )

        self.assertFalse(result.passed)
        self.assertTrue(any("other_daily" in item for item in result.errors), result.errors)
        self.assertTrue(any("predict.py" in item for item in result.errors), result.errors)
        self.assertIn("cross_scheme_imports", _evidence_keys(result))

    def test_predict_direct_file_write_is_rejected(self) -> None:
        from harness.context import GateContext
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(
                project_root,
                scheme_id="demo_daily",
            )
            predict_path = scheme_dir / "predict.py"
            predict_path.write_text(
                predict_path.read_text(encoding="utf-8")
                + '\nPath("forged.csv").write_text("bad")\n',
                encoding="utf-8",
            )

            result = StaticGate().run(
                GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    project_root=project_root,
                )
            )

        self.assertFalse(result.passed)
        self.assertTrue(
            any("file write call" in item for item in result.errors),
            result.errors,
        )


class HarnessRuntimeGateTests(unittest.TestCase):
    def test_dry_run_gate_validates_actual_input_without_rebuilding(self) -> None:
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
            extra={
                "input_artifact_path": "",
                "input_artifact_source": "shared_data_service_daily",
                "feature_date": "2026-06-07",
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_minimal_scheme(project_root, scheme_id="demo_daily")
            artifact_path = project_root / "daily_output.csv"
            artifact_path.write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )
            record.extra["input_artifact_path"] = str(artifact_path)
            engine = SimpleNamespace(dispose=lambda: None)

            def run_native(*_args, input_root: Path, **_kwargs):
                generated = (
                    input_root
                    / "views"
                    / "demo_daily"
                    / "daily_output_2026-06-08.csv"
                )
                generated.parent.mkdir(parents=True)
                generated.write_text(
                    "date,TB0YWI0C\n2026-06-07,1.8\n",
                    encoding="utf-8",
                )
                record.extra["input_artifact_path"] = str(generated)
                return [record]

            def read_receipts(_root: Path):
                return {
                    "daily": _daily_audit_receipt(
                        Path(record.extra["input_artifact_path"]),
                        feature_date="2026-06-07",
                    )
                }

            with (
                patch(
                    "harness.gates.dry_run_gate.run_scheme_subprocess",
                    side_effect=run_native,
                ) as run_scheme,
                patch(
                    "harness.gates.dry_run_gate.get_calendar",
                    return_value=_fake_daily_semantics_calendar(),
                ),
                patch(
                    "harness.gates.dry_run_gate.snapshot_table_counts",
                    side_effect=[
                        {"t_scheme_predictions": 10, "t_scheme_run_log": 20},
                        {"t_scheme_predictions": 10, "t_scheme_run_log": 20},
                    ],
                ),
                patch(
                    "harness.gates.dry_run_gate._load_input_audit_receipts",
                    side_effect=read_receipts,
                ),
            ):
                result = DryRunGate().run(
                    GateContext(
                        scheme_id="demo_daily",
                        predict_date="2026-06-08",
                        project_root=project_root,
                        engine_factory=lambda: engine,
                    )
                )

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["input_artifacts"][0]["frequency"], "daily")
        self.assertEqual(evidence["input_artifacts"][0]["columns"], ["date", "TB0YWI0C"])
        self.assertEqual(evidence["input_artifacts"][0]["missing_required_cols"], [])
        self.assertEqual(evidence["input_artifacts"][0]["first_coverage_key"], "2026-06-07")
        run_scheme.assert_called_once()

    def test_dry_run_input_contract_rejects_missing_actual_columns(self) -> None:
        from harness.gates.dry_run_gate import _validate_input_artifacts
        from shared.models import PredictionRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "daily_output.csv"
            artifact_path.write_text("date\n2026-06-07\n", encoding="utf-8")
            record = PredictionRecord(
                scheme_id="demo_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-08",
                target_date="2026-06-09",
                predicted_direction=1,
                feature_date="2026-06-07",
                extra={
                    "input_artifact_path": str(artifact_path),
                    "input_artifact_source": "shared_data_service_daily",
                },
            )
            config = {
                "frequency": "daily",
                "input_spec": {
                    "data_version": "shared_data_service_daily.v1",
                    "required_columns": ["date", "TB0YWI0C"],
                },
            }
            with patch(
                "harness.gates.dry_run_gate.input_artifact_path",
                return_value=artifact_path,
            ):
                errors, evidence = _validate_input_artifacts(
                    [record],
                    config,
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    trusted_input_root=artifact_path.parent,
                )

        self.assertTrue(any("TB0YWI0C" in error for error in errors), errors)
        self.assertEqual(evidence[0]["missing_required_cols"], ["TB0YWI0C"])

    def test_dry_run_input_contract_requires_current_builder_receipt(self) -> None:
        from harness.gates.dry_run_gate import _validate_input_artifacts
        from shared.models import PredictionRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "daily_output.csv"
            artifact_path.write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )
            record = PredictionRecord(
                scheme_id="demo_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-08",
                target_date="2026-06-09",
                predicted_direction=1,
                feature_date="2026-06-07",
                extra={
                    "input_artifact_path": str(artifact_path),
                    "input_artifact_source": "shared_data_service_daily",
                },
            )
            config = {
                "frequency": "daily",
                "input_spec": {
                    "data_version": "shared_data_service_daily.v1",
                    "required_columns": ["date", "TB0YWI0C"],
                },
            }
            with patch(
                "harness.gates.dry_run_gate.input_artifact_path",
                return_value=artifact_path,
            ):
                errors, _evidence = _validate_input_artifacts(
                    [record],
                    config,
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    audit_receipts={},
                    trusted_input_root=artifact_path.parent,
                )

        self.assertTrue(
            any("not generated by the shared builder" in error for error in errors),
            errors,
        )

    def test_dry_run_input_contract_rejects_symlink_artifact(self) -> None:
        from harness.gates.dry_run_gate import _validate_input_artifacts
        from shared.models import PredictionRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "outside.csv"
            target.write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )
            artifact_path = root / "daily_output.csv"
            artifact_path.symlink_to(target)
            record = PredictionRecord(
                scheme_id="demo_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-08",
                target_date="2026-06-09",
                predicted_direction=1,
                feature_date="2026-06-07",
                extra={
                    "input_artifact_path": str(artifact_path),
                    "input_artifact_source": "shared_data_service_daily",
                },
            )
            config = {
                "frequency": "daily",
                "input_spec": {
                    "data_version": "shared_data_service_daily.v1",
                    "required_columns": ["date", "TB0YWI0C"],
                },
            }
            with patch(
                "harness.gates.dry_run_gate.input_artifact_path",
                return_value=artifact_path,
            ):
                errors, _evidence = _validate_input_artifacts(
                    [record],
                    config,
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    trusted_input_root=artifact_path.parent,
                )

        self.assertTrue(any("non-symlink" in error for error in errors), errors)

    def test_dry_run_input_contract_rejects_symlink_ancestor(self) -> None:
        from harness.gates.dry_run_gate import _controlled_input_file_details

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trusted = root / "trusted"
            outside = root / "outside"
            trusted.mkdir()
            outside.mkdir()
            (trusted / "demo_daily").symlink_to(outside, target_is_directory=True)
            artifact_path = trusted / "demo_daily" / "daily_output.csv"
            artifact_path.write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )

            details, error = _controlled_input_file_details(
                artifact_path,
                trusted_root=trusted,
            )

        self.assertIsNone(details)
        self.assertIn("ancestor is unsafe", str(error))

    def test_dry_run_input_contract_rejects_receipt_identity_and_future_cutoff(
        self,
    ) -> None:
        from harness.gates.dry_run_gate import _validate_input_artifacts
        from shared.models import PredictionRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "daily_output.csv"
            artifact_path.write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )
            receipt = _daily_audit_receipt(
                artifact_path,
                feature_date="2026-06-07",
            )
            receipt["data_version"] = "wrong.v1"
            receipt["content_hash"] = "0" * 64
            receipt["metadata"]["end_date"] = "2026-06-09"
            receipt["date_coverage"]["end"] = "2026-06-09"
            record = PredictionRecord(
                scheme_id="demo_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-08",
                target_date="2026-06-09",
                predicted_direction=1,
                feature_date="2026-06-07",
                extra={
                    "input_artifact_path": str(artifact_path),
                    "input_artifact_source": "shared_data_service_daily",
                },
            )
            config = {
                "frequency": "daily",
                "input_spec": {
                    "data_version": "shared_data_service_daily.v1",
                    "required_columns": ["date", "TB0YWI0C"],
                },
            }
            with patch(
                "harness.gates.dry_run_gate.input_artifact_path",
                return_value=artifact_path,
            ):
                errors, _evidence = _validate_input_artifacts(
                    [record],
                    config,
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    audit_receipts={"daily": receipt},
                    trusted_input_root=artifact_path.parent,
                )

        self.assertTrue(any("data_version mismatch" in item for item in errors))
        self.assertTrue(any("content_hash mismatch" in item for item in errors))
        self.assertTrue(any("end_date mismatch" in item for item in errors))
        self.assertTrue(any("exceeds feature_date" in item for item in errors))

    def test_dry_run_input_contract_validates_auxiliary_artifacts(self) -> None:
        from harness.gates.dry_run_gate import _validate_input_artifacts
        from shared.models import PredictionRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = {
                "daily": root / "daily.csv",
                "weekly": root / "weekly.csv",
                "monthly": root / "monthly.csv",
            }
            paths["daily"].write_text(
                "date,TB0YWI0C\n2026-06-07,1.8\n",
                encoding="utf-8",
            )
            paths["weekly"].write_text(
                "week_id,S0114089\n202622,1.0\n",
                encoding="utf-8",
            )
            paths["monthly"].write_text(
                "month_id,M0000545\n202605,2.0\n",
                encoding="utf-8",
            )
            record = PredictionRecord(
                scheme_id="demo_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-08",
                target_date="2026-06-09",
                predicted_direction=1,
                feature_date="2026-06-07",
                extra={
                    "input_artifact_path": str(paths["daily"]),
                    "input_artifact_source": "shared_data_service_daily",
                    "weekly_input_artifact_path": str(paths["weekly"]),
                    "weekly_input_artifact_source": "shared_data_service_weekly",
                    "monthly_input_artifact_path": str(paths["monthly"]),
                    "monthly_input_artifact_source": "shared_data_service_monthly",
                },
            )
            config = {
                "frequency": "daily",
                "input_spec": {
                    "data_version": "shared_data_service_daily.v1",
                    "required_columns": ["date", "TB0YWI0C"],
                    "auxiliary_inputs": [
                        {
                            "frequency": "weekly",
                            "data_version": "shared_data_service_weekly.v1",
                            "required_columns": ["week_id", "S0114089"],
                        },
                        {
                            "frequency": "monthly",
                            "data_version": "shared_data_service_monthly.v1",
                            "required_columns": ["month_id", "M0000545"],
                        },
                    ],
                },
            }
            with patch(
                "harness.gates.dry_run_gate.input_artifact_path",
                side_effect=lambda **kwargs: paths[kwargs["frequency"]],
            ):
                errors, evidence = _validate_input_artifacts(
                    [record],
                    config,
                    scheme_id="demo_daily",
                    predict_date="2026-06-08",
                    trusted_input_root=root,
                )

        self.assertEqual(errors, [])
        self.assertEqual(
            [item["frequency"] for item in evidence],
            ["daily", "weekly", "monthly"],
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
                            )
                        )

        self.assertFalse(result.passed)
        self.assertTrue(any("input_artifact_source" in error for error in result.errors), result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["predictions_table_delta"], 0)
        self.assertEqual(evidence["run_log_delta"], 0)


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
                with patch(
                    "harness.gates.backtest_gate.run_backtest_runner",
                    return_value=current,
                ) as run_backtest:
                    result = BacktestGate().run(
                        GateContext(
                            scheme_id="demo_daily",
                            predict_date="2026-06-08",
                            project_root=project_root,
                            engine_factory=lambda: engine,
                        )
                    )

        self.assertFalse(run_backtest.call_args.kwargs["persist"])

        self.assertTrue(result.passed, result.errors)
        evidence = _evidence_dict(result)
        self.assertEqual(evidence["row_count"], 2)
        self.assertEqual(evidence["monthly_count"], 1)
        self.assertEqual(evidence["protected_table_deltas"], {"t_backtest_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0})

    def test_orchestrator_fail_fast_stops_after_first_failure(self) -> None:
        from harness.context import GateContext
        from harness.orchestrator import onboard
        from harness.result import Evidence, GateResult, GateStatus

        class FakeGate:
            def __init__(self, name: str, status: GateStatus) -> None:
                self.name = name
                self.status = status

            def run(self, ctx: GateContext) -> GateResult:
                return GateResult(
                    gate_name=self.name,
                    status=self.status,
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
            )
            with (
                patch(
                    "harness.orchestrator.persist_harness_run_start",
                    return_value=True,
                ),
                patch(
                    "harness.orchestrator.persist_harness_run_complete",
                    return_value=True,
                ),
            ):
                report = onboard(
                    ctx,
                    stage="all",
                    gates=[
                        FakeGate("static", GateStatus.PASSED),
                        FakeGate("input", GateStatus.FAILED),
                        FakeGate("dry-run", GateStatus.PASSED),
                    ],
                )

        self.assertFalse(report.overall_passed)
        self.assertEqual([item.gate_name for item in report.results], ["static", "input"])


def _evidence_keys(result) -> set[str]:
    return {item.key for item in result.evidence}


def _evidence_dict(result) -> dict:
    return {item.key: item.value for item in result.evidence}


def _fake_daily_semantics_calendar() -> SimpleNamespace:
    return SimpleNamespace(
        previous_trading_day=lambda _predict_date: "2026-06-07",
        nth_trading_day_after=lambda _feature_date, _horizon: "2026-06-09",
    )


def _daily_audit_receipt(
    artifact_path: Path,
    *,
    feature_date: str,
) -> dict:
    details = artifact_path.stat()
    return {
        "scheme_id": "demo_daily",
        "frequency": "daily",
        "path": str(artifact_path),
        "source": "shared_data_service_daily",
        "data_version": "shared_data_service_daily.v1",
        "file_size": details.st_size,
        "modified_ns": details.st_mtime_ns,
        "content_hash": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "row_count": 1,
        "columns": ["date", "TB0YWI0C"],
        "date_coverage": {
            "field": "date",
            "start": feature_date,
            "end": feature_date,
        },
        "metadata": {
            "predict_date": "2026-06-08",
            "end_date": feature_date,
        },
    }


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
