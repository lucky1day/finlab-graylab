from __future__ import annotations

from shared.input_artifacts import create_input_engine as _input_artifact_contract
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE
from shared.weekly_average_lgbm_predict_adapter import run_weekly_average_lgbm_prediction


SCHEME_ID = "weekly_avg_1y_lgbm_0529"
TARGET_TENOR = "1Y"
TARGET_RULE = WEEKLY_AVERAGE_TARGET_RULE
HORIZON = 6
_ = _input_artifact_contract


def run(predict_date: str):
    """执行 1Y 周平均 LGBM 预测。"""
    return run_weekly_average_lgbm_prediction(SCHEME_ID, predict_date)
