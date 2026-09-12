"""周频原 ID 回收保持 h6 事实与 h1 执行，不改动既有历史。"""

from datetime import datetime
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text

from harness import w2_reclaim_prepare as prepare
from scheduler import repository as repo
from test_same_id_runtime_upgrade import migration, _sqlite_upsert
from test_same_id_runtime_upgrade_mysql import mysql_migration, MYSQL_URL
from test_same_id_runtime_upgrade_reclaim import reclaim_scope, reclaim_plan, reclaim_apply
from test_w2_reclaim_prepare import prepared
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.contracts import BlackboxRequest


@pytest.fixture
def weekly_prepared(prepared):
    env = prepared
    root = Path(__file__).resolve().parents[1]
    new = {key: replace(load_scheme_config(root / "schemes" / key / "config.yaml"),
                       environment_fingerprint="e" * 64, data_snapshot_id="current-snapshot")
           for key in prepare._stateless_ids("W1B")}
    old = {key: replace(cfg, runtime_type="native_adapter", scheme_version="native-version")
           for key, cfg in new.items()}
    requests = {key: BlackboxRequest(key + ":request", "2026-09-12", "2026-09-11", "2026-09-18",
                                    "2026-09-10", "202635", "202609") for key in new}
    snapshot = env.capture.return_value[2]
    plan = {"wave": "W1B", "input": env.plan["input"],
            "scheduler": {"cadence": "weekly", "timer_fenced": True},
            "equivalence": {key: {"request_count": 72, "generation_id": "older-generation"} for key in new},
            "requests": {key: asdict(req) for key, req in requests.items()}}
    env.capture.return_value = old, new, snapshot, requests, plan
    env.kwargs.update(wave="W1B", predict_date="2026-09-12", expected_plan_sha256=prepare._json_sha256(plan))
    return env


@pytest.mark.parametrize("fail_second", [False, True])
def test_weekly_prepare_three_calls_six_locks_no_partial_business_write(weekly_prepared, fail_second):
    env = weekly_prepared
    real_fake = env.algorithm.side_effect
    count = 0
    def execute(**kwargs):
        nonlocal count
        count += 1
        if fail_second and count == 2:
            raise RuntimeError("second target failed")
        return real_fake(**kwargs)
    env.algorithm.side_effect = execute
    if fail_second:
        with pytest.raises(RuntimeError, match="second target"):
            prepare.execute_w2_reclaim_prepare(env.engine, **env.kwargs)
        assert env.algorithm.call_count == 2
        assert [call.kwargs["status"] for call in env.complete.call_args_list] == ["passed", "failed"]
        assert not (env.kwargs["work_dir"] / "complete.json").exists()
    else:
        result = prepare.execute_w2_reclaim_prepare(env.engine, **env.kwargs)
        assert result["algorithm_executions"] == 3 and len(result["harness_run_ids"]) == 3
        assert not result["prediction_written"] and not result["registry_changed"]
        assert all(call.kwargs["status"] == "passed" for call in env.complete.call_args_list)
    ids = prepare._stateless_ids("W1B")
    assert [key for action, key in env.locks if action == "acquire"] == sorted((*ids, *(key + "_bbv2" for key in ids)))
    assert len(env.locks) == 12


@pytest.mark.parametrize("status,gates", [("passed", [{"status": "passed"}]), ("running", [{"status": "passed"}])])
def test_failed_prepare_never_rewrites_committed_or_uncertain_gate(prepared, monkeypatch, status, gates):
    env = prepared
    key, cfg = next(iter(env.new.items()))
    from types import SimpleNamespace
    ctx = SimpleNamespace(scheme_id=key, config=cfg)
    monkeypatch.setattr(prepare, "_same_id_rows_conn", lambda _conn, table, *_args, **_kw:
                        [{"status": status}] if table == "t_harness_runs" else gates)
    prepare._record_failed_gate(env.engine, ctx, "run", "start", RuntimeError("original"))
    env.complete.assert_not_called()


def test_prepare_work_cannot_pollute_snapshot_generation(prepared, tmp_path):
    env = prepared
    snapshot = env.capture.return_value[2]
    snapshot.root_dir = tmp_path / "generation"
    snapshot.data_dir = snapshot.root_dir / "data"
    snapshot.data_dir.mkdir(parents=True)
    env.kwargs["work_dir"] = snapshot.root_dir / "operation"
    with pytest.raises(ValueError, match="outside releases and input"):
        prepare.execute_w2_reclaim_prepare(env.engine, **env.kwargs)
    assert not env.kwargs["work_dir"].exists()
    env.start.assert_not_called()
    env.algorithm.assert_not_called()


def check_roundtrip(fixture):
    engine, kwargs, control = reclaim_scope(fixture, "W1B")
    before = reclaim_plan(engine, kwargs, control)
    reclaim_apply(engine, kwargs, control, before)
    after = reclaim_plan(engine, kwargs | {"action": "rollback"}, control)
    assert before["facts"] == after["facts"]
    assert len(after["candidate_versions"]) == 3
    for row in after["registry"]:
        alias = row["base_scheme_id"].endswith("_bbv2")
        assert row["horizon"] == (1 if alias else 6)
        assert row["status"] == ("archived" if alias else "active")
    reclaim_apply(engine, kwargs | {"action": "rollback"}, control, after)
    restored = reclaim_plan(engine, kwargs, control)
    assert restored["facts"] == before["facts"]
    assert restored["registry"] == before["registry"]


def test_sqlite_weekly_original_ids_preserve_horizon_and_facts(migration):
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        check_roundtrip(migration)


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL URL")
def test_mysql_weekly_original_ids_preserve_horizon_and_facts(mysql_migration):
    check_roundtrip(mysql_migration)


@pytest.mark.parametrize("drift", ["source_horizon", "target_horizon", "cadence", "missing_member"])
def test_weekly_scope_or_semantics_drift_is_rejected(migration, drift):
    engine, kwargs, control = reclaim_scope(migration, "W1B")
    key = next(iter(kwargs["old_configs"]))
    if drift == "source_horizon":
        kwargs["source_configs"][key].horizon = 6
    elif drift == "target_horizon":
        kwargs["new_configs"][key].horizon = 1
    elif drift == "cadence":
        control["scheduler"]["cadence"] = "daily"
    else:
        kwargs["new_configs"].pop(key)
    with pytest.raises((ValueError, RuntimeError)):
        reclaim_plan(engine, kwargs, control)


@pytest.mark.skipif(not MYSQL_URL, reason="requires explicit isolated MySQL URL")
def test_mysql_weekly_second_member_failure_rolls_back_all(mysql_migration):
    engine, kwargs, control = reclaim_scope(mysql_migration, "W1B")
    before = reclaim_plan(engine, kwargs, control)
    key = sorted(kwargs["new_configs"])[1]
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE t_scheme_registry ADD CONSTRAINT keep_weekly_native "
            "CHECK (base_scheme_id <> '" + key + "' OR runtime_type <> 'blackbox_v2')")
    with pytest.raises(Exception):
        reclaim_apply(engine, kwargs, control, before)
    assert reclaim_plan(engine, kwargs, control) == before


@pytest.mark.parametrize("hour,expected", [(11, "2026-09-05"), (12, "2026-09-12")])
def test_weekly_request_respects_saturday_trigger(monkeypatch, hour, expected):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 12, hour, 0, tzinfo=tz)
    monkeypatch.setattr(prepare, "datetime", Clock)
    assert prepare._weekly_request_date(None) == expected
    with pytest.raises(ValueError):
        prepare._weekly_request_date("2026-09-19")
    with pytest.raises(ValueError):
        prepare._weekly_request_date("2026-09-11")
