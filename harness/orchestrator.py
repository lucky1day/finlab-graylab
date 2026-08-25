from __future__ import annotations

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
from harness.registry import gates_for_stage
from harness.result import Evidence, GateResult, GateStatus, OnboardReport


def onboard(
    ctx: GateContext,
    stage: str = "all",
    gates: Iterable[Gate] | None = None,
) -> OnboardReport:
    """按 stage 顺序串联 Gate，任一失败或阻塞立即停止。"""
    normalized_stage = stage.strip().lower()
    selected_gates = list(gates) if gates is not None else gates_for_stage(stage, ctx=ctx)
    harness_run_id = new_harness_run_id()
    run_started_at = utc_now()
    start_persisted = persist_harness_run_start(
        ctx,
        harness_run_id=harness_run_id,
        stage=(
            NATIVE_MAINTENANCE_STAGE
            if normalized_stage == NATIVE_MAINTENANCE_STAGE
            else stage
        ),
        started_at=run_started_at,
    )
    if not start_persisted:
        return _persistence_failure_report(
            ctx,
            stage=stage,
            harness_run_id=harness_run_id,
            results=[],
            operation="run_start",
        )
    try:
        results: list[GateResult] = []
        for gate in selected_gates:
            result = _run_gate(ctx, gate)
            results.append(result)
            gate_result_persisted = persist_harness_gate_result(
                ctx,
                harness_run_id,
                result,
            )
            if not gate_result_persisted:
                report = _persistence_failure_report(
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
            harness_run_id=harness_run_id,
            control_plane_persisted=True,
        )
        finish_persisted = persist_harness_run_finish(
            ctx,
            harness_run_id=harness_run_id,
            status=_report_status(report),
            finished_at=utc_now(),
        )
        if not finish_persisted:
            report = _persistence_failure_report(
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
            )
        return report
    finally:
        if getattr(ctx.config, "runtime_type", "native_adapter") == "blackbox_v2":
            from harness.blackbox_v2.gates import cleanup_runtime_input

            cleanup_runtime_input(ctx)


def _run_gate(ctx: GateContext, gate: Gate) -> GateResult:
    if getattr(gate, "requires_operation", False) and not ctx.operation:
        now = utc_now()
        return GateResult(
            gate_name=gate.name,
            status=GateStatus.BLOCKED,
            passed=False,
            evidence=[Evidence("operation_required", True)],
            errors=[f"{gate.name} requires a direct operator command"],
            started_at=now,
            finished_at=now,
        )
    return gate.run(ctx)


def _persistence_failure_report(
    ctx: GateContext,
    *,
    stage: str,
    harness_run_id: str,
    results: list[GateResult],
    operation: str,
    gate_name: str | None = None,
) -> OnboardReport:
    """返回控制面写入失败的 fail-closed 结果，不再保存本地 JSON。"""
    now = utc_now()
    evidence = [
        Evidence("control_plane_persisted", False),
        Evidence("persistence_required_stage", stage.strip().lower()),
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
            "harness requires durable control-plane persistence; "
            f"{operation} returned false"
        ],
        started_at=now,
        finished_at=now,
    )
    return OnboardReport(
        scheme_id=ctx.scheme_id,
        predict_date=ctx.predict_date,
        stage_requested=stage,
        results=[*results, failure],
        overall_passed=False,
        harness_run_id=harness_run_id,
        control_plane_persisted=False,
    )


def _report_status(report: OnboardReport) -> str:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return "blocked"
    if report.overall_passed:
        return "passed"
    return "failed"
