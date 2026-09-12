"""仅 loopback 随机独立 MySQL schema 验收 W2 历史结果物化。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from scheduler import repository as repo
from test_same_id_runtime_upgrade_mysql import mysql_migration, MYSQL_URL
from test_same_id_live_preservation import (
    preservation_fixture, preservation_plan, preservation_apply, assert_preservation_result,
    backup_for_preservation, seed_preserved_historical_compare, preserved_compare_snapshot,
)


pytestmark = pytest.mark.skipif(not MYSQL_URL, reason="isolated loopback MySQL URL is required")


@pytest.fixture
def mysql_preservation(mysql_migration):
    return preservation_fixture(mysql_migration)


def test_mysql_preserves_source_rows_and_creates_four_manual_imports(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    result = preservation_apply(engine, kwargs, control, before)
    assert_preservation_result(engine, kwargs, before, result)
    with pytest.raises(RuntimeError, match="existing target"):
        preservation_apply(engine, kwargs, control, before)


def test_mysql_fourth_prediction_constraint_failure_rolls_back_four_new_runs(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE t_scheme_predictions ADD CONSTRAINT stop_fourth_import CHECK "
                            "(scheme_id <> 'daily_7y_1_v28' OR target_date <> '2026-09-17')")
    with pytest.raises(OperationalError):
        preservation_apply(engine, kwargs, control, before)
    assert preservation_plan(engine, kwargs, control) == before
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM t_scheme_runs WHERE run_type='manual'").scalar_one() == 0


@pytest.mark.parametrize("alias", [False, True])
def test_mysql_preservation_contends_with_normal_lifecycle_lock(mysql_preservation, monkeypatch, alias):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    key = "daily_5y_2_v28" + ("_bbv2" if alias else "")
    monkeypatch.setattr(repo, "_BLACKBOX_LIFECYCLE_LOCK_TIMEOUT_SEC", 0.25)
    with repo._blackbox_activation_advisory_lock(engine, scheme_id=key):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(preservation_apply, engine, kwargs, control, before)
            with pytest.raises(repo.BlackboxActivationLockTimeout):
                future.result(timeout=10)
    assert preservation_plan(engine, kwargs, control) == before


def test_mysql_preservation_serializes_concurrent_imports(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(preservation_apply, engine, kwargs, control, before) for _ in range(2)]
        passed, refused = [], []
        for future in futures:
            try:
                passed.append(future.result(timeout=10))
            except RuntimeError as error:
                refused.append(str(error))
    assert len(passed) == len(refused) == 1
    assert "existing target" in refused[0]
    assert_preservation_result(engine, kwargs, before, passed[0])


def test_mysql_preservation_rejects_database_identity_and_precommit_control_drift(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    with pytest.raises(RuntimeError, match="database identity"):
        preservation_plan(engine, kwargs | {"expected_database_name": "not_the_isolated_schema"}, control)
    controls = iter([control, control | {"changed": True}])
    with pytest.raises(RuntimeError, match="control plane changed"):
        preservation_apply(engine, kwargs, control, before, reader=lambda: next(controls))
    assert preservation_plan(engine, kwargs, control) == before


def test_mysql_preservation_rejects_partial_existing_target_without_manual_runs(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    row = dict(before["sources"][0]["prediction"])
    row["scheme_id"] = row["scheme_id"].removesuffix("_bbv2")
    with engine.begin() as conn:
        repo._insert_run_predictions_conn(conn, [row])
    with pytest.raises(RuntimeError, match="existing target"):
        preservation_apply(engine, kwargs, control, before)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_runs WHERE run_type='manual'")).scalar_one() == 0


def test_mysql_preservation_leaves_preserved_historical_harness_rows_unchanged(mysql_preservation, monkeypatch):
    engine, kwargs, control = mysql_preservation
    audit = seed_preserved_historical_compare(engine, monkeypatch)
    kwargs["backup_evidence"] = backup_for_preservation(engine, kwargs)
    before = preservation_plan(engine, kwargs, control)
    result = preservation_apply(engine, kwargs, control, before)
    assert_preservation_result(engine, kwargs, before, result)
    assert preserved_compare_snapshot(engine) == audit


def test_mysql_preservation_rejects_backup_from_another_database(mysql_preservation):
    engine, kwargs, control = mysql_preservation
    before = preservation_plan(engine, kwargs, control)
    wrong_backup = kwargs["backup_evidence"] | {"source_database_identity_sha256": repo.native_successor_plan_sha256({
        "database_name": "another_isolated_schema", "server_uuid": kwargs["expected_server_uuid"],
    })}
    with pytest.raises(ValueError, match="backup/restore evidence"):
        preservation_apply(engine, kwargs | {"backup_evidence": wrong_backup}, control, before)
    assert preservation_plan(engine, kwargs, control) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_runs WHERE run_type='manual'")).scalar_one() == 0
