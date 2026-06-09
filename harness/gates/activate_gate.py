from __future__ import annotations

from pathlib import Path

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.contracts.config_schema import validate_config
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


class ActivationGate(Gate):
    """方案激活 gate：需 action=activate 的授权 token，将 config.status paused→active。"""

    name = "activate"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"

        auth, auth_errors = verify_authorization(
            ctx.authorization,
            scheme_id=ctx.scheme_id,
            action="activate",
            predict_date=None,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("authorization_required", True),
                ],
                errors=auth_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        errors: list[str] = []
        if not config_path.exists():
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[Evidence("config_path", str(config_path))],
                errors=[f"config.yaml not found: {config_path}"],
                started_at=started_at,
                finished_at=finished_at,
            )

        raw = load_config_raw(config_path)
        config_errors = validate_config(raw, ctx.scheme_id)
        if config_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[Evidence("config_errors", config_errors)],
                errors=[f"config validation failed: {len(config_errors)} error(s)"],
                started_at=started_at,
                finished_at=finished_at,
            )

        previous_status = str(raw.get("status"))
        if previous_status == "active":
            new_status = "active"
            flipped = False
        else:
            new_status = "active"
            flipped = _flip_status_to_active(config_path)

        # 授权审计 + 一次性消费
        audit_dir = ctx.report_dir / "activation_authorization"
        audit_path = write_authorization_audit(auth, audit_dir)
        mark_token_used(auth, used_tokens_path(ctx.project_root))

        # TODO(P1): 在此写入 t_scheme_activation 激活表（DB schema 为后续工作，
        # 当前仅落地 config.yaml status 翻转 + 授权审计）。

        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("scheme_id", ctx.scheme_id),
                Evidence("config_path", str(config_path)),
                Evidence("previous_status", previous_status),
                Evidence("new_status", new_status),
                Evidence("status_flipped", flipped),
                Evidence("cron", _cron_of(raw)),
                Evidence("authorization_audit_path", str(audit_path)),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=audit_path,
        )


def _cron_of(raw: dict) -> str | None:
    schedule = raw.get("schedule")
    if isinstance(schedule, dict):
        cron = schedule.get("cron")
        return str(cron) if cron else None
    return None


def _flip_status_to_active(config_path: Path) -> bool:
    """将 config.yaml 中 status 行从 paused 翻转为 active（保留其余文本）。"""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    flipped = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("status:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"status: active{newline}"
            flipped = True
            break
    if flipped:
        config_path.write_text("".join(lines), encoding="utf-8")
    return flipped
