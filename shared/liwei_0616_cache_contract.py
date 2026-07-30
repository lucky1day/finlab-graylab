"""liwei_0616 cache 的运行 authority 与 generation acceptance 契约。

本模块只做纯结构、身份与摘要校验。legacy qualification 仍只接受旧
capacity evidence envelope；日频 ledger 使用 occurrence 冻结的 direct
runtime context。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Mapping


CACHE_USE_QUALIFICATION_SCHEMA_VERSION = (
    "liwei-0616-cache-use-qualification-v1"
)
TRUSTED_CACHE_USE_QUALIFICATION_SCHEMA_VERSION = (
    "liwei-0616-trusted-cache-use-qualification-v1"
)
GENERATION_ACCEPTANCE_SCHEMA_VERSION = (
    "liwei-0616-generation-acceptance-v1"
)
CACHE_USE_QUALIFICATION_ENV = (
    "BOND_LIWEI_0616_CACHE_USE_QUALIFICATION"
)
DIRECT_CACHE_RUNTIME_CONTEXT_ENV = (
    "BOND_LIWEI_0616_DIRECT_CACHE_RUNTIME_CONTEXT"
)
CACHE_MUTATION_POLICY_ENV = (
    "BOND_LIWEI_0616_CACHE_MUTATION_POLICY"
)
CACHE_MUTATION_POLICY_HIT_ONLY = "hit_only"
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
QUALIFICATION_CORPUS_SCHEMA_VERSION = (
    "liwei-0616-cache-qualification-corpus-v1"
)
MIN_FORCED_COLD_QUALIFICATION_SAMPLES = 1
MIN_REVISION_QUALIFICATION_SAMPLES = 0
QUALIFICATION_COMPARISON_FIELDS = (
    "direction",
    "vote_score",
    "baseline_score",
    "baseline_sign",
    "probability",
    "confidence",
    "internal_fields",
    "phase_a_cache",
)
QUALIFICATION_COVERAGE_TYPES = frozenset(
    {
        "forced_cold",
        "append",
        "proven_suffix",
        "full_rebuild",
    }
)
_REQUIRED_COVERAGE_TYPES = frozenset(
    {
        "forced_cold",
        "append",
        "full_rebuild",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUALIFICATION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "source",
        "qualification_id",
        "base_scheme_id",
        "scheme_version",
        "code_sha256",
        "config_sha256",
        "cache_group",
        "cache_family",
        "tenor",
        "cache_adapter_sha256",
        "cache_core_sha256",
        "spec_fingerprint",
        "cache_abi_version",
        "algorithm_environment_sha256",
        "capacity_candidate_fingerprint",
        "native_exporter_sha256",
        "native_generation_schema_version",
        "native_exporter_version",
        "daily_dependency_lookback_rows",
        "daily_dependency_proof",
        "comparison_fields",
        "comparison_evidence_sha256",
        "corpus",
    }
)
_TRUSTED_FIELDS = frozenset(
    {
        "schema_version",
        "qualification",
        "qualification_sha256",
        "admission_decision_id",
        "admission_evidence_sha256",
        "collector_signer_sha256",
        "operator_signer_sha256",
        "candidate_fingerprint",
    }
)
_CORPUS_FIELDS = frozenset(
    {
        "forced_cold_count",
        "revision_count",
        "coverage_types",
        "forced_cold_trial_ids",
        "revision_trial_ids",
        "evidence_sha256",
    }
)
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
_NATIVE_BINDING_FIELDS = frozenset(
    {
        "generation_id",
        "manifest_sha256",
        "dataset_content_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
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
        "rebind",
        "migration",
        "qualification",
    }
)
_DIRECT_RUNTIME_FIELDS = frozenset(
    {"schema_version", "storage_root", "contract", "consumer"}
)
_DIRECT_RUNTIME_CONTRACT = {
    "manifest_schema_version": 3,
    "input_state_schema_version": 3,
    "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
    "projection_schema_version":
        "liwei-0616-auxiliary-dependency-projection-v1",
    "native_generation_type": "native_source",
    "native_generation_schema_version": "native-generation-v1",
    "native_exporter_version": "native-generation-exporter-v1",
}
_DIRECT_RUNTIME_CONSUMER_FIELDS = frozenset(
    {
        "base_scheme_id",
        "cache_consumer_id",
        "scheme_version",
        "code_sha256",
        "config_sha256",
        "cache_group",
        "spec_fingerprint",
        "publisher_consumer_id",
        "cache_family",
        "tenor",
        "access_mode",
        "cache_adapter_sha256",
        "cache_core_sha256",
        "publisher_projection_proof_identity_sha256",
        "daily_dependency_lookback_rows",
        "daily_dependency_proof",
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


def validate_direct_cache_runtime_context(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """校验 occurrence 传给单个 Liwei consumer 的 direct authority。"""
    value = _mapping(raw, "direct cache runtime context")
    _exact_fields(
        value,
        _DIRECT_RUNTIME_FIELDS,
        "direct cache runtime context",
    )
    if (
        value.get("schema_version")
        != "liwei-0616-direct-cache-runtime-context-v1"
    ):
        raise ValueError(
            "direct cache runtime context schema_version mismatch"
        )
    storage_root = value.get("storage_root")
    if (
        not isinstance(storage_root, str)
        or not storage_root
        or not storage_root.startswith("/")
        or os.path.normpath(storage_root) != storage_root
        or "//" in storage_root
        or "/../" in f"{storage_root}/"
        or storage_root.endswith("/")
    ):
        raise ValueError(
            "direct cache runtime context storage_root is invalid"
        )
    contract = _mapping(
        value.get("contract"),
        "direct cache runtime contract",
    )
    if dict(contract) != _DIRECT_RUNTIME_CONTRACT:
        raise ValueError("direct cache runtime contract mismatch")
    consumer = _mapping(
        value.get("consumer"),
        "direct cache runtime consumer",
    )
    _exact_fields(
        consumer,
        _DIRECT_RUNTIME_CONSUMER_FIELDS,
        "direct cache runtime consumer",
    )
    normalized_consumer = deepcopy(dict(consumer))
    for field in (
        "base_scheme_id",
        "cache_consumer_id",
        "scheme_version",
        "cache_group",
        "publisher_consumer_id",
        "cache_family",
        "tenor",
    ):
        normalized_consumer[field] = _text(
            normalized_consumer.get(field),
            f"direct cache runtime consumer {field}",
        )
    for field in (
        "code_sha256",
        "config_sha256",
        "spec_fingerprint",
        "cache_adapter_sha256",
        "cache_core_sha256",
        "publisher_projection_proof_identity_sha256",
    ):
        normalized_consumer[field] = _sha256(
            normalized_consumer.get(field),
            f"direct cache runtime consumer {field}",
        )
    lookback = normalized_consumer[
        "daily_dependency_lookback_rows"
    ]
    proof = normalized_consumer["daily_dependency_proof"]
    if lookback is None and proof is None:
        pass
    elif (
        isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or lookback < 0
        or not isinstance(proof, str)
        or not proof.strip()
    ):
        raise ValueError(
            "direct cache runtime consumer dependency proof is invalid"
        )
    consumer_id = normalized_consumer["cache_consumer_id"]
    publisher_id = normalized_consumer["publisher_consumer_id"]
    expected_access = (
        "publisher" if consumer_id == publisher_id else "read_only"
    )
    if (
        normalized_consumer["base_scheme_id"] != consumer_id
        or normalized_consumer["access_mode"] != expected_access
        or normalized_consumer["cache_group"]
        != (
            f"{normalized_consumer['cache_family']}:"
            f"{normalized_consumer['tenor']}"
        )
    ):
        raise ValueError(
            "direct cache runtime consumer identity is invalid"
        )
    return {
        "schema_version": value["schema_version"],
        "storage_root": storage_root,
        "contract": dict(contract),
        "consumer": normalized_consumer,
    }


def cache_use_qualification_sha256(
    raw: Mapping[str, object],
) -> str:
    """严格校验并计算逐 consumer qualification 摘要。"""
    normalized = validate_cache_use_qualification(raw)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def qualification_corpus_sha256(
    *,
    forced_cold_trial_ids: list[str],
    revision_trial_ids: list[str],
    coverage_types: list[str],
) -> str:
    """绑定 qualification 真实 corpus ID 列表与覆盖类型。"""
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version":
                    QUALIFICATION_CORPUS_SCHEMA_VERSION,
                "forced_cold_trial_ids": forced_cold_trial_ids,
                "revision_trial_ids": revision_trial_ids,
                "coverage_types": coverage_types,
            }
        )
    ).hexdigest()


def validate_cache_use_qualification(
    raw: Mapping[str, object],
    *,
    expected_base_scheme_id: str | None = None,
    expected_candidate_fingerprint: str | None = None,
) -> dict[str, object]:
    """校验离线 corpus 形成的逐 consumer production qualification。"""
    value = _mapping(raw, "cache use qualification")
    _exact_fields(value, _QUALIFICATION_FIELDS, "cache use qualification")
    if (
        value.get("schema_version")
        != CACHE_USE_QUALIFICATION_SCHEMA_VERSION
    ):
        raise ValueError("cache use qualification schema_version mismatch")
    if value.get("status") != "PASSED":
        raise ValueError("cache use qualification status must be PASSED")
    if value.get("source") != "signed_capacity_corpus":
        raise ValueError(
            "cache use qualification must come from signed capacity corpus"
        )
    for field in (
        "qualification_id",
        "base_scheme_id",
        "scheme_version",
        "cache_group",
        "cache_family",
        "tenor",
        "cache_abi_version",
        "native_generation_schema_version",
        "native_exporter_version",
    ):
        _text(value.get(field), field)
    for field in (
        "code_sha256",
        "config_sha256",
        "cache_adapter_sha256",
        "cache_core_sha256",
        "spec_fingerprint",
        "algorithm_environment_sha256",
        "capacity_candidate_fingerprint",
        "native_exporter_sha256",
        "comparison_evidence_sha256",
    ):
        _sha256(value.get(field), field)
    if (
        expected_base_scheme_id is not None
        and value["base_scheme_id"] != expected_base_scheme_id
    ):
        raise ValueError(
            "cache use qualification base_scheme_id mismatch"
        )
    if (
        expected_candidate_fingerprint is not None
        and value["capacity_candidate_fingerprint"]
        != _sha256(
            expected_candidate_fingerprint,
            "expected_candidate_fingerprint",
        )
    ):
        raise ValueError(
            "cache use qualification candidate fingerprint mismatch"
        )
    expected_group = f"{value['cache_family']}:{value['tenor']}"
    if value["cache_group"] != expected_group:
        raise ValueError(
            "cache use qualification cache_group/family/tenor mismatch"
        )
    rows = value.get("daily_dependency_lookback_rows")
    proof = value.get("daily_dependency_proof")
    if rows is None and proof is None:
        pass
    elif (
        isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or not isinstance(proof, str)
        or not proof.strip()
    ):
        raise ValueError(
            "cache use qualification dependency proof is incomplete"
        )
    if value.get("comparison_fields") != list(
        QUALIFICATION_COMPARISON_FIELDS
    ):
        raise ValueError(
            "cache use qualification comparison_fields are incomplete"
        )
    corpus = _mapping(value.get("corpus"), "qualification corpus")
    _exact_fields(corpus, _CORPUS_FIELDS, "qualification corpus")
    forced_count = _nonnegative_int(
        corpus.get("forced_cold_count"),
        "forced_cold_count",
    )
    revision_count = _nonnegative_int(
        corpus.get("revision_count"),
        "revision_count",
    )
    if forced_count < MIN_FORCED_COLD_QUALIFICATION_SAMPLES:
        raise ValueError(
            "cache qualification forced-cold corpus is below 1"
        )
    if revision_count < MIN_REVISION_QUALIFICATION_SAMPLES:
        raise ValueError(
            "cache qualification revision corpus is below 0"
        )
    coverage = corpus.get("coverage_types")
    if (
        not isinstance(coverage, list)
        or any(
            not isinstance(item, str) or not item
            for item in coverage
        )
        or len(set(coverage)) != len(coverage)
    ):
        raise ValueError(
            "cache qualification coverage_types are invalid"
        )
    coverage_set = frozenset(coverage)
    if (
        not _REQUIRED_COVERAGE_TYPES.issubset(coverage_set)
        or not coverage_set.issubset(QUALIFICATION_COVERAGE_TYPES)
    ):
        raise ValueError(
            "cache qualification corpus coverage types are incomplete"
        )
    if (
        rows is not None
        and "proven_suffix" not in coverage_set
    ):
        raise ValueError(
            "cache qualification suffix proof requires proven_suffix corpus"
        )
    forced_ids = _canonical_trial_ids(
        corpus.get("forced_cold_trial_ids"),
        "corpus.forced_cold_trial_ids",
    )
    revision_ids = _canonical_trial_ids(
        corpus.get("revision_trial_ids"),
        "corpus.revision_trial_ids",
    )
    if len(forced_ids) != forced_count:
        raise ValueError(
            "cache qualification forced-cold corpus count mismatch"
        )
    if len(revision_ids) != revision_count:
        raise ValueError(
            "cache qualification revision corpus count mismatch"
        )
    evidence_sha = _sha256(
        corpus.get("evidence_sha256"),
        "corpus.evidence_sha256",
    )
    expected_evidence_sha = qualification_corpus_sha256(
        forced_cold_trial_ids=forced_ids,
        revision_trial_ids=revision_ids,
        coverage_types=coverage,
    )
    if evidence_sha != expected_evidence_sha:
        raise ValueError(
            "cache qualification corpus evidence_sha256 mismatch"
        )
    return deepcopy(dict(value))


def validate_trusted_cache_use_qualification(
    raw: Mapping[str, object],
    *,
    expected_base_scheme_id: str | None = None,
    expected_candidate_fingerprint: str | None = None,
) -> dict[str, object]:
    """校验 capacity admission 交给 occurrence/子进程的可信 envelope。"""
    value = _mapping(raw, "trusted cache use qualification")
    _exact_fields(value, _TRUSTED_FIELDS, "trusted cache use qualification")
    if (
        value.get("schema_version")
        != TRUSTED_CACHE_USE_QUALIFICATION_SCHEMA_VERSION
    ):
        raise ValueError(
            "trusted cache use qualification schema_version mismatch"
        )
    candidate = _sha256(
        value.get("candidate_fingerprint"),
        "candidate_fingerprint",
    )
    if (
        expected_candidate_fingerprint is not None
        and candidate
        != _sha256(
            expected_candidate_fingerprint,
            "expected_candidate_fingerprint",
        )
    ):
        raise ValueError(
            "trusted cache use qualification candidate mismatch"
        )
    qualification = validate_cache_use_qualification(
        _mapping(value.get("qualification"), "qualification"),
        expected_base_scheme_id=expected_base_scheme_id,
        expected_candidate_fingerprint=candidate,
    )
    actual_sha = hashlib.sha256(
        canonical_json_bytes(qualification)
    ).hexdigest()
    if value.get("qualification_sha256") != actual_sha:
        raise ValueError(
            "trusted cache use qualification qualification_sha256 mismatch"
        )
    for field in ("admission_decision_id",):
        _text(value.get(field), field)
    for field in (
        "admission_evidence_sha256",
        "collector_signer_sha256",
        "operator_signer_sha256",
    ):
        _sha256(value.get(field), field)
    if (
        value["collector_signer_sha256"]
        == value["operator_signer_sha256"]
    ):
        raise ValueError(
            "collector and operator qualification signers must differ"
        )
    return deepcopy(dict(value))


def trusted_qualification_audit_binding(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """提取 PredictionRecord.extra 必须逐字段回报的冻结身份。"""
    trusted = validate_trusted_cache_use_qualification(raw)
    qualification = trusted["qualification"]
    return {
        "base_scheme_id": qualification["base_scheme_id"],
        "qualification_id": qualification["qualification_id"],
        "qualification_sha256": trusted["qualification_sha256"],
        "admission_decision_id": trusted["admission_decision_id"],
        "admission_evidence_sha256":
            trusted["admission_evidence_sha256"],
        "candidate_fingerprint": trusted["candidate_fingerprint"],
    }


def validate_generation_acceptance_record(
    raw: Mapping[str, object],
    *,
    expected_native_generation: Mapping[str, object] | None = None,
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
    if native is None:
        if status != "NON_PRODUCTION":
            raise ValueError(
                "unbound generation acceptance must be NON_PRODUCTION"
            )
    else:
        if status != "ACCEPTED":
            raise ValueError(
                "bound generation acceptance must be ACCEPTED"
            )
        _validate_native_binding(
            _mapping(native, "generation acceptance native_generation")
        )
    if expected_native_generation is not None:
        expected_native = _validate_native_binding(
            _mapping(
                expected_native_generation,
                "expected Native generation",
            )
        )
        if native != expected_native:
            raise ValueError(
                "generation acceptance Native identity mismatch"
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


def validate_prediction_cache_audit(
    extra: Mapping[str, object] | None,
    *,
    expected_qualification: Mapping[str, object],
    expected_native_generation: Mapping[str, object],
) -> None:
    """核对算法回报的 cache audit 与 occurrence 冻结资格/输入。"""
    trusted = validate_trusted_cache_use_qualification(
        expected_qualification
    )
    root = _mapping(extra, "PredictionRecord.extra")
    audit = _mapping(root.get("phase_a_cache"), "phase_a_cache audit")
    if audit.get("capacity_eligible") is not True:
        raise ValueError("phase_a_cache audit is not capacity eligible")
    reported = _mapping(
        audit.get("cache_use_qualification"),
        "reported cache use qualification",
    )
    expected = trusted_qualification_audit_binding(trusted)
    _exact_fields(
        reported,
        frozenset(expected),
        "reported cache use qualification",
    )
    for field, expected_value in expected.items():
        if reported.get(field) != expected_value:
            raise ValueError(
                f"phase_a_cache audit {field} mismatch"
            )
    acceptance = _mapping(
        audit.get("generation_acceptance"),
        "generation acceptance audit",
    )
    validate_generation_acceptance_record(
        acceptance,
        expected_native_generation=expected_native_generation,
    )


def _mapping(value: Any, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _canonical_trial_ids(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            for item in value
        )
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be sorted unique text")
    return list(value)


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


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _validate_native_binding(
    value: Mapping[str, object],
) -> dict[str, object]:
    _exact_fields(
        value,
        _NATIVE_BINDING_FIELDS,
        "Native generation binding",
    )
    for field in (
        "generation_id",
        "business_date",
        "feature_date",
        "schema_version",
        "exporter_version",
    ):
        _text(value.get(field), field)
    for field in ("manifest_sha256", "dataset_content_id"):
        _sha256(value.get(field), field)
    return deepcopy(dict(value))


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
