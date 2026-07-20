from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.context import GateContext


class BlackboxV2HarnessDispatchTests(unittest.TestCase):
    def test_backtest_cli_defaults_to_full_range_without_sample_size(self) -> None:
        from harness.cli import _build_parser

        args = _build_parser().parse_args([
            "gate",
            "backtest",
            "--scheme-id",
            "blackbox_trial",
            "--predict-date",
            "2026-07-20",
            "--persist",
        ])

        self.assertEqual(args.backtest_start_date, "2025-01-01")
        self.assertIsNone(args.sample_size)

    def test_backtest_cli_and_authorization_retain_explicit_start_date(self) -> None:
        from harness.cli import _build_parser

        parser = _build_parser()
        gate_args = parser.parse_args([
            "gate",
            "backtest",
            "--scheme-id",
            "blackbox_trial",
            "--persist",
            "--backtest-start-date",
            "2025-02-03",
        ])
        auth_args = parser.parse_args([
            "auth",
            "issue",
            "--scheme-id",
            "blackbox_trial",
            "--action",
            "backtest_persist",
            "--predict-date",
            "2026-07-20",
            "--backtest-start-date",
            "2025-02-03",
        ])

        self.assertEqual(gate_args.backtest_start_date, "2025-02-03")
        self.assertEqual(auth_args.backtest_start_date, "2025-02-03")

    def test_backtest_persist_auth_cli_requires_predict_date(self) -> None:
        from harness.cli import main

        with self.assertRaises(SystemExit) as raised:
            main([
                "auth",
                "issue",
                "--scheme-id",
                "blackbox_trial",
                "--action",
                "backtest_persist",
            ])

        self.assertEqual(raised.exception.code, 2)

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

    def test_dispatches_blackbox_api_and_reuses_common_live_gate_after_activation(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate
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

        self.assertIsInstance(api_gate, BlackboxApiGate)
        self.assertIsInstance(live_gate, LiveGate)

    def test_dispatches_blackbox_gray_backfill_as_explicit_gate(self) -> None:
        from harness.gates.gray_backfill_gate import GrayBackfillGate
        from harness.registry import gate_for_name

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="blackbox_trial",
                predict_date="2026-05-26",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                config=SimpleNamespace(runtime_type="blackbox_v2"),
            )
            gate = gate_for_name("gray-backfill", ctx=ctx)

        self.assertIsInstance(gate, GrayBackfillGate)

    def test_gray_backfill_cli_requires_predict_date(self) -> None:
        from harness.cli import _build_parser, _run_gate

        args = _build_parser().parse_args([
            "gate",
            "gray-backfill",
            "--scheme-id",
            "blackbox_trial",
            "--prediction-phase",
            "gray_live",
        ])
        with self.assertRaisesRegex(SystemExit, "requires --predict-date"):
            _run_gate(args)

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
