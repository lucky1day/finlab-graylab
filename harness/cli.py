from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from harness.authorization import (
    DEFAULT_BACKTEST_START_DATE,
    EXACT_PREDICT_DATE_ACTIONS,
    NATIVE_LEGACY_ADMISSION_ATTEST_ACTION,
    NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
    issue_token,
)
from harness.daily_real_replay_operator import (
    DailyRealReplayPreflightError,
    run_real_replay_execute,
    run_real_replay_preflight,
)
from harness.context import GateContext
from harness.gates.activate_gate import ActivationGate
from harness.gates.api_gate import ApiGate
from harness.gates.api_readiness_gate import ApiReadinessGate
from harness.gates.backtest_gate import BacktestGate
from harness.gates.compare_gate import CompareGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.legacy_native_admission_attestation import (
    run_legacy_native_admission_attestation,
)
from harness.orchestrator import onboard as run_onboard
from harness.registry import gate_for_name
from harness.result import GateResult, GateStatus, OnboardReport
from harness.signal_gap_plan import SignalGapPlanError, plan_signal_gaps
from harness.signal_gap_native_artifact import (
    SignalGapNativeArtifactRegistrationError,
    prepare_signal_gap_native_artifact,
    register_signal_gap_native_artifact,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import create_engine_from_env
from shared.blackbox_v2.contracts import load_metadata
from shared.blackbox_v2.intake import intake_delivery, intake_warnings
from shared.data_bridge.refresh import DataBridgeRefreshConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if (
        args.command == "daily-real-replay"
        and args.execute_only
        and args.policy_version is not None
    ):
        parser.error(
            "daily-real-replay --policy-version is only valid with "
            "--check-only"
        )
    if args.command == "auth" and args.auth_command == "issue":
        if args.action in EXACT_PREDICT_DATE_ACTIONS and args.predict_date is None:
            parser.error(
                f"auth issue --action {args.action} requires --predict-date"
            )
        if args.action == "activate":
            if not isinstance(args.scheme_version, str) or not args.scheme_version.strip():
                parser.error(
                    "auth issue --action activate requires non-empty --scheme-version"
                )
            if not isinstance(args.issued_by, str) or not args.issued_by.strip():
                parser.error(
                    "auth issue --action activate requires non-empty --issued-by"
                )
        if args.action == "blackbox_revision_activate":
            required = {
                "--scheme-version": args.scheme_version,
                "--harness-run-id": args.harness_run_id,
                "--issued-by": args.issued_by,
            }
            missing = [flag for flag, value in required.items() if value is None]
            if missing:
                parser.error(
                    "auth issue --action blackbox_revision_activate requires "
                    f"{', '.join(missing)}"
                )
        if args.action == NATIVE_LEGACY_ADMISSION_ATTEST_ACTION:
            if args.scheme_id != NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID:
                parser.error(
                    "auth issue --action "
                    f"{NATIVE_LEGACY_ADMISSION_ATTEST_ACTION} requires "
                    f"--scheme-id {NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID}"
                )
            required = {
                "--scheme-version": args.scheme_version,
                "--harness-run-id": args.harness_run_id,
                "--issued-by": args.issued_by,
                "--expires-in": args.expires_in,
            }
            missing = [flag for flag, value in required.items() if value is None]
            if missing:
                parser.error(
                    "auth issue --action "
                    f"{NATIVE_LEGACY_ADMISSION_ATTEST_ACTION} requires "
                    f"{', '.join(missing)}"
                )
            if (
                not isinstance(args.scheme_version, str)
                or not args.scheme_version.strip()
                or not isinstance(args.harness_run_id, str)
                or not args.harness_run_id.strip()
                or not isinstance(args.issued_by, str)
                or not args.issued_by.strip()
                or isinstance(args.expires_in, bool)
                or args.expires_in <= 0
                or args.expires_in > 900
            ):
                parser.error(
                    "auth issue --action "
                    f"{NATIVE_LEGACY_ADMISSION_ATTEST_ACTION} requires non-empty "
                    "--scheme-version, --harness-run-id, --issued-by and a positive "
                    "--expires-in no more than 900"
                )
        issued_by = args.issued_by if args.issued_by is not None else "harness"
        signal_gap_kwargs: dict[str, Any] = {}
        if args.action == "signal_gap_fill_write":
            required = {
                "--scheme-version": args.scheme_version,
                "--plan-sha256": args.plan_sha256,
                "--base-scheme-id": args.base_scheme_id,
                "--target-keys-json": args.target_keys_json,
                "--source-authority-json": args.source_authority_json,
            }
            missing = [
                flag for flag, value in required.items()
                if value is None
            ]
            if missing:
                parser.error(
                    "auth issue --action signal_gap_fill_write "
                    f"requires {', '.join(missing)}"
                )
            signal_gap_kwargs = {
                "plan_sha256": args.plan_sha256,
                "base_scheme_id": args.base_scheme_id,
                "target_keys": _read_json_file(
                    args.target_keys_json
                ),
                "source_authority": _read_json_file(
                    args.source_authority_json
                ),
            }
        elif args.action == "signal_gap_native_artifact_register":
            if args.source_authority_json is None:
                parser.error(
                    "auth issue --action "
                    "signal_gap_native_artifact_register requires "
                    "--source-authority-json"
                )
            signal_gap_kwargs = {
                "source_authority": _read_json_file(
                    args.source_authority_json
                ),
            }
        token = issue_token(
            args.scheme_id,
            args.action,
            args.predict_date,
            scheme_version=args.scheme_version,
            harness_run_id=args.harness_run_id,
            ttl_seconds=args.expires_in,
            issued_by=issued_by,
            backtest_start_date=args.backtest_start_date,
            **signal_gap_kwargs,
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
    if args.command == "native-legacy-admission-attest":
        result = _run_native_legacy_admission_attestation(args)
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
    if args.command == "daily-real-replay":
        execute_only = bool(args.execute_only)
        schema_version = (
            "daily-real-replay-execution-v1"
            if execute_only
            else "daily-real-replay-preflight-v1"
        )
        try:
            runner = (
                run_real_replay_execute
                if execute_only
                else run_real_replay_preflight
            )
            runner_kwargs = {
                "native_manifest": args.native_manifest,
                "databridge_manifest": args.databridge_manifest,
            }
            if not execute_only:
                runner_kwargs["policy_version"] = (
                    args.policy_version or "v1"
                )
            report = runner(
                **runner_kwargs,
            )
        except DailyRealReplayPreflightError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": schema_version,
                        "status": "BLOCKED",
                        "failure_code": exc.code,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        except Exception:
            print(
                json.dumps(
                    {
                        "schema_version": schema_version,
                        "status": "ERROR",
                        "failure_code": (
                            "REPLAY_EXECUTION_INTERNAL_ERROR"
                            if execute_only
                            else "PREFLIGHT_INTERNAL_ERROR"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(
            json.dumps(
                _jsonable(report),
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
                    start_date=args.start,
                    as_of_date=args.as_of,
                    databridge_config=databridge_config,
                )
            finally:
                engine.dispose()
        except SignalGapPlanError as exc:
            print(
                json.dumps(
                    {
                        "schema_version":
                            "active-signal-gap-plan-error-v1",
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
                            "active-signal-gap-plan-error-v1",
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
    if args.command == "signal-gap-native-artifact":
        try:
            if args.native_artifact_command == "prepare":
                result = prepare_signal_gap_native_artifact(
                    capture_business_date=args.capture_business_date,
                    feature_date=args.feature_date,
                    output_root=args.output_root,
                )
            else:
                result = register_signal_gap_native_artifact(
                    manifest=args.manifest,
                    historical_predict_date=
                        args.historical_predict_date,
                    authorize=args.authorize,
                    storage_root=args.storage_root,
                )
        except SignalGapNativeArtifactRegistrationError as exc:
            print(
                json.dumps(
                    {
                        "schema_version":
                            "signal-gap-native-artifact-error-v1",
                        "status": "ERROR",
                        "failure_code": exc.failure_code,
                        "token_consumed": exc.token_consumed,
                        "audit_path": (
                            str(exc.audit_path)
                            if exc.audit_path is not None
                            else None
                        ),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "schema_version":
                            "signal-gap-native-artifact-error-v1",
                        "status": "ERROR",
                        "failure_code":
                            "SIGNAL_GAP_NATIVE_ARTIFACT_ERROR",
                        "token_consumed": False,
                        "audit_path": None,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "signal-gap-fill":
        project_root = args.project_root.resolve()
        ctx = GateContext(
            scheme_id="signal-gap-fill",
            predict_date="signal-gap-fill",
            project_root=project_root,
            report_dir=(
                project_root
                / "reports"
                / "harness"
                / "signal-gap-fill"
                / _timestamp()
            ),
            engine_factory=create_engine_from_env,
            algo_env=args.algo_env,
            timeout_sec=args.timeout_sec,
            signal_gap_plan_path=args.plan.resolve(),
            signal_gap_authorizations=tuple(args.authorize),
        )
        result = gate_for_name("signal-gap-fill", ctx=ctx).run(ctx)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        return _exit_code_for_result(result)
    parser.error("unsupported command")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_name", required=True)
    for gate_name in (
        "static", "input", "unit", "dry-run", "compare", "backtest",
        "api-readiness", "draft-register", "shadow-register", "api", "live",
        "gray-backfill", "lifecycle-reconcile", "revision-activate", "bootstrap",
    ):
        item = gate_subparsers.add_parser(gate_name)
        item.add_argument("--scheme-id", required=True)
        item.add_argument(
            "--predict-date",
            default="static" if gate_name in {"static", "unit", "compare", "backtest", "api-readiness", "api"} else None,
        )
        item.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        item.add_argument("--report-dir", type=Path, default=None)
        item.add_argument("--algo-env", default="forecast_env")
        item.add_argument("--timeout-sec", type=int, default=600)
        item.add_argument("--authorize", default=None)
        item.add_argument("--api-base-url", default="http://127.0.0.1:8100")
        item.add_argument("--api-instance-nonce", default=None)
        item.add_argument("--prediction-phase", choices=("gray_live", "scheduled_live"), default=None)
        if gate_name == "backtest":
            item.add_argument("--persist", action="store_true")
            item.add_argument("--sample-size", type=int, default=None)
            item.add_argument(
                "--backtest-start-date",
                default=DEFAULT_BACKTEST_START_DATE,
            )
        if gate_name == "bootstrap":
            item.add_argument("--expected-empty-schema", required=True)

    onboard_parser = subparsers.add_parser("onboard")
    onboard_parser.add_argument("scheme_id")
    onboard_parser.add_argument("--predict-date", required=True)
    onboard_parser.add_argument("--stage", default="all")
    onboard_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    onboard_parser.add_argument("--report-dir", type=Path, default=None)
    onboard_parser.add_argument("--algo-env", default="forecast_env")
    onboard_parser.add_argument("--timeout-sec", type=int, default=600)
    onboard_parser.add_argument("--api-base-url", default="http://127.0.0.1:8100")
    onboard_parser.add_argument("--api-instance-nonce", default=None)
    onboard_parser.add_argument("--authorize", default=None)
    onboard_parser.add_argument("--prediction-phase", choices=("gray_live", "scheduled_live"), default=None)
    onboard_parser.add_argument("--check-only", action="store_true")

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--scheme-id", required=True)
    activate_parser.add_argument("--predict-date", default="activate")
    activate_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    activate_parser.add_argument("--report-dir", type=Path, default=None)
    activate_parser.add_argument("--authorize", default=None)

    legacy_attestation_parser = subparsers.add_parser(
        "native-legacy-admission-attest"
    )
    legacy_attestation_parser.add_argument("--scheme-id", required=True)
    legacy_attestation_parser.add_argument("--authorize", required=True)
    legacy_attestation_parser.add_argument(
        "--project-root", type=Path, default=PROJECT_ROOT
    )
    legacy_attestation_parser.add_argument("--report-dir", type=Path, default=None)

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

    replay_parser = subparsers.add_parser("daily-real-replay")
    replay_action = replay_parser.add_mutually_exclusive_group(
        required=True,
    )
    replay_action.add_argument(
        "--check-only",
        action="store_true",
    )
    replay_action.add_argument(
        "--execute-only",
        action="store_true",
    )
    replay_parser.add_argument(
        "--native-manifest",
        type=Path,
        required=True,
    )
    replay_parser.add_argument(
        "--databridge-manifest",
        type=Path,
        required=True,
    )
    replay_parser.add_argument(
        "--policy-version",
        choices=("v1", "v2"),
        default=None,
        help="check-only policy identity; execute-only is fixed to v2",
    )

    gap_parser = subparsers.add_parser("signal-gap-plan")
    gap_parser.add_argument("--start", required=True)
    gap_parser.add_argument("--as-of", required=True, dest="as_of")
    gap_parser.add_argument(
        "--format",
        choices=("json",),
        default="json",
    )

    fill_parser = subparsers.add_parser("signal-gap-fill")
    fill_parser.add_argument("--plan", type=Path, required=True)
    fill_parser.add_argument(
        "--authorize",
        action="append",
        required=True,
    )
    fill_parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
    )
    fill_parser.add_argument("--algo-env", default="forecast_env")
    fill_parser.add_argument("--timeout-sec", type=int, default=600)

    native_artifact_parser = subparsers.add_parser(
        "signal-gap-native-artifact"
    )
    native_artifact_subparsers = (
        native_artifact_parser.add_subparsers(
            dest="native_artifact_command",
            required=True,
        )
    )
    native_artifact_prepare = native_artifact_subparsers.add_parser(
        "prepare"
    )
    native_artifact_prepare.add_argument(
        "--capture-business-date",
        required=True,
    )
    native_artifact_prepare.add_argument(
        "--feature-date",
        required=True,
    )
    native_artifact_prepare.add_argument(
        "--output-root",
        type=Path,
        required=True,
    )
    native_artifact_register = native_artifact_subparsers.add_parser(
        "register"
    )
    native_artifact_register.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    native_artifact_register.add_argument(
        "--historical-predict-date",
        required=True,
    )
    native_artifact_register.add_argument(
        "--authorize",
        required=True,
    )
    native_artifact_register.add_argument(
        "--storage-root",
        type=Path,
        required=True,
    )
    auth_parser = subparsers.add_parser("auth")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    issue_parser = auth_subparsers.add_parser("issue")
    issue_parser.add_argument("--scheme-id", required=True)
    issue_parser.add_argument("--action", required=True)
    issue_parser.add_argument("--predict-date", default=None)
    issue_parser.add_argument("--issued-by", default=None)
    issue_parser.add_argument("--expires-in", type=int, default=None, dest="expires_in")
    issue_parser.add_argument("--scheme-version", default=None, dest="scheme_version")
    issue_parser.add_argument("--harness-run-id", default=None, dest="harness_run_id")
    issue_parser.add_argument(
        "--backtest-start-date",
        default=DEFAULT_BACKTEST_START_DATE,
    )
    issue_parser.add_argument("--plan-sha256", default=None)
    issue_parser.add_argument("--base-scheme-id", default=None)
    issue_parser.add_argument(
        "--target-keys-json",
        type=Path,
        default=None,
    )
    issue_parser.add_argument(
        "--source-authority-json",
        type=Path,
        default=None,
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
        "gray-backfill",
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
        expected_empty_schema=getattr(args, "expected_empty_schema", None),
        api_base_url=args.api_base_url,
        api_instance_nonce=args.api_instance_nonce,
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
        api_base_url=args.api_base_url,
        api_instance_nonce=args.api_instance_nonce,
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


def _run_native_legacy_admission_attestation(
    args: argparse.Namespace,
) -> GateResult:
    project_root = args.project_root.resolve()
    report_dir = (
        args.report_dir
        or project_root
        / "reports"
        / "harness"
        / args.scheme_id
        / _timestamp()
    )
    config = _load_config_for_dispatch(
        project_root / "schemes" / args.scheme_id / "config.yaml"
    )
    engine = create_engine_from_env()
    ctx = GateContext(
        scheme_id=args.scheme_id,
        predict_date="legacy-admission-attestation",
        project_root=project_root,
        report_dir=report_dir,
        config=config,
        authorization=args.authorize,
        engine_factory=lambda: engine,
    )
    try:
        return run_legacy_native_admission_attestation(ctx)
    finally:
        engine.dispose()


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
