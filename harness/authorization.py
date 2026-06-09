from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


AUTH_SECRET_ENV = "HARNESS_AUTH_SECRET"


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


class AuthorizationSecretError(RuntimeError):
    """未配置 HARNESS_AUTH_SECRET 时抛出（fail-closed）。"""


def _auth_secret() -> bytes:
    secret = os.environ.get(AUTH_SECRET_ENV)
    if not secret:
        raise AuthorizationSecretError(
            f"{AUTH_SECRET_ENV} is not set; cannot issue or verify signed authorization tokens"
        )
    return secret.encode("utf-8")


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(payload: dict[str, Any]) -> str:
    digest = hmac.new(_auth_secret(), _canonical_payload_bytes(payload), hashlib.sha256).digest()
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
) -> str:
    """签发一个 HMAC 签名、可选 TTL 的一次性授权 token。

    若 HARNESS_AUTH_SECRET 未配置，抛出 AuthorizationSecretError（fail-closed）。
    """
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
    signature = _sign(payload)
    envelope = {"payload": payload, "sig": signature}
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_envelope(token: str) -> dict[str, Any]:
    padding = "=" * (-len(token) % 4)
    decoded = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
    return json.loads(decoded)


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
    )


def verify_authorization(
    token: str | Authorization | None,
    *,
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """校验 token 的 HMAC 签名、过期、一次性、作用域绑定。"""
    if token is None:
        return None, ["authorization token is required"]

    # Authorization 实例不携带签名信封，无法重新校验 HMAC；只在传入原始字符串时校验。
    if isinstance(token, Authorization):
        auth = token
        signature_checked = False
        envelope = None
    else:
        try:
            envelope = _decode_envelope(str(token))
        except Exception as exc:  # noqa: BLE001
            return None, [f"invalid authorization token: {exc}"]
        try:
            auth = parse_token(str(token))
        except Exception as exc:  # noqa: BLE001
            return None, [f"invalid authorization token: {exc}"]
        signature_checked = True

    errors: list[str] = []

    if signature_checked:
        if not isinstance(envelope, dict) or "payload" not in envelope or "sig" not in envelope:
            errors.append("authorization token is missing HMAC signature")
        else:
            try:
                expected_sig = _sign(envelope["payload"])
            except AuthorizationSecretError as exc:
                return None, [str(exc)]
            if not hmac.compare_digest(expected_sig, str(envelope.get("sig", ""))):
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
    if auth.predict_date is not None and predict_date is not None and auth.predict_date != predict_date:
        errors.append(f"predict_date mismatch: token={auth.predict_date}, ctx={predict_date}")
    if _token_hash(auth.token) in _read_used_tokens(used_store_path):
        errors.append("authorization token already used")
    return auth, errors


def mark_token_used(auth: Authorization, used_store_path: Path) -> None:
    used = _read_used_tokens(used_store_path)
    used.add(_token_hash(auth.token))
    used_store_path.parent.mkdir(parents=True, exist_ok=True)
    used_store_path.write_text(json.dumps(sorted(used), ensure_ascii=False, indent=2), encoding="utf-8")


def write_authorization_audit(auth: Authorization, audit_dir: Path) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "authorization.json"
    payload: dict[str, Any] = asdict(auth)
    payload["token_sha256"] = _token_hash(auth.token)
    payload.pop("token", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def used_tokens_path(project_root: Path) -> Path:
    return project_root / "reports" / "harness" / ".used_authorization_tokens.json"


def _read_used_tokens(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
