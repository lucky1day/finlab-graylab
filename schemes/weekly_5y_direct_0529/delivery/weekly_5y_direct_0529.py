#!/usr/bin/env python3
"""Self-contained Blackbox V2 weekly-point successor delivery."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence
import warnings

# The delivery is one process and never creates child processes. Bound all common
# numerical runtimes before importing NumPy/scikit-learn/LightGBM.
for _thread_variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_variable] = "8"

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning)


from typing import Any

import numpy as np
import pandas as pd

# ---- 锁定规则配置（0529 原版，不做改动） ----
# 三个独立因子投票，每个权重 1.0，平局预测下行 (-1)

RULES: tuple[tuple[dict[str, Any], float], ...] = (
    (
        {
            "name": "five_year_7y_10y_spread_momentum_2w",
            "kind": "spread_change",
            "col_a": "TB7YWI3C",
            "col_b": "TB0YWI3C",
            "lookback": 2,
            "sign": 1.0,
        },
        1.0,
    ),
    (
        {
            "name": "five_year_5y_10y_spread_reversal_4w",
            "kind": "spread_change",
            "col_a": "TB5YWI3C",
            "col_b": "TB0YWI3C",
            "lookback": 4,
            "sign": -1.0,
        },
        1.0,
    ),
    (
        {
            "name": "five_year_1y_momentum_4w",
            "kind": "momentum",
            "source_col": "TB1YWI3C",
            "lookback": 4,
            "sign": 1.0,
        },
        1.0,
    ),
)


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {missing}")


def build_rule_signal(df: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    """计算单条规则的方向信号。

    Args:
        df: 含 week_id 和所需收益率列的 DataFrame，按 week_id 升序。
        rule: 规则定义，包含 name/kind/lookback/sign 及对应数据列。

    Returns:
        DataFrame，列 [week_id, {name}__prob_up, {name}__pred_label]，
        仅包含有效信号行（pred_label ∈ {-1, 1}）。
    """
    name = str(rule["name"])
    kind = str(rule["kind"])
    lookback = int(rule["lookback"])
    sign = float(rule.get("sign", 1.0))
    if lookback <= 0:
        raise ValueError(f"规则 lookback 必须 >0: {name}")

    _require_columns(df, ["week_id"])

    if kind == "momentum":
        source_col = str(rule["source_col"])
        _require_columns(df, [source_col])
        raw = pd.to_numeric(df[source_col], errors="coerce").ffill().pct_change(lookback, fill_method=None)
    elif kind == "spread_change":
        col_a = str(rule["col_a"])
        col_b = str(rule["col_b"])
        _require_columns(df, [col_a, col_b])
        spread = pd.to_numeric(df[col_a], errors="coerce") - pd.to_numeric(df[col_b], errors="coerce")
        raw = spread.diff(lookback)
    else:
        raise ValueError(f"不支持的规则类型: {kind}")

    signal = np.sign(raw.to_numpy(dtype=float) * sign)
    valid_mask = np.isin(signal, [-1.0, 1.0])
    return pd.DataFrame(
        {
            "week_id": df.loc[valid_mask, "week_id"].astype(int).to_numpy(),
            f"{name}__prob_up": np.where(signal[valid_mask] > 0, 0.55, 0.45),
            f"{name}__pred_label": signal[valid_mask].astype(int),
        }
    )


def build_rule_vote(
    df: pd.DataFrame,
    rules: tuple[tuple[dict[str, Any], float], ...] | None = None,
    tie_label: int = -1,
) -> pd.DataFrame:
    """多规则加权投票，输出最终预测方向与置信度。

    Args:
        df: 周频宽表，必须含 week_id + 所有规则引用的收益率列。
        rules: 规则列表；默认使用模块级 RULES。
        tie_label: 票数和为 0 时的预测方向（默认 -1=预测下行）。

    Returns:
        DataFrame，包含每个规则的信号列 + 汇总列（rule_vote,
        final_pred_label, final_prob_up, winner_model, source_spec, score_spec）。
        仅包含所有规则均产出有效信号的 week_id（inner join）。
    """
    if rules is None:
        rules = RULES

    merged: pd.DataFrame | None = None
    rule_names: list[str] = []
    weights: list[float] = []

    for rule_def, weight in rules:
        name = str(rule_def["name"])
        rule_names.append(name)
        weights.append(float(weight))
        signal_df = build_rule_signal(df, rule_def)
        merged = signal_df if merged is None else merged.merge(signal_df, on="week_id", how="inner")

    if merged is None or merged.empty:
        raise RuntimeError("没有产生任何有效规则投票行")

    weight_array = np.asarray(weights, dtype=float)
    pred_cols = [f"{name}__pred_label" for name in rule_names]
    prob_cols = [f"{name}__prob_up" for name in rule_names]
    pred_matrix = merged[pred_cols].to_numpy(dtype=float)
    prob_matrix = merged[prob_cols].to_numpy(dtype=float)

    vote = pred_matrix @ weight_array
    weight_sum = float(np.abs(weight_array).sum()) or 1.0

    merged["rule_vote"] = vote
    merged["final_pred_label"] = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label)).astype(int)
    merged["final_prob_up"] = np.clip((prob_matrix @ weight_array) / weight_sum, 0.0, 1.0)
    merged["winner_model"] = "Direct_Production_Final"
    merged["source_spec"] = ";".join(rule_names)
    merged["score_spec"] = ";".join(f"{name}:{weight:.4f}" for name, weight in zip(rule_names, weights))
    return merged

# ---------------------------------------------------------------------------
# Blackbox V2 transport boundary. The algorithm above is the readable Native
# core copied into this delivery; everything below only validates/slices Request
# input and serializes the exact five-field Result.
# ---------------------------------------------------------------------------

DELIVERY_SCHEME_ID = "weekly_5y_direct_0529_bbv2"
DELIVERY_LOOKBACK_WEEKS = 60
DELIVERY_REQUIRED_CURRENT_COLUMNS = ["TB1YWI3C","TB5YWI3C","TB7YWI3C","TB0YWI3C"]
REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
PERIOD_PATTERN = re.compile(r"^[0-9]{6}$")
_WEEK_END_BY_ID: dict[int, pd.Timestamp] = {}


def _strict_date(value: str, field: str) -> date:
    if DATE_PATTERN.fullmatch(value) is None:
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
    predict_date = _strict_date(value["predict_date"], "predict_date")
    feature_date = _strict_date(value["feature_date"], "feature_date")
    target_date = _strict_date(value["target_date"], "target_date")
    daily_cutoff = _strict_date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not feature_date <= predict_date <= target_date or not feature_date < target_date:
        raise ValueError(
            "dates must satisfy feature_date <= predict_date <= target_date "
            "and feature_date < target_date"
        )
    if daily_cutoff != feature_date:
        raise ValueError("daily_cutoff_key must equal feature_date for weekly_point")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if PERIOD_PATTERN.fullmatch(value[field]) is None:
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
        requests: list[dict[str, str]] = []
        for number, row in enumerate(reader, 2):
            if len(row) != 7:
                raise ValueError(f"requests CSV row {number} has missing or extra cells")
            requests.append(validate_request(dict(zip(header, row))))
    if not requests:
        raise ValueError("requests CSV must contain at least one request")
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("request_id values must be unique within a batch")
    return requests


def _read_csv(
    path: Path,
    key: str,
    required: Sequence[str],
    *,
    all_columns: bool = False,
) -> pd.DataFrame:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required regular data file is missing: {path.name}")
    try:
        header = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns.tolist()
    except Exception as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if len(header) != len(set(header)) or key not in header:
        raise ValueError(f"invalid or duplicate key/header in {path.name}: {key}")
    missing = sorted(set(required).difference(header))
    if missing:
        raise ValueError(f"{path.name} is missing required columns: {missing}")
    usecols = None if all_columns else [key, *required]
    frame = pd.read_csv(
        path,
        encoding="utf-8-sig",
        usecols=usecols,
        low_memory=False,
    )
    frame.columns = [str(column).strip().lstrip("\ufeff") for column in frame.columns]
    if frame[key].duplicated().any():
        raise ValueError(f"duplicate key in {path.name}: {key}")
    return frame


def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    calendar = _read_csv(
        data_dir / "api_wind_date.csv",
        "rdate",
        ("week_id",),
    )
    daily = _read_csv(data_dir / "daily_output.csv", "date", ())
    weekly = _read_csv(
        data_dir / "weekly_output.csv",
        "week_id",
        tuple(DELIVERY_REQUIRED_CURRENT_COLUMNS),
        all_columns=True,
    )

    calendar["rdate"] = pd.to_datetime(calendar["rdate"], errors="raise").dt.normalize()
    if calendar["rdate"].duplicated().any() or not calendar["rdate"].is_monotonic_increasing:
        raise ValueError("api_wind_date.csv dates must be unique and ascending")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    if daily["date"].duplicated().any() or not daily["date"].is_monotonic_increasing:
        raise ValueError("daily_output.csv dates must be unique and ascending")
    calendar["week_id"] = pd.to_numeric(calendar["week_id"], errors="raise")
    if (
        calendar["week_id"].isna().any()
        or not np.isfinite(calendar["week_id"]).all()
        or not np.equal(calendar["week_id"], np.floor(calendar["week_id"])).all()
    ):
        raise ValueError("api_wind_date.csv contains invalid week_id values")
    calendar["week_id"] = calendar["week_id"].astype(int)

    weekly["week_id"] = pd.to_numeric(weekly["week_id"], errors="raise")
    if (
        weekly["week_id"].isna().any()
        or not np.isfinite(weekly["week_id"]).all()
        or not np.equal(weekly["week_id"], np.floor(weekly["week_id"])).all()
    ):
        raise ValueError("weekly_output.csv contains invalid week_id values")
    weekly["week_id"] = weekly["week_id"].astype(int)
    if weekly["week_id"].duplicated().any() or not weekly["week_id"].is_monotonic_increasing:
        raise ValueError("weekly_output.csv week_id values must be unique and ascending")
    return calendar, daily, weekly


def _request_week_id(
    request: dict[str, str],
    calendar: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
) -> int:
    feature = pd.Timestamp(request["feature_date"])
    matches = calendar["rdate"].eq(feature)
    if int(matches.sum()) != 1:
        raise ValueError("feature_date must exist exactly once in api_wind_date.csv")
    current_week_id = int(calendar.loc[matches, "week_id"].iloc[0])
    if str(current_week_id) != request["weekly_cutoff_key"]:
        raise ValueError("feature_date does not map to weekly_cutoff_key")

    trading_dates = daily.merge(
        calendar.loc[:, ["rdate", "week_id"]],
        left_on="date",
        right_on="rdate",
        how="left",
        validate="one_to_one",
    )
    if trading_dates["week_id"].isna().any():
        raise ValueError("daily_output.csv contains dates missing from api_wind_date.csv")
    week_ends = (
        trading_dates.groupby("week_id", sort=True, observed=True)["date"]
        .max()
        .sort_index()
    )
    if current_week_id not in week_ends.index or week_ends.loc[current_week_id] != feature:
        raise ValueError("feature_date must be the current observed trading-week endpoint")

    matches = weekly["week_id"].eq(current_week_id)
    if int(matches.sum()) != 1:
        raise ValueError("weekly_cutoff_key must exist exactly once in weekly_output.csv")
    current = weekly.loc[matches, DELIVERY_REQUIRED_CURRENT_COLUMNS].apply(
        pd.to_numeric,
        errors="coerce",
    )
    if current.empty or not bool(np.isfinite(current.to_numpy(dtype=float)).all()):
        raise ValueError("current week is missing a required finite input")
    return current_week_id


def _run_algorithm(weekly: pd.DataFrame) -> pd.DataFrame:
    result = build_rule_vote(weekly)
    return result.loc[:, ["week_id", "final_pred_label"]].rename(
        columns={"final_pred_label": "predicted_direction"}
    )


def generate(
    requests: Sequence[dict[str, str]],
    data: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> list[dict[str, Any]]:
    global _WEEK_END_BY_ID
    calendar, daily, weekly = data
    validated = [validate_request(request) for request in requests]
    week_ids = [
        _request_week_id(request, calendar, daily, weekly)
        for request in validated
    ]

    lower = min(week_id - DELIVERY_LOOKBACK_WEEKS for week_id in week_ids)
    upper = max(week_ids)
    input_frame = weekly.loc[
        weekly["week_id"].between(lower, upper, inclusive="both")
    ].copy()
    _WEEK_END_BY_ID = (
        daily.merge(
            calendar.loc[:, ["rdate", "week_id"]],
            left_on="date",
            right_on="rdate",
            how="left",
            validate="one_to_one",
        )
        .groupby("week_id", sort=True, observed=True)["date"]
        .max()
        .to_dict()
    )
    predictions = _run_algorithm(input_frame)
    predictions["week_id"] = pd.to_numeric(
        predictions["week_id"],
        errors="raise",
    ).astype(int)
    predictions["predicted_direction"] = pd.to_numeric(
        predictions["predicted_direction"],
        errors="raise",
    )
    if predictions["week_id"].duplicated().any():
        raise RuntimeError("algorithm returned duplicate week_id values")
    by_week = predictions.set_index("week_id")["predicted_direction"].to_dict()

    results: list[dict[str, Any]] = []
    for request, week_id in zip(validated, week_ids):
        raw_direction = by_week.get(week_id, 0)
        if (
            isinstance(raw_direction, bool)
            or not np.isfinite(float(raw_direction))
            or not float(raw_direction).is_integer()
            or int(raw_direction) not in (-1, 0, 1)
        ):
            raise RuntimeError("algorithm returned an invalid predicted_direction")
        results.append(
            {
                "request_id": request["request_id"],
                "predict_date": request["predict_date"],
                "feature_date": request["feature_date"],
                "target_date": request["target_date"],
                "predicted_direction": int(raw_direction),
            }
        )
    return results


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
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
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
    writer.writerow(RESULT_FIELDS)
    writer.writerows([[row[field] for field in RESULT_FIELDS] for row in rows])
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
