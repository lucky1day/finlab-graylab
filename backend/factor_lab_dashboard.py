from __future__ import annotations

import gzip
import json
import logging
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock
from typing import Any, Iterator, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from shared.scheme_config_schema import normalize_scheme_owner

from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
    DASHBOARD_SCHEMA_VERSION,
    DETAIL_ROW_FIELDS,
    FACTOR_LAB_HISTORY_START_DATE,
    FACTOR_LAB_LIVE_TARGET_START_DATE,
    MONTHLY_ROW_FIELDS,
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
    live_actual_selector,
    validate_dashboard_payload,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MYSQL_SNAPSHOT_SQL = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
MAX_RAW_JSON_BYTES = 1_500_000
MAX_GZIP_JSON_BYTES = 100_000
# These source-row caps are corruption/resource guards, not business pagination.
# Every query reads at most cap + 1 rows and fails instead of truncating.
MAX_REGISTRY_SOURCE_ROWS = 1_000
MAX_TARGET_SOURCE_ROWS = 1_000
MAX_PRODUCT_PREDICTION_SOURCE_ROWS = 100_000
MAX_ACTUAL_SOURCE_ROWS = 80_000
MAX_BACKTEST_RUN_SOURCE_ROWS = 100_000
_ACTUAL_SOURCE_KIND_ORDER = (
    "daily_1d",
    "daily_5d",
    "weekly",
    "monthly",
    "period_average",
)
_ACTUAL_SOURCE_FRAGMENTS = {
    "daily_1d": """
        SELECT 'daily_1d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_1d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :daily_1d_tenors
          AND trade_date >= :history_start_date
    """,
    "daily_5d": """
        SELECT 'daily_5d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_5d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :daily_5d_tenors
          AND trade_date >= :history_start_date
    """,
    "weekly": """
        SELECT 'weekly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_weekly AS actual_direction
        FROM t_scheme_weekly_actuals
        WHERE (tenor, target_rule) IN :weekly_scopes
          AND target_date >= :history_start_date
    """,
    "monthly": """
        SELECT 'monthly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_monthly AS actual_direction
        FROM t_scheme_monthly_actuals
        WHERE (tenor, target_rule) IN :monthly_scopes
          AND target_date >= :history_start_date
    """,
    "period_average": """
        SELECT 'period_average' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               actual_direction
        FROM t_scheme_period_average_actuals
        WHERE (tenor, target_rule) IN :period_average_scopes
          AND target_date >= :history_start_date
    """,
}
_ACTUAL_SOURCE_SCOPE_PARAMS = {
    "daily_1d": "daily_1d_tenors",
    "daily_5d": "daily_5d_tenors",
    "weekly": "weekly_scopes",
    "monthly": "monthly_scopes",
    "period_average": "period_average_scopes",
}
_ACTUAL_SOURCE_DATE_FILTERS = {
    "daily_1d": (
        " AND trade_date >= :target_date_from"
        " AND trade_date < :target_date_before"
    ),
    "daily_5d": (
        " AND trade_date >= :target_date_from"
        " AND trade_date < :target_date_before"
    ),
    "weekly": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
    "monthly": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
    "period_average": (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
    ),
}
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


@contextmanager
def dashboard_read_connection(engine: Engine) -> Iterator[Connection]:
    """返回一次 dashboard 构建独占的同一只读一致性连接。"""
    connection = engine.connect()
    transaction = None
    is_mysql = engine.dialect.name == "mysql"
    try:
        if is_mysql:
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            connection.exec_driver_sql(MYSQL_SNAPSHOT_SQL)
        else:
            transaction = connection.begin()
        yield connection
    finally:
        try:
            if is_mysql:
                connection.rollback()
            elif transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            connection.close()


def build_factor_lab_dashboard(
    engine: Engine,
) -> dict[str, Any]:
    """在一个一致性事务内构建不含逐日明细的 V5 首屏。"""
    return _build_dashboard_representation(engine)


def build_factor_lab_dashboard_detail(
    engine: Engine,
    *,
    scheme_id: str,
    month: str,
    source: str,
) -> dict[str, Any] | None:
    """按 active composite scheme、月份和来源构建 V5 明细。"""
    if source not in {"all", "backtest", "live"}:
        raise ValueError("dashboard detail source is invalid")
    if not isinstance(month, str) or len(month) != 7:
        raise ValueError("dashboard detail month is invalid")
    return _build_dashboard_representation(
        engine,
        registry_scheme_id=scheme_id,
        detail_month=month,
        detail_source=source,
    )


def _build_dashboard_representation(
    engine: Engine,
    *,
    registry_scheme_id: str | None = None,
    detail_month: str | None = None,
    detail_source: str | None = None,
) -> dict[str, Any] | None:
    build_started_at = time.perf_counter()
    captured = datetime.now(SHANGHAI_TIMEZONE)
    display_until = captured.date().isoformat()

    db_read_started_at = time.perf_counter()
    with dashboard_read_connection(engine) as connection:
        registry_rows = _read_active_registry(
            connection,
            registry_scheme_id=registry_scheme_id,
        )
        if registry_scheme_id is not None and not registry_rows:
            return None
        detail_date_range = (
            _detail_target_date_range(
                detail_month,
                task_type=str(registry_rows[0]["task_type"]),
            )
            if registry_scheme_id is not None
            else None
        )
        target_rows = (
            _read_active_targets(connection)
            if registry_scheme_id is None
            else []
        )
        prediction_rows = _read_product_predictions(
            connection,
            registry_rows,
            target_date_range=detail_date_range,
        )
        actual_rows = _read_live_actuals(
            connection,
            registry_rows,
            target_date_range=detail_date_range,
        )
        backtest_run_rows = _read_backtest_runs(
            connection,
            registry_rows,
        )
        selected_backtest_runs = choose_latest_backtest_runs(
            backtest_run_rows,
            registry_rows,
        )
    db_read_seconds = time.perf_counter() - db_read_started_at

    canonical_started_at = time.perf_counter()
    registry = [_registry_dto(row) for row in registry_rows]
    targets = [_target_dto(row) for row in target_rows]
    target_labels = {
        target["target_code"]: target["display_name"] for target in targets
    }
    if registry_scheme_id is None:
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
    canonical_predictions = choose_live_prediction_rows(
        prediction_rows,
        display_until=display_until,
        task_type_by_scheme=registry_task_type_index(registry_rows),
    )
    history_prediction_rows_excluded = 0
    predictions_by_scheme: dict[tuple[str, str, int], list[Mapping[str, Any]]] = (
        defaultdict(list)
    )
    for row in canonical_predictions:
        predict_date = _iso_date(
            row.get("predict_date"),
            field="prediction predict_date",
        )
        if not is_factor_lab_history_visible(predict_date):
            history_prediction_rows_excluded += 1
            continue
        predictions_by_scheme[
            (
                _required_text(row.get("scheme_id"), field="prediction scheme_id"),
                _required_text(
                    row.get("target_tenor"), field="prediction target_tenor"
                ),
                _required_int(row.get("horizon"), field="prediction horizon"),
            )
        ].append(row)

    schemes: list[dict[str, Any]] = []
    live_row_count = 0
    backtest_row_count = 0
    for scheme in registry:
        selector = live_actual_selector(scheme["task_type"])
        details_by_source: dict[str, list[dict[str, Any]]] = {
            "backtest": [],
            "live": [],
        }
        prediction_key = (
            scheme["base_scheme_id"],
            scheme["target_tenor"],
            scheme["horizon"],
        )
        for prediction in predictions_by_scheme.get(prediction_key, []):
            joined_actual_direction = actual_facts.get(selector, {}).get(
                (
                    scheme["target_tenor"],
                    _iso_date(
                        prediction.get("target_date"),
                        field="prediction target_date",
                    ),
                    selector[1],
                )
            )
            detail = dict(prediction)
            published_actual = prediction.get("backtest_actual_direction")
            detail["actual_direction"] = (
                _direction_value(
                    published_actual,
                    field="backtest_actual_direction",
                )
                if published_actual is not None
                else joined_actual_direction
            )
            details_by_source[
                dashboard_result_source(detail.get("target_date"))
            ].append(detail)

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

        backtest_details = details_by_source["backtest"]
        live_details = details_by_source["live"]
        backtest_row_count += len(backtest_details)
        live_row_count += len(live_details)

        if registry_scheme_id is not None:
            rows: list[list[Any]] = []
            if detail_source in {"all", "backtest"}:
                rows.extend(
                    compact_detail_row(row, source="backtest")
                    for row in backtest_details
                    if _display_month(row, scheme["task_type"]) == detail_month
                )
            if detail_source in {"all", "live"}:
                rows.extend(
                    compact_detail_row(row, source="live")
                    for row in live_details
                    if _display_month(row, scheme["task_type"]) == detail_month
                )
            rows.sort(key=_compact_row_sort_key)
            payload = {
                "schema_version": DASHBOARD_SCHEMA_VERSION,
                "representation": "detail",
                "snapshot_id": uuid4().hex,
                "generated_at": captured.isoformat(timespec="seconds"),
                "display_until": display_until,
                "live_target_start_date": (
                    FACTOR_LAB_LIVE_TARGET_START_DATE.isoformat()
                ),
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

        schemes.append(
            {
                **scheme,
                "target_label": target_labels[scheme["target_tenor"]],
                "monthly_rows": _monthly_rows(
                    backtest_details=backtest_details,
                    live_details=live_details,
                    task_type=scheme["task_type"],
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
    canonical_build_seconds = time.perf_counter() - canonical_started_at
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
            "history_prediction_rows_excluded_before_policy_start": (
                history_prediction_rows_excluded
            ),
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
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for source, rows in (
        ("backtest", backtest_details),
        ("live", live_details),
    ):
        for row in rows:
            if row.get("actual_direction") is None:
                continue
            grouped[(_display_month(row, task_type), source)].append(row)

    result: list[list[Any]] = []
    source_rank = {"backtest": 0, "live": 1}
    for (month, source), rows in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], source_rank[item[0][1]]),
    ):
        predicted = [
            _direction_value(row.get("predicted_direction"), field="predicted_direction")
            for row in rows
        ]
        actual = [
            _direction_value(row.get("actual_direction"), field="actual_direction")
            for row in rows
        ]
        metric_indexes = [
            index for index, direction in enumerate(predicted) if direction in {-1, 1}
        ]
        result.append(
            [
                month,
                source,
                len(rows),
                len(metric_indexes),
                sum(predicted[index] == actual[index] for index in metric_indexes),
                predicted.count(1),
                predicted.count(-1),
                predicted.count(0),
                actual.count(1),
                actual.count(-1),
                actual.count(0),
                sum(predicted[index] == actual[index] == 1 for index in metric_indexes),
                sum(predicted[index] == actual[index] == -1 for index in metric_indexes),
            ]
        )
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
        raise ValueError("dashboard detail month is required")
    try:
        display_start = date.fromisoformat(f"{display_month}-01")
    except ValueError as exc:
        raise ValueError("dashboard detail month is invalid") from exc
    if display_start.strftime("%Y-%m") != display_month:
        raise ValueError("dashboard detail month is invalid")

    target_start = (
        _shift_month(display_start, -1)
        if task_type == "monthly_average"
        else display_start
    )
    return target_start.isoformat(), _shift_month(target_start, 1).isoformat()


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


def _read_active_registry(
    connection: Connection,
    *,
    registry_scheme_id: str | None = None,
) -> list[Mapping[str, Any]]:
    scheme_filter = (
        " AND scheme_id = :registry_scheme_id"
        if registry_scheme_id is not None
        else ""
    )
    statement = text(
        f"""
        SELECT scheme_id, base_scheme_id, runtime_type, name, owner, description,
               horizon, task_type, frequency, target_tenor, status, deployed_at
        FROM t_scheme_registry
        WHERE status = :active_status{scheme_filter}
        ORDER BY target_tenor, task_type, scheme_id
        LIMIT :dashboard_source_limit
        """
    )
    params = {"active_status": "active"}
    if registry_scheme_id is not None:
        params["registry_scheme_id"] = registry_scheme_id
    return _read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="active_registry",
        cap=MAX_REGISTRY_SOURCE_ROWS,
    )


def _read_backtest_runs(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    base_scheme_ids = sorted(
        {str(row["base_scheme_id"]) for row in registry_rows}
    )
    if not base_scheme_ids:
        return []
    data_sources = sorted(set(BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE.values()))
    statement = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, created_at, updated_at
        FROM t_backtest_runs
        WHERE status = :success_status
          AND scheme_id IN :base_scheme_ids
          AND data_source IN :data_sources
        LIMIT :dashboard_source_limit
        """
    ).bindparams(
        bindparam("base_scheme_ids", expanding=True),
        bindparam("data_sources", expanding=True),
    )
    return _read_bounded_source_rows(
        connection,
        statement,
        {
            "success_status": "success",
            "base_scheme_ids": base_scheme_ids,
            "data_sources": data_sources,
        },
        dataset="backtest_run_candidates",
        cap=MAX_BACKTEST_RUN_SOURCE_ROWS,
    )


def _read_active_targets(connection: Connection) -> list[Mapping[str, Any]]:
    statement = text(
        """
        SELECT target_code, display_name, asset_class, target_type,
               sort_order, status, extra
        FROM t_target_registry
        WHERE status = :active_status
        ORDER BY sort_order, target_code
        LIMIT :dashboard_source_limit
        """
    )
    return _read_bounded_source_rows(
        connection,
        statement,
        {"active_status": "active"},
        dataset="active_targets",
        cap=MAX_TARGET_SOURCE_ROWS,
    )


def _read_product_predictions(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    target_date_range: tuple[str, str] | None = None,
) -> list[Mapping[str, Any]]:
    scopes = sorted(
        {
            (
                str(row["base_scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            for row in registry_rows
        }
    )
    params: dict[str, Any] = {}
    scope_placeholders: list[str] = []
    for index, (base_scheme_id, target_tenor, horizon) in enumerate(scopes):
        scope_placeholders.append(
            f"(:base_scheme_id_{index}, :target_tenor_{index}, :horizon_{index})"
        )
        params[f"base_scheme_id_{index}"] = base_scheme_id
        params[f"target_tenor_{index}"] = target_tenor
        params[f"horizon_{index}"] = horizon
    where_clause = (
        "(scheme_id, target_tenor, horizon) IN ("
        + ", ".join(scope_placeholders)
        + ")"
        if scope_placeholders
        else "1 = 0"
    )
    date_filter = (
        " AND target_date >= :target_date_from"
        " AND target_date < :target_date_before"
        if target_date_range is not None
        else ""
    )
    if target_date_range is not None:
        params["target_date_from"], params["target_date_before"] = (
            target_date_range
        )
    statement = text(
        f"""
        SELECT id, scheme_id, target_tenor, horizon, predict_date,
               feature_date, target_date, predicted_direction,
               backtest_actual_direction
        FROM t_scheme_predictions
        WHERE {where_clause}{date_filter}
        ORDER BY target_date, predict_date, id
        LIMIT :dashboard_source_limit
        """
    )
    return _read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="product_predictions",
        cap=MAX_PRODUCT_PREDICTION_SOURCE_ROWS,
    )


def _read_live_actuals(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
    *,
    target_date_range: tuple[str, str] | None = None,
) -> list[Mapping[str, Any]]:
    active_scopes = sorted(
        {
            (
                str(row["target_tenor"]),
                *live_actual_selector(row.get("task_type")),
            )
            for row in registry_rows
        }
    )
    if not active_scopes:
        return []
    scopes_by_kind: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for target_tenor, actual_kind, target_rule in active_scopes:
        scopes_by_kind[actual_kind].append((target_tenor, target_rule))

    fragments: list[str] = []
    expanding_params = []
    params: dict[str, Any] = {
        "history_start_date": FACTOR_LAB_HISTORY_START_DATE,
    }
    for actual_kind in _ACTUAL_SOURCE_KIND_ORDER:
        scopes = scopes_by_kind.get(actual_kind)
        if not scopes:
            continue
        fragment = _ACTUAL_SOURCE_FRAGMENTS[actual_kind]
        if target_date_range is not None:
            fragment += _ACTUAL_SOURCE_DATE_FILTERS[actual_kind]
        fragments.append(fragment)
        param_name = _ACTUAL_SOURCE_SCOPE_PARAMS[actual_kind]
        expanding_params.append(bindparam(param_name, expanding=True))
        params[param_name] = (
            sorted({target_tenor for target_tenor, _rule in scopes})
            if actual_kind in {"daily_1d", "daily_5d"}
            else scopes
        )

    statement = text(
        "\nUNION ALL\n".join(fragments)
        + "\nLIMIT :dashboard_source_limit"
    ).bindparams(*expanding_params)
    if target_date_range is not None:
        params["target_date_from"], params["target_date_before"] = (
            target_date_range
        )
    return _read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="live_actuals",
        cap=MAX_ACTUAL_SOURCE_ROWS,
    )


def _read_bounded_source_rows(
    connection: Connection,
    statement: Any,
    params: Mapping[str, Any],
    *,
    dataset: str,
    cap: int,
) -> list[Mapping[str, Any]]:
    execution_params = dict(params)
    execution_params["dashboard_source_limit"] = cap + 1
    rows = list(
        connection.execute(statement, execution_params).mappings().all()
    )
    if len(rows) > cap:
        raise DashboardDataError(
            "dashboard source row limit exceeded: "
            f"dataset={dataset} limit={cap}"
        )
    return rows


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
