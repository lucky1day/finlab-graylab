from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from shared.scheme_config_schema import ALLOWED_TENORS, SCHEME_ID_PATTERN


TASK_COMBINATIONS = {
    "T+1": (1, "target_date_yield_vs_feature_date_yield", "daily"),
    "T+5": (5, "target_date_yield_vs_feature_date_yield", "daily"),
    "weekly_point": (1, "target_week_end_yield_vs_feature_week_end_yield", "weekly"),
    "weekly_average": (1, "target_week_average_yield_vs_feature_week_average_yield", "weekly"),
    "monthly": (1, "target_month_observation_yield_vs_feature_month_observation_yield", "monthly"),
}
REQUIRED_METADATA_FIELDS = {
    "schema_version",
    "scheme_id",
    "name",
    "algorithm_version",
    "target_tenor",
    "task_type",
    "horizon",
    "target_rule",
}
OPTIONAL_METADATA_FIELDS = {"description"}
MAX_DESCRIPTION_LENGTH = 300
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


@dataclass(frozen=True)
class BlackboxMetadata:
    schema_version: str
    scheme_id: str
    name: str
    algorithm_version: str
    target_tenor: str
    task_type: str
    horizon: int
    target_rule: str
    frequency: str
    description: str | None = None


@dataclass(frozen=True)
class BlackboxRequest:
    request_id: str
    predict_date: str
    feature_date: str
    target_date: str
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str


@dataclass(frozen=True)
class BlackboxResult:
    request_id: str
    predict_date: str
    feature_date: str
    target_date: str
    predicted_direction: int


def load_metadata(path: str | Path) -> BlackboxMetadata:
    """Strictly parse one Blackbox V2 metadata document."""
    metadata_path = Path(path)
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Blackbox V2 metadata {metadata_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Blackbox V2 metadata must be a JSON object")
    allowed_fields = REQUIRED_METADATA_FIELDS | OPTIONAL_METADATA_FIELDS
    if not REQUIRED_METADATA_FIELDS.issubset(raw) or not set(raw).issubset(allowed_fields):
        missing = sorted(REQUIRED_METADATA_FIELDS - set(raw))
        extra = sorted(set(raw) - allowed_fields)
        raise ValueError(f"Blackbox V2 metadata fields mismatch: missing={missing}, extra={extra}")

    schema_version = _non_empty_string(raw, "schema_version")
    if schema_version != "1.0":
        raise ValueError("schema_version must be '1.0'")
    scheme_id = _non_empty_string(raw, "scheme_id")
    if not SCHEME_ID_PATTERN.fullmatch(scheme_id):
        raise ValueError("scheme_id must match ^[a-z][a-z0-9_]*$")
    name = _non_empty_string(raw, "name")
    algorithm_version = _non_empty_string(raw, "algorithm_version")
    target_tenor = _non_empty_string(raw, "target_tenor")
    if target_tenor not in ALLOWED_TENORS:
        raise ValueError(f"unsupported target_tenor: {target_tenor}")
    task_type = _non_empty_string(raw, "task_type")
    combination = TASK_COMBINATIONS.get(task_type)
    if combination is None:
        raise ValueError(f"unsupported task_type: {task_type}")
    horizon = raw["horizon"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    target_rule = _non_empty_string(raw, "target_rule")
    description = _optional_description(raw)
    expected_horizon, expected_rule, frequency = combination
    if (horizon, target_rule) != (expected_horizon, expected_rule):
        raise ValueError(
            "task_type, horizon and target_rule must use one fixed combination: "
            f"task_type={task_type}, expected=({expected_horizon}, {expected_rule})"
        )
    return BlackboxMetadata(
        schema_version=schema_version,
        scheme_id=scheme_id,
        name=name,
        algorithm_version=algorithm_version,
        target_tenor=target_tenor,
        task_type=task_type,
        horizon=horizon,
        target_rule=target_rule,
        frequency=frequency,
        description=description,
    )


def _optional_description(raw: dict[str, Any]) -> str | None:
    if "description" not in raw:
        return None
    description = _non_empty_string(raw, "description")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"description must not exceed {MAX_DESCRIPTION_LENGTH} characters"
        )
    if any(marker in description for marker in ("\n", "\r", "<", ">")):
        raise ValueError("description must be single-paragraph plain text")
    return description


def load_request(path: str | Path) -> BlackboxRequest:
    request_path = Path(path)
    try:
        payload = request_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"invalid Request JSON {request_path}: {exc}") from exc
    return load_request_bytes(payload, source=str(request_path))


def load_request_bytes(
    payload: bytes,
    *,
    source: str = "<bytes>",
) -> BlackboxRequest:
    """从调用方已经稳定读取的 UTF-8 bytes 严格解析单条 Request。"""
    if not isinstance(payload, bytes):
        raise ValueError("Request payload must be bytes")
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Request JSON {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Request must be a JSON object")
    return request_from_mapping(raw)


def load_requests(path: str | Path) -> list[BlackboxRequest]:
    request_path = Path(path)
    try:
        with request_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = tuple(reader.fieldnames or ())
            if set(fieldnames) != set(REQUEST_FIELDS) or len(fieldnames) != len(REQUEST_FIELDS):
                raise ValueError(f"Request fields mismatch: expected={list(REQUEST_FIELDS)}, got={list(fieldnames)}")
            requests = [request_from_mapping(dict(row)) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"invalid Request CSV {request_path}: {exc}") from exc
    if not requests:
        raise ValueError("Request batch must contain at least one row")
    ids = [item.request_id for item in requests]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate request_id values: {duplicates}")
    return requests


def request_from_mapping(raw: dict[str, Any]) -> BlackboxRequest:
    if set(raw) != set(REQUEST_FIELDS) or len(raw) != len(REQUEST_FIELDS):
        missing = sorted(set(REQUEST_FIELDS) - set(raw))
        extra = sorted(set(raw) - set(REQUEST_FIELDS))
        raise ValueError(f"Request fields mismatch: missing={missing}, extra={extra}")
    request_id = _non_empty_string(raw, "request_id")
    predict_date = _iso_date(raw, "predict_date")
    feature_date = _iso_date(raw, "feature_date")
    target_date = _iso_date(raw, "target_date")
    if not feature_date <= predict_date <= target_date or not feature_date < target_date:
        raise ValueError("Request dates must satisfy feature_date <= predict_date <= target_date and feature_date < target_date")
    daily_cutoff_key = _iso_date(raw, "daily_cutoff_key")
    weekly_cutoff_key = _six_digit_key(raw, "weekly_cutoff_key")
    monthly_cutoff_key = _six_digit_key(raw, "monthly_cutoff_key")
    return BlackboxRequest(
        request_id=request_id,
        predict_date=predict_date,
        feature_date=feature_date,
        target_date=target_date,
        daily_cutoff_key=daily_cutoff_key,
        weekly_cutoff_key=weekly_cutoff_key,
        monthly_cutoff_key=monthly_cutoff_key,
    )


def load_prediction_result(path: str | Path, request: BlackboxRequest) -> BlackboxResult:
    result_path = Path(path)
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Result JSON {result_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Result must be a JSON object")
    result = result_from_mapping(raw)
    _require_result_echo(result, request)
    return result


def load_backtest_results(path: str | Path, requests: list[BlackboxRequest]) -> list[BlackboxResult]:
    result_path = Path(path)
    try:
        with result_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = tuple(reader.fieldnames or ())
            if set(fieldnames) != set(RESULT_FIELDS) or len(fieldnames) != len(RESULT_FIELDS):
                raise ValueError(f"Result fields mismatch: expected={list(RESULT_FIELDS)}, got={list(fieldnames)}")
            results = [_result_from_csv_mapping(dict(row)) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"invalid Result CSV {result_path}: {exc}") from exc
    if len(results) != len(requests):
        raise ValueError(f"Result row count mismatch: expected={len(requests)}, got={len(results)}")
    for index, (result, request) in enumerate(zip(results, requests, strict=True)):
        try:
            _require_result_echo(result, request)
        except ValueError as exc:
            raise ValueError(f"Result order or echo mismatch at row {index}: {exc}") from exc
    return results


def result_from_mapping(raw: dict[str, Any]) -> BlackboxResult:
    return _result_from_mapping(raw, _parse_json_direction)


def _result_from_csv_mapping(raw: dict[str, Any]) -> BlackboxResult:
    return _result_from_mapping(raw, _parse_csv_direction)


def _result_from_mapping(
    raw: dict[str, Any],
    parse_direction: Callable[[Any], int],
) -> BlackboxResult:
    if set(raw) != set(RESULT_FIELDS) or len(raw) != len(RESULT_FIELDS):
        missing = sorted(set(RESULT_FIELDS) - set(raw))
        extra = sorted(set(raw) - set(RESULT_FIELDS))
        raise ValueError(f"Result fields mismatch: missing={missing}, extra={extra}")
    direction = parse_direction(raw["predicted_direction"])
    return BlackboxResult(
        request_id=_non_empty_string(raw, "request_id"),
        predict_date=_iso_date(raw, "predict_date"),
        feature_date=_iso_date(raw, "feature_date"),
        target_date=_iso_date(raw, "target_date"),
        predicted_direction=direction,
    )


def _parse_json_direction(value: Any) -> int:
    if type(value) is not int or value not in {-1, 0, 1}:
        raise ValueError("predicted_direction must be integer -1, 0 or 1")
    return value


def _parse_csv_direction(value: Any) -> int:
    if value not in ("-1", "0", "1"):
        raise ValueError("predicted_direction must be integer -1, 0 or 1")
    return int(value)


def _require_result_echo(result: BlackboxResult, request: BlackboxRequest) -> None:
    expected = (request.request_id, request.predict_date, request.feature_date, request.target_date)
    actual = (result.request_id, result.predict_date, result.feature_date, result.target_date)
    if actual != expected:
        raise ValueError(f"Result must echo Request fields: expected={expected}, got={actual}")


def _non_empty_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _iso_date(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use canonical YYYY-MM-DD")
    return value


def _six_digit_key(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value):
        raise ValueError(f"{field} must be a six-digit string")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
