"""由 launchd 单次执行的 daily / weekly / monthly actuals 刷新入口。"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scheduler.calendar import is_trading_day
from scheduler.daily_actuals_updater import update_actuals
from scheduler.monthly_actuals_updater import update_monthly_actuals
from scheduler.repository import create_engine_from_env
from scheduler.weekly_actuals_updater import update_weekly_actuals


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
    engine = create_engine_from_env()
    try:
        return is_trading_day(engine, run_date)
    finally:
        engine.dispose()


def _previous_trading_day(run_date: str) -> str:
    engine = create_engine_from_env()
    try:
        sql = text(
            """
            SELECT MAX(rdate)
            FROM t_trade_calendar
            WHERE trade_flag = '1'
              AND rdate < :rdate
            """
        )
        with engine.connect() as conn:
            value = conn.execute(sql, {"rdate": run_date}).scalar()
        if value is None:
            raise ValueError(f"no previous trading day before {run_date}")
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]
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
