from __future__ import annotations

from harness.gates.backtest_gate import BacktestGate
from harness.gates.base import Gate
from harness.gates.compare_gate import CompareGate
from harness.gates.dashboard_gate import DashboardGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.native_maintenance_admission_gate import (
    NATIVE_MAINTENANCE_SEQUENCE,
    NATIVE_MAINTENANCE_STAGE,
    NativeMaintenanceAdmissionGate,
)
from harness.gates.static_gate import StaticGate
from harness.context import GateContext
from shared.scheme_config_loader import load_yaml_mapping


AUTO_SEQUENCE = ["static", "dry-run", "compare", "backtest"]


def sequence_for_stage(
    stage: str,
    *,
    runtime_type: str | None = None,
) -> list[str]:
    normalized = stage.strip().lower()
    if normalized == NATIVE_MAINTENANCE_STAGE:
        return list(NATIVE_MAINTENANCE_SEQUENCE)
    if normalized != "all":
        raise ValueError(f"unsupported onboard stage: {stage}")
    if str(runtime_type or "").strip() == "blackbox_v2":
        raise ValueError(
            "Blackbox V2 has no onboard stage; use intake, persisted backtest, then activate"
        )
    return list(AUTO_SEQUENCE)


def gate_for_name(name: str, *, ctx: GateContext | None = None) -> Gate:
    if name == "dashboard":
        return DashboardGate()
    runtime_type = _runtime_type(ctx)
    if runtime_type == "blackbox_v2":
        from harness.blackbox_v2.gates import BLACKBOX_GATES
        try:
            return BLACKBOX_GATES[name]()
        except KeyError as exc:
            raise ValueError(f"unsupported Blackbox V2 gate: {name}") from exc
    if runtime_type != "native_adapter":
        raise ValueError(f"unsupported runtime_type: {runtime_type}")
    gates = {
        "static": StaticGate,
        "native-maintenance-admission": NativeMaintenanceAdmissionGate,
        "dry-run": DryRunGate,
        "compare": CompareGate,
        "backtest": BacktestGate,
    }
    try:
        return gates[name]()
    except KeyError as exc:
        raise ValueError(f"unsupported gate: {name}") from exc


def gates_for_stage(stage: str, *, ctx: GateContext | None = None) -> list[Gate]:
    names = sequence_for_stage(stage, runtime_type=_runtime_type(ctx))
    return [gate_for_name(name, ctx=ctx) for name in names]


def _runtime_type(ctx: GateContext | None) -> str:
    if ctx is None:
        return "native_adapter"
    config = ctx.config
    if config is not None:
        return str(getattr(config, "runtime_type", "native_adapter"))
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    if not config_path.exists():
        return "native_adapter"
    raw = load_yaml_mapping(config_path)
    return str(raw.get("runtime_type", "native_adapter"))
