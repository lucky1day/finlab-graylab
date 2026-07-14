from __future__ import annotations

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, build_weekly_live_context
from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

from .core.cross_d_overlay import build_cross_d_overlay

SCHEME_ID = "weekly_avg_7y_cross_d_overlay_0529"
HORIZON = 6
SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "7Y"
TARGET_RULE = WEEKLY_AVERAGE_TARGET_RULE
MODEL_VERSION = "cross_d_overlay_0529"
LOOKBACK_WEEKS = 80
FUTURE_LOAD_WEEKS = 6


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 7Y 周平均 Cross-D 叠加预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD，周六运行时可为非交易日。

    Returns:
        一条 7Y 周度 PredictionRecord。
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
            schema_columns=SCHEMA_COLUMNS,
            start_week=start_week,
            end_week=current_week_id,
            as_of_date=feature_date,
            engine=engine,
        )
        weekly_df = input_artifact.dataframe
        if weekly_df.empty:
            raise RuntimeError("周频数据为空，无法生成预测")

        prediction_df = build_cross_d_overlay(weekly_df)
        if prediction_df.empty:
            raise RuntimeError(
                f"Cross-D 算法未产生有效行（输入 {len(weekly_df)} 行，"
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
            raise RuntimeError(f"无法在 week_id ≤ {current_week_id} 范围内找到有效 Cross-D 信号")
        feature_week_id = valid_weeks[-1]
        if int(feature_week_id) != int(current_week_id):
            raise RuntimeError(
                f"当前特征周未产生有效 Cross-D 信号：current_week_id={current_week_id}, "
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
                    "main_dir": int(last_row["main_pred_label"]),
                    "main_score": float(last_row["main_prob_up"]),
                    "d5_d_dir": int(last_row["d5_d_pred_label"]),
                    "d5_d_score": float(last_row["d5_d_prob_up"]),
                    "cross_d_score": float(last_row["cross_d_prob_up"]),
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
