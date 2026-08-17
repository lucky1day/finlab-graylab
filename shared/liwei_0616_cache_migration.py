"""Liwei Phase A 缓存一次性迁移重绑定契约。"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Iterator, Mapping

from shared.liwei_0616_cache_contract import canonical_json_bytes


MIGRATION_REBIND_SCHEMA_VERSION = (
    "liwei-0616-migration-rebind-v1"
)
MIGRATION_REBIND_ID = "aliyun-linux-x86_64-20260817-v1"
MIGRATION_REBIND_ENTRY_COUNT = 7

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "migration_id",
        "entries",
        "receipt_sha256",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "cache_family",
        "tenor",
        "publisher_consumer_id",
        "spec_fingerprint",
        "parent_generation_id",
        "parent_manifest_sha256",
        "parent_generation_content_id",
        "parent_input_content_id",
        "target_input_content_id",
        "baselines",
        "entry_sha256",
    }
)
_BASELINE_EVIDENCE_FIELDS = frozenset(
    {
        "cache_content_sha256",
        "field_sha256",
        "test_date_count",
        "result_config_count",
    }
)
_FIELD_SHA256_FIELDS = frozenset(
    {
        "test_dates",
        "results[].config",
        "results[].preds",
        "results[].probs",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "migration_id",
        "receipt_sha256",
        "entry",
    }
)

_ACTIVE_RECEIPT: ContextVar[dict[str, Any] | None] = ContextVar(
    "liwei_0616_active_cache_rebind_receipt",
    default=None,
)


class CacheMigrationRebindComplete(RuntimeError):
    """通知一次性 operator：当前 family 已发布并应停止方案执行。"""

    def __init__(self, audit: Mapping[str, object]) -> None:
        self.audit = deepcopy(dict(audit))
        super().__init__("cache migration rebind completed")


def validate_cache_rebind_receipt(
    raw: Mapping[str, object],
) -> dict[str, Any]:
    """校验一次性七 family receipt 及其 canonical 摘要。"""
    value = _mapping(raw, "cache rebind receipt")
    _exact_fields(value, _RECEIPT_FIELDS, "cache rebind receipt")
    if (
        value.get("schema_version")
        != MIGRATION_REBIND_SCHEMA_VERSION
    ):
        raise ValueError("cache rebind receipt schema mismatch")
    if value.get("migration_id") != MIGRATION_REBIND_ID:
        raise ValueError("cache rebind migration id mismatch")
    raw_entries = value.get("entries")
    if (
        not isinstance(raw_entries, list)
        or len(raw_entries) != MIGRATION_REBIND_ENTRY_COUNT
    ):
        raise ValueError(
            "cache rebind receipt must contain exactly seven entries"
        )
    entries = [_validate_entry(item) for item in raw_entries]
    entry_keys = [
        (entry["cache_family"], entry["tenor"])
        for entry in entries
    ]
    if len(set(entry_keys)) != len(entry_keys):
        raise ValueError("cache rebind receipt contains duplicate entries")
    if entry_keys != sorted(entry_keys):
        raise ValueError("cache rebind receipt entries are not sorted")
    receipt_sha256 = _sha256(
        value.get("receipt_sha256"),
        "receipt_sha256",
    )
    normalized: dict[str, Any] = {
        "schema_version": MIGRATION_REBIND_SCHEMA_VERSION,
        "migration_id": MIGRATION_REBIND_ID,
        "entries": entries,
        "receipt_sha256": receipt_sha256,
    }
    expected_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                key: item
                for key, item in normalized.items()
                if key != "receipt_sha256"
            }
        )
    ).hexdigest()
    if receipt_sha256 != expected_sha256:
        raise ValueError("cache rebind receipt digest mismatch")
    return normalized


def validate_cache_rebind_evidence(
    raw: Mapping[str, object],
) -> dict[str, Any]:
    """校验写入 generation manifest 的 receipt 精确绑定。"""
    value = _mapping(raw, "cache rebind evidence")
    _exact_fields(value, _EVIDENCE_FIELDS, "cache rebind evidence")
    if (
        value.get("schema_version")
        != MIGRATION_REBIND_SCHEMA_VERSION
    ):
        raise ValueError("cache rebind evidence schema mismatch")
    if value.get("migration_id") != MIGRATION_REBIND_ID:
        raise ValueError("cache rebind evidence migration id mismatch")
    return {
        "schema_version": MIGRATION_REBIND_SCHEMA_VERSION,
        "migration_id": MIGRATION_REBIND_ID,
        "receipt_sha256": _sha256(
            value.get("receipt_sha256"),
            "receipt_sha256",
        ),
        "entry": _validate_entry(value.get("entry")),
    }


@contextmanager
def authorized_cache_rebind(
    receipt: Mapping[str, object],
) -> Iterator[None]:
    """仅在当前进程上下文临时暴露精确 receipt。"""
    validated = validate_cache_rebind_receipt(receipt)
    if _ACTIVE_RECEIPT.get() is not None:
        raise RuntimeError("cache rebind authorization is already active")
    token = _ACTIVE_RECEIPT.set(validated)
    try:
        yield
    finally:
        _ACTIVE_RECEIPT.reset(token)


def active_cache_rebind_entry(
    *,
    cache_family: str,
    tenor: str,
    publisher_consumer_id: str,
) -> dict[str, Any] | None:
    """返回当前 publisher 的精确 manifest evidence；普通调用为空。"""
    receipt = _ACTIVE_RECEIPT.get()
    if receipt is None:
        return None
    for entry in receipt["entries"]:
        if (
            entry["cache_family"] == cache_family
            and entry["tenor"] == tenor
            and entry["publisher_consumer_id"]
            == publisher_consumer_id
        ):
            return {
                "schema_version": receipt["schema_version"],
                "migration_id": receipt["migration_id"],
                "receipt_sha256": receipt["receipt_sha256"],
                "entry": deepcopy(entry),
            }
    return None


def _validate_entry(raw: object) -> dict[str, Any]:
    value = _mapping(raw, "cache rebind entry")
    _exact_fields(value, _ENTRY_FIELDS, "cache rebind entry")
    normalized: dict[str, Any] = {
        "cache_family": _text(
            value.get("cache_family"),
            "cache_family",
        ),
        "tenor": _text(value.get("tenor"), "tenor"),
        "publisher_consumer_id": _text(
            value.get("publisher_consumer_id"),
            "publisher_consumer_id",
        ),
        "spec_fingerprint": _sha256(
            value.get("spec_fingerprint"),
            "spec_fingerprint",
        ),
        "parent_generation_id": _text(
            value.get("parent_generation_id"),
            "parent_generation_id",
        ),
        "parent_manifest_sha256": _sha256(
            value.get("parent_manifest_sha256"),
            "parent_manifest_sha256",
        ),
        "parent_generation_content_id": _sha256(
            value.get("parent_generation_content_id"),
            "parent_generation_content_id",
        ),
        "parent_input_content_id": _sha256(
            value.get("parent_input_content_id"),
            "parent_input_content_id",
        ),
        "target_input_content_id": _sha256(
            value.get("target_input_content_id"),
            "target_input_content_id",
        ),
        "baselines": _validate_baselines(value.get("baselines")),
        "entry_sha256": _sha256(
            value.get("entry_sha256"),
            "entry_sha256",
        ),
    }
    expected_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                key: item
                for key, item in normalized.items()
                if key != "entry_sha256"
            }
        )
    ).hexdigest()
    if normalized["entry_sha256"] != expected_sha256:
        raise ValueError("cache rebind entry digest mismatch")
    return normalized


def _validate_baselines(raw: object) -> dict[str, dict[str, Any]]:
    baselines = _mapping(raw, "cache rebind baselines")
    if not baselines:
        raise ValueError("cache rebind baselines must be non-empty")
    normalized: dict[str, dict[str, Any]] = {}
    for baseline in sorted(baselines):
        name = _text(baseline, "baseline")
        raw_evidence = _mapping(
            baselines[baseline],
            f"cache rebind baseline {name}",
        )
        _exact_fields(
            raw_evidence,
            _BASELINE_EVIDENCE_FIELDS,
            f"cache rebind baseline {name}",
        )
        raw_fields = _mapping(
            raw_evidence.get("field_sha256"),
            f"cache rebind baseline {name} fields",
        )
        _exact_fields(
            raw_fields,
            _FIELD_SHA256_FIELDS,
            f"cache rebind baseline {name} fields",
        )
        normalized[name] = {
            "cache_content_sha256": _sha256(
                raw_evidence.get("cache_content_sha256"),
                f"{name}.cache_content_sha256",
            ),
            "field_sha256": {
                field: _sha256(
                    raw_fields.get(field),
                    f"{name}.{field}",
                )
                for field in sorted(_FIELD_SHA256_FIELDS)
            },
            "test_date_count": _positive_int(
                raw_evidence.get("test_date_count"),
                f"{name}.test_date_count",
            ),
            "result_config_count": _positive_int(
                raw_evidence.get("result_config_count"),
                f"{name}.result_config_count",
            ),
        }
    return normalized


def _mapping(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return deepcopy(dict(raw))


def _exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"cache rebind {label} must be non-empty text")
    return raw


def _sha256(raw: object, label: str) -> str:
    if not isinstance(raw, str) or _SHA256.fullmatch(raw) is None:
        raise ValueError(f"cache rebind {label} must be lowercase sha256")
    return raw


def _positive_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"cache rebind {label} must be a positive integer")
    return raw
