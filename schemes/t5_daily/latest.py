from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from schemes.t5_daily.core import predict_10y, predict_3y, predict_5y, predict_7y
from schemes.t5_daily.core.common_utils import (
    build_fallback_signal,
    build_features,
    choose_threshold,
    make_labels,
)


@dataclass(frozen=True)
class LatestPrediction:
    """T+5 单期限最新预测结果。"""

    tenor: str
    feature_date: str
    vote_pred: int
    model_pred: int
    confidence: float | None
    threshold: float | None
    vote_sum: int
    signal_sum: int
    decision: str
    vote_signals: dict[str, int]
    model_version: str


def _feature_index(df: pd.DataFrame, predict_date: str) -> int:
    target = pd.Timestamp(predict_date)
    eligible = np.flatnonzero(df["date"].lt(target).to_numpy())
    if len(eligible) == 0:
        raise ValueError(f"no feature data available before predict date {predict_date}")
    return int(eligible[-1])


def _build_features(module: Any, df: pd.DataFrame) -> pd.DataFrame:
    if hasattr(module, "build_features_multi"):
        return module.build_features_multi(df, module.AUX_TENORS)
    return build_features(
        df,
        module.CLOSE_COL,
        module.AUX1_COL,
        module.AUX1_NAME,
        module.AUX2_COL,
        module.AUX2_NAME,
        module.SELF_NAME,
    )


def predict_latest_for_module(module: Any, df: pd.DataFrame, predict_date: str, n_jobs: int = 4) -> LatestPrediction:
    """复用原始滚动训练逻辑，预测指定发出日前最近可用特征日。"""
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values("date").reset_index(drop=True)
    idx = _feature_index(work, predict_date)

    close = work[module.CLOSE_COL].copy()
    _, labels = make_labels(work, module.CLOSE_COL, horizon=module.HORIZON)
    features = _build_features(module, work)
    fallback_signal = build_fallback_signal(work, module.CLOSE_COL)
    vote_df = module.build_custom_vote_signals(work, module.RECIPE)

    eligible = np.flatnonzero(
        (np.arange(len(work)) < idx - module.GAP)
        & np.isin(labels, [-1.0, 1.0])
        & close.notna().to_numpy()
    )
    if len(eligible) > module.WINDOW:
        eligible = eligible[-module.WINDOW :]

    if len(eligible) < 120 or len(np.unique(labels[eligible])) < 2:
        prob = None
        threshold = None
        base_pred = int(fallback_signal.iloc[idx])
        decision = "cold_fallback"
    else:
        split = max(80, int(len(eligible) * module.SPLIT_PCT))
        fit_idx = eligible[:split]
        cal_idx = eligible[split:]

        model = lgb.LGBMClassifier(
            objective="binary",
            metric="binary_logloss",
            num_leaves=module.NUM_LEAVES,
            learning_rate=module.LEARNING_RATE,
            n_estimators=module.N_ESTIMATORS,
            min_child_samples=module.MIN_CHILD_SAMPLES,
            reg_alpha=module.REG_ALPHA,
            reg_lambda=module.REG_LAMBDA,
            subsample=0.85,
            colsample_bytree=0.90,
            n_jobs=n_jobs,
            verbosity=-1,
            random_state=42,
            force_col_wise=True,
        )
        model.fit(
            features.iloc[fit_idx],
            (labels[fit_idx] == 1).astype(int),
            eval_set=[(features.iloc[cal_idx], (labels[cal_idx] == 1).astype(int))],
            callbacks=[lgb.early_stopping(20, verbose=False)],
        )
        prob = float(model.predict_proba(features.iloc[[idx]])[:, 1][0])
        cal_prob = model.predict_proba(features.iloc[cal_idx])[:, 1]
        threshold = choose_threshold(cal_prob, labels[cal_idx])
        base_pred = 1 if prob >= threshold else -1
        decision = "model"

    signals = {str(col): int(vote_df.iloc[idx][col]) for col in vote_df.columns}
    signal_sum = sum(signals.values())
    vote_sum = int(base_pred + signal_sum)
    if module.VOTE_THRESHOLD > 0:
        if vote_sum > module.VOTE_THRESHOLD:
            pred = 1
        elif vote_sum < -module.VOTE_THRESHOLD:
            pred = -1
        else:
            pred = base_pred
    else:
        pred = 1 if vote_sum >= 0 else -1

    return LatestPrediction(
        tenor=module.TENOR,
        feature_date=work.loc[idx, "date"].strftime("%Y-%m-%d"),
        vote_pred=int(pred),
        model_pred=int(base_pred),
        confidence=prob,
        threshold=threshold,
        vote_sum=vote_sum,
        signal_sum=int(signal_sum),
        decision=decision,
        vote_signals=signals,
        model_version=f"lgbm_t5_{module.TENOR.lower()}_w{module.WINDOW}",
    )


TENOR_MODULES = {
    "3Y": predict_3y,
    "5Y": predict_5y,
    "7Y": predict_7y,
    "10Y": predict_10y,
}
