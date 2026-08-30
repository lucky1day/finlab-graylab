from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.daily_actuals_updater import resolve_actual_tenors
from scheduler.repository import create_engine_from_env, upsert_monthly_actuals
from shared.actual_facts import (
    build_monthly_actual_records_from_rows,
    read_trade_calendar_rows as read_month_calendar,
    read_yield_rows,
)


def update_monthly_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
    *,
    engine: Engine | None = None,
) -> int:
    """刷新月度 actuals；注入 Engine 时由调用方管理生命周期。"""
    owns_engine = engine is None
    target_engine = engine if engine is not None else create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            target_engine,
            frequency="monthly",
            tenors=tenors,
        )
        if not selected_tenors:
            return 0
        rows = read_yield_rows(
            target_engine,
            tenors=selected_tenors,
            end_date=end_date,
        )
        records = build_monthly_actual_records_from_rows(
            rows,
            read_month_calendar(target_engine),
            start_date=start_date,
            end_date=end_date,
        )
        return upsert_monthly_actuals(target_engine, records)
    finally:
        if owns_engine:
            target_engine.dispose()
