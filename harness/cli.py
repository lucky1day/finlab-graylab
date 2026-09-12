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
    plan_signal_gap_target_range,
    plan_signal_gaps,
)
from harness.same_id_runtime_upgrade import (
    build_same_id_preflight,
    execute_same_id_upgrade,
    parse_w3b_harness_run_ids,
)
from harness.w3b_prepare import build_w3b_prepare_preflight, execute_w3b_prepare
from harness.w2_reclaim_prepare import build_w2_reclaim_prepare_preflight, execute_w2_reclaim_prepare
from harness.w3a_reclaim_prepare import build_w3a_reclaim_prepare_preflight, execute_w3a_reclaim_prepare
from harness.w2_live_preservation import build_w2_live_preservation_preflight, execute_w2_live_preservation
from harness.writer_reclaim import (
    build_writer_reclaim_preflight, execute_writer_reclaim, parse_reclaim_run_ids,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import create_engine_from_env
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
    return None


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
    if args.command == "rebuild-blackbox-state":
        from harness.blackbox_v2.state import rebuild_blackbox_state
        report = rebuild_blackbox_state(
            project_root=args.project_root.resolve(), scheme_id=args.scheme_id,
            predict_date=args.predict_date, expected_scheme_version=args.expected_scheme_version,
            approved_by=args.approved_by,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
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
    if args.command == "intake-blackbox":
        scheme_dir = intake_delivery(
            args.delivery_dir,
            schemes_root=args.project_root.resolve() / "schemes",
            incremental_state=args.incremental_state,
        )
        print(
            json.dumps(
                {
                    "scheme_id": scheme_dir.name,
                    "runtime_type": "blackbox_v2",
                    "scheme_dir": str(scheme_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "signal-gap-fill":
        return _run_signal_gap_fill_command(args)
    if args.command == "migrate-native-successor":
        result = _run_native_successor_migration_command(args)
        print(
            json.dumps(
                _jsonable(result),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    parser.error("unsupported command")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rebuild = subparsers.add_parser("rebuild-blackbox-state")
    rebuild.add_argument("--scheme-id", required=True)
    rebuild.add_argument("--predict-date", required=True)
    rebuild.add_argument("--expected-scheme-version", required=True)
    rebuild.add_argument("--approved-by", required=True)
    rebuild.add_argument("--project-root", type=Path, default=Path.cwd())

    gate_parser = subparsers.add_parser("gate")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_name", required=True)
    for gate_name in (
        "static", "dry-run", "compare", "backtest",
        "dashboard",
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
        if gate_name == "backtest":
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
    intake_parser.add_argument(
        "--incremental-state",
        action="store_true",
        help="enable algorithm-owned private incremental state for this delivery",
    )

    fill_parser = subparsers.add_parser("signal-gap-fill")
    fill_scope = fill_parser.add_mutually_exclusive_group(required=True)
    fill_scope.add_argument(
        "--predict-date",
        type=_iso_date,
    )
    fill_scope.add_argument(
        "--target-date-from",
        type=_iso_date,
    )
    fill_parser.add_argument(
        "--target-date-before",
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

    migration_parser = subparsers.add_parser("migrate-native-successor")
    migration_actions = migration_parser.add_subparsers(
        dest="migration_action",
        required=True,
    )
    for action in ("preflight", "prepare", "cutover", "rollback", "preserve-live"):
        item = migration_actions.add_parser(action)
        item.add_argument("--wave", choices=["W1A", "W1B", "W2", "W3A", "W3B", "W3C", "W3D"], required=True)
        item.add_argument("--scheme-id", action="append", help="W3C/W3D only: select original IDs within the fixed wave")
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--reference-project-root", type=Path, required=True)
        item.add_argument("--rollback-project-root", type=Path, help="W1A/W3A: actual pre-cutover release, not Native reference")
        if action not in {"prepare", "preserve-live"}:
            item.add_argument("--harness-run-id", action="append", required=action != "preflight", metavar="BASE=RUN")
        item.add_argument("--expected-database-name", required=True)
        item.add_argument("--expected-server-uuid", required=True)
        if action == "preflight":
            item.add_argument("--action", choices=["prepare", "cutover", "rollback", "preserve-live"], default="cutover")
        if action in {"preflight", "prepare"}:
            item.add_argument("--predict-date", help="Preparation only; W1B uses an already-due Saturday, W1A/W2/W3A a weekday trading date")
        item.add_argument("--work-dir", type=Path, required=action == "prepare", help="W3A cutover/rollback: completed preparation directory")
        if action != "preflight":
            item.add_argument("--expected-plan-sha256", required=True)
            item.add_argument("--approved-by", required=True)

    return parser


def _run_native_successor_migration_command(
    args: argparse.Namespace,
) -> dict[str, object]:
    """同 ID 升级及已跨 ID Writer 回收；不恢复旧跨 ID 激活路由。"""
    if args.wave in {"W3C", "W3D"}:
        from harness.single_request_prepare import run_single_request_command
        return run_single_request_command(args)
    if getattr(args, "scheme_id", None):
        raise ValueError("scheme-id selection is restricted to W3C/W3D")
    if args.wave == "W1A":
        return _run_w1a_migration_command(args)
    project_root = args.project_root.resolve()
    preserving = args.migration_action == "preserve-live" or (
        args.migration_action == "preflight" and args.action == "preserve-live"
    )
    w3a_options = {}
    if args.wave == "W3A" and not preserving:
        if args.rollback_project_root is None:
            raise ValueError("W3A requires explicit rollback-project-root distinct from Native reference")
        w3a_options["rollback_project_root"] = args.rollback_project_root.resolve()
    elif args.rollback_project_root is not None:
        raise ValueError("rollback-project-root is restricted to W3A")
    if preserving:
        if (args.wave not in {"W2", "W3A"} or getattr(args, "harness_run_id", None)
                or getattr(args, "predict_date", None) or args.rollback_project_root is not None
                or (args.wave == "W3A" and args.work_dir is not None)):
            raise ValueError("preserve-live only accepts fixed W2/W3A; no Harness, prediction date or rollback options")
        engine = create_engine_from_env()
        try:
            kwargs = dict(project_root=project_root, reference_project_root=args.reference_project_root.resolve(),
                          expected_database_name=args.expected_database_name, expected_server_uuid=args.expected_server_uuid)
            if args.wave == "W3A":
                kwargs["wave"] = args.wave
            if args.migration_action == "preflight":
                return build_w2_live_preservation_preflight(engine, **kwargs)
            return execute_w2_live_preservation(engine, **kwargs, expected_plan_sha256=args.expected_plan_sha256,
                                               approved_by=args.approved_by)
        finally:
            engine.dispose()
    preparing = args.migration_action == "prepare" or (
        args.migration_action == "preflight" and args.action == "prepare"
    )
    if preparing and getattr(args, "harness_run_id", None):
        raise ValueError("prepare creates its own real Harness runs; do not supply run IDs")
    reclaiming = args.wave in {"W1B", "W2", "W3A"}
    if getattr(args, "predict_date", None) and not (preparing and args.wave in {"W1B", "W2", "W3A"}):
        raise ValueError("predict-date is restricted to W1B/W2/W3A preparation")
    if args.wave == "W3A" and not preparing:
        if args.work_dir is None:
            raise ValueError("W3A cutover/rollback requires completed preparation work-dir")
        w3a_options["work_dir"] = args.work_dir
    run_ids = None if preparing else (
        parse_reclaim_run_ids(args.wave, args.harness_run_id or []) if reclaiming
        else parse_w3b_harness_run_ids(args.harness_run_id or [])
    )
    engine = create_engine_from_env()
    try:
        if preparing:
            kwargs = dict(project_root=project_root,
                          reference_project_root=args.reference_project_root.resolve(),
                          expected_database_name=args.expected_database_name,
                          expected_server_uuid=args.expected_server_uuid)
            if args.wave in {"W1B", "W2"}:
                kwargs["predict_date"] = args.predict_date
                if args.wave == "W1B":
                    kwargs["wave"] = args.wave
                if args.migration_action == "preflight":
                    return build_w2_reclaim_prepare_preflight(engine, **kwargs)
                return execute_w2_reclaim_prepare(engine, **kwargs,
                    expected_plan_sha256=args.expected_plan_sha256, approved_by=args.approved_by,
                    work_dir=args.work_dir)
            if args.wave == "W3A":
                kwargs.update(w3a_options, predict_date=args.predict_date)
                if args.migration_action == "preflight":
                    return build_w3a_reclaim_prepare_preflight(engine, **kwargs)
                return execute_w3a_reclaim_prepare(engine, **kwargs,
                    expected_plan_sha256=args.expected_plan_sha256, approved_by=args.approved_by,
                    work_dir=args.work_dir)
            if args.migration_action == "preflight":
                return build_w3b_prepare_preflight(engine, **kwargs)
            return execute_w3b_prepare(engine, **kwargs,
                expected_plan_sha256=args.expected_plan_sha256, approved_by=args.approved_by,
                work_dir=args.work_dir)
        if args.migration_action == "preflight":
            preflight = build_writer_reclaim_preflight if reclaiming else build_same_id_preflight
            return preflight(
                engine,
                project_root=project_root,
                reference_project_root=args.reference_project_root.resolve(),
                wave=args.wave,
                harness_run_ids=run_ids,
                action=args.action,
                expected_database_name=args.expected_database_name,
                expected_server_uuid=args.expected_server_uuid,
                **w3a_options,
            )
        execute = execute_writer_reclaim if reclaiming else execute_same_id_upgrade
        return execute(
            engine,
            project_root=project_root,
            reference_project_root=args.reference_project_root.resolve(),
            wave=args.wave,
            harness_run_ids=run_ids,
            action=args.migration_action,
            expected_plan_sha256=args.expected_plan_sha256,
            approved_by=args.approved_by,
            expected_database_name=args.expected_database_name,
            expected_server_uuid=args.expected_server_uuid,
            **w3a_options,
        )
    finally:
        engine.dispose()


def _run_w1a_migration_command(args: argparse.Namespace) -> dict[str, object]:
    """W1A 仅分派两 base 回收，不扩展旧单目标批次。"""
    from harness.w1a_reclaim_prepare import build_w1a_reclaim_prepare_preflight, execute_w1a_reclaim_prepare
    from harness.w1a_writer_reclaim import build_w1a_reclaim_preflight, execute_w1a_reclaim, parse_w1a_run_ids

    action = args.action if args.migration_action == "preflight" else args.migration_action
    if action not in {"prepare", "cutover", "rollback"}:
        raise ValueError("W1A does not import or delete historical facts")
    if args.rollback_project_root is None:
        raise ValueError("W1A requires explicit rollback-project-root distinct from Native reference")
    if action != "prepare" and (args.work_dir is None or getattr(args, "predict_date", None)):
        raise ValueError("W1A cutover/rollback requires completed work-dir and no prediction date")
    if action == "prepare" and getattr(args, "harness_run_id", None):
        raise ValueError("W1A preparation creates its own real Harness runs")
    kwargs = dict(project_root=args.project_root.resolve(), reference_project_root=args.reference_project_root.resolve(),
                  rollback_project_root=args.rollback_project_root.resolve(), expected_database_name=args.expected_database_name,
                  expected_server_uuid=args.expected_server_uuid)
    if action == "prepare":
        kwargs["predict_date"] = args.predict_date
        operation = build_w1a_reclaim_prepare_preflight if args.migration_action == "preflight" else execute_w1a_reclaim_prepare
    else:
        kwargs.update(harness_run_ids=parse_w1a_run_ids(args.harness_run_id or []), action=action, work_dir=args.work_dir)
        operation = build_w1a_reclaim_preflight if args.migration_action == "preflight" else execute_w1a_reclaim
    if args.migration_action != "preflight":
        kwargs.update(expected_plan_sha256=args.expected_plan_sha256, approved_by=args.approved_by)
        if action == "prepare":
            kwargs["work_dir"] = args.work_dir
    engine = create_engine_from_env()
    try:
        return operation(engine, **kwargs)
    finally:
        engine.dispose()


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
    """规划并补齐单日或一个 Blackbox 周频 target 区间。"""
    project_root = args.project_root.resolve()
    range_mode = args.target_date_from is not None
    if range_mode:
        if args.target_date_before is None or args.scheme_id is None:
            raise SystemExit(
                "target range requires --target-date-before and --scheme-id"
            )
        scope = {
            "predict_date": None,
            "target_date_from": args.target_date_from,
            "target_date_before": args.target_date_before,
            "base_scheme_id": args.scheme_id,
        }
    else:
        if args.target_date_before is not None:
            raise SystemExit(
                "--target-date-before requires --target-date-from"
            )
        scope = {
            "predict_date": args.predict_date,
            "base_scheme_id": args.scheme_id,
        }
    report_schema = (
        "target-range-signal-gap-fill-v1"
        if range_mode
        else "single-date-signal-gap-fill-v2"
    )
    try:
        databridge_config = DataBridgeRefreshConfig.from_env()
        if range_mode:
            plan = _plan_signal_gap_range(
                args.target_date_from,
                args.target_date_before,
                args.scheme_id,
                databridge_config=databridge_config,
                project_root=project_root,
            )
        else:
            plan = _plan_signal_gap_date(
                args.predict_date,
                args.scheme_id,
                databridge_config=databridge_config,
                project_root=project_root,
            )
    except SignalGapPlanError as exc:
        _print_signal_gap_fill_result(
            {
                "schema_version": report_schema,
                "status": "BLOCKED",
                "failure_code": exc.code,
                **scope,
                "completed": [],
                "remaining": [],
            },
        )
        return 2
    except Exception:
        _print_signal_gap_fill_result(
            {
                "schema_version": report_schema,
                "status": "BLOCKED",
                "failure_code": "SIGNAL_GAP_PLAN_INTERNAL_ERROR",
                **scope,
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
                "schema_version": report_schema,
                "status": "BLOCKED",
                "failure_code": str(
                    plan.get("failure_code")
                    or "SIGNAL_GAP_PLAN_BLOCKED"
                ),
                **scope,
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
                "schema_version": report_schema,
                "status": skip_status,
                "failure_code": None,
                **scope,
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
    project_root: Path,
) -> dict[str, Any]:
    engine = create_engine_from_env()
    try:
        return plan_signal_gaps(
            engine,
            predict_date=predict_date,
            base_scheme_id=base_scheme_id,
            databridge_config=databridge_config,
            project_root=project_root,
        )
    finally:
        engine.dispose()


def _plan_signal_gap_range(
    target_date_from: str,
    target_date_before: str,
    base_scheme_id: str,
    *,
    databridge_config: DataBridgeRefreshConfig,
    project_root: Path,
) -> dict[str, Any]:
    engine = create_engine_from_env()
    try:
        return plan_signal_gap_target_range(
            engine,
            target_date_from=target_date_from,
            target_date_before=target_date_before,
            base_scheme_id=base_scheme_id,
            databridge_config=databridge_config,
            project_root=project_root,
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
    if isinstance(value, date):
        return value.isoformat()
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
