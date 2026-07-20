from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.authorization import (
    authorization_signing_enabled,
    mark_token_used,
    required_future_expiry_errors,
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
    snapshot_scheme_counts_conn,
    snapshot_table_counts,
    snapshot_table_counts_conn,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import read_blackbox_execution_approval


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
        cfg = _load_config_for_execution(ctx)
        runtime_type = getattr(cfg, "runtime_type", "native_adapter")
        auth = None
        passed_run = None
        preflight_errors: list[str] = []
        if runtime_type == "blackbox_v2":
            auth, preflight_errors = _verify_blackbox_live_authorization(ctx, cfg)
            if preflight_errors:
                return _blocked_blackbox_live(
                    ctx,
                    started_at,
                    preflight_errors,
                    scheme_version=cfg.scheme_version,
                )

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        if runtime_type == "blackbox_v2":
            try:
                before, scheme_before = _snapshot_blackbox_live_counts(
                    engine,
                    ctx.scheme_id,
                    stage="baseline",
                )
            except _BlackboxCountSnapshotError as exc:
                if engine is not None and hasattr(engine, "dispose"):
                    engine.dispose()
                return _failed_blackbox_count_snapshot(
                    ctx,
                    started_at,
                    cfg=cfg,
                    failure=exc,
                )
        else:
            before = snapshot_table_counts(engine, PROTECTED_TABLES)
            scheme_before = _safe_scheme_counts(engine, ctx.scheme_id)
        audit_path: Path | None = None
        run_output = None
        errors: list[str] = []
        status = GateStatus.BLOCKED
        count_snapshot_failure: _BlackboxCountSnapshotError | None = None
        after: dict[str, int] | None = None
        scheme_after: dict[str, int] | None = None
        try:
            if runtime_type == "blackbox_v2":
                try:
                    passed_run = _verify_blackbox_passed_all(engine, cfg)
                    if auth is None:
                        auth_errors = ["Blackbox live authorization is unavailable"]
                    elif auth.harness_run_id != passed_run.harness_run_id:
                        auth_errors = [
                            "authorization harness_run_id must match latest passed all-stage run: "
                            f"token={auth.harness_run_id}, latest={passed_run.harness_run_id}"
                        ]
                    else:
                        approval = read_blackbox_execution_approval(engine, cfg)
                        auth_errors = [] if approval.executable else [
                            "Blackbox live requires exact active production approval: "
                            f"{approval.reason}"
                        ]
                except Exception as exc:  # noqa: BLE001
                    auth_errors = [f"Blackbox live preflight failed: {exc}"]
            else:
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
                if (
                    runtime_type == "blackbox_v2"
                    and (cfg.status != "active" or cfg.version_status != "active")
                ):
                    errors.append(
                        "Blackbox live requires actual config active+active: "
                        f"got={cfg.status}+{cfg.version_status}"
                    )
                else:
                    audit_dir = _audit_dir(ctx)
                    mark_token_used(auth, used_tokens_path(ctx.project_root))
                    audit_path = write_authorization_audit(auth, audit_dir)
                    cfg_for_run = (
                        cfg
                        if getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2"
                        else replace(cfg, status="active")
                    )
                    execute_kwargs = {
                        "algo_env": ctx.algo_env,
                        "timeout_sec": ctx.timeout_sec,
                        "prediction_phase": ctx.prediction_phase,
                    }
                    if runtime_type == "blackbox_v2":
                        execute_kwargs["blackbox_precommit_validator"] = (
                            lambda conn: _validate_blackbox_precommit_deltas(
                                conn,
                                before=before,
                                scheme_before=scheme_before,
                                scheme_id=ctx.scheme_id,
                            )
                        )
                    run_output = execute_scheme(
                        cfg_for_run,
                        ctx.predict_date,
                        **execute_kwargs,
                    )
                    status = GateStatus.PASSED
        finally:
            try:
                if runtime_type == "blackbox_v2":
                    try:
                        after, scheme_after = _snapshot_blackbox_live_counts(
                            engine,
                            ctx.scheme_id,
                            stage="after",
                        )
                    except _BlackboxCountSnapshotError as exc:
                        count_snapshot_failure = exc
                else:
                    after = snapshot_table_counts(engine, PROTECTED_TABLES)
                    scheme_after = _safe_scheme_counts(engine, ctx.scheme_id)
            finally:
                if engine is not None and hasattr(engine, "dispose"):
                    engine.dispose()

        protected_delta = diff_snapshots(before, after) if after is not None else None
        scheme_delta = diff_snapshots(scheme_before, scheme_after) if scheme_after is not None else None

        if count_snapshot_failure is not None:
            errors.append(str(count_snapshot_failure))
            status = GateStatus.FAILED
        elif status != GateStatus.BLOCKED:
            errors.extend(
                _validate_live_deltas(
                    protected_delta or {},
                    scheme_delta or {},
                    strict_blackbox_scheme=runtime_type == "blackbox_v2",
                )
            )
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
                Evidence("scheme_version", cfg.scheme_version if runtime_type == "blackbox_v2" else None),
                Evidence(
                    "harness_run_id",
                    passed_run.harness_run_id if passed_run is not None else None,
                ),
                Evidence("prediction_phase", ctx.prediction_phase),
                Evidence("authorization_audit_path", str(audit_path) if audit_path else None),
                Evidence("protected_table_counts_before", before),
                Evidence("protected_table_counts_after", after),
                Evidence("protected_table_deltas", protected_delta),
                Evidence("authorized_scheme_counts_before", scheme_before),
                Evidence("authorized_scheme_counts_after", scheme_after),
                Evidence("authorized_scheme_table_deltas", scheme_delta),
                Evidence(
                    "count_snapshot_stage",
                    count_snapshot_failure.stage if count_snapshot_failure is not None else None,
                ),
                Evidence(
                    "count_snapshot_error",
                    str(count_snapshot_failure) if count_snapshot_failure is not None else None,
                ),
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


def _verify_blackbox_live_authorization(ctx: GateContext, cfg):
    if not authorization_signing_enabled():
        return None, ["Blackbox live requires HMAC signing via HARNESS_AUTH_SECRET"]
    if not isinstance(ctx.authorization, str):
        return None, ["Blackbox live requires the original signed token string"]
    auth, errors = verify_authorization(
        ctx.authorization,
        scheme_id=ctx.scheme_id,
        action="live_write",
        predict_date=ctx.predict_date,
        used_store_path=used_tokens_path(ctx.project_root),
    )
    if auth is None:
        return None, errors
    errors.extend(required_future_expiry_errors(auth.issued_at, auth.expires_at))
    if not auth.issued_by.strip():
        errors.append("Blackbox live authorization requires non-empty issued_by")
    if auth.scheme_version != cfg.scheme_version:
        errors.append(
            "authorization scheme_version must match current canonical version: "
            f"token={auth.scheme_version}, current={cfg.scheme_version}"
        )
    if cfg.status != "active" or cfg.version_status != "active":
        errors.append(
            "Blackbox live requires actual config active+active: "
            f"got={cfg.status}+{cfg.version_status}"
        )
    return auth, errors


def _verify_blackbox_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all

    return _verify_passed_all(engine, cfg)


def _blocked_blackbox_live(
    ctx: GateContext,
    started_at: str,
    errors: list[str],
    *,
    scheme_version: str,
) -> GateResult:
    return GateResult(
        gate_name="live",
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[
            Evidence("authorized_scheme", ctx.scheme_id),
            Evidence("scheme_version", scheme_version),
            Evidence("harness_run_id", None),
            Evidence("prediction_phase", ctx.prediction_phase),
            Evidence("protected_table_counts_before", {}),
            Evidence("protected_table_counts_after", {}),
            Evidence("protected_table_deltas", {}),
            Evidence("authorized_scheme_counts_before", {}),
            Evidence("authorized_scheme_counts_after", {}),
            Evidence("authorized_scheme_table_deltas", {}),
            Evidence("run_result", None),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


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


class _BlackboxCountSnapshotError(RuntimeError):
    def __init__(self, *, stage: str, scope: str, cause: Exception) -> None:
        self.stage = stage
        self.scope = scope
        super().__init__(
            f"Blackbox LiveGate {stage} {scope} count snapshot failed: "
            f"{type(cause).__name__}: {cause}"
        )


def _snapshot_blackbox_live_counts(
    engine,
    scheme_id: str,
    *,
    stage: str,
) -> tuple[dict[str, int], dict[str, int]]:
    try:
        protected = snapshot_table_counts(engine, PROTECTED_TABLES)
    except Exception as exc:  # noqa: BLE001
        raise _BlackboxCountSnapshotError(
            stage=stage,
            scope="protected-table",
            cause=exc,
        ) from exc
    try:
        scheme = snapshot_scheme_counts(engine, scheme_id)
    except Exception as exc:  # noqa: BLE001
        raise _BlackboxCountSnapshotError(
            stage=stage,
            scope="scheme-table",
            cause=exc,
        ) from exc
    return protected, scheme


def _failed_blackbox_count_snapshot(
    ctx: GateContext,
    started_at: str,
    *,
    cfg,
    failure: _BlackboxCountSnapshotError,
) -> GateResult:
    return GateResult(
        gate_name="live",
        status=GateStatus.FAILED,
        passed=False,
        evidence=[
            Evidence("authorized_scheme", ctx.scheme_id),
            Evidence("scheme_version", cfg.scheme_version),
            Evidence("harness_run_id", None),
            Evidence("prediction_phase", ctx.prediction_phase),
            Evidence("authorization_audit_path", None),
            Evidence("protected_table_counts_before", None),
            Evidence("protected_table_counts_after", None),
            Evidence("protected_table_deltas", None),
            Evidence("authorized_scheme_counts_before", None),
            Evidence("authorized_scheme_counts_after", None),
            Evidence("authorized_scheme_table_deltas", None),
            Evidence("count_snapshot_stage", failure.stage),
            Evidence("count_snapshot_error", str(failure)),
            Evidence("run_result", None),
        ],
        errors=[str(failure)],
        started_at=started_at,
        finished_at=utc_now(),
    )


def _validate_live_deltas(
    protected_delta: dict[str, int],
    scheme_delta: dict[str, int],
    *,
    strict_blackbox_scheme: bool = False,
) -> list[str]:
    errors: list[str] = []
    for table, delta in protected_delta.items():
        if table in LIVE_WRITE_ALLOWED_TABLES:
            if delta <= 0:
                errors.append(f"{table} delta must be > 0 for authorized live write, got {delta}")
        elif delta != 0:
            errors.append(f"{table} delta must remain 0, got {delta}")
    for table in LIVE_WRITE_ALLOWED_TABLES:
        delta = scheme_delta.get(table, 0)
        if strict_blackbox_scheme:
            if delta != 1:
                errors.append(f"{table} authorized scheme delta must be exactly 1, got {delta}")
        elif delta <= 0:
            errors.append(f"{table} authorized scheme delta must be > 0, got {delta}")
    return errors


def _validate_blackbox_precommit_deltas(
    conn,
    *,
    before: dict[str, int],
    scheme_before: dict[str, int],
    scheme_id: str,
) -> None:
    """在成功事务提交前执行 LiveGate 表增量验证。"""
    after = snapshot_table_counts_conn(conn, PROTECTED_TABLES)
    scheme_after = snapshot_scheme_counts_conn(conn, scheme_id)
    errors = _validate_live_deltas(
        diff_snapshots(before, after),
        diff_snapshots(scheme_before, scheme_after),
        strict_blackbox_scheme=True,
    )
    if errors:
        raise RuntimeError("Blackbox LiveGate precommit validation failed: " + "; ".join(errors))


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
