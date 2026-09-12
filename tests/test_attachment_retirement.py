"""十七方案附件退役的一次性事务验收；工具退役时删除。"""

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
import shutil

import pytest
import yaml
from sqlalchemy import text

from scheduler import repository as repo
from scheduler.discovery import load_scheme_config
from test_blackbox_activation import _revision_fixture, _sqlite_revision_upsert, _sqlite_datetime_codecs
from test_blackbox_multi_target import _scheme
from test_same_id_runtime_upgrade_mysql import mysql_migration


@pytest.fixture
def retirement(tmp_path, monkeypatch, _sqlite_datetime_codecs):
    engine, _ = _revision_fixture()
    old_configs, new_configs = {}, {}
    for sid in sorted(repo._ATTACHMENT_RETIREMENT_IDS):
        if sid in {"t1_daily", "t5_daily"}:
            path, raw = _scheme(tmp_path / "old", sid)
        else:
            path = tmp_path / "old" / sid / "config.yaml"
            path.parent.mkdir(parents=True)
            (path.parent / "delivery").mkdir()
            (path.parent / f"delivery/{sid}.py").write_text("# immutable algorithm\n")
            (path.parent / f"delivery/{sid}.json").write_text(json.dumps({
                "schema_version": "1.0", "scheme_id": sid, "name": "Trial", "description": "Fixture",
                "owner": "test", "algorithm_version": "1.0.0", "target_tenor": "5Y",
                "task_type": "T+1", "horizon": 1, "target_rule": "target_date_yield_vs_feature_date_yield"}))
            raw = {"scheme_id": sid, "runtime_type": "blackbox_v2", "input_source": "data_bridge_current",
                   "runtime_profile": "blackbox-v2-v1", "data_schema_version": "data-bridge-v1",
                   "factor_input_mode": "algorithm_managed", "status": "active", "version_status": "draft",
                   "schedule": {"cron": "3 7 * * 1-5", "timezone": "Asia/Shanghai", "timeout_sec": 120},
                   "delivery": {"script": f"delivery/{sid}.py", "metadata": f"delivery/{sid}.json"}}
        (path.parent / "core").mkdir()
        payload = b"# former native attachment\n"
        (path.parent / "core/legacy.py").write_bytes(payload)
        raw["native_attachments"] = {"core/legacy.py": hashlib.sha256(payload).hexdigest()}
        path.write_text(yaml.safe_dump(raw))
        dest = tmp_path / "new" / sid
        shutil.copytree(path.parent, dest)
        del raw["native_attachments"]
        (dest / "config.yaml").write_text(yaml.safe_dump(raw))
        (dest / "core/legacy.py").unlink()
        (dest / "core").rmdir()
        old_configs[sid], new_configs[sid] = [replace(load_scheme_config(p), environment_fingerprint="e" * 64,
                                                    data_snapshot_id="historic-snapshot") for p in (path, dest / "config.yaml")]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM t_scheme_versions"))
        conn.execute(text("DELETE FROM t_scheme_registry"))
        for ddl in ("t_schema_migrations (version INTEGER, state TEXT)",
                    "t_scheme_runs (run_id INTEGER PRIMARY KEY, scheme_id TEXT, status TEXT)",
                    "t_harness_runs (harness_run_id TEXT PRIMARY KEY, scheme_id TEXT, status TEXT)",
                    "t_harness_gate_results (id INTEGER PRIMARY KEY, harness_run_id TEXT)",
                    "t_input_artifacts (artifact_id TEXT PRIMARY KEY, scheme_id TEXT)",
                    "t_backtest_monthly_metrics (id INTEGER PRIMARY KEY, scheme_id TEXT)",
                    "t_backtest_reproduction_checks (id INTEGER PRIMARY KEY, benchmark_id TEXT)"):
            conn.exec_driver_sql("CREATE TABLE " + ddl)
        conn.exec_driver_sql("INSERT INTO t_schema_migrations VALUES (24,'APPLIED')")
        for cfg in old_configs.values():
            _sqlite_revision_upsert(conn, cfg, trusted_status="active", approved_by="original", approved_at=datetime(2026, 9, 12))
            for tenor in cfg.tenors:
                conn.execute(text("INSERT INTO t_scheme_registry (scheme_id,base_scheme_id,name,description,horizon,task_type,runtime_type,tenors,frequency,target_tenor,schedule_cron,schedule_timezone,status) VALUES (:id,:base,'Trial','fixture',:horizon,:task,'blackbox_v2',:tenors,:frequency,:tenor,:cron,:tz,'active')"),
                             {"id": f"{cfg.scheme_id}__h{cfg.horizon}__{tenor}", "base": cfg.scheme_id, "horizon": cfg.horizon,
                              "task": cfg.task_type, "tenors": json.dumps([tenor]), "frequency": cfg.frequency,
                              "tenor": tenor, "cron": cfg.schedule.cron, "tz": cfg.schedule.timezone})
            conn.execute(text("INSERT INTO t_scheme_predictions (scheme_id,scheme_version,extra) VALUES (:id,:version,'untouched')"),
                         {"id": cfg.scheme_id, "version": cfg.scheme_version})
    monkeypatch.setattr(repo, "_upsert_scheme_version_conn", _sqlite_revision_upsert)
    kwargs = {"old_configs": old_configs, "new_configs": new_configs, "action": "cutover",
              "expected_database_name": "isolated", "expected_server_uuid": "isolated"}
    control = {"schema_version": "blackbox-attachment-retirement-control-v1", "scheme_ids": sorted(old_configs),
               "writers_fenced": True, "algorithm_executions": 0, "state_ready": True}
    yield engine, kwargs, control
    engine.dispose()


def plan(fixture):
    engine, kwargs, control = fixture
    return repo.read_blackbox_attachment_retirement_plan(engine, **kwargs, control_plane_evidence=control)


def apply(fixture, *, prepared=None, reader=None):
    engine, kwargs, control = fixture
    prepared = prepared or plan(fixture)
    return repo.apply_blackbox_attachment_retirement(engine, **kwargs,
        expected_plan_sha256=repo.native_successor_plan_sha256(prepared), approved_by="retirement", approved_at=datetime(2026, 9, 12),
        control_plane_evidence_reader=reader or (lambda: control))


def test_all_seventeen_roundtrip_and_retired_reentry_preserves_evidence(retirement):
    engine, kwargs, control = retirement
    before = plan(retirement)
    apply(retirement)
    kwargs["action"] = "rollback"
    after = plan(retirement)
    assert len(after["versions"]) == 34
    assert after["facts"] == before["facts"] and after["registry"] == before["registry"]
    apply(retirement)
    kwargs["action"] = "cutover"
    rollback = plan(retirement)
    assert [r for r in rollback["versions"] if r["scheme_version"] == kwargs["old_configs"][r["scheme_id"]].scheme_version] == before["versions"]
    apply(retirement)
    kwargs["action"] = "rollback"
    assert plan(retirement)["versions"] == after["versions"]


@pytest.mark.parametrize("mutation", ["scope", "fence", "execution", "runtime", "config", "code", "snapshot", "environment"])
def test_fail_closed_identity(retirement, mutation):
    _, kwargs, control = retirement
    key = next(iter(kwargs["new_configs"]))
    cfg = kwargs["new_configs"][key]
    if mutation == "scope":
        del kwargs["new_configs"][key]
    elif mutation == "fence":
        control["writers_fenced"] = False
    elif mutation == "execution":
        control["algorithm_executions"] = 1
    elif mutation in {"config", "code"}:
        path = cfg.path / ("config.yaml" if mutation == "config" else f"delivery/{key}.py")
        if mutation == "config":
            raw = yaml.safe_load(path.read_text()); raw["schedule"]["timeout_sec"] = 300
            path.write_text(yaml.safe_dump(raw))
        else:
            path.write_text("# different algorithm\n")
    else:
        field = {"runtime": "runtime_type", "snapshot": "data_snapshot_id", "environment": "environment_fingerprint"}[mutation]
        kwargs["new_configs"][key] = replace(cfg, **{field: "wrong"})
    with pytest.raises((ValueError, RuntimeError)):
        plan(retirement)


def test_readiness_missing_allows_read_but_never_apply(retirement):
    retirement[2]["state_ready"] = False
    prepared = plan(retirement)
    with pytest.raises(RuntimeError, match="states are not ready"):
        apply(retirement, prepared=prepared)
    assert plan(retirement) == prepared


@pytest.mark.parametrize("failure", ["stale", "late_control", "last_insert"])
def test_entire_transaction_rolls_back(retirement, monkeypatch, failure):
    before = plan(retirement)
    prepared = dict(before)
    reader = None
    if failure == "stale":
        prepared["extra"] = "unapproved"
    elif failure == "late_control":
        calls = iter([retirement[2], retirement[2] | {"drift": True}])
        reader = lambda: next(calls)
    else:
        count = 0
        def fail_last(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 17:
                raise RuntimeError("injected last insert failure")
            return _sqlite_revision_upsert(*args, **kwargs)
        monkeypatch.setattr(repo, "_upsert_scheme_version_conn", fail_last)
    with pytest.raises(RuntimeError):
        apply(retirement, prepared=prepared, reader=reader)
    assert plan(retirement) == before


@pytest.mark.parametrize("field,value", [("status", "draft"), ("environment_fingerprint", "other"),
                                         ("data_snapshot_id", "other"), ("approved_by", None), ("code_hash", "f" * 64)])
def test_existing_candidate_must_be_retired_exact_evidence(retirement, field, value):
    engine, kwargs, _ = retirement
    apply(retirement)
    kwargs["action"] = "rollback"; apply(retirement)
    kwargs["action"] = "cutover"
    cfg = next(iter(kwargs["new_configs"].values()))
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE t_scheme_versions SET {field}=:value WHERE scheme_id=:id AND scheme_version=:version"),
                     {"value": value, "id": cfg.scheme_id, "version": cfg.scheme_version})
    with pytest.raises(RuntimeError):
        plan(retirement)


def test_denied_active_approval_is_not_truthy_success(retirement):
    with retirement[0].begin() as conn:
        conn.execute(text("UPDATE t_scheme_registry SET status='archived'"))
    with pytest.raises(RuntimeError, match="not approved"):
        plan(retirement)


@pytest.mark.parametrize("mutation", ["running", "second_writer", "fact_drift", "schema"])
def test_locked_preflight_rejects_changed_database(retirement, mutation):
    engine, kwargs, _ = retirement
    prepared = plan(retirement)
    cfg = next(iter(kwargs["old_configs"].values()))
    with engine.begin() as conn:
        if mutation == "running":
            conn.execute(text("INSERT INTO t_scheme_runs VALUES (1,:id,'running')"), {"id": cfg.scheme_id})
        elif mutation == "second_writer":
            _sqlite_revision_upsert(conn, replace(cfg, scheme_version="second-writer"), trusted_status="active",
                                    approved_by="other", approved_at=datetime(2026, 9, 12))
        elif mutation == "schema":
            conn.exec_driver_sql("UPDATE t_schema_migrations SET state='APPLYING'")
        else:
            conn.exec_driver_sql("UPDATE t_scheme_predictions SET extra='drift'")
    with pytest.raises(RuntimeError):
        apply(retirement, prepared=prepared)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_versions WHERE approved_by='retirement'")).scalar_one() == 0


@pytest.mark.skipif(not os.getenv("BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL"), reason="requires isolated loopback MySQL")
@pytest.mark.parametrize("failure", [False, True])
def test_real_mysql_seventeen_roundtrip_or_last_failure(retirement, mysql_migration, monkeypatch, failure):
    """真实 MySQL 行锁、JSON、时间类型和最后成员失败，不替换仓储写入实现。"""
    source_engine, kwargs, control = retirement
    engine, mysql_kwargs, _ = mysql_migration
    kwargs = kwargs | {key: mysql_kwargs[key] for key in ("expected_database_name", "expected_server_uuid")}
    # SQLite fixture 唯一替换的是 INSERT 方言；MySQL 必须恢复真实受控 writer。
    monkeypatch.undo()
    with source_engine.connect() as source, engine.begin() as conn:
        for table in ("t_scheme_versions", "t_scheme_registry", "t_scheme_predictions"):
            rows = [dict(row) for row in source.execute(text(f"SELECT * FROM {table}")).mappings()]
            for row in rows:
                row.pop("id", None)
                if table == "t_scheme_registry":
                    row["owner"] = "test"
                    row["deployed_at"] = datetime(2026, 9, 12)
                if table == "t_scheme_predictions":
                    row.update(run_id=1, target_tenor="5Y", horizon=1, predict_date="2026-09-11", feature_date="2026-09-10",
                               target_date="2026-09-11", predicted_direction=1, extra='{"untouched":true}')
                conn.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)
    fixture = engine, kwargs, control
    before = plan(fixture)
    lock_names = ["bfl:bbv2-draft:" + hashlib.sha256(key.encode()).hexdigest()[:32]
                  for key in sorted(repo._ATTACHMENT_RETIREMENT_IDS)]
    def locked_control():
        with engine.connect() as conn:
            owners = [conn.execute(text("SELECT IS_USED_LOCK(:name)"), {"name": name}).scalar_one()
                      for name in lock_names]
        assert None not in owners and len(set(owners)) == 1
        return control
    if failure:
        original = repo._upsert_scheme_version_conn
        calls = 0
        def fail_last(*args, **kw):
            nonlocal calls
            calls += 1
            if calls == 17:
                raise RuntimeError("last MySQL insert failure")
            return original(*args, **kw)
        monkeypatch.setattr(repo, "_upsert_scheme_version_conn", fail_last)
        with pytest.raises(RuntimeError, match="last MySQL"):
            apply(fixture, reader=locked_control)
        assert plan(fixture) == before
    else:
        with pytest.raises(RuntimeError, match="database identity mismatch"):
            repo.read_blackbox_attachment_retirement_plan(engine, **(kwargs | {"expected_server_uuid": "wrong"}),
                                                         control_plane_evidence=control)
        apply(fixture, reader=locked_control)
        kwargs["action"] = "rollback"
        after = plan(fixture)
        assert before["facts"] == after["facts"] and before["registry"] == after["registry"]
        apply(fixture)
        kwargs["action"] = "cutover"
        apply(fixture)
    with engine.connect() as conn:
        assert all(conn.execute(text("SELECT IS_FREE_LOCK(:name)"), {"name": name}).scalar_one() == 1 for name in lock_names)
