"""通用历史回测数据结构与 free-function 工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.metrics import direction_metric_block


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
        month = _metric_month(row)
        grouped.setdefault((row["target_tenor"], month), []).append(row)
    metrics: list[dict[str, Any]] = []
    for (tenor, month), items in sorted(grouped.items()):
        metrics.append(metric_row(items, tenor, month, benchmark_id=benchmark_id))
    return metrics


def _metric_month(row: dict[str, Any]) -> str:
    """返回历史回测月度指标归属月份（按 target_date 分组）。"""
    return _required_target_date(row)[:7]


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
