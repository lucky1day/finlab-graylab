from __future__ import annotations

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, data_service
from shared.models import PredictionRecord

from .core.rule_vote import build_rule_vote

SCHEME_ID = "weekly_5y_direct_0529"
HORIZON = 6

SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
TARGET_TENOR = "5Y"
TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"
MODEL_VERSION = "rule_vote_0529"

_LOOKBACK_WEEKS = 60

def _next_calendar_week_id(calendar, feature_week_id: int) -> int:
    """从 DB 日历读取 feature_week_id 后的下一实际 week_id。"""
    feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
    for day in calendar.next_trading_days(feature_date, 15):
        next_week = calendar.week_id_for_date(day)
        if next_week is not None and int(next_week) != int(feature_week_id):
            return int(next_week)
    raise ValueError(f"无法在 DB 日历中找到 week_id={feature_week_id} 的下一周")


def _feature_week_from_predict_date(calendar, predict_date: str) -> tuple[str, int]:
    """实盘统一用 predict_date 前一交易日作为数据截止日。"""
    feature_date = calendar.previous_trading_day(predict_date)
    wid = calendar.week_id_for_date(feature_date)
    if wid is None:
        raise ValueError(f"无法从 DB 日历解析 feature_date={feature_date} 的 week_id")
    return feature_date, int(wid)


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 5Y 周度规则投票预测。

    Args:
        predict_date: 预测发出日期 YYYY-MM-DD（可能为非交易日如周六）。

    Returns:
        一条 PredictionRecord（5Y 方向预测）。
    """
    engine = data_service.create_sqlalchemy_engine()
    try:
        calendar = get_calendar(engine)
        feature_date, current_week_id = _feature_week_from_predict_date(calendar, predict_date)

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
        target_week_id = _next_calendar_week_id(calendar, feature_week_id)
        target_date = calendar.week_id_to_last_trading_day(target_week_id)

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
