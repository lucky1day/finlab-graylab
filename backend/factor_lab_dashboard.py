from __future__ import annotations

import gzip
import json
import logging
import os
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from backend.db import current_http_request_context
from backend.factor_lab_dashboard_queries import (
    SummaryPredictionReadStats,
    iter_summary_product_predictions,
    read_active_registry,
    read_active_targets,
    read_live_actuals,
    read_product_predictions,
    read_selected_backtest_runs,
    resolve_dashboard_history_replacements,
)
from shared.runtime_paths import RUNTIME_ROOT_ENV, resolve_runtime_state_path
from shared.scheme_config_schema import normalize_scheme_owner

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    DETAIL_ROW_FIELDS,
    FACTOR_LAB_HISTORY_START_DATE,
    FACTOR_LAB_LIVE_TARGET_START_DATE,
    MONTHLY_ROW_FIELDS,
    MonthlySummaryAccumulator,
    DashboardDataError,
    backtest_benchmark_label,
    backtest_data_source_label,
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
    registry_task_type_index,
    collapse_actual_facts_with_diagnostics,
    compact_detail_row,
    dashboard_result_source,
    is_factor_lab_history_visible,
    iter_grouped_live_prediction_rows,
    live_actual_selector,
    validate_dashboard_payload,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MYSQL_SNAPSHOT_SQL = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
MAX_RAW_JSON_BYTES = 1_500_000
MAX_GZIP_JSON_BYTES = 100_000
logger = logging.getLogger(__name__)
_DIAGNOSTICS_LIMIT = 8
_diagnostics_lock = Lock()
_diagnostics_by_snapshot_id: OrderedDict[str, dict[str, Any]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class SnapshotEncoding:
    """Canonical snapshot 的 identity/gzip 编码及其精确字节数。"""

    raw_body: bytes
    gzip_body: bytes
    raw_size: int
    gzip_size: int


class DashboardQueryError(ValueError):
    """表示可归因于 Dashboard 请求参数的稳定输入错误。"""


def parse_dashboard_month(value: object) -> str:
    """解析可安全计算相邻月边界的规范 ``YYYY-MM``。"""
    if (
        not isinstance(value, str)
        or len(value) != 7
        or value[4] != "-"
        or not value[:4].isascii()
        or not value[:4].isdigit()
        or not value[5:].isascii()
        or not value[5:].isdigit()
    ):
        raise DashboardQueryError("dashboard detail month is invalid")
    try:
        display_start = date.fromisoformat(f"{value}-01")
        _shift_month(display_start, -1)
        _shift_month(display_start, 1)
    except ValueError as exc:
        raise DashboardQueryError(
            "dashboard detail month boundary is invalid"
        ) from exc
    canonical = f"{display_start.year:04d}-{display_start.month:02d}"
    if canonical != value:
        raise DashboardQueryError("dashboard detail month is invalid")
    return value


@contextmanager
def dashboard_read_connection(engine: Engine) -> Iterator[Connection]:
    """返回一次 dashboard 构建独占的同一只读一致性连接。"""
    request_context = current_http_request_context()
    if request_context is not None:
        request_context.failure_stage = "dashboard_pool_acquire"
        request_context.require_remaining("dashboard_pool_acquire")
        acquire_started_at = request_context.clock()
    else:
        acquire_started_at = None
    try:
        connection = engine.connect()
    finally:
        if request_context is not None and acquire_started_at is not None:
            request_context.pool_acquire_seconds += max(
                0.0,
                request_context.clock() - acquire_started_at,
            )
    transaction = None
    is_mysql = engine.dialect.name == "mysql"
    try:
        if is_mysql:
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            if request_context is not None:
                request_context.failure_stage = "dashboard_snapshot"
                request_context.require_remaining("dashboard_snapshot")
                db_started_at = request_context.clock()
            else:
                db_started_at = None
            try:
                connection.exec_driver_sql(MYSQL_SNAPSHOT_SQL)
            finally:
                if request_context is not None and db_started_at is not None:
                    request_context.db_seconds += max(
                        0.0,
                        request_context.clock() - db_started_at,
                    )
        else:
            transaction = connection.begin()
        yield connection
    finally:
        try:
            if is_mysql and not connection.invalidated:
                connection.rollback()
            elif transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            connection.close()


def validate_production_scheme_ids(payload: object) -> set[str]:
    """校验运维完整名单；不推断或扩展 Registry 身份。"""
    if not isinstance(payload, dict) or set(payload) - {"scheme_ids"}:
        raise ValueError("expected an object containing only scheme_ids")
    members = payload.get("scheme_ids", [])
    if not isinstance(members, list) or any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in members
    ):
        raise ValueError("scheme_ids must be nonempty trimmed strings in an array")
    if len(set(members)) != len(members):
        raise ValueError("scheme_ids contains duplicates")
    return set(members)


def _read_production_scheme_ids() -> set[str]:
    """每次 Summary 独立读取外置名单；文件错误只撤下标记。"""
    try:
        raw_root = os.environ.get(RUNTIME_ROOT_ENV, "")
        root = Path(raw_root)
        if not raw_root or not root.is_absolute() or not root.is_dir():
            raise ValueError("BFL_RUNTIME_ROOT must name an existing absolute directory")
        path = resolve_runtime_state_path(
            relative_path="config/production_schemes.json",
            development_default=root / "config/production_schemes.json",
        )
        return validate_production_scheme_ids(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("production_schemes_invalid: %s", exc)
        return set()


def _existing_production_scheme_ids(connection: Connection, candidates: set[str]) -> set[str]:
    """在本次只读事务内批量核验存在性；数据库错误向上抛出。"""
    if not candidates:
        return set()
    query = text(
        "SELECT scheme_id FROM t_scheme_registry WHERE scheme_id IN :scheme_ids"
    ).bindparams(bindparam("scheme_ids", expanding=True))
    request_context = current_http_request_context()
    if request_context is not None:
        request_context.failure_stage = "dashboard_query"
        request_context.require_remaining("dashboard_query")
        db_started_at = request_context.clock()
    else:
        db_started_at = None
    try:
        existing = set(
            connection.execute(
                query,
                {"scheme_ids": sorted(candidates)},
            ).scalars()
        )
    finally:
        if request_context is not None and db_started_at is not None:
            request_context.db_seconds += max(
                0.0,
                request_context.clock() - db_started_at,
            )
    for scheme_id in sorted(candidates - existing):
        logger.error("production_scheme_not_found: %r", scheme_id)
    return existing


def build_factor_lab_dashboard(
    engine: Engine,
) -> dict[str, Any]:
    """在一个一致性事务内构建不含逐日明细的 V6 首屏。"""
    return _build_summary(engine)


def build_factor_lab_dashboard_detail(
    engine: Engine,
    *,
    scheme_id: str,
    month: str,
    source: str,
) -> dict[str, Any] | None:
    """按 active composite scheme、月份和来源构建 V6 明细。"""
    if source not in {"all", "backtest", "live"}:
        raise DashboardQueryError("dashboard detail source is invalid")
    parsed_month = parse_dashboard_month(month)
    return _build_detail(
        engine,
        registry_scheme_id=scheme_id,
        detail_month=parsed_month,
        detail_source=source,
    )


def _build_summary(engine: Engine) -> dict[str, Any]:
    """流式读取全量产品事实，构建不随历史明细等比例占内存的 Summary。"""
    build_started_at = time.perf_counter()
    captured = datetime.now(SHANGHAI_TIMEZONE)
    display_until = captured.date().isoformat()

    production_candidates = _read_production_scheme_ids()
    db_read_started_at = time.perf_counter()
    prediction_read_stats = SummaryPredictionReadStats()
    with dashboard_read_connection(engine) as connection:
        production_ids = _existing_production_scheme_ids(
            connection,
            production_candidates,
        )
        registry_rows = read_active_registry(
            connection,
            registry_scheme_id=None,
        )
        replacement_plan = resolve_dashboard_history_replacements(
            connection,
            registry_rows,
        )
        target_rows = read_active_targets(connection)
        actual_rows = read_live_actuals(
            connection,
            registry_rows,
            target_date_range=None,
        )
        backtest_run_rows = read_selected_backtest_runs(
            connection,
            registry_rows,
        )
        selected_backtest_runs = choose_latest_backtest_runs(
            backtest_run_rows,
            registry_rows,
        )
        selected_backtest_runs.update(
            replacement_plan.backtest_runs_by_registry
        )
        request_context = current_http_request_context()
        if request_context is not None:
            request_context.failure_stage = "dashboard_canonical"
            request_context.require_remaining("dashboard_canonical")

        canonical_started_at = time.perf_counter()
        registry = [_registry_dto(row) for row in registry_rows]
        targets = [_target_dto(row) for row in target_rows]
        target_labels = {
            target["target_code"]: target["display_name"] for target in targets
        }
        for scheme in registry:
            if scheme["target_tenor"] not in target_labels:
                raise DashboardDataError(
                    "active registry target is missing from active target registry: "
                    f"scheme_id={scheme['scheme_id']} "
                    f"target_tenor={scheme['target_tenor']}"
                )

        active_actual_scopes = {
            (scheme["target_tenor"], *live_actual_selector(scheme["task_type"]))
            for scheme in registry
        }
        actual_facts, actual_diagnostics = _collapse_actual_rows(
            actual_rows,
            active_actual_scopes=active_actual_scopes,
        )
        schemes_by_prediction_key: dict[
            tuple[str, str, int], list[dict[str, Any]]
        ] = defaultdict(list)
        monthly_by_scheme: dict[
            str,
            dict[tuple[str, str], MonthlySummaryAccumulator],
        ] = {}
        source_counts_by_scheme: dict[str, dict[str, int]] = {}
        for scheme in registry:
            schemes_by_prediction_key[
                (
                    scheme["base_scheme_id"],
                    scheme["target_tenor"],
                    scheme["horizon"],
                )
            ].append(scheme)
            monthly_by_scheme[scheme["scheme_id"]] = {}
            source_counts_by_scheme[scheme["scheme_id"]] = {
                "backtest": 0,
                "live": 0,
            }

        history_prediction_rows_excluded = 0
        prediction_rows = iter_summary_product_predictions(
            connection,
            registry_rows,
            stats=prediction_read_stats,
            replacement_plan=replacement_plan,
        )
        canonical_predictions = iter_grouped_live_prediction_rows(
            prediction_rows,
            display_until=display_until,
            task_type_by_scheme=registry_task_type_index(registry_rows),
        )
        for prediction in canonical_predictions:
            predict_date = _iso_date(
                prediction.get("predict_date"),
                field="prediction predict_date",
            )
            if not is_factor_lab_history_visible(predict_date):
                history_prediction_rows_excluded += 1
                continue
            prediction_key = (
                _required_text(
                    prediction.get("scheme_id"), field="prediction scheme_id"
                ),
                _required_text(
                    prediction.get("target_tenor"),
                    field="prediction target_tenor",
                ),
                _required_int(
                    prediction.get("horizon"), field="prediction horizon"
                ),
            )
            target_date = _iso_date(
                prediction.get("target_date"),
                field="prediction target_date",
            )
            source = dashboard_result_source(target_date)
            for scheme in schemes_by_prediction_key.get(prediction_key, []):
                selector = live_actual_selector(scheme["task_type"])
                published_actual = prediction.get("backtest_actual_direction")
                actual_direction = (
                    _direction_value(
                        published_actual,
                        field="backtest_actual_direction",
                    )
                    if published_actual is not None
                    else actual_facts.get(selector, {}).get(
                        (scheme["target_tenor"], target_date, selector[1])
                    )
                )
                month = _display_month(prediction, scheme["task_type"])
                accumulator = monthly_by_scheme[scheme["scheme_id"]].setdefault(
                    (month, source),
                    MonthlySummaryAccumulator(),
                )
                source_counts_by_scheme[scheme["scheme_id"]][source] += 1
                if actual_direction is not None:
                    accumulator.observe(
                        predicted_direction=prediction.get("predicted_direction"),
                        actual_direction=actual_direction,
                    )
        if request_context is not None:
            request_context.failure_stage = "dashboard_canonical"
            request_context.require_remaining("dashboard_canonical")
        canonical_elapsed = time.perf_counter() - canonical_started_at

    db_read_seconds = (
        time.perf_counter() - db_read_started_at - canonical_elapsed
        + prediction_read_stats.fetch_seconds
    )
    canonical_build_seconds = max(
        0.0,
        canonical_elapsed - prediction_read_stats.fetch_seconds,
    )

    schemes: list[dict[str, Any]] = []
    live_row_count = 0
    backtest_row_count = 0
    for scheme in registry:
        selected_run = selected_backtest_runs.get(scheme["scheme_id"])
        backtest = None
        if selected_run is not None:
            benchmark_id = _required_text(
                selected_run.get("benchmark_id"),
                field="selected backtest benchmark_id",
            )
            data_source = _required_text(
                selected_run.get("data_source"),
                field="selected backtest data_source",
            )
            backtest = {
                "benchmark_id": benchmark_id,
                "benchmark_label": backtest_benchmark_label(benchmark_id),
                "data_source": data_source,
                "data_source_label": backtest_data_source_label(data_source),
                "latest_run_date": _iso_date(
                    selected_run.get("end_date"),
                    field="selected backtest end_date",
                ),
            }

        backtest_row_count += source_counts_by_scheme[scheme["scheme_id"]][
            "backtest"
        ]
        live_row_count += source_counts_by_scheme[scheme["scheme_id"]]["live"]

        schemes.append(
            {
                **scheme,
                "is_production": scheme["scheme_id"] in production_ids,
                "target_label": target_labels[scheme["target_tenor"]],
                "monthly_rows": _compact_monthly_accumulators(
                    monthly_by_scheme[scheme["scheme_id"]],
                ),
                "backtest": backtest,
            }
        )

    schemes.sort(key=lambda item: item["scheme_id"])
    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "representation": "summary",
        "snapshot_id": uuid4().hex,
        "generated_at": captured.isoformat(timespec="seconds"),
        "display_until": display_until,
        "live_target_start_date": (
            FACTOR_LAB_LIVE_TARGET_START_DATE.isoformat()
        ),
        "monthly_row_fields": list(MONTHLY_ROW_FIELDS),
        "target_labels": target_labels,
        "schemes": schemes,
    }
    validate_dashboard_payload(payload)
    _record_build_diagnostics(
        payload["snapshot_id"],
        {
            "snapshot_id": payload["snapshot_id"],
            "db_read_seconds": db_read_seconds,
            "canonical_build_seconds": canonical_build_seconds,
            "build_seconds": time.perf_counter() - build_started_at,
            "representation": "summary",
            "scheme_count": len(schemes),
            "live_row_count": live_row_count,
            "backtest_row_count": backtest_row_count,
            "detail_row_count": live_row_count + backtest_row_count,
            "prediction_source_row_count": prediction_read_stats.source_rows,
            "prediction_cleanup_seconds": prediction_read_stats.cleanup_seconds,
            "prediction_exit_reason": prediction_read_stats.exit_reason,
            "history_prediction_rows_excluded_before_policy_start": (
                history_prediction_rows_excluded
            ),
            **actual_diagnostics,
        },
    )
    return payload


def _build_detail(
    engine: Engine,
    *,
    registry_scheme_id: str,
    detail_month: str,
    detail_source: str,
) -> dict[str, Any] | None:
    """只读取一个 active 方案、展示月和产品来源分区的明细事实。"""
    build_started_at = time.perf_counter()
    captured = datetime.now(SHANGHAI_TIMEZONE)
    display_until = captured.date().isoformat()
    db_read_started_at = time.perf_counter()
    with dashboard_read_connection(engine) as connection:
        registry_rows = read_active_registry(
            connection,
            registry_scheme_id=registry_scheme_id,
        )
        if not registry_rows:
            return None
        scheme = _registry_dto(registry_rows[0])
        replacement_plan = resolve_dashboard_history_replacements(
            connection,
            registry_rows,
        )
        display_date_range = _detail_target_date_range(
            detail_month,
            task_type=scheme["task_type"],
        )
        target_date_range = _detail_source_target_date_range(
            display_date_range,
            source=detail_source,
        )
        prediction_rows = (
            read_product_predictions(
                connection,
                registry_rows,
                target_date_range=target_date_range,
                replacement_plan=replacement_plan,
            )
            if target_date_range is not None
            else []
        )
        canonical_predictions = choose_live_prediction_rows(
            prediction_rows,
            display_until=display_until,
            task_type_by_scheme=registry_task_type_index(registry_rows),
        )
        visible_predictions = [
            row
            for row in canonical_predictions
            if is_factor_lab_history_visible(
                _iso_date(
                    row.get("predict_date"),
                    field="prediction predict_date",
                )
            )
        ]
        missing_actual_target_dates = {
            _iso_date(row.get("target_date"), field="prediction target_date")
            for row in visible_predictions
            if row.get("backtest_actual_direction") is None
        }
        actual_rows = (
            read_live_actuals(
                connection,
                registry_rows,
                target_date_range=target_date_range,
                target_dates=missing_actual_target_dates,
            )
            if missing_actual_target_dates and target_date_range is not None
            else []
        )
    db_read_seconds = time.perf_counter() - db_read_started_at
    request_context = current_http_request_context()
    if request_context is not None:
        request_context.failure_stage = "dashboard_canonical"
        request_context.require_remaining("dashboard_canonical")

    canonical_started_at = time.perf_counter()
    selector = live_actual_selector(scheme["task_type"])
    actual_facts, actual_diagnostics = _collapse_actual_rows(
        actual_rows,
        active_actual_scopes={
            (scheme["target_tenor"], *selector),
        },
    )
    rows: list[list[Any]] = []
    for prediction in visible_predictions:
        target_date = _iso_date(
            prediction.get("target_date"),
            field="prediction target_date",
        )
        source = dashboard_result_source(target_date)
        if detail_source != "all" and source != detail_source:
            raise DashboardDataError(
                "detail prediction escaped requested source partition"
            )
        published_actual = prediction.get("backtest_actual_direction")
        actual_direction = (
            _direction_value(
                published_actual,
                field="backtest_actual_direction",
            )
            if published_actual is not None
            else actual_facts.get(selector, {}).get(
                (
                    scheme["target_tenor"],
                    target_date,
                    selector[1],
                )
            )
        )
        detail = dict(prediction)
        detail["actual_direction"] = actual_direction
        rows.append(compact_detail_row(detail, source=source))

    rows.sort(key=_compact_row_sort_key)
    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "representation": "detail",
        "snapshot_id": uuid4().hex,
        "generated_at": captured.isoformat(timespec="seconds"),
        "display_until": display_until,
        "live_target_start_date": FACTOR_LAB_LIVE_TARGET_START_DATE.isoformat(),
        "scheme_id": scheme["scheme_id"],
        "month": detail_month,
        "source": detail_source,
        "row_fields": list(DETAIL_ROW_FIELDS),
        "rows": rows,
    }
    validate_dashboard_payload(payload)
    _record_build_diagnostics(
        payload["snapshot_id"],
        {
            "snapshot_id": payload["snapshot_id"],
            "representation": "detail",
            "db_read_seconds": db_read_seconds,
            "canonical_build_seconds": (
                time.perf_counter() - canonical_started_at
            ),
            "build_seconds": time.perf_counter() - build_started_at,
            "scheme_count": 1,
            "detail_row_count": len(rows),
            **actual_diagnostics,
        },
    )
    return payload


def dashboard_build_diagnostics(snapshot_id: str) -> dict[str, Any] | None:
    """返回指定成功快照的有界构建诊断副本。"""
    with _diagnostics_lock:
        diagnostics = _diagnostics_by_snapshot_id.get(snapshot_id)
        return None if diagnostics is None else deepcopy(diagnostics)


def record_dashboard_encoding(
    snapshot_id: str,
    encoding: SnapshotEncoding,
) -> None:
    """把 route 唯一一次编码的字节数并入已有诊断。"""
    with _diagnostics_lock:
        diagnostics = _diagnostics_by_snapshot_id.get(snapshot_id)
        if diagnostics is None:
            return
        diagnostics["raw_bytes"] = encoding.raw_size
        diagnostics["gzip_bytes"] = encoding.gzip_size


def _monthly_rows(
    *,
    backtest_details: list[Mapping[str, Any]],
    live_details: list[Mapping[str, Any]],
    task_type: str,
) -> list[list[Any]]:
    grouped: dict[
        tuple[str, str], MonthlySummaryAccumulator
    ] = {}
    for source, rows in (
        ("backtest", backtest_details),
        ("live", live_details),
    ):
        for row in rows:
            # 保留纯待验证月份的明细入口，统计仍只使用已验证记录。
            bucket = grouped.setdefault(
                (_display_month(row, task_type), source),
                MonthlySummaryAccumulator(),
            )
            if row.get("actual_direction") is None:
                continue
            bucket.observe(
                predicted_direction=row.get("predicted_direction"),
                actual_direction=row.get("actual_direction"),
            )

    return _compact_monthly_accumulators(grouped)


def _compact_monthly_accumulators(
    grouped: Mapping[
        tuple[str, str], MonthlySummaryAccumulator
    ],
) -> list[list[Any]]:
    """稳定排序并输出 Summary 月份计数。"""

    result: list[list[Any]] = []
    source_rank = {"backtest": 0, "live": 1}
    for (month, source), accumulator in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], source_rank[item[0][1]]),
    ):
        result.append(accumulator.compact(month=month, source=source))
    return result


def _display_month(row: Mapping[str, Any], task_type: str) -> str:
    target_date = date.fromisoformat(
        _iso_date(row.get("target_date"), field="target_date")
    )
    if task_type != "monthly_average":
        return target_date.strftime("%Y-%m")
    year = target_date.year + (1 if target_date.month == 12 else 0)
    month = 1 if target_date.month == 12 else target_date.month + 1
    return f"{year:04d}-{month:02d}"


def _detail_target_date_range(
    display_month: str | None,
    *,
    task_type: str,
) -> tuple[str, str]:
    """把展示月份收敛为底层 target_date 半开区间。"""
    if display_month is None:
        raise DashboardQueryError("dashboard detail month is required")
    display_start = date.fromisoformat(
        f"{parse_dashboard_month(display_month)}-01"
    )

    target_start = (
        _shift_month(display_start, -1)
        if task_type == "monthly_average"
        else display_start
    )
    return target_start.isoformat(), _shift_month(target_start, 1).isoformat()


def _detail_source_target_date_range(
    display_range: tuple[str, str],
    *,
    source: str,
) -> tuple[str, str] | None:
    """将产品 source 分区与展示月 target_date 区间求交。"""
    if source == "all":
        return display_range
    start = date.fromisoformat(display_range[0])
    before = date.fromisoformat(display_range[1])
    if source == "backtest":
        before = min(before, FACTOR_LAB_LIVE_TARGET_START_DATE)
    elif source == "live":
        start = max(start, FACTOR_LAB_LIVE_TARGET_START_DATE)
    else:
        raise DashboardQueryError("dashboard detail source is invalid")
    if start >= before:
        return None
    return start.isoformat(), before.isoformat()


def _shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _direction_value(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc
    if result not in {-1, 0, 1} or result != value:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result


def _record_build_diagnostics(
    snapshot_id: str,
    diagnostics: Mapping[str, Any],
) -> None:
    """线程安全保存最近少量成功快照的构建诊断。"""
    with _diagnostics_lock:
        _diagnostics_by_snapshot_id[snapshot_id] = deepcopy(dict(diagnostics))
        _diagnostics_by_snapshot_id.move_to_end(snapshot_id)
        while len(_diagnostics_by_snapshot_id) > _DIAGNOSTICS_LIMIT:
            _diagnostics_by_snapshot_id.popitem(last=False)


def _collapse_actual_rows(
    actual_rows: list[Mapping[str, Any]],
    *,
    active_actual_scopes: set[tuple[str, str, str]],
) -> tuple[
    dict[tuple[str, str], dict[tuple[str, str, str], int | None]],
    dict[str, dict[str, int]],
]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in actual_rows:
        candidate_scope = (
            row.get("target_tenor"),
            row.get("actual_kind"),
            row.get("target_rule"),
        )
        if candidate_scope not in active_actual_scopes:
            continue

        _required_text(row.get("target_tenor"), field="target_tenor")
        actual_kind = _required_text(row.get("actual_kind"), field="actual_kind")
        target_rule = _required_text(row.get("target_rule"), field="target_rule")
        grouped[(actual_kind, target_rule)].append(row)

    duplicates_folded = {
        "daily": 0,
        "weekly": 0,
        "monthly": 0,
        "period_average": 0,
    }
    direction_conflicts = {
        "daily": 0,
        "weekly": 0,
        "monthly": 0,
        "period_average": 0,
    }
    facts_by_selector: dict[
        tuple[str, str],
        dict[tuple[str, str, str], int | None],
    ] = {}
    for selector, rows in grouped.items():
        frequency = _actual_frequency(selector[0])
        try:
            result = collapse_actual_facts_with_diagnostics(
                rows,
                fact_name=f"{selector[0]} actuals",
                frequency=frequency,
            )
        except DashboardDataError as exc:
            conflict_locator = exc.diagnostics.get(
                "actual_conflict_locator"
            )
            if not isinstance(conflict_locator, Mapping):
                raise
            duplicates_folded[frequency] += int(
                exc.diagnostics.get("same_direction_duplicates_folded", 0)
            )
            direction_conflicts[frequency] += int(
                exc.diagnostics.get("direction_conflicts", 0)
            )
            diagnostics = {
                "actual_same_direction_duplicates_folded": duplicates_folded,
                "actual_direction_conflicts": direction_conflicts,
                "actual_conflict_locator": conflict_locator,
            }
            _log_actual_conflict(diagnostics)
            raise DashboardDataError(
                str(exc),
                diagnostics=diagnostics,
            ) from exc
        facts_by_selector[selector] = result.facts
        duplicates_folded[frequency] += (
            result.same_direction_duplicates_folded
        )
        direction_conflicts[frequency] += result.direction_conflicts

    return facts_by_selector, {
        "actual_same_direction_duplicates_folded": duplicates_folded,
        "actual_direction_conflicts": direction_conflicts,
    }


def _log_actual_conflict(diagnostics: Mapping[str, Any]) -> None:
    """记录一次不含方向值、异常文本或原始行的冲突事件。"""
    event = {
        "actual_same_direction_duplicates_folded": dict(
            diagnostics["actual_same_direction_duplicates_folded"]
        ),
        "actual_direction_conflicts": dict(
            diagnostics["actual_direction_conflicts"]
        ),
        "actual_conflict_locator": dict(
            diagnostics["actual_conflict_locator"]
        ),
    }
    logger.error(
        "factor_lab_dashboard_actual_conflict %s",
        json.dumps(
            event,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        extra={"dashboard_event": event},
    )


def _actual_frequency(actual_kind: str) -> str:
    """把内部 actual selector 收敛为安全的固定诊断标签。"""
    if actual_kind in {"daily_1d", "daily_5d"}:
        return "daily"
    if actual_kind in {"weekly", "monthly", "period_average"}:
        return actual_kind
    raise DashboardDataError(
        f"dashboard actual_kind is invalid: {actual_kind!r}"
    )


def _registry_dto(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    scheme_id = _required_text(row.get("scheme_id"), field="registry scheme_id")
    base_scheme_id = _required_text(
        row.get("base_scheme_id"), field="registry base_scheme_id"
    )
    target_tenor = _required_text(
        row.get("target_tenor"), field="registry target_tenor"
    )
    task_type = row.get("task_type")
    live_actual_selector(task_type)
    deployed_at = _iso_date(row.get("deployed_at"), field="registry deployed_at")
    if row.get("status") != "active":
        raise DashboardDataError(
            f"dashboard registry row is not active: scheme_id={scheme_id}"
        )
    return {
        "scheme_id": scheme_id,
        "base_scheme_id": base_scheme_id,
        "name": _required_text(row.get("name"), field="registry name"),
        "owner": _required_owner(row.get("owner")),
        "description": str(row.get("description") or ""),
        "horizon": _required_int(row.get("horizon"), field="registry horizon"),
        "task_type": task_type,
        "frequency": _required_text(
            row.get("frequency"), field="registry frequency"
        ),
        "target_tenor": target_tenor,
        "status": "active",
        "deployed_at": deployed_at,
    }


def _target_dto(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_code": _required_text(
            row.get("target_code"), field="target target_code"
        ),
        "display_name": _required_text(
            row.get("display_name"), field="target display_name"
        ),
        "asset_class": str(row.get("asset_class") or ""),
        "target_type": str(row.get("target_type") or ""),
        "sort_order": _required_int(row.get("sort_order"), field="target sort_order"),
        "status": _required_text(row.get("status"), field="target status"),
        "extra": _json_object(row.get("extra")),
    }


def _compact_row_sort_key(row: list[Any]) -> tuple[int, str, str]:
    return (
        {"backtest": 0, "live": 1}[str(row[0])],
        str(row[3]),
        str(row[1]),
    )


def encode_canonical_snapshot(payload: Mapping[str, Any]) -> SnapshotEncoding:
    """按未来 route 的固定参数编码 canonical snapshot 并校验字节预算。"""
    raw_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw_body) > MAX_RAW_JSON_BYTES:
        raise DashboardDataError(
            "dashboard raw JSON exceeds budget: "
            f"bytes={len(raw_body)} limit={MAX_RAW_JSON_BYTES}"
        )

    gzip_body = gzip.compress(raw_body, compresslevel=6)
    if len(gzip_body) > MAX_GZIP_JSON_BYTES:
        raise DashboardDataError(
            "dashboard gzip JSON exceeds budget: "
            f"bytes={len(gzip_body)} limit={MAX_GZIP_JSON_BYTES}"
        )
    return SnapshotEncoding(
        raw_body=raw_body,
        gzip_body=gzip_body,
        raw_size=len(raw_body),
        gzip_size=len(gzip_body),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DashboardDataError(f"dashboard JSON object is invalid: {value!r}") from exc
    if not isinstance(parsed, dict):
        raise DashboardDataError(f"dashboard JSON object is invalid: {value!r}")
    return parsed


def _required_text(value: Any, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    return result


def _required_owner(value: Any) -> str:
    try:
        return normalize_scheme_owner(value)
    except ValueError as exc:
        raise DashboardDataError(
            f"dashboard registry owner is invalid: {value!r}"
        ) from exc


def _required_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"dashboard {field} is invalid: {value!r}"
        ) from exc


def _iso_date(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise DashboardDataError(
                f"dashboard {field} is invalid: {value!r}"
            ) from exc
        if parsed.isoformat() == value:
            return value
    raise DashboardDataError(f"dashboard {field} is invalid: {value!r}")
