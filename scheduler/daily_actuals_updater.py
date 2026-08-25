from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from scheduler.discovery import SCHEMES_ROOT, discover_schemes
from scheduler.repository import create_engine_from_env, delete_actuals_after_source_watermark, upsert_actuals
from shared.actual_facts import (
    build_daily_actual_records_from_rows,
    read_yield_rows,
)
from shared.tenor_mapping import TENOR_TO_INDICATOR, indicator_map_for_tenors, normalize_tenor


logger = logging.getLogger(__name__)

ACTUAL_TASK_TYPES_BY_FREQUENCY: dict[str, tuple[str, ...]] = {
    "daily": ("T+1", "T+5"),
    "weekly": ("weekly_point", "weekly_average"),
    "monthly": ("monthly",),
    "period_average": (
        "monthly_average",
        "quarterly_average",
        "annual_average",
    ),
}
_TENOR_ORDER = {tenor: index for index, tenor in enumerate(TENOR_TO_INDICATOR)}


def configured_active_scheme_tenors(
    schemes_root=SCHEMES_ROOT,
    frequency: str | None = None,
    task_types: Iterable[str] | None = None,
) -> list[str]:
    """读取本地 active 配置期限，仅用于 Registry 漂移诊断。"""
    selected: set[str] = set()
    selected_task_types = set(task_types or ())
    for cfg in discover_schemes(schemes_root):
        if cfg.status != "active":
            continue
        if frequency and cfg.frequency != frequency:
            continue
        if selected_task_types and cfg.task_type not in selected_task_types:
            continue
        selected.update(normalize_tenor(tenor) for tenor in cfg.tenors)
    return sorted(selected)


def _normalize_supported_tenors(tenors: Iterable[object]) -> list[str]:
    selected = {normalize_tenor(tenor) for tenor in tenors}
    unsupported = sorted(selected - TENOR_TO_INDICATOR.keys())
    if unsupported:
        raise ValueError(
            "unsupported actual tenor scope: "
            + ", ".join(unsupported)
        )
    return sorted(selected, key=_TENOR_ORDER.__getitem__)


def active_registry_tenors(
    engine: Engine,
    task_types: Iterable[str],
) -> list[str]:
    """从 active Registry 查询 actual 需要覆盖的期限。"""
    scope = active_registry_tenors_by_task_type(engine, task_types)
    return _normalize_supported_tenors(
        tenor for tenors in scope.values() for tenor in tenors
    )


def active_registry_tenors_by_task_type(
    engine: Engine,
    task_types: Iterable[str],
) -> dict[str, list[str]]:
    """从 active Registry 一次读取每个显式 task_type 的期限范围。"""
    selected_task_types = tuple(dict.fromkeys(str(value) for value in task_types))
    if not selected_task_types:
        raise ValueError("actual task_types cannot be empty")
    sql = text(
        """
        SELECT DISTINCT task_type, target_tenor
        FROM t_scheme_registry
        WHERE status = 'active'
          AND task_type IN :task_types
        """
    ).bindparams(bindparam("task_types", expanding=True))
    with engine.begin() as conn:
        rows = conn.execute(
            sql,
            {"task_types": selected_task_types},
        ).mappings().all()
    grouped: dict[str, list[str]] = {}
    for task_type in selected_task_types:
        selected = [
            row["target_tenor"]
            for row in rows
            if str(row["task_type"]) == task_type
        ]
        if selected:
            grouped[task_type] = _normalize_supported_tenors(selected)
    return grouped


def resolve_actual_tenors(
    engine: Engine,
    *,
    frequency: str,
    tenors: Iterable[str] | None = None,
    schemes_root=SCHEMES_ROOT,
) -> list[str]:
    """解析 updater 期限范围；显式 override 之外以 Registry 为权威。"""
    if tenors is not None:
        return _normalize_supported_tenors(tenors)
    try:
        task_types = ACTUAL_TASK_TYPES_BY_FREQUENCY[frequency]
    except KeyError as exc:
        raise ValueError(f"unsupported actual frequency: {frequency}") from exc

    registry_tenors = active_registry_tenors(engine, task_types)
    try:
        configured_tenors = sorted(
            {
                normalize_tenor(tenor)
                for tenor in configured_active_scheme_tenors(
                    schemes_root,
                    frequency=(None if frequency == "period_average" else frequency),
                    task_types=task_types,
                )
            },
            key=lambda tenor: (
                _TENOR_ORDER.get(tenor, len(_TENOR_ORDER)),
                tenor,
            ),
        )
    except Exception as exc:
        event = {
            "event": "ACTUAL_TENOR_SCOPE_DIAGNOSTIC_FAILED",
            "frequency": frequency,
            "error_type": type(exc).__name__,
        }
        logger.warning(
            "actual_tenor_scope_diagnostic_failed %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            extra={"actual_tenor_scope_event": event},
        )
        configured_tenors = None
    registry_set = set(registry_tenors)
    configured_set = set(configured_tenors or [])
    if configured_tenors is not None and registry_set != configured_set:
        event = {
            "event": "ACTUAL_TENOR_SCOPE_DRIFT",
            "frequency": frequency,
            "registry_tenors": registry_tenors,
            "configured_tenors": configured_tenors,
            "missing_from_config": sorted(
                registry_set - configured_set,
                key=_TENOR_ORDER.__getitem__,
            ),
            "missing_from_registry": sorted(
                configured_set - registry_set,
                key=lambda tenor: _TENOR_ORDER.get(tenor, len(_TENOR_ORDER)),
            ),
        }
        logger.warning(
            "actual_tenor_scope_drift %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            extra={"actual_tenor_scope_event": event},
        )
    if not registry_tenors:
        event = {
            "event": "ACTUAL_TENOR_SCOPE_EMPTY",
            "frequency": frequency,
            "registry_tenors": [],
        }
        logger.warning(
            "actual_tenor_scope_empty %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            extra={"actual_tenor_scope_event": event},
        )
    return registry_tenors


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def read_source_watermarks(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> dict[str, str]:
    """读取每个期限在源表中的最新可用日期。"""
    selected_tenors = [normalize_tenor(tenor) for tenor in (tenors or TENOR_TO_INDICATOR.keys())]
    code_to_tenor = indicator_map_for_tenors(selected_tenors)
    if not code_to_tenor:
        return {}

    params = {
        "codes": list(code_to_tenor.keys()),
        "end_date": _normalize_date(end_date),
    }
    end_filter = "AND rdate <= :end_date" if params["end_date"] else ""
    sql = text(
        f"""
        SELECT indicators_code, MAX(rdate) AS max_date
        FROM api_wind_daily
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
          {end_filter}
        GROUP BY indicators_code
        """
    ).bindparams(bindparam("codes", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return {
        code_to_tenor[str(row["indicators_code"])]: str(row["max_date"])
        for row in rows
        if row["max_date"] is not None
    }


def update_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """从行情表刷新 t_scheme_actuals。"""
    engine = create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            engine,
            frequency="daily",
            tenors=tenors,
        )
        if not selected_tenors:
            return 0
        rows = read_yield_rows(
            engine,
            tenors=selected_tenors,
            end_date=end_date,
        )
        records = build_daily_actual_records_from_rows(
            rows,
            start_date=start_date,
        )
        written = upsert_actuals(engine, records)
        source_watermarks = read_source_watermarks(engine, tenors=selected_tenors, end_date=end_date)
        pruned = delete_actuals_after_source_watermark(
            engine,
            source_watermarks,
            end_date=_normalize_date(end_date),
        )
        if pruned:
            logger.warning("Pruned stale daily actuals beyond source watermark: records=%s", pruned)
        return written
    finally:
        engine.dispose()
