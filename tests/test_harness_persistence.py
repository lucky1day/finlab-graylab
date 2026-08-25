from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
from harness.result import Evidence, GateResult, GateStatus


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params=None):
        self._store.setdefault("calls", []).append((str(sql), params))
        return SimpleNamespace(rowcount=self._store.get("rowcount", 1))


class _CaptureBegin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _CaptureConnection:
        return _CaptureConnection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _CaptureEngine:
    def __init__(self) -> None:
        self.store: dict = {}
        self.disposed = False

    def begin(self) -> _CaptureBegin:
        return _CaptureBegin(self.store)

    def dispose(self) -> None:
        self.disposed = True


class _RecordingPassingGate:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    def run(self, _ctx: GateContext) -> GateResult:
        self._calls.append(self.name)
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[Evidence("gate_executed", self.name)],
            errors=[],
            started_at="2026-08-04T00:00:00+00:00",
            finished_at="2026-08-04T00:00:01+00:00",
        )


def _result_evidence(result: GateResult) -> dict[str, object]:
    return {item.key: item.value for item in result.evidence}


class HarnessPersistenceTests(unittest.TestCase):
    def test_persistence_writes_only_harness_tables(self) -> None:
        from harness.persistence import (
            persist_harness_gate_result,
            persist_harness_run_finish,
            persist_harness_run_start,
        )

        engine = _CaptureEngine()
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                engine_factory=lambda: engine,
            )
            result = GateResult(
                gate_name="static",
                status=GateStatus.PASSED,
                passed=True,
                evidence=[Evidence("checked", True)],
                errors=[],
                started_at="2026-06-08T00:00:00+00:00",
                finished_at="2026-06-08T00:00:01+00:00",
            )

            self.assertTrue(
                persist_harness_run_start(
                    ctx,
                    harness_run_id="hr-test",
                    stage="all",
                    started_at="2026-06-08T00:00:00+00:00",
                )
            )
            self.assertTrue(persist_harness_gate_result(ctx, "hr-test", result))
            self.assertTrue(
                persist_harness_run_finish(
                    ctx,
                    harness_run_id="hr-test",
                    status="passed",
                    finished_at="2026-06-08T00:00:02+00:00",
                    report_uri=str(Path(tmpdir) / "reports"),
                )
            )

        sql_text = "\n".join(sql for sql, _ in engine.store["calls"])
        self.assertIn("t_harness_runs", sql_text)
        self.assertIn("t_harness_gate_results", sql_text)
        self.assertNotIn("t_scheme_predictions", sql_text)
        self.assertNotIn("t_scheme_runs", sql_text)
        self.assertNotIn("t_backtest", sql_text)
        self.assertEqual(
            engine.store["calls"][0][1]["report_uri"],
            str(Path(tmpdir) / "reports"),
        )
        self.assertNotIn("report_uri", engine.store["calls"][1][1])
        self.assertEqual(
            engine.store["calls"][2][1]["report_uri"],
            str(Path(tmpdir) / "reports"),
        )
        self.assertTrue(engine.disposed)





    def test_regular_onboard_still_calls_control_plane_persistence(self) -> None:
        from harness.gates.base import Gate, utc_now
        from harness.orchestrator import onboard

        class PassingGate(Gate):
            name = "static"

            def run(self, ctx: GateContext) -> GateResult:
                now = utc_now()
                return GateResult(
                    gate_name=self.name,
                    status=GateStatus.PASSED,
                    passed=True,
                    evidence=[],
                    errors=[],
                    started_at=now,
                    finished_at=now,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
            )
            with (
                patch("harness.orchestrator.persist_harness_run_start") as start,
                patch("harness.orchestrator.persist_harness_gate_result") as gate,
                patch("harness.orchestrator.persist_harness_run_finish") as finish,
            ):
                report = onboard(
                    ctx,
                    stage="all",
                    gates=[PassingGate()],
                )

        start.assert_called_once()
        gate.assert_called_once()
        finish.assert_called_once()
        self.assertTrue(report.control_plane_persisted)
        self.assertIsNotNone(report.harness_run_id)
        self.assertTrue(report.harness_run_id.startswith("hr_"))

    def test_run_start_persistence_failure_blocks_before_gates(self) -> None:
        from harness.orchestrator import onboard

        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = GateContext(
                scheme_id="native_daily",
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports",
            )
            with (
                patch(
                    "harness.orchestrator.persist_harness_run_start",
                    return_value=False,
                ) as start,
                patch("harness.orchestrator.persist_harness_gate_result") as gate,
                patch("harness.orchestrator.persist_harness_run_finish") as finish,
            ):
                report = onboard(
                    ctx,
                    stage=" Native-Maintenance ",
                    gates=[_RecordingPassingGate("static", calls)],
                )
        start.assert_called_once()
        gate.assert_not_called()
        finish.assert_not_called()
        self.assertEqual(calls, [])
        self.assertFalse(report.overall_passed)
        self.assertFalse(report.control_plane_persisted)
        self.assertEqual([result.gate_name for result in report.results], ["control-plane-persistence"])
        evidence = _result_evidence(report.results[0])
        self.assertEqual(evidence["persistence_operation"], "run_start")
        self.assertFalse(evidence["control_plane_persisted"])
        self.assertFalse(ctx.report_dir.exists())

    def test_gate_persistence_failure_stops_and_closes_failed(self) -> None:
        from harness.orchestrator import onboard

        calls: list[str] = []
        gate_names = ["static", "native-maintenance-admission", "input"]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = GateContext(
                scheme_id="native_daily",
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports",
            )
            with (
                patch(
                    "harness.orchestrator.persist_harness_run_start",
                    return_value=True,
                ),
                patch(
                    "harness.orchestrator.persist_harness_gate_result",
                    side_effect=[True, False],
                ) as persist_gate,
                patch(
                    "harness.orchestrator.persist_harness_run_finish",
                    return_value=True,
                ) as finish,
            ):
                report = onboard(
                    ctx,
                    stage="native-maintenance",
                    gates=[_RecordingPassingGate(name, calls) for name in gate_names],
                )
        self.assertEqual(calls, gate_names[:2])
        self.assertEqual(persist_gate.call_count, 2)
        finish.assert_called_once()
        self.assertEqual(finish.call_args.kwargs["status"], "failed")
        self.assertFalse(report.overall_passed)
        self.assertFalse(report.control_plane_persisted)
        self.assertEqual(
            [result.gate_name for result in report.results],
            [*gate_names[:2], "control-plane-persistence"],
        )
        evidence = _result_evidence(report.results[-1])
        self.assertEqual(evidence["persistence_operation"], "gate_result")
        self.assertEqual(evidence["persistence_gate_name"], gate_names[1])
        self.assertFalse(ctx.report_dir.exists())
