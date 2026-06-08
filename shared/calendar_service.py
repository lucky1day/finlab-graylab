from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


SOURCE_WEEK_TABLES = ("api_wind_weekly", "api_wind_derivative_weekly", "api_wind_daily")


class CalendarService:
    """交易日历和周编号查询服务。"""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def is_trading_day(self, value: str | date | datetime) -> bool:
        """判断指定日期是否交易日。"""
        day = _date_string(value)
        stmt = text("SELECT trade_flag FROM t_trade_calendar WHERE rdate = :rdate LIMIT 1")
        with self._engine_scope() as engine:
            with engine.connect() as conn:
                flag = conn.execute(stmt, {"rdate": day}).scalar()
        if flag is not None:
            return str(flag).strip() == "1"

        try:
            from chinese_calendar import is_workday
        except ImportError:
            return _to_date(day).weekday() < 5
        return bool(is_workday(_to_date(day)))

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
        with self._engine_scope() as engine:
            with engine.connect() as conn:
                rows = conn.execute(stmt, {"rdate": _date_string(value), "limit": int(count)}).scalars().all()
        return [_date_string(row) for row in rows]

    def nth_trading_day_after(self, value: str | date | datetime, n: int) -> str:
        """返回指定日期之后第 n 个交易日。"""
        days = self.next_trading_days(value, n)
        if len(days) < n:
            raise ValueError(f"not enough trading days after {_date_string(value)}")
        return days[-1]

    def week_id_for_date(self, value: str | date | datetime) -> int | None:
        """按源表实际 rdate 反查 week_id。"""
        target_date = _date_string(value)
        with self._engine_scope() as engine:
            for table_name in SOURCE_WEEK_TABLES:
                stmt = text(
                    f"""
                    SELECT week_id
                    FROM {table_name}
                    WHERE rdate = :target_date
                      AND week_id IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
                try:
                    with engine.connect() as conn:
                        row = conn.execute(stmt, {"target_date": target_date}).mappings().first()
                except SQLAlchemyError:
                    continue
                if row and row["week_id"] is not None:
                    return int(row["week_id"])
        return None

    def week_id_to_last_trading_day(self, week_id: int | float | str) -> str:
        """返回 week_id 对应周内最后一个交易日，缺日历时回退到周五规则。"""
        start = _date_string(_week_id_to_monday(week_id))
        end = _date_string(_week_id_to_friday(week_id))
        stmt = text(
            """
            SELECT rdate
            FROM t_trade_calendar
            WHERE trade_flag = '1' AND rdate BETWEEN :start_date AND :end_date
            ORDER BY rdate DESC
            LIMIT 1
            """
        )
        with self._engine_scope() as engine:
            try:
                with engine.connect() as conn:
                    row = conn.execute(stmt, {"start_date": start, "end_date": end}).scalar()
            except SQLAlchemyError:
                row = None
        return _date_string(row) if row is not None else end

    @contextmanager
    def _engine_scope(self) -> Iterator[Engine]:
        own_engine = self._engine is None
        if own_engine:
            from shared.data_service import create_sqlalchemy_engine

            engine = create_sqlalchemy_engine()
        else:
            engine = self._engine
        try:
            yield engine
        finally:
            if own_engine:
                engine.dispose()


def get_calendar(engine: Engine | None = None) -> CalendarService:
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


def _first_monday_of_year(year: int) -> date:
    first_day = date(year, 1, 1)
    return first_day + timedelta(days=(7 - first_day.weekday()) % 7)


def _week_id_to_monday(week_id: int | float | str) -> date:
    text_value = str(int(week_id))
    year = int(text_value[:4])
    week = int(text_value[4:])
    return _first_monday_of_year(year) + timedelta(weeks=week - 1)


def _week_id_to_friday(week_id: int | float | str) -> date:
    return _week_id_to_monday(week_id) + timedelta(days=4)
