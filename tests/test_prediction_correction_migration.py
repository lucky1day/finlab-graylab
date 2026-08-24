from __future__ import annotations

from pathlib import Path

import pytest

from migrations.runner import (
    MIGRATIONS_DIR,
    MigrationPreflightError,
    _expected_prediction_corrections_schema,
    _validate_prediction_corrections_schema,
    build_applying_021_inspection,
    validate_release_migration_manifest,
)


def _manifest():
    return validate_release_migration_manifest(
        sorted(Path(MIGRATIONS_DIR).glob("*.sql"))
    )


def _history(manifest):
    return [
        {
            "version": migration.version,
            "filename": migration.path.name,
            "sha256": migration.sha256,
            "state": "APPLYING" if migration.version == 21 else "APPLIED",
            "baseline_bootstrap": 0,
        }
        for migration in manifest
        if migration.version <= 21
    ]


def test_prediction_correction_migration_schema_is_closed_world() -> None:
    expected = {"exists": True, **_expected_prediction_corrections_schema()}
    _validate_prediction_corrections_schema(
        {"exists": False},
        allow_missing=True,
    )
    _validate_prediction_corrections_schema(expected, allow_missing=False)

    for field in ("columns", "indexes", "checks", "foreign_keys"):
        drifted = dict(expected)
        drifted[field] = {}
        with pytest.raises(MigrationPreflightError, match="unexpected"):
            _validate_prediction_corrections_schema(
                drifted,
                allow_missing=False,
            )
    with pytest.raises(MigrationPreflightError, match="missing"):
        _validate_prediction_corrections_schema(
            {"exists": False},
            allow_missing=False,
        )


def test_applying_021_inspection_accepts_only_missing_or_exact_schema() -> None:
    manifest = _manifest()
    history = _history(manifest)
    identity = {
        "database_name": "bond_db",
        "server_uuid": "11111111-1111-1111-1111-111111111111",
    }
    missing = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        corrections_schema={"exists": False},
    )
    complete = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        corrections_schema={
            "exists": True,
            **_expected_prediction_corrections_schema(),
        },
    )
    drifted_schema = {
        "exists": True,
        **_expected_prediction_corrections_schema(),
    }
    drifted_schema["checks"] = {}
    unsafe = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        corrections_schema=drifted_schema,
    )

    assert missing["classification"] == "COMPATIBLE_PARTIAL"
    assert complete["classification"] == "COMPLETE"
    assert unsafe["classification"] == "UNSAFE"
    assert len(str(missing["state_digest"])) == 64


def test_applying_021_inspection_rejects_history_drift() -> None:
    manifest = _manifest()
    history = _history(manifest)
    history[-1]["state"] = "APPLIED"
    inspection = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity={
            "database_name": "bond_db",
            "server_uuid": "11111111-1111-1111-1111-111111111111",
        },
        corrections_schema={"exists": False},
    )

    assert inspection["classification"] == "UNSAFE"
    assert "only migration 021 may be APPLYING" in str(inspection["reason"])
