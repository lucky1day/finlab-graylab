from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.liwei_0616_cache_contract import (
    SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID,
)


AUTH_SECRET_ENV = "HARNESS_AUTH_SECRET"
DEFAULT_BACKTEST_START_DATE = "2025-01-01"
BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS = 900
BLACKBOX_PRIVILEGED_AUTH_MAX_FUTURE_SKEW_SECONDS = 60
EXACT_PREDICT_DATE_ACTIONS = frozenset(
    {
        "backtest_persist",
        "draft_register",
        "blackbox_revision_activate",
        "gray_backfill_write",
        "signal_gap_fill_write",
        "signal_gap_native_artifact_register",
        "signal_gap_native_cache_prewarm",
    }
)
_AUTHORIZATION_BASE_PAYLOAD_FIELDS = frozenset(
    {
        "action",
        "scheme_id",
        "scheme_version",
        "predict_date",
        "harness_run_id",
        "issued_by",
        "issued_at",
        "expires_at",
        "nonce",
    }
)
_SIGNAL_GAP_FILL_PAYLOAD_FIELDS = frozenset(
    {
        "plan_sha256",
        "base_scheme_id",
        "target_keys",
        "source_authority",
    }
)
_SIGNAL_GAP_NATIVE_ARTIFACT_PAYLOAD_FIELDS = frozenset(
    {"source_authority"}
)
_SIGNAL_GAP_NATIVE_CACHE_PREWARM_PAYLOAD_FIELDS = frozenset(
    {"source_authority"}
)
SIGNAL_GAP_NATIVE_ARTIFACT_SCHEME_ID = (
    "signal-gap-native-artifact"
)
SIGNAL_GAP_NATIVE_CACHE_PREWARM_SCHEME_ID = "signal-gap-native-cache"
SIGNAL_GAP_NATIVE_ARTIFACT_PURPOSE = (
    "signal_gap_gray_live_current_snapshot"
)
SIGNAL_GAP_NATIVE_CACHE_PREWARM_PURPOSE = (
    "signal_gap_native_cache_prewarm"
)
SIGNAL_GAP_NATIVE_ARTIFACT_EXPORTER_VERSION = (
    "native-signal-gap-current-snapshot-v1"
)
_SIGNAL_GAP_TARGET_FIELDS = frozenset(
    {
        "registry_scheme_id",
        "base_scheme_id",
        "target_tenor",
        "horizon",
        "task_type",
        "predict_date",
        "feature_date",
        "target_date",
        "prediction_phase",
    }
)


@dataclass(frozen=True)
class Authorization:
    scheme_id: str
    action: str
    predict_date: str | None
    token: str
    issued_by: str
    issued_at: str
    nonce: str
    scheme_version: str | None = None
    harness_run_id: str | None = None
    expires_at: str | None = None
    backtest_start_date: str | None = None
    plan_sha256: str | None = None
    signal_gap_target_keys: tuple[dict[str, Any], ...] = ()
    source_authority: dict[str, Any] | None = None


class AuthorizationSecretError(RuntimeError):
    """保留以兼容既有 import；当前软默认模式下不再主动抛出。"""


class AuthorizationTokenAlreadyUsedError(RuntimeError):
    """授权 token 已被权威消费。"""


def _auth_secret() -> bytes | None:
    """返回 HMAC 密钥；未配置时返回 None（软默认：token 退化为明文确认闸）。"""
    secret = os.environ.get(AUTH_SECRET_ENV)
    if not secret:
        return None
    return secret.encode("utf-8")


def authorization_signing_enabled() -> bool:
    """返回当前授权 token 是否启用 HMAC 签名。"""
    return _auth_secret() is not None


def authorization_token_hash(token: str | Authorization) -> str:
    """返回可安全持久化的授权 token SHA-256。"""
    raw = token.token if isinstance(token, Authorization) else str(token)
    return _token_hash(raw)


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(payload: dict[str, Any]) -> str | None:
    """有密钥时返回 HMAC 签名；无密钥返回 None（不签名）。"""
    secret = _auth_secret()
    if secret is None:
        return None
    digest = hmac.new(secret, _canonical_payload_bytes(payload), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_token(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str | None = None,
    harness_run_id: str | None = None,
    ttl_seconds: int | None = None,
    issued_by: str = "harness",
    backtest_start_date: str | None = None,
    plan_sha256: str | None = None,
    base_scheme_id: str | None = None,
    target_keys: Any = None,
    source_authority: Any = None,
) -> str:
    """签发一次性授权 token（写库/激活前的确认闸）。

    配置了 HARNESS_AUTH_SECRET 时附带 HMAC 签名；未配置时退化为明文信封，
    token 仍承担一次性 + 作用域绑定的确认职责（软默认，单用户场景无需配置）。
    """
    if action in {
        "signal_gap_fill_write",
        "signal_gap_native_artifact_register",
        "signal_gap_native_cache_prewarm",
        "blackbox_revision_activate",
    }:
        if _auth_secret() is None:
            raise ValueError(
                f"{action} authorization requires HMAC signing"
            )
        if ttl_seconds is None:
            ttl_seconds = BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS
        if (
            isinstance(ttl_seconds, bool)
            or int(ttl_seconds) <= 0
            or int(ttl_seconds)
            > BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS
        ):
            raise ValueError(
                f"{action} authorization lifetime must be "
                "at most 900 seconds"
            )
    if action == "signal_gap_fill_write":
        normalized_plan_sha256 = _require_sha256(
            plan_sha256,
            "plan_sha256",
        )
        normalized_base = _require_text(
            base_scheme_id,
            "base_scheme_id",
        )
        if scheme_id != normalized_base:
            raise ValueError(
                "signal_gap_fill_write scheme_id must equal base_scheme_id"
            )
        normalized_targets = _normalize_signal_gap_target_keys(
            target_keys,
            expected_base_scheme_id=normalized_base,
            expected_predict_date=predict_date,
        )
        normalized_authority = _normalize_signal_gap_source_authority(
            source_authority
        )
        if (
            normalized_authority["authority_type"]
            == "native_archived_generation"
            and normalized_authority["business_date"]
            != _normalize_action_predict_date(
                "signal_gap_fill_write",
                predict_date,
            )
        ):
            raise ValueError(
                "archived Native business_date must equal predict_date"
            )
    elif action == "signal_gap_native_artifact_register":
        if scheme_id != SIGNAL_GAP_NATIVE_ARTIFACT_SCHEME_ID:
            raise ValueError(
                "signal_gap_native_artifact_register scheme_id is invalid"
            )
        normalized_authority = (
            _normalize_signal_gap_native_artifact_authority(
                source_authority,
                expected_historical_predict_date=predict_date,
            )
        )
        normalized_plan_sha256 = None
        normalized_base = None
        normalized_targets = ()
    elif action == "signal_gap_native_cache_prewarm":
        if scheme_id != SIGNAL_GAP_NATIVE_CACHE_PREWARM_SCHEME_ID:
            raise ValueError(
                "signal_gap_native_cache_prewarm scheme_id is invalid"
            )
        normalized_authority = (
            _normalize_signal_gap_native_cache_prewarm_authority(
                source_authority,
                expected_historical_predict_date=predict_date,
            )
        )
        normalized_plan_sha256 = None
        normalized_base = None
        normalized_targets = ()
    else:
        normalized_plan_sha256 = None
        normalized_base = None
        normalized_targets = ()
        normalized_authority = None
    if action in EXACT_PREDICT_DATE_ACTIONS:
        predict_date = _normalize_action_predict_date(action, predict_date)
    if action in {"draft_register", "blackbox_revision_activate"} and (
        not isinstance(issued_by, str) or not issued_by.strip()
    ):
        raise ValueError(
            f"{action} issued_by must be a non-empty string"
        )
    if action == "blackbox_revision_activate":
        for field, value in (
            ("scheme_version", scheme_version),
            ("harness_run_id", harness_run_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "blackbox_revision_activate "
                    f"{field} must be a non-empty string"
                )
    issued_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = None
    if ttl_seconds is not None:
        expires_at = (issued_at_dt + timedelta(seconds=int(ttl_seconds))).isoformat()
    payload = {
        "action": action,
        "scheme_id": scheme_id,
        "scheme_version": scheme_version,
        "predict_date": predict_date,
        "harness_run_id": harness_run_id,
        "issued_by": issued_by,
        "issued_at": issued_at_dt.isoformat(),
        "expires_at": expires_at,
        "nonce": secrets.token_urlsafe(16),
    }
    if action == "backtest_persist":
        payload["backtest_start_date"] = normalize_backtest_start_date(backtest_start_date)
    if action == "signal_gap_fill_write":
        payload.update(
            {
                "plan_sha256": normalized_plan_sha256,
                "base_scheme_id": normalized_base,
                "target_keys": list(normalized_targets),
                "source_authority": normalized_authority,
            }
        )
    elif action == "signal_gap_native_artifact_register":
        payload["source_authority"] = normalized_authority
    elif action == "signal_gap_native_cache_prewarm":
        payload["source_authority"] = normalized_authority
    signature = _sign(payload)
    envelope: dict[str, Any] = {"payload": payload}
    if signature is not None:
        envelope["sig"] = signature
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_envelope(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or not token.isascii():
        raise ValueError("authorization token encoding is not canonical")
    if "=" in token or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in token
    ):
        raise ValueError("authorization token encoding is not canonical")
    padding = "=" * (-len(token) % 4)
    try:
        decoded_bytes = base64.b64decode(
            (token + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("authorization token encoding is not canonical") from exc
    canonical_token = base64.urlsafe_b64encode(decoded_bytes).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical_token, token):
        raise ValueError("authorization token encoding is not canonical")
    try:
        decoded = json.loads(decoded_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authorization token payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("authorization token payload must be an object")
    if not hmac.compare_digest(_canonical_payload_bytes(decoded), decoded_bytes):
        raise ValueError("authorization token payload encoding is not canonical")
    _validate_token_schema(decoded)
    return decoded


def _validate_token_schema(decoded: dict[str, Any]) -> None:
    if "payload" in decoded:
        if frozenset(decoded) not in {
            frozenset({"payload"}),
            frozenset({"payload", "sig"}),
        }:
            raise ValueError("authorization token schema is invalid")
        payload = decoded["payload"]
        if "sig" in decoded and (
            not isinstance(decoded["sig"], str) or not decoded["sig"]
        ):
            raise ValueError("authorization token schema is invalid")
    else:
        payload = decoded
    if not isinstance(payload, dict):
        raise ValueError("authorization token schema is invalid")
    expected_fields = _AUTHORIZATION_BASE_PAYLOAD_FIELDS
    if payload.get("action") == "backtest_persist":
        expected_fields = expected_fields | {"backtest_start_date"}
    elif payload.get("action") == "signal_gap_fill_write":
        expected_fields = (
            expected_fields | _SIGNAL_GAP_FILL_PAYLOAD_FIELDS
        )
    elif payload.get("action") == "signal_gap_native_artifact_register":
        expected_fields = (
            expected_fields
            | _SIGNAL_GAP_NATIVE_ARTIFACT_PAYLOAD_FIELDS
        )
    elif payload.get("action") == "signal_gap_native_cache_prewarm":
        expected_fields = (
            expected_fields
            | _SIGNAL_GAP_NATIVE_CACHE_PREWARM_PAYLOAD_FIELDS
        )
    if frozenset(payload) != expected_fields:
        raise ValueError("authorization token schema is invalid")


def parse_token(token: str) -> Authorization:
    """解析 token（不做签名/过期校验，仅取字段）。兼容旧版无信封 token。"""
    decoded = _decode_envelope(token)
    if isinstance(decoded, dict) and "payload" in decoded:
        payload = decoded["payload"]
    else:
        payload = decoded
    return Authorization(
        scheme_id=str(payload["scheme_id"]),
        action=str(payload["action"]),
        predict_date=str(payload["predict_date"]) if payload.get("predict_date") is not None else None,
        token=token,
        issued_by=str(payload.get("issued_by", "")),
        issued_at=str(payload.get("issued_at", "")),
        nonce=str(payload.get("nonce", "")),
        scheme_version=str(payload["scheme_version"]) if payload.get("scheme_version") is not None else None,
        harness_run_id=str(payload["harness_run_id"]) if payload.get("harness_run_id") is not None else None,
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
        backtest_start_date=(
            str(payload["backtest_start_date"])
            if payload.get("backtest_start_date") is not None
            else None
        ),
        plan_sha256=(
            str(payload["plan_sha256"])
            if payload.get("plan_sha256") is not None
            else None
        ),
        signal_gap_target_keys=tuple(
            dict(item) for item in payload.get("target_keys", ())
        ),
        source_authority=(
            dict(payload["source_authority"])
            if isinstance(payload.get("source_authority"), dict)
            else None
        ),
    )


def verify_authorization(
    token: str | None,
    *,
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    backtest_start_date: str | None = None,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """从原始 token 校验信封模式、签名、过期、一次性和作用域。"""
    if token is None:
        return None, ["authorization token is required"]

    if not isinstance(token, str):
        return None, ["authorization requires the original raw token string"]
    try:
        envelope = _decode_envelope(token)
    except Exception as exc:  # noqa: BLE001
        return None, [f"invalid authorization token: {exc}"]

    secret = _auth_secret()
    signing_enabled = secret is not None
    expected_envelope_fields = (
        frozenset({"payload", "sig"})
        if signing_enabled
        else frozenset({"payload"})
    )
    if frozenset(envelope) != expected_envelope_fields:
        return None, [
            "invalid authorization token: authorization token envelope does not match signing mode"
        ]
    try:
        auth = parse_token(token)
    except Exception as exc:  # noqa: BLE001
        return None, [f"invalid authorization token: {exc}"]

    errors: list[str] = []
    raw_payload = envelope["payload"]
    if action in {"draft_register", "blackbox_revision_activate"} and (
        not isinstance(raw_payload.get("issued_by"), str)
        or not raw_payload["issued_by"].strip()
    ):
        errors.append(
            f"{action} issued_by must be a non-empty string"
        )
    if signing_enabled:
        digest = hmac.new(
            secret,
            _canonical_payload_bytes(envelope["payload"]),
            hashlib.sha256,
        ).digest()
        expected_sig = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        if not hmac.compare_digest(
            expected_sig,
            str(envelope["sig"]),
        ):
            errors.append("authorization token signature is invalid")

    if auth.expires_at:
        try:
            expires_dt = datetime.fromisoformat(auth.expires_at)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_dt:
                errors.append(f"authorization token expired at {auth.expires_at}")
        except ValueError:
            errors.append(f"authorization token has invalid expires_at: {auth.expires_at}")

    if auth.scheme_id != scheme_id:
        errors.append(f"scheme_id mismatch: token={auth.scheme_id}, ctx={scheme_id}")
    if auth.action != action:
        errors.append(f"action mismatch: token={auth.action}, expected={action}")
    if action in EXACT_PREDICT_DATE_ACTIONS:
        if auth.predict_date is None:
            errors.append(f"{action} authorization predict_date is required")
        elif predict_date is None:
            errors.append(f"{action} context predict_date is required")
        else:
            try:
                token_predict_date = _normalize_action_predict_date(
                    action,
                    auth.predict_date,
                )
                context_predict_date = _normalize_action_predict_date(
                    action,
                    predict_date,
                )
            except ValueError as exc:
                errors.append(str(exc))
            else:
                if token_predict_date != context_predict_date:
                    errors.append(
                        "predict_date mismatch: "
                        f"token={token_predict_date}, ctx={context_predict_date}"
                    )
    elif auth.predict_date is not None and predict_date is not None and auth.predict_date != predict_date:
        errors.append(f"predict_date mismatch: token={auth.predict_date}, ctx={predict_date}")
    if (
        action == "backtest_persist"
        and backtest_start_date is not None
        and auth.backtest_start_date != backtest_start_date
    ):
        errors.append(
            "backtest_start_date mismatch: "
            f"token={auth.backtest_start_date}, ctx={backtest_start_date}"
        )
    try:
        if _token_hash(auth.token) in _read_used_tokens(used_store_path):
            errors.append("authorization token already used")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("authorization replay store is invalid or unavailable")
    return auth, errors


def issue_signal_gap_fill_token(
    *,
    plan_sha256: str,
    base_scheme_id: str,
    predict_date: str,
    target_keys: Any,
    scheme_version: str,
    source_authority: Any,
    ttl_seconds: int = BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS,
    issued_by: str = "harness",
) -> str:
    """签发绑定冻结计划与一个原子算法组的短期 HMAC token。"""
    return issue_token(
        base_scheme_id,
        "signal_gap_fill_write",
        predict_date,
        scheme_version=scheme_version,
        ttl_seconds=ttl_seconds,
        issued_by=issued_by,
        plan_sha256=plan_sha256,
        base_scheme_id=base_scheme_id,
        target_keys=target_keys,
        source_authority=source_authority,
    )


def issue_signal_gap_native_artifact_register_token(
    *,
    historical_predict_date: str,
    source_authority: Any,
    ttl_seconds: int = BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS,
    issued_by: str = "harness",
) -> str:
    """签发绑定完整 Native current-snapshot provenance 的 HMAC token。"""
    return issue_token(
        SIGNAL_GAP_NATIVE_ARTIFACT_SCHEME_ID,
        "signal_gap_native_artifact_register",
        historical_predict_date,
        ttl_seconds=ttl_seconds,
        issued_by=issued_by,
        source_authority=source_authority,
    )


def issue_signal_gap_native_cache_prewarm_token(
    *,
    historical_predict_date: str,
    source_authority: Any,
    ttl_seconds: int = BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS,
    issued_by: str = "harness",
) -> str:
    """签发绑定封存 Native artifact 与唯一 publisher 的短期 HMAC token。"""
    return issue_token(
        SIGNAL_GAP_NATIVE_CACHE_PREWARM_SCHEME_ID,
        "signal_gap_native_cache_prewarm",
        historical_predict_date,
        ttl_seconds=ttl_seconds,
        issued_by=issued_by,
        source_authority=source_authority,
    )


def verify_signal_gap_native_artifact_register_authorization(
    token: str | None,
    *,
    historical_predict_date: str,
    source_authority: Any,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """验证 Native gap artifact 的 capture、feature 与完整 provenance。"""
    auth, errors = verify_authorization(
        token,
        scheme_id=SIGNAL_GAP_NATIVE_ARTIFACT_SCHEME_ID,
        action="signal_gap_native_artifact_register",
        predict_date=historical_predict_date,
        used_store_path=used_store_path,
    )
    if _auth_secret() is None:
        errors.append(
            "signal_gap_native_artifact_register authorization requires "
            "HMAC signing"
        )
    try:
        expected = _normalize_signal_gap_native_artifact_authority(
            source_authority,
            expected_historical_predict_date=historical_predict_date,
        )
    except ValueError as exc:
        errors.append(str(exc))
        return auth, errors
    if auth is None:
        return None, errors
    errors.extend(
        required_future_expiry_errors(
            auth.issued_at,
            auth.expires_at,
        )
    )
    try:
        token_authority = (
            _normalize_signal_gap_native_artifact_authority(
                auth.source_authority,
                expected_historical_predict_date=historical_predict_date,
            )
        )
    except ValueError:
        token_authority = None
    if token_authority != expected:
        errors.append("source authority mismatch")
    return auth, errors


def verify_signal_gap_native_cache_prewarm_authorization(
    token: str | None,
    *,
    historical_predict_date: str,
    source_authority: Any,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """验证封存 Native cache prewarm 的完整 artifact/publisher scope。"""
    auth, errors = verify_authorization(
        token,
        scheme_id=SIGNAL_GAP_NATIVE_CACHE_PREWARM_SCHEME_ID,
        action="signal_gap_native_cache_prewarm",
        predict_date=historical_predict_date,
        used_store_path=used_store_path,
    )
    if _auth_secret() is None:
        errors.append(
            "signal_gap_native_cache_prewarm authorization requires "
            "HMAC signing"
        )
    try:
        expected = _normalize_signal_gap_native_cache_prewarm_authority(
            source_authority,
            expected_historical_predict_date=historical_predict_date,
        )
    except ValueError as exc:
        errors.append(str(exc))
        return auth, errors
    if auth is None:
        return None, errors
    errors.extend(
        required_future_expiry_errors(
            auth.issued_at,
            auth.expires_at,
        )
    )
    try:
        token_authority = (
            _normalize_signal_gap_native_cache_prewarm_authority(
                auth.source_authority,
                expected_historical_predict_date=(
                    historical_predict_date
                ),
            )
        )
    except ValueError:
        token_authority = None
    if token_authority != expected:
        errors.append("source authority mismatch")
    return auth, errors


def _normalize_signal_gap_native_artifact_authority(
    value: Any,
    *,
    expected_historical_predict_date: str | None,
) -> dict[str, Any]:
    expected_fields = frozenset(
        {
            "authority_type",
            "purpose",
            "artifact_id",
            "manifest_uri",
            "manifest_sha256",
            "storage_root_identity",
            "dataset_content_id",
            "source_commit_token",
            "capture_business_date",
            "feature_date",
            "exporter_version",
            "vintage_disclaimer",
        }
    )
    if not isinstance(value, dict) or frozenset(value) != expected_fields:
        raise ValueError(
            "signal-gap Native artifact authority schema is invalid"
        )
    normalized = {
        "authority_type": str(value["authority_type"]),
        "purpose": str(value["purpose"]),
        "artifact_id": _require_text(
            value["artifact_id"],
            "artifact_id",
        ),
        "manifest_uri": _require_text(
            value["manifest_uri"],
            "manifest_uri",
        ),
        "manifest_sha256": _require_sha256(
            value["manifest_sha256"],
            "manifest_sha256",
        ),
        "storage_root_identity": _require_sha256(
            value["storage_root_identity"],
            "storage_root_identity",
        ),
        "dataset_content_id": _require_sha256(
            value["dataset_content_id"],
            "dataset_content_id",
        ),
        "source_commit_token": _require_sha256(
            value["source_commit_token"],
            "source_commit_token",
        ),
        "capture_business_date": _normalize_action_predict_date(
            "signal_gap_native_artifact_register",
            value["capture_business_date"],
        ),
        "feature_date": _normalize_action_predict_date(
            "signal_gap_native_artifact_register",
            value["feature_date"],
        ),
        "exporter_version": str(value["exporter_version"]),
        "vintage_disclaimer": str(value["vintage_disclaimer"]),
    }
    if (
        normalized["authority_type"]
        != "native_current_snapshot_artifact"
        or normalized["purpose"]
        != SIGNAL_GAP_NATIVE_ARTIFACT_PURPOSE
        or normalized["exporter_version"]
        != SIGNAL_GAP_NATIVE_ARTIFACT_EXPORTER_VERSION
        or normalized["vintage_disclaimer"]
        != "current_snapshot_as_of_not_historical_vintage"
    ):
        raise ValueError(
            "signal-gap Native artifact authority purpose is invalid"
        )
    manifest_path = Path(normalized["manifest_uri"])
    if (
        not manifest_path.is_absolute()
        or manifest_path.name != "manifest.json"
        or str(manifest_path) != normalized["manifest_uri"]
    ):
        raise ValueError(
            "signal-gap Native artifact manifest_uri is not canonical"
        )
    historical_predict_date = _normalize_action_predict_date(
        "signal_gap_native_artifact_register",
        expected_historical_predict_date,
    )
    if normalized["feature_date"] >= historical_predict_date:
        raise ValueError(
            "signal-gap Native feature_date must precede predict_date"
        )
    if normalized["capture_business_date"] <= historical_predict_date:
        raise ValueError(
            "signal-gap Native capture date must be after predict_date"
        )
    return normalized


def _normalize_signal_gap_native_cache_prewarm_authority(
    value: Any,
    *,
    expected_historical_predict_date: str | None,
) -> dict[str, Any]:
    expected_fields = frozenset(
        {
            "authority_type",
            "purpose",
            "publisher_scheme_id",
            "historical_predict_date",
            "artifact",
        }
    )
    if not isinstance(value, dict) or frozenset(value) != expected_fields:
        raise ValueError(
            "signal-gap Native cache prewarm authority schema is invalid"
        )
    historical_predict_date = _normalize_action_predict_date(
        "signal_gap_native_cache_prewarm",
        value["historical_predict_date"],
    )
    expected_predict_date = _normalize_action_predict_date(
        "signal_gap_native_cache_prewarm",
        expected_historical_predict_date,
    )
    if historical_predict_date != expected_predict_date:
        raise ValueError(
            "signal-gap Native cache prewarm predict_date mismatch"
        )
    normalized = {
        "authority_type": str(value["authority_type"]),
        "purpose": str(value["purpose"]),
        "publisher_scheme_id": _require_text(
            value["publisher_scheme_id"],
            "publisher_scheme_id",
        ),
        "historical_predict_date": historical_predict_date,
        "artifact": _normalize_signal_gap_native_artifact_authority(
            value["artifact"],
            expected_historical_predict_date=historical_predict_date,
        ),
    }
    if (
        normalized["authority_type"]
        != "native_current_snapshot_cache_prewarm"
        or normalized["purpose"]
        != SIGNAL_GAP_NATIVE_CACHE_PREWARM_PURPOSE
        or normalized["publisher_scheme_id"]
        != SIGNAL_GAP_CACHE_PREWARM_PUBLISHER_SCHEME_ID
    ):
        raise ValueError(
            "signal-gap Native cache prewarm authority purpose is invalid"
        )
    return normalized


def verify_signal_gap_fill_authorization(
    token: str | None,
    *,
    plan_sha256: str,
    base_scheme_id: str,
    predict_date: str,
    target_keys: Any,
    scheme_version: str,
    source_authority: Any,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """验证 signal-gap-fill 的完整计划、目标集合、版本和输入权威。"""
    auth, errors = verify_authorization(
        token,
        scheme_id=base_scheme_id,
        action="signal_gap_fill_write",
        predict_date=predict_date,
        used_store_path=used_store_path,
    )
    if _auth_secret() is None:
        errors.append(
            "signal_gap_fill_write authorization requires HMAC signing"
        )
    try:
        expected_plan = _require_sha256(
            plan_sha256,
            "plan_sha256",
        )
        expected_targets = _normalize_signal_gap_target_keys(
            target_keys,
            expected_base_scheme_id=base_scheme_id,
            expected_predict_date=predict_date,
        )
        expected_authority = _normalize_signal_gap_source_authority(
            source_authority
        )
        normalized_predict_date = _normalize_action_predict_date(
            "signal_gap_fill_write",
            predict_date,
        )
    except ValueError as exc:
        errors.append(str(exc))
        return auth, errors
    if (
        expected_authority["authority_type"]
        == "native_archived_generation"
        and expected_authority["business_date"]
        != normalized_predict_date
    ):
        errors.append(
            "archived Native business_date must equal predict_date"
        )
    if auth is None:
        return None, errors
    errors.extend(
        required_future_expiry_errors(
            auth.issued_at,
            auth.expires_at,
        )
    )
    if auth.plan_sha256 != expected_plan:
        errors.append("plan_sha256 mismatch")
    if auth.scheme_version != scheme_version:
        errors.append(
            "scheme_version mismatch: "
            f"token={auth.scheme_version}, ctx={scheme_version}"
        )
    try:
        token_targets = _normalize_signal_gap_target_keys(
            auth.signal_gap_target_keys,
            expected_base_scheme_id=base_scheme_id,
            expected_predict_date=predict_date,
        )
    except ValueError:
        token_targets = ()
    if token_targets != expected_targets:
        errors.append("target multiset mismatch")
    try:
        token_authority = _normalize_signal_gap_source_authority(
            auth.source_authority
        )
    except ValueError:
        token_authority = None
    if token_authority != expected_authority:
        errors.append("source authority mismatch")
    return auth, errors


def _normalize_signal_gap_target_keys(
    value: Any,
    *,
    expected_base_scheme_id: str,
    expected_predict_date: str | None,
) -> tuple[dict[str, Any], ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
    ):
        raise ValueError(
            "signal gap target_keys must be a non-empty list"
        )
    normalized_predict_date = _normalize_action_predict_date(
        "signal_gap_fill_write",
        expected_predict_date,
    )
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or frozenset(raw) != (
            _SIGNAL_GAP_TARGET_FIELDS
        ):
            raise ValueError(
                "signal gap target key schema is invalid"
            )
        horizon = raw["horizon"]
        if isinstance(horizon, bool) or not isinstance(horizon, int):
            raise ValueError(
                "signal gap target horizon must be an integer"
            )
        row = {
            "registry_scheme_id": _require_text(
                raw["registry_scheme_id"],
                "registry_scheme_id",
            ),
            "base_scheme_id": _require_text(
                raw["base_scheme_id"],
                "base_scheme_id",
            ),
            "target_tenor": _require_text(
                raw["target_tenor"],
                "target_tenor",
            ),
            "horizon": int(horizon),
            "task_type": _require_text(
                raw["task_type"],
                "task_type",
            ),
            "predict_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                raw["predict_date"],
            ),
            "feature_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                raw["feature_date"],
            ),
            "target_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                raw["target_date"],
            ),
            "prediction_phase": str(raw["prediction_phase"]),
        }
        if (
            row["base_scheme_id"] != expected_base_scheme_id
            or row["predict_date"] != normalized_predict_date
            or row["prediction_phase"] != "gray_live"
        ):
            raise ValueError(
                "signal gap target key is outside authorization scope"
            )
        rows.append(row)
    rows.sort(
        key=lambda row: _canonical_payload_bytes(row)
    )
    encoded = [_canonical_payload_bytes(row) for row in rows]
    if len(encoded) != len(set(encoded)):
        raise ValueError("signal gap target multiset contains duplicates")
    return tuple(rows)


def _normalize_signal_gap_source_authority(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("signal gap source_authority must be an object")
    authority_type = value.get("authority_type")
    if authority_type == "native_current_snapshot_artifact":
        expected = frozenset(
            {
                "authority_type",
                "artifact_id",
                "manifest_sha256",
                "feature_date",
                "cutoff_date",
                "vintage_disclaimer",
            }
        )
        if frozenset(value) != expected:
            raise ValueError(
                "Native signal gap source authority schema is invalid"
            )
        normalized = {
            "authority_type": authority_type,
            "artifact_id": _require_text(
                value["artifact_id"],
                "artifact_id",
            ),
            "manifest_sha256": _require_sha256(
                value["manifest_sha256"],
                "manifest_sha256",
            ),
            "feature_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["feature_date"],
            ),
            "cutoff_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["cutoff_date"],
            ),
            "vintage_disclaimer": str(value["vintage_disclaimer"]),
        }
    elif authority_type == "native_archived_generation":
        expected = frozenset(
            {
                "authority_type",
                "generation_id",
                "manifest_sha256",
                "business_date",
                "feature_date",
                "cutoff_date",
                "replay_mode",
            }
        )
        if frozenset(value) != expected:
            raise ValueError(
                "archived Native source authority schema is invalid"
            )
        normalized = {
            "authority_type": authority_type,
            "generation_id": _require_text(
                value["generation_id"],
                "generation_id",
            ),
            "manifest_sha256": _require_sha256(
                value["manifest_sha256"],
                "manifest_sha256",
            ),
            "business_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["business_date"],
            ),
            "feature_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["feature_date"],
            ),
            "cutoff_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["cutoff_date"],
            ),
            "replay_mode": str(value["replay_mode"]),
        }
        if (
            normalized["replay_mode"]
            != "historical_sealed_generation_replay"
        ):
            raise ValueError(
                "archived Native replay_mode is invalid"
            )
        if normalized["cutoff_date"] != normalized["feature_date"]:
            raise ValueError(
                "archived Native cutoff_date must equal feature_date"
            )
        if normalized["feature_date"] >= normalized["business_date"]:
            raise ValueError(
                "archived Native feature_date must precede business_date"
            )
    elif authority_type == "databridge_current_generation":
        expected = frozenset(
            {
                "authority_type",
                "generation_id",
                "manifest_sha256",
                "refresh_date",
                "cutoff_date",
                "replay_mode",
                "vintage_disclaimer",
            }
        )
        if frozenset(value) != expected:
            raise ValueError(
                "DataBridge signal gap source authority schema is invalid"
            )
        normalized = {
            "authority_type": authority_type,
            "generation_id": _require_text(
                value["generation_id"],
                "generation_id",
            ),
            "manifest_sha256": _require_sha256(
                value["manifest_sha256"],
                "manifest_sha256",
            ),
            "refresh_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["refresh_date"],
            ),
            "cutoff_date": _normalize_action_predict_date(
                "signal_gap_fill_write",
                value["cutoff_date"],
            ),
            "replay_mode": str(value["replay_mode"]),
            "vintage_disclaimer": str(value["vintage_disclaimer"]),
        }
        if normalized["replay_mode"] != "historical_as_of_replay":
            raise ValueError(
                "DataBridge signal gap replay_mode is invalid"
            )
    else:
        raise ValueError("signal gap source authority type is invalid")
    if (
        authority_type != "native_archived_generation"
        and normalized["vintage_disclaimer"]
        != "current_snapshot_as_of_not_historical_vintage"
    ):
        raise ValueError(
            "signal gap source authority vintage disclaimer is invalid"
        )
    return normalized


def _require_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_sha256(value: Any, field: str) -> str:
    normalized = _require_text(value, field)
    if (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return normalized


def normalize_backtest_start_date(value: str | None) -> str:
    """规范化持久化回测起点；缺失时使用平台默认日期。"""
    resolved = DEFAULT_BACKTEST_START_DATE if value is None else value
    if not isinstance(resolved, str) or not resolved.strip() or resolved != resolved.strip():
        raise ValueError("backtest_start_date must be a canonical YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(resolved)
    except ValueError as exc:
        raise ValueError("backtest_start_date must be a canonical YYYY-MM-DD date") from exc
    if parsed.isoformat() != resolved:
        raise ValueError("backtest_start_date must be a canonical YYYY-MM-DD date")
    return resolved


def _normalize_backtest_predict_date(value: str | None) -> str:
    """要求持久化回测 cutoff 为非空、规范 ISO 日期。"""
    return _normalize_action_predict_date("backtest_persist", value)


def _normalize_action_predict_date(action: str, value: str | None) -> str:
    """要求精确日期授权使用非空、规范 ISO 日期。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(
            f"{action} predict_date must be a canonical YYYY-MM-DD date"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{action} predict_date must be a canonical YYYY-MM-DD date"
        ) from exc
    if parsed.isoformat() != value:
        raise ValueError(
            f"{action} predict_date must be a canonical YYYY-MM-DD date"
        )
    return value


def required_future_expiry_errors(
    issued_at: str | None,
    expires_at: str | None,
) -> list[str]:
    """校验 Blackbox Activation/Reconcile 的签发时间与短 TTL。"""
    errors: list[str] = []
    issued_dt = _parse_required_aware_timestamp("issued_at", issued_at, errors)
    expires_dt = _parse_required_aware_timestamp("expires_at", expires_at, errors)
    if issued_dt is None or expires_dt is None:
        return errors

    now = datetime.now(timezone.utc)
    if issued_dt > now + timedelta(seconds=BLACKBOX_PRIVILEGED_AUTH_MAX_FUTURE_SKEW_SECONDS):
        errors.append(
            "authorization issued_at is materially in the future: "
            f"{issued_at}"
        )
    if expires_dt <= now:
        errors.append(f"authorization expires_at must be in the future: {expires_at}")

    ttl_seconds = (expires_dt - issued_dt).total_seconds()
    if ttl_seconds <= 0:
        errors.append("authorization expires_at must be after issued_at")
    elif ttl_seconds > BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS:
        errors.append(
            "authorization lifetime from issued_at to expires_at must be no more than "
            f"{BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS} seconds"
        )
    return errors


def _parse_required_aware_timestamp(
    field: str,
    value: str | None,
    errors: list[str],
) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"authorization {field} must be a non-empty timezone-aware timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"authorization token has invalid {field}: {value}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"authorization {field} must include a timezone offset: {value}")
        return None
    return parsed.astimezone(timezone.utc)


def mark_token_used(auth: Authorization, used_store_path: Path) -> None:
    _decode_envelope(auth.token)
    token_hash = _token_hash(auth.token)
    used_store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = used_store_path.with_name(f"{used_store_path.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            used = _read_used_tokens(used_store_path)
            if token_hash in used:
                raise AuthorizationTokenAlreadyUsedError("authorization token already used")
            used.add(token_hash)
            _atomic_write_json(used_store_path, sorted(used))
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def write_authorization_audit(auth: Authorization, audit_dir: Path) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "authorization.json"
    payload: dict[str, Any] = asdict(auth)
    payload["token_sha256"] = _token_hash(auth.token)
    payload.pop("token", None)
    _atomic_write_json(path, payload)
    return path


def used_tokens_path(project_root: Path) -> Path:
    return project_root / "reports" / "harness" / ".used_authorization_tokens.json"


def _read_used_tokens(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("authorization replay store must contain a JSON array")
    if any(
        not isinstance(item, str)
        or len(item) != 64
        or any(character not in "0123456789abcdef" for character in item)
        for item in payload
    ):
        raise ValueError("authorization replay store contains an invalid token hash")
    if len(payload) != len(set(payload)):
        raise ValueError("authorization replay store contains duplicate token hashes")
    return set(payload)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
