from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WEEKLY_TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"


@dataclass(frozen=True)
class DailyLiveContext:
    feature_date: str
    target_date: str


@dataclass(frozen=True)
class WeeklyLiveContext:
    feature_date: str
    feature_week_id: int
    target_week_id: int
    target_date: str
    target_rule: str = WEEKLY_TARGET_RULE


def build_daily_live_context(calendar: Any, predict_date: str, *, horizon: int) -> DailyLiveContext:
    """实盘日频语义：predict_date=T+1，feature_date=T，target_date=T+horizon。"""
    feature_date = calendar.previous_trading_day(predict_date)
    target_date = calendar.nth_trading_day_after(feature_date, int(horizon))
    return DailyLiveContext(feature_date=str(feature_date), target_date=str(target_date))


def build_weekly_live_context(calendar: Any, predict_date: str) -> WeeklyLiveContext:
    """实盘周频语义：用 predict_date 前一交易日定位 feature week，再取下一实际周 target。"""
    feature_date = calendar.previous_trading_day(predict_date)
    feature_week_id = calendar.week_id_for_date(feature_date)
    if feature_week_id is None:
        raise ValueError(f"无法从 DB 日历解析 feature_date={feature_date} 的 week_id")
    target_week_id = next_calendar_week_id(calendar, int(feature_week_id))
    target_date = calendar.week_id_to_last_trading_day(target_week_id)
    return WeeklyLiveContext(
        feature_date=str(feature_date),
        feature_week_id=int(feature_week_id),
        target_week_id=int(target_week_id),
        target_date=str(target_date),
    )


def next_calendar_week_id(calendar: Any, feature_week_id: int) -> int:
    """从 DB 日历读取 feature_week_id 后的下一实际 week_id。"""
    feature_date = calendar.week_id_to_last_trading_day(int(feature_week_id))
    for day in calendar.next_trading_days(feature_date, 15):
        next_week = calendar.week_id_for_date(day)
        if next_week is not None and int(next_week) != int(feature_week_id):
            return int(next_week)
    raise ValueError(f"无法在 DB 日历中找到 week_id={feature_week_id} 的下一周")
