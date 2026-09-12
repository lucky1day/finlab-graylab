from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from shared.prediction_context import next_calendar_week_id
from shared.tenor_mapping import TENOR_TO_INDICATOR


@dataclass(frozen=True)
class WeeklyAverageLabel:
    """周平均 actual 标签，按目标周均值相对特征周均值计算。"""

    feature_week_id: int
    target_week_id: int
    feature_date: str
    target_date: str
    feature_yield: float
    target_yield: float
    future_return: float | None
    label: int | None


def read_weekly_average_label_rows(
    engine: Engine,
    *,
    target_tenor: str,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """读取周平均 label 所需的日频收益率明细。"""
    indicator_code = TENOR_TO_INDICATOR[str(target_tenor)]
    end_filter = "AND rdate <= :end_date" if end_date else ""
    stmt = text(
        f"""
        SELECT rdate, indicators_value
        FROM api_wind_daily
        WHERE indicators_code = :indicator_code
          AND indicators_value IS NOT NULL
          {end_filter}
        ORDER BY rdate
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            stmt,
            {"indicator_code": indicator_code, "end_date": end_date},
        ).mappings().all()
    return [
        {
            "tenor": target_tenor,
            "trade_date": str(row["rdate"]),
            "close_yield": _float_or_none(row["indicators_value"]),
        }
        for row in rows
        if _float_or_none(row["indicators_value"]) is not None
    ]


def build_weekly_average_label_map_from_rows(
    rows: list[dict[str, Any]],
    *,
    calendar: Any,
    target_tenor: str,
    live_target_start_date: str,
) -> dict[int, WeeklyAverageLabel]:
    """按日频收益率均值构造 feature_week_id -> 周平均标签。"""
    grouped: dict[int, dict[str, Any]] = defaultdict(lambda: {"items": [], "last": None})
    max_trade_date: str | None = None
    for raw in rows:
        if str(raw.get("tenor")) != str(target_tenor):
            continue
        trade_date = str(raw["trade_date"])
        week_id = calendar.week_id_for_date(trade_date)
        close_yield = _float_or_none(raw.get("close_yield"))
        if week_id is None or close_yield is None:
            continue
        item = {"trade_date": trade_date, "close_yield": close_yield}
        bucket = grouped[int(week_id)]
        bucket["items"].append(item)
        if bucket["last"] is None or trade_date > bucket["last"]["trade_date"]:
            bucket["last"] = item
        if max_trade_date is None or trade_date > max_trade_date:
            max_trade_date = trade_date

    labels: dict[int, WeeklyAverageLabel] = {}
    for feature_week_id in sorted(grouped):
        try:
            target_week_id = next_calendar_week_id(calendar, int(feature_week_id))
        except ValueError:
            continue
        target_bucket = grouped.get(target_week_id)
        if target_bucket is None:
            continue
        target_date = calendar.week_id_to_last_trading_day(target_week_id)
        if target_date >= live_target_start_date:
            continue
        if max_trade_date is None or max_trade_date < target_date:
            continue
        feature_bucket = grouped[feature_week_id]
        feature_yield = _mean_close_yield(feature_bucket["items"])
        target_yield = _mean_close_yield(target_bucket["items"])
        future_return = _weekly_future_return_from_values(feature_yield, target_yield)
        labels[int(feature_week_id)] = WeeklyAverageLabel(
            feature_week_id=int(feature_week_id),
            target_week_id=int(target_week_id),
            feature_date=str(feature_bucket["last"]["trade_date"]),
            target_date=str(target_date),
            feature_yield=feature_yield,
            target_yield=target_yield,
            future_return=future_return,
            label=_label_from_future_return(future_return) if future_return is not None else None,
        )
    return labels


def compact_weekly_benchmark_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 使用的严格周频 benchmark 行。"""
    compact: list[dict[str, Any]] = []
    for row in rows:
        if (row.get("extra") or {}).get("signal_policy_applied") is True:
            continue
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
                "label": label,
                "is_correct": direction == label if direction is not None and label is not None else None,
                "feature_week_id": _int_or_none(extra.get("feature_week_id")),
                "target_week_id": _int_or_none(extra.get("target_week_id")),
            }
        )
        compact[-1].update(_compact_extra_fields(extra, compact[-1]))
    return compact


def _compact_extra_fields(extra: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    """保留 CompareGate 需要的 extra 审计字段，避免把大路径/收益率诊断写入 benchmark CSV。"""
    skipped = {
        "input_artifact_path",
        "input_artifact_source",
        "feature_yield",
        "target_yield",
        "future_return",
    }
    out: dict[str, Any] = {}
    for key in sorted(extra):
        if key in skipped or key in existing:
            continue
        value = extra.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def _weekly_future_return_from_values(current_close: float | None, target_close: float | None) -> float | None:
    if current_close is None or target_close is None or current_close == 0:
        return None
    return (target_close - current_close) / current_close


def _mean_close_yield(items: list[dict[str, Any]]) -> float:
    return sum(float(item["close_yield"]) for item in items) / len(items)


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
