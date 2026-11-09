#!/usr/bin/env python3
"""Blackbox V2 7Y T+5 macro logistic scheme (213 admitted daily indicators)."""

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
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


SEED = 20261029
RANDOM_STATE = SEED
SCHEME_ID = "seven_y_t5_macro_logit_b213_v1"
DAILY_FILENAME = "daily_output.csv"
TARGET_COLUMN = "TB7YWI0C"
HORIZON = 5
MIN_ELIGIBLE = 500
FEATURE_COLUMNS = (
    "90000001",
    "90000007",
    "90000008",
    "90000009",
    "90000010",
    "90000011",
    "90000013",
    "90000016",
    "90000017",
    "90000019",
    "A4087688",
    "AUSHF00O",
    "BIASB5YD",
    "CCIT10YD",
    "CCIT1Y0D",
    "CCIT5Y0D",
    "CM210001",
    "CP126003",
    "CV630002",
    "CV630004",
    "CZ630001",
    "DEA10Y0D",
    "DIF10Y0D",
    "DRS00002",
    "IFCFE00C",
    "IFCFE00O",
    "M0000185",
    "M0000199",
    "M0000271",
    "M0017152",
    "M0020188",
    "M0020195",
    "M0020202",
    "M0020216",
    "M0020223",
    "M0041653",
    "M0041739",
    "M0062541",
    "M0067855",
    "M0075990",
    "M0100612",
    "M0101227",
    "M0101385",
    "M0109973",
    "M0117126",
    "M0143821",
    "M0161518",
    "M0331251",
    "M0342074",
    "M1001654",
    "M1001794",
    "M1001816",
    "M5216731",
    "N0105001",
    "N0107001",
    "N0108001",
    "N0109001",
    "N0110001",
    "N0111001",
    "N0112001",
    "N0113001",
    "N0205001",
    "N0207001",
    "N0208001",
    "N0209001",
    "N0210001",
    "N0211001",
    "N0212001",
    "N0213001",
    "N0605001",
    "N0607001",
    "N0608001",
    "N0609001",
    "N0610001",
    "N0705001",
    "N0707001",
    "N0708001",
    "N0709001",
    "N0805001",
    "N0807001",
    "N0808001",
    "N0809001",
    "N0810001",
    "N0811001",
    "N0812001",
    "N0813001",
    "N1105001",
    "N1108001",
    "N1110001",
    "N1205001",
    "N1208001",
    "N1210001",
    "N1305001",
    "N1307001",
    "N1308001",
    "N1309001",
    "N1310001",
    "N1405001",
    "N1407001",
    "N1408001",
    "N1409001",
    "N1505001",
    "N1508001",
    "N1510001",
    "N2691645",
    "S0031525",
    "S0105896",
    "S0105897",
    "S0106912",
    "S0142908",
    "S0142909",
    "S1179664",
    "S1180497",
    "S2707331",
    "S2707332",
    "S2707333",
    "S2707334",
    "S2707337",
    "S2707338",
    "S2707339",
    "S2707340",
    "S2707341",
    "S2707342",
    "S2707379",
    "S2707380",
    "S2707381",
    "S2707382",
    "S2707383",
    "S2707384",
    "S2707385",
    "S2707386",
    "S5005793",
    "S5005830",
    "S5005835",
    "S5005865",
    "S5005883",
    "S5005958",
    "S5005984",
    "S5006004",
    "S5006011",
    "S5470298",
    "S5470300",
    "S5470301",
    "S5470302",
    "S5470303",
    "S5470304",
    "S5470305",
    "S5470306",
    "S5470308",
    "S5470311",
    "S5470312",
    "S5470313",
    "S5470314",
    "S5470318",
    "S5470319",
    "S5470325",
    "S5470326",
    "S5470327",
    "S5470329",
    "S5470330",
    "S5470331",
    "S5470332",
    "S5470333",
    "S5470335",
    "S5470336",
    "S5470337",
    "S5470339",
    "S5470342",
    "S5470347",
    "S5470349",
    "S5470354",
    "S5470358",
    "S5470364",
    "S5470395",
    "S5470397",
    "S5470402",
    "S5470405",
    "S5470409",
    "S5470411",
    "S5470414",
    "S5470420",
    "S5470426",
    "S5470428",
    "S5470432",
    "S5470433",
    "S5470435",
    "S5470444",
    "S5470446",
    "S5470451",
    "S5470454",
    "S5470455",
    "S5470457",
    "S5707798",
    "S5714972",
    "SEMAC003",
    "SEMAC004",
    "SH000300",
    "SMACD001",
    "SMACD004",
    "SMACDS04",
    "SMACDS07",
    "SRSI1401",
    "SWD00001",
    "SWD00004",
    "SWD00005",
    "SWD00010",
    "SWR00004",
    "TB0YWI0C",
    "TB0YWIPC",
    "TCFE00BV",
    "TMAR2101",
    "TP630001",
    "X2879873",
)

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
        raise ValueError(f"{field_name} is not a valid date")
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
        raise ValueError(f"invalid request JSON: {exc}")
    return validate_request(request)


def load_requests_csv(path: Path) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read requests CSV: {exc}")
    with handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("requests CSV is empty")
        if len(header) != len(set(header)) or set(header) != set(REQUEST_FIELDS) or len(header) != len(REQUEST_FIELDS):
            raise ValueError("requests CSV header must exactly match the contract")
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
        raise ValueError(f"cannot read {DAILY_FILENAME}: {exc}")
    if not header or header[0] != "date":
        raise ValueError(f"{DAILY_FILENAME} first column must be date")
    if len(header) != len(set(header)):
        raise ValueError(f"{DAILY_FILENAME} contains duplicate headers")
    needed = ["date", TARGET_COLUMN]
    missing = sorted(set(needed) - set(header))
    if missing:
        raise ValueError(f"daily data is missing required columns: {missing}")
    absent_features = sorted(set(FEATURE_COLUMNS) - set(header))
    if absent_features:
        raise ValueError(f"daily data is missing required feature columns: {absent_features}")
    try:
        frame = pd.read_csv(
            path,
            usecols=["date", TARGET_COLUMN, *FEATURE_COLUMNS],
            dtype={"date": "string"},
            keep_default_na=False,
            na_values=[""],
        )
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise ValueError(f"cannot parse {DAILY_FILENAME}: {exc}")
    if frame.empty:
        raise ValueError(f"{DAILY_FILENAME} contains no data rows")
    if not frame["date"].str.fullmatch(_DATE_PATTERN, na=False).all():
        raise ValueError("daily dates must use strict YYYY-MM-DD format")
    parsed_dates = pd.to_datetime(frame["date"], errors="raise")
    normalized = parsed_dates.dt.strftime("%Y-%m-%d")
    if normalized.duplicated().any() or not parsed_dates.is_monotonic_increasing:
        raise ValueError("daily dates must be unique and strictly ascending")
    frame["date"] = normalized.astype("string")
    for column in (TARGET_COLUMN, *FEATURE_COLUMNS):
        try:
            values = pd.to_numeric(frame[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid numeric business value: {column}") from exc
        if pd.api.types.is_bool_dtype(values.dtype):
            raise ValueError(f"boolean business values are not allowed: {column}")
        array = values.to_numpy(dtype=float)
        if np.isinf(array).any():
            raise ValueError(f"non-finite business value: {column}")
        frame[column] = values
    return frame


def slice_for_cutoff(daily: pd.DataFrame, cutoff_key: str) -> pd.DataFrame:
    matches = np.flatnonzero(daily["date"].eq(cutoff_key).to_numpy())
    if len(matches) != 1:
        raise ValueError(f"daily_cutoff_key not found exactly once: {cutoff_key}")
    return daily.iloc[: int(matches[0]) + 1].copy().reset_index(drop=True)


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
    labels = make_labels(daily[TARGET_COLUMN])
    eligible = np.flatnonzero(np.isfinite(labels))
    if len(eligible) < MIN_ELIGIBLE:
        raise ValueError(
            f"insufficient matured history: {len(eligible)} eligible labels, "
            f"at least {MIN_ELIGIBLE} required"
        )
    use = eligible
    yy = labels[use].astype(int)
    if np.unique(yy).size < 2:
        raise ValueError("eligible history must contain both direction classes")
    features = daily.loc[:, list(FEATURE_COLUMNS)].to_numpy(dtype=np.float32)
    imputer = SimpleImputer(strategy="median").fit(features[use])
    x_train = imputer.transform(features[use])
    x_now = imputer.transform(features[[-1]])
    scaler = StandardScaler().fit(x_train)
    x_train = scaler.transform(x_train)
    x_now = scaler.transform(x_now)
    model = LogisticRegression(
        C=1.0,
        max_iter=2000,
        tol=1e-8,
        solver="lbfgs",
        random_state=RANDOM_STATE,
    )
    model.fit(x_train, (yy + 1).astype(int))
    probability = model.predict_proba(x_now)[0]
    classes = model.classes_
    p_down = float(probability[classes == 0][0]) if (classes == 0).any() else 0.0
    p_up = float(probability[classes == 2][0]) if (classes == 2).any() else 0.0
    if not (math.isfinite(p_down) and math.isfinite(p_up)):
        raise RuntimeError("model returned non-finite class probabilities")
    direction = 1 if p_up > p_down else -1
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
