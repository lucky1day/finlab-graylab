from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from sqlalchemy import text

from harness.gates.native_maintenance_admission_gate import (
    native_business_identity_snapshot,
)


NATIVE_LEGACY_ADMISSION_ATTEST_SCHEME_ID = "weekly_10y_d_overlay_0529"
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


def legacy_native_admission_attestation_run_id(prior_harness_run_id: str) -> str:
    """仅由既往 all run 身份确定 receipt run ID，且保持字段长度安全。"""
    candidate = f"lna_{prior_harness_run_id}"
    if len(candidate) <= 128:
        return candidate
    return f"lna_{hashlib.sha256(prior_harness_run_id.encode('utf-8')).hexdigest()}"


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
