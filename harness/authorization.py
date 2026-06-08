from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Authorization:
    scheme_id: str
    action: str
    predict_date: str | None
    token: str
    issued_by: str
    issued_at: str
    nonce: str


def issue_token(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    issued_by: str = "harness",
) -> str:
    """签发一个绑定 scheme/action/predict_date 的一次性授权 token。"""
    payload = {
        "scheme_id": scheme_id,
        "action": action,
        "predict_date": predict_date,
        "issued_by": issued_by,
        "issued_at": _utc_now(),
        "nonce": secrets.token_urlsafe(16),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_token(token: str) -> Authorization:
    padding = "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8"))
    return Authorization(
        scheme_id=str(payload["scheme_id"]),
        action=str(payload["action"]),
        predict_date=str(payload["predict_date"]) if payload.get("predict_date") is not None else None,
        token=token,
        issued_by=str(payload.get("issued_by", "")),
        issued_at=str(payload.get("issued_at", "")),
        nonce=str(payload.get("nonce", "")),
    )


def verify_authorization(
    token: str | Authorization | None,
    *,
    scheme_id: str,
    action: str,
    predict_date: str | None,
    used_store_path: Path,
) -> tuple[Authorization | None, list[str]]:
    """校验 token 作用域与一次性使用状态。"""
    if token is None:
        return None, ["authorization token is required"]
    try:
        auth = token if isinstance(token, Authorization) else parse_token(str(token))
    except Exception as exc:
        return None, [f"invalid authorization token: {exc}"]
    errors: list[str] = []
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
