#!/usr/bin/env python3
"""Blackbox V2 Contract 1.0 frozen weekly-close vote scheme template."""

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
import lightgbm as lgb
import xgboost as xgb
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


SEED = 20260901
RANDOM_STATE = SEED
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
SCHEME_CONFIG: dict[str, Any] = {'scheme_id': 'weekly_5y_full_action_lowcorr03_v3',
 'target': 'TB5YWI0C',
 'weekly_target': 'TB5YWI3C',
 'quorum': 1,
 'daily_columns': ['TB0YWI0C', 'TB1YWI0C', 'TB3YWI0C', 'TB5YWI0C', 'TB7YWI0C'],
 'weekly_columns': ['CSI26902',
                    'G0100893',
                    'HWM00007',
                    'HWM00018',
                    'HWW00005',
                    'HWW00010',
                    'L0147566',
                    'M0161682',
                    'M0161686',
                    'M0217126',
                    'M1024902',
                    'M1332208',
                    'RSI010YW',
                    'S5021697',
                    'S5133394',
                    'S5134688',
                    'S5134689',
                    'S5134691',
                    'S5440890',
                    'S5441858',
                    'S5442122',
                    'S5442127',
                    'S5446171',
                    'S5458286',
                    'S5458287',
                    'S5470009',
                    'S5715671',
                    'S5811163',
                    'S5811176',
                    'SH069802',
                    'TB5YWI3C',
                    'TCFE002A',
                    'TCFE002V',
                    'TCFE020I',
                    'TCFE02VR',
                    'TCFE03WR',
                    'ZO000004',
                    'ZO000017',
                    'ZO000035'],
 'members': [{'config_id': 'lgbm_0097',
              'family': 'lgbm',
              'train_window': 208,
              'params': {'n_estimators': 120,
                         'learning_rate': 0.06,
                         'num_leaves': 8,
                         'min_child_samples': 20,
                         'reg_alpha': 0.5,
                         'reg_lambda': 3.0},
              'reject': 0.0,
              'features': ['core::vol4',
                           'core::vol3',
                           'core::spread_TB0YWI0C_TB5YWI0C',
                           'core::vol26',
                           'core::vol8',
                           'core::spread_TB3YWI0C_TB1YWI0C_d1',
                           'core::z3',
                           'core::target_sign2',
                           'core::vol13',
                           'core::spread_TB0YWI0C_TB3YWI0C',
                           'core::spread_TB0YWI0C_TB1YWI0C_d1',
                           'core::spread_TB5YWI0C_TB1YWI0C_d1',
                           'core::spread_TB7YWI0C_TB3YWI0C_d4',
                           'core::spread_TB0YWI0C_TB5YWI0C_d4',
                           'core::spread_TB0YWI0C_TB3YWI0C_d4',
                           'core::spread_TB0YWI0C_TB5YWI0C_d13',
                           'core::vol52',
                           'core::spread_TB7YWI0C_TB3YWI0C',
                           'core::target_diff1',
                           'core::target_diff8',
                           'core::target_sign6',
                           'core::target_sign3',
                           'core::target_sign1',
                           'core::spread_TB3YWI0C_TB1YWI0C_d4',
                           'core::mom8',
                           'core::target_sign52',
                           'core::z13',
                           'core::spread_TB0YWI0C_TB3YWI0C_d13',
                           'core::z8',
                           'core::spread_TB5YWI0C_TB1YWI0C_d4',
                           'core::target_diff2',
                           'core::z4',
                           'core::z52',
                           'core::spread_TB0YWI0C_TB1YWI0C_d4',
                           'core::target_diff3',
                           'core::mom3',
                           'core::target_diff4',
                           'core::target_diff26',
                           'core::mom4',
                           'core::spread_TB5YWI0C_TB3YWI0C',
                           'core::mom26',
                           'core::target_diff52',
                           'core::mom52',
                           'core::spread_TB7YWI0C_TB3YWI0C_d13',
                           'core::spread_TB5YWI0C_TB1YWI0C',
                           'core::spread_TB3YWI0C_TB1YWI0C_d13',
                           'core::spread_TB0YWI0C_TB5YWI0C_d1',
                           'core::z26',
                           'core::target_diff6',
                           'core::target_sign8',
                           'w_diff4::S5442127',
                           'w_diff13::M1332208',
                           'w_raw::TCFE020I',
                           'w_diff1::S5133394',
                           'w_diff2::S5442127',
                           'w_diff13::TCFE020I',
                           'w_diff13::S5458287',
                           'w_diff4::S5134688',
                           'w_diff4::S5442122',
                           'w_diff4::TCFE03WR',
                           'w_diff4::S5470009',
                           'w_diff4::S5715671',
                           'w_diff1::ZO000035',
                           'w_diff4::G0100893',
                           'w_diff1::TCFE002A',
                           'w_diff4::M0161682',
                           'w_diff4::S5446171',
                           'w_diff4::S5811176',
                           'w_diff1::TCFE02VR',
                           'w_diff1::S5458286',
                           'w_diff4::ZO000017',
                           'w_diff13::M1024902',
                           'w_raw::L0147566',
                           'w_diff13::SH069802',
                           'w_diff2::S5470009',
                           'w_diff2::M0161686',
                           'w_diff1::TCFE002V',
                           'w_diff13::S5134689',
                           'w_diff1::S5021697',
                           'w_diff4::S5441858',
                           'w_diff13::HWW00005',
                           'w_diff2::RSI010YW',
                           'w_raw::HWW00005',
                           'w_raw::RSI010YW',
                           'w_diff4::HWM00007',
                           'w_diff2::S5133394',
                           'w_diff2::S5811163',
                           'w_diff1::TCFE020I',
                           'w_raw::HWW00010',
                           'w_diff4::CSI26902',
                           'w_diff1::M0217126',
                           'w_diff13::HWM00018',
                           'w_diff13::S5134691',
                           'w_diff4::ZO000004',
                           'w_diff1::HWW00005',
                           'w_diff1::S5440890']},
             {'config_id': 'rule_0005',
              'family': 'rule',
              'train_window': 208,
              'params': {'rank': 5},
              'reject': 0.0,
              'features': ['core::vol4',
                           'core::vol3',
                           'core::spread_TB0YWI0C_TB5YWI0C',
                           'core::vol26',
                           'core::vol8',
                           'core::spread_TB3YWI0C_TB1YWI0C_d1']},
             {'config_id': 'rule_0019',
              'family': 'rule',
              'train_window': 208,
              'params': {'rank': 19},
              'reject': 0.0,
              'features': ['core::vol4',
                           'core::vol3',
                           'core::spread_TB0YWI0C_TB5YWI0C',
                           'core::vol26',
                           'core::vol8',
                           'core::spread_TB3YWI0C_TB1YWI0C_d1',
                           'core::z3',
                           'core::target_sign2',
                           'core::vol13',
                           'core::spread_TB0YWI0C_TB3YWI0C',
                           'core::spread_TB0YWI0C_TB1YWI0C_d1',
                           'core::spread_TB5YWI0C_TB1YWI0C_d1',
                           'core::spread_TB7YWI0C_TB3YWI0C_d4',
                           'core::spread_TB0YWI0C_TB5YWI0C_d4',
                           'core::spread_TB0YWI0C_TB3YWI0C_d4',
                           'core::spread_TB0YWI0C_TB5YWI0C_d13',
                           'core::vol52',
                           'core::spread_TB7YWI0C_TB3YWI0C',
                           'core::target_diff1',
                           'core::target_diff8']}]}
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
OUTPUT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date", "predicted_direction",
)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KEY_RE = re.compile(r"[0-9]{6}")


def _config_value(name: str) -> Any:
    if name not in SCHEME_CONFIG:
        raise RuntimeError(f"missing frozen scheme configuration: {name}")
    return SCHEME_CONFIG[name]


SCHEME_ID = str(_config_value("scheme_id"))
TARGET = str(_config_value("target"))
WEEKLY_TARGET = str(_config_value("weekly_target"))
QUORUM = int(_config_value("quorum"))
MEMBERS = tuple(_config_value("members"))
DAILY_COLUMNS = tuple(str(x) for x in _config_value("daily_columns"))
WEEKLY_COLUMNS = tuple(str(x) for x in _config_value("weekly_columns"))
if not MEMBERS or not 1 <= QUORUM <= len(MEMBERS):
    raise RuntimeError("invalid frozen vote configuration")


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


def _read_required_strings(
    path: Path,
    key: str,
    required: Sequence[str],
) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"required data file is missing: {path.name}")
    try:
        header = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns.tolist()
    except Exception as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not header or header[0] != key or len(header) != len(set(header)):
        raise ValueError(f"invalid or duplicate key/header in {path.name}: {key}")
    missing = sorted(set(required).difference(header))
    if missing:
        raise ValueError(f"{path.name} is missing required columns: {missing}")
    try:
        frame = pd.read_csv(
            path,
            encoding="utf-8-sig",
            usecols=[key, *required],
            dtype="string",
            keep_default_na=False,
            low_memory=False,
        )
    except Exception as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if frame.empty or frame[key].str.strip().eq("").any() or frame[key].duplicated().any():
        raise ValueError(f"{path.name} {key} must be non-empty and unique")
    return frame


def _numeric_columns(frame: pd.DataFrame, columns: Sequence[str], label: str) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        raw = result[column].astype("string").str.strip()
        numeric = pd.to_numeric(raw.mask(raw.eq("")), errors="coerce")
        invalid = raw.ne("") & numeric.isna()
        if invalid.any() or np.isinf(numeric.dropna().to_numpy(dtype=float)).any():
            raise ValueError(f"{label} contains invalid numeric values in {column}")
        result[column] = numeric
    return result


def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be an existing directory")
    calendar = _read_required_strings(
        data_dir / "api_wind_date.csv", "rdate", ("week_id",)
    )
    if list(calendar.columns) != ["rdate", "week_id"]:
        raise ValueError("api_wind_date.csv columns must be exactly rdate,week_id")
    daily = _read_required_strings(data_dir / "daily_output.csv", "date", DAILY_COLUMNS)
    weekly = _read_required_strings(data_dir / "weekly_output.csv", "week_id", WEEKLY_COLUMNS)
    catalog = _read_required_strings(
        data_dir / "factor_catalog.csv",
        "indicators_code",
        ("frequency", "factor_version"),
    )
    if list(catalog.columns) != ["indicators_code", "frequency", "factor_version"]:
        raise ValueError(
            "factor_catalog.csv columns must be exactly indicators_code,frequency,factor_version"
        )
    catalog_frequency = catalog.set_index("indicators_code")["frequency"].to_dict()
    invalid_daily = sorted(
        column for column in DAILY_COLUMNS if catalog_frequency.get(column) != "daily"
    )
    invalid_weekly = sorted(
        column for column in WEEKLY_COLUMNS if catalog_frequency.get(column) != "weekly"
    )
    if invalid_daily or invalid_weekly:
        raise ValueError(
            "factor_catalog.csv frequency mismatch for frozen columns: "
            f"daily={invalid_daily[:10]} weekly={invalid_weekly[:10]}"
        )
    calendar["rdate"] = pd.to_datetime(calendar["rdate"], errors="raise").dt.normalize()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    if not calendar["rdate"].is_monotonic_increasing or not daily["date"].is_monotonic_increasing:
        raise ValueError("calendar and daily dates must be strictly ascending")
    if not calendar["week_id"].str.fullmatch(KEY_RE).all():
        raise ValueError("calendar contains invalid week_id")
    if not weekly["week_id"].str.fullmatch(KEY_RE).all():
        raise ValueError("weekly data contains invalid week_id")
    daily = _numeric_columns(daily, DAILY_COLUMNS, "daily_output.csv")
    weekly = _numeric_columns(weekly, WEEKLY_COLUMNS, "weekly_output.csv")
    daily = daily.merge(
        calendar.rename(columns={"rdate": "date"}),
        on="date",
        how="left",
        validate="one_to_one",
    )
    if daily["week_id"].isna().any():
        raise ValueError("daily dates are missing from authoritative calendar")
    return calendar, daily, weekly


def cutoff_frames(
    request: dict[str, str],
    calendar: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    same_week_dates = daily.loc[daily["week_id"].astype(str).eq(mapped), "date"]
    if same_week_dates.empty or same_week_dates.max() != cutoff:
        raise ValueError("feature_date is not the last real trading date of its authoritative week")
    weekly_matches = weekly["week_id"].eq(mapped)
    if int(weekly_matches.sum()) != 1:
        raise ValueError("weekly_cutoff_key must exist exactly once in weekly data")
    weekly_index = int(np.flatnonzero(weekly_matches.to_numpy())[0])
    weekly_slice = weekly.iloc[:weekly_index + 1].copy()
    if pd.isna(weekly_slice.iloc[-1][WEEKLY_TARGET]):
        raise ValueError("weekly_cutoff_key is not a closed week")
    closed_week_ids = set(
        weekly_slice.loc[weekly_slice[WEEKLY_TARGET].notna(), "week_id"].astype(str)
    )
    daily_slice = daily.loc[daily["date"].le(cutoff)].copy()
    endpoints = (
        daily_slice.groupby("week_id", sort=False, observed=True)
        .tail(1)
        .sort_values("date")
        .reset_index(drop=True)
    )
    endpoints = endpoints.loc[
        endpoints["week_id"].astype(str).isin(closed_week_ids)
        & endpoints[TARGET].notna()
    ].reset_index(drop=True)
    if endpoints.empty or str(endpoints.iloc[-1]["week_id"]) != mapped:
        raise ValueError("weekly cutoff did not produce a usable feature row")
    return endpoints, weekly_slice


def build_features(endpoints: pd.DataFrame, weekly_slice: pd.DataFrame) -> pd.DataFrame:
    daily_numeric = endpoints.drop(columns=["date", "week_id"], errors="ignore").astype(float)
    daily_numeric.columns = [f"d_raw::{column}" for column in daily_numeric.columns]
    weekly_numeric = weekly_slice.set_index("week_id").reindex(
        endpoints["week_id"].astype(str)
    )
    weekly_numeric = weekly_numeric.reset_index(drop=True).astype(float)
    weekly_numeric.columns = [f"w_raw::{column}" for column in weekly_numeric.columns]
    blocks: list[pd.DataFrame] = [daily_numeric, weekly_numeric]
    for lag in (1, 2, 4, 13):
        daily_diff = daily_numeric.diff(lag)
        daily_diff.columns = [
            f"d_diff{lag}::{column.split('::', 1)[1]}" for column in daily_numeric.columns
        ]
        weekly_diff = weekly_numeric.diff(lag)
        weekly_diff.columns = [
            f"w_diff{lag}::{column.split('::', 1)[1]}" for column in weekly_numeric.columns
        ]
        blocks.extend([daily_diff, weekly_diff])

    current = pd.to_numeric(endpoints[TARGET], errors="coerce")
    core = pd.DataFrame(index=endpoints.index)
    for lag in (1, 2, 3, 4, 6, 8, 13, 26, 52):
        core[f"core::target_diff{lag}"] = current.diff(lag)
        core[f"core::target_sign{lag}"] = np.sign(current.diff(lag))
    change = current.diff()
    for window in (3, 4, 8, 13, 26, 52):
        minimum = max(2, window // 2)
        core[f"core::mom{window}"] = change.rolling(window, min_periods=minimum).sum()
        core[f"core::vol{window}"] = change.rolling(window, min_periods=minimum).std()
        mean = current.rolling(window, min_periods=minimum).mean()
        std = current.rolling(window, min_periods=minimum).std()
        core[f"core::z{window}"] = current.sub(mean).div(std.replace(0.0, np.nan))
    for left, right in (
        ("TB3YWI0C", "TB1YWI0C"),
        ("TB5YWI0C", "TB3YWI0C"),
        ("TB7YWI0C", "TB3YWI0C"),
        ("TB0YWI0C", "TB3YWI0C"),
        ("TB0YWI0C", "TB5YWI0C"),
        ("TB5YWI0C", "TB1YWI0C"),
        ("TB0YWI0C", "TB1YWI0C"),
    ):
        if left in endpoints and right in endpoints:
            spread = pd.to_numeric(endpoints[left], errors="coerce") - pd.to_numeric(
                endpoints[right], errors="coerce"
            )
            core[f"core::spread_{left}_{right}"] = spread
            for lag in (1, 4, 13):
                core[f"core::spread_{left}_{right}_d{lag}"] = spread.diff(lag)
    features = pd.concat([core, *blocks], axis=1).replace([np.inf, -np.inf], np.nan)
    required_features = {
        str(feature)
        for member in MEMBERS
        for feature in member["features"]
    }
    missing = sorted(required_features.difference(features.columns))
    if missing:
        raise ValueError(f"frozen feature manifest cannot be built: {missing}")
    return features.astype(np.float32)


def eligible_labels(endpoints: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    current = pd.to_numeric(endpoints[TARGET], errors="coerce")
    labels = np.sign(current.shift(-1) - current).to_numpy(dtype=float)
    target_dates = endpoints["date"].shift(-1)
    feature_date = endpoints.iloc[-1]["date"]
    eligible = np.flatnonzero(
        np.isfinite(labels)
        & (labels != 0)
        & target_dates.lt(feature_date).fillna(False).to_numpy()
    )
    return labels, eligible


def member_probability(
    member: dict[str, Any],
    features: pd.DataFrame,
    labels: np.ndarray,
    eligible: np.ndarray,
) -> float:
    train_window = int(member["train_window"])
    use = eligible[-train_window:] if train_window > 0 else eligible
    if len(use) < 40 or np.unique(labels[use]).size < 2:
        raise ValueError(
            f"insufficient history for {member['config_id']}: at least 40 two-class labels are required"
        )
    columns = tuple(str(column) for column in member["features"])
    x = features.loc[:, columns].to_numpy(dtype=np.float32)
    yy = labels[use].astype(int)
    family = str(member["family"])
    params = dict(member["params"])
    if family == "rule":
        column = min(int(params["rank"]), x.shape[1] - 1)
        values = x[use, column].astype(float)
        median = float(np.nanmedian(values))
        scale = float(np.nanstd(values))
        current = float(x[-1, column])
        if not math.isfinite(scale) or scale < 1e-12 or not math.isfinite(current):
            raise ValueError("rule member has unusable feature history")
        centered = np.nan_to_num(values - median, nan=0.0)
        correlation = (
            float(np.corrcoef(centered, yy.astype(float))[0, 1])
            if np.std(centered) > 0
            else 0.0
        )
        orientation = 1.0 if correlation >= 0 else -1.0
        z_value = float(np.clip(orientation * (current - median) / scale, -8.0, 8.0))
        return 1.0 / (1.0 + math.exp(-z_value))
    if family == "logistic":
        classifier = LogisticRegression(
            C=float(params["C"]),
            class_weight=params["class_weight"],
            max_iter=1000,
            solver="liblinear",
            random_state=RANDOM_STATE,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")),
             ("scale", StandardScaler()),
             ("model", classifier))
        )
    elif family == "hist_gb":
        classifier = HistGradientBoostingClassifier(
            **params,
            random_state=RANDOM_STATE,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "extra_trees":
        classifier = ExtraTreesClassifier(
            **params,
            class_weight="balanced",
            random_state=RANDOM_STATE,
            n_jobs=1,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "xgb":
        classifier = xgb.XGBClassifier(
            **params,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=RANDOM_STATE,
            n_jobs=1,
            reg_alpha=0.2,
            reg_lambda=2.0,
            tree_method="hist",
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "lgbm":
        classifier = lgb.LGBMClassifier(
            **params,
            objective="binary",
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
            random_state=RANDOM_STATE,
            n_jobs=1,
            subsample=0.85,
            colsample_bytree=0.85,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "knn":
        if len(use) < int(params["n_neighbors"]):
            raise ValueError(f"insufficient history for {member['config_id']} KNN neighbors")
        classifier = KNeighborsClassifier(**params, n_jobs=1)
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")),
             ("scale", StandardScaler()),
             ("model", classifier))
        )
    else:
        raise RuntimeError(f"unknown frozen member family: {family}")
    model.fit(x[use], (yy == 1).astype(int))
    probability = float(model.predict_proba(x[[-1]])[0, 1])
    if not math.isfinite(probability):
        raise RuntimeError(f"{member['config_id']} returned a non-finite probability")
    return probability


def _signal(probability: float, reject: float) -> int:
    return 0 if abs(probability - 0.5) < reject else (1 if probability >= 0.5 else -1)


def predict_direction(
    request: dict[str, str],
    data: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> int:
    calendar, daily, weekly = data
    endpoints, weekly_slice = cutoff_frames(request, calendar, daily, weekly)
    features = build_features(endpoints, weekly_slice)
    labels, eligible = eligible_labels(endpoints)
    member_signals = [
        _signal(
            member_probability(member, features, labels, eligible),
            float(member["reject"]),
        )
        for member in MEMBERS
    ]
    vote_sum = int(sum(member_signals))
    if abs(vote_sum) < QUORUM:
        current = float(endpoints[TARGET].iloc[-1])
        previous = float(endpoints[TARGET].iloc[-2])
        if not math.isfinite(current) or not math.isfinite(previous) or current == previous:
            return -1
        return 1 if current > previous else -1
    return 1 if vote_sum > 0 else -1


def generate(
    requests: Sequence[dict[str, str]],
    data: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in requests:
        request = validate_request(raw)
        direction = predict_direction(request, data)
        if type(direction) is not int or direction not in (-1, 0, 1):
            raise RuntimeError("model returned an invalid direction")
        output.append(
            {
                **{field: request[field] for field in OUTPUT_FIELDS[:-1]},
                "predicted_direction": direction,
            }
        )
    # Contract 1.0 batch self-check: the first, middle, and last rows are
    # deterministically recomputed through the independent single-cutoff path.
    check_indices = sorted({0, len(requests) // 2, len(requests) - 1})
    for index in check_indices:
        request = validate_request(requests[index])
        repeated = predict_direction(request, data)
        if repeated != output[index]["predicted_direction"]:
            raise RuntimeError(
                f"batch independent-cutoff self-check failed at request index {index}"
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
    warnings.filterwarnings("ignore", category=UserWarning)
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
