"""方案归属同事的只读登记。

归属是平台对方案的登记信息，按 registry composite ``scheme_id`` 索引。它不
参与任何计算、gate 或 join，也不进入 ``scheme_version``；因此记录在版本控制
文件中，而不是方案配置或数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

OWNER_SCHEMA_VERSION = "scheme-owner-v1"
OWNER_FILE_RELATIVE_PATH = Path("deploy") / "scheme_owner_v1.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SchemeOwnerError(RuntimeError):
    """归属登记文件缺失或不满足读取契约。"""


def load_scheme_owners(project_root: Path | None = None) -> dict[str, str]:
    """读取 composite scheme_id -> 姓名缩写 的登记映射。

    键必须自身就是 canonical composite ``scheme_id``：登记表由人工编辑，键上
    的首尾空白若被静默 trim，两个不同的合法 JSON 键会归一化成同一个 ID，归属
    按文件顺序被静默覆盖。键的歧义一律 fail-closed；姓名缩写是展示文本，无
    歧义风险，仍做 strip 以满足前端 canonical string 契约。
    """
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    path = root / OWNER_FILE_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SchemeOwnerError(
            f"scheme owner registry is unreadable: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
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
        ):
            raise SchemeOwnerError(
                "scheme owner registry key must be a canonical composite "
                f"scheme_id: {scheme_id!r}"
            )
        if not isinstance(owner, str) or not owner.strip():
            raise SchemeOwnerError(
                f"scheme owner registry has invalid owner for {scheme_id}: {owner!r}"
            )
        result[scheme_id] = owner.strip()
    return result
