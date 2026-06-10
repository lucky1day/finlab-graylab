from __future__ import annotations

from datetime import datetime, timedelta

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
_NEAREST_DATE_FALLBACK_DAYS = 5


def _nearest_week_id(calendar, predict_date: str) -> int:
    """将 predict_date 解析为 week_id。

    周度预测可能在周六发出，此时 predict_date 不是交易日，
    api_wind_date 中无对应记录。向回退最多 _NEAREST_DATE_FALLBACK_DAYS 天，
    取最近的交易日对应的 week_id。
    """
    wid = calendar.week_id_for_date(predict_date)
    if wid is not None:
        return wid
    dt = datetime.strptime(predict_date, "%Y-%m-%d")
    for offset in range(1, _NEAREST_DATE_FALLBACK_DAYS + 1):
        try_date = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        wid = calendar.week_id_for_date(try_date)
        if wid is not None:
            return wid
    raise ValueError(
        f"无法将 predict_date={predict_date} 解析为 week_id "
        f"（回退 {_NEAREST_DATE_FALLBACK_DAYS} 天后仍未找到交易日）"
    )


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
        current_week_id = _nearest_week_id(calendar, predict_date)

        # 加载足够历史用于 lookback（最大 4 周）+ 1 周作为 target 窗口
        start_week = current_week_id - _LOOKBACK_WEEKS
        end_week = current_week_id + 1

        input_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=predict_date,
            schema_columns=SCHEMA_COLUMNS,
            start_week=start_week,
            end_week=end_week,
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

        # 按 current_week_id 定位特征周预测（不是盲目取最后一行）
        # 必须同时满足两个条件：①有投票信号 ②数据中有目标周（下一周）
        # 从 current_week_id 向前搜索，直到找到符合条件的周
        vote_by_week = vote_df.set_index("week_id")
        all_weeks = sorted(weekly_df["week_id"].dropna().unique().astype(int))
        all_weeks_set = set(all_weeks)
        feature_week_id = current_week_id
        while feature_week_id >= weekly_df["week_id"].min():
            if (
                feature_week_id in vote_by_week.index
                and (feature_week_id + 1) in all_weeks_set
            ):
                break
            feature_week_id -= 1
        if feature_week_id not in vote_by_week.index:
            raise RuntimeError(
                f"无法在 week_id ≤ {current_week_id} 范围内找到有效投票信号"
            )
        if (feature_week_id + 1) not in all_weeks_set:
            raise RuntimeError(
                f"feature_week_id={feature_week_id} 之后无可用目标周，"
                f"无法确定 target_week_id（数据范围 {all_weeks[0]}–{all_weeks[-1]}）"
            )
        last_row = vote_by_week.loc[feature_week_id]

        # 从数据中找 target_week_id：feature_week_id 的下一个周
        feature_pos = all_weeks.index(feature_week_id)
        target_week_id = int(all_weeks[feature_pos + 1])

        # week_id → 日期（只读 DB，不使用日历公式）
        feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
        target_date = calendar.week_id_to_last_trading_day(target_week_id)

        return [
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=TARGET_TENOR,
                horizon=HORIZON,
                predict_date=predict_date,
                target_date=target_date,
                predicted_direction=int(last_row["final_pred_label"]),
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
