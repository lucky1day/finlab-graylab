"""由已安装 one-shot 控制面单次执行的三频 actuals 刷新入口。"""
from __future__ import annotations

import argparse
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from scheduler.actuals_errors import ActualStageError
from scheduler.daily_actuals_updater import update_actuals_detailed
from scheduler.monthly_actuals_updater import update_monthly_actuals_detailed
from scheduler.period_average_actuals_updater import update_period_average_actuals_detailed
from scheduler.repository import ActualWriteStats, create_engine_from_env
from scheduler.weekly_actuals_updater import update_weekly_actuals_detailed
from shared.calendar_service import CalendarService, get_calendar


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
PERIOD_AVERAGE_ACTUALS_START_DATE = "2025-01-01"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActualStageSummary:
    """单个 Actual 阶段的终态与写入证据。"""

    stage: str
    status: str
    stats: ActualWriteStats | None = None
    code: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class ActualsJobSummary:
    """一次 Actuals 批次的分阶段终态。"""

    run_date: str
    daily_weekly_end_date: str
    overall: str
    stages: tuple[ActualStageSummary, ...]


class ActualsJobError(RuntimeError):
    """Actual 阶段失败，并携带失败前已提交阶段的完整摘要。"""

    def __init__(self, summary: ActualsJobSummary, cause: Exception) -> None:
        super().__init__(f"Actuals stage failed: {type(cause).__name__}")
        self.summary = summary
        self.cause = cause


def _today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_run_date(value: str | date | None) -> str:
    if value is None:
        return _today()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _is_trading_day(calendar: CalendarService, run_date: str) -> bool:
    """判定是否交易日；日历未收录该日期时以配置错误 fail-closed。

    `t_trade_calendar` 需要人工逐年延长。覆盖耗尽时 `is_trading_day` 同样返回
    False，若不先区分，本入口会把「日历没续期」当成节假日，每天用日历最后一个
    交易日重刷同一批 actuals 并以退出码 0 结束，运维看不出 actuals 已停止推进。
    """
    if not calendar.covers(run_date):
        raise ValueError(f"trade calendar does not cover run date {run_date}")
    return calendar.is_trading_day(run_date)


def _previous_trading_day(calendar: CalendarService, run_date: str) -> str:
    return calendar.previous_trading_day(run_date)


def run_actuals_job(
    run_date: str | date | None = None,
) -> ActualsJobSummary:
    """顺序执行 Actual 阶段，并在失败时保留已完成阶段的提交证据。"""
    target_date = _normalize_run_date(run_date)
    engine = create_engine_from_env()
    try:
        calendar = get_calendar(engine=engine)
        if not _is_trading_day(calendar, target_date):
            daily_weekly_end_date = _previous_trading_day(
                calendar,
                target_date,
            )
            logger.info(
                "Refresh daily/weekly actuals to previous trading day %s on non-trading day %s; "
                "monthly actuals still refresh to %s",
                daily_weekly_end_date,
                target_date,
                target_date,
            )
        else:
            daily_weekly_end_date = target_date

        stages: list[ActualStageSummary] = []
        calls: tuple[tuple[str, Callable[[], ActualWriteStats]], ...] = (
            (
                "daily",
                lambda: update_actuals_detailed(
                    end_date=daily_weekly_end_date,
                    engine=engine,
                ),
            ),
            (
                "weekly",
                lambda: update_weekly_actuals_detailed(
                    end_date=daily_weekly_end_date,
                    engine=engine,
                ),
            ),
            (
                "monthly",
                lambda: update_monthly_actuals_detailed(
                    end_date=target_date,
                    engine=engine,
                ),
            ),
            (
                "period_average",
                lambda: update_period_average_actuals_detailed(
                    # 全历史窗口仍用于捕获迟到数据与源修订；相同事实不会重写。
                    start_date=PERIOD_AVERAGE_ACTUALS_START_DATE,
                    end_date=target_date,
                    engine=engine,
                ),
            ),
        )
        for index, (stage_name, run_stage) in enumerate(calls):
            try:
                stats = run_stage()
            except Exception as exc:
                failure_code = (
                    exc.code
                    if isinstance(exc, ActualStageError)
                    else "stage_failed"
                )
                failure = ActualStageSummary(
                    stage=stage_name,
                    status="failed",
                    code=failure_code,
                    error_type=type(exc).__name__,
                )
                skipped = [
                    ActualStageSummary(stage=name, status="not_run")
                    for name, _ in calls[index + 1 :]
                ]
                stages.append(failure)
                stages.extend(skipped)
                summary = ActualsJobSummary(
                    run_date=target_date,
                    daily_weekly_end_date=daily_weekly_end_date,
                    overall="partial" if index > 0 else "failed",
                    stages=tuple(stages),
                )
                logger.exception(
                    "actuals_stage_exception stage=%s code=%s error_type=%s",
                    stage_name,
                    failure_code,
                    type(exc).__name__,
                )
                logger.error("actuals_stage_terminal %s", asdict(failure))
                for skipped_stage in skipped:
                    logger.info(
                        "actuals_stage_terminal %s",
                        asdict(skipped_stage),
                    )
                logger.error("actuals_job_terminal %s", asdict(summary))
                raise ActualsJobError(summary, exc) from exc
            stage_summary = ActualStageSummary(
                stage=stage_name,
                status="success",
                stats=stats,
            )
            stages.append(stage_summary)
            logger.info("actuals_stage_terminal %s", asdict(stage_summary))

        summary = ActualsJobSummary(
            run_date=target_date,
            daily_weekly_end_date=daily_weekly_end_date,
            overall="success",
            stages=tuple(stages),
        )
        logger.info(
            "actuals_job_terminal %s",
            asdict(summary),
        )
        return summary
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """解析一次性 actuals runner 的命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Refresh daily, weekly, monthly, and period-average actuals."
    )
    parser.add_argument("--date", default=None, help="Run date in YYYY-MM-DD format")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run_actuals_job(run_date=args.date)
    except ActualsJobError as exc:
        logger.error("Actuals runner failed: summary=%s", asdict(exc.summary))
        return 1
    except ValueError as exc:
        logger.error("Actuals runner argument or configuration error: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
