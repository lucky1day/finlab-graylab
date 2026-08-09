"""liwei_0616 cache 的 generation acceptance 契约。"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Mapping


GENERATION_ACCEPTANCE_SCHEMA_VERSION = (
    "liwei-0616-generation-acceptance-v1"
)
CACHE_MUTATION_POLICY_ENV = (
    "BOND_LIWEI_0616_CACHE_MUTATION_POLICY"
)
CACHE_MUTATION_POLICY_PRIVATE_BUILD = "private_build"
PHASE_A_CACHE_ABI_VERSION = "liwei_0616.phase_a.v1"
APPROVED_PHASE_A_CACHE_PUBLISHERS = MappingProxyType({
    "liwei_0616_10y_v61": (
        "10Y",
        "liwei_0616_10y01_full_oos_k3_div_k10",
    ),
    "liwei_0616_5y_v31": (
        "5Y",
        "liwei_0616_5y01_full_oos_k3_div_k10",
    ),
    "liwei_0616_5y_allk10_auc_static_v1": (
        "5Y",
        "liwei_0616_5y_auc_static_all_k3_div_k10",
    ),
    "liwei_0616_5y_allk10_auc_yearly_v1": (
        "5Y",
        "liwei_0616_5y_auc_yearly_all_k3_div_k10",
    ),
    "liwei_0616_5y_allk10_ic_yearly_v1": (
        "5Y",
        "liwei_0616_5y_ic_yearly_all_k3_div_k10",
    ),
    "liwei_0616_7y01_v31": (
        "7Y",
        "liwei_0616_7y01_cons_say_k3_div_k10",
    ),
    "liwei_0616_7y03_v31": (
        "7Y",
        "liwei_0616_7y03_cons_all_k3_div_k8",
    ),
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ACCEPTANCE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "native_generation",
        "input_content_id",
        "parent",
        "candidate_content_id",
        "build_mode",
        "input_change",
        "baselines",
        "evidence_sha256",
    }
)
_PARENT_FIELDS = frozenset(
    {
        "generation_id",
        "manifest_sha256",
        "generation_content_id",
    }
)
_LEGACY_INPUT_CHANGE_FIELDS = frozenset(
    {
        "change_type",
        "frames",
        "suffix_start_date",
        "native_generation_changed",
    }
)
_INPUT_CHANGE_FIELDS = frozenset(
    set(_LEGACY_INPUT_CHANGE_FIELDS)
    | {
        "raw_change_type",
        "effective_auxiliary",
        "date_to_week",
        "projection_status",
    }
)
_FRAME_CHANGE_FIELDS = frozenset(
    {
        "change_type",
        "earliest_changed_key",
        "schema_changed",
    }
)
_ACCEPTANCE_SCOPE_FIELDS = frozenset(
    {
        "affected_dates",
        "authoritative_scope_sha256",
        "candidate_scope_sha256",
        "preserved_dates",
        "parent_preserved_sha256",
        "candidate_preserved_sha256",
        "scope_equal",
        "preserved_equal",
    }
)
_BUILD_MODES = frozenset(
    {
        "full",
        "append",
        "suffix",
        "migration",
        "qualification",
    }
)


def canonical_json_bytes(value: object) -> bytes:
    """返回本契约固定的 canonical JSON bytes。"""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cache contract is not canonical JSON data") from exc
    return encoded.encode("utf-8")


def validate_generation_acceptance_record(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """复算 generation acceptance canonical digest 并校验全部结构。"""
    value = _mapping(raw, "generation acceptance")
    _exact_fields(
        value,
        _GENERATION_ACCEPTANCE_FIELDS,
        "generation acceptance",
    )
    if (
        value.get("schema_version")
        != GENERATION_ACCEPTANCE_SCHEMA_VERSION
    ):
        raise ValueError("generation acceptance schema_version mismatch")
    status = value.get("status")
    native = value.get("native_generation")
    if native is not None or status != "NON_PRODUCTION":
        raise ValueError(
            "generation acceptance must be unbound NON_PRODUCTION"
        )
    _sha256(value.get("input_content_id"), "input_content_id")
    _sha256(
        value.get("candidate_content_id"),
        "candidate_content_id",
    )
    parent = value.get("parent")
    if parent is not None:
        parent_mapping = _mapping(parent, "generation acceptance parent")
        _exact_fields(
            parent_mapping,
            _PARENT_FIELDS,
            "generation acceptance parent",
        )
        _text(parent_mapping.get("generation_id"), "parent.generation_id")
        _sha256(
            parent_mapping.get("manifest_sha256"),
            "parent.manifest_sha256",
        )
        _sha256(
            parent_mapping.get("generation_content_id"),
            "parent.generation_content_id",
        )
    if value.get("build_mode") not in _BUILD_MODES:
        raise ValueError("generation acceptance build_mode is invalid")
    input_change = _mapping(
        value.get("input_change"),
        "generation acceptance input_change",
    )
    input_change_fields = frozenset(input_change)
    if input_change_fields not in (
        _LEGACY_INPUT_CHANGE_FIELDS,
        _INPUT_CHANGE_FIELDS,
    ):
        raise ValueError(
            "generation acceptance input_change fields mismatch"
        )
    _text(input_change.get("change_type"), "input_change.change_type")
    if not isinstance(
        input_change.get("native_generation_changed"),
        bool,
    ):
        raise ValueError(
            "input_change.native_generation_changed must be boolean"
        )
    if input_change["native_generation_changed"]:
        raise ValueError(
            "input_change.native_generation_changed must be false"
        )
    suffix_start = input_change.get("suffix_start_date")
    if suffix_start is not None and (
        not isinstance(suffix_start, str) or not suffix_start
    ):
        raise ValueError(
            "input_change.suffix_start_date must be null or text"
        )
    frames = _mapping(
        input_change.get("frames"),
        "generation acceptance input frames",
    )
    if set(frames) != {"daily", "weekly", "monthly"}:
        raise ValueError(
            "generation acceptance input frame set mismatch"
        )
    for frame_name, frame_raw in frames.items():
        frame = _mapping(
            frame_raw,
            f"generation acceptance frame {frame_name}",
        )
        _exact_fields(
            frame,
            _FRAME_CHANGE_FIELDS,
            f"generation acceptance frame {frame_name}",
        )
        _text(
            frame.get("change_type"),
            f"{frame_name}.change_type",
        )
        earliest = frame.get("earliest_changed_key")
        if (
            earliest is not None
            and (
                isinstance(earliest, bool)
                or not isinstance(earliest, (str, int))
            )
        ):
            raise ValueError(
                f"{frame_name}.earliest_changed_key is invalid"
            )
        if not isinstance(frame.get("schema_changed"), bool):
            raise ValueError(
                f"{frame_name}.schema_changed must be boolean"
            )
    if input_change_fields == _INPUT_CHANGE_FIELDS:
        _text(
            input_change.get("raw_change_type"),
            "input_change.raw_change_type",
        )
        _text(
            input_change.get("projection_status"),
            "input_change.projection_status",
        )
        effective = _mapping(
            input_change.get("effective_auxiliary"),
            "generation acceptance effective auxiliary",
        )
        _exact_fields(
            effective,
            _FRAME_CHANGE_FIELDS,
            "generation acceptance effective auxiliary",
        )
        mapping = _mapping(
            input_change.get("date_to_week"),
            "generation acceptance date_to_week",
        )
        if set(mapping) != {"change_type", "earliest_changed_key"}:
            raise ValueError(
                "generation acceptance date_to_week fields mismatch"
            )
    baselines = _mapping(
        value.get("baselines"),
        "generation acceptance baselines",
    )
    if not baselines:
        raise ValueError(
            "generation acceptance baselines cannot be empty"
        )
    for baseline, scope_raw in baselines.items():
        _text(baseline, "generation acceptance baseline")
        scope = _mapping(
            scope_raw,
            f"generation acceptance scope {baseline}",
        )
        _exact_fields(
            scope,
            _ACCEPTANCE_SCOPE_FIELDS,
            f"generation acceptance scope {baseline}",
        )
        affected = _date_list(
            scope.get("affected_dates"),
            f"{baseline}.affected_dates",
        )
        preserved = _date_list(
            scope.get("preserved_dates"),
            f"{baseline}.preserved_dates",
        )
        if set(affected).intersection(preserved):
            raise ValueError(
                f"{baseline} affected and preserved scopes overlap"
            )
        candidate_scope = _sha256(
            scope.get("candidate_scope_sha256"),
            f"{baseline}.candidate_scope_sha256",
        )
        authoritative = scope.get("authoritative_scope_sha256")
        if affected:
            if (
                _sha256(
                    authoritative,
                    f"{baseline}.authoritative_scope_sha256",
                )
                != candidate_scope
            ):
                raise ValueError(
                    f"{baseline} cold/merged affected scope mismatch"
                )
        elif authoritative is not None:
            raise ValueError(
                f"{baseline} empty affected scope has cold hash"
            )
        parent_preserved = scope.get("parent_preserved_sha256")
        candidate_preserved = scope.get(
            "candidate_preserved_sha256"
        )
        if preserved:
            if (
                _sha256(
                    parent_preserved,
                    f"{baseline}.parent_preserved_sha256",
                )
                != _sha256(
                    candidate_preserved,
                    f"{baseline}.candidate_preserved_sha256",
                )
            ):
                raise ValueError(
                    f"{baseline} preserved-prefix hash mismatch"
                )
        elif (
            parent_preserved is not None
            or candidate_preserved is not None
        ):
            raise ValueError(
                f"{baseline} empty preserved scope has hash"
            )
        if (
            scope.get("scope_equal") is not True
            or scope.get("preserved_equal") is not True
        ):
            raise ValueError(
                f"{baseline} generation acceptance equality failed"
            )
    evidence_sha = _sha256(
        value.get("evidence_sha256"),
        "generation acceptance evidence_sha256",
    )
    payload = {
        key: item
        for key, item in value.items()
        if key != "evidence_sha256"
    }
    actual_sha = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if evidence_sha != actual_sha:
        raise ValueError(
            "generation acceptance evidence_sha256 mismatch"
        )
    return deepcopy(dict(value))


def _mapping(value: Any, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _exact_fields(
    value: Mapping[str, object],
    fields: frozenset[str],
    label: str,
) -> None:
    if set(value) != fields:
        raise ValueError(
            f"{label} fields mismatch: "
            f"missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _date_list(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        raise ValueError(
            f"{field} must be sorted unique non-empty date strings"
        )
    return list(value)
