"""日批 occurrence 台账的纯领域模型与状态投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping


GENERATION_BUILDING = "BUILDING"
GENERATION_SEALED = "SEALED"
GENERATION_INVALIDATED = "INVALIDATED"

OCCURRENCE_PENDING = "PENDING"
OCCURRENCE_RUNNING = "RUNNING"
OCCURRENCE_SUCCESS = "SUCCESS"
OCCURRENCE_FAILED = "FAILED"

ITEM_PENDING = "PENDING"
ITEM_RUNNING = "RUNNING"
ITEM_RETRY_WAIT = "RETRY_WAIT"
ITEM_SUCCESS = "SUCCESS"
ITEM_FAILED_TERMINAL = "FAILED_TERMINAL"
ITEM_ABANDONED = "ABANDONED"
ITEM_EXPIRED = "EXPIRED"

TARGET_PENDING = "PENDING"
TARGET_ACCEPTED = "ACCEPTED"

SLA_PENDING = "PENDING"
SLA_MET = "MET"
SLA_BREACHED = "BREACHED"

ITEM_SLA_PENDING = "PENDING"
ITEM_SLA_ON_TIME = "ON_TIME"
ITEM_SLA_LATE = "LATE"
GUARDRAIL_PENDING = ITEM_SLA_PENDING
GUARDRAIL_MET = ITEM_SLA_ON_TIME
GUARDRAIL_BREACHED = ITEM_SLA_LATE
GUARDRAIL_NOT_APPLICABLE = "NOT_APPLICABLE"

# 日批 failure_code 的唯一稳定词表。数据库 CHECK、协调器、执行器与仓储
# 都必须从这里导入，禁止在各层另建同义别名。
FAILURE_TRANSIENT_INFRA = "TRANSIENT_INFRA"
FAILURE_TIMEOUT = "TIMEOUT"
FAILURE_DATA = "DATA"
FAILURE_CONTRACT = "CONTRACT"
FAILURE_ALGORITHM = "ALGORITHM"
FAILURE_RESULT = "RESULT"
FAILURE_NATIVE_GENERATION_UNSUPPORTED = "NATIVE_GENERATION_UNSUPPORTED"
FAILURE_GENERATION_BUILD_FAILED = "GENERATION_BUILD_FAILED"
FAILURE_GENERATION_HASH_MISMATCH = "GENERATION_HASH_MISMATCH"
FAILURE_GENERATION_INVALIDATED = "GENERATION_INVALIDATED"
FAILURE_ABANDONED_FENCE_PENDING_CLEANUP = (
    "ABANDONED_FENCE_PENDING_CLEANUP"
)
FAILURE_ABANDONED_ORPHAN_CLEANUP = "ABANDONED_ORPHAN_CLEANUP"
FAILURE_RECOVERY_CUTOFF_EXPIRED = "RECOVERY_CUTOFF_EXPIRED"
FAILURE_STALE_ATTEMPT = "STALE_ATTEMPT"
FAILURE_NO_CROSS_DAY = "NO_CROSS_DAY"
FAILURE_INVALID_ITEM_STATE = "INVALID_ITEM_STATE"

TERMINAL_EXECUTION_FAILURE_CODES = frozenset(
    {
        FAILURE_TIMEOUT,
        FAILURE_DATA,
        FAILURE_CONTRACT,
        FAILURE_ALGORITHM,
        FAILURE_RESULT,
    }
)
NONEXECUTED_FAILURE_CODES = frozenset(
    {
        FAILURE_NATIVE_GENERATION_UNSUPPORTED,
        FAILURE_GENERATION_BUILD_FAILED,
        FAILURE_GENERATION_HASH_MISMATCH,
        FAILURE_GENERATION_INVALIDATED,
    }
)
SCHEDULE_FAILURE_CODES = frozenset(
    {
        FAILURE_TRANSIENT_INFRA,
        *TERMINAL_EXECUTION_FAILURE_CODES,
        *NONEXECUTED_FAILURE_CODES,
        FAILURE_ABANDONED_FENCE_PENDING_CLEANUP,
        FAILURE_ABANDONED_ORPHAN_CLEANUP,
        FAILURE_RECOVERY_CUTOFF_EXPIRED,
        FAILURE_STALE_ATTEMPT,
        FAILURE_NO_CROSS_DAY,
        FAILURE_INVALID_ITEM_STATE,
    }
)

_DAILY_TASK_TYPES = {"T+1", "T+5"}
_RUNTIME_TYPES = {"native_adapter", "blackbox_v2"}


@dataclass(frozen=True)
class RegistryTargetSnapshot:
    """Occurrence 创建时冻结的一条 active daily Registry target。"""

    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    task_type: str
    target_tenor: str
    horizon: int
    target_date: str


@dataclass(frozen=True)
class DailyScheduleItemSnapshot:
    """同一 base scheme 的不可变执行项及其验收 targets。"""

    base_scheme_id: str
    runtime_type: str
    scheme_version: str
    code_sha256: str
    config_sha256: str
    cache_group: str
    resource_class: str
    internal_workers: int
    release_offset_minutes: int
    release_at: str
    deadline_at: str
    targets: tuple[RegistryTargetSnapshot, ...]


@dataclass(frozen=True)
class DailyRegistrySnapshot:
    """一次日批创建时的完整 Registry 基线。"""

    items: tuple[DailyScheduleItemSnapshot, ...]
    registry_digest: str

    @property
    def expected_item_count(self) -> int:
        """返回冻结的 base item 数。"""
        return len(self.items)

    @property
    def expected_target_count(self) -> int:
        """返回冻结的 Registry target 数。"""
        return sum(len(item.targets) for item in self.items)


@dataclass(frozen=True)
class TargetSlaProjection:
    """08:00 target availability SLA 的持久化投影。

    ``accepted_target_count`` 是已有 commit-visible receipt 的当前数量，
    不是首个发布事务中仅标记为 ``ACCEPTED`` 的行数。
    """

    status: str
    evaluated_at: datetime
    deadline_at: datetime
    accepted_target_count: int
    accepted_by_deadline_count: int
    expected_target_count: int
    reason: str | None = None
    newly_persisted: bool = False


@dataclass(frozen=True)
class StartGuardrailProjection:
    """Blackbox V2 07:45 启动 guardrail 的只读投影。"""

    status: str
    evaluated_at: datetime
    deadline_at: datetime
    reason: str | None = None
    newly_persisted: bool = False


def freeze_active_daily_registry(
    rows: Iterable[Mapping[str, object]],
) -> DailyRegistrySnapshot:
    """过滤并冻结 active daily Registry，按 base scheme 生成执行项。"""
    grouped: dict[str, list[RegistryTargetSnapshot]] = {}
    item_fields_by_base: dict[str, dict[str, object]] = {}
    seen_registry_ids: set[str] = set()
    seen_item_targets: set[tuple[str, str, int]] = set()

    for row in rows:
        if row.get("status") != "active" or row.get("frequency") != "daily":
            continue
        registry_scheme_id = _required_text(row, "scheme_id")
        base_scheme_id = _required_text(row, "base_scheme_id")
        runtime_type = _required_text(row, "runtime_type")
        task_type = _required_text(row, "task_type")
        target_tenor = _required_text(row, "target_tenor")
        target_date = _required_date_text(row, "target_date")
        scheme_version = _required_text(row, "scheme_version")
        code_sha256 = _required_sha256(row, "code_sha256")
        config_sha256 = _required_sha256(row, "config_sha256")
        cache_group = _required_text(row, "cache_group")
        resource_class = _required_text(row, "resource_class")
        internal_workers = _required_positive_int(
            row.get("internal_workers"),
            field="internal_workers",
        )
        release_offset_minutes = _required_nonnegative_int(
            row.get("release_offset_minutes"),
            field="release_offset_minutes",
        )
        release_at = _required_text(row, "release_at")
        deadline_at = _required_text(row, "deadline_at")
        try:
            horizon = int(row.get("horizon"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid horizon for Registry target {registry_scheme_id}"
            ) from exc
        if horizon <= 0:
            raise ValueError(
                f"invalid horizon for Registry target {registry_scheme_id}: {horizon}"
            )
        if runtime_type not in _RUNTIME_TYPES:
            raise ValueError(
                f"invalid runtime_type for Registry target {registry_scheme_id}: "
                f"{runtime_type}"
            )
        if task_type not in _DAILY_TASK_TYPES:
            raise ValueError(
                f"invalid daily task_type for Registry target {registry_scheme_id}: "
                f"{task_type}"
            )
        expected_horizon = 1 if task_type == "T+1" else 5
        if horizon != expected_horizon:
            raise ValueError(
                "daily task_type/horizon mismatch for Registry target "
                f"{registry_scheme_id}: {task_type} requires {expected_horizon}"
            )
        expected_registry_id = (
            f"{base_scheme_id}__h{horizon}__{target_tenor}"
        )
        if registry_scheme_id != expected_registry_id:
            raise ValueError(
                "invalid composite Registry scheme_id: "
                f"expected={expected_registry_id} actual={registry_scheme_id}"
            )
        if registry_scheme_id in seen_registry_ids:
            raise ValueError(
                f"duplicate Registry scheme_id in daily snapshot: {registry_scheme_id}"
            )
        item_target = (base_scheme_id, target_tenor, horizon)
        if item_target in seen_item_targets:
            raise ValueError(
                "duplicate item target in daily snapshot: "
                f"{base_scheme_id}/{target_tenor}/h{horizon}"
            )
        item_fields = {
            "runtime_type": runtime_type,
            "scheme_version": scheme_version,
            "code_sha256": code_sha256,
            "config_sha256": config_sha256,
            "cache_group": cache_group,
            "resource_class": resource_class,
            "internal_workers": internal_workers,
            "release_offset_minutes": release_offset_minutes,
            "release_at": release_at,
            "deadline_at": deadline_at,
        }
        previous_item_fields = item_fields_by_base.setdefault(
            base_scheme_id,
            item_fields,
        )
        if previous_item_fields != item_fields:
            raise ValueError(
                f"mixed frozen item metadata for base scheme {base_scheme_id}"
            )

        seen_registry_ids.add(registry_scheme_id)
        seen_item_targets.add(item_target)
        grouped.setdefault(base_scheme_id, []).append(
            RegistryTargetSnapshot(
                registry_scheme_id=registry_scheme_id,
                base_scheme_id=base_scheme_id,
                runtime_type=runtime_type,
                task_type=task_type,
                target_tenor=target_tenor,
                horizon=horizon,
                target_date=target_date,
            )
        )

    if not grouped:
        raise ValueError("active daily Registry snapshot is empty")

    items = tuple(
        DailyScheduleItemSnapshot(
            base_scheme_id=base_scheme_id,
            runtime_type=str(
                item_fields_by_base[base_scheme_id]["runtime_type"]
            ),
            scheme_version=str(
                item_fields_by_base[base_scheme_id]["scheme_version"]
            ),
            code_sha256=str(
                item_fields_by_base[base_scheme_id]["code_sha256"]
            ),
            config_sha256=str(
                item_fields_by_base[base_scheme_id]["config_sha256"]
            ),
            cache_group=str(
                item_fields_by_base[base_scheme_id]["cache_group"]
            ),
            targets=tuple(
                sorted(
                    grouped[base_scheme_id],
                    key=lambda target: (
                        target.registry_scheme_id,
                        target.target_tenor,
                        target.horizon,
                    ),
                )
            ),
            resource_class=str(
                item_fields_by_base[base_scheme_id]["resource_class"]
            ),
            internal_workers=_required_positive_int(
                item_fields_by_base[base_scheme_id]["internal_workers"],
                field="internal_workers",
            ),
            release_offset_minutes=_required_nonnegative_int(
                item_fields_by_base[base_scheme_id][
                    "release_offset_minutes"
                ],
                field="release_offset_minutes",
            ),
            release_at=str(
                item_fields_by_base[base_scheme_id]["release_at"]
            ),
            deadline_at=str(
                item_fields_by_base[base_scheme_id]["deadline_at"]
            ),
        )
        for base_scheme_id in sorted(grouped)
    )
    return DailyRegistrySnapshot(
        items=items,
        registry_digest=_registry_digest(items),
    )


def validate_snapshot_cardinality(
    snapshot: DailyRegistrySnapshot,
    *,
    actual_item_count: int,
    actual_target_count: int,
) -> None:
    """按冻结快照动态校验 occurrence 数量，不依赖部署时常量。"""
    if (
        actual_item_count != snapshot.expected_item_count
        or actual_target_count != snapshot.expected_target_count
    ):
        raise ValueError(
            "daily occurrence cardinality mismatch: "
            f"expected items={snapshot.expected_item_count} "
            f"targets={snapshot.expected_target_count}, "
            f"got items={actual_item_count} targets={actual_target_count}"
        )


def project_target_availability_sla(
    *,
    current: TargetSlaProjection | None,
    visible_target_count: int,
    accepted_by_deadline_count: int,
    expected_target_count: int,
    evaluated_at: datetime,
    deadline_at: datetime,
) -> TargetSlaProjection:
    """按 commit-visible receipt 投影 SLA；终态一旦写入便不再反转。"""
    _validate_target_counts(visible_target_count, expected_target_count)
    _validate_target_counts(
        accepted_by_deadline_count,
        expected_target_count,
    )
    if accepted_by_deadline_count > visible_target_count:
        raise ValueError(
            "accepted_by_deadline_count cannot exceed current visible count"
        )
    if current is not None and current.status in {SLA_MET, SLA_BREACHED}:
        return TargetSlaProjection(
            status=current.status,
            evaluated_at=current.evaluated_at,
            deadline_at=current.deadline_at,
            accepted_target_count=visible_target_count,
            accepted_by_deadline_count=current.accepted_by_deadline_count,
            expected_target_count=current.expected_target_count,
            reason=current.reason,
        )
    availability_count = (
        accepted_by_deadline_count
        if evaluated_at >= deadline_at
        else visible_target_count
    )
    if availability_count == expected_target_count:
        status = SLA_MET
        reason = None
    elif evaluated_at >= deadline_at:
        status = SLA_BREACHED
        reason = "TARGETS_INCOMPLETE_AT_DEADLINE"
    else:
        status = SLA_PENDING
        reason = None
    return TargetSlaProjection(
        status=status,
        evaluated_at=evaluated_at,
        deadline_at=deadline_at,
        accepted_target_count=visible_target_count,
        accepted_by_deadline_count=accepted_by_deadline_count,
        expected_target_count=expected_target_count,
        reason=reason,
    )


def project_v2_start_guardrail(
    *,
    current: StartGuardrailProjection | None,
    runtime_type: str,
    started_at: datetime | None,
    evaluated_at: datetime,
    deadline_at: datetime,
) -> StartGuardrailProjection:
    """投影 V2 07:45 guardrail；ON_TIME/LATE 写入后不再反转。"""
    if runtime_type != "blackbox_v2":
        status = GUARDRAIL_NOT_APPLICABLE
        reason = None
    elif current is not None and current.status in {
        ITEM_SLA_ON_TIME,
        ITEM_SLA_LATE,
    }:
        return current
    elif started_at is not None:
        if started_at <= deadline_at:
            status = ITEM_SLA_ON_TIME
            reason = None
        else:
            status = ITEM_SLA_LATE
            reason = "V2_STARTED_AFTER_0745"
    elif evaluated_at >= deadline_at:
        status = ITEM_SLA_LATE
        reason = "V2_NOT_STARTED_BY_0745"
    else:
        status = ITEM_SLA_PENDING
        reason = None
    return StartGuardrailProjection(
        status=status,
        evaluated_at=evaluated_at,
        deadline_at=deadline_at,
        reason=reason,
    )


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"active daily Registry row requires non-empty {field}")
    return value.strip()


def _required_date_text(row: Mapping[str, object], field: str) -> str:
    value = _required_text(row, field)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD, got {value!r}") from exc


def _required_sha256(row: Mapping[str, object], field: str) -> str:
    value = _required_text(row, field).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA256 hex digest")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional text fields must be non-empty when provided")
    return value.strip()


def _optional_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _required_positive_int(value: object, *, field: str) -> int:
    normalized = _optional_positive_int(value, field=field)
    if normalized is None:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _required_nonnegative_int(value: object, *, field: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if isinstance(value, bool) or normalized < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return normalized


def _registry_digest(items: tuple[DailyScheduleItemSnapshot, ...]) -> str:
    payload = [
        {
            "base_scheme_id": item.base_scheme_id,
            "runtime_type": item.runtime_type,
            "scheme_version": item.scheme_version,
            "code_sha256": item.code_sha256,
            "config_sha256": item.config_sha256,
            "cache_group": item.cache_group,
            "resource_class": item.resource_class,
            "internal_workers": item.internal_workers,
            "release_at": item.release_at,
            "deadline_at": item.deadline_at,
            "targets": [
                {
                    "registry_scheme_id": target.registry_scheme_id,
                    "task_type": target.task_type,
                    "target_tenor": target.target_tenor,
                    "horizon": target.horizon,
                    "target_date": target.target_date,
                }
                for target in item.targets
            ],
        }
        for item in items
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_target_counts(accepted: int, expected: int) -> None:
    if expected <= 0:
        raise ValueError("expected_target_count must be positive")
    if accepted < 0 or accepted > expected:
        raise ValueError(
            "accepted_target_count must be between zero and expected_target_count"
        )
