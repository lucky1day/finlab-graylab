"""由 launchd 单次执行的 daily / weekly / monthly actuals 刷新入口。"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from scheduler.daily_actuals_updater import update_actuals
from scheduler.monthly_actuals_updater import update_monthly_actuals
from scheduler.repository import create_engine_from_env
from scheduler.weekly_actuals_updater import update_weekly_actuals
from shared.calendar_service import get_calendar


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_run_date(value: str | date | None) -> str:
    if value is None:
        return _today()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _is_trading_day(run_date: str) -> bool:
    """判定是否交易日；日历未收录该日期时以配置错误 fail-closed。

    `t_trade_calendar` 需要人工逐年延长。覆盖耗尽时 `is_trading_day` 同样返回
    False，若不先区分，本入口会把「日历没续期」当成节假日，每天用日历最后一个
    交易日重刷同一批 actuals 并以退出码 0 结束，运维看不出 actuals 已停止推进。
    """
    engine = create_engine_from_env()
    try:
        calendar = get_calendar(engine=engine)
        if not calendar.covers(run_date):
            raise ValueError(f"trade calendar does not cover run date {run_date}")
        return calendar.is_trading_day(run_date)
    finally:
        engine.dispose()


def _previous_trading_day(run_date: str) -> str:
    engine = create_engine_from_env()
    try:
        return get_calendar(engine=engine).previous_trading_day(run_date)
    finally:
        engine.dispose()


def run_actuals_job(
    run_date: str | date | None = None,
    force: bool = False,
) -> None:
    """执行一次 actuals 刷新，并保持既有非交易日日期语义。"""
    target_date = _normalize_run_date(run_date)
    if not force and not _is_trading_day(target_date):
        daily_weekly_end_date = _previous_trading_day(target_date)
        logger.info(
            "Refresh daily/weekly actuals to previous trading day %s on non-trading day %s; "
            "monthly actuals still refresh to %s",
            daily_weekly_end_date,
            target_date,
            target_date,
        )
    else:
        daily_weekly_end_date = target_date

    daily_written = update_actuals(end_date=daily_weekly_end_date)
    weekly_written = update_weekly_actuals(end_date=daily_weekly_end_date)
    monthly_written = update_monthly_actuals(end_date=target_date)
    logger.info(
        "Actuals refresh finished: date=%s daily_weekly_end_date=%s daily_records=%s "
        "weekly_records=%s monthly_records=%s",
        target_date,
        daily_weekly_end_date,
        daily_written,
        weekly_written,
        monthly_written,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """解析一次性 actuals runner 的命令行参数。"""
    parser = argparse.ArgumentParser(description="Refresh daily, weekly, and monthly actuals.")
    parser.add_argument("--date", default=None, help="Run date in YYYY-MM-DD format")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh daily and weekly actuals at --date on a non-trading day",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run_actuals_job(run_date=args.date, force=args.force)
    except ValueError as exc:
        logger.error("Actuals runner argument or configuration error: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
