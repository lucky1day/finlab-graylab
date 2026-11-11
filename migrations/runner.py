"""唯一迁移执行入口：基线建库、增量迁移、owner lock、会话围栏与可重跑恢复。

历史 001..025 已压缩为 ``migrations/baseline/schema_025.sql``。已有数据库的历史表
逐行匹配 release manifest 中记录的旧文件身份即视为处于基线；空库先执行基线再登记
这些身份。之后只按顺序执行增量迁移。
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sqlalchemy import text

from migrations._contracts import (
    MIGRATION_HISTORY_LOCK,
    MigrationHistoryError,
    MigrationPartialApplyError,
    MigrationPreflightError,
    PreparedMigration,
)
from migrations._manifest import (
    MIGRATIONS_DIR,
    RELEASE_MIGRATION_MANIFEST_PATH,
    MigrationPlan,
    prepare_migration_files,
    select_pending_migrations,
    validate_release_migration_manifest,
)

__all__ = [
    "MIGRATIONS_DIR",
    "RELEASE_MIGRATION_MANIFEST_PATH",
    "apply_pending_migration_files",
    "inspect_applying_migration",
    "prepare_migration_files",
    "preflight_migration_session",
    "recover_applying_migration",
    "validate_mysql_session_contract",
    "validate_release_migration_manifest",
]


def validate_mysql_session_contract(facts: Mapping[str, object]) -> None:
    """校验 migration 所需的 MySQL session 安全前提。

    基线与增量迁移的标识符均为小写，可在 ``lower_case_table_names`` 的
    0/1/2 三种模式下安全执行。
    """
    server_version = str(facts.get("server_version") or "").strip()
    version_comment = str(facts.get("version_comment") or "").strip()
    if "mariadb" in f"{server_version} {version_comment}".lower():
        raise MigrationPreflightError(
            "migration requires Oracle MySQL, not MariaDB"
        )
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", server_version)
    if match is None:
        raise MigrationPreflightError(
            f"cannot parse MySQL server version: {server_version!r}"
        )
    version = tuple(int(part) for part in match.groups())
    if version < (8, 0, 16):
        raise MigrationPreflightError(
            "migration requires MySQL >= 8.0.16 for enforced CHECK "
            f"constraints; found {server_version}"
        )

    sql_modes = {
        mode.strip().upper()
        for mode in str(facts.get("sql_mode") or "").split(",")
        if mode.strip()
    }
    has_strict_mode = bool(
        {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"} & sql_modes
    )
    required_modes = {"NO_ZERO_DATE", "NO_ZERO_IN_DATE"}
    if not has_strict_mode or not required_modes.issubset(sql_modes):
        raise MigrationPreflightError(
            "unsafe MySQL sql_mode for fail-closed migration: "
            f"{sorted(sql_modes)}"
        )

    session_timezone = str(
        facts.get("session_time_zone") or ""
    ).strip().upper()
    if session_timezone not in {"+00:00", "UTC"}:
        raise MigrationPreflightError(
            "migration MySQL session must use UTC (+00:00); "
            f"found {session_timezone!r}"
        )
    if int(facts.get("foreign_key_checks") or 0) != 1:
        raise MigrationPreflightError(
            "migration requires foreign_key_checks=1"
        )
    try:
        lower_case_table_names = int(
            facts.get("lower_case_table_names")
        )
    except (TypeError, ValueError):
        lower_case_table_names = -1
    if lower_case_table_names not in {0, 1, 2}:
        raise MigrationPreflightError(
            "migration found unsupported lower_case_table_names="
            f"{lower_case_table_names}"
        )


def preflight_migration_session(connection: object) -> None:
    """在任何 migration DDL 前校验连接级安全契约。"""
    if getattr(connection.dialect, "name", None) != "mysql":
        return
    facts = connection.execute(
        text(
            """
            SELECT VERSION() AS server_version,
                   @@version_comment AS version_comment,
                   @@SESSION.sql_mode AS sql_mode,
                   @@SESSION.time_zone AS session_time_zone,
                   @@SESSION.foreign_key_checks AS foreign_key_checks,
                   @@lower_case_table_names AS lower_case_table_names
            """
        )
    ).mappings().one()
    validate_mysql_session_contract(facts)


def _is_ddl_statement(statement: str) -> bool:
    """保守识别会触发 MySQL 隐式提交的常见 DDL。"""
    first_word = re.match(r"[A-Za-z]+", statement.lstrip())
    return bool(
        first_word
        and first_word.group(0).upper()
        in {
            "ALTER",
            "CREATE",
            "DROP",
            "RENAME",
            "TRUNCATE",
        }
    )


def _partial_apply_error(
    path: Path,
    *,
    execution_error: BaseException | None,
    postcondition_error: BaseException | None,
) -> MigrationPartialApplyError:
    """构造不暗示 rollback 成功的 MySQL DDL 错误。"""
    details: list[str] = []
    if execution_error is not None:
        details.append(f"execution_error={execution_error!r}")
    if postcondition_error is not None:
        details.append(
            f"postcondition_error={postcondition_error!r}"
        )
    suffix = "; ".join(details) if details else "unknown schema state"
    return MigrationPartialApplyError(
        "MySQL DDL uses implicit commit; migration "
        f"{path.name} may be partially applied and cannot be assumed "
        f"rolled back. Inspect the schema before rerunning. {suffix}"
    )


def _expected_migration_history_schema() -> dict[str, object]:
    """返回 runner 自身 history 表的闭世界定义。"""
    return {
        "table": {
            "table_type": "base table",
            "engine": "innodb",
            "collation": "utf8mb4_0900_ai_ci",
        },
        "columns": {
            "version": ("int", "no", None, ""),
            "filename": ("varchar(255)", "no", None, ""),
            "sha256": ("char(64)", "no", None, ""),
            "state": (
                "enum('applying','applied')",
                "no",
                None,
                "",
            ),
            "baseline_bootstrap": ("tinyint(1)", "no", "0", ""),
            "started_at": (
                "datetime(6)",
                "no",
                "utc_timestamp(6)",
                "default_generated",
            ),
            "applied_at": ("datetime(6)", "yes", None, ""),
        },
        "indexes": {
            "PRIMARY": {
                "unique": True,
                "columns": ("version",),
                "sub_parts": (None,),
            },
            "uk_schema_migration_filename": {
                "unique": True,
                "columns": ("filename",),
                "sub_parts": (None,),
            },
        },
    }


def _migration_history_table_exists(connection: object) -> bool:
    count = int(
        connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 't_schema_migrations'
                  AND table_type = 'BASE TABLE'
                """
            )
        ).scalar_one()
    )
    return count == 1


def _read_migration_history_schema(
    connection: object,
) -> dict[str, object]:
    table_row = connection.execute(
        text(
            """
            SELECT table_type AS table_type,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            """
        )
    ).mappings().one()
    column_rows = connection.execute(
        text(
            """
            SELECT column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    index_rows = connection.execute(
        text(
            """
            SELECT index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 't_schema_migrations'
            ORDER BY index_name, seq_in_index
            """
        )
    ).mappings().all()
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in index_rows:
        grouped.setdefault(str(row["index_name"]), []).append(row)
    return {
        "table": {
            "table_type": str(table_row["table_type"]).lower(),
            "engine": str(table_row.get("engine") or "").lower(),
            "collation": str(
                table_row.get("table_collation") or ""
            ).lower(),
        },
        "columns": {
            str(row["column_name"]).lower(): (
                str(row["column_type"]).lower(),
                str(row["is_nullable"]).lower(),
                (
                    None
                    if row["column_default"] is None
                    else str(row["column_default"]).lower()
                ),
                " ".join(
                    str(row.get("extra") or "").lower().split()
                ),
            )
            for row in column_rows
        },
        "indexes": {
            name: {
                "unique": int(rows[0]["non_unique"]) == 0,
                "columns": tuple(
                    str(row["column_name"]).lower()
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
                "sub_parts": tuple(
                    (
                        None
                        if row.get("sub_part") is None
                        else int(row["sub_part"])
                    )
                    for row in sorted(
                        rows,
                        key=lambda item: int(item["seq_in_index"]),
                    )
                ),
            }
            for name, rows in grouped.items()
        },
    }


def _validate_migration_history_schema(
    schema: Mapping[str, object],
) -> None:
    expected = _expected_migration_history_schema()
    if schema != expected:
        raise MigrationHistoryError(
            "migration history schema definition drift: "
            f"expected={expected} observed={dict(schema)}"
        )


def _create_migration_history_table(connection: object) -> None:
    preflight_migration_session(connection)
    connection.execute(
        text(
            """
            CREATE TABLE t_schema_migrations (
                version INT NOT NULL,
                filename VARCHAR(255) NOT NULL,
                sha256 CHAR(64) NOT NULL,
                state ENUM('APPLYING','APPLIED') NOT NULL,
                baseline_bootstrap TINYINT(1) NOT NULL DEFAULT 0,
                started_at DATETIME(6) NOT NULL
                    DEFAULT (UTC_TIMESTAMP(6)),
                applied_at DATETIME(6) DEFAULT NULL,
                PRIMARY KEY (version),
                UNIQUE KEY uk_schema_migration_filename (filename)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            """
        )
    )


def _read_migration_history(
    connection: object,
) -> list[Mapping[str, object]]:
    return list(
        connection.execute(
            text(
                """
                SELECT version AS version,
                       filename AS filename,
                       sha256 AS sha256,
                       state AS state,
                       baseline_bootstrap AS baseline_bootstrap
                FROM t_schema_migrations
                ORDER BY version
                """
            )
        ).mappings().all()
    )


def _assert_migration_owner_lock_held(connection: object) -> None:
    """确认 migration named lock 仍由当前 owner connection 持有。"""
    owned = int(
        connection.execute(
            text(
                "SELECT IS_USED_LOCK(:lock_name) = CONNECTION_ID()"
            ),
            {"lock_name": MIGRATION_HISTORY_LOCK},
        ).scalar_one()
        or 0
    )
    if owned != 1:
        raise MigrationHistoryError(
            "migration owner lock ownership check failed"
        )


@contextmanager
def _migration_owner_connection(
    engine: object,
    *,
    require_mysql: bool = True,
):
    """持有进程无关的 MySQL migration owner lock。"""
    with engine.connect() as connection:
        is_mysql = getattr(connection.dialect, "name", None) == "mysql"
        if not is_mysql and require_mysql:
            raise MigrationHistoryError(
                "APPLYING migration inspection/recovery requires MySQL"
            )
        if not is_mysql:
            yield connection
            return
        preflight_migration_session(connection)
        acquired = int(
            connection.execute(
                text("SELECT GET_LOCK(:lock_name, 5)"),
                {"lock_name": MIGRATION_HISTORY_LOCK},
            ).scalar_one()
            or 0
        )
        if acquired != 1:
            raise MigrationHistoryError(
                "could not acquire migration owner lock"
            )
        body_error: BaseException | None = None
        try:
            _assert_migration_owner_lock_held(connection)
            yield connection
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                released = int(
                    connection.execute(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": MIGRATION_HISTORY_LOCK},
                    ).scalar_one()
                    or 0
                )
                if released != 1:
                    raise MigrationHistoryError(
                        "migration owner lock release failed"
                    )
            except BaseException as release_error:
                if body_error is None:
                    raise
                body_error.add_note(
                    "migration owner lock release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )


def _read_database_identity(connection: object) -> dict[str, str]:
    """读取当前迁移连接的数据库名与 MySQL 实例身份。"""
    identity_row = connection.execute(
        text(
            "SELECT DATABASE() AS database_name, "
            "@@server_uuid AS server_uuid"
        )
    ).mappings().one()
    return {
        "database_name": str(identity_row["database_name"] or ""),
        "server_uuid": str(identity_row["server_uuid"] or ""),
    }


def _mark_migration_applying(
    owner_connection: object,
    migration: PreparedMigration,
) -> None:
    rollback = getattr(owner_connection, "rollback", None)
    commit = getattr(owner_connection, "commit", None)
    if callable(rollback):
        rollback()
    try:
        _assert_migration_owner_lock_held(owner_connection)
        preflight_migration_session(owner_connection)
        owner_connection.execute(
            text(
                """
                INSERT INTO t_schema_migrations (
                    version,
                    filename,
                    sha256,
                    state,
                    baseline_bootstrap,
                    started_at,
                    applied_at
                ) VALUES (
                    :version,
                    :filename,
                    :sha256,
                    'APPLYING',
                    0,
                    UTC_TIMESTAMP(6),
                    NULL
                )
                """
            ),
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            },
        )
        if callable(commit):
            commit()
    except BaseException:
        if callable(rollback):
            rollback()
        raise


def _mark_migration_applied(
    owner_connection: object,
    migration: PreparedMigration,
) -> None:
    rollback = getattr(owner_connection, "rollback", None)
    commit = getattr(owner_connection, "commit", None)
    if callable(rollback):
        rollback()
    try:
        _assert_migration_owner_lock_held(owner_connection)
        preflight_migration_session(owner_connection)
        result = owner_connection.execute(
            text(
                """
                UPDATE t_schema_migrations
                SET state = 'APPLIED',
                    applied_at = UTC_TIMESTAMP(6)
                WHERE version = :version
                  AND filename = :filename
                  AND sha256 = :sha256
                  AND state = 'APPLYING'
                """
            ),
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            },
        )
        if int(result.rowcount) != 1:
            raise MigrationHistoryError(
                "migration APPLYING completion fence rejected: "
                f"{migration.path.name}"
            )
        if callable(commit):
            commit()
    except BaseException:
        if callable(rollback):
            rollback()
        raise


@contextmanager
def _migration_execution_connection(
    engine: object,
    owner_connection: object | None,
):
    """为 migration 执行提供持锁 owner 或原有独立事务连接。"""
    if owner_connection is None:
        with engine.begin() as connection:
            yield connection
        return
    rollback = getattr(owner_connection, "rollback", None)
    commit = getattr(owner_connection, "commit", None)
    if callable(rollback):
        rollback()
    _assert_migration_owner_lock_held(owner_connection)
    try:
        yield owner_connection
        if callable(commit):
            commit()
    except BaseException:
        if callable(rollback):
            rollback()
        raise


def _execute_prepared_migration_files(
    engine: object,
    prepared: Iterable[tuple[Path, Iterable[str]]],
    *,
    owner_connection: object | None = None,
) -> None:
    """逐条执行已校验的 SQL；DDL 失败按可能部分应用报告。"""
    for path, raw_statements in prepared:
        statements = list(raw_statements)
        ddl_attempted = False
        try:
            with _migration_execution_connection(
                engine,
                owner_connection,
            ) as connection:
                mysql_session = (
                    getattr(connection.dialect, "name", None) == "mysql"
                )
                preflight_migration_session(connection)
                for statement in statements:
                    if owner_connection is not None:
                        _assert_migration_owner_lock_held(connection)
                    if mysql_session and (
                        _is_ddl_statement(statement)
                        or statement.lstrip().upper().startswith("EXECUTE")
                    ):
                        ddl_attempted = True
                    connection.execute(text(statement))
        except BaseException as exc:
            if ddl_attempted:
                raise _partial_apply_error(
                    path,
                    execution_error=exc,
                    postcondition_error=None,
                ) from exc
            raise
        print(f"applied {path.name}")


def _existing_baseline_tables(
    connection: object,
    plan: MigrationPlan,
) -> list[str]:
    names = sorted(plan.baseline.table_names)
    rows = connection.execute(
        text(
            """
            SELECT LOWER(table_name)
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            """
        )
    ).scalars().all()
    present = {str(row) for row in rows}
    return [name for name in names if name in present]


def _install_baseline(owner_connection: object, plan: MigrationPlan) -> None:
    """在不含任何平台表的库上执行基线，并登记等价的 001..N 历史。"""
    existing = _existing_baseline_tables(owner_connection, plan)
    if existing:
        raise MigrationHistoryError(
            "database has platform tables but no migration history; "
            f"refusing to install the baseline over {existing[:5]}"
        )
    _execute_prepared_migration_files(
        None,
        [(plan.baseline.path, plan.baseline.statements)],
        owner_connection=owner_connection,
    )
    rollback = getattr(owner_connection, "rollback", None)
    commit = getattr(owner_connection, "commit", None)
    if callable(rollback):
        rollback()
    try:
        _assert_migration_owner_lock_held(owner_connection)
        for entry in plan.baseline.history:
            owner_connection.execute(
                text(
                    """
                    INSERT INTO t_schema_migrations (
                        version, filename, sha256, state,
                        baseline_bootstrap, started_at, applied_at
                    ) VALUES (
                        :version, :filename, :sha256, 'APPLIED',
                        1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                    )
                    """
                ),
                {
                    "version": entry.version,
                    "filename": entry.filename,
                    "sha256": entry.sha256,
                },
            )
        if callable(commit):
            commit()
    except BaseException:
        if callable(rollback):
            rollback()
        raise


def _load_history_or_install_baseline(
    owner_connection: object,
    plan: MigrationPlan,
) -> list[Mapping[str, object]]:
    if not _migration_history_table_exists(owner_connection):
        _create_migration_history_table(owner_connection)
    _validate_migration_history_schema(
        _read_migration_history_schema(owner_connection)
    )
    history = _read_migration_history(owner_connection)
    if history:
        return history
    _install_baseline(owner_connection, plan)
    return _read_migration_history(owner_connection)


def _run_pending(
    engine: object,
    owner_connection: object,
    pending: Iterable[PreparedMigration],
) -> None:
    for migration in pending:
        _assert_migration_owner_lock_held(owner_connection)
        _mark_migration_applying(owner_connection, migration)
        _execute_prepared_migration_files(
            engine,
            [(migration.path, migration.statements)],
            owner_connection=owner_connection,
        )
        _assert_migration_owner_lock_held(owner_connection)
        _mark_migration_applied(owner_connection, migration)


def apply_pending_migration_files(
    engine: object,
    paths: Iterable[Path],
) -> None:
    """持有 MySQL owner lock，空库先建基线，再只执行 pending 增量迁移。"""
    plan = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        history = _load_history_or_install_baseline(owner_connection, plan)
        pending = select_pending_migrations(plan, history)
        _run_pending(engine, owner_connection, pending)


def _applying_state(
    owner_connection: object,
    plan: MigrationPlan,
) -> dict[str, object]:
    """读取唯一 APPLYING 行并生成稳定状态摘要；不执行写操作。"""
    if not _migration_history_table_exists(owner_connection):
        raise MigrationHistoryError("migration history table does not exist")
    _validate_migration_history_schema(
        _read_migration_history_schema(owner_connection)
    )
    history = _read_migration_history(owner_connection)
    applying = [
        row for row in history if str(row["state"]).upper() == "APPLYING"
    ]
    migration = None
    if len(applying) == 1:
        version = int(applying[0]["version"])
        migration = next(
            (item for item in plan.migrations if item.version == version),
            None,
        )
    state: dict[str, object] = {
        "database_identity": _read_database_identity(owner_connection),
        "applying": [
            {
                "version": int(row["version"]),
                "filename": str(row["filename"]),
                "sha256": str(row["sha256"]),
            }
            for row in applying
        ],
        "rerunnable": bool(
            migration is not None
            and migration.rerunnable
            and str(applying[0]["sha256"]) == migration.sha256
            and str(applying[0]["filename"]) == migration.path.name
        ),
    }
    state["state_digest"] = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return state


def inspect_applying_migration(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """只读报告中断的增量迁移及其是否可安全重跑。"""
    plan = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        return _applying_state(owner_connection, plan)


def recover_applying_migration(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """重跑声明为可重跑的唯一 APPLYING 迁移；状态须与先前只读检查一致。"""
    plan = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        state = _applying_state(owner_connection, plan)
        if state["state_digest"] != expected_state_digest:
            raise MigrationHistoryError(
                "migration state changed since inspection; inspect again"
            )
        if not state["rerunnable"]:
            raise MigrationHistoryError(
                "APPLYING migration is not declared rerunnable; restore from "
                "backup instead of replaying it"
            )
        version = int(state["applying"][0]["version"])
        migration = next(
            item for item in plan.migrations if item.version == version
        )
        _execute_prepared_migration_files(
            engine,
            [(migration.path, migration.statements)],
            owner_connection=owner_connection,
        )
        _assert_migration_owner_lock_held(owner_connection)
        _mark_migration_applied(owner_connection, migration)
        return {"recovered": migration.path.name, **state}
