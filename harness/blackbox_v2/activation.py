from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from harness.operation import (
    operation_scope_sha256,
    verify_direct_operation,
)
from harness.context import GateContext
from harness.gates.base import create_default_engine, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import (
    BlackboxLifecycleIdentityAbsent,
    activate_blackbox_initial,
    activate_blackbox_revision,
    read_blackbox_lifecycle_state,
    read_blackbox_revision_activation_preflight,
)


def activate_blackbox(ctx: GateContext) -> GateResult:
    """使用唯一入口激活首次上线或同一业务身份修订。"""
    return guarded_result("activate", lambda started_at: _activate(ctx, started_at))


def _activate(ctx: GateContext, started_at: str) -> GateResult:
    cfg = _reload_pinned_canonical(_config(ctx))
    engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
    identity_created = False
    preflight = None
    activation_mode = "initial"
    try:
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
        approved_at = datetime.now(timezone.utc)
        try:
            preflight = read_blackbox_revision_activation_preflight(
                engine,
                enriched_cfg,
            )
        except ValueError:
            try:
                db_state = read_blackbox_lifecycle_state(engine, enriched_cfg)
            except BlackboxLifecycleIdentityAbsent:
                enriched_cfg = replace(
                    _reload_pinned_canonical(cfg),
                    environment_fingerprint=passed_backtest.environment_fingerprint,
                    data_snapshot_id=passed_backtest.data_snapshot_id,
                )
                state = activate_blackbox_initial(
                    engine,
                    enriched_cfg,
                    backtest_run_id=passed_backtest.backtest_run_id,
                    backtest_benchmark_id=passed_backtest.benchmark_id,
                    backtest_data_snapshot_id=passed_backtest.data_snapshot_id,
                    backtest_generation_id=passed_backtest.generation_id,
                    backtest_runtime_profile=passed_backtest.runtime_profile,
                    backtest_environment_fingerprint=(
                        passed_backtest.environment_fingerprint
                    ),
                    backtest_code_hash=passed_backtest.code_hash,
                    backtest_config_hash=passed_backtest.config_hash,
                    backtest_manifest_hash=passed_backtest.manifest_hash,
                    backtest_validator_policy_digest=(
                        passed_backtest.script_validator_policy_digest
                    ),
                    approved_by=operation.issued_by,
                    approved_at=approved_at,
                )
                identity_created = True
            else:
                if (
                    db_state.version_status == "active"
                    and db_state.registry_status == "active"
                ):
                    return _blocked(
                        started_at,
                        ["exact Blackbox version is already active"],
                    )
                return _blocked(
                    started_at,
                    [
                        "existing non-active Blackbox identity cannot be promoted: "
                        f"got={db_state.version_status}+{db_state.registry_status}"
                    ],
                )
        else:
            enriched_cfg = replace(
                _reload_pinned_canonical(cfg),
                environment_fingerprint=passed_backtest.environment_fingerprint,
                data_snapshot_id=passed_backtest.data_snapshot_id,
            )
            state = activate_blackbox_revision(
                engine,
                enriched_cfg,
                prior_scheme_version=preflight.prior_scheme_version,
                pending_scheme_versions=preflight.pending_scheme_versions,
                approved_by=operation.issued_by,
                approved_at=approved_at,
            )
            activation_mode = "revision"
    except Exception as exc:  # noqa: BLE001
        return _failed(started_at, [f"Blackbox activation failed: {exc}"])
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
            Evidence(
                "prior_scheme_version",
                preflight.prior_scheme_version if preflight is not None else None,
            ),
            Evidence(
                "retired_pending_versions",
                list(preflight.pending_scheme_versions) if preflight is not None else [],
            ),
            Evidence("runtime_profile", passed_backtest.runtime_profile),
            Evidence("environment_fingerprint", state.environment_fingerprint),
            Evidence("generation_id", passed_backtest.generation_id),
            Evidence("data_snapshot_id", state.data_snapshot_id),
            Evidence("identity_created", identity_created),
            Evidence("approved_by", state.approved_by),
            Evidence(
                "approved_at",
                state.approved_at.isoformat() if state.approved_at is not None else None,
            ),
            Evidence("version_status", state.version_status),
            Evidence("registry_status", state.registry_status),
            Evidence("registry_scheme_ids", list(state.registry_scheme_ids)),
            Evidence("business_tables_written", activation_mode == "initial"),
            Evidence("config_changed", False),
            Evidence("operator", operation.issued_by),
            Evidence("operation_scope_sha256", operation_scope_sha256(operation)),
            Evidence("activation_mode", activation_mode),
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


def _environment_fingerprint(project_root: Path) -> str:
    from harness.blackbox_v2.gates import _environment_fingerprint as fingerprint

    return fingerprint(project_root)


def _blocked(started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name="activate",
        status=GateStatus.BLOCKED,
        evidence=[Evidence("operation_required", True)],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )


def _failed(
    started_at: str,
    errors: list[str],
) -> GateResult:
    return GateResult(
        gate_name="activate",
        status=GateStatus.FAILED,
        evidence=[],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
