from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, URL

from shared.db_config import DatabaseConfig


def create_engine_from_env(
    config: DatabaseConfig | None = None,
) -> Engine:
    """从显式配置或当前环境创建调度 SQLAlchemy Engine。"""
    cfg = config if config is not None else DatabaseConfig.from_env()
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        query={"charset": cfg.charset},
    )
    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args={
            "init_command": "SET SESSION time_zone = '+00:00'",
        },
    )
    event.listen(
        engine,
        "checkout",
        _set_mysql_session_utc_on_checkout,
    )
    return engine


def _set_mysql_session_utc_on_checkout(
    dbapi_connection: object,
    _connection_record: object,
    _connection_proxy: object,
) -> None:
    """每次连接池 checkout 都修复 MySQL session 时区为 UTC。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()


def dialect_name(conn: Connection) -> str:
    """返回连接方言名，供内部持久化实现共享。"""
    return str(getattr(getattr(conn, "dialect", None), "name", "mysql"))
