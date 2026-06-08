from __future__ import annotations

import pandas as pd


def legacy_week_id_to_monday(week_id: int | float | str) -> pd.Timestamp:
    """按 0529 周频原始脚本口径把 week_id 转换为周一日期。"""
    week_text = str(int(week_id))
    if len(week_text) < 6:
        return pd.NaT
    if len(week_text) == 8:
        parsed = pd.to_datetime(week_text, format="%Y%m%d", errors="coerce")
        if pd.notna(parsed):
            return pd.Timestamp(parsed)
    year = int(week_text[:4])
    week = int(week_text[4:])
    try:
        return pd.Timestamp.fromisocalendar(year, week, 1)
    except ValueError:
        return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=(week - 1) * 7)


def legacy_week_id_to_friday(week_id: int | float | str) -> pd.Timestamp:
    """按 0529 周频原始脚本口径把 week_id 转换为周五/特征周日期。"""
    week_text = str(int(week_id))
    if len(week_text) < 6:
        return pd.NaT
    if len(week_text) == 8:
        parsed = pd.to_datetime(week_text, format="%Y%m%d", errors="coerce")
        if pd.notna(parsed):
            return pd.Timestamp(parsed)
    year = int(week_text[:4])
    week = int(week_text[4:])
    try:
        return pd.Timestamp.fromisocalendar(year, week, 5)
    except ValueError:
        return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=(week - 1) * 7 + 4)
