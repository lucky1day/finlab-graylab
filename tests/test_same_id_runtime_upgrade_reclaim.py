"""W2/W3A 临时 Writer 回收事务；所有 fixture 均为隔离数据库。"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler import repository as repo
from shared.task_specs import TASK_COMBINATIONS
from test_same_id_runtime_upgrade import migration, _sqlite_upsert


def reclaim_scope(migration_fixture, wave):
    """生成旧原 ID archived、临时 ID active 和多条 Native 历史的独立样本。"""
    engine, prior, control = migration_fixture
    template_id = next(iter(prior["old_configs"]))
    old_configs, source_configs, new_configs = {}, {}, {}

    def insert(conn, table, row):
        row = {key: value for key, value in row.items() if key != "id"}
        result = conn.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES "
                                   f"({','.join(':' + key for key in row)})"), row)
        return result.lastrowid

    with engine.begin() as conn:
        def one(table, where, params):
            return dict(conn.execute(text(f"SELECT * FROM {table} WHERE {where}"), params).mappings().one())
        version = one("t_scheme_versions", "scheme_id=:id", {"id": template_id})
        registry = one("t_scheme_registry", "base_scheme_id=:id", {"id": template_id})
        harness_run = one("t_harness_runs", "scheme_id=:id", {"id": template_id})
        harness_gate = one("t_harness_gate_results", "harness_run_id=:id", {"id": prior["harness_run_ids"][template_id]})
        backtest = one("t_backtest_runs", "id=42", {})
        backtest_prediction = one("t_backtest_predictions", "id=1", {})
        fact = one("t_scheme_predictions", "id=1", {})
        for index, scheme_id in enumerate(sorted(repo._SAME_ID_RECLAIM_WAVES[wave])):
            old, new = deepcopy(prior["old_configs"][template_id]), deepcopy(prior["new_configs"][template_id])
            for cfg in (old, new):
                cfg.scheme_id, cfg.horizon, cfg.task_type, cfg.frequency = scheme_id, 5, "T+5", "daily"
                cfg.tenors = ["7Y" if scheme_id == "daily_7y_1_v28" else "5Y"]
            old.target_rule = None
            new.target_rule = TASK_COMBINATIONS["T+5"][1]
            source = deepcopy(new)
            source.scheme_id, source.scheme_version, source.manifest_hash = scheme_id + "_bbv2", "alias-bb-v1", "2" * 64
            old_configs[scheme_id], source_configs[scheme_id], new_configs[scheme_id] = old, source, new
            insert(conn, "t_scheme_versions", version | repo._same_id_reclaim_identity(old) | {"status": "retired"})
            insert(conn, "t_scheme_versions", version | {
                field: getattr(source, field) for field in (
                    "scheme_id", "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash",
                    "algorithm_version", "contract_version", "runtime_profile", "environment_fingerprint", "data_snapshot_id",
                )} | {"status": "active"})
            for status in ("active", "active", "paused", "retired"):
                suffix = f"historical-{status}-{index}"
                index += 1
                insert(conn, "t_scheme_versions", version | {
                    "scheme_id": scheme_id, "scheme_version": suffix, "status": status,
                })
            for cfg, status in ((old, "archived"), (source, "active")):
                insert(conn, "t_scheme_registry", registry | {
                    "scheme_id": repo.registry_scheme_id(cfg.scheme_id, 5, cfg.tenors[0]),
                    "base_scheme_id": cfg.scheme_id, "horizon": 5, "task_type": "T+5", "frequency": "daily",
                    "tenors": json.dumps(cfg.tenors), "target_tenor": cfg.tenors[0],
                    "runtime_type": cfg.runtime_type, "status": status,
                })
            insert(conn, "t_harness_runs", harness_run | {"scheme_id": scheme_id, "harness_run_id": scheme_id})
            summary = json.loads(harness_gate["summary_json"])
            proof = summary["evidence"][0]["value"]
            proof["scheme_id"] = scheme_id
            proof["old_identity"] = {key: value for key, value in repo._same_id_reclaim_identity(old).items() if key != "scheme_id"}
            proof["new_identity"] = {key: value for key, value in repo._same_id_reclaim_identity(new).items() if key != "scheme_id"}
            insert(conn, "t_harness_gate_results", harness_gate | {"harness_run_id": scheme_id, "summary_json": json.dumps(summary)})
            backtest_summary = json.loads(backtest["summary"])
            backtest_summary.update(scheme_version=source.scheme_version, manifest_hash=source.manifest_hash)
            run_id = insert(conn, "t_backtest_runs", backtest | {
                "scheme_id": source.scheme_id, "benchmark_id": "reclaim-" + scheme_id,
                "code_hash": source.code_hash, "config_hash": source.config_hash,
                "summary": json.dumps(backtest_summary),
            })
            insert(conn, "t_backtest_predictions", backtest_prediction | {
                "run_id": run_id, "scheme_id": source.scheme_id, "horizon": 5, "target_tenor": source.tenors[0],
            })
            for cfg in (old, source):
                insert(conn, "t_scheme_predictions", fact | {"scheme_id": cfg.scheme_id, "target_tenor": cfg.tenors[0], "horizon": 5})
    kwargs = prior | {"wave": wave, "old_configs": old_configs, "source_configs": source_configs,
                      "new_configs": new_configs, "harness_run_ids": {key: key for key in old_configs}}
    control = control | {
        "schema_version": "same-id-writer-reclaim-control-plane-v1", "wave": wave, "deployment_target": "aliyun-gray",
        "scheduler": {"control_plane": "systemd_one_shot", "timer_fenced": True, "unique_writer": True},
        "native_canonical_selection": {key: repo._same_id_reclaim_identity(cfg) for key, cfg in old_configs.items()},
        "source_canonical_selection": {key: repo._same_id_reclaim_identity(cfg) for key, cfg in source_configs.items()},
    }
    return engine, kwargs, control


def reclaim_plan(engine, kwargs, control):
    return repo.read_same_id_writer_reclaim_plan(engine, **kwargs, control_plane_evidence=control)


def reclaim_apply(engine, kwargs, control, plan, reader=None):
    return repo.apply_same_id_writer_reclaim(
        engine, **kwargs, expected_plan_sha256=repo.native_successor_plan_sha256(plan),
        approved_by="isolated-reclaim-test", approved_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        control_plane_evidence_reader=reader or (lambda: control),
    )


@pytest.fixture(params=["W2", "W3A"])
def reclaim(migration, request):
    return reclaim_scope(migration, request.param)


def assert_reclaim_history(initial, current, *, rolled_back):
    assert initial["facts"] == current["facts"]
    assert initial["facts"]["t_scheme_predictions"]["count"] == 4
    prior = {(row["scheme_id"], row["scheme_version"]): row for row in initial["versions"]}
    for row in current["versions"]:
        if row["scheme_version"].startswith("historical-"):
            original = prior[row["scheme_id"], row["scheme_version"]]
            assert row == (original | {"status": "retired"} if original["status"] == "active" else original)
    active = [row for row in current["versions"] if row["status"] == "active"]
    assert len(active) == 2
    assert all(row["runtime_type"] == "blackbox_v2" for row in active)
    assert all(row["scheme_id"].endswith("_bbv2") == rolled_back for row in active)
    for row in current["registry"]:
        alias = row["base_scheme_id"].endswith("_bbv2")
        assert row["status"] == ("active" if alias == rolled_back else "archived")


def test_reclaim_and_rollback_preserve_all_facts_and_only_restore_alias_writer(reclaim):
    engine, kwargs, control = reclaim
    before = reclaim_plan(engine, kwargs, control)
    assert len(before["historical_native_versions_to_retire"]) == 4
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        reclaim_apply(engine, kwargs, control, before)
        rollback_args = kwargs | {"action": "rollback"}
        rollback = reclaim_plan(engine, rollback_args, control)
        assert_reclaim_history(before, rollback, rolled_back=False)
        reclaim_apply(engine, rollback_args, control, rollback)
        restored = reclaim_plan(engine, kwargs, control)
        assert_reclaim_history(before, restored, rolled_back=True)


@pytest.mark.parametrize("failure", ["second_member", "facts", "source_metadata", "control"])
def test_reclaim_failure_rolls_back_all_writer_and_history_changes(reclaim, failure):
    engine, kwargs, control = reclaim
    before = reclaim_plan(engine, kwargs, control)
    calls = 0
    def fail(conn, cfg, **values):
        nonlocal calls
        calls += 1
        result = _sqlite_upsert(conn, cfg, **values)
        if failure == "second_member" and calls == 2:
            raise RuntimeError("injected failure")
        if failure == "facts":
            conn.execute(text("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE scheme_id=:id"), {"id": cfg.scheme_id})
        if failure == "source_metadata":
            conn.execute(text("UPDATE t_scheme_versions SET created_by='changed' WHERE scheme_id=:id"), {"id": cfg.scheme_id + "_bbv2"})
        return result
    controls = iter([control, control | {"drift": True}])
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=fail):
        with pytest.raises(RuntimeError, match="injected|changed"):
            reclaim_apply(engine, kwargs, control, before, reader=(lambda: next(controls)) if failure == "control" else None)
    assert reclaim_plan(engine, kwargs, control) == before


@pytest.mark.parametrize("failure", ["missing_backtest", "wrong_backtest_version", "source_script", "source_exact", "other_writer", "running_alias", "partial", "unfenced", "missing_gate"])
def test_reclaim_rejects_unverified_sources_or_unsafe_scope(reclaim, failure):
    engine, kwargs, control = reclaim
    key = next(iter(kwargs["old_configs"]))
    with engine.begin() as conn:
        if failure == "missing_backtest":
            conn.execute(text("UPDATE t_backtest_runs SET status='failed' WHERE scheme_id=:id"), {"id": key + "_bbv2"})
        elif failure == "wrong_backtest_version":
            conn.execute(text("UPDATE t_backtest_runs SET summary='{}' WHERE scheme_id=:id"), {"id": key + "_bbv2"})
        elif failure == "source_script":
            kwargs["source_configs"][key].code_hash = "0" * 64
        elif failure == "source_exact":
            conn.execute(text("UPDATE t_scheme_versions SET config_hash='changed' WHERE scheme_id=:id"), {"id": key + "_bbv2"})
        elif failure == "other_writer":
            conn.execute(text("UPDATE t_scheme_versions SET runtime_type='blackbox_v2' WHERE scheme_id=:id AND status='active'"), {"id": key})
        elif failure == "running_alias":
            conn.execute(text("INSERT INTO t_scheme_runs (scheme_id,predict_date,status) VALUES (:id,'2026-09-12','running')"), {"id": key + "_bbv2"})
        elif failure == "partial":
            kwargs = kwargs | {field: {key: kwargs[field][key]} for field in ("old_configs", "source_configs", "new_configs", "harness_run_ids")}
        elif failure == "unfenced":
            control["scheduler"]["timer_fenced"] = False
        else:
            conn.execute(text("UPDATE t_harness_gate_results SET status='failed' WHERE harness_run_id=:id"), {"id": key})
    with pytest.raises((ValueError, RuntimeError)):
        reclaim_plan(engine, kwargs, control)


def test_reclaim_stale_plan_rejects_alias_fact_drift(reclaim):
    engine, kwargs, control = reclaim
    before = reclaim_plan(engine, kwargs, control)
    source = next(iter(kwargs["source_configs"].values())).scheme_id
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE scheme_id=:id"), {"id": source})
    with pytest.raises(RuntimeError, match="plan changed"):
        reclaim_apply(engine, kwargs, control, before)
