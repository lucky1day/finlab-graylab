from __future__ import annotations

from datetime import datetime, timedelta

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_daily_input_artifact
from shared.models import PredictionRecord

from .latest_prediction import TENOR_MODULES, predict_latest_for_module


SCHEME_ID = "t5_daily"
HORIZON = 5


def _target_date_from_feature_date(feature_date: str, horizon: int) -> str:
    """按原始 T+5 标签语义，取特征日后第 horizon 个交易日。"""
    return get_calendar().nth_trading_day_after(feature_date, horizon)


def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行 T+5 日频预测。

    只做 I/O 编排，不修改 core 目录中的原始算法逻辑。
    """
    end_date = predict_date
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=8 * 365)).strftime("%Y-%m-%d")
    input_artifact = build_daily_input_artifact(
        scheme_id=SCHEME_ID,
        predict_date=predict_date,
        start_date=start_date,
        end_date=end_date,
    )
    daily_df = input_artifact.dataframe

    records: list[PredictionRecord] = []
    for tenor in ("3Y", "5Y", "7Y", "10Y"):
        result = predict_latest_for_module(TENOR_MODULES[tenor], daily_df, predict_date)
        target_date = _target_date_from_feature_date(result.feature_date, HORIZON)
        records.append(
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=tenor,
                horizon=HORIZON,
                predict_date=predict_date,
                target_date=target_date,
                predicted_direction=result.vote_pred,
                confidence=result.confidence,
                model_version=result.model_version,
                extra={
                    "feature_date": result.feature_date,
                    "model_pred": result.model_pred,
                    "vote_sum": result.vote_sum,
                    "signal_sum": result.signal_sum,
                    "threshold": result.threshold,
                    "decision": result.decision,
                    "vote_signals": result.vote_signals,
                    "input_artifact_path": str(input_artifact.path),
                    "input_artifact_source": input_artifact.source,
                },
            )
        )
    return records
