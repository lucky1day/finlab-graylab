from __future__ import annotations

import math
from typing import Any

import pandas as pd

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_TARGET_RULE, build_weekly_live_context
from shared.signal_policy import no_signal_as_flat

from .core.rule_vote import build_rule_vote

SCHEME_ID = "weekly_5y_direct_0529"
HORIZON = 6

SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "5Y"
TARGET_RULE = WEEKLY_TARGET_RULE
MODEL_VERSION = "rule_vote_0529"

_LOOKBACK_WEEKS = 60


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
    """执行 5Y 周度规则投票预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD（可能为非交易日如周六）。

    Returns:
        一条 PredictionRecord（5Y 方向预测）。
    """
    engine = create_input_engine()
    try:
        calendar = get_calendar(engine)
        context = build_weekly_live_context(calendar, predict_date)
        feature_date = context.feature_date
        current_week_id = context.feature_week_id

        # 加载足够历史用于 lookback（最大 4 周）+ 1 周作为 target 窗口
        start_week = current_week_id - _LOOKBACK_WEEKS

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

        vote_df = build_rule_vote(weekly_df)
        target_week_id = context.target_week_id
        target_date = context.target_date

        if vote_df.empty:
            raise RuntimeError(
                f"规则投票未产生有效行（共 {len(weekly_df)} 行周频数据，"
                f"week_id 范围 {weekly_df['week_id'].min()}–{weekly_df['week_id'].max()}）"
            )

        # 按 current_week_id 定位特征周预测
        vote_df = _normalize_output_week_ids(vote_df)
        available_signal_weeks = sorted(
            int(value) for value in vote_df["week_id"].unique()
        )
        if current_week_id not in available_signal_weeks:
            outcome = no_signal_as_flat(
                "rule_vote",
                extra={
                    "feature_week_id": current_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_rule": TARGET_RULE,
                    "available_signal_weeks": available_signal_weeks,
                    "latest_signal_week_id": (
                        available_signal_weeks[-1] if available_signal_weeks else None
                    ),
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
        last_row = vote_df[vote_df["week_id"].eq(feature_week_id)].iloc[-1]

        # week_id → 日期（只读 DB，不使用日历公式）
        return [
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=TARGET_TENOR,
                horizon=HORIZON,
                predict_date=predict_date,
                target_date=target_date,
                predicted_direction=int(last_row["final_pred_label"]),
                feature_date=feature_date,
                confidence=float(last_row["final_prob_up"]),
                model_version=MODEL_VERSION,
                extra={
                    "feature_week_id": feature_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_rule": TARGET_RULE,
                    "rule_vote": float(last_row["rule_vote"]),
                    "source_spec": str(last_row.get("source_spec", "")),
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
        ]
    finally:
        engine.dispose()
