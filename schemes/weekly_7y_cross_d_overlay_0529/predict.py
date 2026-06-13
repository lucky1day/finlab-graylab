from __future__ import annotations

from typing import Any

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, data_service
from shared.models import PredictionRecord

from .core.cross_d_overlay import build_cross_d_overlay

SCHEME_ID = "weekly_7y_cross_d_overlay_0529"
HORIZON = 6
SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "7Y"
TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"
MODEL_VERSION = "cross_d_overlay_0529"
LOOKBACK_WEEKS = 80
FUTURE_LOAD_WEEKS = 6


def _feature_week_from_predict_date(calendar: Any, predict_date: str) -> tuple[str, int]:
    """实盘统一用 predict_date 前一交易日作为数据截止日。"""
    feature_date = calendar.previous_trading_day(predict_date)
    wid = calendar.week_id_for_date(feature_date)
    if wid is None:
        raise ValueError(f"无法从 DB 日历解析 feature_date={feature_date} 的 week_id")
    return feature_date, int(wid)


def _next_calendar_week_id(calendar: Any, feature_week_id: int) -> int:
    """从 DB 日历读取 feature_week_id 后的下一实际 week_id。"""
    feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
    for day in calendar.next_trading_days(feature_date, 15):
        next_week = calendar.week_id_for_date(day)
        if next_week is not None and int(next_week) != int(feature_week_id):
            return int(next_week)
    raise ValueError(f"无法在 DB 日历中找到 week_id={feature_week_id} 的下一周")


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 7Y 周度 Cross-D 叠加预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD，周六运行时可为非交易日。

    Returns:
        一条 7Y 周度 PredictionRecord。
    """
    engine = data_service.create_sqlalchemy_engine()
    try:
        calendar = get_calendar(engine)
        feature_date, current_week_id = _feature_week_from_predict_date(calendar, predict_date)
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
        target_week_id = _next_calendar_week_id(calendar, feature_week_id)
        target_date = calendar.week_id_to_last_trading_day(target_week_id)
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
