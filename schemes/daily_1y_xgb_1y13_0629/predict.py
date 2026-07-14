from __future__ import annotations

from shared.daily_0629_predict_adapter import run_daily_0629_prediction
from shared.input_artifacts import build_daily_input_artifact as _input_artifact_contract


SCHEME_ID = "daily_1y_xgb_1y13_0629"
_ = _input_artifact_contract


def run(predict_date: str):
    """执行 0629 日度 1Y13 T+1 预测。"""
    return run_daily_0629_prediction(SCHEME_ID, predict_date)
