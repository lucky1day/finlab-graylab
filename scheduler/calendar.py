from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine


def normalize_date(value: str | date | datetime) -> date:
    """规范化日期输入。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_trading_day(engine: Engine, value: str | date | datetime) -> bool:
    """判断是否交易日，优先使用本机交易日历，缺失时回退到 chinese_calendar。"""
    day = normalize_date(value)
    sql = text("SELECT trade_flag FROM t_trade_calendar WHERE rdate = :rdate LIMIT 1")
    with engine.connect() as conn:
        flag = conn.execute(sql, {"rdate": day.isoformat()}).scalar()
    if flag is not None:
        return str(flag).strip() == "1"

    try:
        from chinese_calendar import is_workday
    except ImportError:
        return day.weekday() < 5
    return bool(is_workday(day))
