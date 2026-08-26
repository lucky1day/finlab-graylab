from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date
from dataclasses import fields, is_dataclass
from getpass import getuser
from pathlib import Path
from typing import Any

from harness.operation import (
    DEFAULT_BACKTEST_START_DATE,
    EXACT_PREDICT_DATE_ACTIONS,
    build_direct_operation,
)
from harness.context import GateContext
from harness.gates.activate_gate import ActivationGate
from harness.orchestrator import onboard as run_onboard
from harness.registry import gate_for_name
from harness.result import GateResult, GateStatus, OnboardReport
from harness.signal_gap_fill import run_signal_gap_fill
from harness.signal_gap_plan import (
    SignalGapPlanError,
    plan_signal_gaps,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import create_engine_from_env, registry_scheme_id
from shared.blackbox_v2.contracts import load_metadata
from shared.blackbox_v2.intake import intake_delivery
from shared.data_bridge.refresh import DataBridgeRefreshConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _StoreOnce(argparse.Action):
    """拒绝同一精确范围参数在一次命令中重复出现。"""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be specified only once")
        setattr(namespace, self.dest, values)


def _operator_id(explicit: str | None) -> str:
    """取得非秘密的操作人标识；单人环境默认无需每次填写。"""
    value = explicit or os.environ.get("BFL_OPERATOR_ID") or getuser()
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SystemExit(
            "direct side-effect command requires a non-empty operator id; "
            "set BFL_OPERATOR_ID or pass --operator"
        )
    return value


def _gate_action(args: argparse.Namespace) -> str | None:
    """把副作用 Gate 映射为内部精确审计 action。"""
    if args.gate_name == "backtest":
        return "backtest_persist" if bool(getattr(args, "persist", False)) else None
    return {
        "lifecycle-reconcile": "blackbox_reconcile",
    }.get(args.gate_name)


def _direct_operation(
    *,
    action: str | None,
    scheme_id: str,
    config: Any,
    predict_date: str | None,
    operator: str | None,
    backtest_start_date: str | None = None,
) -> object | None:
    """把一次明确副作用命令转换为非秘密审计作用域。

    该过程不生成或保存操作者凭据。
    """
    if action is None:
        return None
    version = str(getattr(config, "scheme_version", "") or "").strip()
    if not version:
        raise SystemExit(
            "direct side-effect command requires a valid canonical scheme config"
        )
    scoped_predict_date = (
        predict_date if action in EXACT_PREDICT_DATE_ACTIONS else None
    )
    return build_direct_operation(
        scheme_id,
        action,
        scoped_predict_date,
        scheme_version=version,
        issued_by=_operator_id(operator),
        backtest_start_date=backtest_start_date,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
    if args.command == "intake-blackbox":
        scheme_dir = intake_delivery(
            args.delivery_dir,
            schemes_root=args.project_root.resolve() / "schemes",
        )
        metadata = load_metadata(
            scheme_dir / "delivery" / f"{scheme_dir.name}.json"
        )
        print(
            json.dumps(
                {
                    "scheme_id": scheme_dir.name,
                    "registry_scheme_id": registry_scheme_id(
                        metadata.scheme_id,
                        metadata.horizon,
                        metadata.target_tenor,
                    ),
                    "runtime_type": "blackbox_v2",
                    "scheme_dir": str(scheme_dir),
                    "warnings": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "signal-gap-fill":
        return _run_signal_gap_fill_command(args)
    parser.error("unsupported command")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_name", required=True)
    for gate_name in (
        "static", "dry-run", "compare", "backtest",
        "dashboard", "lifecycle-reconcile",
    ):
        item = gate_subparsers.add_parser(gate_name)
        item.add_argument("--scheme-id", required=True)
        item.add_argument(
            "--predict-date",
            required=gate_name == "backtest",
            default=(
                "dashboard"
                if gate_name == "dashboard"
                else "static"
                if gate_name in {"static", "compare"}
                else None
            ),
        )
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--algo-env", default="forecast_env")
        item.add_argument("--timeout-sec", type=int, default=600)
        if gate_name in {
            "backtest",
            "lifecycle-reconcile",
        }:
            item.add_argument(
                "--operator",
                default=None,
                help=(
                    "non-secret audit identity; defaults to "
                    "BFL_OPERATOR_ID or OS user"
                ),
            )
        if gate_name == "dashboard":
            item.add_argument(
                "--api-base-url",
                default="http://127.0.0.1:8100",
            )
        if gate_name == "backtest":
            item.add_argument("--persist", action="store_true")
            item.add_argument(
                "--backtest-start-date",
                default=DEFAULT_BACKTEST_START_DATE,
            )
    onboard_parser = subparsers.add_parser("onboard")
    onboard_parser.add_argument("scheme_id")
    onboard_parser.add_argument("--predict-date", required=True)
    onboard_parser.add_argument(
        "--stage",
        choices=("all", "native-maintenance"),
        default="all",
    )
    onboard_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    onboard_parser.add_argument("--algo-env", default="forecast_env")
    onboard_parser.add_argument("--timeout-sec", type=int, default=600)

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--scheme-id", required=True)
    activate_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    activate_parser.add_argument(
        "--operator",
        default=None,
        help="non-secret audit identity; defaults to BFL_OPERATOR_ID or OS user",
    )

    intake_parser = subparsers.add_parser("intake-blackbox")
    intake_parser.add_argument("--delivery-dir", type=Path, required=True)
    intake_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    fill_parser = subparsers.add_parser("signal-gap-fill")
    fill_parser.add_argument(
        "--predict-date",
        required=True,
        type=_iso_date,
    )
    fill_parser.add_argument(
        "--scheme-id",
        action=_StoreOnce,
        type=_base_scheme_id,
        default=None,
        help="fill one exact active base scheme id",
    )
    fill_parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
    )
    fill_parser.add_argument("--timeout-sec", type=int, default=600)

    return parser


def _run_gate(args: argparse.Namespace) -> GateResult:
    if args.gate_name in {
        "dry-run",
    } and not args.predict_date:
        raise SystemExit(f"gate {args.gate_name} requires --predict-date")
    project_root = args.project_root.resolve()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    action = _gate_action(args)
    backtest_start_date = getattr(
        args,
        "backtest_start_date",
        DEFAULT_BACKTEST_START_DATE,
    )
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        config=config,
        algo_env=args.algo_env,
        engine_factory=create_engine_from_env,
        timeout_sec=args.timeout_sec,
        operation=_direct_operation(
            action=action,
            scheme_id=args.scheme_id,
            config=config,
            predict_date=args.predict_date,
            operator=getattr(args, "operator", None),
            backtest_start_date=backtest_start_date,
        ),
        persist_backtest=bool(getattr(args, "persist", False)),
        backtest_start_date=backtest_start_date,
        api_base_url=getattr(
            args,
            "api_base_url",
            "http://127.0.0.1:8100",
        ),
    )
    gate = gate_for_name(args.gate_name, ctx=ctx)
    return gate.run(ctx)


def _run_onboard_command(args: argparse.Namespace) -> OnboardReport:
    project_root = args.project_root.resolve()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        config=config,
        algo_env=args.algo_env,
        timeout_sec=args.timeout_sec,
        engine_factory=create_engine_from_env,
    )
    return run_onboard(ctx, stage=args.stage)


def _load_config_for_dispatch(config_path: Path):
    """配置可用时提前加载；缺失或非法时交由对应 StaticGate 报告。"""
    if not config_path.is_file():
        return None
    try:
        return load_scheme_config(config_path)
    except (OSError, UnicodeError, ValueError):
        return None


def _run_activate(args: argparse.Namespace) -> GateResult:
    project_root = args.project_root.resolve()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    action = (
        "blackbox_activate"
        if getattr(config, "runtime_type", None) == "blackbox_v2"
        else "activate"
    )
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date="activate",
        project_root=project_root,
        config=config,
        operation=_direct_operation(
            action=action,
            scheme_id=args.scheme_id,
            config=config,
            predict_date=None,
            operator=args.operator,
        ),
        engine_factory=create_engine_from_env,
    )
    return ActivationGate().run(ctx)


def _exit_code_for_result(result: GateResult) -> int:
    if result.status == GateStatus.BLOCKED:
        return 2
    return 0 if result.passed else 1


def _run_signal_gap_fill_command(args: argparse.Namespace) -> int:
    """规划并一次性补齐单个日期的 active 方案信号缺口。"""
    project_root = args.project_root.resolve()
    try:
        databridge_config = DataBridgeRefreshConfig.from_env()
        plan = _plan_signal_gap_date(
            args.predict_date,
            args.scheme_id,
            databridge_config=databridge_config,
        )
    except SignalGapPlanError as exc:
        _print_signal_gap_fill_result(
            {
                "schema_version": "single-date-signal-gap-fill-v2",
                "status": "BLOCKED",
                "failure_code": exc.code,
                "predict_date": args.predict_date,
                "base_scheme_id": args.scheme_id,
                "completed": [],
                "remaining": [],
            },
        )
        return 2
    except Exception:
        _print_signal_gap_fill_result(
            {
                "schema_version": "single-date-signal-gap-fill-v2",
                "status": "BLOCKED",
                "failure_code": "SIGNAL_GAP_PLAN_INTERNAL_ERROR",
                "predict_date": args.predict_date,
                "base_scheme_id": args.scheme_id,
                "completed": [],
                "remaining": [],
            },
        )
        return 2

    counts = plan.get("counts", {})
    if (
        plan.get("status") == "BLOCKED"
        or int(counts.get("blocked", 0)) > 0
    ):
        _print_signal_gap_fill_result(
            {
                "schema_version": "single-date-signal-gap-fill-v2",
                "status": "BLOCKED",
                "failure_code": str(
                    plan.get("failure_code")
                    or "SIGNAL_GAP_PLAN_BLOCKED"
                ),
                "predict_date": args.predict_date,
                "base_scheme_id": args.scheme_id,
                "completed": [],
                "remaining": [],
            },
        )
        return 2
    if int(counts.get("actionable", 0)) == 0:
        skip_status = (
            "SKIP_NOT_DUE"
            if int(counts.get("expected", 0)) == 0
            else "SKIP_PRESENT"
        )
        _print_signal_gap_fill_result(
            {
                "schema_version": "single-date-signal-gap-fill-v2",
                "status": skip_status,
                "failure_code": None,
                "predict_date": args.predict_date,
                "base_scheme_id": args.scheme_id,
                "completed": [],
                "remaining": [],
            },
        )
        return 0

    result = run_signal_gap_fill(
        plan=plan,
        project_root=project_root,
        engine_factory=create_engine_from_env,
        databridge_config=databridge_config,
        algo_env="forecast_env",
        timeout_sec=args.timeout_sec,
    )
    _print_signal_gap_fill_result(result)
    if result.get("status") == "BLOCKED":
        return 2
    return 0 if result.get("status") in {
        "PASSED",
        "SKIP_NOT_DUE",
        "SKIP_PRESENT",
    } else 1


def _plan_signal_gap_date(
    predict_date: str,
    base_scheme_id: str | None,
    *,
    databridge_config: DataBridgeRefreshConfig,
) -> dict[str, Any]:
    engine = create_engine_from_env()
    try:
        return plan_signal_gaps(
            engine,
            predict_date=predict_date,
            base_scheme_id=base_scheme_id,
            databridge_config=databridge_config,
        )
    finally:
        engine.dispose()


def _print_signal_gap_fill_result(report: Mapping[str, Any]) -> None:
    print(json.dumps(dict(report), ensure_ascii=False, indent=2))


def _iso_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError(
            "predict date must use YYYY-MM-DD"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "predict date must be a valid calendar date"
        ) from exc
    return value


def _base_scheme_id(value: str) -> str:
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]*", value)
        or re.fullmatch(
            r"[a-z][a-z0-9_]*__h\d+__(?:1Y|3Y|5Y|7Y|10Y)",
            value,
        )
        is not None
        or "," in value
    ):
        raise argparse.ArgumentTypeError(
            "scheme id must be one exact base scheme id"
        )
    return value


def _exit_code_for_report(report: OnboardReport) -> int:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return 2
    return 0 if report.overall_passed else 1


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, GateStatus):
        return value.value
    if is_dataclass(value):
        payload = {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
        if isinstance(value, GateResult):
            payload["passed"] = value.passed
        elif isinstance(value, OnboardReport):
            payload["overall_passed"] = value.overall_passed
            payload["control_plane_persisted"] = value.control_plane_persisted
        return payload
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
