"""日频 occurrence 的单机协调器运行时。

APScheduler 只调用本模块的一个入口；业务排队、generation fence、恢复和
SLA 控制均以冻结 occurrence ledger 为准。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time as wall_time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scheduler.alerts import AlertDispatcher, AlertEvent, AlertSettings
from scheduler.daily_coordinator import OccurrenceLockUnavailable
from scheduler.daily_coordinator import (
    ItemControlState,
    OccurrenceFileLock,
    plan_dispatches,
)
from scheduler.daily_policy import POLICY_V2_PATH, load_daily_policy
from scheduler.data_contract import (
    detect_late_source_writes,
    inspect_native_input_readiness,
)
from scheduler.discovery import discover_schemes
from scheduler.generation_registry import (
    register_databridge_generation,
    register_native_generation,
)
from scheduler.repository import (
    confirm_schedule_attempt_orphan_cleanup,
    create_engine_from_env,
    create_schedule_occurrence,
    evaluate_schedule_item_start_sla,
    evaluate_schedule_occurrence_target_sla,
    expire_schedule_items,
    fail_schedule_item_without_attempt,
    fence_current_schedule_attempt,
    finalize_reclaimed_generation_payload,
    read_sealed_input_generation,
    read_schedule_execution_envelope,
    read_schedule_occurrence_snapshot,
    reconcile_schedule_occurrence_visibility_receipts,
    resolve_reclaimable_generation_payloads,
    upsert_scheduler_heartbeat,
)
from scheduler.scheduled_executor import execute_scheduled_item
from scheduler.process_control import ProcessStartGuard
from shared.calendar_service import CalendarService, FrozenCalendarService
from shared.daily_coordinator_mode import (
    assert_daily_coordinator_epoch_matches_policy,
    require_current_daily_coordinator_identity,
    resolve_daily_runtime_root,
)
from shared.daily_storage_preflight import preflight_daily_storage
from shared.data_bridge.client import DataBridgeClient, DataBridgeClientConfig
from shared.data_bridge.authority import (
    resolve_databridge_continuity_authority_from_engine,
)
from shared.data_bridge.refresh import (
    DailyCoordinatorPublicationCapability,
    DataBridgeRefreshConfig,
    DataBridgeStore,
    check_current_dataset,
    run_full_refresh,
)
from shared.databridge_input_generation import (
    cleanup_databridge_generation_debris,
    create_databridge_generation,
    delete_reclaimable_databridge_generation,
    find_published_databridge_generation,
    open_databridge_generation,
    preflight_databridge_generation_storage,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    cleanup_native_generation_debris,
    create_native_generation,
    delete_reclaimable_native_generation,
    find_published_native_generation,
    open_native_generation,
    preflight_native_generation_storage,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_KEY = "critical-daily-signals-v1"
DAILY_RUNTIME_ROOT = resolve_daily_runtime_root()
LOCK_ROOT = DAILY_RUNTIME_ROOT / "occurrence-locks"
NATIVE_GENERATION_ROOT = (
    PROJECT_ROOT / "backtest_artifacts" / "input_generations" / "native"
)
DATABRIDGE_GENERATION_ROOT = (
    PROJECT_ROOT / "backtest_artifacts" / "input_generations" / "databridge"
)
DATABRIDGE_SCHEMA_PATH = (
    PROJECT_ROOT
    / "shared"
    / "blackbox_v2"
    / "data_bridge_v1_schema.json"
)
_SCHEDULE_EXECUTION_TOKEN_ENV = "BOND_SCHEDULE_EXECUTION_TOKEN"
_SAFE_EXECUTION_TOKEN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

@dataclass(frozen=True)
class OccurrenceInputs:
    """创建 occurrence 前冻结的日期、版本和 policy 输入。"""

    feature_date: str
    target_dates: Mapping[str, str]
    item_policy_by_base: Mapping[str, Mapping[str, object]]
    policy_json: Mapping[str, object]


@dataclass(frozen=True)
class GenerationBuildOutcome:
    """Native/DataBridge 两条输入路径的独立构建结果。"""

    native_generation: Any | None
    databridge_generation: Any | None
    native_error: str | None = None
    databridge_error: str | None = None

    @property
    def failed(self) -> bool:
        return self.native_error is not None or self.databridge_error is not None


@dataclass(frozen=True)
class DailyRuntimeResult:
    """一次协调器调用的稳定结果摘要。"""

    business_date: str
    occurrence_id: int | None
    status: str
    dispatched_scheme_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchdogResult:
    """一次隔离 watchdog 求值结果。"""

    business_date: str
    occurrence_id: int | None
    stage: str
    status: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class DataBridgeReadinessProjection:
    """06:55 DataBridge SEALED readiness 的确定性投影。"""

    status: str
    reason: str | None
    generation_id: str | None
    sealed_at: datetime | None
    guardrail_at: datetime


@dataclass(frozen=True)
class _DeadlineCatchupResult:
    """一次重入对 write-once deadline 投影的补偿结果。"""

    late_v2_scheme_ids: tuple[str, ...] = ()
    newly_late_v2_scheme_ids: tuple[str, ...] = ()
    target_sla_projection: Any | None = None


@dataclass(frozen=True)
class _DriveItemsResult:
    """dispatch loop 的退出原因；cleanup 阻断不得退化成普通 incomplete。"""

    status: str
    dispatched_scheme_ids: tuple[str, ...]


class _GenerationAvailability:
    """向单一 dispatch event loop 发布输入 generation 就绪状态。"""

    def __init__(
        self,
        allowed_resource_combinations: Any | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._outcome = GenerationBuildOutcome(None, None)
        self._done = False
        self._active_resource_classes: set[str] = set()
        self._allowed_resource_combinations = frozenset(
            tuple(sorted(str(value).strip() for value in combination))
            for combination in (allowed_resource_combinations or ())
        )

    def publish_native(self, context: Any) -> None:
        with self._lock:
            self._outcome = GenerationBuildOutcome(
                native_generation=context,
                databridge_generation=None,
            )

    def finish(self, outcome: GenerationBuildOutcome) -> None:
        with self._lock:
            self._outcome = outcome
            self._done = True

    @contextmanager
    def occupy(self, resource_class: str):
        """把输入构建阶段纳入与算法相同的驻留资源决策。"""
        normalized = str(resource_class).strip()
        if not normalized:
            raise ValueError("resource_class must be non-empty")
        with self._lock:
            if normalized in self._active_resource_classes:
                raise RuntimeError(
                    f"input resource is already occupied: {normalized}"
                )
            proposed = tuple(
                sorted(
                    self._active_resource_classes | {normalized}
                )
            )
            if proposed not in self._allowed_resource_combinations:
                raise RuntimeError(
                    "input resource combination is not admitted by the "
                    f"frozen policy: {proposed}"
                )
            self._active_resource_classes.add(normalized)
        try:
            yield
        finally:
            with self._lock:
                self._active_resource_classes.discard(normalized)

    def snapshot(
        self,
    ) -> tuple[GenerationBuildOutcome, bool, tuple[str, ...]]:
        with self._lock:
            return (
                self._outcome,
                self._done,
                tuple(sorted(self._active_resource_classes)),
            )


class NativeReadinessPending(RuntimeError):
    """日历或 T-1 最小输入尚未到位，可由同日 recovery tick 重试。"""


class DefaultDailyRuntimeServices:
    """生产依赖适配；业务执行身份只从冻结 ledger 信封读取。"""

    def __init__(
        self,
        *,
        algo_env: str = "forecast_env",
        engine: Any | None = None,
    ) -> None:
        self.engine = engine or create_engine_from_env()
        self._owns_engine = engine is None
        self._algo_env = algo_env
        self._policy: Any | None = None
        self._configs: dict[str, Any] = {}
        self._direct_cache_authorities: dict[str, object] | None = None
        self._process_start_guard = ProcessStartGuard()
        self._alert_dispatcher = AlertDispatcher(
            AlertSettings.from_env()
        )

    def bind_direct_cache_authorities(
        self,
        authorities: Mapping[str, object],
    ) -> None:
        """绑定只读重算且完整校验的 cache authority。"""
        normalized = json.loads(
            json.dumps(
                authorities,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if (
            self._direct_cache_authorities is not None
            and self._direct_cache_authorities != normalized
        ):
            raise RuntimeError("direct cache authority changed")
        self._direct_cache_authorities = normalized

    def revalidate_direct_authority(self) -> None:
        """冻结 occurrence 后再次重算 direct authority，封死 DB 冻结竞态。"""
        if self._direct_cache_authorities is None:
            raise RuntimeError("direct cache authority has not been bound")
        current = _require_production_entry_authority(
            engine=self.engine,
            algo_env=self._algo_env,
        )
        self.bind_direct_cache_authorities(current)

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def now(self) -> datetime:
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    def load_policy(self):
        discovered = discover_schemes(strict=True)
        policy = load_daily_policy(
            POLICY_V2_PATH,
            discovered=discovered,
        )
        self._policy = policy
        self._configs = {
            item.scheme_id: item
            for item in discovered
            if item.status == "active"
            and item.frequency == "daily"
            and item.scheme_id in policy.schemes
        }
        return policy

    def load_current_audit_policy(self):
        """审计落盘后 best-effort 复核当前 policy/direct authority。"""
        authorities = _require_production_entry_authority(
            engine=self.engine,
            algo_env=self._algo_env,
        )
        self.bind_direct_cache_authorities(authorities)
        return self.load_policy()

    def occurrence_lock(self, business_date: date) -> OccurrenceFileLock:
        _ensure_private_directory(LOCK_ROOT)
        return OccurrenceFileLock(
            (
                LOCK_ROOT
                / f"{SCHEDULE_KEY}-{business_date.isoformat()}.lock"
            ).resolve(strict=False)
        )

    def is_trading_day(self, business_date: date) -> bool | None:
        """返回交易日状态；日历尚无该日时用 None 表示继续等待。"""
        with self.engine.connect() as connection:
            flag = connection.execute(
                text(
                    """
                    SELECT trade_flag
                    FROM t_trade_calendar
                    WHERE rdate = :rdate
                    LIMIT 1
                    """
                ),
                {"rdate": business_date.isoformat()},
            ).scalar()
        if flag is None:
            return None
        return str(flag).strip() == "1"

    def check_native_readiness(
        self,
        *,
        feature_date: str,
    ):
        """只读检查 T-1 最小输入锚点，不把源库变成算法输入。"""
        return inspect_native_input_readiness(
            self.engine,
            feature_date=feature_date,
        )

    def find_occurrence_id(self, business_date: date) -> int | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    """
                    SELECT occurrence_id
                    FROM t_schedule_occurrences
                    WHERE schedule_key = :schedule_key
                      AND predict_date = :predict_date
                    LIMIT 1
                    """
                ),
                {
                    "schedule_key": SCHEDULE_KEY,
                    "predict_date": business_date.isoformat(),
                },
            ).scalar()
        return None if value is None else int(value)

    def find_unbound_building_generations(
        self,
        *,
        business_date: date,
    ) -> tuple[Mapping[str, object], ...]:
        """查找当日原子封存前崩溃遗留的未绑定 generation。"""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT generation_id, generation_type
                    FROM t_input_generations AS g
                    WHERE g.business_date = :business_date
                      AND g.state = 'BUILDING'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM t_schedule_items AS i
                          WHERE i.input_generation_id = g.generation_id
                      )
                    ORDER BY g.generation_type, g.generation_id
                    """
                ),
                {"business_date": business_date.isoformat()},
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    def find_generation_fences(
        self,
        *,
        business_date: date,
        generation_type: str,
    ) -> tuple[Mapping[str, object], ...]:
        """读取当日可恢复的 DB generation fence，不推导或补写身份。"""
        if generation_type not in {"native_source", "databridge_v1"}:
            raise ValueError(
                f"unsupported generation_type: {generation_type}"
            )
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT generation_id, generation_type, business_date,
                           feature_date, readiness_basis,
                           source_commit_token, dataset_content_id,
                           schema_version, exporter_version, manifest_uri,
                           manifest_sha256, native_generation_id,
                           native_manifest_sha256, state, sealed_at
                    FROM t_input_generations
                    WHERE business_date = :business_date
                      AND generation_type = :generation_type
                      AND (
                          generation_type != 'native_source'
                          OR exporter_version = :native_exporter_version
                      )
                      AND state IN ('BUILDING', 'SEALED')
                    ORDER BY generation_id
                    """
                ),
                {
                    "business_date": business_date.isoformat(),
                    "generation_type": generation_type,
                    "native_exporter_version":
                        NATIVE_GENERATION_EXPORTER_VERSION,
                },
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    def maintain_generation_storage(self) -> Mapping[str, object]:
        """在 occurrence owner 下执行 crash cleanup、叶子回收和硬门禁。"""
        policy = self._require_policy()
        _ensure_private_directory(NATIVE_GENERATION_ROOT)
        _ensure_private_directory(DATABRIDGE_GENERATION_ROOT)
        removed_debris = {
            "native": list(
                cleanup_native_generation_debris(
                    NATIVE_GENERATION_ROOT
                )
            ),
            "databridge": list(
                cleanup_databridge_generation_debris(
                    DATABRIDGE_GENERATION_ROOT
                )
            ),
        }
        refresh_config = DataBridgeRefreshConfig.from_env()
        removed_debris["databridge_current"] = list(
            DataBridgeStore(
                data_root=refresh_config.data_root,
                runtime_root=refresh_config.runtime_root,
            ).cleanup_crash_debris()
        )

        reclaimed: list[str] = []
        while True:
            candidates = resolve_reclaimable_generation_payloads(
                self.engine
            )
            if not candidates:
                break
            # 两阶段：整批 exact path + rehash 全部通过后才删除任何 payload。
            for candidate in candidates:
                _validate_reclaimable_generation_payload(
                    candidate,
                    native_root=NATIVE_GENERATION_ROOT,
                    databridge_root=DATABRIDGE_GENERATION_ROOT,
                )
            finalized_count = 0
            for candidate in candidates:
                if candidate.generation_type == "native_source":
                    delete_reclaimable_native_generation(
                        NATIVE_GENERATION_ROOT,
                        generation_id=candidate.generation_id,
                        manifest_sha256=candidate.manifest_sha256,
                        business_date=candidate.business_date,
                        feature_date=candidate.feature_date,
                    )
                elif candidate.generation_type == "databridge_v1":
                    delete_reclaimable_databridge_generation(
                        DATABRIDGE_GENERATION_ROOT,
                        schema_path=DATABRIDGE_SCHEMA_PATH,
                        generation_id=candidate.generation_id,
                        manifest_sha256=candidate.manifest_sha256,
                        business_date=candidate.business_date,
                        feature_date=candidate.feature_date,
                    )
                else:
                    raise RuntimeError(
                        "unsupported reclaim generation_type: "
                        f"{candidate.generation_type}"
                    )
                finalized = finalize_reclaimed_generation_payload(
                    self.engine,
                    generation_id=candidate.generation_id,
                    expected_generation_type=(
                        candidate.generation_type
                    ),
                    expected_business_date=candidate.business_date,
                    expected_feature_date=candidate.feature_date,
                    expected_manifest_uri=candidate.manifest_uri,
                    expected_manifest_sha256=(
                        candidate.manifest_sha256
                    ),
                )
                if finalized:
                    finalized_count += 1
                    reclaimed.append(candidate.generation_id)
            if finalized_count != len(candidates):
                raise RuntimeError(
                    "generation reclaim DB finalize made no complete "
                    "progress"
                )

        native_storage = preflight_native_generation_storage(
            NATIVE_GENERATION_ROOT,
            min_free_bytes=policy.generation_min_free_bytes,
            max_total_bytes=policy.generation_max_total_bytes,
            max_generation_count=policy.generation_max_count,
            reserve_generation_count=1,
        )
        databridge_storage = (
            preflight_databridge_generation_storage(
                DATABRIDGE_GENERATION_ROOT,
                min_free_bytes=policy.generation_min_free_bytes,
                max_total_bytes=policy.generation_max_total_bytes,
                max_generation_count=policy.generation_max_count,
                reserve_generation_count=1,
            )
        )
        return {
            "removed_debris": removed_debris,
            "reclaimed_generation_ids": reclaimed,
            "native_total_bytes": native_storage["total_bytes"],
            "native_generation_count":
                native_storage["generation_count"],
            "databridge_total_bytes":
                databridge_storage["total_bytes"],
            "databridge_generation_count":
                databridge_storage["generation_count"],
        }

    def recover_native_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
    ):
        """仅按 DB exact fence 恢复 Native generation。"""
        normalized_business_date = date.fromisoformat(business_date)
        fences = self.find_generation_fences(
            business_date=normalized_business_date,
            generation_type="native_source",
        )
        if len(fences) > 1:
            raise RuntimeError(
                "ambiguous Native DB generation fences: "
                + _generation_id_summary(fences)
            )
        _ensure_private_directory(NATIVE_GENERATION_ROOT)
        if fences:
            fence = fences[0]
            context = _open_native_fence(
                fence,
                business_date=business_date,
                feature_date=feature_date,
            )
            _validate_generation_fence_identity(
                context,
                fence,
            )
            return context
        return None

    def recover_databridge_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
        native_generation: Any,
    ):
        """仅按 DB exact fence 恢复 DataBridge generation。"""
        normalized_business_date = date.fromisoformat(business_date)
        fences = self.find_generation_fences(
            business_date=normalized_business_date,
            generation_type="databridge_v1",
        )
        if len(fences) > 1:
            raise RuntimeError(
                "ambiguous DataBridge DB generation fences: "
                + _generation_id_summary(fences)
            )
        _ensure_private_directory(DATABRIDGE_GENERATION_ROOT)
        if fences:
            fence = fences[0]
            context = _open_databridge_fence(
                fence,
                business_date=business_date,
                feature_date=feature_date,
            )
            _validate_generation_fence_identity(
                context,
                fence,
            )
        else:
            context = None
        if context is None:
            return None
        if (
            context.native_generation_id
            != native_generation.generation_id
            or context.native_manifest_sha256
            != native_generation.manifest_sha256
        ):
            raise RuntimeError(
                "recovered DataBridge parent generation drifted"
            )
        return context

    def prepare_occurrence_inputs(
        self,
        policy: Any,
        business_date: date,
    ) -> OccurrenceInputs:
        calendar = CalendarService(self.engine)
        try:
            feature_date = calendar.previous_trading_day(business_date)
        except ValueError as exc:
            raise NativeReadinessPending(str(exc)) from exc
        target_dates: dict[str, str] = {}
        item_policy_by_base: dict[str, Mapping[str, object]] = {}
        timezone_info = ZoneInfo(policy.timezone)
        for scheme_id, scheme_policy in policy.schemes.items():
            config = self._configs.get(scheme_id)
            if config is None:
                raise RuntimeError(
                    f"daily policy config is unavailable: {scheme_id}"
                )
            if (
                config.runtime_type != scheme_policy.runtime_type
                or config.task_type != scheme_policy.task_type
                or config.horizon != scheme_policy.horizon
                or tuple(config.tenors) != scheme_policy.target_tenors
            ):
                raise RuntimeError(
                    f"daily policy/config identity drift: {scheme_id}"
                )
            try:
                target_date = calendar.nth_trading_day_after(
                    feature_date,
                    scheme_policy.horizon,
                )
            except ValueError as exc:
                raise NativeReadinessPending(str(exc)) from exc
            for tenor in scheme_policy.target_tenors:
                registry_id = (
                    f"{scheme_id}__h{scheme_policy.horizon}__{tenor}"
                )
                target_dates[registry_id] = target_date
            release_at = datetime.combine(
                business_date,
                policy.not_before,
                tzinfo=timezone_info,
            ).astimezone(timezone.utc)
            deadline_at = datetime.combine(
                business_date,
                scheme_policy.absolute_deadline,
                tzinfo=timezone_info,
            ).astimezone(timezone.utc)
            item_policy_by_base[scheme_id] = {
                "scheme_version": config.scheme_version,
                "code_sha256": config.code_hash,
                "config_sha256": config.config_hash,
                "cache_group": scheme_policy.cache_group,
                "resource_class": scheme_policy.resource_class,
                "internal_workers": scheme_policy.internal_workers,
                "release_offset_minutes":
                    scheme_policy.v2_release_offset_min or 0,
                "release_at": release_at,
                "deadline_at": deadline_at,
            }
            direct_consumer = _direct_cache_consumer(
                self._direct_cache_authorities,
                scheme_id,
            )
            if direct_consumer is not None:
                item_policy_by_base[scheme_id].update(
                    {
                        "cache_adapter_sha256":
                            direct_consumer["cache_adapter_sha256"],
                        "cache_core_sha256":
                            direct_consumer["cache_core_sha256"],
                    }
                )
        if (
            len(item_policy_by_base) != policy.expected_item_count
            or len(target_dates) != policy.expected_target_count
        ):
            raise RuntimeError(
                "prepared occurrence cardinality differs from daily policy"
            )
        return OccurrenceInputs(
            feature_date=feature_date,
            target_dates=target_dates,
            item_policy_by_base=item_policy_by_base,
            policy_json=_policy_payload(
                policy,
                daily_coordinator_epoch=(
                    require_current_daily_coordinator_identity()
                    .policy_payload()
                ),
                direct_cache_authorities=self._direct_cache_authorities,
            ),
        )

    def read_frozen_inputs(
        self,
        *,
        occurrence_id: int,
        policy: Any,
    ) -> OccurrenceInputs:
        snapshot = read_schedule_occurrence_snapshot(
            self.engine,
            occurrence_id=occurrence_id,
        )
        target_dates: dict[str, str] = {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT registry_scheme_id, target_date
                    FROM t_schedule_item_targets
                    WHERE occurrence_id = :occurrence_id
                    ORDER BY registry_scheme_id
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).mappings().all()
        for row in rows:
            target_dates[str(row["registry_scheme_id"])] = (
                date.fromisoformat(str(row["target_date"])[:10]).isoformat()
            )
        feature_date = snapshot.occurrence.feature_date
        for summary in snapshot.items:
            if summary.item.input_generation_id is None:
                continue
            envelope = read_schedule_execution_envelope(
                self.engine,
                item_id=summary.item.item_id,
            )
            if envelope.generation.feature_date != feature_date:
                raise RuntimeError(
                    "bound generation feature_date differs from frozen "
                    "occurrence"
                )
        item_policy_by_base = {
            summary.item.base_scheme_id: {
                "scheme_version": summary.item.scheme_version,
                "code_sha256": summary.item.code_sha256,
                "config_sha256": summary.item.config_sha256,
                "cache_group": summary.item.cache_group,
                "resource_class": summary.item.resource_class,
                "internal_workers": summary.item.internal_workers,
                "release_offset_minutes":
                    summary.item.release_offset_minutes,
                "release_at": summary.item.release_at,
                "deadline_at": summary.item.deadline_at,
            }
            for summary in snapshot.items
        }
        if set(item_policy_by_base) != set(policy.schemes):
            raise RuntimeError(
                "frozen occurrence item identity differs from policy"
            )
        return OccurrenceInputs(
            feature_date=feature_date,
            target_dates=target_dates,
            item_policy_by_base=item_policy_by_base,
            policy_json=snapshot.occurrence.policy_json,
        )

    def create_occurrence(
        self,
        *,
        business_date: date,
        inputs: OccurrenceInputs,
        policy: Any,
    ) -> int:
        timezone_info = ZoneInfo(policy.timezone)
        return create_schedule_occurrence(
            self.engine,
            schedule_key=SCHEDULE_KEY,
            predict_date=business_date.isoformat(),
            feature_date=inputs.feature_date,
            target_dates=inputs.target_dates,
            item_policy_by_base=inputs.item_policy_by_base,
            policy_version=policy.version,
            policy_json=inputs.policy_json,
            sla_deadline_at=datetime.combine(
                business_date,
                policy.sla_deadline,
                tzinfo=timezone_info,
            ).astimezone(timezone.utc),
            recovery_cutoff_at=datetime.combine(
                business_date,
                policy.recovery_cutoff,
                tzinfo=timezone_info,
            ).astimezone(timezone.utc),
        )

    def read_snapshot(self, occurrence_id: int):
        return read_schedule_occurrence_snapshot(
            self.engine,
            occurrence_id=occurrence_id,
        )

    def validate_occurrence_policy(
        self,
        *,
        snapshot: Any,
        policy: Any,
    ) -> None:
        occurrence = snapshot.occurrence
        current_identity = assert_daily_coordinator_epoch_matches_policy(
            occurrence.policy_json
        )
        if occurrence.policy_version != policy.version:
            raise RuntimeError(
                "frozen occurrence policy version differs from runtime policy"
            )
        if dict(occurrence.policy_json) != _policy_payload(
            policy,
            daily_coordinator_epoch=current_identity.policy_payload(),
            direct_cache_authorities=self._direct_cache_authorities,
        ):
            raise RuntimeError(
                "frozen occurrence policy payload differs from runtime policy"
            )

    def validate_occurrence_epoch(self, *, snapshot: Any) -> None:
        """仅验证冻结 occurrence 与 current machine epoch 完全一致。"""
        assert_daily_coordinator_epoch_matches_policy(
            snapshot.occurrence.policy_json
        )

    def build_native_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
        capture_not_after: datetime,
        source_contract_cutoff: datetime,
    ):
        _ensure_private_directory(NATIVE_GENERATION_ROOT)
        policy = self._require_policy()
        return create_native_generation(
            self.engine,
            business_date=business_date,
            feature_date=feature_date,
            output_root=NATIVE_GENERATION_ROOT,
            readiness_basis="CLOCK_CONTRACT",
            capture_not_after=capture_not_after,
            source_contract_cutoff=source_contract_cutoff,
            max_total_bytes=policy.generation_max_total_bytes,
            min_free_bytes=policy.generation_min_free_bytes,
        )

    def refresh_databridge(
        self,
        *,
        business_date: str,
        feature_date: str,
        publication_capability: DailyCoordinatorPublicationCapability,
    ):
        config = DataBridgeRefreshConfig.from_env()
        client = DataBridgeClient(DataBridgeClientConfig.from_env())
        policy = self._require_policy()
        deadline = datetime.combine(
            date.fromisoformat(business_date),
            policy.recovery_cutoff,
            tzinfo=ZoneInfo(policy.timezone),
        )
        continuity_authority = (
            resolve_databridge_continuity_authority_from_engine(
                config,
                feature_date=feature_date,
                engine=self.engine,
            )
        )
        refresh_result = run_full_refresh(
            client=client,
            config=config,
            expected_daily_date=feature_date,
            refresh_date=business_date,
            publish=True,
            deadline_at=deadline,
            publication_capability=publication_capability,
            continuity_authority=continuity_authority,
        )
        return check_current_dataset(
            config,
            required_refresh_date=business_date,
            expected_daily_date=feature_date,
            expected_generation_id=str(
                refresh_result.state["generation_id"]
            ),
            expected_business_digest=str(
                refresh_result.state["business_digest"]
            ),
            expected_publication_capability=publication_capability,
        )

    def build_databridge_generation(
        self,
        *,
        current: Any,
        native_generation: Any,
        business_date: str,
        feature_date: str,
    ):
        _ensure_private_directory(DATABRIDGE_GENERATION_ROOT)
        policy = self._require_policy()
        return create_databridge_generation(
            current,
            native_generation=native_generation,
            business_date=business_date,
            feature_date=feature_date,
            output_root=DATABRIDGE_GENERATION_ROOT,
            schema_path=DATABRIDGE_SCHEMA_PATH,
            max_total_bytes=policy.generation_max_total_bytes,
            min_free_bytes=policy.generation_min_free_bytes,
        )

    def validate_frozen_calendar(
        self,
        *,
        native_generation: Any,
        inputs: OccurrenceInputs,
        policy: Any,
    ) -> None:
        if native_generation is None:
            raise RuntimeError("Native calendar generation is required")
        if native_generation.feature_date != inputs.feature_date:
            raise RuntimeError(
                "Native generation feature date differs from occurrence"
            )
        calendar = FrozenCalendarService(native_generation)
        if (
            calendar.previous_trading_day(
                native_generation.business_date
            )
            != inputs.feature_date
        ):
            raise RuntimeError(
                "Native generation previous trading day differs from "
                "occurrence feature date"
            )
        for scheme_id, scheme_policy in policy.schemes.items():
            expected_target = calendar.nth_trading_day_after(
                inputs.feature_date,
                scheme_policy.horizon,
            )
            for tenor in scheme_policy.target_tenors:
                registry_id = (
                    f"{scheme_id}__h{scheme_policy.horizon}__{tenor}"
                )
                actual_target = inputs.target_dates.get(registry_id)
                if actual_target != expected_target:
                    raise RuntimeError(
                        "frozen target date differs from Native calendar: "
                        f"{registry_id}={actual_target} != {expected_target}"
                    )

    def register_native_generation(
        self,
        context: Any,
        *,
        occurrence_id: int,
    ) -> str:
        return register_native_generation(
            self.engine,
            context,
            occurrence_id=occurrence_id,
        )

    def register_databridge_generation(
        self,
        context: Any,
        *,
        occurrence_id: int,
    ) -> str:
        return register_databridge_generation(
            self.engine,
            context,
            schema_path=DATABRIDGE_SCHEMA_PATH,
            occurrence_id=occurrence_id,
        )

    def resolve_bound_generations(
        self,
        *,
        snapshot: Any,
        inputs: OccurrenceInputs,
        policy: Any,
    ) -> tuple[Any | None, Any | None]:
        del policy
        for runtime_type in ("native_adapter", "blackbox_v2"):
            runtime_items = [
                summary.item
                for summary in snapshot.items
                if summary.item.runtime_type == runtime_type
            ]
            bound_ids = {
                item.input_generation_id
                for item in runtime_items
                if item.input_generation_id is not None
            }
            bound_count = sum(
                item.input_generation_id is not None
                for item in runtime_items
            )
            if 0 < bound_count < len(runtime_items):
                raise RuntimeError(
                    "occurrence has a partial runtime generation binding: "
                    f"{runtime_type}"
                )
            if len(bound_ids) > 1:
                raise RuntimeError(
                    "occurrence has mixed runtime generation bindings: "
                    f"{runtime_type}={sorted(bound_ids)}"
                )
        native_envelopes: dict[str, Any] = {}
        databridge_envelopes: dict[str, Any] = {}
        for summary in snapshot.items:
            item = summary.item
            if item.input_generation_id is None:
                continue
            envelope = read_schedule_execution_envelope(
                self.engine,
                item_id=item.item_id,
            )
            generation = envelope.generation
            if generation.generation_id != item.input_generation_id:
                raise RuntimeError(
                    "schedule execution envelope generation differs from "
                    f"frozen item binding: item_id={item.item_id}"
                )
            if generation.generation_type == "native_source":
                _remember_generation_envelope(
                    native_envelopes,
                    generation,
                )
            elif generation.generation_type == "databridge_v1":
                _remember_generation_envelope(
                    databridge_envelopes,
                    generation,
                )
                _remember_generation_envelope(
                    native_envelopes,
                    envelope.calendar_generation,
                )
            else:
                raise RuntimeError(
                    "unsupported frozen generation type: "
                    f"{generation.generation_type}"
                )

        native_by_id = {
            generation_id: _open_native_envelope(generation)
            for generation_id, generation in native_envelopes.items()
        }
        databridge_by_id = {
            generation_id: open_databridge_generation(
                Path(generation.manifest_uri),
                expected_generation_id=generation.generation_id,
                expected_manifest_sha256=generation.manifest_sha256,
                expected_business_date=generation.business_date,
                expected_feature_date=generation.feature_date,
                schema_path=DATABRIDGE_SCHEMA_PATH,
            )
            for generation_id, generation in (
                databridge_envelopes.items()
            )
        }
        for generation_id, context in native_by_id.items():
            if context.generation_id != generation_id:
                raise RuntimeError(
                    "opened Native generation identity differs from "
                    f"ledger envelope: {generation_id}"
                )
        for generation_id, context in databridge_by_id.items():
            if context.generation_id != generation_id:
                raise RuntimeError(
                    "opened DataBridge generation identity differs from "
                    f"ledger envelope: {generation_id}"
                )
        if len(native_by_id) > 1 or len(databridge_by_id) > 1:
            raise RuntimeError(
                "occurrence contains more than one generation per runtime"
            )
        native = next(iter(native_by_id.values()), None)
        databridge = next(iter(databridge_by_id.values()), None)
        if native is not None and native.feature_date != inputs.feature_date:
            raise RuntimeError(
                "bound Native generation feature date drifted"
            )
        if (
            databridge is not None
            and (
                native is None
                or databridge.native_generation_id
                != native.generation_id
                or databridge.native_manifest_sha256
                != native.manifest_sha256
            )
        ):
            raise RuntimeError(
                "bound DataBridge/Native generation relation drifted"
            )
        return native, databridge

    def fail_item(
        self,
        *,
        item_id: int,
        failure_code: str,
        message: str,
    ) -> None:
        fail_schedule_item_without_attempt(
            self.engine,
            item_id=item_id,
            failure_code=failure_code,
            failure_message=message,
        )

    def dispatch_decisions(
        self,
        *,
        business_date: date,
        snapshot: Any,
        native_generation: Any | None,
        databridge_generation: Any | None,
        running_scheme_ids: tuple[str, ...],
        running_resource_classes: tuple[str, ...],
        now: datetime,
    ):
        policy = self._require_policy()
        expected_native_generation_id = (
            _snapshot_runtime_generation_id(
                snapshot,
                runtime_type="native_adapter",
            )
        )
        expected_databridge_generation_id = (
            _snapshot_runtime_generation_id(
                snapshot,
                runtime_type="blackbox_v2",
            )
        )
        states = {
            summary.item.base_scheme_id: ItemControlState(
                scheme_id=summary.item.base_scheme_id,
                state=summary.item.state,
                attempt_no=summary.item.attempt_no,
                failure_code=summary.item.failure_code,
            )
            for summary in snapshot.items
        }
        v2_release_at_by_scheme = {
            summary.item.base_scheme_id:
                _parse_frozen_ledger_datetime(
                    summary.item.release_at,
                    field=(
                        f"{summary.item.base_scheme_id}.release_at"
                    ),
                )
            for summary in snapshot.items
            if summary.item.runtime_type == "blackbox_v2"
        }
        return plan_dispatches(
            policy,
            business_date=business_date,
            now=now,
            item_states=states,
            native_generation_sealed=native_generation is not None,
            databridge_generation_business_date=(
                date.fromisoformat(databridge_generation.business_date)
                if databridge_generation is not None
                else None
            ),
            v2_release_at_by_scheme=v2_release_at_by_scheme,
            native_generation_business_date=(
                date.fromisoformat(native_generation.business_date)
                if native_generation is not None
                else None
            ),
            native_generation_id=(
                native_generation.generation_id
                if native_generation is not None
                else None
            ),
            expected_native_generation_id=(
                expected_native_generation_id
            ),
            databridge_generation_id=(
                databridge_generation.generation_id
                if databridge_generation is not None
                else None
            ),
            expected_databridge_generation_id=(
                expected_databridge_generation_id
            ),
            running_scheme_ids=running_scheme_ids,
            running_resource_classes=running_resource_classes,
        )

    def execute_item(
        self,
        *,
        item_id: int,
        trigger_origin: str,
    ):
        return execute_scheduled_item(
            self.engine,
            item_id=item_id,
            algo_env=self._algo_env,
            trigger_origin=trigger_origin,
            process_start_guard=self._process_start_guard,
        )

    def heartbeat(
        self,
        *,
        occurrence_id: int | None,
        state: str,
        details: Mapping[str, object],
    ):
        identity = require_current_daily_coordinator_identity()
        return upsert_scheduler_heartbeat(
            self.engine,
            service_name="daily-coordinator",
            process_id=os.getpid(),
            host_name=socket.gethostname(),
            state=state,
            occurrence_id=occurrence_id,
            details={
                **details,
                "coordinator_mode": identity.mode,
                "daily_coordinator_epoch": identity.policy_payload(),
            },
        )

    def alert(
        self,
        *,
        code: str,
        severity: str,
        business_date: date,
        occurrence_id: int | None,
        message: str,
        scheme_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._alert_dispatcher.dispatch(
            AlertEvent.now(
                code=code,
                severity=severity,
                predict_date=business_date.isoformat(),
                occurrence_id=occurrence_id,
                scheme_id=scheme_id,
                message=message,
                details=details,
            )
        )

    def detect_late_writes(
        self,
        *,
        business_date: date,
        feature_date: str,
    ):
        policy = self._require_policy()
        cutoff = datetime.combine(
            business_date,
            policy.not_before,
            tzinfo=ZoneInfo(policy.timezone),
        )
        return detect_late_source_writes(
            self.engine,
            feature_date=feature_date,
            cutoff_at=cutoff,
        )

    def project_databridge_readiness(
        self,
        *,
        snapshot: Any,
        guardrail_at: datetime,
    ) -> DataBridgeReadinessProjection:
        """仅用冻结 item 绑定与 DB sealed_at 投影 06:55 readiness。"""
        generation_id = _snapshot_runtime_generation_id(
            snapshot,
            runtime_type="blackbox_v2",
        )
        if generation_id is None:
            return DataBridgeReadinessProjection(
                status="LATE",
                reason="DATABRIDGE_NOT_SEALED_BY_GUARDRAIL",
                generation_id=None,
                sealed_at=None,
                guardrail_at=guardrail_at,
            )
        try:
            generation = read_sealed_input_generation(
                self.engine,
                generation_id=generation_id,
                expected_generation_type="databridge_v1",
            )
        except RuntimeError:
            return DataBridgeReadinessProjection(
                status="LATE",
                reason="DATABRIDGE_DB_FENCE_NOT_SEALED",
                generation_id=generation_id,
                sealed_at=None,
                guardrail_at=guardrail_at,
            )
        if generation.sealed_at is None:
            raise RuntimeError(
                "SEALED DataBridge generation is missing sealed_at"
            )
        sealed_at = _parse_frozen_ledger_datetime(
            generation.sealed_at,
            field="databridge.sealed_at",
        )
        guardrail_utc = _parse_frozen_ledger_datetime(
            guardrail_at,
            field="databridge_readiness_guardrail",
        )
        on_time = sealed_at <= guardrail_utc
        return DataBridgeReadinessProjection(
            status="ON_TIME" if on_time else "LATE",
            reason=(
                None
                if on_time
                else "DATABRIDGE_SEALED_AFTER_GUARDRAIL"
            ),
            generation_id=generation_id,
            sealed_at=sealed_at,
            guardrail_at=guardrail_at,
        )

    def evaluate_v2_start(
        self,
        *,
        item_id: int,
        evaluated_at: datetime,
    ):
        del evaluated_at
        return evaluate_schedule_item_start_sla(
            self.engine,
            item_id=item_id,
        )

    def evaluate_target_sla(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ):
        del evaluated_at
        return evaluate_schedule_occurrence_target_sla(
            self.engine,
            occurrence_id=occurrence_id,
        )

    def reconcile_occurrence_visibility_receipts(
        self,
        *,
        occurrence_id: int,
    ) -> int:
        return reconcile_schedule_occurrence_visibility_receipts(
            self.engine,
            occurrence_id=occurrence_id,
        )

    def expire_items(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ) -> int:
        del evaluated_at
        return expire_schedule_items(
            self.engine,
            occurrence_id=occurrence_id,
        )

    def fence_item(self, *, item_id: int):
        return fence_current_schedule_attempt(
            self.engine,
            item_id=item_id,
        )

    def cleanup_fenced_attempt(
        self,
        *,
        item: Any,
        fenced_attempt: Any,
    ) -> bool:
        return _terminate_verified_orphan(
            scheme_id=item.base_scheme_id,
            execution_token=fenced_attempt.execution_token,
            process_group_id=fenced_attempt.process_group_id,
            process_id=fenced_attempt.process_id,
        )

    def confirm_orphan_cleanup(self, *, fenced_attempt: Any) -> None:
        confirm_schedule_attempt_orphan_cleanup(
            self.engine,
            item_id=fenced_attempt.item_id,
            run_id=fenced_attempt.run_id,
            execution_token=fenced_attempt.execution_token,
        )

    def wait_for_progress(self, seconds: float) -> None:
        threading.Event().wait(max(0.0, min(float(seconds), 1.0)))

    def _require_policy(self):
        if self._policy is None:
            raise RuntimeError("daily policy has not been loaded")
        return self._policy


class DailyRuntime:
    """协调一个交易日的冻结 occurrence。"""

    def __init__(self, services: Any) -> None:
        self._services = services

    @staticmethod
    def _require_frozen_occurrence_policy_version(
        *,
        snapshot: Any,
        policy: Any,
    ) -> None:
        """在任何副作用前校验冻结账本与当前 runtime 的 policy 版本。"""
        occurrence = snapshot.occurrence
        runtime_version = getattr(policy, "version", None)
        policy_json = getattr(occurrence, "policy_json", None)
        if (
            getattr(occurrence, "policy_version", None)
            != runtime_version
            or not isinstance(policy_json, Mapping)
            or policy_json.get("version") != runtime_version
        ):
            raise RuntimeError(
                "frozen occurrence policy version differs from runtime policy"
            )

    def run_occurrence(
        self,
        *,
        run_date: str | date | None = None,
        trigger_origin: str = "apscheduler",
    ) -> DailyRuntimeResult:
        policy = self._services.load_policy()
        now = self._services.now().astimezone(ZoneInfo(policy.timezone))
        business_date = _business_date(run_date, now=now)
        if business_date != now.date():
            return DailyRuntimeResult(
                business_date=business_date.isoformat(),
                occurrence_id=None,
                status="cross_day_rejected",
            )
        not_before = datetime.combine(
            business_date,
            policy.not_before,
            tzinfo=ZoneInfo(policy.timezone),
        )
        if now < not_before:
            return DailyRuntimeResult(
                business_date=business_date.isoformat(),
                occurrence_id=None,
                status="not_before",
            )
        with _optional_owner(
            self._services.occurrence_lock(business_date)
        ) as acquired:
            if not acquired:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=None,
                    status="already_owned",
                )
            occurrence_id = self._services.find_occurrence_id(
                business_date
            )
            if occurrence_id is not None:
                snapshot = self._services.read_snapshot(occurrence_id)
                self._require_frozen_occurrence_policy_version(
                    snapshot=snapshot,
                    policy=policy,
                )
                _validate_snapshot_cardinality(snapshot, policy=policy)
                self._services.validate_occurrence_policy(
                    snapshot=snapshot,
                    policy=policy,
                )
            building_generations = (
                self._services.find_unbound_building_generations(
                    business_date=business_date,
                )
            )
            if occurrence_id is None and building_generations:
                details = {
                    "reason": "UNBOUND_BUILDING_GENERATION",
                    "generations": _generation_id_summary(
                        building_generations
                    ),
                }
                self._services.heartbeat(
                    occurrence_id=None,
                    state="GENERATION_FAILED",
                    details=details,
                )
                self._services.alert(
                    code="GENERATION_BUILD_FAILED",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=None,
                    message=(
                        "Unbound BUILDING input generation has no "
                        "daily occurrence"
                    ),
                    details=details,
                )
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=None,
                    status="generation_failed",
                )
            inputs: OccurrenceInputs | None
            if occurrence_id is None:
                trading_day_status = self._services.is_trading_day(
                    business_date
                )
                if trading_day_status is None:
                    details = {
                        "business_date": business_date.isoformat(),
                        "missing_requirements": [
                            "calendar:business_date"
                        ],
                        "trigger_origin": trigger_origin,
                    }
                    self._services.heartbeat(
                        occurrence_id=None,
                        state="WAITING_NATIVE_READINESS",
                        details=details,
                    )
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=None,
                        status="waiting_for_native_readiness",
                    )
                if not trading_day_status:
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=None,
                        status="non_trading_day",
                    )
                try:
                    inputs = self._services.prepare_occurrence_inputs(
                        policy,
                        business_date,
                    )
                except NativeReadinessPending as exc:
                    details = {
                        "business_date": business_date.isoformat(),
                        "missing_requirements": [
                            "calendar:occurrence_horizon"
                        ],
                        "reason": _bounded_exception(exc),
                        "trigger_origin": trigger_origin,
                    }
                    self._services.heartbeat(
                        occurrence_id=None,
                        state="WAITING_NATIVE_READINESS",
                        details=details,
                    )
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=None,
                        status="waiting_for_native_readiness",
                    )
                occurrence_id = self._services.create_occurrence(
                    business_date=business_date,
                    inputs=inputs,
                    policy=policy,
                )
                snapshot = self._services.read_snapshot(occurrence_id)
                _validate_snapshot_cardinality(snapshot, policy=policy)
                self._services.validate_occurrence_policy(
                    snapshot=snapshot,
                    policy=policy,
                )
                self._services.revalidate_direct_authority()
            else:
                inputs = None
            if inputs is None:
                inputs = self._services.read_frozen_inputs(
                    occurrence_id=occurrence_id,
                    policy=policy,
                )
            if self._reconcile_visibility_receipts(
                occurrence_id=occurrence_id,
                business_date=business_date,
                phase="run_occurrence_pre_snapshot",
            ) is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="visibility_reconciliation_failed",
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
            self._catch_up_databridge_readiness(
                snapshot=snapshot,
                business_date=business_date,
                policy=policy,
                now=now,
            )
            self._catch_up_deadline_projections(
                snapshot=snapshot,
                business_date=business_date,
                policy=policy,
            )
            if (
                snapshot.actual_accepted_target_count
                == snapshot.occurrence.expected_target_count
            ):
                self._services.heartbeat(
                    occurrence_id=occurrence_id,
                    state="COMPLETE",
                    details={
                        "accepted_target_count":
                            snapshot.actual_accepted_target_count,
                        "expected_target_count":
                            snapshot.occurrence.expected_target_count,
                    },
                )
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="complete",
                )
            recovery_cutoff = datetime.combine(
                business_date,
                policy.recovery_cutoff,
                tzinfo=ZoneInfo(policy.timezone),
            )
            if now >= recovery_cutoff:
                expired = self._services.expire_items(
                    occurrence_id=occurrence_id,
                    evaluated_at=now,
                )
                snapshot = self._services.read_snapshot(occurrence_id)
                details = {
                    "expired_item_count": expired,
                    "accepted_target_count":
                        snapshot.actual_accepted_target_count,
                    "expected_target_count":
                        snapshot.occurrence.expected_target_count,
                }
                self._services.heartbeat(
                    occurrence_id=occurrence_id,
                    state="RECOVERY_CUTOFF",
                    details=details,
                )
                if (
                    snapshot.actual_accepted_target_count
                    != snapshot.occurrence.expected_target_count
                ):
                    self._services.alert(
                        code="RECOVERY_CUTOFF_INCOMPLETE",
                        severity="critical",
                        business_date=business_date,
                        occurrence_id=occurrence_id,
                        message=(
                            "Daily occurrence remains incomplete at 08:30"
                        ),
                        details=details,
                    )
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="recovery_cutoff",
                )
            if (
                _snapshot_runtime_generation_id(
                    snapshot,
                    runtime_type="native_adapter",
                )
                is None
            ):
                readiness = self._services.check_native_readiness(
                    feature_date=inputs.feature_date,
                )
                if not readiness.ready:
                    details = {
                        "feature_date": inputs.feature_date,
                        "missing_requirements": list(
                            readiness.missing_requirements
                        ),
                        "trigger_origin": trigger_origin,
                    }
                    self._services.heartbeat(
                        occurrence_id=occurrence_id,
                        state="WAITING_NATIVE_READINESS",
                        details=details,
                    )
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=occurrence_id,
                        status="waiting_for_native_readiness",
                    )
            try:
                self._services.maintain_generation_storage()
            except Exception as exc:
                details = {
                    "reason": _bounded_exception(exc),
                    "error_type": type(exc).__name__,
                    "stage": "pre_generation_storage",
                }
                self._services.heartbeat(
                    occurrence_id=occurrence_id,
                    state="GENERATION_STORAGE_UNAVAILABLE",
                    details=details,
                )
                self._services.alert(
                    code="GENERATION_STORAGE_UNAVAILABLE",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    message=(
                        "Generation storage cleanup/preflight failed"
                    ),
                    details=details,
                )
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="generation_storage_failed",
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state="PREPARING_INPUTS",
                details={"trigger_origin": trigger_origin},
            )
            if not self._recover_running_items(
                snapshot=snapshot,
                business_date=business_date,
                occurrence_id=occurrence_id,
            ):
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="recovery_blocked",
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            self._mark_unsupported_items(
                snapshot=snapshot,
                policy=policy,
            )
            generation_availability = _GenerationAvailability(
                policy.allowed_resource_combinations
            )

            def build_generation_pipeline() -> GenerationBuildOutcome:
                try:
                    outcome = self._build_input_generations(
                        occurrence_id=occurrence_id,
                        business_date=business_date,
                        inputs=inputs,
                        policy=policy,
                        snapshot=snapshot,
                        on_native_ready=(
                            generation_availability.publish_native
                        ),
                        resource_occupancy=generation_availability,
                    )
                except Exception as exc:
                    message = _bounded_exception(exc)
                    outcome = GenerationBuildOutcome(
                        native_generation=None,
                        databridge_generation=None,
                        native_error=message,
                        databridge_error=message,
                    )
                generation_availability.finish(outcome)
                return outcome

            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="daily-generation-pipeline",
            ) as generation_pool:
                generation_future = generation_pool.submit(
                    build_generation_pipeline
                )
                drive_result = self._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=business_date,
                    policy=policy,
                    generation_availability=generation_availability,
                    trigger_origin=trigger_origin,
                )
                dispatched = drive_result.dispatched_scheme_ids
                generations = generation_future.result()
            if drive_result.status == "recovery_blocked":
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="recovery_blocked",
                    dispatched_scheme_ids=tuple(dispatched),
                )
            if self._reconcile_visibility_receipts(
                occurrence_id=occurrence_id,
                business_date=business_date,
                phase="run_occurrence_pre_final_snapshot",
            ) is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="visibility_reconciliation_failed",
                    dispatched_scheme_ids=tuple(dispatched),
                )
            final_snapshot = self._services.read_snapshot(occurrence_id)
            complete = (
                final_snapshot.actual_accepted_target_count
                == final_snapshot.occurrence.expected_target_count
            )
            status = (
                "complete"
                if complete
                else (
                    "generation_failed"
                    if generations.failed
                    else "incomplete"
                )
            )
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state=status.upper(),
                details={
                    "accepted_target_count":
                        final_snapshot.actual_accepted_target_count,
                    "expected_target_count":
                        final_snapshot.occurrence.expected_target_count,
                },
            )
            return DailyRuntimeResult(
                business_date=business_date.isoformat(),
                occurrence_id=occurrence_id,
                status=status,
                dispatched_scheme_ids=tuple(dispatched),
            )

    def run_watchdog(
        self,
        *,
        stage: str,
        run_date: str | date | None = None,
    ) -> WatchdogResult:
        stages = {
            "databridge_readiness_guardrail",
            "progress",
            "v2_start_guardrail",
            "target_sla",
            "recovery_cutoff",
        }
        if stage not in stages:
            raise ValueError(f"unsupported daily watchdog stage: {stage}")
        frozen_now = self._services.now().astimezone(
            ZoneInfo("Asia/Shanghai")
        )
        frozen_business_date = _business_date(
            run_date,
            now=frozen_now,
        )
        if frozen_business_date != frozen_now.date():
            return WatchdogResult(
                business_date=frozen_business_date.isoformat(),
                occurrence_id=None,
                stage=stage,
                status="cross_day_rejected",
                details={},
            )
        if stage != "progress":
            frozen_occurrence_id = self._services.find_occurrence_id(
                frozen_business_date
            )
            if frozen_occurrence_id is not None:
                return self._run_frozen_deadline_watchdog(
                    stage=stage,
                    business_date=frozen_business_date,
                    occurrence_id=frozen_occurrence_id,
                    now=frozen_now,
                )
            trading_day_status = self._services.is_trading_day(
                frozen_business_date
            )
            if trading_day_status is False:
                details = {
                    "business_date":
                        frozen_business_date.isoformat(),
                    "reason": "NON_TRADING_DAY",
                }
                self._services.heartbeat(
                    occurrence_id=None,
                    state="IDLE",
                    details=details,
                )
                return WatchdogResult(
                    business_date=frozen_business_date.isoformat(),
                    occurrence_id=None,
                    stage=stage,
                    status="non_trading_day",
                    details=details,
                )
            if trading_day_status is None:
                # 关闭首次 occurrence lookup 与日历就绪检查之间的竞态；
                # 已冻结 occurrence 永远优先于随后可变的 live calendar。
                frozen_occurrence_id = (
                    self._services.find_occurrence_id(
                        frozen_business_date
                    )
                )
                if frozen_occurrence_id is not None:
                    return self._run_frozen_deadline_watchdog(
                        stage=stage,
                        business_date=frozen_business_date,
                        occurrence_id=frozen_occurrence_id,
                        now=frozen_now,
                    )
                details = {
                    "business_date":
                        frozen_business_date.isoformat(),
                    "missing_requirements": [
                        "calendar:business_date"
                    ],
                    "reason": "BUSINESS_CALENDAR_PENDING",
                }
                self._services.heartbeat(
                    occurrence_id=None,
                    state="WAITING_NATIVE_READINESS",
                    details=details,
                )
                self._services.alert(
                    code="DAILY_OCCURRENCE_MISSING",
                    severity="critical",
                    business_date=frozen_business_date,
                    occurrence_id=None,
                    message=(
                        "Daily occurrence is waiting for business calendar"
                    ),
                    details={"stage": stage},
                )
                return WatchdogResult(
                    business_date=frozen_business_date.isoformat(),
                    occurrence_id=None,
                    stage=stage,
                    status="waiting_for_native_readiness",
                    details=details,
                )
            # 关闭“首次 lookup 为空、随后协调器刚创建 occurrence”的竞态。
            frozen_occurrence_id = self._services.find_occurrence_id(
                frozen_business_date
            )
            if frozen_occurrence_id is not None:
                return self._run_frozen_deadline_watchdog(
                    stage=stage,
                    business_date=frozen_business_date,
                    occurrence_id=frozen_occurrence_id,
                    now=frozen_now,
                )
            details = {"reason": "OCCURRENCE_NOT_FOUND"}
            self._services.heartbeat(
                occurrence_id=None,
                state=f"WATCHDOG_{stage.upper()}",
                details=details,
            )
            self._services.alert(
                code="DAILY_OCCURRENCE_MISSING",
                severity="critical",
                business_date=frozen_business_date,
                occurrence_id=None,
                message=(
                    "Daily occurrence is missing at watchdog evaluation"
                ),
                details={"stage": stage},
            )
            return WatchdogResult(
                business_date=frozen_business_date.isoformat(),
                occurrence_id=None,
                stage=stage,
                status="missing",
                details=details,
            )
        policy = self._services.load_policy()
        now = self._services.now().astimezone(ZoneInfo(policy.timezone))
        business_date = _business_date(run_date, now=now)
        if business_date != now.date():
            return WatchdogResult(
                business_date=business_date.isoformat(),
                occurrence_id=None,
                stage=stage,
                status="cross_day_rejected",
                details={},
            )
        occurrence_id = self._services.find_occurrence_id(business_date)
        if occurrence_id is None:
            trading_day_status = self._services.is_trading_day(
                business_date
            )
            if trading_day_status is False:
                details = {
                    "business_date": business_date.isoformat(),
                    "reason": "NON_TRADING_DAY",
                }
                self._services.heartbeat(
                    occurrence_id=None,
                    state="IDLE",
                    details=details,
                )
                return WatchdogResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=None,
                    stage=stage,
                    status="non_trading_day",
                    details=details,
                )
            if trading_day_status is None:
                occurrence_id = self._services.find_occurrence_id(
                    business_date
                )
                if occurrence_id is None:
                    details = {
                        "business_date": business_date.isoformat(),
                        "missing_requirements": [
                            "calendar:business_date"
                        ],
                        "reason": "BUSINESS_CALENDAR_PENDING",
                    }
                    self._services.heartbeat(
                        occurrence_id=None,
                        state="WAITING_NATIVE_READINESS",
                        details=details,
                    )
                    self._services.alert(
                        code="DAILY_OCCURRENCE_MISSING",
                        severity="critical",
                        business_date=business_date,
                        occurrence_id=None,
                        message=(
                            "Daily occurrence is waiting for business "
                            "calendar"
                        ),
                        details={"stage": stage},
                    )
                    return WatchdogResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=None,
                        stage=stage,
                        status="waiting_for_native_readiness",
                        details=details,
                    )
            if occurrence_id is None:
                # 关闭首次 lookup 为空、协调器随后创建 occurrence 的竞态。
                occurrence_id = self._services.find_occurrence_id(
                    business_date
                )
        if occurrence_id is None:
            details = {"reason": "OCCURRENCE_NOT_FOUND"}
            self._services.heartbeat(
                occurrence_id=None,
                state=f"WATCHDOG_{stage.upper()}",
                details=details,
            )
            self._services.alert(
                code="DAILY_OCCURRENCE_MISSING",
                severity="critical",
                business_date=business_date,
                occurrence_id=None,
                message=(
                    "Daily occurrence is missing at watchdog evaluation"
                ),
                details={"stage": stage},
            )
            return WatchdogResult(
                business_date=business_date.isoformat(),
                occurrence_id=None,
                stage=stage,
                status="missing",
                details=details,
            )
        snapshot = self._services.read_snapshot(occurrence_id)
        _validate_snapshot_cardinality(snapshot, policy=policy)
        self._services.validate_occurrence_policy(
            snapshot=snapshot,
            policy=policy,
        )
        deadline_catchup = _DeadlineCatchupResult()
        deadline_visibility_reconciled: int | None = None
        if stage in {"target_sla", "recovery_cutoff"}:
            deadline_visibility_reconciled = (
                self._reconcile_visibility_receipts(
                    occurrence_id=occurrence_id,
                    business_date=business_date,
                    phase=(
                        "target_sla_pre_evaluate"
                        if stage == "target_sla"
                        else "recovery_cutoff_pre_evaluate"
                    ),
                )
            )
            if deadline_visibility_reconciled is None:
                return WatchdogResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    stage=stage,
                    status="visibility_reconciliation_failed",
                    details={
                        "reason":
                            "VISIBILITY_RECEIPT_RECONCILE_FAILED",
                    },
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
            deadline_catchup = (
                self._catch_up_deadline_projections(
                    snapshot=snapshot,
                    business_date=business_date,
                    policy=policy,
                )
            )
        details: dict[str, object] = {
            "accepted_target_count":
                snapshot.actual_accepted_target_count,
            "expected_target_count":
                snapshot.occurrence.expected_target_count,
        }
        self._append_late_write_diagnostics(
            snapshot=snapshot,
            business_date=business_date,
            occurrence_id=occurrence_id,
            stage=stage,
            details=details,
        )
        building_generations = (
            self._services.find_unbound_building_generations(
                business_date=business_date,
            )
        )
        if building_generations:
            with _optional_owner(
                self._services.occurrence_lock(business_date)
            ) as acquired:
                if not acquired:
                    details.update(
                        {
                            "reason": "COORDINATOR_ACTIVE",
                            "generations": _generation_id_summary(
                                building_generations
                            ),
                        }
                    )
                    self._services.heartbeat(
                        occurrence_id=occurrence_id,
                        state=f"WATCHDOG_{stage.upper()}",
                        details=details,
                    )
                    return WatchdogResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=occurrence_id,
                        stage=stage,
                        status="coordinator_active",
                        details=details,
                    )
                building_generations = (
                    self._services.find_unbound_building_generations(
                        business_date=business_date,
                    )
                )
                if not building_generations:
                    snapshot = self._services.read_snapshot(
                        occurrence_id
                    )
                    _validate_snapshot_cardinality(
                        snapshot,
                        policy=policy,
                    )
                    self._services.validate_occurrence_policy(
                        snapshot=snapshot,
                        policy=policy,
                    )
        if building_generations:
            details.update(
                {
                    "reason": "UNBOUND_BUILDING_GENERATION",
                    "generations": _generation_id_summary(
                        building_generations
                    ),
                }
            )
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state=f"WATCHDOG_{stage.upper()}",
                details=details,
            )
            self._services.alert(
                code="GENERATION_BUILD_FAILED",
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message=(
                    "Watchdog found an unbound BUILDING input generation"
                ),
                details={"stage": stage, **details},
            )
            return WatchdogResult(
                business_date=business_date.isoformat(),
                occurrence_id=occurrence_id,
                stage=stage,
                status="generation_failed",
                details=details,
            )
        details["accepted_target_count"] = (
            snapshot.actual_accepted_target_count
        )
        details["expected_target_count"] = (
            snapshot.occurrence.expected_target_count
        )
        status = "ok"
        if stage == "progress":
            forecast = _progress_forecast(
                snapshot=snapshot,
                policy=policy,
                now=now,
                business_date=business_date,
            )
            details.update(forecast)
            if snapshot.actual_accepted_target_count == 0:
                status = "behind"
                self._services.alert(
                    code="DAILY_NO_PROGRESS",
                    severity="error",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    message="Daily occurrence has no accepted targets at 07:00",
                    details=details,
                )
            elif (
                snapshot.actual_accepted_target_count
                < snapshot.occurrence.expected_target_count
            ):
                status = "in_progress"
            if bool(forecast["eta_overline"]):
                status = "behind"
                self._services.alert(
                    code="DAILY_ETA_OVERLINE",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    message=(
                        "Static remaining-work forecast exceeds the 07:55 "
                        "target-ready guardrail"
                    ),
                    details=details,
                )
        elif stage == "v2_start_guardrail":
            late_scheme_ids: list[str] = []
            newly_late_scheme_ids: list[str] = []
            for summary in snapshot.items:
                item = summary.item
                if item.runtime_type != "blackbox_v2":
                    continue
                stored_sla_status = str(
                    getattr(item, "sla_status", "PENDING")
                ).upper()
                if stored_sla_status == "LATE":
                    late_scheme_ids.append(item.base_scheme_id)
                    continue
                if stored_sla_status != "PENDING":
                    continue
                projection = self._services.evaluate_v2_start(
                    item_id=item.item_id,
                    evaluated_at=now,
                )
                if projection.status == "LATE":
                    late_scheme_ids.append(item.base_scheme_id)
                    newly_late_scheme_ids.append(item.base_scheme_id)
                    self._services.alert(
                        code="V2_START_LATE",
                        severity="critical",
                        business_date=business_date,
                        occurrence_id=occurrence_id,
                        scheme_id=item.base_scheme_id,
                        message=(
                            "Blackbox V2 item had not started by 07:45"
                        ),
                        details={"reason": projection.reason},
                    )
            details["late_v2_scheme_ids"] = late_scheme_ids
            details["newly_late_v2_scheme_ids"] = newly_late_scheme_ids
            status = "late" if late_scheme_ids else "ok"
        elif stage == "target_sla":
            details["visibility_receipts_reconciled"] = (
                deadline_visibility_reconciled
            )
            projection = deadline_catchup.target_sla_projection
            if projection is None:
                stored_outcome = str(
                    getattr(
                        snapshot.occurrence,
                        "sla_outcome",
                        "PENDING",
                    )
                ).upper()
                details.update(
                    {
                        "sla_outcome": stored_outcome,
                        "sla_reason": getattr(
                            snapshot.occurrence,
                            "sla_reason",
                            None,
                        ),
                    }
                )
                status = (
                    "breached"
                    if stored_outcome == "BREACHED"
                    else (
                        "met"
                        if stored_outcome == "MET"
                        else "pending"
                    )
                )
            else:
                normalized_projection_status = str(
                    projection.status
                ).upper()
                details.update(
                    {
                        "sla_outcome": normalized_projection_status,
                        "accepted_target_count":
                            projection.accepted_target_count,
                        "expected_target_count":
                            projection.expected_target_count,
                        "sla_reason": projection.reason,
                    }
                )
                status_by_outcome = {
                    "BREACHED": "breached",
                    "MET": "met",
                    "PENDING": "pending",
                }
                if normalized_projection_status not in status_by_outcome:
                    raise RuntimeError(
                        "Unknown target SLA outcome: "
                        f"{normalized_projection_status}"
                    )
                status = status_by_outcome[normalized_projection_status]
        else:
            details["visibility_receipts_reconciled"] = (
                deadline_visibility_reconciled
            )
            details["deadline_catchup_late_v2_scheme_ids"] = list(
                deadline_catchup.late_v2_scheme_ids
            )
            if deadline_catchup.target_sla_projection is not None:
                details["sla_outcome"] = (
                    deadline_catchup.target_sla_projection.status
                )
            expired = self._services.expire_items(
                occurrence_id=occurrence_id,
                evaluated_at=now,
            )
            snapshot = self._services.read_snapshot(occurrence_id)
            details.update(
                {
                    "expired_item_count": expired,
                    "accepted_target_count":
                        snapshot.actual_accepted_target_count,
                    "expected_target_count":
                        snapshot.occurrence.expected_target_count,
                }
            )
            if (
                snapshot.actual_accepted_target_count
                < snapshot.occurrence.expected_target_count
            ):
                status = "incomplete"
                self._services.alert(
                    code="RECOVERY_CUTOFF_INCOMPLETE",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    message=(
                        "Daily occurrence remains incomplete at 08:30"
                    ),
                    details=details,
                )
            else:
                status = "complete"
        self._services.heartbeat(
            occurrence_id=occurrence_id,
            state=f"WATCHDOG_{stage.upper()}",
            details=details,
        )
        return WatchdogResult(
            business_date=business_date.isoformat(),
            occurrence_id=occurrence_id,
            stage=stage,
            status=status,
            details=details,
        )

    def _run_frozen_deadline_watchdog(
        self,
        *,
        stage: str,
        business_date: date,
        occurrence_id: int,
        now: datetime,
    ) -> WatchdogResult:
        """先完成冻结账本时点转换，再做所有可变控制面诊断。"""
        snapshot = self._services.read_snapshot(occurrence_id)
        _validate_frozen_snapshot_cardinality(snapshot)
        self._services.validate_occurrence_epoch(snapshot=snapshot)
        visibility_reconciled: int | None = None
        expired = 0

        # 08:30 cutoff 是独立持久化 fence；不得等待 visibility、
        # generation 或源表诊断。
        if stage == "recovery_cutoff":
            expired = self._services.expire_items(
                occurrence_id=occurrence_id,
                evaluated_at=now,
            )
            snapshot = self._services.read_snapshot(occurrence_id)
            _validate_frozen_snapshot_cardinality(snapshot)

        include_target_sla = stage in {
            "target_sla",
            "recovery_cutoff",
        }
        if include_target_sla:
            visibility_reconciled = self._reconcile_visibility_receipts(
                occurrence_id=occurrence_id,
                business_date=business_date,
                phase=(
                    "target_sla_pre_evaluate"
                    if stage == "target_sla"
                    else "recovery_cutoff_pre_evaluate"
                ),
            )
            if visibility_reconciled is not None:
                snapshot = self._services.read_snapshot(occurrence_id)
                _validate_frozen_snapshot_cardinality(snapshot)

        deadline_catchup = self._catch_up_deadline_projections(
            snapshot=snapshot,
            business_date=business_date,
            policy=None,
            include_v2=True,
            # Reconcile 是 crash-after-commit receipt 的补写优化，不是
            # write-once SLA 的门锁。失败时 evaluator 仍按现有
            # visible_at/accepted linkage 保守求值，不能令 outcome 永久
            # 停留在 PENDING。
            include_target_sla=include_target_sla,
        )
        snapshot = self._services.read_snapshot(occurrence_id)
        _validate_frozen_snapshot_cardinality(snapshot)
        details: dict[str, object] = {
            "accepted_target_count":
                snapshot.actual_accepted_target_count,
            "expected_target_count":
                snapshot.occurrence.expected_target_count,
            "visibility_receipts_reconciled":
                visibility_reconciled,
        }

        if stage == "databridge_readiness_guardrail":
            projection, readiness_details = (
                self._catch_up_databridge_readiness(
                    snapshot=snapshot,
                    business_date=business_date,
                    policy=None,
                    now=now,
                )
            )
            details.update(readiness_details)
            if projection is None:
                status = "pending"
            else:
                status = (
                    "late"
                    if str(projection.status).upper() == "LATE"
                    else "ok"
                )
        elif stage == "v2_start_guardrail":
            stored_late = [
                summary.item.base_scheme_id
                for summary in snapshot.items
                if (
                    summary.item.runtime_type == "blackbox_v2"
                    and str(
                        getattr(
                            summary.item,
                            "sla_status",
                            "PENDING",
                        )
                    ).upper()
                    == "LATE"
                )
            ]
            late_scheme_ids = sorted(
                set(stored_late)
                | set(deadline_catchup.late_v2_scheme_ids)
            )
            details["late_v2_scheme_ids"] = late_scheme_ids
            details["newly_late_v2_scheme_ids"] = list(
                deadline_catchup.newly_late_v2_scheme_ids
            )
            status = "late" if late_scheme_ids else "ok"
        elif stage == "target_sla":
            projection = deadline_catchup.target_sla_projection
            if projection is None:
                outcome = str(
                    getattr(
                        snapshot.occurrence,
                        "sla_outcome",
                        "PENDING",
                    )
                ).upper()
                reason = getattr(
                    snapshot.occurrence,
                    "sla_reason",
                    None,
                )
            else:
                outcome = str(projection.status).upper()
                reason = projection.reason
                details["accepted_target_count"] = (
                    projection.accepted_target_count
                )
                details["expected_target_count"] = (
                    projection.expected_target_count
                )
            details["sla_outcome"] = outcome
            details["sla_reason"] = reason
            status = {
                "BREACHED": "breached",
                "MET": "met",
                "PENDING": "pending",
            }.get(outcome)
            if status is None:
                raise RuntimeError(
                    f"Unknown target SLA outcome: {outcome}"
                )
        else:
            details["expired_item_count"] = expired
            details["deadline_catchup_late_v2_scheme_ids"] = list(
                deadline_catchup.late_v2_scheme_ids
            )
            if deadline_catchup.target_sla_projection is not None:
                details["sla_outcome"] = (
                    deadline_catchup.target_sla_projection.status
                )
            status = (
                "complete"
                if snapshot.actual_accepted_target_count
                == snapshot.occurrence.expected_target_count
                else "incomplete"
            )
            if status == "incomplete":
                self._services.alert(
                    code="RECOVERY_CUTOFF_INCOMPLETE",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    message=(
                        "Daily occurrence remains incomplete at 08:30"
                    ),
                    details=details,
                )

        self._append_frozen_watchdog_diagnostics(
            snapshot=snapshot,
            business_date=business_date,
            occurrence_id=occurrence_id,
            stage=stage,
            details=details,
        )
        self._services.heartbeat(
            occurrence_id=occurrence_id,
            state=f"WATCHDOG_{stage.upper()}",
            details=details,
        )
        return WatchdogResult(
            business_date=business_date.isoformat(),
            occurrence_id=occurrence_id,
            stage=stage,
            status=status,
            details=details,
        )

    def _append_late_write_diagnostics(
        self,
        *,
        snapshot: Any,
        business_date: date,
        occurrence_id: int,
        stage: str,
        details: dict[str, object],
    ) -> None:
        """仅依赖冻结 occurrence 日期执行晚写诊断，不依赖 generation 绑定。"""
        feature_date = date.fromisoformat(
            str(snapshot.occurrence.feature_date)
        ).isoformat()
        try:
            late_writes = self._services.detect_late_writes(
                business_date=business_date,
                feature_date=feature_date,
            )
        except Exception as exc:
            details["late_source_writes"] = []
            details["late_source_writes_skipped_reason"] = (
                "LATE_WRITE_PROBE_FAILED"
            )
            details["late_source_write_probe_error"] = (
                _bounded_exception(exc)
            )
            self._services.alert(
                code="ITEM_EXECUTION_AUDIT_FAILURE",
                severity="error",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message="Late-write probe failed after deadline audit",
                details={
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
            )
            return
        late_details = [
            {
                "table_name": finding.table_name,
                "late_row_count": int(finding.late_row_count),
                "latest_write_at": finding.latest_write_at,
            }
            for finding in late_writes
        ]
        details["late_source_writes"] = late_details
        details.pop("late_source_writes_skipped_reason", None)
        details.pop("late_source_write_probe_error", None)
        if late_details:
            self._services.alert(
                code="DATA_CONTRACT_BREACH",
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message=(
                    "Source rows inside the frozen input domain arrived "
                    "after the 06:30 contract cutoff"
                ),
                details={"stage": stage, "findings": late_details},
            )

    def _append_frozen_watchdog_diagnostics(
        self,
        *,
        snapshot: Any,
        business_date: date,
        occurrence_id: int,
        stage: str,
        details: dict[str, object],
    ) -> None:
        """在 deadline 转换后 best-effort 采集可变控制面诊断。"""
        policy = None
        try:
            loader = getattr(
                self._services,
                "load_current_audit_policy",
                self._services.load_policy,
            )
            policy = loader()
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
        except Exception as exc:
            details["control_plane_drift"] = _bounded_exception(exc)
            self._services.alert(
                code="DAILY_CONTROL_PLANE_DRIFT",
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message=(
                    "Current policy or direct authority differs from "
                    "the frozen occurrence"
                ),
                details={
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
            )

        building_generations: tuple[object, ...] = ()
        try:
            building_generations = (
                self._services.find_unbound_building_generations(
                    business_date=business_date,
                )
            )
        except Exception as exc:
            details["generation_diagnostic_error"] = (
                _bounded_exception(exc)
            )
            self._services.alert(
                code="ITEM_EXECUTION_AUDIT_FAILURE",
                severity="error",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message="Generation diagnostic failed after deadline audit",
                details={
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
            )
        if building_generations:
            details["unbound_building_generations"] = (
                _generation_id_summary(building_generations)
            )
            self._services.alert(
                code="GENERATION_BUILD_FAILED",
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message=(
                    "Watchdog found an unbound BUILDING input generation"
                ),
                details={
                    "stage": stage,
                    "reason": "UNBOUND_BUILDING_GENERATION",
                    "generations": _generation_id_summary(
                        building_generations
                    ),
                },
            )

        self._append_late_write_diagnostics(
            snapshot=snapshot,
            business_date=business_date,
            occurrence_id=occurrence_id,
            stage=stage,
            details=details,
        )

    def _reconcile_visibility_receipts(
        self,
        *,
        occurrence_id: int,
        business_date: date,
        phase: str,
    ) -> int | None:
        try:
            return int(
                self._services.reconcile_occurrence_visibility_receipts(
                    occurrence_id=occurrence_id,
                )
            )
        except Exception as exc:
            details = {
                "phase": phase,
                "reason": "VISIBILITY_RECEIPT_RECONCILE_FAILED",
                "error": _bounded_exception(exc),
            }
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state="VISIBILITY_RECONCILIATION_FAILED",
                details=details,
            )
            self._services.alert(
                code="VISIBILITY_RECEIPT_RECONCILE_FAILED",
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message=(
                    "Commit-visible receipt reconciliation failed; "
                    "completion and SLA evaluation are blocked"
                ),
                details=details,
            )
            return None

    def _catch_up_databridge_readiness(
        self,
        *,
        snapshot: Any,
        business_date: date,
        policy: Any | None,
        now: datetime,
        emit_alert: bool = True,
    ) -> tuple[DataBridgeReadinessProjection | None, dict[str, object]]:
        """按冻结 06:55 时点和可信 sealed_at 投影；late 不阻断刷新。"""
        guardrail_at = _databridge_readiness_guardrail_at(
            snapshot,
            business_date=business_date,
            policy=policy,
        )
        if now < guardrail_at:
            return None, {
                "databridge_readiness_status": "PENDING",
                "databridge_readiness_guardrail_at":
                    guardrail_at.isoformat(),
            }
        projection = self._services.project_databridge_readiness(
            snapshot=snapshot,
            guardrail_at=guardrail_at,
        )
        sealed_at = projection.sealed_at
        details: dict[str, object] = {
            "databridge_readiness_status":
                str(projection.status).upper(),
            "databridge_readiness_reason": projection.reason,
            "databridge_generation_id": projection.generation_id,
            "databridge_readiness_sealed_at": (
                None
                if sealed_at is None
                else sealed_at.isoformat()
            ),
            "databridge_readiness_guardrail_at":
                guardrail_at.isoformat(),
        }
        if (
            str(projection.status).upper() == "LATE"
            and emit_alert
        ):
            self._services.alert(
                code="DATABRIDGE_READINESS_LATE",
                severity="critical",
                business_date=business_date,
                occurrence_id=snapshot.occurrence.occurrence_id,
                message=(
                    "DataBridge generation was not ready by 06:55; "
                    "same-day refresh continues until 08:30"
                ),
                details={
                    **details,
                    "stage": "databridge_readiness_guardrail",
                },
            )
        return projection, details

    def _catch_up_deadline_projections(
        self,
        *,
        snapshot: Any,
        business_date: date,
        policy: Any | None,
        include_v2: bool | None = None,
        include_target_sla: bool | None = None,
    ) -> _DeadlineCatchupResult:
        """重入时补写已到期且仍为 PENDING 的 write-once 审计。"""
        evaluated_at = self._services.now().astimezone(
            ZoneInfo(
                policy.timezone
                if policy is not None
                else "Asia/Shanghai"
            )
        )
        if include_v2 is None:
            if policy is None:
                raise ValueError(
                    "policy is required when include_v2 is not explicit"
                )
            guardrail_at = datetime.combine(
                business_date,
                policy.v2_start_guardrail,
                tzinfo=ZoneInfo(policy.timezone),
            )
            include_v2 = evaluated_at >= guardrail_at
        late_v2_scheme_ids: list[str] = []
        newly_late_v2_scheme_ids: list[str] = []
        if include_v2:
            for summary in snapshot.items:
                item = summary.item
                if (
                    item.runtime_type != "blackbox_v2"
                    or str(getattr(item, "sla_status", "PENDING")).upper()
                    != "PENDING"
                ):
                    continue
                projection = self._services.evaluate_v2_start(
                    item_id=item.item_id,
                    evaluated_at=evaluated_at,
                )
                if projection.status != "LATE":
                    continue
                late_v2_scheme_ids.append(item.base_scheme_id)
                if getattr(
                    projection,
                    "newly_persisted",
                    True,
                ):
                    newly_late_v2_scheme_ids.append(
                        item.base_scheme_id
                    )
                    self._services.alert(
                        code="V2_START_LATE",
                        severity="critical",
                        business_date=business_date,
                        occurrence_id=(
                            snapshot.occurrence.occurrence_id
                        ),
                        scheme_id=item.base_scheme_id,
                        message=(
                            "Blackbox V2 item had not started by 07:45"
                        ),
                        details={
                            "reason": projection.reason,
                            "trigger": "deadline_catchup",
                        },
                    )

        target_projection = None
        if include_target_sla is None:
            if policy is None:
                raise ValueError(
                    "policy is required when include_target_sla is "
                    "not explicit"
                )
            sla_deadline_at = datetime.combine(
                business_date,
                policy.sla_deadline,
                tzinfo=ZoneInfo(policy.timezone),
            )
            include_target_sla = evaluated_at >= sla_deadline_at
        if (
            include_target_sla
            and str(
                getattr(
                    snapshot.occurrence,
                    "sla_outcome",
                    "PENDING",
                )
            ).upper()
            == "PENDING"
        ):
            target_projection = self._services.evaluate_target_sla(
                occurrence_id=snapshot.occurrence.occurrence_id,
                evaluated_at=evaluated_at,
            )
            if (
                target_projection.status == "BREACHED"
                and getattr(
                    target_projection,
                    "newly_persisted",
                    True,
                )
            ):
                self._services.alert(
                    code="DAILY_TARGET_SLA_BREACHED",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=snapshot.occurrence.occurrence_id,
                    message=(
                        "Daily occurrence accepted "
                        f"{target_projection.accepted_target_count}/"
                        f"{target_projection.expected_target_count}"
                        " targets at 08:00"
                    ),
                    details={
                        "accepted_target_count":
                            target_projection.accepted_target_count,
                        "expected_target_count":
                            target_projection.expected_target_count,
                        "sla_reason": target_projection.reason,
                        "trigger": "deadline_catchup",
                    },
                )
        return _DeadlineCatchupResult(
            late_v2_scheme_ids=tuple(late_v2_scheme_ids),
            newly_late_v2_scheme_ids=tuple(
                newly_late_v2_scheme_ids
            ),
            target_sla_projection=target_projection,
        )

    def run_operator_recovery(
        self,
        *,
        scheme_id: str,
        run_date: str | date | None = None,
    ) -> DailyRuntimeResult:
        policy = self._services.load_policy()
        if scheme_id not in policy.schemes:
            raise ValueError(
                f"scheme is not in frozen daily policy: {scheme_id}"
            )
        now = self._services.now().astimezone(ZoneInfo(policy.timezone))
        business_date = _business_date(run_date, now=now)
        if business_date != now.date():
            return DailyRuntimeResult(
                business_date=business_date.isoformat(),
                occurrence_id=None,
                status="cross_day_rejected",
            )
        cutoff = datetime.combine(
            business_date,
            policy.recovery_cutoff,
            tzinfo=ZoneInfo(policy.timezone),
        )
        with _optional_owner(
            self._services.occurrence_lock(business_date)
        ) as acquired:
            if not acquired:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=None,
                    status="already_owned",
                )
            occurrence_id = self._services.find_occurrence_id(
                business_date
            )
            if occurrence_id is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=None,
                    status="occurrence_missing",
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            self._require_frozen_occurrence_policy_version(
                snapshot=snapshot,
                policy=policy,
            )
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
            if self._reconcile_visibility_receipts(
                occurrence_id=occurrence_id,
                business_date=business_date,
                phase="operator_recovery_pre_snapshot",
            ) is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="visibility_reconciliation_failed",
                )
            snapshot = self._services.read_snapshot(occurrence_id)
            _validate_snapshot_cardinality(snapshot, policy=policy)
            self._services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )
            self._catch_up_deadline_projections(
                snapshot=snapshot,
                business_date=business_date,
                policy=policy,
            )
            now = self._services.now().astimezone(
                ZoneInfo(policy.timezone)
            )
            if now >= cutoff:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="recovery_cutoff",
                )
            target = next(
                (
                    summary.item
                    for summary in snapshot.items
                    if summary.item.base_scheme_id == scheme_id
                ),
                None,
            )
            if target is None:
                raise RuntimeError(
                    "requested policy scheme is missing from frozen occurrence"
                )
            if target.state == "SUCCESS":
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="complete",
                )
            if target.state in {
                "FAILED_TERMINAL",
                "EXPIRED",
            }:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="terminal",
                )
            if target.state == "RUNNING" or (
                target.state == "ABANDONED"
                and target.failure_code
                == "ABANDONED_FENCE_PENDING_CLEANUP"
            ):
                try:
                    fenced_attempt = self._services.fence_item(
                        item_id=target.item_id
                    )
                    cleanup_confirmed = (
                        self._services.cleanup_fenced_attempt(
                            item=target,
                            fenced_attempt=fenced_attempt,
                        )
                    )
                except Exception:
                    cleanup_confirmed = False
                if cleanup_confirmed is not True:
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=occurrence_id,
                        status="recovery_blocked",
                    )
                try:
                    self._services.confirm_orphan_cleanup(
                        fenced_attempt=fenced_attempt,
                    )
                except Exception:
                    return DailyRuntimeResult(
                        business_date=business_date.isoformat(),
                        occurrence_id=occurrence_id,
                        status="recovery_blocked",
                    )
                snapshot = self._services.read_snapshot(occurrence_id)
                target = next(
                    summary.item
                    for summary in snapshot.items
                    if summary.item.base_scheme_id == scheme_id
                )
            if target.input_generation_id is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="generation_unbound",
                )
            inputs = self._services.read_frozen_inputs(
                occurrence_id=occurrence_id,
                policy=policy,
            )
            native_generation, databridge_generation = (
                self._services.resolve_bound_generations(
                    snapshot=snapshot,
                    inputs=inputs,
                    policy=policy,
                )
            )
            try:
                _validate_atomic_generation_bindings(
                    snapshot=snapshot,
                    native_generation=native_generation,
                    databridge_generation=databridge_generation,
                )
            except Exception as exc:
                self._services.alert(
                    code="GENERATION_BUILD_FAILED",
                    severity="critical",
                    business_date=business_date,
                    occurrence_id=occurrence_id,
                    scheme_id=scheme_id,
                    message=(
                        "Operator recovery rejected incomplete generation "
                        "binding"
                    ),
                    details={"error": _bounded_exception(exc)},
                )
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="generation_invalid",
                )
            required_generation = (
                native_generation
                if target.runtime_type == "native_adapter"
                else databridge_generation
            )
            if required_generation is None:
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="generation_invalid",
                )
            self._services.validate_frozen_calendar(
                native_generation=native_generation,
                inputs=inputs,
                policy=policy,
            )
            running_ids = tuple(
                summary.item.base_scheme_id
                for summary in snapshot.items
                if (
                    summary.item.state == "RUNNING"
                    and summary.item.base_scheme_id != scheme_id
                )
            )
            decisions = self._services.dispatch_decisions(
                business_date=business_date,
                snapshot=snapshot,
                native_generation=native_generation,
                databridge_generation=databridge_generation,
                running_scheme_ids=running_ids,
                running_resource_classes=(),
                now=now,
            )
            decision = next(
                item
                for item in decisions
                if item.scheme_id == scheme_id
            )
            if decision.action != "DISPATCH":
                return DailyRuntimeResult(
                    business_date=business_date.isoformat(),
                    occurrence_id=occurrence_id,
                    status="not_dispatchable",
                )
            execution = self._services.execute_item(
                item_id=target.item_id,
                trigger_origin="operator_recovery",
            )
            status = (
                "complete"
                if execution.status == "success"
                else execution.status
            )
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state="OPERATOR_RECOVERY",
                details={
                    "scheme_id": scheme_id,
                    "execution_status": execution.status,
                },
            )
            return DailyRuntimeResult(
                business_date=business_date.isoformat(),
                occurrence_id=occurrence_id,
                status=status,
                dispatched_scheme_ids=(scheme_id,),
            )

    def _report_building_generation_failure(
        self,
        *,
        snapshot: Any,
        building_generations: tuple[object, ...],
        business_date: date,
        occurrence_id: int,
    ) -> None:
        message = (
            "Unbound BUILDING input generation requires operator "
            "intervention: "
            + _generation_id_summary(building_generations)
        )
        self._mark_generation_failures(
            snapshot=snapshot,
            outcome=GenerationBuildOutcome(
                native_generation=None,
                databridge_generation=None,
                native_error=message,
                databridge_error=message,
            ),
            business_date=business_date,
            occurrence_id=occurrence_id,
        )

    def _recover_running_items(
        self,
        *,
        snapshot: Any,
        business_date: date,
        occurrence_id: int,
    ) -> bool:
        blocked: list[str] = []
        cleanup_errors: dict[str, str] = {}
        fenced_attempts: list[tuple[Any, Any]] = []
        for summary in snapshot.items:
            item = summary.item
            pending_cleanup = (
                item.state == "ABANDONED"
                and item.failure_code
                == "ABANDONED_FENCE_PENDING_CLEANUP"
            )
            if item.state != "RUNNING" and not pending_cleanup:
                continue
            try:
                fenced_attempt = self._services.fence_item(
                    item_id=item.item_id
                )
            except Exception as exc:
                blocked.append(item.base_scheme_id)
                cleanup_errors[item.base_scheme_id] = _bounded_exception(
                    exc
                )
                continue
            fenced_attempts.append((item, fenced_attempt))
        for item, fenced_attempt in fenced_attempts:
            try:
                cleanup_confirmed = (
                    self._services.cleanup_fenced_attempt(
                        item=item,
                        fenced_attempt=fenced_attempt,
                    )
                )
            except Exception as exc:
                cleanup_confirmed = False
                cleanup_errors[item.base_scheme_id] = _bounded_exception(
                    exc
                )
            if cleanup_confirmed is not True:
                blocked.append(item.base_scheme_id)
                continue
            try:
                self._services.confirm_orphan_cleanup(
                    fenced_attempt=fenced_attempt,
                )
            except Exception as exc:
                blocked.append(item.base_scheme_id)
                cleanup_errors[item.base_scheme_id] = (
                    _bounded_exception(exc)
                )
        if not blocked:
            return True
        details = {
            "scheme_ids": blocked,
            "cleanup_errors": cleanup_errors,
        }
        self._services.heartbeat(
            occurrence_id=occurrence_id,
            state="RECOVERY_BLOCKED",
            details=details,
        )
        self._services.alert(
            code="ORPHAN_CLEANUP_UNCONFIRMED",
            severity="critical",
            business_date=business_date,
            occurrence_id=occurrence_id,
            message="A running attempt could not be safely fenced for recovery",
            details=details,
        )
        return False

    def _build_input_generations(
        self,
        *,
        occurrence_id: int,
        business_date: date,
        inputs: OccurrenceInputs,
        policy: Any,
        snapshot: Any,
        on_native_ready: Callable[[Any], None],
        resource_occupancy: _GenerationAvailability,
    ) -> GenerationBuildOutcome:
        try:
            native_generation, databridge_generation = (
                self._services.resolve_bound_generations(
                    snapshot=snapshot,
                    inputs=inputs,
                    policy=policy,
                )
            )
        except Exception as exc:
            message = _bounded_exception(exc)
            return GenerationBuildOutcome(
                native_generation=None,
                databridge_generation=None,
                native_error=message,
                databridge_error=message,
            )
        if databridge_generation is not None and native_generation is None:
            return GenerationBuildOutcome(
                native_generation=None,
                databridge_generation=None,
                native_error=(
                    "DataBridge generation has no verified Native relation"
                ),
                databridge_error=(
                    "DataBridge generation has no verified Native relation"
                ),
            )

        def refresh_current():
            def occurrence_validator() -> Mapping[str, object]:
                current_snapshot = self._services.read_snapshot(
                    occurrence_id
                )
                occurrence = current_snapshot.occurrence
                frozen_epoch = occurrence.policy_json.get(
                    "daily_coordinator_epoch"
                )
                if not isinstance(frozen_epoch, Mapping):
                    raise RuntimeError(
                        "daily occurrence publication epoch is unavailable"
                    )
                return {
                    "occurrence_id": occurrence.occurrence_id,
                    "business_date": occurrence.predict_date,
                    "daily_coordinator_epoch": dict(frozen_epoch),
                }

            with resource_occupancy.occupy("databridge_refresh"):
                return self._services.refresh_databridge(
                    business_date=business_date.isoformat(),
                    feature_date=inputs.feature_date,
                    publication_capability=(
                        DailyCoordinatorPublicationCapability.from_occurrence(
                            occurrence_id=occurrence_id,
                            business_date=business_date.isoformat(),
                            policy_json=snapshot.occurrence.policy_json,
                            occurrence_validator=occurrence_validator,
                        )
                    ),
                )

        def validate_and_publish_native(
            context: Any,
            *,
            register: bool,
        ) -> None:
            self._services.validate_frozen_calendar(
                native_generation=context,
                inputs=inputs,
                policy=policy,
            )
            if register:
                self._services.register_native_generation(
                    context,
                    occurrence_id=occurrence_id,
                )
            current_snapshot = self._services.read_snapshot(
                occurrence_id
            )
            _validate_runtime_generation_binding(
                snapshot=current_snapshot,
                runtime_type="native_adapter",
                generation=context,
            )
            on_native_ready(context)

        native_ready_published = False
        if native_generation is None:
            try:
                recovered_native = (
                    self._services.recover_native_generation(
                        business_date=business_date.isoformat(),
                        feature_date=inputs.feature_date,
                    )
                )
            except Exception as exc:
                message = _bounded_exception(exc)
                return GenerationBuildOutcome(
                    native_generation=None,
                    databridge_generation=None,
                    native_error=message,
                    databridge_error=message,
                )
            if recovered_native is not None:
                try:
                    validate_and_publish_native(
                        recovered_native,
                        register=True,
                    )
                except Exception as exc:
                    message = _bounded_exception(exc)
                    return GenerationBuildOutcome(
                        native_generation=None,
                        databridge_generation=None,
                        native_error=message,
                        databridge_error=message,
                    )
                native_generation = recovered_native
                native_ready_published = True

        if native_generation is not None:
            if not native_ready_published:
                try:
                    validate_and_publish_native(
                        native_generation,
                        register=False,
                    )
                except Exception as exc:
                    message = _bounded_exception(exc)
                    return GenerationBuildOutcome(
                        native_generation=None,
                        databridge_generation=None,
                        native_error=message,
                        databridge_error=message,
                    )
            if databridge_generation is not None:
                try:
                    _validate_runtime_generation_binding(
                        snapshot=self._services.read_snapshot(
                            occurrence_id
                        ),
                        runtime_type="blackbox_v2",
                        generation=databridge_generation,
                    )
                except Exception as exc:
                    return GenerationBuildOutcome(
                        native_generation=native_generation,
                        databridge_generation=None,
                        databridge_error=_bounded_exception(exc),
                    )
                return GenerationBuildOutcome(
                    native_generation=native_generation,
                    databridge_generation=databridge_generation,
                )
            try:
                recovered_databridge = (
                    self._services.recover_databridge_generation(
                        business_date=business_date.isoformat(),
                        feature_date=inputs.feature_date,
                        native_generation=native_generation,
                    )
                )
                if recovered_databridge is not None:
                    self._services.register_databridge_generation(
                        recovered_databridge,
                        occurrence_id=occurrence_id,
                    )
                    _validate_runtime_generation_binding(
                        snapshot=self._services.read_snapshot(
                            occurrence_id
                        ),
                        runtime_type="blackbox_v2",
                        generation=recovered_databridge,
                    )
                    return GenerationBuildOutcome(
                        native_generation=native_generation,
                        databridge_generation=recovered_databridge,
                    )
            except Exception as exc:
                return GenerationBuildOutcome(
                    native_generation=native_generation,
                    databridge_generation=None,
                    databridge_error=_bounded_exception(exc),
                )
            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="daily-input",
            ) as pool:
                refresh_future = pool.submit(
                    refresh_current,
                )
                current, refresh_error = _future_value(refresh_future)
            native_error = None
        else:
            timezone_info = ZoneInfo(policy.timezone)
            source_contract_cutoff = datetime.combine(
                business_date,
                policy.not_before,
                tzinfo=timezone_info,
            )
            capture_not_after = datetime.combine(
                business_date,
                policy.native_capture_deadline,
                tzinfo=timezone_info,
            )

            def build_new_native():
                with resource_occupancy.occupy("native_export"):
                    return self._services.build_native_generation(
                        business_date=business_date.isoformat(),
                        feature_date=inputs.feature_date,
                        capture_not_after=capture_not_after,
                        source_contract_cutoff=source_contract_cutoff,
                    )

            current_time = self._services.now().astimezone(
                timezone_info
            )
            if current_time >= capture_not_after:
                native_generation = None
                native_error = (
                    "Native snapshot capture window expired before export: "
                    f"now={current_time.isoformat()} "
                    f"not_after={capture_not_after.isoformat()}"
                )
                with ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="daily-input",
                ) as pool:
                    refresh_future = pool.submit(
                        refresh_current,
                    )
                    current, refresh_error = _future_value(
                        refresh_future
                    )
            else:
                with ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="daily-input",
                ) as pool:
                    refresh_future = pool.submit(
                        refresh_current,
                    )
                    native_future = pool.submit(
                        build_new_native,
                    )
                    native_generation, native_error = _future_value(
                        native_future
                    )
                    if native_generation is not None:
                        try:
                            validate_and_publish_native(
                                native_generation,
                                register=True,
                            )
                        except Exception as exc:
                            native_generation = None
                            native_error = _bounded_exception(exc)
                    current, refresh_error = _future_value(
                        refresh_future
                    )
        if native_generation is None:
            return GenerationBuildOutcome(
                native_generation=None,
                databridge_generation=None,
                native_error=native_error,
                databridge_error=(
                    refresh_error
                    or "Native calendar generation is unavailable"
                ),
            )
        if current is None:
            return GenerationBuildOutcome(
                native_generation=native_generation,
                databridge_generation=None,
                databridge_error=refresh_error,
            )
        try:
            with resource_occupancy.occupy("databridge_pack"):
                databridge_generation = (
                    self._services.build_databridge_generation(
                        current=current,
                        native_generation=native_generation,
                        business_date=business_date.isoformat(),
                        feature_date=inputs.feature_date,
                    )
                )
                self._services.register_databridge_generation(
                    databridge_generation,
                    occurrence_id=occurrence_id,
                )
                _validate_runtime_generation_binding(
                    snapshot=self._services.read_snapshot(occurrence_id),
                    runtime_type="blackbox_v2",
                    generation=databridge_generation,
                )
        except Exception as exc:
            return GenerationBuildOutcome(
                native_generation=native_generation,
                databridge_generation=None,
                databridge_error=_bounded_exception(exc),
            )
        return GenerationBuildOutcome(
            native_generation=native_generation,
            databridge_generation=databridge_generation,
        )

    def _mark_unsupported_items(
        self,
        *,
        snapshot: Any,
        policy: Any,
    ) -> None:
        for summary in snapshot.items:
            item = summary.item
            scheme_policy = policy.schemes[item.base_scheme_id]
            if (
                scheme_policy.input_compatibility == "unsupported"
                and item.state in {"PENDING", "RETRY_WAIT", "ABANDONED"}
            ):
                self._services.fail_item(
                    item_id=item.item_id,
                    failure_code="NATIVE_GENERATION_UNSUPPORTED",
                    message="Native scheme has no generation-v1 input adapter",
                )
                self._services.alert(
                    code="NATIVE_GENERATION_UNSUPPORTED",
                    severity="critical",
                    business_date=date.fromisoformat(
                        snapshot.occurrence.predict_date
                    ),
                    occurrence_id=(
                        snapshot.occurrence.occurrence_id
                    ),
                    scheme_id=item.base_scheme_id,
                    message=(
                        "Native scheme is excluded because no frozen "
                        "generation adapter exists"
                    ),
                )

    def _mark_generation_failures(
        self,
        *,
        snapshot: Any,
        outcome: GenerationBuildOutcome,
        business_date: date,
        occurrence_id: int,
    ) -> None:
        failures: list[tuple[Any, str, str]] = []
        for summary in snapshot.items:
            item = summary.item
            if item.state not in {"PENDING", "RETRY_WAIT", "ABANDONED"}:
                continue
            if (
                item.runtime_type == "native_adapter"
                and outcome.native_generation is None
            ):
                message = outcome.native_error or "Native generation failed"
                failures.append(
                    (item, _generation_failure_code(message), message)
                )
            elif (
                item.runtime_type == "blackbox_v2"
                and outcome.databridge_generation is None
            ):
                message = (
                    outcome.databridge_error
                    or "DataBridge generation failed"
                )
                failures.append(
                    (item, _generation_failure_code(message), message)
                )
        for item, failure_code, message in failures:
            self._services.fail_item(
                item_id=item.item_id,
                failure_code=failure_code,
                message=message,
            )
        if outcome.failed:
            details = {
                "native_error": outcome.native_error,
                "databridge_error": outcome.databridge_error,
                "failed_scheme_ids": [
                    item.base_scheme_id
                    for item, _failure_code, _message in failures
                ],
                "failure_codes": sorted(
                    {
                        failure_code
                        for _item, failure_code, _message in failures
                    }
                ),
            }
            self._services.heartbeat(
                occurrence_id=occurrence_id,
                state="GENERATION_FAILED",
                details=details,
            )
            alert_code = (
                details["failure_codes"][0]
                if len(details["failure_codes"]) == 1
                else "GENERATION_BUILD_FAILED"
            )
            self._services.alert(
                code=str(alert_code),
                severity="critical",
                business_date=business_date,
                occurrence_id=occurrence_id,
                message="Daily input generation could not be sealed",
                details=details,
            )

    def _drive_items(
        self,
        *,
        occurrence_id: int,
        business_date: date,
        policy: Any,
        generation_availability: _GenerationAvailability,
        trigger_origin: str,
    ) -> _DriveItemsResult:
        dispatched: list[str] = []
        active: dict[Future[Any], str] = {}
        generation_failures_marked = False
        if (
            policy.native_max_concurrency != 2
            or policy.v2_max_concurrency != 2
        ):
            raise RuntimeError(
                "daily runtime only permits the approved 2 Native / 2 V2 "
                "same-Mac pool limits"
            )
        pool_by_name = {
            "native": ThreadPoolExecutor(
                max_workers=policy.native_max_concurrency,
                thread_name_prefix="daily-native",
            ),
            "v2": ThreadPoolExecutor(
                max_workers=policy.v2_max_concurrency,
                thread_name_prefix="daily-v2",
            ),
        }
        try:
            while True:
                (
                    generations,
                    generation_pipeline_done,
                    active_input_resources,
                ) = (
                    generation_availability.snapshot()
                )
                snapshot = self._services.read_snapshot(occurrence_id)
                if (
                    generation_pipeline_done
                    and generations.failed
                    and not generation_failures_marked
                ):
                    self._mark_generation_failures(
                        snapshot=snapshot,
                        outcome=generations,
                        business_date=business_date,
                        occurrence_id=occurrence_id,
                    )
                    generation_failures_marked = True
                    snapshot = self._services.read_snapshot(
                        occurrence_id
                    )
                if (
                    snapshot.actual_accepted_target_count
                    == snapshot.occurrence.expected_target_count
                ):
                    break
                now = self._services.now().astimezone(
                    ZoneInfo(policy.timezone)
                )
                cutoff = datetime.combine(
                    business_date,
                    policy.recovery_cutoff,
                    tzinfo=ZoneInfo(policy.timezone),
                )
                if now >= cutoff:
                    self._services.expire_items(
                        occurrence_id=occurrence_id,
                        evaluated_at=now,
                    )
                    break
                running = tuple(active.values())
                self._services.heartbeat(
                    occurrence_id=occurrence_id,
                    state="RUNNING",
                    details={
                        "running_scheme_ids": list(running),
                        "accepted_target_count":
                            snapshot.actual_accepted_target_count,
                        "expected_target_count":
                            snapshot.occurrence.expected_target_count,
                    },
                )
                decisions = self._services.dispatch_decisions(
                    business_date=business_date,
                    snapshot=snapshot,
                    native_generation=generations.native_generation,
                    databridge_generation=(
                        generations.databridge_generation
                    ),
                    running_scheme_ids=running,
                    running_resource_classes=active_input_resources,
                    now=now,
                )
                item_by_scheme = {
                    summary.item.base_scheme_id: summary.item
                    for summary in snapshot.items
                }
                started = False
                for decision in decisions:
                    if decision.action != "DISPATCH":
                        continue
                    if decision.scheme_id in active.values():
                        continue
                    item = item_by_scheme[decision.scheme_id]
                    origin = _item_trigger_origin(
                        item_state=item.state,
                        default_origin=trigger_origin,
                    )
                    future = pool_by_name[decision.pool].submit(
                        self._services.execute_item,
                        item_id=item.item_id,
                        trigger_origin=origin,
                    )
                    active[future] = decision.scheme_id
                    dispatched.append(decision.scheme_id)
                    started = True
                if active:
                    done, _pending = wait(
                        tuple(active),
                        timeout=1.0,
                        return_when=FIRST_COMPLETED,
                    )
                    claim_rejected = False
                    cleanup_blocked = False
                    for future in done:
                        scheme_id = active.pop(future)
                        try:
                            execution = future.result()
                        except Exception as exc:
                            self._services.alert(
                                code="ITEM_EXECUTION_AUDIT_FAILURE",
                                severity="critical",
                                business_date=business_date,
                                occurrence_id=occurrence_id,
                                scheme_id=scheme_id,
                                message=(
                                    "Scheduled item raised outside its "
                                    "ledger failure audit"
                                ),
                                details={
                                    "error": _bounded_exception(exc)
                                },
                            )
                            raise
                        if execution.status == "claim_rejected":
                            claim_rejected = True
                            self._services.alert(
                                code="ITEM_CLAIM_REJECTED",
                                severity="warning",
                                business_date=business_date,
                                occurrence_id=occurrence_id,
                                scheme_id=scheme_id,
                                message=(
                                    "Scheduled item claim was rejected; "
                                    "the coordinator will resnapshot"
                                ),
                                details={
                                    "failure_code":
                                        execution.failure_code,
                                    "error_message":
                                        execution.error_message,
                                },
                            )
                        if (
                            execution.status == "failed"
                            and execution.failure_code
                            != "TRANSIENT_INFRA"
                        ):
                            self._services.alert(
                                code="ITEM_TERMINAL_FAILURE",
                                severity="critical",
                                business_date=business_date,
                                occurrence_id=occurrence_id,
                                scheme_id=scheme_id,
                                message=(
                                    "Daily scheduled item reached a "
                                    "terminal failure"
                                ),
                                details={
                                    "failure_code":
                                        execution.failure_code,
                                    "error_message":
                                        execution.error_message,
                                },
                            )
                        if execution.status in {
                            "fenced_pending_cleanup",
                            "recovery_blocked",
                        }:
                            cleanup_blocked = True
                            cleanup_details = {
                                "execution_status": execution.status,
                                "run_id": getattr(
                                    execution,
                                    "run_id",
                                    None,
                                ),
                                "attempt_no": getattr(
                                    execution,
                                    "attempt_no",
                                    None,
                                ),
                                "failure_code": getattr(
                                    execution,
                                    "failure_code",
                                    None,
                                ),
                                "error_message": getattr(
                                    execution,
                                    "error_message",
                                    None,
                                ),
                            }
                            self._services.heartbeat(
                                occurrence_id=occurrence_id,
                                state="RECOVERY_BLOCKED",
                                details={
                                    "scheme_id": scheme_id,
                                    **cleanup_details,
                                },
                            )
                            self._services.alert(
                                code=(
                                    "SCHEDULE_ATTEMPT_CLEANUP_BLOCKED"
                                ),
                                severity="critical",
                                business_date=business_date,
                                occurrence_id=occurrence_id,
                                scheme_id=scheme_id,
                                message=(
                                    "Scheduled attempt cleanup is not "
                                    "confirmed; dispatch stopped until the "
                                    "next coordinator recovery"
                                ),
                                details=cleanup_details,
                            )
                    if cleanup_blocked:
                        return _DriveItemsResult(
                            status="recovery_blocked",
                            dispatched_scheme_ids=tuple(dispatched),
                        )
                    if claim_rejected:
                        self._services.wait_for_progress(1.0)
                    continue
                terminal_or_success = all(
                    summary.item.state
                    in {"SUCCESS", "FAILED_TERMINAL", "EXPIRED"}
                    for summary in snapshot.items
                )
                if terminal_or_success:
                    break
                if not started:
                    self._services.wait_for_progress(1.0)
        finally:
            for pool in pool_by_name.values():
                pool.shutdown(wait=True, cancel_futures=False)
        return _DriveItemsResult(
            status="finished",
            dispatched_scheme_ids=tuple(dispatched),
        )


def _item_trigger_origin(
    *,
    item_state: str,
    default_origin: str,
) -> str:
    """按持久化状态选择 claim origin，避免把恢复误记为自动重试。"""
    if item_state == "RETRY_WAIT":
        return "auto_retry"
    if item_state == "ABANDONED":
        if default_origin == "operator_recovery":
            return "operator_recovery"
        return "startup_catchup"
    return default_origin


def _business_date(
    value: str | date | None,
    *,
    now: datetime,
) -> date:
    if value is None:
        return now.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _validate_snapshot_cardinality(snapshot: Any, *, policy: Any) -> None:
    if (
        snapshot.actual_item_count != policy.expected_item_count
        or snapshot.actual_target_count != policy.expected_target_count
        or snapshot.occurrence.expected_item_count
        != policy.expected_item_count
        or snapshot.occurrence.expected_target_count
        != policy.expected_target_count
    ):
        raise RuntimeError(
            "daily occurrence cardinality differs from versioned policy"
        )


def _validate_frozen_snapshot_cardinality(snapshot: Any) -> None:
    """只按 occurrence 自身冻结的动态期望数量校验账本。"""
    if (
        snapshot.actual_item_count
        != snapshot.occurrence.expected_item_count
        or snapshot.actual_target_count
        != snapshot.occurrence.expected_target_count
    ):
        raise RuntimeError(
            "daily occurrence cardinality differs from frozen occurrence"
        )


def _progress_forecast(
    *,
    snapshot: Any,
    policy: Any,
    now: datetime,
    business_date: date,
) -> dict[str, object]:
    native_seconds: list[int] = []
    v2_seconds: list[int] = []
    terminal_missing_targets: list[str] = []
    terminal_states = {
        "FAILED_TERMINAL",
        "EXPIRED",
    }
    for summary in snapshot.items:
        item = summary.item
        if item.state == "SUCCESS":
            continue
        if item.state in terminal_states:
            if summary.accepted_target_count < summary.target_count:
                terminal_missing_targets.append(item.base_scheme_id)
            continue
        seconds = int(
            policy.schemes[item.base_scheme_id].estimated_cold_sec
        )
        if item.runtime_type == "native_adapter":
            native_seconds.append(seconds)
        else:
            v2_seconds.append(max(seconds, policy.v2_timeout_sec))
    native_makespan = _two_lane_lower_bound(native_seconds)
    v2_makespan = _two_lane_lower_bound(v2_seconds)
    remaining_sec = max(native_makespan, v2_makespan)
    projected_finish = now + timedelta(seconds=remaining_sec)
    target_ready = datetime.combine(
        business_date,
        policy.target_ready,
        tzinfo=ZoneInfo(policy.timezone),
    )
    eta_overline = (
        bool(terminal_missing_targets)
        or projected_finish > target_ready
    )
    return {
        "optimistic_remaining_sec": remaining_sec,
        "projected_finish_at": projected_finish.isoformat(),
        "target_ready_at": target_ready.isoformat(),
        "terminal_missing_scheme_ids":
            sorted(terminal_missing_targets),
        "eta_overline": eta_overline,
        "eta_basis": "STATIC_TWO_LANE_OPTIMISTIC_LOWER_BOUND",
    }


def _two_lane_lower_bound(durations: list[int]) -> int:
    lanes = [0, 0]
    for duration in sorted(durations, reverse=True):
        index = 0 if lanes[0] <= lanes[1] else 1
        lanes[index] += duration
    return max(lanes)


def _generation_id_summary(
    generations: tuple[object, ...],
) -> str:
    values: list[str] = []
    for generation in generations:
        if isinstance(generation, Mapping):
            generation_id = generation.get("generation_id")
            generation_type = generation.get("generation_type")
        else:
            generation_id = getattr(generation, "generation_id", None)
            generation_type = getattr(
                generation,
                "generation_type",
                None,
            )
        values.append(
            f"{generation_type or 'unknown'}:"
            f"{generation_id or 'unknown'}"
        )
    return ",".join(values)


def _validate_atomic_generation_bindings(
    *,
    snapshot: Any,
    native_generation: Any | None,
    databridge_generation: Any | None,
) -> None:
    expected_by_runtime = {
        "native_adapter": (
            None
            if native_generation is None
            else native_generation.generation_id
        ),
        "blackbox_v2": (
            None
            if databridge_generation is None
            else databridge_generation.generation_id
        ),
    }
    mismatches: list[str] = []
    for summary in snapshot.items:
        item = summary.item
        expected = expected_by_runtime.get(item.runtime_type)
        actual = item.input_generation_id
        if actual != expected:
            mismatches.append(
                f"{item.base_scheme_id}={actual or 'NULL'}"
                f"(expected={expected or 'NULL'})"
            )
    if mismatches:
        raise RuntimeError(
            "atomic generation binding postcondition failed: "
            + ",".join(sorted(mismatches))
        )


def _validate_runtime_generation_binding(
    *,
    snapshot: Any,
    runtime_type: str,
    generation: Any,
) -> None:
    expected = generation.generation_id
    mismatches = [
        f"{summary.item.base_scheme_id}="
        f"{summary.item.input_generation_id or 'NULL'}"
        for summary in snapshot.items
        if (
            summary.item.runtime_type == runtime_type
            and summary.item.input_generation_id != expected
        )
    ]
    if mismatches:
        raise RuntimeError(
            "atomic runtime generation binding postcondition failed: "
            f"{runtime_type} expected={expected} "
            + ",".join(sorted(mismatches))
        )


def _future_value(future: Future[Any]) -> tuple[Any | None, str | None]:
    try:
        return future.result(), None
    except Exception as exc:
        return None, _bounded_exception(exc)


def _bounded_exception(exc: Exception) -> str:
    value = f"{type(exc).__name__}: {exc}".strip()
    return value if len(value) <= 2000 else value[:1997] + "..."


def _generation_failure_code(message: str) -> str:
    normalized = message.casefold()
    if "invalidated" in normalized:
        return "GENERATION_INVALIDATED"
    if any(
        token in normalized
        for token in (
            "sha256",
            "sha-256",
            "hash mismatch",
            "digest mismatch",
            "tamper",
        )
    ):
        return "GENERATION_HASH_MISMATCH"
    return "GENERATION_BUILD_FAILED"


def _direct_cache_consumer(
    authorities: Mapping[str, object] | None,
    scheme_id: str,
) -> Mapping[str, object] | None:
    if authorities is None:
        return None
    consumers = authorities.get("consumers")
    if not isinstance(consumers, Mapping):
        raise RuntimeError("direct cache authority consumers are invalid")
    consumer = consumers.get(scheme_id)
    if consumer is None:
        return None
    if not isinstance(consumer, Mapping):
        raise RuntimeError(
            f"direct cache consumer is invalid: {scheme_id}"
        )
    return consumer


def _policy_payload(
    policy: Any,
    *,
    daily_coordinator_epoch: Mapping[str, object] | None = None,
    direct_cache_authorities: Mapping[str, object] | None = None,
) -> dict[str, object]:
    scheme_rows: list[dict[str, object]] = []
    for scheme_id in sorted(policy.schemes):
        row = {
            **asdict(policy.schemes[scheme_id]),
            "absolute_deadline":
                policy.schemes[
                    scheme_id
                ].absolute_deadline.strftime("%H:%M"),
        }
        consumer = _direct_cache_consumer(
            direct_cache_authorities,
            scheme_id,
        )
        if consumer is not None:
            row["cache_adapter_sha256"] = consumer[
                "cache_adapter_sha256"
            ]
            row["cache_core_sha256"] = consumer[
                "cache_core_sha256"
            ]
        scheme_rows.append(row)
    payload = {
        "version": policy.version,
        "evidence_version": policy.evidence_version,
        "evidence_note": policy.evidence_note,
        "timezone": policy.timezone,
        "expected_item_count": policy.expected_item_count,
        "expected_target_count": policy.expected_target_count,
        "times": {
            "not_before": policy.not_before.strftime("%H:%M"),
            "native_capture_deadline":
                policy.native_capture_deadline.strftime("%H:%M"),
            "databridge_readiness_guardrail":
                policy.databridge_readiness_guardrail.strftime("%H:%M"),
            "watchdog": policy.watchdog.strftime("%H:%M"),
            "v2_start_guardrail":
                policy.v2_start_guardrail.strftime("%H:%M"),
            "target_ready": policy.target_ready.strftime("%H:%M"),
            "sla_deadline": policy.sla_deadline.strftime("%H:%M"),
            "recovery_cutoff":
                policy.recovery_cutoff.strftime("%H:%M"),
        },
        "pools": {
            "native_max_concurrency": policy.native_max_concurrency,
            "v2_max_concurrency": policy.v2_max_concurrency,
            "v2_timeout_sec": policy.v2_timeout_sec,
            "native_auto_scale": policy.native_auto_scale,
        },
        "retry_max": policy.retry_max,
        "retry_cleanup_commit_margin_sec":
            policy.retry_cleanup_commit_margin_sec,
        "generation_storage": {
            "max_generation_count":
                policy.generation_max_count,
            "max_total_bytes":
                policy.generation_max_total_bytes,
            "min_free_bytes":
                policy.generation_min_free_bytes,
        },
        "allowed_resource_combinations": [
            list(combination)
            for combination in sorted(
                policy.allowed_resource_combinations
            )
        ],
        "schemes": scheme_rows,
    }
    if daily_coordinator_epoch is not None:
        payload["daily_coordinator_epoch"] = dict(
            daily_coordinator_epoch
        )
    if direct_cache_authorities is not None:
        payload["direct_cache_authorities"] = dict(
            direct_cache_authorities
        )
    return json.loads(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _open_native_envelope(generation: Any):
    return open_native_generation(
        Path(generation.manifest_uri),
        expected_generation_id=generation.generation_id,
        expected_manifest_sha256=generation.manifest_sha256,
        expected_business_date=generation.business_date,
        expected_feature_date=generation.feature_date,
    )


def _remember_generation_envelope(
    envelopes: dict[str, Any],
    generation: Any,
) -> None:
    """按 generation ID 去重，并拒绝同 ID 的 ledger 身份漂移。"""
    generation_id = str(generation.generation_id)
    existing = envelopes.get(generation_id)
    if existing is None:
        envelopes[generation_id] = generation
        return
    fields = (
        "generation_id",
        "generation_type",
        "business_date",
        "feature_date",
        "manifest_uri",
        "manifest_sha256",
        "native_generation_id",
        "native_manifest_sha256",
        "state",
        "sealed_at",
    )
    drift = {
        field: (
            getattr(existing, field, None),
            getattr(generation, field, None),
        )
        for field in fields
        if getattr(existing, field, None)
        != getattr(generation, field, None)
    }
    if drift:
        raise RuntimeError(
            "same generation ID has inconsistent ledger envelopes: "
            f"{generation_id}={drift}"
        )


def _open_native_fence(
    fence: Mapping[str, object],
    *,
    business_date: str,
    feature_date: str,
):
    _validate_fence_dates(
        fence,
        business_date=business_date,
        feature_date=feature_date,
    )
    manifest_path = _fence_manifest_path(fence)
    return open_native_generation(
        manifest_path,
        expected_generation_id=str(fence["generation_id"]),
        expected_manifest_sha256=str(fence["manifest_sha256"]),
        expected_business_date=business_date,
        expected_feature_date=feature_date,
    )


def _open_databridge_fence(
    fence: Mapping[str, object],
    *,
    business_date: str,
    feature_date: str,
):
    _validate_fence_dates(
        fence,
        business_date=business_date,
        feature_date=feature_date,
    )
    manifest_path = _fence_manifest_path(fence)
    return open_databridge_generation(
        manifest_path,
        expected_generation_id=str(fence["generation_id"]),
        expected_manifest_sha256=str(fence["manifest_sha256"]),
        expected_business_date=business_date,
        expected_feature_date=feature_date,
        schema_path=DATABRIDGE_SCHEMA_PATH,
    )


def _validate_fence_dates(
    fence: Mapping[str, object],
    *,
    business_date: str,
    feature_date: str,
) -> None:
    observed_business_date = date.fromisoformat(
        str(fence.get("business_date"))[:10]
    ).isoformat()
    observed_feature_date = date.fromisoformat(
        str(fence.get("feature_date"))[:10]
    ).isoformat()
    if observed_business_date != business_date:
        raise RuntimeError(
            "generation DB fence business_date drifted: "
            f"{observed_business_date} != {business_date}"
        )
    if observed_feature_date != feature_date:
        raise RuntimeError(
            "generation DB fence feature_date drifted: "
            f"{observed_feature_date} != {feature_date}"
        )
    state = str(fence.get("state"))
    if state not in {"BUILDING", "SEALED"}:
        raise RuntimeError(
            f"generation DB fence has non-recoverable state: {state}"
        )
    if state == "SEALED" and fence.get("sealed_at") is None:
        raise RuntimeError(
            "SEALED generation DB fence has no sealed_at"
        )


def _fence_manifest_path(
    fence: Mapping[str, object],
) -> Path:
    value = str(fence.get("manifest_uri") or "").strip()
    path = Path(value)
    if not value or not path.is_absolute():
        raise RuntimeError(
            "generation DB fence manifest_uri must be absolute"
        )
    return path


def _validate_generation_fence_identity(
    context: Any,
    fence: Mapping[str, object],
) -> None:
    """复核 manifest 不可变身份与 DB fence 完全一致。"""
    expected = {
        "generation_id": str(fence.get("generation_id")),
        "generation_type": str(fence.get("generation_type")),
        "business_date": date.fromisoformat(
            str(fence.get("business_date"))[:10]
        ).isoformat(),
        "feature_date": date.fromisoformat(
            str(fence.get("feature_date"))[:10]
        ).isoformat(),
        "readiness_basis": str(fence.get("readiness_basis")),
        "source_commit_token": str(
            fence.get("source_commit_token")
        ),
        "dataset_content_id": str(fence.get("dataset_content_id")),
        "schema_version": str(fence.get("schema_version")),
        "exporter_version": str(fence.get("exporter_version")),
        "manifest_uri": str(fence.get("manifest_uri")),
        "manifest_sha256": str(fence.get("manifest_sha256")),
        "native_generation_id": (
            None
            if fence.get("native_generation_id") is None
            else str(fence.get("native_generation_id"))
        ),
        "native_manifest_sha256": (
            None
            if fence.get("native_manifest_sha256") is None
            else str(fence.get("native_manifest_sha256"))
        ),
    }
    observed = {
        "generation_id": context.generation_id,
        "generation_type": context.generation_type,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "readiness_basis": context.readiness_basis,
        "source_commit_token": context.source_commit_token,
        "dataset_content_id": context.dataset_content_id,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
        "manifest_uri": str(context.manifest_path),
        "manifest_sha256": context.manifest_sha256,
        "native_generation_id": getattr(
            context,
            "native_generation_id",
            None,
        ),
        "native_manifest_sha256": getattr(
            context,
            "native_manifest_sha256",
            None,
        ),
    }
    drift = {
        field: (expected[field], observed[field])
        for field in expected
        if expected[field] != observed[field]
    }
    if drift:
        raise RuntimeError(
            f"generation DB fence identity drifted: {drift}"
        )


def _validate_reclaimable_generation_payload(
    candidate: Any,
    *,
    native_root: Path,
    databridge_root: Path,
) -> None:
    """删除前验证 DB candidate 的 exact path 与完整 payload；缺失视为崩溃重入。"""
    if candidate.generation_type == "native_source":
        root = native_root
    elif candidate.generation_type == "databridge_v1":
        root = databridge_root
    else:
        raise RuntimeError(
            "unsupported reclaim generation_type: "
            f"{candidate.generation_type}"
        )
    expected_manifest = (
        root / candidate.generation_id / "manifest.json"
    )
    stored_manifest = Path(candidate.manifest_uri)
    if (
        not stored_manifest.is_absolute()
        or stored_manifest != expected_manifest
    ):
        raise RuntimeError(
            "reclaimable generation manifest_uri is outside its exact "
            f"storage identity: {candidate.generation_id}"
        )
    generation_root = expected_manifest.parent
    if not os.path.lexists(generation_root):
        # 文件删完、DB finalize 前崩溃的合法重入窗口。
        return
    if candidate.generation_type == "native_source":
        open_native_generation(
            expected_manifest,
            expected_generation_id=candidate.generation_id,
            expected_manifest_sha256=candidate.manifest_sha256,
            expected_business_date=candidate.business_date,
            expected_feature_date=candidate.feature_date,
        )
    else:
        open_databridge_generation(
            expected_manifest,
            expected_generation_id=candidate.generation_id,
            expected_manifest_sha256=candidate.manifest_sha256,
            expected_business_date=candidate.business_date,
            expected_feature_date=candidate.feature_date,
            schema_path=DATABRIDGE_SCHEMA_PATH,
        )


def _parse_frozen_ledger_datetime(
    value: str | datetime,
    *,
    field: str,
) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 新账本 DATETIME(6) 统一存 UTC，但 MySQL driver 返回 naive 值。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snapshot_runtime_generation_id(
    snapshot: Any,
    *,
    runtime_type: str,
) -> str | None:
    runtime_items = [
        summary.item
        for summary in snapshot.items
        if summary.item.runtime_type == runtime_type
    ]
    if not runtime_items:
        return None
    raw_ids = [
        getattr(item, "input_generation_id", None)
        for item in runtime_items
    ]
    if any(value is None or not str(value).strip() for value in raw_ids):
        return None
    generation_ids = {str(value) for value in raw_ids}
    if len(generation_ids) != 1:
        raise RuntimeError(
            "frozen occurrence has mixed runtime generation identity: "
            f"{runtime_type}={sorted(generation_ids)}"
        )
    return next(iter(generation_ids))


def _databridge_readiness_guardrail_at(
    snapshot: Any,
    *,
    business_date: date,
    policy: Any | None,
) -> datetime:
    if policy is not None:
        guardrail = policy.databridge_readiness_guardrail
        timezone_name = policy.timezone
    else:
        policy_json = getattr(
            snapshot.occurrence,
            "policy_json",
            None,
        )
        if not isinstance(policy_json, Mapping):
            raise RuntimeError(
                "frozen occurrence policy_json is missing"
            )
        times = policy_json.get("times")
        if not isinstance(times, Mapping):
            raise RuntimeError(
                "frozen occurrence policy times are missing"
            )
        raw = times.get("databridge_readiness_guardrail")
        if not isinstance(raw, str):
            raise RuntimeError(
                "frozen DataBridge readiness guardrail is missing"
            )
        try:
            guardrail = datetime.strptime(raw, "%H:%M").time()
        except ValueError as exc:
            raise RuntimeError(
                "frozen DataBridge readiness guardrail is invalid"
            ) from exc
        timezone_name = "Asia/Shanghai"
    return datetime.combine(
        business_date,
        guardrail,
        tzinfo=ZoneInfo(timezone_name),
    )


def _ensure_private_directory(path: Path) -> None:
    normalized = path.resolve(strict=False)
    if normalized != path:
        raise RuntimeError(
            f"daily runtime directory must be symlink-free: {path}"
        )
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    details = path.lstat()
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(
            f"daily runtime path is not a real directory: {path}"
        )
    if details.st_uid != os.getuid():
        raise RuntimeError(
            f"daily runtime directory has another owner: {path}"
        )
    if details.st_mode & 0o077:
        raise RuntimeError(
            "daily runtime directory must be private "
            f"(mode 0700 or stricter): {path}"
        )


def _terminate_verified_orphan(
    *,
    scheme_id: str,
    execution_token: str,
    process_group_id: int | None,
    process_id: int | None,
) -> bool:
    if (
        not _is_safe_scheme_id(scheme_id)
        or not _is_safe_execution_token(execution_token)
    ):
        return False
    if process_id is not None and (
        not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 1
        or process_id == os.getpid()
    ):
        return False
    if process_group_id is not None and (
        not isinstance(process_group_id, int)
        or isinstance(process_group_id, bool)
        or process_group_id <= 1
        or process_group_id == os.getpgrp()
    ):
        return False
    if process_group_id is None and process_id is None:
        rows = _process_rows()
        token_rows = [
            row
            for row in rows
            if _row_has_execution_token(
                row[2],
                execution_token=execution_token,
            )
        ]
        if not token_rows:
            return not any(
                _command_belongs_to_scheme(
                    command,
                    scheme_id=scheme_id,
                )
                for _pid, _pgid, command in rows
            )
        matching_groups = {row[1] for row in token_rows}
        if len(matching_groups) != 1:
            return False
        selected_group = next(iter(matching_groups))
        if (
            selected_group <= 1
            or selected_group == os.getpgrp()
        ):
            return False
        members = [
            row for row in rows if row[1] == selected_group
        ]
        if not _rows_belong_to_attempt(
            members,
            scheme_id=scheme_id,
            execution_token=execution_token,
        ):
            return False
        if not _process_group_exists(selected_group):
            return True
        return _terminate_process_group(
            selected_group,
            scheme_id=scheme_id,
            execution_token=execution_token,
        )
    if process_group_id is not None:
        if not _process_group_exists(process_group_id):
            if (
                process_id is not None
                and _process_exists(process_id)
            ):
                return False
            return True
        members = _process_rows(process_group_id=process_group_id)
        if (
            process_id is not None
            and all(pid != process_id for pid, _pgid, _command in members)
            and _process_exists(process_id)
        ):
            return False
        if not _rows_belong_to_attempt(
            members,
            scheme_id=scheme_id,
            execution_token=execution_token,
        ):
            return False
        return _terminate_process_group(
            process_group_id,
            scheme_id=scheme_id,
            execution_token=execution_token,
        )
    if process_id is None or process_id <= 1 or process_id == os.getpid():
        return False
    if not _process_exists(process_id):
        return True
    rows = _process_rows(process_id=process_id)
    if (
        len(rows) != 1
        or rows[0][0] != process_id
        or rows[0][1] <= 1
        or rows[0][1] == os.getpgrp()
        or not _row_has_execution_token(
            rows[0][2],
            execution_token=execution_token,
        )
        or not _command_belongs_to_scheme(
            rows[0][2],
            scheme_id=scheme_id,
        )
    ):
        return False
    discovered_group_id = rows[0][1]
    if not _process_group_exists(discovered_group_id):
        return False
    members = _process_rows(
        process_group_id=discovered_group_id,
    )
    if (
        all(pid != process_id for pid, _pgid, _command in members)
        or not _rows_belong_to_attempt(
            members,
            scheme_id=scheme_id,
            execution_token=execution_token,
        )
    ):
        return False
    return _terminate_process_group(
        discovered_group_id,
        scheme_id=scheme_id,
        execution_token=execution_token,
    )


def _command_belongs_to_scheme(command: str, *, scheme_id: str) -> bool:
    expected_script = str(
        (
            PROJECT_ROOT
            / "schemes"
            / scheme_id
            / "delivery"
            / f"{scheme_id}.py"
        ).resolve()
    )
    tokens = command.split()
    native_identity = any(
        tokens[index:index + 3] == [
            "-m",
            "scheduler.scheme_runner",
            "--scheme-id",
        ]
        and index + 3 < len(tokens)
        and tokens[index + 3] == scheme_id
        for index in range(max(0, len(tokens) - 3))
    )
    return (
        native_identity
        or expected_script in tokens
    )


def _row_has_execution_token(
    command_and_environment: str,
    *,
    execution_token: str,
) -> bool:
    expected = f"{_SCHEDULE_EXECUTION_TOKEN_ENV}={execution_token}"
    return expected in command_and_environment.split()


def _rows_belong_to_attempt(
    rows: list[tuple[int, int, str]],
    *,
    scheme_id: str,
    execution_token: str,
) -> bool:
    return (
        bool(rows)
        and all(
            pid > 1
            and pid != os.getpid()
            and _row_has_execution_token(
                command,
                execution_token=execution_token,
            )
            for pid, _pgid, command in rows
        )
        and any(
            _command_belongs_to_scheme(command, scheme_id=scheme_id)
            for _pid, _pgid, command in rows
        )
    )


def _terminate_process_group(
    process_group_id: int,
    *,
    scheme_id: str,
    execution_token: str,
) -> bool:
    os.killpg(process_group_id, 15)
    if _wait_until_gone(
        lambda: _process_group_exists(process_group_id)
    ):
        return True
    remaining = _process_rows(
        process_group_id=process_group_id,
    )
    if not _rows_belong_to_attempt(
        remaining,
        scheme_id=scheme_id,
        execution_token=execution_token,
    ):
        return False
    os.killpg(process_group_id, 9)
    return _wait_until_gone(
        lambda: _process_group_exists(process_group_id)
    )


def _is_safe_execution_token(execution_token: object) -> bool:
    return (
        isinstance(execution_token, str)
        and bool(execution_token)
        and len(execution_token) <= 128
        and all(
            char in _SAFE_EXECUTION_TOKEN_CHARACTERS
            for char in execution_token
        )
    )


def _is_safe_scheme_id(scheme_id: object) -> bool:
    return (
        isinstance(scheme_id, str)
        and bool(scheme_id)
        and len(scheme_id) <= 128
        and all(
            char in _SAFE_EXECUTION_TOKEN_CHARACTERS
            for char in scheme_id
        )
    )


def _process_rows(
    *,
    process_group_id: int | None = None,
    process_id: int | None = None,
) -> list[tuple[int, int, str]]:
    result = subprocess.run(
        [
            "/bin/ps",
            "-E",
            "-ww",
            "-axo",
            "pid=,pgid=,command=",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    rows: list[tuple[int, int, str]] = []
    for raw in result.stdout.splitlines():
        parts = raw.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            pid_value = int(parts[0])
            pgid_value = int(parts[1])
        except ValueError:
            continue
        if process_group_id is not None and pgid_value != process_group_id:
            continue
        if process_id is not None and pid_value != process_id:
            continue
        rows.append((pid_value, pgid_value, parts[2]))
    return rows


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_until_gone(predicate: Any) -> bool:
    deadline = wall_time.monotonic() + 5.0
    while wall_time.monotonic() < deadline:
        if not predicate():
            return True
        wall_time.sleep(0.1)
    return not predicate()


@contextmanager
def _optional_owner(lock: Any):
    try:
        lock.acquire()
    except OccurrenceLockUnavailable:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def run_daily_occurrence(
    run_date: str | date | None = None,
    *,
    algo_env: str = "forecast_env",
    trigger_origin: str = "apscheduler",
    _services: Any | None = None,
) -> DailyRuntimeResult:
    """APScheduler/launchd 唯一日批入口。"""
    services, dispose = _runtime_services(
        _services,
        algo_env=algo_env,
        verify_direct_authority=True,
    )
    try:
        if _services is None:
            preflight_daily_storage()
        return DailyRuntime(services).run_occurrence(
            run_date=run_date,
            trigger_origin=trigger_origin,
        )
    finally:
        dispose()


def run_daily_watchdog(
    *,
    stage: str,
    run_date: str | date | None = None,
    _services: Any | None = None,
) -> WatchdogResult:
    """隔离 executor 使用的 07:00/07:45/08:00/08:30 控制入口。"""
    services, dispose = _runtime_services(
        _services,
        verify_direct_authority=stage == "progress",
        audit_only=stage != "progress",
    )
    try:
        return DailyRuntime(services).run_watchdog(
            stage=stage,
            run_date=run_date,
        )
    finally:
        dispose()


def run_operator_recovery(
    *,
    scheme_id: str,
    run_date: str | date | None = None,
    algo_env: str = "forecast_env",
    _services: Any | None = None,
) -> DailyRuntimeResult:
    """admin 恢复入口；只消费同一 occurrence 已冻结的 generation。"""
    services, dispose = _runtime_services(
        _services,
        algo_env=algo_env,
        verify_direct_authority=True,
    )
    try:
        if _services is None:
            preflight_daily_storage()
        return DailyRuntime(services).run_operator_recovery(
            scheme_id=scheme_id,
            run_date=run_date,
        )
    finally:
        dispose()


def _runtime_services(
    injected: Any | None,
    *,
    algo_env: str = "forecast_env",
    verify_direct_authority: bool = False,
    audit_only: bool = False,
) -> tuple[Any, Any]:
    if injected is not None:
        return injected, (lambda: None)
    services = DefaultDailyRuntimeServices(algo_env=algo_env)
    try:
        if audit_only:
            _require_ledger_runtime_mode()
        elif verify_direct_authority:
            authorities = _require_production_entry_authority(
                engine=services.engine,
                algo_env=algo_env,
            )
            services.bind_direct_cache_authorities(authorities)
        else:
            _require_ledger_runtime_mode()
    except BaseException:
        services.close()
        raise
    return services, services.close


def _require_ledger_runtime_mode() -> None:
    """审计/心跳入口只校验本机 ledger rollout。"""
    from shared.daily_coordinator_mode import (
        bootstrap_deployment_daily_coordinator_mode,
    )

    if bootstrap_deployment_daily_coordinator_mode() != "ledger":
        raise RuntimeError(
            "daily runtime requires ledger coordinator mode"
        )


def _require_production_entry_authority(
    *,
    engine: Any,
    algo_env: str = "forecast_env",
) -> Mapping[str, object]:
    """最内层只接受 ledger mode 与当前 exact direct authority。"""
    from scheduler.daily_direct_authority import (
        build_daily_direct_cache_authorities,
    )

    _require_ledger_runtime_mode()
    return build_daily_direct_cache_authorities(
        engine,
        policy_path=POLICY_V2_PATH,
        algo_env=algo_env,
    )
