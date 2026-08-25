"""通用历史回测数据结构与 free-function 工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from backtests.repository import (
    clean_json,
    create_backtest_run,
    replace_backtest_predictions,
    update_backtest_run_summary,
)
from shared.artifact_paths import benchmark_input_root
from shared.input_artifacts import build_daily_input_artifact
from shared.metrics import direction_dist as shared_direction_dist
from shared.metrics import direction_metric_block


RangeMapping = Mapping[str, Any]
ArtifactBuilder = Callable[..., Any]


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


def read_daily_csv(path: str | Path) -> pd.DataFrame:
    """读取并标准化 historical daily_output。"""
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"daily output missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_db_aligned_daily(
    csv_df: pd.DataFrame | None = None,
    engine: Engine | None = None,
    artifact_scheme_id: str = "daily_common",
    *,
    benchmark_id: str,
    canonical_csv: str | Path | None,
    start_date: str | None = None,
    end_date: str | None = None,
    artifact_builder: ArtifactBuilder = build_daily_input_artifact,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成完整 DB 版 daily_output，以及必要时按 source CSV 对齐后的版本。

    DB 输入统一经过 shared.input_artifacts 生成和读回，再喂给算法。
    """
    if csv_df is None:
        original = read_daily_csv(canonical_csv) if canonical_csv is not None else None
    else:
        original = csv_df
    if original is not None:
        start_date = original["date"].min().strftime("%Y-%m-%d")
        end_date = original["date"].max().strftime("%Y-%m-%d")
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required when no source CSV is provided")
    input_artifact = artifact_builder(
        scheme_id=artifact_scheme_id,
        predict_date=end_date,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        output_root=benchmark_input_root(benchmark_id),
    )
    db_df = input_artifact.dataframe
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"], errors="coerce").dt.normalize()
    db_df = db_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if original is None:
        return db_df, db_df

    aligned = db_df.set_index("date")
    aligned = aligned.reindex(original["date"])
    aligned = aligned.reindex(columns=[col for col in original.columns if col != "date"])
    aligned = aligned.reset_index()
    aligned = aligned.rename(columns={"index": "date"})
    return db_df, aligned


def build_framework_db_aligned_daily(
    csv_df: pd.DataFrame | None = None,
    engine: Engine | None = None,
    *,
    benchmark_id: str,
    canonical_csv: str | Path | None,
    artifact_builder: ArtifactBuilder = build_daily_input_artifact,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成当前框架 data_service 默认口径的 DB daily_output，用于额外对照。"""
    return build_db_aligned_daily(
        csv_df=csv_df,
        engine=engine,
        artifact_scheme_id="daily_framework",
        benchmark_id=benchmark_id,
        canonical_csv=canonical_csv,
        artifact_builder=artifact_builder,
    )


def compare_daily_frames(
    csv_df: pd.DataFrame,
    db_full: pd.DataFrame,
    db_aligned: pd.DataFrame,
    *,
    target_columns: Iterable[str],
) -> dict[str, Any]:
    csv_dates = csv_df["date"].dt.strftime("%Y-%m-%d").tolist()
    db_dates = db_aligned["date"].dt.strftime("%Y-%m-%d").tolist()
    csv_cols = csv_df.columns.tolist()
    db_aligned_cols = db_aligned.columns.tolist()
    csv_set = set(csv_cols)
    db_full_set = set(db_full.columns.tolist())
    csv_only = sorted(csv_set - db_full_set)
    db_only = sorted(db_full_set - csv_set)

    target_set = set(target_columns)
    target_diff: dict[str, float | None] = {}
    max_abs = 0.0
    missing_diff_count = 0
    first_diff: dict[str, Any] | None = None
    above_threshold: list[dict[str, Any]] = []
    numeric_cols = [col for col in csv_cols if col != "date"]

    for col in numeric_cols:
        left = pd.to_numeric(csv_df[col], errors="coerce")
        right = pd.to_numeric(db_aligned[col], errors="coerce") if col in db_aligned.columns else pd.Series(np.nan, index=csv_df.index)
        left_na = left.isna()
        right_na = right.isna()
        missing_mask = left_na.ne(right_na)
        missing_count = int(missing_mask.sum())
        missing_diff_count += missing_count
        both = ~(left_na | right_na)
        col_max = None
        if bool(both.any()):
            diff = (left[both] - right[both]).abs()
            col_max = float(diff.max()) if len(diff) else 0.0
            max_abs = max(max_abs, col_max)
            diff_mask = diff.gt(1e-8)
            if bool(diff_mask.any()) and len(above_threshold) < 50:
                idx = int(diff[diff_mask].index[0])
                above_threshold.append(
                    {
                        "column": col,
                        "date": csv_df.loc[idx, "date"].strftime("%Y-%m-%d"),
                        "csv": float(left.loc[idx]),
                        "db": float(right.loc[idx]),
                        "abs_diff": float(diff.loc[idx]),
                    }
                )
                if first_diff is None:
                    first_diff = above_threshold[-1]
        if col in target_set:
            target_diff[col] = col_max
        if first_diff is None and missing_count:
            idx = int(missing_mask[missing_mask].index[0])
            first_diff = {
                "column": col,
                "date": csv_df.loc[idx, "date"].strftime("%Y-%m-%d"),
                "csv_is_null": bool(left_na.loc[idx]),
                "db_is_null": bool(right_na.loc[idx]),
            }

    date_match = csv_dates == db_dates
    column_order_match = csv_cols == db_aligned_cols
    target_ok = all((value is not None and value <= 1e-10) for value in target_diff.values())
    numeric_ok = max_abs <= 1e-8 and missing_diff_count == 0
    status = "success" if date_match and column_order_match and target_ok and numeric_ok else "failed"
    return {
        "status": status,
        "csv": _frame_profile(csv_df),
        "db_full": _frame_profile(db_full),
        "db_aligned": _frame_profile(db_aligned),
        "columns": {
            "csv_only": csv_only,
            "db_only": db_only,
            "column_order_match": column_order_match,
        },
        "date_match": date_match,
        "target_max_abs_diff": target_diff,
        "numeric_diff": {
            "overall_max_abs_diff": max_abs,
            "missing_diff_count": int(missing_diff_count),
            "above_threshold_sample": above_threshold,
            "first_diff": first_diff,
        },
        "thresholds": {
            "target_abs_diff": 1e-10,
            "numeric_abs_diff": 1e-8,
        },
    }


def compare_generated_frames(left: pd.DataFrame, right: pd.DataFrame) -> dict[str, Any]:
    """比较上游 DB 生成 CSV 与当前框架 DB 生成 CSV 的对齐版。"""
    left_dates = left["date"].dt.strftime("%Y-%m-%d").tolist()
    right_dates = right["date"].dt.strftime("%Y-%m-%d").tolist()
    first_diff = None
    max_abs = 0.0
    missing_diff_count = 0
    for col in [item for item in left.columns if item != "date" and item in right.columns]:
        left_series = pd.to_numeric(left[col], errors="coerce")
        right_series = pd.to_numeric(right[col], errors="coerce")
        missing = left_series.isna().ne(right_series.isna())
        missing_diff_count += int(missing.sum())
        both = ~(left_series.isna() | right_series.isna())
        if bool(both.any()):
            diff = (left_series[both] - right_series[both]).abs()
            col_max = float(diff.max()) if len(diff) else 0.0
            max_abs = max(max_abs, col_max)
            bad = diff.gt(1e-12)
            if first_diff is None and bool(bad.any()):
                idx = int(diff[bad].index[0])
                first_diff = {
                    "column": col,
                    "date": left.loc[idx, "date"].strftime("%Y-%m-%d"),
                    "upstream": float(left_series.loc[idx]),
                    "framework": float(right_series.loc[idx]),
                    "abs_diff": float(diff.loc[idx]),
                }
        if first_diff is None and bool(missing.any()):
            idx = int(missing[missing].index[0])
            first_diff = {
                "column": col,
                "date": left.loc[idx, "date"].strftime("%Y-%m-%d"),
                "upstream_is_null": bool(left_series.isna().loc[idx]),
                "framework_is_null": bool(right_series.isna().loc[idx]),
            }
    return {
        "date_match": left_dates == right_dates,
        "column_order_match": left.columns.tolist() == right.columns.tolist(),
        "rows": {"upstream": int(len(left)), "framework": int(len(right))},
        "columns": {"upstream": int(len(left.columns)), "framework": int(len(right.columns))},
        "overall_max_abs_diff": max_abs,
        "missing_diff_count": missing_diff_count,
        "first_diff": first_diff,
    }


def exclude_daily_rows_for_evaluation_week(
    csv_df: pd.DataFrame,
    db_aligned: pd.DataFrame,
    *,
    excluded_target_ranges: Iterable[RangeMapping],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从数据一致性报告的有效口径中排除暂不验证的目标周日期。"""
    csv_mask = ~csv_df["date"].dt.strftime("%Y-%m-%d").map(lambda value: _date_in_excluded_ranges(value, excluded_target_ranges))
    db_mask = ~db_aligned["date"].dt.strftime("%Y-%m-%d").map(lambda value: _date_in_excluded_ranges(value, excluded_target_ranges))
    return csv_df.loc[csv_mask].reset_index(drop=True), db_aligned.loc[db_mask].reset_index(drop=True)


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
    return {
        "by_tenor": {tenor: aggregate_rows(items) for tenor, items in sorted(by_tenor.items())},
        "periods_by_tenor": {tenor: period_summaries(items) for tenor, items in sorted(by_tenor.items())},
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


def compare_prediction_rows(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    fields: Iterable[str],
    float_fields: Iterable[str] = (),
    float_tolerance: float = 1e-9,
) -> dict[str, Any]:
    baseline_map = {(row["target_tenor"], row["predict_date"]): row for row in baseline}
    candidate_map = {(row["target_tenor"], row["predict_date"]): row for row in candidate}
    baseline_keys = set(baseline_map)
    candidate_keys = set(candidate_map)
    mismatch_rows = []
    for key in sorted(baseline_keys & candidate_keys):
        left = baseline_map[key]
        right = candidate_map[key]
        diffs = {}
        for field in fields:
            if _normalize_scalar(left.get(field)) != _normalize_scalar(right.get(field)):
                diffs[field] = {"baseline": clean_json(left.get(field)), "candidate": clean_json(right.get(field))}
        for field in float_fields:
            left_value = _float_or_none(left.get(field))
            right_value = _float_or_none(right.get(field))
            if left_value is None and right_value is None:
                continue
            if left_value is None or right_value is None or abs(left_value - right_value) > float_tolerance:
                diffs[field] = {"baseline": left_value, "candidate": right_value}
        if diffs:
            mismatch_rows.append({"target_tenor": key[0], "predict_date": key[1], "diffs": diffs})
    return {
        "baseline_rows": len(baseline),
        "candidate_rows": len(candidate),
        "matched_rows": len(baseline_keys & candidate_keys),
        "baseline_only": [{"target_tenor": key[0], "predict_date": key[1]} for key in sorted(baseline_keys - candidate_keys)[:200]],
        "candidate_only": [{"target_tenor": key[0], "predict_date": key[1]} for key in sorted(candidate_keys - baseline_keys)[:200]],
        "mismatch_count": len(mismatch_rows) + len(baseline_keys - candidate_keys) + len(candidate_keys - baseline_keys),
        "mismatch_rows": mismatch_rows[:200],
        "float_tolerance": float_tolerance,
    }


def persist_run_output(engine: Engine, output: RunOutput, *, benchmark_id: str) -> int:
    run_id = create_backtest_run(
        engine,
        benchmark_id=benchmark_id,
        scheme_id=output.scheme_id,
        data_source=output.data_source,
        start_date=output.start_date,
        end_date=output.end_date,
        status="running",
        summary=output.summary,
        report_path=output.report_path,
        code_hash=output.summary.get("code_hash"),
        config_hash=output.summary.get("config_hash"),
        input_artifact_hash=output.summary.get("input_artifact_hash"),
        run_mode="persist",
    )
    replace_backtest_predictions(engine, run_id, output.rows)
    output.summary["run_id"] = run_id
    update_backtest_run_summary(
        engine,
        run_id=run_id,
        status="success",
        summary=output.summary,
        report_path=output.report_path,
    )
    return run_id


def _frame_profile(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "date_min": df["date"].min().strftime("%Y-%m-%d") if len(df) else None,
        "date_max": df["date"].max().strftime("%Y-%m-%d") if len(df) else None,
    }


def direction_dist(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    dist = dict(shared_direction_dist(rows, key))
    dist["missing"] = sum(1 for row in rows if row.get(key) is None)
    return dist


def safe_div(num: int, den: int) -> float | None:
    return None if den == 0 else float(num / den)


def infer_target_date(daily: pd.DataFrame, feature_date: str, horizon: int) -> str | None:
    dates = pd.to_datetime(daily["date"]).dt.normalize()
    matches = dates[dates.eq(pd.Timestamp(feature_date))]
    if matches.empty:
        return None
    target_idx = int(matches.index[-1]) + horizon
    if target_idx >= len(dates):
        return None
    return dates.iloc[target_idx].strftime("%Y-%m-%d")


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if hasattr(value, "item"):
        return _normalize_scalar(value.item())
    return value


def _int_or_none(value: Any) -> int | None:
    value = _normalize_scalar(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    value = _normalize_scalar(value)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(result) or np.isinf(result):
        return None
    return result
