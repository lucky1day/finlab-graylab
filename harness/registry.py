from __future__ import annotations

from harness.gates.backtest_gate import BacktestGate
from harness.gates.base import Gate
from harness.gates.compare_gate import CompareGate
from harness.gates.dashboard_gate import DashboardGate
from harness.gates.dry_run_gate import DryRunGate
from harness.gates.input_gate import InputGate
from harness.gates.live_gate import LiveGate
from harness.gates.native_maintenance_admission_gate import (
    NATIVE_MAINTENANCE_SEQUENCE,
    NATIVE_MAINTENANCE_STAGE,
    NativeMaintenanceAdmissionGate,
)
from harness.gates.static_gate import StaticGate
from harness.gates.unit_gate import UnitGate
from harness.context import GateContext
from shared.scheme_config_loader import load_yaml_mapping


AUTO_SEQUENCE = ["static", "input", "unit", "dry-run", "compare", "backtest"]
# Blackbox 的 CompareGate 已经更强地覆盖了 dry-run 与 no-persist backtest 的全部断言：
# 其 baseline 与 dry-run 是逐参数相同的同一次 predict；其 batch=100 vs batch=1 比 no-persist
# backtest 的 100 vs 80 更严格。Native 的 CompareGate 是完全不同的 benchmark 对比实现，
# 不覆盖这两段，因此 Native 序列保持不变。
BLACKBOX_AUTO_SEQUENCE = ["static", "input", "unit", "compare"]


def auto_sequence_for_runtime(runtime_type: str | None) -> list[str]:
    """返回该 runtime 的自动段序列。"""
    if str(runtime_type or "").strip() == "blackbox_v2":
        return list(BLACKBOX_AUTO_SEQUENCE)
    return list(AUTO_SEQUENCE)


def sequence_for_stage(
    stage: str,
    *,
    runtime_type: str | None = None,
) -> list[str]:
    normalized = stage.strip().lower()
    if normalized == NATIVE_MAINTENANCE_STAGE:
        return list(NATIVE_MAINTENANCE_SEQUENCE)
    sequence = auto_sequence_for_runtime(runtime_type)
    if normalized == "all":
        return sequence
    if normalized not in sequence:
        raise ValueError(f"unsupported onboard stage: {stage}")
    return sequence[: sequence.index(normalized) + 1]


def gate_for_name(name: str, *, ctx: GateContext | None = None) -> Gate:
    if name == "dashboard":
        return DashboardGate()
    runtime_type = _runtime_type(ctx)
    if runtime_type == "blackbox_v2":
        from harness.blackbox_v2.gates import BLACKBOX_GATES
        from harness.blackbox_v2.activation import BlackboxLifecycleReconcileGate
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate
        from harness.blackbox_v2.lifecycle_bootstrap import (
            BlackboxLifecycleBootstrapGate,
        )
        from harness.blackbox_v2.revision_activation import BlackboxRevisionActivateGate
        common_post_activation_gates = {
            "draft-register": BlackboxDraftRegisterGate,
            "revision-activate": BlackboxRevisionActivateGate,
            "live": LiveGate,
            "lifecycle-reconcile": BlackboxLifecycleReconcileGate,
            "lifecycle-bootstrap": BlackboxLifecycleBootstrapGate,
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
        "native-maintenance-admission": NativeMaintenanceAdmissionGate,
        "input": InputGate,
        "unit": UnitGate,
        "dry-run": DryRunGate,
        "compare": CompareGate,
        "backtest": BacktestGate,
        "live": LiveGate,
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
