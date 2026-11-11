"""迁移公共值类型与错误。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class MigrationSQLParseError(ValueError):
    """迁移 SQL 不能被安全拆分。"""


class MigrationPreflightError(RuntimeError):
    """执行任何 DDL 前的会话或库状态前提不满足。"""


class MigrationPartialApplyError(RuntimeError):
    """MySQL DDL 隐式提交后失败，库可能处于部分应用状态。"""


class MigrationHistoryError(MigrationPreflightError):
    """迁移历史与 release manifest 不一致。"""


@dataclass(frozen=True)
class PreparedMigration:
    """已解析且通过摘要校验的一个迁移文件。"""

    version: int
    path: Path
    sha256: str
    statements: tuple[str, ...]
    rerunnable: bool = False


MIGRATION_HISTORY_LOCK = "bond_factor_lab_schema_migrations"
RERUNNABLE_MARKER = "-- bfl-migration: rerunnable"
