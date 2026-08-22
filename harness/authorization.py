from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from shared.runtime_paths import resolve_runtime_state_path

DEFAULT_BACKTEST_START_DATE = "2025-01-01"
USED_TOKENS_FILENAME = ".used_authorization_tokens.json"
USED_TOKENS_RELATIVE_PATH = f"reports/harness/{USED_TOKENS_FILENAME}"

SIDE_EFFECT_ACTIONS = frozenset(
    {
        "activate",
        "live_write",
        "backtest_persist",
        "draft_register",
        "shadow_register",
        "blackbox_activate",
        "blackbox_lifecycle_bootstrap",
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
        "blackbox_lifecycle_bootstrap",
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
    nonce: str
    scheme_version: str
    harness_run_id: str | None
    backtest_start_date: str | None = None


class AuthorizationTokenAlreadyUsedError(RuntimeError):
    """本次直接操作授权已经被消费。"""


def authorization_token_hash(token: str | Authorization) -> str:
    """返回可持久化的直接操作 ID 摘要。"""
    raw = token.token if isinstance(token, Authorization) else str(token)
    return _token_hash(raw)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def issue_token(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str | None = None,
    harness_run_id: str | None = None,
    issued_by: str = "harness",
    backtest_start_date: str | None = None,
) -> str:
    """构造一次 CLI 调用内部使用的直接操作授权。

    该值不是凭据，不依赖密钥，也不应由操作者手工签发或复制。CLI 在执行副作用命令时
    自动构造它；Gate 再把系统选出的 exact Harness run 绑定到审计记录。保留这个函数名
    只为兼容内部调用和测试，用户操作面不再暴露 ``auth issue``。
    """
    if action not in SIDE_EFFECT_ACTIONS:
        raise ValueError(f"unknown authorization action: {action}")
    normalized_scheme_id = _require_text(scheme_id, "scheme_id")
    normalized_version = _require_text(scheme_version, "scheme_version")
    normalized_issuer = _require_text(issued_by, "issued_by")
    if action in EXACT_PREDICT_DATE_ACTIONS:
        normalized_predict_date = _normalize_action_predict_date(action, predict_date)
    else:
        if predict_date is not None:
            raise ValueError(f"{action} authorization does not accept predict_date")
        normalized_predict_date = None

    if harness_run_id is None:
        normalized_run_id = None
    else:
        normalized_run_id = _require_text(harness_run_id, "harness_run_id")

    payload: dict[str, Any] = {
        "action": action,
        "scheme_id": normalized_scheme_id,
        "scheme_version": normalized_version,
        "predict_date": normalized_predict_date,
        "harness_run_id": normalized_run_id,
        "issued_by": normalized_issuer,
        "nonce": secrets.token_urlsafe(16),
    }
    if action == "backtest_persist":
        payload["backtest_start_date"] = normalize_backtest_start_date(
            backtest_start_date
        )
    envelope = {"payload": payload}
    return base64.urlsafe_b64encode(_canonical_json_bytes(envelope)).decode(
        "ascii"
    ).rstrip("=")


def _decode_envelope(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or not token.isascii():
        raise ValueError("direct authorization encoding is not canonical")
    if "=" in token or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in token
    ):
        raise ValueError("direct authorization encoding is not canonical")
    padding = "=" * (-len(token) % 4)
    try:
        decoded_bytes = base64.b64decode(
            (token + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("direct authorization encoding is not canonical") from exc
    canonical_token = base64.urlsafe_b64encode(decoded_bytes).decode("ascii").rstrip("=")
    if canonical_token != token:
        raise ValueError("direct authorization encoding is not canonical")
    try:
        decoded = json.loads(decoded_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("direct authorization payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("direct authorization schema is invalid")
    if _canonical_json_bytes(decoded) != decoded_bytes:
        raise ValueError("direct authorization payload encoding is not canonical")
    _validate_token_schema(decoded)
    return decoded


def _validate_token_schema(envelope: dict[str, Any]) -> None:
    if frozenset(envelope) != frozenset({"payload"}):
        raise ValueError("direct authorization schema is invalid")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("direct authorization schema is invalid")
    action = payload.get("action")
    if action not in SIDE_EFFECT_ACTIONS:
        raise ValueError("direct authorization schema is invalid")
    expected_fields = _BASE_PAYLOAD_FIELDS
    if action == "backtest_persist":
        expected_fields = expected_fields | {"backtest_start_date"}
    if frozenset(payload) != expected_fields:
        raise ValueError("direct authorization schema is invalid")
    _require_text(payload.get("scheme_id"), "scheme_id")
    _require_text(payload.get("scheme_version"), "scheme_version")
    _require_text(payload.get("issued_by"), "issued_by")
    _require_text(payload.get("nonce"), "nonce")
    harness_run_id = payload.get("harness_run_id")
    if harness_run_id is not None:
        _require_text(harness_run_id, "harness_run_id")
    if action in EXACT_PREDICT_DATE_ACTIONS:
        _normalize_action_predict_date(action, payload.get("predict_date"))
    elif payload.get("predict_date") is not None:
        raise ValueError(f"{action} authorization does not accept predict_date")
    if action == "backtest_persist":
        normalize_backtest_start_date(payload.get("backtest_start_date"))


def parse_token(token: str) -> Authorization:
    """解析 CLI 内部生成的 canonical 直接操作授权。"""
    payload = _decode_envelope(token)["payload"]
    return Authorization(
        scheme_id=payload["scheme_id"],
        action=payload["action"],
        predict_date=payload["predict_date"],
        token=token,
        issued_by=payload["issued_by"],
        nonce=payload["nonce"],
        scheme_version=payload["scheme_version"],
        harness_run_id=payload["harness_run_id"],
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
    """校验直接操作意图、精确作用域与 replay store。

    ``harness_run_id`` 由 Gate 从数据库选择，不再要求单人操作者查询并誊写。若内部测试或
    调用方显式提供了 run id，仍会逐字校验；否则在验证成功后将系统选择的 run id 绑定到
    返回的审计对象。
    """
    if token is None:
        return None, ["direct operator command authorization is required"]
    if not isinstance(token, str):
        return None, ["direct authorization requires an internal operation id"]
    try:
        auth = parse_token(token)
    except Exception as exc:  # noqa: BLE001
        return None, [f"invalid direct authorization: {exc}"]

    errors: list[str] = []
    if auth.scheme_id != scheme_id:
        errors.append(
            f"scheme_id mismatch: operation={auth.scheme_id}, ctx={scheme_id}"
        )
    if auth.action != action:
        errors.append(
            f"action mismatch: operation={auth.action}, expected={action}"
        )
    if auth.scheme_version != scheme_version:
        errors.append(
            "scheme_version mismatch: "
            f"operation={auth.scheme_version}, ctx={scheme_version}"
        )
    expected_harness_run_id = harness_run_id
    expected_harness_run_is_valid = True
    if action in HARNESS_RUN_SCOPED_ACTIONS:
        try:
            expected_harness_run_id = _require_text(
                harness_run_id,
                "expected harness_run_id",
            )
        except ValueError as exc:
            errors.append(str(exc))
            expected_harness_run_is_valid = False
    if (
        expected_harness_run_is_valid
        and auth.harness_run_id is not None
        and auth.harness_run_id != expected_harness_run_id
    ):
        errors.append(
            "harness_run_id mismatch: "
            f"operation={auth.harness_run_id}, ctx={expected_harness_run_id}"
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
                    f"operation={auth.predict_date}, ctx={expected_predict_date}"
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
                    f"operation={auth.backtest_start_date}, ctx={expected_start}"
                )
    try:
        if _token_hash(auth.token) in _read_used_tokens(used_store_path):
            errors.append("direct authorization operation already used")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("authorization replay store is invalid or unavailable")
    if not errors and auth.harness_run_id is None:
        auth = replace(auth, harness_run_id=expected_harness_run_id)
    return auth, errors


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
                    "direct authorization operation already used"
                )
            used.add(token_hash)
            _atomic_write_json(used_store_path, sorted(used))
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def write_authorization_audit(auth: Authorization, audit_dir: Path) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "authorization.json"
    payload: dict[str, Any] = asdict(auth)
    payload["authorization_mode"] = "direct_operator_command_v1"
    payload["operation_id_sha256"] = _token_hash(auth.token)
    payload.pop("token", None)
    _atomic_write_json(path, payload)
    return path


def used_tokens_path(project_root: Path) -> Path:
    """单次 CLI 操作 ID 的重放保护存储。

    该记录是**主机级**状态，必须跨 source release 存活：release 的 `reports/` 出厂即空
    （`reports/**` 被 gitignore），把它留在源码树内会使已消费 operation id 在切换 release
    后复活。操作者不需要接触该文件。
    """
    return resolve_runtime_state_path(
        relative_path=USED_TOKENS_RELATIVE_PATH,
        development_default=(
            Path(project_root) / "reports" / "harness" / USED_TOKENS_FILENAME
        ),
    )


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
        raise ValueError("authorization replay store contains an invalid operation hash")
    if len(payload) != len(set(payload)):
        raise ValueError("authorization replay store contains duplicate operation hashes")
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
