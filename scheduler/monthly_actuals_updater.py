from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from scheduler.daily_actuals_updater import active_scheme_tenors, read_yield_rows
from scheduler.repository import create_engine_from_env, upsert_monthly_actuals
from shared.models import MonthlyActualRecord
from shared.prediction_context import MONTHLY_TARGET_RULE


@dataclass(frozen=True)
class MonthCalendar:
    trading_days: tuple[str, ...]

    def is_trading_day(self, value: str | date | datetime) -> bool:
        return _normalize_date(value) in set(self.trading_days)

    def next_trading_days(self, value: str | date | datetime, count: int) -> list[str]:
        normalized = _normalize_date(value)
        if normalized is None or count <= 0:
            return []
        return [day for day in self.trading_days if day > normalized][:count]

    def previous_trading_day(self, value: str | date | datetime) -> str:
        normalized = _normalize_date(value)
        if normalized is None:
            raise ValueError("value is required")
        candidates = [day for day in self.trading_days if day < normalized]
        if not candidates:
            raise ValueError(f"no previous trading day before {normalized}")
        return candidates[-1]

    def first_trading_day_on_or_after(self, value: date) -> str:
        day = value.isoformat()
        if self.is_trading_day(day):
            return day
        candidates = self.next_trading_days((value - timedelta(days=1)).isoformat(), 20)
        for candidate in candidates:
            if candidate >= day:
                return candidate
        raise ValueError(f"no trading day on or after {day}")

    def last_trading_day_on_or_before(self, value: date) -> str:
        day = value.isoformat()
        if self.is_trading_day(day):
            return day
        return self.previous_trading_day(day)


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()


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


def _add_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, value.day)
    return date(value.year, value.month + 1, value.day)


def _month_anchor(month_id: str) -> date:
    return date.fromisoformat(f"{month_id}-15")


def _build_month_calendar(rows: Iterable[dict]) -> MonthCalendar:
    trading_days = sorted(
        day
        for raw in rows
        if str(raw.get("trade_flag", "")).strip() == "1"
        for day in [_normalize_date(raw.get("rdate"))]
        if day is not None
    )
    return MonthCalendar(trading_days=tuple(trading_days))


def read_month_calendar(engine: Engine) -> list[dict]:
    """读取交易日历。"""
    stmt = text(
        """
        SELECT rdate, trade_flag
        FROM t_trade_calendar
        ORDER BY rdate
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def build_monthly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[MonthlyActualRecord]:
    """按 feature 月 15 日与 target 月 15 日观测收益率生成月度实际方向。"""
    start = _normalize_date(start_date)
    end = _normalize_date(end_date)
    calendar = _build_month_calendar(calendar_rows)
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for raw in rows:
        trade_date = _normalize_date(raw.get("trade_date"))
        if trade_date is None:
            continue
        grouped[str(raw["tenor"])][trade_date] = _to_float(raw["close_yield"])

    records: list[MonthlyActualRecord] = []
    for tenor, by_date in grouped.items():
        feature_months = sorted({trade_date[:7] for trade_date in by_date})
        for feature_month_id in feature_months:
            feature_anchor = _month_anchor(feature_month_id)
            target_anchor = _add_month(feature_anchor)
            try:
                predict_date = feature_anchor.isoformat()
                feature_date = calendar.last_trading_day_on_or_before(feature_anchor)
                target_date = calendar.last_trading_day_on_or_before(target_anchor)
            except ValueError:
                continue
            target_month_id = target_anchor.strftime("%Y-%m")
            if feature_date[:7] != feature_month_id or target_date[:7] != target_month_id:
                continue
            if feature_date not in by_date or target_date not in by_date:
                continue
            if start and target_date < start:
                continue
            if end and target_date > end:
                continue
            feature_yield = by_date[feature_date]
            target_yield = by_date[target_date]
            direction = _sign(target_yield - feature_yield)
            records.append(
                MonthlyActualRecord(
                    tenor=tenor,
                    feature_month_id=feature_month_id,
                    target_month_id=target_month_id,
                    predict_date=predict_date,
                    feature_date=feature_date,
                    target_date=target_date,
                    feature_yield=feature_yield,
                    target_yield=target_yield,
                    direction_monthly=direction,
                    price_signal=_price_signal(direction),
                    target_rule=MONTHLY_TARGET_RULE,
                    extra={
                        "direction_basis": "yield",
                        "aggregation": "feature_month_15_observation_vs_target_month_15_observation",
                        "yield_direction_1": "price_short",
                        "yield_direction_minus_1": "price_long",
                    },
                )
            )
    return sorted(records, key=lambda record: (record.tenor, record.predict_date, record.target_date))


def build_monthly_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[MonthlyActualRecord]:
    """从日频收益率源表构建月度实际方向。"""
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    calendar_rows = read_month_calendar(engine)
    return build_monthly_actual_records_from_rows(
        rows,
        calendar_rows,
        start_date=start_date,
        end_date=end_date,
    )


def update_monthly_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """刷新 t_scheme_monthly_actuals，不修改日度/周度 actuals。"""
    engine = create_engine_from_env()
    try:
        selected_tenors = list(tenors) if tenors is not None else active_scheme_tenors(frequency="monthly")
        records = build_monthly_actual_records(engine, start_date=start_date, end_date=end_date, tenors=selected_tenors)
        return upsert_monthly_actuals(engine, records)
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh t_scheme_monthly_actuals from api_wind_daily.")
    parser.add_argument("--start-date", default=None, help="Optional target date start in YYYY-MM-DD format")
    parser.add_argument("--end-date", default=None, help="Optional target date end in YYYY-MM-DD format")
    parser.add_argument("--tenor", action="append", help="Limit to one tenor")
    args = parser.parse_args()

    written = update_monthly_actuals(start_date=args.start_date, end_date=args.end_date, tenors=args.tenor)
    print(f"monthly_actuals_written={written}")


if __name__ == "__main__":
    main()
