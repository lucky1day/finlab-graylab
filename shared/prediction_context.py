from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


WEEKLY_TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"
WEEKLY_AVERAGE_TARGET_RULE = "next_week_average_yield_vs_current_week_average_yield"
MONTHLY_TARGET_RULE = "next_month_observation_yield_vs_feature_month_observation_yield"


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


@dataclass(frozen=True)
class MonthlyLiveContext:
    trigger_date: str
    scheduled_trigger_date: str
    db_rdate: str
    feature_date: str
    feature_month_id: str
    target_month_id: str
    target_date: str
    target_rule: str = MONTHLY_TARGET_RULE


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


def build_monthly_live_context(calendar: Any, predict_date: str) -> MonthlyLiveContext:
    """月频 source 语义：自然月 15 日触发，输入截止到 15 日及以前最近交易日。"""
    scheduled = date.fromisoformat(str(predict_date)[:10])
    trigger = date(scheduled.year, scheduled.month, 15)
    if scheduled != trigger:
        raise ValueError(
            f"monthly predict_date must be natural month 15 {trigger.isoformat()}, got {scheduled.isoformat()}"
        )
    feature_date = _last_trading_day_on_or_before(calendar, trigger)
    target_anchor = _add_month(trigger)
    target_date = _last_trading_day_on_or_before(calendar, target_anchor)
    return MonthlyLiveContext(
        trigger_date=trigger.isoformat(),
        scheduled_trigger_date=trigger.isoformat(),
        db_rdate=trigger.isoformat(),
        feature_date=feature_date,
        feature_month_id=trigger.strftime("%Y-%m"),
        target_month_id=target_anchor.strftime("%Y-%m"),
        target_date=target_date,
    )


def next_calendar_week_id(calendar: Any, feature_week_id: int) -> int:
    """从 DB 日历读取 feature_week_id 后的下一实际 week_id。"""
    feature_date = calendar.week_id_to_last_trading_day(int(feature_week_id))
    for day in calendar.next_trading_days(feature_date, 15):
        next_week = calendar.week_id_for_date(day)
        if next_week is not None and int(next_week) != int(feature_week_id):
            return int(next_week)
    raise ValueError(f"无法在 DB 日历中找到 week_id={feature_week_id} 的下一周")


def _last_trading_day_on_or_before(calendar: Any, value: date) -> str:
    day = value.isoformat()
    if calendar.is_trading_day(day):
        return day
    return str(calendar.previous_trading_day(day))[:10]


def _add_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, value.day)
    return date(value.year, value.month + 1, value.day)
