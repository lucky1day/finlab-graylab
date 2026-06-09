from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.context import GateContext
from harness.gates.base import Gate, utc_now
from harness.orchestrator import onboard
from harness.result import Evidence, GateResult, GateStatus


class _SkippingCompareGate(Gate):
    name = "compare"

    def run(self, ctx: GateContext) -> GateResult:
        now = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.SKIPPED,
            passed=True,
            evidence=[Evidence("skipped", True)],
            errors=[],
            started_at=now,
            finished_at=now,
        )


class _PassingGate(Gate):
    name = "after_compare"

    def __init__(self) -> None:
        self.ran = False

    def run(self, ctx: GateContext) -> GateResult:
        self.ran = True
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


class OrchestratorCompareTest(unittest.TestCase):
    def test_skipped_compare_does_not_halt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = GateContext(
                scheme_id="demo",
                predict_date="static",
                project_root=root,
                report_dir=root / "out",
            )
            after = _PassingGate()
            report = onboard(ctx, stage="all", gates=[_SkippingCompareGate(), after])
            self.assertTrue(after.ran)
            self.assertTrue(report.overall_passed)
            self.assertEqual(
                [r.status for r in report.results],
                [GateStatus.SKIPPED, GateStatus.PASSED],
            )


if __name__ == "__main__":
    unittest.main()
