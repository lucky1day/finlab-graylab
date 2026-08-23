from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from shared.calendar_service import is_trading_day_row
from shared.models import (
    ActualRecord,
    MonthlyActualRecord,
    PeriodAverageActualRecord,
    WeeklyActualRecord,
)
from shared.period_average_buckets import (
    PeriodBucket,
    build_period_buckets,
    complete_bucket_average,
    target_pointer,
)
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
    next_calendar_week_id,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES, TASK_COMBINATIONS
from shared.tenor_mapping import TENOR_TO_INDICATOR, indicator_map_for_tenors, normalize_tenor
from shared.week_calendar_normalizer import normalize_week_calendar_rows


def normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()


def read_yield_rows(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> list[dict]:
    """读取平台 actual 构造器共用的日频收益率事实。"""
    selected = [normalize_tenor(item) for item in (tenors or TENOR_TO_INDICATOR.keys())]
    code_to_tenor = indicator_map_for_tenors(selected)
    if not code_to_tenor:
        return []
    params = {"codes": list(code_to_tenor), "end_date": normalize_date(end_date)}
    end_filter = "AND rdate <= :end_date" if params["end_date"] else ""
    statement = text(
        f"""
        SELECT rdate, indicators_code, indicators_value
        FROM api_wind_daily
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
          {end_filter}
        ORDER BY indicators_code, rdate
        """
    ).bindparams(bindparam("codes", expanding=True))
    with engine.connect() as connection:
        rows = connection.execute(statement, params).mappings().all()
    return [
        {
            "trade_date": str(row["rdate"])[:10],
            "tenor": code_to_tenor[str(row["indicators_code"])],
            "close_yield": _to_float(row["indicators_value"]),
        }
        for row in rows
    ]


def read_trade_calendar_rows(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT rdate, trade_flag FROM t_trade_calendar ORDER BY rdate")
            ).mappings().all()
        ]


def read_week_calendar_rows(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT wd.rdate, wd.week_id, tc.trade_flag
                    FROM api_wind_date wd
                    LEFT JOIN t_trade_calendar tc ON tc.rdate = wd.rdate
                    WHERE wd.week_id IS NOT NULL
                    ORDER BY wd.rdate
                    """
                )
            ).mappings().all()
        ]


def build_daily_actual_records_from_rows(
    rows: Iterable[dict],
    *,
    start_date: str | date | datetime | None = None,
    strict_duplicates: bool = False,
) -> list[ActualRecord]:
    start = normalize_date(start_date)
    grouped: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        tenor = str(raw["tenor"])
        trade_date = normalize_date(raw.get("trade_date"))
        if trade_date is None:
            continue
        key = (tenor, trade_date)
        if strict_duplicates and key in seen:
            raise ValueError(f"duplicate source actual fact: tenor={tenor}, trade_date={trade_date}")
        seen.add(key)
        grouped[tenor].append(
            {"trade_date": trade_date, "close_yield": _to_float(raw["close_yield"])}
        )
    records: list[ActualRecord] = []
    for tenor, items in grouped.items():
        items.sort(key=lambda item: item["trade_date"])
        for index, item in enumerate(items):
            trade_date = item["trade_date"]
            if start and trade_date < start:
                continue
            previous_1 = items[index - 1]["close_yield"] if index >= 1 else None
            previous_5 = items[index - 5]["close_yield"] if index >= 5 else None
            records.append(
                ActualRecord(
                    tenor=tenor,
                    trade_date=trade_date,
                    close_yield=item["close_yield"],
                    direction_1d=_sign(item["close_yield"] - previous_1) if previous_1 is not None else None,
                    direction_5d=_sign(item["close_yield"] - previous_5) if previous_5 is not None else None,
                )
            )
    return records


@dataclass(frozen=True)
class WeekCalendar:
    date_to_week_id: dict[str, int]
    week_last_trading_day: dict[int, str]
    week_predict_date: dict[int, str]
    trading_days: tuple[str, ...]

    def week_id_for_date(self, value: str | date | datetime) -> int | None:
        normalized = normalize_date(value)
        return self.date_to_week_id.get(normalized) if normalized else None

    def week_id_to_last_trading_day(self, week_id: int | float | str) -> str:
        return self.week_last_trading_day[int(week_id)]

    def next_trading_days(self, value: str | date | datetime, count: int) -> list[str]:
        normalized = normalize_date(value)
        return [] if normalized is None else [day for day in self.trading_days if day > normalized][:count]


def build_week_calendar(rows: Iterable[dict]) -> WeekCalendar:
    date_to_week_id: dict[str, int] = {}
    rows_by_week: dict[int, list[dict]] = defaultdict(list)
    for raw in normalize_week_calendar_rows(rows):
        rdate = normalize_date(raw.get("rdate"))
        week_id = _week_id(raw.get("week_id"))
        if rdate is None or week_id is None:
            continue
        item = {
            "rdate": rdate,
            "is_trading": is_trading_day_row(rdate, raw.get("trade_flag")),
            "is_workday": str(raw.get("trade_flag")).strip() == "1",
        }
        date_to_week_id[rdate] = week_id
        rows_by_week[week_id].append(item)
    week_ids = sorted(rows_by_week, key=lambda value: min(row["rdate"] for row in rows_by_week[value]))
    last_days: dict[int, str] = {}
    predict_dates: dict[int, str] = {}
    for week_id in week_ids:
        week_rows = sorted(rows_by_week[week_id], key=lambda row: row["rdate"])
        trading = [row["rdate"] for row in week_rows if row["is_trading"]]
        if not trading:
            continue
        last_days[week_id] = trading[-1]
        # predict_date 是审计字段「信号发出日」，跟随调度用的工作日历，而不是
        # 交易日历；它同时是 t_scheme_weekly_actuals 唯一键
        # uk_weekly_actual_predict_rule(tenor, predict_date, target_rule) 的成分，
        # 若随交易日定义漂移，全量重算会插入新行而旧行不删，产生重复事实。
        workdays = [row["rdate"] for row in week_rows if row["is_workday"]]
        issue_anchor = workdays[-1] if workdays else trading[-1]
        later = [row["rdate"] for row in week_rows if row["rdate"] > issue_anchor]
        predict_dates[week_id] = (
            later[0]
            if later
            else (date.fromisoformat(issue_anchor) + timedelta(days=1)).isoformat()
        )
    return WeekCalendar(
        date_to_week_id=date_to_week_id,
        week_last_trading_day=last_days,
        week_predict_date=predict_dates,
        trading_days=tuple(
            sorted(row["rdate"] for week_id in week_ids for row in rows_by_week[week_id] if row["is_trading"])
        ),
    )


def build_weekly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    *,
    strict_duplicates: bool = False,
) -> list[WeeklyActualRecord]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    calendar = build_week_calendar(calendar_rows)
    grouped: dict[str, dict[int, dict]] = defaultdict(dict)
    max_date: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        trade_date = normalize_date(raw.get("trade_date"))
        if trade_date is None:
            continue
        tenor = str(raw["tenor"])
        key = (tenor, trade_date)
        if strict_duplicates and key in seen:
            raise ValueError(f"duplicate source actual fact: tenor={tenor}, trade_date={trade_date}")
        seen.add(key)
        week_id = calendar.date_to_week_id.get(trade_date)
        if week_id is None:
            raise ValueError(f"trade_date={trade_date} missing from api_wind_date")
        max_date[tenor] = max(trade_date, max_date.get(tenor, trade_date))
        item = {"trade_date": trade_date, "close_yield": _to_float(raw["close_yield"])}
        bucket = grouped[tenor].setdefault(week_id, {"items": [], "last": None})
        bucket["items"].append(item)
        if bucket["last"] is None or trade_date > bucket["last"]["trade_date"]:
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
            if target_week_end is None or predict_date is None or max_date[tenor] < target_week_end:
                continue
            feature_bucket = by_week[feature_week_id]
            target_bucket = by_week[target_week_id]
            feature = feature_bucket["last"]
            target = target_bucket["last"]
            if start and target["trade_date"] < start:
                continue
            if end and target["trade_date"] > end:
                continue
            pairs = (
                (WEEKLY_TARGET_RULE, feature["close_yield"], target["close_yield"], "last_available_trading_day"),
                (
                    WEEKLY_AVERAGE_TARGET_RULE,
                    _average(feature_bucket["items"]),
                    _average(target_bucket["items"]),
                    "available_trading_day_average",
                ),
            )
            for rule, feature_yield, target_yield, aggregation in pairs:
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
                        target_rule=rule,
                        extra={
                            "direction_basis": "yield",
                            "aggregation": aggregation,
                            "yield_direction_1": "price_short",
                            "yield_direction_minus_1": "price_long",
                        },
                    )
                )
    order = {WEEKLY_TARGET_RULE: 0, WEEKLY_AVERAGE_TARGET_RULE: 1}
    return sorted(records, key=lambda item: (item.tenor, item.predict_date, order[item.target_rule]))


@dataclass(frozen=True)
class MonthCalendar:
    trading_days: tuple[str, ...]

    def is_trading_day(self, value: str | date | datetime) -> bool:
        return normalize_date(value) in set(self.trading_days)

    def previous_trading_day(self, value: str | date | datetime) -> str:
        normalized = normalize_date(value)
        candidates = [day for day in self.trading_days if normalized is not None and day < normalized]
        if not candidates:
            raise ValueError(f"no previous trading day before {normalized}")
        return candidates[-1]

    def last_trading_day_on_or_before(self, value: date) -> str:
        return value.isoformat() if self.is_trading_day(value) else self.previous_trading_day(value)


def build_month_calendar(rows: Iterable[dict]) -> MonthCalendar:
    trading_days = sorted(
        value
        for raw in rows
        for value in [normalize_date(raw.get("rdate"))]
        if value is not None and is_trading_day_row(value, raw.get("trade_flag"))
    )
    return MonthCalendar(tuple(trading_days))


def build_monthly_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    *,
    strict_duplicates: bool = False,
) -> list[MonthlyActualRecord]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    calendar = build_month_calendar(calendar_rows)
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for raw in rows:
        trade_date = normalize_date(raw.get("trade_date"))
        if trade_date is None:
            continue
        tenor = str(raw["tenor"])
        if strict_duplicates and trade_date in grouped[tenor]:
            raise ValueError(f"duplicate source actual fact: tenor={tenor}, trade_date={trade_date}")
        grouped[tenor][trade_date] = _to_float(raw["close_yield"])
    records: list[MonthlyActualRecord] = []
    for tenor, by_date in grouped.items():
        for feature_month_id in sorted({trade_date[:7] for trade_date in by_date}):
            feature_anchor = date.fromisoformat(f"{feature_month_id}-15")
            target_anchor = _add_month(feature_anchor)
            try:
                feature_date = calendar.last_trading_day_on_or_before(feature_anchor)
                target_date = calendar.last_trading_day_on_or_before(target_anchor)
            except ValueError:
                continue
            target_month_id = target_anchor.strftime("%Y-%m")
            if feature_date[:7] != feature_month_id or target_date[:7] != target_month_id:
                continue
            if feature_date not in by_date or target_date not in by_date:
                continue
            if start and target_date < start or end and target_date > end:
                continue
            feature_yield = by_date[feature_date]
            target_yield = by_date[target_date]
            direction = _sign(target_yield - feature_yield)
            records.append(
                MonthlyActualRecord(
                    tenor=tenor,
                    feature_month_id=feature_month_id,
                    target_month_id=target_month_id,
                    predict_date=feature_anchor.isoformat(),
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
    return sorted(records, key=lambda item: (item.tenor, item.predict_date, item.target_date))


def build_period_average_actual_records_from_rows(
    rows: Iterable[dict],
    calendar_rows: Iterable[dict],
    *,
    task_types: Iterable[str] = PERIOD_AVERAGE_TASK_TYPES,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> list[PeriodAverageActualRecord]:
    """按完整连续的 MID/CQ/SF 桶生成周期均值 actual。"""
    selected_task_types = tuple(dict.fromkeys(str(item) for item in task_types))
    unsupported = sorted(set(selected_task_types) - PERIOD_AVERAGE_TASK_TYPES)
    if unsupported:
        raise ValueError(
            "unsupported period-average actual task_types: " + ", ".join(unsupported)
        )
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    frozen_calendar = tuple(dict(item) for item in calendar_rows)
    buckets_by_task = {
        task_type: build_period_buckets(task_type, frozen_calendar)
        for task_type in selected_task_types
    }
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for raw in rows:
        trade_date = normalize_date(raw.get("trade_date"))
        if trade_date is None:
            raise ValueError("period-average source actual fact is missing trade_date")
        tenor = str(raw["tenor"])
        if trade_date in grouped[tenor]:
            raise ValueError(
                f"duplicate source actual fact: tenor={tenor}, trade_date={trade_date}"
            )
        grouped[tenor][trade_date] = _to_float(raw["close_yield"])

    records: list[PeriodAverageActualRecord] = []
    for tenor, by_date in grouped.items():
        if not by_date:
            continue
        source_start, source_end = min(by_date), max(by_date)
        for task_type in selected_task_types:
            target_rule = TASK_COMBINATIONS[task_type][1]
            buckets = buckets_by_task[task_type]
            for feature_bucket, target_bucket in zip(buckets, buckets[1:]):
                if feature_bucket.start_date < source_start:
                    continue
                if target_bucket.anchor_date > source_end:
                    continue
                prediction_target_date = target_pointer(feature_bucket.anchor_date)
                if start and prediction_target_date < start:
                    continue
                if end and target_bucket.anchor_date > end:
                    continue
                feature_yield = complete_bucket_average(
                    feature_bucket,
                    _bucket_rows(feature_bucket, by_date),
                )
                target_yield = complete_bucket_average(
                    target_bucket,
                    _bucket_rows(target_bucket, by_date),
                )
                direction = _sign(target_yield - feature_yield)
                records.append(
                    PeriodAverageActualRecord(
                        tenor=tenor,
                        predict_date=feature_bucket.anchor_date,
                        feature_date=feature_bucket.anchor_date,
                        target_date=prediction_target_date,
                        feature_yield=feature_yield,
                        target_yield=target_yield,
                        actual_direction=direction,
                        price_signal=_price_signal(direction),
                        target_rule=target_rule,
                        extra={
                            "direction_basis": "yield",
                            "aggregation": "complete_trading_day_bucket_average",
                            "feature_bucket": _period_bucket_evidence(feature_bucket),
                            "target_bucket": _period_bucket_evidence(target_bucket),
                            "yield_direction_1": "price_short",
                            "yield_direction_minus_1": "price_long",
                        },
                    )
                )
    return sorted(
        records,
        key=lambda item: (item.tenor, item.predict_date, item.target_rule),
    )


def _bucket_rows(
    bucket: PeriodBucket,
    values: dict[str, float],
) -> list[dict[str, object]]:
    return [
        {
            "trade_date": trade_date,
            "close_yield": values[trade_date],
        }
        for trade_date in bucket.trading_days
        if trade_date in values
    ]


def _period_bucket_evidence(bucket: PeriodBucket) -> dict[str, object]:
    return {
        "label": bucket.label,
        "start_date": bucket.start_date,
        "end_date": bucket.end_date,
        "anchor_date": bucket.anchor_date,
        "sample_count": len(bucket.trading_days),
    }


def _week_id(value: object) -> int | None:
    if value is None:
        return None
    normalized = str(value).strip().removesuffix(".0")
    return int(normalized) if normalized else None


def _to_float(value: object) -> float:
    return float(value) if not isinstance(value, Decimal) else float(value)


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _average(items: list[dict]) -> float:
    return sum(item["close_yield"] for item in items) / len(items)


def _price_signal(direction: int) -> str:
    return "空" if direction > 0 else "多" if direction < 0 else "平"


def _add_month(value: date) -> date:
    return date(value.year + 1, 1, value.day) if value.month == 12 else date(value.year, value.month + 1, value.day)
