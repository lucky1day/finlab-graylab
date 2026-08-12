from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.daily_actuals_updater import read_yield_rows, resolve_actual_tenors
from scheduler.repository import create_engine_from_env, upsert_weekly_actuals
from shared.actual_facts import (
    build_weekly_actual_records_from_rows as build_shared_weekly_actual_records_from_rows,
    read_week_calendar_rows,
)
from shared.tenor_mapping import TENOR_TO_INDICATOR
from shared.models import WeeklyActualRecord


def read_week_calendar(engine: Engine) -> list[dict]:
    """读取周编号和交易日标记，周编号以 api_wind_date 为准。"""
    return read_week_calendar_rows(engine)

def build_weekly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[WeeklyActualRecord]:
    """按周内最后一个可用交易日生成周度实际方向。"""
    return build_shared_weekly_actual_records_from_rows(
        rows,
        calendar_rows,
        start_date=start_date,
        end_date=end_date,
    )


def build_weekly_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[WeeklyActualRecord]:
    """从日频收益率源表构建周度实际方向。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    calendar_rows = read_week_calendar(engine)
    return build_weekly_actual_records_from_rows(
        rows,
        calendar_rows,
        start_date=start_date,
        end_date=end_date,
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
        records = build_weekly_actual_records(engine, start_date=start_date, end_date=end_date, tenors=selected_tenors)
        return upsert_weekly_actuals(engine, records)
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh t_scheme_weekly_actuals from api_wind_daily.")
    parser.add_argument("--start-date", default=None, help="Optional target date start in YYYY-MM-DD format")
    parser.add_argument("--end-date", default=None, help="Optional target date end in YYYY-MM-DD format")
    parser.add_argument("--tenor", action="append", help="Limit to one tenor")
    args = parser.parse_args()

    written = update_weekly_actuals(start_date=args.start_date, end_date=args.end_date, tenors=args.tenor)
    print(f"weekly_actuals_written={written}")


if __name__ == "__main__":
    main()
