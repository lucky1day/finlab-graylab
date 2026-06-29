from __future__ import annotations

from shared.input_artifacts import create_input_engine as _input_artifact_contract
from shared.monthly_predict_adapter import run_monthly_prediction
from shared.prediction_context import MONTHLY_TARGET_RULE


SCHEME_ID = "monthly_10y_rf_top5_0629"
TARGET_TENOR = "10Y"
TARGET_RULE = MONTHLY_TARGET_RULE
HORIZON = 30
_ = _input_artifact_contract


def run(predict_date: str):
    """执行 10Y 月度 RF top5 预测。"""
    return run_monthly_prediction(SCHEME_ID, predict_date)
