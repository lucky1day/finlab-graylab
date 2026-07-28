"""日频单协调器的版本化、静态容量策略。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from scheduler.blackbox_scheduler_admission import (
    LEGACY_AUTOMATIC,
    BlackboxSchedulerAdmissionError,
    load_blackbox_scheduler_admission,
)
from scheduler.discovery import SchemeConfig, discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
DAILY_TASK_TYPES = {"T+1", "T+5"}
RUNTIME_TYPES = {"native_adapter", "blackbox_v2"}
INPUT_COMPATIBILITIES = {
    "generation_v1",
    "databridge_v1",
    "live_source_0629",
    "unsupported",
}
APPROVED_0629_LIVE_SOURCE_SCHEMES = frozenset(
    {
        "daily_10y_lgbm_10y04_0629",
        "daily_1y_xgb_1y13_0629",
        "daily_5y_lgbm_5y10_0629",
    }
)
EXPECTED_V2_RELEASE_OFFSETS = (0, 2, 4, 6)
APPROVED_V2_HARD_RUNTIME_SEC = 120
DEFAULT_NATIVE_HARD_RUNTIME_SEC = 600
REQUIRED_INPUT_STARTUP_RESOURCE_COMBINATION = tuple(
    sorted(("native_export", "databridge_refresh"))
)
EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME = {
    "one_y_t5_liq_excess_a_v1": 0,
    "one_y_t5_liq_excess_a_w252_l7_v1": 2,
    "one_y_t5_liq_excess_a_w350_l7_v1": 4,
    "one_y_t5_liq_excess_b_w252_l7_v1": 6,
}
EXPECTED_V1_ITEM_COUNT = 21
EXPECTED_V1_TARGET_COUNT = 25
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DailyPolicyError(ValueError):
    """日频 scheduler policy 不完整或与 discovery 漂移。"""


@dataclass(frozen=True)
class SchemeDailyPolicy:
    """一个 active daily base scheme 的不可变调度策略。"""

    scheme_id: str
    runtime_type: str
    task_type: str
    horizon: int
    target_tenors: tuple[str, ...]
    resource_class: str
    internal_workers: int
    cache_group: str
    cache_prerequisite: bool
    cache_spec_fingerprint: str | None
    estimated_cold_sec: int
    admitted_hard_runtime_sec: int
    absolute_deadline: time
    input_compatibility: str
    source_package_sha256: str | None = None
    v2_release_offset_min: int | None = None


@dataclass(frozen=True)
class DailySchedulerPolicy:
    """单 Mac 日批的版本化控制面策略。"""

    version: str
    evidence_version: str
    evidence_note: str
    timezone: str
    expected_item_count: int
    expected_target_count: int
    not_before: time
    native_capture_deadline: time
    databridge_readiness_guardrail: time
    watchdog: time
    v2_start_guardrail: time
    target_ready: time
    sla_deadline: time
    recovery_cutoff: time
    native_max_concurrency: int
    v2_max_concurrency: int
    v2_timeout_sec: int
    retry_max: int
    retry_cleanup_commit_margin_sec: int
    native_auto_scale: bool
    generation_max_count: int
    generation_max_total_bytes: int
    generation_min_free_bytes: int
    allowed_resource_combinations: frozenset[tuple[str, ...]]
    schemes: Mapping[str, SchemeDailyPolicy]


def load_daily_policy(
    path: str | Path = DEFAULT_POLICY_PATH,
    *,
    discovered: Iterable[SchemeConfig] | None = None,
) -> DailySchedulerPolicy:
    """加载 policy，并与当前 active daily discovery 做全量 fail-closed 校验。"""
    policy_path = Path(path)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DailyPolicyError(f"daily policy not found: {policy_path}") from exc
    except json.JSONDecodeError as exc:
        raise DailyPolicyError(f"daily policy is invalid JSON: {policy_path}") from exc
    if not isinstance(payload, dict):
        raise DailyPolicyError("daily policy root must be an object")

    discovered_configs = tuple(discovered if discovered is not None else discover_schemes())
    discovered_active_daily = tuple(
        config
        for config in discovered_configs
        if config.status == "active" and config.frequency == "daily"
    )
    try:
        scheduler_admission = load_blackbox_scheduler_admission()
    except BlackboxSchedulerAdmissionError as exc:
        raise DailyPolicyError(
            "Blackbox scheduler admission is invalid; daily policy "
            "cannot select the formal occurrence set"
        ) from exc
    active_daily = tuple(
        config
        for config in discovered_active_daily
        if scheduler_admission.allows(
            config,
            plane=LEGACY_AUTOMATIC,
        )
    )
    discovered_by_id = _index_discovered(active_daily)
    scheme_rows = _require_list(payload, "schemes")
    if any(not isinstance(row, dict) for row in scheme_rows):
        raise DailyPolicyError("daily policy scheme entries must be objects")
    row_ids = [_required_text(row, "scheme_id") for row in scheme_rows]
    duplicate_ids = sorted(
        scheme_id for scheme_id in set(row_ids) if row_ids.count(scheme_id) > 1
    )
    if duplicate_ids:
        raise DailyPolicyError(
            f"daily policy contains duplicate schemes: {duplicate_ids}"
        )
    policy_ids = set(row_ids)
    discovered_ids = set(discovered_by_id)
    unknown = sorted(policy_ids - discovered_ids)
    if unknown:
        raise DailyPolicyError(f"daily policy contains unknown schemes: {unknown}")
    missing = sorted(discovered_ids - policy_ids)
    if missing:
        raise DailyPolicyError(f"daily policy is missing active daily schemes: {missing}")

    times = _require_mapping(payload, "times")
    pools = _require_mapping(payload, "pools")
    retry = _require_mapping(payload, "retry")
    generation_storage = _require_mapping(
        payload,
        "generation_storage",
    )
    resource_governor = _require_mapping(payload, "resource_governor")
    parsed_schemes = {
        row["scheme_id"]: _parse_scheme_policy(
            row,
            discovered_by_id[row["scheme_id"]],
        )
        for row in scheme_rows
    }
    policy = DailySchedulerPolicy(
        version=_required_text(payload, "version"),
        evidence_version=_required_text(payload, "evidence_version"),
        evidence_note=_required_text(payload, "evidence_note"),
        timezone=_required_text(payload, "timezone"),
        expected_item_count=_positive_int(payload, "expected_item_count"),
        expected_target_count=_positive_int(payload, "expected_target_count"),
        not_before=_parse_time(times, "not_before"),
        native_capture_deadline=_parse_time(
            times,
            "native_capture_deadline",
        ),
        databridge_readiness_guardrail=_parse_time(
            times,
            "databridge_readiness_guardrail",
        ),
        watchdog=_parse_time(times, "watchdog"),
        v2_start_guardrail=_parse_time(times, "v2_start_guardrail"),
        target_ready=_parse_time(times, "target_ready"),
        sla_deadline=_parse_time(times, "sla_deadline"),
        recovery_cutoff=_parse_time(times, "recovery_cutoff"),
        native_max_concurrency=_positive_int(
            pools,
            "native_max_concurrency",
        ),
        v2_max_concurrency=_positive_int(pools, "v2_max_concurrency"),
        v2_timeout_sec=_positive_int(pools, "v2_timeout_sec"),
        retry_max=_positive_int(retry, "max_retries"),
        retry_cleanup_commit_margin_sec=_positive_int(
            retry,
            "cleanup_commit_margin_sec",
        ),
        native_auto_scale=_required_bool(pools, "native_auto_scale"),
        generation_max_count=_positive_int(
            generation_storage,
            "max_generation_count",
        ),
        generation_max_total_bytes=_positive_int(
            generation_storage,
            "max_total_bytes",
        ),
        generation_min_free_bytes=_positive_int(
            generation_storage,
            "min_free_bytes",
        ),
        allowed_resource_combinations=_parse_resource_combinations(
            resource_governor,
        ),
        schemes=MappingProxyType(parsed_schemes),
    )
    _validate_global_policy(policy, active_daily)
    return policy


def _index_discovered(
    configs: Iterable[SchemeConfig],
) -> dict[str, SchemeConfig]:
    indexed: dict[str, SchemeConfig] = {}
    for config in configs:
        if config.scheme_id in indexed:
            raise DailyPolicyError(
                f"discovery contains duplicate active daily scheme: {config.scheme_id}"
            )
        if config.task_type not in DAILY_TASK_TYPES:
            raise DailyPolicyError(
                f"active daily scheme has illegal task_type: "
                f"{config.scheme_id}={config.task_type}"
            )
        indexed[config.scheme_id] = config
    if not indexed:
        raise DailyPolicyError("active daily discovery is empty")
    return indexed


def _parse_scheme_policy(
    row: object,
    config: SchemeConfig,
) -> SchemeDailyPolicy:
    if not isinstance(row, dict):
        raise DailyPolicyError("daily policy scheme entries must be objects")
    scheme_id = _required_text(row, "scheme_id")
    runtime_type = _required_text(row, "runtime_type")
    task_type = _required_text(row, "task_type")
    if runtime_type not in RUNTIME_TYPES:
        raise DailyPolicyError(
            f"{scheme_id}: unsupported runtime_type={runtime_type}"
        )
    if task_type not in DAILY_TASK_TYPES:
        raise DailyPolicyError(f"{scheme_id}: illegal task_type={task_type}")
    if runtime_type != config.runtime_type:
        raise DailyPolicyError(
            f"{scheme_id}: runtime_type mismatch policy={runtime_type} "
            f"discovery={config.runtime_type}"
        )
    if task_type != config.task_type:
        raise DailyPolicyError(
            f"{scheme_id}: task_type mismatch policy={task_type} "
            f"discovery={config.task_type}"
        )
    horizon = _positive_int(row, "horizon")
    if horizon != config.horizon:
        raise DailyPolicyError(
            f"{scheme_id}: horizon mismatch policy={horizon} "
            f"discovery={config.horizon}"
        )
    target_tenors = tuple(_required_string_list(row, "target_tenors"))
    if target_tenors != tuple(config.tenors):
        raise DailyPolicyError(
            f"{scheme_id}: target_tenors mismatch policy={target_tenors} "
            f"discovery={tuple(config.tenors)}"
        )
    input_compatibility = _required_text(row, "input_compatibility")
    if input_compatibility not in INPUT_COMPATIBILITIES:
        raise DailyPolicyError(
            f"{scheme_id}: unsupported input_compatibility={input_compatibility}"
        )
    release_offset = row.get("v2_release_offset_min")
    if release_offset is not None:
        if isinstance(release_offset, bool) or not isinstance(release_offset, int):
            raise DailyPolicyError(
                f"{scheme_id}: v2_release_offset_min must be an integer"
            )
        if release_offset < 0:
            raise DailyPolicyError(
                f"{scheme_id}: v2_release_offset_min must be non-negative"
            )
    admitted_hard_runtime_sec = _positive_int(
        row,
        "admitted_hard_runtime_sec",
    )
    required_hard_runtime_sec = _required_hard_runtime_sec(config)
    if admitted_hard_runtime_sec != required_hard_runtime_sec:
        raise DailyPolicyError(
            f"{scheme_id}: admitted_hard_runtime_sec must equal the "
            "enforced scheme timeout: "
            f"policy={admitted_hard_runtime_sec} "
            f"required={required_hard_runtime_sec}"
        )
    return SchemeDailyPolicy(
        scheme_id=scheme_id,
        runtime_type=runtime_type,
        task_type=task_type,
        horizon=horizon,
        target_tenors=target_tenors,
        resource_class=_required_text(row, "resource_class"),
        internal_workers=_positive_int(row, "internal_workers"),
        cache_group=_required_text(row, "cache_group"),
        cache_prerequisite=_required_bool(row, "cache_prerequisite"),
        cache_spec_fingerprint=_optional_sha256(
            row,
            "cache_spec_fingerprint",
        ),
        estimated_cold_sec=_positive_int(row, "estimated_cold_sec"),
        admitted_hard_runtime_sec=admitted_hard_runtime_sec,
        absolute_deadline=_parse_time(row, "absolute_deadline"),
        input_compatibility=input_compatibility,
        source_package_sha256=_optional_sha256(
            row,
            "source_package_sha256",
        ),
        v2_release_offset_min=release_offset,
    )


def _required_hard_runtime_sec(config: SchemeConfig) -> int:
    """返回执行器真实强制的静态 timeout，不读取运行期统计。"""
    if config.runtime_type == "blackbox_v2":
        return APPROVED_V2_HARD_RUNTIME_SEC
    configured = config.schedule.timeout_sec
    return (
        int(configured)
        if configured is not None
        else DEFAULT_NATIVE_HARD_RUNTIME_SEC
    )


def _validate_global_policy(
    policy: DailySchedulerPolicy,
    discovered: tuple[SchemeConfig, ...],
) -> None:
    if policy.version != "daily-scheduler-policy-v1":
        raise DailyPolicyError(f"unsupported daily policy version: {policy.version}")
    if policy.timezone != "Asia/Shanghai":
        raise DailyPolicyError("daily policy timezone must be Asia/Shanghai")
    approved_times = {
        "not_before": (policy.not_before, "06:30"),
        "native_capture_deadline": (
            policy.native_capture_deadline,
            "08:30",
        ),
        "databridge_readiness_guardrail": (
            policy.databridge_readiness_guardrail,
            "06:55",
        ),
        "watchdog": (policy.watchdog, "07:00"),
        "v2_start_guardrail": (policy.v2_start_guardrail, "07:45"),
        "target_ready": (policy.target_ready, "07:55"),
        "sla_deadline": (policy.sla_deadline, "08:00"),
        "recovery_cutoff": (policy.recovery_cutoff, "08:30"),
    }
    for field, (actual, expected) in approved_times.items():
        if actual.strftime("%H:%M") != expected:
            raise DailyPolicyError(
                f"{field} must be {expected}, got {actual.strftime('%H:%M')}"
            )
    if not (
        policy.not_before
        < policy.databridge_readiness_guardrail
        < policy.watchdog
        < policy.target_ready
        < policy.sla_deadline
        < policy.recovery_cutoff
        == policy.native_capture_deadline
    ):
        raise DailyPolicyError(
            "time order must be not_before < "
            "databridge_readiness_guardrail < watchdog < target_ready < "
            "sla_deadline < recovery_cutoff == native_capture_deadline"
        )
    if policy.native_max_concurrency != 2:
        raise DailyPolicyError(
            "native_max_concurrency must remain fixed at 2; auto-3 is forbidden"
        )
    if policy.native_auto_scale:
        raise DailyPolicyError("native_auto_scale must be false")
    if policy.v2_max_concurrency != 2:
        raise DailyPolicyError("v2_max_concurrency must be 2")
    if policy.v2_timeout_sec != APPROVED_V2_HARD_RUNTIME_SEC:
        raise DailyPolicyError("v2_timeout_sec must be 120")
    if policy.retry_max != 1:
        raise DailyPolicyError("retry max_retries must be 1")
    if (
        policy.expected_item_count != EXPECTED_V1_ITEM_COUNT
        or policy.expected_target_count != EXPECTED_V1_TARGET_COUNT
    ):
        raise DailyPolicyError(
            "daily-scheduler-policy-v1 cardinality must remain exactly "
            f"{EXPECTED_V1_ITEM_COUNT} items/{EXPECTED_V1_TARGET_COUNT} targets; "
            "a changed active set requires a new capacity-admitted policy version"
        )

    actual_items = len(discovered)
    actual_targets = sum(len(config.tenors) for config in discovered)
    if policy.expected_item_count != actual_items:
        raise DailyPolicyError(
            f"expected_item_count mismatch policy={policy.expected_item_count} "
            f"discovery={actual_items}"
        )
    if policy.expected_target_count != actual_targets:
        raise DailyPolicyError(
            f"expected_target_count mismatch policy={policy.expected_target_count} "
            f"discovery={actual_targets}"
        )

    unsupported = {
        item.scheme_id
        for item in policy.schemes.values()
        if item.input_compatibility == "unsupported"
    }
    if unsupported:
        raise DailyPolicyError(
            "daily policy cannot retain unsupported Native inputs: "
            f"schemes: {sorted(unsupported)}"
        )
    live_source = {
        item.scheme_id
        for item in policy.schemes.values()
        if item.input_compatibility == "live_source_0629"
    }
    if live_source != APPROVED_0629_LIVE_SOURCE_SCHEMES:
        raise DailyPolicyError(
            "live source compatibility must be limited to the approved "
            "0629 schemes: "
            f"{sorted(live_source)}"
        )
    for item in policy.schemes.values():
        if item.runtime_type == "blackbox_v2":
            if item.input_compatibility != "databridge_v1":
                raise DailyPolicyError(
                    f"{item.scheme_id}: Blackbox V2 requires databridge_v1"
                )
            if item.v2_release_offset_min is None:
                raise DailyPolicyError(
                    f"{item.scheme_id}: Blackbox V2 requires a release offset"
                )
        else:
            if item.v2_release_offset_min is not None:
                raise DailyPolicyError(
                    f"{item.scheme_id}: Native cannot declare a V2 release offset"
                )
            expected_native_input = (
                "live_source_0629"
                if (
                    item.scheme_id
                    in APPROVED_0629_LIVE_SOURCE_SCHEMES
                )
                else "generation_v1"
            )
            if item.input_compatibility != expected_native_input:
                raise DailyPolicyError(
                    f"{item.scheme_id}: Native input compatibility must be "
                    f"{expected_native_input}"
                )
        if item.input_compatibility == "live_source_0629":
            if item.source_package_sha256 is None:
                raise DailyPolicyError(
                    f"{item.scheme_id}: live source compatibility requires "
                    "source_package_sha256"
                )
        elif item.source_package_sha256 is not None:
            raise DailyPolicyError(
                f"{item.scheme_id}: source_package_sha256 is only valid "
                "for live_source_0629"
            )
        if item.absolute_deadline > policy.target_ready:
            raise DailyPolicyError(
                f"{item.scheme_id}: absolute_deadline exceeds target_ready"
            )
    v2_offsets = tuple(
        sorted(
            item.v2_release_offset_min
            for item in policy.schemes.values()
            if item.runtime_type == "blackbox_v2"
            and item.v2_release_offset_min is not None
        )
    )
    if v2_offsets != EXPECTED_V2_RELEASE_OFFSETS:
        raise DailyPolicyError(
            "Blackbox V2 release offsets must be exactly "
            f"{EXPECTED_V2_RELEASE_OFFSETS}, got {v2_offsets}"
        )
    actual_v2_mapping = {
        item.scheme_id: item.v2_release_offset_min
        for item in policy.schemes.values()
        if item.runtime_type == "blackbox_v2"
    }
    if actual_v2_mapping != EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME:
        raise DailyPolicyError(
            "Blackbox V2 release offset mapping must be exactly "
            f"{EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME}, "
            f"got {actual_v2_mapping}"
        )
    supported_classes = {
        item.resource_class
        for item in policy.schemes.values()
        if item.input_compatibility != "unsupported"
    }
    declared_classes = {
        resource_class
        for combination in policy.allowed_resource_combinations
        for resource_class in combination
    }
    missing_classes = supported_classes - declared_classes
    if missing_classes:
        raise DailyPolicyError(
            f"resource governor is missing declared classes: {sorted(missing_classes)}"
        )
    for required_signature in (
        ("native_export",),
        ("databridge_refresh",),
        ("databridge_pack",),
        REQUIRED_INPUT_STARTUP_RESOURCE_COMBINATION,
    ):
        if (
            tuple(sorted(required_signature))
            not in policy.allowed_resource_combinations
        ):
            raise DailyPolicyError(
                "resource governor is missing mandatory input startup "
                f"combination: {tuple(sorted(required_signature))}"
            )
    native_groups: dict[str, list[SchemeDailyPolicy]] = {}
    for item in policy.schemes.values():
        if item.runtime_type != "native_adapter":
            continue
        native_groups.setdefault(item.cache_group, []).append(item)
        if (
            item.cache_group.startswith("liwei_0616_")
            and item.cache_spec_fingerprint is None
        ):
            raise DailyPolicyError(
                f"{item.scheme_id}: liwei cache spec fingerprint is required"
            )
    for cache_group, members in native_groups.items():
        if len(members) < 2:
            continue
        prerequisite_count = sum(
            item.cache_prerequisite for item in members
        )
        if prerequisite_count != 1:
            raise DailyPolicyError(
                f"{cache_group}: shared Native cache group requires "
                "exactly one cache prerequisite"
            )
        fingerprints = {
            item.cache_spec_fingerprint for item in members
        }
        if None in fingerprints or len(fingerprints) != 1:
            raise DailyPolicyError(
                f"{cache_group}: shared Native cache spec fingerprint "
                "mismatch"
            )


def _parse_resource_combinations(
    payload: Mapping[str, object],
) -> frozenset[tuple[str, ...]]:
    rows = _require_list(payload, "allowed_combinations")
    combinations: set[tuple[str, ...]] = set()
    for row in rows:
        if not isinstance(row, list) or not row:
            raise DailyPolicyError(
                "resource allowed_combinations entries must be non-empty arrays"
            )
        values = []
        for value in row:
            if not isinstance(value, str) or not value.strip():
                raise DailyPolicyError(
                    "resource combination classes must be non-empty strings"
                )
            values.append(value.strip())
        signature = tuple(sorted(values))
        if signature in combinations:
            raise DailyPolicyError(
                f"duplicate resource combination: {signature}"
            )
        combinations.add(signature)
    return frozenset(combinations)


def _require_mapping(
    payload: Mapping[str, object],
    field: str,
) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise DailyPolicyError(f"{field} must be an object")
    return value


def _require_list(
    payload: Mapping[str, object],
    field: str,
) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise DailyPolicyError(f"{field} must be an array")
    return value


def _required_string_list(
    payload: Mapping[str, object],
    field: str,
) -> list[str]:
    values = _require_list(payload, field)
    if not values:
        raise DailyPolicyError(f"{field} must be a non-empty array")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise DailyPolicyError(f"{field} values must be non-empty strings")
        normalized.append(value.strip())
    if len(set(normalized)) != len(normalized):
        raise DailyPolicyError(f"{field} values must be unique")
    return normalized


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DailyPolicyError(f"{field} must be a non-empty string")
    return value.strip()


def _required_bool(payload: Mapping[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise DailyPolicyError(f"{field} must be a boolean")
    return value


def _optional_sha256(
    payload: Mapping[str, object],
    field: str,
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DailyPolicyError(
            f"{field} must be null or a lowercase SHA-256"
        )
    return value


def _positive_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DailyPolicyError(f"{field} must be a positive integer")
    return value


def _parse_time(payload: Mapping[str, object], field: str) -> time:
    value = _required_text(payload, field)
    if len(value) != 5 or value[2] != ":":
        raise DailyPolicyError(f"{field} must use HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise DailyPolicyError(f"{field} must use HH:MM") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise DailyPolicyError(f"{field} must use minute precision")
    return parsed
