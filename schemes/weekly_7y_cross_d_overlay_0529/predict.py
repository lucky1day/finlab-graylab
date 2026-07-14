from __future__ import annotations

import math
from typing import Any

import pandas as pd

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_TARGET_RULE, build_weekly_live_context
from shared.signal_policy import no_signal_as_flat

from .core.cross_d_overlay import build_cross_d_overlay

SCHEME_ID = "weekly_7y_cross_d_overlay_0529"
HORIZON = 6
SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "7Y"
TARGET_RULE = WEEKLY_TARGET_RULE
MODEL_VERSION = "cross_d_overlay_0529"
LOOKBACK_WEEKS = 80
FUTURE_LOAD_WEEKS = 6


def _require_current_feature_input(
    frame: pd.DataFrame,
    current_week_id: int,
    required_columns: list[str],
) -> None:
    """确认当前特征周输入已到位且必要收益率有效。"""
    if "week_id" not in frame.columns:
        raise ValueError("周频输入缺少 week_id")
    week_ids = pd.to_numeric(frame["week_id"], errors="coerce")
    current_rows = frame.loc[week_ids.eq(int(current_week_id))]
    if current_rows.empty:
        finite_week_ids = [
            int(value)
            for value in week_ids.dropna().tolist()
            if math.isfinite(float(value)) and float(value).is_integer()
        ]
        latest = max(finite_week_ids) if finite_week_ids else None
        raise RuntimeError(
            f"周频输入水位不足：缺少当前 feature_week_id={current_week_id}，"
            f"最新有效 week_id={latest}"
        )

    missing_columns = [column for column in required_columns if column not in frame.columns]
    if missing_columns:
        raise ValueError(f"周频输入缺少必要列: {missing_columns}")
    numeric = current_rows[required_columns].apply(pd.to_numeric, errors="coerce")
    complete = numeric.apply(
        lambda row: all(pd.notna(value) and math.isfinite(float(value)) for value in row),
        axis=1,
    )
    if not bool(complete.any()):
        invalid_columns = [
            column
            for column in required_columns
            if not any(
                pd.notna(value) and math.isfinite(float(value))
                for value in numeric[column]
            )
        ]
        raise RuntimeError(
            f"当前周必要输入缺失：feature_week_id={current_week_id}, "
            f"columns={invalid_columns or required_columns}"
        )


def _normalize_output_week_ids(frame: pd.DataFrame) -> pd.DataFrame:
    """严格校验并规范化算法输出的 week_id。"""
    normalized = frame.copy()

    def normalize(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"算法输出 week_id 必须为严格整数: {value!r}")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"算法输出 week_id 必须为严格整数: {value!r}") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"算法输出 week_id 必须为严格整数: {value!r}")
        return int(numeric)

    normalized["week_id"] = normalized["week_id"].map(normalize)
    return normalized


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 7Y 周度 Cross-D 叠加预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD，周六运行时可为非交易日。

    Returns:
        一条 7Y 周度 PredictionRecord。
    """
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
            schema_columns=SCHEMA_COLUMNS,
            start_week=start_week,
            end_week=current_week_id,
            as_of_date=feature_date,
            engine=engine,
        )
        weekly_df = input_artifact.dataframe
        if weekly_df.empty:
            raise RuntimeError("周频数据为空，无法生成预测")
        _require_current_feature_input(
            weekly_df,
            current_week_id,
            [column for column in SCHEMA_COLUMNS if column != "week_id"],
        )

        prediction_df = build_cross_d_overlay(weekly_df)
        target_week_id = context.target_week_id
        target_date = context.target_date
        if prediction_df.empty:
            raise RuntimeError(
                f"Cross-D 算法未产生有效行（输入 {len(weekly_df)} 行，"
                f"week_id 范围 {weekly_df['week_id'].min()}–{weekly_df['week_id'].max()}）"
            )

        prediction_df = _normalize_output_week_ids(prediction_df)
        valid_weeks = sorted(
            int(value)
            for value in prediction_df["week_id"].dropna().unique()
            if int(value) <= int(current_week_id)
        )
        if current_week_id not in valid_weeks:
            outcome = no_signal_as_flat(
                "cross_d_overlay",
                extra={
                    "feature_week_id": current_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_rule": TARGET_RULE,
                    "available_signal_weeks": valid_weeks,
                    "latest_signal_week_id": valid_weeks[-1] if valid_weeks else None,
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
            return [
                PredictionRecord(
                    scheme_id=SCHEME_ID,
                    target_tenor=TARGET_TENOR,
                    horizon=HORIZON,
                    predict_date=predict_date,
                    target_date=target_date,
                    predicted_direction=outcome.predicted_direction,
                    feature_date=feature_date,
                    confidence=outcome.confidence,
                    model_version=MODEL_VERSION,
                    extra=dict(outcome.extra),
                )
            ]
        feature_week_id = current_week_id
        last_row = prediction_df[prediction_df["week_id"].eq(feature_week_id)].iloc[-1]

        return [
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=TARGET_TENOR,
                horizon=HORIZON,
                predict_date=predict_date,
                target_date=target_date,
                predicted_direction=int(last_row["cross_d_pred_label"]),
                feature_date=feature_date,
                confidence=float(last_row["cross_d_prob_up"]),
                model_version=MODEL_VERSION,
                extra={
                    "feature_week_id": feature_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_rule": TARGET_RULE,
                    "cross_d_overlay": bool(last_row["cross_d_overlay"]),
                    "cross_d_signal_source": str(last_row["cross_d_signal_source"]),
                    "main_pred_label": int(last_row["main_pred_label"]),
                    "main_prob_up": float(last_row["main_prob_up"]),
                    "d5_d_pred_label": int(last_row["d5_d_pred_label"]),
                    "d5_d_prob_up": float(last_row["d5_d_prob_up"]),
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
        ]
    finally:
        engine.dispose()
