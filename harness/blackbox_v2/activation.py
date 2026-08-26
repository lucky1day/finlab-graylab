from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from harness.operation import (
    operation_scope_sha256,
    verify_direct_operation,
)
from harness.context import GateContext
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import (
    BlackboxLifecycleIdentityAbsent,
    activate_blackbox_revision,
    apply_blackbox_lifecycle_state,
    register_blackbox_draft_identity,
    read_blackbox_lifecycle_state,
    read_blackbox_revision_activation_preflight,
)
from shared.blackbox_v2.lifecycle import (
    LifecycleOperationError,
    LifecycleState,
    assert_lifecycle_clear,
    lifecycle_operation_lock,
    pending_journals,
    perform_lifecycle_transition,
    reconcile_journal,
)


def activate_blackbox(ctx: GateContext) -> GateResult:
    """使用唯一入口激活首次上线或同一业务身份修订。"""
    return guarded_result("activate", lambda started_at: _activate(ctx, started_at))


def _activate(ctx: GateContext, started_at: str) -> GateResult:
    cfg = _config(ctx)
    if cfg.status == "paused" and cfg.version_status == "draft":
        return _activate_initial(ctx, started_at, cfg)
    if cfg.status == "active" and cfg.version_status == "active":
        return _activate_revision(ctx, started_at, cfg)
    return _blocked(
        started_at,
        [
            "Blackbox activation requires paused+draft for initial activation "
            "or active+active for a revision: "
            f"got={cfg.status}+{cfg.version_status}"
        ],
    )


def _activate_initial(
    ctx: GateContext,
    started_at: str,
    cfg: SchemeConfig,
) -> GateResult:
    try:
        assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
    except RuntimeError as exc:
        return _blocked(started_at, [str(exc)])
    engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
    identity_created = False
    try:
        _validate_canonical_delivery(cfg)
        passed_backtest = _verify_passed_backtest(engine, cfg)
        operation, errors = verify_direct_operation(
            ctx.operation,
            scheme_id=ctx.scheme_id,
            action="blackbox_activate",
            scheme_version=cfg.scheme_version,
        )
        if operation is None or errors:
            return _blocked(started_at, errors)
        evidence_errors = _validate_execution_evidence(ctx, cfg, passed_backtest)
        if evidence_errors:
            return _blocked(started_at, evidence_errors)

        enriched_cfg = replace(
            cfg,
            environment_fingerprint=passed_backtest.environment_fingerprint,
            data_snapshot_id=passed_backtest.data_snapshot_id,
        )
        try:
            db_state = read_blackbox_lifecycle_state(engine, enriched_cfg)
        except BlackboxLifecycleIdentityAbsent:
            if cfg.version_status != "draft":
                raise
            register_blackbox_draft_identity(engine, enriched_cfg)
            identity_created = True
            db_state = read_blackbox_lifecycle_state(engine, enriched_cfg)

        evidence_errors = _validate_initial_state(cfg, db_state, passed_backtest)
        if evidence_errors:
            return _blocked(started_at, evidence_errors)

        previous = LifecycleState(
            "paused",
            db_state.version_status,
            db_state.registry_status,
            config_version_status=cfg.version_status,
        )
        target = LifecycleState("active", "active", "active")
        approved_at = datetime.now(timezone.utc)

        def apply_database(state: LifecycleState) -> None:
            current = _reload_pinned_with_evidence(enriched_cfg, cfg.scheme_version)
            if state.version_status == "active":
                apply_blackbox_lifecycle_state(
                    engine,
                    current,
                    version_status="active",
                    registry_status="active",
                    approved_by=operation.issued_by,
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
            evidence_run_id=f"backtest:{passed_backtest.backtest_run_id}",
            previous=previous,
            target=target,
            compensation=previous,
            operation_scope_sha256=operation_scope_sha256(operation),
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

    return GateResult(
        gate_name="activate",
        status=GateStatus.PASSED,
        evidence=[
            Evidence("scheme_id", cfg.scheme_id),
            Evidence("scheme_version", cfg.scheme_version),
            Evidence("backtest_run_id", passed_backtest.backtest_run_id),
            Evidence("benchmark_id", passed_backtest.benchmark_id),
            Evidence("runtime_profile", passed_backtest.runtime_profile),
            Evidence("environment_fingerprint", passed_backtest.environment_fingerprint),
            Evidence("generation_id", passed_backtest.generation_id),
            Evidence("data_snapshot_id", passed_backtest.data_snapshot_id),
            Evidence("identity_created", identity_created),
            Evidence("approved_by", operation.issued_by),
            Evidence("approved_at", approved_at.isoformat()),
            Evidence("config_status", "active"),
            Evidence("version_status", "active"),
            Evidence("registry_status", "active"),
            Evidence("journal_path", str(journal_path)),
            Evidence("operator", operation.issued_by),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("activation_mode", "initial"),
        ],
        errors=[],
        started_at=started_at,
        finished_at=utc_now(),
    )


def _activate_revision(
    ctx: GateContext,
    started_at: str,
    cfg: SchemeConfig,
) -> GateResult:
    """在同一入口内原子切换已上线 Blackbox 的同身份修订。"""
    engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
    try:
        with lifecycle_operation_lock(ctx.project_root, cfg.scheme_id):
            assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
            pinned_cfg = _reload_pinned_canonical(cfg)
            _validate_canonical_delivery(pinned_cfg)
            passed_backtest = _verify_passed_backtest(engine, pinned_cfg)
            operation, errors = verify_direct_operation(
                ctx.operation,
                scheme_id=cfg.scheme_id,
                action="blackbox_activate",
                scheme_version=pinned_cfg.scheme_version,
            )
            if operation is None or errors:
                return _blocked(started_at, errors)
            evidence_errors = _validate_execution_evidence(
                ctx,
                pinned_cfg,
                passed_backtest,
            )
            if evidence_errors:
                return _blocked(started_at, evidence_errors)
            enriched_cfg = replace(
                pinned_cfg,
                environment_fingerprint=passed_backtest.environment_fingerprint,
                data_snapshot_id=passed_backtest.data_snapshot_id,
            )
            preflight = read_blackbox_revision_activation_preflight(
                engine,
                enriched_cfg,
            )

            final_cfg = _reload_pinned_canonical(cfg)
            assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
            enriched_cfg = replace(
                final_cfg,
                environment_fingerprint=passed_backtest.environment_fingerprint,
                data_snapshot_id=passed_backtest.data_snapshot_id,
            )
            approved_at = datetime.now(timezone.utc)
            state = activate_blackbox_revision(
                engine,
                enriched_cfg,
                prior_scheme_version=preflight.prior_scheme_version,
                pending_scheme_versions=preflight.pending_scheme_versions,
                approved_by=operation.issued_by,
                approved_at=approved_at,
            )
    except ValueError as exc:
        return _blocked(started_at, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        return _failed(
            started_at,
            [f"Blackbox revision activation failed: {exc}"],
            evidence=[
                Evidence("business_tables_written", False),
                Evidence("config_changed", False),
            ],
        )
    finally:
        if hasattr(engine, "dispose"):
            engine.dispose()

    return GateResult(
        gate_name="activate",
        status=GateStatus.PASSED,
        evidence=[
            Evidence("scheme_id", state.scheme_id),
            Evidence("scheme_version", state.scheme_version),
            Evidence("backtest_run_id", passed_backtest.backtest_run_id),
            Evidence("benchmark_id", passed_backtest.benchmark_id),
            Evidence("prior_scheme_version", preflight.prior_scheme_version),
            Evidence("retired_pending_versions", list(preflight.pending_scheme_versions)),
            Evidence("version_status", state.version_status),
            Evidence("registry_status", state.registry_status),
            Evidence("registry_scheme_ids", list(state.registry_scheme_ids)),
            Evidence("runtime_profile", passed_backtest.runtime_profile),
            Evidence("environment_fingerprint", state.environment_fingerprint),
            Evidence("generation_id", passed_backtest.generation_id),
            Evidence("data_snapshot_id", state.data_snapshot_id),
            Evidence("approved_by", state.approved_by),
            Evidence(
                "approved_at",
                state.approved_at.isoformat() if state.approved_at is not None else None,
            ),
            Evidence("business_tables_written", False),
            Evidence("config_changed", False),
            Evidence("operator", operation.issued_by),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("activation_mode", "revision"),
        ],
        errors=[],
        started_at=started_at,
        finished_at=utc_now(),
    )


class BlackboxLifecycleReconcileGate(Gate):
    name = "lifecycle-reconcile"

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
        operation, errors = verify_direct_operation(
            ctx.operation,
            scheme_id=cfg.scheme_id,
            action="blackbox_reconcile",
            scheme_version=cfg.scheme_version,
        )
        if operation is None or errors:
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
        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
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
            restored = reconcile_journal(
                path,
                apply_database=apply_database,
                read_state=read_state,
                operation_scope_sha256=operation_scope_sha256(operation),
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
            evidence=[
                Evidence("journal_path", str(path)),
                Evidence("restored_state", restored.__dict__),
                Evidence("actual_state_before", asdict(actual_before)),
                Evidence("actual_state_after", asdict(actual_after)),
                Evidence("promoted", False),
                Evidence("operator", operation.issued_by),
                Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            ],
            errors=[],
            started_at=started_at,
            finished_at=utc_now(),
        )


def _validate_execution_evidence(
    ctx: GateContext,
    cfg: SchemeConfig,
    passed_backtest,
) -> list[str]:
    errors: list[str] = []
    if passed_backtest.runtime_profile != cfg.runtime_profile:
        errors.append(
            "backtest runtime_profile mismatch: "
            f"{passed_backtest.runtime_profile} != {cfg.runtime_profile}"
        )
    if passed_backtest.environment_fingerprint != _environment_fingerprint(ctx.project_root):
        errors.append("backtest environment does not match frozen environment manifest")
    if not passed_backtest.generation_id.strip():
        errors.append("backtest DataBridge generation_id evidence is missing")
    if not passed_backtest.data_snapshot_id.strip():
        errors.append("backtest data_snapshot_id evidence is missing")
    return errors


def _validate_initial_state(cfg: SchemeConfig, db_state, passed_backtest) -> list[str]:
    errors: list[str] = []
    if db_state.scheme_id != cfg.scheme_id or db_state.scheme_version != cfg.scheme_version:
        errors.append("database lifecycle identity does not match current canonical version")
    if (
        db_state.version_status != "draft"
        or db_state.registry_status != "paused"
        or db_state.version_status != cfg.version_status
    ):
        errors.append(
            "Blackbox activation requires matching paused draft state: "
            f"config={cfg.version_status}, "
            f"database={db_state.version_status}+{db_state.registry_status}"
        )
    if db_state.environment_fingerprint != passed_backtest.environment_fingerprint:
        errors.append("database environment fingerprint does not match backtest evidence")
    if db_state.data_snapshot_id != passed_backtest.data_snapshot_id:
        errors.append("database data_snapshot_id does not match backtest evidence")
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
            "Blackbox canonical delivery drift before activation: "
            + "; ".join(mismatches)
        )
    return current


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(f"Blackbox activation requires runtime_type=blackbox_v2, got {cfg.runtime_type}")
    return cfg


def _verify_passed_backtest(engine, cfg):
    from harness.blackbox_v2.gates import verify_passed_blackbox_backtest as verify

    return verify(engine, cfg)


def _validate_canonical_delivery(cfg: SchemeConfig):
    from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery

    return validate_canonical_blackbox_delivery(cfg)


def _environment_fingerprint(project_root: Path) -> str:
    from harness.blackbox_v2.gates import _environment_fingerprint as fingerprint

    return fingerprint(project_root)


def _blocked(started_at: str, errors: list[str], *, gate_name: str = "activate") -> GateResult:
    return GateResult(
        gate_name=gate_name,
        status=GateStatus.BLOCKED,
        evidence=[Evidence("operation_required", True)],
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
        evidence=evidence or [],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
