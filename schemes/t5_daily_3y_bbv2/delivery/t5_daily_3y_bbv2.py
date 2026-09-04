#!/usr/bin/env python3
"""Native 0529 日频 T+1/T+5 算法的自包含 Blackbox V2 实现。"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "8")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "8")
os.environ.setdefault("MKL_NUM_THREADS", "8")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "8")

import lightgbm as lgb
import numpy as np
import pandas as pd


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
PERIOD_KEY_RE = re.compile(r"^\d{6}$")
COL_MAP = {
    "1Y": "TB1YWI0C", "3Y": "TB3YWI0C", "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C", "10Y": "TB0YWI0C",
}


@dataclass(frozen=True)
class Settings:
    family: str
    tenor: str
    close_col: str
    style: str = ""
    mid_col: str | None = None
    long_col: str | None = None
    short_col: str | None = None
    window: int = 126
    num_leaves: int = 3
    min_child_samples: int = 20
    learning_rate: float = 0.03
    n_estimators: int = 140
    epsilon: float = 0.0
    split_pct: float = 0.8
    reg_alpha: float = 0.8
    reg_lambda: float = 3.0
    vote_threshold: int = 0


SETTINGS = {
    "t1_daily_5y_bbv2": Settings(
        "t1", "5Y", COL_MAP["5Y"], style="5y", long_col=COL_MAP["10Y"],
        short_col=COL_MAP["1Y"], min_child_samples=40, epsilon=0.04,
    ),
    "t1_daily_10y_bbv2": Settings(
        "t1", "10Y", COL_MAP["10Y"], style="10y", mid_col=COL_MAP["5Y"],
        short_col=COL_MAP["1Y"],
    ),
    "t5_daily_3y_bbv2": Settings(
        "t5", "3Y", COL_MAP["3Y"], window=550, min_child_samples=11,
        split_pct=0.82, reg_alpha=0.0, reg_lambda=0.5, vote_threshold=1,
    ),
    "t5_daily_5y_bbv2": Settings(
        "t5", "5Y", COL_MAP["5Y"], window=756, num_leaves=5,
        min_child_samples=10, split_pct=0.85, reg_lambda=0.5,
        vote_threshold=1,
    ),
    "t5_daily_7y_bbv2": Settings(
        "t5", "7Y", COL_MAP["7Y"], window=300, min_child_samples=10,
        split_pct=0.70, reg_alpha=0.0, reg_lambda=0.5,
    ),
    "t5_daily_10y_bbv2": Settings(
        "t5", "10Y", COL_MAP["10Y"], window=240, min_child_samples=10,
        reg_alpha=0.0, reg_lambda=0.5, vote_threshold=1,
    ),
}
CONFIG = SETTINGS.get(Path(__file__).stem)
if CONFIG is None:
    raise RuntimeError("delivery filename does not identify a supported successor")


class ContractError(ValueError):
    """输入或输出不满足 Blackbox V2 合同。"""


def _strict_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ContractError(f"{field} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid date") from exc
    return value


def validate_request(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != set(REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    if any(not isinstance(raw[field], str) for field in REQUEST_FIELDS):
        raise ContractError("all request fields must be strings")
    request = {field: raw[field] for field in REQUEST_FIELDS}
    if not request["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature = _strict_date(request["feature_date"], "feature_date")
    predict = _strict_date(request["predict_date"], "predict_date")
    target = _strict_date(request["target_date"], "target_date")
    _strict_date(request["daily_cutoff_key"], "daily_cutoff_key")
    if not feature <= predict <= target or not feature < target:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not PERIOD_KEY_RE.fullmatch(request[field]):
            raise ContractError(f"{field} must be a six-digit string")
    if request["daily_cutoff_key"] != feature:
        raise ContractError("feature_date must equal daily_cutoff_key")
    return request


def read_requests(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.command == "predict":
        try:
            with args.request.open("r", encoding="utf-8-sig") as handle:
                requests = [validate_request(json.load(handle))]
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("cannot read request JSON") from exc
    else:
        try:
            with args.requests.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if (
                    reader.fieldnames is None
                    or set(reader.fieldnames) != set(REQUEST_FIELDS)
                    or len(reader.fieldnames) != len(REQUEST_FIELDS)
                ):
                    raise ContractError("request CSV fields must exactly match Contract 1.0")
                requests = [validate_request(row) for row in reader]
        except (OSError, csv.Error) as exc:
            raise ContractError("cannot read request CSV") from exc
        if not requests:
            raise ContractError("request CSV must contain at least one row")
    ids = [request["request_id"] for request in requests]
    if len(ids) != len(set(ids)):
        raise ContractError("request_id values must be unique within a batch")
    return requests


def _period_frame(path: Path, key: str) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype={key: "string"})
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError(f"cannot read {path.name}") from exc
    if key not in frame or frame.empty:
        raise ContractError(f"{path.name} must contain non-empty {key}")
    values = frame[key].astype("string")
    if (values.isna().any() or not values.str.fullmatch(PERIOD_KEY_RE).fillna(False).all()
            or values.duplicated().any() or not values.is_monotonic_increasing):
        raise ContractError(f"{path.name} {key} values are invalid")
    return frame


def read_daily_and_validate_snapshot(
    data_dir: Path, requests: list[dict[str, str]],
) -> pd.DataFrame:
    paths = {name: data_dir / name for name in DATA_FILES}
    if any(not path.is_file() for path in paths.values()):
        raise ContractError("data-dir must contain all five DataBridge files")
    try:
        daily = pd.read_csv(paths["daily_output.csv"])
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError("cannot read daily_output.csv") from exc
    required = {"date", CONFIG.close_col}
    if CONFIG.family == "t1":
        required.update(col for col in (CONFIG.mid_col, CONFIG.long_col, CONFIG.short_col) if col)
    else:
        required.update(COL_MAP.values())
    missing = sorted(required - set(daily.columns))
    if missing:
        raise ContractError(f"daily_output.csv is missing required columns: {missing}")
    try:
        daily["date"] = pd.to_datetime(daily["date"], format="%Y-%m-%d", errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily_output.csv date values are invalid") from exc
    if daily.empty or daily["date"].duplicated().any() or not daily["date"].is_monotonic_increasing:
        raise ContractError("daily_output.csv dates must be non-empty, unique, and ascending")
    for col in required - {"date"}:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    weekly = _period_frame(paths["weekly_output.csv"], "week_id")
    monthly = _period_frame(paths["monthly_output.csv"], "month_id")
    try:
        week_map = pd.read_csv(
            paths["api_wind_date.csv"], dtype={"rdate": "string", "week_id": "string"},
            keep_default_na=False, encoding="utf-8-sig",
        )
        catalog = pd.read_csv(paths["factor_catalog.csv"], dtype="string")
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError("cannot read DataBridge calendar or catalog") from exc
    if week_map.columns.tolist() != ["rdate", "week_id"] or week_map.empty:
        raise ContractError("api_wind_date.csv must contain rdate,week_id")
    if set(catalog.columns) != {"indicators_code", "frequency", "factor_version"} or catalog.empty:
        raise ContractError("factor_catalog.csv schema is invalid")
    try:
        week_map["rdate"] = pd.to_datetime(
            week_map["rdate"], format="%Y-%m-%d", errors="raise",
        ).dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("api_wind_date.csv rdate values are invalid") from exc
    if (week_map["rdate"].duplicated().any() or not week_map["rdate"].is_monotonic_increasing
            or not week_map["week_id"].str.fullmatch(PERIOD_KEY_RE).all()):
        raise ContractError("api_wind_date.csv keys are invalid")
    catalog_daily = set(catalog.loc[catalog["frequency"].eq("daily"), "indicators_code"])
    if not required - {"date"} <= catalog_daily:
        raise ContractError("factor_catalog.csv does not declare every required daily factor")
    daily_dates = set(daily["date"].dt.strftime("%Y-%m-%d"))
    weekly_keys = set(weekly["week_id"].astype(str))
    monthly_keys = set(monthly["month_id"].astype(str))
    mapped_weeks = week_map.set_index(week_map["rdate"].dt.strftime("%Y-%m-%d"))["week_id"]
    for request in requests:
        feature = request["feature_date"]
        if feature not in daily_dates:
            raise ContractError("daily cutoff key must occur exactly once")
        if request["weekly_cutoff_key"] not in weekly_keys:
            raise ContractError("weekly cutoff key must occur exactly once")
        if request["monthly_cutoff_key"] not in monthly_keys:
            raise ContractError("monthly cutoff key must occur exactly once")
        if feature not in mapped_weeks or str(mapped_weeks.loc[feature]) != request["weekly_cutoff_key"]:
            raise ContractError("daily cutoff calendar mapping must match weekly cutoff key")
    return daily


def safe_sign(values: pd.Series) -> pd.Series:
    return np.sign(values).replace(0, -1).fillna(-1).astype(int)


def add_tenor_features(
    features: dict[str, pd.Series], df: pd.DataFrame, name: str, col: str,
    *, t1: bool,
) -> None:
    close = df[col]
    ret = (
        close.pct_change(fill_method=None)
        if t1
        else close.ffill().pct_change(fill_method=None)
    )
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


def add_spread_features(
    features: dict[str, pd.Series], df: pd.DataFrame, left_name: str,
    right_name: str, left_col: str, right_col: str,
) -> None:
    spread = df[left_col].sub(df[right_col])
    base = f"spread_{left_name}_{right_name}"
    features[base] = spread
    for window in (5, 20, 60, 120):
        ma = spread.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = spread.rolling(window, min_periods=max(2, window // 2)).std()
        features[f"{base}_z{window}"] = spread.sub(ma).div(sd.replace(0, np.nan))
        features[f"{base}_chg{window}"] = spread.sub(spread.shift(window))


def feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    features: dict[str, pd.Series] = {}
    if CONFIG.family == "t1" and CONFIG.style == "5y":
        add_tenor_features(features, df, "1Y", CONFIG.short_col or "", t1=True)
        add_tenor_features(features, df, "5Y", CONFIG.close_col, t1=True)
        add_tenor_features(features, df, "10Y", CONFIG.long_col or "", t1=True)
        add_spread_features(features, df, "5Y", "1Y", CONFIG.close_col, CONFIG.short_col or "")
        add_spread_features(features, df, "10Y", "5Y", CONFIG.long_col or "", CONFIG.close_col)
        add_spread_features(features, df, "10Y", "1Y", CONFIG.long_col or "", CONFIG.short_col or "")
    elif CONFIG.family == "t1":
        add_tenor_features(features, df, "1Y", CONFIG.short_col or "", t1=True)
        add_tenor_features(features, df, "5Y", CONFIG.mid_col or "", t1=True)
        add_tenor_features(features, df, "10Y", CONFIG.close_col, t1=True)
        add_spread_features(features, df, "10Y", "1Y", CONFIG.close_col, CONFIG.short_col or "")
        add_spread_features(features, df, "10Y", "5Y", CONFIG.close_col, CONFIG.mid_col or "")
        add_spread_features(features, df, "5Y", "1Y", CONFIG.mid_col or "", CONFIG.short_col or "")
    else:
        auxiliaries = {
            "3Y": (("1Y",), ("5Y",)),
            "5Y": (("3Y",), ("1Y",)),
            "7Y": (("5Y",), ("10Y",)),
            "10Y": (("7Y",), ("5Y",), ("1Y",)),
        }[CONFIG.tenor]
        pairs = [(CONFIG.tenor, CONFIG.close_col)]
        add_tenor_features(features, df, CONFIG.tenor, CONFIG.close_col, t1=False)
        for (tenor,) in auxiliaries:
            add_tenor_features(features, df, tenor, COL_MAP[tenor], t1=False)
            pairs.append((tenor, COL_MAP[tenor]))
        for left in range(len(pairs)):
            for right in range(left + 1, len(pairs)):
                left_name, left_col = pairs[left]
                right_name, right_col = pairs[right]
                add_spread_features(
                    features, df, left_name, right_name, left_col, right_col,
                )
    return pd.DataFrame(features, index=df.index).replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)


def fallback_signal(df: pd.DataFrame) -> pd.Series:
    close = df[CONFIG.close_col]
    returns = (
        close.pct_change(fill_method=None)
        if CONFIG.family == "t1"
        else close.ffill().pct_change(fill_method=None)
    )
    return -safe_sign(returns.shift(19))


def t1_vote_signals(df: pd.DataFrame) -> pd.DataFrame:
    if CONFIG.style == "5y":
        ten_year = df[CONFIG.long_col or ""]
        ten_ret = ten_year.pct_change(fill_method=None)
        five_year = df[CONFIG.close_col]
        one_year = df[CONFIG.short_col or ""]
        ratio = five_year.div(one_year).sub(1.0)
        mean = ratio.rolling(300, min_periods=150).mean()
        std = ratio.rolling(300, min_periods=150).std()
        signals = {
            "10Y_anti_lag11": -safe_sign(ten_ret.shift(10)),
            "ratio_5Y_1Y_z_anti300": -safe_sign(ratio.sub(mean).div(std.replace(0, np.nan))),
            "spread_10Y_5Y_mom_lag252": safe_sign(ten_year.sub(five_year).diff().shift(251)),
            "10Y_below_ma150": -safe_sign(ten_year.sub(ten_year.rolling(150, min_periods=75).mean())),
        }
    else:
        ten_ret = df[CONFIG.close_col].pct_change(fill_method=None)
        five_year = df[CONFIG.mid_col or ""]
        five_ret = five_year.pct_change(fill_method=None)
        spread_change = five_year.sub(df[CONFIG.short_col or ""]).diff()
        signals = {
            "5Y_anti_sum180": -safe_sign(five_ret.rolling(180).sum()),
            "spread_5Y_1Y_chg_anti_sum252": -safe_sign(spread_change.rolling(252).sum()),
            "5Y_mom_lag3": safe_sign(five_ret.shift(2)),
            "spread_5Y_1Y_chg_anti_sum10": -safe_sign(spread_change.rolling(10).sum()),
            "spread_5Y_1Y_chg_anti_sum40": -safe_sign(spread_change.rolling(40).sum()),
            "5Y_anti_lag252": -safe_sign(five_ret.shift(251)),
            "10Y_anti_sum180": -safe_sign(ten_ret.rolling(180).sum()),
        }
    return pd.DataFrame(signals, index=df.index)


def t5_vote_signals(df: pd.DataFrame) -> pd.DataFrame:
    def z_anti(tenor: str, window: int) -> pd.Series:
        close = df[COL_MAP[tenor]]
        return -safe_sign(close - close.rolling(window).mean())

    def spread_anti(left: str, right: str, window: int) -> pd.Series:
        return -safe_sign(df[COL_MAP[left]].sub(df[COL_MAP[right]]).diff().rolling(window).sum())

    if CONFIG.tenor == "3Y":
        signals = {
            "3Y_z_anti180": z_anti("3Y", 180),
            "3Y_z_anti252": z_anti("3Y", 252),
            "bf_z_anti120": -safe_sign(
                (2 * df[COL_MAP["3Y"]] - df[COL_MAP["1Y"]] - df[COL_MAP["5Y"]])
                - (2 * df[COL_MAP["3Y"]] - df[COL_MAP["1Y"]] - df[COL_MAP["5Y"]]).rolling(120).mean()
            ),
        }
    elif CONFIG.tenor == "5Y":
        close = df[COL_MAP["5Y"]]
        signals = {
            "5Y_z_anti90": z_anti("5Y", 90),
            "5Y_z_anti252": z_anti("5Y", 252),
            "5Y_ema_anti40": -safe_sign(close - close.ewm(span=40).mean()),
            "spr_5Y3Y_anti60": spread_anti("5Y", "3Y", 60),
            "spr_5Y10Y_anti20": spread_anti("5Y", "10Y", 20),
        }
    elif CONFIG.tenor == "7Y":
        close = df[COL_MAP["7Y"]]
        ret = close.ffill().pct_change(fill_method=None)
        high = (ret.rolling(20).std() > ret.rolling(60).std()).astype(int)
        butterfly = 2 * close - df[COL_MAP["5Y"]] - df[COL_MAP["10Y"]]
        signals = {
            "7Y_z_anti90": z_anti("7Y", 90),
            "7Y_z_highvol252": (z_anti("7Y", 252) * high).replace(0, -1).astype(int),
            "spr_7Y10Y_anti60": spread_anti("7Y", "10Y", 60),
            "bf_z_anti40": -safe_sign(butterfly - butterfly.rolling(40).mean()),
        }
    else:
        close = df[COL_MAP["10Y"]]
        butterfly = 2 * df[COL_MAP["7Y"]] - df[COL_MAP["5Y"]] - close
        signals = {
            "10Y_z_anti90": z_anti("10Y", 90),
            "10Y_z_anti120": z_anti("10Y", 120),
            "10Y_z_anti252": z_anti("10Y", 252),
            "10Y_mom_sum120": safe_sign(close.ffill().pct_change(fill_method=None).rolling(120).sum()),
            "bf7_z_anti60": -safe_sign(butterfly - butterfly.rolling(60).mean()),
        }
    return pd.DataFrame(signals, index=df.index)


def choose_threshold(probabilities: np.ndarray, labels: np.ndarray) -> float:
    if not len(probabilities):
        return 0.5
    best_score = -np.inf
    best_threshold = 0.5
    for threshold in np.linspace(0.38, 0.62, 49):
        predicted = np.where(probabilities >= threshold, 1, -1)
        accuracy = float((predicted == labels).mean())
        up = predicted == 1
        down = predicted == -1
        up_precision = ((up) & (labels == 1)).sum() / max(up.sum(), 1)
        down_precision = ((down) & (labels == -1)).sum() / max(down.sum(), 1)
        score = accuracy + 0.04 * min(float(up_precision), float(down_precision))
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


class Engine:
    """单次 CLI 内共享因果特征，只缓存不可变内存数据。"""

    def __init__(self, daily: pd.DataFrame) -> None:
        self.daily = daily
        self.positions = {
            value.strftime("%Y-%m-%d"): index for index, value in enumerate(daily["date"])
        }
        self.close = daily[CONFIG.close_col]
        horizon = 1 if CONFIG.family == "t1" else 5
        future_return = self.close.shift(-horizon).div(self.close).sub(1.0)
        self.labels = np.select(
            [future_return > 0.0, future_return < -0.0], [1, -1], default=0,
        ).astype(float)
        self.labels[future_return.isna()] = np.nan
        self.features = feature_matrix(daily)
        self.fallback = fallback_signal(daily)
        self.votes = t1_vote_signals(daily) if CONFIG.family == "t1" else t5_vote_signals(daily)

    def predict(self, request: dict[str, str]) -> int:
        idx = self.positions[request["feature_date"]]
        if idx < 20:
            raise ContractError("daily input has insufficient history for fallback")
        gap = 0 if CONFIG.family == "t1" else 5
        eligible = np.flatnonzero(
            (np.arange(len(self.daily)) < idx - gap)
            & np.isin(self.labels, [-1.0, 1.0])
            & self.close.notna().to_numpy()
        )
        if len(eligible) > CONFIG.window:
            eligible = eligible[-CONFIG.window:]
        if len(eligible) < min(120, CONFIG.window) or len(np.unique(self.labels[eligible])) < 2:
            base = int(self.fallback.iloc[idx])
        else:
            split = max(80, int(len(eligible) * CONFIG.split_pct))
            fit_idx = eligible[:split]
            cal_idx = eligible[split:]
            model = lgb.LGBMClassifier(
                objective="binary", metric="binary_logloss", num_leaves=CONFIG.num_leaves,
                learning_rate=CONFIG.learning_rate, n_estimators=CONFIG.n_estimators,
                min_child_samples=CONFIG.min_child_samples, reg_alpha=CONFIG.reg_alpha,
                reg_lambda=CONFIG.reg_lambda,
                subsample=0.95 if CONFIG.family == "t1" else 0.85,
                colsample_bytree=0.95 if CONFIG.family == "t1" else 0.90,
                random_state=42, n_jobs=1 if CONFIG.family == "t1" else 8,
                verbosity=-1, force_col_wise=CONFIG.family == "t5",
            )
            fit_kwargs: dict[str, Any] = {}
            if CONFIG.family == "t5":
                fit_kwargs = {
                    "eval_set": [(self.features.iloc[cal_idx], (self.labels[cal_idx] == 1).astype(int))],
                    "callbacks": [lgb.early_stopping(20, verbose=False)],
                }
            model.fit(
                self.features.iloc[fit_idx], (self.labels[fit_idx] == 1).astype(int),
                **fit_kwargs,
            )
            probability = float(model.predict_proba(self.features.iloc[[idx]])[:, 1][0])
            cal_probability = model.predict_proba(self.features.iloc[cal_idx])[:, 1]
            threshold = choose_threshold(cal_probability, self.labels[cal_idx])
            model_pred = 1 if probability >= threshold else -1
            if CONFIG.family == "t1" and abs(probability - threshold) <= CONFIG.epsilon:
                base = int(self.fallback.iloc[idx])
            else:
                base = model_pred
        signal_sum = int(self.votes.iloc[idx].sum())
        vote_sum = base + signal_sum
        if CONFIG.family == "t1" or CONFIG.vote_threshold == 0:
            return 1 if vote_sum >= 0 else -1
        if vote_sum > CONFIG.vote_threshold:
            return 1
        if vote_sum < -CONFIG.vote_threshold:
            return -1
        return int(base)


def write_atomic(output: Path, writer: Any) -> None:
    if not output.parent.is_dir():
        raise ContractError("output directory does not exist")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=output.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def emit(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    if args.command == "predict":
        write_atomic(
            args.output,
            lambda handle: json.dump(rows[0], handle, ensure_ascii=False, separators=(",", ":")),
        )
        return

    def writer(handle: Any) -> None:
        output = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
        output.writeheader()
        output.writerows(rows)

    write_atomic(args.output, writer)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="0529 daily Native successor")
    subcommands = result.add_subparsers(dest="command", required=True)
    for command, input_flag in (("predict", "--request"), ("backtest", "--requests")):
        item = subcommands.add_parser(command)
        item.add_argument(input_flag, required=True, type=Path)
        item.add_argument("--data-dir", required=True, type=Path)
        item.add_argument("--output", required=True, type=Path)
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    np.random.seed(42)
    try:
        args = parser().parse_args()
        requests = read_requests(args)
        daily = read_daily_and_validate_snapshot(args.data_dir, requests)
        engine = Engine(daily)
        rows = []
        for request in requests:
            direction = engine.predict(request)
            if type(direction) is not int or direction not in (-1, 0, 1):
                raise ContractError("predicted_direction must be integer -1, 0 or 1")
            rows.append(
                {field: request[field] for field in RESULT_FIELDS[:-1]}
                | {"predicted_direction": int(direction)}
            )
        emit(args, rows)
        logging.info("completed %s for %d request(s)", args.command, len(rows))
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected execution failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
