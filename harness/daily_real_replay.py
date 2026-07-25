"""真实日频算法的隔离 21/25 功能联跑边界。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from scheduler import scheduled_executor
from scheduler.daily_coordinator import (
    OccurrenceFileLock,
    OccurrenceLockUnavailable,
    ResourceGovernor,
    dispatch_order,
)
from scheduler.daily_ledger import freeze_active_daily_registry
from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
    EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME,
    load_daily_policy,
)
from scheduler.daily_runtime import _policy_payload
from scheduler.discovery import discover_schemes
from scheduler.repository import (
    create_schedule_occurrence as _repository_create_schedule_occurrence,
)
from scheduler.repository import (
    read_schedule_execution_envelope
    as _repository_read_schedule_execution_envelope,
)
from scheduler.repository import (
    read_schedule_occurrence_snapshot
    as _repository_read_schedule_occurrence_snapshot,
)
from scheduler.repository import (
    register_seal_and_bind_schedule_occurrence_generation
    as _repository_register_generation,
)
from shared.calendar_service import FrozenCalendarService
from shared.daily_coordinator_mode import (
    DailyCoordinatorEpochIdentity,
    VerifiedIsolatedDailyDatabase,
    assert_daily_coordinator_epoch_matches_policy,
    bind_isolated_daily_coordinator_epoch,
    recheck_verified_isolated_daily_database,
    resolve_daily_runtime_root,
    verify_and_register_isolated_daily_database,
)
from shared.databridge_input_generation import (
    DataBridgeGenerationContext,
    open_databridge_generation,
)
from shared.input_artifacts import BLACKBOX_SCHEMA_PATH
from shared.native_input_generation import (
    NativeGenerationContext,
    open_native_generation,
)


REAL_REPLAY_SCHEMA_VERSION = "daily-real-replay-v1"
REAL_REPLAY_SCHEDULE_PREFIX = "isolated-real-replay-v1-"
EXPECTED_ITEM_COUNT = 21
EXPECTED_TARGET_COUNT = 25
EXPECTED_NATIVE_COUNT = 17
EXPECTED_V2_COUNT = 4
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyRealReplayError(RuntimeError):
    """真实日频隔离联跑的输入或冻结策略不满足契约。"""


@dataclass(frozen=True)
class DailyRealReplayInputs:
    """一组已重新打开并交叉验证的真实联跑 generation。"""

    native_generation: NativeGenerationContext
    databridge_generation: DataBridgeGenerationContext
    business_date: str
    feature_date: str


DailyRealReplayDatabaseIdentity = VerifiedIsolatedDailyDatabase


@dataclass(frozen=True)
class RealReplayRuntimeResult:
    """一次隔离真实回放的非 SLA 结果。"""

    occurrence_id: int
    status: str
    accepted_target_count: int
    expected_target_count: int
    dispatched_scheme_ids: tuple[str, ...]
    failed_scheme_ids: tuple[str, ...]
    blocked_scheme_ids: tuple[str, ...]
    cutoff_at: datetime
    qualification: str = "EXCLUDED"


class RealReplayRuntime:
    """串行安全基线：只经 canonical executor 写隔离 replay ledger。

    本类刻意不接受 services、clock、runner、verifier 或 executor 注入。
    当前先以单 owner、单驻留任务证明完整控制边界；后续双池优化只能在
    保持同一 ``run`` 公共接口和 governor 约束的前提下替换内部拓扑。
    canonical executor 仍可记录历史回放中被 EXCLUDED 的 V2 guardrail
    审计字段；它们不构成生产 operational/SLA late 证据。本 runtime
    也不调用 occurrence SLA evaluator 或生产健康控制面。
    """

    def __init__(
        self,
        engine: Any,
        *,
        isolation: DailyRealReplayDatabaseIdentity,
        occurrence_id: int,
        policy: Any,
        configs: Mapping[str, Any],
        inputs: DailyRealReplayInputs,
    ) -> None:
        normalized_occurrence_id = int(occurrence_id)
        if normalized_occurrence_id <= 0:
            raise DailyRealReplayError(
                "real replay occurrence_id must be positive"
            )
        self._engine = engine
        self._isolation = isolation
        self._occurrence_id = normalized_occurrence_id
        self._policy = policy
        self._configs = dict(configs)
        if not isinstance(inputs, DailyRealReplayInputs):
            raise DailyRealReplayError(
                "real replay verified generation inputs are required"
            )
        self._inputs = inputs
        _assert_deployed_real_replay_definitions(
            policy,
            configs=self._configs,
        )
        self._owner_active = False
        _recheck_real_replay_database(self._engine, self._isolation)
        snapshot = self._read_validated_snapshot()
        _assert_real_replay_execution_envelopes(
            self._engine,
            snapshot=snapshot,
            policy=self._policy,
            inputs=self._inputs,
        )
        self._cutoff_at = _stored_utc_datetime(
            snapshot.occurrence.recovery_cutoff_at,
            field="recovery_cutoff_at",
        )
        observed_at = _now_utc()
        if self._cutoff_at != _next_shanghai_midnight(observed_at):
            raise DailyRealReplayError(
                "real replay cutoff is not the next Shanghai midnight"
            )
        self._governor = ResourceGovernor(policy)

    def run(self) -> RealReplayRuntimeResult:
        """在隔离 owner 锁内串行完成所有可独立执行的首轮 item。"""
        _recheck_real_replay_database(self._engine, self._isolation)
        snapshot = self._read_validated_snapshot()
        lock = OccurrenceFileLock(self._owner_lock_path())
        try:
            lock.acquire()
        except OccurrenceLockUnavailable:
            return self._result(
                snapshot,
                status="recovery_blocked",
                blocked_scheme_ids=tuple(
                    sorted(
                        summary.item.base_scheme_id
                        for summary in snapshot.items
                        if summary.item.state != "SUCCESS"
                    )
                ),
            )
        self._owner_active = True
        dispatched: list[str] = []
        try:
            while True:
                _recheck_real_replay_database(
                    self._engine,
                    self._isolation,
                )
                snapshot = self._read_validated_snapshot()
                _assert_real_replay_execution_envelopes(
                    self._engine,
                    snapshot=snapshot,
                    policy=self._policy,
                    inputs=self._inputs,
                )
                if (
                    snapshot.actual_accepted_target_count
                    == EXPECTED_TARGET_COUNT
                ):
                    return self._result(
                        snapshot,
                        status="complete",
                        dispatched_scheme_ids=tuple(dispatched),
                    )
                observed_at = _now_utc()
                if observed_at >= self._cutoff_at:
                    return self._result(
                        snapshot,
                        status="cutoff_incomplete",
                        dispatched_scheme_ids=tuple(dispatched),
                    )
                unexpected_running = tuple(
                    sorted(
                        summary.item.base_scheme_id
                        for summary in snapshot.items
                        if summary.item.state
                        in {"RUNNING", "ABANDONED"}
                    )
                )
                if unexpected_running:
                    return self._result(
                        snapshot,
                        status="recovery_blocked",
                        dispatched_scheme_ids=tuple(dispatched),
                        blocked_scheme_ids=unexpected_running,
                    )
                candidates, cache_blocked, earliest_release = (
                    self._eligible_replay_candidates(
                        snapshot,
                        observed_at=observed_at,
                    )
                )
                if not candidates:
                    if earliest_release is not None:
                        _wait_for_release(
                            min(
                                1.0,
                                max(
                                    0.0,
                                    (
                                        min(
                                            earliest_release,
                                            self._cutoff_at,
                                        )
                                        - observed_at
                                    ).total_seconds(),
                                ),
                            )
                        )
                        continue
                    return self._result(
                        snapshot,
                        status="incomplete",
                        dispatched_scheme_ids=tuple(dispatched),
                        blocked_scheme_ids=cache_blocked,
                    )
                scheme_id = candidates[0]
                if scheme_id in dispatched:
                    return self._result(
                        snapshot,
                        status="integrity_error",
                        dispatched_scheme_ids=tuple(dispatched),
                        blocked_scheme_ids=(scheme_id,),
                    )
                item = next(
                    summary.item
                    for summary in snapshot.items
                    if summary.item.base_scheme_id == scheme_id
                )
                decision = self._governor.can_start(
                    running_scheme_ids=(),
                    candidate_scheme_id=scheme_id,
                )
                if not decision.allowed:
                    raise DailyRealReplayError(
                        "real replay resource governor rejected "
                        f"{scheme_id}: {decision.reason}"
                    )
                _recheck_real_replay_database(
                    self._engine,
                    self._isolation,
                )
                if _now_utc() >= self._cutoff_at:
                    return self._result(
                        snapshot,
                        status="cutoff_incomplete",
                        dispatched_scheme_ids=tuple(dispatched),
                    )
                result = self._execute_owned_item(
                    item_id=int(item.item_id)
                )
                dispatched.append(scheme_id)
                status = str(getattr(result, "status", ""))
                if status in {
                    "claim_rejected",
                    "stale_rejected",
                    "fenced_pending_cleanup",
                    "recovery_blocked",
                }:
                    return self._result(
                        self._read_validated_snapshot(),
                        status="recovery_blocked",
                        dispatched_scheme_ids=tuple(dispatched),
                        blocked_scheme_ids=(scheme_id,),
                    )
                if status not in {
                    "success",
                    "failed",
                    "retry_wait",
                }:
                    return self._result(
                        self._read_validated_snapshot(),
                        status="integrity_error",
                        dispatched_scheme_ids=tuple(dispatched),
                        blocked_scheme_ids=(scheme_id,),
                    )
        finally:
            self._owner_active = False
            lock.release()

    def _execute_owned_item(self, *, item_id: int) -> Any:
        if not self._owner_active:
            raise DailyRealReplayError(
                "real replay item execution requires the owner lock"
            )
        _recheck_real_replay_database(self._engine, self._isolation)
        if _now_utc() >= self._cutoff_at:
            raise DailyRealReplayError(
                "real replay execution reached the frozen Shanghai cutoff"
            )
        result = scheduled_executor.execute_scheduled_item(
            self._engine,
            item_id=int(item_id),
            trigger_origin="operator_recovery",
        )
        _recheck_real_replay_database(self._engine, self._isolation)
        return result

    def _read_validated_snapshot(self) -> Any:
        snapshot = _repository_read_schedule_occurrence_snapshot(
            self._engine,
            occurrence_id=self._occurrence_id,
        )
        _assert_real_replay_runtime_snapshot(
            snapshot,
            occurrence_id=self._occurrence_id,
            policy=self._policy,
            configs=self._configs,
            inputs=self._inputs,
        )
        try:
            assert_daily_coordinator_epoch_matches_policy(
                snapshot.occurrence.policy_json,
                engine=self._engine,
            )
        except RuntimeError as exc:
            raise DailyRealReplayError(str(exc)) from exc
        if hasattr(self, "_cutoff_at"):
            observed_cutoff = _stored_utc_datetime(
                snapshot.occurrence.recovery_cutoff_at,
                field="recovery_cutoff_at",
            )
            if observed_cutoff != self._cutoff_at:
                raise DailyRealReplayError(
                    "real replay runtime cutoff drifted"
                )
        return snapshot

    def _eligible_replay_candidates(
        self,
        snapshot: Any,
        *,
        observed_at: datetime,
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        datetime | None,
    ]:
        state_by_scheme = {
            summary.item.base_scheme_id: summary.item.state
            for summary in snapshot.items
        }
        prewarmer_by_group = _cache_prewarmer_by_group(self._policy)
        candidates: list[str] = []
        blocked: list[str] = []
        future_releases: list[datetime] = []
        for summary in snapshot.items:
            item = summary.item
            if (
                item.state != "PENDING"
                or int(item.attempt_no) != 0
                or item.current_run_id is not None
            ):
                continue
            release_at = _stored_utc_datetime(
                item.release_at,
                field="release_at",
            )
            if release_at > observed_at:
                future_releases.append(release_at)
                continue
            scheme_policy = self._policy.schemes[
                item.base_scheme_id
            ]
            if not scheme_policy.cache_prerequisite:
                prewarmer = prewarmer_by_group.get(
                    scheme_policy.cache_group
                )
                if (
                    prewarmer is not None
                    and state_by_scheme.get(prewarmer) != "SUCCESS"
                ):
                    blocked.append(item.base_scheme_id)
                    continue
            candidates.append(item.base_scheme_id)
        return (
            dispatch_order(self._policy, candidates),
            tuple(sorted(blocked)),
            min(future_releases) if future_releases else None,
        )

    def _owner_lock_path(self) -> Path:
        return (
            _real_replay_lock_root()
            / "real-replay-runtime.lock"
        ).resolve(strict=False)

    def _result(
        self,
        snapshot: Any,
        *,
        status: str,
        dispatched_scheme_ids: tuple[str, ...] = (),
        blocked_scheme_ids: tuple[str, ...] = (),
    ) -> RealReplayRuntimeResult:
        failed = tuple(
            sorted(
                summary.item.base_scheme_id
                for summary in snapshot.items
                if summary.item.state
                in {"FAILED_TERMINAL", "RETRY_WAIT", "EXPIRED"}
            )
        )
        return RealReplayRuntimeResult(
            occurrence_id=self._occurrence_id,
            status=status,
            accepted_target_count=int(
                snapshot.actual_accepted_target_count
            ),
            expected_target_count=EXPECTED_TARGET_COUNT,
            dispatched_scheme_ids=dispatched_scheme_ids,
            failed_scheme_ids=failed,
            blocked_scheme_ids=blocked_scheme_ids,
            cutoff_at=self._cutoff_at,
        )


def verify_real_replay_database(
    engine: Any,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
    expected_port: int,
    expected_private_root: str | Path,
) -> DailyRealReplayDatabaseIdentity:
    """验证并保护真实联跑专用的 loopback 临时 MySQL Engine。"""
    try:
        return verify_and_register_isolated_daily_database(
            engine,
            expected_database_name=expected_database_name,
            expected_server_uuid=expected_server_uuid,
            expected_port=expected_port,
            expected_private_root=expected_private_root,
            connection_marker_key=REAL_REPLAY_SCHEMA_VERSION,
        )
    except RuntimeError as exc:
        raise DailyRealReplayError(str(exc)) from exc


def open_real_replay_generations(
    *,
    native_manifest: str | Path,
    databridge_manifest: str | Path,
    databridge_schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> DailyRealReplayInputs:
    """重新校验两份不可变 manifest，并验证 DataBridge 的 Native 父关系。"""
    native = open_native_generation(Path(native_manifest).resolve())
    databridge = open_databridge_generation(
        Path(databridge_manifest).resolve(),
        schema_path=Path(databridge_schema_path).resolve(),
    )
    drift: list[str] = []
    if databridge.native_generation_id != native.generation_id:
        drift.append("generation_id")
    if databridge.native_manifest_sha256 != native.manifest_sha256:
        drift.append("manifest_sha256")
    if databridge.business_date != native.business_date:
        drift.append("business_date")
    if databridge.feature_date != native.feature_date:
        drift.append("feature_date")
    if drift:
        raise DailyRealReplayError(
            "DataBridge Native parent mismatch: " + ", ".join(drift)
        )
    return DailyRealReplayInputs(
        native_generation=native,
        databridge_generation=databridge,
        business_date=native.business_date,
        feature_date=native.feature_date,
    )


def bind_real_replay_epoch(
    engine: Any,
    *,
    isolation: DailyRealReplayDatabaseIdentity,
    epoch_payload: Mapping[str, object],
) -> DailyCoordinatorEpochIdentity:
    """把 replay epoch 仅绑定到当前受保护临时 MySQL Engine。"""
    _recheck_real_replay_database(engine, isolation)
    return bind_isolated_daily_coordinator_epoch(
        engine,
        frozen=dict(epoch_payload),
        database_name=isolation.database_name,
        server_uuid=isolation.server_uuid,
        isolation=isolation,
        connection_marker_key=REAL_REPLAY_SCHEMA_VERSION,
    )


def create_real_replay_occurrence(
    engine: Any,
    *,
    isolation: DailyRealReplayDatabaseIdentity,
    policy: Any,
    configs: Mapping[str, Any],
    inputs: DailyRealReplayInputs,
    schedule_key: str,
    opened_at: datetime,
    epoch_payload: Mapping[str, object],
) -> int:
    """只经受保护 Engine 创建不冒充 SLA/容量证据的 occurrence。"""
    _recheck_real_replay_database(engine, isolation)
    return int(
        _repository_create_schedule_occurrence(
            engine,
            **_build_real_replay_occurrence_args(
                policy=policy,
                configs=configs,
                inputs=inputs,
                schedule_key=schedule_key,
                opened_at=opened_at,
                epoch_payload=epoch_payload,
            ),
        )
    )


def _build_real_replay_occurrence_args(
    *,
    policy: Any,
    configs: Mapping[str, Any],
    inputs: DailyRealReplayInputs,
    schedule_key: str,
    opened_at: datetime,
    epoch_payload: Mapping[str, object],
) -> dict[str, object]:
    normalized_opened_at = _aware_utc(opened_at)
    normalized_schedule_key = (
        schedule_key.strip() if isinstance(schedule_key, str) else ""
    )
    if not normalized_schedule_key.startswith(
        REAL_REPLAY_SCHEDULE_PREFIX
    ):
        raise DailyRealReplayError(
            "real replay schedule namespace must use "
            f"{REAL_REPLAY_SCHEDULE_PREFIX}*"
        )
    if set(configs) != set(policy.schemes):
        raise DailyRealReplayError(
            "real replay config identities differ from policy"
        )
    if len(policy.schemes) != EXPECTED_ITEM_COUNT:
        raise DailyRealReplayError(
            "real replay policy does not contain 21 items"
        )

    calendar = FrozenCalendarService(inputs.native_generation)
    if (
        calendar.previous_trading_day(inputs.business_date)
        != inputs.feature_date
    ):
        raise DailyRealReplayError(
            "real replay business/feature date is not a trading-day pair"
        )

    target_dates: dict[str, str] = {}
    item_policy_by_base: dict[str, dict[str, object]] = {}
    release_offsets = [
        int(item.v2_release_offset_min or 0)
        for item in policy.schemes.values()
        if item.runtime_type == "blackbox_v2"
    ]
    if sorted(release_offsets) != [0, 2, 4, 6]:
        raise DailyRealReplayError(
            "real replay V2 release offsets differ from 0/2/4/6"
        )
    release_base = normalized_opened_at - timedelta(
        minutes=max(release_offsets)
    )
    replay_cutoff = _next_shanghai_midnight(normalized_opened_at)

    for scheme_id, scheme_policy in policy.schemes.items():
        config = configs[scheme_id]
        target_date = calendar.nth_trading_day_after(
            inputs.feature_date,
            int(scheme_policy.horizon),
        )
        for tenor in scheme_policy.target_tenors:
            registry_id = (
                f"{scheme_id}__h{int(scheme_policy.horizon)}__{tenor}"
            )
            target_dates[registry_id] = target_date
        release_offset = int(
            scheme_policy.v2_release_offset_min or 0
        )
        release_at = (
            release_base + timedelta(minutes=release_offset)
            if scheme_policy.runtime_type == "blackbox_v2"
            else normalized_opened_at
        )
        item_policy_by_base[scheme_id] = {
            "scheme_version": config.scheme_version,
            "code_sha256": config.code_hash,
            "config_sha256": config.config_hash,
            "cache_group": scheme_policy.cache_group,
            "resource_class": scheme_policy.resource_class,
            "internal_workers": scheme_policy.internal_workers,
            "release_offset_minutes": release_offset,
            "release_at": release_at,
            "deadline_at": replay_cutoff,
        }

    if len(target_dates) != EXPECTED_TARGET_COUNT:
        raise DailyRealReplayError(
            "real replay policy does not contain 25 targets"
        )
    policy_json = _policy_payload(
        policy,
        daily_coordinator_epoch=epoch_payload,
    )
    input_modes = _input_mode_counts(policy_json)
    if (
        set(input_modes)
        - {
            "generation_v1",
            "live_source_0629",
            "databridge_v1",
        }
        or input_modes.get("databridge_v1", 0) != EXPECTED_V2_COUNT
        or (
            input_modes.get("generation_v1", 0)
            + input_modes.get("live_source_0629", 0)
        )
        != EXPECTED_NATIVE_COUNT
    ):
        raise DailyRealReplayError(
            "real replay input compatibility matrix drifted: "
            f"{input_modes}"
        )
    _assert_input_compatibility_identities(
        policy_json,
        expected_policy_schemes=policy.schemes,
        expected_configs=configs,
    )
    cache_scheme_ids = sorted(
        str(row["scheme_id"])
        for row in policy_json["schemes"]
        if row.get("cache_spec_fingerprint") is not None
    )
    for row in policy_json["schemes"]:
        row["cache_spec_fingerprint"] = None
    policy_json["real_replay_projection"] = {
        "schema_version": REAL_REPLAY_SCHEMA_VERSION,
        "purpose": "isolated_21_item_25_target_function_replay",
        "algorithm_execution":
            "real_17_native_plus_4_blackbox_v2",
        "persistence": "isolated_mysql_only",
        "capacity_qualification": "EXCLUDED",
        "sla_qualification": "EXCLUDED",
        "cache_completion_qualification": "EXCLUDED",
        "exclusion_reason": "FUNCTION_REPLAY_NOT_PRODUCTION_ADMISSION",
        "excluded_cache_scheme_ids": cache_scheme_ids,
        "native_generation_id":
            inputs.native_generation.generation_id,
        "native_manifest_sha256":
            inputs.native_generation.manifest_sha256,
        "databridge_generation_id":
            inputs.databridge_generation.generation_id,
        "databridge_manifest_sha256":
            inputs.databridge_generation.manifest_sha256,
    }
    return {
        "schedule_key": normalized_schedule_key,
        "predict_date": inputs.business_date,
        "feature_date": inputs.feature_date,
        "target_dates": target_dates,
        "item_policy_by_base": item_policy_by_base,
        "policy_version": policy.version,
        "policy_json": policy_json,
        "sla_deadline_at": replay_cutoff,
        "recovery_cutoff_at": replay_cutoff,
    }


def register_real_replay_generations(
    engine: Any,
    *,
    occurrence_id: int,
    inputs: DailyRealReplayInputs,
    isolation: DailyRealReplayDatabaseIdentity,
) -> dict[str, tuple[str, int]]:
    """按 Native 父、DataBridge 子顺序登记并绑定真实 manifest。"""
    _recheck_real_replay_database(engine, isolation)
    native = inputs.native_generation
    databridge = inputs.databridge_generation
    native_binding = _repository_register_generation(
        engine,
        occurrence_id=int(occurrence_id),
        generation_id=native.generation_id,
        generation_type=native.generation_type,
        business_date=native.business_date,
        feature_date=native.feature_date,
        readiness_basis=native.readiness_basis,
        source_commit_token=native.source_commit_token,
        dataset_content_id=native.dataset_content_id,
        schema_version=native.schema_version,
        exporter_version=native.exporter_version,
        manifest_uri=str(native.manifest_path),
        manifest_sha256=native.manifest_sha256,
        expected_feature_date=inputs.feature_date,
    )
    databridge_binding = _repository_register_generation(
        engine,
        occurrence_id=int(occurrence_id),
        generation_id=databridge.generation_id,
        generation_type=databridge.generation_type,
        business_date=databridge.business_date,
        feature_date=databridge.feature_date,
        readiness_basis=databridge.readiness_basis,
        source_commit_token=databridge.source_commit_token,
        dataset_content_id=databridge.dataset_content_id,
        schema_version=databridge.schema_version,
        exporter_version=databridge.exporter_version,
        manifest_uri=str(databridge.manifest_path),
        manifest_sha256=databridge.manifest_sha256,
        native_generation_id=native.generation_id,
        native_manifest_sha256=native.manifest_sha256,
        expected_feature_date=inputs.feature_date,
    )
    expected = {
        "native_source": (native.generation_id, 17),
        "databridge_v1": (databridge.generation_id, 4),
    }
    actual = {
        "native_source": native_binding,
        "databridge_v1": databridge_binding,
    }
    if actual != expected:
        raise DailyRealReplayError(
            "real replay generation binding cardinality drifted: "
            f"{actual}"
        )
    return actual


def _recheck_real_replay_database(
    engine: Any,
    isolation: DailyRealReplayDatabaseIdentity,
) -> None:
    try:
        recheck_verified_isolated_daily_database(engine, isolation)
    except RuntimeError as exc:
        raise DailyRealReplayError(str(exc)) from exc


def _assert_real_replay_runtime_snapshot(
    snapshot: Any,
    *,
    occurrence_id: int,
    policy: Any,
    configs: Mapping[str, Any],
    inputs: DailyRealReplayInputs,
) -> None:
    """拒绝任何会把功能回放冒充 SLA/容量证据的冻结账本。"""
    occurrence = getattr(snapshot, "occurrence", None)
    if (
        occurrence is None
        or int(getattr(occurrence, "occurrence_id", 0))
        != int(occurrence_id)
        or not str(getattr(occurrence, "schedule_key", "")).startswith(
            REAL_REPLAY_SCHEDULE_PREFIX
        )
    ):
        raise DailyRealReplayError(
            "real replay runtime occurrence identity drifted"
        )
    if (
        int(getattr(occurrence, "expected_item_count", -1))
        != EXPECTED_ITEM_COUNT
        or int(getattr(occurrence, "expected_target_count", -1))
        != EXPECTED_TARGET_COUNT
        or int(getattr(snapshot, "actual_item_count", -1))
        != EXPECTED_ITEM_COUNT
        or int(getattr(snapshot, "actual_target_count", -1))
        != EXPECTED_TARGET_COUNT
        or len(tuple(getattr(snapshot, "items", ())))
        != EXPECTED_ITEM_COUNT
    ):
        raise DailyRealReplayError(
            "real replay runtime ledger cardinality drifted"
        )
    policy_json = getattr(occurrence, "policy_json", None)
    projection = (
        policy_json.get("real_replay_projection")
        if isinstance(policy_json, Mapping)
        else None
    )
    expected_projection = {
        "schema_version": REAL_REPLAY_SCHEMA_VERSION,
        "purpose": "isolated_21_item_25_target_function_replay",
        "algorithm_execution":
            "real_17_native_plus_4_blackbox_v2",
        "persistence": "isolated_mysql_only",
        "capacity_qualification": "EXCLUDED",
        "sla_qualification": "EXCLUDED",
        "cache_completion_qualification": "EXCLUDED",
        "exclusion_reason":
            "FUNCTION_REPLAY_NOT_PRODUCTION_ADMISSION",
        "native_generation_id":
            inputs.native_generation.generation_id,
        "native_manifest_sha256":
            inputs.native_generation.manifest_sha256,
        "databridge_generation_id":
            inputs.databridge_generation.generation_id,
        "databridge_manifest_sha256":
            inputs.databridge_generation.manifest_sha256,
    }
    if not isinstance(projection, Mapping):
        raise DailyRealReplayError(
            "real replay runtime qualification is missing"
        )
    drift = sorted(
        field
        for field, expected in expected_projection.items()
        if projection.get(field) != expected
    )
    if drift:
        raise DailyRealReplayError(
            "real replay runtime qualification drifted: "
            + ", ".join(drift)
        )
    policy_text = json.dumps(
        dict(policy_json),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        getattr(occurrence, "policy_sha256", None)
        != hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
    ):
        raise DailyRealReplayError(
            "real replay runtime policy digest drifted"
        )

    if (
        str(getattr(occurrence, "policy_version", ""))
        != str(policy.version)
        or set(configs) != set(policy.schemes)
        or str(getattr(occurrence, "predict_date", ""))
        != inputs.business_date
        or str(getattr(occurrence, "feature_date", ""))
        != inputs.feature_date
    ):
        raise DailyRealReplayError(
            "real replay runtime policy or generation dates drifted"
        )
    frozen_policy = dict(policy_json)
    frozen_policy.pop("daily_coordinator_epoch", None)
    frozen_policy.pop("real_replay_projection", None)
    expected_policy = _policy_payload(policy)
    for row in expected_policy["schemes"]:
        row["cache_spec_fingerprint"] = None
    if frozen_policy != expected_policy:
        raise DailyRealReplayError(
            "real replay runtime frozen policy drifted"
        )

    cutoff_at = _stored_utc_datetime(
        occurrence.recovery_cutoff_at,
        field="recovery_cutoff_at",
    )
    if (
        _stored_utc_datetime(
            occurrence.sla_deadline_at,
            field="sla_deadline_at",
        )
        != cutoff_at
        or getattr(occurrence, "sla_outcome", None) != "PENDING"
        or getattr(occurrence, "sla_evaluated_at", None) is not None
        or getattr(
            occurrence,
            "sla_accepted_target_count",
            None,
        )
        is not None
    ):
        raise DailyRealReplayError(
            "real replay runtime SLA exclusion state drifted"
        )

    expected_native_id = inputs.native_generation.generation_id
    expected_databridge_id = (
        inputs.databridge_generation.generation_id
    )
    item_ids: list[int] = []
    item_scheme_ids: list[str] = []
    target_total = 0
    accepted_total = 0
    observed_state_counts: dict[str, int] = {}
    for summary in snapshot.items:
        item = getattr(summary, "item", None)
        scheme_id = str(getattr(item, "base_scheme_id", ""))
        scheme_policy = policy.schemes.get(scheme_id)
        config = configs.get(scheme_id)
        if (
            item is None
            or int(getattr(item, "occurrence_id", 0))
            != int(occurrence_id)
            or scheme_policy is None
            or config is None
        ):
            raise DailyRealReplayError(
                "real replay runtime item occurrence drifted"
            )
        item_ids.append(int(getattr(item, "item_id", 0)))
        item_scheme_ids.append(scheme_id)
        if (
            str(getattr(item, "runtime_type", ""))
            != scheme_policy.runtime_type
            or str(getattr(item, "scheme_version", ""))
            != config.scheme_version
            or str(getattr(item, "code_sha256", ""))
            != config.code_hash
            or str(getattr(item, "config_sha256", ""))
            != config.config_hash
            or str(getattr(item, "cache_group", ""))
            != scheme_policy.cache_group
            or str(getattr(item, "resource_class", ""))
            != scheme_policy.resource_class
            or int(getattr(item, "internal_workers", -1))
            != int(scheme_policy.internal_workers)
            or int(
                getattr(item, "release_offset_minutes", -1)
            )
            != int(scheme_policy.v2_release_offset_min or 0)
        ):
            raise DailyRealReplayError(
                "real replay runtime item policy identity drifted"
            )
        expected_generation_id = (
            expected_databridge_id
            if scheme_policy.runtime_type == "blackbox_v2"
            else expected_native_id
        )
        if item.input_generation_id != expected_generation_id:
            raise DailyRealReplayError(
                "real replay runtime item generation identity drifted"
            )
        if (
            _stored_utc_datetime(
                item.deadline_at,
                field="item.deadline_at",
            )
            != cutoff_at
            or _stored_utc_datetime(
                item.recovery_cutoff_at,
                field="item.recovery_cutoff_at",
            )
            != cutoff_at
            or _stored_utc_datetime(
                item.occurrence_sla_deadline_at,
                field="item.occurrence_sla_deadline_at",
            )
            != cutoff_at
            or _stored_utc_datetime(
                item.release_at,
                field="item.release_at",
            )
            >= cutoff_at
        ):
            raise DailyRealReplayError(
                "real replay runtime item cutoff drifted"
            )
        state = str(getattr(item, "state", ""))
        attempt_no = int(getattr(item, "attempt_no", -1))
        current_run_id = getattr(item, "current_run_id", None)
        if (
            state == "PENDING"
            and (attempt_no != 0 or current_run_id is not None)
        ) or (
            state != "PENDING"
            and (attempt_no != 1 or current_run_id is None)
        ):
            raise DailyRealReplayError(
                "real replay runtime item attempt fence drifted"
            )
        target_count = int(getattr(summary, "target_count", -1))
        accepted_count = int(
            getattr(summary, "accepted_target_count", -1)
        )
        if target_count <= 0 or accepted_count < 0:
            raise DailyRealReplayError(
                "real replay runtime target cardinality drifted"
            )
        if (
            state == "SUCCESS"
            and accepted_count != target_count
        ) or (
            state != "SUCCESS"
            and accepted_count != 0
        ):
            raise DailyRealReplayError(
                "real replay runtime target receipt state drifted"
            )
        item_sla_status = getattr(item, "sla_status", None)
        item_sla_evaluated_at = getattr(
            item,
            "sla_evaluated_at",
            None,
        )
        late_reason = getattr(item, "late_reason", None)
        if scheme_policy.runtime_type == "native_adapter":
            item_sla_valid = (
                item_sla_status == "PENDING"
                and item_sla_evaluated_at is None
                and late_reason is None
            )
        else:
            item_sla_valid = (
                item_sla_status == "PENDING"
                and item_sla_evaluated_at is None
                and late_reason is None
            ) or (
                item_sla_status == "ON_TIME"
                and isinstance(item_sla_evaluated_at, datetime)
                and late_reason is None
            ) or (
                item_sla_status == "LATE"
                and isinstance(item_sla_evaluated_at, datetime)
                and late_reason
                in {
                    "V2_STARTED_AFTER_0745",
                    "V2_NOT_STARTED_BY_0745",
                }
            )
        if not item_sla_valid:
            raise DailyRealReplayError(
                "real replay runtime item SLA state drifted"
            )
        target_total += target_count
        accepted_total += accepted_count
        observed_state_counts[state] = (
            observed_state_counts.get(state, 0) + 1
        )
    if (
        any(item_id <= 0 for item_id in item_ids)
        or len(item_ids) != len(set(item_ids))
        or set(item_scheme_ids) != set(policy.schemes)
        or len(item_scheme_ids) != len(set(item_scheme_ids))
    ):
        raise DailyRealReplayError(
            "real replay runtime item identities drifted"
        )
    if (
        target_total != EXPECTED_TARGET_COUNT
        or accepted_total
        != int(snapshot.actual_accepted_target_count)
        or accepted_total
        != int(getattr(occurrence, "accepted_target_count", -1))
        or tuple(sorted(observed_state_counts.items()))
        != tuple(snapshot.item_state_counts)
    ):
        raise DailyRealReplayError(
            "real replay runtime target cardinality drifted"
        )
    if (
        accepted_total == EXPECTED_TARGET_COUNT
        and getattr(occurrence, "completion_state", None) != "SUCCESS"
    ) or (
        accepted_total != EXPECTED_TARGET_COUNT
        and getattr(occurrence, "completion_state", None)
        not in {"PENDING", "RUNNING", "FAILED"}
    ):
        raise DailyRealReplayError(
            "real replay runtime completion state drifted"
        )


def _stored_utc_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise DailyRealReplayError(
            f"real replay {field} must be a datetime"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _assert_deployed_real_replay_definitions(
    policy: Any,
    *,
    configs: Mapping[str, Any],
) -> None:
    """要求 caller 传入的定义就是当前仓库严格 discovery 的结果。"""
    discovered = tuple(discover_schemes(strict=True))
    deployed_policy = load_daily_policy(discovered=discovered)
    deployed_configs = {
        config.scheme_id: config
        for config in discovered
        if config.status == "active" and config.frequency == "daily"
    }
    if policy != deployed_policy or dict(configs) != deployed_configs:
        raise DailyRealReplayError(
            "real replay runtime definitions differ from deployed policy"
        )


def _assert_real_replay_execution_envelopes(
    engine: Any,
    *,
    snapshot: Any,
    policy: Any,
    inputs: DailyRealReplayInputs,
) -> None:
    """以逐 item 严格信封闭合 25 个 target 与 generation 身份。"""
    calendar = FrozenCalendarService(inputs.native_generation)
    frozen_rows: list[dict[str, object]] = []
    native_release_times = {
        _stored_utc_datetime(
            summary.item.release_at,
            field="native.release_at",
        )
        for summary in snapshot.items
        if summary.item.runtime_type == "native_adapter"
    }
    if len(native_release_times) != 1:
        raise DailyRealReplayError(
            "real replay Native initial release identity drifted"
        )
    initial_opened_at = next(iter(native_release_times))
    max_v2_offset = max(
        int(item.v2_release_offset_min or 0)
        for item in policy.schemes.values()
        if item.runtime_type == "blackbox_v2"
    )
    for summary in snapshot.items:
        item = summary.item
        envelope = _repository_read_schedule_execution_envelope(
            engine,
            item_id=int(item.item_id),
        )
        scheme_id = str(item.base_scheme_id)
        scheme_policy = policy.schemes[scheme_id]
        expected_generation = (
            inputs.databridge_generation
            if scheme_policy.runtime_type == "blackbox_v2"
            else inputs.native_generation
        )
        if (
            envelope.occurrence != snapshot.occurrence
            or envelope.item != item
            or envelope.generation.generation_id
            != expected_generation.generation_id
            or envelope.generation.manifest_sha256
            != expected_generation.manifest_sha256
            or envelope.generation.business_date
            != inputs.business_date
            or envelope.generation.feature_date
            != inputs.feature_date
            or envelope.generation.state != "SEALED"
            or envelope.generation.sealed_at is None
            or envelope.calendar_generation.generation_id
            != inputs.native_generation.generation_id
            or envelope.calendar_generation.manifest_sha256
            != inputs.native_generation.manifest_sha256
            or envelope.calendar_generation.state != "SEALED"
            or envelope.calendar_generation.sealed_at is None
        ):
            raise DailyRealReplayError(
                "real replay execution envelope generation drifted: "
                f"{scheme_id}"
            )
        initial_release_at = (
            initial_opened_at
            - timedelta(minutes=max_v2_offset)
            + timedelta(
                minutes=int(
                    scheme_policy.v2_release_offset_min or 0
                )
            )
            if scheme_policy.runtime_type == "blackbox_v2"
            else initial_opened_at
        )
        expected_release_at = (
            max(
                initial_release_at,
                _stored_utc_datetime(
                    envelope.generation.sealed_at,
                    field="databridge ledger sealed_at",
                )
                + timedelta(
                    minutes=int(
                        scheme_policy.v2_release_offset_min or 0
                    )
                ),
            )
            if scheme_policy.runtime_type == "blackbox_v2"
            else initial_release_at
        )
        if (
            _stored_utc_datetime(
                item.release_at,
                field="item.release_at",
            )
            != expected_release_at
        ):
            raise DailyRealReplayError(
                "real replay execution envelope release drifted: "
                f"{scheme_id}"
            )
        expected_target_date = calendar.nth_trading_day_after(
            inputs.feature_date,
            int(scheme_policy.horizon),
        )
        expected_targets = {
            (
                f"{scheme_id}__h{int(scheme_policy.horizon)}__{tenor}",
                scheme_id,
                scheme_policy.runtime_type,
                scheme_policy.task_type,
                tenor,
                int(scheme_policy.horizon),
                expected_target_date,
            )
            for tenor in scheme_policy.target_tenors
        }
        actual_targets = {
            (
                target.registry_scheme_id,
                target.base_scheme_id,
                target.runtime_type,
                target.task_type,
                target.target_tenor,
                int(target.horizon),
                target.target_date,
            )
            for target in envelope.targets
        }
        if (
            actual_targets != expected_targets
            or int(summary.target_count) != len(expected_targets)
        ):
            raise DailyRealReplayError(
                "real replay execution envelope target identity drifted: "
                f"{scheme_id}"
            )
        for target in envelope.targets:
            frozen_rows.append(
                {
                    "status": "active",
                    "frequency": "daily",
                    "scheme_id": target.registry_scheme_id,
                    "base_scheme_id": scheme_id,
                    "runtime_type": scheme_policy.runtime_type,
                    "task_type": target.task_type,
                    "target_tenor": target.target_tenor,
                    "horizon": int(target.horizon),
                    "target_date": target.target_date,
                    "scheme_version": item.scheme_version,
                    "code_sha256": item.code_sha256,
                    "config_sha256": item.config_sha256,
                    "cache_group": item.cache_group,
                    "resource_class": item.resource_class,
                    "internal_workers": int(item.internal_workers),
                    "release_offset_minutes":
                        int(item.release_offset_minutes),
                    "release_at": initial_release_at
                    .replace(tzinfo=None)
                    .isoformat(sep=" "),
                    "deadline_at": _stored_utc_datetime(
                        item.deadline_at,
                        field="item.deadline_at",
                    )
                    .replace(tzinfo=None)
                    .isoformat(sep=" "),
                }
            )
    try:
        registry_digest = freeze_active_daily_registry(
            frozen_rows
        ).registry_digest
    except ValueError as exc:
        raise DailyRealReplayError(str(exc)) from exc
    if registry_digest != snapshot.occurrence.registry_digest:
        raise DailyRealReplayError(
            "real replay execution envelope registry digest drifted"
        )


def _now_utc() -> datetime:
    """采样真实 wall clock；仅保留私有测试 patch seam。"""
    return datetime.now(timezone.utc)


def _wait_for_release(seconds: float) -> None:
    """只为冻结 release 做至多一秒的可重入等待。"""
    threading.Event().wait(max(0.0, min(float(seconds), 1.0)))


def _real_replay_lock_root() -> Path:
    """返回所有隔离回放 owner 共用、与临时 MySQL 无关的私有锁目录。"""
    candidate = (
        resolve_daily_runtime_root()
        / "isolated-real-replay-locks"
    )
    if candidate.is_symlink():
        raise DailyRealReplayError(
            "real replay machine-global lock root is unsafe"
        )
    root = candidate.resolve(strict=False)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise DailyRealReplayError(
            "real replay machine-global lock root is unsafe"
        )
    return root


def _iso_utc_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DailyRealReplayError(
            f"real replay {field} must be an ISO datetime"
        )
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DailyRealReplayError(
            f"real replay {field} must be an ISO datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailyRealReplayError(
            f"real replay {field} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _cache_prewarmer_by_group(policy: Any) -> dict[str, str]:
    """为声明 consumer 的 cache group 找到唯一 prewarmer。"""
    members_by_group: dict[str, list[Any]] = {}
    for item in policy.schemes.values():
        if item.runtime_type != "native_adapter":
            continue
        members_by_group.setdefault(item.cache_group, []).append(item)
    result: dict[str, str] = {}
    for cache_group, members in members_by_group.items():
        if len(members) < 2:
            continue
        prewarmers = [
            item.scheme_id
            for item in members
            if item.cache_prerequisite
        ]
        if len(prewarmers) != 1:
            raise DailyRealReplayError(
                "real replay cache prerequisite topology drifted: "
                f"{cache_group}"
            )
        result[cache_group] = prewarmers[0]
    return result


def _input_mode_counts(
    policy_json: Mapping[str, object],
) -> dict[str, int]:
    rows = policy_json.get("schemes")
    if not isinstance(rows, list):
        raise DailyRealReplayError(
            "real replay policy schemes are unavailable"
        )
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise DailyRealReplayError(
                "real replay policy scheme row is invalid"
            )
        mode = row.get("input_compatibility")
        if not isinstance(mode, str) or not mode:
            raise DailyRealReplayError(
                "real replay input compatibility is invalid"
            )
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _assert_input_compatibility_identities(
    policy_json: Mapping[str, object],
    *,
    expected_policy_schemes: Mapping[str, Any],
    expected_configs: Mapping[str, Any],
) -> None:
    """要求 17 个 Native 的动态模式和四个 V2 身份完全一致。"""
    rows = policy_json.get("schemes")
    if not isinstance(rows, list):
        raise DailyRealReplayError(
            "real replay policy schemes are unavailable"
        )
    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    if len(normalized_rows) != len(rows):
        raise DailyRealReplayError(
            "real replay input compatibility identities are invalid"
        )
    row_ids = [row.get("scheme_id") for row in normalized_rows]
    if any(
        not isinstance(scheme_id, str)
        or not scheme_id
        or scheme_id != scheme_id.strip()
        for scheme_id in row_ids
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities are invalid"
        )
    canonical_ids = [str(scheme_id) for scheme_id in row_ids]
    expected_ids = set(expected_policy_schemes)
    if (
        any(
            not isinstance(scheme_id, str)
            or not scheme_id
            or scheme_id != scheme_id.strip()
            for scheme_id in (
                *expected_policy_schemes.keys(),
                *expected_configs.keys(),
            )
        )
        or len(canonical_ids) != len(set(canonical_ids))
        or set(canonical_ids) != expected_ids
        or expected_ids != set(expected_configs)
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    rows_by_id = {
        scheme_id: row
        for scheme_id, row in zip(
            canonical_ids,
            normalized_rows,
            strict=True,
        )
    }
    expected_v2_offsets = dict(
        EXPECTED_V2_RELEASE_OFFSETS_BY_SCHEME
    )
    expected_v2_ids = set(expected_v2_offsets)
    if not expected_v2_ids < expected_ids:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    for scheme_id in sorted(expected_ids):
        row = rows_by_id[scheme_id]
        policy_item = expected_policy_schemes[scheme_id]
        config = expected_configs[scheme_id]
        if (
            getattr(policy_item, "scheme_id", None) != scheme_id
            or getattr(config, "scheme_id", None) != scheme_id
            or row.get("runtime_type")
            != getattr(policy_item, "runtime_type", None)
            or row.get("runtime_type")
            != getattr(config, "runtime_type", None)
            or row.get("input_compatibility")
            != getattr(policy_item, "input_compatibility", None)
            or row.get("source_package_sha256")
            != getattr(policy_item, "source_package_sha256", None)
        ):
            raise DailyRealReplayError(
                "real replay input compatibility identities drifted"
            )
    actual_v2_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("runtime_type") == "blackbox_v2"
    }
    if actual_v2_ids != expected_v2_ids:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    offset_drift = False
    for scheme_id, expected_offset in expected_v2_offsets.items():
        observed_offset = rows_by_id[scheme_id].get(
            "v2_release_offset_min"
        )
        if (
            not isinstance(observed_offset, int)
            or isinstance(observed_offset, bool)
            or observed_offset != expected_offset
        ):
            offset_drift = True
    if offset_drift:
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    actual_live_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "live_source_0629"
    }
    expected_native_ids = expected_ids - expected_v2_ids
    actual_native_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("runtime_type") == "native_adapter"
    }
    actual_generation_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "generation_v1"
    }
    actual_databridge_ids = {
        scheme_id
        for scheme_id, row in rows_by_id.items()
        if row.get("input_compatibility") == "databridge_v1"
    }
    if (
        actual_native_ids != expected_native_ids
        or not (
            actual_live_ids
            <= set(APPROVED_0629_LIVE_SOURCE_SCHEMES)
        )
        or not (actual_live_ids <= expected_native_ids)
        or actual_generation_ids
        != expected_native_ids - actual_live_ids
        or actual_databridge_ids != expected_v2_ids
    ):
        raise DailyRealReplayError(
            "real replay input compatibility identities drifted"
        )
    for row in rows_by_id.values():
        mode = row.get("input_compatibility")
        source_sha256 = row.get("source_package_sha256")
        if mode == "live_source_0629":
            if (
                not isinstance(source_sha256, str)
                or len(source_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in source_sha256
                )
            ):
                raise DailyRealReplayError(
                    "real replay input compatibility identities drifted"
                )
        elif source_sha256 is not None:
            raise DailyRealReplayError(
                "real replay input compatibility identities drifted"
            )


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DailyRealReplayError(
            "real replay opened_at must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _next_shanghai_midnight(value: datetime) -> datetime:
    """返回给定时刻之后的下一个上海自然日零点（UTC）。"""
    local_date = value.astimezone(SHANGHAI_TIMEZONE).date()
    next_date = local_date + timedelta(days=1)
    return datetime.combine(
        next_date,
        time.min,
        tzinfo=SHANGHAI_TIMEZONE,
    ).astimezone(timezone.utc)
