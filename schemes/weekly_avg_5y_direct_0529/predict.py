from __future__ import annotations

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, build_weekly_live_context
from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

from .core.rule_vote import build_rule_vote

SCHEME_ID = "weekly_avg_5y_direct_0529"
HORIZON = 6

SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "5Y"
TARGET_RULE = WEEKLY_AVERAGE_TARGET_RULE
MODEL_VERSION = "rule_vote_0529"

_LOOKBACK_WEEKS = 60


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 5Y 周平均规则投票预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD（可能为非交易日如周六）。

    Returns:
        一条 PredictionRecord（5Y 方向预测）。
    """
    require_weekly_average_source_evidence(SCHEME_ID)
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

        vote_df = build_rule_vote(weekly_df)
        if vote_df.empty:
            raise RuntimeError(
                f"规则投票未产生有效行（共 {len(weekly_df)} 行周频数据，"
                f"week_id 范围 {weekly_df['week_id'].min()}–{weekly_df['week_id'].max()}）"
            )

        # 按 current_week_id 定位特征周预测
        vote_by_week = vote_df.set_index("week_id")
        if current_week_id not in vote_by_week.index:
            raise RuntimeError(
                f"当前特征周未产生有效投票信号：current_week_id={current_week_id}, "
                f"available_signal_weeks={sorted(vote_by_week.index.astype(int).tolist())}"
            )
        feature_week_id = current_week_id
        last_row = vote_by_week.loc[feature_week_id]

        # week_id → 日期（只读 DB，不使用日历公式）
        target_week_id = context.target_week_id
        target_date = context.target_date

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
                    "vote_score": float(last_row["rule_vote"]),
                    "rule_vote": float(last_row["rule_vote"]),
                    "source_spec": str(last_row.get("source_spec", "")),
                    "score_spec": str(last_row.get("score_spec", "")),
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
        ]
    finally:
        engine.dispose()
