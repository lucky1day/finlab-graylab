from __future__ import annotations

from copy import deepcopy

from migrations.auth_022 import (
    AUTH_TABLES,
    EXPECTED_COLUMNS,
    EXPECTED_CONSTRAINTS,
    EXPECTED_FOREIGN_KEYS,
    EXPECTED_INDEXES,
    classify_auth_schema,
)


def _schema(table_count: int = 3) -> dict[str, object]:
    existing = set(AUTH_TABLES[:table_count])
    return {
        "tables": {
            table: ("INNODB", "utf8mb4_0900_ai_ci")
            for table in existing
        },
        "columns": {
            table: deepcopy(EXPECTED_COLUMNS[table]) for table in existing
        },
        "column_order": {
            table: tuple(EXPECTED_COLUMNS[table]) for table in existing
        },
        "indexes": {
            table: deepcopy(EXPECTED_INDEXES[table]) for table in existing
        },
        "invalid_index_metadata": {},
        "foreign_keys": {
            name: value
            for name, value in EXPECTED_FOREIGN_KEYS.items()
            if value[0] in existing
        },
        "constraints": {
            table: deepcopy(EXPECTED_CONSTRAINTS[table])
            for table in existing
        },
    }


def test_auth_schema_accepts_zero_one_two_and_three_table_prefixes() -> None:
    assert classify_auth_schema(_schema(0)) == "COMPATIBLE_PARTIAL"
    assert classify_auth_schema(_schema(1)) == "COMPATIBLE_PARTIAL"
    assert classify_auth_schema(_schema(2)) == "COMPATIBLE_PARTIAL"
    assert classify_auth_schema(_schema(3)) == "COMPLETE"


def test_auth_schema_rejects_column_index_constraint_and_collation_drift() -> None:
    column_drift = _schema()
    column_drift["columns"]["t_auth_users"]["username"] = (  # type: ignore[index]
        "varchar(64)", "NO", None, "", "ascii", "ascii_bin"
    )
    index_drift = _schema()
    del index_drift["indexes"]["t_auth_sessions"][  # type: ignore[index]
        "uk_auth_sessions_token_hash"
    ]
    fk_drift = _schema()
    del fk_drift["foreign_keys"]["fk_auth_sessions_user"]  # type: ignore[index]
    constraint_drift = _schema()
    constraint_drift["constraints"]["t_auth_users"][  # type: ignore[index]
        "ck_unexpected"
    ] = "CHECK"
    collation_drift = _schema()
    collation_drift["tables"]["t_auth_users"] = (  # type: ignore[index]
        "INNODB", "utf8mb4_general_ci"
    )
    for drift in (
        column_drift,
        index_drift,
        fk_drift,
        constraint_drift,
        collation_drift,
    ):
        assert classify_auth_schema(drift) == "UNSAFE"
