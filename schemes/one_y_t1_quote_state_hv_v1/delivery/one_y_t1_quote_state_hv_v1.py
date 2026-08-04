#!/usr/bin/env python3
"""Blackbox V2 1Y T+1 direction scheme.

核心方法：双 LightGBM（报价幅度、方向）按周滚动拟合；月内因果前缀配额控制出手；
高波动状态翻转方向。周频行按时间顺序在当周最后一个已观测日生效，随后前向填充。
固定参数：W=756，leaves=5，min-child=55，Q=.005，move=.45，margin=.06，
coverage=.50，10日/60日波动比翻转阈值=.70，random_state=20260729。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

SCHEME_ID = "one_y_t1_quote_state_hv_v1"
RANDOM_STATE = 20260729
np.random.seed(RANDOM_STATE)
DATE_FORMAT = "%Y-%m-%d"
SIX_DIGIT_KEY = re.compile(r"^\d{6}$")
REQUIRED_REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "predicted_direction",
)
TIME_KEY_BY_FILE = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
}
YIELD_COLUMNS = {"1Y": "TB1YWI0C", "3Y": "TB3YWI0C", "5Y": "TB5YWI0C", "7Y": "TB7YWI0C", "10Y": "TB0YWI0C"}
MARKET_FACTORS = ("DR007IBC", "USDCNH0C", "SH000300", "IFCFE00C", "S0031525", "AUSHF00C", "G0006352", "DRS00001")
WEEKLY_COLS = ("S0114089", "N1355677", "V0135838", "V0184553", "W0192843", "X0100205", "Y0110594", "HWW00001", "HWW00002", "HWW00003", "WC000002")
MODEL = {"window": 756, "num_leaves": 5, "min_child_samples": 55, "n_estimators": 120, "learning_rate": .025, "reg_alpha": .75, "reg_lambda": 2., "colsample_bytree": .75}
QUOTE_MOVE, MOVE_CUTOFF, DIRECTION_MARGIN, COVERAGE_FLOOR, VOL_FLIP_LEVEL = .005, .45, .06, .50, .70


class ContractError(ValueError):
    """A Contract 1.0 request or data snapshot is invalid."""


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)


def _clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(c).strip().lstrip("\ufeff") for c in frame.columns]
    return frame


def _validate_schema_header(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
    except (OSError, StopIteration, csv.Error) as exc:
        raise ContractError("cannot read %s header" % path.name) from exc
    expected_key = TIME_KEY_BY_FILE.get(path.name)
    header = [str(column).strip().lstrip("\ufeff") for column in header]
    if (
        expected_key is None
        or not header
        or header[0] != expected_key
        or len(header) != len(set(header))
    ):
        raise ContractError("%s does not match data-bridge-v1 schema" % path.name)


def read_snapshots(data_dir: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    paths = {"daily": data_dir / "daily_output.csv", "weekly": data_dir / "weekly_output.csv", "monthly": data_dir / "monthly_output.csv"}
    for path in paths.values():
        _validate_schema_header(path)
    try:
        daily = _clean_columns(pd.read_csv(paths["daily"]))
        weekly = _clean_columns(pd.read_csv(paths["weekly"], dtype={"week_id": str}))
        monthly = _clean_columns(pd.read_csv(paths["monthly"], dtype={"month_id": str}))
    except OSError as exc:
        raise ContractError("cannot read required data-bridge CSV files") from exc
    if "date" not in daily or "week_id" not in weekly or "month_id" not in monthly:
        raise ContractError("data-bridge time-key columns do not match schema")
    try:
        daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily_output.csv contains invalid dates") from exc
    weekly["week_id"] = weekly["week_id"].astype(str).str.zfill(6)
    monthly["month_id"] = monthly["month_id"].astype(str).str.zfill(6)
    for frame, key, name in ((daily, "date", "daily"), (weekly, "week_id", "weekly"), (monthly, "month_id", "monthly")):
        if frame.empty or frame[key].duplicated().any() or not frame[key].is_monotonic_increasing:
            raise ContractError("%s time keys must be non-empty, unique, and ascending" % name)
    return daily, weekly, monthly


def truncate_exact(frame: pd.DataFrame, key: str, cutoff: str, name: str) -> pd.DataFrame:
    values = frame[key].dt.strftime(DATE_FORMAT).to_numpy() if key == "date" else frame[key].astype(str).to_numpy()
    matched = np.flatnonzero(values == cutoff)
    if len(matched) != 1:
        raise ContractError("%s cutoff key must occur exactly once" % name)
    return frame.iloc[:int(matched[0]) + 1].copy()


def validate_date(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError("%s must be a YYYY-MM-DD string" % field)
    try:
        return datetime.strptime(value, DATE_FORMAT)
    except ValueError as exc:
        raise ContractError("%s must be a valid YYYY-MM-DD date" % field) from exc


def validate_request(record: Dict[str, Any]) -> Dict[str, str]:
    if set(record) != set(REQUIRED_REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    value = {key: record[key] for key in REQUIRED_REQUEST_FIELDS}
    if not all(isinstance(item, str) for item in value.values()) or not value["request_id"].strip():
        raise ContractError("all request fields must be strings and request_id non-empty")
    feature = validate_date(value["feature_date"], "feature_date")
    predict = validate_date(value["predict_date"], "predict_date")
    target = validate_date(value["target_date"], "target_date")
    cutoff = validate_date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not feature <= predict <= target or not feature < target:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    if cutoff.strftime(DATE_FORMAT) != value["daily_cutoff_key"]:
        raise ContractError("daily_cutoff_key must be normalized YYYY-MM-DD")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not SIX_DIGIT_KEY.fullmatch(value[field]):
            raise ContractError("%s must be a six-digit string" % field)
    return value


def read_single_request(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            decoded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("cannot read request JSON") from exc
    if not isinstance(decoded, dict):
        raise ContractError("request JSON must be an object")
    return validate_request(decoded)


def read_batch_requests(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(REQUIRED_REQUEST_FIELDS):
                raise ContractError("request CSV fields must exactly match Contract 1.0")
            records = [validate_request(row) for row in reader]
    except OSError as exc:
        raise ContractError("cannot read request CSV") from exc
    if not 1 <= len(records) <= 100:
        raise ContractError("request CSV must contain one to 100 rows")
    if len({row["request_id"] for row in records}) != len(records):
        raise ContractError("request_id values must be unique within a batch")
    return records


def _required_columns(frame: pd.DataFrame, names: Sequence[str], source: str) -> None:
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise ContractError("%s missing required columns: %s" % (source, ",".join(missing)))


def align_weekly(weekly: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Use chronological weekly rows; release a row on each calendar week's last observed daily date."""
    _required_columns(weekly, WEEKLY_COLS, "weekly_output.csv")
    values = weekly.loc[:, WEEKLY_COLS].apply(pd.to_numeric, errors="coerce")
    week_starts = dates - pd.to_timedelta(dates.weekday, unit="D")
    unique_weeks = pd.Index(week_starts).drop_duplicates()
    if len(values) < len(unique_weeks):
        raise ContractError("weekly history is shorter than daily trading-week history")
    selected = values.tail(len(unique_weeks)).reset_index(drop=True)
    position = pd.Series(np.arange(len(unique_weeks)), index=unique_weeks).reindex(week_starts).to_numpy(dtype=np.int64)
    last_positions = pd.Series(np.arange(len(dates))).groupby(position).max().to_numpy()
    result: Dict[str, np.ndarray] = {}
    for name in WEEKLY_COLS:
        placed = np.full(len(dates), np.nan, dtype=np.float64)
        placed[last_positions] = selected[name].to_numpy(dtype=np.float64)
        result[name] = pd.Series(placed).ffill().to_numpy()
    return pd.DataFrame(result)


def safe_series(frame: pd.DataFrame, name: str) -> pd.Series:
    return pd.to_numeric(frame[name], errors="coerce").ffill().bfill()


def build_features(daily: pd.DataFrame, weekly: pd.DataFrame) -> np.ndarray:
    _required_columns(daily, tuple(YIELD_COLUMNS.values()) + MARKET_FACTORS, "daily_output.csv")
    values: Dict[str, pd.Series] = {}
    yields = {name: safe_series(daily, column) for name, column in YIELD_COLUMNS.items()}
    for name, series in yields.items():
        for lag in (1, 5, 10, 20):
            values["yield_%s_diff%d" % (name, lag)] = series.diff(lag)
        for window in (20, 60, 120):
            std = series.rolling(window, min_periods=window).std().replace(0., np.nan)
            values["yield_%s_z%d" % (name, window)] = (series - series.rolling(window, min_periods=window).mean()) / std
    for name, left, right in (("curve_3_1", "3Y", "1Y"), ("curve_5_3", "5Y", "3Y"), ("curve_10_5", "10Y", "5Y"), ("curve_10_1", "10Y", "1Y")):
        spread = yields[left] - yields[right]
        values[name + "_level"] = spread
        for lag in (1, 5, 10, 20):
            values["%s_diff%d" % (name, lag)] = spread.diff(lag)
        values[name + "_z60"] = (spread - spread.rolling(60, min_periods=60).mean()) / spread.rolling(60, min_periods=60).std().replace(0., np.nan)
    belly = yields["3Y"] - .5 * (yields["1Y"] + yields["5Y"])
    for lag in (1, 5, 10, 20):
        values["belly_diff%d" % lag] = belly.diff(lag)
    for name in MARKET_FACTORS:
        series = safe_series(daily, name)
        ret1 = series.pct_change()
        for lag in (1, 5, 10, 20):
            values["market_%s_ret%d" % (name, lag)] = series.pct_change(lag)
        for window in (20, 60):
            values["market_%s_retz%d" % (name, window)] = (ret1 - ret1.rolling(window, min_periods=window).mean()) / ret1.rolling(window, min_periods=window).std().replace(0., np.nan)
    aligned = align_weekly(weekly, pd.DatetimeIndex(daily["date"]))
    for name in WEEKLY_COLS:
        values["weekly_" + name] = aligned[name]
        values["weekly_" + name + "_diff"] = aligned[name].diff()
    return pd.DataFrame(values).replace([np.inf, -np.inf], np.nan).fillna(0.).to_numpy(dtype=np.float32)


def classifier() -> LGBMClassifier:
    return LGBMClassifier(objective="binary", metric="binary_logloss", n_estimators=MODEL["n_estimators"], learning_rate=MODEL["learning_rate"], num_leaves=MODEL["num_leaves"], min_child_samples=MODEL["min_child_samples"], reg_alpha=MODEL["reg_alpha"], reg_lambda=MODEL["reg_lambda"], colsample_bytree=MODEL["colsample_bytree"], class_weight="balanced", random_state=RANDOM_STATE, n_jobs=1, verbosity=-1, deterministic=True, force_col_wise=True)


def probability_path(features: np.ndarray, labels: np.ndarray, weeks: np.ndarray, wanted: np.ndarray) -> np.ndarray:
    output = np.full(len(labels), np.nan, dtype=np.float64)
    all_rows = np.arange(len(labels))
    for week in pd.unique(weeks[wanted]):
        predict_idx = np.flatnonzero(wanted & (weeks == week))
        first = int(predict_idx[0])
        history = np.flatnonzero((all_rows < first - 1) & np.isfinite(labels))
        history = history[-MODEL["window"]:]
        if len(history) < 120 or len(np.unique(labels[history])) < 2:
            continue
        model = classifier()
        model.fit(features[history], labels[history].astype(np.int8))
        output[predict_idx] = model.predict_proba(features[predict_idx])[:, 1]
    return output


def predict_direction(record: Dict[str, str], snapshots: Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]) -> int:
    source_daily, source_weekly, source_monthly = snapshots
    daily = truncate_exact(source_daily, "date", record["daily_cutoff_key"], "daily")
    weekly = truncate_exact(source_weekly, "week_id", record["weekly_cutoff_key"], "weekly")
    _ = truncate_exact(source_monthly, "month_id", record["monthly_cutoff_key"], "monthly")
    dates = pd.DatetimeIndex(daily["date"])
    feature_date = pd.Timestamp(record["feature_date"])
    if feature_date != dates[-1]:
        raise ContractError("feature_date must equal daily_cutoff_key for this T+1 scheme")
    features = build_features(daily, weekly)
    close = safe_series(daily, "TB1YWI0C").to_numpy(dtype=np.float64)
    change = np.full(len(close), np.nan)
    change[:-1] = close[1:] - close[:-1]
    direction = np.where(np.isfinite(change) & (change != 0.), (change > 0.).astype(float), np.nan)
    move = np.where(np.isfinite(change), (np.abs(change) >= QUOTE_MOVE).astype(float), np.nan)
    next_dates = np.empty(len(dates), dtype="datetime64[ns]")
    next_dates[:-1] = dates[1:].to_numpy()
    next_dates[-1] = np.datetime64(pd.Timestamp(record["target_date"]))
    target_month = pd.PeriodIndex(next_dates, freq="M")
    wanted = np.asarray(target_month == target_month[-1])
    week_start = (dates - pd.to_timedelta(dates.weekday, unit="D")).asi8
    move_prob = probability_path(features, move, week_start, wanted)
    direction_prob = probability_path(features, direction, week_start, wanted)
    prediction = np.zeros(len(dates), dtype=np.int8)
    primary = wanted & np.isfinite(move_prob) & np.isfinite(direction_prob) & (move_prob >= MOVE_CUTOFF)
    prediction[primary & (direction_prob >= .5 + DIRECTION_MARGIN)] = 1
    prediction[primary & (direction_prob <= .5 - DIRECTION_MARGIN)] = -1
    issued = 0
    for index in np.flatnonzero(wanted):
        if prediction[index] != 0:
            issued += 1
            continue
        required = int(np.ceil((np.flatnonzero(wanted == True).tolist().index(index) + 1) * COVERAGE_FLOOR))
        if issued < required and np.isfinite(direction_prob[index]):
            prediction[index] = 1 if direction_prob[index] >= .5 else -1
            issued += 1
    delta = pd.Series(close).diff()
    vol = (delta.rolling(10, min_periods=10).std(ddof=0) / delta.rolling(60, min_periods=30).std(ddof=0).replace(0., np.nan)).to_numpy()
    prediction[vol > VOL_FLIP_LEVEL] *= -1
    return int(prediction[-1])


def make_result(record: Dict[str, str], direction: int) -> Dict[str, Any]:
    return {"request_id": record["request_id"], "predict_date": record["predict_date"], "feature_date": record["feature_date"], "target_date": record["target_date"], "predicted_direction": int(direction)}


def atomic_write(output: Path, writer: Any) -> None:
    if not output.parent.is_dir():
        raise ContractError("output directory does not exist")
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=str(output.parent), delete=False) as handle:
            temp = Path(handle.name)
            writer(handle)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(str(temp), str(output))
    except Exception:
        if temp is not None and temp.exists():
            temp.unlink()
        raise


def write_json(output: Path, result: Dict[str, Any]) -> None:
    atomic_write(output, lambda handle: json.dump(result, handle, ensure_ascii=False, separators=(",", ":")))


def write_csv(output: Path, results: Sequence[Dict[str, Any]]) -> None:
    def writer(handle: Any) -> None:
        csv_writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
        csv_writer.writeheader(); csv_writer.writerows(results)
    atomic_write(output, writer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1Y T+1 Blackbox V2 prediction scheme")
    subs = parser.add_subparsers(dest="command", required=True)
    for name, arg in (("predict", "--request"), ("backtest", "--requests")):
        command = subs.add_parser(name)
        command.add_argument(arg, required=True, type=Path)
        command.add_argument("--data-dir", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    configure_logging()
    try:
        args = build_parser().parse_args()
        records = [read_single_request(args.request)] if args.command == "predict" else read_batch_requests(args.requests)
        snapshots = read_snapshots(args.data_dir)
        cache: Dict[Tuple[str, str, str, str, str], int] = {}
        results = []
        for record in records:
            key = tuple(record[name] for name in ("feature_date", "target_date", "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key"))
            if key not in cache:
                cache[key] = predict_direction(record, snapshots)
            results.append(make_result(record, cache[key]))
        if args.command == "predict": write_json(args.output, results[0])
        else: write_csv(args.output, results)
        logging.info("completed %s for %d request(s)", args.command, len(results))
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc); return 1
    except Exception:
        logging.exception("unexpected execution failure"); return 1


if __name__ == "__main__":
    sys.exit(main())
