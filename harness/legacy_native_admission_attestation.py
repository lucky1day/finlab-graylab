from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from sqlalchemy import text

from harness.authorization import (
    NATIVE_LEGACY_ADMISSION_ATTEST_ACTION,
    NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
    Authorization,
    AuthorizationTokenAlreadyUsedError,
    authorization_token_hash,
    mark_token_used,
    required_future_expiry_errors,
    used_tokens_path,
    verify_authorization,
)
from harness.context import GateContext
from harness.gates.base import utc_now
from harness.gates.native_maintenance_admission_gate import (
    _config_and_policy_errors,
    _current_native_candidate_error,
    _prior_compare_evidence_error_conn,
    _read_current_native_candidate_conn,
    _read_prior_native_admission_conn,
    _read_prior_native_static_identity_state_conn,
    native_business_identity_snapshot,
)
from harness.persistence import (
    LegacyNativeAdmissionAttestationPersistenceError,
    persist_legacy_native_admission_attestation,
)
from harness.result import Evidence, GateResult, GateStatus


LEGACY_NATIVE_ADMISSION_ATTEST_ACTION = NATIVE_LEGACY_ADMISSION_ATTEST_ACTION
LEGACY_NATIVE_ADMISSION_ATTEST_STAGE = "legacy-native-admission-attestation"
LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME = LEGACY_NATIVE_ADMISSION_ATTEST_STAGE
LEGACY_NATIVE_ADMISSION_ATTEST_EVIDENCE_KEY = (
    "legacy_native_admission_attestation"
)
LEGACY_NATIVE_ADMISSION_ATTEST_SCHEMA_VERSION = (
    "legacy_native_admission_attestation_v1"
)
LEGACY_NATIVE_ADMISSION_ATTEST_ASSERTION = (
    "operator_attests_legacy_native_admission_identity"
)
LEGACY_NATIVE_ADMISSION_ATTEST_TRIGGERED_BY = (
    "native-legacy-admission-attestation-operator"
)
LEGACY_NATIVE_ADMISSION_IDENTITY_SOURCE = "legacy_operator_attestation_v1"
_FIXED_BUSINESS_IDENTITY = native_business_identity_snapshot(
    scheme_id=NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
    runtime_type="native_adapter",
    horizon=6,
    task_type="weekly_point",
    frequency="weekly",
    tenors=("10Y",),
)
_RECEIPT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "scope_scheme_id",
        "assertion",
        "prior_admitted_scheme_version",
        "prior_harness_run_id",
        "business_identity",
        "issued_by",
        "issued_at",
        "authorization_token_sha256",
    }
)


@dataclass(frozen=True)
class LegacyNativeAdmissionReceipt:
    harness_run_id: str
    business_identity: dict[str, object]


@dataclass(frozen=True)
class LegacyNativeAdmissionAttestationPreflight:
    prior_admitted_scheme_version: str
    prior_harness_run_id: str
    business_identity: dict[str, object]


def legacy_native_admission_attestation_run_id(prior_harness_run_id: str) -> str:
    """仅由既往 all run 身份确定 receipt run ID，且保持字段长度安全。"""
    candidate = f"lna_{prior_harness_run_id}"
    if len(candidate) <= 128:
        return candidate
    return f"lna_{hashlib.sha256(prior_harness_run_id.encode('utf-8')).hexdigest()}"


def run_legacy_native_admission_attestation(ctx: GateContext) -> GateResult:
    """执行唯一授权的 legacy Native 身份证明，绝不写业务表。"""
    started_at = utc_now()
    if ctx.scheme_id != NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID:
        return _blocked_result(
            ctx,
            started_at,
            [
                "legacy Native admission attestation only supports "
                f"{NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID}"
            ],
        )

    auth, auth_errors = verify_authorization(
        ctx.authorization,
        scheme_id=NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
        action=NATIVE_LEGACY_ADMISSION_ATTEST_ACTION,
        used_store_path=used_tokens_path(ctx.project_root),
    )
    if auth is not None:
        auth_errors.extend(
            required_future_expiry_errors(auth.issued_at, auth.expires_at)
        )
    if auth_errors or auth is None:
        return _blocked_result(
            ctx,
            started_at,
            auth_errors or ["legacy Native admission attestation authorization failed"],
        )

    preflight, preflight_errors = _read_attestation_preflight(ctx)
    if preflight_errors or preflight is None:
        return _blocked_result(ctx, started_at, preflight_errors)
    binding_errors = _authorization_binding_errors(auth, preflight)
    if binding_errors:
        return _blocked_result(ctx, started_at, binding_errors)

    try:
        mark_token_used(auth, used_tokens_path(ctx.project_root))
    except AuthorizationTokenAlreadyUsedError as exc:
        return _blocked_result(ctx, started_at, [str(exc)])
    except Exception as exc:  # noqa: BLE001 - replay store failures must block write.
        return _blocked_result(
            ctx,
            started_at,
            [f"legacy Native admission attestation token consumption failed: {exc}"],
        )

    payload = _attestation_payload(auth, preflight)
    receipt_run_id = legacy_native_admission_attestation_run_id(
        preflight.prior_harness_run_id
    )
    finished_at = utc_now()
    try:
        persist_legacy_native_admission_attestation(
            ctx,
            harness_run_id=receipt_run_id,
            stage=LEGACY_NATIVE_ADMISSION_ATTEST_STAGE,
            gate_name=LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME,
            triggered_by=LEGACY_NATIVE_ADMISSION_ATTEST_TRIGGERED_BY,
            evidence_key=LEGACY_NATIVE_ADMISSION_ATTEST_EVIDENCE_KEY,
            evidence_value=payload,
            started_at=started_at,
            finished_at=finished_at,
        )
    except LegacyNativeAdmissionAttestationPersistenceError as exc:
        return GateResult(
            gate_name=LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME,
            status=GateStatus.FAILED,
            passed=False,
            evidence=[Evidence("token_consumed", True)],
            errors=[str(exc)],
            started_at=started_at,
            finished_at=utc_now(),
        )
    return GateResult(
        gate_name=LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME,
        status=GateStatus.PASSED,
        passed=True,
        evidence=[
            Evidence(
                LEGACY_NATIVE_ADMISSION_ATTEST_EVIDENCE_KEY,
                payload,
                receipt_run_id,
            )
        ],
        errors=[],
        started_at=started_at,
        finished_at=finished_at,
    )


def read_legacy_native_admission_attestation_receipt_conn(
    conn,
    *,
    prior_admitted_scheme_version: str,
    prior_harness_run_id: str,
    business_identity: dict[str, object],
) -> tuple[LegacyNativeAdmissionReceipt | None, str | None]:
    """只读读取且严格验证唯一的固定 scope operator receipt。"""
    if business_identity != _FIXED_BUSINESS_IDENTITY:
        return None, (
            "legacy Native admission attestation receipt requires the fixed "
            "10Y business identity"
        )
    rows = (
        conn.execute(
            text(
                """
                SELECT harness_run_id, scheme_id, scheme_version, stage, status,
                       triggered_by, code_hash, config_hash
                FROM t_harness_runs
                WHERE scheme_id = :scheme_id
                  AND stage = :stage
                """
            ),
            {
                "scheme_id": NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
                "stage": LEGACY_NATIVE_ADMISSION_ATTEST_STAGE,
            },
        )
        .mappings()
        .all()
    )
    if not rows:
        return None, None
    if len(rows) != 1:
        return None, "legacy Native admission attestation receipt is duplicate"
    row = rows[0]
    expected_run_id = legacy_native_admission_attestation_run_id(
        prior_harness_run_id
    )
    if (
        row.get("harness_run_id") != expected_run_id
        or row.get("status") != "passed"
        or row.get("scheme_version") is not None
        or row.get("code_hash") is not None
        or row.get("config_hash") is not None
        or row.get("triggered_by") != LEGACY_NATIVE_ADMISSION_ATTEST_TRIGGERED_BY
    ):
        return None, "legacy Native admission attestation receipt is noncanonical"
    gate_rows = (
        conn.execute(
            text(
                """
                SELECT gate_name, status, summary_json
                FROM t_harness_gate_results
                WHERE harness_run_id = :harness_run_id
                """
            ),
            {"harness_run_id": expected_run_id},
        )
        .mappings()
        .all()
    )
    if len(gate_rows) != 1:
        return None, "legacy Native admission attestation receipt is duplicate"
    gate = gate_rows[0]
    if (
        gate.get("gate_name") != LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME
        or gate.get("status") != "passed"
    ):
        return None, "legacy Native admission attestation receipt is noncanonical"
    payload = _canonical_receipt_payload(gate.get("summary_json"))
    if payload is None:
        return None, "legacy Native admission attestation receipt is noncanonical"
    if payload["business_identity"] != _FIXED_BUSINESS_IDENTITY:
        return None, (
            "legacy Native admission attestation receipt requires the fixed "
            "10Y business identity"
        )
    if (
        payload["prior_admitted_scheme_version"]
        != prior_admitted_scheme_version
        or payload["prior_harness_run_id"] != prior_harness_run_id
        or payload["business_identity"] != business_identity
    ):
        return None, "legacy Native admission attestation receipt does not match current admission"
    return (
        LegacyNativeAdmissionReceipt(
            harness_run_id=expected_run_id,
            business_identity=dict(payload["business_identity"]),
        ),
        None,
    )


def _read_attestation_preflight(
    ctx: GateContext,
) -> tuple[LegacyNativeAdmissionAttestationPreflight | None, list[str]]:
    config_errors = _config_and_policy_errors(ctx)
    if config_errors:
        return None, config_errors
    cfg = ctx.config
    assert cfg is not None
    try:
        business_identity = native_business_identity_snapshot(
            scheme_id=cfg.scheme_id,
            runtime_type=cfg.runtime_type,
            horizon=cfg.horizon,
            task_type=cfg.task_type,
            frequency=cfg.frequency,
            tenors=cfg.tenors,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return None, [f"legacy Native admission identity is invalid: {exc}"]
    if business_identity != _FIXED_BUSINESS_IDENTITY:
        return None, [
            "legacy Native admission attestation requires the fixed 10Y "
            "business identity"
        ]

    engine = None
    owns_engine = ctx.engine_factory is None
    try:
        if ctx.engine_factory is not None:
            engine = ctx.engine_factory()
        else:
            from scheduler.repository import create_engine_from_env

            engine = create_engine_from_env()
        if engine is None:
            return None, ["cannot connect to database for legacy Native admission attestation"]
        with engine.begin() as conn:
            current_candidate = _read_current_native_candidate_conn(
                conn,
                scheme_id=ctx.scheme_id,
                scheme_version=str(cfg.scheme_version),
            )
            _current_status, current_candidate_error = _current_native_candidate_error(
                current_candidate,
                scheme_id=ctx.scheme_id,
                scheme_version=str(cfg.scheme_version),
            )
            if current_candidate_error is not None:
                return None, [current_candidate_error]
            prior = _read_prior_native_admission_conn(
                conn,
                scheme_id=ctx.scheme_id,
                current_scheme_version=str(cfg.scheme_version),
            )
            if prior is None:
                return None, [
                    "no prior active Native version has a passed all-stage harness run "
                    "with a passed compare gate"
                ]
            prior_version = str(prior["scheme_version"])
            prior_run_id = str(prior["harness_run_id"])
            compare_error = _prior_compare_evidence_error_conn(
                conn,
                harness_run_id=prior_run_id,
            )
            if compare_error is not None:
                return None, [compare_error]
            static_state = _read_prior_native_static_identity_state_conn(
                conn,
                harness_run_id=prior_run_id,
            )
            if not static_state.genuinely_missing_identity_evidence:
                return None, [
                    static_state.error
                    or "prior admitted Native static gate identity snapshot is already present"
                ]
            receipt, receipt_error = (
                read_legacy_native_admission_attestation_receipt_conn(
                    conn,
                    prior_admitted_scheme_version=prior_version,
                    prior_harness_run_id=prior_run_id,
                    business_identity=business_identity,
                )
            )
            if receipt_error is not None:
                return None, [receipt_error]
            if receipt is not None:
                return None, [
                    "legacy Native admission attestation receipt already exists"
                ]
    except Exception as exc:  # noqa: BLE001 - admission proof must fail closed.
        return None, [f"legacy Native admission attestation read failed: {exc}"]
    finally:
        if owns_engine and engine is not None and hasattr(engine, "dispose"):
            engine.dispose()
    return (
        LegacyNativeAdmissionAttestationPreflight(
            prior_admitted_scheme_version=prior_version,
            prior_harness_run_id=prior_run_id,
            business_identity=business_identity,
        ),
        [],
    )


def _authorization_binding_errors(
    auth: Authorization,
    preflight: LegacyNativeAdmissionAttestationPreflight,
) -> list[str]:
    errors: list[str] = []
    if auth.scheme_version != preflight.prior_admitted_scheme_version:
        errors.append(
            "prior scheme_version mismatch: "
            f"token={auth.scheme_version}, selected={preflight.prior_admitted_scheme_version}"
        )
    if auth.harness_run_id != preflight.prior_harness_run_id:
        errors.append(
            "prior harness_run_id mismatch: "
            f"token={auth.harness_run_id}, selected={preflight.prior_harness_run_id}"
        )
    return errors


def _attestation_payload(
    auth: Authorization,
    preflight: LegacyNativeAdmissionAttestationPreflight,
) -> dict[str, object]:
    return {
        "schema_version": LEGACY_NATIVE_ADMISSION_ATTEST_SCHEMA_VERSION,
        "scope_scheme_id": NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID,
        "assertion": LEGACY_NATIVE_ADMISSION_ATTEST_ASSERTION,
        "prior_admitted_scheme_version": preflight.prior_admitted_scheme_version,
        "prior_harness_run_id": preflight.prior_harness_run_id,
        "business_identity": preflight.business_identity,
        "issued_by": auth.issued_by,
        "issued_at": auth.issued_at,
        "authorization_token_sha256": authorization_token_hash(auth),
    }


def _canonical_receipt_payload(value: object) -> dict[str, object] | None:
    summary = _decode_json_mapping(value)
    if (
        summary is None
        or set(summary) != {"passed", "evidence", "errors"}
        or summary.get("passed") is not True
        or summary.get("errors") != []
        or not isinstance(summary.get("evidence"), list)
        or len(summary["evidence"]) != 1
    ):
        return None
    item = summary["evidence"][0]
    if (
        not isinstance(item, Mapping)
        or set(item) != {"key", "value", "detail"}
        or item.get("key") != LEGACY_NATIVE_ADMISSION_ATTEST_EVIDENCE_KEY
        or item.get("detail") is not None
        or not isinstance(item.get("value"), Mapping)
    ):
        return None
    payload = dict(item["value"])
    if set(payload) != _RECEIPT_PAYLOAD_FIELDS:
        return None
    if (
        payload.get("schema_version") != LEGACY_NATIVE_ADMISSION_ATTEST_SCHEMA_VERSION
        or payload.get("scope_scheme_id") != NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID
        or payload.get("assertion") != LEGACY_NATIVE_ADMISSION_ATTEST_ASSERTION
        or not isinstance(payload.get("issued_by"), str)
        or not payload["issued_by"].strip()
        or not _is_aware_timestamp(payload.get("issued_at"))
        or not _is_sha256(payload.get("authorization_token_sha256"))
        or not isinstance(payload.get("prior_admitted_scheme_version"), str)
        or not payload["prior_admitted_scheme_version"]
        or not isinstance(payload.get("prior_harness_run_id"), str)
        or not payload["prior_harness_run_id"]
        or not isinstance(payload.get("business_identity"), Mapping)
    ):
        return None
    return payload


def _decode_json_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _is_aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _blocked_result(
    ctx: GateContext,
    started_at: str,
    errors: list[str],
) -> GateResult:
    return GateResult(
        gate_name=LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME,
        status=GateStatus.BLOCKED,
        passed=False,
        evidence=[
            Evidence("scheme_id", ctx.scheme_id),
            Evidence("token_consumed", False),
        ],
        errors=errors or ["legacy Native admission attestation is blocked"],
        started_at=started_at,
        finished_at=utc_now(),
    )
