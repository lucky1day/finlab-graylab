from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import build_weekly_input_artifact
from shared.models import PredictionRecord
from shared.weekly_calendar import next_week_id, week_id_to_friday
from schemes.weekly_10y_d_overlay.core.weekly_data_service import read_source_week_id_for_date

from .core.predictors import REQUIRED_WEEKLY_COLUMNS, SOURCE_NAME, TARGET_RULE, predict_w7y


SCHEME_ID = "weekly_7y_cross_d_overlay"
TARGET_TENOR = "7Y"
HORIZON_DAYS = 6


@dataclass(frozen=True)
class Weekly7YPredictionContext:
    predict_date: str
    feature_week_id: int
    feature_date: str
    target_week_id: int
    target_date: str


def resolve_weekly_prediction_context(
    predict_date: str,
    source_feature_week_id: int,
) -> Weekly7YPredictionContext:
    """按源表 week_id 生成周度 7Y 预测上下文。"""
    run_date = pd.Timestamp(predict_date).normalize()
    feature_week_id = int(source_feature_week_id)
    feature_date = (run_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    target_week_id = next_week_id(feature_week_id)
    target_date = (run_date + pd.Timedelta(days=6)).strftime("%Y-%m-%d")
    return Weekly7YPredictionContext(
        predict_date=run_date.strftime("%Y-%m-%d"),
        feature_week_id=feature_week_id,
        feature_date=feature_date,
        target_week_id=target_week_id,
        target_date=target_date,
    )


def latest_supported_predict_date(weekly_df: pd.DataFrame) -> str:
    """返回 7Y cross-D 关键周频特征完整覆盖的最新周六预测日。"""
    if weekly_df.empty or "week_id" not in weekly_df.columns:
        return "unavailable"
    missing_columns = [col for col in REQUIRED_WEEKLY_COLUMNS if col not in weekly_df.columns]
    if missing_columns:
        return "unavailable"

    weekly = weekly_df.copy()
    weekly["_week_id"] = pd.to_numeric(weekly["week_id"], errors="coerce")
    weekly = weekly.dropna(subset=["_week_id"]).copy()
    if weekly.empty:
        return "unavailable"
    weekly["_week_id"] = weekly["_week_id"].astype(int)
    for col in REQUIRED_WEEKLY_COLUMNS:
        weekly[col] = pd.to_numeric(weekly[col], errors="coerce")
    valid = weekly.dropna(subset=list(REQUIRED_WEEKLY_COLUMNS))
    if valid.empty:
        return "unavailable"

    latest_valid_week = int(valid["_week_id"].max())
    latest_feature_date = week_id_to_friday(latest_valid_week)
    return (pd.Timestamp(latest_feature_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def validate_weekly_data_coverage(weekly_df: pd.DataFrame, context: Weekly7YPredictionContext) -> None:
    """确认 DB 周频输入已经覆盖本次 7Y 预测所需特征周。"""
    if weekly_df.empty or "week_id" not in weekly_df.columns:
        raise ValueError("weekly source data is empty; cannot run weekly_7y_cross_d_overlay")
    weekly = weekly_df.copy()
    weekly["_week_id"] = pd.to_numeric(weekly["week_id"], errors="coerce").astype("Int64")
    weekly = weekly.dropna(subset=["_week_id"]).copy()
    if weekly.empty:
        raise ValueError("weekly source data has no valid week_id; cannot run weekly_7y_cross_d_overlay")
    weekly["_week_id"] = weekly["_week_id"].astype(int)
    missing_columns = [col for col in REQUIRED_WEEKLY_COLUMNS if col not in weekly.columns]
    if missing_columns:
        raise ValueError(f"weekly source data is missing required columns: {missing_columns}")

    max_week_id = int(weekly["_week_id"].max())
    if max_week_id < context.feature_week_id:
        latest_feature_date = week_id_to_friday(max_week_id).strftime("%Y-%m-%d")
        latest_predict_date = latest_supported_predict_date(weekly)
        raise ValueError(
            "weekly source data is only available through "
            f"week_id {max_week_id} ({latest_feature_date}); "
            f"predict_date {context.predict_date} requires feature_week_id "
            f"{context.feature_week_id} ({context.feature_date}). "
            f"Latest supported Saturday predict_date is {latest_predict_date}."
        )

    feature_rows = weekly[weekly["_week_id"].eq(context.feature_week_id)]
    if feature_rows.empty:
        raise ValueError(f"weekly source data does not contain feature_week_id {context.feature_week_id}")
    feature_row = feature_rows.iloc[-1]
    missing_values = [
        col
        for col in REQUIRED_WEEKLY_COLUMNS
        if pd.isna(pd.to_numeric(pd.Series([feature_row[col]]), errors="coerce").iloc[0])
    ]
    if missing_values:
        available = weekly[weekly["_week_id"].le(context.feature_week_id)].copy()
        latest_predict_date = latest_supported_predict_date(available)
        raise ValueError(
            f"weekly source data row week_id {context.feature_week_id} ({context.feature_date}) "
            f"is missing required weekly values: {missing_values}. "
            f"Latest supported Saturday predict_date is {latest_predict_date}."
        )


def run(predict_date: str) -> list[PredictionRecord]:
    """执行周度 7Y cross-D overlay 方案预测；只读源数据，不写库。"""
    run_date = pd.Timestamp(predict_date).normalize()
    feature_date = (run_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    engine = create_sqlalchemy_engine()
    try:
        source_feature_week_id = read_source_week_id_for_date(feature_date, engine=engine)
        if source_feature_week_id is None:
            raise ValueError(f"weekly source data does not contain feature_date {feature_date}")
        context = resolve_weekly_prediction_context(predict_date, source_feature_week_id=source_feature_week_id)
        input_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=context.predict_date,
            end_week=context.feature_week_id,
            engine=engine,
        )
        weekly_df = input_artifact.dataframe
    finally:
        engine.dispose()

    validate_weekly_data_coverage(weekly_df, context)
    result = predict_w7y(weekly_df, context.predict_date, target_week_id=context.feature_week_id)
    extra = {
        "frequency": "weekly",
        "target_rule": TARGET_RULE,
        "feature_week_id": context.feature_week_id,
        "feature_date": context.feature_date,
        "target_week_id": context.target_week_id,
        "target_date": context.target_date,
        "source_week_id": result.week_id,
        "source_frequency": result.frequency,
        "prediction_column": result.prediction_column,
        "probability_column": result.probability_column,
        "source_spec": result.source_spec,
        "score_spec": result.score_spec,
        "input_artifact_path": str(input_artifact.path),
        "input_artifact_source": input_artifact.source,
    }
    return [
        PredictionRecord(
            scheme_id=SCHEME_ID,
            target_tenor=TARGET_TENOR,
            horizon=HORIZON_DAYS,
            predict_date=context.predict_date,
            target_date=context.target_date,
            predicted_direction=int(result.pred_label),
            confidence=float(result.prob_up),
            model_version=SOURCE_NAME,
            extra=extra,
        )
    ]
