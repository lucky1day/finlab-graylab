from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from sqlalchemy import text

from migrations.auth_022 import classify_auth_schema, read_auth_schema


PROFILE_COLUMNS = ("full_name", "organization_name")
BASE_USER_COLUMN_ORDER = (
    "id",
    "username",
    "password_hash",
    "role",
    "status",
    "is_protected_admin",
    "must_change_password",
    "failed_login_count",
    "login_not_before",
    "password_changed_at",
    "created_by",
    "disabled_by",
    "disabled_at",
    "created_at",
    "updated_at",
)
EXPECTED_PROFILE_COLUMNS: dict[str, tuple[object, ...]] = {
    "full_name": (
        "varchar(100)",
        "YES",
        None,
        "",
        "utf8mb4",
        "utf8mb4_0900_ai_ci",
    ),
    "organization_name": (
        "varchar(200)",
        "YES",
        None,
        "",
        "utf8mb4",
        "utf8mb4_0900_ai_ci",
    ),
}


def read_auth_profile_schema(connection: Any) -> dict[str, object]:
    """读取 023 用户资料列与取消强制改密的精确状态。"""
    snapshot = read_auth_schema(connection)
    base_snapshot = deepcopy(snapshot)
    columns = base_snapshot.get("columns", {})
    column_order = base_snapshot.get("column_order", {})
    user_columns = columns.get("t_auth_users", {})
    user_order = tuple(column_order.get("t_auth_users", ()))

    profile_columns = {
        name: user_columns[name]
        for name in PROFILE_COLUMNS
        if name in user_columns
    }
    for name in PROFILE_COLUMNS:
        user_columns.pop(name, None)
    column_order["t_auth_users"] = tuple(
        name for name in user_order if name not in PROFILE_COLUMNS
    )

    must_change = user_columns.get("must_change_password")
    must_change_default: object = None
    if isinstance(must_change, tuple) and len(must_change) == 6:
        must_change_default = must_change[2]
        normalized = list(must_change)
        normalized[2] = "1"
        user_columns["must_change_password"] = tuple(normalized)

    forced_count = int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM t_auth_users "
                "WHERE must_change_password <> 0"
            )
        ).scalar_one()
    )
    return {
        "base_schema_classification": classify_auth_schema(base_snapshot),
        "profile_columns": profile_columns,
        "user_column_order": user_order,
        "must_change_password_default": must_change_default,
        "forced_password_user_count": forced_count,
    }


def classify_auth_profile_schema(schema: Mapping[str, object]) -> str:
    """分类 023 未执行、可安全重放、完整与漂移状态。"""
    if schema.get("base_schema_classification") != "COMPLETE":
        return "UNSAFE"
    columns = schema.get("profile_columns")
    order = schema.get("user_column_order")
    default = schema.get("must_change_password_default")
    forced_count = schema.get("forced_password_user_count")
    if not isinstance(columns, Mapping) or not isinstance(order, tuple):
        return "UNSAFE"
    if not set(columns).issubset(PROFILE_COLUMNS):
        return "UNSAFE"
    for name, definition in columns.items():
        if definition != EXPECTED_PROFILE_COLUMNS[name]:
            return "UNSAFE"
    expected_order = (
        BASE_USER_COLUMN_ORDER[:2]
        + tuple(name for name in PROFILE_COLUMNS if name in columns)
        + BASE_USER_COLUMN_ORDER[2:]
    )
    if order != expected_order or default not in {"0", "1"}:
        return "UNSAFE"
    if not isinstance(forced_count, int) or forced_count < 0:
        return "UNSAFE"
    if (
        set(columns) == set(PROFILE_COLUMNS)
        and default == "0"
        and forced_count == 0
    ):
        return "COMPLETE"
    return "COMPATIBLE_PARTIAL"
