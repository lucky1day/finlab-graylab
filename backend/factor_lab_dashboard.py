from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from backend.factor_lab_dashboard_semantics import (
    DASHBOARD_SCHEMA_VERSION,
    ROW_FIELDS,
    DashboardDataError,
    choose_live_prediction_rows,
    collapse_actual_facts,
    compact_detail_row,
    live_actual_selector,
    validate_dashboard_payload,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
MYSQL_SNAPSHOT_SQL = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
_DATETIME_TYPE = datetime


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
    captured = captured_at or datetime.now(SHANGHAI_TIMEZONE)
    if not isinstance(captured, _DATETIME_TYPE) or captured.tzinfo is None:
        raise DashboardDataError("dashboard captured_at must be timezone-aware")
    captured = captured.astimezone(SHANGHAI_TIMEZONE)
    display_until = captured.date().isoformat()

    with dashboard_read_connection(engine) as connection:
        registry_rows = _read_active_registry(connection)
        target_rows = _read_active_targets(connection)
        prediction_rows = _read_live_predictions(connection, registry_rows)
        actual_rows = _read_live_actuals(connection, registry_rows)

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
    actual_facts = _collapse_actual_rows(
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

    schemes: list[dict[str, Any]] = []
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

        schemes.append(
            {
                **scheme,
                "target_label": target_labels[scheme["target_tenor"]],
                "live_rows": live_rows,
                "backtest": None,
            }
        )

    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "generated_at": captured.isoformat(timespec="seconds"),
        "display_until": display_until,
        "row_fields": list(ROW_FIELDS),
        "target_labels": target_labels,
        "schemes": schemes,
    }
    validate_dashboard_payload(payload)
    return payload


def _read_active_registry(connection: Connection) -> list[Mapping[str, Any]]:
    statement = text(
        """
        SELECT scheme_id, base_scheme_id, name, description, horizon,
               task_type, frequency, target_tenor, status, deployed_at
        FROM t_scheme_registry
        WHERE status = :active_status
        ORDER BY target_tenor, task_type, scheme_id
        """
    )
    return list(
        connection.execute(statement, {"active_status": "active"}).mappings().all()
    )


def _read_active_targets(connection: Connection) -> list[Mapping[str, Any]]:
    statement = text(
        """
        SELECT target_code, display_name, asset_class, target_type,
               sort_order, status, extra
        FROM t_target_registry
        WHERE status = :active_status
        ORDER BY sort_order, target_code
        """
    )
    return list(
        connection.execute(statement, {"active_status": "active"}).mappings().all()
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
        """
    )
    return list(connection.execute(statement, params).mappings().all())


def _read_live_actuals(
    connection: Connection,
    registry_rows: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    target_tenors = sorted({str(row["target_tenor"]) for row in registry_rows})
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
        """
    ).bindparams(bindparam("target_tenors", expanding=True))
    return list(
        connection.execute(
            statement,
            {"target_tenors": target_tenors},
        )
        .mappings()
        .all()
    )


def _collapse_actual_rows(
    actual_rows: list[Mapping[str, Any]],
    *,
    active_actual_scopes: set[tuple[str, str, str]],
) -> dict[tuple[str, str], dict[tuple[str, str, str], int | None]]:
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

    return {
        selector: collapse_actual_facts(
            rows,
            fact_name=f"{selector[0]} actuals",
        )
        for selector, rows in grouped.items()
    }


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
