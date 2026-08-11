from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)


DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v1"
FACTOR_LAB_HISTORY_START_DATE = "2025-01-01"
ROW_FIELDS = (
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction",
)
VALID_TASK_TYPES = {"T+1", "T+5", "weekly_point", "weekly_average", "monthly"}
WEEKLY_METRIC_TASK_TYPES = {"weekly_point", "weekly_average"}
VALID_LIVE_PREDICTION_PHASES = {"gray_live", "scheduled_live"}
VALID_SIGNAL_STATUSES = {"missing", "not_due", "present"}
DAILY_TARGET_RULE = "target_date_yield_vs_feature_date_yield"
LIVE_ACTUAL_SELECTORS = {
    "T+1": ("daily_1d", DAILY_TARGET_RULE),
    "T+5": ("daily_5d", DAILY_TARGET_RULE),
    "weekly_point": ("weekly", WEEKLY_TARGET_RULE),
    "weekly_average": ("weekly", WEEKLY_AVERAGE_TARGET_RULE),
    "monthly": ("monthly", MONTHLY_TARGET_RULE),
}
BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE = {
    "native_adapter": "framework_db_aligned",
    "blackbox_v2": "blackbox_v2_current_snapshot_as_of",
}
BACKTEST_BENCHMARK_LABELS = {
    "model_muti_0529": "0529历史基准",
}
BACKTEST_DATA_SOURCE_LABELS = {
    "baseline_original_csv": "原始代码基准CSV回测",
    "framework_original_csv": "框架算法基准CSV回测",
    "framework_db_aligned": "当前DB对齐回测",
    "blackbox_v2_current_snapshot_as_of": "Blackbox V2 当前快照回测",
    "runtime_default": "按方案运行时选择回测",
    "source_original_monthly_binary_runner": "月度0629原始二进制Runner回测",
}
_ACTUAL_CONFLICT_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})
_ACTUAL_CONFLICT_TENOR_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,31}\Z",
    flags=re.ASCII,
)
_ACTUAL_CONFLICT_RULE_PATTERN = re.compile(
    r"[a-z][a-z0-9_]{0,127}\Z",
    flags=re.ASCII,
)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
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
    "snapshot_id",
    "generated_at",
    "display_until",
    "stale",
    "snapshot_age_ms",
    "row_fields",
    "target_labels",
    "schemes",
}
SCHEME_FIELDS = {
    "scheme_id",
    "base_scheme_id",
    "name",
    "description",
    "horizon",
    "task_type",
    "frequency",
    "target_tenor",
    "target_label",
    "status",
    "deployed_at",
    "signal_status",
    "signal_failure_category",
    "live_rows",
    "backtest",
}
BACKTEST_FIELDS = {
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date",
    "rows",
}


class DashboardDataError(RuntimeError):
    """展示快照存在冲突或结构错误。"""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def is_factor_lab_history_visible(predict_date: str) -> bool:
    """判断信号是否位于当前因子实验室展示窗口。"""
    return predict_date >= FACTOR_LAB_HISTORY_START_DATE


@dataclass(frozen=True, slots=True)
class ActualFactCollapseResult:
    """actual 事实折叠结果及不含事实明细的安全计数。"""

    facts: dict[tuple[str, str, str], int | None]
    same_direction_duplicates_folded: int
    direction_conflicts: int


def live_actual_selector(task_type: Any) -> tuple[str, str]:
    """按 Registry task_type 返回 actual 事实类型与规则，不推断 horizon。"""
    try:
        return LIVE_ACTUAL_SELECTORS[task_type]
    except (KeyError, TypeError) as exc:
        raise DashboardDataError(
            f"dashboard scheme has invalid task_type: {task_type!r}"
        ) from exc


def choose_latest_backtest_runs(
    run_rows: Iterable[Mapping[str, Any]],
    registry_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """按默认 runtime source 与两层 latest-success 规则选择回测 run。"""
    registry = list(registry_rows)
    runtime_by_base: dict[str, str] = {}
    for row in registry:
        status = row.get("status")
        if status not in (None, "active"):
            continue
        base_scheme_id = _required_text(
            row.get("base_scheme_id"), field="registry base_scheme_id"
        )
        runtime_type = _required_text(
            row.get("runtime_type"), field="registry runtime_type"
        )
        if runtime_type not in BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE:
            raise DashboardDataError(
                "dashboard registry runtime_type is invalid: "
                f"base_scheme_id={base_scheme_id} runtime_type={runtime_type!r}"
            )
        current = runtime_by_base.get(base_scheme_id)
        if current is not None and current != runtime_type:
            raise DashboardDataError(
                "active Registry runtime_type is inconsistent: "
                f"base_scheme_id={base_scheme_id} "
                f"runtime_types={sorted({current, runtime_type})}"
            )
        runtime_by_base[base_scheme_id] = runtime_type

    latest_by_benchmark_scope: dict[
        tuple[str, str, str], Mapping[str, Any]
    ] = {}
    for row in run_rows:
        if row.get("status") != "success":
            continue
        base_scheme_id = _required_text(
            row.get("scheme_id"), field="backtest run scheme_id"
        )
        runtime_type = runtime_by_base.get(base_scheme_id)
        if runtime_type is None:
            continue
        data_source = _required_text(
            row.get("data_source"), field="backtest run data_source"
        )
        if data_source != BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE[runtime_type]:
            continue
        key = (
            _required_text(
                row.get("benchmark_id"), field="backtest run benchmark_id"
            ),
            base_scheme_id,
            data_source,
        )
        current = latest_by_benchmark_scope.get(key)
        if current is None or _backtest_run_rank(row) > _backtest_run_rank(current):
            latest_by_benchmark_scope[key] = row

    candidates_by_base: dict[str, list[Mapping[str, Any]]] = {}
    for row in latest_by_benchmark_scope.values():
        candidates_by_base.setdefault(str(row["scheme_id"]), []).append(row)

    selected: dict[str, Mapping[str, Any]] = {}
    for row in registry:
        status = row.get("status")
        if status not in (None, "active"):
            continue
        scheme_id = _required_text(row.get("scheme_id"), field="registry scheme_id")
        base_scheme_id = _required_text(
            row.get("base_scheme_id"), field="registry base_scheme_id"
        )
        candidates = candidates_by_base.get(base_scheme_id, [])
        if candidates:
            selected[scheme_id] = max(candidates, key=_backtest_run_rank)
    return selected


def backtest_benchmark_label(benchmark_id: Any) -> str:
    """返回回测 benchmark 的可读标签。"""
    if benchmark_id in (None, "", "all"):
        return "全部历史基准"
    value = str(benchmark_id)
    return BACKTEST_BENCHMARK_LABELS.get(value, value)


def backtest_data_source_label(data_source: Any) -> str:
    """返回回测 data source 的可读标签。"""
    value = str(data_source)
    return BACKTEST_DATA_SOURCE_LABELS.get(value, value)


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


def collapse_actual_facts_with_diagnostics(
    rows: Iterable[Mapping[str, Any]],
    *,
    fact_name: str,
    frequency: str | None = None,
) -> ActualFactCollapseResult:
    """单遍折叠 actual，并返回安全的重复/冲突聚合计数。"""
    facts: dict[tuple[str, str, str], int | None] = {}
    same_direction_duplicates_folded = 0
    for row in rows:
        key = (
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _required_iso_date(row.get("target_date"), field="target_date"),
            _required_text(row.get("target_rule"), field="target_rule"),
        )
        direction = _direction(row.get("actual_direction"), allow_none=True)
        if key in facts:
            if facts[key] != direction:
                raise DashboardDataError(
                    f"{fact_name} has conflicting directions",
                    diagnostics={
                        "same_direction_duplicates_folded": (
                            same_direction_duplicates_folded
                        ),
                        "direction_conflicts": 1,
                        **(
                            {
                                "actual_conflict_locator": (
                                    _actual_conflict_locator(
                                        frequency=frequency,
                                        target_tenor=key[0],
                                        target_date=key[1],
                                        target_rule=key[2],
                                    )
                                )
                            }
                            if frequency in _ACTUAL_CONFLICT_FREQUENCIES
                            else {}
                        ),
                    },
                )
            same_direction_duplicates_folded += 1
        facts[key] = direction
    return ActualFactCollapseResult(
        facts=facts,
        same_direction_duplicates_folded=same_direction_duplicates_folded,
        direction_conflicts=0,
    )


def _actual_conflict_locator(
    *,
    frequency: str,
    target_tenor: str,
    target_date: str,
    target_rule: str,
) -> dict[str, str]:
    """构造只含规范业务字段或稳定哈希替代值的冲突定位信息。"""
    return {
        "frequency": frequency,
        "target_tenor": _safe_actual_locator_text(
            target_tenor,
            pattern=_ACTUAL_CONFLICT_TENOR_PATTERN,
        ),
        "target_date": _safe_actual_locator_date(target_date),
        "target_rule": _safe_actual_locator_text(
            target_rule,
            pattern=_ACTUAL_CONFLICT_RULE_PATTERN,
        ),
    }


def _safe_actual_locator_text(
    value: str,
    *,
    pattern: re.Pattern[str],
) -> str:
    """合法值原样保留；其它值只输出稳定短哈希。"""
    if pattern.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"redacted-sha256:{digest}"


def _safe_actual_locator_date(value: str) -> str:
    """仅保留 canonical YYYY-MM-DD；其它日期只输出稳定短哈希。"""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.isoformat() == value:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"redacted-sha256:{digest}"


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
    """校验 dashboard v1 顶层合同、Registry 身份与明细行。"""
    if not isinstance(payload, Mapping):
        raise DashboardDataError("dashboard payload must be an object")
    if payload.get("schema_version") != DASHBOARD_SCHEMA_VERSION:
        raise DashboardDataError(
            f"dashboard schema_version must be {DASHBOARD_SCHEMA_VERSION!r}"
        )
    _validate_exact_fields(
        payload,
        expected=TOP_LEVEL_FIELDS,
        context="dashboard payload",
    )
    if payload.get("row_fields") != list(ROW_FIELDS):
        raise DashboardDataError("dashboard row_fields do not match ROW_FIELDS")

    _required_snapshot_id(payload.get("snapshot_id"))
    _required_aware_iso_datetime(
        payload.get("generated_at"), field="generated_at"
    )
    _required_payload_iso_date(
        payload.get("display_until"), field="display_until"
    )
    if type(payload.get("stale")) is not bool:
        raise DashboardDataError(
            f"dashboard stale is invalid: {payload.get('stale')!r}"
        )
    _required_json_integer(
        payload.get("snapshot_age_ms"),
        field="snapshot_age_ms",
        minimum=0,
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
            minimum=1,
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
        signal_status = scheme.get("signal_status")
        if signal_status not in VALID_SIGNAL_STATUSES:
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] has invalid signal_status: "
                f"{signal_status!r}"
            )
        signal_failure_category = scheme.get("signal_failure_category")
        if signal_status == "missing":
            _required_string(
                signal_failure_category,
                field=(
                    f"scheme[{scheme_index}] signal_failure_category"
                ),
            )
        elif signal_failure_category is not None:
            raise DashboardDataError(
                f"dashboard scheme[{scheme_index}] signal_failure_category "
                "must be null when signal is available"
            )
        _required_string(
            scheme.get("name"), field=f"scheme[{scheme_index}] name"
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
        _validate_compact_rows(
            scheme.get("live_rows"),
            source="live",
            context=f"dashboard scheme[{scheme_index}].live_rows",
        )

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
        _validate_compact_rows(
            backtest.get("rows"),
            source="backtest",
            context=f"dashboard scheme[{scheme_index}].backtest.rows",
        )
    if ordered_scheme_ids != sorted(ordered_scheme_ids):
        raise DashboardDataError("dashboard schemes are not sorted by scheme_id")


def _validate_compact_rows(rows: Any, *, source: str, context: str) -> None:
    if not isinstance(rows, list):
        raise DashboardDataError(f"{context} must be a list")
    seen_points: set[str] = set()
    sort_keys: list[tuple[str, str]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(ROW_FIELDS):
            width = len(row) if isinstance(row, list) else None
            raise DashboardDataError(
                f"{context}[{row_index}] has invalid row width: {width}"
            )
        for field_index, field in enumerate(ROW_FIELDS[:3]):
            value = row[field_index]
            if not isinstance(value, str) or not value:
                raise DashboardDataError(
                    f"{context}[{row_index}].{field} must be an ISO date string"
                )
            _required_iso_date(value, field=f"{context}[{row_index}].{field}")
        detail = dict(zip(ROW_FIELDS, row, strict=True))
        compact_detail_row(detail, source=source)
        target_date = str(detail["target_date"])
        if target_date in seen_points:
            raise DashboardDataError(
                f"{context} has duplicate {source} prediction point: {target_date}"
            )
        seen_points.add(target_date)
        sort_keys.append((target_date, str(detail["predict_date"])))
    if sort_keys != sorted(sort_keys):
        raise DashboardDataError(f"{context} is not canonically sorted")


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
    if str(extra.get("task_type") or "") in WEEKLY_METRIC_TASK_TYPES:
        return True
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


def _backtest_updated_at_rank(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif (
        isinstance(value, str)
        and value
        and value == value.strip()
    ):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DashboardDataError(
                f"backtest run updated_at is invalid: {value!r}"
            ) from exc
    else:
        raise DashboardDataError(
            f"backtest run updated_at is invalid: {value!r}"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _backtest_run_rank(row: Mapping[str, Any]) -> tuple[datetime, int]:
    updated_rank = _backtest_updated_at_rank(row.get("updated_at"))
    try:
        run_id = int(row.get("id"))
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"backtest run id is invalid: {row.get('id')!r}"
        ) from exc
    return updated_rank, run_id


def _required_text(value: Any, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DashboardDataError(f"dashboard row has invalid {field}: {value!r}")
    return result


def _required_string(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or _is_structured_boundary_character(value[0])
        or _is_structured_boundary_character(value[-1])
    ):
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
    minimum: int | None = None,
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
    if minimum is not None and result < minimum:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result


def _required_aware_iso_datetime(value: Any, *, field: str) -> str:
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
    try:
        result = _optional_iso_date(value)
    except DashboardDataError as exc:
        raise DashboardDataError(
            f"dashboard row has invalid {field}: {value!r}"
        ) from exc
    if result is None:
        raise DashboardDataError(f"dashboard row has invalid {field}: {value!r}")
    return result


def _direction(value: Any, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and float(value).is_integer()
        and int(value) in {-1, 0, 1}
    ):
        return int(value)
    raise DashboardDataError(f"dashboard direction is invalid: {value!r}")
