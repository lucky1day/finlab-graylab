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


AUTO_SEQUENCE = ["static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness"]
EXPLICIT_SEQUENCE = ["live", "activate"]


def sequence_for_stage(stage: str) -> list[str]:
    normalized = stage.strip().lower()
    if normalized == "all":
        return list(AUTO_SEQUENCE)
    if normalized not in AUTO_SEQUENCE:
        raise ValueError(f"unsupported onboard stage: {stage}")
    return AUTO_SEQUENCE[: AUTO_SEQUENCE.index(normalized) + 1]


def gate_for_name(name: str) -> Gate:
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


def gates_for_stage(stage: str) -> list[Gate]:
    return [gate_for_name(name) for name in sequence_for_stage(stage)]
