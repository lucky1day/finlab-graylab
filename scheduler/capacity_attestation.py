"""容量证据的精确候选与可信采集契约。

本模块只验证结构和候选绑定。CMS 来源认证由
``scheduler.capacity_admission`` 在 runtime admission 边界完成。
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from scheduler.capacity_gate import (
    EXPECTED_TARGET_COUNT,
    EXPECTED_V2_SCHEME_IDS,
    OBSERVATION_SCHEMA_VERSION,
)
from shared.liwei_0616_cache_contract import (
    PHASE_A_CACHE_ABI_VERSION,
    validate_cache_use_qualification,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    NATIVE_GENERATION_SCHEMA_VERSION,
)


ATTESTED_EVIDENCE_SCHEMA_VERSION = (
    "daily-capacity-attested-evidence-v2"
)
CANDIDATE_SCHEMA_VERSION = "daily-capacity-candidate-v3"
ARTIFACT_SET_SCHEMA_VERSION = "daily-capacity-artifact-set-v1"
EXPECTED_ITEM_COUNT = 21
COMPONENT_DIGEST_FIELDS = (
    "scheduler_release_sha256",
    "scheme_bundle_sha256",
    "service_environment_sha256",
    "algorithm_environment_sha256",
    "runtime_profile_sha256",
    "migration_set_sha256",
    "native_exporter_sha256",
    "databridge_exporter_sha256",
    "database_identity_sha256",
    "control_plane_identity_sha256",
)
_COMPONENT_DIGEST_FIELD_SET = frozenset(COMPONENT_DIGEST_FIELDS)
REQUIRED_FAULT_OUTCOMES = MappingProxyType(
    {
        "child_running_kill": "RECOVERED_SINGLE_WINNER",
        "post_result_kill": "RECOVERED_SINGLE_WINNER",
        "pre_commit_kill": "RECOVERED_SINGLE_WINNER",
        "post_commit_pre_ack_kill": "COMMIT_RECOGNIZED_NO_REEXECUTE",
        "timeout": "TERMINAL_TIMEOUT_NO_RETRY",
        "disk_full": "FAIL_CLOSED_NO_PARTIAL_PUBLISH",
        "databridge_delay": "WAITED_FOR_SAME_DAY_GENERATION",
        "registry_drift": "FROZEN_REGISTRY_REJECTED_DRIFT",
        "generation_tamper": "HASH_MISMATCH_FAIL_CLOSED",
        "late_source_write": "DATA_CONTRACT_BREACH_FROZEN",
    }
)
REQUIRED_FAULT_SCENARIOS = frozenset(REQUIRED_FAULT_OUTCOMES)
_ATTESTED_FIELDS = frozenset(
    {
        "schema_version",
        "candidate",
        "candidate_fingerprint",
        "observations",
        "fault_trials",
        "cache_use_qualifications",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "machine_id",
        "hardware_model",
        "os_build",
        "memory_bytes",
        "policy_version",
        "policy_sha256",
        "registry_manifest_sha256",
        "expected_item_count",
        "expected_target_count",
        "target_manifest",
        "scheduler_release_sha256",
        "scheme_bundle_sha256",
        "service_environment_sha256",
        "algorithm_environment_sha256",
        "runtime_profile_sha256",
        "migration_set_sha256",
        "native_exporter_sha256",
        "databridge_exporter_sha256",
        "database_identity_sha256",
        "control_plane_identity_sha256",
    }
)
_TARGET_FIELDS = frozenset(
    {
        "registry_scheme_id",
        "base_scheme_id",
        "runtime_type",
        "target_tenor",
        "horizon",
        "task_type",
        "scheme_version",
        "code_sha256",
        "config_sha256",
        "cache_group",
        "cache_spec_fingerprint",
        "cache_adapter_sha256",
        "cache_core_sha256",
    }
)
_FAULT_FIELDS = frozenset(
    {
        "trial_id",
        "scenario",
        "business_date",
        "candidate_fingerprint",
        "run_id",
        "expected_outcome_code",
        "observed_outcome_code",
        "status",
        "evidence",
    }
)
_ARTIFACT_FIELDS = frozenset({"artifact_id", "sha256"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CapacityAttestationError(ValueError):
    """attested capacity evidence 无法形成可信候选绑定。"""


@dataclass(frozen=True)
class ValidatedAttestedCapacityEvidence:
    """经过严格结构与候选绑定校验的 evidence envelope。"""

    candidate: Mapping[str, object]
    candidate_fingerprint: str
    observations: Mapping[str, object]
    fault_trials: tuple[Mapping[str, object], ...]
    target_ids: frozenset[str]
    cache_use_qualifications: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class ValidatedCapacityCandidate:
    """可由 runtime 重算并与签名候选精确比较的 canonical candidate。"""

    payload: Mapping[str, object]
    fingerprint: str
    target_ids: frozenset[str]


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
        raise CapacityAttestationError(
            "capacity attestation is not canonical JSON data"
        ) from exc
    return encoded.encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """计算 canonical JSON 的 SHA-256。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_sha256(content: bytes) -> str:
    """计算原始 artifact bytes；不做换行、Unicode 或格式归一化。"""

    if not isinstance(content, bytes):
        raise CapacityAttestationError(
            "capacity artifact content must be bytes"
        )
    return hashlib.sha256(content).hexdigest()


def canonical_artifact_set_sha256(
    namespace: str,
    artifacts: Mapping[str, str],
) -> str:
    """计算可跨进程重算的 component/file-set digest。

    ``artifacts`` 的 key 是稳定 POSIX logical identity（例如仓库相对路径、
    conda explicit export 名或数据库身份字段名），value 必须是该 artifact
    原始 bytes 的 SHA-256。输入顺序不参与结果；identity、内容增删及内容变化
    都会改变 digest。
    """

    normalized_namespace = _logical_identity(
        namespace,
        "artifact set namespace",
    )
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise CapacityAttestationError(
            "capacity artifact set must be a non-empty object"
        )
    rows: list[dict[str, str]] = []
    for raw_identity, raw_sha256 in artifacts.items():
        identity = _logical_identity(
            raw_identity,
            "artifact logical identity",
        )
        rows.append(
            {
                "identity": identity,
                "sha256": _sha256_text(
                    raw_sha256,
                    f"artifact {identity}.sha256",
                ),
            }
        )
    rows.sort(key=lambda row: row["identity"])
    return canonical_json_sha256(
        {
            "schema_version": ARTIFACT_SET_SCHEMA_VERSION,
            "namespace": normalized_namespace,
            "artifacts": rows,
        }
    )


def build_capacity_target(
    *,
    registry_scheme_id: str,
    base_scheme_id: str,
    runtime_type: str,
    target_tenor: str,
    horizon: int,
    task_type: str,
    scheme_version: str,
    code_artifacts: Mapping[str, str],
    config_artifacts: Mapping[str, str],
    cache_group: str,
    cache_spec_fingerprint: str | None,
    cache_adapter_sha256: str | None,
    cache_core_sha256: str | None,
) -> dict[str, object]:
    """按统一 file-set 语义构造一条真实 Registry target。

    code/config artifact identity 应使用相对项目根的稳定 POSIX 路径，摘要由
    :func:`content_sha256` 对实际文件 bytes 生成。
    """

    row: dict[str, object] = {
        "registry_scheme_id": registry_scheme_id,
        "base_scheme_id": base_scheme_id,
        "runtime_type": runtime_type,
        "target_tenor": target_tenor,
        "horizon": horizon,
        "task_type": task_type,
        "scheme_version": scheme_version,
        "code_sha256": canonical_artifact_set_sha256(
            f"scheme-code/{base_scheme_id}/{scheme_version}",
            code_artifacts,
        ),
        "config_sha256": canonical_artifact_set_sha256(
            f"scheme-config/{base_scheme_id}/{scheme_version}",
            config_artifacts,
        ),
        "cache_group": cache_group,
        "cache_spec_fingerprint": cache_spec_fingerprint,
        "cache_adapter_sha256": cache_adapter_sha256,
        "cache_core_sha256": cache_core_sha256,
    }
    return _validate_target(row, 0)


def build_capacity_candidate(
    *,
    machine_id: str,
    hardware_model: str,
    os_build: str,
    memory_bytes: int,
    policy_version: str,
    policy_bytes: bytes,
    target_manifest: Sequence[Mapping[str, object]],
    component_artifacts: Mapping[str, Mapping[str, str]],
) -> ValidatedCapacityCandidate:
    """用同一 canonical 规则构造 offline 与 runtime 候选。

    digest 字段语义固定如下：

    * ``policy_sha256``：policy 文件原始 bytes；
    * ``registry_manifest_sha256``：排序后的 25 条 target canonical JSON；
    * target ``code/config_sha256``：应由 :func:`build_capacity_target`
      对各自文件集生成；
    * 其余十个 ``*_sha256``：由同名 namespace 下的 logical
      identity → raw-content-SHA manifest 生成。典型 identity 分别是
      release 文件、scheme bundle 文件、两套 conda explicit exports、
      runtime profile、migration 文件、两个 exporter 文件和稳定 DB
      identity 字段。
    """

    if not isinstance(policy_bytes, bytes):
        raise CapacityAttestationError(
            "capacity policy content must be bytes"
        )
    if not isinstance(component_artifacts, Mapping):
        raise CapacityAttestationError(
            "capacity component_artifacts must be an object"
        )
    actual_fields = set(component_artifacts)
    if actual_fields != _COMPONENT_DIGEST_FIELD_SET:
        raise CapacityAttestationError(
            "capacity component_artifacts fields mismatch: "
            f"missing={sorted(_COMPONENT_DIGEST_FIELD_SET - actual_fields)} "
            f"unknown={sorted(actual_fields - _COMPONENT_DIGEST_FIELD_SET)}"
        )
    targets = [dict(row) for row in target_manifest]
    candidate: dict[str, object] = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "machine_id": machine_id,
        "hardware_model": hardware_model,
        "os_build": os_build,
        "memory_bytes": memory_bytes,
        "policy_version": policy_version,
        "policy_sha256": content_sha256(policy_bytes),
        "registry_manifest_sha256": canonical_json_sha256(targets),
        "expected_item_count": EXPECTED_ITEM_COUNT,
        "expected_target_count": EXPECTED_TARGET_COUNT,
        "target_manifest": targets,
    }
    for field in COMPONENT_DIGEST_FIELDS:
        candidate[field] = canonical_artifact_set_sha256(
            field,
            component_artifacts[field],
        )
    return validate_capacity_candidate(candidate)


def candidate_fingerprint(candidate: Mapping[str, object]) -> str:
    """严格验证 candidate 后计算内容寻址 fingerprint。"""
    normalized, _ = _validate_candidate(candidate)
    return canonical_json_sha256(normalized)


def validate_capacity_candidate(
    candidate: Mapping[str, object],
) -> ValidatedCapacityCandidate:
    """解析候选并返回独立 payload、fingerprint 与 target IDs。"""

    normalized, target_ids = _validate_candidate(candidate)
    return ValidatedCapacityCandidate(
        payload=deepcopy(normalized),
        fingerprint=canonical_json_sha256(normalized),
        target_ids=target_ids,
    )


def verify_current_candidate(
    signed_candidate: Mapping[str, object],
    current_candidate: (
        Mapping[str, object]
        | Callable[[], Mapping[str, object]]
    ),
) -> ValidatedCapacityCandidate:
    """把签名候选与 runtime 当下重算候选做 exact fingerprint 比较。

    ``current_candidate`` 可以是 mapping，也可以是由协调器注入的零参数
    callback。本函数不读取 DB、不发现 scheme，避免反向依赖 repository；
    调用方必须在 discovery/Registry freeze 前用同一 candidate builder 重算。
    """

    signed = validate_capacity_candidate(signed_candidate)
    try:
        current_raw = (
            current_candidate()
            if callable(current_candidate)
            else current_candidate
        )
    except Exception as exc:
        raise CapacityAttestationError(
            "current capacity candidate provider failed"
        ) from exc
    if not isinstance(current_raw, Mapping):
        raise CapacityAttestationError(
            "current capacity candidate provider must return an object"
        )
    current = validate_capacity_candidate(current_raw)
    if canonical_json_bytes(current.payload) != canonical_json_bytes(
        signed.payload
    ):
        raise CapacityAttestationError(
            "current capacity candidate fingerprint differs "
            "from signed candidate"
        )
    return current


def validate_attested_capacity_evidence(
    payload: Mapping[str, object],
    *,
    expected_machine_id: str | None = None,
    expected_policy_version: str | None = None,
    expected_policy_sha256: str | None = None,
) -> ValidatedAttestedCapacityEvidence:
    """验证 attested envelope 的精确 candidate、target 和 fault 绑定。"""
    root = _mapping(payload, "root")
    _require_exact_fields(root, _ATTESTED_FIELDS, "root")
    if root.get("schema_version") != ATTESTED_EVIDENCE_SCHEMA_VERSION:
        raise CapacityAttestationError(
            "attested evidence schema_version is invalid"
        )

    candidate, target_ids = _validate_candidate(
        _mapping(root.get("candidate"), "root.candidate")
    )
    fingerprint = _sha256_text(
        root.get("candidate_fingerprint"),
        "root.candidate_fingerprint",
    )
    actual_fingerprint = canonical_json_sha256(candidate)
    if fingerprint != actual_fingerprint:
        raise CapacityAttestationError(
            "root.candidate_fingerprint does not match candidate"
        )

    machine_id = _text(candidate, "machine_id", "root.candidate")
    policy_version = _text(
        candidate,
        "policy_version",
        "root.candidate",
    )
    policy_sha256 = _sha256_text(
        candidate.get("policy_sha256"),
        "root.candidate.policy_sha256",
    )
    if expected_machine_id is not None and machine_id != expected_machine_id:
        raise CapacityAttestationError(
            "candidate machine_id does not match expected machine"
        )
    if (
        expected_policy_version is not None
        and policy_version != expected_policy_version
    ):
        raise CapacityAttestationError(
            "candidate policy_version does not match expected policy"
        )
    if (
        expected_policy_sha256 is not None
        and policy_sha256 != expected_policy_sha256
    ):
        raise CapacityAttestationError(
            "candidate policy_sha256 does not match expected policy"
        )

    observations = _mapping(
        root.get("observations"),
        "root.observations",
    )
    if observations.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise CapacityAttestationError(
            "root.observations must use the offline observation schema"
        )
    if observations.get("machine_id") != machine_id:
        raise CapacityAttestationError(
            "observations machine_id differs from candidate"
        )
    if observations.get("policy_version") != policy_version:
        raise CapacityAttestationError(
            "observations policy_version differs from candidate"
        )
    _validate_observation_bindings(
        observations,
        candidate_fingerprint_value=fingerprint,
        target_ids=target_ids,
    )

    fault_trials = _validate_fault_trials(
        root.get("fault_trials"),
        candidate_fingerprint_value=fingerprint,
    )
    cache_use_qualifications = _validate_cache_use_qualifications(
        root.get("cache_use_qualifications"),
        candidate=candidate,
        candidate_fingerprint_value=fingerprint,
        observations=observations,
    )
    return ValidatedAttestedCapacityEvidence(
        candidate=deepcopy(candidate),
        candidate_fingerprint=fingerprint,
        observations=deepcopy(dict(observations)),
        fault_trials=tuple(deepcopy(row) for row in fault_trials),
        target_ids=target_ids,
        cache_use_qualifications=deepcopy(cache_use_qualifications),
    )


def _validate_candidate(
    raw: Mapping[str, object],
) -> tuple[dict[str, object], frozenset[str]]:
    candidate = dict(_mapping(raw, "candidate"))
    _require_exact_fields(candidate, _CANDIDATE_FIELDS, "candidate")
    if candidate.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise CapacityAttestationError(
            "candidate schema_version is invalid"
        )
    for field in (
        "machine_id",
        "hardware_model",
        "os_build",
        "policy_version",
    ):
        _text(candidate, field, "candidate")
    for field in (
        "policy_sha256",
        "registry_manifest_sha256",
        "scheduler_release_sha256",
        "scheme_bundle_sha256",
        "service_environment_sha256",
        "algorithm_environment_sha256",
        "runtime_profile_sha256",
        "migration_set_sha256",
        "native_exporter_sha256",
        "databridge_exporter_sha256",
        "database_identity_sha256",
        "control_plane_identity_sha256",
    ):
        _sha256_text(candidate.get(field), f"candidate.{field}")
    _positive_int(
        candidate.get("memory_bytes"),
        "candidate.memory_bytes",
    )

    expected_items = _positive_int(
        candidate.get("expected_item_count"),
        "candidate.expected_item_count",
    )
    expected_targets = _positive_int(
        candidate.get("expected_target_count"),
        "candidate.expected_target_count",
    )
    if (
        expected_items != EXPECTED_ITEM_COUNT
        or expected_targets != EXPECTED_TARGET_COUNT
    ):
        raise CapacityAttestationError(
            "candidate cardinality must be exactly 21 items / 25 targets"
        )

    raw_targets = candidate.get("target_manifest")
    if not isinstance(raw_targets, list):
        raise CapacityAttestationError(
            "candidate.target_manifest must be an array"
        )
    targets = [
        _validate_target(row, index)
        for index, row in enumerate(raw_targets)
    ]
    registry_ids = [str(row["registry_scheme_id"]) for row in targets]
    if len(registry_ids) != len(set(registry_ids)):
        raise CapacityAttestationError(
            "candidate target manifest contains duplicate registry IDs"
        )
    if len(registry_ids) != expected_targets:
        raise CapacityAttestationError(
            "candidate target manifest target count mismatch"
        )
    base_ids = {str(row["base_scheme_id"]) for row in targets}
    if len(base_ids) != expected_items:
        raise CapacityAttestationError(
            "candidate target manifest item count mismatch"
        )
    if registry_ids != sorted(registry_ids):
        raise CapacityAttestationError(
            "candidate target manifest must use canonical registry order"
        )
    v2_ids = {
        str(row["base_scheme_id"])
        for row in targets
        if row["runtime_type"] == "blackbox_v2"
    }
    if v2_ids != EXPECTED_V2_SCHEME_IDS:
        raise CapacityAttestationError(
            "candidate target manifest Blackbox V2 set mismatch"
        )
    _validate_base_metadata_consistency(targets)

    actual_manifest_sha256 = canonical_json_sha256(targets)
    if candidate["registry_manifest_sha256"] != actual_manifest_sha256:
        raise CapacityAttestationError(
            "candidate registry_manifest_sha256 mismatch"
        )
    candidate["target_manifest"] = targets
    return candidate, frozenset(registry_ids)


def _validate_target(raw: object, index: int) -> dict[str, object]:
    path = f"candidate.target_manifest[{index}]"
    row = dict(_mapping(raw, path))
    _require_exact_fields(row, _TARGET_FIELDS, path)
    registry_id = _text(row, "registry_scheme_id", path)
    base_id = _text(row, "base_scheme_id", path)
    runtime_type = _text(row, "runtime_type", path)
    if runtime_type not in {"native_adapter", "blackbox_v2"}:
        raise CapacityAttestationError(
            f"{path}.runtime_type is invalid"
        )
    tenor = _text(row, "target_tenor", path)
    horizon = _positive_int(row.get("horizon"), f"{path}.horizon")
    task_type = _text(row, "task_type", path)
    if task_type not in {"T+1", "T+5"}:
        raise CapacityAttestationError(f"{path}.task_type is invalid")
    if registry_id != f"{base_id}__h{horizon}__{tenor}":
        raise CapacityAttestationError(
            f"{path}.registry_scheme_id is not a composite target identity"
        )
    _text(row, "scheme_version", path)
    _sha256_text(row.get("code_sha256"), f"{path}.code_sha256")
    _sha256_text(row.get("config_sha256"), f"{path}.config_sha256")
    _text(row, "cache_group", path)
    cache_fields = (
        "cache_spec_fingerprint",
        "cache_adapter_sha256",
        "cache_core_sha256",
    )
    cache_values = tuple(row.get(field) for field in cache_fields)
    if all(value is None for value in cache_values):
        pass
    elif any(value is None for value in cache_values):
        raise CapacityAttestationError(
            f"{path} cache qualification identity is partial"
        )
    else:
        for field in cache_fields:
            _sha256_text(row.get(field), f"{path}.{field}")
        if runtime_type != "native_adapter":
            raise CapacityAttestationError(
                f"{path} only Native targets may declare cache qualification"
            )
    return row


def _validate_base_metadata_consistency(
    targets: Sequence[Mapping[str, object]],
) -> None:
    seen: dict[str, tuple[object, ...]] = {}
    for row in targets:
        base_id = str(row["base_scheme_id"])
        identity = (
            row["runtime_type"],
            row["horizon"],
            row["task_type"],
            row["scheme_version"],
            row["code_sha256"],
            row["config_sha256"],
            row["cache_group"],
            row["cache_spec_fingerprint"],
            row["cache_adapter_sha256"],
            row["cache_core_sha256"],
        )
        previous = seen.setdefault(base_id, identity)
        if previous != identity:
            raise CapacityAttestationError(
                "candidate target manifest has inconsistent base metadata"
            )


def _validate_cache_use_qualifications(
    raw: object,
    *,
    candidate: Mapping[str, object],
    candidate_fingerprint_value: str,
    observations: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    """校验签名 evidence 中逐 consumer qualification 的 exact set。"""
    if not isinstance(raw, list):
        raise CapacityAttestationError(
            "cache_use_qualifications must be an array"
        )
    targets = candidate["target_manifest"]
    target_by_base: dict[str, Mapping[str, object]] = {}
    for target in targets:
        if target["cache_spec_fingerprint"] is None:
            continue
        target_by_base.setdefault(str(target["base_scheme_id"]), target)
    expected_ids = set(target_by_base)
    expected_forced_ids = sorted(
        str(row["batch_id"])
        for row in observations["daily_batches"]
        if row.get("observation_kind") == "forced_cold"
    )
    expected_revision_ids = sorted(
        str(row["trial_id"])
        for row in observations["revision_trials"]
    )
    validated_by_base: dict[str, Mapping[str, object]] = {}
    actual_order: list[str] = []
    for index, qualification_raw in enumerate(raw):
        path = f"cache_use_qualifications[{index}]"
        if not isinstance(qualification_raw, Mapping):
            raise CapacityAttestationError(f"{path} must be an object")
        try:
            qualification = validate_cache_use_qualification(
                qualification_raw,
                expected_candidate_fingerprint=(
                    candidate_fingerprint_value
                ),
            )
        except ValueError as exc:
            raise CapacityAttestationError(
                f"{path} is invalid: {exc}"
            ) from exc
        base_id = str(qualification["base_scheme_id"])
        if base_id in validated_by_base:
            raise CapacityAttestationError(
                "cache use qualification contains duplicate consumer"
            )
        target = target_by_base.get(base_id)
        if target is None:
            raise CapacityAttestationError(
                "cache use qualification consumer set mismatch"
            )
        expected = {
            "scheme_version": target["scheme_version"],
            "code_sha256": target["code_sha256"],
            "config_sha256": target["config_sha256"],
            "cache_group": target["cache_group"],
            "spec_fingerprint": target["cache_spec_fingerprint"],
            "cache_adapter_sha256": target["cache_adapter_sha256"],
            "cache_core_sha256": target["cache_core_sha256"],
            "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
            "algorithm_environment_sha256":
                candidate["algorithm_environment_sha256"],
            "native_exporter_sha256":
                candidate["native_exporter_sha256"],
            "native_generation_schema_version":
                NATIVE_GENERATION_SCHEMA_VERSION,
            "native_exporter_version":
                NATIVE_GENERATION_EXPORTER_VERSION,
        }
        drift = sorted(
            field
            for field, expected_value in expected.items()
            if qualification.get(field) != expected_value
        )
        if drift:
            raise CapacityAttestationError(
                f"{path} candidate binding drift: " + ",".join(drift)
            )
        if (
            qualification["daily_dependency_lookback_rows"] is not None
            and "proven_suffix"
            not in qualification["corpus"]["coverage_types"]
        ):
            raise CapacityAttestationError(
                f"{path} suffix proof has no proven_suffix corpus"
            )
        corpus = qualification["corpus"]
        if (
            corpus["forced_cold_trial_ids"] != expected_forced_ids
            or corpus["revision_trial_ids"] != expected_revision_ids
            or corpus["forced_cold_count"] != len(expected_forced_ids)
            or corpus["revision_count"] != len(expected_revision_ids)
        ):
            raise CapacityAttestationError(
                f"{path} corpus IDs do not match signed observations"
            )
        validated_by_base[base_id] = qualification
        actual_order.append(base_id)
    if set(validated_by_base) != expected_ids:
        missing = sorted(expected_ids - set(validated_by_base))
        unknown = sorted(set(validated_by_base) - expected_ids)
        raise CapacityAttestationError(
            "cache use qualification consumer set mismatch: "
            f"missing={missing} unknown={unknown}"
        )
    if actual_order != sorted(actual_order):
        raise CapacityAttestationError(
            "cache use qualifications must use canonical consumer order"
        )
    return {
        base_id: deepcopy(validated_by_base[base_id])
        for base_id in sorted(validated_by_base)
    }


def _validate_observation_bindings(
    observations: Mapping[str, object],
    *,
    candidate_fingerprint_value: str,
    target_ids: frozenset[str],
) -> None:
    batches = observations.get("daily_batches")
    revisions = observations.get("revision_trials")
    if not isinstance(batches, list) or not isinstance(revisions, list):
        raise CapacityAttestationError(
            "observations daily_batches/revision_trials must be arrays"
        )
    for index, raw in enumerate(batches):
        path = f"observations.daily_batches[{index}]"
        row = _mapping(raw, path)
        if row.get("candidate_fingerprint") != (
            candidate_fingerprint_value
        ):
            raise CapacityAttestationError(
                f"{path}.candidate_fingerprint mismatch"
            )
        targets = row.get("targets")
        if not isinstance(targets, list):
            raise CapacityAttestationError(f"{path}.targets must be an array")
        actual_ids: list[str] = []
        for target_index, target_raw in enumerate(targets):
            target = _mapping(
                target_raw,
                f"{path}.targets[{target_index}]",
            )
            actual_ids.append(
                _text(
                    target,
                    "target_id",
                    f"{path}.targets[{target_index}]",
                )
            )
        if (
            len(actual_ids) != len(target_ids)
            or len(actual_ids) != len(set(actual_ids))
            or set(actual_ids) != target_ids
        ):
            raise CapacityAttestationError(
                f"{path} target manifest does not match candidate"
            )
    for index, raw in enumerate(revisions):
        path = f"observations.revision_trials[{index}]"
        row = _mapping(raw, path)
        if row.get("candidate_fingerprint") != (
            candidate_fingerprint_value
        ):
            raise CapacityAttestationError(
                f"{path}.candidate_fingerprint mismatch"
            )


def _validate_fault_trials(
    raw: object,
    *,
    candidate_fingerprint_value: str,
) -> tuple[dict[str, object], ...]:
    if not isinstance(raw, list):
        raise CapacityAttestationError(
            "root.fault_trials must be an array"
        )
    rows: list[dict[str, object]] = []
    scenarios: list[str] = []
    unique_values: dict[str, set[str]] = {
        "trial_id": set(),
        "run_id": set(),
        "artifact_id": set(),
        "artifact_sha256": set(),
    }
    for index, value in enumerate(raw):
        path = f"root.fault_trials[{index}]"
        row = dict(_mapping(value, path))
        _require_exact_fields(row, _FAULT_FIELDS, path)
        trial_id = _text(row, "trial_id", path)
        scenario = _text(row, "scenario", path)
        run_id = _text(row, "run_id", path)
        if row.get("candidate_fingerprint") != (
            candidate_fingerprint_value
        ):
            raise CapacityAttestationError(
                f"{path}.candidate_fingerprint mismatch"
            )
        _parse_date(row.get("business_date"), f"{path}.business_date")
        expected = _text(row, "expected_outcome_code", path)
        observed = _text(row, "observed_outcome_code", path)
        required_outcome = REQUIRED_FAULT_OUTCOMES.get(scenario)
        if required_outcome is None or expected != required_outcome:
            raise CapacityAttestationError(
                f"{path} does not declare the required expected outcome"
            )
        if row.get("status") != "PASS" or observed != required_outcome:
            raise CapacityAttestationError(
                f"{path} fault trial did not pass its expected outcome"
            )
        artifact = dict(_mapping(row.get("evidence"), f"{path}.evidence"))
        _require_exact_fields(
            artifact,
            _ARTIFACT_FIELDS,
            f"{path}.evidence",
        )
        artifact_id = _text(artifact, "artifact_id", f"{path}.evidence")
        artifact_sha = _sha256_text(
            artifact.get("sha256"),
            f"{path}.evidence.sha256",
        )
        values = {
            "trial_id": trial_id,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha,
        }
        for field, field_value in values.items():
            if field_value in unique_values[field]:
                raise CapacityAttestationError(
                    f"fault trial reuses {field}"
                )
            unique_values[field].add(field_value)
        row["evidence"] = artifact
        scenarios.append(scenario)
        rows.append(row)
    if (
        len(scenarios) != len(REQUIRED_FAULT_SCENARIOS)
        or len(scenarios) != len(set(scenarios))
        or set(scenarios) != REQUIRED_FAULT_SCENARIOS
    ):
        raise CapacityAttestationError(
            "fault scenario set must contain every required scenario once"
        )
    return tuple(rows)


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CapacityAttestationError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise CapacityAttestationError(f"{path} keys must be strings")
    return value


def _require_exact_fields(
    row: Mapping[str, object],
    expected: frozenset[str],
    path: str,
) -> None:
    actual = set(row)
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        raise CapacityAttestationError(
            f"{path} fields mismatch: "
            f"missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _text(
    row: Mapping[str, object],
    field: str,
    path: str,
) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapacityAttestationError(
            f"{path}.{field} must be non-empty text"
        )
    return value.strip()


def _sha256_text(value: object, path: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapacityAttestationError(
            f"{path} must be canonical lowercase SHA-256"
        )
    return value


def _logical_identity(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapacityAttestationError(
            f"{path} must be canonical non-empty text"
        )
    if "\\" in value:
        raise CapacityAttestationError(
            f"{path} must use POSIX separators"
        )
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or str(candidate) != value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise CapacityAttestationError(
            f"{path} must be a normalized relative logical identity"
        )
    return value


def _positive_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityAttestationError(
            f"{path} must be a positive integer"
        )
    return value


def _parse_date(value: object, path: str) -> date:
    if not isinstance(value, str):
        raise CapacityAttestationError(f"{path} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CapacityAttestationError(
            f"{path} must be an ISO date"
        ) from exc
    if parsed.isoformat() != value:
        raise CapacityAttestationError(
            f"{path} must use canonical YYYY-MM-DD"
        )
    return parsed
