from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Iterable, Mapping


DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v1"
ROW_FIELDS = (
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction",
)
VALID_TASK_TYPES = {"T+1", "T+5", "weekly_point", "weekly_average", "monthly"}
VALID_LIVE_PREDICTION_PHASES = {"gray_live", "scheduled_live"}


class DashboardDataError(RuntimeError):
    """展示快照存在冲突或结构错误。"""


def choose_live_prediction_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    display_until: Any,
) -> list[Mapping[str, Any]]:
    """按旧 API 规则选择 canonical 实盘预测，再过滤未来发出日。"""
    latest_by_point: dict[tuple[Any, Any, Any, str], Mapping[str, Any]] = {}
    for row in rows:
        point_date = _required_iso_date(row.get("target_date"), field="target_date")
        key = (
            row.get("scheme_id"),
            row.get("target_tenor"),
            row.get("horizon"),
            point_date,
        )
        current = latest_by_point.get(key)
        if current is None or _is_better_prediction_for_point(row, current):
            latest_by_point[key] = row

    selected = sorted(
        latest_by_point.values(),
        key=lambda row: (
            _required_iso_date(row.get("target_date"), field="target_date"),
            _optional_iso_date(row.get("predict_date")) or "",
            str(row.get("target_tenor") or ""),
        ),
    )
    last_display_date = _optional_iso_date(display_until)
    return [
        row
        for row in selected
        if not (
            (predict_date := _optional_iso_date(row.get("predict_date")))
            and last_display_date
            and predict_date > last_display_date
        )
    ]


def collapse_actual_facts(
    rows: Iterable[Mapping[str, Any]],
    *,
    fact_name: str,
) -> dict[tuple[str, str, str], int | None]:
    """按业务事实键折叠同方向 actual，方向冲突时失败关闭。"""
    facts: dict[tuple[str, str, str], int | None] = {}
    for row in rows:
        key = (
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _required_iso_date(row.get("target_date"), field="target_date"),
            _required_text(row.get("target_rule"), field="target_rule"),
        )
        direction = _direction(row.get("actual_direction"), allow_none=True)
        if key in facts and facts[key] != direction:
            raise DashboardDataError(
                f"{fact_name} has conflicting directions for fact key {key}: "
                f"{facts[key]} != {direction}"
            )
        facts[key] = direction
    return facts


def compact_detail_row(row: Mapping[str, Any], *, source: str) -> list[Any]:
    """把展示明细编码为固定六列数组。"""
    if source not in {"live", "backtest"}:
        raise DashboardDataError(f"unknown dashboard detail source: {source}")

    prediction_phase = row.get("prediction_phase")
    if source == "live":
        if prediction_phase not in VALID_LIVE_PREDICTION_PHASES:
            raise DashboardDataError(
                f"live prediction_phase is invalid: {prediction_phase!r}"
            )
    elif prediction_phase is not None:
        raise DashboardDataError(
            f"backtest prediction_phase must be None: {prediction_phase!r}"
        )

    actual_direction = _direction(
        row.get("actual_direction"),
        allow_none=source == "live",
    )
    return [
        _required_iso_date(row.get("predict_date"), field="predict_date"),
        _required_iso_date(row.get("feature_date"), field="feature_date"),
        _required_iso_date(row.get("target_date"), field="target_date"),
        prediction_phase,
        _direction(row.get("predicted_direction"), allow_none=False),
        actual_direction,
    ]


def validate_dashboard_payload(payload: Mapping[str, Any]) -> None:
    """校验 dashboard v1 当前可构建的最小结构与明细行。"""
    if not isinstance(payload, Mapping):
        raise DashboardDataError("dashboard payload must be an object")
    if payload.get("schema_version") != DASHBOARD_SCHEMA_VERSION:
        raise DashboardDataError(
            f"dashboard schema_version must be {DASHBOARD_SCHEMA_VERSION!r}"
        )
    if payload.get("row_fields") != list(ROW_FIELDS):
        raise DashboardDataError("dashboard row_fields do not match ROW_FIELDS")

    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        raise DashboardDataError("dashboard schemes must be a list")
    for scheme_index, scheme in enumerate(schemes):
        if not isinstance(scheme, Mapping):
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] must be an object"
            )
        task_type = scheme.get("task_type")
        if task_type not in VALID_TASK_TYPES:
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] has invalid task_type: {task_type!r}"
            )
        _validate_compact_rows(
            scheme.get("live_rows"),
            source="live",
            context=f"dashboard scheme[{scheme_index}].live_rows",
        )

        backtest = scheme.get("backtest")
        if backtest is None:
            continue
        if not isinstance(backtest, Mapping):
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}].backtest must be an object or None"
            )
        _validate_compact_rows(
            backtest.get("rows"),
            source="backtest",
            context=f"dashboard scheme[{scheme_index}].backtest.rows",
        )


def _validate_compact_rows(rows: Any, *, source: str, context: str) -> None:
    if not isinstance(rows, list):
        raise DashboardDataError(f"{context} must be a list")
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(ROW_FIELDS):
            width = len(row) if isinstance(row, list) else None
            raise DashboardDataError(
                f"{context}[{row_index}] has invalid row width: {width}"
            )
        compact_detail_row(dict(zip(ROW_FIELDS, row, strict=True)), source=source)


def _is_better_prediction_for_point(
    candidate: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    candidate_extra = _json_object(candidate.get("extra"))
    if _is_weekly_metric(candidate.get("horizon"), candidate_extra):
        return _is_better_weekly_prediction(candidate, current)
    return _row_id(candidate) > _row_id(current)


def _is_better_weekly_prediction(
    candidate: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    candidate_feature = _prediction_feature_date(candidate)
    current_feature = _prediction_feature_date(current)
    if candidate_feature != current_feature:
        return candidate_feature > current_feature

    candidate_predict = _optional_iso_date(candidate.get("predict_date")) or ""
    current_predict = _optional_iso_date(current.get("predict_date")) or ""
    if candidate_predict != current_predict:
        return candidate_predict < current_predict

    return _row_id(candidate) > _row_id(current)


def _prediction_feature_date(row: Mapping[str, Any]) -> str:
    extra = _json_object(row.get("extra"))
    return (
        _optional_iso_date(row.get("feature_date"))
        or _optional_iso_date(extra.get("feature_date"))
        or _optional_iso_date(row.get("predict_date"))
        or ""
    )


def _is_weekly_metric(horizon: Any, extra: Mapping[str, Any]) -> bool:
    frequency = str(extra.get("frequency") or "").lower()
    try:
        is_weekly = int(horizon) == 6
    except (TypeError, ValueError):
        is_weekly = False
    return is_weekly or frequency == "weekly"


def _json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(value) if value is not None else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _row_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"prediction id is invalid: {row.get('id')!r}"
        ) from exc


def _required_text(value: Any, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DashboardDataError(f"dashboard row has invalid {field}: {value!r}")
    return result


def _optional_iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise DashboardDataError(f"dashboard date is invalid: {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DashboardDataError(f"dashboard date is invalid: {value!r}") from exc
    if parsed.isoformat() != value:
        raise DashboardDataError(f"dashboard date is invalid: {value!r}")
    return value


def _required_iso_date(value: Any, *, field: str) -> str:
    result = _optional_iso_date(value)
    if result is None:
        raise DashboardDataError(f"dashboard row has invalid {field}: {value!r}")
    return result


def _direction(value: Any, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is int and value in {-1, 0, 1}:
        return value
    raise DashboardDataError(f"dashboard direction is invalid: {value!r}")
