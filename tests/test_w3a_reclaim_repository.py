"""固定 W3A 修订例外与准备来源 DB 绑定；SQLite/随机隔离 MySQL 实测。"""

from copy import deepcopy
from pathlib import Path
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from harness.w3a_revision_evidence import verify_reviewed_w3a_delivery_change
from scheduler import repository as repo
from scheduler.discovery import load_scheme_config
from test_same_id_runtime_upgrade import migration, _sqlite_upsert
from test_same_id_runtime_upgrade_mysql import mysql_migration, MYSQL_URL
from test_same_id_runtime_upgrade_reclaim import reclaim_scope, reclaim_plan, reclaim_apply, assert_reclaim_history


ROOT = Path(__file__).resolve().parents[1]
FULL = "liwei_0616_5y01_full_oos_k3_div_k10"


def reviewed_scope(fixture):
    engine, kwargs, control = reclaim_scope(fixture, "W3A")
    source = load_scheme_config(ROOT / "schemes" / (FULL + "_bbv2") / "config.yaml")
    new = load_scheme_config(ROOT / "schemes" / FULL / "config.yaml")
    conversion = verify_reviewed_w3a_delivery_change(project_root=ROOT,
        source_script=source.delivery_script.read_bytes(), source_metadata=source.delivery_metadata.read_bytes(),
        candidate_script=new.delivery_script.read_bytes(), candidate_metadata=new.delivery_metadata.read_bytes())
    prior_source = kwargs["source_configs"][FULL]
    old_source_version = prior_source.scheme_version
    for target, actual in ((prior_source, source), (kwargs["new_configs"][FULL], new)):
        for field in ("scheme_version", "code_hash", "config_hash", "manifest_hash"):
            setattr(target, field, getattr(actual, field))
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_versions SET scheme_version=:scheme_version,code_hash=:code_hash,config_hash=:config_hash,manifest_hash=:manifest_hash WHERE scheme_id=:scheme_id AND scheme_version=:old"),
            repo._same_id_reclaim_identity(prior_source) | {"old": old_source_version})
        conn.execute(text("UPDATE t_harness_runs SET scheme_version=:scheme_version,code_hash=:code_hash,config_hash=:config_hash WHERE scheme_id=:scheme_id"), repo._same_id_reclaim_identity(new))
        summary = json.loads(conn.execute(text("SELECT summary_json FROM t_harness_gate_results WHERE harness_run_id=:run"), {"run": FULL}).scalar_one())
        summary["evidence"][0]["value"]["new_identity"] = {key: value for key, value in repo._same_id_reclaim_identity(new).items() if key != "scheme_id"}
        conn.execute(text("UPDATE t_harness_gate_results SET summary_json=:summary WHERE harness_run_id=:run"), {"run": FULL, "summary": json.dumps(summary)})
        backtest = dict(conn.execute(text("SELECT id,summary FROM t_backtest_runs WHERE scheme_id=:id"), {"id": source.scheme_id}).mappings().one())
        backtest_summary = json.loads(backtest["summary"])
        backtest_summary.update(scheme_version=source.scheme_version, manifest_hash=source.manifest_hash)
        conn.execute(text("UPDATE t_backtest_runs SET code_hash=:code,config_hash=:config,summary=:summary WHERE id=:id"),
            {"id": backtest["id"], "code": source.code_hash, "config": source.config_hash, "summary": json.dumps(backtest_summary)})
        identity = (dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
                    if engine.dialect.name == "mysql" else {"isolated_test": "sqlite"})
        proofs = {key: json.loads(conn.execute(text("SELECT summary_json FROM t_harness_gate_results WHERE harness_run_id=:run"), {"run": key}).scalar_one())["evidence"][0]["value"] for key in kwargs["new_configs"]}
    control["source_canonical_selection"] = {key: repo._same_id_reclaim_identity(cfg) for key, cfg in kwargs["source_configs"].items()}
    control["release"] = {"identity_conversions": {FULL: conversion}}
    control["w3a_readiness"] = {"status": "ready", "source_and_candidate_current_input_verified": True,
        "prepare_database_identity_sha256": repo.native_successor_plan_sha256(identity),
        "harness_run_ids": kwargs["harness_run_ids"],
        "local_execution_sha256": {key: value["local_execution_sha256"] for key, value in proofs.items()},
        "equivalence_sha256": {key: value["equivalence_sha256"] for key, value in proofs.items()}}
    return engine, kwargs, control


def assert_cutover_rollback(fixture):
    engine, kwargs, control = reviewed_scope(fixture)
    initial = reclaim_plan(engine, kwargs, control)
    reclaim_apply(engine, kwargs, control, initial)
    backward = reclaim_plan(engine, kwargs | {"action": "rollback"}, control)
    assert_reclaim_history(initial, backward, rolled_back=False)
    reclaim_apply(engine, kwargs | {"action": "rollback"}, control, backward)
    restored = reclaim_plan(engine, kwargs, control)
    assert_reclaim_history(initial, restored, rolled_back=True)


def test_sqlite_fixed_reviewed_full_revision_uses_atomic_existing_reclaim(migration):
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        assert_cutover_rollback(migration)


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL test URL")
def test_mysql_fixed_reviewed_full_revision_cutover_rollback(mysql_migration):
    assert_cutover_rollback(mysql_migration)


@pytest.mark.parametrize("drift", ["code", "version", "conversion", "readiness", "gate", "database"])
def test_sqlite_reviewed_exception_is_closed(migration, drift):
    engine, kwargs, control = reviewed_scope(migration)
    if drift == "code":
        kwargs["new_configs"][FULL].code_hash = "0" * 64
    elif drift == "version":
        kwargs["new_configs"][FULL].scheme_version = "another"
    elif drift == "conversion":
        control["release"]["identity_conversions"][FULL]["weekly_length_guard_preserved"] = False
    elif drift == "readiness":
        control["w3a_readiness"]["source_and_candidate_current_input_verified"] = False
    elif drift == "gate":
        control["w3a_readiness"]["local_execution_sha256"][FULL] = "0" * 64
    else:
        control["w3a_readiness"]["prepare_database_identity_sha256"] = "0" * 64
    with pytest.raises((RuntimeError, ValueError)):
        reclaim_plan(engine, kwargs, control)


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL test URL")
def test_mysql_rejects_preparation_from_another_database(mysql_migration):
    engine, kwargs, control = reviewed_scope(mysql_migration)
    before = reclaim_plan(engine, kwargs, control)
    drift = deepcopy(control)
    drift["w3a_readiness"]["prepare_database_identity_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="another database"):
        reclaim_apply(engine, kwargs, control, before, reader=lambda: drift)
    assert reclaim_plan(engine, kwargs, control) == before


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL test URL")
def test_mysql_second_member_failure_rolls_back_revision_and_all_history(mysql_migration):
    engine, kwargs, control = reviewed_scope(mysql_migration)
    before = reclaim_plan(engine, kwargs, control)
    original = repo._upsert_scheme_version_conn
    def fail_second(conn, cfg, **options):
        if cfg.scheme_id != FULL:
            raise RuntimeError("second member injected failure")
        return original(conn, cfg, **options)
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="second member"):
            reclaim_apply(engine, kwargs, control, before)
    assert reclaim_plan(engine, kwargs, control) == before
