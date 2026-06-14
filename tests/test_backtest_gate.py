from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
from harness.gates.backtest_gate import BacktestGate
from harness.result import GateStatus


def _evidence_dict(result) -> dict:
    return {item.key: item.value for item in result.evidence}


def _write_scheme_with_runner(project_root: Path, scheme_id: str = "demo_daily") -> Path:
    """写出一个带 backtest.runner 的最小方案 config，仅供 BacktestGate 使用。"""
    scheme_dir = project_root / "schemes" / scheme_id
    scheme_dir.mkdir(parents=True)
    config_path = scheme_dir / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                'name: "Demo"',
                "backtest:",
                "  runner: backtests.demo_daily_reproduction",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return scheme_dir


class BacktestGateBootstrapTests(unittest.TestCase):
    def test_baseline_absent_bootstraps_and_passes(self) -> None:
        current = {
            "status": "success",
            "scheme_id": "demo_daily",
            "row_count": 3,
            "monthly_count": 1,
            "summary": {"by_tenor": {"10Y": {"samples": 3, "correct": 2}}},
            "elapsed_sec": 12.5,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme_with_runner(project_root)
            baseline_path = (
                project_root / "reports" / "refactor_baseline" / "demo_daily" / "backtest_no_persist.json"
            )
            self.assertFalse(baseline_path.exists())

            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.backtest_gate.snapshot_table_counts", side_effect=[{}, {}]):
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

            self.assertEqual(result.status, GateStatus.PASSED)
            self.assertTrue(result.passed, result.errors)
            evidence = _evidence_dict(result)
            self.assertTrue(evidence["baseline_bootstrapped"])
            self.assertIsNone(evidence["diff_count"])
            # 自举后基线文件应被写出，且内容等于本次输出。
            self.assertTrue(baseline_path.exists())
            written = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.assertEqual(written, current)

    def test_baseline_present_uses_diff_path(self) -> None:
        baseline = {
            "status": "success",
            "scheme_id": "demo_daily",
            "row_count": 2,
            "monthly_count": 1,
            "summary": {"by_tenor": {"10Y": {"samples": 2, "correct": 1}}},
            "elapsed_sec": 1.0,
        }
        # elapsed_sec 不同应被忽略；其余相同 -> diff_count == 0。
        current = dict(baseline)
        current["elapsed_sec"] = 99.0
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme_with_runner(project_root)
            baseline_dir = project_root / "reports" / "refactor_baseline" / "demo_daily"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "backtest_no_persist.json").write_text(json.dumps(baseline), encoding="utf-8")

            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.backtest_gate.snapshot_table_counts", side_effect=[{}, {}]):
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

            self.assertEqual(result.status, GateStatus.PASSED)
            evidence = _evidence_dict(result)
            self.assertFalse(evidence["baseline_bootstrapped"])
            self.assertEqual(evidence["diff_count"], 0)

    def test_baseline_present_diff_flags_failure(self) -> None:
        baseline = {"status": "success", "row_count": 2, "elapsed_sec": 1.0}
        current = {"status": "success", "row_count": 5, "elapsed_sec": 1.0}
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme_with_runner(project_root)
            baseline_dir = project_root / "reports" / "refactor_baseline" / "demo_daily"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "backtest_no_persist.json").write_text(json.dumps(baseline), encoding="utf-8")

            engine = SimpleNamespace(dispose=lambda: None)
            with patch("harness.gates.backtest_gate.snapshot_table_counts", side_effect=[{}, {}]):
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

            self.assertEqual(result.status, GateStatus.FAILED)
            self.assertFalse(result.passed)
            evidence = _evidence_dict(result)
            self.assertFalse(evidence["baseline_bootstrapped"])
            self.assertEqual(evidence["diff_count"], 1)

    def test_no_persist_fails_when_protected_table_changes(self) -> None:
        current = {
            "status": "success",
            "scheme_id": "demo_daily",
            "row_count": 3,
            "monthly_count": 1,
            "elapsed_sec": 1.0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_scheme_with_runner(project_root)
            baseline_dir = project_root / "reports" / "refactor_baseline" / "demo_daily"
            baseline_dir.mkdir(parents=True)
            (baseline_dir / "backtest_no_persist.json").write_text(json.dumps(current), encoding="utf-8")
            engine = SimpleNamespace(dispose=lambda: None)
            snapshots = [
                {"t_scheme_predictions": 10, "t_scheme_run_log": 20},
                {"t_scheme_predictions": 11, "t_scheme_run_log": 20},
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

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("t_scheme_predictions delta must remain 0" in error for error in result.errors), result.errors)

    def test_persist_backtest_does_not_allow_monthly_metrics_delta(self) -> None:
        from harness.gates.backtest_gate import _validate_backtest_table_deltas

        errors = _validate_backtest_table_deltas(
            {
                "t_backtest_runs": 1,
                "t_backtest_predictions": 10,
                "t_backtest_monthly_metrics": 1,
            },
            persist=True,
        )

        self.assertTrue(
            any("t_backtest_monthly_metrics delta must remain 0" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
