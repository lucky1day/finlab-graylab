from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, datetime
from functools import cached_property
from typing import Mapping

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from shared.week_calendar_normalizer import normalize_week_calendar_rows


def read_calendar_snapshot_from_connection(
    connection: Connection,
) -> Mapping[str, pd.DataFrame]:
    """复用调用方事务导出两张日历表，避免拆分一致性快照。"""
    queries = (
        (
            "api_wind_date.csv",
            text(
                """
                SELECT rdate, week_id
                FROM api_wind_date
                ORDER BY rdate
                """
            ),
            ("rdate", "week_id"),
        ),
        (
            "t_trade_calendar.csv",
            text(
                """
                SELECT rdate, trade_flag
                FROM t_trade_calendar
                ORDER BY rdate
                """
            ),
            ("rdate", "trade_flag"),
        ),
    )
    return {
        filename: pd.DataFrame(
            connection.execute(statement).mappings().all(),
            columns=list(columns),
        )
        for filename, statement, columns in queries
    }


def is_trading_day_row(
    rdate: str | date | datetime,
    trade_flag: object,
) -> bool:
    """判断一行日历是否为交易日：工作日历 trade_flag='1' 且非周末。

    ``t_trade_calendar`` 是工作日历，跟随国务院节假日安排：调休补班的周六/周日
    ``trade_flag`` 同样为 ``'1'``，但市场在这些日子无行情。平台的交易日必须在
    工作日基础上再排除周末，本函数是该判定的唯一定义。
    """
    if str(trade_flag).strip() != "1":
        return False
    return _to_date(rdate).weekday() < 5


class CalendarService:
    """交易日历和周编号查询服务。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def covers(self, value: str | date | datetime) -> bool:
        """日历是否收录该日期，与是否交易日无关。

        ``is_trading_day`` 对未收录日期返回 False，无法区分「节假日」与
        「日历尚未延长到该日期」。需要区分二者的调用方（例如按日历判断本次
        调度是否适用）必须先用本方法确认覆盖，否则会把运维事件误判成休市。
        """
        return _date_string(value) in self._calendar_dates

    def is_trading_day(self, value: str | date | datetime) -> bool:
        """日历未收录的日期返回 False，与既有契约一致。"""
        return _date_string(value) in self._trading_day_set

    def next_trading_days(self, value: str | date | datetime, count: int) -> list[str]:
        """返回指定日期之后的后续交易日。"""
        if count <= 0:
            return []
        days = self._trading_days
        return list(days[bisect_right(days, _date_string(value)) :][: int(count)])

    def previous_trading_day(self, value: str | date | datetime) -> str:
        """返回指定日期之前最近的交易日。"""
        day = _date_string(value)
        days = self._trading_days
        position = bisect_left(days, day)
        if position == 0:
            raise ValueError(f"no previous trading day before {day}")
        return days[position - 1]

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
        """同周日期中取最大的交易日（工作日历 trade_flag='1' 且非周末）。"""
        wid = int(week_id)
        row = self._week_last_trading_day.get(wid)
        if row is not None:
            return row
        raise ValueError(
            f"no trading day found for week_id={wid} in api_wind_date/t_trade_calendar"
        )

    @cached_property
    def _calendar_rows(self) -> tuple[tuple[str, object], ...]:
        """一次载入并冻结整张工作日历。"""
        stmt = text("SELECT rdate, trade_flag FROM t_trade_calendar ORDER BY rdate")
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(
            (_date_string(row["rdate"]), row["trade_flag"]) for row in rows
        )

    @cached_property
    def _calendar_dates(self) -> frozenset[str]:
        return frozenset(rdate for rdate, _flag in self._calendar_rows)

    @cached_property
    def _trading_days(self) -> tuple[str, ...]:
        """交易日序列，判定口径见 is_trading_day_row。"""
        return tuple(
            sorted(
                rdate
                for rdate, flag in self._calendar_rows
                if is_trading_day_row(rdate, flag)
            )
        )

    @cached_property
    def _trading_day_set(self) -> frozenset[str]:
        return frozenset(self._trading_days)

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
            if row.get("week_id") is None or not is_trading_day_row(
                row["rdate"], row.get("trade_flag")
            ):
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
