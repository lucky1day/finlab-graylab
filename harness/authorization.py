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

AUTH_SECRET_ENV = "HARNESS_AUTH_SECRET"
DEFAULT_BACKTEST_START_DATE = "2025-01-01"
AUTH_MAX_TTL_SECONDS = 900
AUTH_MAX_FUTURE_SKEW_SECONDS = 60

SIDE_EFFECT_ACTIONS = frozenset(
    {
        "activate",
        "live_write",
        "backtest_persist",
        "draft_register",
        "shadow_register",
        "blackbox_activate",
        "blackbox_reconcile",
        "blackbox_revision_activate",
    }
)
EXACT_PREDICT_DATE_ACTIONS = frozenset(
    {
        "live_write",
        "backtest_persist",
        "draft_register",
        "shadow_register",
        "blackbox_revision_activate",
    }
)
HARNESS_RUN_SCOPED_ACTIONS = frozenset(
    {
        "draft_register",
        "shadow_register",
        "blackbox_activate",
        "blackbox_reconcile",
        "blackbox_revision_activate",
    }
)
_BASE_PAYLOAD_FIELDS = frozenset(
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


@dataclass(frozen=True)
class Authorization:
    scheme_id: str
    action: str
    predict_date: str | None
    token: str
    issued_by: str
    issued_at: str
    nonce: str
    scheme_version: str
    harness_run_id: str | None
    expires_at: str
    backtest_start_date: str | None = None


class AuthorizationSecretError(RuntimeError):
    """授权密钥未配置。"""


class AuthorizationTokenAlreadyUsedError(RuntimeError):
    """授权 token 已被权威消费。"""


def _auth_secret() -> bytes:
    secret = os.environ.get(AUTH_SECRET_ENV)
    if not isinstance(secret, str) or not secret:
        raise AuthorizationSecretError(
            "HARNESS_AUTH_SECRET is required for authorization tokens"
        )
    return secret.encode("utf-8")


def authorization_token_hash(token: str | Authorization) -> str:
    """返回可安全持久化的授权 token SHA-256。"""
    raw = token.token if isinstance(token, Authorization) else str(token)
    return _token_hash(raw)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign(payload: dict[str, Any]) -> str:
    """使用必需的 HMAC 密钥签名 canonical payload。"""
    digest = hmac.new(
        _auth_secret(),
        _canonical_json_bytes(payload),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_token(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str | None = None,
    harness_run_id: str | None = None,
    ttl_seconds: int = AUTH_MAX_TTL_SECONDS,
    issued_by: str = "harness",
    backtest_start_date: str | None = None,
) -> str:
    """签发最长 15 分钟、绑定精确作用域的一次性 HMAC token。"""
    _auth_secret()
    if action not in SIDE_EFFECT_ACTIONS:
        raise ValueError(f"unknown authorization action: {action}")
    normalized_scheme_id = _require_text(scheme_id, "scheme_id")
    normalized_version = _require_text(scheme_version, "scheme_version")
    normalized_issuer = _require_text(issued_by, "issued_by")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("authorization ttl_seconds must be an integer in 1..900")
    if ttl_seconds < 1 or ttl_seconds > AUTH_MAX_TTL_SECONDS:
        raise ValueError("authorization ttl_seconds must be in 1..900")

    if action in EXACT_PREDICT_DATE_ACTIONS:
        normalized_predict_date = _normalize_action_predict_date(action, predict_date)
    else:
        if predict_date is not None:
            raise ValueError(f"{action} authorization does not accept predict_date")
        normalized_predict_date = None

    if action in HARNESS_RUN_SCOPED_ACTIONS:
        normalized_run_id = _require_text(harness_run_id, "harness_run_id")
    elif harness_run_id is None:
        normalized_run_id = None
    else:
        normalized_run_id = _require_text(harness_run_id, "harness_run_id")

    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    payload: dict[str, Any] = {
        "action": action,
        "scheme_id": normalized_scheme_id,
        "scheme_version": normalized_version,
        "predict_date": normalized_predict_date,
        "harness_run_id": normalized_run_id,
        "issued_by": normalized_issuer,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(seconds=ttl_seconds)).isoformat(),
        "nonce": secrets.token_urlsafe(16),
    }
    if action == "backtest_persist":
        payload["backtest_start_date"] = normalize_backtest_start_date(
            backtest_start_date
        )
    envelope = {"payload": payload, "sig": _sign(payload)}
    return base64.urlsafe_b64encode(_canonical_json_bytes(envelope)).decode(
        "ascii"
    ).rstrip("=")


def _decode_envelope(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or not token.isascii():
        raise ValueError("authorization token encoding is not canonical")
    if "=" in token or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
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
        raise ValueError("authorization token schema is invalid")
    if not hmac.compare_digest(_canonical_json_bytes(decoded), decoded_bytes):
        raise ValueError("authorization token payload encoding is not canonical")
    _validate_token_schema(decoded)
    return decoded


def _validate_token_schema(envelope: dict[str, Any]) -> None:
    if frozenset(envelope) != frozenset({"payload", "sig"}):
        raise ValueError("authorization token schema is invalid")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("authorization token schema is invalid")
    action = payload.get("action")
    if action not in SIDE_EFFECT_ACTIONS:
        raise ValueError("authorization token schema is invalid")
    expected_fields = _BASE_PAYLOAD_FIELDS
    if action == "backtest_persist":
        expected_fields = expected_fields | {"backtest_start_date"}
    if frozenset(payload) != expected_fields:
        raise ValueError("authorization token schema is invalid")
    _validate_canonical_signature(envelope.get("sig"))
    _require_text(payload.get("scheme_id"), "scheme_id")
    _require_text(payload.get("scheme_version"), "scheme_version")
    _require_text(payload.get("issued_by"), "issued_by")
    _require_text(payload.get("nonce"), "nonce")
    _require_text(payload.get("issued_at"), "issued_at")
    _require_text(payload.get("expires_at"), "expires_at")
    harness_run_id = payload.get("harness_run_id")
    if action in HARNESS_RUN_SCOPED_ACTIONS:
        _require_text(harness_run_id, "harness_run_id")
    elif harness_run_id is not None:
        _require_text(harness_run_id, "harness_run_id")
    if action in EXACT_PREDICT_DATE_ACTIONS:
        _normalize_action_predict_date(action, payload.get("predict_date"))
    elif payload.get("predict_date") is not None:
        raise ValueError(f"{action} authorization does not accept predict_date")
    if action == "backtest_persist":
        normalize_backtest_start_date(payload.get("backtest_start_date"))


def _validate_canonical_signature(signature: Any) -> None:
    if not isinstance(signature, str) or not signature or "=" in signature:
        raise ValueError("authorization token schema is invalid")
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in signature
    ):
        raise ValueError("authorization token schema is invalid")
    try:
        decoded = base64.b64decode(
            (signature + "=" * (-len(signature) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("authorization token schema is invalid") from exc
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != hashlib.sha256().digest_size or not hmac.compare_digest(
        canonical,
        signature,
    ):
        raise ValueError("authorization token schema is invalid")


def parse_token(token: str) -> Authorization:
    """只解析精确 signed canonical envelope，不替代完整验证。"""
    payload = _decode_envelope(token)["payload"]
    return Authorization(
        scheme_id=payload["scheme_id"],
        action=payload["action"],
        predict_date=payload["predict_date"],
        token=token,
        issued_by=payload["issued_by"],
        issued_at=payload["issued_at"],
        nonce=payload["nonce"],
        scheme_version=payload["scheme_version"],
        harness_run_id=payload["harness_run_id"],
        expires_at=payload["expires_at"],
        backtest_start_date=payload.get("backtest_start_date"),
    )


def verify_authorization(
    token: str | None,
    *,
    scheme_id: str,
    action: str,
    scheme_version: str,
    predict_date: str | None = None,
    harness_run_id: str | None = None,
    backtest_start_date: str | None = None,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """统一校验密钥、签名、时效、精确作用域与 replay store。"""
    try:
        secret = _auth_secret()
    except AuthorizationSecretError as exc:
        return None, [str(exc)]
    if token is None:
        return None, ["authorization token is required"]
    if not isinstance(token, str):
        return None, ["authorization requires the original raw token string"]
    try:
        envelope = _decode_envelope(token)
        auth = parse_token(token)
    except Exception as exc:  # noqa: BLE001
        return None, [f"invalid authorization token: {exc}"]

    errors: list[str] = []
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(
            secret,
            _canonical_json_bytes(envelope["payload"]),
            hashlib.sha256,
        ).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected_signature, envelope["sig"]):
        errors.append("authorization token signature is invalid")
    errors.extend(_timestamp_errors(auth.issued_at, auth.expires_at))

    if auth.scheme_id != scheme_id:
        errors.append(f"scheme_id mismatch: token={auth.scheme_id}, ctx={scheme_id}")
    if auth.action != action:
        errors.append(f"action mismatch: token={auth.action}, expected={action}")
    if auth.scheme_version != scheme_version:
        errors.append(
            "scheme_version mismatch: "
            f"token={auth.scheme_version}, ctx={scheme_version}"
        )
    if auth.harness_run_id != harness_run_id:
        errors.append(
            "harness_run_id mismatch: "
            f"token={auth.harness_run_id}, ctx={harness_run_id}"
        )

    if action in EXACT_PREDICT_DATE_ACTIONS:
        try:
            expected_predict_date = _normalize_action_predict_date(action, predict_date)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if auth.predict_date != expected_predict_date:
                errors.append(
                    "predict_date mismatch: "
                    f"token={auth.predict_date}, ctx={expected_predict_date}"
                )
    elif predict_date is not None:
        errors.append(f"{action} authorization does not accept predict_date")
    if action == "backtest_persist":
        try:
            expected_start = normalize_backtest_start_date(backtest_start_date)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if auth.backtest_start_date != expected_start:
                errors.append(
                    "backtest_start_date mismatch: "
                    f"token={auth.backtest_start_date}, ctx={expected_start}"
                )
    try:
        if _token_hash(auth.token) in _read_used_tokens(used_store_path):
            errors.append("authorization token already used")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("authorization replay store is invalid or unavailable")
    return auth, errors


def _timestamp_errors(issued_at: str, expires_at: str) -> list[str]:
    errors: list[str] = []
    issued = _parse_aware_timestamp("issued_at", issued_at, errors)
    expires = _parse_aware_timestamp("expires_at", expires_at, errors)
    if issued is None or expires is None:
        return errors
    now = datetime.now(timezone.utc)
    if issued > now + timedelta(seconds=AUTH_MAX_FUTURE_SKEW_SECONDS):
        errors.append(f"authorization issued_at is materially in the future: {issued_at}")
    if expires <= now:
        errors.append(f"authorization token expired at {expires_at}")
    ttl_seconds = (expires - issued).total_seconds()
    if ttl_seconds < 1 or ttl_seconds > AUTH_MAX_TTL_SECONDS:
        errors.append("authorization lifetime must be in 1..900 seconds")
    return errors


def _parse_aware_timestamp(
    field: str,
    value: str,
    errors: list[str],
) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        errors.append(f"authorization token has invalid {field}: {value}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"authorization {field} must include a timezone offset: {value}")
        return None
    return parsed.astimezone(timezone.utc)


def _require_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(f"{field} must be a non-empty string")
    return value


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
                raise AuthorizationTokenAlreadyUsedError(
                    "authorization token already used"
                )
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
