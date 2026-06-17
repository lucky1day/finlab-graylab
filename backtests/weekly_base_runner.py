from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from backtests.repository import clean_json
from shared.prediction_context import WEEKLY_TARGET_RULE, next_calendar_week_id


@dataclass(frozen=True)
class WeeklyBacktestSpec:
    """周频回测行构造的公共业务参数。"""

    benchmark_id: str
    scheme_id: str
    target_tenor: str
    horizon_days: int
    target_column: str
    model_version: str
    predict_start_date: str
    live_target_start_date: str
    target_rule: str = WEEKLY_TARGET_RULE
    precheck_predict_start: bool = False
    precheck_target: bool = False


@dataclass(frozen=True)
class WeeklyPredictionPoint:
    """单个 feature_week_id 的算法输出。"""

    source_row: Mapping[str, Any]
    predicted_direction: int | None
    confidence: float | None
    extra: Mapping[str, Any] = field(default_factory=dict)


def build_weekly_backtest_rows(
    weekly_df: pd.DataFrame,
    *,
    calendar: Any,
    spec: WeeklyBacktestSpec,
    artifact_path: Path,
    artifact_source: str,
    normalize_frame: Callable[[pd.DataFrame], pd.DataFrame],
    predict_for_feature: Callable[[pd.DataFrame, int], WeeklyPredictionPoint | None],
    weekly_frame_for_feature: Callable[[int, str], pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """按 feature_week_id 逐周构造 PIT 回测行。

    回测发出日固定为 feature_date；target week 统一来自 DB 日历的下一实际周。
    """
    weekly = normalize_frame(weekly_df)
    if weekly.empty:
        return []

    rows: list[dict[str, Any]] = []
    feature_weeks = [int(value) for value in weekly["week_id"].tolist()]
    weekly_by_id = {int(row["week_id"]): row for _, row in weekly.iterrows()}

    for feature_week_id in feature_weeks:
        if feature_week_id not in weekly_by_id:
            continue
        feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
        predict_date = feature_date
        if spec.precheck_predict_start and predict_date < spec.predict_start_date:
            continue

        target = None
        if spec.precheck_target:
            target = _resolve_target(calendar, weekly_by_id, feature_week_id, spec.live_target_start_date)
            if target is None:
                continue

        history_source = weekly_frame_for_feature(feature_week_id, feature_date) if weekly_frame_for_feature else weekly
        history = normalize_frame(history_source)
        history = history[history["week_id"].le(feature_week_id)].copy()
        history_by_id = {int(row["week_id"]): row for _, row in history.iterrows()}
        feature_row = history_by_id.get(feature_week_id)
        if feature_row is None:
            continue

        point = predict_for_feature(history, feature_week_id)
        if point is None:
            continue

        if target is None:
            target = _resolve_target(calendar, weekly_by_id, feature_week_id, spec.live_target_start_date)
            if target is None:
                continue

        if predict_date < spec.predict_start_date:
            continue

        target_week_id, target_date, target_row = target
        future_return = _weekly_future_return(feature_row, target_row, spec.target_column)
        label = _label_from_future_return(future_return) if future_return is not None else None
        source_row = clean_json({**feature_row.to_dict(), **dict(point.source_row)})
        extra = {
            "frequency": "weekly",
            "model_version": spec.model_version,
            "target_rule": spec.target_rule,
            "feature_week_id": feature_week_id,
            "target_week_id": target_week_id,
            "feature_date": feature_date,
            "target_date": target_date,
            "future_return": future_return,
            "input_artifact_path": str(artifact_path),
            "input_artifact_source": artifact_source,
        }
        extra.update(dict(point.extra))

        rows.append(
            {
                "benchmark_id": spec.benchmark_id,
                "scheme_id": spec.scheme_id,
                "target_tenor": spec.target_tenor,
                "horizon": spec.horizon_days,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": target_date,
                "label": label,
                "predicted_direction": point.predicted_direction,
                "model_pred": point.predicted_direction,
                "confidence": point.confidence,
                "source_row": source_row,
                "extra": extra,
            }
        )

    return rows


def compact_weekly_benchmark_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 使用的严格周频 benchmark 行。"""
    compact: list[dict[str, Any]] = []
    for row in rows:
        direction = _int_or_none(row.get("predicted_direction"))
        label = _int_or_none(row.get("label"))
        extra = row.get("extra") or {}
        compact.append(
            {
                "feature_date": str(row["feature_date"]),
                "target_date": str(row["target_date"]),
                "target_tenor": str(row["target_tenor"]),
                "horizon": _int_or_none(row.get("horizon")),
                "direction": direction,
                "confidence": _float_or_none(row.get("confidence")),
                "label": label,
                "is_correct": direction == label if direction is not None and label is not None else None,
                "feature_week_id": _int_or_none(extra.get("feature_week_id")),
                "target_week_id": _int_or_none(extra.get("target_week_id")),
            }
        )
    return compact


def _resolve_target(
    calendar: Any,
    weekly_by_id: dict[int, pd.Series],
    feature_week_id: int,
    live_target_start_date: str,
) -> tuple[int, str, pd.Series] | None:
    try:
        target_week_id = next_calendar_week_id(calendar, feature_week_id)
    except ValueError:
        return None
    target_row = weekly_by_id.get(target_week_id)
    if target_row is None:
        return None
    target_date = calendar.week_id_to_last_trading_day(target_week_id)
    if target_date >= live_target_start_date:
        return None
    return target_week_id, target_date, target_row


def _weekly_future_return(feature_row: pd.Series, target_row: pd.Series, target_column: str) -> float | None:
    current_close = _float_or_none(feature_row.get(target_column))
    target_close = _float_or_none(target_row.get(target_column))
    if current_close is None or target_close is None or current_close == 0:
        return None
    return (target_close - current_close) / current_close


def _label_from_future_return(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result
