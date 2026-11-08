"""Blackbox 历史回测结果、月度指标与汇总口径。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from collections.abc import Mapping

from shared.models import DIRECTION_VALUES


@dataclass
class RunOutput:
    scheme_id: str
    data_source: str
    start_date: str
    end_date: str
    rows: list[dict[str, Any]]
    monthly_metrics: list[dict[str, Any]]
    summary: dict[str, Any]
    report_path: str | None = None


def make_run_output(
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]],
    *,
    benchmark_id: str,
    report_path: str | None = None,
) -> RunOutput:
    monthly = build_monthly_metrics(rows, benchmark_id=benchmark_id)
    summary = build_summary(rows)
    summary["row_count"] = len(rows)
    summary["monthly_count"] = len(monthly)
    return RunOutput(
        scheme_id=scheme_id,
        data_source=data_source,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        monthly_metrics=monthly,
        summary=summary,
        report_path=report_path,
    )


def build_monthly_metrics(rows: list[dict[str, Any]], *, benchmark_id: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        month = _required_target_date(row)[:7]
        grouped.setdefault((row["target_tenor"], month), []).append(row)
    metrics: list[dict[str, Any]] = []
    for (tenor, month), items in sorted(grouped.items()):
        metrics.append(metric_row(items, tenor, month, benchmark_id=benchmark_id))
    return metrics


def _required_target_date(row: dict[str, Any]) -> str:
    value = row.get("target_date")
    if value is not None and str(value).strip():
        return str(value)
    raise ValueError(
        "missing required target_date for backtest row "
        f"scheme_id={row.get('scheme_id')} "
        f"target_tenor={row.get('target_tenor')} "
        f"predict_date={row.get('predict_date')}"
    )


def metric_row(rows: list[dict[str, Any]], tenor: str, month: str, *, benchmark_id: str) -> dict[str, Any]:
    valid = [row for row in rows if row.get("label") is not None and row.get("predicted_direction") is not None]
    metrics = direction_metric_block(valid, actual_key="label")
    return {
        "benchmark_id": benchmark_id,
        "scheme_id": valid[0]["scheme_id"] if valid else rows[0]["scheme_id"],
        "target_tenor": tenor,
        "horizon": int(rows[0]["horizon"]),
        "month": month,
        "sample_count": metrics["samples"],
        "metric_sample_count": metrics["metric_samples"],
        "correct_count": metrics["correct"],
        "accuracy": metrics["accuracy"],
        "up_precision": metrics["up_precision"],
        "up_recall": metrics["up_recall"],
        "down_precision": metrics["down_precision"],
        "down_recall": metrics["down_recall"],
        "actual_dist": metrics["actual_dist"],
        "predicted_dist": metrics["predicted_dist"],
        "metric_actual_dist": metrics["metric_actual_dist"],
        "metric_predicted_dist": metrics["metric_predicted_dist"],
    }


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_tenor: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tenor.setdefault(row["target_tenor"], []).append(row)
    return {
        "by_tenor": {
            tenor: aggregate_rows(items)
            for tenor, items in sorted(by_tenor.items())
        },
    }


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("label") is not None and row.get("predicted_direction") is not None]
    metrics = direction_metric_block(valid, actual_key="label")
    return {
        "samples": metrics["samples"],
        "metric_samples": metrics["metric_samples"],
        "correct": metrics["correct"],
        "accuracy": metrics["accuracy"],
        "accuracy_pct": round(metrics["accuracy"] * 100, 1) if metrics["accuracy"] is not None else None,
        "actual_dist": metrics["actual_dist"],
        "predicted_dist": metrics["predicted_dist"],
        "metric_actual_dist": metrics["metric_actual_dist"],
        "metric_predicted_dist": metrics["metric_predicted_dist"],
        "date_min": min((row["predict_date"] for row in valid), default=None),
        "date_max": max((row["predict_date"] for row in valid), default=None),
    }


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
