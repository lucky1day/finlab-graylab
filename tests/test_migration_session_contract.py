from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from migrations.runner import (
    MIGRATIONS_DIR,
    MigrationPartialApplyError,
    MigrationPreflightError,
    _execute_prepared_migration_files,
    _migration_owner_connection,
    _registry_owner_authority,
    _show_create_mentions_serving_pointer,
    _validate_registry_owner_schema,
    build_applying_021_inspection,
    validate_release_migration_manifest,
    validate_mysql_session_contract,
)


class _LockResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _LockConnection:
    def __init__(
        self,
        *,
        dialect_name: str = "mysql",
        acquired: int = 1,
        owned: int | None = 1,
        released: int | None = 1,
        release_error: BaseException | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.acquired = acquired
        self.owned = owned
        self.released = released
        self.release_error = release_error
        self.statements: list[str] = []

    def execute(self, statement: object, parameters=None) -> _LockResult:
        sql = str(statement)
        self.statements.append(sql)
        if "GET_LOCK" in sql:
            return _LockResult(self.acquired)
        if "IS_USED_LOCK" in sql:
            return _LockResult(self.owned)
        if "LOSE_OWNER_FOR_TEST" in sql:
            self.owned = 0
            return _LockResult(1)
        if "RELEASE_LOCK" in sql and self.release_error is not None:
            raise self.release_error
        if "RELEASE_LOCK" in sql:
            return _LockResult(self.released)
        return _LockResult(1)


class _LockEngine:
    def __init__(self, connection: _LockConnection) -> None:
        self.connection = connection

    def connect(self):
        return nullcontext(self.connection)

    def begin(self):
        return nullcontext(self.connection)


def test_migration_owner_lock_acquire_failure_does_not_release() -> None:
    connection = _LockConnection(acquired=0)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="could not acquire migration owner lock",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("lock body must not run")

    assert sum("GET_LOCK" in sql for sql in connection.statements) == 1
    assert all("RELEASE_LOCK" not in sql for sql in connection.statements)


def test_migration_owner_lock_releases_after_success() -> None:
    connection = _LockConnection()
    with patch("migrations.runner.preflight_migration_session"):
        with _migration_owner_connection(_LockEngine(connection)):
            pass

    assert sum("GET_LOCK" in sql for sql in connection.statements) == 1
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


@pytest.mark.parametrize("owned", [0, None])
def test_migration_owner_lock_rejects_lost_ownership(
    owned: int | None,
) -> None:
    connection = _LockConnection(owned=owned)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock ownership check failed",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("lock body must not run after ownership loss")

    assert sum("IS_USED_LOCK" in sql for sql in connection.statements) == 1
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


def test_migration_owner_connection_allows_non_mysql_apply_without_lock() -> None:
    connection = _LockConnection(dialect_name="sqlite")
    with patch("migrations.runner.preflight_migration_session") as preflight:
        with _migration_owner_connection(
            _LockEngine(connection),
            require_mysql=False,
        ) as yielded:
            assert yielded is connection

    preflight.assert_not_called()
    assert connection.statements == []


def test_migration_owner_connection_rejects_non_mysql_by_default() -> None:
    connection = _LockConnection(dialect_name="sqlite")
    with pytest.raises(
        MigrationPreflightError,
        match="inspection/recovery requires MySQL",
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("non-MySQL inspection body must not run")

    assert connection.statements == []


def test_migration_owner_lock_preserves_body_failure() -> None:
    connection = _LockConnection()
    body_error = RuntimeError("migration body failed")
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


def test_migration_owner_lock_reports_release_failure_after_success() -> None:
    release_error = RuntimeError("release failed")
    connection = _LockConnection(release_error=release_error)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="release failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pass

    assert raised.value is release_error


@pytest.mark.parametrize("released", [0, None])
def test_migration_owner_lock_rejects_non_owner_release_result(
    released: int | None,
) -> None:
    connection = _LockConnection(released=released)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock release failed",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pass


def test_migration_owner_lock_preserves_body_when_release_result_fails() -> None:
    body_error = RuntimeError("migration body failed")
    connection = _LockConnection(released=0)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "migration owner lock release failed: MigrationHistoryError: "
        "migration owner lock release failed"
    ]


def test_migration_executor_stops_before_next_statement_after_owner_loss() -> None:
    connection = _LockConnection()
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock ownership check failed",
        ),
    ):
        _execute_prepared_migration_files(
            object(),
            [
                (
                    Path("999_owner_loss_test.sql"),
                    (
                        "SELECT 'LOSE_OWNER_FOR_TEST'",
                        "CREATE TABLE forbidden_after_owner_loss (id INT)",
                    ),
                )
            ],
            owner_connection=connection,
        )

    assert any(
        "LOSE_OWNER_FOR_TEST" in sql for sql in connection.statements
    )
    assert all(
        "forbidden_after_owner_loss" not in sql
        for sql in connection.statements
    )


def test_migration_024_dynamic_ddl_failure_is_reported_as_partial() -> None:
    class FailingConnection(_LockConnection):
        def execute(self, statement: object, parameters=None):
            if "FAIL_AFTER_DYNAMIC_DDL" in str(statement):
                raise RuntimeError("injected dynamic DDL failure")
            return super().execute(statement, parameters)

    connection = FailingConnection()
    with (
        patch("migrations.runner.preflight_migration_session"),
        patch(
            "migrations.runner.read_scheme_prediction_fact_state",
            return_value={},
        ),
        patch(
            "migrations.runner.classify_scheme_prediction_fact_state",
            return_value="COMPATIBLE_PARTIAL",
        ),
        pytest.raises(
            MigrationPartialApplyError,
            match="may be partially applied",
        ),
    ):
        _execute_prepared_migration_files(
            _LockEngine(connection),
            [
                (
                    Path("024_scheme_prediction_fact_source.sql"),
                    ("SELECT 'FAIL_AFTER_DYNAMIC_DDL'",),
                )
            ],
        )


def test_migration_owner_lock_preserves_body_failure_when_release_fails() -> None:
    body_error = RuntimeError("migration body failed")
    connection = _LockConnection(
        release_error=RuntimeError("release failed"),
    )
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "migration owner lock release failed: RuntimeError: release failed"
    ]


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


def test_registry_owner_migration_remains_valid_for_current_schemes() -> None:
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

    current_configs = {}
    for config_path in sorted(Path("schemes").glob("*/config.yaml")):
        cfg = load_scheme_config(config_path)
        for tenor in cfg.tenors:
            current_configs[
                registry_scheme_id(cfg.scheme_id, cfg.horizon, tenor)
            ] = cfg
    assert set(owners) <= set(current_configs)
    for registry_id in set(current_configs) - set(owners):
        cfg = current_configs[registry_id]
        assert cfg.runtime_type == "blackbox_v2"
        assert cfg.owner
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
    registry_owner = next(item for item in manifest if item.version == 21)
    assert registry_owner.path.name == "021_registry_owner.sql"
    assert manifest[-1].version == 24
    assert manifest[-1].path.name == "024_scheme_prediction_fact_source.sql"


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
    release_manifest = validate_release_migration_manifest(
        sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    )
    manifest = tuple(
        migration
        for migration in release_manifest
        if migration.version <= 21
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
