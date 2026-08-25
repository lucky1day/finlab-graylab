from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from shared.actual_facts import (
    build_daily_actual_records_from_rows,
    build_monthly_actual_records_from_rows,
    read_trade_calendar_rows as read_month_calendar,
    read_yield_rows,
)
from shared.models import ActualRecord, MonthlyActualRecord


def build_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[ActualRecord]:
    """构建日频实际方向记录。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    return build_daily_actual_records_from_rows(rows, start_date=start_date)


def build_monthly_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[MonthlyActualRecord]:
    """从日频收益率源表构建月度实际方向。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    calendar_rows = read_month_calendar(engine)
    return build_monthly_actual_records_from_rows(
        rows,
        calendar_rows,
        start_date=start_date,
        end_date=end_date,
    )
