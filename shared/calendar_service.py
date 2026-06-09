from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine


class CalendarService:
    """交易日历和周编号查询服务。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def is_trading_day(self, value: str | date | datetime) -> bool:
        """只读 t_trade_calendar.trade_flag 判断交易日，无回退。"""
        day = _date_string(value)
        stmt = text("SELECT trade_flag FROM t_trade_calendar WHERE rdate = :rdate LIMIT 1")
        with self._engine.connect() as conn:
            flag = conn.execute(stmt, {"rdate": day}).scalar()
        if flag is not None:
            return str(flag).strip() == "1"
        return False

    def next_trading_days(self, value: str | date | datetime, count: int) -> list[str]:
        """返回指定日期之后的后续交易日。"""
        if count <= 0:
            return []
        stmt = text(
            """
            SELECT rdate
            FROM t_trade_calendar
            WHERE trade_flag = '1' AND rdate > :rdate
            ORDER BY rdate
            LIMIT :limit
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt, {"rdate": _date_string(value), "limit": int(count)}).scalars().all()
        return [_date_string(row) for row in rows]

    def nth_trading_day_after(self, value: str | date | datetime, n: int) -> str:
        """返回指定日期之后第 n 个交易日。"""
        days = self.next_trading_days(value, n)
        if len(days) < n:
            raise ValueError(f"not enough trading days after {_date_string(value)}")
        return days[-1]

    def week_id_for_date(self, value: str | date | datetime) -> int | None:
        """唯一权威源：api_wind_date.week_id。"""
        target_date = _date_string(value)
        stmt = text("SELECT week_id FROM api_wind_date WHERE rdate = :rdate LIMIT 1")
        with self._engine.connect() as conn:
            row = conn.execute(stmt, {"rdate": target_date}).mappings().first()
        if row and row["week_id"] is not None:
            return int(row["week_id"])
        return None

    def week_id_to_last_trading_day(self, week_id: int | float | str) -> str:
        """同周日期中取 t_trade_calendar.trade_flag='1' 的最大日期。"""
        wid = str(int(week_id))
        stmt = text(
            """
            SELECT MAX(wd.rdate)
            FROM api_wind_date wd
            JOIN t_trade_calendar tc ON tc.rdate = wd.rdate
            WHERE wd.week_id = :week_id AND tc.trade_flag = '1'
            """
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt, {"week_id": wid}).scalar()
        if row is not None:
            return _date_string(row)

        fallback = text("SELECT MAX(rdate) FROM api_wind_date WHERE week_id = :week_id")
        with self._engine.connect() as conn:
            row = conn.execute(fallback, {"week_id": wid}).scalar()
        if row is not None:
            return _date_string(row)
        raise ValueError(f"no date found for week_id={wid} in api_wind_date")


def get_calendar(engine: Engine) -> CalendarService:
    """获取日历服务。"""
    return CalendarService(engine=engine)


def _date_string(value: str | date | datetime) -> str:
    return _to_date(value).isoformat()


def _to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()
