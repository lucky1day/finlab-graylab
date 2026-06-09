from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from harness.context import GateContext
from harness.gates.base import Gate, utc_now
from harness.persistence import (
    new_harness_run_id,
    persist_harness_gate_result,
    persist_harness_run_finish,
    persist_harness_run_start,
)
from harness.registry import gates_for_stage
from harness.report.writer import write_gate_result, write_onboard_report
from harness.result import Evidence, GateResult, GateStatus, OnboardReport


def onboard(ctx: GateContext, stage: str = "all", gates: Iterable[Gate] | None = None) -> OnboardReport:
    """按 stage 顺序串联 Gate，任一失败或阻塞立即停止。"""
    selected_gates = list(gates) if gates is not None else gates_for_stage(stage)
    harness_run_id = new_harness_run_id()
    run_started_at = utc_now()
    persist_harness_run_start(ctx, harness_run_id=harness_run_id, stage=stage, started_at=run_started_at)
    results: list[GateResult] = []
    for gate in selected_gates:
        result = _run_gate(ctx, gate)
        result_path = write_gate_result(ctx, result)
        if result.report_path is None:
            result = replace(result, report_path=result_path)
        results.append(result)
        persist_harness_gate_result(ctx, harness_run_id, result)
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
    )
    report_path = write_onboard_report(report)
    persist_harness_run_finish(
        ctx,
        harness_run_id=harness_run_id,
        status=_report_status(report),
        finished_at=utc_now(),
        report_uri=str(report_path),
    )
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


def _report_status(report: OnboardReport) -> str:
    if any(result.status == GateStatus.BLOCKED for result in report.results):
        return "blocked"
    if report.overall_passed:
        return "passed"
    return "failed"
