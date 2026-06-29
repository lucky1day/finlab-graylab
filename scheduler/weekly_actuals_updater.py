from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from scheduler.daily_actuals_updater import active_scheme_tenors, read_yield_rows
from scheduler.repository import create_engine_from_env, upsert_weekly_actuals
from shared.models import WeeklyActualRecord
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, WEEKLY_TARGET_RULE, next_calendar_week_id
from shared.tenor_mapping import TENOR_TO_INDICATOR


TARGET_RULE = WEEKLY_TARGET_RULE
TARGET_RULES = (WEEKLY_TARGET_RULE, WEEKLY_AVERAGE_TARGET_RULE)


@dataclass(frozen=True)
class WeekCalendar:
    date_to_week_id: dict[str, int]
    week_last_trading_day: dict[int, str]
    week_predict_date: dict[int, str]
    trading_days: tuple[str, ...]

    def week_id_for_date(self, value: str | date | datetime) -> int | None:
        normalized = _normalize_date(value)
        return self.date_to_week_id.get(normalized) if normalized else None

    def week_id_to_last_trading_day(self, week_id: int | float | str) -> str:
        return self.week_last_trading_day[int(week_id)]

    def next_trading_days(self, value: str | date | datetime, count: int) -> list[str]:
        normalized = _normalize_date(value)
        if normalized is None:
            return []
        return [day for day in self.trading_days if day > normalized][:count]


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


def _average_yield(items: list[dict]) -> float:
    return sum(item["close_yield"] for item in items) / len(items)


def _normalize_week_id(value: object) -> int | None:
    if value is None:
        return None
    text_value = str(value).strip().replace(".0", "")
    if not text_value:
        return None
    return int(text_value)


def _is_trading_flag(value: object) -> bool:
    return str(value).strip() == "1"


def read_week_calendar(engine: Engine) -> list[dict]:
    """读取周编号和交易日标记，周编号以 api_wind_date 为准。"""
    stmt = text(
        """
        SELECT wd.rdate, wd.week_id, tc.trade_flag
        FROM api_wind_date wd
        LEFT JOIN t_trade_calendar tc ON tc.rdate = wd.rdate
        WHERE wd.week_id IS NOT NULL
        ORDER BY wd.rdate
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def _build_week_calendar(rows: Iterable[dict]) -> WeekCalendar:
    date_to_week_id: dict[str, int] = {}
    rows_by_week: dict[int, list[dict]] = defaultdict(list)
    for raw in rows:
        rdate = _normalize_date(raw.get("rdate"))
        week_id = _normalize_week_id(raw.get("week_id"))
        if rdate is None or week_id is None:
            continue
        item = {"rdate": rdate, "is_trading": _is_trading_flag(raw.get("trade_flag"))}
        date_to_week_id[rdate] = week_id
        rows_by_week[week_id].append(item)

    week_ids = sorted(rows_by_week, key=lambda wid: min(row["rdate"] for row in rows_by_week[wid]))
    week_last_trading_day: dict[int, str] = {}
    week_predict_date: dict[int, str] = {}
    for week_id in week_ids:
        week_rows = sorted(rows_by_week[week_id], key=lambda row: row["rdate"])
        trading_days = [row["rdate"] for row in week_rows if row["is_trading"]]
        if not trading_days:
            continue
        last_trading_day = trading_days[-1]
        week_last_trading_day[week_id] = last_trading_day
        later_dates = [row["rdate"] for row in week_rows if row["rdate"] > last_trading_day]
        if later_dates:
            week_predict_date[week_id] = later_dates[0]
        else:
            week_predict_date[week_id] = (date.fromisoformat(last_trading_day) + timedelta(days=1)).isoformat()

    return WeekCalendar(
        date_to_week_id=date_to_week_id,
        week_last_trading_day=week_last_trading_day,
        week_predict_date=week_predict_date,
        trading_days=tuple(
            sorted(
                row["rdate"]
                for week_id in week_ids
                for row in rows_by_week[week_id]
                if row["is_trading"]
            )
        ),
    )


def build_weekly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[WeeklyActualRecord]:
    """按周内最后一个可用交易日生成周度实际方向。"""
    start = _normalize_date(start_date)
    end = _normalize_date(end_date)
    calendar = _build_week_calendar(calendar_rows)
    grouped: dict[str, dict[int, dict]] = defaultdict(dict)
    max_trade_date_by_tenor: dict[str, str] = {}

    for raw in rows:
        trade_date = _normalize_date(raw["trade_date"])
        if trade_date is None:
            continue
        week_id = calendar.date_to_week_id.get(trade_date)
        if week_id is None:
            raise ValueError(f"trade_date={trade_date} missing from api_wind_date")
        tenor = str(raw["tenor"])
        if tenor not in max_trade_date_by_tenor or trade_date > max_trade_date_by_tenor[tenor]:
            max_trade_date_by_tenor[tenor] = trade_date
        item = {
            "trade_date": trade_date,
            "close_yield": _to_float(raw["close_yield"]),
        }
        bucket = grouped[tenor].setdefault(week_id, {"items": [], "last": None})
        bucket["items"].append(item)
        existing = bucket["last"]
        if existing is None or trade_date > existing["trade_date"]:
            bucket["last"] = item

    records: list[WeeklyActualRecord] = []
    for tenor, by_week in grouped.items():
        for feature_week_id in sorted(by_week):
            try:
                target_week_id = next_calendar_week_id(calendar, feature_week_id)
            except ValueError:
                continue
            if target_week_id not in by_week:
                continue
            target_week_end = calendar.week_last_trading_day.get(target_week_id)
            predict_date = calendar.week_predict_date.get(feature_week_id)
            if target_week_end is None or predict_date is None:
                continue
            if max_trade_date_by_tenor[tenor] < target_week_end:
                continue
            feature_bucket = by_week[feature_week_id]
            target_bucket = by_week[target_week_id]
            feature = feature_bucket["last"]
            target = target_bucket["last"]
            if start and target["trade_date"] < start:
                continue
            if end and target["trade_date"] > end:
                continue

            for target_rule, feature_yield, target_yield, aggregation in (
                (
                    WEEKLY_TARGET_RULE,
                    feature["close_yield"],
                    target["close_yield"],
                    "last_available_trading_day",
                ),
                (
                    WEEKLY_AVERAGE_TARGET_RULE,
                    _average_yield(feature_bucket["items"]),
                    _average_yield(target_bucket["items"]),
                    "available_trading_day_average",
                ),
            ):
                direction = _sign(target_yield - feature_yield)
                records.append(
                    WeeklyActualRecord(
                        tenor=tenor,
                        feature_week_id=feature_week_id,
                        target_week_id=target_week_id,
                        predict_date=predict_date,
                        feature_date=feature["trade_date"],
                        target_date=target["trade_date"],
                        feature_yield=feature_yield,
                        target_yield=target_yield,
                        direction_weekly=direction,
                        price_signal=_price_signal(direction),
                        target_rule=target_rule,
                        extra={
                            "direction_basis": "yield",
                            "aggregation": aggregation,
                            "yield_direction_1": "price_short",
                            "yield_direction_minus_1": "price_long",
                        },
                    )
            )
    rule_order = {rule: index for index, rule in enumerate(TARGET_RULES)}
    return sorted(records, key=lambda record: (record.tenor, record.predict_date, rule_order[record.target_rule]))


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
        selected_tenors = list(tenors) if tenors is not None else active_scheme_tenors(frequency="weekly")
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
