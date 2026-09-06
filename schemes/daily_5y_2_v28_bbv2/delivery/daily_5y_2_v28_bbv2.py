#!/usr/bin/env python3
"""V28 日频算法的自包含 Blackbox V2 successor。"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMBA_NUM_THREADS"] = "1"

import numpy as np
import pandas as pd

def align_weekly_previous_complete(
    weekly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
    date_to_week: Mapping[str, int | str],
) -> pd.DataFrame:
    """把周频输入对齐到每个日频样本的上一完整周。"""
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if "week_id" not in weekly_df.columns:
        return pd.DataFrame(index=range(len(daily_index)))

    weekly = weekly_df.copy()
    weekly["week_id"] = pd.to_numeric(weekly["week_id"], errors="coerce")
    weekly = weekly.dropna(subset=["week_id"]).copy()
    weekly["week_id"] = weekly["week_id"].astype(int)
    weekly = weekly.sort_values("week_id").drop_duplicates("week_id", keep="last")
    value_cols = [col for col in weekly.columns if col != "week_id"]
    if not value_cols:
        return pd.DataFrame(index=range(len(daily_index)))

    week_ids = weekly["week_id"].tolist()
    weekly_by_id = weekly.set_index("week_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_index), np.nan, dtype=np.float64) for col in value_cols
    }
    for row_index, daily_date in enumerate(daily_index):
        current_week = _week_id_for_date(daily_date, date_to_week)
        if current_week is None:
            continue
        previous_candidates = [week_id for week_id in week_ids if week_id < current_week]
        if not previous_candidates:
            continue
        previous_week = previous_candidates[-1]
        for col in value_cols:
            result[col][row_index] = weekly_by_id.at[previous_week, col]
    return pd.DataFrame(result, index=range(len(daily_index)))


def align_monthly_previous_month(
    monthly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
) -> pd.DataFrame:
    """把月频输入对齐到每个日频样本的上一自然月，month_id 严格使用 YYYYMM。"""
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if "month_id" not in monthly_df.columns:
        return pd.DataFrame(index=range(len(daily_index)))

    monthly = monthly_df.copy()
    monthly["month_id"] = monthly["month_id"].astype(str).str.strip()
    monthly = monthly[monthly["month_id"].str.fullmatch(r"\d{6}", na=False)]
    monthly = monthly.sort_values("month_id").drop_duplicates("month_id", keep="last")
    value_cols = [col for col in monthly.columns if col != "month_id"]
    if not value_cols:
        return pd.DataFrame(index=range(len(daily_index)))

    monthly_by_id = monthly.set_index("month_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_index), np.nan, dtype=np.float64) for col in value_cols
    }
    previous_month_ids = [_previous_month_id(daily_date) for daily_date in daily_index]
    for row_index, month_id in enumerate(previous_month_ids):
        if month_id not in monthly_by_id.index:
            continue
        for col in value_cols:
            result[col][row_index] = monthly_by_id.at[month_id, col]
    return pd.DataFrame(result, index=range(len(daily_index)))


def _week_id_for_date(
    daily_date: pd.Timestamp,
    date_to_week: Mapping[str, int | str],
) -> int | None:
    value = date_to_week.get(daily_date.strftime("%Y-%m-%d"))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _previous_month_id(daily_date: pd.Timestamp) -> str:
    year = int(daily_date.year)
    month = int(daily_date.month) - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}{month:02d}"

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Optional accelerators
# ---------------------------------------------------------------------------
try:
    import bottleneck as bn
    _HAS_BN = True
except ImportError:
    _HAS_BN = False

try:
    from numba import njit as _njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False
    def _njit(*args, **kwargs):
        """Identity decorator when numba is unavailable."""
        if args and callable(args[0]):
            return args[0]
        def _wrap(fn):
            return fn
        return _wrap

# ---------------------------------------------------------------------------
# Constants shared across ALL tenors
# ---------------------------------------------------------------------------
HORIZON = 5
PURGE_GAP = 5

COL_MAP = {
    "1Y": "TB1YWI0C",
    "3Y": "TB3YWI0C",
    "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C",
    "10Y": "TB0YWI0C",
}

MF_CATEGORIES = {
    "equity":           ["SH000300", "IFCFE00C"],
    "commodity_energy": ["S0031525"],
    "commodity_metal":  ["AUSHF00C", "CUSHF01C", "RBSHF01C"],
    "fx":               ["USDCNH0C", "SX5EDF0C"],
    "money_market":     ["DR007IBC", "M0017153"],
    "credit":           ["G0006352", "G0006353"],
    "macro":            ["M0000005", "M0000271", "M0048486"],
    "sentiment":        ["G0003956", "B2559386", "G0003892"],
    "swap":             ["DRS00001", "DRS00002"],
    "commodity_other":  ["AGSHF01C", "ZNSHF00C", "HCSHF01C"],
}

MONTHLY_COLS = [
    "M0000545", "M0041340", "M0041341", "M0041342",
    "M0061518", "M0096870", "M0317126",
    "M0009970", "M0009973", "M0001227",
]
WEEKLY_COLS = [
    "S0114089", "N1355677", "V0135838", "V0184553",
    "W0192843", "X0100205", "Y0110594",
    "HWW00001", "HWW00002", "HWW00003",
]

MODEL_VERSION = "5y_2_v28"

BASE_5Y_2_CONFIG: dict[str, Any] = {
    "name": "predict_5y_2",
    "tenor": "5Y",
    "close": "TB5YWI0C",
    "self_name": "5Y",
    "aux_pairs": [("3Y", "TB3YWI0C"), ("10Y", "TB0YWI0C")],
    "lgbm_windows": [200, 350, 504, 756],
    "lgbm_leaves": [3, 5, 7, 15],
    "lgbm_min_child": [10, 20],
    "lgbm_alpha": [0.0, 1.0],
    "lgbm_lambda": [0.5, 3.0],
    "lgbm_split": [0.55, 0.70],
    "lgbm_slow_path": True,
    "seeds": [42, 314, 159],
    "K": 15,
    "combo_name": "pol_tp_bf",
    "combo_template": {"policy": 2, "term_prem": 2, "butterfly": 2},
    "lgbm_w": 1.30,
    "vt": 0.15,
    "rebal": "monthly",
    "ew": 378,
    "min_acc": 0.35,
    "ml_mode": "prob",
    "sig_mode": "equal",
    "ens_mode": "standard",
    "ic_top_self": 50,
    "ic_top_mf": 30,
}


def model_config(**overrides) -> dict[str, Any]:
    """返回 5y_2 v28 固定参数副本。"""
    cfg = dict(BASE_5Y_2_CONFIG)
    cfg.update(overrides)
    return cfg

# ============================================================================
# Rolling helpers -- use bottleneck when available
# ============================================================================
def _rolling_mean(arr: np.ndarray, w: int, min_p: int | None = None) -> np.ndarray:
    """1-D rolling mean.  Uses bottleneck if available."""
    mp_ = w if min_p is None else min_p
    if _HAS_BN:
        out = bn.move_mean(arr, window=w, min_count=mp_)
        return out
    return pd.Series(arr).rolling(w, min_periods=mp_).mean().values


def _rolling_std(arr: np.ndarray, w: int, min_p: int | None = None) -> np.ndarray:
    """1-D rolling std.  Uses bottleneck if available."""
    mp_ = w if min_p is None else min_p
    if _HAS_BN:
        out = bn.move_std(arr, window=w, min_count=mp_, ddof=1)
        return out
    return pd.Series(arr).rolling(w, min_periods=mp_).std().values


def _rolling_sum(arr: np.ndarray, w: int) -> np.ndarray:
    if _HAS_BN:
        return bn.move_sum(arr, window=w, min_count=w)
    return pd.Series(arr).rolling(w).sum().values


def _rolling_min(arr: np.ndarray, w: int) -> np.ndarray:
    if _HAS_BN:
        return bn.move_min(arr, window=w, min_count=w)
    return pd.Series(arr).rolling(w).min().values


def _rolling_max(arr: np.ndarray, w: int) -> np.ndarray:
    if _HAS_BN:
        return bn.move_max(arr, window=w, min_count=w)
    return pd.Series(arr).rolling(w).max().values


def _rolling_var(arr: np.ndarray, w: int) -> np.ndarray:
    if _HAS_BN:
        s = bn.move_std(arr, window=w, min_count=w, ddof=1)
        return s ** 2
    return pd.Series(arr).rolling(w).var().values


# ============================================================================
# DATA LOADING
# ============================================================================
def read_daily(path) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass daily_df")


def load_monthly(path, daily_dates) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass monthly_df")


def load_weekly_simple(path, daily_dates) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass weekly_df")


# ============================================================================
# LABELS
# ============================================================================
def safe_sign(values):
    """np.sign but 0 -> -1, NaN -> -1.  Works on Series and ndarray."""
    if isinstance(values, pd.Series):
        return np.sign(values).replace(0, -1).fillna(-1).astype(int)
    out = np.sign(values)
    out[out == 0] = -1
    out[np.isnan(out)] = -1
    return out.astype(int)


def make_labels(df, close_col, horizon=5) -> np.ndarray:
    close = df[close_col].values.astype(np.float64)
    n = len(close)
    fr = np.full(n, np.nan)
    fr[:n - horizon] = close[horizon:] / close[:n - horizon] - 1.0
    labels = np.select([fr > 0, fr < 0], [1.0, -1.0], default=0.0)
    labels[np.isnan(fr)] = np.nan
    return labels


def build_fallback_signal(df, close_col):
    return -safe_sign(df[close_col].pct_change().shift(19))


# ============================================================================
# FEATURE ENGINEERING
# ============================================================================
def build_bond_features(df, close_col: str, aux_pairs: list) -> pd.DataFrame:
    """Build bond yield features.

    Parameters
    ----------
    df : DataFrame with date + yield columns
    close_col : primary close column, e.g. "TB0YWI0C"
    aux_pairs : [(name, col), ...], e.g. [("5Y", "TB5YWI0C"), ("1Y", "TB1YWI0C")]
    """
    # Determine the self-name from COL_MAP reverse lookup
    self_name = None
    for k, v in COL_MAP.items():
        if v == close_col:
            self_name = k
            break
    if self_name is None:
        self_name = "self"

    all_pairs = [(self_name, close_col)] + list(aux_pairs)

    features = {}
    for name, col in all_pairs:
        close = df[col].values.astype(np.float64)
        ret = np.empty_like(close)
        ret[0] = np.nan
        ret[1:] = close[1:] / close[:-1] - 1.0

        # Lag returns
        for lag in (1, 2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120):
            lr = np.empty_like(ret)
            lr[:lag] = np.nan
            lr[lag:] = ret[1:len(ret) - lag + 1] if lag >= 1 else ret
            # Use pandas shift for correctness (matches original exactly)
            lr_s = pd.Series(ret).shift(lag - 1)
            features[f"{name}_ret_lag{lag}"] = lr_s.values
            features[f"{name}_sign_lag{lag}"] = np.sign(lr_s.values)

        ret_s = pd.Series(ret)
        close_s = pd.Series(close)

        # Momentum, vol, z-score
        for w in (2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 252):
            s = _rolling_sum(ret, w)
            features[f"{name}_mom_sum{w}"] = s
            features[f"{name}_mom_sign{w}"] = np.sign(s)
            features[f"{name}_vol{w}"] = _rolling_std(ret, w)

            mp_ = max(2, w // 2)
            ma = _rolling_mean(close, w, min_p=mp_)
            sd = _rolling_std(close, w, min_p=mp_)
            sd_safe = np.where(sd == 0, np.nan, sd)
            features[f"{name}_z{w}"] = (close - ma) / sd_safe

        # MACD
        ema12 = close_s.ewm(span=12, adjust=False, min_periods=6).mean().values
        ema26 = close_s.ewm(span=26, adjust=False, min_periods=13).mean().values
        macd = ema12 - ema26
        macd_signal = pd.Series(macd).ewm(span=9, adjust=False, min_periods=5).mean().values
        features[f"{name}_macd_gap"] = macd - macd_signal

        # RSI
        gain = np.clip(ret, 0, None)
        loss = np.clip(-ret, 0, None)
        gain_ma = _rolling_mean(gain, 14, min_p=7)
        loss_ma = _rolling_mean(loss, 14, min_p=7)
        loss_safe = np.where(loss_ma == 0, np.nan, loss_ma)
        features[f"{name}_rsi14"] = 100 - 100 / (1 + gain_ma / loss_safe)

    # V27: Streak + up-fraction features (24 features = 3 tenors × 8)
    for name, col in all_pairs:
        ret_s = pd.Series(df[col].values.astype(np.float64)).pct_change()
        sign_r = np.sign(ret_s).fillna(0).values
        # Running streak count: +N if N consecutive up, -N if consecutive down
        streak = np.zeros(len(sign_r), dtype=np.float64)
        for i in range(1, len(sign_r)):
            if sign_r[i] == sign_r[i - 1] and sign_r[i] != 0:
                streak[i] = streak[i - 1] + sign_r[i]
            else:
                streak[i] = sign_r[i]
        features[f"{name}_streak_count"] = streak
        features[f"{name}_streak_abs"] = np.abs(streak)
        # Rolling fraction of up days in last N days
        up_flag = (ret_s > 0).astype(float)
        for w in [5, 10, 20]:
            uf = up_flag.rolling(w).mean().values
            features[f"{name}_up_frac{w}"] = uf
            features[f"{name}_up_frac{w}_dev"] = uf - 0.5

    # Spread features
    pair_list = []
    self_col = close_col
    for aname, acol in aux_pairs:
        pair_list.append((self_name, aname, self_col, acol))
    # Cross-aux pairs
    if len(aux_pairs) >= 2:
        for i in range(len(aux_pairs)):
            for j in range(i + 1, len(aux_pairs)):
                pair_list.append((aux_pairs[i][0], aux_pairs[j][0],
                                  aux_pairs[i][1], aux_pairs[j][1]))

    for ln, rn, lc, rc in pair_list:
        spr = df[lc].values - df[rc].values
        features[f"spread_{ln}_{rn}"] = spr
        for w in (5, 20, 60, 120):
            mp_ = max(2, w // 2)
            ma = _rolling_mean(spr, w, min_p=mp_)
            sd = _rolling_std(spr, w, min_p=mp_)
            sd_safe = np.where(sd == 0, np.nan, sd)
            features[f"spread_{ln}_{rn}_z{w}"] = (spr - ma) / sd_safe
            spr_shifted = np.empty_like(spr)
            spr_shifted[:w] = np.nan
            spr_shifted[w:] = spr[:-w]
            features[f"spread_{ln}_{rn}_chg{w}"] = spr - spr_shifted

    out = pd.DataFrame(features, index=df.index)
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    out.ffill(inplace=True)
    out.fillna(0.0, inplace=True)
    return out


def build_mf_features(df, mf_categories: dict | None = None) -> Tuple[pd.DataFrame, dict]:
    """Build market factor features.  Returns (feature_df, feat_cat_map)."""
    if mf_categories is None:
        mf_categories = MF_CATEGORIES
    feats = {}
    feat_cat = {}
    for cat, cols in mf_categories.items():
        for col in cols:
            if col not in df.columns:
                continue
            v = pd.to_numeric(df[col], errors="coerce").ffill()
            vl = v.shift(1).values.astype(np.float64)
            prefix = f"mf_{col}"

            # Returns
            r1 = np.empty_like(vl); r1[0] = np.nan
            r1[1:] = vl[1:] / vl[:-1] - 1.0
            feats[f"{prefix}_r1"] = r1

            r5 = np.empty_like(vl); r5[:5] = np.nan
            r5[5:] = vl[5:] / vl[:-5] - 1.0
            feats[f"{prefix}_r5"] = r5

            r20 = np.empty_like(vl); r20[:20] = np.nan
            r20[20:] = vl[20:] / vl[:-20] - 1.0
            feats[f"{prefix}_r20"] = r20

            # Z-scores
            ma20 = _rolling_mean(vl, 20, min_p=10)
            sd20 = _rolling_std(vl, 20, min_p=10)
            sd20_safe = np.where(sd20 == 0, np.nan, sd20)
            feats[f"{prefix}_z20"] = (vl - ma20) / sd20_safe

            ma60 = _rolling_mean(vl, 60, min_p=30)
            sd60 = _rolling_std(vl, 60, min_p=30)
            sd60_safe = np.where(sd60 == 0, np.nan, sd60)
            feats[f"{prefix}_z60"] = (vl - ma60) / sd60_safe

            # Momentum
            mom20 = _rolling_sum(r1, 20)
            feats[f"{prefix}_mom20"] = mom20

            for fn in [f"{prefix}_r1", f"{prefix}_r5", f"{prefix}_r20",
                       f"{prefix}_z20", f"{prefix}_z60", f"{prefix}_mom20"]:
                feat_cat[fn] = cat

    out = pd.DataFrame(feats, index=df.index)
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    out.fillna(0.0, inplace=True)
    return out, feat_cat


def build_wkmo_features(wk_df, mo_df,
                        weekly_cols: list | None = None,
                        monthly_cols: list | None = None) -> pd.DataFrame:
    feats = {}
    for col in wk_df.columns:
        v = wk_df[col].ffill().fillna(0).values
        feats[f"wk_{col}_val"] = v
        d = np.empty_like(v); d[0] = 0.0; d[1:] = v[1:] - v[:-1]
        feats[f"wk_{col}_chg"] = d
    for col in mo_df.columns:
        v = mo_df[col].ffill().fillna(0).values
        feats[f"mo_{col}_val"] = v
        d = np.empty_like(v); d[0] = 0.0; d[1:] = v[1:] - v[:-1]
        feats[f"mo_{col}_chg"] = d
    out = pd.DataFrame(feats)
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    out.fillna(0.0, inplace=True)
    return out


# ============================================================================
# IC SCREENING  (vectorised correlation)
# ============================================================================
def ic_screen(feat_vals: np.ndarray, labels: np.ndarray,
              n_self: int, mf_feat_cat: dict, mf_col_names: list,
              top_self: int = 50, top_mf: int = 30,
              min_cats: int = 6) -> Tuple[np.ndarray, int]:
    """Select features by IC on pre-test data.

    Uses matrix-level correlation computation instead of per-column loops.
    """
    n_feat = feat_vals.shape[1]
    valid_rows = ~np.isnan(labels)
    # Also mask rows where any feature is nan (column-wise below)
    ll = labels[valid_rows]

    # Vectorised IC: compute correlation of each column with labels
    fv = feat_vals[valid_rows]
    # Per-column valid mask (handle NaN features)
    ics = np.zeros(n_feat)
    # Demean labels once
    ll_dm = ll - np.nanmean(ll)
    ll_std = np.nanstd(ll)
    if ll_std < 1e-12:
        return np.arange(min(top_self, n_self), dtype=int), 0

    for j in range(n_feat):
        v = fv[:, j]
        mask = ~np.isnan(v)
        if mask.sum() < 50:
            continue
        vv = v[mask]
        lm = ll[mask]
        vs = np.std(vv)
        if vs < 1e-12:
            continue
        ics[j] = abs(np.corrcoef(vv, lm)[0, 1])

    # Self features: top by IC
    self_order = np.argsort(-ics[:n_self])
    sel_self = self_order[:min(top_self, len(self_order))]

    # MF features: ensure category diversity
    mf_ics = ics[n_self:]
    mf_order = np.argsort(-mf_ics)
    sel_mf: list[int] = []
    cat_count: dict[str, int] = {}
    for idx in mf_order:
        if idx >= len(mf_col_names):
            continue
        cat = mf_feat_cat.get(mf_col_names[idx], "other")
        if cat not in cat_count:
            cat_count[cat] = 0
        if cat_count[cat] < 4:
            sel_mf.append(idx)
            cat_count[cat] += 1
        if len(cat_count) >= min_cats and len(sel_mf) >= top_mf:
            break
    for idx in mf_order:
        if len(sel_mf) >= top_mf:
            break
        if idx not in sel_mf and idx < len(mf_col_names):
            sel_mf.append(idx)

    sel_mf_global = np.array(sel_mf, dtype=int) + n_self
    selected = np.sort(np.concatenate([sel_self, sel_mf_global]))
    return selected, len(cat_count)


# ============================================================================
# SIGNAL SYSTEM  -- 451 signals from 5 yield curves
# ============================================================================
def build_all_signals(df, col_map: dict | None = None,
                      tenor: str = "10Y") -> pd.DataFrame:
    """Build exactly 451 directional signals from 5 yield curves."""
    if col_map is None:
        col_map = COL_MAP
    signals = {}
    rets = {tnr: df[col_map[tnr]].pct_change() for tnr in col_map}

    for tnr in ["1Y", "3Y", "5Y", "7Y", "10Y"]:
        ret = rets[tnr]
        close = df[col_map[tnr]]

        # Anti-momentum  (8 x 5 = 40)
        for w in [20, 40, 60, 90, 120, 180, 252, 504]:
            signals[f"{tnr}_anti_sum{w}"] = -safe_sign(ret.rolling(w).sum())

        # Anti-lag  (4 x 5 = 20)
        for lag in [20, 60, 120, 252]:
            signals[f"{tnr}_anti_lag{lag}"] = -safe_sign(ret.shift(lag - 1))

        # Z-anti  (8 x 5 = 40)
        for w in [20, 40, 60, 90, 120, 180, 252, 504]:
            signals[f"{tnr}_z_anti{w}"] = -safe_sign(close - close.rolling(w).mean())

        # RSI reversal  (1 x 5 = 5)
        g = ret.clip(lower=0).rolling(14, min_periods=7).mean()
        l_ = (-ret.clip(upper=0)).rolling(14, min_periods=7).mean()
        rsi = 100 - 100 / (1 + g.div(l_.replace(0, np.nan)))
        s = np.where(rsi > 65, -1, np.where(rsi < 35, 1, 0)).astype(int)
        signals[f"{tnr}_rsi_rev"] = (
            pd.Series(s, index=df.index).replace(0, np.nan)
            .ffill().fillna(-1).astype(int))

        # Bounce  (4 x 5 = 20)
        for d in [1, 2, 3, 5]:
            signals[f"{tnr}_bounce{d}d"] = -safe_sign(ret.rolling(d).sum())

        # Bollinger reversal  (3 x 5 = 15)
        for w in [20, 60, 120]:
            ma = close.rolling(w).mean()
            sd = close.rolling(w).std()
            bz = (close - ma) / sd.replace(0, np.nan)
            s = np.where(bz > 2, -1, np.where(bz < -2, 1, 0)).astype(int)
            signals[f"{tnr}_boll_rev{w}"] = (
                pd.Series(s, index=df.index).replace(0, np.nan)
                .ffill().fillna(-1).astype(int))

        # Amplitude reversal  (3 x 5 = 15)
        for w in [5, 10, 20]:
            mv = ret.rolling(w).sum()
            vo = ret.rolling(60).std() * np.sqrt(w)
            mz = mv / vo.replace(0, np.nan)
            s = np.where(mz > 1.5, -1, np.where(mz < -1.5, 1, 0)).astype(int)
            signals[f"{tnr}_amp_rev{w}"] = (
                pd.Series(s, index=df.index).replace(0, np.nan)
                .ffill().fillna(-1).astype(int))

        # Percentile reversal  (4 x 5 = 20)
        for w in [20, 60, 120, 252]:
            rmin = close.rolling(w).min()
            rmax = close.rolling(w).max()
            pctl = (close - rmin) / (rmax - rmin).replace(0, np.nan)
            s = np.where(pctl > 0.8, -1, np.where(pctl < 0.2, 1, 0)).astype(int)
            signals[f"{tnr}_pctl_rev{w}"] = (
                pd.Series(s, index=df.index).replace(0, np.nan)
                .ffill().fillna(-1).astype(int))

        # EMA reversal  (3 x 5 = 15)
        for f_, sl in [(5, 20), (10, 40), (20, 60)]:
            ef = close.ewm(span=f_, adjust=False).mean()
            es = close.ewm(span=sl, adjust=False).mean()
            signals[f"{tnr}_ema_rev{f_}_{sl}"] = -safe_sign(
                (ef - es) / close.rolling(sl).std().replace(0, np.nan))

        # Conditional reversal  (2 x 5 = 10)
        for w in [20, 60]:
            v1 = ret.rolling(w).var()
            vl = ret.rolling(w * 2).var()
            vr = vl / (2 * v1).replace(0, np.nan)
            ms = ret.rolling(w).sum()
            signals[f"{tnr}_cond_rev{w}"] = np.where(
                vr < 0.85, -safe_sign(ms), safe_sign(ms)).astype(int)

    # Spread anti  (6 x 9 = 54)  +  Spread z-reversal  (3 x 9 = 27)
    spread_pairs = [
        ("3Y", "1Y"), ("5Y", "1Y"), ("5Y", "3Y"), ("7Y", "3Y"),
        ("7Y", "5Y"), ("10Y", "5Y"), ("10Y", "7Y"), ("10Y", "1Y"),
        ("10Y", "3Y"),
    ]
    for left, right in spread_pairs:
        spr = df[col_map[left]] - df[col_map[right]]
        sc = spr.diff()
        for w in [10, 20, 40, 60, 120, 252]:
            signals[f"spr_{left}{right}_anti{w}"] = -safe_sign(sc.rolling(w).sum())
        for w in [20, 60, 120]:
            sm = spr.rolling(w).mean()
            ss = spr.rolling(w).std()
            sz = (spr - sm) / ss.replace(0, np.nan)
            s = np.where(sz > 1.5, -1, np.where(sz < -1.5, 1, 0)).astype(int)
            signals[f"spr_zrev_{left}{right}_{w}"] = (
                pd.Series(s, index=df.index).replace(0, np.nan)
                .ffill().fillna(-1).astype(int))

    # Relative value reversal  (3 x 3 = 9)
    for tnr, left, right in [("5Y", "3Y", "7Y"), ("7Y", "5Y", "10Y"),
                              ("3Y", "1Y", "5Y")]:
        fair = (df[col_map[left]] + df[col_map[right]]) / 2
        rc = df[col_map[tnr]] - fair
        for w in [20, 60, 120]:
            rm = rc.rolling(w).mean()
            rs = rc.rolling(w).std()
            signals[f"{tnr}_rv_rev{w}"] = -safe_sign(
                (rc - rm) / rs.replace(0, np.nan))

    # Butterfly  (6 + 5 = 11)
    c5 = df[col_map["5Y"]]
    c7 = df[col_map["7Y"]]
    c10 = df[col_map["10Y"]]
    bf = 2 * c7 - c5 - c10
    bc = bf.diff()
    for w in [10, 20, 40, 60, 120, 252]:
        signals[f"bf_anti{w}"] = -safe_sign(bc.rolling(w).sum())
    for w in [20, 40, 60, 120, 252]:
        signals[f"bf_z_anti{w}"] = -safe_sign(bf - bf.rolling(w).mean())

    # Term premium  (5 x 7 x 2 = 70)
    for ln, rn in [("10Y", "1Y"), ("10Y", "3Y"), ("5Y", "1Y"), ("7Y", "1Y"),
                   ("3Y", "1Y"), ("7Y", "3Y"), ("5Y", "3Y")]:
        tp = df[col_map[ln]] - df[col_map[rn]]
        tc = tp.diff()
        for w in [20, 40, 60, 120, 252]:
            signals[f"tp_{ln}{rn}_anti{w}"] = -safe_sign(tc.rolling(w).sum())
            signals[f"tp_{ln}{rn}_z_anti{w}"] = -safe_sign(
                tp - tp.rolling(w).mean())

    # Policy anti  (6 x 2 = 12)
    r1y, r3y = rets["1Y"], rets["3Y"]
    for w in [5, 10, 20, 40, 60, 120]:
        signals[f"pol_1Y_anti{w}"] = -safe_sign(r1y.rolling(w).sum())
        signals[f"pol_3Y_anti{w}"] = -safe_sign(r3y.rolling(w).sum())

    # Lead-lag  (4 x 10 = 40)
    for leader, follower in [
        ("1Y", "3Y"), ("1Y", "5Y"), ("1Y", "7Y"), ("1Y", "10Y"),
        ("3Y", "5Y"), ("3Y", "7Y"), ("3Y", "10Y"),
        ("5Y", "7Y"), ("5Y", "10Y"), ("7Y", "10Y"),
    ]:
        for lag in [1, 2, 3, 5]:
            signals[f"{leader}_lead{follower}_lag{lag}"] = safe_sign(
                rets[leader].shift(lag))

    # Concordance anti  (4)
    for tnr in ["3Y", "5Y", "7Y", "10Y"]:
        ret = rets[tnr]
        s5 = np.sign(ret.rolling(5).sum())
        s20 = np.sign(ret.rolling(20).sum())
        s60 = np.sign(ret.rolling(60).sum())
        signals[f"{tnr}_concord_anti"] = -safe_sign(s5 + s20 + s60)

    # Excess return anti  (5 x 4 = 20)
    for tn, er in [
        ("3Y", rets["3Y"] - 0.5 * (rets["1Y"] + rets["5Y"])),
        ("5Y", rets["5Y"] - 0.5 * (rets["3Y"] + rets["7Y"])),
        ("7Y", rets["7Y"] - 0.5 * (rets["5Y"] + rets["10Y"])),
        ("10Y", rets["10Y"] - rets["7Y"]),
    ]:
        for w in [5, 10, 20, 40, 60]:
            signals[f"excess_{tn}_anti{w}"] = -safe_sign(er.rolling(w).sum())

    # Vol regime  (4)
    for tnr in ["3Y", "5Y", "7Y", "10Y"]:
        ret = rets[tnr]
        signals[f"{tnr}_vol_regime"] = -safe_sign(
            ret.rolling(20).std() - ret.rolling(60).std())

    # ── V25: Streak signals — consecutive same-direction → contrarian ──
    for tnr in ["1Y", "3Y", "5Y", "7Y", "10Y"]:
        ret_sign = np.sign(rets[tnr]).fillna(0)
        # Type 1: strict consecutive streak
        for streak_len in [3, 5, 7, 10, 15]:
            streak_sum = ret_sign.rolling(streak_len).sum()
            s = np.where(streak_sum >= streak_len, -1,
                    np.where(streak_sum <= -streak_len, 1, 0)).astype(int)
            signals[f"{tnr}_streak_anti{streak_len}"] = (
                pd.Series(s, index=df.index)
                .replace(0, np.nan).ffill().fillna(0).astype(int))
        # Type 2: rolling up-fraction extreme → contrarian
        up_flag = (rets[tnr] > 0).astype(float)
        for w in [10, 20, 40]:
            frac = up_flag.rolling(w).mean()
            s = np.where(frac >= 0.75, -1,
                    np.where(frac <= 0.25, 1, 0)).astype(int)
            signals[f"{tnr}_upfrac_anti{w}"] = (
                pd.Series(s, index=df.index)
                .replace(0, np.nan).ffill().fillna(0).astype(int))

    # ── V25: Momentum / trend-following signals (follow the trend) ──
    for tnr in ["1Y", "3Y", "5Y", "7Y", "10Y"]:
        ret = rets[tnr]
        close = df[col_map[tnr]]
        # Type 1: Pure momentum — follow N-day direction
        for w in [10, 20, 40, 60, 120]:
            signals[f"{tnr}_follow{w}"] = safe_sign(ret.rolling(w).sum())
        # Type 2: Trend — above/below moving average
        for w in [60, 120, 252]:
            signals[f"{tnr}_above_ma{w}"] = safe_sign(
                close - close.rolling(w).mean())
        # Type 3: EMA crossover — fast > slow = follow trend
        for f_, sl in [(5, 20), (10, 40), (20, 60)]:
            ef = close.ewm(span=f_, adjust=False).mean()
            es = close.ewm(span=sl, adjust=False).mean()
            signals[f"{tnr}_ema_follow{f_}_{sl}"] = safe_sign(ef - es)
        # Type 4: Breakout — close near N-day extremes = continuation
        for w in [20, 60, 120]:
            rmin = close.rolling(w).min()
            rmax = close.rolling(w).max()
            pctl = (close - rmin) / (rmax - rmin).replace(0, np.nan)
            s = np.where(pctl > 0.8, 1,
                    np.where(pctl < 0.2, -1, 0)).astype(int)
            signals[f"{tnr}_breakout{w}"] = (
                pd.Series(s, index=df.index)
                .replace(0, np.nan).ffill().fillna(0).astype(int))

    # ── V25: Cross-asset leading signals ──
    # Equity -> bond: stocks up -> bonds sell (yield up)
    for eq_col in ["SH000300", "IFCFE00C"]:
        if eq_col in df.columns:
            eq_v = pd.to_numeric(df[eq_col], errors="coerce").ffill()
            eq_ret = eq_v.pct_change()
            for w in [5, 10, 20, 60]:
                signals[f"eq_{eq_col[:6]}_lead{w}"] = safe_sign(
                    eq_ret.rolling(w).sum())
    # Money market -> bond: DR007 rising = tight liquidity = bonds sell
    if "DR007IBC" in df.columns:
        dr = pd.to_numeric(df["DR007IBC"], errors="coerce").ffill()
        dr_chg = dr.diff()
        for w in [5, 10, 20, 60]:
            signals[f"dr007_dir{w}"] = safe_sign(dr_chg.rolling(w).sum())
    # FX -> bond: USDCNH up (CNH weak) = capital outflow = bonds sell
    if "USDCNH0C" in df.columns:
        fx = pd.to_numeric(df["USDCNH0C"], errors="coerce").ffill()
        fx_ret = fx.pct_change()
        for w in [5, 10, 20, 60]:
            signals[f"fx_usdcnh_lead{w}"] = safe_sign(
                fx_ret.rolling(w).sum())
    # Commodity -> bond: commodity up = inflation = bonds sell
    for cmd_col in ["S0031525", "AUSHF00C", "CUSHF01C"]:
        if cmd_col in df.columns:
            cmd_v = pd.to_numeric(df[cmd_col], errors="coerce").ffill()
            cmd_ret = cmd_v.pct_change()
            for w in [10, 20, 60]:
                signals[f"cmd_{cmd_col[:6]}_lead{w}"] = safe_sign(
                    cmd_ret.rolling(w).sum())

    return pd.DataFrame(signals, index=df.index).fillna(0).astype(int)


def classify_signals(signal_names: list, tenor: str = "10Y") -> dict:
    """Classify signal names into categories.  Returns {cat: np.array(indices)}.

    V25: added momentum, cross_asset, streak categories.
    """
    categories: dict[str, list] = {}
    for i, name in enumerate(signal_names):
        # V25: momentum/trend-following signals
        if ("_follow" in name or "_above_ma" in name
                or "_breakout" in name or "_ema_follow" in name):
            cat = "momentum"
        # V25: cross-asset leading signals
        elif (name.startswith("eq_") or name.startswith("dr007_")
              or name.startswith("fx_") or name.startswith("cmd_")):
            cat = "cross_asset"
        elif name.startswith("spr_zrev_") or "_rv_rev" in name:
            cat = "rel_value"
        elif any(x in name for x in [
            "_pctl_rev", "_amp_rev", "_ema_rev",
            "_boll_rev", "_rsi_rev", "_cond_rev",
        ]):
            cat = "mean_rev" if name.startswith(f"{tenor}_") else "cross_rev"
        elif "_bounce" in name:
            cat = "mean_rev" if name.startswith(f"{tenor}_") else "cross_rev"
        elif name.startswith(f"{tenor}_concord"):
            cat = "concordance"
        elif "_lead" in name:
            cat = "lead_lag"
        elif "pol_" in name:
            cat = "policy"
        elif name.startswith("bf_"):
            cat = "butterfly"
        elif name.startswith("excess_"):
            cat = "excess"
        elif "_vol_regime" in name:
            cat = "vol_regime"
        # V25: streak signals
        elif "_streak_anti" in name or "_upfrac_anti" in name:
            cat = "streak"
        elif name.startswith("tp_"):
            cat = "term_prem"
        elif name.startswith("spr_"):
            cat = "spread"
        elif "_z_anti" in name:
            cat = "self_z" if name.startswith(f"{tenor}_") else "cross_z"
        elif "_anti_sum" in name or "_anti_lag" in name:
            cat = "self_anti" if name.startswith(f"{tenor}_") else "cross_anti"
        else:
            cat = "other"
        categories.setdefault(cat, []).append(i)
    return {k: np.array(v, dtype=int) for k, v in categories.items()}


def select_combo_signals(sig_accs: np.ndarray, cat_indices: dict,
                         template: dict, min_acc: float) -> np.ndarray:
    selected = []
    for cat, count in template.items():
        if cat in cat_indices:
            ci = cat_indices[cat]
            ca = sig_accs[ci]
            elig = ci[ca >= min_acc]
            if len(elig) > 0:
                selected.extend(
                    elig[np.argsort(-sig_accs[elig])[:count]].tolist())
    return (np.unique(np.array(selected, dtype=int))
            if selected else np.array([], dtype=int))


def weighted_signal_sum(sig_row, sel_indices, sig_accs_row):
    """V25: Weight each signal by (accuracy - 0.5), so better signals count more.

    Returns weighted_avg * len(sel_indices) for backward compatibility.
    Caller normalizes by dividing by N to get [-1,+1].
    """
    if len(sel_indices) == 0:
        return 0.0
    sigs = sig_row[sel_indices].astype(np.float64)
    accs = sig_accs_row[sel_indices]
    weights = np.clip(accs - 0.5, 0.0, None)
    if weights.sum() < 1e-8:
        return float(sigs.sum())
    return float((sigs * weights).sum() / weights.sum()) * len(sel_indices)


def _diverse_topk(scores, config_windows, K):
    """V25: Select top-K configs ensuring at least one from each window size."""
    unique_windows = np.unique(config_windows)
    selected = []
    # First: best from each window size
    for w in unique_windows:
        mask = config_windows == w
        w_indices = np.flatnonzero(mask)
        if len(w_indices) > 0:
            best = w_indices[np.argmax(scores[w_indices])]
            selected.append(best)
    # Then: fill remaining slots with best overall
    remaining = K - len(selected)
    if remaining > 0:
        all_sorted = np.argsort(-scores)
        for idx in all_sorted:
            if idx not in selected:
                selected.append(idx)
                if len(selected) >= K:
                    break
    return np.array(selected[:K], dtype=int)


# ============================================================================
# LIGHTGBM -- grid builder + threshold calibration
# ============================================================================
def build_lgbm_grid(windows, leaves, min_child, alphas, lambdas,
                    splits, slow_path: bool = False) -> list:
    grid = list(itertools.product(
        windows, leaves, min_child, alphas, lambdas, splits))
    result = [dict(zip(["window", "num_leaves", "min_child_samples",
                        "reg_alpha", "reg_lambda", "split_pct"], g))
              for g in grid]
    if slow_path:
        # V27: Slow-learning path — lr=0.01, n_estimators=350
        slow = list(itertools.product(
            [w for w in windows if w >= 350],
            [l for l in leaves if l >= 5],
            [20], [0.0], [1.0], [0.65]))
        for g in slow:
            d = dict(zip(["window", "num_leaves", "min_child_samples",
                          "reg_alpha", "reg_lambda", "split_pct"], g))
            d["learning_rate"] = 0.01
            d["n_estimators"] = 350
            result.append(d)
    return result


# -- Numba-accelerated threshold search ------------------------------------
if _HAS_NUMBA:
    @_njit(cache=False)
    def _choose_threshold_nb(cal_prob, cal_labels_bin, n_th=61):
        """Find best threshold using composite score.  Numba hot path."""
        best_score = -1e30
        best_th = 0.5
        n = len(cal_prob)
        if n == 0:
            return 0.5
        for ti in range(n_th):
            th = 0.35 + ti * (0.30 / (n_th - 1))
            n_up = 0; n_dn = 0
            correct = 0; up_correct = 0; dn_correct = 0
            for k in range(n):
                if cal_prob[k] >= th:
                    pred = 1; n_up += 1
                    if cal_labels_bin[k] == 1:
                        up_correct += 1; correct += 1
                else:
                    pred = 0; n_dn += 1
                    if cal_labels_bin[k] == 0:
                        dn_correct += 1; correct += 1
            n_total = n_up + n_dn
            # V22: Hard balance floor — skip if <12% minority direction
            if n_total > 20 and min(n_up, n_dn) < 0.12 * n_total:
                continue
            acc = correct / n
            up_p = up_correct / max(n_up, 1)
            dn_p = dn_correct / max(n_dn, 1)
            frac_min = min(n_up, n_dn) / max(n, 1)
            score = acc + 0.15 * min(up_p, dn_p) + 0.20 * frac_min
            if score > best_score:
                best_score = score
                best_th = th
        return best_th

    def choose_threshold(cal_prob, cal_labels):
        """Choose threshold -- numba accelerated."""
        cal_bin = (cal_labels == 1).astype(np.int32)
        return float(_choose_threshold_nb(
            cal_prob.astype(np.float64), cal_bin))
else:
    def choose_threshold(cal_prob, cal_labels):
        """Choose threshold -- pure numpy fallback."""
        best_score, best_th = -np.inf, 0.5
        thresholds = np.linspace(0.35, 0.65, 61)
        cal_bin = (cal_labels == 1).astype(np.int32)
        n = len(cal_prob)
        if n == 0:
            return 0.5
        for th in thresholds:
            pred = (cal_prob >= th).astype(np.int32)
            correct = int((pred == cal_bin).sum())
            acc = correct / n
            n_up = int(pred.sum())
            n_dn = n - n_up
            n_total = n_up + n_dn
            # V22: Hard balance floor — skip if <12% minority direction
            if n_total > 20 and min(n_up, n_dn) < 0.12 * n_total:
                continue
            up_correct = int(((pred == 1) & (cal_bin == 1)).sum())
            dn_correct = int(((pred == 0) & (cal_bin == 0)).sum())
            up_p = up_correct / max(n_up, 1)
            dn_p = dn_correct / max(n_dn, 1)
            frac_min = min(n_up, n_dn) / max(n, 1)
            # V22: Stronger balance penalty
            score = acc + 0.15 * min(float(up_p), float(dn_p)) + 0.20 * frac_min
            if score > best_score:
                best_score, best_th = score, float(th)
        return best_th


# -- Read-only context shared by the bounded worker threads -----------------
_G: dict = {}
_LGB_DATASET_CACHE = threading.local()


def _init_worker(df_len, feat, labels, close, fallback, test_idx,
                 horizon, purge_gap, seeds=None):
    _G.update(df_len=df_len, feat=feat, labels=labels, close=close,
              fallback=fallback, test_idx=test_idx,
              horizon=horizon, purge_gap=purge_gap,
              seeds=seeds or [42, 314])


def run_config(config: dict) -> dict:
    """Train one LGBM config across all test days.  Runs inside worker pool.

    V25: Multi-seed LGBM — trains with multiple seeds, averages probabilities.
    Returns both preds and probs for ensemble use.
    """
    try:
        import lightgbm as lgb
        labels = _G["labels"]; test_idx = _G["test_idx"]
        feat = _G["feat"]; close = _G["close"]; df_len = _G["df_len"]
        horizon = _G["horizon"]; purge_gap = _G["purge_gap"]
        seeds = _G.get("seeds", [42, 314])
        n_test = len(test_idx)
        preds = np.zeros(n_test, dtype=np.int32)
        probs = np.full(n_test, 0.5, dtype=np.float64)

        # Pre-compute eligible-index arrays
        labels_valid = np.isin(labels, [-1.0, 1.0])
        close_valid = ~np.isnan(close)
        all_idx = np.arange(df_len)

        for i, idx in enumerate(test_idx):
            w = config["window"]
            mask = (all_idx < idx - horizon) & labels_valid & close_valid
            elig = np.flatnonzero(mask)
            if len(elig) > w:
                elig = elig[-w:]
            if len(elig) < 120 or len(np.unique(labels[elig])) < 2:
                preds[i] = int(_G["fallback"].iloc[idx])
                probs[i] = 0.5
                continue
            split = max(80, int(len(elig) * config["split_pct"]))
            purge_end = min(split + purge_gap, len(elig) - 20)
            fit_idx = elig[:split]
            cal_idx = elig[purge_end:]
            if len(cal_idx) < 20:
                preds[i] = int(_G["fallback"].iloc[idx])
                probs[i] = 0.5
                continue

            # V25: Multi-seed training — average probabilities
            fit_y = (labels[fit_idx] == 1).astype(int)
            n_pos = max(int(fit_y.sum()), 1)
            n_neg = max(len(fit_y) - n_pos, 1)
            w_pos = len(fit_y) / (2.0 * n_pos)
            w_neg = len(fit_y) / (2.0 * n_neg)
            class_w = np.where(fit_y == 1, w_pos, w_neg)
            time_w = np.exp(np.linspace(-1.0, 0.0, len(fit_idx)))
            sample_w = class_w * time_w
            cal_y = (labels[cal_idx] == 1).astype(int)

            prob_seeds = []
            cal_probs_first = None
            dataset_cache = getattr(_LGB_DATASET_CACHE, "items", None)
            if dataset_cache is None:
                dataset_cache = {}
                _LGB_DATASET_CACHE.items = dataset_cache
            dataset_key = (
                int(idx),
                int(w),
                float(config["split_pct"]),
                int(config["min_child_samples"]),
            )
            datasets = dataset_cache.get(dataset_key)
            if datasets is None:
                train_set = lgb.Dataset(
                    feat[fit_idx], label=fit_y, weight=sample_w)
                valid_set = lgb.Dataset(
                    feat[cal_idx], label=cal_y, reference=train_set)
                datasets = (train_set, valid_set)
                dataset_cache[dataset_key] = datasets
            train_set, valid_set = datasets
            for seed in seeds:
                n_est = config.get("n_estimators", 140)
                params = _lgbm_train_params(config, seed)
                model = lgb.train(
                    params,
                    train_set,
                    num_boost_round=n_est,
                    valid_sets=[valid_set],
                    callbacks=[lgb.early_stopping(20, verbose=False)],
                )
                p = float(model.predict(
                    feat[[idx]], num_iteration=model.best_iteration)[0])
                prob_seeds.append(p)
                if cal_probs_first is None:
                    cal_probs_first = model.predict(
                        feat[cal_idx], num_iteration=model.best_iteration)

            prob = float(np.mean(prob_seeds))
            probs[i] = prob
            threshold = choose_threshold(cal_probs_first, labels[cal_idx])
            preds[i] = 1 if prob >= threshold else -1
        return {"config": config, "preds": preds, "probs": probs}
    except Exception as exc:
        raise RuntimeError(f"V28 LightGBM config failed: {config!r}") from exc


def _lgbm_train_params(config: dict, seed: int) -> dict[str, Any]:
    """返回与 ``LGBMClassifier`` 原路径完全一致的训练参数。"""
    return {
        "boosting_type": "gbdt",
        "colsample_bytree": 0.90,
        "learning_rate": config.get("learning_rate", 0.03),
        "max_depth": -1,
        "min_child_samples": config["min_child_samples"],
        "min_child_weight": 0.001,
        "min_split_gain": 0.0,
        "num_leaves": config["num_leaves"],
        "random_state": seed,
        "reg_alpha": config["reg_alpha"],
        "reg_lambda": config["reg_lambda"],
        "subsample": 0.85,
        "subsample_for_bin": 200000,
        "subsample_freq": 0,
        "metric": ["binary_logloss"],
        "verbosity": -1,
        "force_col_wise": True,
        "objective": "binary",
        "num_threads": 1,
    }


# ============================================================================
# EVALUATION
# ============================================================================
def pacc(p, l, m) -> Tuple[float, int]:
    """V28: accuracy on trade samples only (pred != 0)."""
    if m.sum() == 0:
        return float("nan"), 0
    pm, lm = p[m], l[m]
    trade = pm != 0
    n_trade = int(trade.sum())
    if n_trade == 0:
        return float("nan"), int(m.sum())
    return float((pm[trade] == lm[trade]).mean()), int(m.sum())


def trade_rate_fn(p, m) -> float:
    """V28: fraction of samples with directional prediction (pred != 0)."""
    if m.sum() == 0:
        return 0.0
    return float((p[m] != 0).sum()) / float(m.sum())


def check_balance(preds, dates, max_bad: int = 1) -> Tuple[bool, list]:
    """V28: bad month = trade samples all one direction (flat excluded)."""
    months = dates.to_period("M")
    bad = []
    for m in sorted(months.unique()):
        mm = months == m
        mp = preds[mm]
        trade_mp = mp[mp != 0]
        if len(trade_mp) < 3:
            continue
        nu = int((trade_mp == 1).sum())
        nd = int((trade_mp == -1).sum())
        if nu < 1 or nd < 1:
            bad.append(f"{m}({nu}u/{nd}d)")
    return len(bad) <= max_bad, bad


def apply_balance_fix(final: np.ndarray, vote_sums: np.ndarray,
                      test_months, n_fix: int) -> np.ndarray:
    """Flip weakest predictions to avoid all-up or all-down months.

    Uses numpy argsort instead of Python inner loops.
    """
    for month in sorted(test_months.unique()):
        pm = test_months == month
        pi = np.where(pm)[0]
        if len(pi) < 3:
            continue
        nu = int((final[pi] == 1).sum())
        nd = int((final[pi] == -1).sum())
        if nu >= 1 and nd >= 1:
            continue
        if nd < 1:
            # Need to flip some up -> down
            order = np.argsort(vote_sums[pi])
            fl = 0
            for ix in order:
                if final[pi[ix]] == 1 and fl < n_fix:
                    final[pi[ix]] = -1
                    fl += 1
        elif nu < 1:
            # Need to flip some down -> up
            order = np.argsort(-vote_sums[pi])
            fl = 0
            for ix in order:
                if final[pi[ix]] == -1 and fl < n_fix:
                    final[pi[ix]] = 1
                    fl += 1
    return final


# ============================================================================
# MAIN RUNNER
# ============================================================================
def prepare_model_frames(
    daily_df: pd.DataFrame | None,
    weekly_df: pd.DataFrame | None,
    monthly_df: pd.DataFrame | None,
    daily_dates: pd.Series,
    date_to_week: Mapping[str, int | str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把平台产出的 weekly/monthly artifact 对齐成算法特征矩阵。"""
    if weekly_df is None:
        aligned_weekly = pd.DataFrame(index=range(len(daily_dates)))
    elif date_to_week is None:
        aligned_weekly = _align_weekly_like_legacy(weekly_df, daily_dates)
    else:
        aligned_weekly = align_weekly_previous_complete(weekly_df, daily_dates, date_to_week)

    if monthly_df is None:
        aligned_monthly = pd.DataFrame(index=range(len(daily_dates)))
    else:
        aligned_monthly = align_monthly_previous_month(monthly_df, daily_dates)
    aligned_weekly = aligned_weekly[[col for col in WEEKLY_COLS if col in aligned_weekly.columns]]
    aligned_monthly = aligned_monthly[[col for col in MONTHLY_COLS if col in aligned_monthly.columns]]
    return aligned_weekly, aligned_monthly


def _normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lstrip("\ufeff") for c in out.columns]
    if "date" not in out.columns:
        raise ValueError("daily dataframe missing date column")
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in out.columns:
        if col != "date":
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _normalize_aux_frame(df: pd.DataFrame | None, id_col: str) -> pd.DataFrame | None:
    if df is None:
        return None
    out = df.copy()
    out.columns = [str(c).strip().lstrip("\ufeff") for c in out.columns]
    if id_col not in out.columns:
        raise ValueError(f"auxiliary dataframe missing {id_col} column")
    if id_col == "month_id":
        out[id_col] = out[id_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    else:
        out[id_col] = pd.to_numeric(out[id_col], errors="coerce")
    for col in out.columns:
        if col != id_col:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _align_weekly_like_legacy(weekly_df: pd.DataFrame, daily_dates: pd.Series) -> pd.DataFrame:
    weekly = _normalize_aux_frame(weekly_df, "week_id")
    if weekly is None:
        return pd.DataFrame(index=range(len(daily_dates)))
    existing = [col for col in WEEKLY_COLS if col in weekly.columns]
    if not existing:
        return pd.DataFrame(index=range(len(daily_dates)))
    dates = sorted(pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize().tolist())
    if not dates:
        return pd.DataFrame(index=range(len(daily_dates)))
    week_arr = np.zeros(len(dates), dtype=int)
    current_year, seq, previous_dow = dates[0].year, 1, -1
    for i, daily_date in enumerate(dates):
        if daily_date.year != current_year:
            current_year, seq, previous_dow = daily_date.year, 1, -1
        dow = daily_date.weekday()
        if previous_dow >= 0 and (
            dow <= previous_dow and (daily_date - dates[i - 1]).days > 1
            or (daily_date - dates[i - 1]).days > 5
        ):
            seq += 1
        previous_dow = dow
        week_arr[i] = current_year * 100 + seq
    date_to_pos = {daily_date: i for i, daily_date in enumerate(dates)}
    date_to_week = {daily_date: int(week_arr[i]) for i, daily_date in enumerate(dates)}
    available_weeks = sorted(set(week_arr))
    week_to_previous = {
        week_id: available_weeks[i - 1] if i > 0 else None
        for i, week_id in enumerate(available_weeks)
    }
    weekly_indexed = weekly.set_index("week_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_dates), np.nan, dtype=np.float64) for col in existing
    }
    for daily_date in pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize().tolist():
        if daily_date not in date_to_week:
            continue
        previous_week = week_to_previous.get(date_to_week[daily_date])
        if previous_week is None or previous_week not in weekly_indexed.index:
            continue
        row_index = date_to_pos[daily_date]
        for col in existing:
            result[col][row_index] = weekly_indexed.at[previous_week, col]
    return pd.DataFrame(result, index=range(len(daily_dates)))


def _labels_for_detail(values: np.ndarray) -> list[int | None]:
    result: list[int | None] = []
    for value in values:
        result.append(None if pd.isna(value) else int(value))
    return result


def _prediction_detail_frame(
    df: pd.DataFrame,
    test_idx: np.ndarray,
    final: np.ndarray,
    true_labels: np.ndarray,
    ens_prob: np.ndarray,
    ml_base: np.ndarray,
    sig_avg: np.ndarray,
    has_sig: np.ndarray,
    vote_score: np.ndarray,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "anchor_date": pd.to_datetime(df["date"].values[test_idx]).strftime("%Y-%m-%d"),
            "prediction": final.astype(int),
            "true_label": _labels_for_detail(true_labels),
            "confidence": np.abs(final).astype(float),
            "vote_score": vote_score.astype(float),
            "ens_prob": ens_prob.astype(float),
            "ml_base": ml_base.astype(float),
            "sig_avg": sig_avg.astype(float),
            "has_signal": has_sig.astype(bool),
        }
    )


def prepare_prediction_inputs(cfg: dict) -> dict[str, Any]:
    """构建与 Request 截止无关、可在同一批次内安全复用的只读矩阵。"""
    if "daily_df" not in cfg:
        raise ValueError(
            "daily_df is required; build inputs through shared.input_artifacts "
            "before calling core"
        )
    df = _normalize_daily_frame(cfg["daily_df"])
    weekly_raw = _normalize_aux_frame(cfg.get("weekly_df"), "week_id")
    monthly_raw = _normalize_aux_frame(cfg.get("monthly_df"), "month_id")
    data_cutoff = cfg.get("data_cutoff")
    if data_cutoff:
        df = df[df["date"] < pd.Timestamp(data_cutoff)].reset_index(drop=True)
    wk_df, mo_df = prepare_model_frames(
        df,
        weekly_raw,
        monthly_raw,
        df["date"],
        cfg.get("date_to_week"),
    )

    close_col = cfg["close"]
    labels = make_labels(df, close_col, horizon=cfg.get("horizon", HORIZON))
    labels_h1 = make_labels(df, close_col, horizon=1)
    close = df[close_col].values.astype(np.float64)
    fallback = build_fallback_signal(df, close_col)

    bond_f = build_bond_features(df, close_col, cfg["aux_pairs"])
    mf_f, mf_feat_cat = build_mf_features(df)
    wkmo_f = build_wkmo_features(wk_df, mo_df)
    for column in wkmo_f.columns:
        mf_feat_cat[column] = "weekly" if column.startswith("wk_") else "monthly"
    self_cols = list(bond_f.columns)
    mf_cols = list(mf_f.columns) + list(wkmo_f.columns)
    feat_all = pd.concat([bond_f, mf_f, wkmo_f], axis=1).values.astype(
        np.float64
    )

    sig_df = build_all_signals(df, COL_MAP, cfg["tenor"])
    sig_names = list(sig_df.columns)
    return {
        "df": df,
        "weekly_column_count": len(wk_df.columns),
        "monthly_column_count": len(mo_df.columns),
        "labels": labels,
        "labels_h1": labels_h1,
        "close": close,
        "fallback": fallback,
        "feat_all": feat_all,
        "n_self": len(self_cols),
        "mf_feat_cat": mf_feat_cat,
        "mf_cols": mf_cols,
        "sig_names": sig_names,
        "sig_matrix": sig_df.values.astype(np.int32),
        "cat_indices": classify_signals(sig_names, cfg["tenor"]),
    }


def run_prediction(cfg: dict) -> np.ndarray | pd.DataFrame:
    """Run a single fixed-config prediction.

    Parameters
    ----------
    cfg : dict with keys:
        tenor, close, aux_pairs, lgbm_windows, lgbm_leaves,
        lgbm_min_child, lgbm_alpha, lgbm_lambda, lgbm_split,
        K, combo_name, combo_template, lgbm_w, vt, rebal, ew,
        min_acc, ic_top_self, ic_top_mf, n_workers, data_dir,
        seeds, ml_mode, sig_mode, ens_mode  (V25 new)

    Returns
    -------
    np.ndarray of predictions (+1 / -1 / 0) for the test period (V28: 0=flat)
    """
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
    os.environ["NUMBA_NUM_THREADS"] = "1"

    import lightgbm as lgb

    tenor = cfg["tenor"]
    K = cfg.get("K", 7)
    combo_name = cfg.get("combo_name", "bf_core5")
    combo_template = cfg.get("combo_template", {"butterfly": 3, "term_prem": 2})
    lgbm_w = cfg.get("lgbm_w", 0.75)
    vt = cfg.get("vt", 0)
    rebal = cfg.get("rebal", "quarterly")
    ew = cfg.get("ew", 252)
    min_acc_val = cfg.get("min_acc", 0.45)
    ic_top_self = cfg.get("ic_top_self", 50)
    ic_top_mf = cfg.get("ic_top_mf", 30)
    n_workers = cfg.get("n_workers", 4)
    horizon = cfg.get("horizon", HORIZON)
    purge_gap = cfg.get("purge_gap", PURGE_GAP)
    # V25 new parameters
    seeds = cfg.get("seeds", [42, 314])
    ml_mode = cfg.get("ml_mode", "prob")     # "binary" or "prob"
    sig_mode = cfg.get("sig_mode", "equal")  # "equal" or "weighted"
    ens_mode = cfg.get("ens_mode", "standard")  # "standard"/"diverse"/"accwt"
    test_start = pd.Timestamp(cfg.get("test_start", "2024-07-01"))
    test_end = pd.Timestamp(cfg.get("test_end", "2026-04-30"))
    require_labels = bool(cfg.get("require_labels", True))
    emit_report = bool(cfg.get("emit_report", True))

    t0 = time.time()

    # -- Load or reuse read-only batch matrices --
    print(f"  Loading data...")
    prepared = cfg.get("prepared_inputs") or prepare_prediction_inputs(cfg)
    df = prepared["df"]
    labels = prepared["labels"]
    labels_h1 = prepared["labels_h1"]
    close = prepared["close"]
    fallback = prepared["fallback"]
    feat_all = prepared["feat_all"]
    n_self = prepared["n_self"]
    mf_feat_cat = prepared["mf_feat_cat"]
    mf_cols = prepared["mf_cols"]
    print(f"  Daily: {len(df)}, Weekly cols: {prepared['weekly_column_count']}, "
          f"Monthly cols: {prepared['monthly_column_count']}")

    # -- Features + IC screening --
    print(f"  Using prepared features...")
    print(f"  Self features: {n_self}, MF features: {len(mf_cols)}, "
          f"Total: {feat_all.shape[1]}")

    print(f"  IC screening on pre-test data (strict gap)...")
    _ts_row = int(np.flatnonzero(
        df["date"].values >= np.datetime64(test_start.strftime("%Y-%m-%d")))[0])
    pre_mask = ((np.arange(len(df)) < _ts_row - horizon)
                & np.isin(labels, [-1.0, 1.0])
                & ~np.isnan(close))
    pre_idx = np.flatnonzero(pre_mask)
    if len(pre_idx) > 1000:
        pre_idx = pre_idx[-1000:]
    selected_feats, n_cats = ic_screen(
        feat_all[pre_idx], labels[pre_idx], n_self, mf_feat_cat, mf_cols,
        top_self=ic_top_self, top_mf=ic_top_mf, min_cats=6)
    feat_selected = feat_all[:, selected_feats]
    n_mf_sel = int((selected_feats >= n_self).sum())
    print(f"  Selected: {len(selected_feats)} features "
          f"({len(selected_feats) - n_mf_sel} self + {n_mf_sel} MF "
          f"from {n_cats} categories)")

    # -- Test period --
    ts_s = test_start
    ts_e = test_end
    test_mask = (df["date"] >= ts_s).values & (df["date"] <= ts_e).values & ~np.isnan(close)
    if require_labels:
        test_mask = test_mask & ~np.isnan(labels)
    signal_selection_idx = np.flatnonzero(test_mask)
    requested_dates = cfg.get("requested_dates")
    if requested_dates is not None:
        requested_months = {str(item)[:7] for item in requested_dates}
        if len(requested_months) != 1:
            raise ValueError("requested_dates must belong to one calendar month")
        test_mask = test_mask & df["date"].dt.strftime("%Y-%m-%d").isin(
            requested_dates
        ).to_numpy()
    test_idx = np.flatnonzero(test_mask)
    if len(test_idx) == 0:
        raise ValueError(f"no test samples between {ts_s.date()} and {ts_e.date()}")
    test_dates = pd.to_datetime(df["date"].values[test_idx])
    true_labels = labels[test_idx]
    pre_m = np.asarray(
        (test_dates >= "2024-07-01") & (test_dates <= "2024-12-31"))
    sim_m = np.asarray(
        (test_dates >= "2025-01-01") & (test_dates <= "2025-06-30"))
    real_m = np.asarray(
        (test_dates >= "2025-07-01") & (test_dates <= "2026-04-30"))
    test_months = test_dates.to_period("M")

    print(f"\n{'=' * 80}")
    print(f"  {tenor} -- LGBM weighted ML signal + voting")
    print(f"  H={horizon}, Features: {len(selected_feats)} "
          f"({n_cats} MF categories)")
    print(f"  Test: {len(test_idx)}, Pre: {pre_m.sum()}, "
          f"Sim: {sim_m.sum()}, Real: {real_m.sum()}")
    print(f"{'=' * 80}")

    # -- Phase A: LGBM grid --
    lgbm_slow_path = cfg.get("lgbm_slow_path", False)
    lgbm_grid = build_lgbm_grid(
        cfg["lgbm_windows"], cfg["lgbm_leaves"],
        cfg["lgbm_min_child"], cfg["lgbm_alpha"],
        cfg["lgbm_lambda"], cfg["lgbm_split"],
        slow_path=lgbm_slow_path)

    _init_worker(len(df), feat_selected, labels, close, fallback,
                 test_idx, horizon, purge_gap, seeds)

    t1 = time.time()
    print(f"\n  Phase A: Training {len(lgbm_grid)} LGBM configs "
          f"with {n_workers} workers ({len(seeds)} seeds)...")
    worker_count = min(4, max(1, int(n_workers)))
    if worker_count == 1:
        results = list(map(run_config, lgbm_grid))
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(run_config, lgbm_grid))
    if len(results) != len(lgbm_grid):
        raise RuntimeError("V28 LightGBM grid result coverage mismatch")
    print(f"  Done: {len(results)} configs in "
          f"{(time.time() - t1) / 60:.1f} min")

    # -- Phase B: Signals --
    print(f"\n  Phase B: Computing signals...")
    sig_names = prepared["sig_names"]
    sig_matrix = prepared["sig_matrix"]
    cat_indices = prepared["cat_indices"]
    print(f"  Signals: {len(sig_names)}, "
          f"Categories: {list(cat_indices.keys())}")

    # Signal accuracy (H=1)
    selection_sa = np.full(
        (len(signal_selection_idx), len(sig_names)),
        np.nan,
        dtype=np.float32,
    )
    for i, idx in enumerate(signal_selection_idx):
        cands = np.flatnonzero(
            (np.arange(len(df)) < idx - 1)
            & np.isin(labels_h1, [-1.0, 1.0])
            & ~np.isnan(close))
        if len(cands) > ew:
            cands = cands[-ew:]
        if len(cands) < 40:
            continue
        # Vectorised accuracy: (sig == label) mean over candidates
        selection_sa[i] = (
            sig_matrix[cands] == labels_h1[cands][:, None]
        ).mean(
            axis=0).astype(np.float32)

    # -- Phase C: Build ensemble + apply signals (V25) --
    print(f"\n  Phase C: Assembling final predictions (V25)...")
    print(f"    ens_mode={ens_mode} ml_mode={ml_mode} "
          f"sig_mode={sig_mode} lgbm_w={lgbm_w} VT={vt}")

    # V25: Build ensemble with specified mode
    config_windows = np.array([r["config"]["window"] for r in results])
    ens = np.zeros(len(test_idx), dtype=np.int32)
    ens_prob = np.full(len(test_idx), 0.5, dtype=np.float64)

    for month in sorted(test_months.unique()):
        pm_mask = test_months == month
        pi = np.where(pm_mask)[0]
        if len(pi) == 0:
            continue
        usable_end = max(0, pi[0] - horizon)
        if usable_end < 5:
            prob_sum = np.zeros(len(pi), dtype=np.float64)
            for r in results:
                prob_sum += r["probs"][pi]
            ens_prob[pi] = prob_sum / len(results)
        else:
            scores = np.array([
                float((r["preds"][:usable_end]
                       == true_labels[:usable_end]).mean())
                for r in results])
            if ens_mode == "diverse":
                top_k = _diverse_topk(scores, config_windows, K)
            else:
                top_k = np.argsort(-scores)[:K]

            if ens_mode == "accwt":
                # Accuracy-weighted probability averaging
                ws = scores[top_k] - 0.5
                ws = np.clip(ws, 0.01, None)
                ws = ws / ws.sum()
                prob_sum = np.zeros(len(pi), dtype=np.float64)
                for j, ci in enumerate(top_k):
                    prob_sum += results[ci]["probs"][pi] * ws[j]
                ens_prob[pi] = prob_sum
            else:
                prob_sum = np.zeros(len(pi), dtype=np.float64)
                for ci in top_k:
                    prob_sum += results[ci]["probs"][pi]
                ens_prob[pi] = prob_sum / len(top_k)
        ens[pi] = np.where(ens_prob[pi] >= 0.5, 1, -1).astype(np.int32)

    # V25: ML base — binary or continuous
    if ml_mode == "prob":
        ml_base = (ens_prob - 0.5) * 2.0  # [-1, +1] continuous
    else:
        ml_base = ens.astype(np.float64)   # +1 or -1

    # Walk-forward signal selection
    if rebal == "monthly":
        periods = test_dates.to_period("M"); lb = 60
    elif rebal == "quarterly":
        periods = test_dates.to_period("Q"); lb = 120
    else:  # yearly
        periods = test_dates.to_period("Q"); lb = 250
    selection_dates = pd.to_datetime(df["date"].values[signal_selection_idx])
    if rebal == "monthly":
        selection_periods = selection_dates.to_period("M")
    else:
        selection_periods = selection_dates.to_period("Q")

    spd: list = [None] * len(test_idx)
    spd_sa: list = [None] * len(test_idx)
    for period in sorted(periods.unique()):
        pm = periods == period
        pi = np.where(pm)[0]
        if len(pi) == 0:
            continue
        selection_pi = np.where(selection_periods == period)[0]
        before = np.where(selection_periods < period)[0]
        if len(before) > lb:
            before = before[-lb:]
        avg_sa = (
            np.nanmean(selection_sa[before], axis=0)
            if len(before) >= 5
            else selection_sa[selection_pi[0]]
        )
        if np.all(np.isnan(avg_sa)):
            continue
        sel = select_combo_signals(avg_sa, cat_indices,
                                   combo_template, min_acc_val)
        if len(sel) == 0:
            continue
        for i in pi:
            spd[i] = sel
            spd_sa[i] = avg_sa

    # V25: Precompute NORMALIZED signal averages — both in [-1,+1]
    sig_avg = np.zeros(len(test_idx), dtype=np.float64)
    has_sig = np.zeros(len(test_idx), dtype=bool)
    for i in range(len(test_idx)):
        sel = spd[i]
        if sel is not None and len(sel) > 0:
            n_s = float(len(sel))
            if sig_mode == "weighted" and spd_sa[i] is not None:
                sig_avg[i] = weighted_signal_sum(
                    sig_matrix[test_idx[i]], sel, spd_sa[i]) / n_s
            else:
                sig_avg[i] = float(
                    sig_matrix[test_idx[i], sel].sum()) / n_s
            has_sig[i] = True

    # V25: vote_sum = lgbm_w * ml_base + sig_avg (both in [-1,+1])
    vs = lgbm_w * ml_base + sig_avg
    vs_full = np.where(has_sig, vs, ml_base)

    # V28: three-class — flat when VT>0 and |vs| < VT
    final = ens.copy()
    if vt > 0:
        final[has_sig & (vs_full > vt)] = 1
        final[has_sig & (vs_full < -vt)] = -1
        final[has_sig & (np.abs(vs_full) <= vt)] = 0  # V28: flat
    else:
        final[has_sig & (vs_full >= 0)] = 1
        final[has_sig & (vs_full < 0)] = -1

    # V25: NO balance fix — predictions are pure

    if emit_report:
        sv, _ = pacc(final, true_labels, sim_m)
        rv, _ = pacc(final, true_labels, real_m)
        pv, _ = pacc(final, true_labels, pre_m)
        s_tr = trade_rate_fn(final, sim_m)
        r_tr = trade_rate_fn(final, real_m)
        _, sb = check_balance(final[sim_m], test_dates[sim_m])
        _, rb = check_balance(final[real_m], test_dates[real_m])
        nb = len(sb) + len(rb)
        gate = (sv >= 0.60 and rv >= 0.60 and nb <= 1
                and s_tr > 0.70 and r_tr > 0.70)

        print(f"\n{'=' * 80}")
        print(f"  {tenor} RESULTS  K={K} {combo_name} lgbm_w={lgbm_w} "
              f"VT={vt} {rebal} ew={ew} mac={min_acc_val}")
        print(f"  ens_mode={ens_mode} ml_mode={ml_mode} sig_mode={sig_mode}")
        print(f"  seeds={seeds}")
        print(f"  pre={pv:.1%}  sim={sv:.1%}  real={rv:.1%}  bad={nb}")
        print(f"  sim_trade_rate={s_tr:.1%}  real_trade_rate={r_tr:.1%}")
        if sb:
            print(f"  sim bad months: {sb}")
        if rb:
            print(f"  real bad months: {rb}")
        print(f"  GATE: {'PASS' if gate else 'FAIL'}")
        print(f"  Time: {(time.time() - t0) / 60:.1f} min")
        print(f"{'=' * 80}")

        # -- V28: Monthly detail report (sim+real merged) --
        _print_monthly_detail_v28(final, true_labels, test_dates, sim_m, real_m,
                                  tenor)

    if cfg.get("return_details", False):
        return _prediction_detail_frame(
            df,
            test_idx,
            final,
            true_labels,
            ens_prob,
            ml_base,
            sig_avg,
            has_sig,
            vs_full,
        )
    return final


def _print_monthly_detail_v28(final, true_labels, test_dates, sim_m, real_m,
                               tenor):
    """V28: Print merged sim+real monthly table with flat/trade columns."""
    combined_m = sim_m | real_m
    preds = final[combined_m]
    labels = true_labels[combined_m]
    dates = test_dates[combined_m]
    months = dates.to_period("M")

    print(f"\n{'=' * 120}")
    print(f"  {tenor} Sim+Real期 (T+5日预测) — V28三分类月度评估汇总")
    print(f"{'=' * 120}")
    print(f"{'月份':>10s}  {'总样本':>4s}  {'涨样本':>4s}  {'跌样本':>4s}  "
          f"{'平样本':>4s}  {'预测涨':>4s}  {'预测跌':>4s}  {'预测平':>4s}  "
          f"{'出手率':>8s}  {'方向准确率':>16s}  {'当月日频':>8s}  "
          f"{'涨精确率':>16s}  {'涨召回率':>16s}  "
          f"{'跌精确率':>16s}  {'跌召回率':>16s}")

    total_trade_correct = 0
    total_trade_n = 0
    total_n = 0
    acc_list = []
    tr_list = []

    for month in sorted(months.unique()):
        mm = months == month
        m_preds = preds[mm]
        m_labels = labels[mm]
        n = len(m_preds)
        n_up_true = int((m_labels == 1).sum())
        n_dn_true = int((m_labels == -1).sum())
        n_flat_true = n - n_up_true - n_dn_true
        n_pred_up = int((m_preds == 1).sum())
        n_pred_dn = int((m_preds == -1).sum())
        n_pred_flat = int((m_preds == 0).sum())

        # V28: accuracy only on trade samples
        trade_mask = m_preds != 0
        n_trade = int(trade_mask.sum())
        m_trade_rate = n_trade / max(n, 1)
        tr_list.append(m_trade_rate)

        if n_trade > 0:
            trade_correct = int((m_preds[trade_mask] == m_labels[trade_mask]).sum())
            trade_acc = trade_correct / n_trade
        else:
            trade_correct = 0
            trade_acc = float("nan")
        total_trade_correct += trade_correct
        total_trade_n += n_trade
        total_n += n
        acc_list.append(trade_acc)

        # All-sample accuracy (for reference)
        all_correct = int((m_preds == m_labels).sum())
        all_acc = all_correct / max(n, 1)

        # Up precision/recall (on trade samples)
        up_tp = int(((m_preds == 1) & (m_labels == 1)).sum())
        up_prec = up_tp / max(n_pred_up, 1)
        up_rec = up_tp / max(n_up_true, 1)
        dn_tp = int(((m_preds == -1) & (m_labels == -1)).sum())
        dn_prec = dn_tp / max(n_pred_dn, 1)
        dn_rec = dn_tp / max(n_dn_true, 1)

        up_prec_s = f"{up_prec:.0%} ({up_tp}/{n_pred_up})"
        up_rec_s = f"{up_rec:.0%} ({up_tp}/{n_up_true})"
        dn_prec_s = f"{dn_prec:.0%} ({dn_tp}/{n_pred_dn})"
        dn_rec_s = f"{dn_rec:.0%} ({dn_tp}/{n_dn_true})"

        acc_s = f"{trade_acc:.2%} ({trade_correct}/{n_trade})" if n_trade > 0 else "N/A"
        print(f"{str(month):>10s}  {n:4d}  {n_up_true:4d}  {n_dn_true:4d}  "
              f"{n_flat_true:4d}  {n_pred_up:4d}  {n_pred_dn:4d}  {n_pred_flat:4d}  "
              f"{m_trade_rate:7.1%}  "
              f"{acc_s:>16s}  {all_acc:7.2%}  "
              f"{up_prec_s:>16s}  {up_rec_s:>16s}  "
              f"{dn_prec_s:>16s}  {dn_rec_s:>16s}")

    print(f"\n  🎯 汇总统计:")
    overall_acc = total_trade_correct / max(total_trade_n, 1)
    valid_accs = [a for a in acc_list if not np.isnan(a)]
    mean_acc = float(np.mean(valid_accs)) if valid_accs else 0
    std_acc = float(np.std(valid_accs)) if valid_accs else 0
    best_i = int(np.argmax(valid_accs)) if valid_accs else 0
    worst_i = int(np.argmin(valid_accs)) if valid_accs else 0
    ms = sorted(months.unique())
    overall_tr = total_trade_n / max(total_n, 1)
    print(f"    🎯 整体方向准确率（加权平均）: {overall_acc:.4f} ({overall_acc:.2%})")
    print(f"    🎯 平均方向准确率: {mean_acc:.4f} ({mean_acc:.2%})")
    if valid_accs:
        print(f"    🎯 最高方向准确率: {max(valid_accs):.4f} ({max(valid_accs):.2%})"
              f" - {ms[best_i]}")
        print(f"    🎯 最低方向准确率: {min(valid_accs):.4f} ({min(valid_accs):.2%})"
              f" - {ms[worst_i]}")
    print(f"    🎯 准确率标准差: {std_acc:.4f}")
    print(f"    🎯 总出手率: {overall_tr:.4f} ({overall_tr:.2%})")
    # Overall up/down precision
    all_up_tp = int(((preds == 1) & (labels == 1)).sum())
    all_pred_up = int((preds == 1).sum())
    all_dn_tp = int(((preds == -1) & (labels == -1)).sum())
    all_pred_dn = int((preds == -1).sum())
    print(f"    🎯 加权平均涨准确率: "
          f"{all_up_tp / max(all_pred_up, 1):.4f} "
          f"({all_up_tp / max(all_pred_up, 1):.2%})")
    print(f"    🎯 加权平均跌准确率: "
          f"{all_dn_tp / max(all_pred_dn, 1):.4f} "
          f"({all_dn_tp / max(all_pred_dn, 1):.2%})")
    print(f"{'=' * 120}")

# Blackbox V2 transport
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "predicted_direction",
)
DATA_FILES = (
    "daily_output.csv", "weekly_output.csv", "monthly_output.csv",
    "api_wind_date.csv", "factor_catalog.csv",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^\d{6}$")
ADDITIVE_MONTHLY = frozenset({"M0041340", "M0041341", "M0041342"})
REQUIRED_DAILY = frozenset(
    set(COL_MAP.values())
    | {factor for group in MF_CATEGORIES.values() for factor in group}
)
REQUIRED_WEEKLY = frozenset(WEEKLY_COLS)
REQUIRED_MONTHLY = frozenset(MONTHLY_COLS) - ADDITIVE_MONTHLY


class ContractError(ValueError):
    """Blackbox 输入或输出合同错误。"""


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ContractError(f"{field} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{field} is invalid") from exc
    return value


def validate_request(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != set(REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    if any(not isinstance(raw[field], str) for field in REQUEST_FIELDS):
        raise ContractError("all request fields must be strings")
    request = dict(raw)
    if not request["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature = _date(request["feature_date"], "feature_date")
    predict = _date(request["predict_date"], "predict_date")
    target = _date(request["target_date"], "target_date")
    if _date(request["daily_cutoff_key"], "daily_cutoff_key") != feature:
        raise ContractError("daily_cutoff_key must equal feature_date")
    if not feature <= predict <= target or not feature < target:
        raise ContractError("dates must satisfy feature <= predict <= target")
    if any(
        not KEY_RE.fullmatch(request[field])
        for field in ("weekly_cutoff_key", "monthly_cutoff_key")
    ):
        raise ContractError("weekly/monthly cutoff keys must be six digits")
    return request


def read_requests(args: argparse.Namespace) -> list[dict[str, str]]:
    try:
        if args.command == "predict":
            with args.request.open(encoding="utf-8-sig") as handle:
                requests = [validate_request(json.load(handle))]
        else:
            with args.requests.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if (
                    set(reader.fieldnames or ()) != set(REQUEST_FIELDS)
                    or len(reader.fieldnames or ()) != len(REQUEST_FIELDS)
                ):
                    raise ContractError(
                        "request CSV fields must exactly match Contract 1.0"
                    )
                requests = [validate_request(row) for row in reader]
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise ContractError("cannot read Request") from exc
    if args.command == "backtest" and not requests:
        raise ContractError("backtest requires at least one request")
    ids = [item["request_id"] for item in requests]
    if len(ids) != len(set(ids)):
        raise ContractError("request_id must be unique")
    return requests


def _period(path: Path, key: str) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype={key: "string"})
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError(f"cannot read {path.name}") from exc
    if key not in frame or frame.empty:
        raise ContractError(f"{path.name} is empty or missing {key}")
    values = frame[key].astype("string")
    if (
        values.isna().any()
        or not values.str.fullmatch(KEY_RE).fillna(False).all()
        or values.duplicated().any()
        or not values.is_monotonic_increasing
    ):
        raise ContractError(f"{path.name} {key} is invalid")
    return frame


def read_snapshot(
    data_dir: Path, requests: list[dict[str, str]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    paths = {name: data_dir / name for name in DATA_FILES}
    if any(not path.is_file() for path in paths.values()):
        raise ContractError("data-dir must contain exactly the five required files")
    try:
        daily = pd.read_csv(paths["daily_output.csv"])
        calendar = pd.read_csv(
            paths["api_wind_date.csv"],
            dtype={"rdate": "string", "week_id": "string"},
            keep_default_na=False,
        )
        catalog = pd.read_csv(paths["factor_catalog.csv"], dtype="string")
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError("cannot read DataBridge snapshot") from exc
    weekly = _period(paths["weekly_output.csv"], "week_id")
    monthly = _period(paths["monthly_output.csv"], "month_id")
    if not ({"date"} | REQUIRED_DAILY) <= set(daily):
        raise ContractError("daily_output.csv is missing required factors")
    if not ({"week_id"} | REQUIRED_WEEKLY) <= set(weekly):
        raise ContractError("weekly_output.csv is missing required factors")
    if not ({"month_id"} | REQUIRED_MONTHLY) <= set(monthly):
        raise ContractError("monthly_output.csv is missing required factors")
    if list(calendar.columns) != ["rdate", "week_id"] or calendar.empty:
        raise ContractError("api_wind_date.csv schema is invalid")
    if set(catalog.columns) != {
        "indicators_code", "frequency", "factor_version"
    } or catalog.empty:
        raise ContractError("factor_catalog.csv schema is invalid")
    for frequency, required in (
        ("daily", REQUIRED_DAILY),
        ("weekly", REQUIRED_WEEKLY),
        ("monthly", REQUIRED_MONTHLY),
    ):
        declared = set(
            catalog.loc[catalog["frequency"].eq(frequency), "indicators_code"]
            .dropna().astype(str)
        )
        if not required <= declared:
            raise ContractError(f"factor_catalog.csv lacks {frequency} factors")
    try:
        daily["date"] = pd.to_datetime(
            daily["date"], format="%Y-%m-%d", errors="raise"
        ).dt.normalize()
        calendar["rdate"] = pd.to_datetime(
            calendar["rdate"], format="%Y-%m-%d", errors="raise"
        ).dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily/calendar date is invalid") from exc
    if (
        daily.empty or daily["date"].duplicated().any()
        or not daily["date"].is_monotonic_increasing
        or calendar["rdate"].duplicated().any()
        or not calendar["rdate"].is_monotonic_increasing
        or not calendar["week_id"].str.fullmatch(KEY_RE).all()
    ):
        raise ContractError("daily/calendar key is invalid")
    monthly = monthly.drop(columns=list(ADDITIVE_MONTHLY), errors="ignore")
    daily_keys = set(daily["date"].dt.strftime("%Y-%m-%d"))
    weekly_keys = set(weekly["week_id"].astype(str))
    monthly_keys = set(monthly["month_id"].astype(str))
    week_by_date = calendar.set_index(
        calendar["rdate"].dt.strftime("%Y-%m-%d")
    )["week_id"]
    for request in requests:
        feature = request["feature_date"]
        if (
            feature not in daily_keys
            or request["weekly_cutoff_key"] not in weekly_keys
            or request["monthly_cutoff_key"] not in monthly_keys
            or feature not in week_by_date
            or str(week_by_date.loc[feature]) != request["weekly_cutoff_key"]
        ):
            raise ContractError(
                f"Request cutoff is not present/consistent: {request['request_id']}"
            )
    return daily, weekly, monthly, calendar


def generate_results(
    requests: list[dict[str, str]],
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    monthly: pd.DataFrame,
    calendar: pd.DataFrame,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for request in requests:
        grouped.setdefault(request["feature_date"][:7], []).append(request)
    feature_end = max(item["feature_date"] for item in requests)
    weekly_end = max(item["weekly_cutoff_key"] for item in requests)
    monthly_end = max(item["monthly_cutoff_key"] for item in requests)
    daily_input = daily.loc[daily["date"] <= pd.Timestamp(feature_end)].copy()
    weekly_input = weekly.loc[weekly["week_id"].astype(str) <= weekly_end].copy()
    monthly_input = monthly.loc[monthly["month_id"].astype(str) <= monthly_end].copy()
    calendar_input = calendar.loc[calendar["rdate"] <= pd.Timestamp(feature_end)]
    date_to_week = dict(zip(
        calendar_input["rdate"].dt.strftime("%Y-%m-%d"),
        calendar_input["week_id"].astype(str),
    ))
    prepared_inputs = prepare_prediction_inputs(model_config(
        daily_df=daily_input,
        weekly_df=weekly_input,
        monthly_df=monthly_input,
        date_to_week=date_to_week,
    ))
    predictions: dict[str, int] = {}
    for month, batch in sorted(grouped.items()):
        feature_end = max(item["feature_date"] for item in batch)
        with redirect_stdout(sys.stderr):
            details = run_prediction(model_config(
                test_start=f"{month}-01",
                test_end=feature_end,
                require_labels=False,
                emit_report=False,
                return_details=True,
                requested_dates=[item["feature_date"] for item in batch],
                prepared_inputs=prepared_inputs,
                n_workers=4,
            ))
        if not isinstance(details, pd.DataFrame) or details.empty:
            raise ContractError(f"algorithm returned no rows for {month}")
        by_date = dict(zip(
            details["anchor_date"].astype(str),
            details["prediction"].astype(int),
        ))
        for request in batch:
            if request["feature_date"] not in by_date:
                raise ContractError(
                    f"algorithm omitted request: {request['request_id']}"
                )
            direction = int(by_date[request["feature_date"]])
            if direction not in (-1, 0, 1):
                raise ContractError("predicted_direction is invalid")
            predictions[request["request_id"]] = direction
    return [
        {field: item[field] for field in RESULT_FIELDS[:-1]}
        | {"predicted_direction": predictions[item["request_id"]]}
        for item in requests
    ]


def _atomic_write(path: Path, writer: Any) -> None:
    if not path.parent.is_dir():
        raise ContractError("output parent does not exist")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def emit(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    if args.command == "predict":
        _atomic_write(
            args.output,
            lambda handle: json.dump(
                rows[0], handle, ensure_ascii=False, separators=(",", ":")
            ),
        )
        return
    def writer(handle: Any) -> None:
        output = csv.DictWriter(
            handle, fieldnames=RESULT_FIELDS, lineterminator="\n"
        )
        output.writeheader()
        output.writerows(rows)
    _atomic_write(args.output, writer)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="V28 Blackbox V2 successor")
    commands = result.add_subparsers(dest="command", required=True)
    for command, flag in (("predict", "--request"), ("backtest", "--requests")):
        item = commands.add_parser(command)
        item.add_argument(flag, type=Path, required=True)
        item.add_argument("--data-dir", type=Path, required=True)
        item.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    np.random.seed(42)
    try:
        args = parser().parse_args()
        requests = read_requests(args)
        rows = generate_results(requests, *read_snapshot(args.data_dir, requests))
        emit(args, rows)
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected execution failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
