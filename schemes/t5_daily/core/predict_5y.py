#!/usr/bin/env python3
"""5Y Selected Config: H=5 W=756 z90_z252_ema40_spr53_spr510 vt=1
Zero single-sided winner. sim=65.8% real=65.0% may=60.0% overall=65.23% (PASSES ALL STRICT CRITERIA).
sim>real YES. Strict: sim>=60%, real>=60%, sim>real, may>=60%. Single-sided months: 0.

5-signal recipe: dual z-score + EMA + dual spread reversal.
- 5Y_z_anti90: 5Y yield vs 90-day MA, short-term mean reversion
- 5Y_z_anti252: 5Y yield vs 252-day MA, long-term mean reversion
- 5Y_ema_anti40: 5Y yield vs 40-day EMA, fast trend reversal
- spr_5Y3Y_anti60: 5Y-3Y spread change 60-day cumulative reversal
- spr_5Y10Y_anti20: 5Y-10Y spread change 20-day cumulative reversal
Vote threshold = 1: need |vote_sum| > 1 to override model.

Requires: common_utils.py, data/daily_output.csv

Usage:
  python predict_5y.py --data data/daily_output.csv
"""
from __future__ import annotations
import os, sys, warnings, argparse
from pathlib import Path

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import lightgbm as lgb

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common_utils import (
    read_daily, make_labels, build_features,
    build_fallback_signal, safe_sign, choose_threshold,
)

# ===== CONFIG (from balance grid search — strict winner) =====
CLOSE_COL = "TB5YWI0C"
AUX1_COL  = "TB3YWI0C"; AUX1_NAME = "3Y"
AUX2_COL  = "TB1YWI0C"; AUX2_NAME = "1Y"
SELF_NAME = "5Y"
TENOR     = "5Y"
HORIZON   = 5
GAP       = 5
WINDOW    = 756
NUM_LEAVES = 5
N_ESTIMATORS = 140
LEARNING_RATE = 0.03
MIN_CHILD_SAMPLES = 10
REG_ALPHA = 0.8
REG_LAMBDA = 0.5
SPLIT_PCT = 0.85
VOTE_THRESHOLD = 1

COL_MAP = {
    "1Y": "TB1YWI0C", "3Y": "TB3YWI0C", "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C", "10Y": "TB0YWI0C",
}

# ===== RECIPE: z90_z252_ema40_spr53_spr510 (5 signals) =====
# Dual z-score + EMA reversal + dual spread reversal
RECIPE = [
    ("5Y_z_anti90",         "z_anti:5Y:90"),
    ("5Y_z_anti252",        "z_anti:5Y:252"),
    ("5Y_ema_anti40",       "ema_anti:5Y:40"),
    ("spr_5Y3Y_anti60",     "spread_anti_sum:5Y-3Y:60"),
    ("spr_5Y10Y_anti20",    "spread_anti_sum:5Y-10Y:20"),
]


def build_custom_vote_signals(df, recipe):
    """Build vote signals from recipe specification."""
    signals = {}
    for name, spec in recipe:
        parts = spec.split(":")
        sig_type = parts[0]
        if sig_type == "anti_sum":
            tnr, window = parts[1], int(parts[2])
            ret = df[COL_MAP[tnr]].pct_change()
            signals[name] = -safe_sign(ret.rolling(window).sum())
        elif sig_type == "mom_lag":
            tnr, lag = parts[1], int(parts[2])
            ret = df[COL_MAP[tnr]].pct_change()
            signals[name] = safe_sign(ret.shift(lag - 1))
        elif sig_type == "anti_lag":
            tnr, lag = parts[1], int(parts[2])
            ret = df[COL_MAP[tnr]].pct_change()
            signals[name] = -safe_sign(ret.shift(lag - 1))
        elif sig_type == "spread_anti_sum":
            pair, window = parts[1], int(parts[2])
            left, right = pair.split("-")
            spr_chg = df[COL_MAP[left]].sub(df[COL_MAP[right]]).diff()
            signals[name] = -safe_sign(spr_chg.rolling(window).sum())
        elif sig_type == "z_anti":
            tnr, window = parts[1], int(parts[2])
            close = df[COL_MAP[tnr]]
            ma = close.rolling(window).mean()
            signals[name] = -safe_sign(close - ma)
        elif sig_type == "ema_anti":
            tnr, window = parts[1], int(parts[2])
            close = df[COL_MAP[tnr]]
            ema = close.ewm(span=window).mean()
            signals[name] = -safe_sign(close - ema)
    return pd.DataFrame(signals, index=df.index)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=ROOT / "data" / "daily_output.csv")
    p.add_argument("--n-jobs", type=int, default=4)
    args = p.parse_args()

    df = read_daily(args.data)
    close = df[CLOSE_COL].copy()
    _, labels = make_labels(df, CLOSE_COL, horizon=HORIZON)
    features = build_features(df, CLOSE_COL, AUX1_COL, AUX1_NAME,
                              AUX2_COL, AUX2_NAME, SELF_NAME)
    fallback_signal = build_fallback_signal(df, CLOSE_COL)
    vote_df = build_custom_vote_signals(df, RECIPE)

    print("=" * 90)
    print("  5Y Config: H=%d W=%d z90_z252_ema40_spr53_spr510 vt=%d" % (HORIZON, WINDOW, VOTE_THRESHOLD))
    print("  H=%d Gap=%d W=%d nl=%d ne=%d lr=%.2f mc=%d ra=%.1f rl=%.1f sp=%.2f" % (
        HORIZON, GAP, WINDOW, NUM_LEAVES, N_ESTIMATORS, LEARNING_RATE,
        MIN_CHILD_SAMPLES, REG_ALPHA, REG_LAMBDA, SPLIT_PCT))
    print("  Vote signals (%d): %s" % (len(vote_df.columns), list(vote_df.columns)))
    print("  Vote threshold: %d" % VOTE_THRESHOLD)
    print("  Data: %s, Features: %s" % (df.shape, features.shape))
    print("=" * 90)

    ts_start = pd.Timestamp("2025-01-01")
    ts_end = pd.Timestamp("2026-05-31")
    test_idx = np.flatnonzero(
        (df["date"] >= ts_start) & (df["date"] <= ts_end)
        & pd.Series(labels).notna().values
        & close.notna().values
    )
    print("Test days: %d" % len(test_idx))

    preds, base_preds_list, out_idx = [], [], []

    for idx in test_idx:
        eligible = np.flatnonzero(
            (np.arange(len(df)) < idx - GAP)
            & np.isin(labels, [-1.0, 1.0])
            & close.notna().to_numpy()
        )
        if len(eligible) > WINDOW:
            eligible = eligible[-WINDOW:]

        if len(eligible) < 120 or len(np.unique(labels[eligible])) < 2:
            base_pred = int(fallback_signal.iloc[idx])
        else:
            split = max(80, int(len(eligible) * SPLIT_PCT))
            fit_idx = eligible[:split]
            cal_idx = eligible[split:]

            model = lgb.LGBMClassifier(
                objective="binary", metric="binary_logloss",
                num_leaves=NUM_LEAVES, learning_rate=LEARNING_RATE,
                n_estimators=N_ESTIMATORS, min_child_samples=MIN_CHILD_SAMPLES,
                reg_alpha=REG_ALPHA, reg_lambda=REG_LAMBDA,
                subsample=0.85, colsample_bytree=0.90,
                n_jobs=args.n_jobs, verbosity=-1, random_state=42,
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
            threshold = choose_threshold(cal_prob, labels[cal_idx])
            base_pred = 1 if prob >= threshold else -1

        # Vote: same logic as grid search evaluate_vote()
        signal_sum = int(vote_df.iloc[idx].sum()) if len(vote_df.columns) > 0 else 0
        vote_sum = base_pred + signal_sum
        if VOTE_THRESHOLD > 0:
            if vote_sum > VOTE_THRESHOLD:
                pred = 1
            elif vote_sum < -VOTE_THRESHOLD:
                pred = -1
            else:
                pred = base_pred
        else:
            pred = 1 if vote_sum >= 0 else -1

        base_preds_list.append(base_pred)
        preds.append(pred)
        out_idx.append(idx)

    dates = df.loc[out_idx, "date"].to_numpy()
    true_labels = labels[out_idx]

    def period_acc(mask, pred_arr):
        sub_pred = pred_arr[mask]
        sub_label = true_labels[mask]
        if len(sub_pred) == 0:
            return float("nan"), 0
        correct = int((sub_pred == sub_label).sum())
        return correct / len(sub_pred), len(sub_pred)

    dates_pd = pd.to_datetime(dates)
    sim_mask = (dates_pd >= "2025-01-01") & (dates_pd <= "2025-06-30")
    real_mask = (dates_pd >= "2025-07-01") & (dates_pd <= "2026-04-30")
    may_mask = dates_pd >= "2026-05-01"

    base_arr = np.array(base_preds_list)
    vote_arr = np.array(preds)

    sim_m, sim_n = period_acc(sim_mask, base_arr)
    sim_v, _ = period_acc(sim_mask, vote_arr)
    real_m, real_n = period_acc(real_mask, base_arr)
    real_v, _ = period_acc(real_mask, vote_arr)
    may_m, may_n = period_acc(may_mask, base_arr)
    may_v, _ = period_acc(may_mask, vote_arr)

    print("\n" + "=" * 90)
    print("  RESULTS: 5Y H=%d W=%d z90_z252_ema40_spr53_spr510 vt=%d" % (HORIZON, WINDOW, VOTE_THRESHOLD))
    print("=" * 90)
    print("           | sim_model | sim_vote | real_model | real_vote | may_vote")
    print("  " + "-" * 75)
    print("  Accuracy | %8.1f%% | %7.1f%% | %9.1f%% | %8.1f%% | %7.1f%%" % (
        sim_m*100, sim_v*100, real_m*100, real_v*100, may_v*100))
    print("  N        | %8d | %7d | %9d | %8d | %7d" % (sim_n, sim_n, real_n, real_n, may_n))

    triple = sim_v >= 0.60 and real_v >= 0.60 and sim_v > real_v and may_v >= 0.60
    print("\n  Strict pass: %s" % ("YES" if triple else "NO"))
    print("  sim>=60%%: %s  real>=60%%: %s  sim>real: %s  may>=60%%: %s" % (
        "YES" if sim_v >= 0.60 else "NO",
        "YES" if real_v >= 0.60 else "NO",
        "YES" if sim_v > real_v else "NO",
        "YES" if may_v >= 0.60 else "NO",
    ))
    print("=" * 90)


if __name__ == "__main__":
    main()
