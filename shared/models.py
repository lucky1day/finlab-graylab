from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DIRECTION_VALUES = frozenset({-1, 0, 1})


@dataclass(frozen=True)
class PredictionRecord:
    """统一预测记录，每个期限一条。"""

    scheme_id: str
    target_tenor: str
    horizon: int
    predict_date: str
    target_date: str
    predicted_direction: int
    feature_date: str | None = None
    prediction_phase: str | None = None
    confidence: float | None = None
    model_version: str | None = None
    extra: dict[str, Any] | None = None
    run_id: int | None = None
    scheme_version: str | None = None


@dataclass(frozen=True)
class ActualRecord:
    """统一实际方向记录，每个期限和交易日一条。"""

    tenor: str
    trade_date: str
    close_yield: float
    direction_1d: int | None = None
    direction_5d: int | None = None


@dataclass(frozen=True)
class WeeklyActualRecord:
    """周度实际方向记录，方向为收益率口径；展示时映射为空/多/平。"""

    tenor: str
    feature_week_id: int
    target_week_id: int
    predict_date: str
    feature_date: str
    target_date: str
    feature_yield: float
    target_yield: float
    direction_weekly: int
    price_signal: str
    target_rule: str
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class MonthlyActualRecord:
    """月度实际方向记录，方向为收益率口径；展示时映射为空/多/平。"""

    tenor: str
    feature_month_id: str
    target_month_id: str
    predict_date: str
    feature_date: str
    target_date: str
    feature_yield: float
    target_yield: float
    direction_monthly: int
    price_signal: str
    target_rule: str
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class PeriodAverageActualRecord:
    """MID/CQ/SF 周期均值实际方向记录。"""

    tenor: str
    predict_date: str
    feature_date: str
    target_date: str
    feature_yield: float
    target_yield: float
    actual_direction: int
    price_signal: str
    target_rule: str
    extra: dict[str, Any] | None = None
