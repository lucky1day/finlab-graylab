"""W1A 八身份事务的隔离验收；不运行真实算法。"""

from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text

from harness import w1a_writer_reclaim as control
from scheduler import repository as repo
from shared.scheme_config_schema import MULTI_TARGET_DELIVERIES
from shared.task_specs import TASK_COMBINATIONS
from test_same_id_runtime_upgrade import migration, _sqlite_upsert
from test_same_id_runtime_upgrade_mysql import mysql_migration, MYSQL_URL


@pytest.fixture
def w1a(migration):
    return _seed_w1a(migration)


def _seed_w1a(migration):
    engine, prior, evidence = migration
    template = next(iter(prior["old_configs"]))
    old, sources, new, conversions = {}, {}, {}, {}

    def insert(conn, table, row):
        row = {key: value for key, value in row.items() if key != "id"}
        result = conn.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)
        return result.lastrowid

    with engine.begin() as conn:
        def one(table, where="1=1", params=None):
            return dict(conn.execute(text(f"SELECT * FROM {table} WHERE {where} LIMIT 1"), params or {}).mappings().one())
        version = one("t_scheme_versions")
        registry = one("t_scheme_registry")
        harness = one("t_harness_runs")
        gate = one("t_harness_gate_results")
        backtest = one("t_backtest_runs")
        prediction = one("t_backtest_predictions")
        fact = one("t_scheme_predictions")
        for base, targets in MULTI_TARGET_DELIVERIES.items():
            old[base], new[base] = deepcopy(prior["old_configs"][template]), deepcopy(prior["new_configs"][template])
            horizon, task = (1, "T+1") if base == "t1_daily" else (5, "T+5")
            for cfg in (old[base], new[base]):
                cfg.scheme_id, cfg.horizon, cfg.task_type, cfg.frequency = base, horizon, task, "daily"
                cfg.tenors = list(targets)
                cfg.schedule.cron = "3 7 * * 1-5"
                cfg.incremental_state = False
            old[base].target_rule = None
            new[base].target_rule = TASK_COMBINATIONS[task][1]
            insert(conn, "t_scheme_versions", version | repo._same_id_reclaim_identity(old[base]) | {"status": "retired"})
            insert(conn, "t_scheme_versions", version | repo._same_id_reclaim_identity(old[base]) | {"scheme_version": "historical-active", "status": "active"})
            insert(conn, "t_scheme_versions", version | repo._same_id_reclaim_identity(old[base]) | {"scheme_version": "historical-paused", "status": "paused"})
            conversions[base] = {}
            for tenor, alias in targets.items():
                source = deepcopy(new[base])
                source.scheme_id, source.scheme_version, source.manifest_hash, source.tenors = alias, "alias-exact", "2" * 64, [tenor]
                sources[alias] = source
                insert(conn, "t_scheme_versions", version | {field: getattr(source, field) for field in (
                    *control.FIELDS, "algorithm_version", "contract_version", "runtime_profile", "environment_fingerprint", "data_snapshot_id")} | {"status": "active"})
                for cfg, status in ((old[base], "archived"), (source, "active")):
                    insert(conn, "t_scheme_registry", registry | {
                        "scheme_id": repo.registry_scheme_id(cfg.scheme_id, horizon, tenor), "base_scheme_id": cfg.scheme_id,
                        "horizon": horizon, "task_type": task, "frequency": "daily", "schedule_cron": cfg.schedule.cron,
                        "schedule_timezone": cfg.schedule.timezone, "tenors": json.dumps([tenor]), "target_tenor": tenor,
                        "runtime_type": cfg.runtime_type, "status": status})
                    insert(conn, "t_scheme_predictions", fact | {"scheme_id": cfg.scheme_id, "target_tenor": tenor, "horizon": horizon})
                summary = json.loads(backtest["summary"])
                summary.update(scheme_version=source.scheme_version, manifest_hash=source.manifest_hash)
                run = insert(conn, "t_backtest_runs", backtest | {"scheme_id": alias, "benchmark_id": "source-" + alias,
                    "code_hash": source.code_hash, "config_hash": source.config_hash, "summary": json.dumps(summary)})
                insert(conn, "t_backtest_predictions", prediction | {"run_id": run, "scheme_id": alias, "horizon": horizon, "target_tenor": tenor})
                conversions[base][alias] = {"source_scheme_id": alias, "scheme_id": base, "target_tenor": tenor,
                                          "source_code_sha256": source.code_hash, "candidate_code_sha256": source.code_hash}
            insert(conn, "t_harness_runs", harness | {"scheme_id": base, "harness_run_id": base})
            summary = json.loads(gate["summary_json"])
            proof = summary["evidence"][0]["value"]
            proof.update(scheme_id=base, old_identity={field: getattr(old[base], field) for field in control.FIELDS if field != "scheme_id"},
                         new_identity={field: getattr(new[base], field) for field in control.FIELDS if field != "scheme_id"})
            insert(conn, "t_harness_gate_results", gate | {"harness_run_id": base, "summary_json": json.dumps(summary)})
    kwargs = prior | {"old_configs": old, "source_configs": sources, "new_configs": new,
                      "harness_run_ids": {key: key for key in old}}
    evidence = evidence | {
        "schema_version": "w1a-writer-reclaim-control-v1", "wave": "W1A", "deployment_target": "aliyun-gray",
        "scheduler": {"control_plane": "systemd_one_shot", "cadence": "daily", "timer_fenced": True, "unique_writer": True},
        "native_canonical_selection": {key: control._identity(cfg) for key, cfg in old.items()},
        "source_canonical_selection": {key: control._identity(cfg) for key, cfg in sources.items()},
        "candidate_canonical_selection": {key: control._identity(cfg) for key, cfg in new.items()},
        "release": {"identity_conversions": conversions},
        "readiness": {"harness_run_ids": kwargs["harness_run_ids"], "local_execution_sha256": {key: "b" * 64 for key in old},
                      "equivalence_sha256": {key: "a" * 64 for key in old},
                      "prepare_database_identity_sha256": repo.native_successor_plan_sha256(
                          {"database_name": kwargs["expected_database_name"], "server_uuid": kwargs["expected_server_uuid"]}
                          if engine.dialect.name == "mysql" else {"isolated_test": "sqlite"})},
    }
    return engine, kwargs, evidence


def _plan(engine, kwargs, evidence):
    return repo.read_w1a_writer_reclaim_plan(engine, **kwargs, control_plane_evidence=evidence)


def _apply(engine, kwargs, evidence, plan):
    return repo.apply_w1a_writer_reclaim(engine, **kwargs, control_plane_evidence_reader=lambda: evidence,
        expected_plan_sha256=repo.native_successor_plan_sha256(plan), approved_by="test", approved_at=datetime.now(timezone.utc))


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL URL")
@pytest.mark.parametrize("fail_second", [False, True])
def test_mysql_w1a_eight_identity_roundtrip_and_atomic_failure(mysql_migration, fail_second):
    engine, kwargs, evidence = _seed_w1a(mysql_migration)
    initial = _plan(engine, kwargs, evidence)
    if fail_second:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE t_scheme_registry ADD CONSTRAINT keep_t5_native CHECK (base_scheme_id <> 't5_daily' OR runtime_type = 'native_adapter')")
        from sqlalchemy.exc import OperationalError

        with pytest.raises(OperationalError):
            _apply(engine, kwargs, evidence, initial)
        assert _plan(engine, kwargs, evidence) == initial
        return
    _apply(engine, kwargs, evidence, initial)
    rollback = _plan(engine, kwargs | {"action": "rollback"}, evidence)
    assert rollback["facts"] == initial["facts"]
    assert {row["scheme_id"] for row in rollback["versions"] if row["status"] == "active"} == set(control.IDS)
    assert sum(row["status"] == "active" for row in rollback["registry"]) == 6
    _apply(engine, kwargs | {"action": "rollback"}, evidence, rollback)
    after = _plan(engine, kwargs, evidence)
    assert after["facts"] == initial["facts"]
    assert {row["scheme_id"] for row in after["versions"] if row["status"] == "active"} == set(control.ALIASES)
    assert all(row["status"] != "active" for row in after["versions"] if row["runtime_type"] == "native_adapter")


def test_eight_identity_cutover_and_rollback_preserve_facts_and_six_business_ids(w1a):
    engine, kwargs, evidence = w1a
    before = _plan(engine, kwargs, evidence)
    assert len(before["registry"]) == 12
    assert len(before["source_backtests"]) == 6
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        _apply(engine, kwargs, evidence, before)
        rollback = _plan(engine, kwargs | {"action": "rollback"}, evidence)
        assert rollback["facts"] == before["facts"]
        assert sum(row["status"] == "active" for row in rollback["registry"]) == 6
        assert {row["scheme_id"] for row in rollback["versions"] if row["status"] == "active"} == set(control.IDS)
        _apply(engine, kwargs | {"action": "rollback"}, evidence, rollback)
    after = _plan(engine, kwargs, evidence)
    assert after["facts"] == before["facts"]
    assert {row["scheme_id"] for row in after["versions"] if row["status"] == "active"} == set(control.ALIASES)
    assert all(row["status"] != "active" for row in after["versions"] if row["runtime_type"] == "native_adapter")


@pytest.mark.parametrize("failure", ["second_upsert", "registry", "historical", "facts"])
def test_mid_transaction_failures_roll_back_every_writer(w1a, failure):
    engine, kwargs, evidence = w1a
    initial = _plan(engine, kwargs, evidence)
    calls = []

    def upsert(conn, cfg, **values):
        calls.append(cfg.scheme_id)
        if failure == "second_upsert" and len(calls) == 2:
            raise RuntimeError("injected failure")
        result = _sqlite_upsert(conn, cfg, **values)
        if failure == "registry":
            conn.execute(text("UPDATE t_scheme_registry SET target_tenor='1Y' WHERE base_scheme_id='t1_daily'"))
        elif failure == "historical":
            conn.execute(text("UPDATE t_scheme_versions SET code_hash='changed' WHERE scheme_id='t1_daily' AND scheme_version='historical-paused'"))
        elif failure == "facts":
            conn.execute(text("UPDATE t_scheme_predictions SET predicted_direction=0 WHERE scheme_id='t1_daily'"))
        return result

    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=upsert), pytest.raises(RuntimeError):
        _apply(engine, kwargs, evidence, initial)
    assert _plan(engine, kwargs, evidence) == initial


@pytest.mark.parametrize("drift", ["schema", "source", "registry", "running", "gate", "receipt", "scope"])
def test_preflight_rejects_partial_or_untrusted_w1a_state(w1a, drift):
    engine, kwargs, evidence = w1a
    with engine.begin() as conn:
        if drift == "schema":
            conn.execute(text("UPDATE t_schema_migrations SET state='APPLYING'"))
        elif drift == "source":
            conn.execute(text("UPDATE t_scheme_versions SET code_hash='wrong' WHERE scheme_id='t5_daily_7y_bbv2'"))
        elif drift == "registry":
            conn.execute(text("UPDATE t_scheme_registry SET status='paused' WHERE base_scheme_id='t1_daily_5y_bbv2'"))
        elif drift == "running":
            conn.execute(text("UPDATE t_harness_runs SET status='running' WHERE scheme_id='t1_daily'"))
        elif drift == "gate":
            conn.execute(text("UPDATE t_harness_gate_results SET status='failed' WHERE harness_run_id='t5_daily'"))
        elif drift == "receipt":
            evidence["readiness"]["equivalence_sha256"]["t5_daily"] = "c" * 64
        else:
            kwargs["source_configs"].pop("t5_daily_7y_bbv2")
    with pytest.raises((RuntimeError, ValueError)):
        _plan(engine, kwargs, evidence)


def test_prepare_refuses_existing_gate_and_never_recalculates(w1a):
    engine, kwargs, evidence = w1a
    prepare_kwargs = kwargs | {"action": "prepare", "harness_run_ids": {}}
    with pytest.raises(RuntimeError, match="preparation exists"):
        _plan(engine, prepare_kwargs, evidence)
    plan = repo.read_w1a_writer_reclaim_plan(engine, **prepare_kwargs, control_plane_evidence=evidence, permitted_runs=control.IDS)
    assert plan["facts"]["t_scheme_predictions"]["count"] > 0


def test_apply_takes_all_eight_locks_and_rejects_stale_plan(w1a):
    engine, kwargs, evidence = w1a
    plan = _plan(engine, kwargs, evidence)
    wrapper = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), begin=engine.begin)
    locks = []
    with patch.object(repo, "_blackbox_activation_advisory_lock", side_effect=lambda engine, scheme_id: (locks.append(scheme_id), nullcontext())[1]):
        with pytest.raises(RuntimeError, match="plan changed"):
            repo.apply_w1a_writer_reclaim(wrapper, **kwargs, control_plane_evidence_reader=lambda: evidence,
                expected_plan_sha256="0" * 64, approved_by="test", approved_at=datetime.now(timezone.utc))
    assert locks == list(control.ALL_IDS)
    assert _plan(engine, kwargs, evidence) == plan


def test_final_control_read_drift_rolls_back_complete_transaction(w1a):
    engine, kwargs, evidence = w1a
    before = _plan(engine, kwargs, evidence)
    captures = iter([evidence, evidence | {"changed_after_sql": True}])
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert), pytest.raises(RuntimeError, match="control plane changed"):
        repo.apply_w1a_writer_reclaim(engine, **kwargs, control_plane_evidence_reader=lambda: next(captures),
            expected_plan_sha256=repo.native_successor_plan_sha256(before), approved_by="test", approved_at=datetime.now(timezone.utc))
    assert _plan(engine, kwargs, evidence) == before
