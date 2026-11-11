from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from shared.legacy_prediction_migration import LegacyPredictionMigration
from shared.legacy_backtest_lineage import LegacyBacktestLineage
from shared.platform_dates import LIVE_START_DATE
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import TASK_COMBINATIONS


HISTORY_START_DATE = "2025-01-01"
# 运行证据的取数边界独立于 Dashboard 展示分区。
LIVE_RUN_EVIDENCE_START_DATE = LIVE_START_DATE
LIVE_FEATURE_START_DATE = LIVE_START_DATE
MAX_RESPONSE_BYTES = 1_500_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MONTHLY_ROW_FIELDS = (
    "month",
    "source",
    "samples",
    "metric_samples",
    "correct",
    "predicted_up",
    "predicted_down",
    "predicted_flat",
    "actual_up",
    "actual_down",
    "actual_flat",
    "up_true_positive",
    "down_true_positive",
)
DETAIL_ROW_FIELDS = (
    "source",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
    "actual_direction",
)


@dataclass(frozen=True, slots=True)
class ConsistencyFact:
    scheme_id: str
    target_tenor: str
    horizon: int
    task_type: str
    predict_date: str
    feature_date: str
    target_date: str
    predicted_direction: int
    actual_direction: int | None


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


@dataclass(frozen=True, slots=True)
class _LegacyMigrationMatch:
    entry: LegacyPredictionMigration
    backtest_run_id: int
    mismatch_business_keys: frozenset[tuple[str, str, int, str]]


@dataclass(frozen=True, slots=True)
class _LegacyBacktestLineageMatch:
    entry: LegacyBacktestLineage
    backtest_run_id: int


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


def _source_registry_scope_status(
    run: Mapping[str, Any],
    *,
    target_tenor: str,
    horizon: int,
) -> str | None:
    """返回历史来源精确 tenor/horizon Registry 状态。"""
    scopes = run.get("source_registry_scopes")
    if isinstance(scopes, (list, tuple)):
        matches = {
            str(item.get("status"))
            for item in scopes
            if isinstance(item, Mapping)
            and str(item.get("target_tenor")) == target_tenor
            and int(item.get("horizon") or 0) == horizon
        }
        if len(matches) == 1:
            return next(iter(matches))
    return None


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


def _display_month(fact: ConsistencyFact) -> str:
    return fact.feature_date[:7]


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


def _source(value: str) -> str:
    return "live" if date.fromisoformat(value) >= LIVE_FEATURE_START_DATE else "backtest"


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


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


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
