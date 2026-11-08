"""唯一迁移执行与恢复编排入口，维护 owner lock、会话及事务围栏。"""

from __future__ import annotations

from contextlib import contextmanager
import hmac
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping
from sqlalchemy import text
from migrations.recovery_orchestration import (
    RecoveryOrchestrationSpec,
    SPEC_017,
    SPEC_018,
    SPEC_019,
    SPEC_021,
    SPEC_022,
    SPEC_023,
    SPEC_024,
    SPEC_025,
)
from migrations.platform_confidence_025 import (
    classify_prediction_schema,
    read_prediction_schema,
)
from migrations.auth_022 import classify_auth_schema, read_auth_schema
from migrations.auth_profile_023 import (
    classify_auth_profile_schema,
    read_auth_profile_schema,
)
from migrations.scheme_prediction_fact_024 import (
    classify_scheme_prediction_fact_state,
    read_scheme_prediction_fact_state,
)
from migrations._contracts import (
    AUTH_MIGRATION_FILENAME,
    AUTH_MIGRATION_SHA256,
    AUTH_MIGRATION_VERSION,
    AUTH_PROFILE_MIGRATION_FILENAME,
    AUTH_PROFILE_MIGRATION_SHA256,
    AUTH_PROFILE_MIGRATION_VERSION,
    DAILY_LEDGER_MIGRATION_FILENAME,
    DAILY_LEDGER_MIGRATION_SHA256,
    DAILY_LEDGER_MIGRATION_VERSION,
    DAILY_LEDGER_NEW_TABLES,
    DAILY_LEDGER_RUNNER_GUARD,
    MIGRATION_HISTORY_LOCK,
    MigrationHistoryError,
    MigrationPartialApplyError,
    MigrationPreflightError,
    MigrationSQLParseError,
    PERIOD_AVERAGE_ACTUALS_MIGRATION_FILENAME,
    PreparedMigration,
    REGISTRY_OWNER_MIGRATION_FILENAME,
    REGISTRY_OWNER_MIGRATION_SHA256,
    REGISTRY_OWNER_MIGRATION_VERSION,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION,
    SCHEME_PREDICTION_FACT_MIGRATION_FILENAME,
    SCHEME_PREDICTION_FACT_MIGRATION_SHA256,
    SCHEME_PREDICTION_FACT_MIGRATION_VERSION,
    SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME,
    SERVING_POINTER_RETIREMENT_MIGRATION_SHA256,
    SERVING_POINTER_RETIREMENT_MIGRATION_VERSION,
)
from migrations._sql import (
    split_sql_statements,
)
from migrations._manifest import (
    MIGRATIONS_DIR,
    PROJECT_ROOT,
    RELEASE_MIGRATION_MANIFEST_PATH,
    RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION,
    prepare_migration_files,
    select_pending_migrations,
    validate_release_migration_manifest,
)
from migrations._schema import (
    _classify_registry_owner_recovery_state,
    _classify_schedule_run_started_at_shape,
    _classify_serving_pointer_retirement_state,
    _read_period_average_actuals_schema,
    _read_registry_owner_recovery_state,
    _read_registry_owner_schema,
    _read_schedule_run_started_at_shape,
    _read_serving_pointer_retirement_state,
    _registry_owner_authority,
    _validate_period_average_actuals_schema,
    _validate_registry_owner_schema,
    daily_ledger_nullable_transition_definitions,
    expected_daily_ledger_schema_fingerprint,
    expected_legacy_016_baseline,
    legacy_016_data_violation_queries,
    preflight_daily_ledger_upgrade,
    read_daily_ledger_schema_fingerprint,
    read_daily_ledger_schema_global_constraints,
    read_daily_ledger_upgrade_state,
    read_legacy_016_baseline,
    validate_daily_ledger_schema_fingerprint,
    validate_daily_ledger_upgrade_state,
    validate_legacy_016_baseline,
)
from migrations._recovery_state import (
    _applying_017_state_digest,
    _recovery_target,
    build_applying_017_inspection,
    build_applying_018_inspection,
    build_applying_019_inspection,
    build_applying_021_inspection,
    build_applying_022_inspection,
    build_applying_023_inspection,
    build_applying_024_inspection,
    build_applying_025_inspection,
)


def validate_mysql_session_contract(
    facts: Mapping[str, object],
    *,
    require_reviewed_constraint_namespace: bool = False,
) -> None:
    """校验 migration 所需的 MySQL session 安全前提。

    只有 migration 017 的约束命名空间实现依赖
    ``lower_case_table_names=2``；其余 migration 的 canonical 标识符均为
    小写，可在 MySQL 支持的 0/1/2 三种模式下安全核验与执行。
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
    if (
        require_reviewed_constraint_namespace
        and lower_case_table_names != 2
    ):
        raise MigrationPreflightError(
            "migration 017 requires lower_case_table_names=2 for the "
            "reviewed constraint namespace semantics"
        )


def preflight_migration_session(
    connection: object,
    *,
    require_reviewed_constraint_namespace: bool = False,
) -> None:
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
    validate_mysql_session_contract(
        facts,
        require_reviewed_constraint_namespace=(
            require_reviewed_constraint_namespace
        ),
    )


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


def _is_daily_ledger_migration(path: Path) -> bool:
    """migration 017 需要额外的数据与 schema 定义门禁。"""
    return path.name.startswith("017_")


def _is_schedule_run_started_at_migration(path: Path) -> bool:
    """migration 018 需要精确列定义 postcondition。"""
    return path.name == SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME


def _is_serving_pointer_retirement_migration(path: Path) -> bool:
    """migration 019 需要精确 serving-pointer source/target 门禁。"""
    return path.name == SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME


def _is_period_average_actuals_migration(path: Path) -> bool:
    """migration 020 只允许缺表或精确完整目标表。"""
    return path.name == PERIOD_AVERAGE_ACTUALS_MIGRATION_FILENAME


def _is_registry_owner_migration(path: Path) -> bool:
    """migration 021 只允许 owner 缺失或精确完整终态。"""
    return path.name == REGISTRY_OWNER_MIGRATION_FILENAME


def _is_auth_migration(path: Path) -> bool:
    """migration 022 只允许全无、精确部分或精确完整认证表。"""
    return path.name == AUTH_MIGRATION_FILENAME


def _is_auth_profile_migration(path: Path) -> bool:
    """migration 023 只允许精确资料列与强制改密状态。"""
    return path.name == AUTH_PROFILE_MIGRATION_FILENAME


def _is_scheme_prediction_fact_migration(path: Path) -> bool:
    """migration 024 只允许旧事实表或精确可恢复中间态。"""
    return path.name == SCHEME_PREDICTION_FACT_MIGRATION_FILENAME


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


def _unsafe_applying_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    database_identity: Mapping[str, object],
    reason: BaseException,
    version: int,
) -> dict[str, object]:
    """在 history/schema 无法完整读取时仍返回稳定 UNSAFE 结果。"""
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in manifest
            if migration.version <= version
        ],
        "inspection_error": {
            "type": type(reason).__name__,
            "message": str(reason),
        },
    }
    return {
        "classification": "UNSAFE",
        "state_digest": _applying_017_state_digest(canonical_state),
        "database_identity": dict(database_identity),
        "migration": None,
        "reason": str(reason),
    }


def _read_validated_migration_history(connection: object) -> list[dict[str, object]]:
    """按既有顺序检查历史表、校验结构，再读取历史；不改变调用方事务。"""
    if not _migration_history_table_exists(connection):
        raise MigrationHistoryError("migration history table does not exist")
    _validate_migration_history_schema(_read_migration_history_schema(connection))
    return _read_migration_history(connection)


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


def _read_applying_017_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 017 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        upgrade_state = read_daily_ledger_upgrade_state(connection)
        return build_applying_017_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            upgrade_state=upgrade_state,
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=17,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


RecoveryInspectionReader = Callable[
    [object, list[PreparedMigration]],
    dict[str, object],
]


def _inspect_applying_migration(
    engine: object,
    paths: Iterable[Path],
    *,
    read_inspection: RecoveryInspectionReader,
) -> dict[str, object]:
    """在唯一 owner lock 内执行版本级只读检查。"""
    manifest = validate_release_migration_manifest(paths)
    with _migration_owner_connection(engine) as owner_connection:
        return read_inspection(owner_connection, manifest)


def _recover_applying_migration(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
    spec: RecoveryOrchestrationSpec,
    read_inspection: RecoveryInspectionReader,
) -> dict[str, object]:
    """执行共享 digest fence、重放和 history 收口骨架。"""
    if re.fullmatch(r"[0-9a-f]{64}", expected_state_digest) is None:
        raise MigrationHistoryError(
            "expected state digest must be 64 lowercase hex characters"
        )
    manifest = validate_release_migration_manifest(paths)
    target = _recovery_target(manifest, spec)
    with _migration_owner_connection(engine) as owner_connection:
        initial = read_inspection(owner_connection, manifest)
        observed_digest = str(initial.get("state_digest") or "")
        if not hmac.compare_digest(observed_digest, expected_state_digest):
            raise MigrationHistoryError(spec.digest_changed_error)
        classification = str(initial.get("classification") or "")
        if classification == "UNSAFE":
            raise MigrationHistoryError(
                spec.unsafe_error_prefix + str(initial.get("reason"))
            )
        if classification == "COMPATIBLE_PARTIAL":
            rollback = getattr(owner_connection, "rollback", None)
            if callable(rollback):
                rollback()
            _assert_migration_owner_lock_held(owner_connection)
            _execute_prepared_migration_files(
                engine,
                [(target.path, target.statements)],
                owner_connection=owner_connection,
            )
            if callable(rollback):
                rollback()
            _assert_migration_owner_lock_held(owner_connection)
            completed = read_inspection(owner_connection, manifest)
            if completed.get("classification") != "COMPLETE":
                raise MigrationPartialApplyError(
                    spec.partial_error_prefix + f"inspection={completed}"
                )
        elif classification != "COMPLETE":
            raise MigrationHistoryError(
                spec.unknown_classification_prefix + repr(classification)
            )
        _assert_migration_owner_lock_held(owner_connection)
        _mark_migration_applied(owner_connection, target)
        return {
            "recovery_outcome": "APPLIED",
            "initial_classification": classification,
            "initial_state_digest": observed_digest,
            "migration": {
                "version": target.version,
                "filename": target.path.name,
                "sha256": target.sha256,
            },
        }


def inspect_applying_migration_017(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_017_inspection,
    )


def recover_applying_migration_017(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 017。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_017,
        read_inspection=_read_applying_017_inspection,
    )


def _read_applying_018_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 018 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_018_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            started_at_shape=_read_schedule_run_started_at_shape(
                connection
            ),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=18,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_018(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 018，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_018_inspection,
    )


def recover_applying_migration_018(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 018。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_018,
        read_inspection=_read_applying_018_inspection,
    )


def _read_applying_019_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 019 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_019_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            pointer_state=_read_serving_pointer_retirement_state(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=19,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_019(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 019，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_019_inspection,
    )


def recover_applying_migration_019(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 019。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_019,
        read_inspection=_read_applying_019_inspection,
    )


def _read_applying_021_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 021 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_021_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            registry_state=_read_registry_owner_recovery_state(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=21,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_021(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 021，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_021_inspection,
    )


def recover_applying_migration_021(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 021。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_021,
        read_inspection=_read_applying_021_inspection,
    )


def _read_applying_022_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 022 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_022_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            auth_schema=read_auth_schema(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=22,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_022(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 022，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_022_inspection,
    )


def recover_applying_migration_022(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 022。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_022,
        read_inspection=_read_applying_022_inspection,
    )


def _read_applying_023_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 023 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_023_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            auth_profile_schema=read_auth_profile_schema(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        return _unsafe_applying_inspection(
            version=23,
            manifest=manifest,
            database_identity=database_identity,
            reason=exc,
        )


def inspect_applying_migration_023(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 023，不执行 DDL/DML。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_023_inspection,
    )


def recover_applying_migration_023(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 023。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_023,
        read_inspection=_read_applying_023_inspection,
    )


def _read_applying_024_inspection(
    connection: object,
    manifest: list[PreparedMigration],
) -> dict[str, object]:
    """在 owner lock 内读取一次 canonical APPLYING 024 状态。"""
    database_identity = _read_database_identity(connection)
    try:
        history = _read_validated_migration_history(connection)
        return build_applying_024_inspection(
            manifest=manifest,
            history=history,
            database_identity=database_identity,
            fact_state=read_scheme_prediction_fact_state(connection),
        )
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        canonical_state = {
            "database_identity": database_identity,
            "inspection_error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        return {
            "classification": "UNSAFE",
            "state_digest": _applying_017_state_digest(canonical_state),
            "database_identity": database_identity,
            "migration": None,
            "reason": str(exc),
        }


def inspect_applying_migration_024(
    engine: object,
    paths: Iterable[Path],
) -> dict[str, object]:
    """连接 live DB，以 SELECT + named lock 检查 024。"""
    return _inspect_applying_migration(
        engine,
        paths,
        read_inspection=_read_applying_024_inspection,
    )


def recover_applying_migration_024(
    engine: object,
    paths: Iterable[Path],
    *,
    expected_state_digest: str,
) -> dict[str, object]:
    """以 inspect digest 为 fence 恢复唯一受支持的 APPLYING 024。"""
    return _recover_applying_migration(
        engine,
        paths,
        expected_state_digest=expected_state_digest,
        spec=SPEC_024,
        read_inspection=_read_applying_024_inspection,
    )


def _read_applying_025_inspection(
    connection: object, manifest: list[PreparedMigration]
) -> dict[str, object]:
    """在既有迁移 owner lock 内读取 025 状态。"""
    identity = _read_database_identity(connection)
    try:
        _validate_migration_history_schema(_read_migration_history_schema(connection))
        return build_applying_025_inspection(
            manifest=manifest,
            history=_read_migration_history(connection),
            database_identity=identity,
            prediction_schema=read_prediction_schema(connection),
        )
    except (KeyError, MigrationPreflightError, TypeError, ValueError) as exc:
        return {
            "classification": "UNSAFE",
            "state_digest": _applying_017_state_digest(
                {"database_identity": identity, "error": str(exc)}
            ),
            "database_identity": identity,
            "migration": None,
            "reason": str(exc),
        }


def inspect_applying_migration_025(engine: object, paths: Iterable[Path]) -> dict[str, object]:
    """只读检查 025 中断恢复；不执行 DDL 或 DML。"""
    return _inspect_applying_migration(engine, paths, read_inspection=_read_applying_025_inspection)


def recover_applying_migration_025(
    engine: object, paths: Iterable[Path], *, expected_state_digest: str
) -> dict[str, object]:
    """只删除仍存在的授权列，完整读回后才标记 APPLIED。"""
    return _recover_applying_migration(
        engine, paths, expected_state_digest=expected_state_digest,
        spec=SPEC_025, read_inspection=_read_applying_025_inspection,
    )


def _legacy_domain_table_count(connection: object) -> int:
    required = sorted(expected_legacy_016_baseline()["tables"])
    table_sql = ",".join(f"'{name}'" for name in required)
    return int(
        connection.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_type = 'BASE TABLE'
                  AND table_name IN ({table_sql})
                """
            )
        ).scalar_one()
    )


def _bootstrap_legacy_migration_history(
    owner_connection: object,
    manifest: list[PreparedMigration],
) -> list[Mapping[str, object]]:
    baseline = [
        migration for migration in manifest if migration.version <= 16
    ]
    if [migration.version for migration in baseline] != list(
        range(1, 17)
    ):
        raise MigrationHistoryError(
            "legacy baseline bootstrap requires exact migrations 001..016"
        )
    rollback = getattr(owner_connection, "rollback", None)
    commit = getattr(owner_connection, "commit", None)
    if callable(rollback):
        rollback()
    try:
        preflight_migration_session(owner_connection)
        for migration in baseline:
            _assert_migration_owner_lock_held(owner_connection)
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
                        'APPLIED',
                        1,
                        UTC_TIMESTAMP(6),
                        UTC_TIMESTAMP(6)
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
    return [
        {
            "version": migration.version,
            "filename": migration.path.name,
            "sha256": migration.sha256,
            "state": "APPLIED",
            "baseline_bootstrap": 1,
        }
        for migration in baseline
    ]


def _load_or_bootstrap_migration_history(
    owner_connection: object,
    manifest: list[PreparedMigration],
) -> list[Mapping[str, object]]:
    history_exists = _migration_history_table_exists(owner_connection)
    history: list[Mapping[str, object]] = []
    if history_exists:
        _validate_migration_history_schema(
            _read_migration_history_schema(owner_connection)
        )
        history = _read_migration_history(owner_connection)
        if history:
            return history

    domain_table_count = _legacy_domain_table_count(owner_connection)
    if domain_table_count > 0:
        read_legacy_016_baseline(owner_connection)
        if not history_exists:
            _create_migration_history_table(owner_connection)
            _validate_migration_history_schema(
                _read_migration_history_schema(owner_connection)
            )
        return _bootstrap_legacy_migration_history(
            owner_connection,
            manifest,
        )

    if not history_exists:
        _create_migration_history_table(owner_connection)
        _validate_migration_history_schema(
            _read_migration_history_schema(owner_connection)
        )
    return []


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
    """执行已解析文件；只由 history runner 或 parser 测试入口调用。"""
    for path, raw_statements in prepared:
        statements = list(raw_statements)
        daily_ledger = _is_daily_ledger_migration(path)
        schedule_run_started_at = (
            _is_schedule_run_started_at_migration(path)
        )
        serving_pointer_retirement = (
            _is_serving_pointer_retirement_migration(path)
        )
        period_average_actuals = _is_period_average_actuals_migration(path)
        registry_owner = _is_registry_owner_migration(path)
        auth_migration = _is_auth_migration(path)
        auth_profile_migration = _is_auth_profile_migration(path)
        scheme_prediction_fact_migration = (
            _is_scheme_prediction_fact_migration(path)
        )
        confidence_retirement = path.name == SPEC_025.filename
        mysql_session = False
        execution_started = False
        ddl_attempted = False
        execution_error: BaseException | None = None
        try:
            with _migration_execution_connection(
                engine,
                owner_connection,
            ) as connection:
                mysql_session = (
                    getattr(connection.dialect, "name", None)
                    == "mysql"
                )
                preflight_migration_session(
                    connection,
                    require_reviewed_constraint_namespace=daily_ledger,
                )
                if daily_ledger and mysql_session:
                    preflight_daily_ledger_upgrade(connection)
                    connection.execute(
                        text(
                            "SET @bond_factor_lab_migration_runner_017 = "
                            ":runner_guard"
                        ),
                        {"runner_guard": DAILY_LEDGER_RUNNER_GUARD},
                    )
                if serving_pointer_retirement and mysql_session:
                    classification = (
                        _classify_serving_pointer_retirement_state(
                            _read_serving_pointer_retirement_state(
                                connection
                            )
                        )
                    )
                    if classification != "COMPATIBLE_PARTIAL":
                        raise MigrationPreflightError(
                            "migration 019 requires the complete "
                            "serving-pointer source definition before "
                            "DROP TABLE"
                        )
                if period_average_actuals and mysql_session:
                    _validate_period_average_actuals_schema(
                        _read_period_average_actuals_schema(connection),
                        allow_missing=True,
                    )
                if registry_owner and mysql_session:
                    _validate_registry_owner_schema(
                        _read_registry_owner_schema(connection),
                        allow_missing=True,
                    )
                if auth_migration and mysql_session:
                    if classify_auth_schema(read_auth_schema(connection)) == "UNSAFE":
                        raise MigrationPreflightError(
                            "migration 022 found authentication schema drift"
                        )
                if auth_profile_migration and mysql_session:
                    if (
                        classify_auth_profile_schema(
                            read_auth_profile_schema(connection)
                        )
                        == "UNSAFE"
                    ):
                        raise MigrationPreflightError(
                            "migration 023 found authentication profile "
                            "schema drift"
                        )
                if scheme_prediction_fact_migration and mysql_session:
                    classification = classify_scheme_prediction_fact_state(
                        read_scheme_prediction_fact_state(connection)
                    )
                    if classification == "UNSAFE":
                        raise MigrationPreflightError(
                            "migration 024 found scheme prediction fact drift"
                        )
                    # 024 的 ALTER TABLE 由 PREPARE/EXECUTE 动态执行。预检之后
                    # 任一语句失败都可能发生在 MySQL 已隐式提交 DDL 之后，因此
                    # 必须按 partial-apply 报告并进入受控 inspect/recovery。
                    ddl_attempted = True
                if confidence_retirement and mysql_session:
                    if classify_prediction_schema(read_prediction_schema(connection)) == "UNSAFE":
                        raise MigrationPreflightError("migration 025 found prediction schema drift")
                    # 动态 EXECUTE 包含 DDL；失败后必须经 inspect/recovery，而非普通重试。
                    ddl_attempted = True
                for statement in statements:
                    if owner_connection is not None:
                        _assert_migration_owner_lock_held(connection)
                    execution_started = True
                    if mysql_session and _is_ddl_statement(statement):
                        ddl_attempted = True
                    connection.execute(text(statement))
        except BaseException as exc:
            execution_error = exc

        postcondition_error: BaseException | None = None
        if daily_ledger and mysql_session and execution_started:
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(
                        connection,
                        require_reviewed_constraint_namespace=True,
                    )
                    fingerprint = (
                        read_daily_ledger_schema_fingerprint(connection)
                    )
                    validate_daily_ledger_schema_fingerprint(
                        fingerprint,
                        allow_missing=False,
                    )
            except BaseException as exc:
                postcondition_error = exc
        if (
            schedule_run_started_at
            and mysql_session
            and execution_started
        ):
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    classification = (
                        _classify_schedule_run_started_at_shape(
                            _read_schedule_run_started_at_shape(
                                connection
                            )
                        )
                    )
                    if classification != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 018 did not reach target definition"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if (
            serving_pointer_retirement
            and mysql_session
            and execution_started
        ):
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    classification = (
                        _classify_serving_pointer_retirement_state(
                            _read_serving_pointer_retirement_state(
                                connection
                            )
                        )
                    )
                    if classification != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 019 did not remove "
                            "t_scheme_serving_pointer"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if period_average_actuals and mysql_session and execution_started:
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    _validate_period_average_actuals_schema(
                        _read_period_average_actuals_schema(connection),
                        allow_missing=False,
                    )
            except BaseException as exc:
                postcondition_error = exc
        if registry_owner and mysql_session and execution_started:
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    _validate_registry_owner_schema(
                        _read_registry_owner_schema(connection),
                        allow_missing=False,
                    )
            except BaseException as exc:
                postcondition_error = exc
        if auth_migration and mysql_session and execution_started:
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    if classify_auth_schema(read_auth_schema(connection)) != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 022 did not reach complete schema"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if auth_profile_migration and mysql_session and execution_started:
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    if (
                        classify_auth_profile_schema(
                            read_auth_profile_schema(connection)
                        )
                        != "COMPLETE"
                    ):
                        raise MigrationPreflightError(
                            "migration 023 did not reach complete schema"
                        )
            except BaseException as exc:
                postcondition_error = exc
        if (
            scheme_prediction_fact_migration
            and mysql_session
            and execution_started
        ):
            try:
                with _migration_execution_connection(
                    engine,
                    owner_connection,
                ) as connection:
                    preflight_migration_session(connection)
                    state = read_scheme_prediction_fact_state(connection)
                    if classify_scheme_prediction_fact_state(state) != "COMPLETE":
                        raise MigrationPreflightError(
                            "migration 024 did not reach complete scheme "
                            "prediction fact state"
                        )
            except BaseException as exc:
                postcondition_error = exc

        if confidence_retirement and mysql_session and execution_started:
            try:
                with _migration_execution_connection(engine, owner_connection) as connection:
                    preflight_migration_session(connection)
                    if classify_prediction_schema(read_prediction_schema(connection)) != "COMPLETE":
                        raise MigrationPreflightError("migration 025 did not reach complete schema")
            except BaseException as exc:
                postcondition_error = exc

        if execution_error is not None:
            if ddl_attempted:
                raise _partial_apply_error(
                    path,
                    execution_error=execution_error,
                    postcondition_error=postcondition_error,
                ) from execution_error
            raise execution_error
        if postcondition_error is not None:
            raise _partial_apply_error(
                path,
                execution_error=None,
                postcondition_error=postcondition_error,
            ) from postcondition_error
        print(f"applied {path.name}")


def apply_pending_migration_files(
    engine: object,
    paths: Iterable[Path],
) -> None:
    """持有 MySQL owner lock，以 checksum history 只执行 pending 后缀。"""
    manifest = validate_release_migration_manifest(paths)
    with _migration_owner_connection(
        engine,
        require_mysql=False,
    ) as owner_connection:
        if getattr(owner_connection.dialect, "name", None) != "mysql":
            _execute_prepared_migration_files(
                engine,
                [
                    (migration.path, migration.statements)
                    for migration in manifest
                ],
            )
            return
        history = _load_or_bootstrap_migration_history(
            owner_connection,
            manifest,
        )
        pending = select_pending_migrations(manifest, history)
        if history and int(history[-1]["version"]) == SPEC_025.version:
            if classify_prediction_schema(read_prediction_schema(owner_connection)) != "COMPLETE":
                raise MigrationPreflightError("applied migration 025 prediction schema drift")
        for migration in pending:
            if _is_registry_owner_migration(migration.path):
                rollback = getattr(owner_connection, "rollback", None)
                if callable(rollback):
                    rollback()
                _assert_migration_owner_lock_held(owner_connection)
                preflight_migration_session(owner_connection)
                _classify_registry_owner_recovery_state(
                    _read_registry_owner_recovery_state(
                        owner_connection
                    ),
                    authority=_registry_owner_authority(migration),
                )
            _assert_migration_owner_lock_held(owner_connection)
            _mark_migration_applying(owner_connection, migration)
            _assert_migration_owner_lock_held(owner_connection)
            _execute_prepared_migration_files(
                engine,
                [(migration.path, migration.statements)],
                owner_connection=owner_connection,
            )
            _assert_migration_owner_lock_held(owner_connection)
            _mark_migration_applied(owner_connection, migration)
