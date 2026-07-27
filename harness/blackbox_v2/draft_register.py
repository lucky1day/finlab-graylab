from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date
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
from scheduler.repository import register_blackbox_draft_identity


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

            enriched_cfg = replace(
                cfg,
                environment_fingerprint=environment_fingerprint,
                data_snapshot_id=data_snapshot_id,
            )
            mark_token_used(auth, used_tokens_path(ctx.project_root))
            audit_path = write_authorization_audit(
                auth,
                ctx.report_dir / "draft_register_authorization",
            )
            state = register_blackbox_draft_identity(
                engine,
                enriched_cfg,
                expected_harness_run_id=passed_run.harness_run_id,
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
                Evidence(
                    "before",
                    {
                        "scheme_id": cfg.scheme_id,
                        "scheme_version": cfg.scheme_version,
                        "registry_scheme_ids": list(state.registry_scheme_ids),
                        "identity_exists": False,
                    },
                ),
                Evidence("after", asdict(state)),
                Evidence("harness_run_id", passed_run.harness_run_id),
                Evidence("predict_date", passed_predict_date),
                Evidence("authorization_audit_path", str(audit_path)),
                Evidence("business_tables_written", False),
                Evidence("activation_performed", False),
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
            "Blackbox draft registration requires runtime_type=blackbox_v2, "
            f"got {cfg.runtime_type}"
        )
    return cfg


def _verify_passed_all(engine, cfg):
    from harness.blackbox_v2.gates import _verify_passed_all as verify

    return verify(engine, cfg)


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
