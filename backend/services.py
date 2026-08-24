from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from backend.factor_lab_dashboard_semantics import (
    BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE,
    apply_live_prediction_corrections,
    backtest_benchmark_label,
    backtest_data_source_label,
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
    is_factor_lab_history_visible,
)
from scheduler.discovery import discover_schemes
from scheduler.repository import sync_scheme_registry
from shared.metrics import direction_metric_block
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import (
    ALLOWED_TASK_TYPES as SHARED_ALLOWED_TASK_TYPES,
    PERIOD_AVERAGE_TASK_TYPES,
    TASK_COMBINATIONS,
)


logger = logging.getLogger(__name__)


DEFAULT_TARGET_LABELS = {
    "1Y": "1Y国债活跃",
    "3Y": "3Y国债活跃",
    "5Y": "5Y国债活跃",
    "7Y": "7Y国债活跃",
    "10Y": "10Y国债活跃",
}
DEFAULT_TARGET_ORDER = ["1Y", "3Y", "5Y", "7Y", "10Y"]
ALLOWED_TASK_TYPES = set(SHARED_ALLOWED_TASK_TYPES)
WEEKLY_TASK_TARGET_RULES = {
    "weekly_point": WEEKLY_TARGET_RULE,
    "weekly_average": WEEKLY_AVERAGE_TARGET_RULE,
}
PERIOD_AVERAGE_TASK_TARGET_RULES = {
    task_type: TASK_COMBINATIONS[task_type][1]
    for task_type in PERIOD_AVERAGE_TASK_TYPES
}
def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _require_deployed_at(row: Any, *, context: str) -> str:
    deployed_at = _iso(row["deployed_at"])
    if deployed_at:
        return deployed_at
    raise ValueError(
        f"{context} missing deployed_at: "
        f"scheme_id={row['scheme_id']} "
        f"base_scheme_id={row['base_scheme_id']} "
        f"target_tenor={row['target_tenor']}"
    )


def _require_task_type(row: Any, *, context: str) -> str:
    task_type = str(row["task_type"] or "").strip()
    if task_type in ALLOWED_TASK_TYPES:
        return task_type
    raise ValueError(
        f"{context} invalid task_type: "
        f"scheme_id={row['scheme_id']} "
        f"base_scheme_id={row['base_scheme_id']} "
        f"target_tenor={row['target_tenor']} "
        f"task_type={task_type or None}"
    )


def _require_runtime_type(row: Any, *, context: str) -> str:
    runtime_type = str(row["runtime_type"] or "").strip()
    if runtime_type in BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE:
        return runtime_type
    raise ValueError(
        f"{context} invalid runtime_type: "
        f"scheme_id={row['scheme_id']} "
        f"base_scheme_id={row['base_scheme_id']} "
        f"target_tenor={row['target_tenor']} "
        f"runtime_type={runtime_type or None}"
    )


def _require_target_date(value: Any, *, context: str, row: Any | None = None) -> str:
    target_date = _iso(value)
    if target_date:
        return target_date
    detail = ""
    if row is not None:
        detail = (
            f" scheme_id={row['scheme_id'] if 'scheme_id' in row else None}"
            f" target_tenor={row['target_tenor'] if 'target_tenor' in row else None}"
            f" predict_date={_iso(row['predict_date']) if 'predict_date' in row else None}"
        )
    raise ValueError(f"{context} missing required target_date{detail}")


def _json_value(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _month_start(month: str | None) -> str | None:
    if not month:
        return None
    datetime.strptime(month, "%Y-%m")
    return f"{month}-01"


def _month_end(month: str | None) -> str | None:
    if not month:
        return None
    parsed = datetime.strptime(month, "%Y-%m")
    if parsed.month == 12:
        next_month = date(parsed.year + 1, 1, 1)
    else:
        next_month = date(parsed.year, parsed.month + 1, 1)
    return next_month.isoformat()


def _list_target_registry(engine: Engine) -> list[dict[str, Any]]:
    """读取 Y 标的注册表；未迁移时返回默认国债活跃标的。"""
    sql = text(
        """
        SELECT target_code, display_name, asset_class, target_type,
               sort_order, status, extra, created_at, updated_at
        FROM t_target_registry
        ORDER BY sort_order, target_code
        """
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
    except SQLAlchemyError as exc:
        message = str(exc)
        if "t_target_registry" not in message and "1146" not in message:
            raise
        rows = []

    if not rows:
        return [
            {
                "target_code": code,
                "display_name": DEFAULT_TARGET_LABELS[code],
                "asset_class": "bond",
                "target_type": "active_treasury",
                "sort_order": index,
                "status": "active",
                "extra": {"legacy_tenor": code},
                "created_at": None,
                "updated_at": None,
            }
            for index, code in enumerate(DEFAULT_TARGET_ORDER, start=1)
        ]

    return [
        {
            "target_code": row["target_code"],
            "display_name": row["display_name"],
            "asset_class": row["asset_class"],
            "target_type": row["target_type"],
            "sort_order": row["sort_order"],
            "status": row["status"],
            "extra": _json_value(row["extra"], {}),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _target_labels(engine: Engine) -> dict[str, str]:
    return {
        item["target_code"]: item["display_name"]
        for item in _list_target_registry(engine)
        if item["status"] == "active"
    }


def _visible_targets(engine: Engine) -> set[str]:
    labels = _target_labels(engine)
    return set(labels) or set(DEFAULT_TARGET_ORDER)


def _target_label(target_tenor: str, labels: dict[str, str] | None = None) -> str:
    """返回前端展示用的 Y 标的名称。"""
    labels = labels or DEFAULT_TARGET_LABELS
    return labels.get(str(target_tenor), str(target_tenor))


def _backtest_benchmark_label(benchmark_id: str | None) -> str:
    return backtest_benchmark_label(benchmark_id)


def _backtest_data_source_label(data_source: str) -> str:
    return backtest_data_source_label(data_source)


def _backtest_scheme_name(meta: dict[str, Any], scheme_id: str) -> str:
    return str(meta.get("name") or scheme_id)


def _backtest_display_name(
    *,
    meta: dict[str, Any],
    scheme_id: str,
    target_label: str,
    data_source: str,
) -> str:
    return f"{_backtest_scheme_name(meta, scheme_id)} · {target_label}"


def list_targets(engine: Engine) -> list[dict[str, Any]]:
    """返回 Y 标的注册表。"""
    return _list_target_registry(engine)


def _metric_block(rows: list[dict]) -> dict[str, Any]:
    metrics = direction_metric_block(rows)
    accuracy = _ratio_to_percent(metrics["accuracy"])
    return {
        "total": metrics["samples"],
        "samples": metrics["samples"],
        "metric_samples": metrics["metric_samples"],
        "correct": metrics["correct"],
        "accuracy": accuracy,
        "overall": accuracy,
        "up_precision": _ratio_to_percent(metrics["up_precision"]),
        "up_recall": _ratio_to_percent(metrics["up_recall"]),
        "down_precision": _ratio_to_percent(metrics["down_precision"]),
        "down_recall": _ratio_to_percent(metrics["down_recall"]),
        "actual_dist": metrics["actual_dist"],
        "predicted_dist": metrics["predicted_dist"],
        "metric_actual_dist": metrics["metric_actual_dist"],
        "metric_predicted_dist": metrics["metric_predicted_dist"],
    }


def sync_registry_from_configs(engine: Engine) -> bool:
    """显式把当前 schemes 配置同步到 Registry。"""
    sync_scheme_registry(engine, discover_schemes())
    return True


def list_schemes(engine: Engine) -> list[dict[str, Any]]:
    """返回注册表中的业务方案行。

    只读路径：不再触发 registry 写库同步，直接读取 registry 表当前内容。
    registry 同步由应用启动钩子与受保护的 admin 端点负责。
    """
    sql = text(
        """
        SELECT r.scheme_id, r.base_scheme_id, r.runtime_type, r.name, r.description, r.horizon, r.task_type, r.frequency,
               r.target_tenor, r.schedule_cron, r.schedule_timezone, r.status,
               r.deployed_at, r.created_at, r.updated_at
        FROM t_scheme_registry r
        WHERE r.status = 'active'
        ORDER BY r.scheme_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return [
        {
            "scheme_id": row["scheme_id"],
            "base_scheme_id": row["base_scheme_id"],
            "runtime_type": _require_runtime_type(
                row,
                context="active registry row",
            ),
            "name": row["name"],
            "description": row["description"],
            "horizon": row["horizon"],
            "task_type": _require_task_type(row, context="active registry row"),
            "frequency": row["frequency"],
            "target_tenor": row["target_tenor"],
            "schedule_cron": row["schedule_cron"],
            "schedule_timezone": row["schedule_timezone"],
            "status": row["status"],
            "deployed_at": _require_deployed_at(row, context="active registry row"),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _scheme_meta_from_config(cfg: Any) -> dict[str, Any]:
    return {
        "scheme_id": cfg.scheme_id,
        "base_scheme_id": cfg.scheme_id,
        "name": cfg.name,
        "description": cfg.description,
        "horizon": cfg.horizon,
        "task_type": cfg.task_type,
        "frequency": cfg.frequency,
        "schedule_cron": cfg.schedule.cron,
        "schedule_timezone": cfg.schedule.timezone,
        "status": cfg.status,
    }


def _backtest_scheme_meta(engine: Engine) -> dict[tuple[str, str], dict[str, Any]]:
    """只读获取回测矩阵所需方案元数据，不触发 registry 同步。"""
    sql = text(
        """
        SELECT scheme_id, base_scheme_id, runtime_type, name, description, horizon, task_type, frequency,
               target_tenor, schedule_cron, schedule_timezone, status,
               deployed_at, created_at, updated_at
        FROM t_scheme_registry
        WHERE status = 'active'
        """
    )
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
    except SQLAlchemyError as exc:
        message = str(exc)
        if "1146" not in message and "no such table: t_scheme_registry" not in message:
            raise
        rows = []

    for row in rows:
        key = (str(row["base_scheme_id"]), str(row["target_tenor"]))
        meta[key] = {
            "scheme_id": row["scheme_id"],
            "base_scheme_id": row["base_scheme_id"],
            "runtime_type": _require_runtime_type(row, context="active registry row"),
            "name": row["name"],
            "description": row["description"],
            "horizon": row["horizon"],
            "task_type": _require_task_type(row, context="active registry row"),
            "frequency": row["frequency"],
            "target_tenor": row["target_tenor"],
            "schedule_cron": row["schedule_cron"],
            "schedule_timezone": row["schedule_timezone"],
            "status": row["status"],
            "deployed_at": _require_deployed_at(row, context="active registry row"),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    return meta


def list_predictions(
    engine: Engine,
    scheme_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """分页返回单个 active registry 业务方案的预测明细。"""
    if not scheme_id:
        raise LookupError("registry scheme_id is required")
    registry_row = _registry_scheme_row(engine, scheme_id)
    base_scheme_id = str(registry_row["base_scheme_id"])
    target_tenor = str(registry_row["target_tenor"])
    horizon = int(registry_row["horizon"])
    target_labels = _target_labels(engine)
    filters = [
        "p.scheme_id = :base_scheme_id",
        "p.target_tenor = :target_tenor",
        "p.horizon = :horizon",
    ]
    params: dict[str, Any] = {
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "horizon": horizon,
        "limit": min(max(limit, 1), 1000),
        "offset": max(offset, 0),
    }
    if start_date:
        filters.append("p.predict_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        filters.append("p.predict_date <= :end_date")
        params["end_date"] = end_date
    full_where = "WHERE " + " AND ".join(filters)
    count_sql = text(
        f"SELECT COUNT(*) FROM t_scheme_predictions p {full_where}"
    )
    data_sql = text(
        f"""
        SELECT id, scheme_version, scheme_id, target_tenor, horizon,
               predict_date, feature_date, target_date,
               prediction_phase, predicted_direction, confidence, model_version, extra,
               created_at, updated_at
        FROM t_scheme_predictions
        AS p
        {full_where}
        ORDER BY predict_date DESC, scheme_id, target_tenor
        LIMIT :limit OFFSET :offset
        """
    )
    with engine.connect() as conn:
        total = conn.execute(count_sql, params).scalar_one()
        rows = _apply_prediction_corrections(
            conn,
            conn.execute(data_sql, params).mappings().all(),
        )
    return {
        "total": total,
        "limit": params["limit"],
        "offset": params["offset"],
        "items": [
            {
                "scheme_id": scheme_id,
                "base_scheme_id": row["scheme_id"],
                "target_tenor": row["target_tenor"],
                "target_label": _target_label(row["target_tenor"], target_labels),
                "horizon": row["horizon"],
                "predict_date": _iso(row["predict_date"]),
                "feature_date": _row_feature_date(row),
                "target_date": _iso(row["target_date"]),
                "prediction_phase": _row_prediction_phase(row),
                "predicted_direction": row["predicted_direction"],
                "confidence": row["confidence"],
                "model_version": row["model_version"],
                "extra": _json_value(row["extra"], {}),
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ],
    }


def _apply_prediction_corrections(
    connection: Connection,
    prediction_rows: list[Any],
) -> list[Any]:
    prediction_ids = sorted({int(row["id"]) for row in prediction_rows})
    if not prediction_ids:
        return prediction_rows
    statement = text(
        """
        SELECT prediction_id, scheme_id, target_tenor, horizon,
               predict_date, feature_date, target_date,
               scheme_version, prediction_phase,
               original_direction, corrected_direction, operation_id
        FROM t_scheme_prediction_corrections
        WHERE prediction_id IN :prediction_ids
        """
    ).bindparams(bindparam("prediction_ids", expanding=True))
    corrections = connection.execute(
        statement,
        {"prediction_ids": prediction_ids},
    ).mappings().all()
    corrected, _diagnostics = apply_live_prediction_corrections(
        prediction_rows,
        corrections,
    )
    return corrected


def list_actuals(
    engine: Engine,
    tenor: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """分页返回实际方向明细。"""
    filters = []
    params: dict[str, Any] = {"limit": min(max(limit, 1), 1000), "offset": max(offset, 0)}
    if tenor:
        filters.append("tenor = :tenor")
        params["tenor"] = tenor
    if start_date:
        filters.append("trade_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        filters.append("trade_date <= :end_date")
        params["end_date"] = end_date
    where_sql = "WHERE " + " AND ".join(filters) if filters else ""
    count_sql = text(f"SELECT COUNT(*) FROM t_scheme_actuals {where_sql}")
    data_sql = text(
        f"""
        SELECT tenor, trade_date, close_yield, direction_1d, direction_5d, created_at, updated_at
        FROM t_scheme_actuals
        {where_sql}
        ORDER BY trade_date DESC, tenor
        LIMIT :limit OFFSET :offset
        """
    )
    with engine.connect() as conn:
        total = conn.execute(count_sql, params).scalar_one()
        rows = conn.execute(data_sql, params).mappings().all()
    return {
        "total": total,
        "limit": params["limit"],
        "offset": params["offset"],
        "items": [
            {
                "tenor": row["tenor"],
                "trade_date": _iso(row["trade_date"]),
                "close_yield": row["close_yield"],
                "direction_1d": row["direction_1d"],
                "direction_5d": row["direction_5d"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ],
    }


def _require_single_actual_direction(
    variants: Any,
    *,
    fact: str,
    target_date: str,
    target_tenor: str,
) -> None:
    """周/月 actual 表按事实键可能有多行，方向冲突时必须 fail-closed。

    这两张表的唯一键是 ``(tenor, predict_date, target_rule)``，而 join 键是
    ``(tenor, target_date, target_rule)``，因此同一事实键合法地可以出现多行。
    方向一致时折叠即可；方向冲突说明事实本身矛盾，不得静默取其中一条。
    """
    if variants is not None and int(variants) > 1:
        raise ValueError(
            f"conflicting {fact} actual directions for "
            f"tenor={target_tenor} target_date={target_date}: "
            f"{int(variants)} distinct values"
        )


def _registry_scheme_row(engine: Engine, scheme_id: str) -> dict[str, Any]:
    """读取单个 registry 业务方案行；metrics/API 只接受该 ID。"""
    sql = text(
        """
        SELECT scheme_id, base_scheme_id, name, description, horizon, task_type, frequency,
               target_tenor, schedule_cron, schedule_timezone, status,
               deployed_at, created_at, updated_at
        FROM t_scheme_registry
        WHERE scheme_id = :scheme_id
          AND status = 'active'
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"scheme_id": scheme_id}).mappings().first()
    if row is None:
        raise LookupError(f"registry scheme not found: {scheme_id}")
    return {
        "scheme_id": row["scheme_id"],
        "base_scheme_id": row["base_scheme_id"],
        "name": row["name"],
        "description": row["description"],
        "horizon": row["horizon"],
        "task_type": _require_task_type(row, context="active registry row"),
        "frequency": row["frequency"],
        "target_tenor": row["target_tenor"],
        "schedule_cron": row["schedule_cron"],
        "schedule_timezone": row["schedule_timezone"],
        "status": row["status"],
        "deployed_at": _require_deployed_at(row, context="active registry row"),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def scheme_metrics(
    engine: Engine,
    scheme_id: str,
    start_month: str | None = None,
    end_month: str | None = None,
) -> dict[str, Any]:
    """实时计算单个 registry 业务方案的准确率指标。"""
    registry_row = _registry_scheme_row(engine, scheme_id)
    base_scheme_id = str(registry_row["base_scheme_id"])
    target_tenor = str(registry_row["target_tenor"])
    horizon = int(registry_row["horizon"])
    task_type = str(registry_row["task_type"])
    weekly_target_rule = WEEKLY_TASK_TARGET_RULES.get(task_type, WEEKLY_TARGET_RULE)
    period_average_target_rule = PERIOD_AVERAGE_TASK_TARGET_RULES.get(
        task_type,
        TASK_COMBINATIONS["monthly_average"][1],
    )
    target_labels = _target_labels(engine)
    filters = [
        "p.scheme_id = :base_scheme_id",
        "p.target_tenor = :target_tenor",
        "p.horizon = :horizon",
    ]
    params: dict[str, Any] = {
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "horizon": horizon,
        "weekly_target_rule": weekly_target_rule,
        "monthly_target_rule": MONTHLY_TARGET_RULE,
        "period_average_target_rule": period_average_target_rule,
    }

    sql = text(
        f"""
        SELECT p.id, p.run_id, p.scheme_version, p.scheme_id, p.target_tenor, p.horizon,
               p.predict_date, p.feature_date, p.target_date,
               p.prediction_phase, p.predicted_direction, p.confidence, p.model_version, p.extra,
               a.direction_1d, a.direction_5d, wa.direction_weekly, ma.direction_monthly,
               pa.actual_direction AS period_average_direction,
               wa.direction_variants AS weekly_direction_variants,
               ma.direction_variants AS monthly_direction_variants,
               pa.direction_variants AS period_average_direction_variants
        FROM t_scheme_predictions p
        LEFT JOIN t_scheme_actuals a
          ON a.tenor = p.target_tenor
         AND a.trade_date = p.target_date
        LEFT JOIN (
            SELECT tenor, target_date, target_rule,
                   MIN(direction_weekly) AS direction_weekly,
                   COUNT(DISTINCT direction_weekly) AS direction_variants
            FROM t_scheme_weekly_actuals
            GROUP BY tenor, target_date, target_rule
        ) wa
          ON wa.tenor = p.target_tenor
         AND wa.target_date = p.target_date
         AND wa.target_rule = :weekly_target_rule
        LEFT JOIN (
            SELECT tenor, target_date, target_rule,
                   MIN(direction_monthly) AS direction_monthly,
                   COUNT(DISTINCT direction_monthly) AS direction_variants
            FROM t_scheme_monthly_actuals
            GROUP BY tenor, target_date, target_rule
        ) ma
          ON ma.tenor = p.target_tenor
         AND ma.target_date = p.target_date
         AND ma.target_rule = :monthly_target_rule
        LEFT JOIN (
            SELECT tenor, target_date, target_rule,
                   MIN(actual_direction) AS actual_direction,
                   COUNT(DISTINCT actual_direction) AS direction_variants
            FROM t_scheme_period_average_actuals
            GROUP BY tenor, target_date, target_rule
        ) pa
          ON pa.tenor = p.target_tenor
         AND pa.target_date = p.target_date
         AND pa.target_rule = :period_average_target_rule
        WHERE {" AND ".join(filters)}
        ORDER BY p.predict_date, p.target_date, p.target_tenor
        """
    )
    with engine.connect() as conn:
        raw_rows = _apply_prediction_corrections(
            conn,
            conn.execute(sql, params).mappings().all(),
        )

    display_until = _today_iso()
    raw_rows = choose_live_prediction_rows(
        raw_rows,
        display_until=display_until,
        task_type_by_scheme={
            (base_scheme_id, target_tenor, horizon): task_type,
        },
    )

    daily_rows: list[dict[str, Any]] = []
    matched_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        extra = _json_value(row["extra"], {})
        predict_date = _iso(row["predict_date"])
        target_date = _require_target_date(row["target_date"], context="scheme metrics", row=row)
        metric_month = _scheme_metric_month(
            row["horizon"],
            predict_date,
            target_date,
            extra,
        )
        if start_month and metric_month < start_month:
            continue
        if end_month and metric_month > end_month:
            continue
        if task_type in WEEKLY_TASK_TARGET_RULES:
            _require_single_actual_direction(
                row["weekly_direction_variants"],
                fact="weekly",
                target_date=target_date,
                target_tenor=target_tenor,
            )
            actual_direction = row["direction_weekly"]
        elif task_type == "monthly":
            _require_single_actual_direction(
                row["monthly_direction_variants"],
                fact="monthly",
                target_date=target_date,
                target_tenor=target_tenor,
            )
            actual_direction = row["direction_monthly"]
        elif task_type in PERIOD_AVERAGE_TASK_TARGET_RULES:
            _require_single_actual_direction(
                row["period_average_direction_variants"],
                fact="period_average",
                target_date=target_date,
                target_tenor=target_tenor,
            )
            actual_direction = row["period_average_direction"]
        elif row["horizon"] == 1:
            actual_direction = row["direction_1d"]
        else:
            actual_direction = row["direction_5d"]
        item = {
            "scheme_id": scheme_id,
            "base_scheme_id": base_scheme_id,
            "run_id": row["run_id"],
            "target_tenor": row["target_tenor"],
            "horizon": row["horizon"],
            "predict_date": predict_date,
            "feature_date": _row_feature_date(row, extra),
            "target_date": target_date,
            "prediction_phase": _row_prediction_phase(row, extra),
            "scheme_version": row["scheme_version"],
            "request_id": extra.get("request_id"),
            "data_snapshot_id": extra.get("data_snapshot_id"),
            "runtime_type": extra.get("runtime_type"),
            "predicted_direction": row["predicted_direction"],
            "actual_direction": actual_direction,
            "is_correct": None if actual_direction is None else row["predicted_direction"] == actual_direction,
            "confidence": row["confidence"],
            "model_version": row["model_version"],
        }
        daily_rows.append(item)
        if actual_direction is not None:
            item = {**item, "_metric_month": metric_month}
            matched_rows.append(item)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in matched_rows:
        grouped[row["_metric_month"]].append(row)
    monthly_metrics = []
    for month in sorted(grouped):
        monthly_metrics.append({"month": month, **_metric_block(grouped[month])})

    return {
        "scheme_id": scheme_id,
        "base_scheme_id": base_scheme_id,
        "target_tenor": target_tenor,
        "target_label": _target_label(target_tenor, target_labels),
        "task_type": registry_row["task_type"],
        "registry": registry_row,
        "start_month": start_month,
        "end_month": end_month,
        "monthly_metrics": monthly_metrics,
        "summary": _metric_block(matched_rows),
        "phase_ranges": _phase_ranges(daily_rows),
        "daily_rows": daily_rows,
    }


def _scheme_metric_month(horizon: Any, predict_date: str, target_date: str, extra: dict[str, Any]) -> str:
    return _require_target_date(target_date, context="scheme metric month")[:7]


def _row_feature_date(row: Any, extra: dict[str, Any] | None = None) -> str:
    extra = extra if extra is not None else _json_value(row["extra"], {})
    return _iso(row["feature_date"]) or _iso(extra.get("feature_date")) or ""


def _row_prediction_phase(row: Any, extra: dict[str, Any] | None = None) -> str:
    extra = extra if extra is not None else _json_value(row["extra"], {})
    return str(row["prediction_phase"] or extra.get("prediction_phase") or "")


def _phase_ranges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        phase = str(row.get("prediction_phase") or "")
        if phase:
            grouped[phase].append(row)
    result: list[dict[str, Any]] = []
    for phase, items in grouped.items():
        predict_dates = sorted(str(row.get("predict_date") or "") for row in items if row.get("predict_date"))
        target_dates = sorted(str(row.get("target_date") or "") for row in items if row.get("target_date"))
        result.append(
            {
                "prediction_phase": phase,
                "start_predict_date": predict_dates[0] if predict_dates else "",
                "end_predict_date": predict_dates[-1] if predict_dates else "",
                "start_target_date": target_dates[0] if target_dates else "",
                "end_target_date": target_dates[-1] if target_dates else "",
                "rows": len(items),
            }
        )
    return sorted(result, key=lambda item: (item["start_predict_date"], item["prediction_phase"]))


def _today_iso() -> str:
    return os.getenv("BOND_FACTOR_LAB_TODAY") or date.today().isoformat()


def _backtest_run_candidates(
    engine: Engine,
    *,
    base_scheme_ids: list[str],
) -> list[Any]:
    if not base_scheme_ids:
        return []
    data_sources = sorted(set(BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE.values()))
    statement = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, summary, report_path, created_at, updated_at
        FROM t_backtest_runs
        WHERE status = :success_status
          AND scheme_id IN :base_scheme_ids
          AND data_source IN :data_sources
        """
    ).bindparams(
        bindparam("base_scheme_ids", expanding=True),
        bindparam("data_sources", expanding=True),
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                statement,
                {
                    "success_status": "success",
                    "base_scheme_ids": base_scheme_ids,
                    "data_sources": data_sources,
                },
            ).mappings().all()
        )


def _validate_default_backtest_registry_scopes(
    schemes: list[dict[str, Any]],
    *,
    selected_by_registry: dict[str, Any],
    registry_rows: list[dict[str, Any]],
) -> None:
    schemes_by_scope = {
        (str(item["scheme_id"]), int(item["run_id"])): item
        for item in schemes
    }
    for registry_row in registry_rows:
        registry_scheme_id = str(registry_row["scheme_id"])
        selected_run = selected_by_registry.get(registry_scheme_id)
        if selected_run is None:
            continue
        run_id = int(selected_run["id"])
        item = schemes_by_scope.get((registry_scheme_id, run_id))
        if item is None:
            raise ValueError(
                f"base_scheme_id={registry_row['base_scheme_id']} "
                f"target_tenor={registry_row['target_tenor']} has no detail "
                f"for selected backtest run_id={run_id}"
            )
        registry_horizon = int(registry_row["horizon"])
        daily_rows = item.get("daily_rows")
        if not isinstance(daily_rows, list) or not daily_rows:
            raise ValueError(
                "backtest detail horizon does not match Registry scheme "
                f"{registry_scheme_id}: detail=None "
                f"registry={registry_horizon}"
            )
        for detail_row in daily_rows:
            detail_horizon = (
                detail_row.get("horizon")
                if isinstance(detail_row, dict)
                else None
            )
            if type(detail_horizon) is not int or detail_horizon != registry_horizon:
                raise ValueError(
                    "backtest detail horizon does not match Registry scheme "
                    f"{registry_scheme_id}: detail={detail_horizon} "
                    f"registry={registry_horizon}"
                )


def backtest_factor_lab_results(
    engine: Engine,
    benchmark_id: str | None = None,
    data_source: str | None = None,
) -> dict[str, Any]:
    """返回前端方案矩阵可直接展示的最新历史回测结果。"""
    scheme_meta = _backtest_scheme_meta(engine)
    target_labels = _target_labels(engine)
    visible_targets = _visible_targets(engine)
    run_sql = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, summary, report_path, created_at, updated_at
        FROM v_latest_backtest_run
        WHERE (:benchmark_id IS NULL OR benchmark_id = :benchmark_id)
          AND (
                (:auto_source = 1 AND data_source IN (
                    'framework_db_aligned',
                    'blackbox_v2_current_snapshot_as_of'
                ))
                OR (:auto_source = 0 AND data_source = :data_source)
          )
        ORDER BY benchmark_id, scheme_id, updated_at DESC, id DESC
        """
    )
    use_default_selector = benchmark_id is None and data_source is None
    registry_rows: list[dict[str, Any]] = []
    selected_by_registry: dict[str, Any] = {}
    if use_default_selector:
        registry_rows = list(scheme_meta.values())
        candidate_rows = _backtest_run_candidates(
            engine,
            base_scheme_ids=sorted(
                {str(row["base_scheme_id"]) for row in registry_rows}
            ),
        )
        selected_by_registry = choose_latest_backtest_runs(
            candidate_rows,
            registry_rows,
        )
        run_rows_by_id = {
            int(row["id"]): row for row in selected_by_registry.values()
        }
        run_rows = list(run_rows_by_id.values())
    else:
        with engine.connect() as conn:
            run_rows = conn.execute(
                run_sql,
                {
                    "benchmark_id": benchmark_id,
                    "data_source": data_source,
                    "auto_source": 1 if data_source is None else 0,
                },
            ).mappings().all()

    if data_source is None and not use_default_selector:
        runtime_types_by_scheme: dict[str, set[str]] = defaultdict(set)
        for (base_scheme_id, _target_tenor), meta in scheme_meta.items():
            runtime_types_by_scheme[base_scheme_id].add(str(meta["runtime_type"]))
        inconsistent = {
            scheme_id: sorted(runtime_types)
            for scheme_id, runtime_types in runtime_types_by_scheme.items()
            if len(runtime_types) != 1
        }
        if inconsistent:
            raise ValueError(f"active Registry runtime_type is inconsistent: {inconsistent}")
        preferred_by_scheme = {
            scheme_id: BACKTEST_DEFAULT_SOURCE_BY_RUNTIME_TYPE[next(iter(runtime_types))]
            for scheme_id, runtime_types in runtime_types_by_scheme.items()
        }
        run_rows = [
            row
            for row in run_rows
            if str(row["data_source"]) == preferred_by_scheme.get(str(row["scheme_id"]))
        ]

    schemes: list[dict[str, Any]] = []
    for row in run_rows:
        run = _backtest_run_row(row)
        audit_daily_rows = _backtest_frontend_daily_rows(engine, run["id"])
        _validate_backtest_prediction_details(run, audit_daily_rows)
        daily_rows = {
            tenor: [
                detail
                for detail in details
                if is_factor_lab_history_visible(str(detail["predict_date"]))
            ]
            for tenor, details in audit_daily_rows.items()
        }
        daily_rows = {
            tenor: details for tenor, details in daily_rows.items() if details
        }
        metrics = _backtest_frontend_monthly_metrics_from_daily_rows(daily_rows)
        tenors = sorted(metrics, key=_tenor_sort_key)
        for tenor in tenors:
            if tenor not in visible_targets:
                continue
            meta = scheme_meta.get((run["scheme_id"], tenor), {})
            if not meta:
                logger.warning(
                    "Skip unregistered backtest row: run_id=%s base_scheme_id=%s target_tenor=%s",
                    run["id"],
                    run["scheme_id"],
                    tenor,
                )
                continue
            target_label = _target_label(tenor, target_labels)
            horizon = _infer_horizon(metrics.get(tenor), daily_rows.get(tenor), meta)
            registry_id = str(meta["scheme_id"])
            base_scheme_id = str(meta["base_scheme_id"])
            task_type = str(meta["task_type"])
            frequency = meta["frequency"]
            scheme_name = _backtest_scheme_name(meta, base_scheme_id)
            benchmark_label = _backtest_benchmark_label(run["benchmark_id"])
            data_source_label = _backtest_data_source_label(run["data_source"])
            display_name = _backtest_display_name(
                meta=meta,
                scheme_id=base_scheme_id,
                target_label=target_label,
                data_source=run["data_source"],
            )
            schemes.append(
                {
                    "id": f'{run["benchmark_id"]}:{registry_id}:{run["data_source"]}',
                    "run_id": run["id"],
                    "benchmark_id": run["benchmark_id"],
                    "benchmark_label": benchmark_label,
                    "scheme_id": registry_id,
                    "base_scheme_id": base_scheme_id,
                    "runtime_type": meta["runtime_type"],
                    "scheme_name": scheme_name,
                    "description": str(meta.get("description") or ""),
                    "data_source": run["data_source"],
                    "data_source_label": data_source_label,
                    "target_tenor": tenor,
                    "target_label": target_label,
                    "horizon": horizon,
                    "task_type": task_type,
                    "frequency": frequency,
                    "deployed_at": meta.get("deployed_at"),
                    "created_at": meta.get("created_at"),
                    "updated_at": meta.get("updated_at"),
                    "display_name": display_name,
                    "name": display_name,
                    "status": "complete",
                    "scheme_version": run["summary"].get("scheme_version"),
                    "data_snapshot_id": run["summary"].get("data_snapshot_id"),
                    "harness_run_id": run["summary"].get("harness_run_id"),
                    "generation_id": run["summary"].get("generation_id"),
                    "start_date": run["start_date"],
                    "end_date": run["end_date"],
                    "latest_run": {
                        "date": run["end_date"],
                        "status": run["status"],
                        "updated_at": run["updated_at"],
                    },
                    "monthly_metrics": metrics.get(tenor, []),
                    "daily_rows": daily_rows.get(tenor, []),
                    "summary": _metric_block(daily_rows.get(tenor, [])),
                }
            )

    if benchmark_id is None:
        latest_by_scope: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in schemes:
            key = (
                str(item["runtime_type"]),
                str(item["base_scheme_id"]),
                str(item["target_tenor"]),
                str(item["data_source"]),
            )
            current = latest_by_scope.get(key)
            item_rank = (
                str(item["latest_run"].get("updated_at") or ""),
                int(item["run_id"]),
            )
            current_rank = (
                str(current["latest_run"].get("updated_at") or ""),
                int(current["run_id"]),
            ) if current is not None else ("", -1)
            if current is None or item_rank > current_rank:
                latest_by_scope[key] = item
        schemes = list(latest_by_scope.values())

    if use_default_selector:
        _validate_default_backtest_registry_scopes(
            schemes,
            selected_by_registry=selected_by_registry,
            registry_rows=registry_rows,
        )
        schemes.sort(
            key=lambda item: (
                str(item["scheme_id"]),
                int(item["run_id"]),
                str(item["target_tenor"]),
                str(item["data_source"]),
            )
        )

    selected_sources = {str(item["data_source"]) for item in schemes}
    if len(selected_sources) == 1:
        resolved_data_source = next(iter(selected_sources))
    elif selected_sources:
        resolved_data_source = "runtime_default"
    else:
        resolved_data_source = data_source or "framework_db_aligned"

    return {
        "benchmark_id": benchmark_id or "all",
        "benchmark_label": _backtest_benchmark_label(benchmark_id),
        "data_source": resolved_data_source,
        "data_source_label": _backtest_data_source_label(resolved_data_source),
        "target_labels": target_labels,
        "schemes": schemes,
    }


def _backtest_frontend_monthly_metrics_from_daily_rows(
    daily_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for tenor, rows in daily_rows.items():
        for row in rows:
            if row.get("actual_direction") is None or row.get("predicted_direction") is None:
                continue
            month = _require_target_date(row.get("target_date"), context="backtest monthly metrics", row=row)[:7]
            grouped[tenor][month].append(row)

    result: dict[str, list[dict[str, Any]]] = {}
    for tenor, by_month in grouped.items():
        result[tenor] = []
        for month, rows in sorted(by_month.items()):
            result[tenor].append(
                {
                    "target_tenor": tenor,
                    "horizon": rows[0].get("horizon"),
                    "month": month,
                    **_metric_block(rows),
                }
            )
    return result


def _validate_backtest_prediction_details(
    run: dict[str, Any],
    daily_rows: dict[str, list[dict[str, Any]]],
) -> None:
    row_count = sum(len(rows) for rows in daily_rows.values())
    if row_count <= 0:
        raise ValueError(f"backtest run id={run['id']} has no backtest prediction details")
    required_fields = (
        "target_tenor",
        "horizon",
        "predict_date",
        "feature_date",
        "target_date",
        "predicted_direction",
        "actual_direction",
    )
    for rows in daily_rows.values():
        for row in rows:
            missing = [
                field
                for field in required_fields
                if row.get(field) is None or str(row.get(field)).strip() == ""
            ]
            if not missing:
                continue
            raise ValueError(
                "backtest run id="
                f"{run['id']} has missing required backtest prediction fields "
                f"{','.join(missing)} "
                f"target_tenor={row.get('target_tenor')} "
                f"predict_date={row.get('predict_date')} "
                f"target_date={row.get('target_date')}"
            )


def _backtest_frontend_daily_rows(engine: Engine, run_id: int) -> dict[str, list[dict[str, Any]]]:
    sql = text(
        """
        SELECT target_tenor, horizon, predict_date, feature_date, target_date,
               label, predicted_direction, confidence
        FROM t_backtest_predictions
        WHERE run_id = :run_id
        ORDER BY target_tenor, predict_date
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"run_id": run_id}).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        actual_direction = row["label"]
        grouped[row["target_tenor"]].append(
            {
                "target_tenor": row["target_tenor"],
                "horizon": row["horizon"],
                "predict_date": _iso(row["predict_date"]),
                "feature_date": _iso(row["feature_date"]),
                "target_date": _iso(row["target_date"]),
                "predicted_direction": row["predicted_direction"],
                "actual_direction": actual_direction,
                "is_correct": None if actual_direction is None else row["predicted_direction"] == actual_direction,
                "confidence": row["confidence"],
            }
        )
    return grouped


def _ratio_to_percent(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value) * 100, 1)


def _infer_horizon(monthly_rows: list[dict[str, Any]] | None, daily_rows: list[dict[str, Any]] | None, meta: dict[str, Any]) -> int | None:
    if monthly_rows:
        return monthly_rows[0].get("horizon")
    if daily_rows:
        return daily_rows[0].get("horizon")
    value = meta.get("horizon")
    return int(value) if value is not None else None


def _tenor_sort_key(tenor: str) -> tuple[int, str]:
    try:
        return (int(str(tenor).rstrip("Y")), str(tenor))
    except ValueError:
        return (999, str(tenor))


def list_backtest_runs(
    engine: Engine,
    scheme_id: str | None = None,
    benchmark_id: str | None = None,
) -> list[dict[str, Any]]:
    """返回历史复现 run 列表。"""
    filters = []
    params: dict[str, Any] = {}
    if scheme_id:
        filters.append("scheme_id = :scheme_id")
        params["scheme_id"] = scheme_id
    if benchmark_id:
        filters.append("benchmark_id = :benchmark_id")
        params["benchmark_id"] = benchmark_id
    where_sql = "WHERE " + " AND ".join(filters) if filters else ""
    sql = text(
        f"""
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, summary, report_path, created_at, updated_at
        FROM t_backtest_runs
        {where_sql}
        ORDER BY scheme_id, start_date DESC, data_source
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [_backtest_run_row(row) for row in rows]


def get_backtest_run(
    engine: Engine,
    run_id: int,
    include_predictions: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any] | None:
    """返回单个历史复现 run，必要时带分页预测明细。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
                       status, summary, report_path, created_at, updated_at
                FROM t_backtest_runs
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id},
        ).mappings().first()
        if row is None:
            return None
        result = _backtest_run_row(row)
        if include_predictions:
            params = {"run_id": run_id, "limit": min(max(limit, 1), 2000), "offset": max(offset, 0)}
            total = conn.execute(
                text("SELECT COUNT(*) FROM t_backtest_predictions WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
            predictions = conn.execute(
                text(
                    """
                    SELECT target_tenor, horizon, predict_date, feature_date, target_date,
                           label, predicted_direction, model_pred, confidence, source_row, extra
                    FROM t_backtest_predictions
                    WHERE run_id = :run_id
                    ORDER BY target_tenor, predict_date
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().all()
            result["predictions"] = {
                "total": total,
                "limit": params["limit"],
                "offset": params["offset"],
                "items": [_backtest_prediction_row(item) for item in predictions],
            }
    return result


def backtest_diffs(engine: Engine, run_id: int) -> dict[str, Any] | None:
    """返回 framework run 与 baseline 的差异摘要。"""
    run = get_backtest_run(engine, run_id)
    if run is None:
        return None
    summary = run.get("summary") or {}
    comparison = summary.get("comparison")
    baseline = _find_baseline_run(engine, run)
    return {
        "run": run,
        "baseline_run": baseline,
        "comparison": comparison
        or {
            "baseline_rows": None,
            "candidate_rows": summary.get("row_count"),
            "matched_rows": None,
            "mismatch_count": None,
            "mismatch_rows": [],
        },
    }


def list_data_checks(engine: Engine, benchmark_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """返回最近的数据一致性检查结果。"""
    filters = []
    params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
    if benchmark_id:
        filters.append("benchmark_id = :benchmark_id")
        params["benchmark_id"] = benchmark_id
    where_sql = "WHERE " + " AND ".join(filters) if filters else ""
    sql = text(
        f"""
        SELECT id, benchmark_id, check_name, status, source_path,
               row_count_csv, row_count_db, col_count_csv, col_count_db,
               date_min_csv, date_max_csv, date_min_db, date_max_db,
               csv_only_columns, db_only_columns, target_max_abs_diff,
               overall_max_abs_diff, missing_diff_count, first_diff, report, created_at
        FROM t_backtest_reproduction_checks
        {where_sql}
        ORDER BY id DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [_data_check_row(row) for row in rows]


def _backtest_run_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "benchmark_id": row["benchmark_id"],
        "scheme_id": row["scheme_id"],
        "data_source": row["data_source"],
        "start_date": _iso(row["start_date"]),
        "end_date": _iso(row["end_date"]),
        "status": row["status"],
        "summary": _json_value(row["summary"], {}),
        "report_path": row["report_path"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _backtest_prediction_row(row: Any) -> dict[str, Any]:
    return {
        "target_tenor": row["target_tenor"],
        "horizon": row["horizon"],
        "predict_date": _iso(row["predict_date"]),
        "feature_date": _iso(row["feature_date"]),
        "target_date": _iso(row["target_date"]),
        "label": row["label"],
        "predicted_direction": row["predicted_direction"],
        "model_pred": row["model_pred"],
        "confidence": row["confidence"],
        "source_row": _json_value(row["source_row"], {}),
        "extra": _json_value(row["extra"], {}),
    }


def _data_check_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "benchmark_id": row["benchmark_id"],
        "check_name": row["check_name"],
        "status": row["status"],
        "source_path": row["source_path"],
        "csv": {
            "rows": row["row_count_csv"],
            "columns": row["col_count_csv"],
            "date_min": _iso(row["date_min_csv"]),
            "date_max": _iso(row["date_max_csv"]),
        },
        "db_aligned": {
            "rows": row["row_count_db"],
            "columns": row["col_count_db"],
            "date_min": _iso(row["date_min_db"]),
            "date_max": _iso(row["date_max_db"]),
        },
        "csv_only_columns": _json_value(row["csv_only_columns"], []),
        "db_only_columns": _json_value(row["db_only_columns"], []),
        "target_max_abs_diff": _json_value(row["target_max_abs_diff"], {}),
        "overall_max_abs_diff": row["overall_max_abs_diff"],
        "missing_diff_count": row["missing_diff_count"],
        "first_diff": _json_value(row["first_diff"], {}),
        "report": _json_value(row["report"], {}),
        "created_at": _iso(row["created_at"]),
    }


def _find_baseline_run(engine: Engine, run: dict[str, Any]) -> dict[str, Any] | None:
    if run["data_source"] == "baseline_original_csv":
        return None
    sql = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, summary, report_path, created_at, updated_at
        FROM t_backtest_runs
        WHERE benchmark_id = :benchmark_id
          AND scheme_id = :scheme_id
          AND data_source = 'baseline_original_csv'
          AND start_date = :start_date
          AND end_date = :end_date
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {
                "benchmark_id": run["benchmark_id"],
                "scheme_id": run["scheme_id"],
                "start_date": run["start_date"],
                "end_date": run["end_date"],
            },
        ).mappings().first()
    return _backtest_run_row(row) if row else None
