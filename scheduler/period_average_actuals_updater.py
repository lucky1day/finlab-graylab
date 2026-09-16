from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.actuals_errors import require_actual_source_tenors
from scheduler.daily_actuals_updater import (
    active_registry_tenors_by_task_type,
    resolve_actual_tenors,
)
from scheduler.repository import (
    ActualWriteStats,
    create_engine_from_env,
    upsert_period_average_actuals_detailed,
)
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
    *,
    engine: Engine | None = None,
) -> int:
    """兼容入口：刷新周期均值 Actual，并返回本批处理条数。"""
    return update_period_average_actuals_detailed(
        start_date=start_date,
        end_date=end_date,
        tenors=tenors,
        engine=engine,
    ).attempted


def update_period_average_actuals_detailed(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
    *,
    engine: Engine | None = None,
) -> ActualWriteStats:
    """刷新周期均值 Actual，并返回真实写入统计。"""
    owns_engine = engine is None
    target_engine = engine if engine is not None else create_engine_from_env()
    try:
        if tenors is not None:
            selected_tenors = resolve_actual_tenors(
                target_engine,
                frequency="period_average",
                tenors=tenors,
            )
            scope = {
                task_type: selected_tenors
                for task_type in PERIOD_AVERAGE_TASK_TYPES
            }
        else:
            scope = active_registry_tenors_by_task_type(
                target_engine,
                PERIOD_AVERAGE_TASK_TYPES,
            )
        if not scope:
            return ActualWriteStats()
        selected_tenors = sorted(
            {tenor for task_tenors in scope.values() for tenor in task_tenors}
        )
        rows = require_actual_source_tenors(
            read_yield_rows(
                target_engine,
                tenors=selected_tenors,
                end_date=end_date,
            ),
            expected_tenors=selected_tenors,
            stage="period_average",
        )
        calendar_rows = read_trade_calendar_rows(target_engine)
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
        return upsert_period_average_actuals_detailed(target_engine, records)
    finally:
        if owns_engine:
            target_engine.dispose()
