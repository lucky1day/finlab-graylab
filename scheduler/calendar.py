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
    """只读 t_trade_calendar.trade_flag 判断交易日。"""
    day = normalize_date(value)
    sql = text("SELECT trade_flag FROM t_trade_calendar WHERE rdate = :rdate LIMIT 1")
    with engine.connect() as conn:
        flag = conn.execute(sql, {"rdate": day.isoformat()}).scalar()
    if flag is not None:
        return str(flag).strip() == "1"
    return False
