from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from shared.platform_dates import LIVE_START_DATE
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import TASK_COMBINATIONS


# 运行证据的取数边界独立于 Dashboard 展示分区。
LIVE_RUN_EVIDENCE_START_DATE = LIVE_START_DATE
MAX_RESPONSE_BYTES = 1_500_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    registry_rows: tuple[dict[str, Any], ...]
    prediction_rows: tuple[dict[str, Any], ...]
    actual_rows: tuple[dict[str, Any], ...]
    live_runs: tuple[dict[str, Any], ...]
    backtest_runs: tuple[dict[str, Any], ...]
    calendar_rows: tuple[dict[str, Any], ...]
    digest: str
    backtest_prediction_rows: tuple[dict[str, Any], ...] = ()
    version_rows: tuple[dict[str, Any], ...] = ()
    input_artifact_rows: tuple[dict[str, Any], ...] = ()
    actual_watermarks: tuple[dict[str, Any], ...] = ()


class DataConsistencyError(ValueError):
    """表示数据库事实或公开表示违反数据一致性合同。"""


class DataConsistencyTimeout(DataConsistencyError):
    """表示独立对账已经耗尽本次调用的总时间预算。"""


class DataConsistencyAuthorityUnavailable(DataConsistencyError):
    """表示无法从冻结事实建立权威预期，Gate 必须 BLOCKED。"""


def _product_business_key(row: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(row["scheme_id"]),
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="target_date"),
    )


def _product_reference_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="target_date"),
    )


def _backtest_source_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="source target_date"),
    )


def _json_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DataConsistencyError(f"{field} is invalid JSON") from exc
        if isinstance(loaded, Mapping):
            return loaded
    raise DataConsistencyError(f"{field} must be an object")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_snapshot_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"snapshot-[0-9a-f]{24}", value
    ) is not None


def _canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _remaining_budget(
    deadline: float,
    monotonic: Callable[[], float],
) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise DataConsistencyTimeout(
            "data_consistency_timeout: total gate budget exhausted"
        )
    return remaining


def _actual_selector(task_type: str) -> tuple[str, str]:
    expected = TASK_COMBINATIONS.get(task_type)
    if expected is None:
        raise DataConsistencyError(f"unsupported task_type: {task_type!r}")
    target_rule = expected[1]
    if task_type == "T+1":
        return "daily_1d", target_rule
    if task_type == "T+5":
        return "daily_5d", target_rule
    if task_type == "weekly_point":
        return "weekly", WEEKLY_TARGET_RULE
    if task_type == "weekly_average":
        return "weekly", WEEKLY_AVERAGE_TARGET_RULE
    if task_type == "monthly":
        return "monthly", MONTHLY_TARGET_RULE
    return "period_average", target_rule


def _clock_display_date(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise ValueError("data consistency clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("data consistency clock must return an aware datetime")
    return value.astimezone(SHANGHAI_TIMEZONE).date().isoformat()


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DataConsistencyError(f"{field} must be a non-empty canonical string")
    return value


def _iso_date(value: Any, *, field: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise DataConsistencyError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataConsistencyError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise DataConsistencyError(f"{field} must be a canonical ISO date")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DataConsistencyError(f"{field} must be a positive integer")
    return value


def _direction(
    value: Any,
    *,
    field: str,
    allow_none: bool = False,
) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in {-1, 0, 1}:
        raise DataConsistencyError(f"{field} must be one of -1, 0, 1")
    return value
