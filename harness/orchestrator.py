from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

from harness.context import GateContext
from harness.gates.base import Gate, utc_now
from harness.gates.native_maintenance_admission_gate import NATIVE_MAINTENANCE_STAGE
from harness.persistence import (
    new_harness_run_id,
    persist_harness_gate_result,
    persist_harness_run_finish,
    persist_harness_run_start,
)
from harness.registry import AUTO_SEQUENCE, gates_for_stage
from harness.report.writer import write_gate_result, write_onboard_report
from harness.result import Evidence, GateResult, GateStatus, OnboardReport


def onboard(
    ctx: GateContext,
    stage: str = "all",
    gates: Iterable[Gate] | None = None,
    *,
    check_only: bool = False,
) -> OnboardReport:
    """按 stage 顺序串联 Gate，任一失败或阻塞立即停止。"""
    check_only = bool(check_only or ctx.check_only)
    normalized_stage = stage.strip().lower()
    strict_native_maintenance = (
        not check_only and normalized_stage == NATIVE_MAINTENANCE_STAGE
    )
    if check_only and (
        normalized_stage != "all"
        or ctx.authorization is not None
        or ctx.prediction_phase is not None
        or ctx.persist_backtest
    ):
        raise ValueError(
            "check-only requires stage=all and forbids authorization, "
            "prediction side-effect phases, and persisted backtest"
        )
    if check_only and gates is not None:
        raise ValueError(
            "check-only forbids caller-supplied gates and requires the "
            "canonical automatic sequence"
        )
    selected_gates = list(gates) if gates is not None else gates_for_stage(stage, ctx=ctx)
    if check_only and [gate.name for gate in selected_gates] != AUTO_SEQUENCE:
        raise ValueError(
            "check-only canonical automatic gate sequence mismatch"
        )
    harness_run_id = new_harness_run_id()
    run_started_at = utc_now()
    if not check_only:
        start_persisted = persist_harness_run_start(
            ctx,
            harness_run_id=harness_run_id,
            stage=(
                NATIVE_MAINTENANCE_STAGE
                if strict_native_maintenance
                else stage
            ),
            started_at=run_started_at,
        )
        if strict_native_maintenance and not start_persisted:
            report, _report_path = _write_persistence_failure_report(
                ctx,
                stage=stage,
                harness_run_id=harness_run_id,
                results=[],
                operation="run_start",
            )
            return report
    results: list[GateResult] = []
    for gate in selected_gates:
        result = _run_gate(ctx, gate)
        result_path = write_gate_result(ctx, result)
        if result.report_path is None:
            result = replace(result, report_path=result_path)
        results.append(result)
        if not check_only:
            gate_result_persisted = persist_harness_gate_result(
                ctx,
                harness_run_id,
                result,
            )
            if strict_native_maintenance and not gate_result_persisted:
                report, report_path = _write_persistence_failure_report(
                    ctx,
                    stage=stage,
                    harness_run_id=harness_run_id,
                    results=results,
                    operation="gate_result",
                    gate_name=result.gate_name,
                )
                persist_harness_run_finish(
                    ctx,
                    harness_run_id=harness_run_id,
                    status="failed",
                    finished_at=utc_now(),
                    report_uri=str(report_path),
                )
                return report
        # SKIPPED 视为非阻塞（passed=True）；仅 FAILED/BLOCKED 立即停止。
        if result.status in (GateStatus.SKIPPED, GateStatus.PASSED):
            continue
        if not result.passed:
            break

    report = OnboardReport(
        scheme_id=ctx.scheme_id,
        predict_date=ctx.predict_date,
        stage_requested=stage,
        results=results,
        overall_passed=bool(results) and all(item.passed for item in results),
        report_dir=ctx.report_dir,
        harness_run_id=harness_run_id,
        check_only=check_only,
        control_plane_persisted=not check_only,
        business_tables_written=bool(ctx.persist_backtest),
        persist_backtest=bool(ctx.persist_backtest),
    )
    report_path = write_onboard_report(report)
    if not check_only:
        finish_persisted = persist_harness_run_finish(
            ctx,
            harness_run_id=harness_run_id,
            status=_report_status(report),
            finished_at=utc_now(),
            report_uri=str(report_path),
        )
        if strict_native_maintenance and not finish_persisted:
            report, report_path = _write_persistence_failure_report(
                ctx,
                stage=stage,
                harness_run_id=harness_run_id,
                results=results,
                operation="run_finish",
            )
            persist_harness_run_finish(
                ctx,
                harness_run_id=harness_run_id,
                status="failed",
                finished_at=utc_now(),
                report_uri=str(report_path),
            )
    if getattr(ctx.config, "runtime_type", "native_adapter") == "blackbox_v2":
        from harness.blackbox_v2.gates import cleanup_runtime_input

        cleanup_runtime_input(ctx)
    return report


def _run_gate(ctx: GateContext, gate: Gate) -> GateResult:
    if getattr(gate, "requires_authorization", False) and not ctx.authorization:
        now = utc_now()
        return GateResult(
            gate_name=gate.name,
            status=GateStatus.BLOCKED,
            passed=False,
            evidence=[Evidence("authorization_required", True)],
            errors=[f"{gate.name} requires authorization"],
            started_at=now,
            finished_at=now,
        )
    return gate.run(ctx)


def _write_persistence_failure_report(
    ctx: GateContext,
    *,
    stage: str,
    harness_run_id: str,
    results: list[GateResult],
    operation: str,
    gate_name: str | None = None,
) -> tuple[OnboardReport, Path]:
    """为 Native maintenance 的控制面写入失败保留本地 fail-closed 证据。"""
    now = utc_now()
    evidence = [
        Evidence("control_plane_persisted", False),
        Evidence("persistence_required_stage", "native-maintenance"),
        Evidence("persistence_operation", operation),
        Evidence("harness_run_id", harness_run_id),
    ]
    if gate_name is not None:
        evidence.append(Evidence("persistence_gate_name", gate_name))
    failure = GateResult(
        gate_name="control-plane-persistence",
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=evidence,
        errors=[
            "native-maintenance requires durable control-plane persistence; "
            f"{operation} returned false"
        ],
        started_at=now,
        finished_at=now,
    )
    failure_path = write_gate_result(ctx, failure)
    persisted_results = [*results, replace(failure, report_path=failure_path)]
    report = OnboardReport(
        scheme_id=ctx.scheme_id,
        predict_date=ctx.predict_date,
        stage_requested=stage,
        results=persisted_results,
        overall_passed=False,
        report_dir=ctx.report_dir,
        harness_run_id=harness_run_id,
        check_only=False,
        control_plane_persisted=False,
        business_tables_written=bool(ctx.persist_backtest),
        persist_backtest=bool(ctx.persist_backtest),
    )
    return report, write_onboard_report(report)


def _report_status(report: OnboardReport) -> str:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return "blocked"
    if report.overall_passed:
        return "passed"
    return "failed"
