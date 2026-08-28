from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import pytest

from migrations.runner import (
    MIGRATIONS_DIR,
    MigrationPreflightError,
    _registry_owner_authority,
    _show_create_mentions_serving_pointer,
    _validate_registry_owner_schema,
    build_applying_021_inspection,
    validate_release_migration_manifest,
    validate_mysql_session_contract,
)


def _safe_facts(lower_case_table_names: object) -> dict[str, object]:
    return {
        "server_version": "8.0.43",
        "version_comment": "MySQL Community Server - GPL",
        "sql_mode": "STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE",
        "session_time_zone": "+00:00",
        "foreign_key_checks": 1,
        "lower_case_table_names": lower_case_table_names,
    }


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_general_migration_accepts_supported_identifier_modes(
    mode: int,
) -> None:
    validate_mysql_session_contract(_safe_facts(mode))


def test_migration_017_keeps_reviewed_constraint_namespace_gate() -> None:
    with pytest.raises(
        MigrationPreflightError,
        match="migration 017 requires lower_case_table_names=2",
    ):
        validate_mysql_session_contract(
            _safe_facts(0),
            require_reviewed_constraint_namespace=True,
        )

    validate_mysql_session_contract(
        _safe_facts(2),
        require_reviewed_constraint_namespace=True,
    )


def test_general_migration_rejects_unknown_identifier_mode() -> None:
    with pytest.raises(
        MigrationPreflightError,
        match="unsupported lower_case_table_names",
    ):
        validate_mysql_session_contract(_safe_facts("unknown"))


class _ShowCreateResult:
    def __init__(self, definition: str) -> None:
        self._definition = definition

    def mappings(self) -> _ShowCreateResult:
        return self

    def one(self) -> dict[str, str]:
        return {"Create Procedure": self._definition}


class _ShowCreateConnection:
    def __init__(self, definition: str) -> None:
        self._definition = definition
        self.statement = ""

    def execute(self, statement: object) -> _ShowCreateResult:
        self.statement = str(statement)
        return _ShowCreateResult(self._definition)


def test_hidden_routine_definition_uses_show_create() -> None:
    connection = _ShowCreateConnection(
        "CREATE PROCEDURE `p` () SELECT 1"
    )
    assert not _show_create_mentions_serving_pointer(
        connection,
        object_type="PROCEDURE",
        object_schema="bond_db",
        object_name="p",
    )
    assert connection.statement == "SHOW CREATE PROCEDURE `bond_db`.`p`"


def test_hidden_definition_detects_actual_pointer_dependency() -> None:
    connection = _ShowCreateConnection(
        "CREATE VIEW `v` AS SELECT * FROM t_scheme_serving_pointer"
    )
    assert _show_create_mentions_serving_pointer(
        connection,
        object_type="VIEW",
        object_schema="bond_db",
        object_name="v",
    )


def test_registry_owner_migration_matches_all_current_composite_ids() -> None:
    from scheduler.discovery import load_scheme_config
    from scheduler.repository import registry_scheme_id

    sql = (MIGRATIONS_DIR / "021_registry_owner.sql").read_text(
        encoding="utf-8"
    )
    values_sql = sql.split("INSERT INTO `_bfl_registry_owner_021`", 1)[1]
    values_sql = values_sql.split(";", 1)[0]
    rows = re.findall(r"\('([^']+)', '([^']+)'\)", values_sql)
    owners = dict(rows)
    assert len(rows) == len(owners) == 96
    reviewed_authority_digest = hashlib.sha256(
        json.dumps(
            rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert reviewed_authority_digest == (
        "a60ed52cd321a3a3495c80ff35128c9295da88f77a3afbfb15046adb9806ada4"
    )
    assert Counter(owners.values()) == {
        "rl": 28,
        "wg": 6,
        "liwei": 2,
        "lw": 39,
        "fengrl": 20,
        "CWG": 1,
    }

    current_ids = set()
    for config_path in sorted(Path("schemes").glob("*/config.yaml")):
        cfg = load_scheme_config(config_path)
        current_ids.update(
            registry_scheme_id(cfg.scheme_id, cfg.horizon, tenor)
            for tenor in cfg.tenors
        )
    assert set(owners) == current_ids
    assert owners["weekly_5y_curve_logit_v1__h1__5Y"] == "lw"
    assert owners["weekly_10y_hetero_vote_v1__h1__10Y"] == "lw"
    assert owners["ten_y_t5_curve_spread_adapt90_v1__h5__10Y"] == "lw"
    assert owners["ten_y_t5_short_amp_adapt756_v1__h5__10Y"] == "lw"


def test_registry_owner_schema_accepts_only_missing_source_or_exact_target() -> None:
    _validate_registry_owner_schema({"exists": False}, allow_missing=True)
    _validate_registry_owner_schema(
        {
            "exists": True,
            "column": ("varchar(64)", "yes", None, ""),
            "invalid_count": 0,
        },
        allow_missing=True,
    )
    target = {
        "exists": True,
        "column": ("varchar(64)", "no", None, ""),
        "invalid_count": 0,
    }
    _validate_registry_owner_schema(target, allow_missing=False)

    with pytest.raises(MigrationPreflightError, match="missing"):
        _validate_registry_owner_schema(
            {"exists": False},
            allow_missing=False,
        )
    with pytest.raises(MigrationPreflightError, match="unexpected"):
        _validate_registry_owner_schema(
            {**target, "invalid_count": 1},
            allow_missing=False,
        )


def test_release_manifest_includes_registry_owner_checksum() -> None:
    manifest = validate_release_migration_manifest(
        sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    )
    assert manifest[-1].version == 21
    assert manifest[-1].path.name == "021_registry_owner.sql"


def test_registry_owner_sql_is_reentrant_and_checks_both_identity_directions() -> None:
    sql = (MIGRATIONS_DIR / "021_registry_owner.sql").read_text(
        encoding="utf-8"
    )
    assert "FROM information_schema.columns" in sql
    assert "PREPARE bfl_registry_owner_add_021" in sql
    assert (
        "FROM `t_scheme_registry` AS registry\n"
        "LEFT JOIN `_bfl_registry_owner_021` AS authority"
    ) in sql
    assert (
        "FROM `_bfl_registry_owner_021` AS authority\n"
        "LEFT JOIN `t_scheme_registry` AS registry"
    ) in sql
    assert sql.index("FROM `t_scheme_registry` AS registry") < sql.index(
        "SET @bfl_registry_owner_add_sql_021"
    )
    assert sql.index("FROM `_bfl_registry_owner_021` AS authority") < sql.index(
        "SET @bfl_registry_owner_add_sql_021"
    )
    assert "BINARY registry.`owner` <> BINARY authority.`owner`" in sql


def _applying_021_fixture():
    manifest = validate_release_migration_manifest(
        sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    )
    target = manifest[-1]
    authority = _registry_owner_authority(target)
    history = [
        {
            "version": migration.version,
            "filename": migration.path.name,
            "sha256": migration.sha256,
            "state": "APPLYING" if migration.version == 21 else "APPLIED",
            "baseline_bootstrap": 0,
        }
        for migration in manifest
    ]
    identity = {
        "database_name": "isolated_test",
        "server_uuid": "00000000-0000-0000-0000-000000000001",
    }
    return manifest, history, identity, authority


def test_applying_021_inspection_classifies_partial_complete_and_conflict() -> None:
    manifest, history, identity, authority = _applying_021_fixture()
    rows = [
        {"scheme_id": scheme_id, "owner": None}
        for scheme_id in sorted(authority)
    ]
    partial = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        registry_state={"schema": {"exists": False}, "rows": rows},
    )
    assert partial["classification"] == "COMPATIBLE_PARTIAL"

    complete_rows = [
        {"scheme_id": scheme_id, "owner": authority[scheme_id]}
        for scheme_id in sorted(authority)
    ]
    complete = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        registry_state={
            "schema": {
                "exists": True,
                "column": ("varchar(64)", "no", None, ""),
                "invalid_count": 0,
            },
            "rows": complete_rows,
        },
    )
    assert complete["classification"] == "COMPLETE"
    assert complete["state_digest"] != partial["state_digest"]

    complete_rows[0]["owner"] = "conflict"
    unsafe = build_applying_021_inspection(
        manifest=manifest,
        history=history,
        database_identity=identity,
        registry_state={
            "schema": {
                "exists": True,
                "column": ("varchar(64)", "yes", None, ""),
                "invalid_count": 0,
            },
            "rows": complete_rows,
        },
    )
    assert unsafe["classification"] == "UNSAFE"
    assert "conflicts" in str(unsafe["reason"])


def test_migration_cli_requires_fenced_021_recovery() -> None:
    from scripts.apply_migrations import _parse_args

    inspect = _parse_args(["--inspect-applying-021"])
    assert inspect.inspect_applying_021 is True
    recover = _parse_args(
        [
            "--recover-applying-021",
            "--apply",
            "--state-digest",
            "0" * 64,
            "--expected-database-name",
            "isolated_test",
            "--expected-server-uuid",
            "00000000-0000-0000-0000-000000000001",
        ]
    )
    assert recover.recover_applying_021 is True
    with pytest.raises(SystemExit):
        _parse_args(["--recover-applying-021", "--apply"])
