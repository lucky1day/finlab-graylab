from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

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
) -> int:
    """刷新 t_scheme_monthly_actuals，不修改日度/周度 actuals。"""
    engine = create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            engine,
            frequency="monthly",
            tenors=tenors,
        )
        if not selected_tenors:
            return 0
        rows = read_yield_rows(
            engine,
            tenors=selected_tenors,
            end_date=end_date,
        )
        records = build_monthly_actual_records_from_rows(
            rows,
            read_month_calendar(engine),
            start_date=start_date,
            end_date=end_date,
        )
        return upsert_monthly_actuals(engine, records)
    finally:
        engine.dispose()
