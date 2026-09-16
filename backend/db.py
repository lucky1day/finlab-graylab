from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterator
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL

from shared.db_config import DatabaseConfig


HTTP_REQUEST_BUDGET_SECONDS = 4.0
HTTP_DB_POOL_TIMEOUT_SECONDS = 0.25
HTTP_DB_MAX_OVERFLOW = 0
AUTH_DB_POOL_SIZE = 5
DASHBOARD_DB_POOL_SIZE = 5
_HTTP_DB_CONNECT_TIMEOUT_SECONDS = 0.5
_HTTP_DB_READ_TIMEOUT_SECONDS = 2.0
_HTTP_DB_WRITE_TIMEOUT_SECONDS = 0.5
_HTTP_DB_MAX_EXECUTION_TIME_MS = 1000
_HTTP_DB_POOL_RECYCLE_SECONDS = 300
_REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,128}\Z", flags=re.ASCII)


class RequestBudgetExceeded(TimeoutError):
    """后端请求累计预算已耗尽。"""


@dataclass(slots=True)
class HttpRequestContext:
    """贯穿认证、查询、构建和编码的单次后端请求计时。"""

    request_id: str
    started_at: float
    deadline_at: float
    clock: Callable[[], float] = field(repr=False, compare=False)
    auth_seconds: float = 0.0
    pool_acquire_seconds: float = 0.0
    db_seconds: float = 0.0
    build_seconds: float = 0.0
    encode_seconds: float = 0.0
    failure_stage: str | None = None

    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def remaining_seconds(self) -> float:
        return self.deadline_at - self.clock()

    def require_remaining(self, stage: str) -> None:
        if self.remaining_seconds() <= 0:
            self.failure_stage = stage
            raise RequestBudgetExceeded(
                f"backend request budget exhausted before {stage}"
            )


_CURRENT_HTTP_REQUEST_CONTEXT: ContextVar[HttpRequestContext | None] = (
    ContextVar("bfl_http_request_context", default=None)
)


def create_http_request_context(
    candidate_request_id: str | None,
    *,
    clock: Callable[[], float] = time.perf_counter,
    budget_seconds: float = HTTP_REQUEST_BUDGET_SECONDS,
) -> HttpRequestContext:
    """创建一次请求唯一的 ID、起点与后端截止时刻。"""
    request_id = (
        candidate_request_id
        if candidate_request_id is not None
        and _REQUEST_ID_PATTERN.fullmatch(candidate_request_id)
        else uuid4().hex
    )
    started_at = clock()
    return HttpRequestContext(
        request_id=request_id,
        started_at=started_at,
        deadline_at=started_at + budget_seconds,
        clock=clock,
    )


@contextmanager
def bind_http_request_context(
    context: HttpRequestContext,
) -> Iterator[HttpRequestContext]:
    token: Token[HttpRequestContext | None] = (
        _CURRENT_HTTP_REQUEST_CONTEXT.set(context)
    )
    try:
        yield context
    finally:
        _CURRENT_HTTP_REQUEST_CONTEXT.reset(token)


def current_http_request_context() -> HttpRequestContext | None:
    return _CURRENT_HTTP_REQUEST_CONTEXT.get()


def _set_mysql_session_utc_on_checkout(
    dbapi_connection: object,
    _connection_record: object,
    _connection_proxy: object,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()


def _create_http_engine(
    config: DatabaseConfig,
    *,
    pool_size: int,
) -> Engine:
    """创建有界连接池、驱动超时和单条 SQL 上限的 HTTP Engine。"""
    url = URL.create(
        drivername="mysql+pymysql",
        username=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
        database=config.database,
        query={"charset": config.charset},
    )
    engine = create_engine(
        url,
        future=True,
        pool_size=pool_size,
        max_overflow=HTTP_DB_MAX_OVERFLOW,
        pool_timeout=HTTP_DB_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        pool_recycle=_HTTP_DB_POOL_RECYCLE_SECONDS,
        connect_args={
            "connect_timeout": _HTTP_DB_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": _HTTP_DB_READ_TIMEOUT_SECONDS,
            "write_timeout": _HTTP_DB_WRITE_TIMEOUT_SECONDS,
            "init_command": (
                "SET SESSION MAX_EXECUTION_TIME="
                f"{_HTTP_DB_MAX_EXECUTION_TIME_MS}"
            ),
        },
    )
    event.listen(engine, "checkout", _set_mysql_session_utc_on_checkout)
    return engine


@lru_cache(maxsize=8)
def get_engine(config: DatabaseConfig | None = None) -> Engine:
    """返回认证和 health 专用的有界 HTTP Engine。"""
    selected = config if config is not None else DatabaseConfig.from_env()
    return _create_http_engine(selected, pool_size=AUTH_DB_POOL_SIZE)


@lru_cache(maxsize=8)
def get_dashboard_engine(config: DatabaseConfig | None = None) -> Engine:
    """返回仅供 dashboard 一致性只读快照使用的有界超时 Engine。"""
    selected = config if config is not None else DatabaseConfig.from_env()
    return _create_http_engine(
        selected,
        pool_size=DASHBOARD_DB_POOL_SIZE,
    )
