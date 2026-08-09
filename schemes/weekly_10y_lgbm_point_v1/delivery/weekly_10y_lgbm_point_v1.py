#!/usr/bin/env python3
"""SOP-compliant weekly 10Y government-bond yield direction prediction."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence

import numpy as np
import pandas as pd


SCHEME_ID = "weekly_10y_lgbm_point_v1"
TARGET_COLUMN = "TB0YWI1C"
WEEKLY_FILENAME = "weekly_output.csv"
FROZEN_FEATURES_SHA256 = "009f5f99f34bf032e2c7a1061a6a930669e6b8315768002fc1ba2650b300d89d"

REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
OUTPUT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)

FROZEN_FEATURES = (
    "S0114089",
    "TB0YWI2C",
    "DEA10Y0W",
    "DIF10Y0W",
    "TCFE10BV",
    "TCFE20BV",
    "HWW00001",
    "HWW00002",
    "HWW00003",
    "HWW00004",
    "HWW00005",
    "HWW00006",
    "HWW00007",
    "HWW00008",
    "HWW00009",
    "HWW00010",
    "HWW00011",
    "HWW00012",
    "HWC00001",
    "HWC00002",
    "HWC00003",
    "HWL00001",
    "EMA120YW",
    "HWM00001",
    "HWM00002",
    "HWM00003",
    "HWM00004",
    "HWM00005",
    "HWM00006",
    "HWM00007",
    "HWM00008",
    "HWM00009",
    "HWM00012",
    "HWM00013",
    "HWM00014",
    "HWM00015",
    "HWM00016",
    "HWM00017",
    "HWM00018",
    "HWM00019",
    "HWM00020",
    "HWM00021",
    "HWR00001",
    "HWS00001",
    "HWS00002",
    "HWS00003",
    "HWS00004",
    "HWS00005",
    "HWS00006",
    "EMA260YW",
    "ZA000001",
    "ZY000006",
    "ZY000007",
    "ZY000008",
    "ZY000009",
    "ZY000010",
    "ZY000011",
    "ZY000012",
    "ZY000013",
    "ZY000014",
    "ZY000015",
    "ZY000016",
    "ZY000017",
    "ZY000018",
    "ZY000019",
    "ZY000020",
    "ZY000021",
    "ZY000022",
    "ZY000023",
    "ZY000024",
    "ZY000025",
    "ZY000026",
    "ZY000027",
    "ZY000029",
    "ZY000030",
    "ZY000031",
    "ZY000032",
    "ZY000033",
    "ZY000034",
    "ZY000035",
)

TARGET_LAGS = (1, 2, 3, 4, 5, 8, 13, 26, 52)
TARGET_WINDOWS = (2, 3, 4, 8, 13, 26, 52)
TARGET_FEATURE_COUNT = len(TARGET_LAGS) * 2 + len(TARGET_WINDOWS) * 4
FEATURE_COUNT = TARGET_FEATURE_COUNT + len(FROZEN_FEATURES) * 2

_compact_features = json.dumps(list(FROZEN_FEATURES), ensure_ascii=False, separators=(",", ":"))
if hashlib.sha256(_compact_features.encode("utf-8")).hexdigest() != FROZEN_FEATURES_SHA256:
    raise RuntimeError("embedded frozen feature list failed its integrity check")
if len(FROZEN_FEATURES) != 80 or FEATURE_COUNT != 206:
    raise RuntimeError("embedded feature configuration has the wrong size")

_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SIX_DIGIT_PATTERN = re.compile(r"[0-9]{6}")


def _strict_date(value: str, field_name: str) -> date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use strict YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use strict YYYY-MM-DD format")
    return parsed


def validate_request(request: Any) -> dict[str, str]:
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object or CSV record")
    if set(request) != set(REQUEST_FIELDS) or len(request) != len(REQUEST_FIELDS):
        raise ValueError("request fields must exactly match the seven-field contract")
    if any(not isinstance(request[field], str) for field in REQUEST_FIELDS):
        raise ValueError("all request values must be strings")
    if not request["request_id"].strip():
        raise ValueError("request_id must not be blank")

    predict_date = _strict_date(request["predict_date"], "predict_date")
    feature_date = _strict_date(request["feature_date"], "feature_date")
    target_date = _strict_date(request["target_date"], "target_date")
    _strict_date(request["daily_cutoff_key"], "daily_cutoff_key")

    if not (feature_date <= predict_date <= target_date and feature_date < target_date):
        raise ValueError("dates must satisfy feature_date <= predict_date <= target_date and feature_date < target_date")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if _SIX_DIGIT_PATTERN.fullmatch(request[field]) is None:
            raise ValueError(f"{field} must be a six-digit string")
    return dict(request)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant {value} is not allowed")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_request_json(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
        request = json.loads(
            content,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid request JSON: {exc}") from exc
    return validate_request(request)


def load_requests_csv(path: Path) -> list[dict[str, str]]:
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
        if len(header) != len(set(header)):
            raise ValueError("requests CSV contains duplicate headers")
        if set(header) != set(REQUEST_FIELDS) or len(header) != len(REQUEST_FIELDS):
            raise ValueError("requests CSV header must exactly match the seven-field contract")

        requests: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(f"requests CSV row {row_number} has missing or extra cells")
            requests.append(validate_request(dict(zip(header, row))))

    if not requests:
        raise ValueError("requests CSV must contain at least one request")
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("request_id values must be unique within a batch")
    return requests


def load_weekly_data(data_dir: Path) -> pd.DataFrame:
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    path = data_dir / WEEKLY_FILENAME
    if not path.is_file():
        raise ValueError(f"required data file is missing: {WEEKLY_FILENAME}")

    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {WEEKLY_FILENAME}: {exc}") from exc

    with handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{WEEKLY_FILENAME} is empty") from exc
        if len(header) != len(set(header)):
            raise ValueError(f"{WEEKLY_FILENAME} contains duplicate headers")
        if header[0] != "week_id":
            raise ValueError(f"{WEEKLY_FILENAME} first column must be week_id")

        week_ids: list[str] = []
        numeric_rows: list[list[float]] = []
        seen_week_ids: set[str] = set()
        previous_week_id: str | None = None
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(f"{WEEKLY_FILENAME} row {row_number} has missing or extra cells")
            week_id = row[0]
            if _SIX_DIGIT_PATTERN.fullmatch(week_id) is None:
                raise ValueError(f"invalid week_id at row {row_number}")
            if week_id in seen_week_ids:
                raise ValueError(f"duplicate week_id at row {row_number}: {week_id}")
            if previous_week_id is not None and week_id <= previous_week_id:
                raise ValueError("week_id values must be in strict ascending order")

            values: list[float] = []
            for column_number, raw_value in enumerate(row[1:], start=2):
                if raw_value == "":
                    values.append(np.nan)
                    continue
                try:
                    value = float(raw_value)
                except ValueError as exc:
                    raise ValueError(
                        f"non-numeric value at row {row_number}, column {column_number}"
                    ) from exc
                if not math.isfinite(value):
                    raise ValueError(f"non-finite value at row {row_number}, column {column_number}")
                values.append(value)

            week_ids.append(week_id)
            numeric_rows.append(values)
            seen_week_ids.add(week_id)
            previous_week_id = week_id

    if not week_ids:
        raise ValueError(f"{WEEKLY_FILENAME} contains no data rows")
    frame = pd.DataFrame(numeric_rows, columns=header[1:], dtype=float)
    frame.insert(0, "week_id", pd.Series(week_ids, dtype="string"))
    return frame


def slice_for_cutoff(weekly: pd.DataFrame, weekly_cutoff_key: str) -> pd.DataFrame:
    matches = np.flatnonzero(weekly["week_id"].eq(weekly_cutoff_key).to_numpy())
    if len(matches) != 1:
        raise ValueError(f"weekly_cutoff_key not found exactly once: {weekly_cutoff_key}")
    cutoff_index = int(matches[0])
    return weekly.iloc[: cutoff_index + 1].copy().reset_index(drop=True)


def _target_history_features(target: pd.Series) -> pd.DataFrame:
    close = pd.to_numeric(target, errors="raise")
    returns = close.pct_change(fill_method=None)
    features = pd.DataFrame(index=close.index)
    for lag in TARGET_LAGS:
        lag_return = returns.shift(lag - 1)
        features[f"ret_lag{lag}"] = lag_return
        features[f"sign_lag{lag}"] = np.sign(lag_return)
    for window in TARGET_WINDOWS:
        momentum = returns.rolling(window).sum()
        features[f"mom_sum{window}"] = momentum
        features[f"mom_sign{window}"] = np.sign(momentum)
        features[f"vol{window}"] = returns.rolling(window).std()
        minimum = max(2, window // 2)
        mean = close.rolling(window, min_periods=minimum).mean()
        standard_deviation = close.rolling(window, min_periods=minimum).std()
        features[f"z{window}"] = close.sub(mean).div(standard_deviation.replace(0, np.nan))
    return features


def build_features(weekly: pd.DataFrame) -> pd.DataFrame:
    required = {TARGET_COLUMN, *FROZEN_FEATURES}
    missing = required.difference(weekly.columns)
    if missing:
        raise ValueError(f"weekly data is missing required model columns: {sorted(missing)}")

    raw = weekly.loc[:, FROZEN_FEATURES].astype(float)
    differences = raw.diff()
    differences.columns = [f"{column}_d1" for column in FROZEN_FEATURES]
    features = pd.concat(
        (_target_history_features(weekly[TARGET_COLUMN]), raw, differences),
        axis=1,
    )
    if features.shape[1] != FEATURE_COUNT:
        raise RuntimeError("model feature construction did not produce exactly 206 columns")
    return features.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)


def make_labels(target: pd.Series) -> pd.Series:
    current = pd.to_numeric(target, errors="raise")
    following = current.shift(-1)
    valid = current.notna() & following.notna()
    labels = pd.Series(np.nan, index=current.index, dtype=float)
    labels.loc[valid] = np.where(following.loc[valid] > current.loc[valid], 1, -1)
    return labels


def threshold_grid() -> np.ndarray:
    return np.linspace(0.30, 0.70, 41)


def choose_threshold(
    calibration_probabilities: np.ndarray,
    calibration_labels: np.ndarray,
    historical_up_rate: float,
) -> float:
    probabilities = np.asarray(calibration_probabilities, dtype=float)
    labels = np.asarray(calibration_labels)
    if len(probabilities) == 0 or len(probabilities) != len(labels):
        raise ValueError("calibration data must be nonempty and aligned")
    target_up_rate = float(np.clip(historical_up_rate, 0.30, 0.55))
    best_score = -np.inf
    best_threshold = 0.30
    for threshold in threshold_grid():
        predicted = np.where(probabilities >= threshold, 1, -1)
        accuracy = float((predicted == labels).mean())
        predicted_up_rate = float((predicted == 1).mean())
        score = accuracy - 0.25 * abs(predicted_up_rate - target_up_rate)
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _import_lightgbm():
    try:
        return importlib.import_module("lightgbm")
    except ImportError as exc:
        raise RuntimeError("LightGBM is required for prediction") from exc


def _positive_probabilities(model: Any, features: pd.DataFrame) -> np.ndarray:
    probabilities = np.asarray(model.predict_proba(features), dtype=float)
    if probabilities.ndim != 2 or probabilities.shape[0] != len(features) or probabilities.shape[1] < 2:
        raise RuntimeError("LightGBM returned an invalid probability matrix")
    positive_column = 1
    classes = getattr(model, "classes_", None)
    if classes is not None:
        matches = np.flatnonzero(np.asarray(classes) == 1)
        if len(matches) != 1:
            raise RuntimeError("LightGBM positive class is unavailable")
        positive_column = int(matches[0])
    positive = probabilities[:, positive_column]
    if not np.isfinite(positive).all():
        raise RuntimeError("LightGBM returned non-finite probabilities")
    return positive


def predict_direction(weekly_slice: pd.DataFrame) -> int:
    if weekly_slice.empty:
        raise ValueError("weekly cutoff slice is empty")
    target = pd.to_numeric(weekly_slice[TARGET_COLUMN], errors="raise")
    cutoff_index = len(weekly_slice) - 1
    if pd.isna(target.iloc[cutoff_index]):
        raise ValueError("target value is missing at the weekly cutoff")

    labels = make_labels(target)
    eligible = np.flatnonzero(labels.notna().to_numpy())
    eligible = eligible[eligible < cutoff_index]
    if len(eligible) > 104:
        eligible = eligible[-104:]
    if len(eligible) < 40:
        raise ValueError(f"insufficient history: at least 40 eligible labels are required; found {len(eligible)}")

    eligible_labels = labels.iloc[eligible].astype(int).to_numpy()
    if len(np.unique(eligible_labels)) != 2:
        raise ValueError("eligible history must contain both direction classes")

    split = max(35, int(len(eligible) * 0.78))
    split = min(split, len(eligible) - 1)
    fit_indices = eligible[:split]
    calibration_indices = eligible[split:]
    fit_labels = labels.iloc[fit_indices].astype(int).to_numpy()
    if len(np.unique(fit_labels)) != 2:
        raise ValueError("fit history must contain both direction classes")
    if len(calibration_indices) == 0:
        raise ValueError("calibration history must be nonempty")

    features = build_features(weekly_slice)
    lightgbm = _import_lightgbm()
    model = lightgbm.LGBMClassifier(
        objective="binary",
        metric="binary_logloss",
        num_leaves=2,
        learning_rate=0.01,
        n_estimators=120,
        min_child_samples=12,
        reg_alpha=0.5,
        reg_lambda=2.0,
        subsample=0.85,
        subsample_freq=0,
        colsample_bytree=0.85,
        n_jobs=1,
        verbosity=-1,
        random_state=42,
        deterministic=True,
        force_col_wise=True,
    )
    model.fit(features.iloc[fit_indices], (fit_labels == 1).astype(int))

    calibration_probabilities = _positive_probabilities(model, features.iloc[calibration_indices])
    historical_up_rate = float((eligible_labels == 1).mean())
    threshold = choose_threshold(
        calibration_probabilities,
        labels.iloc[calibration_indices].astype(int).to_numpy(),
        historical_up_rate,
    )
    probability = float(_positive_probabilities(model, features.iloc[[cutoff_index]])[0])
    return int(1 if probability >= threshold else -1)


def generate_results(requests: Sequence[dict[str, str]], weekly: pd.DataFrame) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw_request in requests:
        request = validate_request(raw_request)
        cutoff_data = slice_for_cutoff(weekly, request["weekly_cutoff_key"])
        direction = predict_direction(cutoff_data)
        if type(direction) is not int or direction not in (-1, 1):
            raise RuntimeError("model returned an invalid predicted direction")
        results.append(
            {
                "request_id": request["request_id"],
                "predict_date": request["predict_date"],
                "feature_date": request["feature_date"],
                "target_date": request["target_date"],
                "predicted_direction": direction,
            }
        )
    return results


def validate_output_path(output: Path, data_dir: Path) -> Path:
    output = Path(output)
    if os.path.lexists(output):
        raise ValueError("output path already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent must be an existing directory")
    resolved_output = output.parent.resolve() / output.name
    resolved_data_dir = Path(data_dir).resolve()
    if resolved_output == resolved_data_dir or resolved_data_dir in resolved_output.parents:
        raise ValueError("output path must not be inside data-dir")
    return output


def atomic_write_text(output: Path, content: str) -> None:
    output = Path(output)
    file_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
        )
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="")
        file_descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(output):
            raise FileExistsError("output path appeared before atomic replacement")
        os.replace(temporary_name, output)
        temporary_name = None
    except BaseException:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        raise


def _serialize_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"


def _serialize_csv(results: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(OUTPUT_FIELDS)
    for result in results:
        writer.writerow([result[field] for field in OUTPUT_FIELDS])
    return buffer.getvalue()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    predict_parser = subparsers.add_parser("predict", help="generate one prediction")
    predict_parser.add_argument("--request", type=Path, required=True)
    predict_parser.add_argument("--data-dir", type=Path, required=True)
    predict_parser.add_argument("--output", type=Path, required=True)

    backtest_parser = subparsers.add_parser("backtest", help="generate a prediction batch")
    backtest_parser.add_argument("--requests", type=Path, required=True)
    backtest_parser.add_argument("--data-dir", type=Path, required=True)
    backtest_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = validate_output_path(args.output, args.data_dir)
        if args.command == "predict":
            requests = [load_request_json(args.request)]
        else:
            requests = load_requests_csv(args.requests)
        weekly = load_weekly_data(args.data_dir)
        results = generate_results(requests, weekly)
        content = _serialize_json(results[0]) if args.command == "predict" else _serialize_csv(results)
        atomic_write_text(output, content)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
