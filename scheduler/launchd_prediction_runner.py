"""由 launchd one-shot 启动的单批预测入口。

本模块不保存 cron、ledger、occurrence 或 startup catch-up 状态。每次进程启动时
只做一次严格发现、按 active cadence 筛选、DataBridge Gate 校验和逐方案执行；跨 cadence 的
互斥由 runtime 目录中的单一阻塞锁保证。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from scheduler.deployment_scope import (
    DeploymentScopeError,
    require_deployment_target_for_control_plane,
)
from scheduler.discovery import discover_schemes
from scheduler.executor import (
    DEFAULT_ALGO_ENV,
    SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT,
    _launchd_scheduled_execution_context,
    execute_scheme,
)
from scheduler.repository import (
    PREDICTION_KEYS_ALREADY_EXIST,
    create_engine_from_env,
)
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.calendar_service import get_calendar
from shared.prediction_context import is_weekly_signal_date
from shared.prediction_context import build_monthly_live_context
from shared.period_average_buckets import period_anchor_dates
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES, PREDICTION_CADENCES
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.liwei_0616_cache_contract import APPROVED_PHASE_A_CACHE_PUBLISHERS
from shared.one_shot_control_plane import LAUNCHD_ONE_SHOT_CONTROL_PLANE


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA_BRIDGE_READY_MAX_WAIT_SEC = 30 * 60
DATA_BRIDGE_READY_POLL_INTERVAL_SEC = 30


class LaunchdPredictionConfigurationError(RuntimeError):
    """launchd one-shot prediction runner 的配置或输入不成立。"""


@dataclass
class LaunchdPredictionSummary:
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


def _today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_cadence(value: str) -> str:
    cadence = str(value).strip().lower()
    if cadence not in PREDICTION_CADENCES:
        raise LaunchdPredictionConfigurationError("invalid cadence")
    return cadence


def _normalize_predict_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise LaunchdPredictionConfigurationError("invalid predict_date") from exc


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
        raise LaunchdPredictionConfigurationError(
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


def _candidate_matches_cadence(cfg: object, cadence: str) -> bool:
    """按显式 task_type 选择周期均值，其余 cadence 保持频率语义。"""
    task_type = str(getattr(cfg, "task_type", "") or "")
    if cadence == "period_average":
        return task_type in PERIOD_AVERAGE_TASK_TYPES
    return (
        task_type not in PERIOD_AVERAGE_TASK_TYPES
        and getattr(cfg, "frequency", None) == cadence
    )


def _period_due_task_types(
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
    summary: LaunchdPredictionSummary,
    cfg: object,
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    scheduled_preflight_failure: str | None = None,
) -> None:
    """执行一个候选并将稳定结果归入当前 one-shot 摘要。"""
    try:
        execute_kwargs = {
            "algo_env": algo_env,
            "prediction_phase": "scheduled_live",
            "scheduled_control_plane": scheduled_control_plane,
            "scheduled_execution_context": scheduled_execution_context,
        }
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


def _finalize(summary: LaunchdPredictionSummary, *, configuration_error: bool) -> None:
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
    if configuration_error:
        summary.outcome = "configuration_error"
        summary.exit_code = 2
    elif (
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


def run(
    cadence: str,
    *,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
) -> LaunchdPredictionSummary:
    """执行一次指定 cadence 的 launchd scheduled_live 批次。"""
    return _run_one_shot(
        cadence,
        predict_date=predict_date,
        algo_env=algo_env,
        scheduled_control_plane=LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        scheduled_execution_context=(
            _launchd_scheduled_execution_context()
        ),
        event="launchd_prediction_run",
    )


def _run_one_shot(
    cadence: str,
    *,
    predict_date: str,
    algo_env: str,
    scheduled_control_plane: str,
    scheduled_execution_context: object,
    event: str,
) -> LaunchdPredictionSummary:
    """执行一次指定 cadence 的受控 scheduled_live 批次。"""
    normalized_cadence = _normalize_cadence(cadence)
    normalized_date = _normalize_predict_date(predict_date)
    if normalized_cadence == "monthly" and date.fromisoformat(normalized_date).day != 15:
        raise LaunchdPredictionConfigurationError(
            "monthly one-shot must run on natural month day 15"
        )

    try:
        require_deployment_target_for_control_plane(scheduled_control_plane)
    except DeploymentScopeError as exc:
        raise LaunchdPredictionConfigurationError(
            "deployment target does not match one-shot control plane"
        ) from exc

    try:
        data_bridge_config = DataBridgeRefreshConfig.from_env()
    except Exception as exc:  # noqa: BLE001 - only expose stable CLI category
        raise LaunchdPredictionConfigurationError(
            "DataBridge refresh configuration is unavailable"
        ) from exc

    summary = LaunchdPredictionSummary(
        cadence=normalized_cadence,
        predict_date=normalized_date,
        event=event,
    )
    with _runner_lock(data_bridge_config):
        try:
            discovered = discover_schemes(strict=True)
        except Exception as exc:  # noqa: BLE001 - strict discovery is configuration
            raise LaunchdPredictionConfigurationError(
                "strict scheme discovery failed"
            ) from exc
        candidates = [
            cfg
            for cfg in discovered
            if getattr(cfg, "status", None) == "active"
            and _candidate_matches_cadence(cfg, normalized_cadence)
        ]
        summary.discovered = len(candidates)

        candidates = _cache_publishers_first(candidates)
        if not candidates:
            _finalize(summary, configuration_error=False)
            return summary

        engine = None
        try:
            engine = create_engine_from_env()
            calendar = get_calendar(engine)
            if not calendar.covers(normalized_date):
                # 日历需要人工逐年延长。覆盖耗尽时 is_trading_day 同样返回
                # False，若不先区分，整批生产会伪装成节假日静默跳过并以退出码
                # 0 结束；周频/月频连这层判定都没有，会直接沿用陈旧交易日。
                raise LaunchdPredictionConfigurationError(
                    "trade calendar does not cover predict_date"
                )
            if normalized_cadence == "period_average":
                due_task_types, invalid_task_types = _period_due_task_types(
                    candidates,
                    calendar,
                    normalized_date,
                )
                for cfg in candidates:
                    task_type = str(getattr(cfg, "task_type", "") or "")
                    if task_type in invalid_task_types:
                        summary.blocked.append(
                            _candidate_item(cfg, "period_calendar_invalid")
                        )
                candidates = [
                    cfg
                    for cfg in candidates
                    if str(getattr(cfg, "task_type", "") or "")
                    in due_task_types
                ]
                if not candidates:
                    if summary.blocked:
                        _finalize(summary, configuration_error=False)
                    else:
                        summary.outcome = "not_applicable"
                        summary.exit_code = 0
                    return summary
            # 本次调度是否适用于当前 cadence。周频除了自然周六，还要求这一周
            # 真的关闭了新的 feature 周——整周无交易日时，重复业务键虽会以
            # insert-only benign skip 收口，仍会产生误导性的 run date 并浪费
            # 算法执行。
            not_applicable = (
                not calendar.is_trading_day(normalized_date)
                if normalized_cadence == "daily"
                else not is_weekly_signal_date(calendar, normalized_date)
                if normalized_cadence == "weekly"
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
                return summary

            data_bridge_dependents = [
                cfg
                for cfg in candidates
                if normalized_cadence in {"daily", "monthly", "period_average"}
                and _is_data_bridge_dependent(cfg)
            ]
            data_bridge_dependent_ids = {
                id(cfg) for cfg in data_bridge_dependents
            }
            for cfg in candidates:
                if id(cfg) not in data_bridge_dependent_ids:
                    _execute_candidate(
                        summary,
                        cfg,
                        predict_date=normalized_date,
                        algo_env=algo_env,
                        scheduled_control_plane=scheduled_control_plane,
                        scheduled_execution_context=(
                            scheduled_execution_context
                        ),
                    )

            if data_bridge_dependents:
                expected_daily_date: str | None = None
                try:
                    expected_daily_date = (
                        normalized_date
                        if normalized_cadence == "period_average"
                        else build_monthly_live_context(
                            calendar,
                            normalized_date,
                        ).feature_date
                        if normalized_cadence == "monthly"
                        else str(calendar.previous_trading_day(normalized_date))[:10]
                    )
                except Exception:  # noqa: BLE001 - preserve calendar isolation
                    for cfg in data_bridge_dependents:
                        summary.blocked.append(
                            _candidate_item(cfg, "calendar_unavailable")
                        )
                if expected_daily_date is not None:
                    gate_code = _wait_for_v2_daily_ready(
                        data_bridge_config,
                        run_date=normalized_date,
                        expected_daily_date=expected_daily_date,
                    )
                    if (
                        gate_code
                        == SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
                    ):
                        for cfg in data_bridge_dependents:
                            _execute_candidate(
                                summary,
                                cfg,
                                predict_date=normalized_date,
                                algo_env=algo_env,
                                scheduled_control_plane=(
                                    scheduled_control_plane
                                ),
                                scheduled_execution_context=(
                                    scheduled_execution_context
                                ),
                                scheduled_preflight_failure=(
                                    SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
                                ),
                            )
                    elif gate_code is not None:
                        for cfg in data_bridge_dependents:
                            summary.blocked.append(_candidate_item(cfg, gate_code))
                    else:
                        for cfg in data_bridge_dependents:
                            _execute_candidate(
                                summary,
                                cfg,
                                predict_date=normalized_date,
                                algo_env=algo_env,
                                scheduled_control_plane=(
                                    scheduled_control_plane
                                ),
                                scheduled_execution_context=(
                                    scheduled_execution_context
                                ),
                            )

            _finalize(summary, configuration_error=False)
            return summary
        except LaunchdPredictionConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001 - outer dependencies are config failures
            raise LaunchdPredictionConfigurationError(
                "launchd prediction dependencies are unavailable"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()


def _configuration_summary(
    cadence: str,
    predict_date: str,
    *,
    event: str = "launchd_prediction_run",
) -> LaunchdPredictionSummary:
    summary = LaunchdPredictionSummary(
        cadence=str(cadence).strip().lower(),
        predict_date=str(predict_date),
        event=event,
        outcome="configuration_error",
        exit_code=2,
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：只接受 cadence/date/env；错误始终为固定的结构化摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cadence",
        required=True,
        choices=sorted(PREDICTION_CADENCES),
    )
    parser.add_argument("--predict-date", default=_today())
    parser.add_argument("--algo-env", default=DEFAULT_ALGO_ENV)
    args = parser.parse_args(argv)
    try:
        summary = run(
            args.cadence,
            predict_date=args.predict_date,
            algo_env=args.algo_env,
        )
    except LaunchdPredictionConfigurationError:
        summary = _configuration_summary(args.cadence, args.predict_date)
    except Exception:  # noqa: BLE001 - CLI must never serialize raw dependency errors
        summary = _configuration_summary(args.cadence, args.predict_date)
    print(
        json.dumps(
            summary.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
