from __future__ import annotations

from harness.gates.api_gate import ApiGate
from harness.gates.api_readiness_gate import ApiReadinessGate
from harness.gates.backtest_gate import BacktestGate
from harness.gates.base import Gate
from harness.gates.compare_gate import CompareGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.context import GateContext


AUTO_SEQUENCE = ["static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness"]
EXPLICIT_SEQUENCE = ["live", "activate", "lifecycle-reconcile"]


def sequence_for_stage(stage: str) -> list[str]:
    normalized = stage.strip().lower()
    if normalized == "all":
        return list(AUTO_SEQUENCE)
    if normalized not in AUTO_SEQUENCE:
        raise ValueError(f"unsupported onboard stage: {stage}")
    return AUTO_SEQUENCE[: AUTO_SEQUENCE.index(normalized) + 1]


def gate_for_name(name: str, *, ctx: GateContext | None = None) -> Gate:
    runtime_type = _runtime_type(ctx)
    if runtime_type == "blackbox_v2":
        from harness.blackbox_v2.gates import BLACKBOX_GATES
        from harness.blackbox_v2.activation import BlackboxLifecycleReconcileGate

        common_post_activation_gates = {
            "api": ApiGate,
            "live": LiveGate,
            "lifecycle-reconcile": BlackboxLifecycleReconcileGate,
        }
        if name in common_post_activation_gates:
            return common_post_activation_gates[name]()
        try:
            return BLACKBOX_GATES[name]()
        except KeyError as exc:
            raise ValueError(f"unsupported Blackbox V2 gate: {name}") from exc
    if runtime_type != "native_adapter":
        raise ValueError(f"unsupported runtime_type: {runtime_type}")
    gates = {
        "static": StaticGate,
        "input": InputGate,
        "unit": UnitGate,
        "dry-run": DryRunGate,
        "compare": CompareGate,
        "backtest": BacktestGate,
        "api-readiness": ApiReadinessGate,
        "api": ApiGate,
        "live": LiveGate,
    }
    try:
        return gates[name]()
    except KeyError as exc:
        raise ValueError(f"unsupported gate: {name}") from exc


def gates_for_stage(stage: str, *, ctx: GateContext | None = None) -> list[Gate]:
    return [gate_for_name(name, ctx=ctx) for name in sequence_for_stage(stage)]


def _runtime_type(ctx: GateContext | None) -> str:
    if ctx is None:
        return "native_adapter"
    config = ctx.config
    if config is not None:
        return str(getattr(config, "runtime_type", "native_adapter"))
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    if not config_path.exists():
        return "native_adapter"
    from harness.config_loader import load_config_raw

    raw = load_config_raw(config_path)
    return str(raw.get("runtime_type", "native_adapter"))
