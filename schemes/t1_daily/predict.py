from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_daily_input_artifact, data_service
from shared.models import PredictionRecord

from .core.config import TENOR_CONFIGS
from .core.lgbm_predictor import predict_latest_for_config


SCHEME_ID = "t1_daily"
HORIZON = 1
LOOKBACK_DAYS = 7 * 365
CONFIG_PATH = Path(__file__).with_name("config.yaml")


def _configured_tenors() -> set[str]:
    """读取当前方案启用的预测期限。"""
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("tenors:"):
            raw_value = line.split(":", 1)[1].strip()
            return {str(item) for item in ast.literal_eval(raw_value)}
    return set()


def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行 T+1 日频预测。

    Args:
        predict_date: 预测发出日期，格式 YYYY-MM-DD。

    Returns:
        统一预测记录列表，每个 tenor 一条。
    """
    datetime.strptime(predict_date, "%Y-%m-%d")
    engine = data_service.create_sqlalchemy_engine()
    try:
        feature_date = get_calendar(engine).previous_trading_day(predict_date)
        start_date = (datetime.strptime(feature_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        input_artifact = build_daily_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=predict_date,
            start_date=start_date,
            end_date=feature_date,
            engine=engine,
        )
        daily_df = input_artifact.dataframe
        configured_tenors = _configured_tenors()

        records: list[PredictionRecord] = []
        for frequency, config in TENOR_CONFIGS.items():
            if config.tenor not in configured_tenors:
                continue
            result = predict_latest_for_config(daily_df, config, current_date=predict_date)
            records.append(
                PredictionRecord(
                    scheme_id=SCHEME_ID,
                    target_tenor=config.tenor,
                    horizon=HORIZON,
                    predict_date=predict_date,
                    target_date=result.target_date,
                    predicted_direction=result.pred_label,
                    feature_date=result.feature_date,
                    confidence=result.prob_up,
                    model_version=f"lgbm_w{config.window}",
                    extra={
                        "frequency": frequency,
                        "feature_date": result.feature_date,
                        "base_pred": result.base_pred,
                        "base_decision": result.base_decision,
                        "vote_sum": result.vote_sum,
                        "decision": result.decision,
                        "threshold": result.threshold_used,
                        "train_start": result.train_start,
                        "train_end": result.train_end,
                        "input_artifact_path": str(input_artifact.path),
                        "input_artifact_source": input_artifact.source,
                    },
                )
            )

        return records
    finally:
        engine.dispose()
