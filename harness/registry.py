from __future__ import annotations

from harness.context import GateContext
from harness.gates.base import Gate
from harness.gates.dashboard_gate import DashboardGate
from scheduler.discovery import load_scheme_config


def gate_for_name(name: str, *, ctx: GateContext | None = None) -> Gate:
    """只分派 Blackbox 回测与公共只读 Dashboard 验收。"""
    if name == "dashboard":
        return DashboardGate()
    if ctx is None:
        raise ValueError("Blackbox gate requires a scheme context")
    config = ctx.config or load_scheme_config(
        ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    )
    if config.runtime_type != "blackbox_v2":
        raise ValueError("Native validation gates are retired; W4 remains on its fixed version")
    from harness.blackbox_v2.gates import BlackboxBacktestGate

    if name == "backtest":
        return BlackboxBacktestGate()
    raise ValueError(f"unsupported Blackbox V2 gate: {name}")
