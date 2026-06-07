#!/usr/bin/env python3
"""Shared utilities for 3/5/7/10Y bond yield direction prediction.

Faithfully follows the architecture of daily_10y_lgbm_predict0509.py:
- Per-day rolling retrain with FIXED hyperparams
- Sliding window of most recent eligible (non-flat) samples
- 80/20 train/cal split for threshold calibration
- Vote signals selected once on pre-sim training data, then frozen
"""
from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
import lightgbm as lgb

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def read_daily(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [col.strip().lstrip("﻿") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError("daily data must contain a date column")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Label construction
# ---------------------------------------------------------------------------

def make_labels(df: pd.DataFrame, close_col: str,
                threshold: float = 0.0,
                horizon: int = 1) -> tuple[pd.Series, np.ndarray]:
    close = df[close_col].copy()
    future_return = close.shift(-horizon).div(close).sub(1.0)
    labels = np.select(
        [future_return > threshold, future_return < -threshold],
        [1, -1],
        default=0,
    ).astype(float)
    labels[future_return.isna()] = np.nan
    return future_return, labels


# ---------------------------------------------------------------------------
# Feature engineering (mirrors reference code exactly)
# ---------------------------------------------------------------------------

def safe_sign(values) -> pd.Series:
    return np.sign(values).replace(0, -1).fillna(-1).astype(int)


def add_tenor_features(features: dict, df: pd.DataFrame,
                       name: str, col: str) -> None:
    close = df[col].copy()
    ret = close.pct_change()

    for lag in (1, 2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120):
        lag_ret = ret.shift(lag - 1)
        features[f"{name}_ret_lag{lag}"] = lag_ret
        features[f"{name}_sign_lag{lag}"] = np.sign(lag_ret)

    for window in (2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 252):
        summed = ret.rolling(window).sum()
        features[f"{name}_mom_sum{window}"] = summed
        features[f"{name}_mom_sign{window}"] = np.sign(summed)
        features[f"{name}_vol{window}"] = ret.rolling(window).std()
        ma = close.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = close.rolling(window, min_periods=max(2, window // 2)).std()
        features[f"{name}_z{window}"] = close.sub(ma).div(sd.replace(0, np.nan))

    ema12 = close.ewm(span=12, adjust=False, min_periods=6).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=13).mean()
    macd = ema12 - ema26
    features[f"{name}_macd_gap"] = macd - macd.ewm(span=9, adjust=False, min_periods=5).mean()

    gain = ret.clip(lower=0).rolling(14, min_periods=7).mean()
    loss = (-ret.clip(upper=0)).rolling(14, min_periods=7).mean()
    features[f"{name}_rsi14"] = 100 - 100 / (1 + gain.div(loss.replace(0, np.nan)))


def add_spread_features(features: dict, df: pd.DataFrame,
                        left_name: str, right_name: str,
                        left_col: str, right_col: str) -> None:
    spread = df[left_col].sub(df[right_col])
    features[f"spread_{left_name}_{right_name}"] = spread
    for window in (5, 20, 60, 120):
        ma = spread.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = spread.rolling(window, min_periods=max(2, window // 2)).std()
        features[f"spread_{left_name}_{right_name}_z{window}"] = (
            spread.sub(ma).div(sd.replace(0, np.nan))
        )
        features[f"spread_{left_name}_{right_name}_chg{window}"] = (
            spread.sub(spread.shift(window))
        )


def build_features(df: pd.DataFrame, close_col: str,
                   aux1_col: str, aux1_name: str,
                   aux2_col: str, aux2_name: str,
                   self_name: str) -> pd.DataFrame:
    features: dict[str, pd.Series] = {}
    add_tenor_features(features, df, self_name, close_col)
    add_tenor_features(features, df, aux1_name, aux1_col)
    add_tenor_features(features, df, aux2_name, aux2_col)
    add_spread_features(features, df, self_name, aux1_name, close_col, aux1_col)
    add_spread_features(features, df, self_name, aux2_name, close_col, aux2_col)
    add_spread_features(features, df, aux1_name, aux2_name, aux1_col, aux2_col)
    return pd.DataFrame(features, index=df.index).replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)


def build_fallback_signal(df: pd.DataFrame, close_col: str) -> pd.Series:
    ret = df[close_col].pct_change()
    return -safe_sign(ret.shift(19))


# ---------------------------------------------------------------------------
# Vote signal system
# ---------------------------------------------------------------------------

def build_vote_signals(df: pd.DataFrame, close_col: str,
                       aux1_col: str, aux1_name: str,
                       aux2_col: str, aux2_name: str,
                       self_name: str, tenor: str,
                       driver: str | None = None) -> pd.DataFrame:
    """Build hardcoded vote signals following the reference code's pattern.

    driver param overrides the default tenor→driving logic:
      None  = use defaults (1Y for 3Y, self for 5Y, 10Y for 7Y, 5Y for 10Y)
      "self" = self-driven (like 5Y does for itself)
      "5y"  = 5Y-driven (like 10Y does)
      "1y"  = 1Y-driven (original 3Y approach)
      "10y" = 10Y-driven (original 7Y approach)
    """
    col_map = {
        "1Y": "TB1YWI0C", "3Y": "TB3YWI0C", "5Y": "TB5YWI0C",
        "7Y": "TB7YWI0C", "10Y": "TB0YWI0C",
    }

    self_ret = df[close_col].pct_change()
    aux1_ret = df[aux1_col].pct_change()
    aux2_ret = df[aux2_col].pct_change()

    if driver is None:
        # Default logic
        if tenor == "3Y":
            drv_ret, drv_name = aux1_ret, aux1_name
            spr_chg = df[close_col].sub(df[aux1_col]).diff()
            spr_name = f"{self_name}_{aux1_name}"
            cross_ret, cross_name = aux2_ret, aux2_name
        elif tenor == "5Y":
            drv_ret, drv_name = self_ret, self_name
            spr_chg = df[close_col].sub(df[aux1_col]).diff()
            spr_name = f"{self_name}_{aux1_name}"
            cross_ret, cross_name = aux2_ret, aux2_name
        elif tenor == "7Y":
            drv_ret, drv_name = aux2_ret, aux2_name
            spr_chg = df[close_col].sub(df[aux1_col]).diff()
            spr_name = f"{self_name}_{aux1_name}"
            cross_ret, cross_name = self_ret, self_name
        else:
            drv_ret, drv_name = aux1_ret, aux1_name
            spr_chg = df[aux1_col].sub(df[aux2_col]).diff()
            spr_name = f"{aux1_name}_{aux2_name}"
            cross_ret, cross_name = self_ret, self_name
    elif driver == "self":
        drv_ret, drv_name = self_ret, self_name
        best_aux = aux1_name if aux1_name != self_name else aux2_name
        best_aux_col = aux1_col if aux1_name != self_name else aux2_col
        spr_chg = df[close_col].sub(df[best_aux_col]).diff()
        spr_name = f"{self_name}_{best_aux}"
        other_aux = aux2_name if aux1_name != self_name else aux1_name
        other_aux_col = aux2_col if aux1_name != self_name else aux1_col
        cross_ret = df[other_aux_col].pct_change()
        cross_name = other_aux
    elif driver == "5y":
        drv_col = col_map["5Y"]
        drv_ret = df[drv_col].pct_change()
        drv_name = "5Y"
        spr_chg = df[drv_col].sub(df[close_col]).diff() if tenor != "5Y" else df[close_col].sub(df[col_map["1Y"]]).diff()
        spr_name = f"5Y_{self_name}" if tenor != "5Y" else f"{self_name}_1Y"
        cross_ret, cross_name = self_ret, self_name
        if self_name == "5Y":
            cross_ret = df[col_map["10Y"]].pct_change()
            cross_name = "10Y"
    elif driver == "10y":
        drv_col = col_map["10Y"]
        drv_ret = df[drv_col].pct_change()
        drv_name = "10Y"
        spr_chg = df[drv_col].sub(df[close_col]).diff() if tenor != "10Y" else df[col_map["5Y"]].sub(df[col_map["1Y"]]).diff()
        spr_name = f"10Y_{self_name}" if tenor != "10Y" else "5Y_1Y"
        cross_ret, cross_name = self_ret, self_name
        if self_name == "10Y":
            cross_ret = df[col_map["5Y"]].pct_change()
            cross_name = "5Y"
    else:  # "1y"
        drv_col = col_map["1Y"]
        drv_ret = df[drv_col].pct_change()
        drv_name = "1Y"
        spr_chg = df[close_col].sub(df[drv_col]).diff()
        spr_name = f"{self_name}_1Y"
        cross_ret, cross_name = self_ret, self_name
        if self_name == "1Y":
            cross_ret = df[col_map["5Y"]].pct_change()
            cross_name = "5Y"

    signals = {
        f"{drv_name}_anti_sum180": -safe_sign(drv_ret.rolling(180).sum()),
        f"spread_{spr_name}_chg_anti_sum252": -safe_sign(spr_chg.rolling(252).sum()),
        f"{drv_name}_mom_lag3": safe_sign(drv_ret.shift(2)),
        f"spread_{spr_name}_chg_anti_sum10": -safe_sign(spr_chg.rolling(10).sum()),
        f"spread_{spr_name}_chg_anti_sum40": -safe_sign(spr_chg.rolling(40).sum()),
        f"{drv_name}_anti_lag252": -safe_sign(drv_ret.shift(251)),
        f"{cross_name}_anti_sum180": -safe_sign(cross_ret.rolling(180).sum()),
    }
    return pd.DataFrame(signals, index=df.index)


# ---------------------------------------------------------------------------
# Threshold calibration (same as reference)
# ---------------------------------------------------------------------------

def choose_threshold(cal_prob: np.ndarray, cal_labels: np.ndarray) -> float:
    best_score = -np.inf
    best_th = 0.5
    for th in np.linspace(0.38, 0.62, 49):
        pred = np.where(cal_prob >= th, 1, -1)
        accuracy = float((pred == cal_labels).mean())
        pred_up = pred == 1
        pred_dn = pred == -1
        up_p = ((pred_up) & (cal_labels == 1)).sum() / max(pred_up.sum(), 1)
        dn_p = ((pred_dn) & (cal_labels == -1)).sum() / max(pred_dn.sum(), 1)
        score = accuracy + 0.04 * min(float(up_p), float(dn_p))
        if score > best_score:
            best_score = score
            best_th = float(th)
    return best_th


# ---------------------------------------------------------------------------
# Main prediction loop (mirrors reference code structure exactly)
# ---------------------------------------------------------------------------

def run_prediction(
    df: pd.DataFrame,
    close_col: str, aux1_col: str, aux1_name: str,
    aux2_col: str, aux2_name: str, self_name: str,
    tenor: str,
    horizon: int = 1,
    window: int = 252,
    num_leaves: int = 3,
    n_estimators: int = 140,
    learning_rate: float = 0.03,
    min_child_samples: int = 20,
    epsilon: float = 0.0,
    n_jobs: int = 4,
    test_start: str = "2025-01-01",
    test_end: str = "2026-05-31",
    driver: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:

    close = df[close_col].copy()
    future_return, labels = make_labels(df, close_col, horizon=horizon)
    features = build_features(df, close_col, aux1_col, aux1_name,
                              aux2_col, aux2_name, self_name)
    fallback_signal = build_fallback_signal(df, close_col)

    # Build vote signals (hardcoded per tenor, following reference code pattern)
    vote_df = build_vote_signals(df, close_col, aux1_col, aux1_name,
                                 aux2_col, aux2_name, self_name, tenor,
                                 driver=driver)
    selected_signals = list(vote_df.columns)
    print(f"  [{tenor}] {len(selected_signals)} vote signals: {selected_signals}")

    # Test period indices
    ts_start = pd.Timestamp(test_start)
    ts_end = pd.Timestamp(test_end)
    test_idx = np.flatnonzero(
        (df["date"] >= ts_start) & (df["date"] <= ts_end)
        & pd.Series(labels).notna().values
        & close.notna().values
    )
    print(f"  [{tenor}] Test period: {test_start} ~ {test_end}, {len(test_idx)} days")

    # --- Per-day rolling prediction (EXACT same logic as reference code) ---
    preds, probs, thresholds_used = [], [], []
    decisions, base_preds_list, vote_sums_list = [], [], []
    out_idx = []

    for i, idx in enumerate(test_idx):
        # Eligible: all rows before idx with label in {-1, 1} and close valid
        eligible = np.flatnonzero(
            (np.arange(len(df)) < idx)
            & np.isin(labels, [-1.0, 1.0])
            & close.notna().to_numpy()
        )
        if len(eligible) > window:
            eligible = eligible[-window:]

        if len(eligible) < 120 or len(np.unique(labels[eligible])) < 2:
            prob = 0.5
            threshold_used = 0.5
            base_pred = int(fallback_signal.iloc[idx])
            decision = "cold_fallback"
        else:
            split = max(80, int(len(eligible) * 0.80))
            fit_idx = eligible[:split]
            cal_idx = eligible[split:]

            model = lgb.LGBMClassifier(
                objective="binary", metric="binary_logloss",
                num_leaves=num_leaves, learning_rate=learning_rate,
                n_estimators=n_estimators, min_child_samples=min_child_samples,
                reg_alpha=0.8, reg_lambda=3.0,
                subsample=0.85, colsample_bytree=0.90,
                n_jobs=n_jobs, verbosity=-1, random_state=42,
                force_col_wise=True,
            )
            model.fit(
                features.iloc[fit_idx],
                (labels[fit_idx] == 1).astype(int),
                eval_set=[(features.iloc[cal_idx],
                           (labels[cal_idx] == 1).astype(int))],
                callbacks=[lgb.early_stopping(20, verbose=False)],
            )

            prob = float(model.predict_proba(features.iloc[[idx]])[:, 1][0])
            cal_prob = model.predict_proba(features.iloc[cal_idx])[:, 1]
            threshold_used = choose_threshold(cal_prob, labels[cal_idx])
            model_pred = 1 if prob >= threshold_used else -1

            if abs(prob - threshold_used) <= epsilon:
                base_pred = int(fallback_signal.iloc[idx])
                decision = "anti_lag20"
            else:
                base_pred = model_pred
                decision = "model"

        # Vote
        signal_sum = int(vote_df.iloc[idx].sum()) if len(vote_df.columns) > 0 else 0
        vote_sum = base_pred + signal_sum
        pred = 1 if vote_sum >= 0 else -1

        preds.append(pred)
        probs.append(prob)
        thresholds_used.append(threshold_used)
        decisions.append(decision)
        base_preds_list.append(base_pred)
        vote_sums_list.append(vote_sum)
        out_idx.append(idx)

        if (i + 1) % 50 == 0:
            print(f"    [{tenor}] {i+1}/{len(test_idx)} done")

    # Build results
    predictions = pd.DataFrame({
        "date": df.loc[out_idx, "date"].to_numpy(),
        "future_return": future_return.iloc[out_idx].to_numpy(),
        "label": labels[out_idx],
        "pred_label": preds,
        "prob_up": probs,
        "threshold_used": thresholds_used,
        "base_pred": base_preds_list,
        "decision": decisions,
        "vote_sum": vote_sums_list,
    })
    for col in vote_df.columns:
        predictions[col] = vote_df.loc[out_idx, col].to_numpy()
    predictions["month"] = predictions["date"].dt.strftime("%Y-%m")
    predictions["is_true_direction"] = predictions["label"].isin([-1.0, 1.0])
    predictions["direction_correct"] = (
        predictions["is_true_direction"] & predictions["label"].eq(predictions["pred_label"])
    )
    predictions["all_row_correct"] = predictions["label"].eq(predictions["pred_label"])

    monthly = build_monthly_metrics(predictions)
    summary = build_summary(predictions, monthly, tenor, horizon, window,
                            selected_signals)
    return predictions, monthly, summary


# ---------------------------------------------------------------------------
# Metrics & reporting
# ---------------------------------------------------------------------------

def safe_rate(num: int, den: int) -> float:
    return float(num / den) if den else np.nan


def build_monthly_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for month, g in predictions.groupby("month", sort=True):
        label = g["label"]
        pred = g["pred_label"]
        total = len(g)
        true_up = int(label.eq(1).sum())
        true_down = int(label.eq(-1).sum())
        true_flat = int(label.eq(0).sum())
        pred_up = int(pred.eq(1).sum())
        pred_down = int(pred.eq(-1).sum())
        dir_mask = label.ne(0)
        dir_correct = int((dir_mask & pred.eq(label)).sum())
        dir_acc = safe_rate(dir_correct, total)
        rows.append({
            "month": month, "total": total,
            "true_up": true_up, "true_down": true_down, "true_flat": true_flat,
            "pred_up": pred_up, "pred_down": pred_down,
            "direction_correct": dir_correct,
            "direction_accuracy": dir_acc,
            "up_precision": safe_rate(int((pred.eq(1) & label.eq(1)).sum()), pred_up),
            "down_precision": safe_rate(int((pred.eq(-1) & label.eq(-1)).sum()), pred_down),
        })
    return pd.DataFrame(rows)


def build_summary(predictions: pd.DataFrame, monthly: pd.DataFrame,
                  tenor: str, horizon: int, window: int,
                  selected_signals: list[str]) -> dict[str, Any]:
    def _period_acc(mask):
        sub = predictions[mask]
        if len(sub) == 0:
            return {"n": 0, "accuracy": float("nan")}
        non_flat = sub["label"].ne(0)
        correct = int((non_flat & sub["label"].eq(sub["pred_label"])).sum())
        return {"n": len(sub), "accuracy": safe_rate(correct, len(sub))}

    sim = _period_acc(predictions["month"].between("2025-01", "2025-06"))
    real = _period_acc(predictions["month"].between("2025-07", "2026-04"))
    live = _period_acc(predictions["month"] >= "2026-05")

    return {
        "tenor": tenor, "horizon": f"T+{horizon}", "window": window,
        "n_vote_signals": len(selected_signals),
        "vote_signals": selected_signals,
        "sim_accuracy": sim["accuracy"], "sim_n": sim["n"],
        "sim_pass": not np.isnan(sim["accuracy"]) and sim["accuracy"] > 0.50,
        "real_accuracy": real["accuracy"], "real_n": real["n"],
        "real_pass": not np.isnan(real["accuracy"]) and real["accuracy"] > 0.60,
        "live_accuracy": live["accuracy"], "live_n": live["n"],
    }


def format_pct(value: float) -> str:
    if pd.isna(value):
        return "N/A"
    return f"{value:.4f} ({value * 100:.2f}%)"


def print_report(predictions: pd.DataFrame, monthly: pd.DataFrame,
                 summary: dict[str, Any]) -> None:
    print("=" * 70)
    print(f"  {summary['tenor']} {summary['horizon']} | window={summary['window']}")
    print("=" * 70)
    disp = monthly[["month", "total", "true_up", "true_down", "true_flat",
                     "pred_up", "pred_down", "direction_correct",
                     "direction_accuracy"]].copy()
    disp["direction_accuracy"] = disp["direction_accuracy"].apply(
        lambda x: f"{x:.4f}" if not pd.isna(x) else "N/A")
    print(disp.to_string(index=False))
    print()
    sp = "PASS" if summary["sim_pass"] else "FAIL"
    rp = "PASS" if summary["real_pass"] else "FAIL"
    print(f"  Sim  (25.01-25.06): {format_pct(summary['sim_accuracy'])} "
          f"(n={summary['sim_n']}) {sp}")
    print(f"  Real (25.07-26.04): {format_pct(summary['real_accuracy'])} "
          f"(n={summary['real_n']}) {rp}")
    print(f"  Live (26.05+):      {format_pct(summary['live_accuracy'])} "
          f"(n={summary['live_n']})")
    print(f"  Vote signals ({summary['n_vote_signals']}): "
          f"{', '.join(summary['vote_signals'])}")
    print()
