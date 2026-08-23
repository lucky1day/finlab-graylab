from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.daily_actuals_updater import (
    active_registry_tenors_by_task_type,
    read_yield_rows,
    resolve_actual_tenors,
)
from scheduler.repository import create_engine_from_env, upsert_period_average_actuals
from shared.actual_facts import (
    build_period_average_actual_records_from_rows as build_shared_records,
    read_trade_calendar_rows,
)
from shared.models import PeriodAverageActualRecord
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


def build_period_average_actual_records(
    engine: Engine,
    *,
    task_types: Iterable[str] = PERIOD_AVERAGE_TASK_TYPES,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[PeriodAverageActualRecord]:
    """从日频收益率和交易日历构建全部周期均值 actual。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    calendar_rows = read_trade_calendar_rows(engine)
    return build_shared_records(
        rows,
        calendar_rows,
        task_types=task_types,
        start_date=start_date,
        end_date=end_date,
    )


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


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Refresh t_scheme_period_average_actuals from api_wind_daily."
    )
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--tenor", action="append")
    args = parser.parse_args()
    written = update_period_average_actuals(
        start_date=args.start_date,
        end_date=args.end_date,
        tenors=args.tenor,
    )
    print(f"period_average_actuals_written={written}")


if __name__ == "__main__":
    main()
