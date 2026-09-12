from __future__ import annotations

from copy import deepcopy
from itertools import product

import pytest

from migrations.platform_confidence_025 import (
    TABLES,
    classify_prediction_schema,
    expected_prediction_schema,
)
from migrations.runner import (
    MIGRATIONS_DIR,
    build_applying_025_inspection,
    validate_release_migration_manifest,
)


@pytest.mark.parametrize("present", list(product((False, True), repeat=2)))
def test_only_authorized_column_absence_is_recoverable(present) -> None:
    schema = expected_prediction_schema()
    source = expected_prediction_schema(with_confidence=True)
    for table, keep in zip(TABLES, present):
        if keep:
            schema[table] = source[table]
    assert classify_prediction_schema(schema) == (
        "COMPATIBLE_PARTIAL" if any(present) else "COMPLETE"
    )


@pytest.mark.parametrize("category", ["columns", "indexes", "foreign_keys", "checks"])
def test_unrelated_schema_drift_is_rejected(category) -> None:
    schema = expected_prediction_schema()
    schema[TABLES[0]][category] = () if category == "columns" else {}
    assert classify_prediction_schema(schema) == "UNSAFE"


def _inspection_inputs():
    manifest = validate_release_migration_manifest(sorted(MIGRATIONS_DIR.glob("*.sql")))
    return {
        "manifest": manifest,
        "history": [
            {"version": m.version, "filename": m.path.name, "sha256": m.sha256,
             "state": "APPLYING" if m.version == 25 else "APPLIED",
             "baseline_bootstrap": 0}
            for m in manifest
        ],
        "database_identity": {"database_name": "isolated_test", "server_uuid": "test-uuid"},
        "prediction_schema": expected_prediction_schema(),
    }


def test_recovery_digest_binds_schema_database_and_exact_history() -> None:
    inputs = _inspection_inputs()
    complete = build_applying_025_inspection(**inputs)
    assert complete["classification"] == "COMPLETE"
    for key, value in (
        ("prediction_schema", expected_prediction_schema(with_confidence=True)),
        ("database_identity", {"database_name": "another", "server_uuid": "test-uuid"}),
    ):
        changed = build_applying_025_inspection(**{**inputs, key: value})
        assert changed["state_digest"] != complete["state_digest"]
    for field, value in (("sha256", "0" * 64), ("state", "APPLIED"), ("baseline_bootstrap", 1)):
        drift = deepcopy(inputs)
        drift["history"][-1][field] = value
        assert build_applying_025_inspection(**drift)["classification"] == "UNSAFE"


def test_wrong_confidence_type_and_missing_table_are_not_partial_states() -> None:
    schema = expected_prediction_schema(with_confidence=True)
    schema[TABLES[0]]["columns"] = tuple(
        (name, "DOUBLE" if name == "confidence" else kind, nullable, default)
        for name, kind, nullable, default in schema[TABLES[0]]["columns"]
    )
    assert classify_prediction_schema(schema) == "UNSAFE"
    assert classify_prediction_schema({TABLES[0]: schema[TABLES[0]]}) == "UNSAFE"
