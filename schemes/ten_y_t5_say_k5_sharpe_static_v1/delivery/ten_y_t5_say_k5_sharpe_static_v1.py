#!/usr/bin/env python3
"""Frozen 10Y T+5 Blackbox V2 scheme (Contract 1.0)."""
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


RANDOM_STATE = 42
HORIZON = 5
MIN_TRAIN_ROWS = 100
DAILY_COLUMN_COUNT = 774
DAILY_HEADER_SHA256 = "e002207087e1e89e3d65013f38613f5561a784731068dcefc4600aa33ec43534"
REQUIRED_REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date", "predicted_direction",
)
DATE_FORMAT = "%Y-%m-%d"
SIX_DIGIT_KEY = re.compile(r"^\d{6}$")
YIELD_COLUMNS = ("TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
MARKET_FACTORS = ('DR007IBC', 'M0017153', 'USDCNH0C', 'SX5EDF0C', 'SH000300', 'IFCFE00C', 'S0031525', 'AUSHF00C', 'CUSHF01C', 'RBSHF01C', 'G0006352', 'G0006353', 'M0000005', 'M0000271', 'M0048486', 'G0003956', 'B2559386', 'G0003892', 'DRS00001', 'DRS00002')
SCHEME_ID = 'ten_y_t5_say_k5_sharpe_static_v1'
if Path(__file__).stem != SCHEME_ID:
    raise RuntimeError("script filename must equal the fixed scheme_id")
CONFIG = {'members': ('curve_momentum_5', 'ten_y_reversion_20', 'market_breadth_10', 'liquidity_trend_20'), 'lgbm': {'window': 252, 'n_estimators': 100, 'learning_rate': 0.025, 'num_leaves': 7, 'min_child_samples': 25, 'reg_alpha': 1.0, 'reg_lambda': 1.5, 'colsample_bytree': 0.8}}
class ContractError(ValueError):
    """A request or its allowed data snapshot does not satisfy Contract 1.0."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def safe_sign(values: Any) -> Any:
    result = np.sign(values)
    if isinstance(result, pd.Series):
        return result.replace(0, -1).fillna(-1).astype(np.int8)
    result = np.asarray(result)
    result[result == 0] = -1
    result[np.isnan(result)] = -1
    return result.astype(np.int8)


def validate_date(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError("%s must be a YYYY-MM-DD string" % field)
    try:
        return datetime.strptime(value, DATE_FORMAT)
    except ValueError as exc:
        raise ContractError("%s must be a valid YYYY-MM-DD date" % field) from exc


def validate_request(record: Dict[str, Any]) -> Dict[str, str]:
    if set(record) != set(REQUIRED_REQUEST_FIELDS):
        raise ContractError("request fields must exactly match Contract 1.0")
    value = {field: record[field] for field in REQUIRED_REQUEST_FIELDS}
    if not all(isinstance(item, str) for item in value.values()):
        raise ContractError("all request fields must be strings")
    if not value["request_id"].strip():
        raise ContractError("request_id must be non-empty")
    feature_date = validate_date(value["feature_date"], "feature_date")
    predict_date = validate_date(value["predict_date"], "predict_date")
    target_date = validate_date(value["target_date"], "target_date")
    daily_cutoff = validate_date(value["daily_cutoff_key"], "daily_cutoff_key")
    if not feature_date <= predict_date <= target_date or not feature_date < target_date:
        raise ContractError("request dates do not satisfy feature <= predict <= target")
    if daily_cutoff.strftime(DATE_FORMAT) != value["daily_cutoff_key"]:
        raise ContractError("daily_cutoff_key must be a normalized YYYY-MM-DD date")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not SIX_DIGIT_KEY.fullmatch(value[field]):
            raise ContractError("%s must be a six-digit string" % field)
    return value


def read_single_request(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            decoded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("cannot read request JSON") from exc
    if not isinstance(decoded, dict):
        raise ContractError("request JSON must be an object")
    return validate_request(decoded)


def read_batch_requests(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(REQUIRED_REQUEST_FIELDS):
                raise ContractError("request CSV fields must exactly match Contract 1.0")
            records = [validate_request(row) for row in reader]
    except OSError as exc:
        raise ContractError("cannot read request CSV") from exc
    if not 1 <= len(records) <= 100:
        raise ContractError("request CSV must contain one to 100 rows")
    request_ids = [record["request_id"] for record in records]
    if len(set(request_ids)) != len(request_ids):
        raise ContractError("request_id values must be unique within a batch")
    return records


def validate_daily_header(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
    except (OSError, StopIteration) as exc:
        raise ContractError("cannot read daily_output.csv header") from exc
    digest = hashlib.sha256("\x1f".join(header).encode("utf-8")).hexdigest()
    if len(header) != DAILY_COLUMN_COUNT or digest != DAILY_HEADER_SHA256:
        raise ContractError("daily_output.csv does not match data-bridge-v1 schema")


def read_daily_snapshot(data_dir: Path) -> pd.DataFrame:
    path = data_dir / "daily_output.csv"
    validate_daily_header(path)
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig")
        dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ContractError("daily_output.csv contains an invalid date column") from exc
    if frame.empty or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ContractError("daily_output.csv dates must be non-empty, unique, and ascending")
    required_columns = set(YIELD_COLUMNS + MARKET_FACTORS)
    if not required_columns.issubset(frame.columns):
        raise ContractError("daily_output.csv is missing required model columns")
    frame = frame.copy()
    frame["_date_key"] = dates.dt.strftime(DATE_FORMAT)
    return frame


def truncate_at_cutoff(frame: pd.DataFrame, cutoff_key: str) -> pd.DataFrame:
    matches = np.flatnonzero(frame["_date_key"].to_numpy() == cutoff_key)
    if len(matches) != 1:
        raise ContractError("daily_cutoff_key is not present exactly once in daily_output.csv")
    truncated = frame.iloc[: int(matches[0]) + 1].copy()
    if truncated.empty:
        raise ContractError("daily cutoff leaves no usable observations")
    return truncated


def build_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Ten-year curve and 20-market-factor signals, all cutoff-local."""
    ten_y = pd.to_numeric(frame["TB0YWI0C"], errors="coerce").ffill()
    seven_y = pd.to_numeric(frame["TB7YWI0C"], errors="coerce").ffill()
    five_y = pd.to_numeric(frame["TB5YWI0C"], errors="coerce").ffill()
    curve = ten_y - five_y
    market_moves = pd.DataFrame({
        column: pd.to_numeric(frame[column], errors="coerce").ffill().pct_change()
        for column in MARKET_FACTORS
    })
    market_breadth = market_moves.apply(safe_sign, axis=0).sum(axis=1)
    rates_spread = (ten_y - seven_y).diff()
    liquidity = pd.to_numeric(frame["DR007IBC"], errors="coerce").ffill().diff()
    risk = market_moves[["SH000300", "IFCFE00C", "USDCNH0C", "S0031525", "AUSHF00C", "G0006352"]].mean(axis=1)
    return pd.DataFrame({
        "curve_momentum_5": safe_sign(curve.diff().rolling(5).sum()),
        "ten_y_reversion_20": -safe_sign(ten_y.diff().rolling(20).sum()),
        "market_breadth_10": safe_sign(market_breadth.rolling(10).sum()),
        "liquidity_trend_20": -safe_sign(liquidity.rolling(20).sum()),
        "risk_switch_20": -safe_sign((risk - rates_spread).rolling(20).sum()),
    }, index=frame.index).fillna(0).astype(np.int8)


def build_features(frame: pd.DataFrame) -> np.ndarray:
    output = pd.DataFrame(index=frame.index)
    for column in YIELD_COLUMNS + MARKET_FACTORS:
        series = pd.to_numeric(frame[column], errors="coerce").ffill()
        for lag in (1, 5, 10, 20):
            output["%s_ret%s" % (column, lag)] = series.pct_change(lag)
    return output.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=np.float32)


def static_vote(signals: pd.DataFrame, index: int) -> int:
    values = signals.loc[:, CONFIG["members"]].iloc[index].to_numpy(dtype=np.int8)
    direction = int(np.sign(values.sum()))
    return direction if direction else int(values[0])


def machine_direction(features: np.ndarray, labels: np.ndarray, feature_index: int, params: Dict[str, float]) -> int:
    train_end = feature_index - HORIZON
    train_start = max(0, train_end - int(params["window"]))
    if train_end - train_start < MIN_TRAIN_ROWS:
        raise ContractError("daily cutoff leaves insufficient history for the fixed model")
    x_train, y_train = features[train_start:train_end], labels[train_start:train_end]
    if len(np.unique(y_train)) < 2:
        raise ContractError("daily cutoff leaves a single-class training window")
    model = LGBMClassifier(
        objective="binary", n_estimators=int(params["n_estimators"]),
        learning_rate=float(params["learning_rate"]), num_leaves=int(params["num_leaves"]),
        min_child_samples=int(params["min_child_samples"]), reg_alpha=float(params["reg_alpha"]),
        reg_lambda=float(params["reg_lambda"]), colsample_bytree=float(params["colsample_bytree"]),
        random_state=RANDOM_STATE, n_jobs=1, verbosity=-1,
    )
    model.fit(x_train, (y_train > 0).astype(np.int8))
    probability = float(model.predict_proba(features[feature_index:feature_index + 1])[:, 1][0])
    return 1 if probability >= 0.5 else -1


def predict_direction(truncated: pd.DataFrame) -> int:
    if len(truncated) <= HORIZON:
        raise ContractError("daily cutoff leaves insufficient observations")
    close = pd.to_numeric(truncated["TB0YWI0C"], errors="coerce").to_numpy(dtype=np.float64)
    labels = safe_sign(close[HORIZON:] / close[:-HORIZON] - 1.0)
    feature_index = len(truncated) - 1
    signals = build_signals(truncated)
    static = static_vote(signals, feature_index)
    params = CONFIG["lgbm"]
    if params is None:
        return static
    machine = machine_direction(build_features(truncated), labels, feature_index, params)
    return static if static == machine else int(signals[CONFIG["members"][0]].iat[feature_index])


def make_result(record: Dict[str, str], direction: int) -> Dict[str, Any]:
    return {
        "request_id": record["request_id"], "predict_date": record["predict_date"],
        "feature_date": record["feature_date"], "target_date": record["target_date"],
        "predicted_direction": int(direction),
    }


def predict_all(records: Sequence[Dict[str, str]], frame: pd.DataFrame) -> List[Dict[str, Any]]:
    direction_cache: Dict[str, int] = {}
    results: List[Dict[str, Any]] = []
    for record in records:
        cutoff = record["daily_cutoff_key"]
        if cutoff not in direction_cache:
            direction_cache[cutoff] = predict_direction(truncate_at_cutoff(frame, cutoff))
        results.append(make_result(record, direction_cache[cutoff]))
    return results


def atomic_write_json(output: Path, result: Dict[str, Any]) -> None:
    atomic_write(output, lambda handle: json.dump(result, handle, ensure_ascii=False, separators=(",", ":")))


def atomic_write_csv(output: Path, results: Sequence[Dict[str, Any]]) -> None:
    def write(handle: Any) -> None:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)
    atomic_write(output, write)


def atomic_write(output: Path, write_content: Any) -> None:
    if not output.parent.is_dir():
        raise ContractError("output directory does not exist")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=str(output.parent), delete=False) as handle:
            temporary_path = Path(handle.name)
            write_content(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(output))
    except Exception:
        if temporary_path is not None:
            try:
                if temporary_path.exists():
                    temporary_path.unlink()
            except OSError:
                pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="10Y T+5 Blackbox V2 prediction scheme")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, request_arg in (("predict", "--request"), ("backtest", "--requests")):
        command = subparsers.add_parser(name)
        command.add_argument(request_arg, required=True, type=Path)
        command.add_argument("--data-dir", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    return parser


def run(arguments: argparse.Namespace) -> None:
    records = [read_single_request(arguments.request)] if arguments.command == "predict" else read_batch_requests(arguments.requests)
    frame = read_daily_snapshot(arguments.data_dir)
    results = predict_all(records, frame)
    if arguments.command == "predict":
        atomic_write_json(arguments.output, results[0])
    else:
        atomic_write_csv(arguments.output, results)
    logging.info("completed %s for %d request(s)", arguments.command, len(results))


def main() -> int:
    configure_logging()
    try:
        arguments = build_parser().parse_args()
        run(arguments)
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected execution failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
