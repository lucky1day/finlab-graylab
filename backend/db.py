from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL

from scheduler.repository import create_engine_from_env
from shared.db_config import DatabaseConfig

# Dashboard 的 public upstream 预算是 3 秒；驱动 I/O 和单条 SELECT 都须
# 在预算内 fail-closed，避免同步 snapshot owner 永久占住 single-flight。
_DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS = 0.5
_DASHBOARD_DB_READ_TIMEOUT_SECONDS = 0.75
_DASHBOARD_DB_WRITE_TIMEOUT_SECONDS = 0.5
_DASHBOARD_DB_MAX_EXECUTION_TIME_MS = 500
_DASHBOARD_DB_POOL_RECYCLE_SECONDS = 300


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """返回后端共享的 SQLAlchemy Engine。"""
    return create_engine_from_env()


@lru_cache(maxsize=1)
def get_dashboard_engine() -> Engine:
    """返回仅供 dashboard 一致性只读快照使用的有界超时 Engine。"""
    cfg = DatabaseConfig.from_env()
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        query={"charset": cfg.charset},
    )
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_recycle=_DASHBOARD_DB_POOL_RECYCLE_SECONDS,
        connect_args={
            "connect_timeout": _DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": _DASHBOARD_DB_READ_TIMEOUT_SECONDS,
            "write_timeout": _DASHBOARD_DB_WRITE_TIMEOUT_SECONDS,
            "init_command": (
                "SET SESSION MAX_EXECUTION_TIME="
                f"{_DASHBOARD_DB_MAX_EXECUTION_TIME_MS}"
            ),
        },
    )
