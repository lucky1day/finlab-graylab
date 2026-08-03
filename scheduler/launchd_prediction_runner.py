"""由 launchd one-shot 启动的单批预测入口。

本模块不保存 cron、ledger、occurrence 或 startup catch-up 状态。每次进程启动时
只做一次严格发现、精确准入、DataBridge Gate 校验和逐方案执行；跨 cadence 的
互斥由 runtime 目录中的单一非阻塞锁保证。
"""

from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from scheduler.blackbox_scheduler_admission import (
    LAUNCHD_ONE_SHOT,
    BlackboxSchedulerAdmissionError,
    ScheduledPredictionConfigurationError,
    ScheduledPredictionControlPlaneDenied,
    load_blackbox_scheduler_admission,
    require_scheduled_prediction_control_plane_with_policy_snapshot,
    uses_blackbox_scheduler_admission,
)
from scheduler.discovery import discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, execute_scheme
from scheduler.repository import create_engine_from_env
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.calendar_service import get_calendar
from shared.data_bridge.refresh import DataBridgeRefreshConfig


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
VALID_CADENCES = frozenset({"daily", "weekly", "monthly"})


class LaunchdPredictionConfigurationError(RuntimeError):
    """launchd one-shot prediction runner 的配置或输入不成立。"""


@dataclass
class LaunchdPredictionSummary:
    """一次 one-shot 批运行的无敏感结构化摘要。"""

    cadence: str
    predict_date: str
    discovered: int = 0
    outcome: str = "success"
    exit_code: int = 0
    executed: list[dict[str, object]] = field(default_factory=list)
    denied: list[dict[str, object]] = field(default_factory=list)
    blocked: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        """返回字段与顺序稳定、可写入 launchd 日志的 JSON 对象。"""
        return {
            "event": "launchd_prediction_run",
            "cadence": self.cadence,
            "predict_date": self.predict_date,
            "outcome": self.outcome,
            "discovered": self.discovered,
            "executed": _sorted_items(self.executed),
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
    if cadence not in VALID_CADENCES:
        raise LaunchdPredictionConfigurationError("invalid cadence")
    return cadence


def _normalize_predict_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise LaunchdPredictionConfigurationError("invalid predict_date") from exc


@contextmanager
def _runner_lock(config: DataBridgeRefreshConfig) -> Iterator[bool]:
    """以跨 cadence 的全局非阻塞锁排除第二个 generic writer。"""
    lock_path = config.runtime_root / "launchd_prediction_runner" / "predictions.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise LaunchdPredictionConfigurationError(
            "launchd prediction runner lock is unavailable"
        ) from exc


def _candidate_item(cfg: object, code: str) -> dict[str, object]:
    return {"scheme_id": str(getattr(cfg, "scheme_id", "")), "code": code}


def _finalize(summary: LaunchdPredictionSummary, *, configuration_error: bool) -> None:
    for items in (
        summary.executed,
        summary.denied,
        summary.blocked,
        summary.skipped,
        summary.failed,
    ):
        items.sort(key=lambda item: str(item.get("scheme_id", "")))
    if configuration_error:
        summary.outcome = "configuration_error"
        summary.exit_code = 2
    elif summary.denied or summary.blocked or summary.skipped or summary.failed:
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
    """执行一次指定 cadence 的 launchd-only scheduled_live 批次。"""
    normalized_cadence = _normalize_cadence(cadence)
    normalized_date = _normalize_predict_date(predict_date)
    if normalized_cadence == "monthly" and date.fromisoformat(normalized_date).day != 15:
        raise LaunchdPredictionConfigurationError(
            "monthly one-shot must run on natural month day 15"
        )

    try:
        data_bridge_config = DataBridgeRefreshConfig.from_env()
    except Exception as exc:  # noqa: BLE001 - only expose stable CLI category
        raise LaunchdPredictionConfigurationError(
            "DataBridge refresh configuration is unavailable"
        ) from exc

    summary = LaunchdPredictionSummary(
        cadence=normalized_cadence,
        predict_date=normalized_date,
    )
    with _runner_lock(data_bridge_config) as acquired:
        if not acquired:
            summary.outcome = "lock_conflict"
            summary.exit_code = 1
            return summary

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
            and getattr(cfg, "frequency", None) == normalized_cadence
        ]
        summary.discovered = len(candidates)

        controlled_candidates = [
            cfg
            for cfg in candidates
            if uses_blackbox_scheduler_admission(cfg)
        ]
        policy = None
        policy_invalid = False
        if controlled_candidates:
            try:
                policy = load_blackbox_scheduler_admission()
            except BlackboxSchedulerAdmissionError:
                policy_invalid = True

        admitted_candidates: list[object] = []
        for cfg in candidates:
            if not uses_blackbox_scheduler_admission(cfg):
                admitted_candidates.append(cfg)
                continue
            if policy_invalid:
                summary.blocked.append(
                    _candidate_item(
                        cfg,
                        "admission_configuration_invalid",
                    )
                )
                continue
            try:
                require_scheduled_prediction_control_plane_with_policy_snapshot(
                    cfg,
                    plane=LAUNCHD_ONE_SHOT,
                    policy=policy,
                )
            except ScheduledPredictionControlPlaneDenied:
                summary.denied.append(
                    _candidate_item(cfg, "control_plane_denied")
                )
                continue
            except ScheduledPredictionConfigurationError:
                summary.blocked.append(
                    _candidate_item(
                        cfg,
                        "admission_configuration_invalid",
                    )
                )
                continue
            admitted_candidates.append(cfg)

        if not admitted_candidates:
            _finalize(summary, configuration_error=policy_invalid)
            return summary

        engine = None
        try:
            engine = create_engine_from_env()
            calendar = get_calendar(engine)
            if (
                normalized_cadence == "daily"
                and not calendar.is_trading_day(normalized_date)
            ):
                summary.denied.clear()
                summary.blocked.clear()
                summary.skipped.clear()
                summary.failed.clear()
                summary.outcome = "not_applicable"
                summary.exit_code = 0
                return summary

            expected_daily_date: str | None = None
            for cfg in admitted_candidates:
                if (
                    getattr(cfg, "runtime_type", None) == "blackbox_v2"
                    and getattr(cfg, "input_source", None)
                    == "data_bridge_current"
                ):
                    if expected_daily_date is None:
                        try:
                            expected_daily_date = str(
                                calendar.previous_trading_day(normalized_date)
                            )[:10]
                        except Exception:  # noqa: BLE001 - preserve calendar isolation
                            summary.blocked.append(
                                _candidate_item(cfg, "calendar_unavailable")
                            )
                            continue
                    try:
                        require_v2_daily_ready(
                            data_bridge_config,
                            normalized_date,
                            expected_daily_date,
                        )
                    except V2DailyGateBlocked:
                        summary.blocked.append(
                            _candidate_item(cfg, "v2_gate_blocked")
                        )
                        continue
                    except Exception:  # noqa: BLE001 - never reveal low-level gate details
                        summary.blocked.append(
                            _candidate_item(cfg, "v2_gate_unavailable")
                        )
                        continue

                try:
                    result = execute_scheme(
                        cfg,
                        normalized_date,
                        algo_env=algo_env,
                        prediction_phase="scheduled_live",
                        scheduled_control_plane="launchd_one_shot",
                    )
                except Exception:  # noqa: BLE001 - isolate one candidate
                    summary.failed.append(
                        _candidate_item(cfg, "execution_exception")
                    )
                    continue

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
                    summary.skipped.append(
                        _candidate_item(cfg, "execution_skipped")
                    )
                else:
                    summary.failed.append(
                        _candidate_item(cfg, f"execution_{status}"))

            _finalize(summary, configuration_error=policy_invalid)
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


def _configuration_summary(cadence: str, predict_date: str) -> LaunchdPredictionSummary:
    summary = LaunchdPredictionSummary(
        cadence=str(cadence).strip().lower(),
        predict_date=str(predict_date),
        outcome="configuration_error",
        exit_code=2,
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：只接受 cadence/date/env；错误始终为固定的结构化摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadence", required=True, choices=sorted(VALID_CADENCES))
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
