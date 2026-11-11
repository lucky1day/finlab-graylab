from __future__ import annotations

import json
import logging
import os
import stat
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from contextlib import contextmanager
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
    require_unambiguous_registry_scopes,
)
from shared.runtime_paths import RUNTIME_ROOT_ENV, resolve_runtime_state_path

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    DETAIL_ROW_FIELDS,
    FACTOR_LAB_HISTORY_START_DATE,
    FACTOR_LAB_LIVE_FEATURE_START_DATE,
    MONTHLY_ROW_FIELDS,
    RANGE_ROW_FIELDS,
    MonthlySummaryAccumulator,
    DashboardDataError,
    backtest_benchmark_label,
    backtest_data_source_label,
    map_selected_backtest_runs,
    choose_live_prediction_rows,
    registry_task_type_index,
    collapse_actual_facts_with_diagnostics,
    compact_detail_row,
    dashboard_result_source,
    is_factor_lab_history_visible,
    iter_grouped_live_prediction_rows,
    live_actual_selector,
)
from backend.factor_lab_dashboard_dto import (
    _direction_value,
    _iso_date,
    _registry_dto,
    _required_int,
    _required_text,
    _target_dto,
)
from backend.factor_lab_dashboard_encoding import (
    MAX_GZIP_JSON_BYTES,
    MAX_RAW_JSON_BYTES,
    SnapshotEncoding,
    encode_canonical_snapshot,
)
from backend.factor_lab_dashboard_validation import validate_dashboard_payload

# 稳定导入面：编码、DTO 与 payload 校验已拆到同级模块，这里继续再导出。
__all__ = [
    "FACTOR_LAB_HISTORY_START_DATE",
    "MAX_GZIP_JSON_BYTES",
    "MAX_RAW_JSON_BYTES",
    "DashboardDataError",
    "DashboardQueryError",
    "SnapshotEncoding",
    "build_factor_lab_dashboard",
    "build_factor_lab_dashboard_detail",
    "dashboard_build_diagnostics",
    "dashboard_read_connection",
    "encode_canonical_snapshot",
    "parse_dashboard_date_range",
    "parse_dashboard_month",
    "record_dashboard_encoding",
    "validate_production_scheme_ids",
]


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MYSQL_SNAPSHOT_SQL = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
MAX_PRODUCTION_LIST_BYTES = 256 * 1024
logger = logging.getLogger(__name__)
_DIAGNOSTICS_LIMIT = 8
_diagnostics_lock = Lock()
_diagnostics_by_snapshot_id: OrderedDict[str, dict[str, Any]] = OrderedDict()


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


def parse_dashboard_date_range(start_date: object, end_date: object) -> dict[str, str] | None:
    """解析包含首尾日的特征日期区间；不顺延自然日。"""
    if start_date is None and end_date is None:
        return None
    for value in (start_date, end_date):
        if not isinstance(value, str) or len(value) != 10:
            raise DashboardQueryError("dashboard date range is invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise DashboardQueryError("dashboard date range is invalid") from exc
        if parsed.isoformat() != value:
            raise DashboardQueryError("dashboard date range is invalid")
    if start_date > end_date:
        raise DashboardQueryError("dashboard date range is reversed")
    return {"start_date": start_date, "end_date": end_date}


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
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("production scheme list must be a regular file")
            if before.st_size > MAX_PRODUCTION_LIST_BYTES:
                raise ValueError("production scheme list exceeds byte limit")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_PRODUCTION_LIST_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_PRODUCTION_LIST_BYTES:
            raise ValueError("production scheme list exceeds byte limit")
        if (
            len(raw) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ValueError("production scheme list changed during read")
        return validate_production_scheme_ids(json.loads(raw.decode("utf-8")))
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
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """在一致性事务中构建日级区间计数和完整月份计数。"""
    selected_range = parse_dashboard_date_range(start_date, end_date)
    return _build_summary(engine, selected_range=selected_range)


def build_factor_lab_dashboard_detail(
    engine: Engine,
    *,
    scheme_id: str,
    month: str,
    source: str,
) -> dict[str, Any] | None:
    """按 active composite scheme、月份和来源构建 V7 明细。"""
    if source not in {"all", "backtest", "live"}:
        raise DashboardQueryError("dashboard detail source is invalid")
    parsed_month = parse_dashboard_month(month)
    return _build_detail(
        engine,
        registry_scheme_id=scheme_id,
        detail_month=parsed_month,
        detail_source=source,
    )


def _build_summary(
    engine: Engine, *, selected_range: dict[str, str] | None,
) -> dict[str, Any]:
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
        require_unambiguous_registry_scopes(registry_rows)
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
        selected_backtest_runs = map_selected_backtest_runs(
            backtest_run_rows,
            registry_rows,
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
        range_by_scheme: dict[str, dict[str, MonthlySummaryAccumulator]] = {}
        feature_date_bounds: dict[str, dict[str, str] | None] = {
            "all": None, "backtest": None, "live": None,
        }
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
            range_by_scheme[scheme["scheme_id"]] = {}
            source_counts_by_scheme[scheme["scheme_id"]] = {
                "backtest": 0,
                "live": 0,
            }

        history_prediction_rows_excluded = 0
        prediction_rows = iter_summary_product_predictions(
            connection,
            registry_rows,
            stats=prediction_read_stats,
        )
        canonical_predictions = iter_grouped_live_prediction_rows(
            prediction_rows,
            display_until=display_until,
            task_type_by_scheme=registry_task_type_index(registry_rows),
        )
        for prediction in canonical_predictions:
            feature_date = _iso_date(
                prediction.get("feature_date"),
                field="prediction feature_date",
            )
            if not is_factor_lab_history_visible(feature_date):
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
            source = dashboard_result_source(prediction.get("feature_date"))
            for scheme in schemes_by_prediction_key.get(prediction_key, []):
                for bounds_source in ("all", source):
                    bounds = feature_date_bounds[bounds_source]
                    if bounds is None:
                        feature_date_bounds[bounds_source] = {
                            "start_date": feature_date, "end_date": feature_date,
                        }
                    else:
                        bounds["start_date"] = min(bounds["start_date"], feature_date)
                        bounds["end_date"] = max(bounds["end_date"], feature_date)
                range_accumulator = None
                if selected_range is None or (
                    selected_range["start_date"] <= feature_date <= selected_range["end_date"]
                ):
                    range_accumulator = range_by_scheme[scheme["scheme_id"]].setdefault(
                        source, MonthlySummaryAccumulator(),
                    )
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
                month = _display_month(prediction)
                accumulator = monthly_by_scheme[scheme["scheme_id"]].setdefault(
                    (month, source),
                    MonthlySummaryAccumulator(),
                )
                source_counts_by_scheme[scheme["scheme_id"]][source] += 1
                if actual_direction is not None:
                    if range_accumulator is not None:
                        range_accumulator.observe(
                            predicted_direction=prediction.get("predicted_direction"),
                            actual_direction=actual_direction,
                        )
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
                "range_rows": [
                    accumulator.compact(month="", source=source)[1:]
                    for source, accumulator in sorted(
                        range_by_scheme[scheme["scheme_id"]].items()
                    )
                ],
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
        "live_feature_start_date": (
            FACTOR_LAB_LIVE_FEATURE_START_DATE.isoformat()
        ),
        "monthly_row_fields": list(MONTHLY_ROW_FIELDS),
        "range_row_fields": list(RANGE_ROW_FIELDS),
        "feature_date_bounds": feature_date_bounds,
        "selected_feature_range": selected_range,
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
        require_unambiguous_registry_scopes(registry_rows)
        display_date_range = _detail_feature_date_range(
            detail_month,
        )
        feature_date_range = _detail_source_feature_date_range(
            display_date_range,
            source=detail_source,
        )
        prediction_rows = (
            read_product_predictions(
                connection,
                registry_rows,
                feature_date_range=feature_date_range,
            )
            if feature_date_range is not None
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
                    row.get("feature_date"),
                    field="prediction feature_date",
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
                target_date_range=None,
                target_dates=missing_actual_target_dates,
            )
            if missing_actual_target_dates and feature_date_range is not None
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
        source = dashboard_result_source(prediction.get("feature_date"))
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
        "live_feature_start_date": FACTOR_LAB_LIVE_FEATURE_START_DATE.isoformat(),
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


def _display_month(row: Mapping[str, Any]) -> str:
    """所有任务统一归入特征基准日所在自然月。"""
    return _iso_date(row.get("feature_date"), field="feature_date")[:7]


def _detail_feature_date_range(display_month: str | None) -> tuple[str, str]:
    """把展示月份转换为特征基准日半开区间。"""
    if display_month is None:
        raise DashboardQueryError("dashboard detail month is required")
    display_start = date.fromisoformat(
        f"{parse_dashboard_month(display_month)}-01"
    )
    return display_start.isoformat(), _shift_month(display_start, 1).isoformat()


def _detail_source_feature_date_range(
    display_range: tuple[str, str],
    *,
    source: str,
) -> tuple[str, str] | None:
    """将展示 source 分区与特征基准月区间求交。"""
    if source == "all":
        return display_range
    start = date.fromisoformat(display_range[0])
    before = date.fromisoformat(display_range[1])
    if source == "backtest":
        before = min(before, FACTOR_LAB_LIVE_FEATURE_START_DATE)
    elif source == "live":
        start = max(start, FACTOR_LAB_LIVE_FEATURE_START_DATE)
    else:
        raise DashboardQueryError("dashboard detail source is invalid")
    if start >= before:
        return None
    return start.isoformat(), before.isoformat()


def _shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


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


def _compact_row_sort_key(row: list[Any]) -> tuple[int, str, str, str]:
    return (
        {"backtest": 0, "live": 1}[str(row[0])],
        str(row[2]),
        str(row[3]),
        str(row[1]),
    )


