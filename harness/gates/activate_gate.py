from __future__ import annotations

from dataclasses import replace
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
from harness.contracts.onboarding_policy import validate_onboarding_policy
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


class ActivationGate(Gate):
    """方案激活 gate：需 action=activate 的授权 token，将 config.status paused→active。"""

    name = "activate"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        cfg = ctx.config
        raw_runtime_type = None
        if config_path.is_file():
            try:
                raw = load_config_raw(config_path)
                raw_runtime_type = raw.get("runtime_type") if isinstance(raw, dict) else None
            except (OSError, UnicodeError, ValueError):
                raw_runtime_type = getattr(cfg, "runtime_type", None)
        if raw_runtime_type == "blackbox_v2" or getattr(cfg, "runtime_type", None) == "blackbox_v2":
            try:
                from scheduler.discovery import load_scheme_config

                cfg = load_scheme_config(config_path)
            except Exception as exc:  # noqa: BLE001
                return guarded_result(
                    self.name,
                    lambda started_at: _blackbox_config_failure(started_at, config_path, exc),
                )
            from harness.blackbox_v2.activation import activate_blackbox

            return activate_blackbox(replace(ctx, config=cfg))
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

        policy_errors = validate_onboarding_policy(
            ctx.project_root,
            ctx.scheme_id,
            str(raw.get("runtime_type", "native_adapter")),
        )
        if policy_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("onboarding_policy_allowed", False),
                    Evidence("onboarding_policy_errors", policy_errors),
                ],
                errors=policy_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        # 校验 gate 历史：当前 paused 版本必须有一次 stage=all 全通过的 harness 运行。
        validation_scheme_version = _compute_scheme_version(ctx)
        gate_history_errors = _verify_gate_history(ctx, validation_scheme_version)
        if gate_history_errors:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("gate_history_errors", gate_history_errors),
                ],
                errors=gate_history_errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        # 消费是首次授权副作用；锁内重检保证并发输家不会修改配置或 Registry。
        mark_token_used(auth, used_tokens_path(ctx.project_root))
        audit_dir = ctx.report_dir / "activation_authorization"
        audit_path = write_authorization_audit(auth, audit_dir)

        previous_status = str(raw.get("status"))
        if previous_status == "active":
            new_status = "active"
            flipped = False
        else:
            new_status = "active"
            flipped = _flip_status_to_active(config_path)

        try:
            activated_scheme_version = _sync_registry_after_activation(ctx)
        except Exception as exc:
            rollback_error: str | None = None
            if flipped and previous_status != "active":
                try:
                    _set_status(config_path, previous_status)
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            finished_at = utc_now()
            errors = [f"registry sync after activation failed: {exc}"]
            if rollback_error:
                errors.append(f"failed to roll back config status after sync failure: {rollback_error}")
            return GateResult(
                gate_name=self.name,
                status=GateStatus.FAILED,
                passed=False,
                evidence=[
                    Evidence("scheme_id", ctx.scheme_id),
                    Evidence("validation_scheme_version", validation_scheme_version),
                    Evidence("config_path", str(config_path)),
                    Evidence("previous_status", previous_status),
                    Evidence("new_status", new_status),
                    Evidence("status_flipped", flipped),
                    Evidence("status_rolled_back", rollback_error is None and flipped and previous_status != "active"),
                    Evidence("registry_synced", False),
                ],
                errors=errors,
                started_at=started_at,
                finished_at=finished_at,
            )

        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("scheme_id", ctx.scheme_id),
                Evidence("validation_scheme_version", validation_scheme_version),
                Evidence("activated_scheme_version", activated_scheme_version),
                Evidence("config_path", str(config_path)),
                Evidence("previous_status", previous_status),
                Evidence("new_status", new_status),
                Evidence("status_flipped", flipped),
                Evidence("cron", _cron_of(raw)),
                Evidence("gate_history_verified", True),
                Evidence("registry_synced", True),
                Evidence("authorization_audit_path", str(audit_path)),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=audit_path,
        )


def _blackbox_config_failure(started_at: str, config_path: Path, exc: Exception) -> GateResult:
    return GateResult(
        gate_name="activate",
        status=GateStatus.FAILED,
        passed=False,
        evidence=[
            Evidence("config_path", str(config_path)),
            Evidence("runtime_type", "blackbox_v2"),
        ],
        errors=[f"Blackbox config failed strict loading: {exc}"],
        started_at=started_at,
        finished_at=utc_now(),
    )


def _cron_of(raw: dict) -> str | None:
    schedule = raw.get("schedule")
    if isinstance(schedule, dict):
        cron = schedule.get("cron")
        return str(cron) if cron else None
    return None


def _flip_status_to_active(config_path: Path) -> bool:
    """将 config.yaml 中 status 行从 paused 翻转为 active（保留其余文本）。"""
    return _set_status(config_path, "active")


def _set_status(config_path: Path, status: str) -> bool:
    """替换 config.yaml 中的 status 行（保留其余文本）。"""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    flipped = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("status:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"status: {status}{newline}"
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
    "static", "input", "unit", "dry-run", "compare", "backtest", "api-readiness",
})


def _sync_registry_after_activation(ctx: GateContext) -> str:
    """激活 config 后同步 registry 与 scheme version，并返回激活后的版本号。"""
    from scheduler.discovery import discover_schemes
    from scheduler.repository import sync_scheme_registry

    schemes_root = ctx.project_root / "schemes"
    configs = discover_schemes(schemes_root=schemes_root, strict=False)
    target = next((cfg for cfg in configs if cfg.scheme_id == ctx.scheme_id), None)
    if target is None:
        raise ValueError(f"scheme not discovered after activation: {ctx.scheme_id}")
    if target.status != "active":
        raise ValueError(f"activated config status is not active: {target.status}")
    engine = ctx.engine_factory() if ctx.engine_factory is not None else _db_engine()
    owns_engine = ctx.engine_factory is None
    if engine is None:
        raise RuntimeError("cannot connect to database for registry sync")
    try:
        sync_scheme_registry(engine, configs)
    finally:
        if owns_engine and engine is not None and hasattr(engine, "dispose"):
            engine.dispose()
    return target.scheme_version


def _verify_gate_history(ctx: GateContext, scheme_version: str) -> list[str]:
    """查询 t_harness_gate_results 确认当前版本已通过全部必要 gate。

    返回错误列表，空列表 = 校验通过。
    """
    from sqlalchemy import text

    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    raw_config = load_config_raw(config_path) if config_path.exists() else {}
    backtest_config = raw_config.get("backtest") if isinstance(raw_config.get("backtest"), dict) else {}
    benchmark_required = bool(backtest_config.get("benchmark_required"))
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
                    ORDER BY finished_at DESC, harness_run_id DESC
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

            gate_statuses = {str(row[0]): str(row[1]) for row in rows}
            if benchmark_required and gate_statuses.get("compare") == "skipped":
                return [
                    f"CompareGate status is skipped for benchmark_required scheme {ctx.scheme_id}; "
                    "run compare with original/current benchmarks until status=passed"
                ]
            passed_gates = {
                gate_name
                for gate_name, status in gate_statuses.items()
                if status == "passed" or (status == "skipped" and not benchmark_required)
            }
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
