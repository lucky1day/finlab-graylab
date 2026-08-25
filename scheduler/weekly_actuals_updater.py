from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from scheduler.daily_actuals_updater import resolve_actual_tenors
from scheduler.repository import create_engine_from_env, upsert_weekly_actuals
from shared.actual_facts import (
    build_weekly_actual_records_from_rows,
    read_week_calendar_rows,
    read_yield_rows,
)


def update_weekly_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """刷新 t_scheme_weekly_actuals，不修改日度 actuals。"""
    engine = create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            engine,
            frequency="weekly",
            tenors=tenors,
        )
        if not selected_tenors:
            return 0
        rows = read_yield_rows(
            engine,
            tenors=selected_tenors,
            end_date=end_date,
        )
        records = build_weekly_actual_records_from_rows(
            rows,
            read_week_calendar_rows(engine),
            start_date=start_date,
            end_date=end_date,
        )
        return upsert_weekly_actuals(engine, records)
    finally:
        engine.dispose()
