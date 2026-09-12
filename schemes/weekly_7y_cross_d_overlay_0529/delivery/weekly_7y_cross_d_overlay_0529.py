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


from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


TARGET_COL = "TB7YWI3C"
REQUIRED_COLUMNS = ("week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C")

CROSS_D_CONFIG: dict[str, Any] = {
    "source": "5Y_D",
    "allowed_direction": "down",
    "main_low_conf": 0.05,
    "aux_min_conf": 0.0,
    "rate_threshold": 2.0,
    "require_aux_overlay": False,
}


@dataclass(frozen=True)
class RuleSpec:
    """0529 规则定义。"""

    name: str
    kind: str
    lookback: int
    sign: float = 1.0
    source_col: str = ""
    col_a: str = ""
    col_b: str = ""
    weight: float = 1.0


SEVEN_Y_BASE_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="seven_year_1y_3y_spread_reversal_3w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=3,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_1y_5y_spread_momentum_3w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=3,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_1y_5y_spread_momentum_6w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=6,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_5y_7y_spread_momentum_1w",
        kind="spread_change",
        col_a="TB5YWI3C",
        col_b="TB7YWI3C",
        lookback=1,
        sign=1.0,
    ),
)

SEVEN_Y_REBOUND_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="seven_year_overlay_3y_momentum_1w",
        kind="momentum",
        source_col="TB3YWI3C",
        lookback=1,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_3y_ratio_reversal_8w",
        kind="ratio_momentum",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=8,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_3y_spread_reversal_8w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=8,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_5y_ratio_momentum_1w",
        kind="ratio_momentum",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=1,
        sign=1.0,
    ),
)

FIVE_Y_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="five_year_7y_10y_spread_momentum_2w",
        kind="spread_change",
        col_a="TB7YWI3C",
        col_b="TB0YWI3C",
        lookback=2,
        sign=1.0,
    ),
    RuleSpec(
        name="five_year_5y_10y_spread_reversal_4w",
        kind="spread_change",
        col_a="TB5YWI3C",
        col_b="TB0YWI3C",
        lookback=4,
        sign=-1.0,
    ),
    RuleSpec(
        name="five_year_1y_momentum_4w",
        kind="momentum",
        source_col="TB1YWI3C",
        lookback=4,
        sign=1.0,
    ),
)


def build_cross_d_overlay(weekly: pd.DataFrame) -> pd.DataFrame:
    """构建 7Y Cross-D 最终预测序列。

    Args:
        weekly: 周频宽表，必须包含 week_id 与 REQUIRED_COLUMNS 中的收益率列。

    Returns:
        DataFrame，按 week_id 升序，含 7Y 主模型、5Y 辅助信号与最终 Cross-D 输出。
    """
    normalized = _normalize_weekly(weekly)
    seven_y = build_7y_main(normalized)
    five_y = build_5y_aux(normalized)
    return apply_cross_d(seven_y, five_y, normalized)


def build_7y_main(weekly: pd.DataFrame) -> pd.DataFrame:
    """7Y 主规则投票 + 低利率反弹叠加。"""
    out = fixed_vote(weekly, SEVEN_Y_BASE_RULES, TARGET_COL, "seven_y_base", tie_label=-1)
    for rule in SEVEN_Y_REBOUND_RULES:
        add_rule_signal(out, weekly, rule)

    overlay_names = [rule.name for rule in SEVEN_Y_REBOUND_RULES]
    overlay_cols = [f"{name}__signal" for name in overlay_names]
    overlay_valid = out[overlay_cols].isin([-1.0, 1.0]).all(axis=1).to_numpy()
    overlay_matrix = out[overlay_cols].to_numpy(dtype=float)
    agree_count = np.where(overlay_valid, np.sum(overlay_matrix == 1.0, axis=1), np.nan)
    overlay_vote = np.where(overlay_valid, np.nansum(overlay_matrix, axis=1), np.nan)
    regime_valid = pd.to_numeric(weekly[TARGET_COL], errors="coerce").le(1.85).to_numpy()

    base_pred = out["seven_y_base_pred_label"].to_numpy(dtype=float).copy()
    base_prob = out["seven_y_base_prob_up"].to_numpy(dtype=float).copy()
    base_vote = out["seven_y_base_vote"].to_numpy(dtype=float)
    base_valid = np.isin(base_pred, [-1.0, 1.0])

    label_overlay_candidate = (
        base_valid
        & overlay_valid
        & (base_pred == -1.0)
        & (base_vote <= -2.0)
        & (agree_count >= 3)
    )
    label_overlay_applied = label_overlay_candidate & regime_valid

    pred = base_pred.copy()
    prob = base_prob.copy()
    pred[label_overlay_applied] = 1.0
    adjusted_prob = np.clip(0.62 + 0.03 * np.maximum(agree_count - 3, 0), 0.0, 1.0)
    prob[label_overlay_applied] = np.maximum(prob[label_overlay_applied], adjusted_prob[label_overlay_applied])

    out["actual_label"] = out["seven_y_base_actual_label"]
    out["future_return"] = out["seven_y_base_future_return"]
    out["label_overlay_vote"] = overlay_vote
    out["label_overlay_agree_count"] = agree_count
    out["label_overlay_regime_valid"] = regime_valid
    out["label_overlay_candidate"] = label_overlay_candidate
    out["label_overlay_applied"] = label_overlay_applied
    out["main_pred_label"] = pred
    out["main_prob_up"] = prob
    return out[out["main_pred_label"].isin([-1.0, 1.0])].copy()


def build_5y_aux(weekly: pd.DataFrame) -> pd.DataFrame:
    """构建 5Y D 辅助信号。"""
    out = fixed_vote(weekly, FIVE_Y_RULES, "TB5YWI3C", "five_y", tie_label=-1)
    out = out[out["five_y_pred_label"].isin([-1.0, 1.0])].copy()
    out["d5_d_pred_label"] = out["five_y_pred_label"].astype(int)
    out["d5_d_prob_up"] = out["five_y_prob_up"].astype(float)
    out["d5_conf"] = (out["d5_d_prob_up"] - 0.5).abs()
    out["d5_d_model2_overlay"] = False
    out["d5_d_signal_source"] = "fixed_5y_vote"
    return out[
        [
            "week_id",
            "d5_d_pred_label",
            "d5_d_prob_up",
            "d5_conf",
            "d5_d_model2_overlay",
            "d5_d_signal_source",
        ]
    ].copy()


def apply_cross_d(seven_y: pd.DataFrame, five_y: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """将 5Y 下行确认窄口径覆盖到 7Y 主信号。"""
    rates = weekly[list(REQUIRED_COLUMNS)].copy()
    out = seven_y.merge(five_y, on="week_id", how="inner")
    out = out.drop(columns=[col for col in REQUIRED_COLUMNS if col in out.columns and col != "week_id"])
    out = out.merge(rates, on="week_id", how="left")

    aux_pred = out["d5_d_pred_label"].astype(int)
    aux_prob = out["d5_d_prob_up"].astype(float)
    aux_conf = out["d5_conf"].astype(float)
    main_pred = out["main_pred_label"].astype(int)
    main_conf = (out["main_prob_up"].astype(float) - 0.5).abs()

    overlay = (
        aux_pred.ne(main_pred)
        & (main_conf <= float(CROSS_D_CONFIG["main_low_conf"]))
        & (aux_conf >= float(CROSS_D_CONFIG["aux_min_conf"]))
        & aux_pred.eq(-1)
        & (out["TB7YWI3C"].astype(float) <= float(CROSS_D_CONFIG["rate_threshold"]))
    )

    out["cross_d_overlay"] = overlay.astype(bool)
    out["cross_d_pred_label"] = np.where(overlay, aux_pred, main_pred).astype(int)
    out["cross_d_prob_up"] = np.where(overlay, aux_prob, out["main_prob_up"]).astype(float)
    out["cross_d_signal_source"] = np.where(overlay, "5y_d_down_overlay", "7y_rule_vote_main")

    valid_actual = out["actual_label"].notna()
    out["cross_d_is_correct"] = (
        valid_actual & out["cross_d_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    )
    out["main_is_correct"] = (
        valid_actual & out["main_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    )
    return out.sort_values("week_id").reset_index(drop=True)


def fixed_vote(
    weekly: pd.DataFrame,
    rules: tuple[RuleSpec, ...],
    target_col: str,
    prefix: str,
    tie_label: int = -1,
) -> pd.DataFrame:
    """固定规则加权投票。"""
    _require_columns(weekly, ["week_id", target_col])
    out = weekly[["week_id", target_col]].copy()
    future_return, actual_label = make_labels(weekly, target_col)
    out[f"{prefix}_future_return"] = future_return
    out[f"{prefix}_actual_label"] = actual_label

    for rule in rules:
        add_rule_signal(out, weekly, rule)

    signal_cols = [f"{rule.name}__signal" for rule in rules]
    valid = out[signal_cols].isin([-1.0, 1.0]).all(axis=1)
    weights = np.asarray([rule.weight for rule in rules], dtype=float)
    matrix = out[signal_cols].to_numpy(dtype=float)
    vote = matrix @ weights
    pred = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label)).astype(float)
    pred[~valid.to_numpy()] = np.nan

    prob_matrix = np.where(matrix > 0, 0.55, 0.45)
    prob_up = (prob_matrix @ weights) / max(float(np.abs(weights).sum()), 1.0)
    prob_up[~valid.to_numpy()] = np.nan

    out[f"{prefix}_vote"] = vote
    out[f"{prefix}_pred_label"] = pred
    out[f"{prefix}_prob_up"] = prob_up
    return out


def make_labels(df: pd.DataFrame, target_col: str) -> tuple[pd.Series, pd.Series]:
    """计算下一周收益率变化方向，仅供回测评估使用。"""
    close = pd.to_numeric(df[target_col], errors="coerce")
    future_return = close.shift(-1).div(close).sub(1.0)
    label = pd.Series(
        np.select([future_return > 0, future_return < 0], [1, -1], default=0).astype(float),
        index=df.index,
    )
    label[future_return.isna()] = np.nan
    return future_return, label


def add_rule_signal(out: pd.DataFrame, weekly: pd.DataFrame, rule: RuleSpec) -> None:
    """向输出表追加单条规则信号。"""
    raw = rule_raw(rule, weekly)
    signed_raw = raw * rule.sign
    out[f"{rule.name}__raw"] = raw
    out[f"{rule.name}__signed_raw"] = signed_raw
    out[f"{rule.name}__signal"] = np.sign(signed_raw.to_numpy(dtype=float))


def rule_raw(rule: RuleSpec, weekly: pd.DataFrame) -> pd.Series:
    """计算规则原始动量或利差变化。"""
    if rule.kind == "momentum":
        _require_columns(weekly, [rule.source_col])
        return pd.to_numeric(weekly[rule.source_col], errors="coerce").pct_change(
            rule.lookback,
            fill_method=None,
        )
    if rule.kind == "spread_change":
        _require_columns(weekly, [rule.col_a, rule.col_b])
        spread = pd.to_numeric(weekly[rule.col_a], errors="coerce") - pd.to_numeric(
            weekly[rule.col_b],
            errors="coerce",
        )
        return spread.diff(rule.lookback)
    if rule.kind == "ratio_momentum":
        _require_columns(weekly, [rule.col_a, rule.col_b])
        ratio = pd.to_numeric(weekly[rule.col_a], errors="coerce") / pd.to_numeric(
            weekly[rule.col_b],
            errors="coerce",
        )
        return ratio.pct_change(rule.lookback, fill_method=None)
    raise ValueError(f"不支持的规则类型: {rule.kind}")


def _normalize_weekly(weekly: pd.DataFrame) -> pd.DataFrame:
    df = weekly.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    _require_columns(df, list(REQUIRED_COLUMNS))
    df = df[list(REQUIRED_COLUMNS)].copy()
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    for col in REQUIRED_COLUMNS:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").drop_duplicates("week_id", keep="last").reset_index(drop=True)


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {missing}")

# ---------------------------------------------------------------------------
# Blackbox V2 transport boundary. The algorithm above is the readable Native
# core copied into this delivery; everything below only validates/slices Request
# input and serializes the exact five-field Result.
# ---------------------------------------------------------------------------

DELIVERY_SCHEME_ID = "weekly_7y_cross_d_overlay_0529_bbv2"
DELIVERY_LOOKBACK_WEEKS = 80
DELIVERY_REQUIRED_CURRENT_COLUMNS = ["TB1YWI3C","TB3YWI3C","TB5YWI3C","TB7YWI3C","TB0YWI3C"]
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
    result = build_cross_d_overlay(weekly)
    return result.loc[:, ["week_id", "cross_d_pred_label"]].rename(
        columns={"cross_d_pred_label": "predicted_direction"}
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
