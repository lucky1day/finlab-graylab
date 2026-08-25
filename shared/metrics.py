"""统一方向预测指标口径。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.models import DIRECTION_VALUES


def safe_div(numerator: int, denominator: int) -> float | None:
    """安全除法；分母为 0 时返回 None。"""
    if denominator == 0:
        return None
    return numerator / denominator


def direction_dist(rows: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    """统计涨/跌/平方向分布。"""
    return {
        "up": sum(1 for row in rows if _direction_or_none(row.get(key)) == 1),
        "down": sum(1 for row in rows if _direction_or_none(row.get(key)) == -1),
        "flat": sum(1 for row in rows if _direction_or_none(row.get(key)) == 0),
    }


def direction_metric_block(
    rows: list[Mapping[str, Any]],
    *,
    predicted_key: str = "predicted_direction",
    actual_key: str = "actual_direction",
) -> dict[str, Any]:
    """计算方向预测指标。

    `samples` 是可评价样本总数，包含预测为平的样本；准确率、precision、
    recall 的分母使用 `metric_samples`，即只包含有方向信号的预测样本。
    """
    valid = [
        row
        for row in rows
        if _direction_or_none(row.get(predicted_key)) is not None
        and _direction_or_none(row.get(actual_key)) is not None
    ]
    metric_rows = [
        row
        for row in valid
        if _direction_or_none(row.get(predicted_key)) in (-1, 1)
    ]
    correct = sum(
        1
        for row in metric_rows
        if _direction_or_none(row.get(predicted_key)) == _direction_or_none(row.get(actual_key))
    )
    pred_up = sum(1 for row in metric_rows if _direction_or_none(row.get(predicted_key)) == 1)
    pred_down = sum(1 for row in metric_rows if _direction_or_none(row.get(predicted_key)) == -1)
    actual_up = sum(1 for row in metric_rows if _direction_or_none(row.get(actual_key)) == 1)
    actual_down = sum(1 for row in metric_rows if _direction_or_none(row.get(actual_key)) == -1)
    up_tp = sum(
        1
        for row in metric_rows
        if _direction_or_none(row.get(predicted_key)) == 1
        and _direction_or_none(row.get(actual_key)) == 1
    )
    down_tp = sum(
        1
        for row in metric_rows
        if _direction_or_none(row.get(predicted_key)) == -1
        and _direction_or_none(row.get(actual_key)) == -1
    )
    metric_samples = len(metric_rows)
    return {
        "samples": len(valid),
        "metric_samples": metric_samples,
        "correct": correct,
        "accuracy": safe_div(correct, metric_samples),
        "up_precision": safe_div(up_tp, pred_up),
        "up_recall": safe_div(up_tp, actual_up),
        "down_precision": safe_div(down_tp, pred_down),
        "down_recall": safe_div(down_tp, actual_down),
        "actual_dist": direction_dist(valid, actual_key),
        "predicted_dist": direction_dist(valid, predicted_key),
        "metric_actual_dist": direction_dist(metric_rows, actual_key),
        "metric_predicted_dist": direction_dist(metric_rows, predicted_key),
    }


def _direction_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        direction = int(value)
    except (TypeError, ValueError):
        return None
    if direction not in DIRECTION_VALUES:
        return None
    return direction
