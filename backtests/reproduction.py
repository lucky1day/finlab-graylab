from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from backtests.repository import (
    clean_json,
    insert_reproduction_check,
    replace_backtest_monthly_metrics,
    replace_backtest_predictions,
    upsert_backtest_run,
)
from shared.data_service import build_daily_output_from_db, create_sqlalchemy_engine
from shared.artifact_paths import benchmark_data_check_root, benchmark_input_root
from shared.input_artifacts import build_daily_input_artifact


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ID = "model_muti_0529"
CANONICAL_DAILY = PROJECT_ROOT / "benchmarks" / "model_muti_0529" / "daily_output.csv"
DATA_CHECK_ROOT = benchmark_data_check_root(BENCHMARK_ID)
TARGET_COLUMNS = ("TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
UPSTREAM_DAILY_TARGETS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")
T5_BACKTEST_START = "2025-01-01"
T5_BACKTEST_END = "2026-05-31"
T1_BACKTEST_START = "2025-01-02"
T1_BACKTEST_END = "2026-05-28"
EVALUATION_EXCLUDED_TARGET_RANGES = (
    {
        "label": "2026-05 last target week",
        "start": "2026-05-25",
        "end": "2026-05-29",
        "reason": "temporary validation exclusion requested by user",
    },
)

EXPECTED_T5_REPORT: dict[str, dict[str, Any]] = {
    "3Y": {"all": {"samples": 328, "correct": 228, "accuracy_pct": 69.5}, "sim_n": 117, "real_n": 203, "may_n": 8},
    "5Y": {"all": {"samples": 328, "correct": 212, "accuracy_pct": 64.6}, "sim_n": 117, "real_n": 203, "may_n": 8},
    "7Y": {"all": {"samples": 328, "correct": 218, "accuracy_pct": 66.5}, "sim_n": 117, "real_n": 203, "may_n": 8},
    "10Y": {"all": {"samples": 328, "correct": 208, "accuracy_pct": 63.4}, "sim_n": 117, "real_n": 203, "may_n": 8},
}


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


def read_daily_csv(path: str | Path = CANONICAL_DAILY) -> pd.DataFrame:
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
    upstream_mode: bool = True,
    artifact_scheme_id: str = "daily_common",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成完整 DB 版 daily_output 和按 canonical CSV 对齐后的版本。

    upstream_mode=True 时使用上游 data_service.py 的交易日锚定口径:
    DAILY_TARGETS = TB1YWI0C/TB5YWI0C/TB0YWI0C。历史复现的 DB 输入
    必须先经过这个“从 DB 生成 CSV”的步骤，再喂给算法。
    """
    original = csv_df if csv_df is not None else read_daily_csv()
    start_date = original["date"].min().strftime("%Y-%m-%d")
    end_date = original["date"].max().strftime("%Y-%m-%d")
    if upstream_mode:
        input_artifact = build_daily_input_artifact(
            scheme_id=artifact_scheme_id,
            predict_date=end_date,
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            output_root=benchmark_input_root(BENCHMARK_ID),
        )
        db_df = input_artifact.dataframe
    else:
        db_df = build_daily_output_from_db(
            start_date=start_date,
            end_date=end_date,
            target_columns=None,
            engine=engine,
        )
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"], errors="coerce").dt.normalize()
    db_df = db_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    aligned = db_df.set_index("date")
    aligned = aligned.reindex(original["date"])
    for col in original.columns:
        if col != "date" and col not in aligned.columns:
            aligned[col] = np.nan
    aligned = aligned[[col for col in original.columns if col != "date"]].reset_index()
    aligned = aligned.rename(columns={"index": "date"})
    return db_df, aligned


def build_framework_db_aligned_daily(csv_df: pd.DataFrame | None = None, engine: Engine | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成当前框架 data_service 默认口径的 DB daily_output，用于额外对照。"""
    return build_db_aligned_daily(csv_df=csv_df, engine=engine, upstream_mode=False)


def run_data_alignment_check(engine: Engine | None = None, persist: bool = True) -> dict[str, Any]:
    """对比 canonical CSV 与当前 DB 生成的 daily_output。"""
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        csv_df = read_daily_csv()
        upstream_full, upstream_aligned = build_db_aligned_daily(
            csv_df,
            engine=engine,
            upstream_mode=True,
            artifact_scheme_id="daily_common",
        )
        framework_full, framework_aligned = build_framework_db_aligned_daily(csv_df, engine=engine)
        report = compare_daily_frames(csv_df, upstream_full, upstream_aligned)
        effective_csv, effective_aligned = exclude_daily_rows_for_evaluation_week(csv_df, upstream_aligned)
        report["evaluation_exclusion"] = evaluation_exclusion_summary(len(csv_df), len(effective_csv))
        report["excluding_evaluation_target_week"] = compare_daily_frames(effective_csv, upstream_full, effective_aligned)
        report["generation"] = {
            "primary": "shared_daily_data_service",
            "upstream_daily_targets": list(UPSTREAM_DAILY_TARGETS),
            "framework_daily_targets": list(TARGET_COLUMNS),
            "daily_data_service_path": "shared/data_service.py",
        }
        report["framework_db_comparison"] = compare_generated_frames(upstream_aligned, framework_aligned)
        report["framework_db_full"] = _frame_profile(framework_full)
        row = {
            "benchmark_id": BENCHMARK_ID,
            "check_name": "canonical_csv_vs_upstream_db_generated",
            "status": report["status"],
            "source_path": str(CANONICAL_DAILY),
            "row_count_csv": report["csv"]["rows"],
            "row_count_db": report["db_aligned"]["rows"],
            "col_count_csv": report["csv"]["columns"],
            "col_count_db": report["db_aligned"]["columns"],
            "date_min_csv": report["csv"]["date_min"],
            "date_max_csv": report["csv"]["date_max"],
            "date_min_db": report["db_aligned"]["date_min"],
            "date_max_db": report["db_aligned"]["date_max"],
            "csv_only_columns": report["columns"]["csv_only"],
            "db_only_columns": report["columns"]["db_only"],
            "target_max_abs_diff": report["target_max_abs_diff"],
            "overall_max_abs_diff": report["numeric_diff"]["overall_max_abs_diff"],
            "missing_diff_count": report["numeric_diff"]["missing_diff_count"],
            "first_diff": report["numeric_diff"]["first_diff"],
            "report": report,
        }
        if persist:
            insert_reproduction_check(engine, row)
        artifact = DATA_CHECK_ROOT / "upstream_db_generated_daily_output.csv"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        upstream_aligned.to_csv(artifact, index=False)
        report["db_aligned_path"] = str(artifact)
        framework_artifact = DATA_CHECK_ROOT / "framework_db_generated_daily_output.csv"
        framework_aligned.to_csv(framework_artifact, index=False)
        report["framework_db_aligned_path"] = str(framework_artifact)
        return report
    finally:
        if own_engine:
            engine.dispose()


def compare_daily_frames(csv_df: pd.DataFrame, db_full: pd.DataFrame, db_aligned: pd.DataFrame) -> dict[str, Any]:
    csv_dates = csv_df["date"].dt.strftime("%Y-%m-%d").tolist()
    db_dates = db_aligned["date"].dt.strftime("%Y-%m-%d").tolist()
    csv_cols = csv_df.columns.tolist()
    db_aligned_cols = db_aligned.columns.tolist()
    csv_set = set(csv_cols)
    db_full_set = set(db_full.columns.tolist())
    csv_only = sorted(csv_set - db_full_set)
    db_only = sorted(db_full_set - csv_set)

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
        if col in TARGET_COLUMNS:
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


def exclude_daily_rows_for_evaluation_week(csv_df: pd.DataFrame, db_aligned: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从数据一致性报告的有效口径中排除暂不验证的目标周日期。"""
    csv_mask = ~csv_df["date"].dt.strftime("%Y-%m-%d").map(_date_in_excluded_ranges)
    db_mask = ~db_aligned["date"].dt.strftime("%Y-%m-%d").map(_date_in_excluded_ranges)
    return csv_df.loc[csv_mask].reset_index(drop=True), db_aligned.loc[db_mask].reset_index(drop=True)


def apply_evaluation_exclusions(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """排除暂不纳入历史验证的目标日期样本。"""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        target_date = row.get("target_date") or row.get("predict_date")
        if target_date and _date_in_excluded_ranges(str(target_date)):
            excluded.append(row)
        else:
            included.append(row)
    return included, excluded


def evaluation_exclusion_summary(raw_count: int, included_count: int) -> dict[str, Any]:
    return {
        "date_field": "target_date",
        "ranges": [dict(item) for item in EVALUATION_EXCLUDED_TARGET_RANGES],
        "raw_row_count": int(raw_count),
        "included_row_count": int(included_count),
        "excluded_row_count": int(raw_count - included_count),
    }


def _date_in_excluded_ranges(value: str) -> bool:
    for item in EVALUATION_EXCLUDED_TARGET_RANGES:
        if item["start"] <= value <= item["end"]:
            return True
    return False


def run_t5_reproduction(engine: Engine | None = None, db_aligned: pd.DataFrame | None = None, n_jobs: int = 4) -> list[RunOutput]:
    """生成 t5 canonical-csv、framework-csv 和 framework-db 三组输出。"""
    csv_df = read_daily_csv()
    if db_aligned is None:
        _, db_aligned = build_db_aligned_daily(
            csv_df,
            engine=engine,
            upstream_mode=True,
            artifact_scheme_id="t5_daily",
        )

    baseline_rows, baseline_path = run_t5_canonical_csv_baseline(n_jobs=n_jobs)
    baseline = make_run_output("t5_daily", "baseline_original_csv", T5_BACKTEST_START, T5_BACKTEST_END, baseline_rows, report_path=baseline_path)
    framework_csv = make_run_output(
        "t5_daily",
        "framework_original_csv",
        T5_BACKTEST_START,
        T5_BACKTEST_END,
        run_t5_framework_backtest(csv_df, n_jobs=n_jobs),
    )
    framework_db = make_run_output(
        "t5_daily",
        "framework_db_aligned",
        T5_BACKTEST_START,
        T5_BACKTEST_END,
        run_t5_framework_backtest(db_aligned, n_jobs=n_jobs),
    )
    framework_csv.summary["comparison"] = compare_prediction_rows(
        baseline.rows,
        framework_csv.rows,
        fields=("label", "predicted_direction", "model_pred"),
    )
    framework_db.summary["comparison"] = compare_prediction_rows(
        baseline.rows,
        framework_db.rows,
        fields=("label", "predicted_direction", "model_pred"),
    )
    for output in (baseline, framework_csv, framework_db):
        output.summary["expected_report"] = EXPECTED_T5_REPORT
        output.summary["report_reproduction"] = compare_t5_expected_report(output.summary.get("periods_by_tenor", {}))
    return [baseline, framework_csv, framework_db]


def run_t5_canonical_csv_baseline(n_jobs: int = 4) -> tuple[list[dict[str, Any]], str]:
    daily = read_daily_csv()
    return run_t5_framework_backtest(daily, n_jobs=n_jobs), str(CANONICAL_DAILY)


def run_t5_framework_backtest(df: pd.DataFrame, n_jobs: int = 4) -> list[dict[str, Any]]:
    from schemes.t5_daily.latest import TENOR_MODULES, _build_features
    from schemes.t5_daily.core.common_utils import build_fallback_signal, choose_threshold, make_labels

    daily = df.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.normalize()
    daily = daily.sort_values("date").reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for tenor in ("3Y", "5Y", "7Y", "10Y"):
        module = TENOR_MODULES[tenor]
        close = pd.to_numeric(daily[module.CLOSE_COL], errors="coerce")
        _, labels = make_labels(daily, module.CLOSE_COL, horizon=module.HORIZON)
        features = _build_features(module, daily)
        fallback_signal = build_fallback_signal(daily, module.CLOSE_COL)
        vote_df = module.build_custom_vote_signals(daily, module.RECIPE)
        test_idx = np.flatnonzero(
            (daily["date"] >= pd.Timestamp(T5_BACKTEST_START))
            & (daily["date"] <= pd.Timestamp(T5_BACKTEST_END))
            & pd.Series(labels).notna().to_numpy()
            & close.notna().to_numpy()
        )
        for idx in test_idx:
            row = _run_t5_single_index(module, daily, labels, close, features, fallback_signal, vote_df, idx, n_jobs)
            rows.append(row)
    return rows


def _run_t5_single_index(module: Any, daily: pd.DataFrame, labels: np.ndarray, close: pd.Series, features: pd.DataFrame, fallback_signal: pd.Series, vote_df: pd.DataFrame, idx: int, n_jobs: int) -> dict[str, Any]:
    import lightgbm as lgb

    eligible = np.flatnonzero(
        (np.arange(len(daily)) < idx - module.GAP)
        & np.isin(labels, [-1.0, 1.0])
        & close.notna().to_numpy()
    )
    if len(eligible) > module.WINDOW:
        eligible = eligible[-module.WINDOW :]

    prob = None
    threshold = None
    if len(eligible) < 120 or len(np.unique(labels[eligible])) < 2:
        base_pred = int(fallback_signal.iloc[idx])
        decision = "cold_fallback"
    else:
        split = max(80, int(len(eligible) * module.SPLIT_PCT))
        fit_idx = eligible[:split]
        cal_idx = eligible[split:]
        model = lgb.LGBMClassifier(
            objective="binary",
            metric="binary_logloss",
            num_leaves=module.NUM_LEAVES,
            learning_rate=module.LEARNING_RATE,
            n_estimators=module.N_ESTIMATORS,
            min_child_samples=module.MIN_CHILD_SAMPLES,
            reg_alpha=module.REG_ALPHA,
            reg_lambda=module.REG_LAMBDA,
            subsample=0.85,
            colsample_bytree=0.90,
            n_jobs=n_jobs,
            verbosity=-1,
            random_state=42,
            force_col_wise=True,
        )
        model.fit(
            features.iloc[fit_idx],
            (labels[fit_idx] == 1).astype(int),
            eval_set=[(features.iloc[cal_idx], (labels[cal_idx] == 1).astype(int))],
            callbacks=[lgb.early_stopping(20, verbose=False)],
        )
        prob = float(model.predict_proba(features.iloc[[idx]])[:, 1][0])
        cal_prob = model.predict_proba(features.iloc[cal_idx])[:, 1]
        from schemes.t5_daily.core.common_utils import choose_threshold

        threshold = choose_threshold(cal_prob, labels[cal_idx])
        base_pred = 1 if prob >= threshold else -1
        decision = "model"

    vote_signals = {str(col): int(vote_df.iloc[idx][col]) for col in vote_df.columns}
    signal_sum = sum(vote_signals.values())
    vote_sum = int(base_pred + signal_sum)
    if module.VOTE_THRESHOLD > 0:
        if vote_sum > module.VOTE_THRESHOLD:
            pred = 1
        elif vote_sum < -module.VOTE_THRESHOLD:
            pred = -1
        else:
            pred = base_pred
    else:
        pred = 1 if vote_sum >= 0 else -1

    feature_date = daily.loc[idx, "date"].strftime("%Y-%m-%d")
    target_idx = idx + int(module.HORIZON)
    target_date = daily.loc[target_idx, "date"].strftime("%Y-%m-%d") if target_idx < len(daily) else None
    return {
        "benchmark_id": BENCHMARK_ID,
        "scheme_id": "t5_daily",
        "target_tenor": module.TENOR,
        "horizon": int(module.HORIZON),
        "predict_date": feature_date,
        "feature_date": feature_date,
        "target_date": target_date,
        "label": _int_or_none(labels[idx]),
        "predicted_direction": int(pred),
        "model_pred": int(base_pred),
        "confidence": prob,
        "source_row": {
            "date": feature_date,
            "true_label": _int_or_none(labels[idx]),
            "model_pred": int(base_pred),
            "vote_pred": int(pred),
        },
        "extra": {
            "threshold": threshold,
            "vote_sum": vote_sum,
            "signal_sum": int(signal_sum),
            "decision": decision,
            "vote_signals": vote_signals,
        },
    }


def run_t1_reproduction(engine: Engine | None = None, db_aligned: pd.DataFrame | None = None) -> list[RunOutput]:
    """生成 t1 canonical-csv、framework-csv 和 framework-db 三组输出。"""
    csv_df = read_daily_csv()
    if db_aligned is None:
        _, db_aligned = build_db_aligned_daily(
            csv_df,
            engine=engine,
            upstream_mode=True,
            artifact_scheme_id="t1_daily",
        )

    baseline_rows, baseline_path = run_t1_canonical_csv_baseline(csv_df)
    baseline = make_run_output("t1_daily", "baseline_original_csv", T1_BACKTEST_START, T1_BACKTEST_END, baseline_rows, report_path=baseline_path)
    framework_csv = make_run_output(
        "t1_daily",
        "framework_original_csv",
        T1_BACKTEST_START,
        T1_BACKTEST_END,
        run_t1_framework_backtest(csv_df),
    )
    framework_db = make_run_output(
        "t1_daily",
        "framework_db_aligned",
        T1_BACKTEST_START,
        T1_BACKTEST_END,
        run_t1_framework_backtest(db_aligned),
    )
    fields = ("label", "predicted_direction", "model_pred")
    framework_csv.summary["comparison"] = compare_prediction_rows(baseline.rows, framework_csv.rows, fields=fields, float_fields=("confidence",), float_tolerance=1e-6)
    framework_db.summary["comparison"] = compare_prediction_rows(baseline.rows, framework_db.rows, fields=fields, float_fields=("confidence",), float_tolerance=1e-6)
    return [baseline, framework_csv, framework_db]


def run_t1_canonical_csv_baseline(daily_df: pd.DataFrame) -> tuple[list[dict[str, Any]], str]:
    return run_t1_framework_backtest(daily_df), str(CANONICAL_DAILY)


def run_t1_framework_backtest(df: pd.DataFrame) -> list[dict[str, Any]]:
    from schemes.t1_daily.core.config import TENOR_CONFIGS
    from schemes.t1_daily.core.lgbm_predictor import predict_latest_for_config

    daily = df.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.normalize()
    daily = daily.sort_values("date").reset_index(drop=True)
    dates = daily.loc[daily["date"].between(pd.Timestamp(T1_BACKTEST_START), pd.Timestamp(T1_BACKTEST_END)), "date"].dt.strftime("%Y-%m-%d").tolist()
    rows: list[dict[str, Any]] = []
    for run_date in dates:
        for frequency in ("D1Y", "D5Y", "D10Y"):
            result = predict_latest_for_config(daily, TENOR_CONFIGS[frequency], current_date=run_date)
            rows.append(_t1_prediction_result_to_row(daily, result))
    return rows


def _t1_prediction_result_to_row(daily_df: pd.DataFrame, result: Any) -> dict[str, Any]:
    config = result.config
    dates = pd.to_datetime(daily_df["date"]).dt.normalize()
    feature_idx = int(dates[dates.eq(pd.Timestamp(result.feature_date))].index[-1])
    target_matches = dates[dates.eq(pd.Timestamp(result.target_date))]
    close = pd.to_numeric(daily_df[config.close_col], errors="coerce")
    if len(target_matches):
        target_idx = int(target_matches.index[-1])
        future_return = close.iloc[target_idx] / close.iloc[feature_idx] - 1.0
        if future_return > config.threshold:
            label = 1
        elif future_return < -config.threshold:
            label = -1
        else:
            label = 0
    else:
        future_return = None
        label = None
    return {
        "benchmark_id": BENCHMARK_ID,
        "scheme_id": "t1_daily",
        "target_tenor": result.tenor,
        "horizon": 1,
        "predict_date": result.target_date,
        "feature_date": result.feature_date,
        "target_date": result.target_date,
        "label": label,
        "predicted_direction": int(result.pred_label),
        "model_pred": int(result.base_pred),
        "confidence": float(result.prob_up),
        "source_row": {
            "date": result.target_date,
            "target_date": result.target_date,
            "feature_date": result.feature_date,
            "frequency": result.frequency,
            "tenor": result.tenor,
            "future_return": future_return,
            "label": label,
            "pred_label": int(result.pred_label),
            "prob_up": float(result.prob_up),
            "base_pred": int(result.base_pred),
        },
        "extra": {
            "frequency": result.frequency,
            "threshold_used": result.threshold_used,
            "base_decision": result.base_decision,
            "vote_sum": result.vote_sum,
            "decision": result.decision,
            "train_start": result.train_start,
            "train_end": result.train_end,
            "feature_count": len(result.feature_columns),
        },
    }


def make_run_output(
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]],
    report_path: str | None = None,
) -> RunOutput:
    filtered_rows, excluded_rows = apply_evaluation_exclusions(rows)
    monthly = build_monthly_metrics(filtered_rows)
    summary = build_summary(filtered_rows)
    summary["row_count"] = len(filtered_rows)
    summary["raw_row_count"] = len(rows)
    summary["excluded_row_count"] = len(excluded_rows)
    summary["evaluation_filter"] = evaluation_exclusion_summary(len(rows), len(filtered_rows))
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


def build_monthly_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        month = _metric_month(row)
        grouped.setdefault((row["target_tenor"], month), []).append(row)
    metrics: list[dict[str, Any]] = []
    for (tenor, month), items in sorted(grouped.items()):
        metrics.append(metric_row(items, tenor, month))
    return metrics


def _metric_month(row: dict[str, Any]) -> str:
    """返回历史回测月度指标归属月份。"""
    if _is_weekly_backtest_row(row):
        return str(row.get("feature_date") or row["predict_date"])[:7]
    return str(row["predict_date"])[:7]


def _is_weekly_backtest_row(row: dict[str, Any]) -> bool:
    extra = row.get("extra") or {}
    frequency = extra.get("frequency") if isinstance(extra, dict) else None
    if str(frequency or "").lower() == "weekly":
        return True
    try:
        return int(row.get("horizon") or 0) == 6
    except (TypeError, ValueError):
        return False


def metric_row(rows: list[dict[str, Any]], tenor: str, month: str) -> dict[str, Any]:
    valid = [row for row in rows if row.get("label") is not None and row.get("predicted_direction") is not None]
    total = len(valid)
    correct = sum(1 for row in valid if int(row["label"]) == int(row["predicted_direction"]))
    pred_up = sum(1 for row in valid if row["predicted_direction"] == 1)
    pred_down = sum(1 for row in valid if row["predicted_direction"] == -1)
    actual_up = sum(1 for row in valid if row["label"] == 1)
    actual_down = sum(1 for row in valid if row["label"] == -1)
    return {
        "benchmark_id": BENCHMARK_ID,
        "scheme_id": valid[0]["scheme_id"] if valid else rows[0]["scheme_id"],
        "target_tenor": tenor,
        "horizon": int(rows[0]["horizon"]),
        "month": month,
        "sample_count": total,
        "correct_count": correct,
        "accuracy": safe_div(correct, total),
        "up_precision": safe_div(sum(1 for row in valid if row["predicted_direction"] == 1 and row["label"] == 1), pred_up),
        "up_recall": safe_div(sum(1 for row in valid if row["predicted_direction"] == 1 and row["label"] == 1), actual_up),
        "down_precision": safe_div(sum(1 for row in valid if row["predicted_direction"] == -1 and row["label"] == -1), pred_down),
        "down_recall": safe_div(sum(1 for row in valid if row["predicted_direction"] == -1 and row["label"] == -1), actual_down),
        "actual_dist": direction_dist(valid, "label"),
        "predicted_dist": direction_dist(valid, "predicted_direction"),
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
    correct = sum(1 for row in valid if row["label"] == row["predicted_direction"])
    return {
        "samples": len(valid),
        "correct": correct,
        "accuracy": safe_div(correct, len(valid)),
        "accuracy_pct": round(safe_div(correct, len(valid)) * 100, 1) if valid else None,
        "actual_dist": direction_dist(valid, "label"),
        "predicted_dist": direction_dist(valid, "predicted_direction"),
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


def compare_t5_expected_report(periods_by_tenor: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for tenor, expected in EXPECTED_T5_REPORT.items():
        actual_periods = periods_by_tenor.get(tenor, {})
        actual_all = actual_periods.get("all", {})
        results[tenor] = {
            "all_match": actual_all.get("samples") == expected["all"]["samples"]
            and actual_all.get("correct") == expected["all"]["correct"]
            and actual_all.get("accuracy_pct") == expected["all"]["accuracy_pct"],
            "sample_count_match": {
                "sim": (actual_periods.get("sim") or {}).get("samples") == expected["sim_n"],
                "real": (actual_periods.get("real") or {}).get("samples") == expected["real_n"],
                "may": (actual_periods.get("may") or {}).get("samples") == expected["may_n"],
            },
            "actual": actual_periods,
            "expected": expected,
        }
    return results


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


def persist_run_output(engine: Engine, output: RunOutput) -> int:
    run_id = upsert_backtest_run(
        engine,
        benchmark_id=BENCHMARK_ID,
        scheme_id=output.scheme_id,
        data_source=output.data_source,
        start_date=output.start_date,
        end_date=output.end_date,
        status="success",
        summary=output.summary,
        report_path=output.report_path,
    )
    replace_backtest_predictions(engine, run_id, output.rows)
    replace_backtest_monthly_metrics(engine, run_id, output.monthly_metrics)
    output.summary["run_id"] = run_id
    upsert_backtest_run(
        engine,
        benchmark_id=BENCHMARK_ID,
        scheme_id=output.scheme_id,
        data_source=output.data_source,
        start_date=output.start_date,
        end_date=output.end_date,
        status="success",
        summary=output.summary,
        report_path=output.report_path,
    )
    return run_id


def run_reproduction(include_t1: bool = True, include_t5: bool = True, n_jobs: int = 4, persist: bool = True) -> dict[str, Any]:
    started = time.time()
    engine = create_sqlalchemy_engine()
    try:
        data_check = run_data_alignment_check(engine=engine, persist=persist)
        outputs: list[RunOutput] = []
        if include_t5:
            outputs.extend(run_t5_reproduction(engine=engine, n_jobs=n_jobs))
        if include_t1:
            outputs.extend(run_t1_reproduction(engine=engine))
        run_ids = []
        if persist:
            for output in outputs:
                run_ids.append(persist_run_output(engine, output))
        return {
            "benchmark_id": BENCHMARK_ID,
            "data_check_status": data_check["status"],
            "run_ids": run_ids,
            "runs": [
                {
                    "scheme_id": output.scheme_id,
                    "data_source": output.data_source,
                    "rows": len(output.rows),
                    "summary": output.summary,
                }
                for output in outputs
            ],
            "elapsed_sec": round(time.time() - started, 3),
        }
    finally:
        engine.dispose()


def _frame_profile(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "date_min": df["date"].min().strftime("%Y-%m-%d") if len(df) else None,
        "date_max": df["date"].max().strftime("%Y-%m-%d") if len(df) else None,
    }


def direction_dist(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return {
        "up": sum(1 for row in rows if row.get(key) == 1),
        "down": sum(1 for row in rows if row.get(key) == -1),
        "flat": sum(1 for row in rows if row.get(key) == 0),
        "missing": sum(1 for row in rows if row.get(key) is None),
    }


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


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run historical benchmark reproduction checks.")
    parser.add_argument("--skip-t1", action="store_true")
    parser.add_argument("--skip-t5", action="store_true")
    parser.add_argument("--n-jobs", type=int, default=4)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args(argv)
    result = run_reproduction(
        include_t1=not args.skip_t1,
        include_t5=not args.skip_t5,
        n_jobs=args.n_jobs,
        persist=not args.no_persist,
    )
    print(json.dumps(clean_json(result), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
