from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from shared.actual_facts import (
    build_daily_actual_records_from_rows,
    build_monthly_actual_records_from_rows as build_shared_monthly_actual_records_from_rows,
    read_trade_calendar_rows,
    read_yield_rows as read_shared_yield_rows,
)
from shared.models import ActualRecord, MonthlyActualRecord


def read_yield_rows(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> list[dict]:
    """读取实际收益率序列。"""
    return read_shared_yield_rows(engine, tenors=tenors, end_date=end_date)


def build_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[ActualRecord]:
    """构建日频实际方向记录。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    return build_daily_actual_records_from_rows(rows, start_date=start_date)


def read_month_calendar(engine: Engine) -> list[dict]:
    """读取交易日历。"""
    return read_trade_calendar_rows(engine)


def build_monthly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[MonthlyActualRecord]:
    """按 feature 月 15 日与 target 月 15 日观测收益率生成月度实际方向。"""
    return build_shared_monthly_actual_records_from_rows(
        rows,
        calendar_rows,
        start_date=start_date,
        end_date=end_date,
    )


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
