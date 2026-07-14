from __future__ import annotations

from typing import Any

import pandas as pd

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, build_weekly_live_context
from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

from .core.d_overlay import build_d_overlay

SCHEME_ID = "weekly_avg_10y_d_overlay_0529"
HORIZON = 6
TARGET_TENOR = "10Y"
TARGET_RULE = WEEKLY_AVERAGE_TARGET_RULE
MODEL_VERSION = "10y_d_overlay_0529"
TARGET_COL = "TB0YWI3C"
LOOKBACK_WEEKS = 600
FUTURE_LOAD_WEEKS = 6


def _attach_db_week_dates(weekly_df: pd.DataFrame, calendar: Any) -> pd.DataFrame:
    """为周频宽表注入 DB 周历日期。"""
    df = weekly_df.copy()
    if "week_id" not in df.columns:
        raise ValueError("周频输入缺少 week_id")
    week_ids = pd.to_numeric(df["week_id"], errors="coerce").dropna().astype(int).unique()
    week_dates = {int(wid): calendar.week_id_to_last_trading_day(int(wid)) for wid in week_ids}
    model_dates = {int(wid): _legacy_segment_anchor_date(int(wid)) for wid in week_ids}
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    df["week_date"] = df["week_id"].map(week_dates)
    df["model_date"] = df["week_id"].map(model_dates)
    return df


def _legacy_segment_anchor_date(week_id: int) -> str:
    """保留原始算法的模型分段锚点；不用于 target/display 日期。"""
    text = str(int(week_id))
    year = int(text[:4])
    ordinal = int(text[4:])
    jan1 = pd.Timestamp(f"{year}-01-01")
    first_thu = jan1 + pd.Timedelta(days=(3 - jan1.weekday()) % 7)
    first_anchor = first_thu - pd.Timedelta(days=3)
    return (first_anchor + pd.Timedelta(weeks=ordinal - 1)).strftime("%Y-%m-%d")


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 10Y 周平均 D-overlay 预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD，周六运行时可为非交易日。

    Returns:
        一条 10Y 周度 PredictionRecord。
    """
    require_weekly_average_source_evidence(SCHEME_ID)
    engine = create_input_engine()
    try:
        calendar = get_calendar(engine)
        context = build_weekly_live_context(calendar, predict_date)
        feature_date = context.feature_date
        current_week_id = context.feature_week_id
        start_week = current_week_id - LOOKBACK_WEEKS

        input_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=predict_date,
            schema_columns=None,
            start_week=start_week,
            end_week=current_week_id,
            as_of_date=feature_date,
            engine=engine,
        )
        weekly_df = _attach_db_week_dates(input_artifact.dataframe, calendar)
        if weekly_df.empty:
            raise RuntimeError("周频数据为空，无法生成预测")

        prediction_df = build_d_overlay(weekly_df)
        if prediction_df.empty:
            raise RuntimeError(
                f"10Y D-overlay 算法未产生有效行（输入 {len(weekly_df)} 行，"
                f"week_id 范围 {weekly_df['week_id'].min()}–{weekly_df['week_id'].max()}）"
            )

        prediction_df = prediction_df.copy()
        prediction_df["week_id"] = prediction_df["week_id"].astype(int)
        valid_weeks = sorted(
            int(value)
            for value in prediction_df["week_id"].dropna().unique()
            if int(value) <= int(current_week_id)
        )
        if not valid_weeks:
            raise RuntimeError(f"无法在 week_id ≤ {current_week_id} 范围内找到有效 10Y D-overlay 信号")
        feature_week_id = valid_weeks[-1]
        if int(feature_week_id) != int(current_week_id):
            raise RuntimeError(
                f"10Y D-overlay 未能产出当前特征周信号：current_week_id={current_week_id}, "
                f"latest_signal_week_id={feature_week_id}"
            )

        target_week_id = context.target_week_id
        target_date = context.target_date
        last_row = prediction_df[prediction_df["week_id"].eq(feature_week_id)].iloc[-1]

        return [
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=TARGET_TENOR,
                horizon=HORIZON,
                predict_date=predict_date,
                target_date=target_date,
                predicted_direction=int(last_row["d_pred_label"]),
                feature_date=feature_date,
                confidence=float(last_row["d_prob_up"]),
                model_version=MODEL_VERSION,
                extra={
                    "feature_week_id": feature_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_rule": TARGET_RULE,
                    "source": MODEL_VERSION,
                    "score_dir": _int_or_none(last_row.get("score_pred_label")),
                    "score_score": _float_or_none(last_row.get("score_prob_up")),
                    "model2_dir": _int_or_none(last_row.get("d_model2_pred_label")),
                    "model2_score": _float_or_none(last_row.get("model2_prob_up")),
                    "d_final_score": _float_or_none(last_row.get("d_prob_up")),
                    "d_model2_overlay": bool(last_row.get("d_model2_overlay", False)),
                    "d_signal_source": str(last_row.get("d_signal_source", "")),
                    "score_pred_label": _int_or_none(last_row.get("score_pred_label")),
                    "score_prob_up": _float_or_none(last_row.get("score_prob_up")),
                    "model2_prob_up": _float_or_none(last_row.get("model2_prob_up")),
                    "d_model2_pred_label": _int_or_none(last_row.get("d_model2_pred_label")),
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
        ]
    finally:
        engine.dispose()


def _int_or_none(value: Any) -> int | None:
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
