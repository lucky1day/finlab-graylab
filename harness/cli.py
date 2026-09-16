from __future__ import annotations

import argparse
import json
import os
import re
import stat
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
from harness.registry import gate_for_name
from harness.result import GateResult, GateStatus
from harness.signal_gap_fill import run_signal_gap_fill
from harness.signal_gap_plan import (
    SignalGapPlanError,
    plan_signal_gap_target_range,
    plan_signal_gaps,
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
    for gate_name in ("backtest", "dashboard", "data-consistency"):
        item = gate_subparsers.add_parser(gate_name)
        if gate_name in {"dashboard", "data-consistency"}:
            item.add_argument(
                "--scheme-id",
                required=True,
                action="append",
                help="base scheme id; repeat for one shared HTTP reconciliation",
            )
        else:
            item.add_argument("--scheme-id", required=True)
        item.add_argument(
            "--predict-date",
            required=gate_name == "backtest",
            default=(
                gate_name
                if gate_name in {"dashboard", "data-consistency"}
                else None
            ),
        )
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
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
        if gate_name in {"dashboard", "data-consistency"}:
            item.add_argument(
                "--api-base-url",
                default="http://127.0.0.1:8100",
            )
            session_input = item.add_mutually_exclusive_group(required=True)
            session_input.add_argument(
                "--session-file",
                type=Path,
                help="private file containing only the opaque session token",
            )
            session_input.add_argument(
                "--session-fd",
                type=int,
                help="open file descriptor containing only the opaque session token",
            )
        if gate_name == "backtest":
            item.add_argument("--persist", action="store_true")
            item.add_argument(
                "--backtest-start-date",
                default=DEFAULT_BACKTEST_START_DATE,
            )
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

    return parser


def _run_gate(args: argparse.Namespace) -> GateResult:
    project_root = args.project_root.resolve()
    if args.gate_name in {"dashboard", "data-consistency"}:
        scheme_ids = tuple(args.scheme_id)
        if len(set(scheme_ids)) != len(scheme_ids):
            raise SystemExit("HTTP reconciliation scheme ids must not be repeated")
        scheme_id = scheme_ids[0]
        session_token = _read_dashboard_session(
            session_file=args.session_file,
            session_fd=args.session_fd,
        )
    else:
        scheme_ids = ()
        scheme_id = args.scheme_id
        session_token = None
    config = _load_config_for_dispatch(
        project_root / "schemes" / scheme_id / "config.yaml"
    )
    action = _gate_action(args)
    backtest_start_date = getattr(
        args,
        "backtest_start_date",
        DEFAULT_BACKTEST_START_DATE,
    )
    ctx = GateContext(
        scheme_id=scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        config=config,
        engine_factory=create_engine_from_env,
        timeout_sec=args.timeout_sec,
        operation=_direct_operation(
            action=action,
            scheme_id=scheme_id,
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
        dashboard_scheme_ids=scheme_ids,
        dashboard_session_token=session_token,
    )
    gate = gate_for_name(args.gate_name, ctx=ctx)
    return gate.run(ctx)


def _read_dashboard_session(
    *,
    session_file: Path | None,
    session_fd: int | None,
) -> str:
    """从私有文件或调用方传入的文件描述符读取 Dashboard 会话。"""
    if (session_file is None) == (session_fd is None):
        raise SystemExit(
            "dashboard gate requires exactly one of --session-file or --session-fd"
        )
    if session_file is not None:
        if not session_file.is_absolute():
            raise SystemExit("dashboard session file must be an absolute path")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SystemExit("dashboard session file requires O_NOFOLLOW support")
        try:
            descriptor = os.open(session_file, flags | nofollow)
        except OSError as exc:
            raise SystemExit("dashboard session file is unavailable") from exc
    else:
        if session_fd is None or session_fd < 0:
            raise SystemExit("dashboard session fd must be non-negative")
        try:
            descriptor = os.dup(session_fd)
        except OSError as exc:
            raise SystemExit("dashboard session fd is unavailable") from exc
        try:
            os.set_blocking(descriptor, False)
        except OSError as exc:
            os.close(descriptor)
            raise SystemExit("dashboard session fd is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if session_file is not None:
            allowed_type = stat.S_ISREG(details.st_mode)
            type_error = "dashboard session file must be an owned regular file"
        else:
            allowed_type = stat.S_ISREG(details.st_mode) or stat.S_ISFIFO(
                details.st_mode
            )
            type_error = (
                "dashboard session fd must reference an owned regular file or pipe"
            )
        if not allowed_type or details.st_uid != os.geteuid():
            raise SystemExit(
                type_error
            )
        if stat.S_ISREG(details.st_mode) and stat.S_IMODE(details.st_mode) & 0o077:
            raise SystemExit("dashboard session file must not be group/world accessible")
        chunks: list[bytes] = []
        remaining = 1025
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1025))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except BlockingIOError as exc:
        raise SystemExit(
            "dashboard session fd is not ready or is incomplete"
        ) from exc
    except OSError as exc:
        raise SystemExit("dashboard session input is unreadable") from exc
    finally:
        os.close(descriptor)
    if len(raw) > 1024:
        raise SystemExit("dashboard session input is too large")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("dashboard session input must be valid UTF-8") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise SystemExit("dashboard session input must contain exactly one token")
    if len(value) > 512 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise SystemExit("dashboard session token has an invalid format")
    return value


def _load_config_for_dispatch(config_path: Path):
    """严格加载 canonical，非法配置不得回退到旧 Native 验证。"""
    try:
        return load_scheme_config(config_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"canonical config failed strict discovery: {exc}") from exc


def _run_activate(args: argparse.Namespace) -> GateResult:
    project_root = args.project_root.resolve()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    if config.runtime_type != "blackbox_v2":
        raise SystemExit("Native activation is retired; W4 remains on its fixed version")
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date="activate",
        project_root=project_root,
        config=config,
        operation=_direct_operation(
            action="blackbox_activate",
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
        return payload
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
