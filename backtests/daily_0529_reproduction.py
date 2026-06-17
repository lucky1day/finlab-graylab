from __future__ import annotations

import argparse
import ast
import json
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import (
    BacktestSpec,
    RunOutput,
    _float_or_none as _base_float_or_none,
    _frame_profile as _base_frame_profile,
    _int_or_none as _base_int_or_none,
    _metric_month as _base_metric_month,
    _normalize_scalar as _base_normalize_scalar,
    aggregate_rows as _base_aggregate_rows,
    apply_evaluation_exclusions as _base_apply_evaluation_exclusions,
    build_db_aligned_daily as _base_build_db_aligned_daily,
    build_framework_db_aligned_daily as _base_build_framework_db_aligned_daily,
    build_monthly_metrics as _base_build_monthly_metrics,
    build_summary as _base_build_summary,
    compare_daily_frames as _base_compare_daily_frames,
    compare_generated_frames,
    compare_prediction_rows as _base_compare_prediction_rows,
    direction_dist as _base_direction_dist,
    evaluation_exclusion_summary as _base_evaluation_exclusion_summary,
    exclude_daily_rows_for_evaluation_week as _base_exclude_daily_rows_for_evaluation_week,
    infer_target_date as _base_infer_target_date,
    make_run_output as _base_make_run_output,
    metric_row as _base_metric_row,
    period_summaries as _base_period_summaries,
    persist_run_output as _base_persist_run_output,
    read_daily_csv as _base_read_daily_csv,
    safe_div as _base_safe_div,
)
from backtests.repository import (
    clean_json,
    insert_reproduction_check,
)
from shared.artifact_paths import benchmark_data_check_root, benchmark_input_root
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import build_daily_input_artifact


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ID = "model_muti_0529"
CANONICAL_DAILY = PROJECT_ROOT / "benchmarks" / "model_muti_0529" / "daily_output.csv"
DATA_CHECK_ROOT = benchmark_data_check_root(BENCHMARK_ID)
TARGET_COLUMNS = ("TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
UPSTREAM_DAILY_TARGETS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")
T5_BACKTEST_START = "2025-01-01"
T5_BACKTEST_END = "2026-05-31"
T1_BACKTEST_START = "2025-01-01"
T1_BACKTEST_END = "2026-05-29"
LIVE_TARGET_START_DATE = "2026-06-01"
DAILY0529_REQUIRED_TARGET_END_DATE = "2026-05-29"
DAILY0529_DB_INPUT_START_DATE = "2010-07-27"
T1_CONFIG_PATH = PROJECT_ROOT / "schemes" / "t1_daily" / "config.yaml"
EVALUATION_EXCLUDED_TARGET_RANGES: tuple[dict[str, str], ...] = ()

EXPECTED_T5_REPORT: dict[str, dict[str, Any]] = {
    "3Y": {"all": {"samples": 333, "correct": 231, "accuracy_pct": 69.4}, "sim_n": 117, "real_n": 203, "may_n": 13},
    "5Y": {"all": {"samples": 333, "correct": 213, "accuracy_pct": 64.0}, "sim_n": 117, "real_n": 203, "may_n": 13},
    "7Y": {"all": {"samples": 333, "correct": 218, "accuracy_pct": 65.5}, "sim_n": 117, "real_n": 203, "may_n": 13},
    "10Y": {"all": {"samples": 333, "correct": 211, "accuracy_pct": 63.4}, "sim_n": 117, "real_n": 203, "may_n": 13},
}


T1_DAILY_SPEC = BacktestSpec(
    benchmark_id=BENCHMARK_ID,
    scheme_id="t1_daily",
    canonical_csv=CANONICAL_DAILY,
    target_columns=TARGET_COLUMNS,
    start_date=T1_BACKTEST_START,
    end_date=T1_BACKTEST_END,
    excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES,
)
T5_DAILY_SPEC = BacktestSpec(
    benchmark_id=BENCHMARK_ID,
    scheme_id="t5_daily",
    canonical_csv=CANONICAL_DAILY,
    target_columns=TARGET_COLUMNS,
    start_date=T5_BACKTEST_START,
    end_date=T5_BACKTEST_END,
    expected_report=EXPECTED_T5_REPORT,
    excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES,
)


def read_daily_csv(path: str | Path = CANONICAL_DAILY) -> pd.DataFrame:
    """读取并标准化 historical daily_output。"""
    return _base_read_daily_csv(path)


def build_daily0529_benchmark_frame(
    csv_df: pd.DataFrame | None = None,
    engine: Engine | None = None,
    *,
    artifact_builder: Any = build_daily_input_artifact,
) -> pd.DataFrame:
    """返回 T1/T5 benchmark 使用的日频输入。

    根目录 canonical CSV 只到 2026-05-28；为覆盖 2026-05 全部 target 交易日，
    需要从 DB 补 2026-05-29 这一目标验证日。补齐行只用于计算 label/actual，
    不把 feature_date 推到灰度区。
    """
    daily = (csv_df.copy() if csv_df is not None else read_daily_csv()).copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce").dt.normalize()
    daily = daily.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    required_end = pd.Timestamp(DAILY0529_REQUIRED_TARGET_END_DATE)
    current_end = daily["date"].max()
    if current_end >= required_end:
        return daily

    input_artifact = artifact_builder(
        scheme_id="daily0529_target_month_completion",
        predict_date=DAILY0529_REQUIRED_TARGET_END_DATE,
        start_date=daily["date"].min().strftime("%Y-%m-%d"),
        end_date=DAILY0529_REQUIRED_TARGET_END_DATE,
        engine=engine,
        output_root=benchmark_input_root(BENCHMARK_ID),
    )
    db_df = input_artifact.dataframe.copy()
    db_df["date"] = pd.to_datetime(db_df["date"], errors="coerce").dt.normalize()
    db_df = db_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    extra_rows = db_df.loc[(db_df["date"] > current_end) & (db_df["date"] <= required_end)]
    if extra_rows.empty:
        raise ValueError(f"DB daily input missing required target completion date {DAILY0529_REQUIRED_TARGET_END_DATE}")
    missing_target_columns = [col for col in TARGET_COLUMNS if col not in extra_rows.columns]
    if missing_target_columns:
        raise ValueError(f"DB daily input missing target columns for target completion: {missing_target_columns}")
    extra_rows = extra_rows.reindex(columns=daily.columns)
    required_row = extra_rows.loc[extra_rows["date"].eq(required_end)]
    if required_row.empty:
        raise ValueError(f"DB daily input missing required target completion date {DAILY0529_REQUIRED_TARGET_END_DATE}")
    null_target_columns = [
        col for col in TARGET_COLUMNS
        if col in required_row.columns and pd.isna(required_row.iloc[0][col])
    ]
    if null_target_columns:
        raise ValueError(f"DB daily input has null target values for target completion: {null_target_columns}")
    return pd.concat([daily, extra_rows], ignore_index=True)


def build_daily0529_db_input_frame(
    engine: Engine | None = None,
    artifact_scheme_id: str = "daily0529_db_first",
) -> pd.DataFrame:
    """从 DB-first input artifact 生成 0529 日频回测输入。"""
    input_artifact = build_daily_input_artifact(
        scheme_id=artifact_scheme_id,
        predict_date=DAILY0529_REQUIRED_TARGET_END_DATE,
        start_date=DAILY0529_DB_INPUT_START_DATE,
        end_date=DAILY0529_REQUIRED_TARGET_END_DATE,
        engine=engine,
        output_root=benchmark_input_root(BENCHMARK_ID),
    )
    daily = input_artifact.dataframe.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce").dt.normalize()
    return daily.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def build_db_aligned_daily(
    csv_df: pd.DataFrame | None = None,
    engine: Engine | None = None,
    upstream_mode: bool = True,
    artifact_scheme_id: str = "daily_common",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成完整 DB 版 daily_output 和按 canonical CSV 对齐后的版本。

    历史复现的 DB 输入统一经过 shared.input_artifacts 生成和读回，
    再喂给算法。upstream_mode 保留为旧调用兼容参数，不再绕过统一输入层。
    """
    return _base_build_db_aligned_daily(
        csv_df=csv_df,
        engine=engine,
        upstream_mode=upstream_mode,
        artifact_scheme_id=artifact_scheme_id,
        benchmark_id=BENCHMARK_ID,
        canonical_csv=CANONICAL_DAILY,
        artifact_builder=build_daily_input_artifact,
    )


def build_framework_db_aligned_daily(csv_df: pd.DataFrame | None = None, engine: Engine | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成当前框架 data_service 默认口径的 DB daily_output，用于额外对照。"""
    return _base_build_framework_db_aligned_daily(
        csv_df=csv_df,
        engine=engine,
        benchmark_id=BENCHMARK_ID,
        canonical_csv=CANONICAL_DAILY,
        artifact_builder=build_daily_input_artifact,
    )


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
            "primary": "shared_data_service_daily",
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
    return _base_compare_daily_frames(csv_df, db_full, db_aligned, target_columns=TARGET_COLUMNS)


def exclude_daily_rows_for_evaluation_week(csv_df: pd.DataFrame, db_aligned: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从数据一致性报告的有效口径中排除暂不验证的目标周日期。"""
    return _base_exclude_daily_rows_for_evaluation_week(
        csv_df,
        db_aligned,
        excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES,
    )


def apply_evaluation_exclusions(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """排除暂不纳入历史验证的目标日期样本。"""
    return _base_apply_evaluation_exclusions(rows, excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES)


def evaluation_exclusion_summary(raw_count: int, included_count: int) -> dict[str, Any]:
    return _base_evaluation_exclusion_summary(
        raw_count,
        included_count,
        excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES,
    )


def _date_in_excluded_ranges(value: str) -> bool:
    return any(item["start"] <= value <= item["end"] for item in EVALUATION_EXCLUDED_TARGET_RANGES)


def run_t5_reproduction(
    engine: Engine | None = None,
    db_aligned: pd.DataFrame | None = None,
    n_jobs: int = 4,
    *,
    include_source_evidence: bool = False,
) -> list[RunOutput]:
    """生成 t5 DB-first 输出；显式 source evidence 模式才生成 CSV 对照。"""
    csv_df = build_daily0529_benchmark_frame(engine=engine) if include_source_evidence else None
    if db_aligned is None:
        if include_source_evidence:
            _, db_aligned = build_db_aligned_daily(
                csv_df,
                engine=engine,
                upstream_mode=True,
                artifact_scheme_id="t5_daily",
            )
        else:
            db_aligned = build_daily0529_db_input_frame(engine=engine, artifact_scheme_id="t5_daily")

    framework_db = make_run_output(
        "t5_daily",
        "framework_db_aligned",
        T5_BACKTEST_START,
        T5_BACKTEST_END,
        run_t5_framework_backtest(db_aligned, n_jobs=n_jobs),
    )
    framework_db.summary["expected_report"] = EXPECTED_T5_REPORT
    framework_db.summary["report_reproduction"] = compare_t5_expected_report(
        framework_db.summary.get("periods_by_tenor", {})
    )
    if not include_source_evidence:
        return [framework_db]

    baseline_rows, baseline_path = run_t5_canonical_csv_baseline(n_jobs=n_jobs)
    baseline = make_run_output(
        "t5_daily",
        "baseline_original_csv",
        T5_BACKTEST_START,
        T5_BACKTEST_END,
        baseline_rows,
        report_path=baseline_path,
    )
    framework_csv = make_run_output(
        "t5_daily",
        "framework_original_csv",
        T5_BACKTEST_START,
        T5_BACKTEST_END,
        run_t5_framework_backtest(csv_df, n_jobs=n_jobs),
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
    for output in (baseline, framework_csv):
        output.summary["expected_report"] = EXPECTED_T5_REPORT
        output.summary["report_reproduction"] = compare_t5_expected_report(output.summary.get("periods_by_tenor", {}))
    return [baseline, framework_csv, framework_db]


def run_t5_canonical_csv_baseline(n_jobs: int = 4) -> tuple[list[dict[str, Any]], str]:
    daily = build_daily0529_benchmark_frame()
    return run_t5_framework_backtest(daily, n_jobs=n_jobs), str(CANONICAL_DAILY)


def run_t5_framework_backtest(df: pd.DataFrame, n_jobs: int = 4) -> list[dict[str, Any]]:
    from schemes.t5_daily.latest_prediction import TENOR_MODULES, _build_features
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
            target_date = row.get("target_date")
            if target_date is None or str(target_date) >= LIVE_TARGET_START_DATE:
                continue
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


def run_t1_reproduction(
    engine: Engine | None = None,
    db_aligned: pd.DataFrame | None = None,
    *,
    include_source_evidence: bool = False,
) -> list[RunOutput]:
    """生成 t1 DB-first 输出；显式 source evidence 模式才生成 CSV 对照。"""
    csv_df = build_daily0529_benchmark_frame(engine=engine) if include_source_evidence else None
    if db_aligned is None:
        if include_source_evidence:
            _, db_aligned = build_db_aligned_daily(
                csv_df,
                engine=engine,
                upstream_mode=True,
                artifact_scheme_id="t1_daily",
            )
        else:
            db_aligned = build_daily0529_db_input_frame(engine=engine, artifact_scheme_id="t1_daily")

    framework_db = make_run_output(
        "t1_daily",
        "framework_db_aligned",
        T1_BACKTEST_START,
        T1_BACKTEST_END,
        run_t1_framework_backtest(db_aligned),
    )
    fields = ("label", "predicted_direction", "model_pred")
    if not include_source_evidence:
        return [framework_db]

    baseline_rows, baseline_path = run_t1_canonical_csv_baseline(csv_df)
    baseline = make_run_output(
        "t1_daily",
        "baseline_original_csv",
        T1_BACKTEST_START,
        T1_BACKTEST_END,
        baseline_rows,
        report_path=baseline_path,
    )
    framework_csv = make_run_output(
        "t1_daily",
        "framework_original_csv",
        T1_BACKTEST_START,
        T1_BACKTEST_END,
        run_t1_framework_backtest(csv_df),
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
    target_dates = daily.loc[
        daily["date"].between(pd.Timestamp(T1_BACKTEST_START), pd.Timestamp(T1_BACKTEST_END)),
        "date",
    ].dt.strftime("%Y-%m-%d").tolist()
    configured_tenors = _configured_t1_tenors()
    rows: list[dict[str, Any]] = []
    for target_date in target_dates:
        for frequency in ("D1Y", "D5Y", "D10Y"):
            if TENOR_CONFIGS[frequency].tenor not in configured_tenors:
                continue
            result = predict_latest_for_config(daily, TENOR_CONFIGS[frequency], target_date=target_date)
            row = _t1_prediction_result_to_row(daily, result)
            if row["predict_date"] < T1_BACKTEST_START:
                continue
            rows.append(row)
    return rows


def _configured_t1_tenors() -> set[str]:
    for line in T1_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("tenors:"):
            raw_value = line.split(":", 1)[1].strip()
            return {str(item) for item in ast.literal_eval(raw_value)}
    return set()


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
        "predict_date": result.feature_date,
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
    return _base_make_run_output(
        scheme_id=scheme_id,
        data_source=data_source,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        benchmark_id=BENCHMARK_ID,
        excluded_target_ranges=EVALUATION_EXCLUDED_TARGET_RANGES,
        report_path=report_path,
    )


def build_monthly_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _base_build_monthly_metrics(rows, benchmark_id=BENCHMARK_ID)


def _metric_month(row: dict[str, Any]) -> str:
    """返回历史回测月度指标归属月份。"""
    return _base_metric_month(row)


def metric_row(rows: list[dict[str, Any]], tenor: str, month: str) -> dict[str, Any]:
    return _base_metric_row(rows, tenor, month, benchmark_id=BENCHMARK_ID)


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _base_build_summary(rows)


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _base_aggregate_rows(rows)


def period_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _base_period_summaries(rows)


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
    return _base_compare_prediction_rows(
        baseline,
        candidate,
        fields=fields,
        float_fields=float_fields,
        float_tolerance=float_tolerance,
    )


def persist_run_output(engine: Engine, output: RunOutput) -> int:
    return _base_persist_run_output(engine, output, benchmark_id=BENCHMARK_ID)


def run_daily_0529_reproduction(
    include_t1: bool = True,
    include_t5: bool = True,
    n_jobs: int = 4,
    persist: bool = True,
    *,
    include_source_evidence: bool = False,
) -> dict[str, Any]:
    started = time.time()
    engine = create_sqlalchemy_engine()
    try:
        data_check = (
            run_data_alignment_check(engine=engine, persist=persist)
            if include_source_evidence
            else {"status": "skipped_source_evidence"}
        )
        outputs: list[RunOutput] = []
        if include_t5:
            outputs.extend(
                run_t5_reproduction(
                    engine=engine,
                    n_jobs=n_jobs,
                    include_source_evidence=include_source_evidence,
                )
            )
        if include_t1:
            outputs.extend(
                run_t1_reproduction(
                    engine=engine,
                    include_source_evidence=include_source_evidence,
                )
            )
        if not include_source_evidence:
            outputs = [output for output in outputs if output.data_source == "framework_db_aligned"]
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
    return _base_frame_profile(df)


def direction_dist(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return _base_direction_dist(rows, key)


def safe_div(num: int, den: int) -> float | None:
    return _base_safe_div(num, den)


def infer_target_date(daily: pd.DataFrame, feature_date: str, horizon: int) -> str | None:
    return _base_infer_target_date(daily, feature_date, horizon)


def _normalize_scalar(value: Any) -> Any:
    return _base_normalize_scalar(value)


def _int_or_none(value: Any) -> int | None:
    return _base_int_or_none(value)


def _float_or_none(value: Any) -> float | None:
    return _base_float_or_none(value)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run daily 0529 historical benchmark reproduction checks.")
    parser.add_argument("--skip-t1", action="store_true")
    parser.add_argument("--skip-t5", action="store_true")
    parser.add_argument("--n-jobs", type=int, default=4)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--include-source-evidence", action="store_true")
    args = parser.parse_args(argv)
    result = run_daily_0529_reproduction(
        include_t1=not args.skip_t1,
        include_t5=not args.skip_t5,
        n_jobs=args.n_jobs,
        persist=not args.no_persist,
        include_source_evidence=args.include_source_evidence,
    )
    print(json.dumps(clean_json(result), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
