from __future__ import annotations

from datetime import date, datetime
from functools import cached_property

from sqlalchemy import text
from sqlalchemy.engine import Engine

from shared.week_calendar_normalizer import normalize_week_calendar_rows


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

    def previous_trading_day(self, value: str | date | datetime) -> str:
        """返回指定日期之前最近的交易日。"""
        stmt = text(
            """
            SELECT MAX(rdate)
            FROM t_trade_calendar
            WHERE trade_flag = '1' AND rdate < :rdate
            """
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt, {"rdate": _date_string(value)}).scalar()
        if row is None:
            raise ValueError(f"no previous trading day before {_date_string(value)}")
        return _date_string(row)

    def nth_trading_day_after(self, value: str | date | datetime, n: int) -> str:
        """返回指定日期之后第 n 个交易日。"""
        days = self.next_trading_days(value, n)
        if len(days) < n:
            raise ValueError(f"not enough trading days after {_date_string(value)}")
        return days[-1]

    def week_id_for_date(self, value: str | date | datetime) -> int | None:
        """读取 DB 周编号，并修正源表中孤立的周编号跳变。"""
        target_date = _date_string(value)
        return self._date_to_week_id.get(target_date)

    def week_id_to_last_trading_day(self, week_id: int | float | str) -> str:
        """同周日期中取 t_trade_calendar.trade_flag='1' 的最大日期。"""
        wid = int(week_id)
        row = self._week_last_trading_day.get(wid)
        if row is not None:
            return row

        raw_wid = str(wid)
        fallback = text("SELECT MAX(rdate) FROM api_wind_date WHERE week_id = :week_id")
        with self._engine.connect() as conn:
            row = conn.execute(fallback, {"week_id": raw_wid}).scalar()
        if row is not None:
            return _date_string(row)
        raise ValueError(f"no date found for week_id={raw_wid} in api_wind_date")

    @cached_property
    def _normalized_week_rows(self) -> list[dict]:
        stmt = text(
            """
            SELECT wd.rdate, wd.week_id, tc.trade_flag
            FROM api_wind_date wd
            LEFT JOIN t_trade_calendar tc ON tc.rdate = wd.rdate
            WHERE wd.week_id IS NOT NULL
            ORDER BY wd.rdate
            """
        )
        with self._engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(stmt).mappings().all()]
        return normalize_week_calendar_rows(rows)

    @cached_property
    def _date_to_week_id(self) -> dict[str, int]:
        result = {}
        for row in self._normalized_week_rows:
            week_id = row.get("week_id")
            if week_id is not None:
                result[_date_string(row["rdate"])] = int(week_id)
        return result

    @cached_property
    def _week_last_trading_day(self) -> dict[int, str]:
        result: dict[int, str] = {}
        for row in self._normalized_week_rows:
            if str(row.get("trade_flag")).strip() != "1" or row.get("week_id") is None:
                continue
            week_id = int(row["week_id"])
            rdate = _date_string(row["rdate"])
            if week_id not in result or rdate > result[week_id]:
                result[week_id] = rdate
        return result


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
