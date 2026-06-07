from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd


DateLike = str | date | datetime | pd.Timestamp


def _to_date(value: DateLike | None = None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def first_monday_of_year(year: int) -> date:
    first_day = date(year, 1, 1)
    return first_day + timedelta(days=(7 - first_day.weekday()) % 7)


def get_week_id_for_date(target_date: DateLike | None = None) -> int:
    """按实盘周频口径计算 week_id：每年第一个周一作为第 1 周。"""
    current = _to_date(target_date)
    monday_of_week = current - timedelta(days=current.weekday())
    first_monday = first_monday_of_year(current.year)
    if monday_of_week < first_monday:
        return get_week_id_for_date(date(current.year - 1, 12, 31))
    week_number = ((monday_of_week - first_monday).days // 7) + 1
    return int(f"{current.year:04d}{week_number:02d}")


def week_id_to_monday(week_id: int | float | str) -> pd.Timestamp:
    text = str(int(week_id))
    year = int(text[:4])
    week = int(text[4:])
    return pd.Timestamp(first_monday_of_year(year) + timedelta(weeks=week - 1))


def week_id_to_friday(week_id: int | float | str) -> pd.Timestamp:
    return week_id_to_monday(week_id) + pd.Timedelta(days=4)


def week_id_to_saturday(week_id: int | float | str) -> pd.Timestamp:
    return week_id_to_monday(week_id) + pd.Timedelta(days=5)


def next_week_id(week_id: int | float | str) -> int:
    return get_week_id_for_date(week_id_to_monday(week_id).date() + timedelta(days=7))
