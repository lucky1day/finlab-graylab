from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .config import TenorConfig
from .feature_engineering import build_fallback_signal, build_feature_matrix, build_vote_signals


@dataclass
class PredictionResult:
    rdate: str
    target_date: str
    feature_date: str
    tenor: str
    frequency: str
    pred_label: int
    prob_up: float
    threshold_used: float
    base_pred: int
    base_decision: str
    vote_sum: int
    decision: str
    feature_columns: list[str]
    feature_origin_map: dict[str, list[str]]
    train_start: str | None
    train_end: str | None
    model: Any
    config: TenorConfig


def make_labels(df: pd.DataFrame, close_col: str, threshold: float) -> tuple[pd.Series, np.ndarray]:
    close = pd.to_numeric(df[close_col], errors="coerce")
    future_return = close.shift(-1).div(close).sub(1.0)
    labels = np.select(
        [future_return > threshold, future_return < -threshold],
        [1, -1],
        default=0,
    ).astype(float)
    labels[future_return.isna()] = np.nan
    return future_return, labels


def choose_threshold(cal_prob: np.ndarray, cal_labels: np.ndarray) -> float:
    if len(cal_prob) == 0:
        return 0.5
    best_score = -np.inf
    best_threshold = 0.5
    for threshold in np.linspace(0.38, 0.62, 49):
        pred = np.where(cal_prob >= threshold, 1, -1)
        accuracy = float((pred == cal_labels).mean())
        pred_up = pred == 1
        pred_down = pred == -1
        up_precision = ((pred_up) & (cal_labels == 1)).sum() / pred_up.sum() if pred_up.sum() else 0.0
        down_precision = ((pred_down) & (cal_labels == -1)).sum() / pred_down.sum() if pred_down.sum() else 0.0
        score = accuracy + 0.04 * min(float(up_precision), float(down_precision))
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _ensure_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    if "date" not in result.columns:
        raise ValueError("daily dataframe must contain date")
    result["date"] = pd.to_datetime(result["date"])
    return result.sort_values("date").reset_index(drop=True)


def _target_and_feature_index(df: pd.DataFrame, current_date: str | None) -> tuple[str, int]:
    if current_date is None:
        if len(df) < 2:
            raise ValueError("at least two rows are required to infer a target date")
        return df["date"].iloc[-1].strftime("%Y-%m-%d"), len(df) - 2
    target = pd.Timestamp(current_date)
    eligible = np.flatnonzero(df["date"].lt(target).to_numpy())
    if not len(eligible):
        raise ValueError(f"no feature data available before target date {current_date}")
    return target.strftime("%Y-%m-%d"), int(eligible[-1])


def _train_model(config: TenorConfig):
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise RuntimeError("LightGBM is required for daily production prediction") from exc
    return lgb.LGBMClassifier(
        objective="binary",
        metric="binary_logloss",
        num_leaves=config.num_leaves,
        learning_rate=config.learning_rate,
        n_estimators=config.n_estimators,
        min_child_samples=config.min_child_samples,
        reg_alpha=0.8,
        reg_lambda=3.0,
        subsample=0.95,
        colsample_bytree=0.95,
        random_state=42,
        n_jobs=config.n_jobs,
        verbosity=-1,
    )


def _combine_vote(config: TenorConfig, base_pred: int, vote_row: pd.Series) -> tuple[int, int, str]:
    if config.style == "10y" and config.vote_scheme == "validation_2025h1":
        vote_sum = int(
            -base_pred
            + vote_row["5Y_anti_sum180"]
            + vote_row["spread_5Y_1Y_chg_anti_sum252"]
            + vote_row["5Y_anti_lag252"]
        )
        return (1 if vote_sum > 0 else -1), vote_sum, "lightgbm_validation_2025h1"
    signal_sum = int(vote_row.sum())
    vote_sum = int(base_pred + signal_sum)
    if config.style == "5y":
        return (1 if vote_sum >= 0 else -1), vote_sum, "lightgbm_basevote_4signals"
    return (1 if vote_sum >= 0 else -1), vote_sum, f"lightgbm_{config.vote_scheme}"


def predict_latest_for_config(df: pd.DataFrame, config: TenorConfig, current_date: str | None = None) -> PredictionResult:
    daily = _ensure_daily_frame(df)
    required = [config.close_col]
    for col in [config.mid_col, config.long_col, config.short_col]:
        if col:
            required.append(col)
    missing = [col for col in required if col not in daily.columns]
    if missing:
        raise ValueError(f"daily dataframe missing required columns: {missing}")

    target_date, idx = _target_and_feature_index(daily, current_date)
    feature_date = daily.loc[idx, "date"].strftime("%Y-%m-%d")
    close = pd.to_numeric(daily[config.close_col], errors="coerce")
    _, labels = make_labels(daily, config.close_col, config.threshold)
    features, origin_map = build_feature_matrix(daily, config)
    fallback_signal = build_fallback_signal(daily, config.close_col)
    vote_signals = build_vote_signals(daily, config)

    eligible = np.flatnonzero(
        (np.arange(len(daily)) < idx)
        & np.isin(labels, [-1.0, 1.0])
        & close.notna().to_numpy()
    )
    if len(eligible) > config.window:
        eligible = eligible[-config.window :]

    model = None
    train_start = None
    train_end = None
    if len(eligible) < min(120, config.window) or len(np.unique(labels[eligible])) < 2:
        prob = 0.5
        threshold_used = 0.5
        base_pred = int(fallback_signal.iloc[idx])
        decision = "cold_fallback"
    else:
        split = max(80, int(len(eligible) * 0.80))
        fit_idx = eligible[:split]
        cal_idx = eligible[split:]
        model = _train_model(config)
        model.fit(features.iloc[fit_idx], (labels[fit_idx] == 1).astype(int))
        prob = float(model.predict_proba(features.iloc[[idx]])[:, 1][0])
        cal_prob = model.predict_proba(features.iloc[cal_idx])[:, 1] if len(cal_idx) else np.array([])
        threshold_used = choose_threshold(cal_prob, labels[cal_idx] if len(cal_idx) else np.array([]))
        model_pred = 1 if prob >= threshold_used else -1
        if abs(prob - threshold_used) <= config.epsilon:
            base_pred = int(fallback_signal.iloc[idx])
            decision = "anti_lag20"
        else:
            base_pred = model_pred
            decision = "model"
        train_start = daily.loc[fit_idx[0], "date"].strftime("%Y-%m-%d")
        train_end = daily.loc[fit_idx[-1], "date"].strftime("%Y-%m-%d")

    pred_label, vote_sum, combined_decision = _combine_vote(config, base_pred, vote_signals.iloc[idx])
    return PredictionResult(
        rdate=target_date,
        target_date=target_date,
        feature_date=feature_date,
        tenor=config.tenor,
        frequency=config.frequency,
        pred_label=int(pred_label),
        prob_up=float(prob),
        threshold_used=float(threshold_used),
        base_pred=int(base_pred),
        base_decision=decision,
        vote_sum=int(vote_sum),
        decision=combined_decision,
        feature_columns=features.columns.tolist(),
        feature_origin_map=origin_map,
        train_start=train_start,
        train_end=train_end,
        model=model,
        config=config,
    )
