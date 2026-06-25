#!/usr/bin/env python3
"""bond_common.py -- V31 shared module for bond yield prediction.

V31 changes from V28:
  - Seasonal VT calibration (去年同期): VT is no longer a fixed parameter
  - For each test month, VT is calibrated using same calendar month last year
  - Asymmetric VT: separate vt_up and vt_dn thresholds
  - Walk-forward adaptive: VT uses all available past data at each point
  - Fallback for early months: use prior test data with HORIZON gap
  - Extended pre period: 2024-01 ~ 2024-12 (was 2024-07 ~ 2024-12 in V28)
  - Config ranking by pre accuracy (anti-overfitting)
  - 49 VT candidates: 7x7 grid of (vt_up, vt_dn)

V28 changes from V25:
  - Three-class prediction: up(+1), down(-1), flat(0)
  - Flat signal when VT>0 and |vs| < VT (no trade)
  - Trade rate constraint: must be >70% in sim and real
  - Accuracy computed only on trade samples (pred != 0)
  - Bad month = trade samples all one direction (flat excluded)
  - Gate: sim_acc>=60% AND real_acc>=60% AND bad<=1 AND trade_rate>70%
  - Monthly report includes 预测平, 出手率 columns

V25 changes from V18:
  - Multi-seed LGBM (averaged probabilities across seeds)
  - Normalized signal average: vs = lgbm_w * ml_base + sig_avg (both in [-1,+1])
  - 3 ensemble modes: standard, diverse, accwt
  - 2 ML modes: binary (+1/-1), prob (continuous [-1,+1])
  - 2 signal modes: equal (simple average), weighted (accuracy-weighted)
  - No balance fix (apply_balance_fix removed)
  - 586 signals across 19 categories (+streak, +momentum, +cross_asset)

All shared functions used by the 9 production prediction scripts
(predict_10y_1.py etc.).  Optimised with:
  - NumPy vectorization wherever possible
  - Numba @njit for hot inner loops (graceful fallback)
  - Optional bottleneck (bn.move_mean / bn.move_std) with pandas fallback

Public API
----------
Data loading:     read_daily, load_monthly, load_weekly_simple
Labels:           make_labels, build_fallback_signal, safe_sign
Features:         build_bond_features, build_mf_features, build_wkmo_features
IC screening:     ic_screen
Signal system:    build_all_signals, classify_signals, select_combo_signals
LGBM:             build_lgbm_grid, choose_threshold, run_config
Evaluation:       pacc, check_balance, apply_balance_fix
Main runner:      run_prediction
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import os
import time
import warnings
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from .data_alignment import align_monthly_previous_month

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
    from numba import njit as _njit, prange as _nb_prange
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
    _nb_prange = range

# ---------------------------------------------------------------------------
# Constants shared across ALL tenors
# ---------------------------------------------------------------------------
HORIZON = 5
PURGE_GAP = 5

# V31: VT candidates for seasonal calibration (asymmetric vt_up, vt_dn)
VT_CANDIDATES = [(u, d) for u in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
                         for d in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]]
_VT_GRID_NP = np.array(VT_CANDIDATES, dtype=np.float64)

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
# DATA PREPARATION
# ============================================================================
def read_daily(path) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass daily_df from shared.input_artifacts")


def load_monthly(path, daily_dates) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass monthly_df from shared.input_artifacts")


def load_weekly_simple(path, daily_dates) -> pd.DataFrame:
    raise ValueError("file-based input is disabled; pass weekly_df from shared.input_artifacts")


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
    else:
        aligned_weekly = _align_weekly_like_legacy(weekly_df, daily_dates, date_to_week)

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
    @_njit(parallel=True, cache=True)
    def _ic_batch_nb(fv, ll, min_count=50):
        """Batch |Pearson IC| for every feature column.  Numba prange parallel."""
        n_rows, n_feat = fv.shape
        ics = np.zeros(n_feat, dtype=np.float64)
        for j in _nb_prange(n_feat):
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


def select_dynamic_template(sig_accs: np.ndarray, cat_indices: dict,
                            budget: int = 6, max_per_cat: int = 2,
                            min_acc: float = 0.45,
                            min_eligible: int = 2) -> dict:
    """V38: Dynamically build combo template by ranking category quality.

    For each category, score = mean accuracy of its top-max_per_cat eligible
    signals.  Allocate slots in two rounds: round-1 gives 1 slot to each top
    category (diversity), round-2 tops up the best categories until budget is
    exhausted.
    """
    cat_scores = {}
    for cat, indices in cat_indices.items():
        if cat == "other":
            continue
        accs = sig_accs[indices]
        elig_accs = accs[accs >= min_acc]
        if len(elig_accs) < min_eligible:
            continue
        top_k = min(max_per_cat, len(elig_accs))
        cat_scores[cat] = float(np.sort(elig_accs)[-top_k:].mean())

    if not cat_scores:
        return {}

    ranked = sorted(cat_scores, key=cat_scores.get, reverse=True)
    template: dict = {}
    remaining = budget

    for cat in ranked:
        if remaining <= 0:
            break
        template[cat] = 1
        remaining -= 1

    for cat in ranked:
        if remaining <= 0:
            break
        if template.get(cat, 0) < max_per_cat:
            add = min(max_per_cat - template[cat], remaining)
            template[cat] += add
            remaining -= add

    return template


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
                    splits, slow_path: bool = False,
                    very_slow_path: bool = False) -> list:
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
    if very_slow_path:
        # V50: Deep-learning path — lr=0.005, n_estimators=600
        vslow = list(itertools.product(
            [w for w in windows if w >= 400],
            [l for l in leaves if l >= 7],
            [20, 40], [0.5], [1.5], [0.60]))
        for g in vslow:
            d = dict(zip(["window", "num_leaves", "min_child_samples",
                          "reg_alpha", "reg_lambda", "split_pct"], g))
            d["learning_rate"] = 0.005
            d["n_estimators"] = 600
            result.append(d)
    return result


# -- Numba-accelerated threshold search ------------------------------------
if _HAS_NUMBA:
    @_njit(cache=True)
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


# -- Global dict for multiprocessing workers --------------------------------
_G: dict = {}


def _init_worker(df_len, feat, labels, close, fallback, test_idx,
                 horizon, purge_gap, seeds=None,
                 subsample=0.85, colsample_bytree=0.90,
                 time_weight_alpha=-1.0, early_stopping=20,
                 period_feats=None, test_day_period=None):
    _G.update(df_len=df_len, feat=feat, labels=labels, close=close,
              fallback=fallback, test_idx=test_idx,
              horizon=horizon, purge_gap=purge_gap,
              seeds=seeds or [42, 314],
              subsample=subsample, colsample_bytree=colsample_bytree,
              time_weight_alpha=time_weight_alpha,
              early_stopping=early_stopping)
    if period_feats is not None:
        _G["period_feats"] = period_feats
        _G["test_day_period"] = test_day_period
    else:
        _G.pop("period_feats", None)
        _G.pop("test_day_period", None)


def run_config(config: dict) -> dict | None:
    """Train one LGBM config across all test days.  Runs inside worker pool.

    V25: Multi-seed LGBM — trains with multiple seeds, averages probabilities.
    Returns both preds and probs for ensemble use.
    """
    try:
        import lightgbm as lgb
        labels = _G["labels"]; test_idx = _G["test_idx"]
        close = _G["close"]; df_len = _G["df_len"]
        _pf = _G.get("period_feats"); _tdp = _G.get("test_day_period")
        feat = _G.get("feat")
        horizon = _G["horizon"]; purge_gap = _G["purge_gap"]
        seeds = _G.get("seeds", [42, 314])
        n_test = len(test_idx)
        preds = np.zeros(n_test, dtype=np.int32)
        probs = np.full(n_test, 0.5, dtype=np.float64)

        # Pre-compute eligible-index arrays
        labels_valid = np.isin(labels, [-1.0, 1.0])
        close_valid = ~np.isnan(close)
        all_idx = np.arange(df_len)

        _feat_cache = {}
        for i, idx in enumerate(test_idx):
            if _pf is not None:
                _p = _tdp[i]
                if _p not in _feat_cache:
                    _feat_cache[_p] = _pf[_p]
                feat = _feat_cache[_p]
            w = config["window"]
            mask = (all_idx < idx - horizon) & labels_valid & close_valid
            elig = np.flatnonzero(mask)
            if len(elig) > w:
                elig = elig[-w:]
            if len(elig) < 120 or len(np.unique(labels[elig])) < 2:
                preds[i] = int(_G["fallback"].iloc[idx])
                probs[i] = 1.0 if preds[i] == 1 else 0.0
                continue
            split = max(80, int(len(elig) * config["split_pct"]))
            purge_end = min(split + purge_gap, len(elig) - 20)
            fit_idx = elig[:split]
            cal_idx = elig[purge_end:]
            if len(cal_idx) < 20:
                preds[i] = int(_G["fallback"].iloc[idx])
                probs[i] = 1.0 if preds[i] == 1 else 0.0
                continue

            # V25: Multi-seed training — average probabilities
            fit_y = (labels[fit_idx] == 1).astype(int)
            n_pos = max(int(fit_y.sum()), 1)
            n_neg = max(len(fit_y) - n_pos, 1)
            w_pos = len(fit_y) / (2.0 * n_pos)
            w_neg = len(fit_y) / (2.0 * n_neg)
            class_w = np.where(fit_y == 1, w_pos, w_neg)
            _tw_alpha = _G.get("time_weight_alpha", -1.0)
            time_w = np.exp(np.linspace(_tw_alpha, 0.0, len(fit_idx)))
            sample_w = class_w * time_w
            cal_y = (labels[cal_idx] == 1).astype(int)

            prob_seeds = []
            cal_probs_first = None
            for seed in seeds:
                lr = config.get("learning_rate", 0.03)
                n_est = config.get("n_estimators", 140)
                model = lgb.LGBMClassifier(
                    objective="binary", metric="binary_logloss",
                    num_leaves=config["num_leaves"], learning_rate=lr,
                    n_estimators=n_est,
                    min_child_samples=config["min_child_samples"],
                    reg_alpha=config["reg_alpha"],
                    reg_lambda=config["reg_lambda"],
                    subsample=_G.get("subsample", 0.85),
                    colsample_bytree=_G.get("colsample_bytree", 0.90),
                    n_jobs=1, verbosity=-1, random_state=seed,
                    force_col_wise=True)
                model.fit(feat[fit_idx], fit_y,
                          sample_weight=sample_w,
                          eval_set=[(feat[cal_idx], cal_y)],
                          callbacks=[lgb.early_stopping(
                              _G.get("early_stopping", 20), verbose=False)])
                p = float(model.predict_proba(feat[[idx]])[:, 1][0])
                prob_seeds.append(p)
                if cal_probs_first is None:
                    cal_probs_first = model.predict_proba(feat[cal_idx])[:, 1]

            prob = float(np.mean(prob_seeds))
            probs[i] = prob
            threshold = choose_threshold(cal_probs_first, labels[cal_idx])
            preds[i] = 1 if prob >= threshold else -1
        return {"config": config, "preds": preds, "probs": probs}
    except Exception:
        return None


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
# JIT HELPERS (used by both production adaptive_w and exploration functions)
# ============================================================================

@_njit(cache=True)
def _classify_asym_jit(vs, hsig, ens, vtu, vtd):
    """Classify with per-element asymmetric VT thresholds."""
    n = len(vs)
    p = np.empty(n, dtype=np.int32)
    for i in range(n):
        if not hsig[i]:
            p[i] = ens[i]
        elif vtu[i] > 0.0 or vtd[i] > 0.0:
            if vs[i] > vtu[i]:
                p[i] = 1
            elif vs[i] < -vtd[i]:
                p[i] = -1
            else:
                p[i] = 0
        else:
            p[i] = 1 if vs[i] >= 0.0 else -1
    return p


_W_GRID_NP = np.array([0.03, 0.05, 0.10, 0.20, 0.30, 0.50, 0.75,
                        1.00, 1.50, 2.00, 3.00], dtype=np.float64)


# ============================================================================
# EXPLORATION FUNCTIONS (extracted to bond_explore.py for production clarity)
# ============================================================================
# V32-V38 exploration functions live in bond_explore.py — only loaded when
# an explore config flag is set. Production predict scripts never import them.



# ============================================================================
# MAIN RUNNER
# ============================================================================
def run_prediction(cfg: dict) -> np.ndarray | tuple[np.ndarray, dict[str, Any]]:
    """Run a single fixed-config prediction.

    Parameters
    ----------
    cfg : dict with keys:
        tenor, close, aux_pairs, lgbm_windows, lgbm_leaves,
        lgbm_min_child, lgbm_alpha, lgbm_lambda, lgbm_split,
        K, combo_name, combo_template, lgbm_w, rebal, ew,
        min_acc, ic_top_self, ic_top_mf, n_workers,
        seeds, ml_mode, sig_mode, ens_mode  (V25 new)
        vt_mode: "seasonal" (V31) or "fixed" (V28 compat)
        vt: fixed VT threshold (only used when vt_mode="fixed")

    Returns
    -------
    np.ndarray of predictions (+1 / -1 / 0) for the test period (V28: 0=flat)
    """
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    tenor = cfg["tenor"]
    close_col = cfg["close"]
    aux_pairs = cfg["aux_pairs"]
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
    # V31: seasonal VT calibration
    vt_mode = cfg.get("vt_mode", "seasonal")  # "seasonal" (V31) or "fixed" (V28)
    lgbm_subsample = cfg.get("lgbm_subsample", 0.85)
    lgbm_colsample = cfg.get("lgbm_colsample", 0.90)
    time_weight_alpha = cfg.get("time_weight_alpha", -1.0)
    early_stopping_rounds = cfg.get("early_stopping", 20)
    if "test_start" not in cfg or "test_end" not in cfg:
        raise ValueError("test_start and test_end are required for PIT prediction")
    test_start = pd.Timestamp(cfg["test_start"])
    test_end = pd.Timestamp(cfg["test_end"])
    test_ranges = cfg.get("test_ranges")
    require_labels = bool(cfg.get("require_labels", True))
    emit_report = bool(cfg.get("emit_report", True))

    t0 = time.time()

    # -- Load data --
    print(f"  Loading data...")
    if "daily_df" not in cfg:
        raise ValueError("daily_df is required; build inputs through shared.input_artifacts before calling core")
    df = _normalize_daily_frame(cfg["daily_df"])
    weekly_raw = _normalize_aux_frame(cfg.get("weekly_df"), "week_id")
    monthly_raw = _normalize_aux_frame(cfg.get("monthly_df"), "month_id")
    wk_df, mo_df = prepare_model_frames(
        df,
        weekly_raw,
        monthly_raw,
        df["date"],
        cfg.get("date_to_week"),
    )
    print(f"  Daily: {len(df)}, Weekly cols: {len(wk_df.columns)}, "
          f"Monthly cols: {len(mo_df.columns)}")

    # -- Labels --
    labels = make_labels(df, close_col, horizon=horizon)
    labels_h1 = make_labels(df, close_col, horizon=1)
    close = df[close_col].values.astype(np.float64)
    fallback = build_fallback_signal(df, close_col)

    # -- Features + IC screening --
    print(f"  Building features...")
    bond_f = build_bond_features(df, close_col, aux_pairs)
    mf_f, mf_feat_cat = build_mf_features(df)
    wkmo_f = build_wkmo_features(wk_df, mo_df)
    for c in wkmo_f.columns:
        mf_feat_cat[c] = "weekly" if c.startswith("wk_") else "monthly"
    self_cols = list(bond_f.columns)
    mf_cols = list(mf_f.columns) + list(wkmo_f.columns)
    n_self = len(self_cols)
    feat_df = pd.concat([bond_f, mf_f, wkmo_f], axis=1)
    feat_all = feat_df.values.astype(np.float64)
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

    # -- Walk-forward IC screening (V39) --
    wf_ic = cfg.get("wf_ic")
    period_feat_matrices = None
    test_day_period = None
    if wf_ic:
        wf_rebal = (wf_ic.get("rebal", "quarterly")
                    if isinstance(wf_ic, dict) else "quarterly")
        wf_cap = (wf_ic.get("cap", 2000)
                  if isinstance(wf_ic, dict) else 2000)
        print(f"\n  Walk-forward IC ({wf_rebal}, cap={wf_cap})...")
        if wf_rebal == "monthly":
            _wf_periods = test_dates.to_period("M")
        elif wf_rebal == "yearly":
            _wf_periods = test_dates.to_period("Y")
        else:
            _wf_periods = test_dates.to_period("Q")
        _wf_unique = sorted(_wf_periods.unique())
        _wf_feat_cols = []
        test_day_period = np.zeros(len(test_idx), dtype=np.int32)
        _valid_ic_mask = np.isin(labels, [-1.0, 1.0]) & ~np.isnan(close)
        _valid_ic_idx = np.flatnonzero(_valid_ic_mask)
        for p_i, period in enumerate(_wf_unique):
            pm = _wf_periods == period
            pi = np.where(pm)[0]
            test_day_period[pi] = p_i
            first_row = test_idx[pi[0]]
            cutoff = first_row - horizon
            end = int(np.searchsorted(_valid_ic_idx, cutoff, side='left'))
            start = max(0, end - wf_cap)
            wf_idx = _valid_ic_idx[start:end]
            if len(wf_idx) >= 100:
                sel_p, _ = ic_screen(
                    feat_all[wf_idx], labels[wf_idx],
                    n_self, mf_feat_cat, mf_cols,
                    top_self=ic_top_self, top_mf=ic_top_mf, min_cats=6)
            else:
                sel_p = selected_feats
            _wf_feat_cols.append(sel_p)
        period_feat_matrices = [
            np.ascontiguousarray(feat_all[:, cols]) for cols in _wf_feat_cols]
        baseline_set = set(selected_feats.tolist())
        for p_i, period in enumerate(_wf_unique):
            cols = _wf_feat_cols[p_i]
            n_mf_p = int((cols >= n_self).sum())
            cur_set = set(cols.tolist())
            n_new = len(cur_set - baseline_set)
            n_drop = len(baseline_set - cur_set)
            print(f"    {period}: {len(cols)} feat "
                  f"({len(cols)-n_mf_p}s+{n_mf_p}m) "
                  f"+{n_new}/-{n_drop} vs baseline")

    print(f"\n{'=' * 80}")
    print(f"  {tenor} -- LGBM weighted ML signal + voting")
    print(f"  H={horizon}, Features: {len(selected_feats)} "
          f"({n_cats} MF categories)"
          + (f" [wf_ic={wf_ic}]" if wf_ic else ""))
    print(f"  Test: {len(test_idx)}, Pre: {pre_m.sum()}, "
          f"Sim: {sim_m.sum()}, Real: {real_m.sum()}")
    print(f"{'=' * 80}")

    # -- Phase A: LGBM grid --
    lgbm_slow_path = cfg.get("lgbm_slow_path", False)
    lgbm_very_slow_path = cfg.get("lgbm_very_slow_path", False)
    lgbm_grid = build_lgbm_grid(
        cfg["lgbm_windows"], cfg["lgbm_leaves"],
        cfg["lgbm_min_child"], cfg["lgbm_alpha"],
        cfg["lgbm_lambda"], cfg["lgbm_split"],
        slow_path=lgbm_slow_path,
        very_slow_path=lgbm_very_slow_path)

    _init_worker(len(df), feat_selected, labels, close, fallback,
                 test_idx, horizon, purge_gap, seeds,
                 lgbm_subsample, lgbm_colsample,
                 time_weight_alpha, early_stopping_rounds,
                 period_feat_matrices, test_day_period)

    t1 = time.time()
    _wf_tag = " (walk-forward IC)" if wf_ic else ""
    print(f"\n  Phase A: Training {len(lgbm_grid)} LGBM configs "
          f"with {n_workers} workers ({len(seeds)} seeds){_wf_tag}...")
    if n_workers <= 1:
        results = [r for r in map(run_config, lgbm_grid) if r is not None]
    else:
        with mp.Pool(
            n_workers,
            initializer=_init_worker,
            initargs=(len(df), feat_selected, labels, close, fallback,
                      test_idx, horizon, purge_gap, seeds,
                      lgbm_subsample, lgbm_colsample,
                      time_weight_alpha, early_stopping_rounds,
                      period_feat_matrices, test_day_period),
        ) as pool:
            results = [r for r in pool.map(run_config, lgbm_grid)
                       if r is not None]
    if not results:
        raise RuntimeError("all LGBM configs failed")
    print(f"  Done: {len(results)} configs in "
          f"{(time.time() - t1) / 60:.1f} min")

    # -- Phase B: Signals --
    print(f"\n  Phase B: Computing signals...")
    sig_df = build_all_signals(df, COL_MAP, tenor)
    sig_names = list(sig_df.columns)
    sig_matrix = sig_df.values.astype(np.int32)
    cat_indices = classify_signals(sig_names, tenor)
    print(f"  Signals: {len(sig_names)}, "
          f"Categories: {list(cat_indices.keys())}")

    # Signal accuracy (H=1) — pre-compute valid index for searchsorted
    _sa_valid_idx = np.flatnonzero(
        np.isin(labels_h1, [-1.0, 1.0]) & ~np.isnan(close))
    sa = np.full((len(test_idx), len(sig_names)), np.nan, dtype=np.float32)
    for i, idx in enumerate(test_idx):
        end = int(np.searchsorted(_sa_valid_idx, idx, side='left'))
        start = max(0, end - ew)
        if end - start < 40:
            continue
        cands = _sa_valid_idx[start:end]
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

    dc = cfg.get("dynamic_combo")
    dc_lookback = dc.get("lookback", "rolling") if dc else "rolling"
    spd: list = [None] * len(test_idx)
    spd_sa: list = [None] * len(test_idx)
    dyn_template_log = {}
    all_periods_sorted = sorted(periods.unique())
    for period in all_periods_sorted:
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
        if dc:
            # Compute category-ranking accuracy based on lookback mode
            if dc_lookback == "seasonal":
                # Same quarter/month last year
                freq_n = 12 if rebal == "monthly" else 4
                target_p = period - freq_n
                seasonal_idx = np.where(periods == target_p)[0]
                if len(seasonal_idx) >= 5:
                    combo_sa = np.nanmean(sa[seasonal_idx], axis=0)
                else:
                    combo_sa = avg_sa
            elif dc_lookback == "blended":
                freq_n = 12 if rebal == "monthly" else 4
                target_p = period - freq_n
                seasonal_idx = np.where(periods == target_p)[0]
                if len(seasonal_idx) >= 5:
                    sa_seasonal = np.nanmean(sa[seasonal_idx], axis=0)
                    combo_sa = 0.5 * avg_sa + 0.5 * sa_seasonal
                else:
                    combo_sa = avg_sa
            else:  # rolling (default)
                combo_sa = avg_sa

            tmpl = select_dynamic_template(
                combo_sa, cat_indices,
                budget=dc.get("budget", 6),
                max_per_cat=dc.get("max_per_cat", 2),
                min_acc=min_acc_val,
                min_eligible=dc.get("min_eligible", 2))
            if not tmpl:
                tmpl = combo_template
            dyn_template_log[str(period)] = tmpl
        else:
            tmpl = combo_template
        sel = select_combo_signals(avg_sa, cat_indices,
                                   tmpl, min_acc_val)
        if len(sel) == 0:
            continue
        for i in pi:
            spd[i] = sel
            spd_sa[i] = avg_sa

    if dyn_template_log:
        print(f"  Dynamic combo (lookback={dc_lookback}) templates per period:")
        for p, t in dyn_template_log.items():
            print(f"    {p}: {t}")

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

    _vt_bal = cfg.get("vt_balance_floor", 0.0)

    if vt_mode == "seasonal":
        # V31: Seasonal VT calibration (去年同期)
        _unique_months = sorted(test_months.unique())
        _month_indices = {m: np.where(test_months == m)[0] for m in _unique_months}
        _seasonal_lookback = {}
        for m in _unique_months:
            same_last_year = m - 12
            if same_last_year in _month_indices:
                lb_idx = _month_indices[same_last_year]
            else:
                m_start = _month_indices[m][0]
                safe_end = max(0, m_start - horizon)
                lb_idx = np.where(np.arange(len(test_idx)) < safe_end)[0]
                if len(lb_idx) > 60:
                    lb_idx = lb_idx[-60:]
            _seasonal_lookback[m] = lb_idx

        ens_i32 = ens.astype(np.int32)
        final = np.zeros(len(vs_full), dtype=np.int32)
        vt_counts = {}
        vt_per_month = {}
        for m in _unique_months:
            mi = _month_indices[m]
            lb = _seasonal_lookback[m]
            best_vt = (0.0, 0.0)
            if len(lb) >= 5:
                best_acc = -1.0
                vs_lb = vs_full[lb]; lab_lb = true_labels[lb]
                hs_lb = has_sig[lb]; ens_lb = ens_i32[lb]
                n_lb = float(len(lb))
                for vtu, vtd in VT_CANDIDATES:
                    if vtu > 0 or vtd > 0:
                        p = np.where(hs_lb & (vs_lb > vtu), 1,
                            np.where(hs_lb & (vs_lb < -vtd), -1,
                                     np.where(hs_lb, 0, ens_lb)))
                    else:
                        p = np.where(hs_lb & (vs_lb >= 0), 1,
                            np.where(hs_lb & (vs_lb < 0), -1, ens_lb))
                    traded = p != 0
                    n_traded = int(traded.sum())
                    tr = n_traded / n_lb
                    if tr < 0.70:
                        continue
                    if _vt_bal > 0 and n_traded >= 5:
                        _n_up = int((p[traded] == 1).sum())
                        if _n_up < n_traded * _vt_bal or _n_up > n_traded * (1.0 - _vt_bal):
                            continue
                    acc = float((p[traded] == lab_lb[traded]).mean()) if n_traded > 0 else 0.0
                    if acc > best_acc:
                        best_acc = acc
                        best_vt = (vtu, vtd)
            vt_counts[best_vt] = vt_counts.get(best_vt, 0) + 1
            vt_per_month[str(m)] = best_vt
            vtu, vtd = best_vt
            vs_m = vs_full[mi]; hs_m = has_sig[mi]; ens_m = ens_i32[mi]
            if vtu > 0 or vtd > 0:
                final[mi] = np.where(hs_m & (vs_m > vtu), 1,
                            np.where(hs_m & (vs_m < -vtd), -1,
                                     np.where(hs_m, 0, ens_m))).astype(np.int32)
            else:
                final[mi] = np.where(hs_m & (vs_m >= 0), 1,
                            np.where(hs_m & (vs_m < 0), -1, ens_m)).astype(np.int32)
        dom_vt = max(vt_counts, key=vt_counts.get) if vt_counts else (0.0, 0.0)
        dom_vtu, dom_vtd = dom_vt

        if cfg.get("adaptive_w"):
            vtu_arr = np.zeros(len(vs_full), dtype=np.float64)
            vtd_arr = np.zeros(len(vs_full), dtype=np.float64)
            for m in _unique_months:
                mi = _month_indices[m]
                vtu_arr[mi], vtd_arr[mi] = vt_per_month[str(m)]
            aw_arr = np.full(len(vs_full), lgbm_w, dtype=np.float64)
            w_per_month_log = {}
            _aw_bounds = cfg.get("adaptive_w_bounds")
            _aw_grid = _W_GRID_NP
            if _aw_bounds:
                _aw_lo, _aw_hi = _aw_bounds
                _aw_grid = _W_GRID_NP[(_W_GRID_NP >= _aw_lo) & (_W_GRID_NP <= _aw_hi)]
            for m in _unique_months:
                mi = _month_indices[m]
                lb = _seasonal_lookback[m]
                if len(lb) < 5:
                    w_per_month_log[str(m)] = lgbm_w
                    continue
                best_w, best_acc = lgbm_w, -1.0
                for w in _aw_grid:
                    vs_test = w * ml_base[lb] + sig_avg[lb]
                    vs_f_t = np.where(has_sig[lb], vs_test, ml_base[lb])
                    p = _classify_asym_jit(vs_f_t, has_sig[lb], ens_i32[lb],
                                            vtu_arr[lb], vtd_arr[lb])
                    traded = int(np.sum(p != 0))
                    if traded < len(lb) * 0.30:
                        continue
                    if _vt_bal > 0 and traded >= 5:
                        _aw_up = int(np.sum(p[p != 0] == 1))
                        if _aw_up < traded * _vt_bal or _aw_up > traded * (1.0 - _vt_bal):
                            continue
                    acc = float(np.sum(p[p != 0] == true_labels[lb][p != 0])) / traded \
                        if traded > 0 else 0.0
                    if acc > best_acc:
                        best_acc, best_w = acc, w
                aw_arr[mi] = best_w
                w_per_month_log[str(m)] = best_w
            vs_aw = aw_arr * ml_base + sig_avg
            vs_full = np.where(has_sig, vs_aw, ml_base)
            for m in _unique_months:
                mi = _month_indices[m]
                vtu, vtd = vt_per_month[str(m)]
                vs_m = vs_full[mi]; hs_m = has_sig[mi]; ens_m = ens_i32[mi]
                if vtu > 0 or vtd > 0:
                    final[mi] = np.where(hs_m & (vs_m > vtu), 1,
                                np.where(hs_m & (vs_m < -vtd), -1,
                                         np.where(hs_m, 0, ens_m))).astype(np.int32)
                else:
                    final[mi] = np.where(hs_m & (vs_m >= 0), 1,
                                np.where(hs_m & (vs_m < 0), -1, ens_m)).astype(np.int32)
            print(f"  Adaptive w: {w_per_month_log}")

        vol_bounds = cfg.get("vol_scale_vt")
        if vol_bounds:
            lo_b, hi_b = vol_bounds
            rets_raw = np.diff(close) / np.maximum(close[:-1], 1e-10)
            vr = np.ones(len(test_idx), dtype=np.float64)
            for i in range(len(test_idx)):
                ix = test_idx[i]
                if ix >= 62:
                    r20 = rets_raw[ix - 21:ix - 1]
                    r60 = rets_raw[ix - 61:ix - 1]
                    if len(r20) == 20 and len(r60) == 60:
                        v20, v60 = float(np.std(r20)), float(np.std(r60))
                        if v60 > 1e-10:
                            vr[i] = v20 / v60
            scale = np.clip(vr, lo_b, hi_b)
            for m in _unique_months:
                mi = _month_indices[m]
                vtu, vtd = vt_per_month[str(m)]
                vtu_s = np.maximum(0.02, vtu * scale[mi])
                vtd_s = np.maximum(0.02, vtd * scale[mi])
                vs_m = vs_full[mi]; hs_m = has_sig[mi]; ens_m = ens_i32[mi]
                for j in range(len(mi)):
                    k = mi[j]
                    if not hs_m[j]:
                        final[k] = ens_m[j]
                    elif vtu_s[j] > 0 or vtd_s[j] > 0:
                        if vs_m[j] > vtu_s[j]:
                            final[k] = 1
                        elif vs_m[j] < -vtd_s[j]:
                            final[k] = -1
                        else:
                            final[k] = 0
                    else:
                        final[k] = 1 if vs_m[j] >= 0 else -1
            print(f"  Vol-scale VT: bounds=({lo_b},{hi_b})")
    else:
        # V28: fixed VT — three-class, flat when VT>0 and |vs| < VT
        final = ens.copy()
        if vt > 0:
            final[has_sig & (vs_full > vt)] = 1
            final[has_sig & (vs_full < -vt)] = -1
            final[has_sig & (np.abs(vs_full) <= vt)] = 0  # V28: flat
        else:
            final[has_sig & (vs_full >= 0)] = 1
            final[has_sig & (vs_full < 0)] = -1
        dom_vtu, dom_vtd = vt, vt
        vt_per_month = {}

    # V25: NO balance fix — predictions are pure

    # -- Report --
    sv, _ = pacc(final, true_labels, sim_m)
    rv, _ = pacc(final, true_labels, real_m)
    pv, _ = pacc(final, true_labels, pre_m)
    s_tr = trade_rate_fn(final, sim_m)
    r_tr = trade_rate_fn(final, real_m)
    p_tr = trade_rate_fn(final, pre_m)
    _, sb = check_balance(final[sim_m], test_dates[sim_m])
    _, rb = check_balance(final[real_m], test_dates[real_m])
    nb = len(sb) + len(rb)
    gate = (sv >= 0.60 and rv >= 0.60 and nb <= 1
            and s_tr > 0.70 and r_tr > 0.70)

    print(f"\n{'=' * 80}")
    if vt_mode == "seasonal":
        print(f"  {tenor} RESULTS  K={K} {combo_name} lgbm_w={lgbm_w} "
              f"VT=seasonal dom=({dom_vtu:.2f},{dom_vtd:.2f}) {rebal} ew={ew} mac={min_acc_val}")
    else:
        print(f"  {tenor} RESULTS  K={K} {combo_name} lgbm_w={lgbm_w} "
              f"VT={vt} {rebal} ew={ew} mac={min_acc_val}")
    print(f"  ens_mode={ens_mode} ml_mode={ml_mode} sig_mode={sig_mode}")
    if dc:
        print(f"  dynamic_combo: budget={dc.get('budget',6)} "
              f"max_per_cat={dc.get('max_per_cat',2)}")
    else:
        print(f"  combo_template={combo_template}")
    print(f"  seeds={seeds}")
    if wf_ic:
        print(f"  wf_ic={wf_ic}")
    print(f"  pre={pv:.1%}  sim={sv:.1%}  real={rv:.1%}  bad={nb}")
    print(f"  pre_trade_rate={p_tr:.1%}  sim_trade_rate={s_tr:.1%}  real_trade_rate={r_tr:.1%}")
    if sb:
        print(f"  sim bad months: {sb}")
    if rb:
        print(f"  real bad months: {rb}")
    print(f"  GATE: {'PASS' if gate else 'FAIL'}")
    if vt_mode == "seasonal" and vt_per_month:
        print(f"  VT per month: {vt_per_month}")
    print(f"  Time: {(time.time() - t0) / 60:.1f} min")
    print(f"{'=' * 80}")

    # -- V31: Monthly detail report (pre+sim+real merged) --
    _print_monthly_detail_v31(final, true_labels, test_dates, pre_m, sim_m,
                              real_m, tenor)

    # -- V38: LGB vs Strategy decomposition --
    _print_lgb_vs_strategy(ens, final, true_labels, test_dates,
                           pre_m, sim_m, real_m, tenor, combo_name)

    # -- V32: Regime filter exploration (optional) --
    _ctx32 = {
        "vs_full": vs_full, "has_sig": has_sig,
        "true_labels": true_labels, "ens": ens.astype(np.int32),
        "close": close, "labels": labels,
        "test_idx": test_idx, "test_dates": test_dates,
        "pre_m": pre_m, "sim_m": sim_m, "real_m": real_m,
        "K": K, "tenor": tenor,
        "months": _unique_months, "month_idx": _month_indices,
        "lookback": _seasonal_lookback,
        "ml_base": ml_base, "sig_avg": sig_avg, "lgbm_w": lgbm_w,
    }
    _explore_flags = [
        "regime_explore", "vt_expand_explore",
        "v33_explore", "v34_explore", "v35_explore",
        "v36_explore", "v37_explore", "v38_explore",
    ]
    if vt_mode == "seasonal" and any(cfg.get(f) for f in _explore_flags):
        from bond_explore import (
            _regime_explore, _vt_expand_explore,
            _v33_explore, _v34_explore, _v35_explore,
            _v36_explore, _v37_explore, _v38_explore,
        )
        _explore_map = {
            "regime_explore": _regime_explore,
            "vt_expand_explore": _vt_expand_explore,
            "v33_explore": _v33_explore, "v34_explore": _v34_explore,
            "v35_explore": _v35_explore, "v36_explore": _v36_explore,
            "v37_explore": _v37_explore, "v38_explore": _v38_explore,
        }
        for flag, func in _explore_map.items():
            if cfg.get(flag):
                func(_ctx32)

    if cfg.get("return_ctx"):
        return final, _ctx32
    return final


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


def _labels_for_detail(values: np.ndarray) -> list[int | None]:
    result: list[int | None] = []
    for value in values:
        result.append(None if pd.isna(value) else int(value))
    return result


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
) -> pd.DataFrame:
    """运行 10Y_01 PIT 窗口，返回 current window 内逐 feature_date 明细。"""
    baseline_contexts: dict[str, dict[str, Any]] = {}
    for baseline in required_baselines():
        _, ctx = run_prediction(
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
                return_ctx=True,
                n_workers=n_workers,
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
    current_mask = (ref_date_strings >= current_start) & (ref_date_strings <= current_end)
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


def _print_monthly_detail_v31(final, true_labels, test_dates, pre_m, sim_m,
                               real_m, tenor):
    """V31: Print merged pre+sim+real monthly table with flat/trade columns."""
    combined_m = pre_m | sim_m | real_m
    preds = final[combined_m]
    labels = true_labels[combined_m]
    dates = test_dates[combined_m]
    months = dates.to_period("M")

    print(f"\n{'=' * 120}")
    print(f"  {tenor} Pre+Sim+Real (T+5) -- V31 seasonal VT monthly report")
    print(f"{'=' * 120}")
    # Build period labels
    pre_months = set(dates[pre_m[combined_m]].to_period("M").unique())
    sim_months = set(dates[sim_m[combined_m]].to_period("M").unique())
    real_months = set(dates[real_m[combined_m]].to_period("M").unique())

    print(f"{'月份':>10s}  {'总样本':>4s}  {'涨样本':>4s}  {'跌样本':>4s}  "
          f"{'平样本':>4s}  {'预测涨':>4s}  {'预测跌':>4s}  {'预测平':>4s}  "
          f"{'出手率':>8s}  {'方向准确率':>16s}  {'当月日频':>8s}  "
          f"{'涨精确率':>16s}  {'涨召回率':>16s}  "
          f"{'跌精确率':>16s}  {'跌召回率':>16s}  {'期':>4s}")

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
        if month in pre_months:
            period = "pre"
        elif month in sim_months:
            period = "sim"
        else:
            period = "real"
        print(f"{str(month):>10s}  {n:4d}  {n_up_true:4d}  {n_dn_true:4d}  "
              f"{n_flat_true:4d}  {n_pred_up:4d}  {n_pred_dn:4d}  {n_pred_flat:4d}  "
              f"{m_trade_rate:7.1%}  "
              f"{acc_s:>16s}  {all_acc:7.2%}  "
              f"{up_prec_s:>16s}  {up_rec_s:>16s}  "
              f"{dn_prec_s:>16s}  {dn_rec_s:>16s}  {period:>4s}")

    print(f"\nSummary:")
    overall_acc = total_trade_correct / max(total_trade_n, 1)
    valid_accs = [a for a in acc_list if not np.isnan(a)]
    mean_acc = float(np.mean(valid_accs)) if valid_accs else 0
    std_acc = float(np.std(valid_accs)) if valid_accs else 0
    best_i = int(np.argmax(valid_accs)) if valid_accs else 0
    worst_i = int(np.argmin(valid_accs)) if valid_accs else 0
    ms = sorted(months.unique())
    overall_tr = total_trade_n / max(total_n, 1)
    print(f"  Overall acc (weighted): {overall_acc:.4f} ({overall_acc:.2%})")
    print(f"  Mean monthly acc: {mean_acc:.4f} ({mean_acc:.2%})")
    if valid_accs:
        print(f"  Best month: {max(valid_accs):.4f} ({max(valid_accs):.2%})"
              f" - {ms[best_i]}")
        print(f"  Worst month: {min(valid_accs):.4f} ({min(valid_accs):.2%})"
              f" - {ms[worst_i]}")
    print(f"  Acc std: {std_acc:.4f}")
    print(f"  Overall trade rate: {overall_tr:.4f} ({overall_tr:.2%})")
    # Overall up/down precision
    all_up_tp = int(((preds == 1) & (labels == 1)).sum())
    all_pred_up = int((preds == 1).sum())
    all_dn_tp = int(((preds == -1) & (labels == -1)).sum())
    all_pred_dn = int((preds == -1).sum())
    print(f"  Up precision: "
          f"{all_up_tp / max(all_pred_up, 1):.4f} "
          f"({all_up_tp / max(all_pred_up, 1):.2%})")
    print(f"  Down precision: "
          f"{all_dn_tp / max(all_pred_dn, 1):.4f} "
          f"({all_dn_tp / max(all_pred_dn, 1):.2%})")
    print(f"{'=' * 120}")


def _print_lgb_vs_strategy(ens, final, true_labels, test_dates,
                            pre_m, sim_m, real_m, tenor, combo_name):
    """V38: Print monthly LGBM vs Strategy accuracy decomposition."""
    combined_m = pre_m | sim_m | real_m
    ens_p = ens[combined_m]
    fin_p = final[combined_m]
    labels = true_labels[combined_m]
    dates = test_dates[combined_m]
    months = dates.to_period("M")

    pre_months = set(dates[pre_m[combined_m]].to_period("M").unique())
    sim_months = set(dates[sim_m[combined_m]].to_period("M").unique())

    print(f"\n{'=' * 80}")
    print(f"  {tenor} LGB vs Strategy ({combo_name}) — monthly decomposition")
    print(f"{'=' * 80}")
    print(f"{'月份':>10s}  {'LGBM':>18s}  {'策略':>18s}  {'Delta':>8s}  {'期':>4s}")

    lgb_total_c, lgb_total_n = 0, 0
    str_total_c, str_total_n = 0, 0

    for month in sorted(months.unique()):
        mm = months == month
        m_ens = ens_p[mm]
        m_fin = fin_p[mm]
        m_lab = labels[mm]
        n = len(m_lab)

        lgb_c = int((m_ens == m_lab).sum())
        lgb_acc = lgb_c / max(n, 1)
        lgb_total_c += lgb_c
        lgb_total_n += n

        trade_m = m_fin != 0
        n_trade = int(trade_m.sum())
        str_c = int((m_fin[trade_m] == m_lab[trade_m]).sum()) if n_trade else 0
        str_acc = str_c / n_trade if n_trade else float("nan")
        str_total_c += str_c
        str_total_n += n_trade

        delta = (str_acc - lgb_acc) if n_trade else float("nan")

        lgb_s = f"{lgb_acc:.1%} ({n}/{n})"
        str_s = f"{str_acc:.1%} ({n_trade}/{n})" if n_trade else "N/A"
        delta_s = f"{delta:+.1%}" if not np.isnan(delta) else "N/A"

        if month in pre_months:
            period = "pre"
        elif month in sim_months:
            period = "sim"
        else:
            period = "real"

        print(f"{str(month):>10s}  {lgb_s:>18s}  {str_s:>18s}  {delta_s:>8s}  {period:>4s}")

    lgb_oa = lgb_total_c / max(lgb_total_n, 1)
    str_oa = str_total_c / max(str_total_n, 1)
    print(f"\n  LGBM overall:     {lgb_oa:.2%} ({lgb_total_c}/{lgb_total_n})")
    print(f"  Strategy overall: {str_oa:.2%} ({str_total_c}/{str_total_n})")
    print(f"  Strategy lift:    {str_oa - lgb_oa:+.2%}")
    print(f"{'=' * 80}")
