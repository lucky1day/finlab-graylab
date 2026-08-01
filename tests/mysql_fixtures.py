from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from harness.daily_real_replay_mysql import (
    IsolatedReplayMySQL,
    isolated_replay_mysql,
)
from migrations.runner import apply_migration_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = tuple(sorted((PROJECT_ROOT / "migrations").glob("*.sql")))


class TemporaryMySQLFixture:
    """为 opt-in 集成测试适配统一的隔离 MySQL 生命周期。"""

    def __init__(self, server: IsolatedReplayMySQL) -> None:
        self._server = server

    def __getattr__(self, name: str):
        return getattr(self._server, name)

    @property
    def server_uuid(self) -> str:
        return self._server.identity.server_uuid

    def create_schema(self, _purpose: str):
        schema, engine = self._server.create_replay_database()
        apply_migration_files(engine, MIGRATIONS)
        return schema, engine

    def _socket_admin_engine(self):
        return self._server._admin_engine()


@contextmanager
def temporary_mysql() -> Iterator[TemporaryMySQLFixture]:
    """启动并自动回收一个完成迁移的隔离 MySQL。"""
    with isolated_replay_mysql() as server:
        yield TemporaryMySQLFixture(server)
