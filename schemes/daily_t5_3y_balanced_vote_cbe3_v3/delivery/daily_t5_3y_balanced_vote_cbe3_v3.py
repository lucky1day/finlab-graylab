#!/usr/bin/env python3
"""Blackbox V2 Contract 1.0 frozen daily T+5 vote scheme template."""

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


SEED = 20260919
RANDOM_STATE = SEED
HORIZON = 5
SCHEME_CONFIG: dict[str, Any] = {'scheme_id': 'daily_t5_3y_balanced_vote_cbe3_v3',
 'target': 'TB3YWI0C',
 'quorum': 1,
 'daily_columns': ['TB0YWI0C', 'TB1YWI0C', 'TB3YWI0C', 'TB5YWI0C', 'TB7YWI0C'],
 'signals': ['5Y_amp_rev10',
             'tp_5Y1Y_anti120',
             'spr_zrev_10Y7Y_20',
             '10Y_amp_rev5',
             'spr_10Y7Y_anti40'],
 'members': [{'config_id': 'dyn_418d3d738cdf05c9',
              'signal': '5Y_amp_rev10',
              'window': 10,
              'min_observations': 5,
              'edge_threshold': 0.3},
             {'config_id': 'dyn_7ac645f0e2eb10de',
              'signal': 'tp_5Y1Y_anti120',
              'window': 10,
              'min_observations': 5,
              'edge_threshold': 0.25},
             {'config_id': 'dyn_4e31297f49782edb',
              'signal': 'spr_zrev_10Y7Y_20',
              'window': 15,
              'min_observations': 5,
              'edge_threshold': 0.25},
             {'config_id': 'dyn_042baa389573bd9f',
              'signal': '10Y_amp_rev5',
              'window': 20,
              'min_observations': 10,
              'edge_threshold': 0.1},
             {'config_id': 'dyn_ccb0a55f394aad01',
              'signal': 'spr_10Y7Y_anti40',
              'window': 10,
              'min_observations': 5,
              'edge_threshold': 0.25}],
 'min_rows': 320}
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
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KEY_RE = re.compile(r"[0-9]{6}")


def _config(name: str) -> Any:
    if name not in SCHEME_CONFIG:
        raise RuntimeError(f"missing frozen scheme configuration: {name}")
    return SCHEME_CONFIG[name]


SCHEME_ID = str(_config("scheme_id"))
TARGET = str(_config("target"))
QUORUM = int(_config("quorum"))
MEMBERS = tuple(_config("members"))
SIGNALS = tuple(str(value) for value in _config("signals"))
DAILY_COLUMNS = tuple(str(value) for value in _config("daily_columns"))
MIN_ROWS = int(_config("min_rows"))
if len(MEMBERS) != 5 or QUORUM != 1:
    raise RuntimeError("invalid frozen five-member vote configuration")


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
    if any(not isinstance(value[field], str) for field in REQUEST_FIELDS):
        raise ValueError("all request values must be strings")
    if not value["request_id"].strip():
        raise ValueError("request_id must not be blank")
    predict = _strict_date(value["predict_date"], "predict_date")
    feature = _strict_date(value["feature_date"], "feature_date")
    target = _strict_date(value["target_date"], "target_date")
    cutoff = _strict_date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not (feature <= predict <= target and feature < target):
        raise ValueError(
            "dates must satisfy feature_date <= predict_date <= target_date and feature_date < target_date"
        )
    if cutoff != feature:
        raise ValueError("daily_cutoff_key must equal feature_date for T+5")
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
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
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
    if not rows:
        raise ValueError("requests CSV must contain at least one request")
    request_ids = [row["request_id"] for row in rows]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("request_id values must be unique within a batch")
    return rows


def _read_header(path: Path) -> list[str]:
    if not path.is_file():
        raise ValueError(f"required data file is missing: {path.name}")
    try:
        header = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns.tolist()
    except Exception as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not header or len(header) != len(set(header)):
        raise ValueError(f"{path.name} has an empty or duplicate header")
    return header


def _load_catalog(path: Path, daily_header: Sequence[str]) -> None:
    header = _read_header(path)
    if header != ["indicators_code", "frequency", "factor_version"]:
        raise ValueError(
            "factor_catalog.csv columns must be exactly indicators_code,frequency,factor_version"
        )
    catalog = pd.read_csv(
        path,
        encoding="utf-8-sig",
        dtype="string",
        keep_default_na=False,
    )
    if catalog.empty:
        raise ValueError("factor_catalog.csv must not be empty")
    for column in header:
        catalog[column] = catalog[column].str.strip()
        if catalog[column].eq("").any():
            raise ValueError(f"factor_catalog.csv contains blank {column}")
    if catalog["indicators_code"].duplicated().any():
        raise ValueError("factor_catalog.csv indicators_code must be globally unique")
    daily_codes = catalog.loc[catalog["frequency"].eq("daily"), "indicators_code"].tolist()
    if daily_codes != list(daily_header[1:]):
        raise ValueError("factor_catalog.csv daily order does not match daily_output.csv")
    frequency_by_code = catalog.set_index("indicators_code")["frequency"].to_dict()
    invalid = [column for column in DAILY_COLUMNS if frequency_by_code.get(column) != "daily"]
    if invalid:
        raise ValueError(f"factor_catalog.csv frequency mismatch: {invalid[:10]}")


def _load_calendar(path: Path) -> pd.DataFrame:
    header = _read_header(path)
    if header != ["rdate", "week_id"]:
        raise ValueError("api_wind_date.csv columns must be exactly rdate,week_id")
    calendar = pd.read_csv(
        path,
        encoding="utf-8-sig",
        dtype="string",
        keep_default_na=False,
    )
    if calendar.empty:
        raise ValueError("api_wind_date.csv must not be empty")
    rdates = calendar["rdate"].str.strip()
    week_ids = calendar["week_id"].str.strip()
    if not rdates.str.fullmatch(DATE_RE).all() or not week_ids.str.fullmatch(KEY_RE).all():
        raise ValueError("api_wind_date.csv contains invalid keys")
    parsed = pd.to_datetime(rdates, format="%Y-%m-%d", errors="raise").dt.normalize()
    if parsed.duplicated().any() or not parsed.is_monotonic_increasing:
        raise ValueError("api_wind_date.csv rdate must be unique and strictly ascending")
    calendar["rdate"] = parsed
    calendar["week_id"] = week_ids
    return calendar


def _load_daily(path: Path, catalog_path: Path, calendar: pd.DataFrame) -> pd.DataFrame:
    header = _read_header(path)
    if header[0] != "date":
        raise ValueError("daily_output.csv must start with date")
    missing = sorted(set(DAILY_COLUMNS).difference(header))
    if missing:
        raise ValueError(f"daily_output.csv is missing required columns: {missing}")
    _load_catalog(catalog_path, header)
    frame = pd.read_csv(
        path,
        encoding="utf-8-sig",
        usecols=["date", *DAILY_COLUMNS],
        dtype="string",
        keep_default_na=False,
        low_memory=False,
    )
    if frame.empty:
        raise ValueError("daily_output.csv must not be empty")
    raw_dates = frame["date"].str.strip()
    if not raw_dates.str.fullmatch(DATE_RE).all():
        raise ValueError("daily_output.csv contains invalid dates")
    parsed = pd.to_datetime(raw_dates, format="%Y-%m-%d", errors="raise").dt.normalize()
    if parsed.duplicated().any() or not parsed.is_monotonic_increasing:
        raise ValueError("daily_output.csv date must be unique and strictly ascending")
    if (parsed.dt.dayofweek >= 5).any():
        raise ValueError("daily_output.csv contains weekend rows")
    mapped_dates = set(calendar["rdate"])
    if any(value not in mapped_dates for value in parsed):
        raise ValueError("daily_output.csv contains dates absent from api_wind_date.csv")
    frame["date"] = parsed
    for column in DAILY_COLUMNS:
        raw = frame[column].str.strip()
        numeric = pd.to_numeric(raw.mask(raw.eq("")), errors="coerce")
        invalid = raw.ne("") & numeric.isna()
        finite = numeric.dropna().to_numpy(dtype="float64")
        if invalid.any() or np.isinf(finite).any():
            raise ValueError(f"daily_output.csv contains invalid values in {column}")
        frame[column] = numeric.astype("float32")
    return frame


def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    calendar = _load_calendar(data_dir / "api_wind_date.csv")
    daily = _load_daily(
        data_dir / "daily_output.csv",
        data_dir / "factor_catalog.csv",
        calendar,
    )
    return calendar, daily


def _safe_sign(values: pd.Series) -> pd.Series:
    return np.sign(values).replace(0, -1).fillna(-1).astype("int8")


def _sticky_extreme(raw: np.ndarray, index: pd.Index) -> pd.Series:
    return (
        pd.Series(raw, index=index)
        .replace(0, np.nan)
        .ffill()
        .fillna(-1)
        .astype("int8")
    )


def build_signals(daily: pd.DataFrame) -> dict[str, np.ndarray]:
    close = {
        "1Y": daily["TB1YWI0C"],
        "3Y": daily["TB3YWI0C"],
        "5Y": daily["TB5YWI0C"],
        "7Y": daily["TB7YWI0C"],
        "10Y": daily["TB0YWI0C"],
    }
    returns = {name: value.pct_change() for name, value in close.items()}
    output: dict[str, np.ndarray] = {}
    if "5Y_amp_rev10" in SIGNALS:
        movement = returns["5Y"].rolling(10).sum()
        volatility = returns["5Y"].rolling(60).std() * math.sqrt(10)
        z_value = movement / volatility.replace(0, np.nan)
        raw = np.where(z_value > 1.5, -1, np.where(z_value < -1.5, 1, 0)).astype(int)
        output["5Y_amp_rev10"] = _sticky_extreme(raw, daily.index).to_numpy(dtype="int8")
    if "10Y_amp_rev5" in SIGNALS:
        movement = returns["10Y"].rolling(5).sum()
        volatility = returns["10Y"].rolling(60).std() * math.sqrt(5)
        z_value = movement / volatility.replace(0, np.nan)
        raw = np.where(z_value > 1.5, -1, np.where(z_value < -1.5, 1, 0)).astype(int)
        output["10Y_amp_rev5"] = _sticky_extreme(raw, daily.index).to_numpy(dtype="int8")
    if "tp_5Y1Y_anti120" in SIGNALS:
        change = (close["5Y"] - close["1Y"]).diff()
        output["tp_5Y1Y_anti120"] = (-_safe_sign(change.rolling(120).sum())).to_numpy(dtype="int8")
    if "spr_zrev_10Y7Y_20" in SIGNALS:
        spread = close["10Y"] - close["7Y"]
        mean = spread.rolling(20).mean()
        std = spread.rolling(20).std()
        z_value = (spread - mean) / std.replace(0, np.nan)
        raw = np.where(z_value > 1.5, -1, np.where(z_value < -1.5, 1, 0)).astype(int)
        output["spr_zrev_10Y7Y_20"] = _sticky_extreme(raw, daily.index).to_numpy(dtype="int8")
    if "spr_10Y7Y_anti40" in SIGNALS:
        change = (close["10Y"] - close["7Y"]).diff()
        output["spr_10Y7Y_anti40"] = (-_safe_sign(change.rolling(40).sum())).to_numpy(dtype="int8")
    if "3Y_lead5Y_lag3" in SIGNALS:
        output["3Y_lead5Y_lag3"] = _safe_sign(returns["3Y"].shift(3)).to_numpy(dtype="int8")
    if "dr007_dir60" in SIGNALS:
        dr007 = daily["DR007IBC"].ffill()
        output["dr007_dir60"] = _safe_sign(dr007.diff().rolling(60).sum()).to_numpy(dtype="int8")
    if "tp_3Y1Y_z_anti252" in SIGNALS:
        premium = close["3Y"] - close["1Y"]
        output["tp_3Y1Y_z_anti252"] = (
            -_safe_sign(premium - premium.rolling(252).mean())
        ).to_numpy(dtype="int8")
    if "3Y_above_ma60" in SIGNALS:
        output["3Y_above_ma60"] = _safe_sign(
            close["3Y"] - close["3Y"].rolling(60).mean()
        ).to_numpy(dtype="int8")
    if "3Y_z_anti60" in SIGNALS:
        output["3Y_z_anti60"] = (
            -_safe_sign(close["3Y"] - close["3Y"].rolling(60).mean())
        ).to_numpy(dtype="int8")
    if set(output) != set(SIGNALS):
        raise RuntimeError("unknown or missing frozen signal implementation")
    return output


def build_target(daily: pd.DataFrame) -> np.ndarray:
    close = daily[TARGET].to_numpy(dtype="float64")
    target = np.full(len(close), np.nan, dtype="float32")
    if len(close) > HORIZON:
        valid = np.isfinite(close[:-HORIZON]) & np.isfinite(close[HORIZON:])
        labels = np.sign(close[HORIZON:] - close[:-HORIZON]).astype("float32")
        labels[~valid] = np.nan
        target[:-HORIZON] = labels
    return target


def history_arrays(
    signal_matrix: np.ndarray,
    target: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    valid = np.isfinite(target)[:, None] & (target[:, None] != 0) & (signal_matrix != 0)
    correctness = np.where(valid, signal_matrix * target[:, None], 0).astype("float32")
    scores = np.vstack(
        [
            np.zeros((1, signal_matrix.shape[1]), dtype="float32"),
            np.cumsum(correctness, axis=0),
        ]
    )
    counts = np.vstack(
        [
            np.zeros((1, signal_matrix.shape[1]), dtype="int32"),
            np.cumsum(valid, axis=0, dtype="int32"),
        ]
    )
    return scores, counts


def prepare_model(daily: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    signal_map = build_signals(daily)
    signal_matrix = np.column_stack([signal_map[name] for name in SIGNALS]).astype("int8")
    target = build_target(daily)
    scores, counts = history_arrays(signal_matrix, target)
    return signal_matrix, scores, counts


def predict_components_at_index(
    index: int,
    signal_matrix: np.ndarray,
    scores: np.ndarray,
    counts: np.ndarray,
) -> tuple[int, tuple[int, ...]]:
    if index < MIN_ROWS - 1:
        raise ValueError(f"daily cutoff has fewer than {MIN_ROWS} required history rows")
    end = max(index - HORIZON, 0)
    members: list[int] = []
    for column, member in enumerate(MEMBERS):
        window = int(member["window"])
        start = max(end - window, 0)
        count = int(counts[end, column] - counts[start, column])
        score = float(scores[end, column] - scores[start, column])
        edge = score / count if count else 0.0
        threshold = float(member["edge_threshold"])
        if count < int(member["min_observations"]):
            polarity = 0
        elif edge > threshold:
            polarity = 1
        elif edge < -threshold:
            polarity = -1
        else:
            polarity = 0
        members.append(int(signal_matrix[index, column]) * polarity)
    vote = int(sum(members))
    direction = 1 if vote >= QUORUM else (-1 if vote <= -QUORUM else 0)
    return direction, tuple(members)


def request_index(
    request: dict[str, str],
    calendar: pd.DataFrame,
    daily: pd.DataFrame,
) -> int:
    cutoff = pd.Timestamp(request["daily_cutoff_key"])
    calendar_matches = np.flatnonzero(calendar["rdate"].eq(cutoff).to_numpy())
    if len(calendar_matches) != 1:
        raise ValueError("daily_cutoff_key must exist exactly once in api_wind_date.csv")
    calendar_index = int(calendar_matches[0])
    if str(calendar.iloc[calendar_index]["week_id"]) != request["weekly_cutoff_key"]:
        raise ValueError("daily_cutoff_key does not map to weekly_cutoff_key")
    daily_matches = np.flatnonzero(daily["date"].eq(cutoff).to_numpy())
    if len(daily_matches) != 1:
        raise ValueError("daily_cutoff_key must exist exactly once in daily_output.csv")
    daily_index = int(daily_matches[0])
    target_date = pd.Timestamp(request["target_date"])
    target_matches = np.flatnonzero(daily["date"].eq(target_date).to_numpy())
    if len(target_matches) == 1:
        if int(target_matches[0]) - daily_index != HORIZON:
            raise ValueError("target_date is not the fifth subsequent daily trading date")
    elif target_date <= pd.Timestamp(daily.iloc[-1]["date"]):
        raise ValueError("historical target_date is absent from daily_output.csv")
    return daily_index


def independent_components(
    index: int,
    daily: pd.DataFrame,
) -> tuple[int, tuple[int, ...]]:
    truncated = daily.iloc[: index + 1].copy()
    matrix, scores, counts = prepare_model(truncated)
    return predict_components_at_index(len(truncated) - 1, matrix, scores, counts)


def generate(
    requests: Sequence[dict[str, str]],
    data: tuple[pd.DataFrame, pd.DataFrame],
) -> list[dict[str, Any]]:
    calendar, daily = data
    signal_matrix, scores, counts = prepare_model(daily)
    output: list[dict[str, Any]] = []
    components: list[tuple[int, ...]] = []
    indices: list[int] = []
    for raw in requests:
        request = validate_request(raw)
        index = request_index(request, calendar, daily)
        direction, member_signals = predict_components_at_index(
            index, signal_matrix, scores, counts
        )
        if type(direction) is not int or direction not in (-1, 0, 1):
            raise RuntimeError("model returned an invalid direction")
        output.append(
            {
                **{field: request[field] for field in OUTPUT_FIELDS[:-1]},
                "predicted_direction": direction,
            }
        )
        components.append(member_signals)
        indices.append(index)
    check_indices = sorted({0, len(requests) // 2, len(requests) - 1})
    for position in check_indices:
        repeated, repeated_members = independent_components(indices[position], daily)
        if (
            repeated != output[position]["predicted_direction"]
            or repeated_members != components[position]
        ):
            raise RuntimeError(
                f"batch independent-cutoff self-check failed at request index {position}"
            )
    return output


def validate_output(output: Path, data_dir: Path) -> Path:
    if os.path.lexists(output):
        raise ValueError("output path already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent must be an existing directory")
    resolved = output.parent.resolve() / output.name
    data_root = data_dir.resolve()
    if resolved == data_root or data_root in resolved.parents:
        raise ValueError("output path must not be inside data-dir")
    return output


def atomic_write(output: Path, content: str) -> None:
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(output):
            raise FileExistsError("output path appeared before atomic replacement")
        os.replace(temporary, output)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def serialize_csv(rows: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(OUTPUT_FIELDS)
    writer.writerows([[row[field] for field in OUTPUT_FIELDS] for row in rows])
    return buffer.getvalue()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    predict = commands.add_parser("predict")
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--data-dir", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--requests", type=Path, required=True)
    backtest.add_argument("--data-dir", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    args = parser().parse_args(argv)
    try:
        output = validate_output(args.output, args.data_dir)
        requests = (
            [load_request(args.request)]
            if args.command == "predict"
            else load_requests(args.requests)
        )
        rows = generate(requests, load_data(args.data_dir))
        content = (
            json.dumps(rows[0], ensure_ascii=False, separators=(",", ":")) + "\n"
            if args.command == "predict"
            else serialize_csv(rows)
        )
        atomic_write(output, content)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
