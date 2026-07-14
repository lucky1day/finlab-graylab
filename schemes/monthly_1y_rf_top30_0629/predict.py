from __future__ import annotations

from shared.input_artifacts import create_input_engine as _input_artifact_contract
from shared.monthly_predict_adapter import run_monthly_prediction
from shared.prediction_context import MONTHLY_TARGET_RULE


SCHEME_ID = "monthly_1y_rf_top30_0629"
TARGET_TENOR = "1Y"
TARGET_RULE = MONTHLY_TARGET_RULE
HORIZON = 30
_ = _input_artifact_contract


def run(predict_date: str):
    """执行 1Y 月度 RF top30 预测。"""
    return run_monthly_prediction(SCHEME_ID, predict_date)
