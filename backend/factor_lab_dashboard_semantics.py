from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Iterator, Mapping

from shared.models import DIRECTION_VALUES
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import (
    PERIOD_AVERAGE_TASK_TYPES,
    PREDICTION_CADENCES,
    TASK_COMBINATIONS,
    WEEKLY_TASK_TYPES,
)


DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v7"
FACTOR_LAB_HISTORY_START_DATE = "2025-01-01"
FACTOR_LAB_LIVE_FEATURE_START_DATE = date(2026, 6, 1)
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
RANGE_ROW_FIELDS = ("source", *MONTHLY_ROW_FIELDS[2:])
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
        """按 Dashboard V7 固定列顺序输出。"""
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


def is_factor_lab_history_visible(feature_date: str) -> bool:
    """判断信号是否位于当前因子实验室展示窗口。"""
    return feature_date >= FACTOR_LAB_HISTORY_START_DATE


def dashboard_result_source(feature_date: Any) -> str:
    """只按特征基准日返回公开的回测/实盘展示口径。"""
    value = _required_iso_date(feature_date, field="feature_date")
    return (
        "live"
        if date.fromisoformat(value) >= FACTOR_LAB_LIVE_FEATURE_START_DATE
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


def map_selected_backtest_runs(
    run_rows: Iterable[Mapping[str, Any]],
    registry_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """将 SQL 已唯一选择的 base 回测映射到 Registry，保留身份一致性检查。"""
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

    by_base: dict[str, Mapping[str, Any]] = {}
    for row in run_rows:
        if row.get("status") != "success":
            continue
        base_scheme_id = _required_text(
            row.get("scheme_id"), field="backtest run scheme_id"
        )
        if base_scheme_id not in runtime_by_base:
            continue
        data_source = _required_text(
            row.get("data_source"), field="backtest run data_source"
        )
        if data_source not in BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE.values():
            continue
        _required_text(row.get("benchmark_id"), field="backtest run benchmark_id")
        _validate_selected_backtest_run(row)
        if base_scheme_id in by_base:
            raise DashboardDataError(
                f"selected backtest run is ambiguous: base_scheme_id={base_scheme_id}"
            )
        by_base[base_scheme_id] = row

    selected: dict[str, Mapping[str, Any]] = {}
    for row in registry:
        status = row.get("status")
        if status not in (None, "active"):
            continue
        scheme_id = _required_text(row.get("scheme_id"), field="registry scheme_id")
        base_scheme_id = _required_text(
            row.get("base_scheme_id"), field="registry base_scheme_id"
        )
        selected_run = by_base.get(base_scheme_id)
        if selected_run is not None:
            selected[scheme_id] = selected_run
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
    """把按需展示明细编码为 V7 固定六列数组。"""
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


def _validate_selected_backtest_run(row: Mapping[str, Any]) -> None:
    """检查选中回测的数据库身份字段，不在 Python 重新排名。"""
    value = row.get("updated_at")
    if isinstance(value, (datetime, date)):
        pass
    elif isinstance(value, str) and value and value == value.strip():
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise DashboardDataError(
                f"backtest run updated_at is invalid: {value!r}"
            ) from exc
    else:
        raise DashboardDataError(
            f"backtest run updated_at is invalid: {value!r}"
        )
    try:
        int(row.get("id"))
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"backtest run id is invalid: {row.get('id')!r}"
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


_VALIDATION_EXPORTS = frozenset(
    {
        "BACKTEST_FIELDS",
        "DETAIL_TOP_LEVEL_FIELDS",
        "GENERATED_AT_PATTERN",
        "MAX_SAFE_JSON_INTEGER",
        "SCHEME_FIELDS",
        "SNAPSHOT_ID_PATTERN",
        "STRUCTURED_BOUNDARY_CODEPOINTS",
        "TOP_LEVEL_FIELDS",
        "VALID_TASK_TYPES",
        "validate_dashboard_payload",
    }
)


def __getattr__(name: str) -> Any:
    """稳定导入面：payload 合同校验已拆到 validation 模块，按需再导出。

    validation 依赖本模块，因此用惰性转发避免循环导入。
    """
    if name in _VALIDATION_EXPORTS:
        from backend import factor_lab_dashboard_validation

        return getattr(factor_lab_dashboard_validation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
