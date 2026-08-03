#!/usr/bin/env python3
"""Blackbox V2 Contract 1.0: 7Y T+1, monthly-refit LightGBM.

The process-local caches make a backtest batch incremental: models are fitted once
per cutoff/month and then reused for every request that needs the same state.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier


RANDOM_STATE = 42
TARGET = "TB7YWI0C"
IS_LONG_WINDOW = Path(__file__).stem.endswith("002_v1")
THRESHOLD = 0.52 if IS_LONG_WINDOW else 0.55
BAND = 0.06
FLIP_BELOW = 0.50 if IS_LONG_WINDOW else 0.30
FLIP_MIN_TRADES = 8
FLIP_LOOKBACK = 3 if IS_LONG_WINDOW else 1
TRAIN_WINDOW = 1008 if IS_LONG_WINDOW else 756
FEATURE_WINDOW = TRAIN_WINDOW
TOP_FEATURES = 60
REQUEST_FIELDS = ("request_id", "predict_date", "feature_date", "target_date", "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key")
RESULT_FIELDS = ("request_id", "predict_date", "feature_date", "target_date", "predicted_direction")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^\d{6}$")
WEEKLY_COLS = ("S0114089", "N1355677", "V0135838", "V0184553", "W0192843", "X0100205", "Y0110594", "HWW00001", "HWW00002", "HWW00003")
MONTHLY_COLS = ("M0000545", "M0041340", "M0041341", "M0041342", "M0061518", "M0096870", "M0317126", "M0009970", "M0009973", "M0001227")
MARKET_GROUPS = {
    "money_market": ("DR007IBC", "DR007IB0", "DRS00001", "DRS00002"),
    "equity": ("SH000300", "CSI26901", "CSI39501", "IFCFE00C"),
    "fx": ("USDCNH0C", "USDCNH00", "USDCNH0H", "USDCNH0L"),
    "commodity": ("S0031525", "AGSHF01C", "AUSHF00C", "ZNSHF00C"),
    "credit": ("G0006352", "G0006353", "G0266632", "G0266641"),
    "swap": ("DRS00001", "DRS00002", "SWR00001", "SWR00002"),
    "macro": ("M0000005", "M0000271", "M0048486", "G0003956"),
}


class ContractError(ValueError):
    pass


def _date(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ContractError(f"{field} must be YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid date") from exc


def validate_request(record: dict[str, Any]) -> dict[str, str]:
    if set(record) != set(REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    if not all(isinstance(record[field], str) for field in REQUEST_FIELDS):
        raise ContractError("all request fields must be strings")
    value = {field: record[field] for field in REQUEST_FIELDS}
    if not value["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature, predict, target = (_date(value[name], name) for name in ("feature_date", "predict_date", "target_date"))
    _date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not feature <= predict <= target or not feature < target:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    if not all(KEY_RE.fullmatch(value[name]) for name in ("weekly_cutoff_key", "monthly_cutoff_key")):
        raise ContractError("weekly/monthly cutoff keys must be six-digit strings")
    return value


def read_requests(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.command == "predict":
        try:
            with args.request.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("cannot read request JSON") from exc
        if not isinstance(raw, dict):
            raise ContractError("request JSON must be an object")
        return [validate_request(raw)]
    try:
        with args.requests.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(REQUEST_FIELDS):
                raise ContractError("request CSV fields must exactly match Contract 1.0")
            records = [validate_request(row) for row in reader]
    except OSError as exc:
        raise ContractError("cannot read request CSV") from exc
    if not 1 <= len(records) <= 100:
        raise ContractError("request CSV must contain one to 100 rows")
    if len({row["request_id"] for row in records}) != len(records):
        raise ContractError("request_id values must be unique within a batch")
    return records


@dataclass(frozen=True)
class Snapshot:
    daily: pd.DataFrame
    weekly: pd.DataFrame
    monthly: pd.DataFrame
    week_map: pd.DataFrame


def validate_period_keys(frame: pd.DataFrame, column: str) -> None:
    """校验 DataBridge 周/月时间键，不将其解释为连续数值。"""
    keys = frame[column].astype("string")
    if (
        keys.isna().any()
        or keys.str.strip().eq("").any()
        or not keys.str.fullmatch(KEY_RE).fillna(False).all()
    ):
        raise ContractError(f"{column} values must be non-empty six-digit strings")
    if keys.duplicated().any() or not keys.is_monotonic_increasing:
        raise ContractError(f"{column} values must be unique and strictly ascending")


def read_snapshot(data_dir: Path) -> Snapshot:
    paths = {name: data_dir / f"{name}_output.csv" for name in ("daily", "weekly", "monthly")}
    week_map_path = data_dir / "api_wind_date.csv"
    if any(not path.is_file() for path in paths.values()) or not week_map_path.is_file():
        raise ContractError("data-dir must contain daily/weekly/monthly_output.csv and api_wind_date.csv")
    try:
        daily = pd.read_csv(paths["daily"])
        weekly = pd.read_csv(paths["weekly"], dtype={"week_id": "string"})
        monthly = pd.read_csv(paths["monthly"], dtype={"month_id": "string"})
    except (OSError, ValueError) as exc:
        raise ContractError("cannot read data-bridge CSV files") from exc
    if "date" not in daily or TARGET not in daily or "week_id" not in weekly or "month_id" not in monthly:
        raise ContractError("data-bridge CSV is missing required keys or 7Y yield")
    try:
        daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily date key is invalid") from exc
    if daily.empty or daily["date"].duplicated().any() or not daily["date"].is_monotonic_increasing:
        raise ContractError("daily date keys must be non-empty, unique, and ascending")
    validate_period_keys(weekly, "week_id")
    validate_period_keys(monthly, "month_id")
    try:
        week_map = pd.read_csv(
            week_map_path,
            dtype={"rdate": "string", "week_id": "string"},
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except (OSError, UnicodeError, ValueError, pd.errors.ParserError) as exc:
        raise ContractError("cannot read api_wind_date.csv") from exc
    if week_map.empty or week_map.columns.tolist() != ["rdate", "week_id"]:
        raise ContractError("api_wind_date.csv must have non-empty rdate,week_id rows")
    raw_rdates = week_map["rdate"]
    week_ids = week_map["week_id"]
    if raw_rdates.str.strip().eq("").any():
        raise ContractError("api_wind_date.csv rdate values must be non-blank")
    try:
        rdates = pd.to_datetime(raw_rdates, format="%Y-%m-%d", errors="raise")
    except (TypeError, ValueError) as exc:
        raise ContractError("api_wind_date.csv rdate values must be valid dates") from exc
    if not rdates.dt.strftime("%Y-%m-%d").eq(raw_rdates).all():
        raise ContractError("api_wind_date.csv rdate values must be strict YYYY-MM-DD")
    if rdates.duplicated().any() or not rdates.is_monotonic_increasing:
        raise ContractError("api_wind_date.csv rdate values must be unique and strictly ascending")
    if week_ids.str.strip().eq("").any() or not week_ids.str.fullmatch(KEY_RE).all():
        raise ContractError("api_wind_date.csv week_id values must be six-digit strings")
    week_map["rdate"] = rdates.dt.normalize()
    return Snapshot(daily, weekly, monthly, week_map)


def clipped(snapshot: Snapshot, request: dict[str, str]) -> Snapshot:
    if request["feature_date"] != request["daily_cutoff_key"]:
        raise ContractError("feature_date must equal daily cutoff key")
    daily_cutoff = pd.Timestamp(request["daily_cutoff_key"])
    daily_matches = np.flatnonzero(snapshot.daily["date"].eq(daily_cutoff))
    weekly_matches = np.flatnonzero(snapshot.weekly["week_id"].astype("string").eq(request["weekly_cutoff_key"]))
    monthly_matches = np.flatnonzero(snapshot.monthly["month_id"].astype("string").eq(request["monthly_cutoff_key"]))
    if len(daily_matches) != 1:
        raise ContractError("daily cutoff key must occur exactly once")
    if len(weekly_matches) != 1:
        raise ContractError("weekly cutoff key must occur exactly once")
    if len(monthly_matches) != 1:
        raise ContractError("monthly cutoff key must occur exactly once")
    week_map = snapshot.week_map
    if week_map is None:
        raise ContractError("api_wind_date.csv mapping is required")
    map_matches = np.flatnonzero(week_map["rdate"].eq(daily_cutoff))
    if len(map_matches) != 1:
        raise ContractError("daily cutoff key must have exactly one calendar mapping")
    if str(week_map.iloc[int(map_matches[0])]["week_id"]) != request["weekly_cutoff_key"]:
        raise ContractError("daily cutoff calendar mapping must match weekly cutoff key")
    daily = snapshot.daily.iloc[: int(daily_matches[0]) + 1].copy().reset_index(drop=True)
    weekly = snapshot.weekly.iloc[: int(weekly_matches[0]) + 1].copy().reset_index(drop=True)
    monthly = snapshot.monthly.iloc[: int(monthly_matches[0]) + 1].copy().reset_index(drop=True)
    if request["feature_date"] not in set(daily["date"].dt.strftime("%Y-%m-%d")):
        raise ContractError("feature_date must occur within daily cutoff")
    week_map = week_map.iloc[: int(map_matches[0]) + 1].copy().reset_index(drop=True)
    return Snapshot(daily, weekly, monthly, week_map)


def add_price_features(out: dict[str, np.ndarray], categories: dict[str, str], prefix: str, values: pd.Series, category: str) -> None:
    level = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    ma = level.rolling(20, min_periods=10).mean()
    sd = level.rolling(20, min_periods=10).std().replace(0, np.nan)
    series_map = {
        "level": level, "r1": level.pct_change(), "r5": level.pct_change(5), "r20": level.pct_change(20),
        "d1": level.diff(), "d5": level.diff(5), "d20": level.diff(20), "z20": (level - ma) / sd,
        "mom5": level.pct_change().rolling(5, min_periods=3).sum(),
    }
    for suffix, series in series_map.items():
        name = f"{prefix}_{suffix}"
        out[name] = series.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy("float32")
        categories[name] = category


def weekly_to_daily(daily: pd.DataFrame, weekly: pd.DataFrame, week_map: pd.DataFrame) -> pd.DataFrame:
    if week_map is None:
        raise ContractError("api_wind_date.csv mapping is required")
    calendar = week_map.set_index("rdate")["week_id"]
    daily_keys = daily["date"].map(calendar).astype("string")
    if daily_keys.isna().any():
        raise ContractError("every daily row must have a calendar week mapping")
    values = weekly.copy()
    values["week_id"] = values["week_id"].astype(str)
    values = values.set_index("week_id")
    if not values.index.is_unique:
        raise ContractError("weekly week_id values must be unique")
    if not daily_keys.isin(values.index).all():
        raise ContractError("every calendar week_id must occur in the clipped weekly data")
    cols = [name for name in WEEKLY_COLS if name in weekly]
    if not cols:
        return pd.DataFrame(index=daily.index)
    last_of_week = daily_keys.ne(daily_keys.shift(-1)).fillna(True)
    result = pd.DataFrame(index=daily.index)
    for col in cols:
        mapped = daily_keys.map(pd.to_numeric(values[col], errors="coerce"))
        placed = mapped.where(last_of_week)
        result[col] = placed.ffill()
    return result


def make_features(snapshot: Snapshot) -> tuple[pd.DataFrame, dict[str, str]]:
    daily, weekly, monthly = snapshot.daily, snapshot.weekly, snapshot.monthly
    out: dict[str, np.ndarray] = {}
    categories: dict[str, str] = {}
    lag = lambda col: pd.to_numeric(daily[col], errors="coerce").ffill().shift(1)
    add_price_features(out, categories, "self_close", lag(TARGET), "self")
    curve_cols = {"1y": "TB1YWI0C", "3y": "TB3YWI0C", "5y": "TB5YWI0C", "7y": TARGET, "10y": "TB0YWI0C"}
    curve = {name: lag(col) for name, col in curve_cols.items() if col in daily}
    close = curve["7y"]
    for long_tenor, short_tenor in (("10y", "1y"), ("10y", "5y"), ("10y", "7y"), ("7y", "3y"), ("5y", "1y"), ("5y", "3y")):
        if long_tenor in curve and short_tenor in curve:
            add_price_features(out, categories, f"ts_{long_tenor}_{short_tenor}", curve[long_tenor] - curve[short_tenor], "term_structure")
    for long_tenor, belly_tenor, short_tenor in (("10y", "5y", "1y"), ("10y", "7y", "3y"), ("7y", "5y", "3y")):
        if all(name in curve for name in (long_tenor, belly_tenor, short_tenor)):
            add_price_features(out, categories, f"ts_bfly_{long_tenor}_{belly_tenor}_{short_tenor}", curve[long_tenor] + curve[short_tenor] - 2.0 * curve[belly_tenor], "term_structure")
    for name in ("5y", "10y", "3y"):
        if name in curve:
            add_price_features(out, categories, f"curve_{name}", curve[name], "curve")
            add_price_features(out, categories, f"spread_{name}", close - curve[name], "curve")
    for category, names in MARKET_GROUPS.items():
        for name in names:
            if name in daily:
                add_price_features(out, categories, f"mf_{name}", lag(name), category)
    wk = weekly_to_daily(daily, weekly, snapshot.week_map)
    for name in wk:
        add_price_features(out, categories, f"wk_{name}", wk[name].ffill().shift(1), "weekly")
    month_values = monthly.set_index(monthly["month_id"].astype(str))
    previous = (daily["date"].dt.to_period("M") - 1).astype(str).str.replace("-", "", regex=False)
    for name in (col for col in MONTHLY_COLS if col in monthly):
        mapped = previous.map(pd.to_numeric(month_values[name], errors="coerce"))
        add_price_features(out, categories, f"mo_{name}", mapped.ffill().shift(1), "monthly")
    return pd.DataFrame(out).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype("float32"), categories


def auc_scores(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    scores = np.zeros(x.shape[1], dtype="float64")
    for idx in range(x.shape[1]):
        value = x[:, idx]
        valid = np.isfinite(value)
        if int(valid.sum()) < 50:
            continue
        observed = value[valid]
        target = (y[valid] > 0).astype("int32")
        p, n = int(target.sum()), int((1 - target).sum())
        if p < 5 or n < 5:
            continue
        order = np.argsort(observed, kind="mergesort")
        ranks = np.empty(len(observed), dtype="float64")
        left = 0
        while left < len(order):
            right = left + 1
            while right < len(order) and observed[order[right]] == observed[order[left]]:
                right += 1
            ranks[order[left:right]] = (left + 1 + right) / 2.0
            left = right
        auc = (ranks[target == 1].sum() - p * (p + 1) / 2.0) / (p * n)
        scores[idx] = abs(float(auc) - 0.5)
    return scores


class Engine:
    def __init__(self, snapshot: Snapshot, cache_dir: Path) -> None:
        self.daily = snapshot.daily
        self.dates = pd.DatetimeIndex(self.daily["date"])
        self.positions = {date.strftime("%Y-%m-%d"): idx for idx, date in enumerate(self.dates)}
        self.x_frame, self.category = make_features(snapshot)
        self.x = self.x_frame.to_numpy("float32")
        close = pd.to_numeric(self.daily[TARGET], errors="coerce").ffill()
        self.truth = np.sign(close.pct_change()).to_numpy("float64")
        self.target_by_pred = pd.Series(self.truth).shift(-1).to_numpy("float64")
        self.valid_real = np.isin(self.truth, [-1.0, 1.0])
        self.names = list(self.x_frame.columns)
        self.models: dict[str, tuple[LGBMClassifier, np.ndarray]] = {}
        self.model_fingerprints: dict[str, str] = {}
        self.selected_by_year: dict[int, np.ndarray] = {}
        self.raw_cache: dict[int, int] = {}
        self.cache_file = cache_dir / f"{Path(__file__).stem}.pkl"
        self._load_cached_models()

    def _load_cached_models(self) -> None:
        if not self.cache_file.is_file():
            return
        try:
            with self.cache_file.open("rb") as handle:
                payload = pickle.load(handle)
            if payload.get("feature_names") == self.names and payload.get("version") == 3:
                for month, model in payload.get("models", {}).items():
                    if payload.get("fingerprints", {}).get(month) == self._model_fingerprint(month):
                        self.models[month] = model
                        self.model_fingerprints[month] = payload["fingerprints"][month]
        except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ValueError):
            logging.warning("ignoring unreadable model cache: %s", self.cache_file)

    def persist_models(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=self.cache_file.parent, delete=False) as handle:
                temporary = Path(handle.name)
                pickle.dump({"version": 3, "feature_names": self.names, "models": self.models, "fingerprints": self.model_fingerprints}, handle, protocol=pickle.HIGHEST_PROTOCOL)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.cache_file)
        except OSError as exc:
            raise ContractError("cannot persist incremental model cache") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)

    def _select_features(self, real_idx: int) -> np.ndarray:
        year = int(self.dates[real_idx].year)
        if year in self.selected_by_year:
            return self.selected_by_year[year]
        same_year = np.flatnonzero(
            (self.dates.year == year) & (self.dates >= pd.Timestamp("2024-01-01")) & self.valid_real
        )
        first_real = int(same_year[0]) if len(same_year) else int(real_idx)
        train_end = first_real - 2
        eligible = np.arange(max(0, train_end + 1))
        eligible = eligible[np.isin(self.target_by_pred[eligible], [-1.0, 1.0])]
        screen = eligible[-FEATURE_WINDOW:]
        scores = auc_scores(self.x[screen], self.target_by_pred[screen])
        order = np.argsort(-scores, kind="mergesort")
        selected: list[int] = []
        seen: set[str] = set()
        for idx in order:
            group = self.category.get(self.names[int(idx)], "other")
            if scores[idx] > 0 and group not in {"self", "curve", "weekly", "monthly"} and group not in seen:
                selected.append(int(idx)); seen.add(group)
            if len(seen) >= 6:
                break
        for idx in order:
            if len(selected) >= TOP_FEATURES:
                break
            if scores[idx] > 0 and int(idx) not in selected:
                selected.append(int(idx))
        chosen = np.array((selected or order[:TOP_FEATURES].tolist())[:TOP_FEATURES], dtype="int32")
        self.selected_by_year[year] = chosen
        return chosen

    def _model_fingerprint(self, month: str) -> str:
        month_mask = (
            (self.dates.strftime("%Y-%m") == month)
            & (self.dates >= pd.Timestamp("2024-01-01")) & self.valid_real
        )
        positions = np.flatnonzero(month_mask)
        if not len(positions):
            return ""
        train_end = int(positions[0]) - 2
        digest = hashlib.sha256()
        digest.update(month.encode("ascii"))
        digest.update(self.x[: train_end + 1].tobytes())
        digest.update(self.target_by_pred[: train_end + 1].tobytes())
        return digest.hexdigest()

    def _model(self, real_idx: int) -> tuple[LGBMClassifier, np.ndarray] | None:
        month = self.dates[real_idx].strftime("%Y-%m")
        if month in self.models:
            return self.models[month]
        first_real = int(np.flatnonzero(
            (self.dates.to_period("M") == self.dates[real_idx].to_period("M"))
            & (self.dates >= pd.Timestamp("2024-01-01")) & self.valid_real
        )[0])
        train_end = first_real - 2
        eligible = np.arange(max(0, train_end + 1))
        eligible = eligible[np.isin(self.target_by_pred[eligible], [-1.0, 1.0])]
        if len(eligible) > TRAIN_WINDOW:
            eligible = eligible[-TRAIN_WINDOW:]
        if len(eligible) < 120 or len(np.unique(self.target_by_pred[eligible])) < 2:
            return None
        chosen = self._select_features(real_idx)
        model = LGBMClassifier(objective="binary", metric="binary_logloss", num_leaves=5, min_child_samples=20,
            learning_rate=0.02, n_estimators=200, reg_alpha=0.0, reg_lambda=1.0, subsample=0.90,
            colsample_bytree=0.90, class_weight="balanced", n_jobs=1, verbosity=-1, random_state=RANDOM_STATE,
            force_col_wise=True)
        model.fit(self.x[eligible][:, chosen], (self.target_by_pred[eligible] > 0).astype("int8"))
        self.models[month] = (model, chosen)
        self.model_fingerprints[month] = self._model_fingerprint(month)
        return self.models[month]

    def raw_signal(self, real_idx: int) -> int:
        if real_idx in self.raw_cache:
            return self.raw_cache[real_idx]
        if self.dates[real_idx] < pd.Timestamp("2024-01-01"):
            self.raw_cache[real_idx] = 0
            return 0
        if not self.valid_real[real_idx]:
            self.raw_cache[real_idx] = 0
            return 0
        state = self._model(real_idx)
        if state is None or real_idx < 1:
            self.raw_cache[real_idx] = 0
            return 0
        model, selected = state
        probability = float(model.predict_proba(self.x[[real_idx - 1]][:, selected])[0, 1])
        signal = 0 if abs(probability - THRESHOLD) < BAND else (1 if probability >= THRESHOLD else -1)
        self.raw_cache[real_idx] = signal
        return signal

    def predict(self, request: dict[str, str]) -> int:
        feature_idx = self.positions[request["feature_date"]]
        real_idx = feature_idx + 1
        if real_idx >= len(self.dates):
            raise ContractError("feature_date has no next trading-day model index")
        signal = self.raw_signal(real_idx)
        current = self.dates[real_idx].to_period("M")
        prior = current - FLIP_LOOKBACK
        mask = self.dates.to_period("M") == prior
        historical = np.flatnonzero(mask)
        signals = np.array([self.raw_signal(int(index)) for index in historical], dtype="int8")
        labels = self.truth[historical]
        traded = (signals != 0) & np.isin(labels, [-1.0, 1.0])
        if int(traded.sum()) >= FLIP_MIN_TRADES and float((signals[traded] == labels[traded]).mean()) < FLIP_BELOW:
            signal = -signal
        return int(signal)


def write_atomic(output: Path, write: Any) -> None:
    if not output.parent.is_dir():
        raise ContractError("output directory does not exist")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name); write(handle); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def emit(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    if args.command == "predict":
        write_atomic(args.output, lambda handle: json.dump(rows[0], handle, ensure_ascii=False, separators=(",", ":")))
    else:
        def writer(handle: Any) -> None:
            output = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
            output.writeheader(); output.writerows(rows)
        write_atomic(args.output, writer)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="7Y T+1 incremental monthly-refit LightGBM")
    sub = result.add_subparsers(dest="command", required=True)
    for command, argument in (("predict", "--request"), ("backtest", "--requests")):
        item = sub.add_parser(command)
        item.add_argument(argument, required=True, type=Path)
        item.add_argument("--data-dir", required=True, type=Path)
        item.add_argument("--output", required=True, type=Path)
        item.add_argument("--cache-dir", type=Path, help="persistent incremental model-cache directory")
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    np.random.seed(RANDOM_STATE)
    try:
        args = parser().parse_args(); records = read_requests(args); snapshot = read_snapshot(args.data_dir)
        cache_dir = args.cache_dir or (args.output.parent / ".blackbox_model_cache")
        engines: dict[tuple[str, str, str], Engine] = {}; rows: list[dict[str, Any]] = []
        for record in records:
            key = (record["daily_cutoff_key"], record["weekly_cutoff_key"], record["monthly_cutoff_key"])
            if key not in engines:
                engines[key] = Engine(clipped(snapshot, record), cache_dir)
            direction = engines[key].predict(record)
            engines[key].persist_models()
            rows.append({name: record[name] for name in RESULT_FIELDS[:-1]} | {"predicted_direction": direction})
        emit(args, rows); logging.info("completed %s for %d request(s)", args.command, len(rows)); return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc); return 1
    except Exception:
        logging.exception("unexpected execution failure"); return 1


if __name__ == "__main__":
    sys.exit(main())
