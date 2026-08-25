from __future__ import annotations

import re
from typing import Iterable


DRY_RUN_GUARD_TABLES = (
    "api_wind_date",
    "api_wind_daily",
    "api_wind_derivative_daily",
    "api_wind_weekly",
    "api_wind_derivative_weekly",
    "api_wind_monthly",
    "api_wind_derivative_monthly",
    "api_wind_indicators_all",
    "t_trade_calendar",
    "t_pre_market_forecast",
    "t_shap",
    "t_scheme_runs",
    "t_scheme_predictions",
    "t_scheme_run_log",
    "t_scheme_actuals",
    "t_scheme_weekly_actuals",
    "t_scheme_monthly_actuals",
    "t_scheme_period_average_actuals",
    "t_backtest_runs",
    "t_backtest_predictions",
    "t_backtest_monthly_metrics",
    "t_backtest_reproduction_checks",
)
PROTECTED_TABLES = DRY_RUN_GUARD_TABLES
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def snapshot_table_counts(engine, table_names: Iterable[str] = DRY_RUN_GUARD_TABLES) -> dict[str, int]:
    """读取表行数快照。"""
    with engine.connect() as conn:
        return snapshot_table_counts_conn(conn, table_names)


def snapshot_table_counts_conn(conn, table_names: Iterable[str] = DRY_RUN_GUARD_TABLES) -> dict[str, int]:
    """在调用方事务中读取表行数快照。"""
    from sqlalchemy import text

    snapshot: dict[str, int] = {}
    for table_name in table_names:
        if not _IDENTIFIER.fullmatch(str(table_name)):
            raise ValueError(f"unsafe table name: {table_name}")
        exists = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                """
            ),
            {"table_name": str(table_name)},
        ).scalar_one()
        if int(exists) == 0:
            snapshot[str(table_name)] = 0
            continue
        value = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar_one()
        snapshot[str(table_name)] = int(value)
    return snapshot


def diff_snapshots(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """计算 after-before 行数差。"""
    keys = sorted(set(before) | set(after))
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys}
