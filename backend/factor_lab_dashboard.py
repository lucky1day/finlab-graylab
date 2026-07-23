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

from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
    DASHBOARD_SCHEMA_VERSION,
    ROW_FIELDS,
    DashboardDataError,
    backtest_benchmark_label,
    backtest_data_source_label,
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
    collapse_actual_facts_with_diagnostics,
    compact_detail_row,
    live_actual_selector,
    validate_dashboard_payload,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MYSQL_SNAPSHOT_SQL = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
_DATETIME_TYPE = datetime
MAX_DETAIL_ROWS = 20_000
MAX_RAW_JSON_BYTES = 1_500_000
MAX_GZIP_JSON_BYTES = 100_000
# These source-row caps are corruption/resource guards, not business pagination.
# Every query reads at most cap + 1 rows and fails instead of truncating.
MAX_REGISTRY_SOURCE_ROWS = 1_000
MAX_TARGET_SOURCE_ROWS = 1_000
MAX_LIVE_PREDICTION_SOURCE_ROWS = 20_000
MAX_ACTUAL_SOURCE_ROWS = 80_000
MAX_BACKTEST_RUN_SOURCE_ROWS = 100_000
MAX_BACKTEST_DETAIL_SOURCE_ROWS = 20_000
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
    *,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """在一个一致性事务内批量读取并构建因子实验室 live 快照。"""
    build_started_at = time.perf_counter()
    captured = captured_at or datetime.now(SHANGHAI_TIMEZONE)
    if not isinstance(captured, _DATETIME_TYPE) or captured.tzinfo is None:
        raise DashboardDataError("dashboard captured_at must be timezone-aware")
    captured = captured.astimezone(SHANGHAI_TIMEZONE)
    display_until = captured.date().isoformat()

    db_read_started_at = time.perf_counter()
    with dashboard_read_connection(engine) as connection:
        registry_rows = _read_active_registry(connection)
        target_rows = _read_active_targets(connection)
        prediction_rows = _read_live_predictions(connection, registry_rows)
        actual_rows = _read_live_actuals(connection, registry_rows)
        backtest_run_rows = _read_backtest_runs(connection, registry_rows)
        selected_backtest_runs = choose_latest_backtest_runs(
            backtest_run_rows,
            registry_rows,
        )
        backtest_detail_rows = _read_backtest_details(
            connection,
            selected_run_ids=sorted(
                {int(row["id"]) for row in selected_backtest_runs.values()}
            ),
        )
    db_read_seconds = time.perf_counter() - db_read_started_at

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
    canonical_predictions = choose_live_prediction_rows(
        prediction_rows,
        display_until=display_until,
    )
    predictions_by_scheme: dict[tuple[str, str, int], list[Mapping[str, Any]]] = (
        defaultdict(list)
    )
    for row in canonical_predictions:
        predictions_by_scheme[
            (
                _required_text(row.get("scheme_id"), field="prediction scheme_id"),
                _required_text(
                    row.get("target_tenor"), field="prediction target_tenor"
                ),
                _required_int(row.get("horizon"), field="prediction horizon"),
            )
        ].append(row)

    backtest_details_by_scope: dict[
        tuple[int, str], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for row in backtest_detail_rows:
        backtest_details_by_scope[
            (
                _required_int(row.get("run_id"), field="backtest detail run_id"),
                _required_text(
                    row.get("target_tenor"),
                    field="backtest detail target_tenor",
                ),
            )
        ].append(row)

    schemes: list[dict[str, Any]] = []
    live_row_count = 0
    backtest_row_count = 0
    for scheme in registry:
        selector = live_actual_selector(scheme["task_type"])
        live_rows: list[list[Any]] = []
        prediction_key = (
            scheme["base_scheme_id"],
            scheme["target_tenor"],
            scheme["horizon"],
        )
        for prediction in predictions_by_scheme.get(prediction_key, []):
            actual_direction = actual_facts.get(selector, {}).get(
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
            detail["actual_direction"] = actual_direction
            live_rows.append(compact_detail_row(detail, source="live"))
            live_row_count += 1

        selected_run = selected_backtest_runs.get(scheme["scheme_id"])
        backtest = None
        if selected_run is not None:
            run_id = _required_int(
                selected_run.get("id"), field="selected backtest run id"
            )
            selected_details = backtest_details_by_scope.get(
                (run_id, scheme["target_tenor"]),
                [],
            )
            if not selected_details:
                raise DashboardDataError(
                    f"base_scheme_id={scheme['base_scheme_id']} "
                    f"target_tenor={scheme['target_tenor']} has no detail "
                    f"for selected backtest run_id={run_id}"
                )
            compact_backtest_rows: list[list[Any]] = []
            for detail_row in selected_details:
                detail_horizon = _required_int(
                    detail_row.get("horizon"), field="backtest detail horizon"
                )
                if detail_horizon != scheme["horizon"]:
                    raise DashboardDataError(
                        "backtest detail horizon does not match Registry scheme "
                        f"{scheme['scheme_id']}: "
                        f"detail={detail_horizon} registry={scheme['horizon']}"
                    )
                detail = dict(detail_row)
                detail["prediction_phase"] = None
                detail["actual_direction"] = detail.get("label")
                compact_backtest_rows.append(
                    compact_detail_row(detail, source="backtest")
                )
                backtest_row_count += 1
            compact_backtest_rows.sort(key=_compact_row_sort_key)
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
                "rows": compact_backtest_rows,
            }

        live_rows.sort(key=_compact_row_sort_key)
        schemes.append(
            {
                **scheme,
                "target_label": target_labels[scheme["target_tenor"]],
                "live_rows": live_rows,
                "backtest": backtest,
            }
        )

    schemes.sort(key=lambda item: item["scheme_id"])
    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "snapshot_id": uuid4().hex,
        "generated_at": captured.isoformat(timespec="seconds"),
        "display_until": display_until,
        "stale": False,
        "snapshot_age_ms": 0,
        "row_fields": list(ROW_FIELDS),
        "target_labels": target_labels,
        "schemes": schemes,
    }
    validate_dashboard_payload(payload)
    canonical_build_seconds = time.perf_counter() - canonical_started_at
    # This checks the canonical builder snapshot only. Task 6 must encode the
    # final stale/snapshot_age_ms body and verify actual identity/gzip ASGI wire
    # bytes after the route and middleware are integrated.
    serialization_started_at = time.perf_counter()
    encoding = _validate_canonical_snapshot_budgets(
        payload,
        detail_rows=live_row_count + backtest_row_count,
    )
    canonical_serialization_seconds = (
        time.perf_counter() - serialization_started_at
    )
    _record_build_diagnostics(
        payload["snapshot_id"],
        {
            "snapshot_id": payload["snapshot_id"],
            "db_read_seconds": db_read_seconds,
            "canonical_build_seconds": canonical_build_seconds,
            "canonical_serialization_seconds": canonical_serialization_seconds,
            "build_seconds": time.perf_counter() - build_started_at,
            "scheme_count": len(schemes),
            "live_row_count": live_row_count,
            "backtest_row_count": backtest_row_count,
            "detail_row_count": live_row_count + backtest_row_count,
            "raw_bytes": encoding.raw_size,
            "gzip_bytes": encoding.gzip_size,
            **actual_diagnostics,
        },
    )
    return payload


def dashboard_build_diagnostics(snapshot_id: str) -> dict[str, Any] | None:
    """返回指定成功快照的有界构建诊断副本。"""
    with _diagnostics_lock:
        diagnostics = _diagnostics_by_snapshot_id.get(snapshot_id)
        return None if diagnostics is None else deepcopy(diagnostics)


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


def _read_active_registry(connection: Connection) -> list[Mapping[str, Any]]:
    statement = text(
        """
        SELECT scheme_id, base_scheme_id, runtime_type, name, description, horizon,
               task_type, frequency, target_tenor, status, deployed_at
        FROM t_scheme_registry
        WHERE status = :active_status
        ORDER BY target_tenor, task_type, scheme_id
        LIMIT :dashboard_source_limit
        """
    )
    return _read_bounded_source_rows(
        connection,
        statement,
        {"active_status": "active"},
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


def _read_backtest_details(
    connection: Connection,
    *,
    selected_run_ids: list[int],
) -> list[Mapping[str, Any]]:
    if not selected_run_ids:
        return []
    statement = text(
        """
        SELECT run_id, target_tenor, horizon, predict_date, feature_date,
               target_date, label, predicted_direction
        FROM t_backtest_predictions
        WHERE run_id IN :run_ids
        LIMIT :dashboard_source_limit
        """
    ).bindparams(bindparam("run_ids", expanding=True))
    return _read_bounded_source_rows(
        connection,
        statement,
        {"run_ids": selected_run_ids},
        dataset="selected_backtest_details",
        cap=MAX_BACKTEST_DETAIL_SOURCE_ROWS,
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


def _read_live_predictions(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    pairs = sorted(
        {
            (str(row["base_scheme_id"]), str(row["target_tenor"]))
            for row in registry_rows
        }
    )
    params: dict[str, Any] = {}
    filters: list[str] = []
    for index, (base_scheme_id, target_tenor) in enumerate(pairs):
        filters.append(
            f"(scheme_id = :base_scheme_id_{index} "
            f"AND target_tenor = :target_tenor_{index})"
        )
        params[f"base_scheme_id_{index}"] = base_scheme_id
        params[f"target_tenor_{index}"] = target_tenor
    where_clause = " OR ".join(filters) if filters else "1 = 0"
    statement = text(
        f"""
        SELECT id, scheme_id, target_tenor, horizon, predict_date,
               feature_date, target_date, prediction_phase,
               predicted_direction, extra
        FROM t_scheme_predictions
        WHERE {where_clause}
        ORDER BY target_date, predict_date, id
        LIMIT :dashboard_source_limit
        """
    )
    return _read_bounded_source_rows(
        connection,
        statement,
        params,
        dataset="live_predictions",
        cap=MAX_LIVE_PREDICTION_SOURCE_ROWS,
    )


def _read_live_actuals(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    target_tenors = sorted({str(row["target_tenor"]) for row in registry_rows})
    if not target_tenors:
        return []
    statement = text(
        """
        SELECT 'daily_1d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_1d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :target_tenors
        UNION ALL
        SELECT 'daily_5d' AS actual_kind,
               tenor AS target_tenor,
               trade_date AS target_date,
               'target_date_yield_vs_feature_date_yield' AS target_rule,
               direction_5d AS actual_direction
        FROM t_scheme_actuals
        WHERE tenor IN :target_tenors
        UNION ALL
        SELECT 'weekly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_weekly AS actual_direction
        FROM t_scheme_weekly_actuals
        WHERE tenor IN :target_tenors
        UNION ALL
        SELECT 'monthly' AS actual_kind,
               tenor AS target_tenor,
               target_date,
               target_rule,
               direction_monthly AS actual_direction
        FROM t_scheme_monthly_actuals
        WHERE tenor IN :target_tenors
        LIMIT :dashboard_source_limit
        """
    ).bindparams(bindparam("target_tenors", expanding=True))
    return _read_bounded_source_rows(
        connection,
        statement,
        {"target_tenors": target_tenors},
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

    duplicates_folded = {"daily": 0, "weekly": 0, "monthly": 0}
    direction_conflicts = {"daily": 0, "weekly": 0, "monthly": 0}
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
    """把内部 actual selector 收敛为安全的三类诊断标签。"""
    if actual_kind in {"daily_1d", "daily_5d"}:
        return "daily"
    if actual_kind in {"weekly", "monthly"}:
        return actual_kind
    raise DashboardDataError(
        f"dashboard actual_kind is invalid: {actual_kind!r}"
    )


def _registry_dto(row: Mapping[str, Any]) -> dict[str, Any]:
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


def _compact_row_sort_key(row: list[Any]) -> tuple[str, str]:
    return str(row[2]), str(row[0])


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


def _validate_canonical_snapshot_budgets(
    payload: Mapping[str, Any],
    *,
    detail_rows: int | None = None,
) -> SnapshotEncoding:
    if detail_rows is None:
        detail_rows = 0
        for scheme in payload["schemes"]:
            detail_rows += len(scheme["live_rows"])
            backtest = scheme["backtest"]
            if backtest is not None:
                detail_rows += len(backtest["rows"])
    if detail_rows > MAX_DETAIL_ROWS:
        raise DashboardDataError(
            "dashboard detail rows exceed budget: "
            f"rows={detail_rows} limit={MAX_DETAIL_ROWS}"
        )

    encoding = encode_canonical_snapshot(payload)
    logger.info(
        "Built factor lab dashboard snapshot_id=%s detail_rows=%s raw_bytes=%s gzip_bytes=%s",
        payload["snapshot_id"],
        detail_rows,
        encoding.raw_size,
        encoding.gzip_size,
    )
    return encoding


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
