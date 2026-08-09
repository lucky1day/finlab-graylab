from __future__ import annotations

from copy import copy
from dataclasses import is_dataclass, replace
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
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import (
    activate_blackbox_revision,
    read_blackbox_revision_activation_preflight,
)
from shared.blackbox_v2.lifecycle import (
    assert_lifecycle_clear,
    lifecycle_operation_lock,
)


class BlackboxRevisionActivateGate(Gate):
    """受控原子切换已上线 Blackbox 的同一业务身份修订。"""

    name = "revision-activate"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        if not authorization_signing_enabled():
            return _blocked(
                started_at,
                [
                    "Blackbox revision activation requires HMAC signing via "
                    "HARNESS_AUTH_SECRET"
                ],
            )
        if not isinstance(ctx.authorization, str):
            return _blocked(
                started_at,
                [
                    "Blackbox revision activation requires the original signed "
                    "token string"
                ],
            )
        auth, errors = verify_authorization(
            ctx.authorization,
            scheme_id=cfg.scheme_id,
            action="blackbox_revision_activate",
            predict_date=ctx.predict_date,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth is not None:
            errors.extend(required_future_expiry_errors(auth.issued_at, auth.expires_at))
            if not isinstance(auth.issued_by, str) or not auth.issued_by.strip():
                errors.append(
                    "Blackbox revision activation authorization requires non-empty "
                    "issued_by"
                )
            if auth.scheme_version != cfg.scheme_version:
                errors.append(
                    "authorization scheme_version must match current canonical "
                    f"version: token={auth.scheme_version}, current={cfg.scheme_version}"
                )
        if cfg.status != "active" or cfg.version_status != "active":
            errors.append(
                "Blackbox revision activation requires config active+active: "
                f"got={cfg.status}+{cfg.version_status}"
            )
        if auth is None or errors:
            return _blocked(started_at, errors)

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        try:
            return _run_with_engine(ctx, started_at, cfg, auth, engine)
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()


def _run_with_engine(
    ctx: GateContext,
    started_at: str,
    cfg: SchemeConfig,
    auth,
    engine,
) -> GateResult:
    """在已创建 engine 的边界内运行，并让调用方统一释放连接池。"""
    try:
        with lifecycle_operation_lock(ctx.project_root, cfg.scheme_id):
            return _run_with_engine_locked(ctx, started_at, cfg, auth, engine)
    except Exception as exc:  # noqa: BLE001
        return _failed(
            started_at,
            [f"Blackbox revision activation lifecycle lock failed: {exc}"],
        )


def _run_with_engine_locked(
    ctx: GateContext,
    started_at: str,
    cfg: SchemeConfig,
    auth,
    engine,
) -> GateResult:
    """在同身份 lifecycle 互斥锁内完成 preflight、token 消费与原子切换。"""
    audit_path: Path | None = None
    try:
        assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
        pinned_cfg = _reload_pinned_canonical(cfg)
        passed_run = _verify_passed_all(engine, pinned_cfg)
        passed_predict_date = _passed_run_predict_date(passed_run, pinned_cfg)
        if auth.harness_run_id != passed_run.harness_run_id:
            return _blocked(
                started_at,
                [
                    "authorization harness_run_id must match latest passed "
                    "all-stage run: "
                    f"token={auth.harness_run_id}, latest={passed_run.harness_run_id}"
                ],
            )
        if ctx.predict_date != passed_predict_date or auth.predict_date != passed_predict_date:
            return _blocked(
                started_at,
                [
                    "revision-activate predict_date must match latest passed "
                    "all-stage run: "
                    f"token={auth.predict_date}, ctx={ctx.predict_date}, "
                    f"latest={passed_predict_date}"
                ],
            )
        evidence_errors = _execution_evidence_errors(ctx, pinned_cfg, passed_run)
        if evidence_errors:
            return _blocked(started_at, evidence_errors)
        enriched_cfg = _with_execution_evidence(
            pinned_cfg,
            environment_fingerprint=str(passed_run.environment_fingerprint),
            data_snapshot_id=str(passed_run.data_snapshot_id),
        )
        preflight = read_blackbox_revision_activation_preflight(engine, enriched_cfg)
    except ValueError as exc:
        return _blocked(started_at, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        return _failed(
            started_at,
            [f"Blackbox revision activation preflight failed: {exc}"],
        )

    try:
        # Config/lifecycle must still be pinned at the irreversible token-use
        # boundary.  The lock prevents cooperating lifecycle operations from
        # changing it between this check and the DB transaction.
        final_cfg = _reload_pinned_canonical(cfg)
        assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
        enriched_cfg = _with_execution_evidence(
            final_cfg,
            environment_fingerprint=str(passed_run.environment_fingerprint),
            data_snapshot_id=str(passed_run.data_snapshot_id),
        )
    except ValueError as exc:
        return _blocked(started_at, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        return _failed(
            started_at,
            [
                "Blackbox revision activation final canonical preflight "
                f"failed: {exc}"
            ],
        )

    try:
        audit_path = write_authorization_audit(
            auth,
            ctx.report_dir / "revision_activate_authorization",
        )
        mark_token_used(auth, used_tokens_path(ctx.project_root))
        state = activate_blackbox_revision(
            engine,
            enriched_cfg,
            prior_scheme_version=preflight.prior_scheme_version,
            pending_scheme_versions=preflight.pending_scheme_versions,
            expected_harness_run_id=passed_run.harness_run_id,
            approved_by=auth.issued_by,
            approved_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001
        return _failed(
            started_at,
            [f"Blackbox revision activation failed: {exc}"],
            evidence=[
                Evidence("authorization_audit_path", str(audit_path) if audit_path else None),
                Evidence("business_tables_written", False),
                Evidence("config_changed", False),
            ],
            report_path=audit_path,
        )

    return GateResult(
        gate_name="revision-activate",
        status=GateStatus.PASSED,
        passed=True,
        evidence=[
            Evidence("scheme_id", state.scheme_id),
            Evidence("scheme_version", state.scheme_version),
            Evidence("harness_run_id", passed_run.harness_run_id),
            Evidence("prior_scheme_version", preflight.prior_scheme_version),
            Evidence("retired_scheme_version", preflight.prior_scheme_version),
            Evidence(
                "retired_pending_versions",
                list(preflight.pending_scheme_versions),
            ),
            Evidence("version_status", state.version_status),
            Evidence("registry_status", state.registry_status),
            Evidence("registry_scheme_ids", list(state.registry_scheme_ids)),
            Evidence("runtime_profile", passed_run.runtime_profile),
            Evidence("environment_fingerprint", state.environment_fingerprint),
            Evidence("generation_id", passed_run.generation_id),
            Evidence("data_snapshot_id", state.data_snapshot_id),
            Evidence("approved_by", state.approved_by),
            Evidence(
                "approved_at",
                state.approved_at.isoformat() if state.approved_at is not None else None,
            ),
            Evidence("business_tables_written", False),
            Evidence("config_changed", False),
            Evidence("authorization_audit_path", str(audit_path)),
        ],
        errors=[],
        started_at=started_at,
        finished_at=utc_now(),
        report_path=audit_path,
    )


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(
        ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    )
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(
            "Blackbox revision activation requires runtime_type=blackbox_v2, "
            f"got {cfg.runtime_type}"
        )
    return cfg


def _verify_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all as verify

    return verify(engine, cfg)


def _environment_fingerprint(project_root: Path) -> str:
    from harness.blackbox_v2.gates import _environment_fingerprint as fingerprint

    return fingerprint(project_root)


def _passed_run_predict_date(passed_run, cfg: SchemeConfig) -> str:
    from harness.blackbox_v2.draft_register import _passed_run_predict_date as read_date

    return read_date(passed_run, cfg)


def _execution_evidence_errors(ctx: GateContext, cfg: SchemeConfig, passed_run) -> list[str]:
    errors: list[str] = []
    if passed_run.runtime_profile != cfg.runtime_profile:
        errors.append(
            "all-stage runtime_profile mismatch: "
            f"{passed_run.runtime_profile} != {cfg.runtime_profile}"
        )
    if passed_run.environment_fingerprint != _environment_fingerprint(ctx.project_root):
        errors.append(
            "all-stage environment fingerprint does not match frozen environment "
            "manifest"
        )
    if not isinstance(passed_run.generation_id, str) or not passed_run.generation_id.strip():
        errors.append("all-stage DataBridge generation_id evidence is missing")
    if (
        not isinstance(passed_run.data_snapshot_id, str)
        or not passed_run.data_snapshot_id.strip()
    ):
        errors.append("all-stage data_snapshot_id evidence is missing")
    return errors


def _with_execution_evidence(
    cfg: SchemeConfig,
    *,
    environment_fingerprint: str,
    data_snapshot_id: str,
) -> SchemeConfig:
    if is_dataclass(cfg):
        return replace(
            cfg,
            environment_fingerprint=environment_fingerprint,
            data_snapshot_id=data_snapshot_id,
        )
    copied = copy(cfg)
    copied.environment_fingerprint = environment_fingerprint
    copied.data_snapshot_id = data_snapshot_id
    return copied


def _reload_pinned_canonical(cfg: SchemeConfig) -> SchemeConfig:
    current = load_scheme_config(cfg.path / "config.yaml")
    fields = (
        "scheme_id",
        "scheme_version",
        "code_hash",
        "config_hash",
        "manifest_hash",
        "runtime_type",
        "status",
        "version_status",
        "algorithm_version",
        "contract_version",
        "runtime_profile",
        "data_schema_version",
        "input_source",
        "platform_inputs",
        "horizon",
        "task_type",
        "tenors",
        "frequency",
        "target_rule",
    )
    mismatches = [
        f"{field}: initial={getattr(cfg, field)!r}, current={getattr(current, field)!r}"
        for field in fields
        if getattr(cfg, field) != getattr(current, field)
    ]
    if mismatches:
        raise ValueError(
            "Blackbox canonical delivery drift before revision activation: "
            + "; ".join(mismatches)
        )
    return current


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _blocked(started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name="revision-activate",
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[
            Evidence("authorization_required", True),
            Evidence("business_tables_written", False),
            Evidence("config_changed", False),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _failed(
    started_at: str,
    errors: list[str],
    *,
    evidence: list[Evidence] | None = None,
    report_path: Path | None = None,
) -> GateResult:
    return GateResult(
        gate_name="revision-activate",
        status=GateStatus.FAILED,
        passed=False,
        evidence=evidence
        or [
            Evidence("business_tables_written", False),
            Evidence("config_changed", False),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
        report_path=report_path,
    )
