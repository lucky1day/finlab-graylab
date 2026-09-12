#!/usr/bin/env python3
"""10Y01 SAY full-OOS 的自包含 Blackbox V2 私有状态草稿。

从2024-01-01累计完整OOS历史，保留Native三方共识与DIV连续方向阈值10。
10Y与7Y独立维护Phase A派生数组；仅通过显式state-input/state-output读取发布。
每个Request按自身截止推进，重算受周频投影影响的尾部及完整排名、信号和控制器。
尚未完成真实输入等价、恢复语义及目标环境性能验收，不用于正式入库。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import platform
import zipfile
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

warnings.filterwarnings("ignore")


def align_monthly_previous_month(
    monthly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
) -> pd.DataFrame:
    """把月频输入对齐到每个日频样本的上一自然月。"""
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if "month_id" not in monthly_df.columns:
        return pd.DataFrame(index=range(len(daily_index)))
    monthly = monthly_df.copy()
    monthly["month_id"] = monthly["month_id"].astype(str).str.strip()
    monthly = monthly[
        monthly["month_id"].str.fullmatch(r"\d{6}", na=False)
    ]
    monthly = monthly.sort_values("month_id").drop_duplicates(
        "month_id", keep="last"
    )
    value_cols = [col for col in monthly.columns if col != "month_id"]
    if not value_cols:
        return pd.DataFrame(index=range(len(daily_index)))
    monthly_by_id = monthly.set_index("month_id")
    result = {
        col: np.full(len(daily_index), np.nan, dtype=np.float64)
        for col in value_cols
    }
    for row_index, daily_date in enumerate(daily_index):
        year = int(daily_date.year)
        month = int(daily_date.month) - 1
        if month == 0:
            year -= 1
            month = 12
        month_id = f"{year:04d}{month:02d}"
        if month_id not in monthly_by_id.index:
            continue
        for col in value_cols:
            result[col][row_index] = monthly_by_id.at[month_id, col]
    return pd.DataFrame(result, index=range(len(daily_index)))

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

MODEL_VERSION = "liwei_0616_10y_01_v61"
SOURCE_MODEL_ID = "10Y_01_cons_SAY_k_3_DIV_K_10"
TARGET_TENOR = "10Y"
SOURCE_IC_SCREEN_START = "2024-01-01"

_SHARED_LGBM: dict[str, Any] = {
    "lgbm_windows": [200, 350, 504, 756],
    "lgbm_leaves": [3, 5, 7, 15],
    "lgbm_min_child": [10, 20],
    "lgbm_alpha": [0.0, 1.0],
    "lgbm_lambda": [0.5, 3.0],
    "lgbm_split": [0.55, 0.70],
    "lgbm_slow_path": True,
    "ic_top_self": 50,
    "ic_top_mf": 30,
    "vt_mode": "seasonal",
    "sig_mode": "equal",
}

BASELINE_CONFIGS: dict[str, dict[str, Any]] = {
    "STD": {
        **_SHARED_LGBM,
        "name": "10y_std",
        "tenor": "10Y",
        "close": "TB0YWI0C",
        "self_name": "10Y",
        "aux_pairs": [("5Y", "TB5YWI0C"), ("1Y", "TB1YWI0C")],
        "seeds": [42, 314, 159],
        "K": 10,
        "combo_name": "rel_value2_policy2_butterfly2",
        "combo_template": {"rel_value": 2, "policy": 2, "butterfly": 2},
        "lgbm_w": 0.10,
        "rebal": "quarterly",
        "ew": 504,
        "min_acc": 0.48,
        "ml_mode": "prob",
        "ens_mode": "standard",
    },
    "DIV": {
        **_SHARED_LGBM,
        "name": "10y_div",
        "tenor": "10Y",
        "close": "TB0YWI0C",
        "self_name": "10Y",
        "aux_pairs": [("5Y", "TB5YWI0C"), ("1Y", "TB1YWI0C")],
        "seeds": [42, 314, 159],
        "K": 5,
        "combo_name": "policy2_butterfly2",
        "combo_template": {"policy": 2, "butterfly": 2},
        "lgbm_w": 0.30,
        "rebal": "quarterly",
        "ew": 378,
        "min_acc": 0.50,
        "ml_mode": "binary",
        "ens_mode": "diverse",
    },
    "ACCWT": {
        **_SHARED_LGBM,
        "name": "10y_accwt",
        "tenor": "10Y",
        "close": "TB0YWI0C",
        "self_name": "10Y",
        "aux_pairs": [("5Y", "TB5YWI0C"), ("1Y", "TB1YWI0C")],
        "seeds": [42, 314, 159],
        "K": 10,
        "combo_name": "rel_value2_policy2_butterfly2",
        "combo_template": {"rel_value": 2, "policy": 2, "butterfly": 2},
        "lgbm_w": 0.10,
        "rebal": "quarterly",
        "ew": 504,
        "min_acc": 0.48,
        "ml_mode": "prob",
        "ens_mode": "accwt",
    },
    "V55_7Y": {
        **_SHARED_LGBM,
        "name": "10y_7y_cross",
        "tenor": "7Y",
        "close": "TB7YWI0C",
        "self_name": "7Y",
        "aux_pairs": [("5Y", "TB5YWI0C"), ("10Y", "TB0YWI0C")],
        "seeds": [42, 314],
        "K": 15,
        "combo_name": "butterfly3_term_prem2",
        "combo_template": {"butterfly": 3, "term_prem": 2},
        "lgbm_w": 1.50,
        "rebal": "quarterly",
        "ew": 252,
        "min_acc": 0.45,
        "ml_mode": "prob",
        "ens_mode": "standard",
    },
}

PROD_CONFIG: dict[str, Any] = {
    "model_no": 1,
    "name": "cons(SAY,k=3)+DIV_K=10",
    "baselines": ["STD", "ACCWT", "V55_7Y"],
    "k_agree": 3,
    "fallback": "DIV",
    "streak_K": 10,
}


def model_config(baseline: str = "STD", **overrides) -> dict[str, Any]:
    """返回 10Y_01 某个 baseline 的固定参数副本。"""
    if baseline not in BASELINE_CONFIGS:
        raise KeyError(f"unknown 10Y_01 baseline: {baseline}")
    cfg = dict(BASELINE_CONFIGS[baseline])
    cfg.update(overrides)
    return cfg


def required_baselines(cfg: dict[str, Any] | None = None) -> list[str]:
    """返回投票基线加 fallback 基线，保持顺序且去重。"""
    model_cfg = PROD_CONFIG if cfg is None else cfg
    names = list(model_cfg["baselines"])
    fallback = str(model_cfg["fallback"])
    if fallback not in names:
        names.append(fallback)
    return names

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
        # pct_change(fill_method='pad') forward-fills NaN before computing returns
        ret = pd.Series(close).pct_change().values

        # Lag returns
        for lag in (1, 2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120):
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

    # Spread features (must come before streak to match sweep column order)
    pair_list = []
    self_col = close_col
    for aname, acol in aux_pairs:
        pair_list.append((self_name, aname, self_col, acol))
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

    # V27: Streak + up-fraction features (24 features = 3 tenors x 8)
    for name, col in all_pairs:
        ret_s = pd.Series(df[col].values.astype(np.float64)).pct_change()
        sign_r = np.sign(ret_s).fillna(0).values
        streak = np.zeros(len(sign_r), dtype=np.float64)
        for i in range(1, len(sign_r)):
            if sign_r[i] == sign_r[i - 1] and sign_r[i] != 0:
                streak[i] = streak[i - 1] + sign_r[i]
            else:
                streak[i] = sign_r[i]
        features[f"{name}_streak_count"] = streak
        features[f"{name}_streak_abs"] = np.abs(streak)
        up_flag = (ret_s > 0).astype(float)
        for w in [5, 10, 20]:
            uf = up_flag.rolling(w).mean().values
            features[f"{name}_up_frac{w}"] = uf
            features[f"{name}_up_frac{w}_dev"] = uf - 0.5

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
if _HAS_NUMBA:
    @_njit(cache=False)
    def _ic_batch_nb(fv, ll, min_count=50):
            """Batch |Pearson IC| for every feature column.  Numba prange parallel."""
            n_rows, n_feat = fv.shape
            ics = np.zeros(n_feat, dtype=np.float64)
            for j in range(n_feat):
                cnt = 0; sv = 0.0; sl = 0.0
                for i in range(n_rows):
                    if not np.isnan(fv[i, j]):
                        cnt += 1
                        sv += fv[i, j]
                        sl += ll[i]
                if cnt < min_count:
                    continue
                mv = sv / cnt; ml = sl / cnt
                cov = 0.0; vv = 0.0; vl = 0.0
                for i in range(n_rows):
                    if not np.isnan(fv[i, j]):
                        dv = fv[i, j] - mv
                        dl = ll[i] - ml
                        cov += dv * dl
                        vv += dv * dv
                        vl += dl * dl
                d = np.sqrt(vv * vl)
                if d < 1e-12:
                    continue
                ics[j] = abs(cov / d)
            return ics


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

    if _HAS_NUMBA:
        ics = _ic_batch_nb(fv.astype(np.float64), ll.astype(np.float64), 50)
    else:
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


# -- Read-only context shared by bounded worker threads ----------------------
_G: dict = {}
_LGB_DATASET_CACHE = threading.local()


def _init_worker(df_len, feat, labels, close, fallback, test_idx,
                 horizon, purge_gap, seeds=None,
                 predict_feat_overrides=None, model_memo=None,
                 model_dates=None, selected_feature_names=None):
    _G.update(df_len=df_len, feat=feat, labels=labels, close=close,
              fallback=fallback, test_idx=test_idx,
              horizon=horizon, purge_gap=purge_gap,
              seeds=seeds or [42, 314],
              predict_feat_overrides=predict_feat_overrides or {},
              phase_context=object(), model_memo=model_memo,
              model_dates=model_dates, selected_feature_names=selected_feature_names)
    _LGB_DATASET_CACHE.items = {}


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
        model_memo = _G.get("model_memo")
        memo_key = json.dumps(config, sort_keys=True)
        n_test = len(test_idx)
        preds = np.zeros(n_test, dtype=np.int32)
        probs = np.full(n_test, 0.5, dtype=np.float64)
        base_preds = np.zeros(n_test, dtype=np.int32)
        base_probs = np.full(n_test, 0.5, dtype=np.float64)

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
                if model_memo is not None:
                    model_memo.pop(memo_key, None)
                preds[i] = int(_G["fallback"].iloc[idx])
                base_preds[i] = preds[i]
                probs[i] = 1.0 if preds[i] == 1 else 0.0
                base_probs[i] = probs[i]
                continue
            split = max(80, int(len(elig) * config["split_pct"]))
            purge_end = min(split + purge_gap, len(elig) - 20)
            fit_idx = elig[:split]
            cal_idx = elig[purge_end:]
            if len(cal_idx) < 20:
                if model_memo is not None:
                    model_memo.pop(memo_key, None)
                preds[i] = int(_G["fallback"].iloc[idx])
                base_preds[i] = preds[i]
                probs[i] = 1.0 if preds[i] == 1 else 0.0
                base_probs[i] = probs[i]
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

            cached = None
            if model_memo is not None:
                model_date = str(_G["model_dates"][idx])
                dependency = hashlib.sha256(json.dumps({
                    "date": model_date,
                    "features": _G["selected_feature_names"],
                    "horizon": horizon, "purge_gap": purge_gap,
                    "config": config, "seeds": seeds,
                    "params": [_lgbm_train_params(config, seed) for seed in seeds],
                    "rounds": config.get("n_estimators", 140),
                    "early_stopping": 20,
                    "arrays": _payload_digest(fit_idx, cal_idx, feat[fit_idx],
                                              fit_y, sample_w, feat[cal_idx], cal_y),
                }, sort_keys=True).encode()).hexdigest()
                previous = model_memo.get(memo_key)
                if previous is not None and previous[0] == dependency:
                    cached = previous

            prob_seeds = []
            override_prob_seeds = []
            cal_probs_first = None
            trained_models = []
            dataset_cache = getattr(_LGB_DATASET_CACHE, "items", None)
            if dataset_cache is None:
                dataset_cache = {}
                _LGB_DATASET_CACHE.items = dataset_cache
            dataset_key = (
                _G["phase_context"],
                int(idx),
                int(w),
                float(config["split_pct"]),
                int(config["min_child_samples"]),
            )
            if cached is None:
                datasets = dataset_cache.get(dataset_key)
                if datasets is None:
                    train_set = lgb.Dataset(
                        feat[fit_idx], label=fit_y, weight=sample_w)
                    valid_set = lgb.Dataset(
                        feat[cal_idx], label=cal_y, reference=train_set)
                    datasets = (train_set, valid_set)
                    dataset_cache[dataset_key] = datasets
                train_set, valid_set = datasets
            for seed_index, seed in enumerate(seeds):
                if cached is None:
                    n_est = config.get("n_estimators", 140)
                    params = _lgbm_train_params(config, seed)
                    model = lgb.train(
                        params,
                        train_set,
                        num_boost_round=n_est,
                        valid_sets=[valid_set],
                        callbacks=[lgb.early_stopping(20, verbose=False)],
                    )
                else:
                    model = cached[1][seed_index]
                if model_memo is not None:
                    trained_models.append(model)
                p = float(model.predict(
                    feat[[idx]], num_iteration=model.best_iteration)[0])
                prob_seeds.append(p)
                override_row = _G["predict_feat_overrides"].get(int(idx))
                if override_row is None:
                    override_prob_seeds.append(p)
                else:
                    override_prob_seeds.append(float(model.predict(
                        np.asarray(override_row, dtype=np.float64).reshape(1, -1),
                        num_iteration=model.best_iteration,
                    )[0]))
                if cached is None and cal_probs_first is None:
                    cal_probs_first = model.predict(
                        feat[cal_idx], num_iteration=model.best_iteration)

            base_prob = float(np.mean(prob_seeds))
            prob = float(np.mean(override_prob_seeds))
            base_probs[i] = base_prob
            probs[i] = prob
            threshold = (cached[2] if cached is not None
                         else choose_threshold(cal_probs_first, labels[cal_idx]))
            if model_memo is not None and cached is None:
                # 一配置只保存最近日期全部seed；成功后替换，不保存Dataset或历史模型。
                model_memo[memo_key] = (dependency, tuple(trained_models), threshold)
            base_preds[i] = 1 if base_prob >= threshold else -1
            preds[i] = 1 if prob >= threshold else -1
        return {
            "config": config,
            "preds": preds,
            "probs": probs,
            "base_preds": base_preds,
            "base_probs": base_probs,
        }
    except Exception as exc:
        raise RuntimeError(f"10Y01 LightGBM config failed: {config!r}") from exc


def _lgbm_train_params(config: dict, seed: int) -> dict[str, Any]:
    """返回与原 LGBMClassifier 路径完全一致的训练参数。"""
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


def _dynamic_report_masks(test_dates: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按源脚本固定 OOS 段位切分 pre/sim/real。"""
    if len(test_dates) == 0:
        empty = np.zeros(0, dtype=bool)
        return empty, empty, empty
    dates = pd.DatetimeIndex(test_dates)
    sim_start = pd.Timestamp(year=2025, month=1, day=1)
    real_start = pd.Timestamp(year=2025, month=7, day=1)
    pre_m = np.asarray(dates < sim_start, dtype=bool)
    sim_m = np.asarray((dates >= sim_start) & (dates < real_start), dtype=bool)
    real_m = np.asarray(dates >= real_start, dtype=bool)
    return pre_m, sim_m, real_m


# ============================================================================
# MAIN RUNNER
# ============================================================================
def prepare_model_frames(
    daily_df: pd.DataFrame | None,
    weekly_df: pd.DataFrame | None,
    monthly_df: pd.DataFrame | None,
    daily_dates: pd.Series,
    date_to_week: Mapping[str, int | str] | None = None,
    week_end_by_id: Mapping[int, str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把平台产出的 weekly/monthly artifact 对齐成算法特征矩阵。"""
    if weekly_df is None:
        aligned_weekly = pd.DataFrame(index=range(len(daily_dates)))
    else:
        aligned_weekly = _align_weekly_like_legacy(
            weekly_df,
            daily_dates,
            date_to_week,
            week_end_by_id,
        )

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


def _align_weekly_like_legacy(
    weekly_df: pd.DataFrame,
    daily_dates: pd.Series | pd.DatetimeIndex | list,
    date_to_week: Mapping[str, int | str] | None = None,
    week_end_by_id: Mapping[int, str] | None = None,
) -> pd.DataFrame:
    weekly = _normalize_aux_frame(weekly_df, "week_id")
    if weekly is None:
        return pd.DataFrame(index=range(len(daily_dates)))
    existing = [col for col in WEEKLY_COLS if col in weekly.columns]
    if not existing:
        return pd.DataFrame(index=range(len(daily_dates)))
    daily_index = pd.DatetimeIndex(pd.to_datetime(daily_dates)).normalize()
    if len(daily_index) == 0:
        return pd.DataFrame(index=range(len(daily_dates)))

    order = np.argsort(daily_index.values, kind="stable")
    sorted_dates = daily_index[order]
    week_arr = _weekly_ids_for_daily_dates(sorted_dates, date_to_week)
    last_row_by_week: dict[int, int] = {}
    for sorted_pos, week_id in enumerate(week_arr):
        if week_id is not None:
            if week_end_by_id is not None:
                expected_end = week_end_by_id.get(int(week_id))
                if (
                    expected_end is None
                    or sorted_dates[sorted_pos].strftime("%Y-%m-%d")
                    != str(expected_end)
                ):
                    continue
            original_pos = int(order[sorted_pos])
            last_row_by_week[int(week_id)] = original_pos

    weekly = weekly.dropna(subset=["week_id"]).copy()
    weekly["week_id"] = weekly["week_id"].astype(int)
    weekly_indexed = weekly.sort_values("week_id").drop_duplicates("week_id", keep="last").set_index("week_id")
    result: dict[str, np.ndarray] = {
        col: np.full(len(daily_dates), np.nan, dtype=np.float64) for col in existing
    }
    for col in existing:
        placed = np.full(len(daily_index), np.nan, dtype=np.float64)
        for week_id, row_index in last_row_by_week.items():
            if week_id in weekly_indexed.index:
                placed[row_index] = float(weekly_indexed.at[week_id, col])
        filled = pd.Series(placed[order]).ffill().to_numpy(dtype=np.float64)
        out = np.full(len(daily_index), np.nan, dtype=np.float64)
        out[order] = filled
        result[col] = out
    return pd.DataFrame(result, index=range(len(daily_dates)))


def _weekly_ids_for_daily_dates(
    sorted_dates: pd.DatetimeIndex,
    date_to_week: Mapping[str, int | str] | None,
) -> list[int | None]:
    if date_to_week is not None:
        result: list[int | None] = []
        for daily_date in sorted_dates:
            raw = date_to_week.get(daily_date.strftime("%Y-%m-%d"))
            try:
                result.append(None if raw is None else int(raw))
            except (TypeError, ValueError):
                result.append(None)
        return result

    result = []
    current_year, seq, previous_dow = int(sorted_dates[0].year), 1, -1
    previous_date: pd.Timestamp | None = None
    for daily_date in sorted_dates:
        if int(daily_date.year) != current_year:
            current_year, seq, previous_dow = int(daily_date.year), 1, -1
        dow = int(daily_date.weekday())
        if previous_date is not None and previous_dow >= 0 and (
            (dow <= previous_dow and (daily_date - previous_date).days > 1)
            or (daily_date - previous_date).days > 5
        ):
            seq += 1
        previous_dow = dow
        previous_date = daily_date
        result.append(current_year * 100 + seq)
    return result


def _labels_for_detail(values: np.ndarray) -> list[int | None]:
    result: list[int | None] = []
    for value in values:
        result.append(None if pd.isna(value) else int(value))
    return result


def prepare_prediction_inputs(cfg: dict) -> dict[str, Any]:
    """构建同一月内五次算法阶段可安全复用的只读矩阵。"""
    if "daily_df" not in cfg:
        raise ValueError("daily_df is required")
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
        cfg.get("week_end_by_id"),
    )
    close_col = cfg["close"]
    labels = make_labels(
        df,
        close_col,
        horizon=cfg.get("horizon", HORIZON),
    )
    labels_h1 = make_labels(df, close_col, horizon=1)
    close = df[close_col].values.astype(np.float64)
    fallback = build_fallback_signal(df, close_col)
    bond_f = build_bond_features(df, close_col, cfg["aux_pairs"])
    mf_f, mf_feat_cat = build_mf_features(df)
    wkmo_f = build_wkmo_features(wk_df, mo_df)
    for column in wkmo_f.columns:
        mf_feat_cat[column] = (
            "weekly" if column.startswith("wk_") else "monthly"
        )
    self_cols = list(bond_f.columns)
    mf_cols = list(mf_f.columns) + list(wkmo_f.columns)
    feature_names = self_cols + list(mf_f.columns) + list(wkmo_f.columns)
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
        "feature_names": feature_names,
        "weekly_feature_values": wk_df.ffill().fillna(0.0),
        "n_self": len(self_cols),
        "mf_feat_cat": mf_feat_cat,
        "mf_cols": mf_cols,
        "sig_names": sig_names,
        "sig_matrix": sig_df.values.astype(np.int32),
        "cat_indices": classify_signals(sig_names, cfg["tenor"]),
    }


def _make_phase_a_cache(
    test_dates: pd.DatetimeIndex,
    results: list[dict[str, Any]],
    *,
    pred_key: str = "preds",
    prob_key: str = "probs",
) -> dict[str, Any]:
    """保存 Phase A LGBM 输出，供同一 source 上下文的子窗口复用。"""
    return {
        "test_dates": [str(day.date()) for day in pd.DatetimeIndex(test_dates)],
        "results": [
            {
                "config": dict(item["config"]),
                "preds": np.asarray(item[pred_key], dtype=np.int32).copy(),
                "probs": np.asarray(item[prob_key], dtype=np.float64).copy(),
            }
            for item in results
        ],
    }


def _slice_phase_a_cache(cache: Mapping[str, Any], test_dates: pd.DatetimeIndex) -> list[dict[str, Any]]:
    """按当前 test_dates 从 Phase A cache 中切出与普通 run_config 等价的结果。"""
    cached_dates = [str(day) for day in cache.get("test_dates", [])]
    position_by_date = {day: index for index, day in enumerate(cached_dates)}
    requested_dates = [str(day.date()) for day in pd.DatetimeIndex(test_dates)]
    missing = [day for day in requested_dates if day not in position_by_date]
    if missing:
        raise ValueError(f"missing cached Phase A rows for dates: {missing[:5]}")
    indices = np.asarray([position_by_date[day] for day in requested_dates], dtype=np.int64)
    sliced: list[dict[str, Any]] = []
    for item in cache.get("results", []):
        sliced.append(
            {
                "config": dict(item["config"]),
                "preds": np.asarray(item["preds"], dtype=np.int32)[indices].copy(),
                "probs": np.asarray(item["probs"], dtype=np.float64)[indices].copy(),
            }
        )
    return sliced


def run_prediction(cfg: dict) -> dict[str, Any]:
    """计算固定基线的 Phase A 或原共识实际消费的 vs_full。"""
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

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
    n_workers = cfg.get("n_workers", 10)
    horizon = cfg.get("horizon", HORIZON)
    purge_gap = cfg.get("purge_gap", PURGE_GAP)
    # V25 new parameters
    seeds = cfg.get("seeds", [42, 314])
    ml_mode = cfg.get("ml_mode", "prob")     # "binary" or "prob"
    sig_mode = cfg.get("sig_mode", "equal")  # "equal" or "weighted"
    ens_mode = cfg.get("ens_mode", "standard")  # "standard"/"diverse"/"accwt"
    if "test_start" not in cfg or "test_end" not in cfg:
        raise ValueError("test_start and test_end are required for PIT prediction")
    test_start = pd.Timestamp(cfg["test_start"])
    test_end = pd.Timestamp(cfg["test_end"])
    require_labels = bool(cfg.get("require_labels", True))

    # -- Load or reuse read-only monthly matrices --
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
    print(
        f"  Daily: {len(df)}, "
        f"Weekly cols: {prepared['weekly_column_count']}, "
        f"Monthly cols: {prepared['monthly_column_count']}"
    )

    # -- Features + IC screening --
    print(f"  Using prepared features...")
    print(f"  Self features: {n_self}, MF features: {len(mf_cols)}, "
          f"Total: {feat_all.shape[1]}")

    print(f"  IC screening on source pre-test data (strict gap)...")
    _ts_row = int(np.flatnonzero(
        df["date"].values >= np.datetime64(SOURCE_IC_SCREEN_START))[0])
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
    test_ranges = cfg.get("test_ranges")
    if test_ranges:
        test_mask = np.zeros(len(df), dtype=bool)
        for start, end in test_ranges:
            range_start = pd.Timestamp(start)
            range_end = pd.Timestamp(end)
            test_mask |= (df["date"] >= range_start).values & (df["date"] <= range_end).values
    else:
        test_mask = (df["date"] >= ts_s).values & (df["date"] <= ts_e).values
    test_mask = test_mask & ~np.isnan(close)
    if require_labels:
        test_mask = test_mask & ~np.isnan(labels)
    test_idx = np.flatnonzero(test_mask)
    if len(test_idx) == 0:
        raise ValueError(f"no test samples between {ts_s.date()} and {ts_e.date()}")
    test_dates = pd.to_datetime(df["date"].values[test_idx])
    true_labels = labels[test_idx]
    pre_m, sim_m, real_m = _dynamic_report_masks(test_dates)
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
    lgbm_grid = cfg.get("lgbm_grid_override")
    if lgbm_grid is None:
        lgbm_grid = build_lgbm_grid(
            cfg["lgbm_windows"], cfg["lgbm_leaves"],
            cfg["lgbm_min_child"], cfg["lgbm_alpha"],
            cfg["lgbm_lambda"], cfg["lgbm_split"],
            slow_path=lgbm_slow_path)
    else:
        lgbm_grid = [dict(item) for item in lgbm_grid]

    t1 = time.time()
    phase_a_cache = cfg.get("phase_a_cache")
    if phase_a_cache is not None:
        print(f"\n  Phase A: Reusing cached LGBM configs for {len(test_idx)} test rows...")
        results = _slice_phase_a_cache(phase_a_cache, test_dates)
    else:
        predict_feat_overrides = {
            int(index): np.asarray(row, dtype=np.float64)[selected_feats]
            for index, row in cfg.get("predict_feat_overrides", {}).items()
        }
        _init_worker(len(df), feat_selected, labels, close, fallback,
                     test_idx, horizon, purge_gap, seeds,
                     predict_feat_overrides, cfg.get("model_memo"),
                     df["date"].dt.strftime("%Y-%m-%d").to_numpy(),
                     [prepared["feature_names"][index] for index in selected_feats])
        if cfg.get("model_memo") is not None:
            keys = [json.dumps(item, sort_keys=True) for item in lgbm_grid]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate configs cannot share model ownership")
        print(f"\n  Phase A: Training {len(lgbm_grid)} LGBM configs "
              f"with {n_workers} workers ({len(seeds)} seeds)...")
        worker_count = min(8, max(1, int(n_workers)))
        if worker_count == 1:
            results = list(map(run_config, lgbm_grid))
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(run_config, lgbm_grid))
    if not results:
        raise RuntimeError("all LGBM configs failed")
    print(f"  Done: {len(results)} configs in "
          f"{(time.time() - t1) / 60:.1f} min")
    if cfg.get("phase_a_only"):
        return {
            "phase_a_cache": _make_phase_a_cache(
                test_dates,
                results,
                pred_key="base_preds",
                prob_key="base_probs",
            ),
            "override_phase_a_cache": _make_phase_a_cache(
                test_dates,
                results,
            ),
            "test_dates": test_dates,
            "true_labels": true_labels,
        }

    # -- Phase B: Signals --
    print(f"\n  Phase B: Computing signals...")
    sig_names = prepared["sig_names"]
    sig_matrix = prepared["sig_matrix"]
    cat_indices = prepared["cat_indices"]
    print(f"  Signals: {len(sig_names)}, "
          f"Categories: {list(cat_indices.keys())}")

    # Signal accuracy (H=1)
    _sa_valid_idx = np.flatnonzero(
        np.isin(labels_h1, [-1.0, 1.0]) & ~np.isnan(close)
    )
    sa = np.full((len(test_idx), len(sig_names)), np.nan, dtype=np.float32)
    for i, idx in enumerate(test_idx):
        end = int(np.searchsorted(_sa_valid_idx, idx, side="left"))
        start = max(0, end - ew)
        if end - start < 40:
            continue
        cands = _sa_valid_idx[start:end]
        # Vectorised accuracy: (sig == label) mean over candidates
        sa[i] = (sig_matrix[cands] == labels_h1[cands][:, None]).mean(
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

    spd: list = [None] * len(test_idx)
    spd_sa: list = [None] * len(test_idx)
    for period in sorted(periods.unique()):
        pm = periods == period
        pi = np.where(pm)[0]
        if len(pi) == 0:
            continue
        before = np.where(periods < period)[0]
        if len(before) > lb:
            before = before[-lb:]
        avg_sa = (np.nanmean(sa[before], axis=0)
                  if len(before) >= 5 else sa[pi[0]])
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

    return {
        "vs_full": vs_full,
        "true_labels": true_labels,
        "test_dates": test_dates,
    }


def apply_consensus(signs: dict[str, np.ndarray], baselines: list[str], k_agree: int, n_rows: int) -> np.ndarray:
    """对 baseline 方向做 k-of-n 共识；不足 k 个同向则输出 0。"""
    votes = np.column_stack([signs[name] for name in baselines])
    up = (votes > 0).sum(axis=1)
    down = (votes < 0).sum(axis=1)
    pred = np.zeros(n_rows, dtype=np.int32)
    pred[up >= k_agree] = 1
    pred[down >= k_agree] = -1
    return pred


def apply_streak_break(pred: np.ndarray, fallback_signs: np.ndarray, streak_k: int) -> np.ndarray:
    """连续同向超过 streak_k 后，用 fallback baseline 方向替换。"""
    out = pred.astype(np.int32).copy()
    streak = 0
    last_dir = 0
    for index, current in enumerate(pred.astype(np.int32)):
        if current != 0 and current == last_dir:
            streak += 1
        elif current != 0:
            streak = 1
            last_dir = int(current)
        if streak > streak_k and fallback_signs[index] != 0:
            out[index] = int(fallback_signs[index])
            if fallback_signs[index] != last_dir:
                streak = 0
    return out


def build_prediction(signs: dict[str, np.ndarray], n_rows: int, cfg: dict[str, Any] | None = None) -> np.ndarray:
    """构建 10Y_01 最终方向。"""
    model_cfg = PROD_CONFIG if cfg is None else cfg
    pred = apply_consensus(
        signs,
        list(model_cfg["baselines"]),
        int(model_cfg["k_agree"]),
        n_rows,
    )
    return apply_streak_break(pred, signs[str(model_cfg["fallback"])], int(model_cfg["streak_K"]))


def align_cross_tenor(
    source_values: np.ndarray,
    source_dates: pd.DatetimeIndex,
    reference_dates: pd.DatetimeIndex,
    n_reference: int,
) -> np.ndarray:
    """按日期把跨期限 baseline 对齐到参考 baseline。"""
    source_index = {str(day.date()): idx for idx, day in enumerate(pd.to_datetime(source_dates))}
    aligned = np.zeros(n_reference, dtype=np.float64)
    for ref_idx, day in enumerate(pd.to_datetime(reference_dates)):
        source_idx = source_index.get(str(day.date()))
        if source_idx is not None:
            aligned[ref_idx] = float(source_values[source_idx])
    return aligned


def run_10y01_for_feature_window(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    feature_date: str | None = None,
    test_ranges: tuple[tuple[str, str], ...],
    current_start: str,
    current_end: str,
    require_labels: bool,
    n_workers: int = 10,
    phase_a_caches: Mapping[str, Any],
    prepared_by_baseline: Mapping[str, Any],
    include_full_context: bool = False,
) -> pd.DataFrame:
    """运行 10Y_01 PIT 窗口，返回 current window 内逐 feature_date 明细。"""
    baseline_contexts: dict[str, dict[str, Any]] = {}
    for baseline in required_baselines():
        phase_a_cache = phase_a_caches.get(str(baseline)) if phase_a_caches is not None else None
        ctx = run_prediction(
            model_config(
                str(baseline),
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                test_start=test_ranges[0][0],
                test_end=test_ranges[-1][1],
                test_ranges=test_ranges,
                require_labels=require_labels,
                emit_report=False,
                prepared_inputs=prepared_by_baseline[str(baseline)],
                n_workers=n_workers,
                **({"phase_a_cache": phase_a_cache} if phase_a_cache is not None else {}),
            )
        )
        if not isinstance(ctx, dict) or len(ctx.get("test_dates", [])) == 0:
            raise RuntimeError(f"10Y_01 baseline {baseline} produced no context rows")
        baseline_contexts[str(baseline)] = ctx

    ref_name = str(PROD_CONFIG["baselines"][0])
    ref_ctx = baseline_contexts[ref_name]
    ref_dates = pd.to_datetime(ref_ctx["test_dates"])
    n_rows = len(ref_dates)
    signs: dict[str, np.ndarray] = {}
    baseline_scores: dict[str, np.ndarray] = {}
    for baseline, ctx in baseline_contexts.items():
        values = np.asarray(ctx["vs_full"], dtype=np.float64)
        dates = pd.to_datetime(ctx["test_dates"])
        if len(values) != n_rows or not pd.Index(dates).equals(pd.Index(ref_dates)):
            values = align_cross_tenor(values, dates, ref_dates, n_rows)
        baseline_scores[baseline] = values
        signs[baseline] = np.sign(values).astype(np.int32)

    final = build_prediction(signs, n_rows)
    ref_date_strings = pd.Series(ref_dates.strftime("%Y-%m-%d"))
    current_mask = pd.Series(True, index=ref_date_strings.index) if include_full_context else (ref_date_strings >= current_start) & (ref_date_strings <= current_end)
    selected_indices = np.flatnonzero(current_mask.to_numpy())
    result = pd.DataFrame(
        {
            "anchor_date": ref_date_strings[current_mask].to_list(),
            "true_label": _labels_for_detail(np.asarray(ref_ctx["true_labels"])[selected_indices]),
        }
    )
    result["prediction"] = final[selected_indices].astype(int)
    result["confidence"] = np.abs(result["prediction"].to_numpy(dtype=int)).astype(float)
    result["vote_score"] = np.mean(
        np.column_stack([baseline_scores[name][selected_indices] for name in PROD_CONFIG["baselines"]]),
        axis=1,
    ).astype(float)
    result["baseline_signs"] = [
        {name: int(signs[name][idx]) for name in required_baselines()}
        for idx in selected_indices
    ]
    result["baseline_scores"] = [
        {name: float(baseline_scores[name][idx]) for name in required_baselines()}
        for idx in selected_indices
    ]
    result["model_version"] = MODEL_VERSION
    result["source_model_id"] = SOURCE_MODEL_ID
    return result.reset_index(drop=True)


# ============================================================================
# BLACKBOX V2 TRANSPORT
# ============================================================================
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
    """校验精确的 Blackbox V2 Request 1.0。"""
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
    """读取单条 JSON 或批量 CSV Request。"""
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
    data_dir: Path,
    requests: list[dict[str, str]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """读取并校验 DataBridge 五文件。"""
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
        daily.empty
        or daily["date"].duplicated().any()
        or not daily["date"].is_monotonic_increasing
        or calendar["rdate"].duplicated().any()
        or not calendar["rdate"].is_monotonic_increasing
        or not calendar["week_id"].str.fullmatch(KEY_RE).all()
    ):
        raise ContractError("daily/calendar key is invalid")
    for frame, columns in (
        (daily, REQUIRED_DAILY),
        (weekly, REQUIRED_WEEKLY),
        (monthly, REQUIRED_MONTHLY),
    ):
        for column in columns:
            numeric = pd.to_numeric(frame[column], errors="coerce")
            if (frame[column].notna() & numeric.isna()).any() or np.isinf(numeric).any():
                raise ContractError(f"invalid consumed numeric values: {column}")
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


def _train_phase_a(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str],
    prepared_inputs: Mapping[str, Any],
    test_range: tuple[str, str],
    n_workers: int,
    baseline: str,
    grid: list[dict[str, Any]] | None = None,
    predict_feat_overrides: Mapping[int, np.ndarray] | None = None,
    model_memo: dict | None = None,
) -> dict[str, Any]:
    dates = prepared_inputs["df"]["date"]
    eligible = (
        (dates >= pd.Timestamp(test_range[0])).to_numpy()
        & (dates <= pd.Timestamp(test_range[1])).to_numpy()
        & ~np.isnan(prepared_inputs["close"])
    )
    if not eligible.any():
        # 原合并 PIT 窗口允许某一子窗口没有该期限的有效行。
        cfg = BASELINE_CONFIGS[baseline]
        empty_grid = grid if grid is not None else build_lgbm_grid(
            cfg["lgbm_windows"], cfg["lgbm_leaves"],
            cfg["lgbm_min_child"], cfg["lgbm_alpha"],
            cfg["lgbm_lambda"], cfg["lgbm_split"],
            slow_path=cfg["lgbm_slow_path"],
        )
        empty_cache = {
            "test_dates": [],
            "results": [
                {
                    "config": dict(config),
                    "preds": np.empty(0, dtype=np.int32),
                    "probs": np.empty(0, dtype=np.float64),
                }
                for config in empty_grid
            ],
        }
        return {
            "phase_a_cache": empty_cache,
            "override_phase_a_cache": empty_cache,
            "test_dates": pd.DatetimeIndex([]),
            "true_labels": np.empty(0, dtype=np.float64),
        }
    overrides: dict[str, Any] = {}
    if grid is not None:
        overrides["lgbm_grid_override"] = grid
    if predict_feat_overrides:
        overrides["predict_feat_overrides"] = predict_feat_overrides
    try:
        with redirect_stdout(sys.stderr):
            result = run_prediction(model_config(
                baseline,
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                test_start=test_range[0],
                test_end=test_range[1],
                test_ranges=(test_range,),
                require_labels=False,
                emit_report=False,
                phase_a_only=True,
                n_workers=n_workers,
                prepared_inputs=prepared_inputs,
                model_memo=model_memo,
                **overrides,
            ))
    finally:
        # 模型只由当前生成调用的family局部容器持有，不能留在worker全局。
        _G.pop("model_memo", None)
    if not isinstance(result, Mapping) or "phase_a_cache" not in result:
        raise RuntimeError("Phase A did not return an in-process cache")
    return dict(result)


def _digest_frame(frame: pd.DataFrame) -> str:
    """试点按固定列序验证已见源前缀，不保存源数据副本。"""
    digest = hashlib.sha256(json.dumps(list(frame.columns), ensure_ascii=True).encode())
    for column in frame:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series.dtype):
            values = np.array(series, dtype="<f8", copy=True)
            values[np.isnan(values)] = np.nan
            payload = values.tobytes()
        else:
            payload = json.dumps(series.astype(str).tolist(), ensure_ascii=True).encode()
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    return digest.hexdigest()

def _trial_identity() -> dict[str, Any]:
    """绑定本草稿精确字节与数值环境，状态不跨方案复用。"""
    import lightgbm
    numba_version = None
    if _HAS_NUMBA:
        import numba
        numba_version = numba.__version__
    return {"schema": "10y01-full-oos-private-1", "code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "metadata": hashlib.sha256(Path(__file__).with_suffix('.json').read_bytes()).hexdigest(),
            "machine": platform.machine(), "numba": numba_version,
            "bottleneck": bn.__version__ if _HAS_BN else None,
            "python": sys.version, "numpy": np.__version__, "pandas": pd.__version__,
            "lightgbm": lightgbm.__version__}

def _payload_digest(*arrays: np.ndarray) -> str:
    """绑定试点数值与摘要数组，拒绝损坏但仍可解析的快照。"""
    digest = hashlib.sha256()
    for value in arrays:
        digest.update(json.dumps([value.dtype.str, value.shape]).encode())
        digest.update(value.tobytes())
    return digest.hexdigest()

def _remove_staging(path: Path | None, published: bool) -> None:
    """已发布后的临时硬链接清理失败只告警，不谎报有效输出失败。"""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        if not published:
            raise
        logging.warning("published output is complete; staging cleanup needed: %s", path)

def _preflight_trial_outputs(args: argparse.Namespace) -> None:
    """私有试点也禁止输出覆盖输入或两类输出互相覆盖。"""
    outputs = [path for path in (args.output, args.state_output) if path is not None]
    resolved = [path.resolve() for path in outputs]
    protected = [getattr(args, 'request', None), getattr(args, 'requests', None), args.state_input]
    protected = {path.resolve() for path in protected if path is not None}
    if len(set(resolved)) != len(resolved) or any(path in protected for path in resolved):
        raise ContractError("trial input/output paths must be distinct")
    for path, target in zip(outputs, resolved):
        if (path.exists() or path.is_symlink() or not path.parent.is_dir()
                or target.is_relative_to(args.data_dir.resolve())
                or target.is_relative_to(Path(__file__).resolve().parent)):
            raise ContractError("trial output must be a new private file outside input/delivery")
        with tempfile.TemporaryFile(dir=path.parent):
            pass

PHASE_FAMILIES = {
    "ten_y": ("STD", "ACCWT", "DIV"),
    "seven_y": ("V55_7Y",),
}
STATE_ARRAY_FIELDS = ("dates", "features", "preds", "probs")
STATE_MAX_BYTES = 16 * 1024 * 1024


def _inspect_state_archive(source: Any, max_rows: int, config_count: int) -> None:
    """按两族独立数组的形状与未压缩长度约束，先检查再分配。"""
    expected = {"header.npy"} | {
        f"{family}_{field}.npy"
        for family in PHASE_FAMILIES for field in STATE_ARRAY_FIELDS
    }
    max_bytes = min(
        STATE_MAX_BYTES,
        len(PHASE_FAMILIES) * max_rows * (40 + 256 + config_count * 9)
        + 65536 + 16384,
    )
    if os.fstat(source.fileno()).st_size > max_bytes:
        raise ContractError("state exceeds input-derived size bound")
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) != len(expected) or {info.filename for info in infos} != expected:
            raise ContractError("invalid state archive members")
        for info in infos:
            if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1 or info.file_size > max_bytes:
                raise ContractError("state must contain bounded uncompressed arrays")
            with archive.open(info) as handle:
                version = np.lib.format.read_magic(handle)
                if version == (1, 0):
                    shape, fortran, dtype = np.lib.format.read_array_header_1_0(handle)
                elif version == (2, 0):
                    shape, fortran, dtype = np.lib.format.read_array_header_2_0(handle)
                else:
                    raise ContractError("unsupported state array version")
                if dtype.hasobject or fortran:
                    raise ContractError("executable or noncanonical state dtype")
                name = info.filename
                if name == "header.npy":
                    valid = shape == () and dtype.kind == "U" and 0 < dtype.itemsize <= 65536
                elif name.endswith("_dates.npy") or name.endswith("_features.npy"):
                    want = np.dtype("U10" if name.endswith("_dates.npy") else "U64")
                    valid = len(shape) == 1 and 0 < shape[0] <= max_rows and dtype == want
                else:
                    want = np.dtype("int8" if name.endswith("_preds.npy") else "float64")
                    valid = len(shape) == 2 and shape[0] == config_count and 0 < shape[1] <= max_rows and dtype == want
                if not valid:
                    raise ContractError("state shape/dtype exceeds input-derived bound")
                count = 1
                for size in shape:
                    count *= size
                if handle.tell() + count * dtype.itemsize != info.file_size:
                    raise ContractError("state array payload length mismatch")


def _save_trial_state(path: Path, header: dict, states: Mapping[str, Any]) -> None:
    """将本方案两族派生数组写入同一份新状态，禁止覆盖输入或已有状态。"""
    if path.exists():
        raise ContractError("trial state output already exists")
    arrays = {
        f"{family}_{field}": states[family][field]
        for family in PHASE_FAMILIES for field in STATE_ARRAY_FIELDS
    }
    header = header | {"payload_sha256": _payload_digest(*arrays.values())}
    temporary = None
    published = False
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            np.savez(handle, header=np.array(json.dumps(header, sort_keys=True)), **arrays)
            handle.flush()
            os.fsync(handle.fileno())
            if handle.tell() > STATE_MAX_BYTES:
                raise ContractError("state exceeds 16 MiB bound")
        os.link(temporary, path)
        published = True
    finally:
        _remove_staging(temporary, published)


def generate_with_private_state(
    args: argparse.Namespace,
    requests: list[dict[str, str]],
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    monthly: pd.DataFrame,
    calendar: pd.DataFrame,
) -> list[dict[str, Any]]:
    """逐cutoff推进独立10Y/7Y Phase A；全OOS排名与控制器每次重算。"""
    identity = _trial_identity()
    authority = {"catalog": hashlib.sha256((args.data_dir / "factor_catalog.csv").read_bytes()).hexdigest()}
    date_to_week = dict(zip(calendar.rdate.dt.strftime("%Y-%m-%d"), calendar.week_id.astype(str)))
    grids = {
        family: build_lgbm_grid(*[model_config(baselines[0])[key] for key in
                                 ("lgbm_windows", "lgbm_leaves", "lgbm_min_child", "lgbm_alpha", "lgbm_lambda", "lgbm_split")],
                               slow_path=True)
        for family, baselines in PHASE_FAMILIES.items()
    }
    states = {
        family: {
            "dates": np.array([], dtype="U10"),
            "features": np.array([], dtype="U64"),
            "preds": np.empty((len(grid), 0), dtype=np.int8),
            "probs": np.empty((len(grid), 0), dtype=np.float64),
        }
        for family, grid in grids.items()
    }
    header = None
    if args.state_input is not None:
        with args.state_input.open("rb") as source:
            _inspect_state_archive(source, len(daily), len(grids["ten_y"]))
            source.seek(0)
            with np.load(source, allow_pickle=False) as loaded:
                header = json.loads(str(loaded["header"].item()))
                states = {
                    family: {field: loaded[f"{family}_{field}"].copy() for field in STATE_ARRAY_FIELDS}
                    for family in PHASE_FAMILIES
                }
        if set(header) != {"identity", "authority", "cutoff", "input_prefixes", "payload_sha256"}:
            raise ContractError("invalid private state header")
        if header["identity"] != identity or header["authority"] != authority:
            raise ContractError("private state identity mismatch")
        _date(header["cutoff"], "state cutoff")
        if set(header["input_prefixes"]) != {"daily_df", "weekly_df", "monthly_df", "calendar_df"}:
            raise ContractError("missing input prefix proof")
        for name, proof in header["input_prefixes"].items():
            if (set(proof) != {"rows", "sha256"} or type(proof["rows"]) is not int
                    or proof["rows"] < 0 or (name != "monthly_df" and proof["rows"] == 0)
                    or not re.fullmatch(r"[0-9a-f]{64}", proof["sha256"])):
                raise ContractError("invalid input prefix proof")
        arrays = [states[family][field] for family in PHASE_FAMILIES for field in STATE_ARRAY_FIELDS]
        if header["payload_sha256"] != _payload_digest(*arrays):
            raise ContractError("private state payload mismatch")
        for family, state in states.items():
            dates, features, preds, probs = (state[field] for field in STATE_ARRAY_FIELDS)
            if (dates.ndim != 1 or dates.dtype != np.dtype("U10") or not len(dates)
                    or not np.all(dates[1:] > dates[:-1]) or dates[-1] > header["cutoff"]
                    or (family == "ten_y" and dates[-1] != header["cutoff"])
                    or features.ndim != 1 or features.dtype != np.dtype("U64")
                    or len(features) != header["input_prefixes"]["daily_df"]["rows"]
                    or any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in features)
                    or preds.dtype != np.dtype("int8") or probs.dtype != np.dtype("float64")
                    or preds.shape != (len(grids[family]), len(dates)) or probs.shape != preds.shape
                    or not np.isin(preds, [-1, 0, 1]).all() or not np.isfinite(probs).all()
                    or ((probs < 0) | (probs > 1)).any()):
                raise ContractError("invalid private state arrays")
    answers = {}
    by_cutoff: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for request in requests:
        key = (request["feature_date"], request["weekly_cutoff_key"], request["monthly_cutoff_key"])
        by_cutoff.setdefault(key, []).append(request)
    # 仅批量调用可复用上一cutoff模型；单点和既有NPZ状态合同完全不变。
    model_memos = {family: {} if len(by_cutoff) > 1 else None for family in PHASE_FAMILIES}
    for (day, week, month), batch in sorted(by_cutoff.items()):
        started = time.perf_counter()
        if header is not None and day < header["cutoff"]:
            raise ContractError("future state cannot serve historical Request")
        raw = {"daily_df": daily.loc[daily.date <= pd.Timestamp(day)].reset_index(drop=True),
               "weekly_df": weekly.loc[weekly.week_id.astype(str) <= week].reset_index(drop=True),
               "monthly_df": monthly.loc[monthly.month_id.astype(str) <= month].reset_index(drop=True),
               "date_to_week": date_to_week}
        consumed_month = (pd.Timestamp(day).to_period("M") - 1).strftime("%Y%m")
        proof_frames = {
            "daily_df": raw["daily_df"].loc[:, ["date", *sorted(REQUIRED_DAILY)]],
            "weekly_df": raw["weekly_df"].loc[:, ["week_id", *sorted(REQUIRED_WEEKLY)]],
            "monthly_df": raw["monthly_df"].loc[
                raw["monthly_df"].month_id <= consumed_month,
                ["month_id", *sorted(REQUIRED_MONTHLY)]].reset_index(drop=True),
            "calendar_df": calendar.loc[calendar.rdate <= pd.Timestamp(day)].reset_index(drop=True),
        }
        if header is not None:
            for name, proof in header["input_prefixes"].items():
                # 周频修订只影响完整特征；保留标签/日历保护并由下方特征指纹失效后缀。
                if name == "weekly_df":
                    continue
                previous_frame = proof_frames[name]
                if name == "monthly_df":
                    previous_month = (pd.Timestamp(header["cutoff"]).to_period("M") - 1).strftime("%Y%m")
                    previous_frame = previous_frame.loc[previous_frame.month_id <= previous_month]
                    if len(previous_frame) != proof["rows"]:
                        raise ContractError("consumed monthly domain changed; explicit rebuild required")
                if len(previous_frame) < proof["rows"] or _digest_frame(previous_frame.iloc[:proof["rows"]]) != proof["sha256"]:
                    raise ContractError("source prefix changed; explicit rebuild required")
        phase_by_baseline = {}
        prepared_by_baseline = {}
        family_progress = {}
        for family, baselines in PHASE_FAMILIES.items():
            grid = grids[family]
            state = states[family]
            dates, features, preds, probs = (state[field] for field in STATE_ARRAY_FIELDS)
            prepared = prepare_prediction_inputs(model_config(baselines[0], **raw))
            df = prepared["df"]
            matrix = np.array(prepared["feat_all"], dtype="<f8", copy=True)
            matrix[np.isnan(matrix)] = np.nan
            new_features = np.array([hashlib.sha256(row.tobytes()).hexdigest() for row in matrix], dtype="U64")
            idx = np.flatnonzero((df.date >= pd.Timestamp("2024-01-01")) & ~np.isnan(prepared["close"]))
            new_dates = df.date.iloc[idx].dt.strftime("%Y-%m-%d").to_numpy(dtype="U10")
            # 7Y可缺当前行，沿用原跨期限日期对齐；但整个基线无历史时原算法仍失败。
            if not len(new_dates) or (family == "ten_y" and new_dates[-1] != day):
                raise ContractError("no valid full-OOS baseline context")
            keep = len(dates)
            if keep and (len(new_dates) < keep or not np.array_equal(new_dates[:keep], dates)):
                raise ContractError("OOS date prefix changed")
            if len(features):
                if len(new_features) < len(features):
                    raise ContractError("feature prefix shortened")
                changed = np.flatnonzero(features != new_features[:len(features)])
                if len(changed):
                    first_changed = df.date.iloc[int(changed[0])].strftime("%Y-%m-%d")
                    keep = min(keep, int(np.searchsorted(dates, first_changed)))
            pieces_preds, pieces_probs = [preds[:, :keep]], [probs[:, :keep]]
            remaining = new_dates[keep:]
            # 控制Dataset临时内存；保留每个历史OOS点全部265配置，不能用两月PIT并集。
            for month_key in sorted({value[:7] for value in remaining}):
                rows = [str(value) for value in remaining if value.startswith(month_key)]
                context = _train_phase_a(**raw, prepared_inputs=prepared,
                                         test_range=(rows[0], rows[-1]), n_workers=4, baseline=baselines[0],
                                         model_memo=model_memos[family])
                computed = context["phase_a_cache"]
                if computed["test_dates"] != rows or len(computed["results"]) != len(grid):
                    raise ContractError("incomplete Phase A suffix")
                for expected, actual in zip(grid, computed["results"]):
                    if expected != actual["config"]:
                        raise ContractError("Phase A config order changed")
                pieces_preds.append(np.array([item["preds"] for item in computed["results"]], dtype=np.int8))
                pieces_probs.append(np.array([item["probs"] for item in computed["results"]], dtype=np.float64))
            preds, probs = np.concatenate(pieces_preds, axis=1), np.concatenate(pieces_probs, axis=1)
            states[family] = {"dates": new_dates, "features": new_features, "preds": preds, "probs": probs}
            phase = {"test_dates": new_dates.tolist(), "results": [
                {"config": config, "preds": preds[index], "probs": probs[index]}
                for index, config in enumerate(grid)]}
            for baseline in baselines:
                phase_by_baseline[baseline] = phase
                prepared_by_baseline[baseline] = prepared
            family_progress[family] = {"oos_rows": len(new_dates), "reused_rows": keep, "trained_rows": len(remaining)}
        with redirect_stdout(sys.stderr):
            detail = run_10y01_for_feature_window(**raw, test_ranges=(("2024-01-01", day),),
                        current_start=day, current_end=day, require_labels=False, n_workers=4,
                        phase_a_caches=phase_by_baseline, prepared_by_baseline=prepared_by_baseline)
        if len(detail) != 1 or str(detail.iloc[0]["anchor_date"]) != day:
            raise ContractError("missing exact full-OOS result")
        direction = int(detail.iloc[0]["prediction"])
        if direction not in (-1, 0, 1):
            raise ContractError("invalid direction")
        for request in batch:
            answers[request["request_id"]] = {key: request[key] for key in RESULT_FIELDS[:-1]} | {"predicted_direction": direction}
        header = {"identity": identity, "authority": authority, "cutoff": day,
                  "input_prefixes": {name: {"rows": len(proof_frames[name]), "sha256": _digest_frame(proof_frames[name])}
                                     for name in ("daily_df", "weekly_df", "monthly_df", "calendar_df")}}
        print(json.dumps({"trial_cutoff": day, "families": family_progress,
                          "seconds": time.perf_counter() - started}), file=sys.stderr, flush=True)
    if args.state_output is not None:
        _save_trial_state(args.state_output, header, states)
        args._state_created = True
    return [answers[request["request_id"]] for request in requests]


def _atomic_write(path: Path, writer: Any) -> None:
    if not path.parent.is_dir():
        raise ContractError("output parent does not exist")
    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        published = True
    finally:
        _remove_staging(temporary, published)


def emit(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    if args.command == "predict":
        _atomic_write(
            args.output,
            lambda handle: json.dump(
                rows[0],
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return

    def writer(handle: Any) -> None:
        output = csv.DictWriter(
            handle,
            fieldnames=RESULT_FIELDS,
            lineterminator="\n",
        )
        output.writeheader()
        output.writerows(rows)

    _atomic_write(args.output, writer)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="liwei_0616 10Y01 Blackbox V2 successor"
    )
    commands = result.add_subparsers(dest="command", required=True)
    for command, flag in (("predict", "--request"), ("backtest", "--requests")):
        item = commands.add_parser(command)
        item.add_argument(flag, type=Path, required=True)
        item.add_argument("--data-dir", type=Path, required=True)
        item.add_argument("--output", type=Path, required=True)
        item.add_argument("--state-input", type=Path)
        item.add_argument("--state-output", type=Path)
    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    np.random.seed(42)
    args = None
    succeeded = False
    try:
        args = parser().parse_args()
        args._state_created = False
        _preflight_trial_outputs(args)
        requests = read_requests(args)
        rows = generate_with_private_state(
            args, requests,
            *read_snapshot(args.data_dir, requests),
        )
        emit(args, rows)
        succeeded = True
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected execution failure")
        return 1
    finally:
        if not succeeded and args is not None and getattr(args, '_state_created', False):
            args.state_output.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
