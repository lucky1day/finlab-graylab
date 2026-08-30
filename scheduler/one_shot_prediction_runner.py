"""平台中立的 scheduled one-shot 单批预测编排。

本模块不保存 cron、ledger、occurrence 或 startup catch-up 状态。每次进程启动时
只做一次严格发现、按 active cadence 筛选、DataBridge Gate 校验和逐方案执行；跨 cadence 的
互斥由 runtime 目录中的单一阻塞锁保证。
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from scheduler.deployment_scope import (
    DeploymentScopeError,
    require_deployment_target_for_control_plane,
)
from scheduler.discovery import discover_schemes
from scheduler.executor import (
    SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT,
    execute_scheme,
    _scheduled_scheme_identity_error,
)
from scheduler.process_control import ProcessStartGuard
from scheduler.repository import (
    PREDICTION_KEYS_ALREADY_EXIST,
    create_engine_from_env,
    resolve_database_lifecycle,
)
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.calendar_service import get_calendar
from shared.prediction_context import is_weekly_signal_date
from shared.prediction_context import build_monthly_live_context
from shared.period_average_buckets import period_anchor_dates
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES, PREDICTION_CADENCES
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.liwei_0616_cache_contract import APPROVED_PHASE_A_CACHE_PUBLISHERS
from shared.liwei_0616_cache_contract import (
    CACHE_MUTATION_POLICY_INCREMENTAL_ONLY,
    CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE,
)
ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA_BRIDGE_READY_MAX_WAIT_SEC = 30 * 60
DATA_BRIDGE_READY_POLL_INTERVAL_SEC = 30


class OneShotPredictionConfigurationError(RuntimeError):
    """scheduled one-shot prediction runner 的配置或输入不成立。"""


@dataclass
class OneShotPredictionSummary:
    """一次 one-shot 批运行的无敏感结构化摘要。"""

    cadence: str
    predict_date: str
    event: str = "launchd_prediction_run"
    discovered: int = 0
    outcome: str = "success"
    exit_code: int = 0
    executed: list[dict[str, object]] = field(default_factory=list)
    excluded: list[dict[str, object]] = field(default_factory=list)
    denied: list[dict[str, object]] = field(default_factory=list)
    blocked: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        """返回字段与顺序稳定、可写入 launchd 日志的 JSON 对象。"""
        return {
            "event": self.event,
            "cadence": self.cadence,
            "predict_date": self.predict_date,
            "outcome": self.outcome,
            "discovered": self.discovered,
            "executed": _sorted_items(self.executed),
            "excluded": _sorted_items(self.excluded),
            "denied": _sorted_items(self.denied),
            "blocked": _sorted_items(self.blocked),
            "skipped": _sorted_items(self.skipped),
            "failed": _sorted_items(self.failed),
            "exit_code": self.exit_code,
        }


def _sorted_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(items, key=lambda item: str(item.get("scheme_id", "")))


def today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_cadence(value: str) -> str:
    cadence = str(value).strip().lower()
    if cadence not in PREDICTION_CADENCES:
        raise OneShotPredictionConfigurationError("invalid cadence")
    return cadence


def _normalize_predict_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise OneShotPredictionConfigurationError("invalid predict_date") from exc


def _normalize_requested_scheme_ids(
    values: Sequence[str] | None,
) -> frozenset[str]:
    if values is None:
        return frozenset()
    normalized = frozenset(str(value).strip() for value in values)
    if not normalized or "" in normalized:
        raise OneShotPredictionConfigurationError("invalid scheme_id")
    return normalized


@contextmanager
def _runner_lock(config: DataBridgeRefreshConfig) -> Iterator[None]:
    """以跨 cadence 的全局阻塞锁串行化 generic writer。"""
    lock_path = config.runtime_root / "launchd_prediction_runner" / "predictions.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise OneShotPredictionConfigurationError(
            "launchd prediction runner lock is unavailable"
        ) from exc


def _candidate_item(cfg: object, code: str) -> dict[str, object]:
    return {"scheme_id": str(getattr(cfg, "scheme_id", "")), "code": code}


def _is_persisted_preflight_failure(result: object, expected_error: str) -> bool:
    """只将已经持久化的预检失败归类为其安全错误码。"""
    return (
        str(getattr(result, "status", "")) == "failed"
        and getattr(result, "error_msg", None) == expected_error
        and getattr(result, "run_id", None) is not None
    )


def _cache_publishers_first(candidates: Sequence[object]) -> list[object]:
    """稳定地让当前批次的 Liwei cache publisher 先于其 consumer 执行。"""
    publisher_ids = {
        publisher_id
        for _tenor, publisher_id in APPROVED_PHASE_A_CACHE_PUBLISHERS.values()
    }
    publishers = [
        cfg
        for cfg in candidates
        if str(getattr(cfg, "scheme_id", "")) in publisher_ids
    ]
    others = [
        cfg
        for cfg in candidates
        if str(getattr(cfg, "scheme_id", "")) not in publisher_ids
    ]
    return publishers + others


def _is_data_bridge_dependent(cfg: object) -> bool:
    """识别 Blackbox V2 对 current DataBridge 的显式依赖。"""
    return (
        getattr(cfg, "runtime_type", None) == "blackbox_v2"
        and getattr(cfg, "input_source", None) == "data_bridge_current"
    )


def candidate_matches_cadence(cfg: object, cadence: str) -> bool:
    """按显式 task_type 选择周期均值，其余 cadence 保持频率语义。"""
    task_type = str(getattr(cfg, "task_type", "") or "")
    if cadence == "period_average":
        return task_type in PERIOD_AVERAGE_TASK_TYPES
    return (
        task_type not in PERIOD_AVERAGE_TASK_TYPES
        and getattr(cfg, "frequency", None) == cadence
    )


def period_due_task_types(
    candidates: Sequence[object],
    calendar: object,
    predict_date: str,
) -> tuple[frozenset[str], frozenset[str]]:
    """返回到期任务及日历识别失败任务，避免一个任务拖累另两个任务。"""
    due: set[str] = set()
    invalid: set[str] = set()
    task_types = sorted(
        {
            str(getattr(cfg, "task_type", "") or "")
            for cfg in candidates
        }
    )
    for task_type in task_types:
        try:
            anchors = period_anchor_dates(
                task_type,
                calendar.period_calendar_rows(),
                start_date=predict_date,
                end_date=predict_date,
            )
        except (AttributeError, ValueError):
            invalid.add(task_type)
            continue
        if predict_date in anchors:
            due.add(task_type)
    return frozenset(due), frozenset(invalid)


def _wait_for_v2_daily_ready(
    config: DataBridgeRefreshConfig,
    *,
    run_date: str,
    expected_daily_date: str,
) -> str | None:
    """只读轮询一批日频 Blackbox 共同使用的就绪凭证。"""
    deadline = time.monotonic() + DATA_BRIDGE_READY_MAX_WAIT_SEC
    while True:
        try:
            require_v2_daily_ready(config, run_date, expected_daily_date)
            return None
        except V2DailyGateBlocked:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
            time.sleep(min(DATA_BRIDGE_READY_POLL_INTERVAL_SEC, remaining))
        except Exception:  # noqa: BLE001 - never reveal low-level gate details
            return "v2_gate_unavailable"


def _execute_candidate(
    summary: OneShotPredictionSummary,
    cfg: object,
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    scheduled_preflight_failure: str | None = None,
    engine=None,
    canonical_config_trusted: bool = False,
    ephemeral_native_runtime_root: Path | None = None,
    cancellation_event: threading.Event | None = None,
    process_start_guard: ProcessStartGuard | None = None,
    native_cache_mutation_policy: str = (
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
    ),
) -> None:
    """执行一个候选并将稳定结果归入当前 one-shot 摘要。"""
    try:
        execute_kwargs = {
            "algo_env": algo_env,
            "prediction_phase": "scheduled_live",
            "scheduled_control_plane": scheduled_control_plane,
            "scheduled_execution_context": scheduled_execution_context,
        }
        if engine is not None:
            execute_kwargs["engine"] = engine
        if canonical_config_trusted:
            execute_kwargs["canonical_config_trusted"] = True
        if ephemeral_native_runtime_root is not None:
            execute_kwargs["ephemeral_native_runtime_root"] = (
                ephemeral_native_runtime_root
            )
            execute_kwargs["native_cache_mutation_policy"] = (
                native_cache_mutation_policy
            )
        if cancellation_event is not None:
            execute_kwargs["cancellation_event"] = cancellation_event
        if process_start_guard is not None:
            execute_kwargs["process_start_guard"] = process_start_guard
        if scheduled_preflight_failure is not None:
            execute_kwargs["scheduled_preflight_failure"] = (
                scheduled_preflight_failure
            )
        result = execute_scheme(cfg, predict_date, **execute_kwargs)
    except Exception:  # noqa: BLE001 - isolate one candidate
        summary.failed.append(_candidate_item(cfg, "execution_exception"))
        return

    if (
        scheduled_preflight_failure is not None
        and _is_persisted_preflight_failure(result, scheduled_preflight_failure)
    ):
        summary.failed.append(_candidate_item(cfg, scheduled_preflight_failure))
        return

    status = str(getattr(result, "status", "failed"))
    if status == "success":
        summary.executed.append(
            {
                "scheme_id": str(getattr(result, "scheme_id", cfg.scheme_id)),
                "status": status,
                "records_written": int(
                    getattr(result, "records_written", 0) or 0
                ),
                "run_id": getattr(result, "run_id", None),
            }
        )
    elif status == "skipped":
        error_msg = getattr(result, "error_msg", None)
        code = (
            PREDICTION_KEYS_ALREADY_EXIST
            if error_msg == PREDICTION_KEYS_ALREADY_EXIST
            else "execution_skipped"
        )
        summary.skipped.append(_candidate_item(cfg, code))
    else:
        summary.failed.append(_candidate_item(cfg, f"execution_{status}"))


def _execute_native_wave(
    summary: OneShotPredictionSummary,
    candidates: Sequence[object],
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    engine,
    input_root: Path,
    cancellation_event: threading.Event,
    process_start_guard: ProcessStartGuard,
    native_cache_mutation_policy: str = (
        CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
    ),
) -> None:
    """最多两个 worker 执行一批 Native，并在主线程合并结果。"""
    if not candidates:
        return

    def execute(cfg: object) -> OneShotPredictionSummary:
        local = OneShotPredictionSummary(summary.cadence, summary.predict_date)
        _execute_candidate(
            local,
            cfg,
            predict_date=predict_date,
            algo_env=algo_env,
            scheduled_control_plane=scheduled_control_plane,
            scheduled_execution_context=scheduled_execution_context,
            engine=engine,
            canonical_config_trusted=True,
            ephemeral_native_runtime_root=input_root,
            cancellation_event=cancellation_event,
            process_start_guard=process_start_guard,
            native_cache_mutation_policy=native_cache_mutation_policy,
        )
        return local

    pool = ThreadPoolExecutor(
        max_workers=min(2, len(candidates)),
        thread_name_prefix="native-one-shot",
    )
    futures = {pool.submit(execute, cfg): cfg for cfg in candidates}
    try:
        for future in as_completed(futures):
            local = future.result()
            summary.executed.extend(local.executed)
            summary.skipped.extend(local.skipped)
            summary.failed.extend(local.failed)
    except BaseException:
        cancellation_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        pool.shutdown(
            wait=True,
            cancel_futures=cancellation_event.is_set(),
        )


def _finalize(summary: OneShotPredictionSummary) -> None:
    for items in (
        summary.executed,
        summary.excluded,
        summary.denied,
        summary.blocked,
        summary.skipped,
        summary.failed,
    ):
        items.sort(key=lambda item: str(item.get("scheme_id", "")))
    actionable_skips = any(
        item.get("code") != PREDICTION_KEYS_ALREADY_EXIST
        for item in summary.skipped
    )
    if (
        summary.denied
        or summary.blocked
        or actionable_skips
        or summary.failed
    ):
        summary.outcome = "partial"
        summary.exit_code = 1
    else:
        summary.outcome = "success"
        summary.exit_code = 0


@dataclass(frozen=True, slots=True)
class _CandidatePartitions:
    native_publishers: tuple[object, ...]
    native_consumers: tuple[object, ...]
    direct: tuple[object, ...]
    data_bridge_dependents: tuple[object, ...]


def _discover_cadence_configs(
    cadence: str,
    requested_scheme_ids: frozenset[str],
) -> list[object]:
    """严格发现并校验请求集合属于当前 cadence。"""
    try:
        discovered = discover_schemes()
    except Exception as exc:  # noqa: BLE001 - strict discovery is configuration
        raise OneShotPredictionConfigurationError(
            "strict scheme discovery failed"
        ) from exc
    discovered_by_id = {
        str(getattr(cfg, "scheme_id", "")): cfg
        for cfg in discovered
    }
    if requested_scheme_ids - set(discovered_by_id):
        raise OneShotPredictionConfigurationError(
            "requested scheme IDs are not available in this deployment"
        )
    cadence_configs = [
        cfg
        for cfg in discovered
        if candidate_matches_cadence(cfg, cadence)
    ]
    cadence_ids = {
        str(getattr(cfg, "scheme_id", ""))
        for cfg in cadence_configs
    }
    if requested_scheme_ids - cadence_ids:
        raise OneShotPredictionConfigurationError(
            "requested scheme IDs do not match cadence"
        )
    return cadence_configs


def _load_active_candidates(
    engine,
    cadence_configs: Sequence[object],
    requested_scheme_ids: frozenset[str],
) -> list[object]:
    """加载数据库 lifecycle，并对定向执行做 exact identity 预检。"""
    effective = resolve_database_lifecycle(engine, cadence_configs)
    candidates = [
        cfg
        for cfg in effective
        if getattr(cfg, "status", None) == "active"
    ]
    if requested_scheme_ids:
        active_by_id = {
            str(getattr(cfg, "scheme_id", "")): cfg
            for cfg in candidates
        }
        if requested_scheme_ids - set(active_by_id):
            raise OneShotPredictionConfigurationError(
                "requested scheme IDs are not active"
            )
        candidates = [
            active_by_id[scheme_id]
            for scheme_id in sorted(requested_scheme_ids)
        ]
        if any(
            _scheduled_scheme_identity_error(engine, cfg) is not None
            for cfg in candidates
        ):
            raise OneShotPredictionConfigurationError(
                "requested scheme identity is not executable"
            )
    return _cache_publishers_first(candidates)


def _filter_due_candidates(
    summary: OneShotPredictionSummary,
    candidates: Sequence[object],
    *,
    cadence: str,
    predict_date: str,
    calendar: object,
) -> tuple[list[object], bool]:
    """应用日历 due filter；第二个返回值表示批次已形成终态。"""
    if not calendar.covers(predict_date):
        raise OneShotPredictionConfigurationError(
            "trade calendar does not cover predict_date"
        )
    selected = list(candidates)
    if cadence == "period_average":
        due_task_types, invalid_task_types = period_due_task_types(
            selected,
            calendar,
            predict_date,
        )
        for cfg in selected:
            task_type = str(getattr(cfg, "task_type", "") or "")
            if task_type in invalid_task_types:
                summary.blocked.append(
                    _candidate_item(cfg, "period_calendar_invalid")
                )
        selected = [
            cfg
            for cfg in selected
            if str(getattr(cfg, "task_type", "") or "")
            in due_task_types
        ]
        if not selected:
            if summary.blocked:
                _finalize(summary)
            else:
                summary.outcome = "not_applicable"
                summary.exit_code = 0
            return [], True
    not_applicable = (
        not calendar.is_trading_day(predict_date)
        if cadence == "daily"
        else not is_weekly_signal_date(calendar, predict_date)
        if cadence == "weekly"
        else False
    )
    if not_applicable:
        summary.excluded.clear()
        summary.denied.clear()
        summary.blocked.clear()
        summary.skipped.clear()
        summary.failed.clear()
        summary.outcome = "not_applicable"
        summary.exit_code = 0
        return [], True
    return selected, False


def _partition_candidates(
    candidates: Sequence[object],
    *,
    cadence: str,
) -> _CandidatePartitions:
    """稳定划分 Native waves、直接执行与 DataBridge-dependent wave。"""
    data_bridge_dependents = tuple(
        cfg
        for cfg in candidates
        if cadence in {"daily", "monthly", "period_average"}
        and _is_data_bridge_dependent(cfg)
    )
    dependent_ids = {id(cfg) for cfg in data_bridge_dependents}
    native = tuple(
        cfg
        for cfg in candidates
        if getattr(cfg, "runtime_type", "native_adapter")
        == "native_adapter"
    )
    publisher_ids = {
        publisher_id
        for _tenor, publisher_id in APPROVED_PHASE_A_CACHE_PUBLISHERS.values()
    }
    native_publishers = tuple(
        cfg
        for cfg in native
        if str(getattr(cfg, "scheme_id", "")) in publisher_ids
    )
    native_consumers = tuple(
        cfg
        for cfg in native
        if str(getattr(cfg, "scheme_id", "")) not in publisher_ids
    )
    direct = tuple(
        cfg
        for cfg in candidates
        if id(cfg) not in dependent_ids and cfg not in native
    )
    return _CandidatePartitions(
        native_publishers=native_publishers,
        native_consumers=native_consumers,
        direct=direct,
        data_bridge_dependents=data_bridge_dependents,
    )


def _execute_data_bridge_wave(
    summary: OneShotPredictionSummary,
    candidates: Sequence[object],
    *,
    cadence: str,
    predict_date: str,
    calendar: object,
    data_bridge_config: DataBridgeRefreshConfig,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    engine,
) -> None:
    """等待共享 ready gate，并以既有错误分类执行 dependent wave。"""
    if not candidates:
        return
    expected_daily_date: str | None = None
    try:
        expected_daily_date = (
            predict_date
            if cadence == "period_average"
            else build_monthly_live_context(
                calendar,
                predict_date,
            ).feature_date
            if cadence == "monthly"
            else str(calendar.previous_trading_day(predict_date))[:10]
        )
    except Exception:  # noqa: BLE001 - preserve calendar isolation
        for cfg in candidates:
            summary.blocked.append(
                _candidate_item(cfg, "calendar_unavailable")
            )
    if expected_daily_date is None:
        return
    gate_code = _wait_for_v2_daily_ready(
        data_bridge_config,
        run_date=predict_date,
        expected_daily_date=expected_daily_date,
    )
    if gate_code == SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT:
        for cfg in candidates:
            _execute_candidate(
                summary,
                cfg,
                predict_date=predict_date,
                algo_env=algo_env,
                scheduled_control_plane=scheduled_control_plane,
                scheduled_execution_context=scheduled_execution_context,
                scheduled_preflight_failure=(
                    SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
                ),
                engine=engine,
                canonical_config_trusted=True,
            )
    elif gate_code is not None:
        for cfg in candidates:
            summary.blocked.append(_candidate_item(cfg, gate_code))
    else:
        for cfg in candidates:
            _execute_candidate(
                summary,
                cfg,
                predict_date=predict_date,
                algo_env=algo_env,
                scheduled_control_plane=scheduled_control_plane,
                scheduled_execution_context=scheduled_execution_context,
                engine=engine,
                canonical_config_trusted=True,
            )


def run_one_shot(
    cadence: str,
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    event: str,
    scheme_ids: Sequence[str] | None = None,
) -> OneShotPredictionSummary:
    """执行一次指定 cadence 的受控 scheduled_live 批次。"""
    normalized_cadence = _normalize_cadence(cadence)
    normalized_date = _normalize_predict_date(predict_date)
    requested_scheme_ids = _normalize_requested_scheme_ids(scheme_ids)
    if normalized_cadence == "monthly" and date.fromisoformat(normalized_date).day != 15:
        raise OneShotPredictionConfigurationError(
            "monthly one-shot must run on natural month day 15"
        )

    try:
        require_deployment_target_for_control_plane(scheduled_control_plane)
    except DeploymentScopeError as exc:
        raise OneShotPredictionConfigurationError(
            "deployment target does not match one-shot control plane"
        ) from exc

    try:
        data_bridge_config = DataBridgeRefreshConfig.from_env()
    except Exception as exc:  # noqa: BLE001 - only expose stable CLI category
        raise OneShotPredictionConfigurationError(
            "DataBridge refresh configuration is unavailable"
        ) from exc

    summary = OneShotPredictionSummary(
        cadence=normalized_cadence,
        predict_date=normalized_date,
        event=event,
    )
    with _runner_lock(data_bridge_config):
        cadence_configs = _discover_cadence_configs(
            normalized_cadence,
            requested_scheme_ids,
        )
        if not cadence_configs:
            _finalize(summary)
            return summary
        engine = None
        try:
            engine = create_engine_from_env()
            candidates = _load_active_candidates(
                engine,
                cadence_configs,
                requested_scheme_ids,
            )
            summary.discovered = len(candidates)
            if not candidates:
                _finalize(summary)
                return summary

            calendar = get_calendar(engine)
            candidates, terminal = _filter_due_candidates(
                summary,
                candidates,
                cadence=normalized_cadence,
                predict_date=normalized_date,
                calendar=calendar,
            )
            if terminal:
                return summary

            partitions = _partition_candidates(
                candidates,
                cadence=normalized_cadence,
            )
            if partitions.native_publishers or partitions.native_consumers:
                cancellation_event = threading.Event()
                process_start_guard = ProcessStartGuard()
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="bfl-native-one-shot-"
                    ) as temporary:
                        os.chmod(temporary, 0o700)
                        input_root = Path(temporary).resolve(strict=True)
                        _execute_native_wave(
                            summary,
                            list(partitions.native_publishers),
                            predict_date=normalized_date,
                            algo_env=algo_env,
                            scheduled_control_plane=scheduled_control_plane,
                            scheduled_execution_context=(
                                scheduled_execution_context
                            ),
                            engine=engine,
                            input_root=input_root,
                            cancellation_event=cancellation_event,
                            process_start_guard=process_start_guard,
                            native_cache_mutation_policy=(
                                CACHE_MUTATION_POLICY_SCHEDULED_BOUNDED_RECONCILE
                                if normalized_cadence == "daily"
                                else CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
                            ),
                        )
                        _execute_native_wave(
                            summary,
                            list(partitions.native_consumers),
                            predict_date=normalized_date,
                            algo_env=algo_env,
                            scheduled_control_plane=scheduled_control_plane,
                            scheduled_execution_context=(
                                scheduled_execution_context
                            ),
                            engine=engine,
                            input_root=input_root,
                            cancellation_event=cancellation_event,
                            process_start_guard=process_start_guard,
                            native_cache_mutation_policy=(
                                CACHE_MUTATION_POLICY_INCREMENTAL_ONLY
                            ),
                        )
                except BaseException:
                    cancellation_event.set()
                    raise

            for cfg in partitions.direct:
                _execute_candidate(
                    summary,
                    cfg,
                    predict_date=normalized_date,
                    algo_env=algo_env,
                    scheduled_control_plane=scheduled_control_plane,
                    scheduled_execution_context=scheduled_execution_context,
                    engine=engine,
                    canonical_config_trusted=True,
                )

            _execute_data_bridge_wave(
                summary,
                partitions.data_bridge_dependents,
                cadence=normalized_cadence,
                predict_date=normalized_date,
                calendar=calendar,
                data_bridge_config=data_bridge_config,
                algo_env=algo_env,
                scheduled_control_plane=scheduled_control_plane,
                scheduled_execution_context=scheduled_execution_context,
                engine=engine,
            )

            _finalize(summary)
            return summary
        except OneShotPredictionConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001 - outer dependencies are config failures
            raise OneShotPredictionConfigurationError(
                "launchd prediction dependencies are unavailable"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()


def configuration_summary(
    cadence: str,
    predict_date: str,
    *,
    event: str,
) -> OneShotPredictionSummary:
    summary = OneShotPredictionSummary(
        cadence=str(cadence).strip().lower(),
        predict_date=str(predict_date),
        event=event,
        outcome="configuration_error",
        exit_code=2,
    )
    return summary
