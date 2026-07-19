from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.prediction_semantics import LIVE_PHASES
from harness.probes.table_guard import (
    LIVE_WRITE_ALLOWED_TABLES,
    PROTECTED_TABLES,
    diff_snapshots,
    snapshot_scheme_counts,
    snapshot_table_counts,
)
from harness.result import Evidence, GateResult, GateStatus


class LiveGate(Gate):
    name = "live"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        lifecycle_error = _blackbox_lifecycle_error(ctx)
        if lifecycle_error is not None:
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[Evidence("lifecycle_clear", False)],
                errors=[lifecycle_error],
                started_at=started_at,
                finished_at=utc_now(),
            )
        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        before = snapshot_table_counts(engine, PROTECTED_TABLES)
        scheme_before = _safe_scheme_counts(engine, ctx.scheme_id)
        audit_path: Path | None = None
        run_output = None
        errors: list[str] = []
        status = GateStatus.BLOCKED
        try:
            auth, auth_errors = verify_authorization(
                ctx.authorization,
                scheme_id=ctx.scheme_id,
                action="live_write",
                predict_date=ctx.predict_date,
                used_store_path=used_tokens_path(ctx.project_root),
            )
            if auth_errors:
                errors.extend(auth_errors)
            elif ctx.prediction_phase not in LIVE_PHASES:
                errors.append(f"live gate requires explicit prediction_phase in {sorted(LIVE_PHASES)}, got {ctx.prediction_phase}")
            else:
                cfg = _load_config_for_execution(ctx)
                if (
                    getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2"
                    and (cfg.status != "active" or cfg.version_status != "active")
                ):
                    errors.append(
                        "Blackbox live requires actual config active+active: "
                        f"got={cfg.status}+{cfg.version_status}"
                    )
                else:
                    audit_dir = _audit_dir(ctx)
                    audit_path = write_authorization_audit(auth, audit_dir)
                    mark_token_used(auth, used_tokens_path(ctx.project_root))
                    cfg_for_run = (
                        cfg
                        if getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2"
                        else replace(cfg, status="active")
                    )
                    run_output = execute_scheme(
                        cfg_for_run,
                        ctx.predict_date,
                        algo_env=ctx.algo_env,
                        timeout_sec=ctx.timeout_sec,
                        prediction_phase=ctx.prediction_phase,
                    )
                    status = GateStatus.PASSED
        finally:
            after = snapshot_table_counts(engine, PROTECTED_TABLES)
            scheme_after = _safe_scheme_counts(engine, ctx.scheme_id)
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        protected_delta = diff_snapshots(before, after)
        scheme_delta = diff_snapshots(scheme_before, scheme_after)

        if status != GateStatus.BLOCKED:
            errors.extend(_validate_live_deltas(protected_delta, scheme_delta))
            if run_output is None:
                errors.append("execute_scheme did not return a result")
            else:
                if getattr(run_output, "status", "") != "success":
                    errors.append(f"execute_scheme status must be success, got {getattr(run_output, 'status', None)}")
                if int(getattr(run_output, "records_written", 0) or 0) <= 0:
                    errors.append("execute_scheme records_written must be > 0")
            status = GateStatus.PASSED if not errors else GateStatus.FAILED

        finished_at = utc_now()
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("authorized_scheme", ctx.scheme_id),
                Evidence("prediction_phase", ctx.prediction_phase),
                Evidence("authorization_audit_path", str(audit_path) if audit_path else None),
                Evidence("protected_table_counts_before", before),
                Evidence("protected_table_counts_after", after),
                Evidence("protected_table_deltas", protected_delta),
                Evidence("authorized_scheme_counts_before", scheme_before),
                Evidence("authorized_scheme_counts_after", scheme_after),
                Evidence("authorized_scheme_table_deltas", scheme_delta),
                Evidence("run_result", _run_result_payload(run_output)),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=audit_path,
        )


def execute_scheme(*args, **kwargs):
    from scheduler.executor import execute_scheme as executor_execute_scheme

    return executor_execute_scheme(*args, **kwargs)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _load_config_for_execution(ctx: GateContext):
    from scheduler.discovery import load_scheme_config

    return load_scheme_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")


def _blackbox_lifecycle_error(ctx: GateContext) -> str | None:
    try:
        cfg = ctx.config or _load_config_for_execution(ctx)
    except Exception:
        return None
    if getattr(cfg, "runtime_type", "native_adapter") != "blackbox_v2":
        return None
    try:
        from shared.blackbox_v2.lifecycle import assert_lifecycle_clear

        assert_lifecycle_clear(ctx.project_root, ctx.scheme_id)
    except RuntimeError as exc:
        return str(exc)
    return None


def _safe_scheme_counts(engine, scheme_id: str) -> dict[str, int]:
    try:
        return snapshot_scheme_counts(engine, scheme_id)
    except Exception:
        return {table: 0 for table in LIVE_WRITE_ALLOWED_TABLES}


def _validate_live_deltas(protected_delta: dict[str, int], scheme_delta: dict[str, int]) -> list[str]:
    errors: list[str] = []
    for table, delta in protected_delta.items():
        if table in LIVE_WRITE_ALLOWED_TABLES:
            if delta <= 0:
                errors.append(f"{table} delta must be > 0 for authorized live write, got {delta}")
        elif delta != 0:
            errors.append(f"{table} delta must remain 0, got {delta}")
    for table in LIVE_WRITE_ALLOWED_TABLES:
        if scheme_delta.get(table, 0) <= 0:
            errors.append(f"{table} authorized scheme delta must be > 0, got {scheme_delta.get(table, 0)}")
    return errors


def _audit_dir(ctx: GateContext) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ctx.report_dir / stamp


def _run_result_payload(run_output) -> dict | None:
    if run_output is None:
        return None
    try:
        return asdict(run_output)
    except TypeError:
        return {
            "status": getattr(run_output, "status", None),
            "records_written": getattr(run_output, "records_written", None),
            "error_msg": getattr(run_output, "error_msg", None),
        }
