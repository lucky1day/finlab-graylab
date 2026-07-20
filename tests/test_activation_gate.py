from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from harness.context import GateContext


class ActivationGateHistoryTests(unittest.TestCase):
    def test_native_gate_history_uses_harness_id_tiebreaker_for_same_finished_second(self) -> None:
        from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES, _verify_gate_history

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t_harness_runs (harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, stage TEXT, status TEXT, finished_at TEXT)"))
            conn.execute(text("CREATE TABLE t_harness_gate_results (harness_run_id TEXT, gate_name TEXT, status TEXT)"))
            conn.execute(
                text("INSERT INTO t_harness_runs VALUES ('hr_a', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00'), ('hr_z', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00')")
            )
            conn.execute(
                text("INSERT INTO t_harness_gate_results VALUES (:run_id, :gate_name, :status)"),
                [
                    {
                        "run_id": harness_run_id,
                        "gate_name": gate_name,
                        "status": "failed" if harness_run_id == "hr_a" else "passed",
                    }
                    for harness_run_id in ("hr_a", "hr_z")
                    for gate_name in REQUIRED_ACTIVATE_GATES
                ],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = project_root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "scheme_id: demo_daily\nbacktest:\n  benchmark_required: false\n",
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-07-06",
                project_root=project_root,
                report_dir=project_root / "reports",
            )
            try:
                with patch("harness.gates.activate_gate._db_engine", return_value=engine):
                    errors = _verify_gate_history(ctx, "v1")
            finally:
                engine.dispose()

        self.assertEqual(errors, [])

    def test_benchmark_required_compare_skipped_blocks_activation(self) -> None:
        from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES, _verify_gate_history

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_harness_runs (
                        harness_run_id TEXT,
                        scheme_id TEXT,
                        scheme_version TEXT,
                        stage TEXT,
                        status TEXT,
                        finished_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_harness_gate_results (
                        harness_run_id TEXT,
                        gate_name TEXT,
                        status TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_runs
                        (harness_run_id, scheme_id, scheme_version, stage, status, finished_at)
                    VALUES ('hr_demo', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_gate_results (harness_run_id, gate_name, status)
                    VALUES (:run_id, :gate_name, :status)
                    """
                ),
                [
                    {
                        "run_id": "hr_demo",
                        "gate_name": gate_name,
                        "status": "skipped" if gate_name == "compare" else "passed",
                    }
                    for gate_name in REQUIRED_ACTIVATE_GATES
                ],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = project_root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "\n".join(
                    [
                        "scheme_id: demo_daily",
                        "backtest:",
                        "  benchmark_required: true",
                    ]
                ),
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-07-06",
                project_root=project_root,
                report_dir=project_root / "reports",
            )

            try:
                with patch("harness.gates.activate_gate._db_engine", return_value=engine):
                    errors = _verify_gate_history(ctx, "v1")
            finally:
                engine.dispose()

        self.assertTrue(any("CompareGate status is skipped" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
