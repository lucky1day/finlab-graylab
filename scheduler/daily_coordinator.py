"""日频单协调器的纯策略、规划与单机 owner 控制原语。

本模块不睡眠、不创建常驻 worker，也不直接修改数据库。调用方负责把这里
产出的确定性决策映射到 occurrence/item 账本和实际执行入口。
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from scheduler.daily_ledger import (
    FAILURE_ABANDONED_FENCE_PENDING_CLEANUP,
    FAILURE_ABANDONED_ORPHAN_CLEANUP,
    FAILURE_INVALID_ITEM_STATE,
    FAILURE_NATIVE_GENERATION_UNSUPPORTED,
    FAILURE_NO_CROSS_DAY,
    FAILURE_RECOVERY_CUTOFF_EXPIRED,
    FAILURE_TRANSIENT_INFRA,
    SCHEDULE_FAILURE_CODES,
    TERMINAL_EXECUTION_FAILURE_CODES,
)
from scheduler.daily_policy import DailySchedulerPolicy, SchemeDailyPolicy


TRANSIENT_FAILURE_CODE = FAILURE_TRANSIENT_INFRA
TERMINAL_FAILURE_CODES = TERMINAL_EXECUTION_FAILURE_CODES
TERMINAL_ITEM_STATES = frozenset(
    {
        "FAILED_TERMINAL",
        "EXPIRED",
    }
)
REQUEUEABLE_ITEM_STATES = frozenset(
    {
        "PENDING",
        "ABANDONED",
        "RETRY_WAIT",
    }
)


class DailyCoordinatorError(ValueError):
    """协调器输入不完整、漂移或自相矛盾。"""


class OccurrenceLockUnavailable(RuntimeError):
    """当日日批已由另一个进程持有。"""


class OccurrenceLockPathChanged(RuntimeError):
    """锁目录自校验后发生替换，拒绝在另一 inode 上取得伪 owner 锁。"""


@dataclass(frozen=True)
class V2Release:
    """一个 Blackbox V2 item 的独立释放时间。"""

    scheme_id: str
    release_at: datetime


@dataclass(frozen=True)
class GovernorDecision:
    """资源 governor 对一次候选启动的判定。"""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class ControlDecision:
    """重试或恢复控制决策。"""

    action: str
    reason: str
    failure_code: str | None = None


@dataclass(frozen=True)
class ItemControlState:
    """planner 所需的最小 item 状态快照。"""

    scheme_id: str
    state: str
    attempt_no: int = 0
    failure_code: str | None = None


@dataclass(frozen=True)
class DispatchDecision:
    """供后续账本/执行集成消费的确定性派发决策。"""

    scheme_id: str
    action: str
    reason: str
    pool: str
    release_at: datetime | None = None
    failure_code: str | None = None
    sla_status: str = "on_time"


def compute_v2_releases(
    policy: DailySchedulerPolicy,
    *,
    sealed_at: datetime,
) -> tuple[V2Release, ...]:
    """按同一 DataBridge sealed_at 计算四个互不依赖的 V2 释放点。"""
    localized_sealed_at = _localized(policy, sealed_at)
    v2_items = sorted(
        (
            item
            for item in policy.schemes.values()
            if item.runtime_type == "blackbox_v2"
        ),
        key=lambda item: (
            _required_release_offset(item),
            item.scheme_id,
        ),
    )
    return tuple(
        V2Release(
            scheme_id=item.scheme_id,
            release_at=localized_sealed_at
            + timedelta(minutes=_required_release_offset(item)),
        )
        for item in v2_items
    )


def dispatch_order(
    policy: DailySchedulerPolicy,
    scheme_ids: Iterable[str],
) -> tuple[str, ...]:
    """按绝对 deadline、cache prerequisite、同级 LPT 确定稳定顺序。"""
    normalized = tuple(scheme_ids)
    if len(set(normalized)) != len(normalized):
        raise DailyCoordinatorError("dispatch candidates contain duplicate schemes")
    unknown = sorted(set(normalized) - set(policy.schemes))
    if unknown:
        raise DailyCoordinatorError(
            f"dispatch candidates contain unknown schemes: {unknown}"
        )
    return tuple(
        sorted(
            normalized,
            key=lambda scheme_id: _dispatch_sort_key(
                policy.schemes[scheme_id]
            ),
        )
    )


class ResourceGovernor:
    """只允许版本化 policy 声明、且须经 rollout 容量门禁的驻留组合。"""

    def __init__(self, policy: DailySchedulerPolicy) -> None:
        self._policy = policy

    def can_start(
        self,
        *,
        running_scheme_ids: Iterable[str],
        running_resource_classes: Iterable[str] = (),
        candidate_scheme_id: str,
    ) -> GovernorDecision:
        """判断候选是否可与当前运行项共存，不执行隐式扩容。"""
        running = tuple(running_scheme_ids)
        if len(set(running)) != len(running):
            return GovernorDecision(False, "DUPLICATE_RUNNING_SCHEME")
        unknown = sorted(set(running) - set(self._policy.schemes))
        if unknown:
            return GovernorDecision(False, "UNKNOWN_RUNNING_SCHEME")
        candidate = self._policy.schemes.get(candidate_scheme_id)
        if candidate is None:
            return GovernorDecision(False, "UNKNOWN_CANDIDATE_SCHEME")
        if candidate_scheme_id in running:
            return GovernorDecision(False, "CANDIDATE_ALREADY_RUNNING")
        if candidate.input_compatibility == "unsupported":
            return GovernorDecision(False, "INPUT_UNSUPPORTED")

        running_items = tuple(self._policy.schemes[value] for value in running)
        native_running = sum(
            item.runtime_type == "native_adapter" for item in running_items
        )
        v2_running = sum(
            item.runtime_type == "blackbox_v2" for item in running_items
        )
        if candidate.runtime_type == "native_adapter":
            if native_running >= self._policy.native_max_concurrency:
                return GovernorDecision(False, "NATIVE_POOL_LIMIT")
        elif candidate.runtime_type == "blackbox_v2":
            if v2_running >= self._policy.v2_max_concurrency:
                return GovernorDecision(False, "V2_POOL_LIMIT")
        else:
            return GovernorDecision(False, "UNSUPPORTED_RUNTIME_TYPE")

        background_resources = tuple(running_resource_classes)
        if (
            len(set(background_resources)) != len(background_resources)
            or any(
                not isinstance(resource, str) or not resource.strip()
                for resource in background_resources
            )
        ):
            return GovernorDecision(
                False,
                "INVALID_RUNNING_RESOURCE_CLASS",
            )
        signature = tuple(
            sorted(
                (
                    *(
                        item.resource_class
                        for item in (*running_items, candidate)
                    ),
                    *background_resources,
                )
            )
        )
        if signature not in self._policy.allowed_resource_combinations:
            return GovernorDecision(
                False,
                "UNAPPROVED_RESOURCE_COMBINATION",
            )
        return GovernorDecision(True, "APPROVED")


def decide_retry(
    policy: DailySchedulerPolicy,
    *,
    scheme_id: str,
    failure_code: str,
    attempt_no: int,
    all_first_attempts_covered: bool,
    business_date: date,
    now: datetime,
) -> ControlDecision:
    """仅为首轮 transient infra 失败计算一次有预算的自动重试。"""
    item = _require_scheme(policy, scheme_id)
    if failure_code not in SCHEDULE_FAILURE_CODES:
        raise DailyCoordinatorError(
            f"unknown schedule failure_code: {failure_code!r}"
        )
    localized_now = _localized(policy, now)
    if localized_now.date() != business_date:
        return ControlDecision(
            "TERMINAL",
            "NO_CROSS_DAY",
            failure_code=FAILURE_NO_CROSS_DAY,
        )
    recovery_cutoff = _at(policy, business_date, policy.recovery_cutoff)
    if localized_now >= recovery_cutoff:
        return ControlDecision(
            "TERMINAL",
            "RECOVERY_CUTOFF_REACHED",
            failure_code=FAILURE_RECOVERY_CUTOFF_EXPIRED,
        )
    if failure_code != TRANSIENT_FAILURE_CODE:
        return ControlDecision(
            "TERMINAL",
            "NON_RETRYABLE_FAILURE",
            failure_code=failure_code,
        )
    if attempt_no < 1:
        raise DailyCoordinatorError("attempt_no must be at least 1")
    if attempt_no >= 1 + policy.retry_max:
        return ControlDecision(
            "TERMINAL",
            "RETRY_EXHAUSTED",
            failure_code=failure_code,
        )
    if not all_first_attempts_covered:
        return ControlDecision(
            "WAIT_FIRST_ROUND",
            "FIRST_ROUND_INCOMPLETE",
            failure_code=failure_code,
        )

    if not _retry_fits_before_cutoff(
        policy,
        item=item,
        now=localized_now,
        recovery_cutoff=recovery_cutoff,
    ):
        return ControlDecision(
            "TERMINAL",
            "RETRY_BUDGET_EXHAUSTED",
            failure_code=failure_code,
        )
    return ControlDecision(
        "RETRY",
        "TRANSIENT_INFRA_RETRY_APPROVED",
        failure_code=failure_code,
    )


def decide_abandoned_retry(
    policy: DailySchedulerPolicy,
    *,
    scheme_id: str,
    failure_code: str,
    attempt_no: int,
    all_first_attempts_covered: bool,
    business_date: date,
    now: datetime,
) -> ControlDecision:
    """仅允许已确认孤儿清理的 attempt 使用一次有预算恢复。"""
    item = _require_scheme(policy, scheme_id)
    localized_now = _localized(policy, now)
    if localized_now.date() != business_date:
        return ControlDecision(
            "TERMINAL",
            "NO_CROSS_DAY",
            failure_code=FAILURE_NO_CROSS_DAY,
        )
    recovery_cutoff = _at(policy, business_date, policy.recovery_cutoff)
    if localized_now >= recovery_cutoff:
        return ControlDecision(
            "TERMINAL",
            "RECOVERY_CUTOFF_REACHED",
            failure_code=FAILURE_RECOVERY_CUTOFF_EXPIRED,
        )
    if failure_code == FAILURE_ABANDONED_FENCE_PENDING_CLEANUP:
        return ControlDecision(
            "WAIT_ORPHAN_CLEANUP",
            "ORPHAN_CLEANUP_NOT_CONFIRMED",
            failure_code=failure_code,
        )
    if failure_code != FAILURE_ABANDONED_ORPHAN_CLEANUP:
        return ControlDecision(
            "TERMINAL",
            "INVALID_ABANDONED_FENCE",
            failure_code=FAILURE_INVALID_ITEM_STATE,
        )
    if attempt_no < 1:
        return ControlDecision(
            "TERMINAL",
            "INVALID_ABANDONED_ATTEMPT",
            failure_code=FAILURE_INVALID_ITEM_STATE,
        )
    if attempt_no >= 1 + policy.retry_max:
        return ControlDecision(
            "TERMINAL",
            "RETRY_EXHAUSTED",
            failure_code=failure_code,
        )
    if not all_first_attempts_covered:
        return ControlDecision(
            "WAIT_FIRST_ROUND",
            "FIRST_ROUND_INCOMPLETE",
            failure_code=failure_code,
        )

    if not _retry_fits_before_cutoff(
        policy,
        item=item,
        now=localized_now,
        recovery_cutoff=recovery_cutoff,
    ):
        return ControlDecision(
            "TERMINAL",
            "RETRY_BUDGET_EXHAUSTED",
            failure_code=failure_code,
        )
    return ControlDecision(
        "RETRY",
        "ABANDONED_RETRY_APPROVED",
        failure_code=failure_code,
    )


def _retry_fits_before_cutoff(
    policy: DailySchedulerPolicy,
    *,
    item: SchemeDailyPolicy,
    now: datetime,
    recovery_cutoff: datetime,
) -> bool:
    """仅使用准入时冻结的硬上界，等于 cutoff 也必须拒绝。"""
    hard_runtime_sec = item.admitted_hard_runtime_sec
    cleanup_commit_margin_sec = (
        policy.retry_cleanup_commit_margin_sec
    )
    if (
        isinstance(hard_runtime_sec, bool)
        or not isinstance(hard_runtime_sec, int)
        or hard_runtime_sec <= 0
        or isinstance(cleanup_commit_margin_sec, bool)
        or not isinstance(cleanup_commit_margin_sec, int)
        or cleanup_commit_margin_sec <= 0
    ):
        raise DailyCoordinatorError(
            "retry hard runtime bound/margin is invalid"
        )
    hard_finish = now + timedelta(
        seconds=hard_runtime_sec + cleanup_commit_margin_sec
    )
    return hard_finish < recovery_cutoff


def decide_recovery(
    policy: DailySchedulerPolicy,
    *,
    scheme_id: str,
    item_state: str,
    occurrence_date: date,
    generation_business_date: date,
    bound_generation_id: str | None,
    occurrence_generation_id: str | None,
    generation_state: str,
    generation_digest_valid: bool,
    now: datetime,
    orphan_cleanup_confirmed: bool = False,
) -> ControlDecision:
    """计算重启恢复动作；不跨日，也不接受其它 generation。"""
    _require_scheme(policy, scheme_id)
    localized_now = _localized(policy, now)
    if occurrence_date != localized_now.date():
        return ControlDecision("NO_CROSS_DAY", "OCCURRENCE_DATE_MISMATCH")
    if generation_business_date != occurrence_date:
        return ControlDecision(
            "GENERATION_MISMATCH",
            "OLD_OR_FOREIGN_GENERATION_FORBIDDEN",
        )
    if not bound_generation_id or not occurrence_generation_id:
        return ControlDecision(
            "GENERATION_IDENTITY_MISSING",
            "FROZEN_GENERATION_ID_REQUIRED",
        )
    if bound_generation_id != occurrence_generation_id:
        return ControlDecision(
            "GENERATION_MISMATCH",
            "BOUND_GENERATION_DIFFERS_FROM_OCCURRENCE",
        )
    if generation_state != "SEALED":
        return ControlDecision(
            "GENERATION_INVALID",
            "FROZEN_GENERATION_NOT_SEALED",
        )
    if generation_digest_valid is not True:
        return ControlDecision(
            "GENERATION_HASH_MISMATCH",
            "FROZEN_GENERATION_DIGEST_INVALID",
        )
    if item_state == "SUCCESS":
        return ControlDecision("SKIP_SUCCESS", "ITEM_ALREADY_ACCEPTED")
    if item_state in TERMINAL_ITEM_STATES:
        return ControlDecision("SKIP_TERMINAL", "ITEM_ALREADY_TERMINAL")
    recovery_cutoff = _at(policy, occurrence_date, policy.recovery_cutoff)
    if item_state == "RUNNING":
        if not orphan_cleanup_confirmed:
            return ControlDecision(
                "REQUIRE_ABANDON_ORPHAN_CLEANUP",
                "RUNNING_ATTEMPT_MUST_BE_FENCED_AND_CLEANED",
            )
        if localized_now >= recovery_cutoff:
            return ControlDecision("EXPIRED", "RECOVERY_CUTOFF_REACHED")
        return ControlDecision(
            "REQUEUE_AFTER_CLEANUP",
            "ABANDONED_ATTEMPT_CLEANED",
        )
    if item_state in REQUEUEABLE_ITEM_STATES:
        if localized_now >= recovery_cutoff:
            return ControlDecision("EXPIRED", "RECOVERY_CUTOFF_REACHED")
        return ControlDecision("REQUEUE", "RECOVERY_ALLOWED")
    return ControlDecision(
        "TERMINAL",
        "UNKNOWN_ITEM_STATE",
        failure_code=FAILURE_INVALID_ITEM_STATE,
    )


def plan_dispatches(
    policy: DailySchedulerPolicy,
    *,
    business_date: date,
    now: datetime,
    item_states: Mapping[str, ItemControlState],
    native_generation_sealed: bool,
    databridge_generation_business_date: date | None = None,
    v2_release_at_by_scheme: Mapping[str, datetime] | None = None,
    native_generation_business_date: date | None = None,
    native_generation_id: str | None = None,
    expected_native_generation_id: str | None = None,
    databridge_generation_id: str | None = None,
    expected_databridge_generation_id: str | None = None,
    running_scheme_ids: Iterable[str] = (),
    running_resource_classes: Iterable[str] = (),
    all_first_attempts_covered: bool | None = None,
) -> tuple[DispatchDecision, ...]:
    """生成全量 item 的一次确定性派发快照，不产生执行副作用。"""
    localized_now = _localized(policy, now)
    _validate_planning_snapshot(policy, item_states)
    explicit_running = tuple(running_scheme_ids)
    state_running = tuple(
        scheme_id
        for scheme_id, item_state in item_states.items()
        if item_state.state == "RUNNING"
    )
    running = _stable_unique((*explicit_running, *state_running))
    unknown_running = sorted(set(running) - set(policy.schemes))
    if unknown_running:
        raise DailyCoordinatorError(
            f"running snapshot contains unknown schemes: {unknown_running}"
        )

    databridge_generation_reason = "DATABRIDGE_GENERATION_NOT_SEALED"
    release_by_id: dict[str, datetime] = {}
    if databridge_generation_business_date is not None:
        if databridge_generation_business_date != business_date:
            databridge_generation_reason = (
                "DATABRIDGE_GENERATION_NOT_CURRENT"
            )
        elif (
            not databridge_generation_id
            or not expected_databridge_generation_id
        ):
            databridge_generation_reason = (
                "DATABRIDGE_GENERATION_IDENTITY_MISSING"
            )
        elif (
            databridge_generation_id
            != expected_databridge_generation_id
        ):
            databridge_generation_reason = (
                "DATABRIDGE_GENERATION_MISMATCH"
            )
        else:
            frozen_releases = dict(v2_release_at_by_scheme or {})
            expected_v2_ids = {
                scheme_id
                for scheme_id, item in policy.schemes.items()
                if item.runtime_type == "blackbox_v2"
            }
            unknown_releases = sorted(
                set(frozen_releases) - expected_v2_ids
            )
            if unknown_releases:
                raise DailyCoordinatorError(
                    "frozen V2 releases contain unknown/non-V2 schemes: "
                    f"{unknown_releases}"
                )
            for scheme_id in expected_v2_ids:
                frozen_release = frozen_releases.get(scheme_id)
                if frozen_release is None:
                    continue
                localized_release = _localized(
                    policy,
                    frozen_release,
                )
                if localized_release.date() != business_date:
                    raise DailyCoordinatorError(
                        "frozen V2 release is outside business_date: "
                        f"{scheme_id}={localized_release.isoformat()}"
                    )
                release_by_id[scheme_id] = localized_release
            if len(release_by_id) != len(expected_v2_ids):
                databridge_generation_reason = (
                    "V2_FROZEN_RELEASE_MISSING"
                )
    decisions: dict[str, DispatchDecision] = {}
    candidates: list[str] = []
    snapshot_first_round_covered = _first_round_covered(
        policy,
        item_states,
    )
    retry_first_round_covered = snapshot_first_round_covered
    if all_first_attempts_covered is not None:
        retry_first_round_covered = (
            snapshot_first_round_covered
            and all_first_attempts_covered
        )
    not_before = _at(policy, business_date, policy.not_before)
    recovery_cutoff = _at(policy, business_date, policy.recovery_cutoff)

    for scheme_id, item in policy.schemes.items():
        state = item_states[scheme_id]
        pool = _pool_for(item)
        if item.input_compatibility == "unsupported":
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "TERMINAL",
                "NATIVE_INPUT_ADAPTER_REQUIRED",
                pool,
                failure_code=FAILURE_NATIVE_GENERATION_UNSUPPORTED,
            )
            continue
        if state.state == "SUCCESS":
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "SKIP_SUCCESS",
                "ITEM_ALREADY_ACCEPTED",
                pool,
            )
            continue
        if state.state in TERMINAL_ITEM_STATES:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "TERMINAL",
                "ITEM_ALREADY_TERMINAL",
                pool,
                failure_code=state.failure_code,
            )
            continue
        if localized_now.date() != business_date:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "TERMINAL",
                "NO_CROSS_DAY",
                pool,
                failure_code=FAILURE_NO_CROSS_DAY,
            )
            continue
        if state.state == "RUNNING":
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "RUNNING",
                "ATTEMPT_IN_PROGRESS",
                pool,
            )
            continue
        if localized_now >= recovery_cutoff:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "EXPIRED",
                "RECOVERY_CUTOFF_REACHED",
                pool,
                failure_code=FAILURE_RECOVERY_CUTOFF_EXPIRED,
            )
            continue
        if localized_now < not_before:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "WAIT_NOT_BEFORE",
                "DAILY_OCCURRENCE_NOT_RELEASED",
                pool,
            )
            continue
        if state.state not in REQUEUEABLE_ITEM_STATES:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "TERMINAL",
                "UNKNOWN_ITEM_STATE",
                pool,
                failure_code=FAILURE_INVALID_ITEM_STATE,
            )
            continue
        if state.state == "ABANDONED":
            abandoned_retry = decide_abandoned_retry(
                policy,
                scheme_id=scheme_id,
                failure_code=state.failure_code or "",
                attempt_no=state.attempt_no,
                all_first_attempts_covered=retry_first_round_covered,
                business_date=business_date,
                now=localized_now,
            )
            if abandoned_retry.action != "RETRY":
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    abandoned_retry.action,
                    abandoned_retry.reason,
                    pool,
                    failure_code=abandoned_retry.failure_code,
                )
                continue
        if state.state == "RETRY_WAIT":
            retry = decide_retry(
                policy,
                scheme_id=scheme_id,
                failure_code=state.failure_code or "",
                attempt_no=state.attempt_no,
                all_first_attempts_covered=retry_first_round_covered,
                business_date=business_date,
                now=localized_now,
            )
            if retry.action != "RETRY":
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    retry.action,
                    retry.reason,
                    pool,
                    failure_code=retry.failure_code,
                )
                continue
        if item.runtime_type == "native_adapter":
            if not native_generation_sealed:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    "NATIVE_GENERATION_NOT_SEALED",
                    pool,
                )
                continue
            if native_generation_business_date is None:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    "NATIVE_GENERATION_IDENTITY_MISSING",
                    pool,
                )
                continue
            if native_generation_business_date != business_date:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    "NATIVE_GENERATION_NOT_CURRENT",
                    pool,
                )
                continue
            if not native_generation_id or not expected_native_generation_id:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    "NATIVE_GENERATION_IDENTITY_MISSING",
                    pool,
                )
                continue
            if native_generation_id != expected_native_generation_id:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    "NATIVE_GENERATION_MISMATCH",
                    pool,
                )
                continue
            cache_prerequisite = _cache_prerequisite_for(
                policy,
                item,
            )
            if (
                cache_prerequisite is not None
                and item_states[cache_prerequisite].state != "SUCCESS"
            ):
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_CACHE_PREREQUISITE",
                    (
                        "CACHE_PREREQUISITE_NOT_SUCCESS:"
                        f"{cache_prerequisite}"
                    ),
                    pool,
                )
                continue
        else:
            release_at = release_by_id.get(scheme_id)
            if release_at is None:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_GENERATION",
                    databridge_generation_reason,
                    pool,
                )
                continue
            if localized_now < release_at:
                decisions[scheme_id] = DispatchDecision(
                    scheme_id,
                    "WAIT_RELEASE",
                    "V2_RELEASE_OFFSET_PENDING",
                    pool,
                    release_at=release_at,
                )
                continue
        candidates.append(scheme_id)

    governor = ResourceGovernor(policy)
    selected_running = list(running)
    for scheme_id in dispatch_order(policy, candidates):
        item = policy.schemes[scheme_id]
        governor_decision = governor.can_start(
            running_scheme_ids=selected_running,
            running_resource_classes=running_resource_classes,
            candidate_scheme_id=scheme_id,
        )
        if governor_decision.allowed:
            decisions[scheme_id] = DispatchDecision(
                scheme_id,
                "DISPATCH",
                "POLICY_APPROVED",
                _pool_for(item),
                release_at=release_by_id.get(scheme_id),
            )
            selected_running.append(scheme_id)
            continue
        wait_action = (
            "WAIT_POOL"
            if governor_decision.reason
            in {"NATIVE_POOL_LIMIT", "V2_POOL_LIMIT"}
            else "WAIT_RESOURCE"
        )
        decisions[scheme_id] = DispatchDecision(
            scheme_id,
            wait_action,
            governor_decision.reason,
            _pool_for(item),
            release_at=release_by_id.get(scheme_id),
        )

    v2_guardrail = _at(policy, business_date, policy.v2_start_guardrail)
    if (
        localized_now.date() == business_date
        and localized_now >= v2_guardrail
    ):
        for scheme_id, item in policy.schemes.items():
            state = item_states[scheme_id]
            if (
                item.runtime_type == "blackbox_v2"
                and state.state == "PENDING"
                and state.attempt_no == 0
            ):
                decisions[scheme_id] = replace(
                    decisions[scheme_id],
                    sla_status="late",
                )

    all_ids = dispatch_order(policy, policy.schemes)
    return tuple(decisions[scheme_id] for scheme_id in all_ids)


class OccurrenceFileLock:
    """基于 ``fcntl.flock`` 的非阻塞单 occurrence owner 锁。

    锁文件是稳定 fence 文件，释放时只解锁并关闭描述符，绝不 unlink。
    """

    def __init__(self, path: str | Path) -> None:
        self._fd: int | None = None
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("occurrence lock path must be absolute")
        if candidate.suffix != ".lock":
            raise ValueError("occurrence lock path must end with .lock")
        if candidate != candidate.resolve(strict=False):
            raise ValueError("occurrence lock path must be normalized and symlink-free")
        if not candidate.parent.is_dir():
            raise ValueError("occurrence lock parent must be an existing directory")
        parent_details = os.stat(candidate.parent, follow_symlinks=False)
        _validate_lock_parent_security(parent_details)
        self._path = candidate
        self._parent_identity = (
            parent_details.st_dev,
            parent_details.st_ino,
        )

    @property
    def path(self) -> Path:
        """返回已校验的显式锁路径。"""
        return self._path

    @property
    def acquired(self) -> bool:
        """当前实例是否持有文件锁。"""
        return self._fd is not None

    def acquire(self) -> OccurrenceFileLock:
        """非阻塞获取独占锁；已被持有时立即抛出。"""
        if self._fd is not None:
            return self
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_flags = os.O_RDONLY
        parent_flags |= getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd: int | None = None
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
            parent_details = os.fstat(parent_fd)
            try:
                _validate_lock_parent_security(parent_details)
            except ValueError as exc:
                raise OccurrenceLockPathChanged(
                    f"occurrence lock parent became unsafe: {self._path.parent}"
                ) from exc
            if (
                parent_details.st_dev,
                parent_details.st_ino,
            ) != self._parent_identity:
                raise OccurrenceLockPathChanged(
                    f"occurrence lock parent changed: {self._path.parent}"
                )
            fd = os.open(
                self._path.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if parent_fd is not None:
                os.close(parent_fd)
            raise OccurrenceLockPathChanged(
                f"cannot safely resolve occurrence lock parent: {self._path}"
            ) from exc
        except BaseException:
            if parent_fd is not None:
                os.close(parent_fd)
            raise
        try:
            current_parent = os.stat(
                self._path.parent,
                follow_symlinks=False,
            )
            try:
                _validate_lock_parent_security(current_parent)
            except ValueError as exc:
                raise OccurrenceLockPathChanged(
                    f"occurrence lock parent became unsafe: {self._path.parent}"
                ) from exc
            if (
                current_parent.st_dev,
                current_parent.st_ino,
            ) != self._parent_identity:
                raise OccurrenceLockPathChanged(
                    f"occurrence lock parent changed: {self._path.parent}"
                )
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("occurrence lock must be a regular file")
            if details.st_uid != os.getuid():
                raise ValueError("occurrence lock must be owned by the service user")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise OccurrenceLockUnavailable(
                    f"daily occurrence lock is already held: {self._path}"
                ) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        self._fd = fd
        return self

    def release(self) -> None:
        """释放 owner 锁但保留 fence 文件。"""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> OccurrenceFileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


def _dispatch_sort_key(item: SchemeDailyPolicy) -> tuple[time, int, int, str]:
    return (
        item.absolute_deadline,
        0 if item.cache_prerequisite else 1,
        -item.estimated_cold_sec,
        item.scheme_id,
    )


def _cache_prerequisite_for(
    policy: DailySchedulerPolicy,
    item: SchemeDailyPolicy,
) -> str | None:
    """返回共享 Native cache group 的唯一 prerequisite。"""
    if item.runtime_type != "native_adapter" or item.cache_prerequisite:
        return None
    members = tuple(
        candidate
        for candidate in policy.schemes.values()
        if candidate.runtime_type == "native_adapter"
        and candidate.cache_group == item.cache_group
    )
    if len(members) < 2:
        return None
    prerequisites = tuple(
        candidate.scheme_id
        for candidate in members
        if candidate.cache_prerequisite
    )
    if len(prerequisites) != 1:
        raise DailyCoordinatorError(
            "shared Native cache group has no unique prerequisite: "
            f"{item.cache_group}"
        )
    return prerequisites[0]


def _required_release_offset(item: SchemeDailyPolicy) -> int:
    value = item.v2_release_offset_min
    if value is None:
        raise DailyCoordinatorError(
            f"{item.scheme_id}: Blackbox V2 release offset is missing"
        )
    return value


def _require_scheme(
    policy: DailySchedulerPolicy,
    scheme_id: str,
) -> SchemeDailyPolicy:
    try:
        return policy.schemes[scheme_id]
    except KeyError as exc:
        raise DailyCoordinatorError(f"unknown daily scheme: {scheme_id}") from exc


def _pool_for(item: SchemeDailyPolicy) -> str:
    if item.runtime_type == "native_adapter":
        return "native"
    if item.runtime_type == "blackbox_v2":
        return "v2"
    raise DailyCoordinatorError(
        f"{item.scheme_id}: unsupported runtime_type={item.runtime_type}"
    )


def _localized(
    policy: DailySchedulerPolicy,
    value: datetime,
) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DailyCoordinatorError("scheduler datetime must be timezone-aware")
    return value.astimezone(ZoneInfo(policy.timezone))


def _at(
    policy: DailySchedulerPolicy,
    business_date: date,
    value: time,
) -> datetime:
    return datetime.combine(
        business_date,
        value,
        tzinfo=ZoneInfo(policy.timezone),
    )


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _validate_planning_snapshot(
    policy: DailySchedulerPolicy,
    item_states: Mapping[str, ItemControlState],
) -> None:
    expected = set(policy.schemes)
    actual = set(item_states)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise DailyCoordinatorError(
            "item state snapshot does not match policy; "
            f"missing={missing}, unknown={unknown}"
        )
    mismatches = sorted(
        scheme_id
        for scheme_id, state in item_states.items()
        if state.scheme_id != scheme_id
    )
    if mismatches:
        raise DailyCoordinatorError(
            f"item state scheme_id mismatch: {mismatches}"
        )
    unknown_failure_codes = sorted(
        {
            str(state.failure_code)
            for state in item_states.values()
            if (
                state.failure_code is not None
                and state.failure_code not in SCHEDULE_FAILURE_CODES
            )
        }
    )
    if unknown_failure_codes:
        raise DailyCoordinatorError(
            "unknown schedule failure_code in item snapshot: "
            f"{unknown_failure_codes}"
        )


def _first_round_covered(
    policy: DailySchedulerPolicy,
    item_states: Mapping[str, ItemControlState],
) -> bool:
    for scheme_id, item in policy.schemes.items():
        state = item_states[scheme_id]
        if is_first_attempt_covered(
            state=state.state,
            attempt_no=state.attempt_no,
            input_unsupported=(
                item.input_compatibility == "unsupported"
            ),
        ):
            continue
        return False
    return True


def is_first_attempt_covered(
    *,
    state: str,
    attempt_no: int,
    input_unsupported: bool = False,
) -> bool:
    """返回 item 是否已不再欠缺首轮执行机会。"""
    return (
        input_unsupported
        or int(attempt_no) >= 1
        or state == "SUCCESS"
        or state in TERMINAL_ITEM_STATES
    )


def _validate_lock_parent_security(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("occurrence lock parent must be a real directory")
    if details.st_uid != os.getuid():
        raise ValueError("occurrence lock parent must be owned by the service user")
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(
            "occurrence lock parent cannot be group/world writable"
        )
