#!/usr/bin/env python3
"""Blackbox V2: frozen 10Y weekly-close heterogeneous vote policy."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


SEED = 20260824
RANDOM_STATE = SEED
SCHEME_ID = "weekly_10y_hetero_vote_v1"
TARGET = "TB0YWI0C"
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
OUTPUT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date", "predicted_direction",
)
BASE24 = (
    "core::spread_TB0YWI0C_TB1YWI0C_d13", "core::target_sign6", "core::z13",
    "core::spread_TB5YWI0C_TB1YWI0C_d13", "core::spread_TB0YWI0C_TB5YWI0C_d13",
    "core::target_sign26", "core::target_sign3", "core::z26", "core::vol13",
    "core::z8", "core::vol4", "core::target_sign2", "core::z4",
    "core::target_sign13", "core::vol8", "core::target_diff8", "core::mom8",
    "core::vol52", "core::target_sign4", "core::target_diff3", "core::mom3",
    "core::vol26", "core::target_diff4", "core::mom4",
)
HIST48 = BASE24 + (
    "core::spread_TB5YWI0C_TB1YWI0C_d1", "core::target_diff6",
    "core::spread_TB0YWI0C_TB1YWI0C_d4", "core::spread_TB0YWI0C_TB1YWI0C_d1",
    "core::target_sign8", "core::spread_TB5YWI0C_TB1YWI0C_d4",
    "core::spread_TB0YWI0C_TB5YWI0C_d4", "core::target_diff2",
    "core::spread_TB0YWI0C_TB1YWI0C", "core::spread_TB0YWI0C_TB5YWI0C",
    "core::target_diff13", "core::mom13", "core::spread_TB5YWI0C_TB1YWI0C",
    "core::target_sign52", "core::z52", "core::target_sign1", "core::target_diff26",
    "core::mom26", "core::z3", "core::target_diff1",
    "core::spread_TB0YWI0C_TB5YWI0C_d1", "core::target_diff52",
    "core::mom52", "core::vol3",
)
RULE22 = BASE24[:22]
DAILY_COLUMNS = ("TB0YWI0C", "TB5YWI0C", "TB1YWI0C")
WEEKLY_COLUMNS = ("TB0YWI3C",)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KEY_RE = re.compile(r"[0-9]{6}")
if len(BASE24) != 24 or len(HIST48) != 48 or len(RULE22) != 22:
    raise RuntimeError("frozen feature manifest is invalid")


def _strict_date(value: str, field: str) -> date:
    if DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must use strict YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use strict YYYY-MM-DD format")
    return parsed


def validate_request(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(REQUEST_FIELDS) or len(value) != 7:
        raise ValueError("request fields must exactly match the seven-field contract")
    if any(not isinstance(value[x], str) for x in REQUEST_FIELDS):
        raise ValueError("all request values must be strings")
    if not value["request_id"].strip():
        raise ValueError("request_id must not be blank")
    predict = _strict_date(value["predict_date"], "predict_date")
    feature = _strict_date(value["feature_date"], "feature_date")
    target = _strict_date(value["target_date"], "target_date")
    cutoff = _strict_date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not (feature <= predict <= target and feature < target):
        raise ValueError("dates must satisfy feature_date <= predict_date <= target_date and feature_date < target_date")
    if cutoff != feature:
        raise ValueError("daily_cutoff_key must equal feature_date for weekly_point")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if KEY_RE.fullmatch(value[field]) is None:
            raise ValueError(f"{field} must be a six-digit string")
    return dict(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON constant {value} is not allowed")


def load_request(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid request JSON: {exc}") from exc
    return validate_request(raw)


def load_requests(path: Path) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read requests CSV: {exc}") from exc
    with handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("requests CSV is empty") from exc
        if len(header) != len(set(header)) or set(header) != set(REQUEST_FIELDS) or len(header) != 7:
            raise ValueError("requests CSV header must exactly match the seven-field contract")
        rows: list[dict[str, str]] = []
        for number, row in enumerate(reader, 2):
            if len(row) != 7:
                raise ValueError(f"requests CSV row {number} has missing or extra cells")
            rows.append(validate_request(dict(zip(header, row))))
    if not 1 <= len(rows) <= 100:
        raise ValueError("requests CSV must contain between 1 and 100 requests")
    ids = [x["request_id"] for x in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("request_id values must be unique within a batch")
    return rows


def _read_required(path: Path, key: str, required: Sequence[str], dtype: dict[str, str] | None = None) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"required data file is missing: {path.name}")
    try:
        header = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns.tolist()
    except Exception as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if len(header) != len(set(header)) or key not in header:
        raise ValueError(f"invalid or duplicate key/header in {path.name}: {key}")
    missing = set(required).difference(header)
    if missing:
        raise ValueError(f"{path.name} is missing required columns: {sorted(missing)}")
    frame = pd.read_csv(path, encoding="utf-8-sig", usecols=[key, *required], dtype=dtype, low_memory=False)
    if frame[key].duplicated().any():
        raise ValueError(f"duplicate key in {path.name}: {key}")
    return frame


def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    calendar = _read_required(data_dir / "api_wind_date.csv", "rdate", ("week_id",), {"rdate": "string", "week_id": "string"})
    daily = _read_required(data_dir / "daily_output.csv", "date", DAILY_COLUMNS, {"date": "string"})
    weekly = _read_required(data_dir / "weekly_output.csv", "week_id", WEEKLY_COLUMNS, {"week_id": "string"})
    for frame, field, label in ((calendar, "rdate", "calendar"), (daily, "date", "daily")):
        parsed = pd.to_datetime(frame[field], errors="raise").dt.normalize()
        if parsed.duplicated().any():
            raise ValueError(f"{label} dates must be unique after normalization")
        if not parsed.is_monotonic_increasing:
            raise ValueError(f"{label} dates must be ascending")
        frame[field] = parsed
    if calendar["week_id"].isna().any() or not calendar["week_id"].str.fullmatch(KEY_RE).all():
        raise ValueError("calendar contains invalid week_id")
    return calendar, daily, weekly


def cutoff_frame(request: dict[str, str], calendar: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    cutoff = pd.Timestamp(request["daily_cutoff_key"])
    matches = calendar["rdate"].eq(cutoff)
    if int(matches.sum()) != 1:
        raise ValueError("daily_cutoff_key must exist exactly once in calendar")
    mapped = str(calendar.loc[matches, "week_id"].iloc[0])
    if mapped != request["weekly_cutoff_key"]:
        raise ValueError("daily_cutoff_key does not map to weekly_cutoff_key")
    daily_matches = daily["date"].eq(cutoff)
    if int(daily_matches.sum()) != 1:
        raise ValueError("daily_cutoff_key must exist exactly once in daily data")
    weekly_matches = weekly["week_id"].eq(request["weekly_cutoff_key"])
    if int(weekly_matches.sum()) != 1:
        raise ValueError("weekly_cutoff_key must exist exactly once in weekly data")
    weekly_index = int(np.flatnonzero(weekly_matches.to_numpy())[0])
    daily_slice = daily.loc[daily["date"].le(cutoff)].copy()
    calendar_slice = calendar.loc[calendar["rdate"].le(cutoff), ["rdate", "week_id"]]
    daily_slice = daily_slice.merge(calendar_slice, left_on="date", right_on="rdate", how="left", validate="one_to_one").drop(columns="rdate")
    if daily_slice["week_id"].isna().any():
        raise ValueError("daily dates are missing from authoritative calendar")
    endpoints = daily_slice.groupby("week_id", sort=False, observed=True).tail(1).sort_values("date").reset_index(drop=True)
    if str(endpoints.iloc[-1]["week_id"]) != mapped or endpoints.iloc[-1]["date"] != cutoff:
        raise ValueError("feature_date is not the current authoritative week endpoint")
    weekly_slice = weekly.iloc[:weekly_index + 1].copy()
    closed = set(weekly_slice.loc[pd.to_numeric(weekly_slice["TB0YWI3C"], errors="coerce").notna(), "week_id"].astype(str))
    if mapped not in closed:
        raise ValueError("weekly_cutoff_key is not a closed week")
    endpoints = endpoints.loc[endpoints["week_id"].astype(str).isin(closed)].reset_index(drop=True)
    # The frozen exploration timeline removes target-missing weeks before all
    # lag and rolling transforms, so reproduce that index exactly.
    endpoints = endpoints.loc[pd.to_numeric(endpoints[TARGET], errors="coerce").notna()].reset_index(drop=True)
    if endpoints.empty or str(endpoints.iloc[-1]["week_id"]) != mapped:
        raise ValueError("weekly cutoff did not produce a usable feature row")
    return endpoints


def build_core(frame: pd.DataFrame) -> pd.DataFrame:
    current = pd.to_numeric(frame[TARGET], errors="coerce")
    features = pd.DataFrame(index=frame.index)
    for lag in (1, 2, 3, 4, 6, 8, 13, 26, 52):
        features[f"core::target_diff{lag}"] = current.diff(lag)
        features[f"core::target_sign{lag}"] = np.sign(current.diff(lag))
    change = current.diff()
    for window in (3, 4, 8, 13, 26, 52):
        features[f"core::mom{window}"] = change.rolling(window, min_periods=max(2, window // 2)).sum()
        features[f"core::vol{window}"] = change.rolling(window, min_periods=max(2, window // 2)).std()
        mean = current.rolling(window, min_periods=max(2, window // 2)).mean()
        std = current.rolling(window, min_periods=max(2, window // 2)).std()
        features[f"core::z{window}"] = current.sub(mean).div(std.replace(0.0, np.nan))
    for left, right in (("TB0YWI0C", "TB5YWI0C"), ("TB5YWI0C", "TB1YWI0C"), ("TB0YWI0C", "TB1YWI0C")):
        spread = pd.to_numeric(frame[left], errors="coerce") - pd.to_numeric(frame[right], errors="coerce")
        features[f"core::spread_{left}_{right}"] = spread
        for lag in (1, 4, 13):
            features[f"core::spread_{left}_{right}_d{lag}"] = spread.diff(lag)
    return features.replace([np.inf, -np.inf], np.nan).astype(np.float32)


def eligible_labels(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    current = pd.to_numeric(frame[TARGET], errors="coerce")
    labels = np.sign(current.shift(-1) - current).to_numpy(dtype=float)
    target_dates = frame["date"].shift(-1)
    eligible = np.flatnonzero(np.isfinite(labels) & (labels != 0) & (target_dates < frame.iloc[-1]["date"]).fillna(False).to_numpy())
    return labels, eligible


def probability(family: str, columns: Sequence[str], train_window: int, frame: pd.DataFrame, features: pd.DataFrame, labels: np.ndarray, eligible: np.ndarray) -> float:
    use = eligible[-train_window:] if train_window > 0 else eligible
    if len(use) < 40 or np.unique(labels[use]).size < 2:
        raise ValueError(f"insufficient history for {family}: at least 40 two-class labels are required")
    x = features.loc[:, columns].to_numpy(dtype=np.float32)
    yy = labels[use].astype(int)
    if family == "rule":
        values = x[use, 21].astype(float)
        median = float(np.nanmedian(values)); scale = float(np.nanstd(values))
        if not math.isfinite(scale) or scale < 1e-12 or not math.isfinite(float(x[-1, 21])):
            raise ValueError("rule member has unusable feature history")
        centered = np.nan_to_num(values - median, nan=0.0)
        corr = float(np.corrcoef(centered, yy.astype(float))[0, 1]) if np.std(centered) > 0 else 0.0
        orientation = 1.0 if corr >= 0 else -1.0
        z = float(np.clip(orientation * (float(x[-1, 21]) - median) / scale, -8.0, 8.0))
        return 1.0 / (1.0 + math.exp(-z))
    if family == "lgbm":
        clf = lgb.LGBMClassifier(
            n_estimators=120, learning_rate=0.06, num_leaves=15, min_child_samples=10,
            reg_alpha=0.5, reg_lambda=3.0, objective="binary", verbosity=-1,
            deterministic=True, force_col_wise=True, random_state=RANDOM_STATE, n_jobs=1,
            subsample=0.85, colsample_bytree=0.85,
        )
        model = Pipeline((("imputer", SimpleImputer(strategy="median")), ("model", clf)))
    elif family == "logistic":
        clf = LogisticRegression(C=10.0, class_weight="balanced", max_iter=1000, solver="liblinear", random_state=RANDOM_STATE)
        model = Pipeline((("imputer", SimpleImputer(strategy="median")), ("scale", StandardScaler()), ("model", clf)))
    elif family == "extra_trees":
        clf = ExtraTreesClassifier(n_estimators=80, max_depth=7, min_samples_leaf=5, max_features=0.5, class_weight="balanced", random_state=RANDOM_STATE, n_jobs=1)
        model = Pipeline((("imputer", SimpleImputer(strategy="median")), ("model", clf)))
    elif family == "hist_gb":
        clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, max_leaf_nodes=3, l2_regularization=0.0, random_state=RANDOM_STATE)
        model = Pipeline((("imputer", SimpleImputer(strategy="median")), ("model", clf)))
    else:
        raise RuntimeError(f"unknown member family: {family}")
    model.fit(x[use], (yy == 1).astype(int))
    result = float(model.predict_proba(x[[-1]])[0, 1])
    if not math.isfinite(result):
        raise RuntimeError(f"{family} returned a non-finite probability")
    return result


def _signal(value: float, reject: float) -> int:
    return 0 if abs(value - 0.5) < reject else (1 if value >= 0.5 else -1)


def predict_direction(request: dict[str, str], calendar: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> int:
    frame = cutoff_frame(request, calendar, daily, weekly)
    if pd.isna(pd.to_numeric(frame[TARGET], errors="coerce").iloc[-1]):
        raise ValueError("target yield is missing at feature_date")
    features = build_core(frame)
    labels, eligible = eligible_labels(frame)
    members = (
        _signal(probability("lgbm", BASE24, 208, frame, features, labels, eligible), 0.04),
        _signal(probability("logistic", BASE24, 416, frame, features, labels, eligible), 0.04),
        _signal(probability("extra_trees", BASE24, 104, frame, features, labels, eligible), 0.00),
        _signal(probability("hist_gb", HIST48, 0, frame, features, labels, eligible), 0.02),
        _signal(probability("rule", RULE22, 208, frame, features, labels, eligible), 0.00),
    )
    total = int(sum(members))
    return 0 if total == 0 else (1 if total > 0 else -1)


def generate(requests: Sequence[dict[str, str]], data: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]) -> list[dict[str, Any]]:
    calendar, daily, weekly = data
    output: list[dict[str, Any]] = []
    for raw in requests:
        request = validate_request(raw)
        direction = predict_direction(request, calendar, daily, weekly)
        if type(direction) is not int or direction not in (-1, 0, 1):
            raise RuntimeError("model returned an invalid direction")
        output.append({**{x: request[x] for x in OUTPUT_FIELDS[:-1]}, "predicted_direction": direction})
    return output


def validate_output(output: Path, data_dir: Path) -> Path:
    if os.path.lexists(output):
        raise ValueError("output path already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent must be an existing directory")
    resolved = output.parent.resolve() / output.name
    root = data_dir.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("output path must not be inside data-dir")
    return output


def atomic_write(output: Path, content: str) -> None:
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        if os.path.lexists(output):
            raise FileExistsError("output path appeared before atomic replacement")
        os.replace(temporary, output); temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try: os.unlink(temporary)
            except FileNotFoundError: pass


def serialize_csv(rows: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n"); writer.writerow(OUTPUT_FIELDS)
    writer.writerows([[row[x] for x in OUTPUT_FIELDS] for row in rows])
    return buffer.getvalue()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    one = commands.add_parser("predict"); one.add_argument("--request", type=Path, required=True); one.add_argument("--data-dir", type=Path, required=True); one.add_argument("--output", type=Path, required=True)
    many = commands.add_parser("backtest"); many.add_argument("--requests", type=Path, required=True); many.add_argument("--data-dir", type=Path, required=True); many.add_argument("--output", type=Path, required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    warnings.filterwarnings("ignore", category=UserWarning)
    args = parser().parse_args(argv)
    try:
        output = validate_output(args.output, args.data_dir)
        requests = [load_request(args.request)] if args.command == "predict" else load_requests(args.requests)
        rows = generate(requests, load_data(args.data_dir))
        content = json.dumps(rows[0], ensure_ascii=False, separators=(",", ":")) + "\n" if args.command == "predict" else serialize_csv(rows)
        atomic_write(output, content)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
