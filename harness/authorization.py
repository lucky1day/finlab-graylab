from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


AUTH_SECRET_ENV = "HARNESS_AUTH_SECRET"
BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS = 900


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
    """保留以兼容既有 import；当前软默认模式下不再主动抛出。"""


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
) -> str:
    """签发一次性授权 token（写库/激活前的确认闸）。

    配置了 HARNESS_AUTH_SECRET 时附带 HMAC 签名；未配置时退化为明文信封，
    token 仍承担一次性 + 作用域绑定的确认职责（软默认，单用户场景无需配置）。
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
    envelope: dict[str, Any] = {"payload": payload}
    if signature is not None:
        envelope["sig"] = signature
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

    # 软默认：仅当配置了 HARNESS_AUTH_SECRET 时才强制校验 HMAC 签名。
    # 未配置时跳过签名校验，token 退化为一次性 + 作用域绑定的确认闸。
    if signature_checked and _auth_secret() is not None:
        if not isinstance(envelope, dict) or "payload" not in envelope or "sig" not in envelope:
            errors.append("authorization token is missing HMAC signature")
        else:
            expected_sig = _sign(envelope["payload"])
            if expected_sig is None or not hmac.compare_digest(expected_sig, str(envelope.get("sig", ""))):
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


def required_future_expiry_errors(expires_at: str | None) -> list[str]:
    """校验 Blackbox 高权限操作强制要求的短期未来过期时间。"""
    if not isinstance(expires_at, str) or not expires_at.strip():
        return ["authorization expires_at must be a non-empty future timestamp"]
    try:
        expires_dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return [f"authorization token has invalid expires_at: {expires_at}"]
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    expires_dt = expires_dt.astimezone(timezone.utc)
    if expires_dt <= now:
        return [f"authorization expires_at must be in the future: {expires_at}"]
    if (expires_dt - now).total_seconds() > BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS:
        return [
            "authorization expires_at must be no more than "
            f"{BLACKBOX_PRIVILEGED_AUTH_MAX_TTL_SECONDS} seconds in the future: {expires_at}"
        ]
    return []


def mark_token_used(auth: Authorization, used_store_path: Path) -> None:
    used = _read_used_tokens(used_store_path)
    used.add(_token_hash(auth.token))
    _atomic_write_json(used_store_path, sorted(used))


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
        return set()
    return {str(item) for item in payload}


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
