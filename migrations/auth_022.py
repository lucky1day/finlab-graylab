from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import text


AUTH_TABLES = (
    "t_auth_users",
    "t_auth_sessions",
    "t_auth_audit_logs",
)


def _column(
    column_type: str,
    nullable: str,
    default: object,
    extra: str = "",
    charset: str | None = None,
    collation: str | None = None,
) -> tuple[object, ...]:
    return (column_type, nullable, default, extra, charset, collation)


EXPECTED_COLUMNS: dict[str, dict[str, tuple[object, ...]]] = {
    "t_auth_users": {
        "id": _column("bigint unsigned", "NO", None, "auto_increment"),
        "username": _column("varchar(32)", "NO", None, charset="ascii", collation="ascii_bin"),
        "password_hash": _column("varchar(255)", "NO", None, charset="ascii", collation="ascii_bin"),
        "role": _column("enum('admin','user')", "NO", None, charset="utf8mb4", collation="utf8mb4_0900_ai_ci"),
        "status": _column("enum('active','disabled')", "NO", "active", charset="utf8mb4", collation="utf8mb4_0900_ai_ci"),
        "is_protected_admin": _column("tinyint(1)", "NO", "0"),
        "must_change_password": _column("tinyint(1)", "NO", "1"),
        "failed_login_count": _column("int unsigned", "NO", "0"),
        "login_not_before": _column("datetime(6)", "YES", None),
        "password_changed_at": _column("datetime(6)", "NO", None),
        "created_by": _column("bigint unsigned", "YES", None),
        "disabled_by": _column("bigint unsigned", "YES", None),
        "disabled_at": _column("datetime(6)", "YES", None),
        "created_at": _column("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED"),
        "updated_at": _column("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)"),
    },
    "t_auth_sessions": {
        "id": _column("bigint unsigned", "NO", None, "auto_increment"),
        "user_id": _column("bigint unsigned", "NO", None),
        "token_hash": _column("binary(32)", "NO", None),
        "expires_at": _column("datetime(6)", "NO", None),
        "revoked_at": _column("datetime(6)", "YES", None),
        "created_at": _column("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED"),
    },
    "t_auth_audit_logs": {
        "id": _column("bigint unsigned", "NO", None, "auto_increment"),
        "event_type": _column("varchar(48)", "NO", None, charset="ascii", collation="ascii_bin"),
        "actor_user_id": _column("bigint unsigned", "YES", None),
        "target_user_id": _column("bigint unsigned", "YES", None),
        "request_id": _column("varchar(128)", "NO", None, charset="ascii", collation="ascii_bin"),
        "detail": _column("json", "NO", None),
        "created_at": _column("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED"),
    },
}

EXPECTED_INDEXES = {
    "t_auth_users": {
        "PRIMARY": (True, ("id",)),
        "uk_auth_users_username": (True, ("username",)),
        "idx_auth_users_role_status_id": (False, ("role", "status", "id")),
        "fk_auth_users_created_by": (False, ("created_by",)),
        "fk_auth_users_disabled_by": (False, ("disabled_by",)),
    },
    "t_auth_sessions": {
        "PRIMARY": (True, ("id",)),
        "uk_auth_sessions_token_hash": (True, ("token_hash",)),
        "idx_auth_sessions_user_active": (False, ("user_id", "revoked_at", "expires_at")),
        "idx_auth_sessions_expiry": (False, ("expires_at", "id")),
    },
    "t_auth_audit_logs": {
        "PRIMARY": (True, ("id",)),
        "idx_auth_audit_target_created": (False, ("target_user_id", "created_at", "id")),
        "idx_auth_audit_actor_created": (False, ("actor_user_id", "created_at", "id")),
        "idx_auth_audit_event_created": (False, ("event_type", "created_at", "id")),
    },
}

EXPECTED_FOREIGN_KEYS = {
    "fk_auth_users_created_by": (
        "t_auth_users", "created_by", "t_auth_users", "id", "RESTRICT", "RESTRICT"
    ),
    "fk_auth_users_disabled_by": (
        "t_auth_users", "disabled_by", "t_auth_users", "id", "RESTRICT", "RESTRICT"
    ),
    "fk_auth_sessions_user": (
        "t_auth_sessions", "user_id", "t_auth_users", "id", "RESTRICT", "RESTRICT"
    ),
    "fk_auth_audit_actor": (
        "t_auth_audit_logs", "actor_user_id", "t_auth_users", "id", "RESTRICT", "RESTRICT"
    ),
    "fk_auth_audit_target": (
        "t_auth_audit_logs", "target_user_id", "t_auth_users", "id", "RESTRICT", "RESTRICT"
    ),
}


def read_auth_schema(connection: Any) -> dict[str, object]:
    """读取 022 三张表完整且稳定的 information_schema 指纹。"""
    table_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   engine AS engine,
                   table_collation AS table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name IN ('t_auth_users','t_auth_sessions','t_auth_audit_logs')
            ORDER BY table_name
            """
        )
    ).mappings().all()
    tables = {
        str(row["table_name"]): (
            str(row["engine"] or "").upper(),
            str(row["table_collation"] or ""),
        )
        for row in table_rows
    }
    column_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   column_name AS column_name,
                   ordinal_position AS ordinal_position,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra,
                   character_set_name AS character_set_name,
                   collation_name AS collation_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name IN ('t_auth_users','t_auth_sessions','t_auth_audit_logs')
            ORDER BY table_name, ordinal_position
            """
        )
    ).mappings().all()
    columns: dict[str, dict[str, tuple[object, ...]]] = {}
    column_order: dict[str, list[str]] = {}
    for row in column_rows:
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        columns.setdefault(table_name, {})[column_name] = (
            str(row["column_type"]),
            str(row["is_nullable"]),
            row["column_default"],
            str(row["extra"] or ""),
            row["character_set_name"],
            row["collation_name"],
        )
        column_order.setdefault(table_name, []).append(column_name)
    index_rows = connection.execute(
        text(
            """
            SELECT table_name AS table_name,
                   index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name,
                   sub_part AS sub_part,
                   index_type AS index_type
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name IN ('t_auth_users','t_auth_sessions','t_auth_audit_logs')
            ORDER BY table_name, index_name, seq_in_index
            """
        )
    ).mappings().all()
    index_parts: dict[tuple[str, str], list[str]] = {}
    index_unique: dict[tuple[str, str], bool] = {}
    index_metadata: dict[tuple[str, str], set[tuple[object, object]]] = {}
    for row in index_rows:
        key = (str(row["table_name"]), str(row["index_name"]))
        index_parts.setdefault(key, []).append(str(row["column_name"]))
        index_unique[key] = not bool(row["non_unique"])
        index_metadata.setdefault(key, set()).add(
            (row["sub_part"], str(row["index_type"]).upper())
        )
    indexes: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {}
    invalid_index_metadata: dict[str, list[str]] = {}
    for (table_name, index_name), parts in index_parts.items():
        indexes.setdefault(table_name, {})[index_name] = (
            index_unique[(table_name, index_name)], tuple(parts)
        )
        if index_metadata[(table_name, index_name)] != {(None, "BTREE")}:
            invalid_index_metadata.setdefault(table_name, []).append(index_name)
    fk_rows = connection.execute(
        text(
            """
            SELECT k.constraint_name AS constraint_name,
                   k.table_name AS table_name,
                   k.column_name AS column_name,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.delete_rule AS delete_rule,
                   r.update_rule AS update_rule
            FROM information_schema.key_column_usage AS k
            INNER JOIN information_schema.referential_constraints AS r
              ON r.constraint_schema = k.constraint_schema
             AND r.constraint_name = k.constraint_name
             AND r.table_name = k.table_name
            WHERE k.constraint_schema = DATABASE()
              AND k.table_name IN ('t_auth_users','t_auth_sessions','t_auth_audit_logs')
            ORDER BY k.constraint_name, k.ordinal_position
            """
        )
    ).mappings().all()
    foreign_keys = {
        str(row["constraint_name"]): (
            str(row["table_name"]),
            str(row["column_name"]),
            str(row["referenced_table_name"]),
            str(row["referenced_column_name"]),
            str(row["delete_rule"]),
            str(row["update_rule"]),
        )
        for row in fk_rows
    }
    return {
        "tables": tables,
        "columns": columns,
        "column_order": {key: tuple(value) for key, value in column_order.items()},
        "indexes": indexes,
        "invalid_index_metadata": invalid_index_metadata,
        "foreign_keys": foreign_keys,
    }


def classify_auth_schema(schema: Mapping[str, object]) -> str:
    """精确分类全无、可恢复部分、完整与漂移状态。"""
    tables = schema.get("tables")
    columns = schema.get("columns")
    column_order = schema.get("column_order")
    indexes = schema.get("indexes")
    invalid_index_metadata = schema.get("invalid_index_metadata")
    foreign_keys = schema.get("foreign_keys")
    if not all(
        isinstance(value, Mapping)
        for value in (
            tables, columns, column_order, indexes,
            invalid_index_metadata, foreign_keys,
        )
    ):
        return "UNSAFE"
    existing = set(tables)
    if not existing.issubset(AUTH_TABLES):
        return "UNSAFE"
    expected_fks = {
        name: value
        for name, value in EXPECTED_FOREIGN_KEYS.items()
        if value[0] in existing
    }
    for table_name in existing:
        if tables.get(table_name) != ("INNODB", "utf8mb4_0900_ai_ci"):
            return "UNSAFE"
        expected_columns = EXPECTED_COLUMNS[table_name]
        if columns.get(table_name) != expected_columns:
            return "UNSAFE"
        if tuple(column_order.get(table_name, ())) != tuple(expected_columns):
            return "UNSAFE"
        if indexes.get(table_name) != EXPECTED_INDEXES[table_name]:
            return "UNSAFE"
        if invalid_index_metadata.get(table_name):
            return "UNSAFE"
    if dict(foreign_keys) != expected_fks:
        return "UNSAFE"
    return "COMPLETE" if existing == set(AUTH_TABLES) else "COMPATIBLE_PARTIAL"
