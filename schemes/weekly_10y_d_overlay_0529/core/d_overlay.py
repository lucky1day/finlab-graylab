from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import pearsonr
from sklearn.feature_selection import SelectKBest, f_classif, mutual_info_classif
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler, StandardScaler

try:
    import lightgbm as lgb
except ImportError:  # pragma: no cover - forecast_env provides lightgbm for live runs.
    lgb = None

@dataclass(frozen=True)
class FactorSpec:
    dimension: str
    feature_id: str
    kind: str
    columns: tuple[str, ...] = ()
    signs: tuple[float, ...] = ()
    vol_window: int = 20


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("funding_liquidity", "fr007_omo_spread", "column", ("90000001",)),
    FactorSpec("funding_liquidity", "fr001_omo_spread", "column", ("90000016",)),
    FactorSpec("funding_liquidity", "dr007_omo_spread", "column", ("90000002",)),
    FactorSpec("funding_liquidity", "dr001_omo_spread", "column", ("90000003",)),
    FactorSpec("funding_liquidity", "r007_omo_spread", "diff", ("M1004533", "M0041653")),
    FactorSpec("funding_liquidity", "r001_omo_spread", "column", ("90000017",)),
    FactorSpec("funding_liquidity", "cd_omo_spread", "column", ("90000004",)),
    FactorSpec("funding_liquidity", "fr007_swap_spread", "column", ("90000013",)),
    FactorSpec("funding_liquidity", "gc007_dr007_spread", "column", ("90000014",)),
    FactorSpec("market_sentiment", "cdb10y_cgb10y_spread", "diff", ("M1004271", "S0059749")),
    FactorSpec(
        "market_sentiment",
        "aa_plus_corp_1_3y_spread",
        "avg_diff",
        ("N1305001", "S0059744", "N1307001", "S0059746"),
    ),
    FactorSpec(
        "market_sentiment",
        "aa_plus_corp_3_5y_spread",
        "avg_diff",
        ("N1307001", "S0059746", "N1308001", "S0059747"),
    ),
    FactorSpec("market_sentiment", "aaa_corp_1_3y_spread", "mean", ("90000007", "90000008", "90000009")),
    FactorSpec("market_sentiment", "aaa_corp_3_5y_spread", "mean", ("90000009", "90000010")),
    FactorSpec("market_sentiment", "cgb10y_bias20", "bias20", ("S0059749",)),
    FactorSpec("market_sentiment", "cgb10y_rsi60", "rsi60", ("S0059749",)),
    FactorSpec("supply_institution", "local_gov_bond_pressure_proxy", "column", ("CV630001",)),
    FactorSpec("supply_institution", "foreign_institution_custody_proxy", "column", ("M1341115",)),
    FactorSpec(
        "macro_fundamental",
        "growth_inv_vol_asset_combo",
        "signed_inv_vol_index",
        ("M0020188", "CSI26904", "CUSHF01C", "S0031505"),
        (1.0, 1.0, 1.0, 1.0),
        20,
    ),
    FactorSpec(
        "macro_fundamental",
        "inflation_inv_vol_short_combo",
        "signed_inv_vol_index",
        ("S1179664", "S0031525"),
        (-1.0, -1.0),
        20,
    ),
    FactorSpec("macro_fundamental", "growth_proxy_tsf_yoy", "column", ("M1525763",)),
    FactorSpec("macro_fundamental", "growth_proxy_new_rmb_loans", "column", ("M5216731",)),
    FactorSpec("macro_fundamental", "growth_proxy_m2_yoy", "column", ("M0101385",)),
    FactorSpec("macro_fundamental", "inflation_proxy_cpi_yoy", "column", ("M0100612",)),
    FactorSpec("macro_fundamental", "inflation_proxy_ppi_yoy", "column", ("M0101227",)),
    FactorSpec("macro_fundamental", "secondhand_home_area", "column", ("S0203233",)),
    FactorSpec("asset_linkage", "sse_bse_volume_ratio", "column", ("90000020",)),
    FactorSpec("asset_linkage", "small_large_cap_ratio", "ratio", ("N2691645", "M0020209")),
    FactorSpec("asset_linkage", "shcomp_sp500_ratio", "ratio", ("M0020188", "G0001672")),
    FactorSpec("macro_fundamental", "growth_pmi", "column", ("M0117126",)),
    FactorSpec("macro_fundamental", "growth_pmi_mom", "column", ("M0417126",)),
    FactorSpec("macro_fundamental", "growth_pmi_yoy", "column", ("M0717126",)),
    FactorSpec("macro_fundamental", "growth_fin_inst_new_rmb_loans", "column", ("M0109973",)),
    FactorSpec("macro_fundamental", "growth_30city_property_area", "column", ("S2707380",)),
    FactorSpec("macro_fundamental", "growth_30city_property_units", "column", ("S2707379",)),
    FactorSpec("macro_fundamental", "growth_bdi", "column", ("S0031550",)),
    FactorSpec("macro_fundamental", "infl_nanhua_composite", "column", ("S0105896",)),
    FactorSpec("macro_fundamental", "infl_nanhua_industrial", "column", ("S0105897",)),
    FactorSpec("macro_fundamental", "infl_crb_spot", "column", ("S0031505",)),
    FactorSpec("macro_fundamental", "infl_brent_fut", "column", ("S0031525",)),
    FactorSpec("macro_fundamental", "infl_wti_fut", "column", ("M0000005",)),
    FactorSpec("macro_fundamental", "infl_rebar_spot", "column", ("S1179664",)),
    FactorSpec("macro_fundamental", "infl_rebar_price", "column", ("S5707798",)),
    FactorSpec("macro_fundamental", "infl_thermal_coal", "column", ("S1180497",)),
    FactorSpec("macro_fundamental", "infl_copper", "column", ("CUSHF01C",)),
    FactorSpec("macro_fundamental", "infl_aluminum", "column", ("ALSHF01C",)),
)

MODEL_LOGISTIC = "Logistic_weekly"
MODEL_KNN = "KNN_weekly"
MODEL_KNN_LOGISTIC = "KNN_Logistic_ensemble"
MODEL_LGBM_CORE = "Weekly_LGBM_core"
MODEL_LGBM_ALT = "Weekly_LGBM_alt"
MODEL_FRAMEWORK = "Treasury_MultiHorizon_Framework"
OPTIMIZED_COMPOSITE_MODEL = "Optimized_Balanced_Composite"
MODEL_REGIME_ADAPTIVE = "Regime_Adaptive_LossGuard"
MODEL_H1_LOCKED_VOTE = "H1_Locked_Vote_Composite"
MODEL_ROLLING_DEV_SELECTOR = "Rolling_Dev_Selected_Selector"

OPTIMIZED_COMPOSITE_CONFIG: dict[str, dict[str, Any]] = {
    "TB1YWI3C": {
        "sources": (
            (MODEL_KNN_LOGISTIC, 0.50),
            (MODEL_LGBM_CORE, 0.50),
        ),
        "threshold": 0.56,
    },
    "TB5YWI3C": {
        "sources": (
            (MODEL_LOGISTIC, 0.30),
            (MODEL_KNN, 0.35),
            (MODEL_FRAMEWORK, 0.35),
        ),
        "threshold": 0.537,
    },
    "TB0YWI3C": {
        "sources": (
            (MODEL_LOGISTIC, 0.85),
            (MODEL_LGBM_CORE, 0.15),
        ),
        "threshold": 0.558,
    },
}

PRODUCTION_SELECTOR_PROFILE = "selector_2023_2025_dev_locked_weekly0519"
PRODUCTION_SELECTOR_CALIBRATION = {
    "warmup_start_week": 202201,
    "calibration_start_week": 202301,
    "calibration_end_week": 202552,
    "external_test_start_week": 202601,
    "external_test_end_week": 202617,
    "objective": "2023-2025 development accuracy with up/down precision and yearly stability constraints; 2026 holdout stress test",
}

TARGET_FINAL_SELECTOR_CONFIG: dict[str, dict[str, Any]] = {
    "TB1YWI3C": {
        "model_priors": {
            MODEL_ROLLING_DEV_SELECTOR: 80.0,
        },
    },
    "TB3YWI3C": {
        "model_priors": {
            MODEL_ROLLING_DEV_SELECTOR: 80.0,
        },
    },
    "TB5YWI3C": {
        "model_priors": {
            MODEL_ROLLING_DEV_SELECTOR: 80.0,
        },
    },
    "TB7YWI3C": {
        "model_priors": {
            MODEL_ROLLING_DEV_SELECTOR: 80.0,
        },
    },
    "TB0YWI3C": {
        "model_priors": {
            MODEL_ROLLING_DEV_SELECTOR: 80.0,
        },
    },
}

REGIME_ADAPTIVE_CONFIG: dict[str, dict[str, Any]] = {
    "TB1YWI3C": {"source_model": MODEL_LGBM_CORE, "lookback": 6, "accuracy_floor": 0.55},
    "TB5YWI3C": {"source_model": MODEL_KNN_LOGISTIC, "lookback": 16, "accuracy_floor": 0.45},
    "TB0YWI3C": {"source_model": MODEL_KNN, "lookback": 8, "accuracy_floor": 0.40},
}

H1_LOCKED_VOTE_CONFIG: dict[str, dict[str, Any]] = {
    "TB1YWI3C": {
        "sources": ((MODEL_REGIME_ADAPTIVE, 1.0),),
        "note": "Strict H1-selected loss guard; uses labels ending before 202527.",
    },
    "TB5YWI3C": {
        "sources": (
            (MODEL_KNN_LOGISTIC, 0.20),
            (MODEL_FRAMEWORK, 0.40),
            (MODEL_LGBM_CORE, 0.40),
        ),
        "note": "Strict H1-selected weighted direction vote.",
    },
    "TB0YWI3C": {
        "sources": (
            (MODEL_KNN_LOGISTIC, 0.10),
            (MODEL_REGIME_ADAPTIVE, 0.50),
            (MODEL_LGBM_CORE, 0.40),
        ),
        "note": "Strict H1-selected weighted direction vote with 2022-2024 support.",
    },
}

ROLLING_DEV_SELECTOR_SOURCES = (
    MODEL_KNN_LOGISTIC,
    MODEL_KNN,
    MODEL_LOGISTIC,
    MODEL_REGIME_ADAPTIVE,
    MODEL_FRAMEWORK,
    MODEL_LGBM_CORE,
)

ROLLING_DEV_SELECTOR_CONFIG: dict[str, dict[str, Any]] = {
    "TB1YWI3C": {
        "mode": "top1",
        "dynamic_sources": (MODEL_REGIME_ADAPTIVE,),
        "dynamic_rule_sources": (
            {
                "name": "term_spread_10y_1y_contra_4w",
                "kind": "spread_change",
                "col_a": "TB0YWI3C",
                "col_b": "TB1YWI3C",
                "lookback": 4,
                "sign": -1.0,
            },
        ),
        "window": 8,
        "topk": 1,
        "acc_w": 1.0,
        "minp_w": 0.0,
        "avgp_w": 0.0,
        "imb_w": 0.0,
        "min_n": 4,
        "min_n_penalty": 0.12,
        "weight_power": 1.0,
        "development_note": "2024-2025H1 robust switch: Regime vs 10Y-1Y spread reversal for 1Y.",
    },
    "TB3YWI3C": {
        "mode": "vote",
        "window": 8,
        "topk": 3,
        "acc_w": 0.75,
        "minp_w": 0.25,
        "avgp_w": 0.0,
        "imb_w": 0.0,
        "min_n": 4,
        "min_n_penalty": 0.12,
        "weight_power": 1.0,
        "development_note": "Generic rolling top-3 weighted selector for 3Y.",
    },
    "TB5YWI3C": {
        "mode": "fixed_vote",
        "rule_sources": (
            (
                {
                    "name": "five_year_7y_10y_spread_momentum_2w",
                    "kind": "spread_change",
                    "col_a": "TB7YWI3C",
                    "col_b": "TB0YWI3C",
                    "lookback": 2,
                    "sign": 1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "five_year_5y_10y_spread_reversal_4w",
                    "kind": "spread_change",
                    "col_a": "TB5YWI3C",
                    "col_b": "TB0YWI3C",
                    "lookback": 4,
                    "sign": -1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "five_year_1y_momentum_4w",
                    "kind": "momentum",
                    "source_col": "TB1YWI3C",
                    "lookback": 4,
                    "sign": 1.0,
                },
                1.0,
            ),
        ),
        "tie_label": -1,
        "development_note": "2023-2025 development-locked 5Y production vote; 2026 kept as holdout stress test.",
    },
    "TB7YWI3C": {
        "mode": "fixed_vote",
        "rule_sources": (
            (
                {
                    "name": "seven_year_1y_3y_spread_reversal_3w",
                    "kind": "spread_change",
                    "col_a": "TB1YWI3C",
                    "col_b": "TB3YWI3C",
                    "lookback": 3,
                    "sign": -1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "seven_year_1y_5y_spread_momentum_3w",
                    "kind": "spread_change",
                    "col_a": "TB1YWI3C",
                    "col_b": "TB5YWI3C",
                    "lookback": 3,
                    "sign": 1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "seven_year_1y_5y_spread_momentum_6w",
                    "kind": "spread_change",
                    "col_a": "TB1YWI3C",
                    "col_b": "TB5YWI3C",
                    "lookback": 6,
                    "sign": 1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "seven_year_5y_7y_spread_momentum_1w",
                    "kind": "spread_change",
                    "col_a": "TB5YWI3C",
                    "col_b": "TB7YWI3C",
                    "lookback": 1,
                    "sign": 1.0,
                },
                1.0,
            ),
        ),
        "tie_label": -1,
        "development_note": "2024-2025 development-selected independent equal-weight rule vote for 7Y.",
    },
    "TB0YWI3C": {
        "mode": "fixed_vote",
        "rule_sources": (
            (
                {
                    "name": "ten_year_1y_5y_spread_momentum_1w",
                    "kind": "spread_change",
                    "col_a": "TB1YWI3C",
                    "col_b": "TB5YWI3C",
                    "lookback": 1,
                    "sign": 1.0,
                },
                1.0,
            ),
            (
                {
                    "name": "ten_year_7y_10y_spread_momentum_1w",
                    "kind": "spread_change",
                    "col_a": "TB7YWI3C",
                    "col_b": "TB0YWI3C",
                    "lookback": 1,
                    "sign": 1.0,
                },
                1.0,
            ),
        ),
        "confidence_rule_sources": (
            {
                "name": "ten_year_enhancer_1y_momentum_4w",
                "kind": "momentum",
                "source_col": "TB1YWI3C",
                "lookback": 4,
                "sign": 1.0,
            },
            {
                "name": "ten_year_enhancer_1y_momentum_8w",
                "kind": "momentum",
                "source_col": "TB1YWI3C",
                "lookback": 8,
                "sign": 1.0,
            },
            {
                "name": "ten_year_enhancer_5y_momentum_26w",
                "kind": "momentum",
                "source_col": "TB5YWI3C",
                "lookback": 26,
                "sign": 1.0,
            },
            {
                "name": "ten_year_enhancer_1y_10y_ratio_momentum_3w",
                "kind": "ratio_momentum",
                "col_a": "TB1YWI3C",
                "col_b": "TB0YWI3C",
                "lookback": 3,
                "sign": 1.0,
            },
        ),
        "confidence_overlay": {
            "base_confidence_prob": 0.60,
            "agree_confidence_prob": 0.68,
            "strong_agree_confidence_prob": 0.72,
            "disagree_confidence_prob": 0.56,
        },
        "tie_label": -1,
        "development_note": "Production 10Y: old stable spread vote is the final direction; 2024-2025 high-score vote is confidence-only.",
    },
}



def make_labels(df: pd.DataFrame, close_col: str, threshold: float) -> tuple[pd.Series, np.ndarray]:
    close = pd.to_numeric(df[close_col], errors="coerce")
    future_return = close.shift(-1).div(close).sub(1.0)
    labels = np.select([future_return > threshold, future_return < -threshold], [1, -1], default=0).astype(float)
    labels[future_return.isna()] = np.nan
    return future_return, labels



def safe_rate(num: float, den: float, default: float = np.nan) -> float:
    return float(num / den) if den else default


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def target_features(df: pd.DataFrame, close_col: str, short_col: str, mid_col: str) -> pd.DataFrame:
    close = pd.to_numeric(df[close_col], errors="coerce")
    ret = close.pct_change()
    features: dict[str, pd.Series] = {}
    for lag in (1, 2, 3, 4, 5, 8, 13, 26, 52):
        lag_ret = ret.shift(lag - 1)
        features[f"ret_lag{lag}"] = lag_ret
        features[f"sign_lag{lag}"] = np.sign(lag_ret)
    for window in (2, 3, 4, 5, 8, 13, 26, 52, 104):
        summed = ret.rolling(window).sum()
        features[f"mom_sum{window}"] = summed
        features[f"mom_sign{window}"] = np.sign(summed)
        features[f"vol{window}"] = ret.rolling(window).std()
        ma = close.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = close.rolling(window, min_periods=max(2, window // 2)).std()
        features[f"z{window}"] = close.sub(ma).div(sd.replace(0, np.nan))
    for name, col in (("1y", short_col), ("5y", mid_col)):
        if col not in df.columns:
            continue
        spread = close.sub(pd.to_numeric(df[col], errors="coerce"))
        for window in (1, 4, 13, 26, 52):
            roll_window = max(2, window)
            ma = spread.rolling(roll_window, min_periods=1).mean()
            sd = spread.rolling(roll_window, min_periods=1).std()
            features[f"spread_{name}_chg{window}"] = spread.sub(spread.shift(window))
            features[f"spread_{name}_z{window}"] = spread.sub(ma).div(sd.replace(0, np.nan))
    return pd.DataFrame(features, index=df.index).replace([np.inf, -np.inf], np.nan).ffill()


def broad_features(df: pd.DataFrame, base: pd.DataFrame, close_col: str, train_cutoff: int) -> pd.DataFrame:
    train_mask = df["week_id"] < train_cutoff
    extra_cols: list[str] = []
    for col in df.columns:
        if col in {"week_id", "week_date", "model_date", close_col}:
            continue
        values = pd.to_numeric(df[col], errors="coerce")
        if values.loc[train_mask].notna().mean() >= 0.80 and values.loc[train_mask].nunique(dropna=True) > 3:
            extra_cols.append(col)
    return pd.concat([base, df[extra_cols]], axis=1).replace([np.inf, -np.inf], np.nan).ffill()


def rsi(series: pd.Series, window: int) -> pd.Series:
    diff = series.diff()
    gain = diff.clip(lower=0).rolling(window, min_periods=max(2, window // 2)).mean()
    loss = (-diff.clip(upper=0)).rolling(window, min_periods=max(2, window // 2)).mean()
    return 100 - 100 / (1 + gain.div(loss.replace(0, np.nan)))


def build_factor_series(weekly: pd.DataFrame, spec: FactorSpec) -> pd.Series | None:
    missing = [col for col in spec.columns if col not in weekly.columns]
    if missing:
        return None
    if spec.kind == "column":
        return weekly[spec.columns[0]]
    if spec.kind == "diff":
        return weekly[spec.columns[0]].sub(weekly[spec.columns[1]])
    if spec.kind == "ratio":
        return weekly[spec.columns[0]].div(weekly[spec.columns[1]].replace(0, np.nan))
    if spec.kind == "mean":
        return weekly[list(spec.columns)].mean(axis=1)
    if spec.kind == "avg_diff":
        diffs = []
        for left, right in zip(spec.columns[::2], spec.columns[1::2]):
            diffs.append(weekly[left].sub(weekly[right]))
        return pd.concat(diffs, axis=1).mean(axis=1)
    if spec.kind == "bias20":
        series = weekly[spec.columns[0]]
        return series.div(series.rolling(20, min_periods=10).mean()).sub(1.0)
    if spec.kind == "rsi60":
        return rsi(weekly[spec.columns[0]], 60)
    if spec.kind == "signed_inv_vol_index":
        values = weekly[list(spec.columns)].ffill()
        returns = values.pct_change()
        vol = returns.rolling(spec.vol_window, min_periods=max(2, spec.vol_window // 2)).std()
        signs = np.asarray(spec.signs if spec.signs else (1.0,) * len(spec.columns), dtype=float)
        weights = signs / vol.replace(0, np.nan)
        weights = weights.div(weights.abs().sum(axis=1).replace(0, np.nan), axis=0)
        return (returns * weights).sum(axis=1).fillna(0.0).cumsum()
    return None


def build_weekly_factor_frame(weekly: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    factor_values: dict[str, pd.Series] = {}
    dimensions: dict[str, str] = {}
    for spec in FACTOR_SPECS:
        series = build_factor_series(weekly, spec)
        if series is None:
            continue
        factor_values[spec.feature_id] = pd.to_numeric(series, errors="coerce")
        dimensions[spec.feature_id] = spec.dimension
    weekly_factors = pd.DataFrame(factor_values, index=weekly.index)
    return weekly_factors.replace([np.inf, -np.inf], np.nan).ffill(), dimensions


def zscore_rolling(series: pd.Series, window: int) -> pd.Series:
    ma = series.rolling(window, min_periods=max(2, window // 2)).mean()
    sd = series.rolling(window, min_periods=max(2, window // 2)).std()
    return series.sub(ma).div(sd.replace(0, np.nan))


def framework_feature_sets(
    weekly: pd.DataFrame,
    target_x: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    factor_weekly, dims = build_weekly_factor_frame(weekly)
    factor_feature_parts: dict[str, dict[str, pd.Series]] = {"cycle": {}, "medium": {}, "short": {}}
    cycle_dims = {"macro_fundamental", "asset_linkage", "supply_institution"}
    medium_dims = {"funding_liquidity", "market_sentiment", "supply_institution"}
    short_dims = {"funding_liquidity", "market_sentiment", "asset_linkage"}
    for col in factor_weekly.columns:
        dim = dims.get(col)
        series = factor_weekly[col]
        if dim in cycle_dims:
            for window in (13, 26, 52):
                factor_feature_parts["cycle"][f"{col}_chg{window}"] = series.sub(series.shift(window))
                factor_feature_parts["cycle"][f"{col}_z{window}"] = zscore_rolling(series, window)
        if dim in medium_dims:
            for window in (4, 8, 13, 26):
                factor_feature_parts["medium"][f"{col}_chg{window}"] = series.sub(series.shift(window))
                factor_feature_parts["medium"][f"{col}_z{window}"] = zscore_rolling(series, window)
        if dim in short_dims:
            for window in (1, 2, 4):
                factor_feature_parts["short"][f"{col}_chg{window}"] = series.sub(series.shift(window))
                factor_feature_parts["short"][f"{col}_z{window}"] = zscore_rolling(series, max(4, window * 4))
    short_target_cols = [
        col
        for col in target_x.columns
        if any(token in col for token in ("ret_lag", "sign_lag", "mom_sum2", "mom_sum3", "mom_sum4", "z2", "z3", "z4"))
    ]
    factor_feature_parts["short"].update({f"target_{col}": target_x[col] for col in short_target_cols})
    outputs: dict[str, pd.DataFrame] = {}
    for layer, parts in factor_feature_parts.items():
        outputs[layer] = pd.DataFrame(parts, index=weekly.index).replace([np.inf, -np.inf], np.nan).ffill()
    return outputs


def choose_train_mask(
    weekly: pd.DataFrame,
    labels: np.ndarray,
    close: pd.Series,
    current_week: int,
    test_start_week: int,
    freeze_test_training: bool,
) -> np.ndarray:
    train_end_week = test_start_week if freeze_test_training and current_week >= test_start_week else current_week
    next_week_id = weekly["week_id"].shift(-1)
    return (
        (weekly["week_id"] < train_end_week)
        & (next_week_id <= train_end_week)
        & pd.Series(labels).notna()
        & close.notna()
    ).to_numpy()


def sklearn_model_predictions(
    model_name: str,
    weekly: pd.DataFrame,
    x: pd.DataFrame,
    labels: np.ndarray,
    close: pd.Series,
    weeks: list[int],
    test_start_week: int,
    freeze_test_training: bool,
    estimator: str,
    k_best: int,
    params: dict[str, Any],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    week_to_idx = {int(week): idx for idx, week in enumerate(weekly["week_id"].astype(int))}
    for week in weeks:
        idx = week_to_idx[week]
        train_mask = choose_train_mask(weekly, labels, close, week, test_start_week, freeze_test_training)
        if int(train_mask.sum()) < 80 or len(np.unique(labels[train_mask])) < 2:
            continue
        if estimator == "logistic":
            clf = LogisticRegression(
                C=float(params.get("C", 1.0)),
                class_weight=params.get("class_weight"),
                solver="liblinear",
                max_iter=1000,
            )
        elif estimator == "knn":
            clf = KNeighborsClassifier(
                n_neighbors=int(params.get("n_neighbors", 9)),
                weights=str(params.get("weights", "uniform")),
            )
        else:
            raise ValueError(estimator)
        pipe = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("select", SelectKBest(f_classif, k=min(k_best, x.shape[1]))),
                ("model", clf),
            ]
        )
        pipe.fit(x.loc[train_mask], (labels[train_mask] == 1).astype(int))
        prob_up = float(pipe.predict_proba(x.iloc[[idx]])[:, 1][0])
        rows.append({"week_id": week, "model_name": model_name, "prob_up": prob_up, "pred_label": 1 if prob_up >= 0.5 else -1})
    return pd.DataFrame(rows)


def ensemble_predictions(left: pd.DataFrame, right: pd.DataFrame, name: str) -> pd.DataFrame:
    columns = ["week_id", "model_name", "prob_up", "pred_label"]
    if left.empty or right.empty or not {"week_id", "prob_up"}.issubset(left.columns) or not {"week_id", "prob_up"}.issubset(right.columns):
        return pd.DataFrame(columns=columns)
    merged = left[["week_id", "prob_up"]].merge(
        right[["week_id", "prob_up"]],
        on="week_id",
        how="inner",
        suffixes=("_left", "_right"),
    )
    merged["prob_up"] = merged[["prob_up_left", "prob_up_right"]].mean(axis=1)
    merged["pred_label"] = np.where(merged["prob_up"] >= 0.5, 1, -1)
    merged["model_name"] = name
    return merged[columns]


def validation_threshold_score(prob_up: np.ndarray, labels: np.ndarray, threshold: float) -> tuple[float, dict[str, float]]:
    pred = np.where(prob_up >= threshold, 1, -1)
    correct = float(np.mean(pred == labels))
    pred_up = pred == 1
    pred_down = pred == -1
    actual_up = labels == 1
    actual_down = labels == -1
    up_precision = shrunk_rate(int((pred_up & actual_up).sum()), int(pred_up.sum()))
    down_precision = shrunk_rate(int((pred_down & actual_down).sum()), int(pred_down.sum()))
    up_recall = shrunk_rate(int((pred_up & actual_up).sum()), int(actual_up.sum()))
    down_recall = shrunk_rate(int((pred_down & actual_down).sum()), int(actual_down.sum()))
    balance_penalty = abs(float(pred_up.mean()) - 0.5) * 0.04
    score = (
        0.55 * correct
        + 0.20 * min(up_precision, down_precision)
        + 0.15 * min(up_recall, down_recall)
        + 0.10 * (up_precision + down_precision) / 2
        - balance_penalty
    )
    metrics = {
        "validation_accuracy": correct,
        "validation_up_precision": up_precision,
        "validation_down_precision": down_precision,
        "validation_up_recall": up_recall,
        "validation_down_recall": down_recall,
        "validation_pred_up": float(pred_up.sum()),
        "validation_pred_down": float(pred_down.sum()),
        "validation_score": float(score),
    }
    return float(score), metrics


@lru_cache(maxsize=64)
def generate_weight_grid(n_sources: int, step: float) -> tuple[tuple[float, ...], ...]:
    units = max(1, int(round(1.0 / step)))
    weights: list[tuple[float, ...]] = []

    def build(prefix: list[int], remaining: int, slots_left: int) -> None:
        if slots_left == 1:
            weights.append(tuple((prefix + [remaining])[i] / units for i in range(n_sources)))
            return
        for value in range(remaining + 1):
            build(prefix + [value], remaining - value, slots_left - 1)

    build([], units, n_sources)
    return tuple(weights)


def choose_dynamic_composite_params(
    history: pd.DataFrame,
    source_names: tuple[str, ...],
    default_weights: tuple[float, ...],
    default_threshold: float,
    threshold_min: float,
    threshold_max: float,
    threshold_step: float,
    weight_step: float,
    weight_prior_strength: float,
    threshold_prior_strength: float,
    up_precision_floor: float,
    min_validation_up_calls: int,
    validation_window: int,
    min_validation_samples: int,
) -> tuple[tuple[float, ...], float, dict[str, float], str]:
    threshold_min = float(threshold_min)
    threshold_max = float(threshold_max)
    valid = history[
        history["actual_label"].isin([-1, 1])
        & history[list(source_names)].notna().all(axis=1)
    ].sort_values("week_id")
    if validation_window > 0:
        valid = valid.tail(validation_window)
    if len(valid) < min_validation_samples or valid["actual_label"].nunique() < 2:
        threshold = float(np.clip(default_threshold, threshold_min, threshold_max))
        return default_weights, threshold, {
            "validation_n": float(len(valid)),
            "validation_score": np.nan,
            "validation_accuracy": np.nan,
            "validation_up_precision": np.nan,
            "validation_down_precision": np.nan,
            "validation_up_recall": np.nan,
            "validation_down_recall": np.nan,
            "validation_pred_up": np.nan,
            "validation_pred_down": np.nan,
        }, "optimized_composite_insufficient_validation"

    source_prob = valid[list(source_names)].to_numpy(dtype=float)
    label = valid["actual_label"].to_numpy(dtype=int)
    threshold_candidates = np.arange(threshold_min, threshold_max + threshold_step / 2, threshold_step)
    weight_candidates = list(generate_weight_grid(len(source_names), weight_step))
    if default_weights not in weight_candidates:
        weight_candidates.append(default_weights)

    best_weights = default_weights
    best_threshold = float(np.clip(0.5, threshold_min, threshold_max))
    best_score_tuple = (-np.inf, -np.inf, -np.inf)
    best_metrics: dict[str, float] = {}
    actual_up = label == 1
    actual_down = label == -1
    actual_up_total = int(actual_up.sum())
    actual_down_total = int(actual_down.sum())
    for weights in weight_candidates:
        composite_prob = source_prob @ np.asarray(weights, dtype=float)
        pred_up = composite_prob[:, None] >= threshold_candidates[None, :]
        pred_down = ~pred_up
        up_correct = (pred_up & actual_up[:, None]).sum(axis=0).astype(float)
        down_correct = (pred_down & actual_down[:, None]).sum(axis=0).astype(float)
        pred_up_total = pred_up.sum(axis=0).astype(float)
        pred_down_total = pred_down.sum(axis=0).astype(float)
        correct = (up_correct + down_correct) / len(valid)
        up_precision = (up_correct + 2.0) / (pred_up_total + 4.0)
        down_precision = (down_correct + 2.0) / (pred_down_total + 4.0)
        up_recall = (up_correct + 2.0) / (actual_up_total + 4.0)
        down_recall = (down_correct + 2.0) / (actual_down_total + 4.0)
        balance_penalty = np.abs(pred_up_total / len(valid) - 0.5) * 0.04
        score = (
            0.55 * correct
            + 0.20 * np.minimum(up_precision, down_precision)
            + 0.15 * np.minimum(up_recall, down_recall)
            + 0.10 * (up_precision + down_precision) / 2
            - balance_penalty
        )
        score -= weight_prior_strength * float(np.abs(np.asarray(weights) - np.asarray(default_weights)).sum())
        score -= threshold_prior_strength * np.abs(threshold_candidates - default_threshold)
        weak_up_mask = (pred_up_total >= min_validation_up_calls) & (up_precision < up_precision_floor)
        score[weak_up_mask] -= 0.35 * (up_precision_floor - up_precision[weak_up_mask] + 0.05)
        sparse_up_mask = pred_up_total < min_validation_up_calls
        score[sparse_up_mask] -= 0.02 * (min_validation_up_calls - pred_up_total[sparse_up_mask])

        min_precision = np.minimum(up_precision, down_precision)
        idx = int(np.lexsort((min_precision, correct, score))[-1])
        metrics = {
            "validation_accuracy": float(correct[idx]),
            "validation_up_precision": float(up_precision[idx]),
            "validation_down_precision": float(down_precision[idx]),
            "validation_up_recall": float(up_recall[idx]),
            "validation_down_recall": float(down_recall[idx]),
            "validation_pred_up": float(pred_up_total[idx]),
            "validation_pred_down": float(pred_down_total[idx]),
            "validation_score": float(score[idx]),
        }
        score_tuple = (
            float(score[idx]),
            metrics["validation_accuracy"],
            min(metrics["validation_up_precision"], metrics["validation_down_precision"]),
        )
        if score_tuple > best_score_tuple:
            best_score_tuple = score_tuple
            best_weights = tuple(float(w) for w in weights)
            best_threshold = float(threshold_candidates[idx])
            best_metrics = metrics
    best_metrics["validation_n"] = float(len(valid))
    return best_weights, best_threshold, best_metrics, "optimized_composite_dynamic_weight_threshold"


def optimized_composite_predictions(
    close_col: str,
    model_frames: dict[str, pd.DataFrame],
    weekly: pd.DataFrame,
    labels: np.ndarray,
    threshold_mode: str,
    validation_window: int,
    min_validation_samples: int,
    threshold_min: float,
    threshold_max: float,
    threshold_step: float,
    weight_step: float,
    weight_prior_strength: float,
    threshold_prior_strength: float,
    up_precision_floor: float,
    min_validation_up_calls: int,
) -> pd.DataFrame:
    config = OPTIMIZED_COMPOSITE_CONFIG.get(close_col)
    columns = [
        "week_id",
        "model_name",
        "prob_up",
        "pred_label",
        "threshold_used",
        "decision",
        "validation_n",
        "validation_score",
        "validation_accuracy",
        "validation_up_precision",
        "validation_down_precision",
        "validation_up_recall",
        "validation_down_recall",
        "validation_pred_up",
        "validation_pred_down",
        "source_spec",
        "weight_spec",
    ]
    if not config:
        return pd.DataFrame(columns=columns)
    sources: tuple[tuple[str, float], ...] = config["sources"]
    source_names = tuple(name for name, _ in sources)
    default_weights = tuple(float(weight) for _, weight in sources)
    fixed_threshold = float(config["threshold"])
    missing = [name for name, _ in sources if name not in model_frames or model_frames[name].empty]
    if missing:
        return pd.DataFrame(columns=columns)

    merged: pd.DataFrame | None = None
    for source_name, weight in sources:
        source = model_frames[source_name][["week_id", "prob_up"]].rename(columns={"prob_up": source_name})
        merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
    if merged is None or merged.empty:
        return pd.DataFrame(columns=columns)

    meta = pd.DataFrame(
        {
            "week_id": weekly["week_id"].astype(int),
            "label_end_week": weekly["week_id"].shift(-1),
            "actual_label": labels,
        }
    )
    source_frame = merged.copy()
    for source_name in source_names:
        source_frame[source_name] = pd.to_numeric(source_frame[source_name], errors="coerce").fillna(0.5)
    out = source_frame.merge(meta, on="week_id", how="left")
    rows: list[dict[str, Any]] = []
    for row in out.sort_values("week_id").itertuples(index=False):
        week = int(row.week_id)
        if threshold_mode == "rolling":
            history = out[
                (out["week_id"] < week)
                & (out["label_end_week"] <= week)
            ]
            weights, threshold, metrics, decision = choose_dynamic_composite_params(
                history,
                source_names,
                default_weights,
                fixed_threshold,
                threshold_min,
                threshold_max,
                threshold_step,
                weight_step,
                weight_prior_strength,
                threshold_prior_strength,
                up_precision_floor,
                min_validation_up_calls,
                validation_window,
                min_validation_samples,
            )
        else:
            weights = default_weights
            threshold = fixed_threshold
            metrics = {
                "validation_n": np.nan,
                "validation_score": np.nan,
                "validation_accuracy": np.nan,
                "validation_up_precision": np.nan,
                "validation_down_precision": np.nan,
                "validation_up_recall": np.nan,
                "validation_down_recall": np.nan,
                "validation_pred_up": np.nan,
                "validation_pred_down": np.nan,
            }
            decision = "optimized_composite_fixed_threshold"
        row_values = row._asdict()
        prob_up = float(sum(float(row_values[source_name]) * weight for source_name, weight in zip(source_names, weights)))
        weight_spec = ";".join(f"{name}:{weight:.4f}" for name, weight in zip(source_names, weights))
        rows.append(
            {
                "week_id": week,
                "model_name": OPTIMIZED_COMPOSITE_MODEL,
                "prob_up": prob_up,
                "pred_label": 1 if prob_up >= threshold else -1,
                "threshold_used": threshold,
                "decision": decision,
                **metrics,
                "source_spec": ";".join(source_names),
                "weight_spec": weight_spec,
            }
        )
    out = pd.DataFrame(rows, columns=columns)
    return out


def regime_adaptive_loss_guard_predictions(
    close_col: str,
    model_frames: dict[str, pd.DataFrame],
    weekly: pd.DataFrame,
    labels: np.ndarray,
) -> pd.DataFrame:
    config = REGIME_ADAPTIVE_CONFIG.get(close_col)
    columns = [
        "week_id",
        "model_name",
        "prob_up",
        "pred_label",
        "source_model",
        "lookback",
        "accuracy_floor",
        "recent_source_accuracy",
        "decision",
    ]
    if not config:
        return pd.DataFrame(columns=columns)
    source_model = str(config["source_model"])
    source = model_frames.get(source_model)
    if source is None or source.empty:
        return pd.DataFrame(columns=columns)
    lookback = int(config["lookback"])
    accuracy_floor = float(config["accuracy_floor"])
    meta = pd.DataFrame(
        {
            "week_id": weekly["week_id"].astype(int),
            "label_end_week": weekly["week_id"].shift(-1),
            "actual_label": labels,
        }
    )
    source_eval = source[["week_id", "prob_up", "pred_label"]].merge(meta, on="week_id", how="left")
    source_eval = source_eval.sort_values("week_id").reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for row in source_eval.itertuples(index=False):
        history = source_eval[
            (source_eval["week_id"] < int(row.week_id))
            & (source_eval["label_end_week"] <= int(row.week_id))
            & source_eval["actual_label"].isin([-1, 1])
        ].tail(lookback)
        if len(history) >= lookback:
            recent_accuracy = float(history["pred_label"].eq(history["actual_label"]).mean())
        else:
            recent_accuracy = np.nan
        flip = bool(pd.notna(recent_accuracy) and recent_accuracy < accuracy_floor)
        source_pred = int(row.pred_label)
        source_prob = float(row.prob_up)
        rows.append(
            {
                "week_id": int(row.week_id),
                "model_name": MODEL_REGIME_ADAPTIVE,
                "prob_up": 1.0 - source_prob if flip else source_prob,
                "pred_label": -source_pred if flip else source_pred,
                "source_model": source_model,
                "lookback": lookback,
                "accuracy_floor": accuracy_floor,
                "recent_source_accuracy": recent_accuracy,
                "decision": "loss_guard_flip" if flip else "source_signal",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def h1_locked_vote_predictions(
    close_col: str,
    model_frames: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    config = H1_LOCKED_VOTE_CONFIG.get(close_col)
    columns = [
        "week_id",
        "model_name",
        "prob_up",
        "pred_label",
        "decision",
        "source_spec",
        "weight_spec",
        "config_note",
    ]
    if not config:
        return pd.DataFrame(columns=columns)
    sources: tuple[tuple[str, float], ...] = config["sources"]
    missing = [name for name, _ in sources if name not in model_frames or model_frames[name].empty]
    if missing:
        return pd.DataFrame(columns=columns)

    merged: pd.DataFrame | None = None
    for source_name, _ in sources:
        source = model_frames[source_name][["week_id", "pred_label"]].rename(columns={"pred_label": source_name})
        merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
    if merged is None or merged.empty:
        return pd.DataFrame(columns=columns)

    weights = np.asarray([float(weight) for _, weight in sources], dtype=float)
    source_names = tuple(name for name, _ in sources)
    score = merged[list(source_names)].to_numpy(dtype=float) @ weights
    weight_sum = float(np.abs(weights).sum()) or 1.0
    out = pd.DataFrame(
        {
            "week_id": merged["week_id"].astype(int),
            "model_name": MODEL_H1_LOCKED_VOTE,
            "prob_up": ((score / weight_sum) + 1.0) / 2.0,
            "pred_label": np.where(score >= 0, 1, -1),
            "decision": "h1_locked_weighted_direction_vote",
            "source_spec": ";".join(source_names),
            "weight_spec": ";".join(f"{name}:{weight:.4f}" for name, weight in sources),
            "config_note": str(config.get("note", "")),
        }
    )
    return out[columns]


def rolling_metric(correct: int, total: int, prior: float = 0.5, prior_n: int = 4) -> float:
    return float((correct + prior * prior_n) / (total + prior_n))


def rule_signal_predictions(rule: dict[str, Any], weekly: pd.DataFrame) -> pd.DataFrame:
    name = str(rule.get("name", "rule_signal"))
    kind = str(rule.get("kind", "momentum"))
    lookback = int(rule.get("lookback", 1))
    sign = float(rule.get("sign", 1.0))
    if lookback <= 0:
        raise ValueError(f"rule lookback must be positive: {name}")
    if kind == "momentum":
        source_col = str(rule["source_col"])
        if source_col not in weekly.columns:
            raise ValueError(f"rule source column not found: {source_col}")
        raw = pd.to_numeric(weekly[source_col], errors="coerce").pct_change(lookback)
    elif kind == "spread_change":
        col_a = str(rule["col_a"])
        col_b = str(rule["col_b"])
        if col_a not in weekly.columns or col_b not in weekly.columns:
            raise ValueError(f"rule spread columns not found: {col_a}, {col_b}")
        spread = pd.to_numeric(weekly[col_a], errors="coerce") - pd.to_numeric(weekly[col_b], errors="coerce")
        raw = spread.diff(lookback)
    elif kind == "ratio_momentum":
        col_a = str(rule["col_a"])
        col_b = str(rule["col_b"])
        if col_a not in weekly.columns or col_b not in weekly.columns:
            raise ValueError(f"rule ratio columns not found: {col_a}, {col_b}")
        ratio = pd.to_numeric(weekly[col_a], errors="coerce") / pd.to_numeric(weekly[col_b], errors="coerce")
        raw = ratio.pct_change(lookback)
    else:
        raise ValueError(f"unsupported rule kind: {kind}")

    signal = np.sign(raw.to_numpy(dtype=float) * sign)
    valid = np.isin(signal, [-1.0, 1.0])
    return pd.DataFrame(
        {
            "week_id": weekly.loc[valid, "week_id"].astype(int).to_numpy(),
            "prob_up": np.where(signal[valid] > 0, 0.55, 0.45),
            "pred_label": signal[valid].astype(int),
            "rule_name": name,
        }
    )


def rolling_dev_selected_predictions(
    close_col: str,
    model_frames: dict[str, pd.DataFrame],
    weekly: pd.DataFrame,
    labels: np.ndarray,
) -> pd.DataFrame:
    config = ROLLING_DEV_SELECTOR_CONFIG.get(close_col)
    columns = [
        "week_id",
        "model_name",
        "prob_up",
        "pred_label",
        "decision",
        "source_spec",
        "score_spec",
        "development_note",
    ]
    if not config:
        return pd.DataFrame(columns=columns)

    configured_sources = tuple(config.get("dynamic_sources", ()))
    source_pool = configured_sources or ROLLING_DEV_SELECTOR_SOURCES
    available_sources = [source for source in source_pool if source in model_frames and not model_frames[source].empty]
    if config.get("mode") == "fixed":
        fixed_model = str(config["fixed_model"])
        if fixed_model not in model_frames or model_frames[fixed_model].empty:
            return pd.DataFrame(columns=columns)
        fixed = model_frames[fixed_model][["week_id", "prob_up", "pred_label"]].copy()
        fixed["model_name"] = MODEL_ROLLING_DEV_SELECTOR
        fixed["decision"] = f"fixed_{fixed_model}"
        fixed["source_spec"] = fixed_model
        fixed["score_spec"] = "development_selected_fixed_model"
        fixed["development_note"] = str(config.get("development_note", ""))
        return fixed[columns]
    if config.get("mode") == "fixed_rule":
        rule = dict(config["rule"])
        rule_frame = rule_signal_predictions(rule, weekly)
        if rule_frame.empty:
            return pd.DataFrame(columns=columns)
        rule_frame["model_name"] = MODEL_ROLLING_DEV_SELECTOR
        rule_frame["decision"] = "development_locked_rule"
        rule_frame["source_spec"] = rule_frame["rule_name"]
        rule_frame["score_spec"] = (
            f"{rule.get('kind')}:{rule.get('source_col', '')}"
            f"{rule.get('col_a', '')}-{rule.get('col_b', '')}:lookback={rule.get('lookback')}:sign={rule.get('sign', 1.0)}"
        )
        rule_frame["development_note"] = str(config.get("development_note", ""))
        return rule_frame[columns]
    if config.get("mode") == "fixed_vote":
        sources: tuple[tuple[str, float], ...] = tuple(config.get("sources", ()))
        rule_sources: tuple[tuple[dict[str, Any], float], ...] = tuple(config.get("rule_sources", ()))
        confidence_rules: tuple[dict[str, Any], ...] = tuple(config.get("confidence_rule_sources", ()))
        source_names = tuple(name for name, _ in sources)
        rule_names = tuple(str(rule.get("name", "rule_signal")) for rule, _ in rule_sources)
        confidence_rule_names = tuple(str(rule.get("name", "confidence_rule_signal")) for rule in confidence_rules)
        missing = [name for name in source_names if name not in model_frames or model_frames[name].empty]
        if (not sources and not rule_sources) or missing:
            return pd.DataFrame(columns=columns)

        merged: pd.DataFrame | None = None
        for source_name in source_names:
            source = model_frames[source_name][["week_id", "prob_up", "pred_label"]].rename(
                columns={"prob_up": f"{source_name}__prob_up", "pred_label": f"{source_name}__pred_label"}
            )
            merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
        for rule, _ in rule_sources:
            rule_name = str(rule.get("name", "rule_signal"))
            source = rule_signal_predictions(dict(rule), weekly)[["week_id", "prob_up", "pred_label"]].rename(
                columns={"prob_up": f"{rule_name}__prob_up", "pred_label": f"{rule_name}__pred_label"}
            )
            merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
        for rule in confidence_rules:
            rule_name = str(rule.get("name", "confidence_rule_signal"))
            source = rule_signal_predictions(dict(rule), weekly)[["week_id", "prob_up", "pred_label"]].rename(
                columns={"prob_up": f"{rule_name}__prob_up", "pred_label": f"{rule_name}__pred_label"}
            )
            merged = source if merged is None else merged.merge(source, on="week_id", how="left")
        if merged is None or merged.empty:
            return pd.DataFrame(columns=columns)

        all_names = source_names + rule_names
        weights = np.asarray(
            [float(weight) for _, weight in sources] + [float(weight) for _, weight in rule_sources],
            dtype=float,
        )
        pred_matrix = merged[[f"{name}__pred_label" for name in all_names]].to_numpy(dtype=float)
        prob_matrix = merged[[f"{name}__prob_up" for name in all_names]].to_numpy(dtype=float)
        vote = pred_matrix @ weights
        weight_sum = float(np.abs(weights).sum()) or 1.0
        tie_label = int(config.get("tie_label", 1))
        base_pred = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label))
        prob_up = np.clip((prob_matrix @ weights) / weight_sum, 0.0, 1.0)
        decision = "development_locked_fixed_vote"
        source_spec = ";".join(all_names)
        score_spec = ";".join(
            [f"{name}:{weight:.4f}" for name, weight in sources]
            + [f"{name}:{weight:.4f}" for name, (_, weight) in zip(rule_names, rule_sources)]
        )
        if confidence_rules:
            overlay = dict(config.get("confidence_overlay", {}))
            enhancer_matrix = merged[[f"{name}__pred_label" for name in confidence_rule_names]].to_numpy(dtype=float)
            enhancer_valid = np.isin(enhancer_matrix, [-1.0, 1.0]).all(axis=1)
            enhancer_vote = np.where(enhancer_valid, np.nansum(enhancer_matrix, axis=1), 0.0)
            enhancer_pred = np.where(enhancer_vote > 0, 1, np.where(enhancer_vote < 0, -1, tie_label))
            base_conf = float(overlay.get("base_confidence_prob", 0.60))
            agree_conf = float(overlay.get("agree_confidence_prob", 0.68))
            strong_agree_conf = float(overlay.get("strong_agree_confidence_prob", agree_conf))
            disagree_conf = float(overlay.get("disagree_confidence_prob", 0.56))
            strong_min_votes = float(overlay.get("strong_min_abs_votes", len(confidence_rule_names)))
            direction_conf = np.full(len(merged), base_conf, dtype=float)
            agrees = enhancer_valid & (enhancer_pred == base_pred)
            strong_agrees = agrees & (np.abs(enhancer_vote) >= strong_min_votes)
            direction_conf[agrees] = agree_conf
            direction_conf[strong_agrees] = strong_agree_conf
            direction_conf[enhancer_valid & ~agrees] = disagree_conf
            prob_up = np.where(base_pred > 0, direction_conf, 1.0 - direction_conf)
            decision = "development_locked_fixed_vote_confidence_overlay"
            source_spec = f"{source_spec}|confidence:{';'.join(confidence_rule_names)}"
            score_spec = f"{score_spec}|confidence_only:{';'.join(confidence_rule_names)}"
        fixed_vote = pd.DataFrame(
            {
                "week_id": merged["week_id"].astype(int),
                "model_name": MODEL_ROLLING_DEV_SELECTOR,
                "prob_up": prob_up,
                "pred_label": base_pred,
                "decision": decision,
                "source_spec": source_spec,
                "score_spec": score_spec,
                "development_note": str(config.get("development_note", "")),
            }
        )
        return fixed_vote[columns]
    if not available_sources:
        return pd.DataFrame(columns=columns)

    merged: pd.DataFrame | None = None
    for source_name in available_sources:
        source = model_frames[source_name][["week_id", "prob_up", "pred_label"]].rename(
            columns={"prob_up": f"{source_name}__prob_up", "pred_label": f"{source_name}__pred_label"}
        )
        merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
    for rule in tuple(config.get("dynamic_rule_sources", ())):
        rule_name = str(rule.get("name", "rule_signal"))
        source = rule_signal_predictions(dict(rule), weekly)[["week_id", "prob_up", "pred_label"]].rename(
            columns={"prob_up": f"{rule_name}__prob_up", "pred_label": f"{rule_name}__pred_label"}
        )
        merged = source if merged is None else merged.merge(source, on="week_id", how="inner")
        available_sources.append(rule_name)
    if merged is None or merged.empty:
        return pd.DataFrame(columns=columns)

    meta = pd.DataFrame(
        {
            "week_id": weekly["week_id"].astype(int),
            "label_end_week": weekly["week_id"].shift(-1),
            "actual_label": labels,
        }
    )
    merged = merged.merge(meta, on="week_id", how="left").sort_values("week_id").reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    window = int(config.get("window", 12))
    topk = int(config.get("topk", 1))
    mode = str(config.get("mode", "top1"))
    for row in merged.itertuples(index=False):
        row_values = row._asdict()
        week = int(row_values["week_id"])
        scored_sources: list[dict[str, Any]] = []
        for source_name in available_sources:
            history = merged[
                (merged["week_id"] < week)
                & (merged["label_end_week"] <= week)
                & merged["actual_label"].isin([-1, 1])
            ].copy()
            if window > 0:
                history = history.tail(window)
            hist_pred = history[f"{source_name}__pred_label"]
            hist_label = history["actual_label"]
            pred_up = hist_pred.eq(1)
            pred_down = hist_pred.eq(-1)
            n = int(len(history))
            acc = rolling_metric(int(hist_pred.eq(hist_label).sum()), n)
            up_precision = rolling_metric(int((pred_up & hist_label.eq(1)).sum()), int(pred_up.sum()))
            down_precision = rolling_metric(int((pred_down & hist_label.eq(-1)).sum()), int(pred_down.sum()))
            hist_up_ratio = float(pred_up.mean()) if n else 0.5
            score = (
                float(config.get("acc_w", 1.0)) * acc
                + float(config.get("minp_w", 0.0)) * min(up_precision, down_precision)
                + float(config.get("avgp_w", 0.0)) * (up_precision + down_precision) / 2
                - float(config.get("imb_w", 0.0)) * abs(hist_up_ratio - 0.5)
            )
            min_n = int(config.get("min_n", 4))
            if n < min_n:
                score -= float(config.get("min_n_penalty", 0.12)) * (min_n - n) / max(1, min_n)
            scored_sources.append(
                {
                    "source": source_name,
                    "score": float(score),
                    "pred_label": int(row_values[f"{source_name}__pred_label"]),
                    "prob_up": float(row_values[f"{source_name}__prob_up"]),
                }
            )
        scored_sources = sorted(scored_sources, key=lambda x: x["score"], reverse=True)
        selected = scored_sources[: max(1, min(topk, len(scored_sources)))]
        if mode == "top1":
            winner = selected[0]
            pred_label = int(winner["pred_label"])
            prob_up = float(winner["prob_up"])
            decision = f"rolling_top1_{winner['source']}"
        else:
            raw_weights = np.asarray([max(0.01, item["score"]) for item in selected], dtype=float)
            weight_power = float(config.get("weight_power", 1.0))
            if weight_power != 1.0:
                raw_weights = raw_weights**weight_power
            vote = float(sum(weight * item["pred_label"] for weight, item in zip(raw_weights, selected)))
            pred_label = 1 if vote >= 0 else -1
            prob_up = float((vote / raw_weights.sum() + 1.0) / 2.0)
            decision = f"rolling_top{len(selected)}_weighted_vote"
        rows.append(
            {
                "week_id": week,
                "model_name": MODEL_ROLLING_DEV_SELECTOR,
                "prob_up": prob_up,
                "pred_label": pred_label,
                "decision": decision,
                "source_spec": ";".join(item["source"] for item in selected),
                "score_spec": ";".join(f"{item['source']}:{item['score']:.4f}" for item in selected),
                "development_note": str(config.get("development_note", "")),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def fit_layer_probability(
    x: pd.DataFrame,
    labels: np.ndarray,
    train_mask: np.ndarray,
    idx: int,
    k_best: int,
    c_value: float,
) -> float:
    if x.shape[1] == 0:
        return 0.5
    train_x = x.loc[train_mask]
    if train_x.notna().any(axis=0).sum() == 0:
        return 0.5
    pipe = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("select", SelectKBest(f_classif, k=min(k_best, x.shape[1]))),
            ("model", LogisticRegression(C=c_value, class_weight="balanced", solver="liblinear", max_iter=1000)),
        ]
    )
    pipe.fit(train_x, (labels[train_mask] == 1).astype(int))
    return float(pipe.predict_proba(x.iloc[[idx]])[:, 1][0])


def framework_predictions(
    weekly: pd.DataFrame,
    layers: dict[str, pd.DataFrame],
    labels: np.ndarray,
    close: pd.Series,
    weeks: list[int],
    test_start_week: int,
    freeze_test_training: bool,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    week_to_idx = {int(week): idx for idx, week in enumerate(weekly["week_id"].astype(int))}
    for week in weeks:
        idx = week_to_idx[week]
        train_mask = choose_train_mask(weekly, labels, close, week, test_start_week, freeze_test_training)
        if int(train_mask.sum()) < 120 or len(np.unique(labels[train_mask])) < 2:
            continue
        cycle = fit_layer_probability(layers["cycle"], labels, train_mask, idx, 16, 0.20)
        medium = fit_layer_probability(layers["medium"], labels, train_mask, idx, 20, 0.30)
        short = fit_layer_probability(layers["short"], labels, train_mask, idx, 12, 0.50)
        prob_up = 0.25 * cycle + 0.35 * medium + 0.40 * short
        rows.append(
            {
                "week_id": week,
                "model_name": MODEL_FRAMEWORK,
                "prob_up": float(prob_up),
                "pred_label": 1 if prob_up >= 0.5 else -1,
                "cycle_prob": cycle,
                "medium_prob": medium,
                "short_prob": short,
            }
        )
    return pd.DataFrame(rows)


def choose_threshold(cal_prob: np.ndarray, cal_labels: np.ndarray) -> float:
    best_score = -np.inf
    best_threshold = 0.5
    for threshold in np.linspace(0.38, 0.62, 49):
        pred = np.where(cal_prob >= threshold, 1, -1)
        score = float((pred == cal_labels).mean())
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def fallback_signal(df: pd.DataFrame, close_col: str, short_col: str, lag: int = 25) -> pd.Series:
    spread_change = pd.to_numeric(df[close_col], errors="coerce").sub(pd.to_numeric(df[short_col], errors="coerce")).diff()
    signal = -np.sign(spread_change.shift(lag))
    return signal.replace(0, -1).fillna(-1).astype(int)


def lgbm_predictions(
    model_name: str,
    weekly: pd.DataFrame,
    features: pd.DataFrame,
    labels: np.ndarray,
    close: pd.Series,
    future_return: pd.Series,
    weeks: list[int],
    test_start_week: int,
    freeze_test_training: bool,
    params: dict[str, Any],
    short_col: str,
    mode: str,
) -> pd.DataFrame:
    if lgb is None:
        return pd.DataFrame(columns=["week_id", "model_name", "prob_up", "pred_label"])
    rows: list[dict[str, Any]] = []
    week_to_idx = {int(week): idx for idx, week in enumerate(weekly["week_id"].astype(int))}
    spread_fallback = fallback_signal(weekly, params.get("close_col", "TB0YWI1C"), short_col)
    close_ret = close.pct_change()
    for week in weeks:
        idx = week_to_idx[week]
        train_mask = choose_train_mask(weekly, labels, close, week, test_start_week, freeze_test_training)
        eligible = np.flatnonzero(train_mask)
        window = int(params.get("window", 104))
        if len(eligible) > window:
            eligible = eligible[-window:]
        if len(eligible) < 40 or len(np.unique(labels[eligible])) < 2:
            prob_up = 0.5
            pred = int(spread_fallback.iloc[idx])
            threshold_used = 0.5
            decision = "cold_fallback"
        else:
            split = max(30, int(len(eligible) * 0.78))
            fit_idx = eligible[:split]
            cal_idx = eligible[split:]
            model = lgb.LGBMClassifier(
                objective="binary",
                metric="binary_logloss",
                num_leaves=int(params.get("num_leaves", 2)),
                learning_rate=float(params.get("learning_rate", 0.03)),
                n_estimators=int(params.get("n_estimators", 160)),
                min_child_samples=int(params.get("min_child_samples", 8)),
                reg_alpha=float(params.get("reg_alpha", 0.5)),
                reg_lambda=float(params.get("reg_lambda", 2.0)),
                subsample=float(params.get("subsample", 0.85)),
                colsample_bytree=float(params.get("colsample_bytree", 0.85)),
                n_jobs=1,
                verbosity=-1,
                random_state=int(params.get("random_state", 42)),
                force_col_wise=True,
            )
            model.fit(
                features.iloc[fit_idx],
                (labels[fit_idx] == 1).astype(int),
                eval_set=[(features.iloc[cal_idx], (labels[cal_idx] == 1).astype(int))],
                callbacks=[lgb.early_stopping(25, verbose=False)],
            )
            prob_up = float(model.predict_proba(features.iloc[[idx]])[:, 1][0])
            cal_prob = model.predict_proba(features.iloc[cal_idx])[:, 1]
            threshold_used = choose_threshold(cal_prob, labels[cal_idx])
            pred = 1 if prob_up >= threshold_used else -1
            decision = "model"
            if mode == "core":
                epsilon = float(params.get("epsilon", 0.14))
                if abs(prob_up - threshold_used) <= epsilon:
                    pred = int(spread_fallback.iloc[idx])
                    decision = "spread_10y_1y_fallback"
            elif mode == "alt_momblend" and abs(prob_up - threshold_used) <= 0.035:
                pred = int(np.sign(close_ret.iloc[idx]) or -1)
                decision = "momentum_blend"
            elif mode == "alt_force_mom" and abs(prob_up - threshold_used) <= 0.08:
                pred = int(np.sign(close_ret.iloc[idx]) or -1)
                decision = "force_momentum"
        rows.append(
            {
                "week_id": week,
                "model_name": model_name,
                "prob_up": prob_up,
                "pred_label": pred,
                "threshold_used": threshold_used,
                "decision": decision,
                "future_return": float(future_return.iloc[idx]) if pd.notna(future_return.iloc[idx]) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def enrich_predictions(predictions: pd.DataFrame, weekly: pd.DataFrame, future_return: pd.Series, labels: np.ndarray) -> pd.DataFrame:
    label_end_week = weekly["week_id"].shift(-1)
    meta = pd.DataFrame(
        {
            "week_id": weekly["week_id"].astype(int),
            "label_end_week": label_end_week,
            "week_date": pd.to_datetime(weekly["week_date"], errors="coerce"),
            "future_return": future_return,
            "actual_label": labels,
        }
    )
    out = predictions.merge(meta, on="week_id", how="left", suffixes=("", "_meta"))
    if "future_return_meta" in out.columns:
        out["future_return"] = out["future_return"].combine_first(out["future_return_meta"])
        out = out.drop(columns=["future_return_meta"])
    out["confidence"] = (out["prob_up"].sub(0.5).abs() * 2).clip(0, 1)
    out["is_correct"] = out["pred_label"].eq(out["actual_label"])
    return out.sort_values(["model_name", "week_id"]).reset_index(drop=True)


def shrunk_rate(correct: int, total: int, prior: float = 0.5, prior_n: int = 4) -> float:
    return float((correct + prior * prior_n) / (total + prior_n))


def ewma_accuracy(history: pd.DataFrame, halflife: float = 4.0) -> float:
    if history.empty:
        return 0.5
    values = history["is_correct"].astype(float).to_numpy()
    ages = np.arange(len(values) - 1, -1, -1)
    weights = np.exp(-np.log(2) * ages / halflife)
    return float(np.dot(values, weights) / weights.sum())


def same_period_last_year_score(history: pd.DataFrame, week_id: int, radius: int = 4) -> tuple[float, int]:
    target_year = int(str(week_id)[:4]) - 1
    target_week = int(str(week_id)[4:])
    hist = history.copy()
    hist["year"] = hist["week_id"].astype(str).str[:4].astype(int)
    hist["week"] = hist["week_id"].astype(str).str[4:].astype(int)
    window = hist[(hist["year"] == target_year) & (hist["week"].sub(target_week).abs() <= radius)]
    if window.empty:
        return 0.5, 0
    correct = int(window["is_correct"].sum())
    total = int(len(window))
    return shrunk_rate(correct, total), total


def stability_score(history: pd.DataFrame) -> float:
    if len(history) < 8:
        return 0.5
    recent = history.tail(16).copy()
    blocks = []
    for start in range(0, len(recent), 4):
        block = recent.iloc[start : start + 4]
        if len(block) >= 3:
            blocks.append(float(block["is_correct"].mean()))
    if not blocks:
        return 0.5
    std = float(np.std(blocks))
    low_streak = 0
    current = 0
    for ok in recent["is_correct"].astype(bool):
        current = 0 if ok else current + 1
        low_streak = max(low_streak, current)
    return float(np.clip(1 - std - 0.06 * max(0, low_streak - 2), 0, 1))


def probability_quality(history: pd.DataFrame) -> float:
    if history.empty:
        return 0.5
    y = (history["actual_label"].eq(1)).astype(float)
    p = history["prob_up"].clip(0.001, 0.999)
    brier = float(np.mean((p - y) ** 2))
    return float(np.clip(1 - brier / 0.35, 0, 1))


def score_one_model(history: pd.DataFrame, week_id: int) -> dict[str, float]:
    recent = history.tail(12)
    recent_dir = ewma_accuracy(recent)
    up_hist = recent[recent["pred_label"].eq(1)]
    down_hist = recent[recent["pred_label"].eq(-1)]
    up_precision = shrunk_rate(int((up_hist["actual_label"] == 1).sum()), int(len(up_hist)))
    down_precision = shrunk_rate(int((down_hist["actual_label"] == -1).sum()), int(len(down_hist)))
    yoy, yoy_n = same_period_last_year_score(history, week_id)
    stable = stability_score(history)
    prob_q = probability_quality(recent)
    signal_imbalance = abs(float(recent["pred_label"].eq(1).mean()) - 0.5) * 2 if len(recent) else 0.0
    score = (
        30 * recent_dir
        + 20 * up_precision
        + 20 * down_precision
        + 15 * yoy
        + 10 * stable
        + 5 * prob_q
    )
    if recent_dir < 0.50:
        score -= 5
    if min(up_precision, down_precision) < 0.48:
        score -= 5
    if signal_imbalance > 0.80:
        score -= 4
    if yoy_n < 3:
        score -= 2
    return {
        "score": float(np.clip(score, 0, 100)),
        "recent_direction_accuracy": recent_dir,
        "recent_up_precision": up_precision,
        "recent_down_precision": down_precision,
        "last_year_same_period_accuracy": yoy,
        "stability": stable,
        "probability_quality": prob_q,
        "signal_imbalance": signal_imbalance,
        "same_period_last_year_n": float(yoy_n),
        "recent_n": float(len(recent)),
    }


def select_final_predictions(
    model_predictions: pd.DataFrame,
    test_weeks: list[int],
    optimized_composite_prior: float = 0.0,
    selector_config: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    score_rows: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    models = sorted(model_predictions["model_name"].unique())
    model_priors = (selector_config or {}).get("model_priors", {})
    up_gate = (selector_config or {}).get("up_gate")
    for week in test_weeks:
        week_candidates = model_predictions[model_predictions["week_id"].eq(week)].copy()
        for model in models:
            history = model_predictions[
                (model_predictions["model_name"].eq(model))
                & (model_predictions["week_id"] < week)
                & (model_predictions["label_end_week"] <= week)
                & model_predictions["actual_label"].isin([-1, 1])
            ].sort_values("week_id")
            metrics = score_one_model(history, week)
            model_prior = float(model_priors.get(model, 0.0))
            if not selector_config and model == OPTIMIZED_COMPOSITE_MODEL:
                model_prior += optimized_composite_prior
            row = {
                "week_id": week,
                "model_name": model,
                **metrics,
                "base_score": metrics["score"],
                "model_prior": model_prior,
                "selection_score": metrics["score"] + model_prior,
            }
            score_rows.append(row)
        scores = pd.DataFrame([r for r in score_rows if r["week_id"] == week])
        score_cols = [
            "model_name",
            "score",
            "selection_score",
            "recent_direction_accuracy",
            "recent_up_precision",
            "recent_down_precision",
            "recent_n",
        ]
        week_scored = week_candidates.merge(scores[score_cols], on="model_name", how="left")
        week_scored = week_scored.sort_values(["selection_score", "confidence"], ascending=[False, False])
        winner = week_scored.iloc[0].to_dict()
        selector_decision = "score_winner"
        if up_gate and int(winner["pred_label"]) == 1:
            pass_gate = (
                float(winner.get("recent_up_precision", 0.0)) >= float(up_gate.get("min_recent_up_precision", 0.0))
                and float(winner.get("recent_direction_accuracy", 0.0)) >= float(up_gate.get("min_recent_direction_accuracy", 0.0))
                and float(winner.get("recent_n", 0.0)) >= float(up_gate.get("min_recent_n", 0.0))
            )
            if not pass_gate:
                down_candidates = week_scored[week_scored["pred_label"].eq(-1)]
                if not down_candidates.empty:
                    winner = down_candidates.iloc[0].to_dict()
                    selector_decision = "up_gate_down_alternative"
                else:
                    winner["pred_label"] = -1
                    selector_decision = "up_gate_flip_down"
        actual_label = pd.to_numeric(pd.Series([winner.get("actual_label")]), errors="coerce").iloc[0]
        future_return = pd.to_numeric(pd.Series([winner.get("future_return")]), errors="coerce").iloc[0]
        has_actual = pd.notna(actual_label)
        final_rows.append(
            {
                "week_id": week,
                "week_date": winner.get("week_date"),
                "winner_model": winner["model_name"],
                "selector_decision": selector_decision,
                "winner_score": winner["selection_score"],
                "final_prob_up": winner["prob_up"],
                "final_pred_label": int(winner["pred_label"]),
                "actual_label": int(actual_label) if has_actual else np.nan,
                "future_return": future_return,
                "is_correct": bool(winner["pred_label"] == actual_label) if has_actual else np.nan,
            }
        )
    return pd.DataFrame(final_rows), pd.DataFrame(score_rows)


def summarize(name: str, frame: pd.DataFrame, pred_col: str = "pred_label") -> dict[str, Any]:
    if frame.empty:
        return {"model_name": name, "direction_accuracy": np.nan, "direction_correct": 0, "direction_total": 0}
    pred = frame[pred_col]
    label = frame["actual_label"]
    correct = int(pred.eq(label).sum())
    total = int(len(frame))
    pred_up = pred.eq(1)
    pred_down = pred.eq(-1)
    return {
        "model_name": name,
        "direction_accuracy": safe_rate(correct, total),
        "direction_correct": correct,
        "direction_total": total,
        "up_precision": safe_rate(int((pred_up & label.eq(1)).sum()), int(pred_up.sum())),
        "down_precision": safe_rate(int((pred_down & label.eq(-1)).sum()), int(pred_down.sum())),
        "pred_up": int(pred_up.sum()),
        "pred_down": int(pred_down.sum()),
    }


def calibration_summary(
    model_predictions: pd.DataFrame,
    calibration_start_week: int,
    calibration_end_week: int,
    external_test_start_week: int,
) -> pd.DataFrame:
    """Summarize calibration-period model quality without test-period labels."""
    if model_predictions.empty:
        return pd.DataFrame()
    cal = model_predictions[
        (model_predictions["week_id"] >= calibration_start_week)
        & (model_predictions["week_id"] <= calibration_end_week)
        & (model_predictions["label_end_week"] < external_test_start_week)
        & model_predictions["actual_label"].notna()
    ].copy()
    rows = []
    for model, group in cal.groupby("model_name", sort=True):
        row = summarize(model, group)
        row["calibration_start_week"] = calibration_start_week
        row["calibration_end_week"] = calibration_end_week
        row["strict_label_end_before_week"] = external_test_start_week
        row["min_week_id"] = int(group["week_id"].min()) if not group.empty else np.nan
        row["max_week_id"] = int(group["week_id"].max()) if not group.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows)

# =============================================================================
# Fixed-parameter 10Y D-overlay flow.
# =============================================================================

DATE_COL = "week_id"
TARGET_RATE_COL = "TB0YWI3C"
HORIZON = 1
LABEL_THRESHOLD = 0.0

START_DATE = pd.Timestamp("2025-07-01")
END_DATE = pd.Timestamp("2026-05-31")

# Use week_date for monthly reporting so week_id 202618, whose week_date is
# 2026-05-01, is counted in 2026-05.
MONTH_SOURCE = "week_date"

# Fixed Score main-model production parameters.
SCORE_START_WEEK = 202201
SCORE_TEST_START_WEEK = 202301
SCORE_TEST_END_WEEK = 202618
SCORE_SELECTOR_CALIBRATION_START_WEEK = 202301
SCORE_SELECTOR_CALIBRATION_END_WEEK = 202552
SCORE_FEATURE_SELECTION_CUTOFF_WEEK = 0
SCORE_THRESHOLD = 0.0
SCORE_FREEZE_TEST_TRAINING = False
SCORE_USE_OPTIMIZED_COMPOSITE = True
SCORE_USE_REGIME_ADAPTIVE_MODEL = True
SCORE_USE_H1_LOCKED_VOTE_MODEL = True
SCORE_USE_ROLLING_DEV_SELECTOR_MODEL = True
SCORE_USE_TARGET_SELECTOR_CONFIG = True
SCORE_OPTIMIZED_COMPOSITE_PRIOR = 0.0
SCORE_COMPOSITE_THRESHOLD_MODE = "rolling"
SCORE_COMPOSITE_VALIDATION_WINDOW = 52
SCORE_COMPOSITE_MIN_VALIDATION_SAMPLES = 24
SCORE_COMPOSITE_THRESHOLD_MIN = 0.20
SCORE_COMPOSITE_THRESHOLD_MAX = 0.80
SCORE_COMPOSITE_THRESHOLD_STEP = 0.005
SCORE_COMPOSITE_WEIGHT_STEP = 0.05
SCORE_COMPOSITE_WEIGHT_PRIOR_STRENGTH = 0.12
SCORE_COMPOSITE_THRESHOLD_PRIOR_STRENGTH = 0.25
SCORE_COMPOSITE_VALIDATION_UP_PRECISION_FLOOR = 0.55
SCORE_COMPOSITE_MIN_VALIDATION_UP_CALLS = 2

# Fixed Model2 parameters.
MODEL2_TRAIN_WINDOW_DAYS = 1825
MODEL2_N_TOP_FEATURES = 30
MODEL2_DEFAULT_THRESHOLD = 0.5
MODEL2_SEGMENTS = (
    ("2023_2024", pd.Timestamp("2023-01-01"), pd.Timestamp("2024-12-31")),
    ("2025H1", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-06-30")),
    ("2025H2_2026", pd.Timestamp("2025-07-01"), pd.Timestamp("2026-04-30")),
)
MODEL2_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 8,
    "learning_rate": 0.09969077407166702,
    "n_estimators": 890,
    "subsample": 0.6054698416953134,
    "colsample_bytree": 0.8836752636748003,
    "reg_alpha": 0.6486264486659488,
    "reg_lambda": 3.7157261614857175e-07,
    "min_child_samples": 61,
    "random_state": 42,
    "verbose": -1,
}

# Fixed D-overlay parameters.
D_CONFIG: dict[str, Any] = {
    "model2_threshold": 0.55,
    "score_low_conf": 0.08,
    "model2_strong_conf": 0.08,
    "rate_threshold": None,
}


def d_pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def require_columns(df: pd.DataFrame, cols: list[str], source: object) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in {source}: {missing}")



def create_label(df: pd.DataFrame, close_col: str) -> pd.DataFrame:
    out = df.copy()
    future_close = out[close_col].shift(-HORIZON)
    current_close = out[close_col]
    out["future_return"] = (future_close - current_close) / current_close
    out["label_5d"] = np.where(
        out["future_return"] > LABEL_THRESHOLD,
        1,
        np.where(out["future_return"] < -LABEL_THRESHOLD, -1, 0),
    )
    return out


def load_engineered_frame(weekly: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    df = _normalize_weekly_input(weekly)
    require_columns(df, [DATE_COL, TARGET_RATE_COL, "week_date", "model_date"], "weekly input")
    df["date"] = pd.to_datetime(df["model_date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    df = create_label(df, TARGET_RATE_COL)

    exclude_cols = ["date", "week_date", "model_date", DATE_COL, TARGET_RATE_COL, "future_return", "label_5d"]
    original_features = [
        col for col in df.columns if col not in exclude_cols and pd.api.types.is_numeric_dtype(df[col])
    ]

    lag_frames = [df[original_features].shift(lag).add_suffix(f"_lag{lag}") for lag in (1, 2, 3)]
    diff_frames = [df[original_features].diff(periods=diff).add_suffix(f"__d{diff}") for diff in (1, 2, 3, 4, 5)]
    engineered = pd.concat([df] + lag_frames + diff_frames, axis=1)
    all_features = (
        original_features
        + [f"{col}_lag{lag}" for lag in (1, 2, 3) for col in original_features]
        + [f"{col}__d{diff}" for diff in (1, 2, 3, 4, 5) for col in original_features]
    )
    return engineered, all_features


def remove_high_corr_features(
    x: np.ndarray,
    feature_names: np.ndarray,
    threshold: float = 0.85,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    if x.shape[1] <= 1:
        return x, feature_names, list(range(x.shape[1]))
    corr = pd.DataFrame(x, columns=list(feature_names)).corr(method="spearman").abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    to_drop: set[str] = set()
    for col in upper.columns:
        high_corr_cols = upper.index[upper[col] > threshold].tolist()
        to_drop.update(high_corr_cols)
    keep = [f for f in feature_names if f not in to_drop]
    keep_idx = [list(feature_names).index(f) for f in keep]
    return x[:, keep_idx], np.asarray(keep), keep_idx


def build_model2_segment(
    base_df: pd.DataFrame,
    all_features: list[str],
    segment_name: str,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
) -> pd.DataFrame:
    train_df = base_df[base_df["date"] < test_start].copy()
    test_df = base_df[(base_df["date"] >= test_start) & (base_df["date"] <= test_end)].copy()

    if MODEL2_TRAIN_WINDOW_DAYS > 0:
        start_date = test_start - pd.Timedelta(days=MODEL2_TRAIN_WINDOW_DAYS)
        train_df = train_df[train_df["date"] >= start_date].copy()

    for frame in (train_df, test_df):
        frame.loc[:, all_features] = frame[all_features].ffill().fillna(0)

    train_df = train_df.dropna(subset=all_features + ["label_5d"])
    test_df = test_df.dropna(subset=all_features)
    train_bin = train_df[train_df["label_5d"].isin([-1, 1])]
    if train_bin.empty or test_df.empty:
        raise RuntimeError(f"Empty train/test data for Model2 segment {segment_name}")

    x_train = train_bin[all_features].to_numpy()
    y_train = train_bin["label_5d"].map({-1: 0, 1: 1}).to_numpy()
    x_test = test_df[all_features].to_numpy()

    scaler = RobustScaler()
    x_train_scaled = scaler.fit_transform(x_train)
    x_test_scaled = scaler.transform(x_test)

    linear_mask = np.zeros(x_train_scaled.shape[1], dtype=bool)
    for i in range(x_train_scaled.shape[1]):
        col = x_train_scaled[:, i]
        if np.allclose(col, col[0]):
            corr = 0.0
        else:
            corr, _ = pearsonr(col, y_train)
            if not np.isfinite(corr):
                corr = 0.0
        linear_mask[i] = abs(corr) >= 0.05

    x_train_scaled = x_train_scaled[:, linear_mask]
    x_test_scaled = x_test_scaled[:, linear_mask]
    filtered_features = np.asarray(all_features)[linear_mask]
    if x_train_scaled.shape[1] == 0:
        raise RuntimeError(f"No features left after linear filter for {segment_name}")

    mi = mutual_info_classif(x_train_scaled, y_train, random_state=42)
    selected_idx = np.argsort(mi)[::-1][: min(MODEL2_N_TOP_FEATURES, len(mi))].tolist()
    x_train_sel = x_train_scaled[:, selected_idx]
    x_test_sel = x_test_scaled[:, selected_idx]
    selected_features = filtered_features[selected_idx]

    x_train_sel, selected_features, keep_idx = remove_high_corr_features(x_train_sel, selected_features)
    x_test_sel = x_test_sel[:, keep_idx]
    if x_train_sel.shape[1] == 0:
        raise RuntimeError(f"No features left after corr filter for {segment_name}")
    if lgb is None:
        raise ImportError("lightgbm is required for Model2 predictions")

    model = lgb.LGBMClassifier(**MODEL2_PARAMS)
    model.fit(x_train_sel, y_train)
    prob_up = model.predict_proba(x_test_sel)[:, 1]
    pred_label = np.where(prob_up > MODEL2_DEFAULT_THRESHOLD, 1, -1)

    return pd.DataFrame(
        {
            "segment": segment_name,
            "week_id": test_df[DATE_COL].astype(int).to_numpy(),
            "date": test_df["date"].to_numpy(),
            "actual_label": pd.to_numeric(test_df["label_5d"], errors="coerce").to_numpy(),
            "future_return": test_df["future_return"].to_numpy(),
            "model2_prob_up": prob_up,
            "model2_pred_label": pred_label,
            "selected_feature_count": len(selected_features),
        }
    )


def build_model2_predictions(weekly: pd.DataFrame) -> pd.DataFrame:
    base_df, all_features = load_engineered_frame(weekly)
    max_date = pd.to_datetime(base_df["date"]).max()
    segments = []
    for name, cutoff, end in MODEL2_SEGMENTS:
        segment_end = max(end, max_date) if name == MODEL2_SEGMENTS[-1][0] else end
        segments.append((name, cutoff, segment_end))
    frames = [
        build_model2_segment(base_df, all_features, name, cutoff, end)
        for name, cutoff, end in segments
    ]
    preds = pd.concat(frames, ignore_index=True).sort_values("week_id").reset_index(drop=True)
    data_cols = base_df[["week_id", TARGET_RATE_COL, "TB1YWI3C", "TB5YWI3C"]].copy()
    preds = preds.merge(data_cols, on="week_id", how="left")
    return preds


def build_score_signals(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    weekly = _normalize_weekly_input(weekly)
    for col in (TARGET_RATE_COL, "TB1YWI3C", "TB5YWI3C"):
        if col not in weekly.columns:
            raise ValueError(f"required weekly column not found for Score engine: {col}")

    close = pd.to_numeric(weekly[TARGET_RATE_COL], errors="coerce")
    future_return, labels = make_labels(weekly, TARGET_RATE_COL, SCORE_THRESHOLD)
    score_end_week = max(int(SCORE_TEST_END_WEEK), int(weekly["week_id"].max()))
    label_series = pd.Series(labels, index=weekly.index)
    valid_weeks = weekly.loc[
        (weekly["week_id"] >= SCORE_START_WEEK)
        & (weekly["week_id"] <= score_end_week)
        & (label_series.notna() | (weekly["week_id"] >= SCORE_TEST_START_WEEK))
        & close.notna(),
        "week_id",
    ].astype(int).tolist()
    test_weeks = [week for week in valid_weeks if SCORE_TEST_START_WEEK <= week <= score_end_week]
    feature_selection_cutoff_week = SCORE_FEATURE_SELECTION_CUTOFF_WEEK or SCORE_TEST_START_WEEK

    x_target = target_features(weekly, TARGET_RATE_COL, "TB1YWI3C", "TB5YWI3C")
    x_broad = broad_features(weekly, x_target, TARGET_RATE_COL, feature_selection_cutoff_week)
    layers = framework_feature_sets(weekly, x_target)

    logistic = sklearn_model_predictions(
        MODEL_LOGISTIC,
        weekly,
        x_target,
        labels,
        close,
        valid_weeks,
        SCORE_TEST_START_WEEK,
        SCORE_FREEZE_TEST_TRAINING,
        estimator="logistic",
        k_best=16,
        params={"C": 2.0, "class_weight": "balanced"},
    )
    knn = sklearn_model_predictions(
        MODEL_KNN,
        weekly,
        x_broad,
        labels,
        close,
        valid_weeks,
        SCORE_TEST_START_WEEK,
        SCORE_FREEZE_TEST_TRAINING,
        estimator="knn",
        k_best=5,
        params={"n_neighbors": 9, "weights": "uniform"},
    )
    knn_logit = ensemble_predictions(logistic, knn, MODEL_KNN_LOGISTIC)
    lgbm_core = lgbm_predictions(
        MODEL_LGBM_CORE,
        weekly,
        x_broad.fillna(0.0),
        labels,
        close,
        future_return,
        valid_weeks,
        SCORE_TEST_START_WEEK,
        SCORE_FREEZE_TEST_TRAINING,
        params={
            "close_col": TARGET_RATE_COL,
            "window": 104,
            "num_leaves": 2,
            "min_child_samples": 8,
            "learning_rate": 0.03,
            "n_estimators": 160,
            "epsilon": 0.14,
        },
        short_col="TB1YWI3C",
        mode="core",
    )
    framework = framework_predictions(
        weekly,
        layers,
        labels,
        close,
        valid_weeks,
        SCORE_TEST_START_WEEK,
        SCORE_FREEZE_TEST_TRAINING,
    )

    base_model_frames = {
        MODEL_LOGISTIC: logistic,
        MODEL_KNN: knn,
        MODEL_KNN_LOGISTIC: knn_logit,
        MODEL_LGBM_CORE: lgbm_core,
        MODEL_FRAMEWORK: framework,
    }
    composite = (
        optimized_composite_predictions(
            TARGET_RATE_COL,
            base_model_frames,
            weekly,
            labels,
            SCORE_COMPOSITE_THRESHOLD_MODE,
            SCORE_COMPOSITE_VALIDATION_WINDOW,
            SCORE_COMPOSITE_MIN_VALIDATION_SAMPLES,
            SCORE_COMPOSITE_THRESHOLD_MIN,
            SCORE_COMPOSITE_THRESHOLD_MAX,
            SCORE_COMPOSITE_THRESHOLD_STEP,
            SCORE_COMPOSITE_WEIGHT_STEP,
            SCORE_COMPOSITE_WEIGHT_PRIOR_STRENGTH,
            SCORE_COMPOSITE_THRESHOLD_PRIOR_STRENGTH,
            SCORE_COMPOSITE_VALIDATION_UP_PRECISION_FLOOR,
            SCORE_COMPOSITE_MIN_VALIDATION_UP_CALLS,
        )
        if SCORE_USE_OPTIMIZED_COMPOSITE
        else pd.DataFrame()
    )
    regime_adaptive = (
        regime_adaptive_loss_guard_predictions(TARGET_RATE_COL, base_model_frames, weekly, labels)
        if SCORE_USE_REGIME_ADAPTIVE_MODEL
        else pd.DataFrame()
    )
    vote_source_frames = dict(base_model_frames)
    if not regime_adaptive.empty:
        vote_source_frames[MODEL_REGIME_ADAPTIVE] = regime_adaptive
    h1_locked_vote = (
        h1_locked_vote_predictions(TARGET_RATE_COL, vote_source_frames)
        if SCORE_USE_H1_LOCKED_VOTE_MODEL
        else pd.DataFrame()
    )
    rolling_dev_selector = (
        rolling_dev_selected_predictions(TARGET_RATE_COL, vote_source_frames, weekly, labels)
        if SCORE_USE_ROLLING_DEV_SELECTOR_MODEL
        else pd.DataFrame()
    )
    if SCORE_USE_ROLLING_DEV_SELECTOR_MODEL and not rolling_dev_selector.empty:
        model_frames = [logistic, knn, knn_logit, lgbm_core, framework, rolling_dev_selector]
    elif SCORE_USE_H1_LOCKED_VOTE_MODEL and not h1_locked_vote.empty:
        model_frames = [logistic, knn, knn_logit, lgbm_core, framework, h1_locked_vote]
    elif SCORE_USE_REGIME_ADAPTIVE_MODEL and not regime_adaptive.empty:
        model_frames = [logistic, knn, knn_logit, lgbm_core, framework, regime_adaptive]
    elif SCORE_USE_OPTIMIZED_COMPOSITE and not composite.empty:
        model_frames = [logistic, knn, knn_logit, lgbm_core, framework, composite]
    else:
        model_frames = [logistic, knn, knn_logit, lgbm_core, framework]

    model_predictions = pd.concat(model_frames, ignore_index=True)
    model_predictions = enrich_predictions(model_predictions, weekly, future_return, labels)
    test_model_predictions = model_predictions[
        (model_predictions["week_id"] >= SCORE_TEST_START_WEEK)
        & (model_predictions["week_id"] <= score_end_week)
    ].copy()
    selector_config = TARGET_FINAL_SELECTOR_CONFIG.get(TARGET_RATE_COL) if SCORE_USE_TARGET_SELECTOR_CONFIG else None
    composite_prior = 0.0 if selector_config else (SCORE_OPTIMIZED_COMPOSITE_PRIOR if SCORE_USE_OPTIMIZED_COMPOSITE else 0.0)
    final, score_history = select_final_predictions(
        model_predictions,
        test_weeks,
        composite_prior,
        selector_config,
    )
    if selector_config:
        final["selector_profile"] = PRODUCTION_SELECTOR_PROFILE
        final["selector_calibration_start_week"] = SCORE_SELECTOR_CALIBRATION_START_WEEK
        final["selector_calibration_end_week"] = SCORE_SELECTOR_CALIBRATION_END_WEEK
    else:
        final["selector_profile"] = "rolling_score_default"
        final["selector_calibration_start_week"] = np.nan
        final["selector_calibration_end_week"] = np.nan

    if final.empty:
        return (
            pd.DataFrame(
                columns=[
                    "week_id",
                    "week_date",
                    "winner_model",
                    "selector_decision",
                    "winner_score",
                    "score_prob_up",
                    "score_pred_label",
                    "score_actual_label",
                    "score_future_return",
                ]
            ),
            test_model_predictions,
            score_history,
        )

    final = final.rename(
        columns={
            "final_pred_label": "score_pred_label",
            "final_prob_up": "score_prob_up",
            "actual_label": "score_actual_label",
            "future_return": "score_future_return",
        }
    )
    keep = [
        "week_id",
        "week_date",
        "winner_model",
        "selector_decision",
        "winner_score",
        "score_prob_up",
        "score_pred_label",
        "score_actual_label",
        "score_future_return",
    ]
    keep = [col for col in keep if col in final.columns]
    final = final[keep].copy()
    final["week_id"] = final["week_id"].astype(int)
    final["week_date"] = pd.to_datetime(final["week_date"])
    return final, test_model_predictions, score_history


def build_base(model2: pd.DataFrame, score: pd.DataFrame) -> pd.DataFrame:
    df = model2.merge(score, on="week_id", how="inner", suffixes=("", "_score"))
    df["month_date"] = pd.to_datetime(df[MONTH_SOURCE])
    df["month"] = df["month_date"].dt.to_period("M").astype(str)
    df["actual_label"] = pd.to_numeric(df["actual_label"], errors="coerce")
    df["score_pred_label"] = df["score_pred_label"].astype(int)
    df["score_prob_up"] = df["score_prob_up"].astype(float)
    df["model2_prob_up"] = df["model2_prob_up"].astype(float)
    df["score_actual_label"] = pd.to_numeric(df["score_actual_label"], errors="coerce")
    df["label_mismatch"] = df["score_actual_label"].notna() & df["actual_label"].notna() & (
        df["score_actual_label"].astype(float) != df["actual_label"].astype(float)
    )
    if df["label_mismatch"].any():
        bad = df.loc[df["label_mismatch"], ["week_id", "actual_label", "score_actual_label"]].head().to_dict("records")
        raise ValueError(f"Score/Model2 label mismatch, examples: {bad}")
    df = df[df["month_date"] >= START_DATE].copy()
    return df.sort_values("month_date").reset_index(drop=True)


def apply_d_overlay(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    threshold = float(D_CONFIG["model2_threshold"])
    score_low_conf = float(D_CONFIG["score_low_conf"])
    model2_strong_conf = float(D_CONFIG["model2_strong_conf"])
    rate_threshold = D_CONFIG["rate_threshold"]

    out["d_score_conf"] = (out["score_prob_up"].astype(float) - 0.5).abs()
    out["d_model2_pred_label"] = np.where(out["model2_prob_up"].astype(float) >= threshold, 1, -1).astype(int)
    out["d_model2_conf"] = (out["model2_prob_up"].astype(float) - threshold).abs()
    out["d_model_disagree"] = out["d_model2_pred_label"] != out["score_pred_label"].astype(int)

    overlay = (
        out["d_model_disagree"]
        & (out["d_score_conf"] <= score_low_conf)
        & (out["d_model2_conf"] >= model2_strong_conf)
    )
    if rate_threshold is not None:
        overlay &= out[TARGET_RATE_COL].astype(float) <= float(rate_threshold)

    out["d_model2_overlay"] = overlay.astype(bool)
    out["d_pred_label"] = np.where(out["d_model2_overlay"], out["d_model2_pred_label"], out["score_pred_label"]).astype(int)
    out["d_prob_up"] = np.where(out["d_model2_overlay"], out["model2_prob_up"], out["score_prob_up"]).astype(float)
    out["d_signal_source"] = np.where(out["d_model2_overlay"], "model2_overlay", "score_main")
    actual = pd.to_numeric(out["actual_label"], errors="coerce")
    out["d_is_correct"] = out["d_pred_label"].astype(int).eq(actual).where(actual.notna(), np.nan)
    return out


def _normalize_weekly_input(weekly: pd.DataFrame) -> pd.DataFrame:
    df = weekly.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    required = [DATE_COL, "week_date", "model_date", TARGET_RATE_COL, "TB1YWI3C", "TB5YWI3C"]
    require_columns(df, required, "weekly input")
    df[DATE_COL] = pd.to_numeric(df[DATE_COL], errors="coerce").astype("Int64")
    df = df.dropna(subset=[DATE_COL, "week_date", "model_date"]).copy()
    df[DATE_COL] = df[DATE_COL].astype(int)
    df["week_date"] = pd.to_datetime(df["week_date"], errors="coerce")
    df["model_date"] = pd.to_datetime(df["model_date"], errors="coerce")
    df = df.dropna(subset=["week_date", "model_date"]).copy()
    for col in df.columns:
        if col not in {DATE_COL, "week_date", "model_date"}:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(DATE_COL).drop_duplicates(DATE_COL, keep="last").reset_index(drop=True)


def build_d_overlay(weekly: pd.DataFrame) -> pd.DataFrame:
    """构建 10Y D-overlay 最终预测序列。"""
    normalized = _normalize_weekly_input(weekly)
    score, _, _ = build_score_signals(normalized)
    if score.empty:
        raise RuntimeError("Score engine produced no predictions")
    model2 = build_model2_predictions(normalized)
    if model2.empty:
        raise RuntimeError("Model2 produced no predictions")
    return apply_d_overlay(build_base(model2, score))


def d_safe_rate(num: int, den: int) -> float:
    return num / den if den else np.nan


def d_metrics(df: pd.DataFrame) -> dict[str, Any]:
    pred = df["d_pred_label"].astype(int)
    actual = pd.to_numeric(df["actual_label"], errors="coerce")
    labelled = actual.notna()
    correct = pred.eq(actual)
    pred_up = int(pred.eq(1).sum())
    pred_down = int(pred.eq(-1).sum())
    true_up = int(actual.eq(1).sum())
    true_down = int(actual.eq(-1).sum())
    true_flat = int(actual.eq(0).sum())
    up_correct = int((pred.eq(1) & actual.eq(1)).sum())
    down_correct = int((pred.eq(-1) & actual.eq(-1)).sum())
    overlay = df["d_model2_overlay"].astype(bool)
    overlay_total = int(overlay.sum())
    overlay_correct = int((overlay & correct).sum())

    return {
        "total": int(len(df)),
        "correct": int(correct.sum()),
        "direction_accuracy": float(correct[labelled].mean()) if int(labelled.sum()) else np.nan,
        "true_up": true_up,
        "true_down": true_down,
        "true_flat": true_flat,
        "pred_up": pred_up,
        "pred_down": pred_down,
        "up_precision": d_safe_rate(up_correct, pred_up),
        "down_precision": d_safe_rate(down_correct, pred_down),
        "up_recall": d_safe_rate(up_correct, true_up),
        "down_recall": d_safe_rate(down_correct, true_down),
        "overlay_total": overlay_total,
        "overlay_correct": overlay_correct,
        "overlay_accuracy": d_safe_rate(overlay_correct, overlay_total),
    }


def d_monthly_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for month, group in df.groupby("month", sort=True):
        row = {"month": month}
        row.update(d_metrics(group))
        rows.append(row)
    return pd.DataFrame(rows)
