from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from harness.context import GateContext
from harness.gates.base import Gate, utc_now
from harness.registry import gates_for_stage
from harness.report.writer import write_gate_result, write_onboard_report
from harness.result import Evidence, GateResult, GateStatus, OnboardReport


def onboard(ctx: GateContext, stage: str = "all", gates: Iterable[Gate] | None = None) -> OnboardReport:
    """按 stage 顺序串联 Gate，任一失败或阻塞立即停止。"""
    selected_gates = list(gates) if gates is not None else gates_for_stage(stage)
    results: list[GateResult] = []
    for gate in selected_gates:
        result = _run_gate(ctx, gate)
        result_path = write_gate_result(ctx, result)
        if result.report_path is None:
            result = replace(result, report_path=result_path)
        results.append(result)
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
    )
    write_onboard_report(report)
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
