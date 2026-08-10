from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.authorization import (
    authorization_token_hash,
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import (
    apply_blackbox_lifecycle_state,
    read_blackbox_lifecycle_state,
)
from shared.blackbox_v2.lifecycle import (
    LifecycleOperationError,
    LifecycleState,
    assert_lifecycle_clear,
    pending_journals,
    perform_lifecycle_transition,
    reconcile_journal,
)


def activate_blackbox(ctx: GateContext) -> GateResult:
    """执行签名授权、可补偿的 Blackbox 正式激活。"""
    return guarded_result("activate", lambda started_at: _activate(ctx, started_at))


def _activate(ctx: GateContext, started_at: str) -> GateResult:
    cfg = _config(ctx)
    try:
        assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
    except RuntimeError as exc:
        return _blocked(started_at, [str(exc)])
    errors: list[str] = []
    if cfg.status != "paused" or cfg.version_status != "shadow":
        errors.append(
            "Blackbox activation requires config paused+shadow: "
            f"got={cfg.status}+{cfg.version_status}"
        )
    if errors:
        return _blocked(started_at, errors)

    engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
    audit_path: Path | None = None
    try:
        passed_run = _verify_passed_all(engine, cfg)
        auth, errors = verify_authorization(
            ctx.authorization,
            scheme_id=ctx.scheme_id,
            action="blackbox_activate",
            scheme_version=cfg.scheme_version,
            harness_run_id=passed_run.harness_run_id,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth is None or errors:
            return _blocked(started_at, errors)
        evidence_errors = _validate_execution_evidence(ctx, cfg, passed_run)
        db_state = read_blackbox_lifecycle_state(engine, cfg)
        evidence_errors.extend(_validate_shadow_state(cfg, db_state, passed_run))
        if evidence_errors:
            return _blocked(started_at, evidence_errors)

        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        approved_at = datetime.now(timezone.utc)
        enriched_cfg = replace(
            cfg,
            environment_fingerprint=passed_run.environment_fingerprint,
            data_snapshot_id=passed_run.data_snapshot_id,
        )

        def consume() -> None:
            nonlocal audit_path
            mark_token_used(auth, used_tokens_path(ctx.project_root))
            audit_path = write_authorization_audit(auth, ctx.report_dir / "activation_authorization")

        def apply_database(state: LifecycleState) -> None:
            current = _reload_pinned_with_evidence(enriched_cfg, cfg.scheme_version)
            if state.version_status == "active":
                apply_blackbox_lifecycle_state(
                    engine,
                    current,
                    version_status="active",
                    registry_status="active",
                    approved_by=auth.issued_by,
                    approved_at=approved_at,
                )
            else:
                apply_blackbox_lifecycle_state(
                    engine,
                    current,
                    version_status=state.version_status,
                    registry_status=state.registry_status,
                )

        def read_state() -> LifecycleState:
            current = _reload_pinned_with_evidence(enriched_cfg, cfg.scheme_version)
            current_db = read_blackbox_lifecycle_state(engine, current)
            return LifecycleState(
                current.status,
                current_db.version_status,
                current_db.registry_status,
                config_version_status=current.version_status,
            )

        _, journal_path = perform_lifecycle_transition(
            project_root=ctx.project_root,
            config_path=cfg.path / "config.yaml",
            action="activate",
            scheme_id=cfg.scheme_id,
            scheme_version=cfg.scheme_version,
            harness_run_id=passed_run.harness_run_id,
            previous=previous,
            target=target,
            compensation=previous,
            token_hash=authorization_token_hash(auth),
            consume_authorization=consume,
            apply_database=apply_database,
            read_state=read_state,
        )
    except LifecycleOperationError as exc:
        return _failed(
            started_at,
            [str(exc)],
            evidence=[
                Evidence("journal_path", str(exc.journal_path)),
                Evidence("compensated", exc.compensated),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return _failed(started_at, [f"Blackbox activation preflight failed: {exc}"])
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()

    finished_at = utc_now()
    return GateResult(
        gate_name="activate",
        status=GateStatus.PASSED,
        passed=True,
        evidence=[
            Evidence("scheme_id", cfg.scheme_id),
            Evidence("scheme_version", cfg.scheme_version),
            Evidence("harness_run_id", passed_run.harness_run_id),
            Evidence("runtime_profile", passed_run.runtime_profile),
            Evidence("environment_fingerprint", passed_run.environment_fingerprint),
            Evidence("generation_id", passed_run.generation_id),
            Evidence("data_snapshot_id", passed_run.data_snapshot_id),
            Evidence("approved_by", auth.issued_by),
            Evidence("approved_at", approved_at.isoformat()),
            Evidence("config_status", "active"),
            Evidence("version_status", "active"),
            Evidence("registry_status", "active"),
            Evidence("journal_path", str(journal_path)),
            Evidence("authorization_audit_path", str(audit_path)),
        ],
        errors=[],
        started_at=started_at,
        finished_at=finished_at,
        report_path=audit_path,
    )


class BlackboxLifecycleReconcileGate(Gate):
    name = "lifecycle-reconcile"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        pending = pending_journals(ctx.project_root, cfg.scheme_id)
        if not pending:
            return _failed(
                started_at,
                ["no incomplete Blackbox lifecycle journal found"],
                gate_name=self.name,
            )
        if len(pending) != 1:
            return _blocked(
                started_at,
                [f"multiple incomplete lifecycle journals require inspection: {len(pending)}"],
                gate_name=self.name,
            )
        path, journal = pending[0]
        current_cfg = _reload_with_evidence(cfg)
        if journal.scheme_version != current_cfg.scheme_version:
            return _blocked(
                started_at,
                [
                    "reconciliation scheme_version mismatch: "
                    f"journal={journal.scheme_version}, "
                    f"current={current_cfg.scheme_version}"
                ],
                gate_name=self.name,
            )
        cfg = current_cfg
        auth, errors = verify_authorization(
            ctx.authorization,
            scheme_id=cfg.scheme_id,
            action="blackbox_reconcile",
            scheme_version=cfg.scheme_version,
            harness_run_id=journal.harness_run_id,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth is None or errors:
            return _blocked(started_at, errors, gate_name=self.name)
        if "active" in {
            journal.previous.config_status,
            str(journal.previous.config_version_status),
            journal.previous.version_status,
            journal.previous.registry_status,
        }:
            return _blocked(
                started_at,
                ["reconciliation previous state is not a non-active safe state"],
                gate_name=self.name,
            )
        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        audit_path: Path | None = None
        try:
            db_evidence = read_blackbox_lifecycle_state(engine, cfg)
        except Exception as exc:  # noqa: BLE001
            if hasattr(engine, "dispose"):
                engine.dispose()
            return _failed(
                started_at,
                [f"cannot read lifecycle evidence for reconciliation: {exc}"],
                gate_name=self.name,
            )
        enriched_cfg = replace(
            cfg,
            environment_fingerprint=db_evidence.environment_fingerprint,
            data_snapshot_id=db_evidence.data_snapshot_id,
        )

        def apply_database(state: LifecycleState) -> None:
            current = _reload_pinned_with_evidence(enriched_cfg, journal.scheme_version)
            apply_blackbox_lifecycle_state(
                engine,
                current,
                version_status=state.version_status,
                registry_status=state.registry_status,
            )

        def read_state() -> LifecycleState:
            current = _reload_pinned_with_evidence(enriched_cfg, journal.scheme_version)
            current_db = read_blackbox_lifecycle_state(engine, current)
            return LifecycleState(
                current.status,
                current_db.version_status,
                current_db.registry_status,
                config_version_status=current.version_status,
            )

        try:
            actual_before = read_state()
            mark_token_used(auth, used_tokens_path(ctx.project_root))
            audit_path = write_authorization_audit(auth, ctx.report_dir / "reconcile_authorization")
            restored = reconcile_journal(
                path,
                config_path=cfg.path / "config.yaml",
                apply_database=apply_database,
                read_state=read_state,
                token_hash=authorization_token_hash(auth),
            )
            actual_after = read_state()
        except Exception as exc:  # noqa: BLE001
            return _failed(
                started_at,
                [f"lifecycle reconciliation failed: {exc}"],
                gate_name=self.name,
            )
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("journal_path", str(path)),
                Evidence("restored_state", restored.__dict__),
                Evidence("actual_state_before", asdict(actual_before)),
                Evidence("actual_state_after", asdict(actual_after)),
                Evidence("promoted", False),
                Evidence("authorization_audit_path", str(audit_path)),
            ],
            errors=[],
            started_at=started_at,
            finished_at=utc_now(),
            report_path=audit_path,
        )


def _validate_execution_evidence(ctx: GateContext, cfg: SchemeConfig, passed_run) -> list[str]:
    errors: list[str] = []
    if passed_run.runtime_profile != cfg.runtime_profile:
        errors.append(
            f"all-stage runtime_profile mismatch: {passed_run.runtime_profile} != {cfg.runtime_profile}"
        )
    if passed_run.environment_fingerprint != _environment_fingerprint(ctx.project_root):
        errors.append("all-stage environment fingerprint does not match frozen environment manifest")
    if not isinstance(passed_run.generation_id, str) or not passed_run.generation_id.strip():
        errors.append("all-stage DataBridge generation_id evidence is missing")
    if not isinstance(passed_run.data_snapshot_id, str) or not passed_run.data_snapshot_id.strip():
        errors.append("all-stage data_snapshot_id evidence is missing")
    return errors


def _validate_shadow_state(cfg: SchemeConfig, db_state, passed_run) -> list[str]:
    errors: list[str] = []
    if db_state.scheme_id != cfg.scheme_id or db_state.scheme_version != cfg.scheme_version:
        errors.append("database lifecycle identity does not match current canonical version")
    if db_state.version_status != "shadow" or db_state.registry_status != "paused":
        errors.append(
            "Blackbox activation requires database shadow+paused: "
            f"got={db_state.version_status}+{db_state.registry_status}"
        )
    if db_state.environment_fingerprint != passed_run.environment_fingerprint:
        errors.append("database environment fingerprint does not match passed all-stage evidence")
    if db_state.data_snapshot_id != passed_run.data_snapshot_id:
        errors.append("database data_snapshot_id does not match passed all-stage evidence")
    return errors


def _reload_with_evidence(cfg: SchemeConfig) -> SchemeConfig:
    current = load_scheme_config(cfg.path / "config.yaml")
    return replace(
        current,
        environment_fingerprint=cfg.environment_fingerprint,
        data_snapshot_id=cfg.data_snapshot_id,
    )


def _reload_pinned_with_evidence(
    cfg: SchemeConfig,
    expected_scheme_version: str,
) -> SchemeConfig:
    current = _reload_with_evidence(cfg)
    if current.scheme_version != expected_scheme_version:
        raise RuntimeError(
            "Blackbox canonical version changed during lifecycle operation: "
            f"expected={expected_scheme_version}, current={current.scheme_version}"
        )
    return current


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(f"Blackbox activation requires runtime_type=blackbox_v2, got {cfg.runtime_type}")
    return cfg


def _verify_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all as verify

    return verify(engine, cfg)


def _environment_fingerprint(project_root: Path) -> str:
    from harness.blackbox_v2.gates import _environment_fingerprint as fingerprint

    return fingerprint(project_root)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _blocked(started_at: str, errors: list[str], *, gate_name: str = "activate") -> GateResult:
    return GateResult(
        gate_name=gate_name,
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[Evidence("authorization_required", True)],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _failed(
    started_at: str,
    errors: list[str],
    *,
    evidence: list[Evidence] | None = None,
    gate_name: str = "activate",
) -> GateResult:
    return GateResult(
        gate_name=gate_name,
        status=GateStatus.FAILED,
        passed=False,
        evidence=evidence or [],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
