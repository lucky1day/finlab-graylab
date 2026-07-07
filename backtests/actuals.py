from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from shared.models import ActualRecord, MonthlyActualRecord
from shared.prediction_context import MONTHLY_TARGET_RULE
from shared.tenor_mapping import TENOR_TO_INDICATOR, indicator_map_for_tenors, normalize_tenor


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


def read_yield_rows(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> list[dict]:
    """读取实际收益率序列。"""
    selected_tenors = [normalize_tenor(tenor) for tenor in (tenors or TENOR_TO_INDICATOR.keys())]
    code_to_tenor = indicator_map_for_tenors(selected_tenors)
    if not code_to_tenor:
        return []

    params = {
        "codes": list(code_to_tenor.keys()),
        "end_date": _normalize_date(end_date),
    }
    end_filter = "AND rdate <= :end_date" if params["end_date"] else ""
    sql = text(
        f"""
        SELECT rdate, indicators_code, indicators_value
        FROM api_wind_daily
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
          {end_filter}
        ORDER BY indicators_code, rdate
        """
    ).bindparams(bindparam("codes", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [
        {
            "trade_date": str(row["rdate"]),
            "tenor": code_to_tenor[str(row["indicators_code"])],
            "close_yield": _to_float(row["indicators_value"]),
        }
        for row in rows
    ]


def build_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[ActualRecord]:
    """构建日频实际方向记录。"""
    start = _normalize_date(start_date)
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["tenor"]].append(row)

    records: list[ActualRecord] = []
    for tenor, items in grouped.items():
        for idx, item in enumerate(items):
            trade_date = item["trade_date"]
            if start and trade_date < start:
                continue
            close_yield = item["close_yield"]
            prev_1 = items[idx - 1]["close_yield"] if idx >= 1 else None
            prev_5 = items[idx - 5]["close_yield"] if idx >= 5 else None
            records.append(
                ActualRecord(
                    tenor=tenor,
                    trade_date=trade_date,
                    close_yield=close_yield,
                    direction_1d=_sign(close_yield - prev_1) if prev_1 is not None else None,
                    direction_5d=_sign(close_yield - prev_5) if prev_5 is not None else None,
                )
            )
    return records


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
