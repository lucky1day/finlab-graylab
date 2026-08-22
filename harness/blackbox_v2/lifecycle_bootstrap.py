from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.repository import read_blackbox_lifecycle_state
from shared.blackbox_v2.lifecycle import (
    LifecycleLockTimeout,
    assert_lifecycle_clear,
    lifecycle_operation_lock,
)
from shared.scheme_lifecycle_state import (
    create_lifecycle_state,
    lifecycle_state_path,
    read_lifecycle_state,
)


class BlackboxLifecycleBootstrapGate(Gate):
    """把既有 active DB 身份一次性固化为本机 active 覆盖层。"""

    name = "lifecycle-bootstrap"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        try:
            cfg = _config(ctx)
            with lifecycle_operation_lock(ctx.project_root, cfg.scheme_id):
                return self._run_locked(ctx, cfg, started_at)
        except LifecycleLockTimeout as exc:
            return _blocked(started_at, [str(exc)])
        except Exception as exc:  # noqa: BLE001
            return _failed(
                started_at,
                [f"Blackbox lifecycle bootstrap preflight failed: {exc}"],
            )

    def _run_locked(
        self,
        ctx: GateContext,
        cfg: SchemeConfig,
        started_at: str,
    ) -> GateResult:
        overlay_path = lifecycle_state_path(ctx.project_root, cfg.scheme_id)
        if os.path.lexists(overlay_path):
            return _blocked(started_at, [f"host lifecycle overlay already exists: {overlay_path}"])
        try:
            assert_lifecycle_clear(ctx.project_root, cfg.scheme_id)
        except RuntimeError as exc:
            return _blocked(started_at, [str(exc)])

        current = load_scheme_config(cfg.path / "config.yaml")
        config_errors = _validate_canonical_config(current, cfg.scheme_version)
        if config_errors:
            return _blocked(started_at, config_errors)

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        effects = _BootstrapEffects()
        try:
            result = self._run_with_engine(
                ctx,
                current,
                engine,
                overlay_path,
                effects,
                started_at,
            )
        except Exception as exc:  # noqa: BLE001
            failure_phase = (
                "commit"
                if effects.authorization_consumed
                else "preflight"
            )
            result = _failed(
                started_at,
                [
                    "Blackbox lifecycle bootstrap "
                    f"{failure_phase} failed: {exc}"
                ],
                evidence=_effect_evidence(effects),
                report_path=effects.authorization_audit_path,
            )
        try:
            if hasattr(engine, "dispose"):
                engine.dispose()
        except Exception as exc:  # noqa: BLE001
            result = replace(
                result,
                evidence=result.evidence + [
                    Evidence("engine_dispose_error", str(exc)),
                ],
            )
        return result

    def _run_with_engine(
        self,
        ctx: GateContext,
        current: SchemeConfig,
        engine,
        overlay_path: Path,
        effects: "_BootstrapEffects",
        started_at: str,
    ) -> GateResult:
        passed_run = _verify_passed_all(engine, current)
        db_state = read_blackbox_lifecycle_state(engine, current)
        preflight_errors = _validate_database_state(current, db_state)
        auth, authorization_errors = verify_authorization(
            ctx.authorization,
            scheme_id=current.scheme_id,
            action="blackbox_lifecycle_bootstrap",
            scheme_version=current.scheme_version,
            harness_run_id=passed_run.harness_run_id,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        preflight_errors.extend(authorization_errors)
        if auth is None or preflight_errors:
            return _blocked(started_at, preflight_errors)

        if os.path.lexists(overlay_path):
            return _blocked(
                started_at,
                [f"host lifecycle overlay already exists: {overlay_path}"],
            )
        assert_lifecycle_clear(ctx.project_root, current.scheme_id)
        pinned = load_scheme_config(current.path / "config.yaml")
        pinned_errors = _validate_canonical_config(
            pinned,
            current.scheme_version,
        )
        if pinned_errors:
            return _blocked(started_at, pinned_errors)

        try:
            mark_token_used(auth, used_tokens_path(ctx.project_root))
            effects.authorization_consumed = True
        except Exception as exc:  # noqa: BLE001
            return _failed(
                started_at,
                [f"authorization consumption failed: {exc}"],
                evidence=_effect_evidence(effects),
            )
        try:
            effects.authorization_audit_path = write_authorization_audit(
                auth,
                ctx.report_dir / "lifecycle_bootstrap_authorization",
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(
                started_at,
                [f"authorization audit failed after token consumption: {exc}"],
                evidence=_effect_evidence(effects),
            )
        try:
            effects.overlay_path = create_lifecycle_state(
                ctx.project_root,
                scheme_id=current.scheme_id,
                scheme_version=current.scheme_version,
                status="active",
                version_status="active",
                harness_run_id=passed_run.harness_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            if os.path.lexists(overlay_path):
                effects.overlay_path = overlay_path
            return _failed(
                started_at,
                [f"host lifecycle overlay commit failed: {exc}"],
                evidence=_effect_evidence(effects),
                report_path=effects.authorization_audit_path,
            )

        record = read_lifecycle_state(
            ctx.project_root,
            current.scheme_id,
            current.scheme_version,
        )
        if record is None or (record.status, record.version_status) != (
            "active",
            "active",
        ):
            return _failed(
                started_at,
                ["host lifecycle overlay independent readback mismatch"],
                evidence=_effect_evidence(effects),
                report_path=effects.authorization_audit_path,
            )

        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("scheme_id", current.scheme_id),
                Evidence("scheme_version", current.scheme_version),
                Evidence("harness_run_id", passed_run.harness_run_id),
                Evidence("config_status", "active"),
                Evidence("version_status", "active"),
                Evidence("registry_status", "active"),
            ] + _effect_evidence(effects),
            errors=[],
            started_at=started_at,
            finished_at=utc_now(),
            report_path=effects.authorization_audit_path,
        )


@dataclass
class _BootstrapEffects:
    authorization_consumed: bool = False
    authorization_audit_path: Path | None = None
    overlay_path: Path | None = None


def _effect_evidence(effects: _BootstrapEffects) -> list[Evidence]:
    evidence = [
        Evidence("database_written", False),
        Evidence("authorization_consumed", effects.authorization_consumed),
    ]
    if effects.authorization_audit_path is not None:
        evidence.append(
            Evidence(
                "authorization_audit_path",
                str(effects.authorization_audit_path),
            )
        )
    if effects.overlay_path is not None:
        evidence.append(Evidence("overlay_path", str(effects.overlay_path)))
    return evidence


def _validate_canonical_config(
    cfg: SchemeConfig,
    expected_scheme_version: str,
) -> list[str]:
    errors: list[str] = []
    if cfg.scheme_version != expected_scheme_version:
        errors.append(
            "canonical scheme_version changed during lifecycle bootstrap: "
            f"expected={expected_scheme_version}, current={cfg.scheme_version}"
        )
    if cfg.status != "active" or cfg.version_status != "active":
        errors.append(
            "lifecycle bootstrap requires canonical config active+active: "
            f"got={cfg.status}+{cfg.version_status}"
        )
    return errors


def _validate_database_state(cfg: SchemeConfig, state) -> list[str]:
    errors: list[str] = []
    if state.scheme_id != cfg.scheme_id or state.scheme_version != cfg.scheme_version:
        errors.append("database lifecycle identity does not match exact canonical version")
    if state.runtime_type != "blackbox_v2":
        errors.append(
            "database exact version runtime_type must be blackbox_v2: "
            f"got={state.runtime_type}"
        )
    if state.version_status != "active" or state.registry_status != "active":
        errors.append(
            "lifecycle bootstrap requires database active+active: "
            f"got={state.version_status}+{state.registry_status}"
        )
    if not state.registry_scheme_ids:
        errors.append("database lifecycle has no composite Registry identity")
    return errors


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(
        ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    )
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(
            "lifecycle bootstrap requires runtime_type=blackbox_v2, "
            f"got={cfg.runtime_type}"
        )
    return cfg


def _verify_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all as verify

    return verify(engine, cfg)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _blocked(started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name=BlackboxLifecycleBootstrapGate.name,
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
    report_path: Path | None = None,
) -> GateResult:
    return GateResult(
        gate_name=BlackboxLifecycleBootstrapGate.name,
        status=GateStatus.FAILED,
        passed=False,
        evidence=evidence or [],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
        report_path=report_path,
    )
