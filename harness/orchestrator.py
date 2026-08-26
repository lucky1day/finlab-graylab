from __future__ import annotations

from typing import Iterable

from harness.context import GateContext
from harness.gates.base import Gate, utc_now
from harness.gates.native_maintenance_admission_gate import NATIVE_MAINTENANCE_STAGE
from harness.persistence import (
    new_harness_run_id,
    persist_harness_run_complete,
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
    results: list[GateResult] = []
    for gate in selected_gates:
        result = gate.run(ctx)
        results.append(result)
        if not result.passed:
            break

    report = OnboardReport(
        scheme_id=ctx.scheme_id,
        predict_date=ctx.predict_date,
        stage_requested=stage,
        results=results,
        harness_run_id=harness_run_id,
    )
    complete_persisted = persist_harness_run_complete(
        ctx,
        harness_run_id=harness_run_id,
        status=_report_status(report),
        finished_at=utc_now(),
        results=results,
    )
    if not complete_persisted:
        report = _persistence_failure_report(
            ctx,
            stage=stage,
            harness_run_id=harness_run_id,
            results=results,
            operation="run_complete",
        )
        persist_harness_run_finish(
            ctx,
            harness_run_id=harness_run_id,
            status="failed",
            finished_at=utc_now(),
        )
    return report


def _persistence_failure_report(
    ctx: GateContext,
    *,
    stage: str,
    harness_run_id: str,
    results: list[GateResult],
    operation: str,
) -> OnboardReport:
    """返回控制面写入失败的 fail-closed 结果，不再保存本地 JSON。"""
    now = utc_now()
    evidence = [
        Evidence("control_plane_persisted", False),
        Evidence("persistence_required_stage", stage.strip().lower()),
        Evidence("persistence_operation", operation),
        Evidence("harness_run_id", harness_run_id),
    ]
    failure = GateResult(
        gate_name="control-plane-persistence",
        status=GateStatus.BLOCKED,
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
        harness_run_id=harness_run_id,
    )


def _report_status(report: OnboardReport) -> str:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return "blocked"
    if report.overall_passed:
        return "passed"
    return "failed"
