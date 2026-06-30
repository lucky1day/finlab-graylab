from __future__ import annotations

from shared.daily_0629_predict_adapter import run_daily_0629_prediction
from shared.input_artifacts import build_daily_input_artifact as _input_artifact_contract


SCHEME_ID = "daily_10y_lgbm_10y04_0629"
_ = _input_artifact_contract


def run(predict_date: str):
    """执行 0629 日度 10Y04 T+1 预测。"""
    return run_daily_0629_prediction(SCHEME_ID, predict_date)
