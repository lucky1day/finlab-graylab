from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from harness.authorization import issue_token
from harness.context import GateContext
from harness.gates.activate_gate import ActivationGate
from harness.gates.api_gate import ApiGate
from harness.gates.backtest_gate import BacktestGate
from harness.gates.compare_gate import CompareGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.orchestrator import onboard as run_onboard
from harness.result import GateResult, GateStatus, OnboardReport
from scheduler.repository import create_engine_from_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "auth" and args.auth_command == "issue":
        token = issue_token(
            args.scheme_id,
            args.action,
            args.predict_date,
            scheme_version=args.scheme_version,
            harness_run_id=args.harness_run_id,
            ttl_seconds=args.expires_in,
            issued_by=args.issued_by,
        )
        print(token)
        return 0
    if args.command == "gate":
        result = _run_gate(args)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        return _exit_code_for_result(result)
    if args.command == "onboard":
        report = _run_onboard_command(args)
        print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))
        return _exit_code_for_report(report)
    if args.command == "activate":
        result = _run_activate(args)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        return _exit_code_for_result(result)
    if args.command == "report":
        return _run_report(args)
    parser.error("unsupported command")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_name", required=True)
    for gate_name in ("static", "input", "unit", "dry-run", "compare", "backtest", "api", "live"):
        item = gate_subparsers.add_parser(gate_name)
        item.add_argument("--scheme-id", required=True)
        item.add_argument("--predict-date", default="static" if gate_name in {"static", "unit", "compare", "backtest", "api"} else None)
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--report-dir", type=Path, default=None)
        item.add_argument("--algo-env", default="forecast_env")
        item.add_argument("--timeout-sec", type=int, default=600)
        item.add_argument("--authorize", default=None)
        item.add_argument("--api-base-url", default="http://127.0.0.1:8100")
        if gate_name == "backtest":
            item.add_argument("--persist", action="store_true")

    onboard_parser = subparsers.add_parser("onboard")
    onboard_parser.add_argument("scheme_id")
    onboard_parser.add_argument("--predict-date", required=True)
    onboard_parser.add_argument("--stage", default="all")
    onboard_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    onboard_parser.add_argument("--report-dir", type=Path, default=None)
    onboard_parser.add_argument("--algo-env", default="forecast_env")
    onboard_parser.add_argument("--timeout-sec", type=int, default=600)
    onboard_parser.add_argument("--api-base-url", default="http://127.0.0.1:8100")
    onboard_parser.add_argument("--authorize", default=None)

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--scheme-id", required=True)
    activate_parser.add_argument("--predict-date", default="activate")
    activate_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    activate_parser.add_argument("--report-dir", type=Path, default=None)
    activate_parser.add_argument("--authorize", default=None)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("scheme_id")
    report_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    report_parser.add_argument("--latest", action="store_true")

    auth_parser = subparsers.add_parser("auth")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    issue_parser = auth_subparsers.add_parser("issue")
    issue_parser.add_argument("--scheme-id", required=True)
    issue_parser.add_argument("--action", required=True)
    issue_parser.add_argument("--predict-date", default=None)
    issue_parser.add_argument("--issued-by", default="harness")
    issue_parser.add_argument("--expires-in", type=int, default=None, dest="expires_in")
    issue_parser.add_argument("--scheme-version", default=None, dest="scheme_version")
    issue_parser.add_argument("--harness-run-id", default=None, dest="harness_run_id")
    return parser


def _run_gate(args: argparse.Namespace) -> GateResult:
    if args.gate_name in {"input", "dry-run", "live"} and not args.predict_date:
        raise SystemExit(f"gate {args.gate_name} requires --predict-date")
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        algo_env=args.algo_env,
        timeout_sec=args.timeout_sec,
        authorization=args.authorize,
        persist_backtest=bool(getattr(args, "persist", False)),
        api_base_url=args.api_base_url,
    )
    gates = {
        "static": StaticGate(),
        "input": InputGate(),
        "unit": UnitGate(),
        "dry-run": DryRunGate(),
        "compare": CompareGate(),
        "backtest": BacktestGate(),
        "api": ApiGate(),
        "live": LiveGate(),
    }
    return gates[args.gate_name].run(ctx)


def _run_onboard_command(args: argparse.Namespace) -> OnboardReport:
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        algo_env=args.algo_env,
        timeout_sec=args.timeout_sec,
        authorization=args.authorize,
        api_base_url=args.api_base_url,
        engine_factory=create_engine_from_env,
    )
    return run_onboard(ctx, stage=args.stage)


def _run_activate(args: argparse.Namespace) -> GateResult:
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        authorization=args.authorize,
    )
    return ActivationGate().run(ctx)


def _run_report(args: argparse.Namespace) -> int:
    if not args.latest:
        raise SystemExit("report currently requires --latest")
    base = args.project_root.resolve() / "reports" / "harness" / args.scheme_id
    reports = sorted(base.glob("*/onboard_report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        print(json.dumps({"error": f"no onboard report found for {args.scheme_id}"}, ensure_ascii=False, indent=2))
        return 1
    print(reports[0].read_text(encoding="utf-8"))
    return 0


def _exit_code_for_result(result: GateResult) -> int:
    if result.status == GateStatus.BLOCKED:
        return 2
    return 0 if result.passed else 1


def _exit_code_for_report(report: OnboardReport) -> int:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return 2
    return 0 if report.overall_passed else 1


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
