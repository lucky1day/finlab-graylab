from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shared.scheme_config_schema import ALLOWED_RUNTIME_TYPES, SCHEME_ID_PATTERN


POLICY_RELATIVE_PATH = Path("deploy/onboarding_policy_v1.json")
POLICY_FIELDS = {
    "policy_version",
    "new_scheme_runtime_type",
    "native_v1_mode",
    "legacy_native_scheme_ids",
}


@dataclass(frozen=True)
class OnboardingPolicy:
    policy_version: str
    new_scheme_runtime_type: str
    native_v1_mode: str
    legacy_native_scheme_ids: tuple[str, ...]


def load_onboarding_policy(project_root: Path) -> OnboardingPolicy:
    """严格读取双版本入库政策。"""
    path = project_root / POLICY_RELATIVE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid onboarding policy {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != POLICY_FIELDS:
        raise ValueError(f"onboarding policy fields must be exactly {sorted(POLICY_FIELDS)}")
    if raw["policy_version"] != "1.0":
        raise ValueError("onboarding policy_version must be '1.0'")
    if raw["new_scheme_runtime_type"] != "blackbox_v2":
        raise ValueError("new_scheme_runtime_type must be blackbox_v2")
    if raw["native_v1_mode"] != "maintenance_only":
        raise ValueError("native_v1_mode must be maintenance_only")

    scheme_ids = raw["legacy_native_scheme_ids"]
    if not isinstance(scheme_ids, list) or not scheme_ids:
        raise ValueError("legacy_native_scheme_ids must be a non-empty list")
    if any(not isinstance(item, str) or not SCHEME_ID_PATTERN.fullmatch(item) for item in scheme_ids):
        raise ValueError("legacy_native_scheme_ids must contain valid scheme_id strings")
    if len(scheme_ids) != len(set(scheme_ids)):
        raise ValueError("legacy_native_scheme_ids must not contain duplicates")
    if scheme_ids != sorted(scheme_ids):
        raise ValueError("legacy_native_scheme_ids must be sorted")

    return OnboardingPolicy(
        policy_version="1.0",
        new_scheme_runtime_type="blackbox_v2",
        native_v1_mode="maintenance_only",
        legacy_native_scheme_ids=tuple(scheme_ids),
    )


def validate_onboarding_policy(
    project_root: Path,
    scheme_id: str,
    runtime_type: str,
) -> list[str]:
    """校验方案身份是否允许进入对应运行时的入库流程。"""
    try:
        policy = load_onboarding_policy(project_root)
    except ValueError as exc:
        return [str(exc)]
    if runtime_type not in ALLOWED_RUNTIME_TYPES:
        return [f"unsupported runtime_type in onboarding policy: {runtime_type}"]
    if runtime_type == policy.new_scheme_runtime_type:
        return []
    if scheme_id in policy.legacy_native_scheme_ids:
        return []
    return [
        "native_adapter onboarding is maintenance-only; "
        f"scheme_id is not in legacy_native_scheme_ids: {scheme_id}"
    ]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
