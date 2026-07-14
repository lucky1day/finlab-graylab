from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


def align_weekly_previous_complete(
    weekly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
    date_to_week: Mapping[str, int | str],
) -> pd.DataFrame:
    """把周频输入对齐到每个日频样本的上一完整周。"""
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if "week_id" not in weekly_df.columns:
        return pd.DataFrame(index=range(len(daily_index)))

    weekly = weekly_df.copy()
    weekly["week_id"] = pd.to_numeric(weekly["week_id"], errors="coerce")
    weekly = weekly.dropna(subset=["week_id"]).copy()
    weekly["week_id"] = weekly["week_id"].astype(int)
    weekly = weekly.sort_values("week_id").drop_duplicates("week_id", keep="last")
    value_cols = [col for col in weekly.columns if col != "week_id"]
    if not value_cols:
        return pd.DataFrame(index=range(len(daily_index)))

    week_ids = weekly["week_id"].tolist()
    weekly_by_id = weekly.set_index("week_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_index), np.nan, dtype=np.float64) for col in value_cols
    }
    for row_index, daily_date in enumerate(daily_index):
        current_week = _week_id_for_date(daily_date, date_to_week)
        if current_week is None:
            continue
        previous_candidates = [week_id for week_id in week_ids if week_id < current_week]
        if not previous_candidates:
            continue
        previous_week = previous_candidates[-1]
        for col in value_cols:
            result[col][row_index] = weekly_by_id.at[previous_week, col]
    return pd.DataFrame(result, index=range(len(daily_index)))


def align_monthly_previous_month(
    monthly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
) -> pd.DataFrame:
    """把月频输入对齐到每个日频样本的上一自然月，month_id 严格使用 YYYYMM。"""
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if "month_id" not in monthly_df.columns:
        return pd.DataFrame(index=range(len(daily_index)))

    monthly = monthly_df.copy()
    monthly["month_id"] = monthly["month_id"].astype(str).str.strip()
    monthly = monthly[monthly["month_id"].str.fullmatch(r"\d{6}", na=False)]
    monthly = monthly.sort_values("month_id").drop_duplicates("month_id", keep="last")
    value_cols = [col for col in monthly.columns if col != "month_id"]
    if not value_cols:
        return pd.DataFrame(index=range(len(daily_index)))

    monthly_by_id = monthly.set_index("month_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_index), np.nan, dtype=np.float64) for col in value_cols
    }
    previous_month_ids = [_previous_month_id(daily_date) for daily_date in daily_index]
    for row_index, month_id in enumerate(previous_month_ids):
        if month_id not in monthly_by_id.index:
            continue
        for col in value_cols:
            result[col][row_index] = monthly_by_id.at[month_id, col]
    return pd.DataFrame(result, index=range(len(daily_index)))


def _week_id_for_date(
    daily_date: pd.Timestamp,
    date_to_week: Mapping[str, int | str],
) -> int | None:
    value = date_to_week.get(daily_date.strftime("%Y-%m-%d"))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _previous_month_id(daily_date: pd.Timestamp) -> str:
    year = int(daily_date.year)
    month = int(daily_date.month) - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}{month:02d}"
