from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.context import GateContext


class BlackboxV2HarnessDispatchTests(unittest.TestCase):
    def test_defaults_to_native_gates_for_existing_callers(self) -> None:
        from harness.gates.static_gate import StaticGate
        from harness.registry import gate_for_name

        self.assertIsInstance(gate_for_name("static"), StaticGate)

    def test_dispatches_all_auto_gates_by_explicit_runtime_type(self) -> None:
        from harness.registry import AUTO_SEQUENCE, gates_for_stage

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="blackbox_trial",
                predict_date="2026-07-16",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                config=SimpleNamespace(runtime_type="blackbox_v2"),
            )
            gates = gates_for_stage("all", ctx=ctx)

        self.assertEqual([gate.name for gate in gates], AUTO_SEQUENCE)
        self.assertTrue(all(type(gate).__module__ == "harness.blackbox_v2.gates" for gate in gates))

    def test_reuses_common_api_and_live_gates_after_blackbox_activation(self) -> None:
        from harness.gates.api_gate import ApiGate
        from harness.gates.live_gate import LiveGate
        from harness.registry import gate_for_name

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="blackbox_trial",
                predict_date="2026-07-16",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                config=SimpleNamespace(runtime_type="blackbox_v2"),
            )
            api_gate = gate_for_name("api", ctx=ctx)
            live_gate = gate_for_name("live", ctx=ctx)

        self.assertIsInstance(api_gate, ApiGate)
        self.assertIsInstance(live_gate, LiveGate)

    def test_rejects_unknown_runtime_type(self) -> None:
        from harness.registry import gates_for_stage

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="unknown",
                predict_date="2026-07-16",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                config=SimpleNamespace(runtime_type="mystery"),
            )
            with self.assertRaisesRegex(ValueError, "runtime_type"):
                gates_for_stage("all", ctx=ctx)


if __name__ == "__main__":
    unittest.main()
