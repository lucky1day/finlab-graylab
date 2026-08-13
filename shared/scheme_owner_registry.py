"""方案归属登记的低层纯合同。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from shared.scheme_config_schema import ALLOWED_TENORS, SCHEME_ID_PATTERN


OWNER_SCHEMA_VERSION = "scheme-owner-v1"
OWNER_FILE_RELATIVE_PATH = Path("deploy") / "scheme_owner_v1.json"
OWNER_PLACEHOLDERS = frozenset({"--", "unknown", "待定"})
COMPOSITE_SCHEME_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__h[1-9][0-9]*__(?:1Y|3Y|5Y|7Y|10Y)$"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SchemeOwnerError(RuntimeError):
    """归属登记文件缺失或不满足读取契约。"""


def normalize_scheme_owner(value: object) -> str:
    """把单个 owner 规范化为可展示且无歧义的纯文本。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("owner must be a non-empty string")
    owner = value.strip()
    if any(marker in owner for marker in ("\n", "\r", "<", ">")):
        raise ValueError("owner must be single-line plain text")
    if owner.casefold() in OWNER_PLACEHOLDERS:
        raise ValueError("owner must not use a placeholder value")
    return owner


def owner_registry_scheme_id(
    base_scheme_id: str,
    horizon: int,
    target_tenor: str,
) -> str:
    """生成 owner registry 使用的 canonical composite scheme_id。"""
    if not isinstance(base_scheme_id, str) or not SCHEME_ID_PATTERN.fullmatch(
        base_scheme_id
    ):
        raise ValueError("base_scheme_id must match ^[a-z][a-z0-9_]*$")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    if target_tenor not in ALLOWED_TENORS:
        raise ValueError(f"unsupported target_tenor: {target_tenor}")
    return f"{base_scheme_id}__h{horizon}__{target_tenor}"


def load_scheme_owner_document(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """严格读取 owner registry，并同时返回原始文档和规范化映射。"""
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except OSError as exc:
        raise SchemeOwnerError(
            f"scheme owner registry is unreadable: {path}"
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise SchemeOwnerError(
            f"scheme owner registry is invalid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SchemeOwnerError("scheme owner registry must be a JSON object")
    if payload.get("schema_version") != OWNER_SCHEMA_VERSION:
        raise SchemeOwnerError(
            "scheme owner registry schema_version must be "
            f"{OWNER_SCHEMA_VERSION!r}, got {payload.get('schema_version')!r}"
        )
    owners = payload.get("owners")
    if not isinstance(owners, dict):
        raise SchemeOwnerError("scheme owner registry owners must be an object")
    result: dict[str, str] = {}
    for scheme_id, owner in owners.items():
        if (
            not isinstance(scheme_id, str)
            or not scheme_id
            or scheme_id != scheme_id.strip()
            or not COMPOSITE_SCHEME_ID_PATTERN.fullmatch(scheme_id)
        ):
            raise SchemeOwnerError(
                "scheme owner registry key must be a canonical composite "
                f"scheme_id: {scheme_id!r}"
            )
        try:
            result[scheme_id] = normalize_scheme_owner(owner)
        except ValueError as exc:
            raise SchemeOwnerError(
                f"scheme owner registry has invalid owner for {scheme_id}"
            ) from exc
    return payload, result


def load_scheme_owners(project_root: Path | None = None) -> dict[str, str]:
    """读取 composite scheme_id -> owner 登记映射。"""
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    _, owners = load_scheme_owner_document(root / OWNER_FILE_RELATIVE_PATH)
    return owners


def serialize_scheme_owner_document(payload: dict[str, Any]) -> bytes:
    """把已验证并更新的 owner 文档编码成稳定 UTF-8 JSON。"""
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
