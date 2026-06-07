from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from . import legacy_weekly_10y_d_overlay_0529 as legacy10
from .date_utils import (
    get_week_id_for_date as date_to_week_id,
    next_week_id,
    week_id_to_friday,
    week_id_to_monday,
)


@dataclass
class WeeklyPredictionResult:
    rdate: str
    frequency: str
    tenor: str
    pred_label: int
    prob_up: float
    week_id: int
    prediction_column: str
    probability_column: str
    source: str
    predictions: pd.DataFrame


def normalize_weekly_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "week_id" not in df.columns:
        raise ValueError("weekly dataframe must contain week_id")

    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    df = df.sort_values("week_id").reset_index(drop=True)
    for col in df.columns:
        if col not in {"week_id", "date", "week_date", "month"}:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "date" not in df.columns:
        df["date"] = df["week_id"].map(week_id_to_monday)
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "week_date" not in df.columns:
        df["week_date"] = df["week_id"].map(week_id_to_friday)
    else:
        df["week_date"] = pd.to_datetime(df["week_date"], errors="coerce")
    return df


def _legacy10_input_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    for col in ("date", "week_date", "month", "month_date"):
        if col in df.columns:
            df = df.drop(columns=[col])
    if "week_id" not in df.columns:
        raise ValueError("weekly dataframe must contain week_id")
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    df = df.sort_values("week_id").reset_index(drop=True)
    for col in df.columns:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _truncate_for_target_week(weekly_df: pd.DataFrame, target_week_id: int) -> pd.DataFrame:
    weekly = normalize_weekly_frame(weekly_df)
    weekly = weekly[weekly["week_id"].astype(int) <= int(target_week_id)].copy()
    if weekly.empty or not weekly["week_id"].astype(int).eq(int(target_week_id)).any():
        raise ValueError(f"target week_id {target_week_id} not found in weekly dataframe")
    return weekly.sort_values("week_id").reset_index(drop=True)


def _append_synthetic_next_week(weekly_df: pd.DataFrame, target_week_id: int) -> pd.DataFrame:
    """为生产周预测追加内存中的下一周行，避免原回测脚本丢掉最新特征周。"""
    weekly = weekly_df.copy().sort_values("week_id").reset_index(drop=True)
    if int(weekly["week_id"].max()) > int(target_week_id):
        return weekly
    target_rows = weekly[weekly["week_id"].astype(int).eq(int(target_week_id))]
    if target_rows.empty:
        raise ValueError(f"target week_id {target_week_id} not found in weekly dataframe")
    synthetic = target_rows.iloc[[-1]].copy()
    synthetic["week_id"] = next_week_id(int(target_week_id))
    if "date" in synthetic.columns:
        synthetic["date"] = synthetic["week_id"].map(week_id_to_monday)
    if "week_date" in synthetic.columns:
        synthetic["week_date"] = synthetic["week_id"].map(week_id_to_friday)
    return pd.concat([weekly, synthetic], ignore_index=True).sort_values("week_id").reset_index(drop=True)


def production_weekly_frame(weekly_df: pd.DataFrame, target_week_id: int) -> pd.DataFrame:
    return _append_synthetic_next_week(_truncate_for_target_week(weekly_df, target_week_id), target_week_id)


def _with_legacy10_frame(weekly: pd.DataFrame, call: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    original_read_weekly = legacy10.read_weekly
    original_read_csv = legacy10.pd.read_csv

    def reader(_path=None) -> pd.DataFrame:
        return weekly.copy()

    def read_csv(path, *args, **kwargs):
        if Path(path) == Path(legacy10.WEEKLY_DATA_PATH):
            return weekly.copy()
        return original_read_csv(path, *args, **kwargs)

    legacy10.read_weekly = reader
    legacy10.pd.read_csv = read_csv
    try:
        return call()
    finally:
        legacy10.read_weekly = original_read_weekly
        legacy10.pd.read_csv = original_read_csv


def _with_attr_values(values: dict[str, object], call: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    originals = {name: getattr(legacy10, name) for name in values}
    for name, value in values.items():
        setattr(legacy10, name, value)
    try:
        return call()
    finally:
        for name, value in originals.items():
            setattr(legacy10, name, value)


def _extended_model2_segments(target_week_id: int) -> tuple:
    target_friday = week_id_to_friday(target_week_id)
    segments = list(legacy10.MODEL2_SEGMENTS)
    if not segments:
        return legacy10.MODEL2_SEGMENTS
    name, start, end = segments[-1]
    if pd.Timestamp(end) < target_friday:
        segments[-1] = (name, start, target_friday)
    return tuple(segments)


def _build_10y_flow(weekly: pd.DataFrame) -> pd.DataFrame:
    score, _, _ = legacy10.build_score_signals()
    model2 = legacy10.build_model2_predictions()
    return legacy10.apply_d_overlay(legacy10.build_base(model2, score))


def _latest_result(
    predictions: pd.DataFrame,
    rdate: str,
    target_week_id: int | None = None,
) -> WeeklyPredictionResult:
    if predictions.empty:
        raise ValueError("W10Y produced no predictions")
    ordered = predictions.sort_values("week_id").copy()
    if target_week_id is not None:
        target_rows = ordered[ordered["week_id"].astype(int).eq(int(target_week_id))]
        if target_rows.empty:
            latest_week_id = int(ordered["week_id"].max())
            latest_feature_date = week_id_to_friday(latest_week_id).strftime("%Y-%m-%d")
            latest_predict_date = (week_id_to_friday(latest_week_id) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            raise ValueError(
                f"W10Y target week_id {target_week_id} not found in prediction output; "
                f"latest model output week_id is {latest_week_id} ({latest_feature_date}). "
                f"Latest supported Saturday predict_date is {latest_predict_date}."
            )
        row = target_rows.iloc[-1]
    else:
        row = ordered.iloc[-1]
    return WeeklyPredictionResult(
        rdate=rdate,
        frequency="W10Y",
        tenor="10Y",
        pred_label=int(row["d_pred_label"]),
        prob_up=float(row["d_prob_up"]) if pd.notna(row["d_prob_up"]) else float("nan"),
        week_id=int(row["week_id"]),
        prediction_column="d_pred_label",
        probability_column="d_prob_up",
        source="10y_d_overlay_0529",
        predictions=predictions.reset_index(drop=True),
    )


def predict_w10y(weekly_df: pd.DataFrame, rdate: str, target_week_id: int | None = None) -> WeeklyPredictionResult:
    source_weekly = production_weekly_frame(weekly_df, target_week_id) if target_week_id is not None else weekly_df
    weekly = _legacy10_input_frame(source_weekly)
    old_target = legacy10.TARGET_RATE_COL

    def call() -> pd.DataFrame:
        legacy10.TARGET_RATE_COL = "TB0YWI3C"
        predictions = _build_10y_flow(weekly)
        end_date = max(legacy10.END_DATE, week_id_to_friday(target_week_id) if target_week_id is not None else legacy10.END_DATE)
        return predictions[
            (predictions["month_date"] >= legacy10.START_DATE)
            & (predictions["month_date"] <= end_date)
        ].copy()

    try:
        overrides: dict[str, object] = {}
        if target_week_id is not None:
            overrides["SCORE_TEST_END_WEEK"] = max(int(legacy10.SCORE_TEST_END_WEEK), int(target_week_id))
            overrides["END_DATE"] = max(legacy10.END_DATE, week_id_to_friday(target_week_id))
            overrides["MODEL2_SEGMENTS"] = _extended_model2_segments(target_week_id)
        predictions = _with_attr_values(overrides, lambda: _with_legacy10_frame(weekly, call))
    finally:
        legacy10.TARGET_RATE_COL = old_target

    return _latest_result(predictions, rdate, target_week_id=target_week_id)
