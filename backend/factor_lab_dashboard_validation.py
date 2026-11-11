"""Dashboard V7 summary/detail payload 的精确公开合同校验。"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Mapping

from shared.scheme_config_schema import normalize_scheme_owner
from shared.task_specs import ALLOWED_TASK_TYPES

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    DETAIL_ROW_FIELDS,
    FACTOR_LAB_LIVE_FEATURE_START_DATE,
    MONTHLY_ROW_FIELDS,
    RANGE_ROW_FIELDS,
    DashboardDataError,
    _required_iso_date,
    compact_detail_row,
    dashboard_result_source,
)


VALID_TASK_TYPES = set(ALLOWED_TASK_TYPES)

MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

GENERATED_AT_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)

STRUCTURED_BOUNDARY_CODEPOINTS = frozenset(
    {
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    }
)

TOP_LEVEL_FIELDS = {
    "schema_version",
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_feature_start_date",
    "monthly_row_fields",
    "range_row_fields",
    "feature_date_bounds",
    "selected_feature_range",
    "target_labels",
    "schemes",
}

DETAIL_TOP_LEVEL_FIELDS = {
    "schema_version",
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_feature_start_date",
    "scheme_id",
    "month",
    "source",
    "row_fields",
    "rows",
}

SCHEME_FIELDS = {
    "is_production",
    "scheme_id",
    "base_scheme_id",
    "name",
    "owner",
    "description",
    "horizon",
    "task_type",
    "frequency",
    "target_tenor",
    "target_label",
    "status",
    "deployed_at",
    "monthly_rows",
    "range_rows",
    "backtest",
}

BACKTEST_FIELDS = {
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date",
}



def validate_dashboard_payload(payload: Mapping[str, Any]) -> None:
    """校验 Dashboard V7 summary 或 detail 的精确公开合同。"""
    if not isinstance(payload, Mapping):
        raise DashboardDataError("dashboard payload must be an object")
    if payload.get("schema_version") != DASHBOARD_SCHEMA_VERSION:
        raise DashboardDataError(
            f"dashboard schema_version must be {DASHBOARD_SCHEMA_VERSION!r}"
        )
    representation = payload.get("representation")
    if representation == "detail":
        _validate_detail_payload(payload)
        return
    if representation != "summary":
        raise DashboardDataError("dashboard representation is invalid")
    _validate_exact_fields(
        payload,
        expected=TOP_LEVEL_FIELDS,
        context="dashboard payload",
    )
    if payload.get("monthly_row_fields") != list(MONTHLY_ROW_FIELDS):
        raise DashboardDataError(
            "dashboard monthly_row_fields do not match MONTHLY_ROW_FIELDS"
        )
    if payload.get("range_row_fields") != list(RANGE_ROW_FIELDS):
        raise DashboardDataError("dashboard range_row_fields are invalid")
    bounds = payload.get("feature_date_bounds")
    if not isinstance(bounds, Mapping) or set(bounds) != {"all", "backtest", "live"}:
        raise DashboardDataError("dashboard feature_date_bounds are invalid")
    for source, value in bounds.items():
        _validate_date_range(value, context=f"feature_date_bounds.{source}")
        if value is not None and source != "all":
            if any(dashboard_result_source(day) != source for day in value.values()):
                raise DashboardDataError("dashboard feature_date_bounds source is invalid")
    available = [value for source, value in bounds.items() if source != "all" and value]
    expected_all = None if not available else {
        "start_date": min(value["start_date"] for value in available),
        "end_date": max(value["end_date"] for value in available),
    }
    if bounds["all"] != expected_all:
        raise DashboardDataError("dashboard feature_date_bounds union is invalid")
    _validate_date_range(payload.get("selected_feature_range"), context="selected_feature_range")
    _validate_live_feature_start_date(payload)

    _required_snapshot_id(payload.get("snapshot_id"))
    _required_aware_iso_datetime(payload.get("generated_at"))
    _required_payload_iso_date(
        payload.get("display_until"), field="display_until"
    )
    target_labels = payload.get("target_labels")
    if not isinstance(target_labels, Mapping):
        raise DashboardDataError("dashboard target_labels must be an object")
    for target, label in target_labels.items():
        _required_string(target, field="target_labels target")
        _required_string(label, field="target_labels label")

    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        raise DashboardDataError("dashboard schemes must be a list")
    scheme_ids: set[str] = set()
    ordered_scheme_ids: list[str] = []
    for scheme_index, scheme in enumerate(schemes):
        if not isinstance(scheme, Mapping):
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] must be an object"
            )
        _validate_exact_fields(
            scheme,
            expected=SCHEME_FIELDS,
            context=f"dashboard scheme[{scheme_index}]",
        )
        if type(scheme.get("is_production")) is not bool:
            raise DashboardDataError("scheme is_production must be a boolean")
        task_type = scheme.get("task_type")
        if task_type not in VALID_TASK_TYPES:
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] has invalid task_type: {task_type!r}"
            )
        scheme_id = _required_string(
            scheme.get("scheme_id"), field=f"scheme[{scheme_index}] scheme_id"
        )
        base_scheme_id = _required_string(
            scheme.get("base_scheme_id"),
            field=f"scheme[{scheme_index}] base_scheme_id",
        )
        target_tenor = _required_string(
            scheme.get("target_tenor"),
            field=f"scheme[{scheme_index}] target_tenor",
        )
        horizon = _required_json_integer(
            scheme.get("horizon"),
            field=f"scheme[{scheme_index}] horizon",
        )
        expected_scheme_id = f"{base_scheme_id}__h{horizon}__{target_tenor}"
        if scheme_id != expected_scheme_id:
            raise DashboardDataError(
                "dashboard composite scheme identity is invalid: "
                f"scheme_id={scheme_id!r} expected={expected_scheme_id!r}"
            )
        if scheme_id in scheme_ids:
            raise DashboardDataError(
                f"dashboard has duplicate scheme_id: {scheme_id}"
            )
        scheme_ids.add(scheme_id)
        ordered_scheme_ids.append(scheme_id)
        _required_payload_iso_date(
            scheme.get("deployed_at"),
            field=f"scheme[{scheme_index}] deployed_at",
        )
        if scheme.get("status") != "active":
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] status must be active"
            )
        _required_string(
            scheme.get("name"), field=f"scheme[{scheme_index}] name"
        )
        _required_owner(
            scheme.get("owner"), field=f"scheme[{scheme_index}] owner"
        )
        description = scheme.get("description")
        if not isinstance(description, str):
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] description must be a string"
            )
        _required_string(
            scheme.get("frequency"),
            field=f"scheme[{scheme_index}] frequency",
        )
        target_label = _required_string(
            scheme.get("target_label"),
            field=f"scheme[{scheme_index}] target_label",
        )
        if target_tenor not in target_labels:
            raise DashboardDataError(
                "dashboard target identity is invalid: "
                f"target_tenor={target_tenor!r}"
            )
        if target_label != target_labels[target_tenor]:
            raise DashboardDataError(
                "dashboard target_label does not match target_labels: "
                f"target_tenor={target_tenor!r} "
                f"target_label={target_label!r}"
            )
        _validate_monthly_rows(
            scheme.get("monthly_rows"),
            context=f"dashboard scheme[{scheme_index}].monthly_rows",
        )

        _validate_range_rows(
            scheme.get("range_rows"),
            context=f"dashboard scheme[{scheme_index}].range_rows",
        )

        full_counts: dict[str, list[int]] = {}
        for monthly_row in scheme["monthly_rows"]:
            totals = full_counts.setdefault(monthly_row[1], [0] * len(RANGE_ROW_FIELDS[1:]))
            for index, count in enumerate(monthly_row[2:]):
                totals[index] += count
        for range_row in scheme["range_rows"]:
            totals = full_counts.get(range_row[0])
            if totals is None or any(
                count > total for count, total in zip(range_row[1:], totals, strict=True)
            ):
                raise DashboardDataError("dashboard range rows exceed full history")
        if payload["selected_feature_range"] is None and scheme["range_rows"] != [
            [source, *counts] for source, counts in sorted(full_counts.items())
        ]:
            raise DashboardDataError("dashboard unfiltered range rows differ from full history")

        backtest = scheme["backtest"]
        if backtest is None:
            continue
        if not isinstance(backtest, Mapping):
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}].backtest must be an object or None"
            )
        _validate_exact_fields(
            backtest,
            expected=BACKTEST_FIELDS,
            context=f"dashboard scheme[{scheme_index}].backtest",
        )
        for field in (
            "benchmark_id",
            "benchmark_label",
            "data_source",
            "data_source_label",
        ):
            _required_string(
                backtest.get(field),
                field=f"scheme[{scheme_index}].backtest.{field}",
            )
        _required_payload_iso_date(
            backtest.get("latest_run_date"),
            field=f"scheme[{scheme_index}].backtest.latest_run_date",
        )
    if ordered_scheme_ids != sorted(ordered_scheme_ids):
        raise DashboardDataError("dashboard schemes are not sorted by scheme_id")



def _validate_detail_payload(payload: Mapping[str, Any]) -> None:
    _validate_exact_fields(
        payload,
        expected=DETAIL_TOP_LEVEL_FIELDS,
        context="dashboard detail payload",
    )
    if payload.get("row_fields") != list(DETAIL_ROW_FIELDS):
        raise DashboardDataError(
            "dashboard detail row_fields do not match DETAIL_ROW_FIELDS"
        )
    _required_snapshot_id(payload.get("snapshot_id"))
    _required_aware_iso_datetime(payload.get("generated_at"))
    _required_payload_iso_date(payload.get("display_until"), field="display_until")
    _validate_live_feature_start_date(payload)
    _required_string(payload.get("scheme_id"), field="detail scheme_id")
    _required_month(payload.get("month"), field="detail month")
    source = payload.get("source")
    if source not in {"all", "backtest", "live"}:
        raise DashboardDataError("dashboard detail source is invalid")
    _validate_compact_rows(
        payload.get("rows"),
        source=None if source == "all" else str(source),
        context="dashboard detail rows",
        month=str(payload["month"]),
    )



def _validate_live_feature_start_date(payload: Mapping[str, Any]) -> None:
    value = _required_payload_iso_date(
        payload.get("live_feature_start_date"),
        field="live_feature_start_date",
    )
    if value != FACTOR_LAB_LIVE_FEATURE_START_DATE.isoformat():
        raise DashboardDataError("dashboard live_feature_start_date is invalid")



def _validate_monthly_rows(
    value: Any,
    *,
    context: str,
) -> None:
    if not isinstance(value, list):
        raise DashboardDataError(f"{context} must be a list")
    previous: tuple[str, int] | None = None
    seen: set[tuple[str, str]] = set()
    source_rank = {"backtest": 0, "live": 1}
    live_start_month = FACTOR_LAB_LIVE_FEATURE_START_DATE.strftime("%Y-%m")
    for index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != len(MONTHLY_ROW_FIELDS):
            raise DashboardDataError(f"{context}[{index}] has invalid row width")
        detail = dict(zip(MONTHLY_ROW_FIELDS, row, strict=True))
        month = _required_month(detail["month"], field=f"{context}[{index}].month")
        source = detail["source"]
        if source not in source_rank:
            raise DashboardDataError(f"{context}[{index}].source is invalid")
        expected_source = "live" if month >= live_start_month else "backtest"
        if source != expected_source:
            raise DashboardDataError(
                f"{context}[{index}].source does not match feature_date policy"
            )
        key = (month, str(source))
        if key in seen:
            raise DashboardDataError(f"{context} has duplicate month/source")
        seen.add(key)
        sort_key = (month, source_rank[str(source)])
        if previous is not None and sort_key < previous:
            raise DashboardDataError(f"{context} is not canonically sorted")
        previous = sort_key
        _validate_counts(detail, context=f"{context}[{index}]")



def _validate_date_range(value: Any, *, context: str) -> None:
    """校验规范日期闭区间或无数据状态。"""
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != {"start_date", "end_date"}:
        raise DashboardDataError(f"dashboard {context} is invalid")
    for field in ("start_date", "end_date"):
        _required_payload_iso_date(value[field], field=f"{context}.{field}")
    if value["start_date"] > value["end_date"]:
        raise DashboardDataError(f"dashboard {context} is reversed")



def _validate_range_rows(value: Any, *, context: str) -> None:
    """校验区间计数，允许纯待验证来源保留零计数行。"""
    if not isinstance(value, list):
        raise DashboardDataError(f"{context} must be a list")
    previous = ""
    for index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != len(RANGE_ROW_FIELDS):
            raise DashboardDataError(f"{context}[{index}] has invalid row width")
        detail = dict(zip(RANGE_ROW_FIELDS, row, strict=True))
        source = detail["source"]
        if source not in {"backtest", "live"} or source <= previous:
            raise DashboardDataError(f"{context}[{index}].source is invalid or duplicated")
        previous = source
        _validate_counts(detail, context=f"{context}[{index}]")



def _validate_counts(detail: Mapping[str, Any], *, context: str) -> None:
    """月份与区间共用已验证计数不变量。"""
    counts = {
        field: _required_nonnegative_integer(
            detail[field], field=f"{context}.{field}"
        )
        for field in MONTHLY_ROW_FIELDS[2:]
    }
    if counts["metric_samples"] > counts["samples"]:
        raise DashboardDataError(f"{context} counts are inconsistent")
    if counts["correct"] > counts["metric_samples"]:
        raise DashboardDataError(f"{context} counts are inconsistent")
    if (
        counts["predicted_up"]
        + counts["predicted_down"]
        + counts["predicted_flat"]
        != counts["samples"]
        or counts["actual_up"]
        + counts["actual_down"]
        + counts["actual_flat"]
        != counts["samples"]
    ):
        raise DashboardDataError(f"{context} distributions are inconsistent")



def _validate_compact_rows(
    rows: Any,
    *,
    source: str | None,
    context: str,
    month: str,
) -> None:
    if not isinstance(rows, list):
        raise DashboardDataError(f"{context} must be a list")
    seen_points: set[tuple[str, str]] = set()
    previous_sort_key: tuple[int, str, str, str] | None = None
    source_rank = {"backtest": 0, "live": 1}
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(DETAIL_ROW_FIELDS):
            width = len(row) if isinstance(row, list) else None
            raise DashboardDataError(
                f"{context}[{row_index}] has invalid row width: {width}"
            )
        row_source = row[0]
        if row_source not in source_rank or (source is not None and row_source != source):
            raise DashboardDataError(f"{context}[{row_index}].source is invalid")
        for field_index, field in enumerate(DETAIL_ROW_FIELDS[1:4], start=1):
            value = row[field_index]
            if not isinstance(value, str) or not value:
                raise DashboardDataError(
                    f"{context}[{row_index}].{field} must be an ISO date string"
                )
        detail = dict(zip(DETAIL_ROW_FIELDS, row, strict=True))
        if row_source != dashboard_result_source(detail["feature_date"]):
            raise DashboardDataError(
                f"{context}[{row_index}].source does not match feature_date policy"
            )
        compact_detail_row(detail, source=str(row_source))
        if str(detail["feature_date"])[:7] != month:
            raise DashboardDataError(f"{context}[{row_index}] escaped feature month")
        target_date = str(detail["target_date"])
        point = (str(row_source), target_date)
        if point in seen_points:
            raise DashboardDataError(
                f"{context} has duplicate {row_source} prediction point: {target_date}"
            )
        seen_points.add(point)
        sort_key = (
            source_rank[str(row_source)],
            str(detail["feature_date"]),
            target_date,
            str(detail["predict_date"]),
        )
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise DashboardDataError(f"{context} is not canonically sorted")
        previous_sort_key = sort_key



def _validate_exact_fields(
    value: Mapping[str, Any],
    *,
    expected: set[str],
    context: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    if missing:
        raise DashboardDataError(
            f"{context} missing required fields: {','.join(missing)}"
        )
    unknown = sorted(actual - expected)
    if unknown:
        raise DashboardDataError(
            f"{context} has unknown fields: {','.join(unknown)}"
        )



def _required_string(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or _is_structured_boundary_character(value[0])
        or _is_structured_boundary_character(value[-1])
    ):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return value



def _required_owner(value: Any, *, field: str) -> str:
    try:
        owner = normalize_scheme_owner(value)
    except ValueError as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc
    _required_string(owner, field=field)
    return owner



def _required_month(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return value



def _required_nonnegative_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return value



def _required_snapshot_id(value: Any) -> str:
    if not isinstance(value, str) or SNAPSHOT_ID_PATTERN.fullmatch(value) is None:
        raise DashboardDataError(f"dashboard snapshot_id is invalid: {value!r}")
    return value



def _is_structured_boundary_character(value: str) -> bool:
    codepoint = ord(value)
    return (
        codepoint <= 0x20
        or 0x7F <= codepoint <= 0x9F
        or codepoint in STRUCTURED_BOUNDARY_CODEPOINTS
    )



def _required_json_integer(
    value: Any,
    *,
    field: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    if isinstance(value, int):
        result = value
    elif not math.isfinite(value) or not value.is_integer():
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    else:
        result = int(value)
    if abs(result) > MAX_SAFE_JSON_INTEGER:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    if result < 1:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result



def _required_aware_iso_datetime(value: Any) -> str:
    field = "generated_at"
    if not isinstance(value, str) or GENERATED_AT_PATTERN.fullmatch(value) is None:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return value



def _required_payload_iso_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DashboardDataError(
            f"dashboard {field} must be an ISO date string"
        )
    return _required_iso_date(value, field=field)

