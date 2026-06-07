from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy.engine import Engine

from scheduler.repository import create_engine_from_env


load_dotenv()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """返回后端共享的 SQLAlchemy Engine。"""
    return create_engine_from_env()
