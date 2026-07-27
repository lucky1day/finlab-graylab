from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

from harness.authorization import (
    _atomic_write_json,
    authorization_signing_enabled,
    authorization_token_hash,
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
    BlackboxLifecycleIdentityAbsent,
    read_blackbox_lifecycle_state,
    register_blackbox_draft_identity,
    registry_scheme_id,
)


class BlackboxDraftRegisterGate(Gate):
    """受控登记全新 Blackbox draft version 与 paused Registry。"""

    name = "draft-register"
    requires_authorization = True

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = _config(ctx)
        if not authorization_signing_enabled():
            return _blocked(
                started_at,
                ["Blackbox draft registration requires HMAC signing via HARNESS_AUTH_SECRET"],
            )
        if not isinstance(ctx.authorization, str):
            return _blocked(
                started_at,
                ["Blackbox draft registration requires the original signed token string"],
            )
        auth, errors = verify_authorization(
            ctx.authorization,
            scheme_id=cfg.scheme_id,
            action="draft_register",
            predict_date=ctx.predict_date,
            used_store_path=used_tokens_path(ctx.project_root),
        )
        if auth is not None:
            errors.extend(required_future_expiry_errors(auth.issued_at, auth.expires_at))
            if not isinstance(auth.issued_by, str) or not auth.issued_by.strip():
                errors.append(
                    "Blackbox draft registration authorization requires issued_by "
                    "to be a non-empty string"
                )
            if auth.scheme_version != cfg.scheme_version:
                errors.append(
                    "authorization scheme_version must match current canonical version: "
                    f"token={auth.scheme_version}, current={cfg.scheme_version}"
                )
        if cfg.status != "paused" or cfg.version_status != "draft":
            errors.append(
                "Blackbox draft registration requires config paused+draft: "
                f"got={cfg.status}+{cfg.version_status}"
            )
        if auth is None or errors:
            return _blocked(started_at, errors)

        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        audit_path: Path | None = None
        outcome_path: Path | None = None
        try:
            passed_run = _verify_passed_all(engine, cfg)
            passed_predict_date = _passed_run_predict_date(passed_run, cfg)
            if auth.harness_run_id != passed_run.harness_run_id:
                return _blocked(
                    started_at,
                    [
                        "authorization harness_run_id must match latest passed all-stage run: "
                        f"token={auth.harness_run_id}, latest={passed_run.harness_run_id}"
                    ],
                )
            if ctx.predict_date != passed_predict_date or auth.predict_date != passed_predict_date:
                return _blocked(
                    started_at,
                    [
                        "draft-register predict_date must match latest passed all-stage run: "
                        f"token={auth.predict_date}, ctx={ctx.predict_date}, "
                        f"latest={passed_predict_date}"
                    ],
                )
            environment_fingerprint = str(
                passed_run.environment_fingerprint or ""
            ).strip()
            data_snapshot_id = str(passed_run.data_snapshot_id or "").strip()
            evidence_errors = []
            if not environment_fingerprint:
                evidence_errors.append(
                    "latest passed all-stage environment_fingerprint is missing"
                )
            if not data_snapshot_id:
                evidence_errors.append(
                    "latest passed all-stage data_snapshot_id is missing"
                )
            if evidence_errors:
                return _blocked(started_at, evidence_errors)

            try:
                pinned_cfg = _reload_pinned_canonical(cfg)
            except ValueError as exc:
                return _blocked(started_at, [str(exc)])
            enriched_cfg = replace(
                pinned_cfg,
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=data_snapshot_id,
            )
            before = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "registry_scheme_ids": [
                    registry_scheme_id(cfg.scheme_id, cfg.horizon, tenor)
                    for tenor in cfg.tenors
                ],
                "identity_exists": False,
            }
            authorization_dir = (
                ctx.report_dir / "draft_register_authorization"
            )
            prepared_outcome = {
                "action": "draft_register",
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "harness_run_id": passed_run.harness_run_id,
                "predict_date": passed_predict_date,
                "authorization_token_sha256": authorization_token_hash(auth),
                "authorization_consumed": False,
                "status": "prepared",
                "database_outcome": "not_started",
                "rollback_outcome": "not_required",
                "reconciliation_required": False,
                "before": before,
                "after": None,
                "error": None,
                "readback_error": None,
                "business_tables_written": False,
                "activation_performed": False,
            }
            try:
                audit_path = write_authorization_audit(
                    auth,
                    authorization_dir,
                )
                outcome_path = authorization_dir / "outcome.json"
                _write_outcome_audit(outcome_path, prepared_outcome)
            except Exception as exc:  # noqa: BLE001
                outcome = {
                    **prepared_outcome,
                    "status": "failed",
                    "database_outcome": "not_started",
                    "reconciliation_required": False,
                    "error": _error_payload(exc),
                }
                return _failed(
                    started_at,
                    [f"draft registration audit preparation failed: {exc}"],
                    outcome=outcome,
                    authorization_audit_path=audit_path,
                    outcome_audit_path=outcome_path,
                )

            try:
                mark_token_used(auth, used_tokens_path(ctx.project_root))
            except Exception as exc:  # noqa: BLE001
                outcome = {
                    **prepared_outcome,
                    "status": "failed",
                    "database_outcome": "not_started",
                    "reconciliation_required": False,
                    "error": _error_payload(exc),
                }
                audit_errors = _try_write_outcome(outcome_path, outcome)
                return _failed(
                    started_at,
                    [f"draft registration authorization consumption failed: {exc}", *audit_errors],
                    outcome=outcome,
                    authorization_audit_path=audit_path,
                    outcome_audit_path=outcome_path,
                )

            try:
                state = register_blackbox_draft_identity(
                    engine,
                    enriched_cfg,
                    expected_harness_run_id=passed_run.harness_run_id,
                )
            except Exception as exc:  # noqa: BLE001
                probe = _database_failure_outcome(engine, enriched_cfg)
                outcome = {
                    **prepared_outcome,
                    "authorization_consumed": True,
                    "status": "failed",
                    "database_outcome": probe.database_outcome,
                    "rollback_outcome": probe.rollback_outcome,
                    "reconciliation_required": (
                        probe.reconciliation_required
                    ),
                    "after": probe.after,
                    "error": _error_payload(exc),
                    "readback_error": probe.readback_error,
                }
                audit_errors = _try_write_outcome(outcome_path, outcome)
                return _failed(
                    started_at,
                    [str(exc), *audit_errors],
                    outcome=outcome,
                    authorization_audit_path=audit_path,
                    outcome_audit_path=outcome_path,
                )

            after = _lifecycle_state_payload(state)
            outcome = {
                **prepared_outcome,
                "authorization_consumed": True,
                "status": "passed",
                "database_outcome": "committed",
                "rollback_outcome": "not_applicable_committed",
                "reconciliation_required": False,
                "after": after,
            }
            audit_errors = _try_write_outcome(outcome_path, outcome)
            if audit_errors:
                audit_error = OSError(audit_errors[0])
                outcome = {
                    **outcome,
                    "status": "failed",
                    "database_outcome": "committed_identity_present",
                    "rollback_outcome": "not_applicable_committed",
                    "reconciliation_required": True,
                    "error": _error_payload(audit_error),
                }
                fallback_path = outcome_path.with_name(
                    "outcome.reconciliation-required.json"
                )
                fallback_errors = _try_write_fallback_outcome(
                    fallback_path,
                    outcome,
                )
                return _failed(
                    started_at,
                    [*audit_errors, *fallback_errors],
                    outcome=outcome,
                    authorization_audit_path=audit_path,
                    outcome_audit_path=(
                        fallback_path if not fallback_errors else outcome_path
                    ),
                )
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("action", "draft_register"),
                Evidence("before", before),
                Evidence("after", after),
                Evidence("outcome", outcome),
                Evidence("harness_run_id", passed_run.harness_run_id),
                Evidence("predict_date", passed_predict_date),
                Evidence("authorization_audit_path", str(audit_path)),
                Evidence("outcome_audit_path", str(outcome_path)),
                Evidence("business_tables_written", False),
                Evidence("activation_performed", False),
            ],
            errors=[],
            started_at=started_at,
            finished_at=utc_now(),
            report_path=outcome_path,
        )


def _config(ctx: GateContext) -> SchemeConfig:
    cfg = ctx.config or load_scheme_config(
        ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    )
    if cfg.runtime_type != "blackbox_v2":
        raise ValueError(
            "Blackbox draft registration requires runtime_type=blackbox_v2, "
            f"got {cfg.runtime_type}"
        )
    return cfg


def _verify_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all as verify

    return verify(engine, cfg)


def _reload_pinned_canonical(cfg: SchemeConfig) -> SchemeConfig:
    current = load_scheme_config(cfg.path / "config.yaml")
    initial_identity = _canonical_identity(cfg)
    current_identity = _canonical_identity(current)
    mismatches = [
        f"{field}: initial={initial_identity[field]!r}, "
        f"current={current_identity[field]!r}"
        for field in initial_identity
        if initial_identity[field] != current_identity[field]
    ]
    if mismatches:
        raise ValueError(
            "Blackbox canonical delivery drift before draft registration: "
            + "; ".join(mismatches)
        )
    return current


def _canonical_identity(cfg: SchemeConfig) -> dict[str, object]:
    return {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
        "runtime_type": cfg.runtime_type,
        "status": cfg.status,
        "version_status": cfg.version_status,
        "name": cfg.name,
        "description": cfg.description,
        "algorithm_version": cfg.algorithm_version,
        "contract_version": cfg.contract_version,
        "runtime_profile": cfg.runtime_profile,
        "data_schema_version": cfg.data_schema_version,
        "input_source": cfg.input_source,
        "platform_inputs": tuple(cfg.platform_inputs),
        "horizon": cfg.horizon,
        "task_type": cfg.task_type,
        "tenors": tuple(cfg.tenors),
        "frequency": cfg.frequency,
        "target_rule": cfg.target_rule,
        "schedule_cron": cfg.schedule.cron,
        "schedule_timezone": cfg.schedule.timezone,
    }


def _passed_run_predict_date(passed_run, cfg: SchemeConfig) -> str:
    value = getattr(passed_run, "predict_date", None)
    if value is None:
        report_path = Path(passed_run.report_uri) / "onboard_report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if payload.get("harness_run_id") != passed_run.harness_run_id:
            raise ValueError("latest passed all-stage report harness_run_id mismatch")
        if payload.get("scheme_id") != cfg.scheme_id:
            raise ValueError("latest passed all-stage report scheme_id mismatch")
        if payload.get("stage_requested") != "all" or payload.get("overall_passed") is not True:
            raise ValueError("latest passed all-stage report lifecycle evidence mismatch")
        value = payload.get("predict_date")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("latest passed all-stage predict_date is missing or non-canonical")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "latest passed all-stage predict_date is missing or non-canonical"
        ) from exc
    if parsed.isoformat() != value:
        raise ValueError("latest passed all-stage predict_date is missing or non-canonical")
    return value


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _write_outcome_audit(path: Path, outcome: dict[str, object]) -> None:
    _atomic_write_json(path, outcome)


def _try_write_outcome(
    path: Path,
    outcome: dict[str, object],
) -> list[str]:
    try:
        _write_outcome_audit(path, outcome)
    except Exception as exc:  # noqa: BLE001
        return [f"draft registration outcome audit write failed: {exc}"]
    return []


def _try_write_fallback_outcome(
    path: Path,
    outcome: dict[str, object],
) -> list[str]:
    try:
        _atomic_write_json(path, outcome)
    except Exception as exc:  # noqa: BLE001
        return [
            "draft registration reconciliation audit fallback write failed: "
            f"{exc}"
        ]
    return []


@dataclass(frozen=True)
class _DatabaseFailureProbe:
    identity_state: str
    database_outcome: str
    rollback_outcome: str
    reconciliation_required: bool
    after: dict[str, object] | None
    readback_error: dict[str, str] | None


def _database_failure_outcome(
    engine,
    cfg: SchemeConfig,
) -> _DatabaseFailureProbe:
    try:
        state = read_blackbox_lifecycle_state(engine, cfg)
    except BlackboxLifecycleIdentityAbsent:
        return _DatabaseFailureProbe(
            identity_state="absent",
            database_outcome="rolled_back_or_not_started",
            rollback_outcome="rolled_back_or_not_started",
            reconciliation_required=False,
            after=None,
            readback_error=None,
        )
    except Exception as exc:  # noqa: BLE001
        return _DatabaseFailureProbe(
            identity_state="unknown",
            database_outcome="unknown",
            rollback_outcome="not_confirmed",
            reconciliation_required=True,
            after=None,
            readback_error=_error_payload(exc),
        )
    return _DatabaseFailureProbe(
        identity_state="present",
        database_outcome="identity_present_requires_reconciliation",
        rollback_outcome="not_confirmed_identity_present",
        reconciliation_required=True,
        after=_lifecycle_state_payload(state),
        readback_error=None,
    )


def _lifecycle_state_payload(state) -> dict[str, object]:
    payload = asdict(state)
    payload["registry_scheme_ids"] = list(payload["registry_scheme_ids"])
    approved_at = payload.get("approved_at")
    if approved_at is not None:
        payload["approved_at"] = approved_at.isoformat()
    return payload


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _failed(
    started_at: str,
    errors: list[str],
    *,
    outcome: dict[str, object],
    authorization_audit_path: Path | None,
    outcome_audit_path: Path | None,
) -> GateResult:
    return GateResult(
        gate_name="draft-register",
        status=GateStatus.FAILED,
        passed=False,
        evidence=[
            Evidence("action", "draft_register"),
            Evidence("outcome", outcome),
            Evidence(
                "authorization_audit_path",
                (
                    str(authorization_audit_path)
                    if authorization_audit_path is not None
                    else None
                ),
            ),
            Evidence(
                "outcome_audit_path",
                (
                    str(outcome_audit_path)
                    if outcome_audit_path is not None
                    else None
                ),
            ),
            Evidence("business_tables_written", False),
            Evidence("activation_performed", False),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
        report_path=(
            outcome_audit_path
            if outcome_audit_path is not None
            and outcome_audit_path.is_file()
            else authorization_audit_path
        ),
    )


def _blocked(started_at: str, errors: list[str]) -> GateResult:
    return GateResult(
        gate_name="draft-register",
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[
            Evidence("action", "draft_register"),
            Evidence("authorization_required", True),
            Evidence("business_tables_written", False),
            Evidence("activation_performed", False),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
    )
