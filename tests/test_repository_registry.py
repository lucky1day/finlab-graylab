from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from scheduler import repository
from shared.models import PredictionRecord


@pytest.fixture(params=("native_adapter", "blackbox_v2"))
def case(request, tmp_path, monkeypatch):
    """通过真实 SQLite 事务验证两个公开完成入口，不模拟 SQL 执行。"""
    runtime_type = request.param
    cfg = SimpleNamespace(
        scheme_id="demo", scheme_version="version-1", runtime_type=runtime_type,
        status="active", version_status="active", horizon=1, task_type="T+1",
        tenors=["5Y", "10Y"], path=tmp_path / "schemes" / "demo",
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )
    monkeypatch.setitem(
        sqlite3.converters,
        "BFL_TEST_TIMESTAMP",
        lambda raw: datetime.fromisoformat(raw.decode()),
    )
    schema = (
        """CREATE TABLE t_scheme_versions (
            scheme_id TEXT, scheme_version TEXT, runtime_type TEXT,
            algorithm_version TEXT, contract_version TEXT, runtime_profile TEXT,
            environment_fingerprint TEXT, data_snapshot_id TEXT, code_hash TEXT,
            config_hash TEXT, manifest_hash TEXT, git_commit TEXT, status TEXT,
            created_by TEXT, approved_by TEXT, approved_at BFL_TEST_TIMESTAMP,
            PRIMARY KEY (scheme_id, scheme_version))""",
        """CREATE TABLE t_scheme_registry (
            scheme_id TEXT PRIMARY KEY, base_scheme_id TEXT, name TEXT,
            description TEXT, horizon INTEGER, task_type TEXT, runtime_type TEXT,
            tenors TEXT, frequency TEXT, target_tenor TEXT, schedule_cron TEXT,
            schedule_timezone TEXT, status TEXT, deployed_at TEXT)""",
        """CREATE TABLE t_scheme_runs (
            run_id INTEGER PRIMARY KEY, scheme_id TEXT, scheme_version TEXT,
            runtime_type TEXT, run_type TEXT, prediction_phase TEXT,
            predict_date TEXT, status TEXT, records_expected INTEGER,
            records_returned INTEGER, records_written INTEGER, data_snapshot_id TEXT,
            started_at TEXT, finished_at TEXT, error_message TEXT)""",
        """CREATE TABLE t_scheme_predictions (
            id INTEGER PRIMARY KEY, run_id INTEGER, backtest_run_id INTEGER,
            scheme_version TEXT, scheme_id TEXT, target_tenor TEXT, horizon INTEGER,
            predict_date TEXT, feature_date TEXT, target_date TEXT,
            predicted_direction INTEGER, backtest_actual_direction INTEGER,
            model_version TEXT, extra TEXT,
            UNIQUE (scheme_id, target_tenor, horizon, target_date))""",
        """CREATE TABLE t_scheme_run_log (
            run_id INTEGER, scheme_id TEXT, run_date TEXT, status TEXT,
            duration_sec REAL, error_msg TEXT)""",
    )
    with engine.begin() as conn:
        for statement in schema:
            conn.execute(text(statement))
        conn.execute(text("""INSERT INTO t_scheme_versions
            (scheme_id, scheme_version, runtime_type, status, approved_by, approved_at)
            VALUES ('demo', 'version-1', :runtime, 'active', 'reviewer', :approved)"""),
            {"runtime": runtime_type, "approved": "2026-07-20 08:30:00"})
        conn.execute(text("""INSERT INTO t_scheme_registry
            (scheme_id, base_scheme_id, horizon, task_type, runtime_type,
             target_tenor, status) VALUES (:id, 'demo', 1, 'T+1', :runtime, :tenor, 'active')"""),
            [{"id": f"demo__h1__{tenor}", "runtime": runtime_type, "tenor": tenor}
             for tenor in cfg.tenors])
        conn.execute(text("""INSERT INTO t_scheme_runs
            (run_id, scheme_id, scheme_version, runtime_type, run_type,
             prediction_phase, predict_date, status, records_expected)
            VALUES (101, 'demo', 'version-1', :runtime, 'active',
                    'scheduled_live', '2026-07-20', 'running', 2)"""),
            {"runtime": runtime_type})
    monkeypatch.setattr(repository, "load_scheme_config", lambda _path: cfg)
    complete = (
        repository.complete_active_native_run
        if runtime_type == "native_adapter"
        else repository.complete_approved_blackbox_run
    )
    records = [
        PredictionRecord(
            scheme_id="demo", target_tenor=tenor, horizon=1,
            predict_date="2026-07-20", feature_date="2026-07-17",
            target_date="2026-07-21", prediction_phase="scheduled_live",
            predicted_direction=1,
        )
        for tenor in cfg.tenors
    ]

    def finish():
        return complete(
            engine, cfg, run_id=101, records=records, scheme_version="version-1",
            records_returned=2, run_date="2026-07-20", duration_sec=1.0,
        )

    try:
        yield engine, cfg, records, finish
    finally:
        engine.dispose()


def _facts(engine):
    with engine.connect() as conn:
        return tuple(
            [tuple(row) for row in conn.execute(text(f"SELECT * FROM {table}"))]
            for table in ("t_scheme_predictions", "t_scheme_runs", "t_scheme_run_log")
        )


def test_native_without_backfilled_registry_owner_fails_closed():
    conn = Mock()
    conn.dialect.name = "sqlite"
    conn.execute.return_value.mappings.return_value.all.return_value = []
    cfg = SimpleNamespace(scheme_id="demo", horizon=1, tenors=["5Y"])
    with pytest.raises(ValueError, match="Registry owner is required"):
        repository._sync_scheme_registry_conn(
            conn, [cfg], effective_statuses={"demo__h1__5Y": "active"},
        )


def test_completion_commits_predictions_run_and_log(case):
    engine, _cfg, _records, finish = case
    assert finish() == ("success", 2, None)
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT run_id, scheme_version, target_tenor,
            feature_date, backtest_run_id, backtest_actual_direction
            FROM t_scheme_predictions ORDER BY target_tenor""")).all() == [
                (101, "version-1", "10Y", "2026-07-17", None, None),
                (101, "version-1", "5Y", "2026-07-17", None, None),
            ]
        assert conn.execute(
            text("SELECT status, records_written FROM t_scheme_runs")
        ).one() == ("success", 2)
        assert conn.execute(
            text("SELECT run_id, status FROM t_scheme_run_log")
        ).one() == (101, "success")


@pytest.mark.parametrize("existing_tenors", [("5Y", "10Y"), ("5Y",)])
def test_existing_business_keys_never_change_or_partially_append(case, existing_tenors):
    engine, _cfg, _records, finish = case
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO t_scheme_predictions
            (run_id, scheme_version, scheme_id, target_tenor, horizon,
             predict_date, feature_date, target_date, predicted_direction, extra)
            VALUES (77, 'historical-version', 'demo', :tenor, 1,
                    '2026-07-19', '2026-07-16', '2026-07-21', -1, 'original')"""),
            [{"tenor": tenor} for tenor in existing_tenors])
    original = _facts(engine)[0]
    status, written, error = finish()
    expected_status = "skipped" if len(existing_tenors) == 2 else "failed"
    assert (status, written) == (expected_status, 0)
    assert error.startswith(
        repository.PREDICTION_KEYS_ALREADY_EXIST if expected_status == "skipped"
        else repository.PARTIAL_PREDICTION_KEY_CONFLICT
    )
    assert _facts(engine)[0] == original
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT status, records_written FROM t_scheme_runs")
        ).one() == (expected_status, 0)
        assert conn.execute(
            text("SELECT status, error_msg FROM t_scheme_run_log")
        ).one() == (expected_status, error)


@pytest.mark.parametrize("trigger", [
    "BEFORE INSERT ON t_scheme_predictions WHEN NEW.target_tenor = '10Y' "
    "BEGIN SELECT RAISE(ABORT, 'injected prediction'); END",
    "BEFORE UPDATE ON t_scheme_runs BEGIN SELECT RAISE(ABORT, 'injected run'); END",
    "BEFORE UPDATE ON t_scheme_runs BEGIN SELECT RAISE(IGNORE); END",
    "BEFORE INSERT ON t_scheme_run_log BEGIN SELECT RAISE(ABORT, 'injected log'); END",
], ids=["second_prediction", "run", "run_missing", "log"])
def test_each_write_stage_failure_rolls_back_all_facts(case, trigger):
    engine, _cfg, _records, finish = case
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TRIGGER fail_stage {trigger}"))
    before = _facts(engine)
    with pytest.raises((IntegrityError, RuntimeError), match="injected|affected 0"):
        finish()
    assert _facts(engine) == before


@pytest.mark.parametrize("mutation", [
    "DELETE FROM t_scheme_versions",
    "UPDATE t_scheme_versions SET scheme_id = 'different'",
    "UPDATE t_scheme_versions SET scheme_version = 'different'",
    "UPDATE t_scheme_versions SET runtime_type = 'different'",
    "UPDATE t_scheme_versions SET status = 'retired'",
    "DELETE FROM t_scheme_registry WHERE target_tenor = '5Y'",
    "UPDATE t_scheme_registry SET scheme_id = 'different' WHERE target_tenor = '5Y'",
    "UPDATE t_scheme_registry SET base_scheme_id = 'different'",
    "UPDATE t_scheme_registry SET target_tenor = 'different'",
    "UPDATE t_scheme_registry SET horizon = 5",
    "UPDATE t_scheme_registry SET task_type = 'T+5'",
    "UPDATE t_scheme_registry SET runtime_type = 'different'",
    "UPDATE t_scheme_registry SET status = 'paused'",
    "UPDATE t_scheme_runs SET scheme_version = 'different'",
])
def test_final_write_revalidates_exact_registry_and_run_identity(case, mutation):
    engine, _cfg, _records, finish = case
    with engine.begin() as conn:
        conn.execute(text(mutation))
    before = _facts(engine)
    with pytest.raises(RuntimeError):
        finish()
    assert _facts(engine) == before


def test_duplicate_record_targets_cannot_publish(case):
    engine, _cfg, records, finish = case
    records[:] = [records[0], records[0]]
    before = _facts(engine)
    with pytest.raises(RuntimeError, match="target set does not match active Registry"):
        finish()
    assert _facts(engine) == before


def test_runtime_dispatch_rejects_wrong_config_runtime(case):
    engine, cfg, _records, finish = case
    cfg.runtime_type = "unsupported"
    before = _facts(engine)
    with pytest.raises(ValueError):
        finish()
    assert _facts(engine) == before


def test_ordinary_completion_rejects_gray_live(case):
    engine, _cfg, records, finish = case
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_runs SET prediction_phase = 'gray_live'"))
    records[:] = [replace(record, prediction_phase="gray_live") for record in records]
    before = _facts(engine)
    with pytest.raises(RuntimeError, match="ordinary completion requires scheduled_live"):
        finish()
    assert _facts(engine) == before


@pytest.mark.parametrize("case", ["blackbox_v2"], indirect=True)
@pytest.mark.parametrize("field", ["approved_by", "approved_at"])
def test_blackbox_requires_approval_evidence(case, field):
    engine, cfg, _records, finish = case
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE t_scheme_versions SET {field} = NULL"))
    assert not repository.read_blackbox_execution_approval(engine, cfg).executable
    before = _facts(engine)
    with pytest.raises(RuntimeError, match="not production-approved"):
        finish()
    assert _facts(engine) == before


@pytest.mark.parametrize("case", ["blackbox_v2"], indirect=True)
@pytest.mark.parametrize("database_status,expected", [("active", "active"), ("retired", "paused")])
def test_blackbox_lifecycle_comes_from_database(case, monkeypatch, database_status, expected):
    engine, cfg, _records, _finish = case
    cfg.status = "paused" if database_status == "active" else "active"
    cfg.version_status = "draft"
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE t_scheme_versions SET status = :status"),
            {"status": database_status},
        )
    monkeypatch.setattr(
        repository, "replace",
        lambda value, **changes: SimpleNamespace(**{**vars(value), **changes}),
    )
    resolved = repository.resolve_database_lifecycle(engine, [cfg])[0]
    assert (resolved.status, resolved.version_status) == (expected, database_status)


@pytest.mark.parametrize("case", ["blackbox_v2"], indirect=True)
def test_blackbox_disk_version_drift_cannot_publish(case, monkeypatch):
    engine, cfg, _records, finish = case
    monkeypatch.setattr(
        repository, "load_scheme_config",
        lambda _path: SimpleNamespace(**{**vars(cfg), "scheme_version": "changed"}),
    )
    before = _facts(engine)
    with pytest.raises(RuntimeError, match="canonical config changed before final write"):
        finish()
    assert _facts(engine) == before
