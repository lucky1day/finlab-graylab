"""基线加增量迁移的公共合同：新库、已有库、历史漂移与可重跑恢复。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from migrations._contracts import MigrationHistoryError
from migrations._sql import split_sql_statements
from migrations.runner import (
    MIGRATIONS_DIR,
    apply_pending_migration_files,
    inspect_applying_migration,
    recover_applying_migration,
    validate_release_migration_manifest,
)


pytestmark = pytest.mark.mysql_integration

PATHS = sorted(MIGRATIONS_DIR.glob("*.sql"))
PLAN = validate_release_migration_manifest(PATHS)
LATEST = PLAN.baseline.through_version + len(PLAN.migrations)
RETIRED_TABLES = {
    "t_input_generations",
    "t_schedule_occurrences",
    "t_schedule_items",
    "t_schedule_item_targets",
    "t_scheduler_heartbeat",
    "t_harness_runs",
    "t_harness_gate_results",
    "t_backtest_reproduction_checks",
}


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(name).lower()
            for name in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            ).scalars()
        }


def _history(engine: Engine) -> list[tuple[int, str, str, int]]:
    with engine.connect() as connection:
        return [
            (int(row[0]), str(row[1]), str(row[2]), int(row[3]))
            for row in connection.execute(
                text(
                    "SELECT version, filename, state, baseline_bootstrap "
                    "FROM t_schema_migrations ORDER BY version"
                )
            )
        ]


def _simulate_database_at_baseline(engine: Engine) -> None:
    """模拟两端现状：025 结构加逐行等于旧文件身份的历史。"""
    with engine.begin() as connection:
        for statement in PLAN.baseline.statements:
            connection.execute(text(statement))
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
        for entry in PLAN.baseline.history:
            connection.execute(
                text(
                    "INSERT INTO t_schema_migrations "
                    "(version, filename, sha256, state, baseline_bootstrap, applied_at) "
                    "VALUES (:v, :f, :s, 'APPLIED', :b, UTC_TIMESTAMP(6))"
                ),
                {
                    "v": entry.version,
                    "f": entry.filename,
                    "s": entry.sha256,
                    "b": 1 if entry.version <= 16 else 0,
                },
            )


def _assert_result_segment(engine: Engine) -> None:
    """生成列只按特征基准日给出 backtest/live，且由数据库计算（VIRTUAL）。"""
    with engine.connect() as connection:
        column_type, extra, expression = connection.execute(
            text(
                "SELECT column_type, extra, generation_expression "
                "FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = 't_scheme_predictions' AND column_name = 'result_segment'"
            )
        ).one()
    assert column_type == "enum('backtest','live')"
    assert "VIRTUAL GENERATED" in extra.upper()
    assert "feature_date" in expression and "2026-06-01" in expression


def _column_type(engine: Engine, table: str, column: str) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT column_type FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()


def _assert_concept_surface(engine: Engine) -> None:
    """028：补跑阶段改名，常量列删除，版本状态不含从未使用的取值。"""
    assert _column_type(engine, "t_scheme_runs", "prediction_phase") == "enum('gap_fill','scheduled_live')"
    assert _column_type(engine, "t_scheme_runs", "run_type") is None
    assert _column_type(engine, "t_backtest_runs", "run_mode") is None
    assert _column_type(engine, "t_scheme_versions", "status") == "enum('draft','active','paused','retired')"


def test_empty_database_gets_baseline_then_incremental(empty_mysql_engine) -> None:
    apply_pending_migration_files(empty_mysql_engine, PATHS)

    history = _history(empty_mysql_engine)
    assert [row[0] for row in history] == list(range(1, LATEST + 1))
    assert all(row[2] == "APPLIED" for row in history)
    assert {row[3] for row in history[:25]} == {1}
    assert {row[3] for row in history[25:]} == {0}
    assert not RETIRED_TABLES & _tables(empty_mysql_engine)
    _assert_result_segment(empty_mysql_engine)
    _assert_concept_surface(empty_mysql_engine)

    apply_pending_migration_files(empty_mysql_engine, PATHS)
    assert _history(empty_mysql_engine) == history


def test_existing_database_at_025_only_runs_new_migrations(empty_mysql_engine) -> None:
    _simulate_database_at_baseline(empty_mysql_engine)
    assert RETIRED_TABLES <= _tables(empty_mysql_engine)

    apply_pending_migration_files(empty_mysql_engine, PATHS)

    history = _history(empty_mysql_engine)
    assert [row[0] for row in history] == list(range(1, LATEST + 1))
    assert [row[1] for row in history[25:]] == [m.path.name for m in PLAN.migrations]
    assert not RETIRED_TABLES & _tables(empty_mysql_engine)
    _assert_result_segment(empty_mysql_engine)
    _assert_concept_surface(empty_mysql_engine)


def test_history_identity_drift_is_refused(empty_mysql_engine) -> None:
    _simulate_database_at_baseline(empty_mysql_engine)
    with empty_mysql_engine.begin() as connection:
        connection.execute(
            text("UPDATE t_schema_migrations SET sha256 = REPEAT('0', 64) WHERE version = 17")
        )

    with pytest.raises(MigrationHistoryError, match="identity drift for 017"):
        apply_pending_migration_files(empty_mysql_engine, PATHS)
    assert RETIRED_TABLES <= _tables(empty_mysql_engine)


def test_history_before_baseline_is_refused(empty_mysql_engine) -> None:
    _simulate_database_at_baseline(empty_mysql_engine)
    with empty_mysql_engine.begin() as connection:
        connection.execute(text("DELETE FROM t_schema_migrations WHERE version > 16"))

    with pytest.raises(MigrationHistoryError, match="stops before the schema baseline"):
        apply_pending_migration_files(empty_mysql_engine, PATHS)


def test_platform_tables_without_history_are_refused(empty_mysql_engine) -> None:
    with empty_mysql_engine.begin() as connection:
        connection.execute(text("CREATE TABLE t_scheme_registry (id INT PRIMARY KEY)"))

    with pytest.raises(MigrationHistoryError, match="no migration history"):
        apply_pending_migration_files(empty_mysql_engine, PATHS)


def test_interrupted_rerunnable_migration_recovers_with_digest_fence(
    empty_mysql_engine,
) -> None:
    _simulate_database_at_baseline(empty_mysql_engine)
    migration = PLAN.migrations[0]
    assert migration.rerunnable
    statements = split_sql_statements(migration.path.read_text())
    half = statements[: len(statements) // 2]
    with empty_mysql_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_schema_migrations (version, filename, sha256, state) "
                "VALUES (:v, :f, :s, 'APPLYING')"
            ),
            {"v": migration.version, "f": migration.path.name, "s": migration.sha256},
        )
        for statement in half:
            connection.execute(text(statement))

    with pytest.raises(MigrationHistoryError, match="APPLYING"):
        apply_pending_migration_files(empty_mysql_engine, PATHS)

    state = inspect_applying_migration(empty_mysql_engine, PATHS)
    assert state["rerunnable"] is True
    with pytest.raises(MigrationHistoryError, match="changed since inspection"):
        recover_applying_migration(empty_mysql_engine, PATHS, expected_state_digest="0" * 64)

    recover_applying_migration(
        empty_mysql_engine, PATHS, expected_state_digest=state["state_digest"]
    )
    assert _history(empty_mysql_engine)[-1][2] == "APPLIED"
    assert not RETIRED_TABLES & _tables(empty_mysql_engine)


def test_028_rewrites_gray_live_runs_and_is_rerunnable(empty_mysql_engine) -> None:
    apply_pending_migration_files(empty_mysql_engine, PATHS)
    migration = next(m for m in PLAN.migrations if m.path.name.startswith("028_"))
    statements = split_sql_statements(migration.path.read_text())
    with empty_mysql_engine.begin() as connection:  # 还原 027 时的形态并放入旧取值
        connection.execute(text("ALTER TABLE t_scheme_runs MODIFY COLUMN prediction_phase ENUM('gray_live','scheduled_live') DEFAULT NULL"))
        connection.execute(text("ALTER TABLE t_scheme_runs ADD COLUMN run_type ENUM('dry_run','shadow','active','manual') NOT NULL DEFAULT 'active' AFTER runtime_type"))
        connection.execute(text("ALTER TABLE t_backtest_runs ADD COLUMN run_mode ENUM('no_persist','persist','reproduction','comparison') DEFAULT NULL AFTER input_artifact_hash"))
        connection.execute(text("ALTER TABLE t_scheme_versions MODIFY COLUMN status ENUM('draft','validated','shadow','active','paused','retired') NOT NULL DEFAULT 'draft'"))
        connection.execute(text(
            "INSERT INTO t_scheme_runs (scheme_id, predict_date, prediction_phase) VALUES "
            "('s', '2026-07-01', 'gray_live'), ('s', '2026-07-02', 'scheduled_live'), ('s', '2026-07-03', NULL)"
        ))
    for _ in range(2):  # 第二遍证明可重跑
        with empty_mysql_engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
        _assert_concept_surface(empty_mysql_engine)
    with empty_mysql_engine.connect() as connection:
        phases = connection.execute(text("SELECT predict_date, prediction_phase FROM t_scheme_runs ORDER BY predict_date")).all()
    assert [row[1] for row in phases] == ["gap_fill", "scheduled_live", None]
