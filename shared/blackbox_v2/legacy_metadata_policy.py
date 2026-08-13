"""方案 A 生效前 Blackbox V2 不可变 Metadata 的显式兼容范围。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from shared.scheme_config_schema import SCHEME_ID_PATTERN


LEGACY_METADATA_SCHEMA_VERSION = "blackbox-v2-legacy-metadata-v1"
LEGACY_METADATA_POLICY_RELATIVE_PATH = (
    Path("deploy") / "blackbox_v2_legacy_metadata_v1.json"
)


class LegacyMetadataPolicyError(RuntimeError):
    """历史 Metadata 兼容清单缺失或不合法。"""


def load_legacy_metadata_hashes(project_root: Path) -> dict[str, str]:
    """严格读取允许缺少 owner 的历史 Metadata 精确 SHA-256。"""
    path = Path(project_root) / LEGACY_METADATA_POLICY_RELATIVE_PATH
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except OSError as exc:
        raise LegacyMetadataPolicyError(
            f"legacy metadata policy is unreadable: {path}"
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise LegacyMetadataPolicyError(
            f"legacy metadata policy is invalid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "metadata_sha256",
    }:
        raise LegacyMetadataPolicyError(
            "legacy metadata policy must contain only schema_version and metadata_sha256"
        )
    if payload["schema_version"] != LEGACY_METADATA_SCHEMA_VERSION:
        raise LegacyMetadataPolicyError(
            "legacy metadata policy schema_version must be "
            f"{LEGACY_METADATA_SCHEMA_VERSION!r}"
        )
    metadata_hashes = payload["metadata_sha256"]
    if not isinstance(metadata_hashes, dict):
        raise LegacyMetadataPolicyError(
            "legacy metadata policy metadata_sha256 must be an object"
        )
    if list(metadata_hashes) != sorted(metadata_hashes):
        raise LegacyMetadataPolicyError(
            "legacy metadata policy scheme IDs must be sorted"
        )
    for scheme_id, metadata_hash in metadata_hashes.items():
        if not isinstance(scheme_id, str) or not SCHEME_ID_PATTERN.fullmatch(
            scheme_id
        ):
            raise LegacyMetadataPolicyError(
                f"legacy metadata policy has invalid scheme_id: {scheme_id!r}"
            )
        if not isinstance(metadata_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", metadata_hash
        ):
            raise LegacyMetadataPolicyError(
                "legacy metadata policy has invalid SHA-256 for "
                f"{scheme_id!r}"
            )
    return dict(metadata_hashes)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
