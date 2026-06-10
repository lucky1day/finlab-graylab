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

        # 校验 gate 历史：当前 scheme_version 必须有一次 stage=all 全通过的 harness 运行
        scheme_version = _compute_scheme_version(ctx)
        gate_history_errors = _verify_gate_history(ctx, scheme_version)
        if gate_history_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("scheme_version", scheme_version),
                    Evidence("gate_history_errors", gate_history_errors),
                ],
                errors=gate_history_errors,
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
        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("scheme_id", ctx.scheme_id),
                Evidence("scheme_version", scheme_version),
                Evidence("config_path", str(config_path)),
                Evidence("previous_status", previous_status),
                Evidence("new_status", new_status),
                Evidence("status_flipped", flipped),
                Evidence("cron", _cron_of(raw)),
                Evidence("gate_history_verified", True),
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


def _compute_scheme_version(ctx: GateContext) -> str:
    """读取 config.yaml 计算当前方案版本号。"""
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    scheme_dir = config_path.parent
    from shared.versioning import compute_code_hash, compute_config_hash, compute_scheme_version
    code_hash = compute_code_hash(scheme_dir)
    config_hash = compute_config_hash(config_path)
    return compute_scheme_version(code_hash, config_hash)


REQUIRED_ACTIVATE_GATES = frozenset({
    "static", "input", "unit", "dry-run", "compare", "backtest", "api",
})


def _verify_gate_history(ctx: GateContext, scheme_version: str) -> list[str]:
    """查询 t_harness_gate_results 确认当前版本已通过全部必要 gate。

    返回错误列表，空列表 = 校验通过。
    """
    from sqlalchemy import text

    engine = _db_engine()
    if engine is None:
        return ["cannot connect to database to verify gate history"]
    try:
        with engine.begin() as conn:
            # 查找最近一次 stage=all 且 status=passed 的 harness 运行
            run = conn.execute(
                text(
                    """
                    SELECT harness_run_id, finished_at
                    FROM t_harness_runs
                    WHERE scheme_id = :scheme_id
                      AND scheme_version = :scheme_version
                      AND stage = 'all'
                      AND status = 'passed'
                    ORDER BY finished_at DESC
                    LIMIT 1
                    """
                ),
                {"scheme_id": ctx.scheme_id, "scheme_version": scheme_version},
            ).one_or_none()
            if run is None:
                return [
                    f"no passed 'all' stage harness run found for {ctx.scheme_id} "
                    f"version {scheme_version}; run 'python -m harness onboard "
                    f"--scheme-id {ctx.scheme_id} --stage all' first"
                ]

            # 查找该运行中各 gate 的状态
            rows = conn.execute(
                text(
                    """
                    SELECT gate_name, status
                    FROM t_harness_gate_results
                    WHERE harness_run_id = :harness_run_id
                    """
                ),
                {"harness_run_id": run[0]},
            ).fetchall()

            passed_gates = {row[0] for row in rows if row[1] in ("passed", "skipped")}
            missing = REQUIRED_ACTIVATE_GATES - passed_gates
            if missing:
                return [
                    f"required gates not all passed for harness run {run[0]}: "
                    f"missing/not passed: {sorted(missing)}"
                ]
            return []
    finally:
        if engine is not None and hasattr(engine, "dispose"):
            engine.dispose()


def _db_engine():
    """创建数据库连接。"""
    try:
        from scheduler.repository import create_engine_from_env
        return create_engine_from_env()
    except Exception:
        return None
