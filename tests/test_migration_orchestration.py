from __future__ import annotations

from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest

from migrations.recovery_versions import RECOVERY_SPECS
from migrations.runner import (
    MIGRATIONS_DIR,
    MigrationHistoryError,
    MigrationPartialApplyError,
    PreparedMigration,
    _recover_applying_migration,
    _recovery_target,
    validate_release_migration_manifest,
)
from scripts import apply_migrations


EXPECTED_SERVER_UUID = "12345678-1234-1234-1234-123456789abc"


def test_cli_mode_table_is_complete_and_unique() -> None:
    modes = apply_migrations._MIGRATION_MODES
    assert [mode.option for mode in modes] == [
        "--inspect-applying-017",
        "--recover-applying-017",
        "--inspect-applying-018",
        "--recover-applying-018",
        "--inspect-applying-019",
        "--recover-applying-019",
        "--inspect-applying-021",
        "--recover-applying-021",
    ]
    assert len({mode.option for mode in modes}) == len(modes)
    assert len({mode.dest for mode in modes}) == len(modes)
    assert len({mode.handler_name for mode in modes}) == len(modes)
    for mode in modes:
        version = mode.option.rsplit("-", 1)[1]
        assert mode.dest == mode.option.removeprefix("--").replace("-", "_")
        assert mode.action in {"inspect", "recover"}
        assert mode.handler_name == (
            f"{mode.action}_applying_migration_{version}"
        )


def test_cli_help_matches_reviewed_fixture() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(project_root / "scripts" / "apply_migrations.py"),
            "--help",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "80"},
    )
    expected = (
        project_root / "tests" / "fixtures" / "apply_migrations_help.txt"
    ).read_text(encoding="utf-8")

    assert completed.stdout == expected
    assert completed.stderr == ""


@pytest.mark.parametrize("spec", RECOVERY_SPECS)
def test_recovery_specs_are_immutable_and_match_release_manifest(spec) -> None:
    manifest = validate_release_migration_manifest(
        sorted(MIGRATIONS_DIR.glob("*.sql"))
    )
    target = next(
        migration
        for migration in manifest
        if migration.version == spec.version
    )

    assert target.path.name == spec.filename
    assert target.sha256 == spec.sha256
    with pytest.raises(FrozenInstanceError):
        spec.version = 999


@pytest.mark.parametrize("spec", RECOVERY_SPECS)
def test_recovery_target_rejects_reviewed_checksum_drift(spec) -> None:
    target = PreparedMigration(
        version=spec.version,
        path=Path(spec.filename),
        sha256=hashlib.sha256(b"drift").hexdigest(),
        statements=("SELECT 1",),
    )

    with pytest.raises(
        MigrationHistoryError,
        match=f"reviewed migration {spec.version:03d}",
    ):
        _recovery_target([target], spec)


@pytest.mark.parametrize("spec", RECOVERY_SPECS)
def test_complete_recovery_marks_history_without_replay(spec) -> None:
    target = PreparedMigration(
        version=spec.version,
        path=Path(spec.filename),
        sha256=spec.sha256,
        statements=("SELECT 1",),
    )
    digest = "a" * 64
    read_inspection = Mock(
        return_value={
            "classification": "COMPLETE",
            "state_digest": digest,
        }
    )
    owner = Mock()
    owner.execute.return_value.scalar_one.return_value = 1
    with (
        patch(
            "migrations.runner.validate_release_migration_manifest",
            return_value=[target],
        ),
        patch(
            "migrations.runner._migration_owner_connection",
            return_value=nullcontext(owner),
        ),
        patch("migrations.runner._execute_prepared_migration_files") as replay,
        patch("migrations.runner._mark_migration_applied") as mark_applied,
    ):
        result = _recover_applying_migration(
            object(),
            [target.path],
            expected_state_digest=digest,
            spec=spec,
            read_inspection=read_inspection,
        )

    replay.assert_not_called()
    mark_applied.assert_called_once_with(owner, target)
    assert result == {
        "recovery_outcome": "APPLIED",
        "initial_classification": "COMPLETE",
        "initial_state_digest": digest,
        "migration": {
            "version": spec.version,
            "filename": spec.filename,
            "sha256": spec.sha256,
        },
    }


@pytest.mark.parametrize("spec", RECOVERY_SPECS)
def test_partial_recovery_requires_complete_readback_before_marking(spec) -> None:
    target = PreparedMigration(
        version=spec.version,
        path=Path(spec.filename),
        sha256=spec.sha256,
        statements=("SELECT 1",),
    )
    digest = "b" * 64
    read_inspection = Mock(
        side_effect=[
            {
                "classification": "COMPATIBLE_PARTIAL",
                "state_digest": digest,
            },
            {
                "classification": "COMPATIBLE_PARTIAL",
                "state_digest": "c" * 64,
            },
        ]
    )
    owner = Mock()
    owner.execute.return_value.scalar_one.return_value = 1
    with (
        patch(
            "migrations.runner.validate_release_migration_manifest",
            return_value=[target],
        ),
        patch(
            "migrations.runner._migration_owner_connection",
            return_value=nullcontext(owner),
        ),
        patch("migrations.runner._execute_prepared_migration_files") as replay,
        patch("migrations.runner._mark_migration_applied") as mark_applied,
        pytest.raises(
            MigrationPartialApplyError,
            match=f"migration {spec.version:03d} replay did not reach COMPLETE",
        ),
    ):
        _recover_applying_migration(
            object(),
            [target.path],
            expected_state_digest=digest,
            spec=spec,
            read_inspection=read_inspection,
        )

    replay.assert_called_once_with(
        ANY,
        [(target.path, target.statements)],
        owner_connection=owner,
    )
    assert owner.rollback.call_count == 2
    mark_applied.assert_not_called()


@pytest.mark.parametrize("version", (17, 18, 19, 21))
def test_cli_inspection_modes_remain_read_only(version: int) -> None:
    args = apply_migrations._parse_args(
        [f"--inspect-applying-{version:03d}"]
    )

    assert getattr(args, f"inspect_applying_{version:03d}")
    assert not args.apply
    assert args.state_digest is None


@pytest.mark.parametrize("version", (17, 18, 19, 21))
def test_cli_recovery_modes_keep_digest_and_identity_fences(version: int) -> None:
    digest = "d" * 64
    args = apply_migrations._parse_args(
        [
            f"--recover-applying-{version:03d}",
            "--apply",
            "--state-digest",
            digest,
            "--expected-database-name",
            "bond_db",
            "--expected-server-uuid",
            EXPECTED_SERVER_UUID,
        ]
    )

    assert getattr(args, f"recover_applying_{version:03d}")
    assert args.state_digest == digest
    assert args.expected_database_name == "bond_db"
    assert args.expected_server_uuid == EXPECTED_SERVER_UUID


@pytest.mark.parametrize("version", (17, 18, 19, 21))
def test_cli_recovery_rejects_missing_expected_identity(version: int) -> None:
    with pytest.raises(SystemExit, match="2"):
        apply_migrations._parse_args(
            [
                f"--recover-applying-{version:03d}",
                "--apply",
                "--state-digest",
                "e" * 64,
            ]
        )


def test_cli_identity_mismatch_does_not_call_recovery_handler() -> None:
    handler = Mock()
    mode = apply_migrations._MigrationMode(
        option="--recover-applying-017",
        dest="recover_applying_017",
        action="recover",
        help="",
        handler_name="test_recovery_handler",
    )
    engine = Mock()
    engine.connect.return_value = nullcontext(
        SimpleNamespace(
            execute=lambda _statement: SimpleNamespace(
                one=lambda: ("wrong_db", EXPECTED_SERVER_UUID)
            )
        )
    )
    with (
        patch.object(apply_migrations, "_MIGRATION_MODES", (mode,)),
        patch.dict(
            apply_migrations.__dict__,
            {"test_recovery_handler": handler},
        ),
        patch.object(
            apply_migrations,
            "validate_release_migration_manifest",
        ),
        patch.object(
            apply_migrations,
            "create_engine_from_env",
            return_value=engine,
        ),
        pytest.raises(RuntimeError, match="database identity mismatch"),
    ):
        apply_migrations.main(
            [
                "--recover-applying-017",
                "--apply",
                "--state-digest",
                "f" * 64,
                "--expected-database-name",
                "bond_db",
                "--expected-server-uuid",
                EXPECTED_SERVER_UUID,
            ]
        )

    handler.assert_not_called()
    engine.dispose.assert_called_once_with()


def test_cli_inspection_json_encoding_is_stable(capsys) -> None:
    result = {"z": 1, "classification": "COMPLETE"}
    handler = Mock(return_value=result)
    mode = apply_migrations._MigrationMode(
        option="--inspect-applying-017",
        dest="inspect_applying_017",
        action="inspect",
        help="",
        handler_name="test_inspection_handler",
    )
    engine = Mock()
    with (
        patch.object(apply_migrations, "_MIGRATION_MODES", (mode,)),
        patch.dict(
            apply_migrations.__dict__,
            {"test_inspection_handler": handler},
        ),
        patch.object(
            apply_migrations,
            "validate_release_migration_manifest",
        ),
        patch.object(
            apply_migrations,
            "create_engine_from_env",
            return_value=engine,
        ),
    ):
        apply_migrations.main(["--inspect-applying-017"])

    assert capsys.readouterr().out == json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    handler.assert_called_once()
    engine.dispose.assert_called_once_with()
