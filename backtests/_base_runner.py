"""通用历史回测数据结构与 free-function 工具。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy.engine import Engine

from backtests.repository import (
    create_backtest_run,
    replace_backtest_predictions,
    update_backtest_run_summary,
)
from shared.metrics import direction_metric_block


RangeMapping = Mapping[str, Any]


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


def apply_evaluation_exclusions(
    rows: list[dict[str, Any]],
    *,
    excluded_target_ranges: Iterable[RangeMapping],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """排除暂不纳入历史验证的目标日期样本。"""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        target_date = _required_target_date(row)
        if target_date and _date_in_excluded_ranges(str(target_date), excluded_target_ranges):
            excluded.append(row)
        else:
            included.append(row)
    return included, excluded


def evaluation_exclusion_summary(
    raw_count: int,
    included_count: int,
    *,
    excluded_target_ranges: Iterable[RangeMapping],
) -> dict[str, Any]:
    return {
        "date_field": "target_date",
        "ranges": [dict(item) for item in excluded_target_ranges],
        "raw_row_count": int(raw_count),
        "included_row_count": int(included_count),
        "excluded_row_count": int(raw_count - included_count),
    }


def _date_in_excluded_ranges(value: str, excluded_target_ranges: Iterable[RangeMapping]) -> bool:
    for item in excluded_target_ranges:
        if item["start"] <= value <= item["end"]:
            return True
    return False


def make_run_output(
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]],
    *,
    benchmark_id: str,
    excluded_target_ranges: Iterable[RangeMapping] = (),
    report_path: str | None = None,
) -> RunOutput:
    filtered_rows, excluded_rows = apply_evaluation_exclusions(rows, excluded_target_ranges=excluded_target_ranges)
    monthly = build_monthly_metrics(filtered_rows, benchmark_id=benchmark_id)
    summary = build_summary(filtered_rows)
    summary["row_count"] = len(filtered_rows)
    summary["raw_row_count"] = len(rows)
    summary["excluded_row_count"] = len(excluded_rows)
    summary["evaluation_filter"] = evaluation_exclusion_summary(
        len(rows),
        len(filtered_rows),
        excluded_target_ranges=excluded_target_ranges,
    )
    summary["monthly_count"] = len(monthly)
    return RunOutput(
        scheme_id=scheme_id,
        data_source=data_source,
        start_date=start_date,
        end_date=end_date,
        rows=filtered_rows,
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
    aggregate_by_tenor: dict[str, dict[str, Any]] = {}
    periods_by_tenor: dict[str, dict[str, Any]] = {}
    for tenor, items in sorted(by_tenor.items()):
        periods = period_summaries(items)
        aggregate_by_tenor[tenor] = deepcopy(periods["all"])
        periods_by_tenor[tenor] = periods
    return {
        "by_tenor": aggregate_by_tenor,
        "periods_by_tenor": periods_by_tenor,
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


def period_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sim": aggregate_rows([row for row in rows if "2025-01-01" <= row["predict_date"] <= "2025-06-30"]),
        "real": aggregate_rows([row for row in rows if "2025-07-01" <= row["predict_date"] <= "2026-04-30"]),
        "may": aggregate_rows([row for row in rows if row["predict_date"] >= "2026-05-01"]),
        "all": aggregate_rows(rows),
    }


def persist_run_output(engine: Engine, output: RunOutput, *, benchmark_id: str) -> int:
    run_id = create_backtest_run(
        engine,
        benchmark_id=benchmark_id,
        scheme_id=output.scheme_id,
        data_source=output.data_source,
        start_date=output.start_date,
        end_date=output.end_date,
        summary=output.summary,
        report_path=output.report_path,
        code_hash=output.summary.get("code_hash"),
        config_hash=output.summary.get("config_hash"),
        input_artifact_hash=output.summary.get("input_artifact_hash"),
    )
    replace_backtest_predictions(engine, run_id, output.rows)
    output.summary["run_id"] = run_id
    update_backtest_run_summary(
        engine,
        run_id=run_id,
        summary=output.summary,
        report_path=output.report_path,
    )
    return run_id
