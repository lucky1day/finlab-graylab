#!/usr/bin/env python3
"""Blackbox V2 Contract 1.0: 3Y T+1 activity dynamic vote."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

RANDOM_STATE = 20260729
LOOKBACK_MONTHS = 2
TOP_K = 1
TARGET_COLUMN = "TB3YWI0C"
DAILY_SCHEMA_COLUMNS = 774
DAILY_SCHEMA_HASH = "e002207087e1e89e3d65013f38613f5561a784731068dcefc4600aa33ec43534"
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
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SIX_DIGIT_PATTERN = re.compile(r"^\d{6}$")
MODEL_SPECS = (
    (40, 3, 0.03),
    (120, 7, 0.08),
    (40, 7, 0.05),
)
EXPERT_SPECS = (
    (120, 0.40, 0, 1),
    (252, 0.20, 0, 1),
    (120, 0.40, 0, 2),
    (60, 0.20, 0, 1),
    (60, 0.40, 0, 1),
    (60, 0.40, 0, 2),
)
RANKED_FEATURES = (
    "TB0YWIPC_level", "S0059748_d1", "TB0YWI0C_d1", "RSI710YD_d1",
    "S0059747_d1", "RSI10Y0D_d1", "M1004267_d1", "TFCFE002_d1",
    "M1000047_d1", "S0059745_d1", "M1004265_d1", "S0059749_d1",
    "S0059751_d1", "S0059750_d1", "S0059744_d1", "TFCFE003_d1",
    "TFCFE001_level", "S0059746_d1", "RSI10Y0D_level", "RSI710YD_level",
    "S0059752_d1", "BIASB5YD_level", "BIAS10YD_d1", "M1004264_d1",
    "M1004263_d1", "MACDC5YD_d1", "M1007670_d1", "M1000051_d1",
    "TFCFE001_d1", "M1004271_d1", "EMA120YD_d1", "M1004274_d1",
    "BIAS10YD_level", "BBI10Y0D_d1", "EMA260YD_d1", "M1007675_d1",
    "M0048486_d1", "MACDC0YD_d1", "S0059745_d5", "S0059833_d1",
    "M1007666_d1", "S0059748_d5", "M1004269_d1", "TB0YWIPC_d1",
    "S0059836_d1", "N0909001_d5", "N0809001_d5", "M1001654_d1",
    "90000012_d1", "N0209001_d5", "S0059775_d5", "N1307001_d5",
    "N1809001_d5", "N0808001_d5", "M1001646_d1", "N0709001_d5",
    "N1409001_d5", "N0907001_d5", "N0109001_d5", "N1909001_d5",
    "M1004272_d1", "M1004552_d5", "N0609001_d5", "N1407001_d5",
    "M1004263_d5", "N1309001_d5", "N0708001_d5", "N0108001_d5",
    "S0059739_d5", "S0059774_d5", "N0105001_d5", "SEMAC002_d1",
    "S0059746_d5", "N0107001_d5", "M1004265_d5", "M1004273_d1",
    "N1408001_d5", "M1004274_d5", "N1908001_d5", "N1907001_d5",
    "M1004273_d5", "DIF10Y0D_d1", "S0059765_d5", "N0908001_d5",
    "N0608001_d5", "N1308001_d5", "N0605001_d5", "N0110001_d5",
    "N1805001_d5", "N0807001_d5", "S0059747_d5", "S0059763_d5",
    "N0208001_d5", "S0167430_d5", "N1105001_d5", "90000008_d1",
    "BBIB5Y0D_level", "BBIB10YD_level", "S0059773_d5", "N0707001_d5",
    "N1808001_d5", "M0048432_d5", "TB0YWI0C_d5", "N0210001_d5",
    "MACDD5YD_level", "S0059764_d5", "N0607001_d5", "N0710001_d5",
    "BIASB0YD_level", "N1410001_d5", "M1000170_d1", "N0910001_d5",
    "N1810001_d5", "N0610001_d5", "S0059762_d5", "N2607001_d5",
    "S0059744_d5", "S0059838_d1", "S0059772_d5", "M1007670_d5",
)


class ContractError(ValueError):
    """Input or data snapshot does not satisfy Contract 1.0."""


def _raw_feature_name(feature_name: str) -> str:
    return feature_name.rsplit("_", 1)[0]


def _validate_daily_header(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
    except (OSError, StopIteration) as exc:
        raise ContractError("cannot read daily_output.csv header") from exc
    digest = hashlib.sha256("\x1f".join(header).encode("utf-8")).hexdigest()
    if len(header) != DAILY_SCHEMA_COLUMNS or digest != DAILY_SCHEMA_HASH:
        raise ContractError("daily_output.csv does not match data-bridge-v1 schema")


def _read_daily(data_dir: Path) -> pd.DataFrame:
    path = data_dir / "daily_output.csv"
    _validate_daily_header(path)
    try:
        frame = pd.read_csv(path, usecols=lambda name: name == "date" or name in {
            TARGET_COLUMN, *(_raw_feature_name(name) for name in RANKED_FEATURES)
        })
    except (OSError, ValueError) as exc:
        raise ContractError("cannot read required daily_output.csv columns") from exc
    frame.columns = [str(column).strip().lstrip("\ufeff") for column in frame.columns]
    expected = {"date", TARGET_COLUMN, *(_raw_feature_name(name) for name in RANKED_FEATURES)}
    if set(frame.columns) != expected:
        raise ContractError("daily_output.csv is missing fixed scheme columns")
    try:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ContractError("daily_output.csv contains invalid dates") from exc
    if frame.empty or frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ContractError("daily date keys must be non-empty, unique, and ascending")
    for column in frame.columns:
        if column != "date":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


class PredictionEngine:
    def __init__(self, daily: pd.DataFrame) -> None:
        self.dates = pd.DatetimeIndex(daily["date"])
        raw_names = sorted({_raw_feature_name(name) for name in RANKED_FEATURES})
        raw = daily[raw_names].ffill()
        feature_columns = []
        for feature_name in RANKED_FEATURES:
            raw_name, transform = feature_name.rsplit("_", 1)
            series = raw[raw_name]
            if transform == "level":
                feature_columns.append(series)
            elif transform == "d1":
                feature_columns.append(series.diff())
            elif transform == "d5":
                feature_columns.append(series.diff(5))
            else:
                raise ContractError("unsupported fixed feature transform")
        self.features = (
            pd.concat(feature_columns, axis=1)
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
            .to_numpy(dtype=np.float32)
        )
        target = daily[TARGET_COLUMN].ffill().to_numpy(dtype=np.float64)
        self.truth = np.full(len(target), np.nan, dtype=np.float64)
        self.truth[:-1] = np.sign(np.diff(target))
        self.activity = np.full(len(target), np.nan, dtype=np.float64)
        self.activity[1:] = np.abs(np.diff(target))
        self.week_period = self.dates.to_period("W-SUN")
        self.month_period = self.dates.to_period("M")
        self.date_position = {date.strftime("%Y-%m-%d"): index for index, date in enumerate(self.dates)}
        self.model_cache: Dict[Tuple[int, int], Any] = {}

    def _fit_week_model(self, model_number: int, row_index: int) -> Any:
        week = self.week_period[row_index]
        first = int(np.flatnonzero(self.week_period == week)[0])
        key = (model_number, first)
        if key in self.model_cache:
            return self.model_cache[key]
        feature_limit, leaves, _ = MODEL_SPECS[model_number]
        history = np.flatnonzero(
            (np.arange(len(self.dates)) < first)
            & np.isfinite(self.truth)
            & (self.truth != 0)
        )
        if len(history) > 1008:
            history = history[-1008:]
        if len(history) < 160 or len(np.unique(self.truth[history])) < 2:
            self.model_cache[key] = None
            return None
        model = LGBMClassifier(
            objective="binary",
            metric="binary_logloss",
            n_estimators=100,
            learning_rate=0.03,
            num_leaves=leaves,
            min_child_samples=40,
            max_depth=-1,
            colsample_bytree=0.85,
            reg_alpha=0.5,
            reg_lambda=2.0,
            class_weight="balanced",
            random_state=RANDOM_STATE,
            n_jobs=1,
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
        )
        model.fit(
            self.features[history, :feature_limit],
            (self.truth[history] > 0).astype(np.int8),
        )
        self.model_cache[key] = model
        return model

    def _model_direction(self, model_number: int, row_index: int) -> int:
        model = self._fit_week_model(model_number, row_index)
        if model is None:
            return 0
        feature_limit, _, margin = MODEL_SPECS[model_number]
        probability = float(
            model.predict_proba(self.features[row_index:row_index + 1, :feature_limit])[0, 1]
        )
        if probability >= 0.5 + margin:
            return 1
        if probability <= 0.5 - margin:
            return -1
        return 0

    def _gate(self, row_index: int, window: int, quantile: float) -> bool:
        history = self.activity[:row_index]
        history = history[np.isfinite(history)]
        if len(history) > window:
            history = history[-window:]
        return (
            len(history) >= 40
            and np.isfinite(self.activity[row_index])
            and self.activity[row_index] >= np.quantile(history, quantile)
        )

    def _expert_signals(self, row_index: int) -> np.ndarray:
        model_signals = [self._model_direction(index, row_index) for index in range(3)]
        output = np.zeros(len(EXPERT_SPECS), dtype=np.int8)
        for index, (window, quantile, high, low) in enumerate(EXPERT_SPECS):
            selected = high if self._gate(row_index, window, quantile) else low
            output[index] = model_signals[selected]
        return output

    def predict(self, record: Dict[str, str]) -> int:
        cutoff_index = self.date_position.get(record["daily_cutoff_key"])
        feature_index = self.date_position.get(record["feature_date"])
        if cutoff_index is None:
            raise ContractError("daily cutoff key must occur exactly once")
        if feature_index is None or feature_index > cutoff_index:
            raise ContractError("feature_date must be present within the daily cutoff")
        current_month = self.month_period[feature_index]
        prior_months = sorted(set(self.month_period[:feature_index]))
        prior_months = [month for month in prior_months if month < current_month][-LOOKBACK_MONTHS:]
        history_indices = np.flatnonzero(
            np.isin(self.month_period, prior_months)
            & (np.arange(len(self.dates)) <= feature_index)
            & np.isfinite(self.truth)
            & (self.truth != 0)
        )
        accuracy = np.full(len(EXPERT_SPECS), 0.5, dtype=np.float64)
        if len(history_indices):
            matrix = np.vstack([self._expert_signals(index) for index in history_indices])
            truth = self.truth[history_indices]
            for expert in range(len(EXPERT_SPECS)):
                traded = matrix[:, expert] != 0
                if int(traded.sum()) >= 8:
                    accuracy[expert] = float(
                        (matrix[traded, expert] == truth[traded]).mean()
                    )
        chosen = np.argsort(-accuracy, kind="stable")[:TOP_K]
        current = self._expert_signals(feature_index)[chosen]
        score = float(current.mean())
        return 1 if score > 0.0 else -1 if score < 0.0 else 0


def _validate_date(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ContractError("%s must be a normalized YYYY-MM-DD string" % field)
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError("%s must be a valid date" % field) from exc


def validate_request(record: Dict[str, Any]) -> Dict[str, str]:
    if set(record) != set(REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    values = {field: record[field] for field in REQUEST_FIELDS}
    if not all(isinstance(value, str) for value in values.values()):
        raise ContractError("all request fields must be strings")
    if not values["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature_date = _validate_date(values["feature_date"], "feature_date")
    predict_date = _validate_date(values["predict_date"], "predict_date")
    target_date = _validate_date(values["target_date"], "target_date")
    _validate_date(values["daily_cutoff_key"], "daily_cutoff_key")
    if not feature_date <= predict_date <= target_date or not feature_date < target_date:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not SIX_DIGIT_PATTERN.fullmatch(values[field]):
            raise ContractError("%s must be a six-digit string" % field)
    return values


def read_requests(arguments: argparse.Namespace) -> List[Dict[str, str]]:
    if arguments.command == "predict":
        try:
            with arguments.request.open("r", encoding="utf-8-sig") as handle:
                decoded = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("cannot read request JSON") from exc
        if not isinstance(decoded, dict):
            raise ContractError("request JSON must be an object")
        return [validate_request(decoded)]
    try:
        with arguments.requests.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(REQUEST_FIELDS):
                raise ContractError("request CSV fields must exactly match Contract 1.0")
            records = [validate_request(record) for record in reader]
    except OSError as exc:
        raise ContractError("cannot read request CSV") from exc
    if not 1 <= len(records) <= 100:
        raise ContractError("request CSV must contain one to 100 rows")
    request_ids = [record["request_id"] for record in records]
    if len(set(request_ids)) != len(request_ids):
        raise ContractError("request_id values must be unique within a batch")
    return records


def make_result(record: Dict[str, str], direction: int) -> Dict[str, Any]:
    return {
        "request_id": record["request_id"],
        "predict_date": record["predict_date"],
        "feature_date": record["feature_date"],
        "target_date": record["target_date"],
        "predicted_direction": int(direction),
    }


def atomic_write(output: Path, writer: Any) -> None:
    if not output.parent.is_dir():
        raise ContractError("output directory does not exist")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=str(output.parent), delete=False
        ) as handle:
            temporary = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(output))
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def write_results(command: str, output: Path, results: Sequence[Dict[str, Any]]) -> None:
    if command == "predict":
        atomic_write(
            output,
            lambda handle: json.dump(
                results[0], handle, ensure_ascii=False, separators=(",", ":")
            ),
        )
        return

    def write_csv(handle: Any) -> None:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)

    atomic_write(output, write_csv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3Y T+1 Blackbox V2 prediction scheme")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, request_argument in (("predict", "--request"), ("backtest", "--requests")):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(request_argument, required=True, type=Path)
        subparser.add_argument("--data-dir", required=True, type=Path)
        subparser.add_argument("--output", required=True, type=Path)
    return parser


def run(arguments: argparse.Namespace) -> None:
    records = read_requests(arguments)
    engine = PredictionEngine(_read_daily(arguments.data_dir))
    cache: Dict[Tuple[str, str], int] = {}
    results = []
    for record in records:
        key = (record["feature_date"], record["daily_cutoff_key"])
        if key not in cache:
            cache[key] = engine.predict(record)
        results.append(make_result(record, cache[key]))
    write_results(arguments.command, arguments.output, results)
    logging.info("completed %s for %d request(s)", arguments.command, len(results))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    np.random.seed(RANDOM_STATE)
    try:
        run(build_parser().parse_args())
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected execution failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
