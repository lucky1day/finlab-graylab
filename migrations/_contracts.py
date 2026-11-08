"""迁移公共值类型、错误与受审版本身份。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from migrations.recovery_versions import (
    SPEC_017,
    SPEC_018,
    SPEC_019,
    SPEC_021,
    SPEC_022,
    SPEC_023,
    SPEC_024,
)


DAILY_LEDGER_MIGRATION_VERSION = SPEC_017.version

DAILY_LEDGER_MIGRATION_FILENAME = SPEC_017.filename

DAILY_LEDGER_MIGRATION_SHA256 = SPEC_017.sha256

SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION = SPEC_018.version

SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME = SPEC_018.filename

SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256 = SPEC_018.sha256

SERVING_POINTER_RETIREMENT_MIGRATION_VERSION = SPEC_019.version

SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME = SPEC_019.filename

SERVING_POINTER_RETIREMENT_MIGRATION_SHA256 = SPEC_019.sha256

REGISTRY_OWNER_MIGRATION_VERSION = SPEC_021.version

REGISTRY_OWNER_MIGRATION_FILENAME = SPEC_021.filename

REGISTRY_OWNER_MIGRATION_SHA256 = SPEC_021.sha256

AUTH_MIGRATION_VERSION = SPEC_022.version

AUTH_MIGRATION_FILENAME = SPEC_022.filename

AUTH_MIGRATION_SHA256 = SPEC_022.sha256

AUTH_PROFILE_MIGRATION_VERSION = SPEC_023.version

AUTH_PROFILE_MIGRATION_FILENAME = SPEC_023.filename

AUTH_PROFILE_MIGRATION_SHA256 = SPEC_023.sha256

SCHEME_PREDICTION_FACT_MIGRATION_VERSION = SPEC_024.version

SCHEME_PREDICTION_FACT_MIGRATION_FILENAME = SPEC_024.filename

SCHEME_PREDICTION_FACT_MIGRATION_SHA256 = SPEC_024.sha256


class MigrationSQLParseError(ValueError):
    """迁移 SQL 无法被安全拆分时抛出的错误。"""


class MigrationPreflightError(RuntimeError):
    """迁移开始前发现无法安全执行的 MySQL 前提。"""


class MigrationPartialApplyError(RuntimeError):
    """MySQL DDL 隐式提交后，迁移可能只完成了一部分。"""


class MigrationHistoryError(MigrationPreflightError):
    """迁移历史不连续、漂移或处于未决状态。"""


@dataclass(frozen=True)
class PreparedMigration:
    """已解析且带内容校验和的 migration 文件。"""

    version: int
    path: Path
    sha256: str
    statements: tuple[str, ...]


DAILY_LEDGER_NEW_TABLES = frozenset(
    {
        "t_input_generations",
        "t_schedule_occurrences",
        "t_schedule_items",
        "t_schedule_item_targets",
        "t_scheduler_heartbeat",
    }
)

MIGRATION_HISTORY_LOCK = "bond_factor_lab_schema_migrations"

DAILY_LEDGER_RUNNER_GUARD = "daily-ledger-017-v1"

PERIOD_AVERAGE_ACTUALS_MIGRATION_FILENAME = (
    "020_period_average_actuals.sql"
)
