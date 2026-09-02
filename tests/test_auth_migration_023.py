from __future__ import annotations

from migrations.auth_profile_023 import (
    BASE_USER_COLUMN_ORDER,
    classify_auth_profile_schema,
)


def _schema(
    *,
    full_name: tuple[object, ...] | None = None,
    organization_name: tuple[object, ...] | None = None,
    default: str = "1",
    forced_count: int = 0,
) -> dict[str, object]:
    columns = {}
    if full_name is not None:
        columns["full_name"] = full_name
    if organization_name is not None:
        columns["organization_name"] = organization_name
    profile_order = tuple(
        name
        for name in ("full_name", "organization_name")
        if name in columns
    )
    return {
        "base_schema_classification": "COMPLETE",
        "profile_columns": columns,
        "user_column_order": (
            BASE_USER_COLUMN_ORDER[:2]
            + profile_order
            + BASE_USER_COLUMN_ORDER[2:]
        ),
        "must_change_password_default": default,
        "forced_password_user_count": forced_count,
    }


FULL_NAME = (
    "varchar(100)", "YES", None, "", "utf8mb4", "utf8mb4_0900_ai_ci"
)
ORGANIZATION_NAME = (
    "varchar(200)", "YES", None, "", "utf8mb4", "utf8mb4_0900_ai_ci"
)


def test_profile_schema_classifies_pre_apply_partial_and_complete() -> None:
    assert classify_auth_profile_schema(_schema()) == "COMPATIBLE_PARTIAL"
    assert classify_auth_profile_schema(
        _schema(full_name=FULL_NAME)
    ) == "COMPATIBLE_PARTIAL"
    assert classify_auth_profile_schema(
        _schema(
            full_name=FULL_NAME,
            organization_name=ORGANIZATION_NAME,
            default="0",
            forced_count=1,
        )
    ) == "COMPATIBLE_PARTIAL"
    assert classify_auth_profile_schema(
        _schema(
            full_name=FULL_NAME,
            organization_name=ORGANIZATION_NAME,
            default="0",
        )
    ) == "COMPLETE"


def test_profile_schema_rejects_base_column_order_and_definition_drift() -> None:
    base_drift = _schema()
    base_drift["base_schema_classification"] = "UNSAFE"
    order_drift = _schema(
        full_name=FULL_NAME,
        organization_name=ORGANIZATION_NAME,
        default="0",
    )
    order_drift["user_column_order"] = (
        BASE_USER_COLUMN_ORDER
        + ("full_name", "organization_name")
    )
    type_drift = _schema(full_name=("varchar(99)",) + FULL_NAME[1:])
    for drift in (base_drift, order_drift, type_drift):
        assert classify_auth_profile_schema(drift) == "UNSAFE"
