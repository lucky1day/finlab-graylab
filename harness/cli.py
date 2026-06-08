from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from harness.authorization import issue_token
from harness.context import GateContext
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.result import GateResult, GateStatus


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "auth" and args.auth_command == "issue":
        token = issue_token(args.scheme_id, args.action, args.predict_date, issued_by=args.issued_by)
        print(token)
        return 0
    if args.command == "gate":
        result = _run_gate(args)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        if result.status == GateStatus.BLOCKED:
            return 2
        return 0 if result.passed else 1
    parser.error("unsupported command")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_name", required=True)
    for gate_name in ("static", "input", "unit", "dry-run", "live"):
        item = gate_subparsers.add_parser(gate_name)
        item.add_argument("--scheme-id", required=True)
        item.add_argument("--predict-date", default="static" if gate_name in {"static", "unit"} else None)
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--report-dir", type=Path, default=None)
        item.add_argument("--algo-env", default="forecast_env")
        item.add_argument("--timeout-sec", type=int, default=600)
        item.add_argument("--authorize", default=None)

    auth_parser = subparsers.add_parser("auth")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    issue_parser = auth_subparsers.add_parser("issue")
    issue_parser.add_argument("--scheme-id", required=True)
    issue_parser.add_argument("--action", required=True)
    issue_parser.add_argument("--predict-date", default=None)
    issue_parser.add_argument("--issued-by", default="harness")
    return parser


def _run_gate(args: argparse.Namespace) -> GateResult:
    if args.gate_name in {"input", "dry-run"} and not args.predict_date:
        raise SystemExit(f"gate {args.gate_name} requires --predict-date")
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        algo_env=args.algo_env,
        timeout_sec=args.timeout_sec,
        authorization=args.authorize,
    )
    gates = {
        "static": StaticGate(),
        "input": InputGate(),
        "unit": UnitGate(),
        "dry-run": DryRunGate(),
        "live": LiveGate(),
    }
    return gates[args.gate_name].run(ctx)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, GateStatus):
        return value.value
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
