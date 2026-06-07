from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.engine import Engine

from scheduler.actuals_updater import TENOR_TO_INDICATOR, read_yield_rows
from scheduler.repository import create_engine_from_env, upsert_weekly_actuals
from shared.models import WeeklyActualRecord


TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _to_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _price_signal(direction: int) -> str:
    if direction > 0:
        return "空"
    if direction < 0:
        return "多"
    return "平"


def _first_monday_of_year(year: int) -> date:
    first_day = date(year, 1, 1)
    return first_day + timedelta(days=(7 - first_day.weekday()) % 7)


def _week_id_for_date(value: str) -> int:
    current = datetime.strptime(value, "%Y-%m-%d").date()
    monday_of_week = current - timedelta(days=current.weekday())
    first_monday = _first_monday_of_year(current.year)
    if monday_of_week < first_monday:
        return _week_id_for_date(date(current.year - 1, 12, 31).isoformat())
    week_number = ((monday_of_week - first_monday).days // 7) + 1
    return int(f"{current.year:04d}{week_number:02d}")


def _week_id_to_monday(week_id: int) -> date:
    text = str(int(week_id))
    year = int(text[:4])
    week = int(text[4:])
    return _first_monday_of_year(year) + timedelta(weeks=week - 1)


def _week_id_to_friday(week_id: int) -> date:
    return _week_id_to_monday(week_id) + timedelta(days=4)


def _next_week_id(week_id: int) -> int:
    return _week_id_for_date((_week_id_to_monday(week_id) + timedelta(days=7)).isoformat())


def _predict_date_for_week(feature_week_id: int) -> str:
    return (_week_id_to_friday(feature_week_id) + timedelta(days=1)).isoformat()


def build_weekly_actual_records_from_rows(
    rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[WeeklyActualRecord]:
    """按周内最后一个可用交易日生成周度实际方向。"""
    start = _normalize_date(start_date)
    end = _normalize_date(end_date)
    grouped: dict[str, dict[int, dict]] = defaultdict(dict)
    max_trade_date_by_tenor: dict[str, str] = {}

    for raw in rows:
        trade_date = _normalize_date(raw["trade_date"])
        if trade_date is None:
            continue
        week_id = _week_id_for_date(trade_date)
        tenor = str(raw["tenor"])
        if tenor not in max_trade_date_by_tenor or trade_date > max_trade_date_by_tenor[tenor]:
            max_trade_date_by_tenor[tenor] = trade_date
        item = {
            "trade_date": trade_date,
            "close_yield": _to_float(raw["close_yield"]),
        }
        existing = grouped[tenor].get(week_id)
        if existing is None or trade_date > existing["trade_date"]:
            grouped[tenor][week_id] = item

    records: list[WeeklyActualRecord] = []
    for tenor, by_week in grouped.items():
        for feature_week_id in sorted(by_week):
            target_week_id = _next_week_id(feature_week_id)
            if target_week_id not in by_week:
                continue
            target_week_end = _week_id_to_friday(target_week_id).isoformat()
            if max_trade_date_by_tenor[tenor] < target_week_end:
                continue
            feature = by_week[feature_week_id]
            target = by_week[target_week_id]
            if start and target["trade_date"] < start:
                continue
            if end and target["trade_date"] > end:
                continue
            direction = _sign(target["close_yield"] - feature["close_yield"])
            records.append(
                WeeklyActualRecord(
                    tenor=tenor,
                    feature_week_id=feature_week_id,
                    target_week_id=target_week_id,
                    predict_date=_predict_date_for_week(feature_week_id),
                    feature_date=feature["trade_date"],
                    target_date=target["trade_date"],
                    feature_yield=feature["close_yield"],
                    target_yield=target["close_yield"],
                    direction_weekly=direction,
                    price_signal=_price_signal(direction),
                    target_rule=TARGET_RULE,
                    extra={
                        "direction_basis": "yield",
                        "yield_direction_1": "price_short",
                        "yield_direction_minus_1": "price_long",
                    },
                )
            )
    return sorted(records, key=lambda record: (record.tenor, record.predict_date))


def build_weekly_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[WeeklyActualRecord]:
    """从日频收益率源表构建周度实际方向。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    return build_weekly_actual_records_from_rows(rows, start_date=start_date, end_date=end_date)


def update_weekly_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """刷新 t_scheme_weekly_actuals，不修改日度 actuals。"""
    engine = create_engine_from_env()
    try:
        records = build_weekly_actual_records(engine, start_date=start_date, end_date=end_date, tenors=tenors)
        return upsert_weekly_actuals(engine, records)
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh t_scheme_weekly_actuals from api_wind_daily.")
    parser.add_argument("--start-date", default=None, help="Optional target date start in YYYY-MM-DD format")
    parser.add_argument("--end-date", default=None, help="Optional target date end in YYYY-MM-DD format")
    parser.add_argument("--tenor", action="append", choices=sorted(TENOR_TO_INDICATOR), help="Limit to one tenor")
    args = parser.parse_args()

    written = update_weekly_actuals(start_date=args.start_date, end_date=args.end_date, tenors=args.tenor)
    print(f"weekly_actuals_written={written}")


if __name__ == "__main__":
    main()
