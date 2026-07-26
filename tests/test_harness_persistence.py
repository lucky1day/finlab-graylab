from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.context import GateContext
from harness.result import Evidence, GateResult, GateStatus


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params=None) -> None:
        self._store.setdefault("calls", []).append((str(sql), params))


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
                report_path=Path(tmpdir) / "reports" / "static.json",
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
                    report_uri=str(Path(tmpdir) / "reports" / "onboard_report.json"),
                )
            )

        sql_text = "\n".join(sql for sql, _ in engine.store["calls"])
        self.assertIn("t_harness_runs", sql_text)
        self.assertIn("t_harness_gate_results", sql_text)
        self.assertNotIn("t_scheme_predictions", sql_text)
        self.assertNotIn("t_scheme_runs", sql_text)
        self.assertNotIn("t_backtest", sql_text)
        self.assertTrue(engine.disposed)

    def test_persistence_failure_degrades_to_json_only(self) -> None:
        from harness.persistence import persist_harness_run_start

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")),
            )

            persisted = persist_harness_run_start(
                ctx,
                harness_run_id="hr-test",
                stage="all",
                started_at="2026-06-08T00:00:00+00:00",
            )

        self.assertFalse(persisted)

    def test_persistence_computes_scheme_version_without_loaded_config(self) -> None:
        from harness.persistence import persist_harness_run_start
        from shared.versioning import compute_code_hash, compute_config_hash, compute_scheme_version

        engine = _CaptureEngine()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text("scheme_id: demo_daily\nstatus: active\n", encoding="utf-8")
            (scheme_dir / "predict.py").write_text("SCHEME_ID = 'demo_daily'\n", encoding="utf-8")
            expected_version = compute_scheme_version(
                compute_code_hash(scheme_dir),
                compute_config_hash(scheme_dir / "config.yaml"),
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=root,
                report_dir=root / "reports",
                engine_factory=lambda: engine,
            )

            self.assertTrue(
                persist_harness_run_start(
                    ctx,
                    harness_run_id="hr-test",
                    stage="all",
                    started_at="2026-06-08T00:00:00+00:00",
                )
            )

        _, params = engine.store["calls"][0]
        self.assertEqual(params["scheme_version"], expected_version)

    def test_onboard_report_carries_generated_harness_run_id(self) -> None:
        from harness.gates.base import Gate, utc_now
        from harness.orchestrator import onboard

        class PassingGate(Gate):
            name = "static"

            def __init__(self, name: str = "static") -> None:
                self.name = name

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
            report = onboard(ctx, stage="all", gates=[PassingGate()])

        self.assertIsNotNone(report.harness_run_id)
        self.assertTrue(report.harness_run_id.startswith("hr_"))

    def test_check_only_onboard_never_calls_control_plane_persistence(self) -> None:
        from harness.gates.base import Gate, utc_now
        from harness.orchestrator import onboard

        class PassingGate(Gate):
            name = "static"

            def __init__(self, name: str = "static") -> None:
                self.name = name

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
                patch(
                    "harness.orchestrator.gates_for_stage",
                    return_value=[
                        PassingGate(name)
                        for name in (
                            "static",
                            "input",
                            "unit",
                            "dry-run",
                            "compare",
                            "backtest",
                            "api-readiness",
                        )
                    ],
                ),
                patch("harness.orchestrator.persist_harness_run_start") as start,
                patch("harness.orchestrator.persist_harness_gate_result") as gate,
                patch("harness.orchestrator.persist_harness_run_finish") as finish,
            ):
                report = onboard(
                    ctx,
                    stage="all",
                    check_only=True,
                )
            persisted_report = json.loads(
                (ctx.report_dir / "onboard_report.json").read_text(
                    encoding="utf-8"
                )
            )

        start.assert_not_called()
        gate.assert_not_called()
        finish.assert_not_called()
        self.assertTrue(report.check_only)
        self.assertFalse(report.control_plane_persisted)
        self.assertFalse(report.business_tables_written)
        self.assertFalse(report.persist_backtest)
        self.assertTrue(report.harness_run_id.startswith("hr_"))
        self.assertTrue(persisted_report["check_only"])
        self.assertFalse(persisted_report["control_plane_persisted"])
        self.assertFalse(persisted_report["business_tables_written"])
        self.assertFalse(persisted_report["persist_backtest"])

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
        self.assertFalse(report.check_only)
        self.assertTrue(report.control_plane_persisted)

    def test_check_only_programmatic_entrypoint_rejects_side_effect_context(self) -> None:
        from harness.orchestrator import onboard
        from harness.gates.base import Gate

        class SideEffectGate(Gate):
            name = "live"

            def run(self, ctx):
                raise AssertionError("must be rejected before execution")

        class MasqueradingGate(SideEffectGate):
            name = "static"

        class InputMasqueradingGate(SideEffectGate):
            name = "input"

        with tempfile.TemporaryDirectory() as tmpdir:
            base = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
            )
            unsafe_contexts = (
                GateContext(
                    **{
                        **base.__dict__,
                        "authorization": "signed-token",
                    }
                ),
                GateContext(
                    **{
                        **base.__dict__,
                        "prediction_phase": "gray_live",
                    }
                ),
                GateContext(
                    **{
                        **base.__dict__,
                        "persist_backtest": True,
                    }
                ),
            )
            for ctx in unsafe_contexts:
                with self.subTest(ctx=ctx), self.assertRaisesRegex(
                    ValueError,
                    "check-only",
                ):
                    onboard(ctx, stage="all", check_only=True)
            with self.assertRaisesRegex(ValueError, "check-only"):
                onboard(base, stage="api", check_only=True)
            with self.assertRaisesRegex(ValueError, "check-only"):
                onboard(
                    base,
                    stage="all",
                    gates=[SideEffectGate()],
                    check_only=True,
                )
            injected_shapes = (
                [MasqueradingGate()],
                [MasqueradingGate(), MasqueradingGate()],
                [InputMasqueradingGate(), MasqueradingGate()],
                [MasqueradingGate(), SideEffectGate()],
            )
            for injected in injected_shapes:
                with self.subTest(injected=injected), self.assertRaisesRegex(
                    ValueError,
                    "check-only",
                ):
                    onboard(
                        base,
                        stage="all",
                        gates=injected,
                        check_only=True,
                    )
            canonical_mismatches = (
                [
                    MasqueradingGate()
                    for _ in range(6)
                ],
                [
                    MasqueradingGate()
                    for _ in range(7)
                ],
                [
                    InputMasqueradingGate(),
                    MasqueradingGate(),
                    *[
                        MasqueradingGate()
                        for _ in range(5)
                    ],
                ],
            )
            for selected in canonical_mismatches:
                with (
                    self.subTest(selected=selected),
                    patch(
                        "harness.orchestrator.gates_for_stage",
                        return_value=selected,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "canonical",
                    ),
                ):
                    onboard(
                        base,
                        stage="all",
                        check_only=True,
                    )

    def test_check_only_reports_seven_gates_in_order_and_fails_fast(self) -> None:
        from harness.gates.base import Gate, utc_now
        from harness.orchestrator import onboard
        from harness.registry import AUTO_SEQUENCE

        calls: list[str] = []

        class NamedGate(Gate):
            def __init__(self, name: str, *, fail: bool = False) -> None:
                self.name = name
                self.fail = fail

            def run(self, ctx: GateContext) -> GateResult:
                calls.append(self.name)
                now = utc_now()
                return GateResult(
                    gate_name=self.name,
                    status=(
                        GateStatus.FAILED
                        if self.fail
                        else GateStatus.PASSED
                    ),
                    passed=not self.fail,
                    evidence=[],
                    errors=["stop"] if self.fail else [],
                    started_at=now,
                    finished_at=now,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-06-08",
                project_root=root,
                report_dir=root / "passing",
            )
            with patch(
                "harness.orchestrator.gates_for_stage",
                return_value=[
                    NamedGate(name) for name in AUTO_SEQUENCE
                ],
            ):
                passing = onboard(
                    ctx,
                    stage="all",
                    check_only=True,
                )
            calls.clear()
            with patch(
                "harness.orchestrator.gates_for_stage",
                return_value=[
                    NamedGate(name, fail=name == "compare")
                    for name in AUTO_SEQUENCE
                ],
            ):
                failed = onboard(
                    GateContext(
                        **{
                            **ctx.__dict__,
                            "report_dir": root / "failed",
                        }
                    ),
                    stage="all",
                    check_only=True,
                )

        self.assertEqual(
            [result.gate_name for result in passing.results],
            AUTO_SEQUENCE,
        )
        self.assertTrue(passing.overall_passed)
        self.assertEqual(calls, AUTO_SEQUENCE[:5])
        self.assertEqual(
            [result.gate_name for result in failed.results],
            AUTO_SEQUENCE[:5],
        )
        self.assertFalse(failed.overall_passed)


if __name__ == "__main__":
    unittest.main()
