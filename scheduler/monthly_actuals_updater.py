from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.actuals_errors import require_actual_source_tenors
from scheduler.daily_actuals_updater import resolve_actual_tenors
from scheduler.repository import (
    ActualWriteStats,
    create_engine_from_env,
    upsert_monthly_actuals_detailed,
)
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
    """兼容入口：刷新月度 Actual，并返回本批处理条数。"""
    return update_monthly_actuals_detailed(
        start_date=start_date,
        end_date=end_date,
        tenors=tenors,
        engine=engine,
    ).attempted


def update_monthly_actuals_detailed(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
    *,
    engine: Engine | None = None,
) -> ActualWriteStats:
    """刷新月度 Actual，并返回真实写入统计。"""
    owns_engine = engine is None
    target_engine = engine if engine is not None else create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            target_engine,
            frequency="monthly",
            tenors=tenors,
        )
        if not selected_tenors:
            return ActualWriteStats()
        rows = require_actual_source_tenors(
            read_yield_rows(
                target_engine,
                tenors=selected_tenors,
                end_date=end_date,
            ),
            expected_tenors=selected_tenors,
            stage="monthly",
        )
        records = build_monthly_actual_records_from_rows(
            rows,
            read_month_calendar(target_engine),
            start_date=start_date,
            end_date=end_date,
        )
        return upsert_monthly_actuals_detailed(target_engine, records)
    finally:
        if owns_engine:
            target_engine.dispose()
