#!/usr/bin/env python3
"""Blackbox V2: frozen 5Y weekly-close curve-logit policy."""

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

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


SEED = 20260825
RANDOM_STATE = SEED
SCHEME_ID = "weekly_5y_curve_logit_v1"
TARGET = "TB5YWI0C"
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
OUTPUT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date", "predicted_direction",
)
FEATURES = (
    "d_z13__d__MACDD5YD", "w_d4__w__TCFE03WR", "w_d1__w__TCFE002A",
    "w_d1__w__TCFE002V", "w_z13__w__TCFE020I", "d_lvl__d__TB0YWIPC",
    "w_z13__w__TB5YWI2C", "d_z13__d__TB0YWIPC", "w_z13__w__TB5YWI2W",
    "w_d1__w__TB1YWI2V", "w_z13__w__TB5YWI2H", "w_z13__w__TB0YWI2H",
    "w_z13__w__TB0YWI2C", "w_d1__w__TCFE02VR", "d_d1__d__TB1YWI0V",
    "d_lvl__d__BIAS10YD", "d_z13__d__TB5YWIPC", "w_lvl__w__MACDD5YW",
    "d_d1__d__CCI5Y00D", "w_z13__w__TCFE002A", "w_z13__w__BBIB5Y0W",
    "w_z13__w__TCFE2MA4", "d_d1__d__TB0YWIPC", "w_lvl__w__RSI010YW",
    "w_d1__w__TCFE020I", "d_z13__d__TB7YWI0C", "w_z13__w__TB7YWI3C",
    "d_d1__d__BIAS10YD", "w_z13__w__TCFE002V", "w_z13__w__MACDD5YW",
    "w_z13__w__TCFE02VR", "w_z13__w__TCFE002L", "w_lvl__w__TCFE03WR",
    "w_z13__w__TCFE10BV", "d_z13__d__TCFE00BV", "d_z13__d__CCI5Y00D",
    "d_lvl__d__TCFE0000", "w_z13__w__TCFE010I", "d_d1__d__MACDD5YD",
    "w_lvl__w__TCFE020I", "d_d4__d__BIASB0YD", "w_lvl__w__TCFE002A",
    "d_z13__d__BIAS10YD", "d_lvl__d__CCI5Y00D", "w_lvl__w__BBIB5Y0W",
    "d_d1__d__CCI10Y0D", "w_d4__w__TB5YWI2H", "d_d1__d__MACDC0YD",
    "d_d4__d__BBIB5Y0D", "w_lvl__w__TCFE2MA8", "d_d1__d__TB5YWIPC",
    "w_z13__w__MACDD1YW", "d_d4__d__BIASB5YD", "w_d4__w__TCFE002A",
    "w_lvl__w__TB1YWI2V", "d_d1__d__RSI5Y00D", "d_lvl__d__ATR1Y00D",
    "w_lvl__w__TCFE002V", "w_z13__w__TCFE2MA8", "w_lvl__w__TCFE02VR",
    "d_lvl__d__TCFE01WR", "d_d1__d__RSI10Y0D", "w_z13__w__BIASB5YW",
    "d_lvl__d__CCI10Y0D", "w_lvl__w__ATR1Y00W", "w_d1__w__TB0YWI1V",
    "w_z13__w__TB0YWI2W", "w_z13__w__TB5YWI2L", "d_z13__d__MACDD0YD",
    "w_z13__w__RSI010YW", "w_z13__w__TB0YWI2L", "w_d1__w__TB1YWI1V",
    "d_d1__d__RSI710YD", "d_d4__d__TB0YWI0V", "d_z13__d__CCI10Y0D",
    "w_lvl__w__TB1YWI1V", "w_d4__w__TCFE02VR", "d_d1__d__RSI75Y0D",
    "d_z13__d__TCFE000L", "d_z13__d__TCFE01WR", "w_z13__w__TB0YWI3C",
    "d_z13__d__TB5YWI0C", "w_z13__w__TB5YWI3C", "w_lvl__w__BIASB5YW",
    "w_d4__w__RSIB5Y0W", "d_z13__d__TCFE000C", "d_d1__d__TCFE0000",
    "d_z13__d__TCFE000S", "w_d1__w__RSI010YW", "d_z13__d__BBIB5Y0D",
    "d_lvl__d__TB5YWIPC", "w_d4__w__BIASB5YW", "w_z13__w__TCFE002S",
    "w_d4__w__TB0YWI2V", "w_lvl__w__BIASB0YW", "d_z13__d__TB0YWI0H",
)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KEY_RE = re.compile(r"[0-9]{6}")


def _raw_name(feature: str) -> str:
    return feature.split("__", 2)[2]


DAILY_COLUMNS = tuple(sorted({TARGET, *(_raw_name(x) for x in FEATURES if x.startswith("d_"))}))
WEEKLY_COLUMNS = tuple(sorted({"TB5YWI3C", *(_raw_name(x) for x in FEATURES if x.startswith("w_"))}))
if len(FEATURES) != 96 or len(set(FEATURES)) != 96:
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


def _numeric_block(frame: pd.DataFrame, columns: Sequence[str], prefix: str) -> pd.DataFrame:
    raw = frame.loc[:, columns].apply(pd.to_numeric, errors="coerce").astype(np.float32)
    raw.columns = [f"{prefix}lvl__{x}" for x in raw.columns]
    d1 = raw.diff(1); d1.columns = [x.replace(f"{prefix}lvl__", f"{prefix}d1__") for x in raw.columns]
    d4 = raw.diff(4); d4.columns = [x.replace(f"{prefix}lvl__", f"{prefix}d4__") for x in raw.columns]
    mean = raw.rolling(13, min_periods=8).mean()
    std = raw.rolling(13, min_periods=8).std().replace(0.0, np.nan)
    z13 = raw.sub(mean).div(std)
    z13.columns = [x.replace(f"{prefix}lvl__", f"{prefix}z13__") for x in raw.columns]
    return pd.concat((raw, d1, d4, z13), axis=1).astype(np.float32)


def _cutoff_frame(request: dict[str, str], calendar: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
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
    closed = set(weekly_slice.loc[pd.to_numeric(weekly_slice["TB5YWI3C"], errors="coerce").notna(), "week_id"].astype(str))
    if mapped not in closed:
        raise ValueError("weekly_cutoff_key is not a closed week")
    endpoints = endpoints.loc[endpoints["week_id"].astype(str).isin(closed)].reset_index(drop=True)
    dcols = list(DAILY_COLUMNS)
    wcols = list(WEEKLY_COLUMNS)
    endpoints = endpoints.rename(columns={x: f"d__{x}" for x in dcols})
    weekly_slice = weekly_slice.rename(columns={x: f"w__{x}" for x in wcols})
    frame = endpoints.merge(weekly_slice, on="week_id", how="left", validate="one_to_one")
    if frame.empty or str(frame.iloc[-1]["week_id"]) != mapped:
        raise ValueError("weekly cutoff did not produce a usable feature row")
    return frame


def predict_direction(request: dict[str, str], calendar: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> int:
    frame = _cutoff_frame(request, calendar, daily, weekly)
    target = pd.to_numeric(frame[f"d__{TARGET}"], errors="coerce")
    if pd.isna(target.iloc[-1]):
        raise ValueError("target yield is missing at feature_date")
    target_dates = frame["date"].shift(-1)
    labels = np.sign(target.shift(-1) - target)
    eligible = np.flatnonzero((target_dates < frame.iloc[-1]["date"]).fillna(False).to_numpy())
    eligible = eligible[np.isfinite(labels.iloc[eligible].to_numpy())]
    if len(eligible) < 80:
        raise ValueError(f"insufficient history: at least 80 eligible labels are required; found {len(eligible)}")
    yy = labels.iloc[eligible].astype(np.int8).to_numpy()
    if np.unique(yy).size < 2:
        raise ValueError("eligible history must contain both direction classes")
    d = _numeric_block(frame, [f"d__{x}" for x in DAILY_COLUMNS], "d_")
    w = _numeric_block(frame, [f"w__{x}" for x in WEEKLY_COLUMNS], "w_")
    features = pd.concat((d, w), axis=1).loc[:, FEATURES]
    values = features.to_numpy(dtype=np.float32)
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    x_train = imputer.fit_transform(values[eligible])
    x_now = imputer.transform(values[[-1]])
    scaler = StandardScaler()
    x_train = scaler.fit_transform(x_train)
    x_now = scaler.transform(x_now)
    model = LogisticRegression(C=0.03, class_weight=None, max_iter=2000, solver="liblinear", random_state=RANDOM_STATE)
    model.fit(x_train, (yy > 0).astype(np.int8))
    probability = float(model.predict_proba(x_now)[0, 1])
    if not math.isfinite(probability):
        raise RuntimeError("model returned a non-finite probability")
    return 1 if probability >= 0.5 else -1


def generate(requests: Sequence[dict[str, str]], data: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]) -> list[dict[str, Any]]:
    calendar, daily, weekly = data
    output: list[dict[str, Any]] = []
    for raw in requests:
        request = validate_request(raw)
        direction = predict_direction(request, calendar, daily, weekly)
        output.append({**{x: request[x] for x in OUTPUT_FIELDS[:-1]}, "predicted_direction": int(direction)})
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
