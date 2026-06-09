from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from scheduler.discovery import discover_schemes
from scheduler.repository import sync_scheme_registry


_REGISTRY_SYNC_SIGNATURES: dict[str, tuple[tuple[str, int, int], ...]] = {}


DEFAULT_TARGET_LABELS = {
    "3Y": "3Y国债活跃",
    "5Y": "5Y国债活跃",
    "7Y": "7Y国债活跃",
    "10Y": "10Y国债活跃",
}
DEFAULT_TARGET_ORDER = ["3Y", "5Y", "7Y", "10Y"]
BACKTEST_BENCHMARK_LABELS = {
    "model_muti_0529": "0529历史基准",
}
BACKTEST_DATA_SOURCE_LABELS = {
    "baseline_original_csv": "原始代码基准CSV回测",
    "framework_original_csv": "框架算法基准CSV回测",
    "framework_db_aligned": "当前DB对齐回测",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


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


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 1)


def _direction_dist(rows: list[dict], key: str) -> dict[str, int]:
    return {
        "up": sum(1 for row in rows if row[key] == 1),
        "down": sum(1 for row in rows if row[key] == -1),
        "flat": sum(1 for row in rows if row[key] == 0),
    }


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


def _backtest_benchmark_label(benchmark_id: str) -> str:
    return BACKTEST_BENCHMARK_LABELS.get(str(benchmark_id), str(benchmark_id))


def _backtest_data_source_label(data_source: str) -> str:
    return BACKTEST_DATA_SOURCE_LABELS.get(str(data_source), str(data_source))


def _backtest_scheme_name(meta: dict[str, Any], scheme_id: str) -> str:
    return str(meta.get("name") or scheme_id)


def _backtest_display_name(
    *,
    meta: dict[str, Any],
    scheme_id: str,
    target_label: str,
    data_source: str,
) -> str:
    return f"{_backtest_scheme_name(meta, scheme_id)}｜{target_label}｜{_backtest_data_source_label(data_source)}"


def list_targets(engine: Engine) -> list[dict[str, Any]]:
    """返回 Y 标的注册表。"""
    return _list_target_registry(engine)


def _metric_block(rows: list[dict]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(1 for row in rows if row["predicted_direction"] == row["actual_direction"])
    pred_up = sum(1 for row in rows if row["predicted_direction"] == 1)
    pred_down = sum(1 for row in rows if row["predicted_direction"] == -1)
    actual_up = sum(1 for row in rows if row["actual_direction"] == 1)
    actual_down = sum(1 for row in rows if row["actual_direction"] == -1)
    up_tp = sum(1 for row in rows if row["predicted_direction"] == 1 and row["actual_direction"] == 1)
    down_tp = sum(1 for row in rows if row["predicted_direction"] == -1 and row["actual_direction"] == -1)
    accuracy = _percent(correct, total)
    return {
        "total": total,
        "samples": total,
        "correct": correct,
        "accuracy": accuracy,
        "overall": accuracy,
        "up_precision": _percent(up_tp, pred_up),
        "up_recall": _percent(up_tp, actual_up),
        "down_precision": _percent(down_tp, pred_down),
        "down_recall": _percent(down_tp, actual_down),
        "actual_dist": _direction_dist(rows, "actual_direction"),
        "predicted_dist": _direction_dist(rows, "predicted_direction"),
    }


def reset_registry_sync_cache() -> None:
    """清空 registry sync 的 config 签名缓存，供测试和显式刷新使用。"""
    _REGISTRY_SYNC_SIGNATURES.clear()


def sync_registry_from_configs(engine: Engine, schemes_root: Path | None = None, force: bool = False) -> bool:
    """把 schemes/ 配置同步到 registry；配置未变化时跳过写库。"""
    signature = _scheme_config_signature(schemes_root)
    cache_key = str(Path(schemes_root).resolve()) if schemes_root is not None else "__default__"
    if not force and _REGISTRY_SYNC_SIGNATURES.get(cache_key) == signature:
        return False
    sync_scheme_registry(engine, discover_schemes(schemes_root) if schemes_root is not None else discover_schemes())
    _REGISTRY_SYNC_SIGNATURES[cache_key] = signature
    return True


def _scheme_config_signature(schemes_root: Path | None = None) -> tuple[tuple[str, int, int], ...]:
    root = Path(schemes_root) if schemes_root is not None else Path(__file__).resolve().parents[1] / "schemes"
    items: list[tuple[str, int, int]] = []
    for config_path in sorted(root.glob("*/config.yaml")):
        stat = config_path.stat()
        items.append((config_path.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(items)


def list_schemes(engine: Engine) -> list[dict[str, Any]]:
    """返回注册方案及最近一次运行状态。

    只读路径：不再触发 registry 写库同步，直接读取 registry 表当前内容。
    registry 同步由应用启动钩子与受保护的 admin 端点负责。
    """
    target_labels = _target_labels(engine)
    sql = text(
        """
        SELECT r.scheme_id, r.name, r.description, r.horizon, r.tenors, r.frequency,
               r.schedule_cron, r.schedule_timezone, r.status,
               l.run_date AS last_run_date, l.status AS last_run_status,
               l.duration_sec AS last_run_duration_sec, l.error_msg AS last_run_error_msg,
               l.created_at AS last_run_at
        FROM t_scheme_registry r
        LEFT JOIN (
            SELECT log.*
            FROM t_scheme_run_log log
            INNER JOIN (
                SELECT scheme_id, MAX(id) AS id
                FROM t_scheme_run_log
                GROUP BY scheme_id
            ) latest ON latest.id = log.id
        ) l ON l.scheme_id = r.scheme_id
        ORDER BY r.status = 'active' DESC, r.scheme_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return [
        {
            "scheme_id": row["scheme_id"],
            "name": row["name"],
            "description": row["description"],
            "horizon": row["horizon"],
            "tenors": _json_value(row["tenors"], []),
            "target_labels": {
                tenor: _target_label(tenor, target_labels)
                for tenor in _json_value(row["tenors"], [])
            },
            "frequency": row["frequency"],
            "schedule": {
                "cron": row["schedule_cron"],
                "timezone": row["schedule_timezone"],
            },
            "status": row["status"],
            "last_run": None
            if row["last_run_date"] is None
            else {
                "date": _iso(row["last_run_date"]),
                "status": row["last_run_status"],
                "duration_sec": row["last_run_duration_sec"],
                "error_msg": row["last_run_error_msg"],
                "created_at": _iso(row["last_run_at"]),
            },
        }
        for row in rows
    ]


def _scheme_meta_from_config(cfg: Any) -> dict[str, Any]:
    return {
        "scheme_id": cfg.scheme_id,
        "name": cfg.name,
        "description": cfg.description,
        "horizon": cfg.horizon,
        "tenors": cfg.tenors,
        "frequency": cfg.frequency,
        "schedule": {
            "cron": cfg.schedule.cron,
            "timezone": cfg.schedule.timezone,
        },
        "status": cfg.status,
    }


def _backtest_scheme_meta(engine: Engine) -> dict[str, dict[str, Any]]:
    """只读获取回测矩阵所需方案元数据，不触发 registry 同步。"""
    sql = text(
        """
        SELECT scheme_id, name, description, horizon, tenors, frequency,
               schedule_cron, schedule_timezone, status
        FROM t_scheme_registry
        """
    )
    meta: dict[str, dict[str, Any]] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
    except SQLAlchemyError as exc:
        message = str(exc)
        if "t_scheme_registry" not in message and "1146" not in message:
            raise
        rows = []

    for row in rows:
        meta[row["scheme_id"]] = {
            "scheme_id": row["scheme_id"],
            "name": row["name"],
            "description": row["description"],
            "horizon": row["horizon"],
            "tenors": _json_value(row["tenors"], []),
            "frequency": row["frequency"],
            "schedule": {
                "cron": row["schedule_cron"],
                "timezone": row["schedule_timezone"],
            },
            "status": row["status"],
        }

    for cfg in discover_schemes():
        meta.setdefault(cfg.scheme_id, _scheme_meta_from_config(cfg))
    return meta


def list_predictions(
    engine: Engine,
    scheme_id: str | None = None,
    tenor: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """分页返回预测明细。"""
    target_labels = _target_labels(engine)
    filters = ["sp.serving_status = 'approved'"]
    params: dict[str, Any] = {"limit": min(max(limit, 1), 1000), "offset": max(offset, 0)}
    if scheme_id:
        filters.append("p.scheme_id = :scheme_id")
        params["scheme_id"] = scheme_id
    if tenor:
        filters.append("p.target_tenor = :tenor")
        params["tenor"] = tenor
    if start_date:
        filters.append("p.predict_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        filters.append("p.predict_date <= :end_date")
        params["end_date"] = end_date
    where_sql = "WHERE " + " AND ".join(filters)
    serving_from_sql = """
        FROM t_scheme_serving_pointer sp
        INNER JOIN t_scheme_predictions p
          ON p.run_id = sp.serving_run_id
         AND p.scheme_id = sp.scheme_id
         AND p.target_tenor = sp.target_tenor
         AND p.predict_date = sp.predict_date
        LEFT JOIN t_scheme_runs sr
          ON sr.run_id = p.run_id
        LEFT JOIN t_input_artifacts ia
          ON ia.artifact_id = sr.input_artifact_id
    """
    count_sql = text(f"SELECT COUNT(*) {serving_from_sql} {where_sql}")
    data_sql = text(
        f"""
        SELECT p.run_id, p.scheme_version, ia.content_hash AS input_artifact_hash,
               p.scheme_id, p.target_tenor, p.horizon, p.predict_date, p.target_date,
               p.predicted_direction, p.confidence, p.model_version, p.extra,
               p.created_at, p.updated_at
        {serving_from_sql}
        {where_sql}
        ORDER BY p.predict_date DESC, p.scheme_id, p.target_tenor
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
                "run_id": row["run_id"],
                "scheme_version": row["scheme_version"],
                "input_artifact_hash": row["input_artifact_hash"],
                "scheme_id": row["scheme_id"],
                "target_tenor": row["target_tenor"],
                "target_label": _target_label(row["target_tenor"], target_labels),
                "horizon": row["horizon"],
                "predict_date": _iso(row["predict_date"]),
                "target_date": _iso(row["target_date"]),
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


def scheme_metrics(
    engine: Engine,
    scheme_id: str,
    tenor: str,
    start_month: str | None = None,
    end_month: str | None = None,
) -> dict[str, Any]:
    """实时计算方案准确率指标。"""
    target_labels = _target_labels(engine)
    filters = ["p.scheme_id = :scheme_id", "p.target_tenor = :tenor"]
    params: dict[str, Any] = {"scheme_id": scheme_id, "tenor": tenor}
    start_date = _month_start(start_month)
    end_exclusive = _month_end(end_month)
    if start_date:
        filters.append("p.predict_date >= :start_date")
        params["start_date"] = start_date
    if end_exclusive:
        filters.append("p.predict_date < :end_exclusive")
        params["end_exclusive"] = end_exclusive

    sql = text(
        f"""
        SELECT p.run_id, p.scheme_version, ia.content_hash AS input_artifact_hash,
               p.scheme_id, p.target_tenor, p.horizon, p.predict_date, p.target_date,
               p.predicted_direction, p.confidence, p.model_version, p.extra,
               a.direction_1d, a.direction_5d, wa.direction_weekly
        FROM t_scheme_serving_pointer sp
        INNER JOIN t_scheme_predictions p
          ON p.run_id = sp.serving_run_id
         AND p.scheme_id = sp.scheme_id
         AND p.target_tenor = sp.target_tenor
         AND p.predict_date = sp.predict_date
        LEFT JOIN t_scheme_runs sr
          ON sr.run_id = p.run_id
        LEFT JOIN t_input_artifacts ia
          ON ia.artifact_id = sr.input_artifact_id
        LEFT JOIN t_scheme_actuals a
          ON a.tenor = p.target_tenor
         AND a.trade_date = p.target_date
        LEFT JOIN t_scheme_weekly_actuals wa
          ON wa.tenor = p.target_tenor
         AND wa.predict_date = p.predict_date
         AND wa.target_date = p.target_date
        WHERE sp.serving_status = 'approved'
          AND {" AND ".join(filters)}
        ORDER BY p.predict_date, p.target_tenor
        """
    )
    with engine.connect() as conn:
        raw_rows = conn.execute(sql, params).mappings().all()

    daily_rows: list[dict[str, Any]] = []
    matched_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        extra = _json_value(row["extra"], {})
        if row["horizon"] == 1:
            actual_direction = row["direction_1d"]
        elif row["horizon"] == 6:
            actual_direction = row["direction_weekly"]
        else:
            actual_direction = row["direction_5d"]
        item = {
            "run_id": row["run_id"],
            "scheme_version": row["scheme_version"],
            "input_artifact_hash": row["input_artifact_hash"],
            "scheme_id": row["scheme_id"],
            "target_tenor": row["target_tenor"],
            "horizon": row["horizon"],
            "predict_date": _iso(row["predict_date"]),
            "target_date": _iso(row["target_date"]),
            "predicted_direction": row["predicted_direction"],
            "actual_direction": actual_direction,
            "is_correct": None if actual_direction is None else row["predicted_direction"] == actual_direction,
            "confidence": row["confidence"],
            "model_version": row["model_version"],
        }
        daily_rows.append(item)
        if actual_direction is not None:
            item = {
                **item,
                "_metric_month": _scheme_metric_month(row["horizon"], item["predict_date"], extra),
            }
            matched_rows.append(item)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in matched_rows:
        grouped[row["_metric_month"]].append(row)
    monthly_metrics = []
    for month in sorted(grouped):
        monthly_metrics.append({"month": month, **_metric_block(grouped[month])})

    return {
        "scheme_id": scheme_id,
        "tenor": tenor,
        "target_label": _target_label(tenor, target_labels),
        "start_month": start_month,
        "end_month": end_month,
        "monthly_metrics": monthly_metrics,
        "summary": _metric_block(matched_rows),
        "daily_rows": daily_rows,
    }


def _compare_metric_key(metric: str | None) -> str:
    aliases = {
        "accuracy": "overall",
        "overall": "overall",
        "up": "up_precision",
        "upPrecision": "up_precision",
        "up_precision": "up_precision",
        "down": "down_precision",
        "downPrecision": "down_precision",
        "down_precision": "down_precision",
    }
    return aliases.get(str(metric or "overall"), "overall")


def _compare_cell(block: dict[str, Any], metric_key: str) -> dict[str, Any]:
    return {
        "value": block.get(metric_key),
        "samples": block["samples"],
        "correct": block["correct"],
        "overall": block["overall"],
        "up_precision": block["up_precision"],
        "down_precision": block["down_precision"],
    }


def metrics_compare(
    engine: Engine,
    frequency: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    metric: str = "overall",
) -> dict[str, Any]:
    """跨方案、跨 tenor 聚合准确率矩阵（只读）。"""
    target_labels = _target_labels(engine)
    metric_key = _compare_metric_key(metric)
    filters = ["sp.serving_status = 'approved'"]
    params: dict[str, Any] = {}
    if frequency:
        filters.append(
            "COALESCE(r.frequency, CASE WHEN p.horizon = 6 THEN 'weekly' ELSE 'daily' END) = :frequency"
        )
        params["frequency"] = frequency
    start_date = _month_start(start_month)
    end_exclusive = _month_end(end_month)
    if start_date:
        filters.append("p.predict_date >= :start_date")
        params["start_date"] = start_date
    if end_exclusive:
        filters.append("p.predict_date < :end_exclusive")
        params["end_exclusive"] = end_exclusive

    sql = text(
        f"""
        SELECT p.scheme_id, COALESCE(r.name, p.scheme_id) AS scheme_name,
               COALESCE(r.status, 'active') AS scheme_status,
               COALESCE(r.frequency, CASE WHEN p.horizon = 6 THEN 'weekly' ELSE 'daily' END) AS frequency,
               COALESCE(r.horizon, p.horizon) AS registry_horizon,
               p.target_tenor, p.horizon, p.predict_date, p.target_date,
               p.predicted_direction, a.direction_1d, a.direction_5d, wa.direction_weekly
        FROM t_scheme_serving_pointer sp
        INNER JOIN t_scheme_predictions p
          ON p.run_id = sp.serving_run_id
         AND p.scheme_id = sp.scheme_id
         AND p.target_tenor = sp.target_tenor
         AND p.predict_date = sp.predict_date
        LEFT JOIN t_scheme_registry r
          ON r.scheme_id = p.scheme_id
        LEFT JOIN t_scheme_actuals a
          ON a.tenor = p.target_tenor
         AND a.trade_date = p.target_date
        LEFT JOIN t_scheme_weekly_actuals wa
          ON wa.tenor = p.target_tenor
         AND wa.predict_date = p.predict_date
         AND wa.target_date = p.target_date
        WHERE {" AND ".join(filters)}
        ORDER BY
          CASE WHEN COALESCE(r.status, 'active') = 'active' THEN 0 ELSE 1 END,
          p.scheme_id, p.target_tenor, p.predict_date
        """
    )
    with engine.connect() as conn:
        raw_rows = conn.execute(sql, params).mappings().all()

    schemes: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    tenors: set[str] = set()
    for row in raw_rows:
        scheme_id = row["scheme_id"]
        tenor = row["target_tenor"]
        tenors.add(tenor)
        schemes.setdefault(
            scheme_id,
            {
                "scheme_id": scheme_id,
                "name": row["scheme_name"],
                "status": row["scheme_status"],
                "frequency": row["frequency"],
                "horizon": row["registry_horizon"],
                "cells": {},
            },
        )
        if row["horizon"] == 1:
            actual_direction = row["direction_1d"]
        elif row["horizon"] == 6:
            actual_direction = row["direction_weekly"]
        else:
            actual_direction = row["direction_5d"]
        if actual_direction is None:
            continue
        grouped[(scheme_id, tenor)].append(
            {
                "predicted_direction": row["predicted_direction"],
                "actual_direction": actual_direction,
            }
        )

    sorted_tenors = sorted(tenors, key=_tenor_sort_key)
    empty_block = _metric_block([])
    for scheme in schemes.values():
        for tenor in sorted_tenors:
            rows = grouped.get((scheme["scheme_id"], tenor), [])
            block = _metric_block(rows) if rows else empty_block
            scheme["cells"][tenor] = _compare_cell(block, metric_key)

    return {
        "frequency": frequency,
        "start_month": start_month,
        "end_month": end_month,
        "metric": metric,
        "metric_key": metric_key,
        "tenors": sorted_tenors,
        "target_labels": {
            tenor: _target_label(tenor, target_labels)
            for tenor in sorted_tenors
        },
        "schemes": list(schemes.values()),
    }


def _scheme_metric_month(horizon: Any, predict_date: str, extra: dict[str, Any]) -> str:
    frequency = str(extra.get("frequency") or "").lower()
    try:
        is_weekly = int(horizon) == 6
    except (TypeError, ValueError):
        is_weekly = False
    if is_weekly or frequency == "weekly":
        return str(extra.get("feature_date") or predict_date)[:7]
    return str(predict_date)[:7]


def backtest_factor_lab_results(
    engine: Engine,
    benchmark_id: str = "model_muti_0529",
    data_source: str = "framework_db_aligned",
) -> dict[str, Any]:
    """返回前端方案矩阵可直接展示的最新历史回测结果。"""
    scheme_meta = _backtest_scheme_meta(engine)
    target_labels = _target_labels(engine)
    visible_targets = _visible_targets(engine)
    run_sql = text(
        """
        SELECT id, benchmark_id, scheme_id, data_source, start_date, end_date,
               status, summary, report_path, created_at, updated_at
        FROM t_backtest_runs
        WHERE benchmark_id = :benchmark_id
          AND data_source = :data_source
          AND status = 'success'
        ORDER BY scheme_id, updated_at DESC, id DESC
        """
    )
    with engine.connect() as conn:
        run_rows = conn.execute(
            run_sql,
            {"benchmark_id": benchmark_id, "data_source": data_source},
        ).mappings().all()

    latest_by_scheme: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        if row["scheme_id"] not in latest_by_scheme:
            latest_by_scheme[row["scheme_id"]] = _backtest_run_row(row)

    schemes: list[dict[str, Any]] = []
    for run in latest_by_scheme.values():
        metrics = _backtest_frontend_monthly_metrics(engine, run["id"])
        daily_rows = _backtest_frontend_daily_rows(engine, run["id"])
        tenors = sorted(set(metrics) | set(daily_rows), key=_tenor_sort_key)
        meta = scheme_meta.get(run["scheme_id"], {})
        for tenor in tenors:
            if tenor not in visible_targets:
                continue
            target_label = _target_label(tenor, target_labels)
            horizon = _infer_horizon(metrics.get(tenor), daily_rows.get(tenor), meta)
            frequency = meta.get("frequency") or ("weekly" if horizon == 6 else "daily")
            scheme_name = _backtest_scheme_name(meta, run["scheme_id"])
            benchmark_label = _backtest_benchmark_label(run["benchmark_id"])
            data_source_label = _backtest_data_source_label(run["data_source"])
            display_name = _backtest_display_name(
                meta=meta,
                scheme_id=run["scheme_id"],
                target_label=target_label,
                data_source=run["data_source"],
            )
            schemes.append(
                {
                    "id": f'{run["scheme_id"]}:{tenor}:{data_source}',
                    "run_id": run["id"],
                    "benchmark_id": run["benchmark_id"],
                    "benchmark_label": benchmark_label,
                    "scheme_id": run["scheme_id"],
                    "scheme_name": scheme_name,
                    "data_source": run["data_source"],
                    "data_source_label": data_source_label,
                    "tenor": tenor,
                    "target_label": target_label,
                    "horizon": horizon,
                    "frequency": frequency,
                    "display_name": display_name,
                    "name": display_name,
                    "status": "complete",
                    "start_date": run["start_date"],
                    "end_date": run["end_date"],
                    "latest_run": {
                        "date": run["end_date"],
                        "status": run["status"],
                        "updated_at": run["updated_at"],
                    },
                    "monthly_metrics": metrics.get(tenor, []),
                    "daily_rows": daily_rows.get(tenor, []),
                    "summary": _metric_block_from_monthly(metrics.get(tenor, [])),
                }
            )

    return {
        "benchmark_id": benchmark_id,
        "benchmark_label": _backtest_benchmark_label(benchmark_id),
        "data_source": data_source,
        "data_source_label": _backtest_data_source_label(data_source),
        "target_labels": target_labels,
        "schemes": schemes,
    }


def _backtest_frontend_monthly_metrics(engine: Engine, run_id: int) -> dict[str, list[dict[str, Any]]]:
    sql = text(
        """
        SELECT target_tenor, horizon, month, sample_count, correct_count, accuracy,
               up_precision, up_recall, down_precision, down_recall,
               actual_dist, predicted_dist
        FROM t_backtest_monthly_metrics
        WHERE run_id = :run_id
        ORDER BY target_tenor, month
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"run_id": run_id}).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["target_tenor"]].append(
            {
                "target_tenor": row["target_tenor"],
                "horizon": row["horizon"],
                "month": row["month"],
                "total": row["sample_count"],
                "samples": row["sample_count"],
                "correct": row["correct_count"],
                "accuracy": _ratio_to_percent(row["accuracy"]),
                "overall": _ratio_to_percent(row["accuracy"]),
                "up_precision": _ratio_to_percent(row["up_precision"]),
                "up_recall": _ratio_to_percent(row["up_recall"]),
                "down_precision": _ratio_to_percent(row["down_precision"]),
                "down_recall": _ratio_to_percent(row["down_recall"]),
                "actual_dist": _json_value(row["actual_dist"], {}),
                "predicted_dist": _json_value(row["predicted_dist"], {}),
            }
        )
    return grouped


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


def _metric_block_from_monthly(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(int(row.get("samples") or 0) for row in rows)
    correct = sum(int(row.get("correct") or 0) for row in rows)
    pred_up = 0
    pred_down = 0
    actual_up = 0
    actual_down = 0
    up_tp = 0
    down_tp = 0
    for row in rows:
        actual_dist = _json_value(row.get("actual_dist"), {})
        predicted_dist = _json_value(row.get("predicted_dist"), {})
        row_actual_up = int(actual_dist.get("up") or 0)
        row_actual_down = int(actual_dist.get("down") or 0)
        row_pred_up = int(predicted_dist.get("up") or 0)
        row_pred_down = int(predicted_dist.get("down") or 0)
        actual_up += row_actual_up
        actual_down += row_actual_down
        pred_up += row_pred_up
        pred_down += row_pred_down
        up_tp += _true_positive_from_metrics(
            row.get("up_recall"),
            row_actual_up,
            row.get("up_precision"),
            row_pred_up,
        )
        down_tp += _true_positive_from_metrics(
            row.get("down_recall"),
            row_actual_down,
            row.get("down_precision"),
            row_pred_down,
        )
    return {
        "total": total,
        "samples": total,
        "correct": correct,
        "accuracy": _percent(correct, total),
        "overall": _percent(correct, total),
        "up_precision": _percent(up_tp, pred_up),
        "up_recall": _percent(up_tp, actual_up),
        "down_precision": _percent(down_tp, pred_down),
        "down_recall": _percent(down_tp, actual_down),
    }


def _true_positive_from_metrics(
    primary_metric: Any,
    primary_denominator: int,
    fallback_metric: Any,
    fallback_denominator: int,
) -> int:
    """从月度 precision/recall 与对应分母还原 TP 计数。"""
    if primary_metric is not None and primary_denominator > 0:
        return max(0, min(primary_denominator, round(float(primary_metric) / 100 * primary_denominator)))
    if fallback_metric is not None and fallback_denominator > 0:
        return max(0, min(fallback_denominator, round(float(fallback_metric) / 100 * fallback_denominator)))
    return 0


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


def backtest_metrics(engine: Engine, run_id: int) -> dict[str, Any] | None:
    """返回某个历史复现 run 的月度指标。"""
    run = get_backtest_run(engine, run_id)
    if run is None:
        return None
    sql = text(
        """
        SELECT target_tenor, horizon, month, sample_count, correct_count, accuracy,
               up_precision, up_recall, down_precision, down_recall,
               actual_dist, predicted_dist
        FROM t_backtest_monthly_metrics
        WHERE run_id = :run_id
        ORDER BY target_tenor, month
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"run_id": run_id}).mappings().all()
    return {
        "run": run,
        "items": [
            {
                "target_tenor": row["target_tenor"],
                "horizon": row["horizon"],
                "month": row["month"],
                "sample_count": row["sample_count"],
                "correct_count": row["correct_count"],
                "accuracy": row["accuracy"],
                "accuracy_pct": None if row["accuracy"] is None else round(float(row["accuracy"]) * 100, 1),
                "up_precision": row["up_precision"],
                "up_recall": row["up_recall"],
                "down_precision": row["down_precision"],
                "down_recall": row["down_recall"],
                "actual_dist": _json_value(row["actual_dist"], {}),
                "predicted_dist": _json_value(row["predicted_dist"], {}),
            }
            for row in rows
        ],
    }


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
