"""V8 Blackbox V2 self-contained XGBoost strategy."""
from __future__ import annotations

"""Blackbox V2 contract functions for embedding into a single strategy file.

A strategy calls run_cli(predict_one, columns_by_frequency, predict_batch=None).
See CONTRACT_README.md before embedding; this file is not a deliverable strategy.
"""

import argparse
import csv
import datetime as dt
import json
import math
import numbers
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import NamedTuple

import numpy as np
import pandas as pd


REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date", "predicted_direction",
)
TABLES = {
    "daily": ("daily_output.csv", "date", "daily_cutoff_key"),
    "weekly": ("weekly_output.csv", "week_id", "weekly_cutoff_key"),
    "monthly": ("monthly_output.csv", "month_id", "monthly_cutoff_key"),
}
DATE_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
KEY_PATTERN = r"[0-9]{6}"
MAX_OUTPUT_BYTES = 50 * 1024 * 1024


class Prediction(NamedTuple):
    direction: int
    score: float | None = None
    probability: float | None = None


def _date_ok(value):
    if not isinstance(value, str) or re.fullmatch(DATE_PATTERN, value) is None:
        return False
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _key_ok(value):
    return isinstance(value, str) and re.fullmatch(KEY_PATTERN, value) is not None


def _fields_ok(fields, expected, what):
    if len(fields) != len(expected) or len(set(fields)) != len(fields) or set(fields) != set(expected):
        raise ValueError(f"{what} must contain exactly the required fields")


def _request(row):
    if not isinstance(row, dict):
        raise ValueError("request must be an object")
    _fields_ok(list(row), REQUEST_FIELDS, "request")
    if any(not isinstance(row[k], str) for k in REQUEST_FIELDS):
        raise ValueError("all request fields must be strings")
    if not row["request_id"].strip():
        raise ValueError("request_id must be nonempty")
    for key in ("predict_date", "feature_date", "target_date", "daily_cutoff_key"):
        if not _date_ok(row[key]):
            raise ValueError(f"invalid {key}")
    for key in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not _key_ok(row[key]):
            raise ValueError(f"invalid {key}")
    feature, predict, target = row["feature_date"], row["predict_date"], row["target_date"]
    if not (feature <= predict <= target and feature < target):
        raise ValueError("request dates are out of order")
    return {k: row[k] for k in REQUEST_FIELDS}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_requests(path, mode):
    with path.open("r", encoding="utf-8", newline="") as stream:
        if mode == "predict":
            return [_request(json.load(stream, object_pairs_hook=_unique_pairs))]
        reader = csv.reader(stream, strict=True)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("empty request CSV") from exc
        _fields_ok(header, REQUEST_FIELDS, "request CSV header")
        rows, ids = [], set()
        for values in reader:
            if len(values) != len(header):
                raise ValueError("incomplete request CSV row")
            row = _request(dict(zip(header, values)))
            if row["request_id"] in ids:
                raise ValueError("duplicate request_id")
            ids.add(row["request_id"])
            rows.append(row)
        if not rows:
            raise ValueError("empty request batch")
        return rows


def _header(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        try:
            header = next(csv.reader(stream, strict=True))
        except StopIteration as exc:
            raise ValueError(f"empty input: {path.name}") from exc
    if not header or any(not name for name in header) or len(set(header)) != len(header):
        raise ValueError(f"invalid or duplicate headers: {path.name}")
    return header


def select_catalog_columns(data_dir, frequency, versions):
    """Select actual factor column names from the platform catalog."""
    if frequency not in TABLES or not versions:
        raise ValueError("invalid factor catalog selection")
    path = Path(data_dir) / "factor_catalog.csv"
    header = _header(path)
    if header != ["indicators_code", "frequency", "factor_version"]:
        raise ValueError("invalid factor catalog header")
    catalog = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    if catalog.empty or (catalog == "").any().any() or catalog["indicators_code"].duplicated().any():
        raise ValueError("invalid factor catalog rows")
    selected = catalog.loc[
        (catalog["frequency"] == frequency) & catalog["factor_version"].isin(versions),
        "indicators_code",
    ].tolist()
    if not selected:
        raise ValueError("no matching catalog columns")
    return selected


def _load_tables(data_dir, columns_by_frequency):
    if not isinstance(columns_by_frequency, dict) or not columns_by_frequency:
        raise ValueError("at least one consumed table must be declared")
    if set(columns_by_frequency) - set(TABLES):
        raise ValueError("unknown consumed frequency")
    tables, positions = {}, {}
    for frequency, requested in columns_by_frequency.items():
        filename, key, _ = TABLES[frequency]
        if not isinstance(requested, (list, tuple)) or any(not isinstance(c, str) or not c for c in requested):
            raise ValueError(f"invalid columns for {frequency}")
        if key in requested or len(set(requested)) != len(requested):
            raise ValueError(f"duplicate columns for {frequency}")
        path = Path(data_dir) / filename
        header = _header(path)
        if header[0] != key:
            raise ValueError(f"incorrect time-key column: {filename}")
        if any(column not in header for column in requested):
            raise ValueError(f"missing consumed column in {filename}")
        frame = pd.read_csv(path, usecols=[key, *requested], dtype=str, keep_default_na=False, na_filter=False)
        if frame.empty:
            raise ValueError(f"empty consumed table: {filename}")
        keys = frame[key].tolist()
        validator = _date_ok if frequency == "daily" else _key_ok
        if any(not validator(value) for value in keys) or len(set(keys)) != len(keys):
            raise ValueError(f"invalid or duplicate time key in {filename}")
        for column in requested:
            values = pd.to_numeric(frame[column].replace("", np.nan), errors="raise")
            if not np.isfinite(values.dropna().to_numpy(dtype=float)).all():
                raise ValueError(f"non-finite value in {filename}: {column}")
            frame[column] = values
        tables[frequency] = frame.loc[:, [key, *requested]]
        positions[frequency] = {value: index for index, value in enumerate(keys)}
    return tables, positions


def _load_calendar(data_dir):
    path = Path(data_dir) / "api_wind_date.csv"
    if _header(path) != ["rdate", "week_id"]:
        raise ValueError("invalid calendar header")
    calendar = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    dates = calendar["rdate"].tolist()
    weeks = calendar["week_id"].tolist()
    if not dates or any(not _date_ok(v) for v in dates) or len(set(dates)) != len(dates):
        raise ValueError("invalid calendar dates")
    if any(not _key_ok(v) for v in weeks):
        raise ValueError("invalid calendar week keys")
    return calendar, dict(zip(dates, weeks))


def _cutoff_frames(request, tables, positions, calendar):
    if calendar is not None and calendar[1].get(request["daily_cutoff_key"]) != request["weekly_cutoff_key"]:
        raise ValueError("daily cutoff does not map to weekly cutoff")
    result = {}
    for frequency, frame in tables.items():
        _, _, cutoff_field = TABLES[frequency]
        cutoff = request[cutoff_field]
        if cutoff not in positions[frequency]:
            raise ValueError(f"cutoff absent in {frequency}: {cutoff}")
        result[frequency] = frame.iloc[:positions[frequency][cutoff] + 1].copy(deep=True)
    if calendar is not None:
        frame = calendar[0]
        result["calendar"] = frame.loc[frame["rdate"] <= request["daily_cutoff_key"]].copy(deep=True)
    return result


def _prediction(value):
    if not isinstance(value, Prediction):
        raise ValueError("strategy must return Prediction")
    if isinstance(value.direction, bool) or not isinstance(value.direction, numbers.Integral) or value.direction not in (-1, 0, 1):
        raise ValueError("invalid predicted direction")
    for name, number in (("score", value.score), ("probability", value.probability)):
        if number is not None and (isinstance(number, bool) or not isinstance(number, numbers.Real) or not math.isfinite(number)):
            raise ValueError(f"invalid internal {name}")
    if value.probability is not None and not 0 <= value.probability <= 1:
        raise ValueError("invalid probability")
    return Prediction(int(value.direction), value.score, value.probability)


def _compute(rows, tables, positions, calendar, predict_one, predict_batch):
    # Validate every request's own cutoffs before any optimization or output.
    for row in rows:
        if calendar is not None and calendar[1].get(row["daily_cutoff_key"]) != row["weekly_cutoff_key"]:
            raise ValueError("daily cutoff does not map to weekly cutoff")
        for frequency in tables:
            if row[TABLES[frequency][2]] not in positions[frequency]:
                raise ValueError(f"cutoff absent in {frequency}")

    def independent(row):
        frames = _cutoff_frames(row, tables, positions, calendar)
        return _prediction(predict_one(dict(row), frames))

    if predict_batch is None:
        predictions = [independent(row) for row in rows]
    else:
        batch_rows = [dict(row) for row in rows]
        batch_tables = {frequency: frame.copy(deep=True) for frequency, frame in tables.items()}
        if calendar is not None:
            batch_tables["calendar"] = calendar[0].copy(deep=True)
        predictions = list(predict_batch(batch_rows, batch_tables))
        if len(predictions) != len(rows):
            raise ValueError("batch prediction count mismatch")
        predictions = [_prediction(value) for value in predictions]
        for index in sorted({0, len(rows) // 2, len(rows) - 1}):
            if predictions[index] != independent(rows[index]):
                raise ValueError(f"batch and independent predictions differ at row {index}")
    return [
        {**{field: row[field] for field in RESULT_FIELDS[:-1]}, "predicted_direction": prediction.direction}
        for row, prediction in zip(rows, predictions)
    ]


def _write_atomic(output, mode, results):
    output = Path(output)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            if mode == "predict":
                json.dump(results[0], stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            else:
                writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(results)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.getsize(temporary) > MAX_OUTPUT_BYTES:
            raise ValueError("output exceeds 50 MiB")
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def run_cli(predict_one, columns_by_frequency, predict_batch=None, require_week_mapping=False, argv=None, backtest_predict_one=None):
    """Return process exit code; stdout remains empty. Caller should sys.exit(run_cli(...))."""
    sys.stdout.flush()
    original_stdout = os.dup(1)
    try:
        os.dup2(2, 1)
        return _run_cli(predict_one, columns_by_frequency, predict_batch, require_week_mapping, argv, backtest_predict_one)
    finally:
        sys.stdout.flush()
        os.dup2(original_stdout, 1)
        os.close(original_stdout)


def _run_cli(predict_one, columns_by_frequency, predict_batch, require_week_mapping, argv, backtest_predict_one=None):
    parser = argparse.ArgumentParser(add_help=False)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("predict", "backtest"):
        command = commands.add_parser(mode, add_help=False)
        command.add_argument("--request" if mode == "predict" else "--requests", required=True)
        command.add_argument("--data-dir", required=True)
        command.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
        request_path = Path(args.request if args.mode == "predict" else args.requests)
        rows = _load_requests(request_path, args.mode)
        data_dir = Path(args.data_dir)
        columns = columns_by_frequency(data_dir) if callable(columns_by_frequency) else columns_by_frequency
        tables, positions = _load_tables(data_dir, columns)
        calendar = _load_calendar(data_dir) if require_week_mapping else None
        one = predict_one
        if args.mode == "backtest" and backtest_predict_one is not None:
            one = backtest_predict_one
            print("historical diagnostic replay: current fixed factor universe; not historical live performance", file=sys.stderr)
        results = _compute(rows, tables, positions, calendar, one,
                           predict_batch if args.mode == "backtest" else None)
        _write_atomic(args.output, args.mode, results)
        return 0
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


from typing import Callable

import numpy as np
import pandas as pd


OperatorFunction = Callable[..., pd.Series]


class OperatorSpec:
    __slots__ = ("name", "category", "family", "arity", "uses_window", "root_only", "function")

    def __init__(self, name, category, family, arity, uses_window, root_only, function):
        self.name = name
        self.category = category
        self.family = family
        self.arity = arity
        self.uses_window = uses_window
        self.root_only = root_only
        self.function = function


OPERATOR_REGISTRY: dict[str, OperatorSpec] = {}
OPERATOR_FAMILIES = {
    "mapping",
    "time_series",
    "regression",
    "arithmetic",
    "logic",
}


def register_operator(
    name: str,
    category: str,
    arity: int,
    uses_window: bool = False,
    *,
    family: str | None = None,
    root_only: bool = False,
):
    if category not in {"unary", "lagged", "rolling", "binary", "ternary"}:
        raise ValueError(f"Unsupported operator category: {category}")
    if arity not in {1, 2, 3}:
        raise ValueError("Operator arity must be 1, 2 or 3")
    resolved_family = family or (
        "time_series"
        if category in {"lagged", "rolling"}
        else "mapping"
        if category == "unary"
        else "arithmetic"
    )
    if resolved_family not in OPERATOR_FAMILIES:
        raise ValueError(f"Unsupported operator family: {resolved_family}")

    def decorator(function: OperatorFunction) -> OperatorFunction:
        if name in OPERATOR_REGISTRY:
            raise ValueError(f"Duplicate operator registration: {name}")
        OPERATOR_REGISTRY[name] = OperatorSpec(
            name,
            category,
            resolved_family,
            arity,
            uses_window,
            root_only,
            function,
        )
        return function

    return decorator


def _required_window(window: int | None) -> int:
    if window is None or int(window) < 1:
        raise ValueError("This operator requires a positive window")
    return int(window)


def _linear_slope(values: np.ndarray) -> float:
    y = np.asarray(values, dtype=float)
    valid = np.isfinite(y)
    if valid.sum() < 2:
        return np.nan
    x = np.arange(len(y), dtype=float)[valid]
    y = y[valid]
    centered_x = x - x.mean()
    denominator = float(np.dot(centered_x, centered_x))
    if denominator <= 1e-12:
        return np.nan
    centered_y = y - y.mean()
    if len(values) == 5 and valid.all():
        # Preserve the original OpenBLAS accumulation order: a one-ULP
        # difference before a rolling rank can change a tied observation.
        terms = centered_x * centered_y
        return float(((terms[0] + (terms[1] + terms[3])) + terms[4]) / denominator)
    return float(np.dot(centered_x, centered_y) / denominator)


def _skewness(values: np.ndarray) -> float:
    sample = np.asarray(values, dtype=float)
    sample = sample[np.isfinite(sample)]
    if len(sample) < 3:
        return np.nan
    centered = sample - sample.mean()
    variance = float(np.mean(centered**2))
    if variance <= 1e-24:
        return np.nan
    biased = float(np.mean(centered**3) / variance**1.5)
    size = len(sample)
    return float(np.sqrt(size * (size - 1)) / (size - 2) * biased)


def _excess_kurtosis(values: np.ndarray) -> float:
    sample = np.asarray(values, dtype=float)
    sample = sample[np.isfinite(sample)]
    if len(sample) < 4:
        return np.nan
    centered = sample - sample.mean()
    variance = float(np.mean(centered**2))
    if variance <= 1e-24:
        return np.nan
    biased_excess = float(np.mean(centered**4) / variance**2 - 3.0)
    size = len(sample)
    return float(
        (size - 1)
        / ((size - 2) * (size - 3))
        * ((size + 1) * biased_excess + 6.0)
    )


def _linear_decay(values: np.ndarray) -> float:
    sample = np.asarray(values, dtype=float)
    weights = np.arange(1.0, len(sample) + 1.0)
    valid = np.isfinite(sample)
    if not valid.any():
        return np.nan
    return float(np.dot(sample[valid], weights[valid]) / weights[valid].sum())


def _binary_sign_entropy(values: np.ndarray) -> float:
    changes = np.asarray(values, dtype=float)
    changes = changes[np.isfinite(changes) & (changes != 0.0)]
    if len(changes) < 2:
        return np.nan
    positive = float(np.mean(changes > 0.0))
    if positive in {0.0, 1.0}:
        return 0.0
    return float(
        -(positive * np.log2(positive) + (1.0 - positive) * np.log2(1.0 - positive))
    )


def _largest_signed_shock(values: np.ndarray) -> float:
    changes = np.asarray(values, dtype=float)
    valid = changes[np.isfinite(changes)]
    if not len(valid):
        return np.nan
    return float(valid[np.argmax(np.abs(valid))])


def _latest_extreme_age(values: np.ndarray, *, find_max: bool) -> float:
    sample = np.asarray(values, dtype=float)
    valid = np.isfinite(sample)
    if not valid.any():
        return np.nan
    valid_positions = np.flatnonzero(valid)
    valid_values = sample[valid]
    extreme = np.nanmax(valid_values) if find_max else np.nanmin(valid_values)
    latest_position = valid_positions[valid_values == extreme][-1]
    return float(len(sample) - 1 - latest_position)


@register_operator("neg", "unary", 1)
def _neg(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return -left


@register_operator("identity", "unary", 1)
def _identity(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left


@register_operator("abs", "unary", 1)
def _abs(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left.abs()


@register_operator("signed_log", "unary", 1)
def _signed_log(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return np.sign(left) * np.log1p(left.abs())


@register_operator("signed_sqrt", "unary", 1)
def _signed_sqrt(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return np.sign(left) * np.sqrt(left.abs())


@register_operator("signed_square", "unary", 1)
def _signed_square(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    return np.sign(left) * left.abs().pow(2)


@register_operator("tanh", "unary", 1)
def _tanh(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return pd.Series(np.tanh(left), index=left.index, name=left.name)


@register_operator("sign", "unary", 1)
def _sign(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return np.sign(left)


@register_operator("positive_part", "unary", 1)
def _positive_part(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    return left.clip(lower=0.0)


@register_operator("negative_part", "unary", 1)
def _negative_part(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    return left.clip(upper=0.0)


@register_operator("lag", "lagged", 1, uses_window=True)
def _lag(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left.shift(_required_window(window))


@register_operator("diff", "lagged", 1, uses_window=True)
def _diff(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left.diff(_required_window(window))


@register_operator("pct", "lagged", 1, uses_window=True)
def _pct(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left.pct_change(_required_window(window), fill_method=None)


@register_operator("acceleration", "lagged", 1, uses_window=True)
def _acceleration(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.diff(value).diff(value)


def _min_periods(window: int) -> int:
    return min(window, max(3, window // 2))


@register_operator("mean", "rolling", 1, uses_window=True)
def _mean(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).mean()


@register_operator("median", "rolling", 1, uses_window=True)
def _median(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).median()


@register_operator("std", "rolling", 1, uses_window=True)
def _std(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).std(ddof=0)


@register_operator("rolling_var", "rolling", 1, uses_window=True)
def _rolling_var(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).var(ddof=0)


@register_operator("rolling_sum", "rolling", 1, uses_window=True)
def _rolling_sum(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).sum()


@register_operator("linear_decay", "rolling", 1, uses_window=True)
def _linear_decay_average(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).apply(
        _linear_decay,
        raw=True,
    )


@register_operator("days_since_max", "rolling", 1, uses_window=True)
def _days_since_max(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).apply(
        lambda sample: _latest_extreme_age(sample, find_max=True),
        raw=True,
    )


@register_operator("days_since_min", "rolling", 1, uses_window=True)
def _days_since_min(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).apply(
        lambda sample: _latest_extreme_age(sample, find_max=False),
        raw=True,
    )


@register_operator("rolling_mad", "rolling", 1, uses_window=True)
def _rolling_mad(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).apply(
        lambda sample: np.nanmedian(
            np.abs(sample - np.nanmedian(sample))
        ),
        raw=True,
    )


@register_operator("rolling_iqr", "rolling", 1, uses_window=True)
def _rolling_iqr(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    rolling = left.rolling(value, min_periods=_min_periods(value))
    return rolling.quantile(0.75) - rolling.quantile(0.25)


@register_operator("tail_imbalance", "rolling", 1, uses_window=True)
def _tail_imbalance(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    """Quantile tail asymmetry: (Q90 + Q10 - 2 * Q50) / (Q90 - Q10)."""
    value = max(3, _required_window(window))
    rolling = left.rolling(value, min_periods=_min_periods(value))
    lower, middle, upper = rolling.quantile(0.1), rolling.quantile(0.5), rolling.quantile(0.9)
    width = upper - lower
    return ((upper + lower - 2.0 * middle) / width.where(width > 1e-12)).mask(width <= 1e-12, 0.0)


@register_operator("max_drawdown", "rolling", 1, uses_window=True)
def _max_drawdown(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    """Largest preceding-peak to later-trough fall, in input units, within a window."""
    value = max(3, _required_window(window))

    def drawdown(sample: np.ndarray) -> float:
        valid = sample[np.isfinite(sample)]
        return float(np.max(np.maximum.accumulate(valid) - valid)) if len(valid) else np.nan

    return left.rolling(value, min_periods=_min_periods(value)).apply(drawdown, raw=True)


@register_operator("rolling_skew", "rolling", 1, uses_window=True)
def _rolling_skew(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    return left.rolling(
        value,
        min_periods=max(3, _min_periods(value)),
    ).apply(_skewness, raw=True)


@register_operator("rolling_kurt", "rolling", 1, uses_window=True)
def _rolling_kurt(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(4, _required_window(window))
    return left.rolling(
        value,
        min_periods=max(4, _min_periods(value)),
    ).apply(_excess_kurtosis, raw=True)


@register_operator("volatility_change", "rolling", 1, uses_window=True)
def _volatility_change(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    volatility = left.rolling(value, min_periods=_min_periods(value)).std(ddof=0)
    return volatility.diff(value)


@register_operator("change_volatility", "rolling", 1, uses_window=True)
def _change_volatility(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Realized volatility of one-step changes rather than levels."""
    value = max(2, _required_window(window))
    return left.diff().rolling(
        value,
        min_periods=_min_periods(value),
    ).std(ddof=0)


@register_operator("change_asymmetry", "rolling", 1, uses_window=True)
def _change_asymmetry(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Compare the RMS magnitude of positive and negative one-step changes."""
    value = max(3, _required_window(window))
    min_periods = _min_periods(value)
    changes = left.diff()
    positive_rms = changes.clip(lower=0.0).pow(2).rolling(
        value,
        min_periods=min_periods,
    ).mean().pow(0.5)
    negative_rms = changes.clip(upper=0.0).abs().pow(2).rolling(
        value,
        min_periods=min_periods,
    ).mean().pow(0.5)
    scale = positive_rms + negative_rms
    return (positive_rms - negative_rms) / scale.where(scale > 1e-12)


@register_operator("shock_intensity", "rolling", 1, uses_window=True)
def _shock_intensity(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Share of recent change energy concentrated in the largest shock."""
    value = max(2, _required_window(window))
    min_periods = _min_periods(value)
    absolute_changes = left.diff().abs()
    largest = absolute_changes.rolling(value, min_periods=min_periods).max()
    energy = absolute_changes.pow(2).rolling(
        value,
        min_periods=min_periods,
    ).sum().pow(0.5)
    return largest / energy.where(energy > 1e-12)


@register_operator("zscore", "rolling", 1, uses_window=True)
def _zscore(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    rolling = left.rolling(value, min_periods=_min_periods(value))
    mean = rolling.mean()
    std = rolling.std(ddof=0)
    return (left - mean) / std.where(std > 1e-12)


@register_operator("rank", "rolling", 1, uses_window=True)
def _rank(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).rank(method="average", pct=True)


@register_operator("history_percentile", "rolling", 1, uses_window=True)
def _history_percentile(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    """Rank the current observation against finite, strictly preceding values."""
    value = _required_window(window)
    if value < 2:
        raise ValueError("history_percentile requires a window of at least 2")
    samples = left.to_numpy(dtype=float)
    result = np.full(len(samples), np.nan)
    for index, current in enumerate(samples):
        if not np.isfinite(current):
            continue
        history = samples[max(0, index - value):index]
        history = history[np.isfinite(history)]
        if len(history) >= _min_periods(value):
            result[index] = (np.sum(history < current) + 0.5 * np.sum(history == current)) / len(history)
    return pd.Series(result, index=left.index, name=left.name)


@register_operator("ema", "rolling", 1, uses_window=True)
def _ema(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.ewm(span=value, min_periods=_min_periods(value), adjust=False).mean()


@register_operator("ewm_std", "rolling", 1, uses_window=True)
def _ewm_std(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    return left.ewm(
        span=value,
        min_periods=_min_periods(value),
        adjust=False,
    ).std(bias=False)


@register_operator("ema_deviation", "rolling", 1, uses_window=True)
def _ema_deviation(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    average = left.ewm(
        span=value,
        min_periods=_min_periods(value),
        adjust=False,
    ).mean()
    return left - average


@register_operator("slope", "rolling", 1, uses_window=True)
def _slope(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).apply(
        _linear_slope,
        raw=True,
    )


@register_operator("trend_strength", "rolling", 1, uses_window=True)
def _trend_strength(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    min_periods = _min_periods(value)
    slope = left.rolling(value, min_periods=min_periods).apply(
        _linear_slope,
        raw=True,
    )
    noise = left.diff().rolling(value, min_periods=min_periods).std(ddof=0)
    strength = slope / noise.where(noise > 1e-12)
    return strength.mask((noise <= 1e-12) & (slope.abs() <= 1e-12), 0.0)


@register_operator("efficiency_ratio", "rolling", 1, uses_window=True)
def _efficiency_ratio(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Kaufman path efficiency: net displacement divided by path length."""
    value = max(2, _required_window(window))
    displacement = left.diff(value).abs()
    path_length = left.diff().abs().rolling(value, min_periods=value).sum()
    return displacement / path_length.where(path_length > 1e-12)


@register_operator("signed_run_length", "rolling", 1, uses_window=True)
def _signed_run_length(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Signed length of the current same-direction change run, capped by window."""
    value = _required_window(window)
    direction = pd.Series(np.sign(left.diff()), index=left.index, dtype=float)
    group = (direction.ne(direction.shift()) | direction.isna()).cumsum()
    run = direction.groupby(group).cumcount().add(1).clip(upper=value)
    result = direction * run / float(value)
    result = result.mask(direction == 0.0, 0.0)
    return result.where(direction.notna())


@register_operator("turning_point_rate", "rolling", 1, uses_window=True)
def _turning_point_rate(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Fraction of valid consecutive changes whose directions reverse."""
    value = max(3, _required_window(window))
    direction = pd.Series(np.sign(left.diff()), index=left.index, dtype=float)
    previous = direction.shift(1)
    valid = direction.notna() & previous.notna()
    turns = (direction * previous < 0.0).astype(float).where(valid)
    return turns.rolling(value, min_periods=_min_periods(value)).mean()


@register_operator("sign_entropy", "rolling", 1, uses_window=True)
def _sign_entropy(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Balance of recent nonzero up/down changes, independent of their order."""
    value = max(3, _required_window(window))
    return left.diff().rolling(value, min_periods=_min_periods(value)).apply(
        _binary_sign_entropy, raw=True
    )


@register_operator("shock_reversal_ratio", "rolling", 1, uses_window=True)
def _shock_reversal_ratio(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Current change against the largest shock in the preceding window."""
    value = max(3, _required_window(window))
    change = left.diff()
    past_shock = change.shift(1).rolling(
        value, min_periods=_min_periods(value)
    ).apply(_largest_signed_shock, raw=True)
    return -change / past_shock.where(past_shock.abs() > 1e-12)


@register_operator("mean_reversion", "rolling", 1, uses_window=True)
def _mean_reversion(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    rolling_mean = left.rolling(value, min_periods=_min_periods(value)).mean()
    return left - rolling_mean


@register_operator("range_position", "rolling", 1, uses_window=True)
def _range_position(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    rolling = left.rolling(value, min_periods=_min_periods(value))
    low = rolling.min()
    width = rolling.max() - low
    return (left - low) / width.where(width > 1e-12) - 0.5


@register_operator("robust_zscore", "rolling", 1, uses_window=True)
def _robust_zscore(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = _required_window(window)
    rolling = left.rolling(value, min_periods=_min_periods(value))
    median = rolling.median()
    interquartile_range = rolling.quantile(0.75) - rolling.quantile(0.25)
    robust_scale = 0.7413 * interquartile_range
    return (left - median) / robust_scale.where(robust_scale > 1e-12)


@register_operator("volatility_ratio", "rolling", 1, uses_window=True)
def _volatility_ratio(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    changes = left.diff()
    short_volatility = changes.rolling(
        value,
        min_periods=_min_periods(value),
    ).std(ddof=0)
    long_window = 2 * value
    long_volatility = changes.rolling(
        long_window,
        min_periods=_min_periods(long_window),
    ).std(ddof=0)
    return short_volatility / long_volatility.where(long_volatility > 1e-12)


@register_operator("autocorr", "rolling", 1, uses_window=True)
def _autocorr(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    return left.rolling(value, min_periods=_min_periods(value)).corr(left.shift(1))


@register_operator("variance_ratio", "rolling", 1, uses_window=True)
def _variance_ratio(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Multi-step variance relative to scaled one-step variance."""
    horizon = max(2, _required_window(window))
    estimation_window = 2 * horizon
    min_periods = horizon
    one_step_variance = left.diff().rolling(
        estimation_window,
        min_periods=min_periods,
    ).var(ddof=0)
    horizon_variance = left.diff(horizon).rolling(
        estimation_window,
        min_periods=min_periods,
    ).var(ddof=0)
    denominator = horizon * one_step_variance
    return horizon_variance / denominator.where(denominator > 1e-12)


@register_operator("rolling_min", "rolling", 1, uses_window=True)
def _rolling_min(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).min()


@register_operator("rolling_max", "rolling", 1, uses_window=True)
def _rolling_max(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    value = _required_window(window)
    return left.rolling(value, min_periods=_min_periods(value)).max()


@register_operator("add", "binary", 2)
def _add(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left + right


@register_operator("sub", "binary", 2)
def _sub(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left - right


@register_operator("mul", "binary", 2)
def _mul(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left * right


@register_operator("div", "binary", 2)
def _div(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return left / right.where(right.abs() > 1e-12)


@register_operator("relative_spread", "binary", 2)
def _relative_spread(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    scale = left.abs() + right.abs()
    return (left - right) / scale.where(scale > 1e-12)


@register_operator("minimum", "binary", 2)
def _minimum(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return pd.concat([left, right], axis=1).min(axis=1)


@register_operator("maximum", "binary", 2)
def _maximum(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return pd.concat([left, right], axis=1).max(axis=1)


@register_operator("normalized_spread", "binary", 2, uses_window=True)
def _normalized_spread(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    spread = left - right
    scale = spread.rolling(value, min_periods=_min_periods(value)).std(ddof=0)
    return spread / scale.where(scale > 1e-12)


def _finite_condition(values: pd.Series) -> pd.Series:
    return (values > 0.0).astype(float).where(np.isfinite(values))


@register_operator("gt", "binary", 2, family="logic")
def _gt(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return (left > right).astype(float).where(np.isfinite(left) & np.isfinite(right))


@register_operator("ge", "binary", 2, family="logic")
def _ge(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return (left >= right).astype(float).where(np.isfinite(left) & np.isfinite(right))


@register_operator("lt", "binary", 2, family="logic")
def _lt(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return (left < right).astype(float).where(np.isfinite(left) & np.isfinite(right))


@register_operator("le", "binary", 2, family="logic")
def _le(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return (left <= right).astype(float).where(np.isfinite(left) & np.isfinite(right))


@register_operator("logical_and", "binary", 2, family="logic")
def _logical_and(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    left_state, right_state = _finite_condition(left), _finite_condition(right)
    result = pd.Series(np.nan, index=left.index, name=left.name)
    return result.mask((left_state == 0.0) | (right_state == 0.0), 0.0).mask(
        (left_state == 1.0) & (right_state == 1.0), 1.0
    )


@register_operator("logical_or", "binary", 2, family="logic")
def _logical_or(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    left_state, right_state = _finite_condition(left), _finite_condition(right)
    result = pd.Series(np.nan, index=left.index, name=left.name)
    return result.mask((left_state == 1.0) | (right_state == 1.0), 1.0).mask(
        (left_state == 0.0) & (right_state == 0.0), 0.0
    )


@register_operator("logical_not", "unary", 1, family="logic")
def _logical_not(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    return 1.0 - _finite_condition(left)


@register_operator("if_else", "ternary", 3, family="logic")
def _if_else(
    condition: pd.Series,
    when_true: pd.Series,
    window: int | None,
    when_false: pd.Series,
) -> pd.Series:
    state = _finite_condition(condition)
    selected = when_true.where(state == 1.0, when_false)
    return selected.where(state.notna() & np.isfinite(selected))


@register_operator(
    "signed_compare",
    "binary",
    2,
    family="logic",
    root_only=True,
)
def _signed_compare(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    result = np.sign(left - right).astype(float)
    return result.where(left.notna() & right.notna())


@register_operator(
    "cross_signal",
    "binary",
    2,
    family="logic",
    root_only=True,
)
def _cross_signal(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    previous_left = left.shift(1)
    previous_right = right.shift(1)
    valid = (
        left.notna()
        & right.notna()
        & previous_left.notna()
        & previous_right.notna()
    )
    crossed_above = (left > right) & (previous_left <= previous_right)
    crossed_below = (left < right) & (previous_left >= previous_right)
    result = pd.Series(0.0, index=left.index, name=left.name)
    result = result.mask(crossed_above, 1.0).mask(crossed_below, -1.0)
    return result.where(valid)


@register_operator(
    "regime_gate",
    "binary",
    2,
    family="logic",
    root_only=True,
)
def _regime_gate(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    result = left.where(right > 0.0, 0.0)
    return result.where(left.notna() & right.notna())


@register_operator(
    "rolling_corr",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_corr(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    valid = left.notna() & right.notna()
    left_pair = left.where(valid)
    right_pair = right.where(valid)
    rolling = left_pair.rolling(value, min_periods=_min_periods(value))
    correlation = rolling.corr(right_pair)
    enough = valid.rolling(value, min_periods=_min_periods(value)).sum() >= _min_periods(value)
    flat = (rolling.std(ddof=0) <= 1e-12) | (
        right_pair.rolling(value, min_periods=_min_periods(value)).std(ddof=0) <= 1e-12
    )
    return correlation.mask(enough & flat, 0.0)


@register_operator(
    "rolling_change_corr",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_change_corr(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Rolling Pearson correlation of one-step changes, not levels."""
    value = max(3, _required_window(window))
    return left.diff().rolling(
        value,
        min_periods=_min_periods(value),
    ).corr(right.diff())


@register_operator("rolling_spearman", "binary", 2, uses_window=True, family="regression")
def _rolling_spearman(left: pd.Series, right: pd.Series | None, window: int | None) -> pd.Series:
    """Rank paired finite observations separately inside each shared trailing window."""
    value = max(3, _required_window(window))
    paired = pd.concat([left, right], axis=1).to_numpy(dtype=float)
    result = np.full(len(paired), np.nan)
    for index in range(len(paired)):
        sample = paired[max(0, index - value + 1):index + 1]
        sample = sample[np.isfinite(sample).all(axis=1)]
        if len(sample) < _min_periods(value):
            continue
        ranks = []
        for column in sample.T:
            _, inverse, counts = np.unique(column, return_inverse=True, return_counts=True)
            midranks = counts.cumsum() - 0.5 * (counts - 1)
            ranked = midranks[inverse]
            ranks.append(ranked - ranked.mean())
        denominator = np.linalg.norm(ranks[0]) * np.linalg.norm(ranks[1])
        result[index] = float(np.dot(*ranks) / denominator) if denominator > 1e-12 else 0.0
    return pd.Series(result, index=left.index, name=left.name)


@register_operator(
    "rolling_lagged_change_corr",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_lagged_change_corr(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Correlation of left changes with one-day-lagged right changes."""
    value = max(3, _required_window(window))
    return left.diff().rolling(
        value,
        min_periods=_min_periods(value),
    ).corr(right.diff().shift(1))


@register_operator(
    "rolling_directional_agreement",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_directional_agreement(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Average signed agreement of paired one-step change directions."""
    value = max(3, _required_window(window))
    left_change = left.diff()
    right_change = right.diff()
    agreement = pd.Series(
        np.sign(left_change) * np.sign(right_change),
        index=left.index,
        dtype=float,
    ).where(left_change.notna() & right_change.notna())
    return agreement.rolling(value, min_periods=_min_periods(value)).mean()


@register_operator(
    "rolling_cov",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_cov(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(3, _required_window(window))
    return left.rolling(value, min_periods=_min_periods(value)).cov(right)


@register_operator(
    "rolling_change_beta",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_change_beta(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Rolling sensitivity of left changes to right changes."""
    value = max(3, _required_window(window))
    min_periods = _min_periods(value)
    left_change = left.diff()
    right_change = right.diff()
    covariance = left_change.rolling(value, min_periods=min_periods).cov(right_change)
    variance = right_change.rolling(value, min_periods=min_periods).var(ddof=1)
    return covariance / variance.where(variance > 1e-12)


@register_operator(
    "rolling_tracking_error",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_tracking_error(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    """Volatility of the difference between paired one-step changes."""
    value = max(2, _required_window(window))
    active_change = left.diff() - right.diff()
    return active_change.rolling(
        value,
        min_periods=_min_periods(value),
    ).std(ddof=0)


@register_operator(
    "rolling_residual",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_residual(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    value = max(5, _required_window(window))
    min_periods = max(5, value // 2)
    covariance = left.rolling(value, min_periods=min_periods).cov(right)
    variance = right.rolling(value, min_periods=min_periods).var(ddof=0)
    beta = covariance / variance.where(variance > 1e-12)
    return left - beta * right


def _rolling_ols_terms(
    left: pd.Series,
    right: pd.Series,
    window: int | None,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    value = max(5, _required_window(window))
    min_periods = max(5, value // 2)
    rolling_left = left.rolling(value, min_periods=min_periods)
    rolling_right = right.rolling(value, min_periods=min_periods)
    covariance = rolling_left.cov(right)
    variance = rolling_right.var(ddof=1)
    beta = covariance / variance.where(variance > 1e-12)
    alpha = rolling_left.mean() - beta * rolling_right.mean()
    correlation = rolling_left.corr(right)
    return alpha, beta, correlation


@register_operator(
    "rolling_beta",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_beta(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    _, beta, _ = _rolling_ols_terms(left, right, window)
    return beta


@register_operator(
    "rolling_alpha",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_alpha(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    alpha, _, _ = _rolling_ols_terms(left, right, window)
    return alpha


@register_operator(
    "rolling_r2",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_r2(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    _, _, correlation = _rolling_ols_terms(left, right, window)
    return correlation.pow(2)


@register_operator(
    "rolling_ols_residual",
    "binary",
    2,
    uses_window=True,
    family="regression",
)
def _rolling_ols_residual(
    left: pd.Series,
    right: pd.Series | None,
    window: int | None,
) -> pd.Series:
    alpha, beta, _ = _rolling_ols_terms(left, right, window)
    return left - alpha - beta * right


def apply_operator(
    name: str,
    left: pd.Series,
    right: pd.Series | None = None,
    window: int | None = None,
    *,
    third: pd.Series | None = None,
) -> pd.Series:
    try:
        spec = OPERATOR_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown operator: {name}") from exc
    if spec.arity >= 2 and right is None:
        raise ValueError(f"Binary operator requires a right operand: {name}")
    if spec.arity == 3:
        if third is None:
            raise ValueError(f"Ternary operator requires a third operand: {name}")
        return spec.function(left, right, window, third).replace([np.inf, -np.inf], np.nan)
    if third is not None:
        raise ValueError(f"Operator does not accept a third operand: {name}")
    return spec.function(left, right, window).replace([np.inf, -np.inf], np.nan)


# The following uses only DataBridge daily columns and embedded specifications.
# Operators above retain the original framework's numerical definitions.
FACTOR_RAW_FIELDS = (
    "TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C",
    "TB5YWI0V", "90000007", "DR007IB0", "DR007IBC", "DR007IBH", "DR007IBL",
)


def prepare_factor_features(daily):
    """Prepare causal V8 daily inputs from DataBridge. Caller supplies cutoff."""
    missing = sorted(set(FACTOR_RAW_FIELDS) - set(daily.columns))
    if missing:
        raise ValueError(f"Missing factor input columns: {missing}")
    frame = daily.loc[:, FACTOR_RAW_FIELDS].apply(pd.to_numeric, errors="raise").astype(float)
    if not isinstance(frame.index, pd.DatetimeIndex) or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("Daily feature index must have unique, increasing dates")
    if np.isinf(frame.to_numpy()).any():
        raise ValueError("Infinite input value")
    # Original configs had target columns 3Y/5Y/10Y only: CURVE__level is their mean.
    levels = frame[["TB3YWI0C", "TB5YWI0C", "TB0YWI0C"]]
    frame["CURVE__level"] = levels.mean(axis=1)
    frame["CURVE__slope_10y_3y"] = frame["TB0YWI0C"] - frame["TB3YWI0C"]
    for tenor, field in (("3y", "TB3YWI0C"), ("5y", "TB5YWI0C"), ("10y", "TB0YWI0C")):
        for lag in (1, 5):
            frame[f"CURVE__{tenor}_d{lag}"] = frame[field].diff(lag)
    width = frame["DR007IBH"] - frame["DR007IBL"]
    frame["CURVE__funding_dr007_range"] = width
    frame["CURVE__funding_dr007_close_location"] = (
        (frame["DR007IBC"] - frame["DR007IBL"]) / width.where(width.abs() > 1e-12) - 0.5
    )
    frame["CURVE__funding_dr007_open_gap"] = frame["DR007IB0"] - frame["DR007IBC"].shift(1)
    return frame


def _expression_key(spec):
    if spec is None:
        return None
    return (spec["op"], spec.get("field"), spec.get("window"), spec.get("value"),
            _expression_key(spec.get("left")), _expression_key(spec.get("right")),
            _expression_key(spec.get("third")))


def _evaluate_factor_expression(spec, features, cache):
    key = _expression_key(spec)
    if key in cache:
        return cache[key]
    op = spec["op"]
    if op == "raw":
        value = features[spec["field"]].astype(float)
    elif op == "constant":
        value = pd.Series(float(spec["value"]), index=features.index, dtype=float)
    else:
        left = _evaluate_factor_expression(spec["left"], features, cache)
        right = _evaluate_factor_expression(spec["right"], features, cache) if spec.get("right") is not None else None
        third = _evaluate_factor_expression(spec["third"], features, cache) if spec.get("third") is not None else None
        value = apply_operator(op, left, right, spec.get("window"), third=third)
    cache[key] = value
    return value


def _evaluate_factor_genome(spec, features, cache):
    def transformed(field, op, window):
        key = ("gp_transform", field, op, int(window))
        if key not in cache:
            cache[key] = apply_operator(op, features[field], window=max(1, int(window)))
        return cache[key]
    left = transformed(spec["y"], spec["y_transform"], spec["y_window"])
    if spec["mode"] == "identity":
        return left
    right = transformed(spec["x"], spec["x_transform"], spec["x_window"])
    if spec["mode"] == "if_else":
        third = features[spec["z"]] if spec.get("z") else pd.Series(0., index=features.index)
        return apply_operator(spec["mode"], left, right, third=third)
    return apply_operator(spec["mode"], left, right, spec.get("mode_window") or spec["y_window"])


def calculate_factor_values(features, sources, factor_ids):
    """Calculate each original factor on the daily clock, before quantiles.

    This can be shared across requests because every operator uses only the
    current and preceding daily observations. Request-specific clock selection
    and label availability must be handled separately by the caller.
    """
    if len(sources) != len(factor_ids) or len(set(factor_ids)) != len(factor_ids):
        raise ValueError("Factor specifications and unique IDs must match")
    if not isinstance(features.index, pd.DatetimeIndex) or not features.index.is_unique or not features.index.is_monotonic_increasing:
        raise ValueError("Feature dates must be unique and increasing")
    cache = {}
    columns = {}
    for identity, source in zip(factor_ids, sources):
        spec = source["specification"]
        raw = (_evaluate_factor_genome(spec, features, cache) if "y_transform" in spec
               else _evaluate_factor_expression(spec, features, cache))
        raw = raw.astype(float).replace([np.inf, -np.inf], np.nan)
        if source.get("postprocess") == "neg":
            raw = -raw
        elif source.get("postprocess") not in ("identity", "raw"):
            raise ValueError("Unsupported factor postprocess")
        columns[identity] = raw
    return pd.DataFrame(columns, index=features.index)


def score_factor_values(values, sources, factor_ids, decision_dates=None):
    """Score raw factors on one increasing, caller-supplied causal clock.

    For a weekly request, the caller must supply the valid historical sample
    decisions known by that request's cutoff plus its current decision date.
    Backward reindexing cannot see later daily observations, even when the raw
    matrix is shared with requests having later cutoffs.
    """
    if len(sources) != len(factor_ids) or len(set(factor_ids)) != len(factor_ids):
        raise ValueError("Factor specifications and unique IDs must match")
    if list(values.columns) != list(factor_ids):
        raise ValueError("Factor-value columns differ from the requested IDs")
    if not isinstance(values.index, pd.DatetimeIndex) or not values.index.is_unique or not values.index.is_monotonic_increasing:
        raise ValueError("Factor-value dates must be unique and increasing")
    dates = values.index if decision_dates is None else pd.DatetimeIndex(decision_dates)
    if dates.hasnans or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError("Decision dates must be unique and increasing")
    if len(dates) and (not len(values) or dates[-1] > values.index[-1]):
        raise ValueError("Decision date exceeds available factor values")
    sampled = values if decision_dates is None else values.reindex(dates, method="ffill")
    columns = {}
    for identity, source in zip(factor_ids, sources):
        raw = sampled[identity]
        policy = (source.get("signal_policy") or source.get("signal_specs")
                  or (source.get("signal_rule") or {}).get("threshold_policy") or {})
        if policy.get("method") != "self_quantile":
            raise ValueError("Unsupported factor comparison policy")
        lagged = raw.shift(1).rolling(int(policy["history_window"]), min_periods=int(policy["min_history"]))
        lower = lagged.quantile(float(policy["down_quantile"]))
        upper = lagged.quantile(float(policy["up_quantile"]))
        orientation = (source.get("signal_rule") or {}).get("orientation", policy.get("orientation", 1))
        columns[identity] = ((raw - (lower + upper) / 2) * orientation).where(raw.notna() & lower.notna() & upper.notna())
    return pd.DataFrame(columns, index=dates)


def calculate_factor_scores(features, sources, factor_ids, decision_dates=None):
    """Compatibility helper: compute raw values and score one decision clock."""
    return score_factor_values(calculate_factor_values(features, sources, factor_ids),
                               sources, factor_ids, decision_dates)

"""Labels used by the frozen v8 T+5 and weekly-close experiments.

Inputs are in-memory DataBridge tables already truncated through ``cutoff``.
All target values use the original bond-price proxy: earlier yield minus
later yield.  The Blackbox output uses yield direction, the opposite sign.
"""

import numpy as np
import pandas as pd


TARGET_COLUMNS = {"3Y": "TB3YWI0C", "5Y": "TB5YWI0C", "10Y": "TB0YWI0C"}
QUOTE_COLUMNS = ("TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
LABEL_COLUMNS = ("signal_date", "signal_close_date", "target_date", "label_available_at",
                 "target", "labels_actual")


def _dated_index(frame, column, cutoff):
    frame = frame.copy()
    if column in frame:
        dates = pd.to_datetime(frame.pop(column), format="%Y-%m-%d", errors="raise")
    elif isinstance(frame.index, pd.DatetimeIndex):
        dates = frame.index
    else:
        raise ValueError(f"missing {column} date column")
    dates = pd.DatetimeIndex(dates)
    if (dates.hasnans or dates.has_duplicates or not dates.is_monotonic_increasing
            or not (dates == dates.normalize()).all()):
        raise ValueError(f"{column} dates must be unique, increasing, and normalized")
    if len(dates) and dates[-1] > cutoff:
        raise ValueError(f"{column} contains rows after the cutoff")
    frame.index = dates
    return frame


def _daily_labels(daily, target_column):
    levels = daily[target_column]
    future_dates = pd.Series(daily.index, index=daily.index).shift(-5)
    result = pd.DataFrame({"signal_date": daily.index,
                           "signal_close_date": daily.index,
                           "target_date": future_dates,
                           "label_available_at": future_dates,
                           "target": levels - levels.shift(-5)}, index=daily.index)
    return result.dropna(subset=["target_date", "target"])


def _weekly_labels(daily, calendar, target_column):
    missing = daily.index.difference(calendar.index)
    if len(missing):
        raise ValueError("calendar does not cover every consumed daily date")
    week_ids = calendar["week_id"].astype("string")
    if week_ids.isna().any() or not week_ids.str.fullmatch(r"[0-9]{6}").all():
        raise ValueError("week_id must contain six digits")
    runs = week_ids[week_ids.ne(week_ids.shift())]
    if runs.duplicated().any():
        raise ValueError("calendar contains noncontiguous week IDs")
    # The original source calendar used the presence of any of five CGB quotes
    # as its trading-day flag.  Do not inspect prices after the cutoff.
    trading = daily.loc[:, QUOTE_COLUMNS].notna().any(axis=1).reindex(calendar.index, fill_value=False)
    all_trading_days = calendar.index[trading]
    groups = list(calendar.assign(week_id=week_ids).groupby("week_id", sort=False))
    target_columns = list(TARGET_COLUMNS.values())
    rows = []
    for number, (_, bucket) in enumerate(groups):
        # Without a preceding and a following ID we cannot prove both bounds.
        if number == 0 or number == len(groups) - 1:
            continue
        start, end = bucket.index[0], bucket.index[-1]
        decision = start - pd.Timedelta(days=start.dayofweek + 3)
        if end > daily.index[-1] or len(pd.bdate_range(start, end).difference(bucket.index)):
            continue
        days = bucket.index[trading.reindex(bucket.index).to_numpy()]
        before = all_trading_days[(all_trading_days < start) & (all_trading_days <= decision)]
        if not len(days) or not len(before):
            continue
        close_date = before[-1]
        prices = daily.reindex(days)[target_columns]
        if prices.isna().any().any() or daily.loc[close_date, target_columns].isna().any():
            continue
        # Match the original quote-calendar conflict check: a daily row with no
        # CGB quotes between the inferred prior close and nominal Friday is not
        # a safe signal close.
        if close_date < decision:
            conflict = daily.index[(daily.index > close_date) & (daily.index <= decision)]
            if len(conflict.difference(all_trading_days)):
                continue
        rows.append({"signal_date": decision, "signal_close_date": close_date,
                     "target_date": days[-1], "label_available_at": end,
                     "target": daily.loc[close_date, target_column] - prices.iloc[-1][target_column]})
    result = pd.DataFrame(rows, columns=LABEL_COLUMNS[:-1])
    if len(result) and result["signal_date"].duplicated().any():
        raise ValueError("multiple weekly targets share a signal date")
    return result


def build_labels(daily, calendar, tenor, frequency, cutoff):
    """Return causally observable labels from already-truncated DataFrames.

    ``daily`` has a ``date`` column or a DatetimeIndex and the five CGB quote
    columns in ``QUOTE_COLUMNS``. ``calendar`` has ``rdate, week_id`` (or a
    DatetimeIndex and ``week_id``); it is required for weekly labels only.
    Rows after the normalized cutoff are rejected, rather than used silently.
    Frequency is ``daily``/``week``; tenor is ``3Y``/``5Y``/``10Y``.
    Output dates are pandas timestamps, the target is the bond-price proxy,
    and ``labels_actual`` is its exact sign.  Consumers must additionally
    require signal, target, and availability dates strictly before each fit.
    """
    tenor = str(tenor).upper()
    if tenor not in TARGET_COLUMNS or frequency not in {"daily", "week"}:
        raise ValueError("unsupported tenor or frequency")
    cutoff = pd.Timestamp(cutoff)
    if pd.isna(cutoff) or cutoff != cutoff.normalize() or cutoff.tz is not None:
        raise ValueError("cutoff must be a normalized date")
    daily = _dated_index(daily, "date", cutoff)
    if daily.empty or not set(QUOTE_COLUMNS) <= set(daily.columns):
        raise ValueError("daily input must contain data and all five CGB quote columns")
    for col in QUOTE_COLUMNS:
        daily[col] = pd.to_numeric(daily[col], errors="raise").astype(float)
        if np.isinf(daily[col].to_numpy()).any():
            raise ValueError("non-finite CGB quote")
    if frequency == "daily":
        result = _daily_labels(daily, TARGET_COLUMNS[tenor])
    else:
        if calendar is None:
            raise ValueError("weekly labels require the date-to-week calendar")
        calendar = _dated_index(calendar, "rdate", cutoff)
        if calendar.empty or "week_id" not in calendar:
            raise ValueError("calendar must contain week IDs")
        result = _weekly_labels(daily, calendar, TARGET_COLUMNS[tenor])
    result = result.copy()
    result["labels_actual"] = np.sign(result["target"]).astype(int)
    return result.loc[:, LABEL_COLUMNS].reset_index(drop=True)

import json

FACTOR_DATA = json.loads('{"factor_ids":["week/gp/5y/v8_20261018T060004_7e96ff67_week_gp_5y:346e234f21997e0d66d7d9582f28ea1ae078b974cca69fd1a15c1e6b214df27e","week/gp/5y/v8_20261018T120005_70aa8297_week_gp_5y:d795cb6fa8c133c5a5462e9f7bfddfd3353360b4593a5872cd3b8e18570b256d","week/gp/5y/v8_20261018T210005_1be415d1_week_gp_5y:fa4d4f700e3e59229ea8dbc82bc4c0098b7bed54d7d1d87f48859582c7a4b91a","week/gp/5y/v8_20261020T030004_36f0c3a1_week_gp_5y:ee9b650755b68cb36a9712f0f6a2b9a77f7b59909f3630dd572769f8cf0c91c0","week/gp/5y/v8_20261022T060005_79079adc_week_gp_5y:34c47978e021605d4c7af4565f533b3589efd3b448b705c877a6596b4974f0cb","week/gp/5y/v8_20261023T030005_3ad0d11e_week_gp_5y:4c561dab3e30f5774c7f808f4d04e071f695f1fc774c51c18b08b81f9535f288","week/gp/5y/v8_20261023T120005_1fcae09a_week_gp_5y:d69181ee64c323d3cfab472e5836b45987bbe73c9330ab233e01843cbcce9895","week/gp/5y/v8_20261023T150005_38585692_week_gp_5y:2d078c5096d1ee5dcab57c1e00bd4d5d89445be16b958f832b58c91c816a0994","week/gp/5y/v8_20261024T120005_2119e369_week_gp_5y:76adbf7b5f85d148d3dfad9239c738b8f289210343274d753ff4797d1e876bf6","week/gp/5y/v8_20261025T000005_330e97a1_week_gp_5y:69e7fec896f22a03d0d10dcfca398800d4f04996e394b976a88a260105c33bf7","week/gp/5y/v8_20261027T210004_dcee72ad_week_gp_5y:8f882344abfbc23a6f838d83e7fb798e7e1552ec5aab01acb1e8a2f30aee37d3","week/gp/5y/v8_20261028T030005_b5193207_week_gp_5y:7445771850bee10c6dcd2354c3b35f5183cd1e5d953d03e72da3a50b5edca42f","week/sr/5y/v8_20261018T001219_acfe4a2f_week_sr_5y:3e0bf557f54d76a152a999c888f528274c121fb7b599aa28f5d61f8ef121736f","week/sr/5y/v8_20261018T001219_acfe4a2f_week_sr_5y:4267ff33a2885742e367bd5e7caa7068787017b2504fc4b19daf612f2caf6c66","week/sr/5y/v8_20261018T150005_045de777_week_sr_5y:a72c59e8f0ac071871cc7d3731b1b7902818f87df1f408ed90bbdf6b4a8eee0c","week/sr/5y/v8_20261019T000000_108bf772_week_sr_5y:fd53ec465c2dace4c6cb047eac9dea8fcd1c6617ed3cfdbe289d4b3efb972ea5","week/sr/5y/v8_20261020T210005_4fbde9ce_week_sr_5y:5955675984600782b844cbfb4830a346688bb908ffe5e7c3db47d24a440db29e","week/sr/5y/v8_20261020T210005_4fbde9ce_week_sr_5y:d186bdd8b08513b442456476aa224bb81cb4ac98f126394563017640fb4a51f7","week/sr/5y/v8_20261021T060005_61e9deca_week_sr_5y:bb7f1c38c8290ca057913cfd6d6b9b84e670f58d11fd4efbc09c2695e39be770"],"sources":[{"method":"GP","specification":{"down_quantile":0.0,"mode":"add","mode_window":1,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"CURVE__level","x_transform":"std","x_window":13,"y":"CURVE__5y_d1","y_transform":"identity","y_window":1,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"rolling_change_corr","mode_window":8,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"90000007","x_transform":"acceleration","x_window":4,"y":"CURVE__3y_d5","y_transform":"change_volatility","y_window":13,"z":null},"postprocess":"identity","direction":1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"identity","mode_window":null,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":null,"x_transform":"identity","x_window":1,"y":"CURVE__10y_d1","y_transform":"mean_reversion","y_window":5,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"add","mode_window":1,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"90000007","x_transform":"history_percentile","x_window":4,"y":"CURVE__5y_d1","y_transform":"history_percentile","y_window":13,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"rolling_tracking_error","mode_window":4,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"DR007IBH","x_transform":"acceleration","x_window":4,"y":"CURVE__funding_dr007_range","y_transform":"variance_ratio","y_window":13,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"identity","mode_window":1,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":null,"x_transform":"identity","x_window":1,"y":"TB3YWI0C","y_transform":"robust_zscore","y_window":3,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"rolling_alpha","mode_window":8,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"CURVE__3y_d5","x_transform":"max_drawdown","x_window":3,"y":"90000007","y_transform":"std","y_window":3,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"normalized_spread","mode_window":8,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"CURVE__5y_d1","x_transform":"history_percentile","x_window":8,"y":"DR007IBC","y_transform":"range_position","y_window":13,"z":null},"postprocess":"identity","direction":1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"normalized_spread","mode_window":3,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"CURVE__5y_d5","x_transform":"rank","x_window":8,"y":"TB1YWI0C","y_transform":"tail_imbalance","y_window":13,"z":null},"postprocess":"identity","direction":1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"rolling_residual","mode_window":8,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":"CURVE__10y_d5","x_transform":"identity","x_window":1,"y":"CURVE__10y_d1","y_transform":"mean_reversion","y_window":13,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"identity","mode_window":1,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":null,"x_transform":"identity","x_window":1,"y":"CURVE__10y_d1","y_transform":"pct","y_window":26,"z":null},"postprocess":"identity","direction":1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"GP","specification":{"down_quantile":0.0,"mode":"identity","mode_window":1,"threshold_mode":"fixed_zero","up_quantile":0.0,"x":null,"x_transform":"identity","x_window":1,"y":"CURVE__5y_d1","y_transform":"diff","y_window":2,"z":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":"DR007IBL","left":null,"op":"raw","right":null,"window":null},"op":"zscore","right":null,"window":5},"op":"zscore","right":null,"window":26},"op":"minimum","right":{"field":null,"left":{"field":null,"left":{"field":"CURVE__5y_d1","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"mean","right":null,"window":26},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":"CURVE__5y_d1","left":null,"op":"raw","right":null,"window":null},"op":"mean_reversion","right":null,"window":13},"op":"add","right":{"field":null,"left":{"field":null,"left":{"field":"CURVE__5y_d1","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"mean","right":null,"window":26},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":"CURVE__5y_d1","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"mean","right":null,"window":26},"op":"sub","right":{"field":null,"left":{"field":null,"left":{"field":"TB0YWI0C","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"tail_imbalance","right":null,"window":26},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":"DR007IBL","left":null,"op":"raw","right":null,"window":null},"op":"zscore","right":null,"window":5},"op":"zscore","right":null,"window":26},"op":"minimum","right":{"field":null,"left":{"field":null,"left":{"field":"TB1YWI0C","left":null,"op":"raw","right":null,"window":null},"op":"std","right":null,"window":5},"op":"std","right":null,"window":8},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"op":"history_percentile","right":null,"window":20},"op":"sub","right":{"field":null,"left":{"field":"DR007IBC","left":null,"op":"raw","right":null,"window":null},"op":"history_percentile","right":null,"window":20},"window":null},"op":"mean_reversion","right":null,"window":13},"op":"maximum","right":{"field":null,"left":{"field":null,"left":{"field":"CURVE__10y_d5","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"ema","right":null,"window":4},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":null,"left":{"field":"TB5YWI0C","left":null,"op":"raw","right":null,"window":null},"op":"acceleration","right":null,"window":8},"op":"mean_reversion","right":null,"window":8},"op":"maximum","right":{"field":null,"left":{"field":null,"left":{"field":"CURVE__10y_d5","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d1","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"ema","right":null,"window":4},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":null,"tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}},{"method":"SR","specification":{"field":null,"left":{"field":null,"left":{"field":"CURVE__5y_d1","left":null,"op":"raw","right":null,"window":null},"op":"zscore","right":null,"window":5},"op":"add","right":{"field":null,"left":{"field":null,"left":{"field":"TB1YWI0C","left":null,"op":"raw","right":null,"window":null},"op":"rolling_lagged_change_corr","right":{"field":"CURVE__10y_d5","left":null,"op":"raw","right":null,"window":null},"window":1},"op":"history_percentile","right":null,"window":8},"window":null},"postprocess":"identity","direction":-1,"signal_rule":null,"signal_policy":null,"signal_specs":{"comparison_orientation":1,"direction_fit_period":"train","down_quantile":0.3,"history_clock":"weekly_decision","history_window":52,"method":"self_quantile","min_history":13,"orientation":-1,"reference_expression":null,"reference_unit":null,"signal_unit":"dimensionless","tie_policy":"neutral","up_quantile":0.7,"warmup_policy":"unavailable"}}]}')
FACTOR_IDS = FACTOR_DATA['factor_ids']
FACTOR_SOURCES = FACTOR_DATA['sources']

TENOR = '5Y'
FREQUENCY = 'week'
XGB_PARAMETERS = {'max_depth': 1, 'n_estimators': 100}
MINIMUM_FEATURE_DATE = '2026-10-30'
MINIMUM_PREDICT_DATE = '2026-11-01'
MINIMUM_BACKTEST_DATE = '2025-01-01'

"""The causal expanding-window model, embedded in each Blackbox scheme."""

from scipy import stats
from xgboost import XGBClassifier


def _consumed_columns(data_dir):
    header = _header(Path(data_dir) / "factor_catalog.csv")
    if header != ["indicators_code", "frequency", "factor_version"]:
        raise ValueError("invalid factor catalog header")
    catalog = pd.read_csv(Path(data_dir) / "factor_catalog.csv", dtype=str,
                          keep_default_na=False, na_filter=False)
    if catalog.empty or (catalog == "").any().any() or catalog.indicators_code.duplicated().any():
        raise ValueError("invalid factor catalog rows")
    available = set(catalog.loc[catalog.frequency == "daily", "indicators_code"])
    missing = set(FACTOR_RAW_FIELDS) - available
    if missing:
        raise ValueError(f"required daily factors absent from catalog: {sorted(missing)}")
    return {"daily": list(FACTOR_RAW_FIELDS)}


def _rank_fit(values, targets):
    n, p = values.shape
    sorted_values = np.full((n, p), np.inf)
    counts = np.zeros(p, dtype=int)
    orientation = np.ones(p, dtype=int)
    active = np.zeros(p, dtype=bool)
    for j in range(p):
        finite = np.isfinite(values[:, j]) & np.isfinite(targets)
        observed, y = values[finite, j], targets[finite]
        counts[j] = len(observed)
        if len(observed) >= 30 and np.std(observed) > 0:
            correlation = stats.spearmanr(observed, y).statistic
            orientation[j] = -1 if correlation < 0 else 1
            active[j] = True
        sorted_values[:len(observed), j] = np.sort(observed * orientation[j])
    return sorted_values, counts, orientation, active


def _rank_apply(values, fitted):
    sorted_values, counts, orientation, active = fitted
    result = np.full(values.shape, np.nan, dtype=np.float32)
    for j in range(values.shape[1]):
        valid = np.isfinite(values[:, j])
        count = counts[j]
        if not active[j] or count == 0:
            result[valid, j] = 0
            continue
        reference = sorted_values[:count, j]
        current = values[valid, j] * orientation[j]
        left = np.searchsorted(reference, current, side="left")
        right = np.searchsorted(reference, current, side="right")
        result[valid, j] = (left + right) / count - 1
    return result


def _fit_model(values, targets, directions):
    fitted = _rank_fit(values, targets)
    x = _rank_apply(values, fitted)
    nonflat = directions != 0
    binary = (directions[nonflat] == 1).astype(int)
    support = np.bincount(binary, minlength=2)
    if (support == 0).any():
        raise ValueError("insufficient nonflat observations in both directions")
    weights = len(binary) / (2 * support[binary])
    model = XGBClassifier(**XGB_PARAMETERS, learning_rate=.03, min_child_weight=10,
                          reg_lambda=10, reg_alpha=.1, subsample=.8,
                          colsample_bytree=.6, max_bin=64, tree_method="hist",
                          objective="binary:logistic", eval_metric="logloss",
                          random_state=20261027, n_jobs=2, verbosity=0)
    model.fit(x[nonflat], binary, sample_weight=weights)
    return fitted, model


def _data_through(frames, cutoff):
    daily = frames["daily"]
    where = np.flatnonzero(daily["date"].to_numpy() == cutoff)
    if len(where) != 1:
        raise ValueError("daily cutoff missing or duplicated")
    daily = daily.iloc[:int(where[0]) + 1].copy()
    dates = pd.to_datetime(daily.pop("date"), format="%Y-%m-%d", errors="raise")
    daily.index = pd.DatetimeIndex(dates)
    if daily.index.has_duplicates:
        raise ValueError("daily dates must be unique")
    # The platform cuts by row position. If the source is not sorted, do not
    # consume an earlier-position row carrying a later-than-cutoff date.
    daily = daily.loc[daily.index <= pd.Timestamp(cutoff)].sort_index()
    calendar = None
    if FREQUENCY == "week":
        calendar = frames["calendar"].copy()
        calendar = calendar.loc[calendar.rdate <= cutoff].sort_values("rdate").reset_index(drop=True)
    return daily, calendar


def _request_clock(row, daily, *, historical_replay=False):
    date = pd.Timestamp(row["feature_date"])
    if historical_replay:
        if row["predict_date"] < MINIMUM_BACKTEST_DATE or date < pd.Timestamp(MINIMUM_BACKTEST_DATE):
            raise ValueError("historical diagnostic replay starts at 2025-01-01")
    else:
        if date < pd.Timestamp(MINIMUM_FEATURE_DATE):
            raise ValueError("feature date precedes the allowed strategy start")
        if row["predict_date"] < MINIMUM_PREDICT_DATE:
            raise ValueError("input was not established as available before this prediction")
    if row["feature_date"] > row["daily_cutoff_key"]:
        raise ValueError("feature date is beyond the daily cutoff")
    if date not in daily.index:
        raise ValueError("feature date is absent from the consumed daily data")
    if FREQUENCY == "week":
        return date, date
    monday = date - pd.Timedelta(days=date.dayofweek)
    dates = daily.index[(daily.index >= monday) & (daily.index <= date)]
    if not len(dates):
        raise ValueError("no trading row for the model's weekly fit")
    # 历史诊断按当时的周初训练；不能把训练时点抬到未来的因子冻结日。
    fit_date = dates[0]
    if date >= pd.Timestamp(MINIMUM_FEATURE_DATE):
        fit_date = max(fit_date, pd.Timestamp(MINIMUM_FEATURE_DATE))
    if fit_date > date:
        raise ValueError("model fit date exceeds the request feature date")
    return date, fit_date


def _predict_many(rows, frames, *, historical_replay=False):
    maximum = max(row["daily_cutoff_key"] for row in rows)
    daily, calendar = _data_through(frames, maximum)
    clocks = [_request_clock(row, daily, historical_replay=historical_replay) for row in rows]
    features = prepare_factor_features(daily)
    cutoff = pd.Timestamp(maximum)
    if FREQUENCY == "daily":
        labels = build_labels(daily, None, TENOR, FREQUENCY, cutoff)
        scores = calculate_factor_scores(features, FACTOR_SOURCES, FACTOR_IDS)
    else:
        labels = build_labels(daily, calendar, TENOR, FREQUENCY, cutoff)
        # A completed period is known only once a row of the next period was
        # observable. This matters when week_id changes on a nonstandard day.
        groups = list(calendar.groupby("week_id", sort=False))
        known_at = {pd.Timestamp(groups[i][1].rdate.iloc[-1]):
                    pd.Timestamp(groups[i + 1][1].rdate.iloc[0])
                    for i in range(len(groups) - 1)}
        labels["known_at"] = pd.to_datetime(labels.label_available_at.map(known_at))
        if labels.known_at.isna().any():
            raise ValueError("weekly labels have no observed next-period boundary")
        # Compute the daily operator values once; every historical score below
        # is reconstructed using only the clock known on its own signal date.
        raw_values = calculate_factor_values(features, FACTOR_SOURCES, FACTOR_IDS)
    fit_cache = {}
    score_cache = {}

    def weekly_score(date):
        if date in score_cache:
            return score_cache[date]
        position = daily.index.searchsorted(date, side="right") - 1
        if position < 0:
            raise ValueError("no daily observations before the weekly signal")
        latest_observation = daily.index[position]
        prior = ((labels.signal_date < date) & (labels.target_date < date)
                 & (labels.label_available_at < date) & (labels.known_at <= date)
                 & (labels.label_available_at <= latest_observation))
        history = pd.DatetimeIndex(labels.loc[prior, "signal_date"])
        dated_calendar = calendar.loc[calendar.rdate <= date.strftime("%Y-%m-%d")]
        if dated_calendar.empty:
            raise ValueError("weekly calendar has no observable history")
        bucket = dated_calendar[dated_calendar.week_id == dated_calendar.week_id.iloc[-1]]
        start = pd.Timestamp(bucket.rdate.iloc[0])
        pending = start - pd.Timedelta(days=start.dayofweek + 3)
        if (pending < date and pending not in history
                and (len(history) == 0 or pending > history[-1])
                and (daily.index <= pending).any()):
            history = history.append(pd.DatetimeIndex([pending]))
        # All current V8 weekly policies have at most 52 previous decisions.
        window = max(int((source.get("signal_policy") or source.get("signal_specs")
                          or (source.get("signal_rule") or {}).get("threshold_policy"))["history_window"])
                     for source in FACTOR_SOURCES)
        decisions = history[-window:].append(pd.DatetimeIndex([date]))
        if decisions.has_duplicates or not decisions.is_monotonic_increasing:
            raise ValueError("weekly decision clock is ambiguous")
        known_values = raw_values.loc[:date]
        if known_values.empty:
            raise ValueError("weekly factor history is empty")
        if known_values.index[-1] < date:
            # Nominal Friday may be a holiday; use the already observed last
            # close, without asking for a future daily observation.
            last = known_values.iloc[[-1]].copy()
            last.index = pd.DatetimeIndex([date])
            known_values = pd.concat([known_values, last])
        value = score_factor_values(known_values, FACTOR_SOURCES, FACTOR_IDS, decisions).iloc[-1].to_numpy(dtype=float)
        score_cache[date] = value
        return value

    results = []
    for date, asof in clocks:
        if asof not in fit_cache:
            eligible = ((labels.signal_date < asof) & (labels.target_date < asof)
                        & (labels.label_available_at < asof)
                        & (labels.target_date >= pd.Timestamp("2023-01-01")))
            if FREQUENCY == "week":
                eligible &= labels.known_at <= asof
            train = labels.loc[eligible]
            minimum = 100 if FREQUENCY == "daily" else 40
            if len(train) < minimum:
                raise ValueError("insufficient causally available training labels")
            historical = (scores.reindex(pd.DatetimeIndex(train.signal_date)).to_numpy(dtype=float)
                          if FREQUENCY == "daily" else
                          np.vstack([weekly_score(day) for day in train.signal_date]))
            if np.isnan(historical).all(axis=1).any():
                raise ValueError("factor history missing for a training date")
            fit_cache[asof] = _fit_model(historical, train.target.to_numpy(dtype=float),
                                         train.labels_actual.to_numpy(dtype=int))
        if date not in score_cache:
            if FREQUENCY == "week":
                values = weekly_score(date).reshape(1, -1)
            elif date in scores.index:
                values = scores.loc[date].to_numpy(dtype=float).reshape(1, -1)
                score_cache[date] = values[0]
            else:
                raise ValueError("factor scores missing for request feature date")
        values = score_cache[date].reshape(1, -1)
        if np.isfinite(values).mean() < .8:
            raise ValueError("factor coverage is below the original 80 percent requirement")
        fitted, model = fit_cache[asof]
        probability = float(model.predict_proba(_rank_apply(values, fitted))[0, 1])
        if not np.isfinite(probability):
            raise ValueError("model did not produce a finite probability")
        # Original classifier predicts the bond-price proxy. Contract returns
        # yield direction, which is its opposite.
        results.append(Prediction(-1 if probability >= .5 else 1, probability, probability))
    return results


def predict_one(row, cutoff_frames):
    return _predict_many([row], cutoff_frames)[0]


def predict_backtest_one(row, cutoff_frames):
    """当前固定因子池的历史诊断；与实盘入口使用相同的数值计算。"""
    return _predict_many([row], cutoff_frames, historical_replay=True)[0]


def predict_batch(rows, full_frames):
    # 每条请求仍使用自己的截止时点；无序输入单独截断。
    if not full_frames["daily"]["date"].is_monotonic_increasing:
        return [_predict_many([row], full_frames, historical_replay=True)[0] for row in rows]
    return _predict_many(rows, full_frames, historical_replay=True)

if __name__ == '__main__':
    raise SystemExit(run_cli(predict_one, _consumed_columns, predict_batch, require_week_mapping=(FREQUENCY == 'week'), backtest_predict_one=predict_backtest_one))
