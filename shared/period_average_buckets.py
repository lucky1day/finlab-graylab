"""MID、CQ 与春节年周期桶的唯一纯计算实现。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

from shared.calendar_service import is_trading_day_row
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


SPRING_WINDOW_END_MONTH = 3
SPRING_WINDOW_END_DAY = 15
MIN_SPRING_GAP_DAYS = 6


@dataclass(frozen=True)
class PeriodBucket:
    """一个完整业务桶及其确定交易日集合。"""

    task_type: str
    label: str
    start_date: str
    end_date: str
    anchor_date: str
    trading_days: tuple[str, ...]


@dataclass(frozen=True)
class SpringBoundary:
    """一个自然年的春节停市边界。"""

    year: int
    previous_trading_day: str
    next_trading_day: str
    gap_days: int


def build_period_buckets(
    task_type: str,
    calendar_rows: Iterable[Mapping[str, object]],
) -> list[PeriodBucket]:
    """从连续完整日历构造所有边界完整的业务桶。"""
    if task_type not in PERIOD_AVERAGE_TASK_TYPES:
        raise ValueError(f"unsupported period-average task_type: {task_type}")
    calendar = _normalize_calendar(calendar_rows)
    if task_type == "monthly_average":
        return _monthly_buckets(calendar)
    if task_type == "quarterly_average":
        return _quarterly_buckets(calendar)
    return _annual_buckets(calendar)


def bucket_for_anchor(
    task_type: str,
    anchor_date: str | date | datetime,
    calendar_rows: Iterable[Mapping[str, object]],
) -> PeriodBucket:
    """返回以指定交易日为锚点的唯一业务桶。"""
    anchor = _to_date(anchor_date).isoformat()
    matches = [
        bucket
        for bucket in build_period_buckets(task_type, calendar_rows)
        if bucket.anchor_date == anchor
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one {task_type} bucket for anchor_date={anchor}, got {len(matches)}"
        )
    return matches[0]


def period_anchor_dates(
    task_type: str,
    calendar_rows: Iterable[Mapping[str, object]],
    *,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
) -> tuple[str, ...]:
    """返回给定闭区间内的全部周期锚点。"""
    start = _to_date(start_date).isoformat() if start_date is not None else None
    end = _to_date(end_date).isoformat() if end_date is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("period-average anchor range start must not exceed end")
    return tuple(
        bucket.anchor_date
        for bucket in build_period_buckets(task_type, calendar_rows)
        if (start is None or bucket.anchor_date >= start)
        and (end is None or bucket.anchor_date <= end)
    )


def target_pointer(feature_date: str | date | datetime) -> str:
    """周期任务目标日期指针固定为 feature_date 后一个自然日。"""
    return (_to_date(feature_date) + timedelta(days=1)).isoformat()


def complete_bucket_average(
    bucket: PeriodBucket,
    rows: Iterable[Mapping[str, object]],
) -> float:
    """严格校验桶内每日值完整唯一后计算算术平均。"""
    values: dict[str, float] = {}
    expected = set(bucket.trading_days)
    for raw in rows:
        raw_date = raw.get("trade_date", raw.get("date"))
        if raw_date is None:
            raise ValueError("period-average observation is missing trade_date")
        trade_date = _to_date(raw_date).isoformat()
        if trade_date not in expected:
            raise ValueError(
                f"period-average observation date {trade_date} is outside bucket {bucket.label}"
            )
        if trade_date in values:
            raise ValueError(
                f"duplicate period-average observation: bucket={bucket.label}, date={trade_date}"
            )
        raw_value = raw.get("close_yield", raw.get("value"))
        if isinstance(raw_value, bool) or raw_value is None:
            raise ValueError(
                f"invalid period-average observation: bucket={bucket.label}, date={trade_date}"
            )
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid period-average observation: bucket={bucket.label}, date={trade_date}"
            ) from exc
        if not math.isfinite(value):
            raise ValueError(
                f"invalid period-average observation: bucket={bucket.label}, date={trade_date}"
            )
        values[trade_date] = value
    missing = sorted(expected - set(values))
    if missing:
        raise ValueError(
            f"period-average bucket {bucket.label} is missing trading dates: {missing}"
        )
    if not values:
        raise ValueError(f"period-average bucket {bucket.label} has no trading observations")
    return sum(values[day] for day in bucket.trading_days) / len(bucket.trading_days)


def detect_spring_boundary(
    year: int,
    calendar_rows: Iterable[Mapping[str, object]],
) -> SpringBoundary:
    """按 1/1~3/15 唯一最长相邻交易日日期差识别春节。"""
    calendar = _normalize_calendar(calendar_rows)
    return _spring_boundary(int(year), calendar)


@dataclass(frozen=True)
class _Calendar:
    first_date: date
    last_date: date
    trading_days: tuple[date, ...]


def _normalize_calendar(
    rows: Iterable[Mapping[str, object]],
) -> _Calendar:
    by_date: dict[date, object] = {}
    for raw in rows:
        if "rdate" not in raw:
            raise ValueError("trade calendar row is missing rdate")
        day = _to_date(raw["rdate"])
        if day in by_date:
            raise ValueError(f"duplicate trade calendar date: {day.isoformat()}")
        by_date[day] = raw.get("trade_flag")
    if not by_date:
        raise ValueError("trade calendar is empty")
    ordered = sorted(by_date)
    expected_count = (ordered[-1] - ordered[0]).days + 1
    if len(ordered) != expected_count:
        present = set(ordered)
        missing = [
            (ordered[0] + timedelta(days=offset)).isoformat()
            for offset in range(expected_count)
            if ordered[0] + timedelta(days=offset) not in present
        ]
        raise ValueError(f"trade calendar coverage is not contiguous: missing={missing}")
    trading_days = tuple(
        day
        for day in ordered
        if is_trading_day_row(day, by_date[day])
    )
    if not trading_days:
        raise ValueError("trade calendar has no trading days")
    return _Calendar(ordered[0], ordered[-1], trading_days)


def _monthly_buckets(calendar: _Calendar) -> list[PeriodBucket]:
    result: list[PeriodBucket] = []
    label_month = date(calendar.first_date.year, calendar.first_date.month, 1)
    final_month = date(calendar.last_date.year, calendar.last_date.month, 1)
    while label_month <= final_month:
        previous_month = _add_month(label_month, -1)
        start = date(previous_month.year, previous_month.month, 16)
        end = date(label_month.year, label_month.month, 15)
        if start >= calendar.first_date and end <= calendar.last_date:
            result.append(
                _make_bucket(
                    "monthly_average",
                    f"MID-{label_month:%Y-%m}",
                    start,
                    end,
                    calendar,
                )
            )
        label_month = _add_month(label_month, 1)
    return result


def _quarterly_buckets(calendar: _Calendar) -> list[PeriodBucket]:
    result: list[PeriodBucket] = []
    for year in range(calendar.first_date.year, calendar.last_date.year + 1):
        for quarter in range(1, 5):
            start_month = (quarter - 1) * 3 + 1
            start = date(year, start_month, 1)
            end = _add_month(start, 3) - timedelta(days=1)
            if start < calendar.first_date or end > calendar.last_date:
                continue
            result.append(
                _make_bucket(
                    "quarterly_average",
                    f"CQ-{year}-Q{quarter}",
                    start,
                    end,
                    calendar,
                )
            )
    return result


def _annual_buckets(calendar: _Calendar) -> list[PeriodBucket]:
    boundaries: dict[int, SpringBoundary] = {}
    for year in range(calendar.first_date.year, calendar.last_date.year + 1):
        window_start = date(year, 1, 1)
        window_end = date(year, SPRING_WINDOW_END_MONTH, SPRING_WINDOW_END_DAY)
        if window_start < calendar.first_date or window_end > calendar.last_date:
            continue
        boundaries[year] = _spring_boundary(year, calendar)
    result: list[PeriodBucket] = []
    for year in sorted(boundaries):
        next_boundary = boundaries.get(year + 1)
        if next_boundary is None:
            continue
        start = date.fromisoformat(boundaries[year].next_trading_day)
        end = date.fromisoformat(next_boundary.previous_trading_day)
        result.append(
            _make_bucket(
                "annual_average",
                f"SF-{year}",
                start,
                end,
                calendar,
            )
        )
    return result


def _spring_boundary(year: int, calendar: _Calendar) -> SpringBoundary:
    start = date(year, 1, 1)
    end = date(year, SPRING_WINDOW_END_MONTH, SPRING_WINDOW_END_DAY)
    if start < calendar.first_date or end > calendar.last_date:
        raise ValueError(f"spring calendar window is not covered for year={year}")
    days = [day for day in calendar.trading_days if start <= day <= end]
    if len(days) < 2:
        raise ValueError(f"spring calendar window has fewer than two trading days for year={year}")
    gaps = [(days[index] - days[index - 1]).days for index in range(1, len(days))]
    maximum = max(gaps)
    if maximum < MIN_SPRING_GAP_DAYS:
        raise ValueError(
            f"spring closure gap is shorter than {MIN_SPRING_GAP_DAYS} days for year={year}"
        )
    positions = [index for index, value in enumerate(gaps, start=1) if value == maximum]
    if len(positions) != 1:
        raise ValueError(f"spring closure gap is not unique for year={year}")
    index = positions[0]
    return SpringBoundary(
        year=year,
        previous_trading_day=days[index - 1].isoformat(),
        next_trading_day=days[index].isoformat(),
        gap_days=maximum,
    )


def _make_bucket(
    task_type: str,
    label: str,
    start: date,
    end: date,
    calendar: _Calendar,
) -> PeriodBucket:
    trading_days = tuple(
        day.isoformat() for day in calendar.trading_days if start <= day <= end
    )
    if not trading_days:
        raise ValueError(f"period-average bucket {label} has no trading days")
    return PeriodBucket(
        task_type=task_type,
        label=label,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        anchor_date=trading_days[-1],
        trading_days=trading_days,
    )


def _add_month(value: date, count: int) -> date:
    month_index = value.year * 12 + value.month - 1 + count
    return date(month_index // 12, month_index % 12 + 1, value.day)


def _to_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid date: {value!r}") from exc
