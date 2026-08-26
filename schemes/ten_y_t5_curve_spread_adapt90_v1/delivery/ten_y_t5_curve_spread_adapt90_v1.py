#!/usr/bin/env python3
"""Blackbox V2 10Y T+5 adaptive 10Y-7Y curve-spread scheme."""

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

import numpy as np
import pandas as pd


SEED = 42
RANDOM_STATE = 42
SCHEME_ID = "ten_y_t5_curve_spread_adapt90_v1"
DAILY_FILENAME = "daily_output.csv"
TARGET_COLUMN = "TB0YWI0C"
SIGNAL_COLUMNS = ("TB0YWI0C", "TB7YWI0C")
REQUIRED_COLUMNS = ("date", *SIGNAL_COLUMNS)
HORIZON = 5
SIGNAL_WINDOW = 252
ADAPT_WINDOW = 90
MIN_OBSERVATIONS = 5

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
    if not (
        feature_date <= predict_date <= target_date
        and feature_date < target_date
    ):
        raise ValueError(
            "dates must satisfy feature_date <= predict_date <= target_date "
            "and feature_date < target_date"
        )
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
        request = json.loads(
            path.read_text(encoding="utf-8"),
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
            raise ValueError("requests CSV header must exactly match the contract")
        requests: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(
                    f"requests CSV row {row_number} has missing or extra cells"
                )
            requests.append(validate_request(dict(zip(header, row))))
    if not 1 <= len(requests) <= 100:
        raise ValueError("requests CSV must contain between 1 and 100 requests")
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("request_id values must be unique within a batch")
    return requests


def load_daily_data(data_dir: Path) -> pd.DataFrame:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    path = data_dir / DAILY_FILENAME
    if not path.is_file():
        raise ValueError(f"required data file is missing: {DAILY_FILENAME}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {DAILY_FILENAME}: {exc}") from exc
    if not header or header[0] != "date":
        raise ValueError(f"{DAILY_FILENAME} first column must be date")
    if len(header) != len(set(header)):
        raise ValueError(f"{DAILY_FILENAME} contains duplicate headers")
    missing = sorted(set(REQUIRED_COLUMNS) - set(header))
    if missing:
        raise ValueError(f"daily data is missing required columns: {missing}")
    try:
        frame = pd.read_csv(
            path,
            usecols=list(REQUIRED_COLUMNS),
            dtype={"date": "string"},
        )
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise ValueError(f"cannot parse {DAILY_FILENAME}: {exc}") from exc
    if frame.empty:
        raise ValueError(f"{DAILY_FILENAME} contains no data rows")
    parsed_dates = pd.to_datetime(frame["date"], errors="raise")
    normalized = parsed_dates.dt.strftime("%Y-%m-%d")
    if normalized.duplicated().any() or not parsed_dates.is_monotonic_increasing:
        raise ValueError("daily dates must be unique and strictly ascending")
    frame["date"] = normalized.astype("string")
    for column in SIGNAL_COLUMNS:
        values = pd.to_numeric(frame[column], errors="raise")
        if np.isinf(values.to_numpy(dtype=float, na_value=np.nan)).any():
            raise ValueError(f"daily column contains non-finite values: {column}")
        frame[column] = values.astype(float)
    return frame


def slice_for_cutoff(daily: pd.DataFrame, cutoff_key: str) -> pd.DataFrame:
    matches = np.flatnonzero(daily["date"].eq(cutoff_key).to_numpy())
    if len(matches) != 1:
        raise ValueError(f"daily_cutoff_key not found exactly once: {cutoff_key}")
    return daily.iloc[: int(matches[0]) + 1].copy().reset_index(drop=True)


def build_raw_signal(daily: pd.DataFrame) -> np.ndarray:
    spread = daily["TB0YWI0C"] - daily["TB7YWI0C"]
    movement = spread.diff().rolling(SIGNAL_WINDOW, min_periods=SIGNAL_WINDOW).sum()
    base = np.sign(movement).replace(0, -1).fillna(-1)
    return (-base).to_numpy(dtype="int8")


def make_labels(target: pd.Series) -> np.ndarray:
    close = pd.to_numeric(target, errors="coerce").to_numpy(dtype=float)
    labels = np.full(len(close), np.nan, dtype=float)
    if len(close) <= HORIZON:
        return labels
    valid = np.isfinite(close[:-HORIZON]) & np.isfinite(close[HORIZON:])
    delta = close[HORIZON:] - close[:-HORIZON]
    signed = np.sign(delta).astype(float)
    signed[~valid] = np.nan
    labels[:-HORIZON] = signed
    return labels


def predict_direction(daily: pd.DataFrame) -> int:
    cutoff_index = len(daily) - 1
    raw_signal = build_raw_signal(daily)
    if raw_signal[cutoff_index] not in (-1, 1):
        raise ValueError("a directional raw signal is unavailable at cutoff")
    labels = make_labels(daily[TARGET_COLUMN])
    end = cutoff_index - HORIZON
    start = max(0, end - ADAPT_WINDOW)
    if end <= start:
        raise ValueError("insufficient history for adaptive polarity")
    historical_signal = raw_signal[start:end]
    historical_labels = labels[start:end]
    valid = np.isin(historical_labels, (-1.0, 1.0))
    if int(valid.sum()) < MIN_OBSERVATIONS:
        raise ValueError(
            f"at least {MIN_OBSERVATIONS} realized labels are required"
        )
    edge = float(
        np.mean(historical_signal[valid] * historical_labels[valid])
    )
    polarity = 1 if edge >= 0.0 else -1
    direction = int(raw_signal[cutoff_index] * polarity)
    if direction not in (-1, 1):
        raise RuntimeError("algorithm returned an invalid direction")
    return direction


def generate_results(
    requests: Sequence[dict[str, str]], daily: pd.DataFrame
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw_request in requests:
        request = validate_request(raw_request)
        cutoff_data = slice_for_cutoff(daily, request["daily_cutoff_key"])
        direction = predict_direction(cutoff_data)
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
    if os.path.lexists(output):
        raise ValueError("output path already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent must be an existing directory")
    resolved_output = output.parent.resolve() / output.name
    resolved_data_dir = data_dir.resolve()
    if resolved_output == resolved_data_dir or resolved_data_dir in resolved_output.parents:
        raise ValueError("output path must not be inside data-dir")
    return output


def atomic_write_text(output: Path, content: str) -> None:
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(output):
            raise FileExistsError("output path appeared before atomic replacement")
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


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
    predict_parser = subparsers.add_parser("predict")
    predict_parser.add_argument("--request", type=Path, required=True)
    predict_parser.add_argument("--data-dir", type=Path, required=True)
    predict_parser.add_argument("--output", type=Path, required=True)
    backtest_parser = subparsers.add_parser("backtest")
    backtest_parser.add_argument("--requests", type=Path, required=True)
    backtest_parser.add_argument("--data-dir", type=Path, required=True)
    backtest_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = validate_output_path(args.output, args.data_dir)
        requests = (
            [load_request_json(args.request)]
            if args.command == "predict"
            else load_requests_csv(args.requests)
        )
        daily = load_daily_data(args.data_dir)
        results = generate_results(requests, daily)
        content = (
            _serialize_json(results[0])
            if args.command == "predict"
            else _serialize_csv(results)
        )
        atomic_write_text(output, content)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
