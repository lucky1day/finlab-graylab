from __future__ import annotations

from dataclasses import replace

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.operation import verify_direct_operation
from harness.result import GateResult, GateStatus
from scheduler.discovery import load_scheme_config


class ActivationGate(Gate):
    """严格绑定当前 canonical 与命令作用域，仅允许 Blackbox 激活。"""

    name = "activate"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = load_scheme_config(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        errors: list[str] = []
        if cfg.runtime_type != "blackbox_v2":
            errors.append("Native activation is retired; W4 remains on its fixed version")
        if cfg.scheme_id != ctx.scheme_id:
            errors.append("canonical scheme_id does not match activation context")
        if ctx.config is not None and any(
            getattr(ctx.config, key, None) != getattr(cfg, key)
            for key in ("scheme_id", "runtime_type", "scheme_version")
        ):
            errors.append("activation context does not match current canonical identity")
        _, operation_errors = verify_direct_operation(
            ctx.operation,
            scheme_id=ctx.scheme_id,
            action="blackbox_activate",
            scheme_version=cfg.scheme_version,
        )
        errors.extend(operation_errors)
        if errors:
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=[],
                errors=errors,
                started_at=started_at,
                finished_at=utc_now(),
            )
        from harness.blackbox_v2.activation import activate_blackbox

        return activate_blackbox(replace(ctx, config=cfg))
