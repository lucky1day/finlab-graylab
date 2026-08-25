from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from scheduler.daily_actuals_updater import (
    active_registry_tenors_by_task_type,
    resolve_actual_tenors,
)
from scheduler.repository import create_engine_from_env, upsert_period_average_actuals
from shared.actual_facts import (
    build_period_average_actual_records_from_rows as build_shared_records,
    read_trade_calendar_rows,
    read_yield_rows,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


def update_period_average_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """刷新单一周期均值 actual 表，不修改既有三类 actual。"""
    engine = create_engine_from_env()
    try:
        if tenors is not None:
            selected_tenors = resolve_actual_tenors(
                engine,
                frequency="period_average",
                tenors=tenors,
            )
            scope = {
                task_type: selected_tenors
                for task_type in PERIOD_AVERAGE_TASK_TYPES
            }
        else:
            scope = active_registry_tenors_by_task_type(
                engine,
                PERIOD_AVERAGE_TASK_TYPES,
            )
        if not scope:
            return 0
        selected_tenors = sorted(
            {tenor for task_tenors in scope.values() for tenor in task_tenors}
        )
        rows = read_yield_rows(engine, tenors=selected_tenors, end_date=end_date)
        calendar_rows = read_trade_calendar_rows(engine)
        records = []
        for task_type, task_tenors in scope.items():
            records.extend(
                build_shared_records(
                    [row for row in rows if row["tenor"] in task_tenors],
                    calendar_rows,
                    task_types=(task_type,),
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        return upsert_period_average_actuals(engine, records)
    finally:
        engine.dispose()
