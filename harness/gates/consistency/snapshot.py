from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from typing import Any, Callable, Iterator, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from shared.prediction_context import LIVE_PREDICTION_PHASES
from harness.gates.consistency.common import (
    DataConsistencyError,
    DatabaseSnapshot,
    LIVE_RUN_EVIDENCE_START_DATE,
    _actual_selector,
    _iso_date,
    _json_mapping,
    _remaining_budget,
)


def _execute_budgeted(
    connection: Connection,
    statement: Any,
    params: Mapping[str, Any] | None = None,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
):
    """每条 SQL 共用 Gate 截止时刻，并同步收紧 MySQL 单语句上限。"""
    if deadline is not None:
        remaining = _remaining_budget(deadline, monotonic)
        if connection.dialect.name == "mysql":
            max_execution_ms = max(1, min(30_000, int(remaining * 1000)))
            connection.exec_driver_sql(
                f"SET SESSION MAX_EXECUTION_TIME={max_execution_ms}"
            )
    result = connection.execute(statement, dict(params or {}))
    if deadline is not None:
        _remaining_budget(deadline, monotonic)
    return result


def _chunks(values: Sequence[Any], size: int = 500) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _capture_snapshot(
    engine: Engine,
    registry_ids: Sequence[str],
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> DatabaseSnapshot:
    with _read_connection(
        engine,
        deadline=deadline,
        monotonic=monotonic,
    ) as connection:
        registry_rows = tuple(
            dict(row)
            for row in _execute_budgeted(
                connection,
                text(
                    "SELECT scheme_id, base_scheme_id, horizon, task_type, "
                    "runtime_type, frequency, target_tenor, status "
                    "FROM t_scheme_registry "
                    "WHERE scheme_id IN :registry_ids"
                ).bindparams(bindparam("registry_ids", expanding=True)),
                {"registry_ids": list(registry_ids)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
        by_id = {str(row["scheme_id"]): row for row in registry_rows}
        if set(by_id) != set(registry_ids) or any(
            row.get("status") != "active" for row in registry_rows
        ):
            raise DataConsistencyError(
                "selected active Registry identities are incomplete"
            )
        scopes = [
            (
                str(row["base_scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            for row in registry_rows
        ]
        prediction_rows = tuple(
            sorted(
                (
                    dict(row)
                    for row in _read_predictions(
                        connection,
                        scopes,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                ),
                key=lambda row: (
                    str(row["scheme_id"]),
                    str(row["target_tenor"]),
                    int(row["horizon"]),
                    str(row["target_date"]),
                    int(row["id"]),
                ),
            )
        )
        actual_rows = tuple(
            dict(row)
            for row in _read_actuals(
                connection,
                registry_rows,
                prediction_rows,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        actual_watermarks = tuple(
            dict(row)
            for row in _read_actual_watermarks(
                connection,
                registry_rows,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        requested_live_ids = sorted(
            {
                int(row["run_id"])
                for row in prediction_rows
                if row.get("run_id") is not None
            }
        )
        authoritative_live_ids = _read_authoritative_live_run_ids(
            connection,
            sorted({str(row["base_scheme_id"]) for row in registry_rows}),
            deadline=deadline,
            monotonic=monotonic,
        )
        requested_live_ids = sorted(
            set(requested_live_ids) | set(authoritative_live_ids)
        )
        requested_backtest_ids = sorted(
            {
                int(row["backtest_run_id"])
                for row in prediction_rows
                if row.get("backtest_run_id") is not None
            }
        )
        live_runs = _read_references(
            connection,
            table="t_scheme_runs",
            column="run_id",
            values=requested_live_ids,
            deadline=deadline,
            monotonic=monotonic,
        )
        backtest_runs = _read_references(
            connection,
            table="t_backtest_runs",
            column="id",
            values=requested_backtest_ids,
            deadline=deadline,
            monotonic=monotonic,
        )
        backtest_prediction_rows = tuple(
            dict(row)
            for row in _read_backtest_predictions(
                connection,
                requested_backtest_ids,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        scheme_versions = {
                (
                    str(row["scheme_id"]),
                    str(row.get("scheme_version") or ""),
                )
                for row in prediction_rows
            }
        for run in backtest_runs.values():
            summary = _json_mapping(
                run.get("summary"), field="backtest summary"
            )
            source_exact = str(summary.get("scheme_version") or "")
            if source_exact:
                scheme_versions.add((str(run["scheme_id"]), source_exact))
        version_rows = tuple(
            dict(row)
            for row in _read_versions(
                connection,
                sorted(scheme_versions),
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        artifact_ids = sorted(
            {
                str(row.get("input_artifact_id") or "")
                for row in live_runs.values()
                if row.get("input_artifact_id")
            }
        )
        input_artifact_rows = tuple(
            dict(row)
            for row in _read_input_artifacts(
                connection,
                artifact_ids,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        calendar_rows = tuple(
            dict(row)
            for row in _execute_budgeted(
                connection,
                text(
                    "SELECT tc.rdate, tc.trade_flag, wd.week_id "
                    "FROM t_trade_calendar tc "
                    "LEFT JOIN api_wind_date wd ON wd.rdate = tc.rdate "
                    "ORDER BY tc.rdate"
                ),
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    digest = _snapshot_digest(
        registry_rows,
        prediction_rows,
        actual_rows,
        tuple(live_runs.values()),
        tuple(backtest_runs.values()),
        calendar_rows,
        backtest_prediction_rows,
        version_rows,
        input_artifact_rows,
        actual_watermarks,
    )
    return DatabaseSnapshot(
        registry_rows=registry_rows,
        prediction_rows=prediction_rows,
        actual_rows=actual_rows,
        live_runs=tuple(
            dict(row) for _, row in sorted(live_runs.items())
        ),
        backtest_runs=tuple(
            dict(row) for _, row in sorted(backtest_runs.items())
        ),
        calendar_rows=calendar_rows,
        digest=digest,
        backtest_prediction_rows=backtest_prediction_rows,
        version_rows=version_rows,
        input_artifact_rows=input_artifact_rows,
        actual_watermarks=actual_watermarks,
    )


@contextmanager
def _read_connection(
    engine: Engine,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> Iterator[Connection]:
    if deadline is not None:
        _remaining_budget(deadline, monotonic)
    connection = engine.connect()
    transaction = None
    is_mysql = engine.dialect.name == "mysql"
    try:
        if is_mysql:
            if deadline is not None:
                _remaining_budget(deadline, monotonic)
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            connection.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
            if deadline is not None:
                _remaining_budget(deadline, monotonic)
        else:
            transaction = connection.begin()
        yield connection
    finally:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        elif is_mysql:
            connection.rollback()
        connection.close()


def _read_predictions(
    connection: Connection,
    scopes: Sequence[tuple[str, str, int]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for scope_chunk in _chunks(scopes):
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for index, (scheme_id, tenor, horizon) in enumerate(scope_chunk):
            clauses.append(
                f"(scheme_id = :scheme_{index} AND target_tenor = :tenor_{index} "
                f"AND horizon = :horizon_{index})"
            )
            params.update(
                {
                    f"scheme_{index}": scheme_id,
                    f"tenor_{index}": tenor,
                    f"horizon_{index}": horizon,
                }
            )
        where = " OR ".join(clauses) or "1 = 0"
        rows.extend(
            _execute_budgeted(
                connection,
                text(
                    "SELECT id, run_id, backtest_run_id, scheme_version, scheme_id, "
                    "target_tenor, horizon, predict_date, feature_date, target_date, "
                    "predicted_direction, backtest_actual_direction "
                    f"FROM t_scheme_predictions WHERE {where} "
                    "ORDER BY scheme_id, target_tenor, horizon, target_date, id"
                ),
                params,
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _read_actuals(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    prediction_rows: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    grouped_dates: dict[
        tuple[str, str, str, str, str, str, str], set[str]
    ] = defaultdict(set)
    for registry in registry_rows:
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        tenor = str(registry["target_tenor"])
        if task_type == "T+1":
            table, date_column, direction_column = (
                "t_scheme_actuals",
                "trade_date",
                "direction_1d",
            )
            rule_filter = ""
        elif task_type == "T+5":
            table, date_column, direction_column = (
                "t_scheme_actuals",
                "trade_date",
                "direction_5d",
            )
            rule_filter = ""
        elif task_type in {"weekly_point", "weekly_average"}:
            table, date_column, direction_column = (
                "t_scheme_weekly_actuals",
                "target_date",
                "direction_weekly",
            )
            rule_filter = " AND target_rule = :target_rule"
        elif task_type == "monthly":
            table, date_column, direction_column = (
                "t_scheme_monthly_actuals",
                "target_date",
                "direction_monthly",
            )
            rule_filter = " AND target_rule = :target_rule"
        else:
            table, date_column, direction_column = (
                "t_scheme_period_average_actuals",
                "target_date",
                "actual_direction",
            )
            rule_filter = " AND target_rule = :target_rule"
        scope = (
            str(registry["base_scheme_id"]),
            tenor,
            int(registry["horizon"]),
        )
        dates = {
            _iso_date(row.get("target_date"), field="target_date")
            for row in prediction_rows
            if row.get("backtest_actual_direction") is None
            and (
                str(row["scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            == scope
        }
        grouped_dates[
            (
                actual_kind,
                tenor,
                target_rule,
                table,
                date_column,
                direction_column,
                rule_filter,
            )
        ].update(dates)

    rows: list[Mapping[str, Any]] = []
    for (
        actual_kind,
        tenor,
        target_rule,
        table,
        date_column,
        direction_column,
        rule_filter,
    ), target_date_set in sorted(grouped_dates.items()):
        target_dates = sorted(target_date_set)
        for date_chunk in _chunks(target_dates):
            statement = text(
                f"SELECT :actual_kind AS actual_kind, tenor AS target_tenor, "
                f"{date_column} AS target_date, "
                f":selected_rule AS target_rule, "
                f"{direction_column} AS actual_direction "
                f"FROM {table} WHERE tenor = :tenor "
                f"AND {date_column} IN :target_dates{rule_filter}"
            ).bindparams(bindparam("target_dates", expanding=True))
            params = {
                "tenor": tenor,
                "target_dates": list(date_chunk),
                "selected_rule": target_rule,
                "target_rule": target_rule,
                "actual_kind": actual_kind,
            }
            rows.extend(
                _execute_budgeted(
                    connection,
                    statement,
                    params,
                    deadline=deadline,
                    monotonic=monotonic,
                ).mappings()
            )
    return rows


def _read_actual_watermarks(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    scopes: set[tuple[str, str, str, str, str, str]] = set()
    for registry in registry_rows:
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        if task_type in {"T+1", "T+5"}:
            table = "t_scheme_actuals"
            date_column = "trade_date"
            rule_filter = ""
        elif task_type in {"weekly_point", "weekly_average"}:
            table = "t_scheme_weekly_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        elif task_type == "monthly":
            table = "t_scheme_monthly_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        else:
            table = "t_scheme_period_average_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        scopes.add(
            (
                actual_kind,
                str(registry["target_tenor"]),
                target_rule,
                table,
                date_column,
                rule_filter,
            )
        )
    watermarks: list[Mapping[str, Any]] = []
    for (
        actual_kind,
        tenor,
        target_rule,
        table,
        date_column,
        rule_filter,
    ) in sorted(scopes):
        result = _execute_budgeted(
            connection,
            text(
                f"SELECT MAX({date_column}) AS max_target_date "
                f"FROM {table} WHERE tenor = :tenor{rule_filter}"
            ),
            {"tenor": tenor, "target_rule": target_rule},
            deadline=deadline,
            monotonic=monotonic,
        ).scalar_one_or_none()
        watermarks.append(
            {
                "actual_kind": actual_kind,
                "target_tenor": tenor,
                "target_rule": target_rule,
                "max_target_date": (
                    str(result)[:10] if result is not None else None
                ),
            }
        )
    return watermarks


def _read_references(
    connection: Connection,
    *,
    table: str,
    column: str,
    values: Sequence[int],
    deadline: float | None,
    monotonic: Callable[[], float],
) -> dict[int, dict[str, Any]]:
    if not values:
        return {}
    if table == "t_scheme_runs":
        selected = (
            f"{column}, scheme_id, scheme_version, runtime_type, status, "
            "prediction_phase, input_artifact_id, data_snapshot_id, "
            "predict_date, records_written"
        )
    elif table == "t_backtest_runs":
        selected = (
            f"{column}, benchmark_id, scheme_id, status, code_hash, config_hash, "
            "input_artifact_hash, summary"
        )
    else:
        raise ValueError("unsupported consistency reference table")
    statement = text(
        f"SELECT {selected} FROM {table} WHERE {column} IN :reference_ids"
    ).bindparams(bindparam("reference_ids", expanding=True))
    result: dict[int, dict[str, Any]] = {}
    for value_chunk in _chunks(values):
        for row in _execute_budgeted(
            connection,
            statement,
            {"reference_ids": list(value_chunk)},
            deadline=deadline,
            monotonic=monotonic,
        ).mappings():
            result[int(row[column])] = {
                **dict(row),
                "reference_id": int(row[column]),
            }
    if table != "t_backtest_runs" or not result:
        return result
    source_ids = sorted({str(row["scheme_id"]) for row in result.values()})
    registry_scopes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source_chunk in _chunks(source_ids):
        for registry_row in _execute_budgeted(
            connection,
            text(
                "SELECT base_scheme_id, target_tenor, horizon, status "
                "FROM t_scheme_registry "
                "WHERE base_scheme_id IN :scheme_ids"
            ).bindparams(bindparam("scheme_ids", expanding=True)),
            {"scheme_ids": list(source_chunk)},
            deadline=deadline,
            monotonic=monotonic,
        ).mappings():
            registry_scopes[str(registry_row["base_scheme_id"])].append(
                {
                    "target_tenor": str(registry_row["target_tenor"]),
                    "horizon": int(registry_row["horizon"]),
                    "status": str(registry_row["status"]),
                }
            )
    for row in result.values():
        scheme_id = str(row["scheme_id"])
        row["source_registry_scopes"] = sorted(
            registry_scopes.get(scheme_id, []),
            key=lambda item: (item["target_tenor"], item["horizon"]),
        )
    return result


def _read_authoritative_live_run_ids(
    connection: Connection,
    scheme_ids: Sequence[str],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[int]:
    run_ids: set[int] = set()
    statement = text(
        "SELECT run_id FROM t_scheme_runs "
        "WHERE scheme_id IN :scheme_ids AND status = 'success' "
        "AND prediction_phase IN :live_phases "
        "AND predict_date >= :live_start"
    ).bindparams(
        bindparam("scheme_ids", expanding=True),
        bindparam("live_phases", expanding=True),
    )
    for scheme_chunk in _chunks(scheme_ids):
        run_ids.update(
            int(value)
            for value in _execute_budgeted(
                connection,
                statement,
                {
                    "scheme_ids": list(scheme_chunk),
                    "live_phases": sorted(LIVE_PREDICTION_PHASES),
                    "live_start": LIVE_RUN_EVIDENCE_START_DATE.isoformat(),
                },
                deadline=deadline,
                monotonic=monotonic,
            ).scalars()
        )
    return sorted(run_ids)


def _read_backtest_predictions(
    connection: Connection,
    run_ids: Sequence[int],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    statement = text(
        "SELECT run_id, scheme_id, target_tenor, horizon, predict_date, "
        "feature_date, target_date, label, predicted_direction "
        "FROM t_backtest_predictions WHERE run_id IN :run_ids "
        "ORDER BY run_id, target_tenor, horizon, target_date, predict_date, id"
    ).bindparams(bindparam("run_ids", expanding=True))
    for run_chunk in _chunks(run_ids):
        rows.extend(
            _execute_budgeted(
                connection,
                statement,
                {"run_ids": list(run_chunk)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _read_versions(
    connection: Connection,
    identities: Sequence[tuple[str, str]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for identity_chunk in _chunks(identities):
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for index, (scheme_id, exact) in enumerate(identity_chunk):
            clauses.append(
                f"(scheme_id = :version_scheme_{index} "
                f"AND scheme_version = :version_exact_{index})"
            )
            params[f"version_scheme_{index}"] = scheme_id
            params[f"version_exact_{index}"] = exact
        if not clauses:
            continue
        rows.extend(
            _execute_budgeted(
                connection,
                text(
                    "SELECT scheme_id, scheme_version, runtime_type, "
                    "data_snapshot_id, code_hash, config_hash, "
                    "manifest_hash, status FROM t_scheme_versions WHERE "
                    + " OR ".join(clauses)
                ),
                params,
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _read_input_artifacts(
    connection: Connection,
    artifact_ids: Sequence[str],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    statement = text(
        "SELECT artifact_id, scheme_id, scheme_version, predict_date, "
        "content_hash, schema_hash FROM t_input_artifacts "
        "WHERE artifact_id IN :artifact_ids"
    ).bindparams(bindparam("artifact_ids", expanding=True))
    for artifact_chunk in _chunks(artifact_ids):
        rows.extend(
            _execute_budgeted(
                connection,
                statement,
                {"artifact_ids": list(artifact_chunk)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _snapshot_digest(*parts: Any) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple, set, frozenset)):
            normalized = [normalize(item) for item in value]
            return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
        if isinstance(value, (date,)):
            return value.isoformat()
        return value

    raw = json.dumps(
        normalize(parts),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
