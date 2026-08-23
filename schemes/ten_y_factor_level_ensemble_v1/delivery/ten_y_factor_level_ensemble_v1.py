#!/usr/bin/env python3
"""Blackbox V2 Contract 1.0: fixed 10Y T+1 factor-level ensemble."""
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
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from xgboost import XGBRegressor


SEED = 20260821
SIM_START = pd.Timestamp("2025-07-01")
TRAIN_WINDOW = 1008
SIGNAL_THRESHOLD = 0.0015860000276006758

REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "predicted_direction",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^\d{6}$")
DAILY_COLUMNS = ("date", "TB3YWI0C", "TB5YWI0C", "TB0YWI0C")

LEVEL_FEATURES = (
    "bond_3Y_z10", "bond_10Y_z5", "bond_3Y_z15", "bond_10Y_z10",
    "rv_10Y_d1", "bond_3Y_z3", "bond_3Y_z5", "bond_3Y_ret_lag1",
    "bond_3Y_sign_lag1", "bond_3Y_mom_sign5", "bond_10Y_ret_lag1",
    "bond_10Y_z3", "bond_3Y_z20", "bond_10Y_z15", "bond_3Y_vol180",
    "bond_3Y_mom_sum5", "bond_3Y_z2", "bond_10Y_sign_lag1",
    "bond_5Y_sign_lag1", "bond_10Y_z2", "bond_10Y_z20", "bond_10Y_z60",
    "rv_10Y_d2", "bond_10Y_vol10", "bond_3Y_z40", "bond_10Y_mom_sum2",
    "bond_10Y_vol90", "bond_5Y_z2", "rv_10Y_d5", "bond_10Y_mom_sum5",
)
LOG = logging.getLogger("ten_y_factor_level_ensemble_v1")


class ContractError(ValueError):
    pass


def parse_date(value: str, field: str) -> datetime:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
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
    request = {field: record[field] for field in REQUEST_FIELDS}
    if not request["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature = parse_date(request["feature_date"], "feature_date")
    predict = parse_date(request["predict_date"], "predict_date")
    target = parse_date(request["target_date"], "target_date")
    parse_date(request["daily_cutoff_key"], "daily_cutoff_key")
    if not feature <= predict <= target or not feature < target:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    if any(KEY_RE.fullmatch(request[field]) is None for field in ("weekly_cutoff_key", "monthly_cutoff_key")):
        raise ContractError("weekly/monthly cutoff keys must be six-digit strings")
    return request


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
            records = [validate_request(dict(row)) for row in reader]
    except OSError as exc:
        raise ContractError("cannot read request CSV") from exc
    if not 1 <= len(records) <= 100:
        raise ContractError("request CSV must contain one to 100 rows")
    if len({row["request_id"] for row in records}) != len(records):
        raise ContractError("request_id values must be unique within a batch")
    return records


def normalized_columns(path: Path) -> list[str]:
    try:
        columns = list(pd.read_csv(path, nrows=0).columns)
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read {path.name}") from exc
    return [str(column).strip().lstrip("\ufeff") for column in columns]


def read_inputs(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily_path = data_dir / "daily_output.csv"
    calendar_path = data_dir / "api_wind_date.csv"
    for path in (daily_path, calendar_path):
        if not path.is_file():
            raise ContractError(f"missing consumed input: {path.name}")
    daily_header = normalized_columns(daily_path)
    missing = [column for column in DAILY_COLUMNS if column not in daily_header]
    if missing:
        raise ContractError(f"daily_output.csv missing required columns: {missing}")
    try:
        daily = pd.read_csv(
            daily_path,
            usecols=lambda column: str(column).strip().lstrip("\ufeff") in DAILY_COLUMNS,
            dtype={"date": "string"},
            low_memory=False,
        )
        daily.columns = [str(column).strip().lstrip("\ufeff") for column in daily.columns]
        calendar = pd.read_csv(calendar_path, dtype={"rdate": "string", "week_id": "string"})
        calendar.columns = [str(column).strip().lstrip("\ufeff") for column in calendar.columns]
    except (OSError, ValueError) as exc:
        raise ContractError("cannot read consumed inputs") from exc
    if not {"rdate", "week_id"}.issubset(calendar.columns):
        raise ContractError("api_wind_date.csv must contain rdate and week_id")
    try:
        daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
        calendar["rdate"] = pd.to_datetime(calendar["rdate"], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily/calendar date key is invalid") from exc
    if daily.empty or daily["date"].duplicated().any() or not daily["date"].is_monotonic_increasing:
        raise ContractError("daily date keys must be non-empty, unique, and ascending")
    if calendar.empty or calendar["rdate"].duplicated().any() or not calendar["rdate"].is_monotonic_increasing:
        raise ContractError("calendar date keys must be non-empty, unique, and ascending")
    calendar["week_id"] = calendar["week_id"].astype("string")
    if calendar["week_id"].isna().any() or not calendar["week_id"].map(lambda value: bool(KEY_RE.fullmatch(str(value)))).all():
        raise ContractError("calendar week_id must be six-digit strings")
    for column in DAILY_COLUMNS[1:]:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
    return daily, calendar[["rdate", "week_id"]]


def truncate_for_request(
    daily: pd.DataFrame,
    calendar: pd.DataFrame,
    request: dict[str, str],
) -> pd.DataFrame:
    cutoff = pd.Timestamp(request["daily_cutoff_key"])
    if int((daily["date"] == cutoff).sum()) != 1:
        raise ContractError("daily_cutoff_key must occur exactly once in daily_output.csv")
    mapping = calendar.loc[calendar["rdate"] == cutoff, "week_id"]
    if len(mapping) != 1 or str(mapping.iloc[0]) != request["weekly_cutoff_key"]:
        raise ContractError("daily cutoff does not map exactly to weekly_cutoff_key")
    clipped = daily.loc[daily["date"] <= cutoff].copy().reset_index(drop=True)
    feature_date = pd.Timestamp(request["feature_date"])
    if int((clipped["date"] == feature_date).sum()) != 1:
        raise ContractError("feature_date must occur exactly once within daily cutoff")
    # Later rows allowed by the Request cutoff cannot influence an earlier feature date.
    clipped = clipped.loc[clipped["date"] <= feature_date].copy().reset_index(drop=True)
    if feature_date < SIM_START:
        raise ContractError("feature_date is earlier than the frozen strategy start")
    if clipped[list(DAILY_COLUMNS[1:])].iloc[-1].isna().any():
        raise ContractError("feature_date has missing required yield values")
    return clipped


def pct_change(values: pd.Series) -> pd.Series:
    return values.ffill().pct_change(fill_method=None)


def bond_feature_set(daily: pd.DataFrame) -> pd.DataFrame:
    result: dict[str, pd.Series] = {}
    yields = {
        "3Y": pd.to_numeric(daily["TB3YWI0C"], errors="coerce"),
        "5Y": pd.to_numeric(daily["TB5YWI0C"], errors="coerce"),
        "10Y": pd.to_numeric(daily["TB0YWI0C"], errors="coerce"),
    }
    needed = set(LEVEL_FEATURES)
    for tenor, values in yields.items():
        returns = pct_change(values)
        prefix = f"bond_{tenor}_"
        if f"{prefix}ret_lag1" in needed:
            result[f"{prefix}ret_lag1"] = returns
        if f"{prefix}sign_lag1" in needed:
            result[f"{prefix}sign_lag1"] = np.sign(returns)
        windows = {
            int(match.group(1))
            for feature in needed
            if (match := re.fullmatch(rf"{re.escape(prefix)}(?:z|vol|mom_sum|mom_sign)(\d+)", feature))
        }
        for window in windows:
            if f"{prefix}mom_sum{window}" in needed or f"{prefix}mom_sign{window}" in needed:
                aggregate = returns.rolling(window, min_periods=window).sum()
                if f"{prefix}mom_sum{window}" in needed:
                    result[f"{prefix}mom_sum{window}"] = aggregate
                if f"{prefix}mom_sign{window}" in needed:
                    result[f"{prefix}mom_sign{window}"] = np.sign(aggregate)
            if f"{prefix}vol{window}" in needed:
                result[f"{prefix}vol{window}"] = returns.rolling(window, min_periods=window).std()
            if f"{prefix}z{window}" in needed:
                minimum = max(2, window // 2)
                mean = values.rolling(window, min_periods=minimum).mean()
                std = values.rolling(window, min_periods=minimum).std().replace(0.0, np.nan)
                result[f"{prefix}z{window}"] = (values - mean) / std
        if f"{prefix}streak_count" in needed:
            signs = np.sign(returns).fillna(0.0).to_numpy(np.float64)
            streak = np.zeros(len(signs), dtype=np.float64)
            for position in range(1, len(signs)):
                if signs[position] == signs[position - 1] and signs[position] != 0:
                    streak[position] = streak[position - 1] + signs[position]
                else:
                    streak[position] = signs[position]
            result[f"{prefix}streak_count"] = pd.Series(streak, index=daily.index)
    result["bond_spread_5Y_10Y"] = yields["5Y"] - yields["10Y"]
    ten_year = yields["10Y"].ffill()
    for lag in (1, 2, 5):
        result[f"rv_10Y_d{lag}"] = ten_year.diff(lag)
    frame = pd.DataFrame(result, index=daily.index).replace([np.inf, -np.inf], np.nan)
    bond_columns = [column for column in frame if column.startswith("bond_")]
    frame.loc[:, bond_columns] = frame.loc[:, bond_columns].ffill().fillna(0.0)
    missing = set(LEVEL_FEATURES) - set(frame.columns)
    if missing:
        raise ContractError(f"internal feature construction is incomplete: {sorted(missing)}")
    return frame.astype("float32")


def make_regressor(family: str):
    if family == "lgb":
        return LGBMRegressor(
            objective="regression_l1", n_estimators=180, learning_rate=0.025,
            num_leaves=5, min_child_samples=30, reg_alpha=1.0, reg_lambda=8.0,
            colsample_bytree=0.80, subsample=0.85, random_state=SEED, n_jobs=1,
            verbosity=-1, force_col_wise=True,
        )
    return XGBRegressor(
        objective="reg:absoluteerror", n_estimators=200, learning_rate=0.025,
        max_depth=2, min_child_weight=12, reg_alpha=1.0, reg_lambda=8.0,
        subsample=0.85, colsample_bytree=0.80, random_state=SEED, n_jobs=1,
        tree_method="hist",
    )


def factor_forecast(
    features: pd.DataFrame,
    target: np.ndarray,
    dates: pd.DatetimeIndex,
    positions: np.ndarray,
    columns: tuple[str, ...],
) -> np.ndarray:
    output = np.full(len(positions), np.nan, dtype=np.float64)
    blocks = dates[positions].to_period("M")
    for block in blocks.unique().sort_values():
        month_start = block.to_timestamp()
        first_row = int(np.flatnonzero(dates >= month_start)[0])
        train_positions = np.arange(max(0, first_row - 1))
        if len(train_positions) > TRAIN_WINDOW:
            train_positions = train_positions[-TRAIN_WINDOW:]
        if len(train_positions) < 180 or not np.isfinite(target[train_positions]).all():
            raise ContractError("insufficient or invalid factor training history")
        test_local = np.flatnonzero(blocks == block)
        test_positions = positions[test_local]
        family_forecasts: list[np.ndarray] = []
        for family in ("lgb", "xgb"):
            model = make_regressor(family)
            model.fit(features.iloc[train_positions][list(columns)], target[train_positions])
            family_forecasts.append(
                np.asarray(model.predict(features.iloc[test_positions][list(columns)]), dtype=np.float64)
            )
        output[test_local] = np.mean(np.vstack(family_forecasts), axis=0)
    if not np.isfinite(output).all():
        raise ContractError("model produced a non-finite factor forecast")
    return output


def predict_trace(daily: pd.DataFrame) -> tuple[int, dict[str, float]]:
    features = bond_feature_set(daily)
    dates = pd.DatetimeIndex(daily["date"])
    current_position = len(daily) - 1
    yield_5y = daily["TB5YWI0C"].to_numpy(np.float64)
    yield_10y = daily["TB0YWI0C"].to_numpy(np.float64)
    delta_5y = np.r_[np.diff(yield_5y), np.nan]
    delta_10y = np.r_[np.diff(yield_10y), np.nan]
    level_target = (delta_5y + delta_10y) / 2.0
    forecast = float(
        factor_forecast(
            features, level_target, dates,
            np.array([current_position], dtype=np.int64), LEVEL_FEATURES,
        )[0]
    )
    direction = direction_from_forecast(forecast)
    return direction, {"level_forecast": forecast}


def direction_from_forecast(forecast: float) -> int:
    if forecast > SIGNAL_THRESHOLD:
        return 1
    if forecast < -SIGNAL_THRESHOLD:
        return -1
    return 0


def predict_direction(daily: pd.DataFrame) -> int:
    direction, _ = predict_trace(daily)
    return direction


def result_from_direction(request: dict[str, str], direction: int) -> dict[str, Any]:
    if direction not in (-1, 0, 1):
        raise ContractError("algorithm did not produce a legal direction")
    return {
        "request_id": request["request_id"],
        "predict_date": request["predict_date"],
        "feature_date": request["feature_date"],
        "target_date": request["target_date"],
        "predicted_direction": int(direction),
    }


def result_for(request: dict[str, str], daily: pd.DataFrame, calendar: pd.DataFrame) -> dict[str, Any]:
    clipped = truncate_for_request(daily, calendar, request)
    direction = predict_direction(clipped)
    return result_from_direction(request, direction)


def results_for_requests(
    requests: list[dict[str, str]],
    daily: pd.DataFrame,
    calendar: pd.DataFrame,
) -> list[dict[str, Any]]:
    """按月只训练一次，并用独立逐条路径对批内首中末做因果自证。"""
    clipped_inputs = [truncate_for_request(daily, calendar, request) for request in requests]
    longest = max(clipped_inputs, key=len)
    features = bond_feature_set(longest)
    dates = pd.DatetimeIndex(longest["date"])
    positions = np.asarray([len(clipped) - 1 for clipped in clipped_inputs], dtype=np.int64)
    yield_5y = longest["TB5YWI0C"].to_numpy(np.float64)
    yield_10y = longest["TB0YWI0C"].to_numpy(np.float64)
    level_target = (
        np.r_[np.diff(yield_5y), np.nan]
        + np.r_[np.diff(yield_10y), np.nan]
    ) / 2.0
    forecasts = factor_forecast(
        features,
        level_target,
        dates,
        positions,
        LEVEL_FEATURES,
    )
    directions = [direction_from_forecast(float(value)) for value in forecasts]

    mismatches: list[str] = []
    for index in sorted({0, len(requests) // 2, len(requests) - 1}):
        expected_direction, expected_trace = predict_trace(clipped_inputs[index])
        actual_forecast = float(forecasts[index])
        if (
            directions[index] != expected_direction
            or actual_forecast != expected_trace["level_forecast"]
        ):
            mismatches.append(requests[index]["request_id"])
    if mismatches:
        LOG.warning(
            "batch causal self-check failed for request_ids=%s; "
            "falling back to independent computation",
            mismatches,
        )
        return [result_for(request, daily, calendar) for request in requests]
    LOG.info(
        "batch causal self-check passed for request_ids=%s",
        [requests[index]["request_id"] for index in sorted({0, len(requests) // 2, len(requests) - 1})],
    )
    return [
        result_from_direction(request, direction)
        for request, direction in zip(requests, directions, strict=True)
    ]


def atomic_output(path: Path, command: str, results: list[dict[str, Any]]) -> None:
    if path.exists():
        raise ContractError("output path must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            if command == "predict":
                json.dump(results[0], handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
            else:
                writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, extrasaction="raise")
                writer.writeheader()
                writer.writerows(results)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    predict = subparsers.add_parser("predict")
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--data-dir", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)
    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--requests", type=Path, required=True)
    backtest.add_argument("--data-dir", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        args = build_parser().parse_args()
        requests = read_requests(args)
        daily, calendar = read_inputs(args.data_dir)
        results = (
            [result_for(requests[0], daily, calendar)]
            if args.command == "predict"
            else results_for_requests(requests, daily, calendar)
        )
        atomic_output(args.output, args.command, results)
        return 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
