from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from shared.models import DIRECTION_VALUES
from shared.scheme_config_schema import normalize_scheme_owner
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import (
    ALLOWED_TASK_TYPES,
    PERIOD_AVERAGE_TASK_TYPES,
    PREDICTION_CADENCES,
    TASK_COMBINATIONS,
    WEEKLY_TASK_TYPES,
)


DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v6"
FACTOR_LAB_HISTORY_START_DATE = "2025-01-01"
FACTOR_LAB_LIVE_TARGET_START_DATE = date(2026, 6, 1)
DETAIL_ROW_FIELDS = (
    "source",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
    "actual_direction",
)
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
VALID_TASK_TYPES = set(ALLOWED_TASK_TYPES)
DAILY_TARGET_RULE = "target_date_yield_vs_feature_date_yield"
LIVE_ACTUAL_SELECTORS = {
    "T+1": ("daily_1d", DAILY_TARGET_RULE),
    "T+5": ("daily_5d", DAILY_TARGET_RULE),
    "weekly_point": ("weekly", WEEKLY_TARGET_RULE),
    "weekly_average": ("weekly", WEEKLY_AVERAGE_TARGET_RULE),
    "monthly": ("monthly", MONTHLY_TARGET_RULE),
    **{
        task_type: ("period_average", TASK_COMBINATIONS[task_type][1])
        for task_type in PERIOD_AVERAGE_TASK_TYPES
    },
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
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_target_start_date",
    "monthly_row_fields",
    "target_labels",
    "schemes",
}
DETAIL_TOP_LEVEL_FIELDS = {
    "schema_version",
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_target_start_date",
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
    "backtest",
}
BACKTEST_FIELDS = {
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date",
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


@dataclass(slots=True)
class MonthlySummaryAccumulator:
    """单个月份/来源的紧凑计数器；创建实例即保留 pending 月份入口。"""

    samples: int = 0
    metric_samples: int = 0
    correct: int = 0
    predicted_up: int = 0
    predicted_down: int = 0
    predicted_flat: int = 0
    actual_up: int = 0
    actual_down: int = 0
    actual_flat: int = 0
    up_true_positive: int = 0
    down_true_positive: int = 0

    def observe(self, *, predicted_direction: Any, actual_direction: Any) -> None:
        """计入一条已验证事实；pending 不调用本方法。"""
        predicted = _direction(predicted_direction, allow_none=False)
        actual = _direction(actual_direction, allow_none=False)
        self.samples += 1
        if predicted == 1:
            self.predicted_up += 1
        elif predicted == -1:
            self.predicted_down += 1
        else:
            self.predicted_flat += 1
        if actual == 1:
            self.actual_up += 1
        elif actual == -1:
            self.actual_down += 1
        else:
            self.actual_flat += 1
        if predicted not in {-1, 1}:
            return
        self.metric_samples += 1
        if predicted == actual:
            self.correct += 1
        if predicted == actual == 1:
            self.up_true_positive += 1
        elif predicted == actual == -1:
            self.down_true_positive += 1

    def compact(self, *, month: str, source: str) -> list[Any]:
        """按 Dashboard V6 固定列顺序输出。"""
        return [
            month,
            source,
            self.samples,
            self.metric_samples,
            self.correct,
            self.predicted_up,
            self.predicted_down,
            self.predicted_flat,
            self.actual_up,
            self.actual_down,
            self.actual_flat,
            self.up_true_positive,
            self.down_true_positive,
        ]


def is_factor_lab_history_visible(predict_date: str) -> bool:
    """判断信号是否位于当前因子实验室展示窗口。"""
    return predict_date >= FACTOR_LAB_HISTORY_START_DATE


def dashboard_result_source(target_date: Any) -> str:
    """只按目标日期返回公开的回测/实盘业务口径。"""
    value = _required_iso_date(target_date, field="target_date")
    return (
        "live"
        if date.fromisoformat(value) >= FACTOR_LAB_LIVE_TARGET_START_DATE
        else "backtest"
    )


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
    """按原业务 ID 与两层 latest-success 选择平台回测来源，不随当前运行时改写历史。"""
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
        tuple[str, str, str],
        tuple[Mapping[str, Any], tuple[datetime, int]],
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
        if data_source not in BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE.values():
            continue
        key = (
            _required_text(
                row.get("benchmark_id"), field="backtest run benchmark_id"
            ),
            base_scheme_id,
            data_source,
        )
        rank = _backtest_run_rank(row)
        current = latest_by_benchmark_scope.get(key)
        if current is None or rank > current[1]:
            latest_by_benchmark_scope[key] = (row, rank)

    latest_by_base: dict[
        str,
        tuple[Mapping[str, Any], tuple[datetime, int]],
    ] = {}
    for row, rank in latest_by_benchmark_scope.values():
        base_scheme_id = str(row["scheme_id"])
        current = latest_by_base.get(base_scheme_id)
        if current is None or rank > current[1]:
            latest_by_base[base_scheme_id] = (row, rank)

    selected: dict[str, Mapping[str, Any]] = {}
    for row in registry:
        status = row.get("status")
        if status not in (None, "active"):
            continue
        scheme_id = _required_text(row.get("scheme_id"), field="registry scheme_id")
        base_scheme_id = _required_text(
            row.get("base_scheme_id"), field="registry base_scheme_id"
        )
        latest = latest_by_base.get(base_scheme_id)
        if latest is not None:
            selected[scheme_id] = latest[0]
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


def registry_task_type_index(
    registry_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, int], str]:
    """按预测行可匹配的键索引 active Registry 的权威 task_type。"""
    index: dict[tuple[str, str, int], str] = {}
    for row in registry_rows:
        if row.get("status") not in (None, "active"):
            continue
        base_scheme_id = _required_text(
            row.get("base_scheme_id"), field="registry base_scheme_id"
        )
        target_tenor = _required_text(
            row.get("target_tenor"), field="registry target_tenor"
        )
        try:
            horizon = int(row["horizon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DashboardDataError(
                f"registry horizon is invalid: {row.get('horizon')!r}"
            ) from exc
        index[(base_scheme_id, target_tenor, horizon)] = _required_text(
            row.get("task_type"), field="registry task_type"
        )
    return index


def choose_live_prediction_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    display_until: Any,
    task_type_by_scheme: Mapping[tuple[str, str, int], str],
) -> list[Mapping[str, Any]]:
    """按旧 API 规则选择 canonical 实盘预测，再过滤未来发出日。

    ``task_type_by_scheme`` 是 active Registry 的权威 task_type 索引，键为
    ``(base_scheme_id, target_tenor, horizon)``，是判定周频归属的唯一依据。
    去重分组键的前三段就是该索引键，因此同一分组内判据恒定。不在索引中的
    行不属于当前 active 业务范围，按点位规则去重，其结果不进入展示。
    """
    latest_by_point: dict[tuple[Any, Any, Any, str], Mapping[str, Any]] = {}
    for row in rows:
        point_date = _required_iso_date(row.get("target_date"), field="target_date")
        key = (
            row.get("scheme_id"),
            row.get("target_tenor"),
            row.get("horizon"),
            point_date,
        )
        is_weekly = (
            task_type_by_scheme.get(_prediction_scheme_key(row))
            in WEEKLY_TASK_TYPES
        )
        current = latest_by_point.get(key)
        if current is None or _is_better_prediction_for_point(
            row,
            current,
            is_weekly=is_weekly,
        ):
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


def iter_grouped_live_prediction_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    display_until: Any,
    task_type_by_scheme: Mapping[tuple[str, str, int], str],
) -> Iterator[Mapping[str, Any]]:
    """从按业务点分组排序的流中逐点选择 canonical 产品事实。

    该入口与 :func:`choose_live_prediction_rows` 使用相同选择规则，但只保留
    当前业务点，供 Summary 在不物化全历史明细的情况下完成聚合。调用方
    必须按 ``scheme_id, target_tenor, horizon, target_date`` 升序提供行。
    """
    last_key: tuple[str, str, int, str] | None = None
    selected: Mapping[str, Any] | None = None
    last_display_date = _optional_iso_date(display_until)

    for row in rows:
        scheme_key = _prediction_scheme_key(row)
        if scheme_key is None:
            raise DashboardDataError("prediction scheme key is invalid")
        key = (
            *scheme_key,
            _required_iso_date(row.get("target_date"), field="target_date"),
        )
        if last_key is not None and key < last_key:
            raise DashboardDataError(
                "summary prediction stream is not grouped by business key"
            )
        if last_key is not None and key != last_key:
            assert selected is not None
            predict_date = _optional_iso_date(selected.get("predict_date"))
            if not (
                predict_date
                and last_display_date
                and predict_date > last_display_date
            ):
                yield selected
            selected = None
        if selected is None or _is_better_prediction_for_point(
            row,
            selected,
            is_weekly=(task_type_by_scheme.get(scheme_key) in WEEKLY_TASK_TYPES),
        ):
            selected = row
        last_key = key

    if selected is not None:
        predict_date = _optional_iso_date(selected.get("predict_date"))
        if not (
            predict_date
            and last_display_date
            and predict_date > last_display_date
        ):
            yield selected


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
                            if frequency in PREDICTION_CADENCES
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
    """把按需展示明细编码为 V6 固定六列数组。"""
    if source not in {"live", "backtest"}:
        raise DashboardDataError(f"unknown dashboard detail source: {source}")

    actual_direction = _direction(
        row.get("actual_direction"),
        allow_none=True,
    )
    return [
        source,
        _required_iso_date(row.get("predict_date"), field="predict_date"),
        _required_iso_date(row.get("feature_date"), field="feature_date"),
        _required_iso_date(row.get("target_date"), field="target_date"),
        _direction(row.get("predicted_direction"), allow_none=False),
        actual_direction,
    ]


def validate_dashboard_payload(payload: Mapping[str, Any]) -> None:
    """校验 Dashboard V6 summary 或 detail 的精确公开合同。"""
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
    _validate_live_target_start_date(payload)

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
            task_type=str(task_type),
            context=f"dashboard scheme[{scheme_index}].monthly_rows",
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
    _validate_live_target_start_date(payload)
    _required_string(payload.get("scheme_id"), field="detail scheme_id")
    _required_month(payload.get("month"), field="detail month")
    source = payload.get("source")
    if source not in {"all", "backtest", "live"}:
        raise DashboardDataError("dashboard detail source is invalid")
    _validate_compact_rows(
        payload.get("rows"),
        source=None if source == "all" else str(source),
        context="dashboard detail rows",
    )


def _validate_live_target_start_date(payload: Mapping[str, Any]) -> None:
    value = _required_payload_iso_date(
        payload.get("live_target_start_date"),
        field="live_target_start_date",
    )
    if value != FACTOR_LAB_LIVE_TARGET_START_DATE.isoformat():
        raise DashboardDataError("dashboard live_target_start_date is invalid")


def _validate_monthly_rows(
    value: Any,
    *,
    task_type: str,
    context: str,
) -> None:
    if not isinstance(value, list):
        raise DashboardDataError(f"{context} must be a list")
    previous: tuple[str, int] | None = None
    seen: set[tuple[str, str]] = set()
    source_rank = {"backtest": 0, "live": 1}
    live_start = FACTOR_LAB_LIVE_TARGET_START_DATE
    if task_type == "monthly_average":
        live_start = date(
            live_start.year + (1 if live_start.month == 12 else 0),
            1 if live_start.month == 12 else live_start.month + 1,
            1,
        )
    live_start_month = live_start.strftime("%Y-%m")
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
                f"{context}[{index}].source does not match target_date policy"
            )
        key = (month, str(source))
        if key in seen:
            raise DashboardDataError(f"{context} has duplicate month/source")
        seen.add(key)
        sort_key = (month, source_rank[str(source)])
        if previous is not None and sort_key < previous:
            raise DashboardDataError(f"{context} is not canonically sorted")
        previous = sort_key
        counts = {
            field: _required_nonnegative_integer(
                detail[field], field=f"{context}[{index}].{field}"
            )
            for field in MONTHLY_ROW_FIELDS[2:]
        }
        if counts["metric_samples"] > counts["samples"]:
            raise DashboardDataError(f"{context}[{index}] counts are inconsistent")
        if counts["correct"] > counts["metric_samples"]:
            raise DashboardDataError(f"{context}[{index}] counts are inconsistent")
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
            raise DashboardDataError(f"{context}[{index}] distributions are inconsistent")


def _validate_compact_rows(
    rows: Any,
    *,
    source: str | None,
    context: str,
) -> None:
    if not isinstance(rows, list):
        raise DashboardDataError(f"{context} must be a list")
    seen_points: set[tuple[str, str]] = set()
    previous_sort_key: tuple[int, str, str] | None = None
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
        if row_source != dashboard_result_source(detail["target_date"]):
            raise DashboardDataError(
                f"{context}[{row_index}].source does not match target_date policy"
            )
        compact_detail_row(detail, source=str(row_source))
        target_date = str(detail["target_date"])
        point = (str(row_source), target_date)
        if point in seen_points:
            raise DashboardDataError(
                f"{context} has duplicate {row_source} prediction point: {target_date}"
            )
        seen_points.add(point)
        sort_key = (
            source_rank[str(row_source)],
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


def _prediction_scheme_key(row: Mapping[str, Any]) -> tuple[str, str, int] | None:
    """预测行的方案键；字段缺失或非法时返回 None，视为不在 active 范围。"""
    try:
        return (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_better_prediction_for_point(
    candidate: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    is_weekly: bool,
) -> bool:
    if is_weekly:
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
        and int(value) in DIRECTION_VALUES
    ):
        return int(value)
    raise DashboardDataError(f"dashboard direction is invalid: {value!r}")
