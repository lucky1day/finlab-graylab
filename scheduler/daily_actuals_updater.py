from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from scheduler.actuals_errors import require_actual_source_tenors
from scheduler.repository import (
    ActualWriteStats,
    _upsert_actuals_detailed_conn,
    create_engine_from_env,
)
from shared.actual_facts import (
    build_daily_actual_records_from_rows,
    read_actual_source_snapshot,
)
from shared.tenor_mapping import TENOR_TO_INDICATOR, normalize_tenor


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
) -> list[str]:
    """解析 updater 期限范围；显式 override 之外以 Registry 为权威。"""
    if tenors is not None:
        return _normalize_supported_tenors(tenors)
    try:
        task_types = ACTUAL_TASK_TYPES_BY_FREQUENCY[frequency]
    except KeyError as exc:
        raise ValueError(f"unsupported actual frequency: {frequency}") from exc

    registry_tenors = active_registry_tenors(engine, task_types)
    if not registry_tenors:
        logger.warning(
            "actual_tenor_scope_empty frequency=%s",
            frequency,
        )
    return registry_tenors


def update_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
    *,
    engine: Engine | None = None,
) -> int:
    """兼容入口：刷新日频 Actual，并返回本批处理条数。"""
    return update_actuals_detailed(
        start_date=start_date,
        end_date=end_date,
        tenors=tenors,
        engine=engine,
    ).attempted


def update_actuals_detailed(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
    *,
    engine: Engine | None = None,
) -> ActualWriteStats:
    """从同一源快照刷新日频 Actual，并返回真实写入统计。"""
    owns_engine = engine is None
    target_engine = engine if engine is not None else create_engine_from_env()
    try:
        selected_tenors = resolve_actual_tenors(
            target_engine,
            frequency="daily",
            tenors=tenors,
        )
        if not selected_tenors:
            return ActualWriteStats()
        with target_engine.begin() as connection:
            snapshot = read_actual_source_snapshot(
                connection,
                tenors=selected_tenors,
                end_date=end_date,
            )
            source_rows = require_actual_source_tenors(
                snapshot.rows,
                expected_tenors=selected_tenors,
                stage="daily",
            )
            records = build_daily_actual_records_from_rows(
                source_rows,
                start_date=start_date,
            )
            stats = _upsert_actuals_detailed_conn(connection, records)
        logger.info(
            "Daily actuals refreshed from source snapshot: "
            "source_digest=%s watermarks=%s attempted=%s inserted=%s "
            "changed=%s unchanged=%s",
            snapshot.source_digest,
            snapshot.watermarks,
            stats.attempted,
            stats.inserted,
            stats.changed,
            stats.unchanged,
        )
        return stats
    finally:
        if owns_engine:
            target_engine.dispose()
