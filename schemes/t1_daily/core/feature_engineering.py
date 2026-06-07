from __future__ import annotations

import numpy as np
import pandas as pd

from .config import TenorConfig


def safe_sign(values: pd.Series) -> pd.Series:
    return np.sign(values).replace(0, -1).fillna(-1).astype(int)


def _add_origin(origin: dict[str, list[str]], feature: str, codes: list[str]) -> None:
    origin[feature] = codes


def add_tenor_features(
    features: dict[str, pd.Series],
    origin: dict[str, list[str]],
    df: pd.DataFrame,
    name: str,
    col: str,
) -> None:
    close = pd.to_numeric(df[col], errors="coerce")
    ret = close.pct_change(fill_method=None)

    for lag in (1, 2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120):
        lag_ret = ret.shift(lag - 1)
        feature = f"{name}_ret_lag{lag}"
        features[feature] = lag_ret
        _add_origin(origin, feature, [col])
        feature = f"{name}_sign_lag{lag}"
        features[feature] = np.sign(lag_ret)
        _add_origin(origin, feature, [col])

    for window in (2, 3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 252):
        summed = ret.rolling(window).sum()
        for feature, series in {
            f"{name}_mom_sum{window}": summed,
            f"{name}_mom_sign{window}": np.sign(summed),
            f"{name}_vol{window}": ret.rolling(window).std(),
        }.items():
            features[feature] = series
            _add_origin(origin, feature, [col])
        ma = close.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = close.rolling(window, min_periods=max(2, window // 2)).std()
        feature = f"{name}_z{window}"
        features[feature] = close.sub(ma).div(sd.replace(0, np.nan))
        _add_origin(origin, feature, [col])

    ema12 = close.ewm(span=12, adjust=False, min_periods=6).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=13).mean()
    macd = ema12 - ema26
    feature = f"{name}_macd_gap"
    features[feature] = macd - macd.ewm(span=9, adjust=False, min_periods=5).mean()
    _add_origin(origin, feature, [col])

    gain = ret.clip(lower=0).rolling(14, min_periods=7).mean()
    loss = (-ret.clip(upper=0)).rolling(14, min_periods=7).mean()
    feature = f"{name}_rsi14"
    features[feature] = 100 - 100 / (1 + gain.div(loss.replace(0, np.nan)))
    _add_origin(origin, feature, [col])


def add_spread_features(
    features: dict[str, pd.Series],
    origin: dict[str, list[str]],
    df: pd.DataFrame,
    left_name: str,
    right_name: str,
    left_col: str,
    right_col: str,
) -> None:
    spread = pd.to_numeric(df[left_col], errors="coerce").sub(pd.to_numeric(df[right_col], errors="coerce"))
    base = f"spread_{left_name}_{right_name}"
    features[base] = spread
    _add_origin(origin, base, [left_col, right_col])
    for window in (5, 20, 60, 120):
        ma = spread.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = spread.rolling(window, min_periods=max(2, window // 2)).std()
        for feature, series in {
            f"{base}_z{window}": spread.sub(ma).div(sd.replace(0, np.nan)),
            f"{base}_chg{window}": spread.sub(spread.shift(window)),
        }.items():
            features[feature] = series
            _add_origin(origin, feature, [left_col, right_col])


def build_feature_matrix(df: pd.DataFrame, config: TenorConfig) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    features: dict[str, pd.Series] = {}
    origin: dict[str, list[str]] = {}
    if config.style == "5y":
        if not config.long_col or not config.short_col:
            raise ValueError("5y style requires long_col and short_col")
        add_tenor_features(features, origin, df, "1Y", config.short_col)
        add_tenor_features(features, origin, df, "5Y", config.close_col)
        add_tenor_features(features, origin, df, "10Y", config.long_col)
        add_spread_features(features, origin, df, "5Y", "1Y", config.close_col, config.short_col)
        add_spread_features(features, origin, df, "10Y", "5Y", config.long_col, config.close_col)
        add_spread_features(features, origin, df, "10Y", "1Y", config.long_col, config.short_col)
    elif config.style == "10y":
        if not config.mid_col or not config.short_col:
            raise ValueError("10y style requires mid_col and short_col")
        add_tenor_features(features, origin, df, "1Y", config.short_col)
        add_tenor_features(features, origin, df, "5Y", config.mid_col)
        add_tenor_features(features, origin, df, "10Y", config.close_col)
        add_spread_features(features, origin, df, "10Y", "1Y", config.close_col, config.short_col)
        add_spread_features(features, origin, df, "10Y", "5Y", config.close_col, config.mid_col)
        add_spread_features(features, origin, df, "5Y", "1Y", config.mid_col, config.short_col)
    else:
        raise ValueError(f"unsupported feature style: {config.style}")
    matrix = pd.DataFrame(features, index=df.index).replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)
    return matrix, origin


def build_fallback_signal(df: pd.DataFrame, close_col: str) -> pd.Series:
    ret = pd.to_numeric(df[close_col], errors="coerce").pct_change(fill_method=None)
    return -safe_sign(ret.shift(19))


def build_vote_signals(df: pd.DataFrame, config: TenorConfig) -> pd.DataFrame:
    if config.style == "5y":
        long_col = config.long_col or ""
        short_col = config.short_col or ""
        ten_year = pd.to_numeric(df[long_col], errors="coerce")
        ten_year_ret = ten_year.pct_change(fill_method=None)
        five_year = pd.to_numeric(df[config.close_col], errors="coerce")
        one_year = pd.to_numeric(df[short_col], errors="coerce")
        ratio_5y_1y = five_year.div(one_year).sub(1.0)
        ratio_mean = ratio_5y_1y.rolling(300, min_periods=150).mean()
        ratio_std = ratio_5y_1y.rolling(300, min_periods=150).std()
        ratio_z = ratio_5y_1y.sub(ratio_mean).div(ratio_std.replace(0, np.nan))
        spread_10y_5y_change = ten_year.sub(five_year).diff()
        ten_year_ma150 = ten_year.rolling(150, min_periods=75).mean()
        signals = {
            "10Y_anti_lag11": -safe_sign(ten_year_ret.shift(10)),
            "ratio_5Y_1Y_z_anti300": -safe_sign(ratio_z),
            "spread_10Y_5Y_mom_lag252": safe_sign(spread_10y_5y_change.shift(251)),
            "10Y_below_ma150": -safe_sign(ten_year.sub(ten_year_ma150)),
        }
    else:
        mid_col = config.mid_col or ""
        short_col = config.short_col or ""
        ten_year_ret = pd.to_numeric(df[config.close_col], errors="coerce").pct_change(fill_method=None)
        five_year = pd.to_numeric(df[mid_col], errors="coerce")
        one_year = pd.to_numeric(df[short_col], errors="coerce")
        five_year_ret = five_year.pct_change(fill_method=None)
        spread_5y_1y_change = five_year.sub(one_year).diff()
        signals = {
            "5Y_anti_sum180": -safe_sign(five_year_ret.rolling(180).sum()),
            "spread_5Y_1Y_chg_anti_sum252": -safe_sign(spread_5y_1y_change.rolling(252).sum()),
            "5Y_mom_lag3": safe_sign(five_year_ret.shift(2)),
            "spread_5Y_1Y_chg_anti_sum10": -safe_sign(spread_5y_1y_change.rolling(10).sum()),
            "spread_5Y_1Y_chg_anti_sum40": -safe_sign(spread_5y_1y_change.rolling(40).sum()),
            "5Y_anti_lag252": -safe_sign(five_year_ret.shift(251)),
            "10Y_anti_sum180": -safe_sign(ten_year_ret.rolling(180).sum()),
        }
    return pd.DataFrame(signals, index=df.index)
