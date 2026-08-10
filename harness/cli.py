from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import date, datetime, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from harness.authorization import (
    DEFAULT_BACKTEST_START_DATE,
    EXACT_PREDICT_DATE_ACTIONS,
    HARNESS_RUN_SCOPED_ACTIONS,
    SIDE_EFFECT_ACTIONS,
    issue_token,
)
from harness.context import GateContext
from harness.gates.activate_gate import ActivationGate
from harness.gates.backtest_gate import BacktestGate
from harness.gates.compare_gate import CompareGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.orchestrator import onboard as run_onboard
from harness.registry import gate_for_name
from harness.result import GateResult, GateStatus, OnboardReport
from harness.signal_gap_fill import run_signal_gap_fill
from harness.signal_gap_plan import (
    SignalGapPlanError,
    plan_signal_gaps,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import create_engine_from_env
from shared.blackbox_v2.contracts import load_metadata
from shared.blackbox_v2.intake import intake_delivery, intake_warnings
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "auth" and args.auth_command == "issue":
        if args.action in EXACT_PREDICT_DATE_ACTIONS and args.predict_date is None:
            parser.error(
                f"auth issue --action {args.action} requires --predict-date"
            )
        if args.action not in EXACT_PREDICT_DATE_ACTIONS and args.predict_date is not None:
            parser.error(
                f"auth issue --action {args.action} does not accept --predict-date"
            )
        if not isinstance(args.scheme_version, str) or not args.scheme_version.strip():
            parser.error("auth issue requires non-empty --scheme-version")
        if not isinstance(args.issued_by, str) or not args.issued_by.strip():
            parser.error("auth issue requires non-empty --issued-by")
        if args.action in HARNESS_RUN_SCOPED_ACTIONS and (
            not isinstance(args.harness_run_id, str)
            or not args.harness_run_id.strip()
        ):
            parser.error(
                f"auth issue --action {args.action} requires --harness-run-id"
            )
        token = issue_token(
            args.scheme_id,
            args.action,
            args.predict_date,
            scheme_version=args.scheme_version,
            harness_run_id=args.harness_run_id,
            ttl_seconds=args.expires_in,
            issued_by=args.issued_by,
            backtest_start_date=args.backtest_start_date,
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
    if args.command == "intake-blackbox":
        scheme_dir = intake_delivery(
            args.delivery_dir,
            schemes_root=args.project_root.resolve() / "schemes",
            runtime_profile=args.runtime_profile,
            data_schema_version=args.data_schema_version,
            platform_inputs=args.platform_input,
        )
        metadata = load_metadata(
            scheme_dir / "delivery" / f"{scheme_dir.name}.json"
        )
        print(
            json.dumps(
                {
                    "scheme_id": scheme_dir.name,
                    "runtime_type": "blackbox_v2",
                    "scheme_dir": str(scheme_dir),
                    "warnings": intake_warnings(metadata),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "signal-gap-plan":
        try:
            databridge_config = DataBridgeRefreshConfig.from_env()
            engine = create_engine_from_env()
            try:
                plan = plan_signal_gaps(
                    engine,
                    predict_date=args.predict_date,
                    base_scheme_id=args.scheme_id,
                    databridge_config=databridge_config,
                )
            finally:
                engine.dispose()
        except SignalGapPlanError as exc:
            print(
                json.dumps(
                    {
                        "schema_version":
                            "single-date-active-live-gap-plan-error-v1",
                        "status": "BLOCKED",
                        "failure_code": exc.code,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        except Exception:
            print(
                json.dumps(
                    {
                        "schema_version":
                            "single-date-active-live-gap-plan-error-v1",
                        "status": "ERROR",
                        "failure_code":
                            "SIGNAL_GAP_PLAN_INTERNAL_ERROR",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return (
            1
            if plan.get("status") == "BLOCKED"
            or int(plan.get("counts", {}).get("blocked", 0))
            else 0
        )
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
        "static", "input", "unit", "dry-run", "compare", "backtest",
        "dashboard", "draft-register", "shadow-register", "live",
        "lifecycle-reconcile", "revision-activate",
    ):
        item = gate_subparsers.add_parser(gate_name)
        item.add_argument("--scheme-id", required=True)
        item.add_argument(
            "--predict-date",
            default=(
                "dashboard"
                if gate_name == "dashboard"
                else "static"
                if gate_name in {"static", "unit", "compare", "backtest"}
                else None
            ),
        )
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--report-dir", type=Path, default=None)
        item.add_argument("--algo-env", default="forecast_env")
        item.add_argument("--timeout-sec", type=int, default=600)
        item.add_argument("--authorize", default=None)
        if gate_name == "dashboard":
            item.add_argument(
                "--api-base-url",
                default="http://127.0.0.1:8100",
            )
        item.add_argument("--prediction-phase", choices=("gray_live", "scheduled_live"), default=None)
        if gate_name == "backtest":
            item.add_argument("--persist", action="store_true")
            item.add_argument("--sample-size", type=int, default=None)
            item.add_argument(
                "--backtest-start-date",
                default=DEFAULT_BACKTEST_START_DATE,
            )
    onboard_parser = subparsers.add_parser("onboard")
    onboard_parser.add_argument("scheme_id")
    onboard_parser.add_argument("--predict-date", required=True)
    onboard_parser.add_argument("--stage", default="all")
    onboard_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    onboard_parser.add_argument("--report-dir", type=Path, default=None)
    onboard_parser.add_argument("--algo-env", default="forecast_env")
    onboard_parser.add_argument("--timeout-sec", type=int, default=600)
    onboard_parser.add_argument("--authorize", default=None)
    onboard_parser.add_argument("--prediction-phase", choices=("gray_live", "scheduled_live"), default=None)
    onboard_parser.add_argument("--check-only", action="store_true")

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

    intake_parser = subparsers.add_parser("intake-blackbox")
    intake_parser.add_argument("--delivery-dir", type=Path, required=True)
    intake_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    intake_parser.add_argument("--runtime-profile", default="blackbox-v2-v1")
    intake_parser.add_argument("--data-schema-version", default="data-bridge-v1")
    intake_parser.add_argument(
        "--platform-input",
        action="append",
        default=None,
        dest="platform_input",
    )

    gap_parser = subparsers.add_parser("signal-gap-plan")
    gap_parser.add_argument(
        "--predict-date",
        required=True,
        type=_iso_date,
    )
    gap_parser.add_argument(
        "--scheme-id",
        action=_StoreOnce,
        type=_base_scheme_id,
        default=None,
        help="restrict the plan to one exact active base scheme id",
    )

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

    auth_parser = subparsers.add_parser("auth")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    issue_parser = auth_subparsers.add_parser("issue")
    issue_parser.add_argument("--scheme-id", required=True)
    issue_parser.add_argument(
        "--action",
        required=True,
        choices=sorted(SIDE_EFFECT_ACTIONS),
    )
    issue_parser.add_argument("--predict-date", default=None)
    issue_parser.add_argument("--issued-by", default=None)
    issue_parser.add_argument("--expires-in", type=int, default=900, dest="expires_in")
    issue_parser.add_argument("--scheme-version", default=None, dest="scheme_version")
    issue_parser.add_argument("--harness-run-id", default=None, dest="harness_run_id")
    issue_parser.add_argument(
        "--backtest-start-date",
        default=DEFAULT_BACKTEST_START_DATE,
    )
    return parser


def _run_gate(args: argparse.Namespace) -> GateResult:
    if args.gate_name in {
        "input",
        "dry-run",
        "draft-register",
        "shadow-register",
        "revision-activate",
        "live",
    } and not args.predict_date:
        raise SystemExit(f"gate {args.gate_name} requires --predict-date")
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        config=config,
        algo_env=args.algo_env,
        engine_factory=create_engine_from_env,
        timeout_sec=args.timeout_sec,
        authorization=args.authorize,
        prediction_phase=getattr(args, "prediction_phase", None),
        persist_backtest=bool(getattr(args, "persist", False)),
        backtest_sample_size=(
            int(args.sample_size)
            if getattr(args, "sample_size", None) is not None
            else None
        ),
        backtest_start_date=getattr(
            args,
            "backtest_start_date",
            DEFAULT_BACKTEST_START_DATE,
        ),
        api_base_url=getattr(
            args,
            "api_base_url",
            "http://127.0.0.1:8100",
        ),
    )
    gate = gate_for_name(args.gate_name, ctx=ctx)
    try:
        return gate.run(ctx)
    finally:
        if getattr(config, "runtime_type", "native_adapter") == "blackbox_v2":
            from harness.blackbox_v2.gates import cleanup_runtime_input

            cleanup_runtime_input(ctx)


def _run_onboard_command(args: argparse.Namespace) -> OnboardReport:
    if args.check_only and (
        args.stage.strip().lower() != "all"
        or args.authorize is not None
        or args.prediction_phase is not None
    ):
        raise SystemExit(
            "--check-only requires --stage all and forbids authorization "
            "or prediction side-effect phases"
        )
    project_root = args.project_root.resolve()
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        config=config,
        algo_env=args.algo_env,
        timeout_sec=args.timeout_sec,
        authorization=args.authorize,
        prediction_phase=getattr(args, "prediction_phase", None),
        engine_factory=create_engine_from_env,
        check_only=bool(args.check_only),
    )
    return run_onboard(
        ctx,
        stage=args.stage,
        check_only=bool(args.check_only),
    )


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
    report_dir = args.report_dir or project_root / "reports" / "harness" / args.scheme_id / _timestamp()
    config = _load_config_for_dispatch(project_root / "schemes" / args.scheme_id / "config.yaml")
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date=args.predict_date,
        project_root=project_root,
        report_dir=report_dir,
        config=config,
        authorization=args.authorize,
        engine_factory=create_engine_from_env,
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


def _run_signal_gap_fill_command(args: argparse.Namespace) -> int:
    """规划并一次性补齐单个日期的 active 方案信号缺口。"""
    project_root = args.project_root.resolve()
    report_root = (
        project_root
        / "reports"
        / "harness"
        / "signal-gap-fill"
    )
    try:
        report_root.mkdir(parents=True)
    except FileExistsError:
        if not report_root.is_dir():
            raise
    report_dir = Path(
        tempfile.mkdtemp(prefix=f"{_timestamp()}-", dir=report_root)
    )
    plan_path = report_dir / "signal_gap_plan.json"
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
            report_dir=report_dir,
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
            report_dir=report_dir,
        )
        return 2
    _write_json_file(plan_path, plan)

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
            report_dir=report_dir,
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
            report_dir=report_dir,
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
    _print_signal_gap_fill_result(
        result,
        report_dir=report_dir,
    )
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


def _print_signal_gap_fill_result(
    report: Mapping[str, Any],
    *,
    report_dir: Path | None = None,
) -> None:
    payload = dict(report)
    if report_dir is not None:
        payload["report_dir"] = str(report_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _write_json_file(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid JSON file {path}: {exc}") from exc


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
