"""同 ID 迁移期的原子切换验收；迁移工具退役时一并删除。"""

from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler import repository as repo
from test_native_successor_migration import _fixture, _sqlite_upsert
from test_blackbox_activation import (
    _activate_revision_repository,
    _revision_fixture,
    _sqlite_revision_upsert,
)


@pytest.fixture
def migration():
    engine, old, new, _, _ = _fixture()
    new.scheme_id = old.scheme_id
    new.horizon = old.horizon
    new.target_rule = old.target_rule
    old2, new2 = deepcopy(old), deepcopy(new)
    old2.scheme_id = new2.scheme_id = "second_native"
    old_configs = {cfg.scheme_id: cfg for cfg in (old, old2)}
    new_configs = {cfg.scheme_id: cfg for cfg in (new, new2)}
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t_harness_runs (harness_run_id TEXT PRIMARY KEY, scheme_id TEXT, scheme_version TEXT, code_hash TEXT, config_hash TEXT, stage TEXT, status TEXT, finished_at TEXT)")
        conn.exec_driver_sql("CREATE TABLE t_harness_gate_results (id INTEGER PRIMARY KEY, harness_run_id TEXT, gate_name TEXT, status TEXT, finished_at TEXT, summary_json TEXT)")
        conn.exec_driver_sql("CREATE TABLE t_input_artifacts (artifact_id TEXT PRIMARY KEY, scheme_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE t_backtest_monthly_metrics (id INTEGER PRIMARY KEY, scheme_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE t_backtest_reproduction_checks (id INTEGER PRIMARY KEY, benchmark_id TEXT)")
        row = dict(conn.execute(text("SELECT * FROM t_scheme_versions")).mappings().one())
        row["scheme_id"] = old2.scheme_id
        conn.execute(text(f"INSERT INTO t_scheme_versions ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)
        row = dict(conn.execute(text("SELECT * FROM t_scheme_registry")).mappings().one())
        row["base_scheme_id"] = old2.scheme_id
        row["scheme_id"] = f"{old2.scheme_id}__h6__5Y"
        conn.execute(text(f"INSERT INTO t_scheme_registry ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)
        row["base_scheme_id"] = "unrelated"
        row["scheme_id"] = "unrelated__h6__5Y"
        row["status"] = "paused"
        conn.execute(text(f"INSERT INTO t_scheme_registry ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)
        fields = ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")
        for index, scheme_id in enumerate(old_configs):
            old, new = old_configs[scheme_id], new_configs[scheme_id]
            summary = {
                "schema_version": "same-id-runtime-upgrade-evidence-v1", "scheme_id": scheme_id,
                "old_identity": {key: getattr(old, key) for key in fields},
                "new_identity": {key: getattr(new, key) for key in fields},
                "environment_fingerprint": new.environment_fingerprint,
                "data_snapshot_id": new.data_snapshot_id, "generation_id": "generation-1",
                "equivalence_sha256": "a" * 64, "local_execution_sha256": "b" * 64,
            }
            conn.execute(text("INSERT INTO t_harness_runs VALUES (:run,:scheme_id,:scheme_version,:code_hash,:config_hash,'native-runtime-upgrade','passed','2026-09-12')"),
                         vars(new) | {"run": scheme_id})
            conn.execute(text("INSERT INTO t_harness_gate_results VALUES (:id,:run,'native-runtime-upgrade','passed','2026-09-12',:summary)"),
                         {"id": index + 1, "run": scheme_id, "summary": json.dumps({
                             "passed": True, "errors": [],
                             "evidence": [{"key": "runtime_upgrade", "value": summary}],
                         })})
    kwargs = dict(old_configs=old_configs, new_configs=new_configs,
                  harness_run_ids={key: key for key in old_configs}, action="cutover",
                  expected_database_name="isolated", expected_server_uuid="isolated")
    control = {"databridge": {"generation_id": "generation-1", "data_snapshot_id": "snapshot-1"},
               "blackbox_environment_fingerprint": "e" * 64, "writer_fenced": True}
    yield engine, kwargs, control
    engine.dispose()


def _plan(engine, kwargs, control):
    return repo.read_same_id_runtime_upgrade_plan(engine, **kwargs, control_plane_evidence=control)


def _apply(engine, kwargs, control, plan=None, reader=None):
    plan = plan or _plan(engine, kwargs, control)
    return repo.apply_same_id_runtime_upgrade(
        engine, **kwargs, expected_plan_sha256=repo.native_successor_plan_sha256(plan),
        approved_by="test-operator", approved_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        control_plane_evidence_reader=reader or (lambda: control),
    )


def test_atomic_cutover_rollback_recutover_preserves_history(migration):
    engine, kwargs, control = migration
    initial = _plan(engine, kwargs, control)
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        _apply(engine, kwargs, control, initial)
        rollback_kwargs = kwargs | {"action": "rollback"}
        rollback = _plan(engine, rollback_kwargs, control)
        assert rollback["facts"] == initial["facts"]
        assert {row["scheme_id"] for row in rollback["registry"]} == {row["scheme_id"] for row in initial["registry"]}
        _apply(engine, rollback_kwargs, control, rollback)
        restored = _plan(engine, kwargs, control)
        assert restored["facts"] == initial["facts"]
        assert restored["registry"] == initial["registry"]
        _apply(engine, kwargs, control, restored)
        assert _plan(engine, rollback_kwargs, control)["facts"] == initial["facts"]


def test_second_member_failure_rolls_back_first_member(migration):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    count = 0

    def fail_second(conn, cfg, **values):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("injected second member failure")
        return _sqlite_upsert(conn, cfg, **values)

    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="injected"):
            _apply(engine, kwargs, control, before)
    assert _plan(engine, kwargs, control) == before


@pytest.mark.parametrize("extra_ids", [
    ("unrelated",), ("native_multi_bbv2",),
    ("native_multi_bbv2", "native_multi_bbv2", "second_native_bbv2"),
])
def test_additional_lifecycle_locks_reject_unrelated_or_partial_scope(migration, extra_ids):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    with pytest.raises(ValueError, match="exactly the temporary successor IDs"):
        _apply(engine, kwargs | {"additional_lifecycle_lock_scheme_ids": extra_ids}, control, before)
    assert _plan(engine, kwargs, control) == before


def test_original_and_temporary_locks_have_one_stable_order(migration, monkeypatch):
    engine, kwargs, control = migration
    extra_ids = tuple(scheme_id + "_bbv2" for scheme_id in kwargs["old_configs"])
    control = control | {"temporary_writer_check": {"scheme_ids": list(extra_ids)}}
    before = _plan(engine, kwargs, control)
    events = []

    @contextmanager
    def lock(_engine, *, scheme_id):
        events.append(("acquire", scheme_id))
        try:
            yield
        finally:
            events.append(("release", scheme_id))

    def stop_before_any_write(*_args, **_kwargs):
        raise RuntimeError("stop after all locks before any write")

    monkeypatch.setattr(engine.dialect, "name", "mysql")
    monkeypatch.setattr(repo, "_blackbox_activation_advisory_lock", lock)
    monkeypatch.setattr(repo, "_same_id_runtime_upgrade_plan_conn", stop_before_any_write)
    with pytest.raises(RuntimeError, match="stop after all locks"):
        _apply(engine, kwargs | {"additional_lifecycle_lock_scheme_ids": extra_ids}, control, before)
    order = sorted(set(kwargs["old_configs"]) | set(extra_ids))
    assert events == [("acquire", key) for key in order] + [("release", key) for key in reversed(order)]


def test_temporary_lock_scope_must_match_captured_control_plane(migration):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    extra_ids = tuple(scheme_id + "_bbv2" for scheme_id in kwargs["old_configs"])
    with pytest.raises(ValueError, match="lock scope differs"):
        _apply(engine, kwargs | {"additional_lifecycle_lock_scheme_ids": extra_ids}, control, before)
    assert _plan(engine, kwargs, control) == before


def test_native_daily_implicit_rule_preserves_blackbox_business_semantics(migration):
    engine, kwargs, control = migration
    from shared.task_specs import TASK_COMBINATIONS

    for configs in (kwargs["old_configs"], kwargs["new_configs"]):
        for cfg in configs.values():
            cfg.task_type = "T+5"
    for cfg in kwargs["old_configs"].values():
        cfg.target_rule = None
    for cfg in kwargs["new_configs"].values():
        cfg.target_rule = TASK_COMBINATIONS["T+5"][1]
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_registry SET task_type='T+5' WHERE base_scheme_id IN ('native_multi','second_native')"))
    assert _plan(engine, kwargs, control)["action"] == "cutover"
    next(iter(kwargs["old_configs"].values())).target_rule = "unknown_rule"
    with pytest.raises(ValueError, match="target_rule"):
        _plan(engine, kwargs, control)


@pytest.mark.parametrize("field", ["algorithm_version", "contract_version", "runtime_profile"])
def test_first_cutover_plan_binds_candidate_runtime_fields(migration, field):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    setattr(next(iter(kwargs["new_configs"].values())), field, "unreviewed")
    with pytest.raises(RuntimeError, match="plan changed"):
        _apply(engine, kwargs, control, before)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_versions WHERE runtime_type='blackbox_v2'")).scalar() == 0


@pytest.mark.parametrize("statement", [
    "UPDATE t_scheme_predictions SET predicted_direction = -1 WHERE id = 1",
    "UPDATE t_scheme_registry SET owner = 'changed' WHERE base_scheme_id = 'unrelated'",
    "UPDATE t_harness_gate_results SET finished_at = 'changed' WHERE id = 1",
])
def test_stale_plan_rejected_without_writes(migration, statement):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    with engine.begin() as conn:
        conn.exec_driver_sql(statement)
    drifted = _plan(engine, kwargs, control)
    with pytest.raises(RuntimeError, match="plan changed"):
        _apply(engine, kwargs, control, before)
    assert _plan(engine, kwargs, control) == drifted


@pytest.mark.parametrize("table,statement", [
    ("scheme", "INSERT INTO t_scheme_runs VALUES (1,'native_multi','2026-09-12','running')"),
    ("backtest", "UPDATE t_backtest_runs SET scheme_id='native_multi',status='running' WHERE id=42"),
    ("harness", "UPDATE t_harness_runs SET status='running' WHERE scheme_id='native_multi'"),
    ("version", "INSERT INTO t_scheme_versions(scheme_id,scheme_version,runtime_type,status) VALUES ('native_multi','unexpected','native_adapter','active')"),
])
def test_second_writer_rejected(migration, table, statement):
    engine, kwargs, control = migration
    with engine.begin() as conn:
        conn.exec_driver_sql(statement)
    with pytest.raises(RuntimeError, match="running|second Writer"):
        _plan(engine, kwargs, control)


@pytest.mark.parametrize("statement", [
    "UPDATE t_harness_runs SET code_hash='wrong'",
    "UPDATE t_harness_gate_results SET status='failed'",
    "UPDATE t_harness_gate_results SET summary_json='{}'",
    "INSERT INTO t_harness_gate_results SELECT id+10,harness_run_id,gate_name,status,finished_at,summary_json FROM t_harness_gate_results WHERE id=1",
])
def test_persisted_evidence_rejected_when_invalid(migration, statement):
    engine, kwargs, control = migration
    with engine.begin() as conn:
        conn.exec_driver_sql(statement)
    with pytest.raises(RuntimeError, match="Harness|gate|evidence"):
        _plan(engine, kwargs, control)


@pytest.mark.parametrize("change", ["duplicate", "extra", "passed", "errors"])
def test_harness_summary_wrapper_is_unambiguous(migration, change):
    engine, kwargs, control = migration
    with engine.begin() as conn:
        summary = json.loads(conn.exec_driver_sql("SELECT summary_json FROM t_harness_gate_results WHERE id=1").scalar_one())
        if change == "duplicate":
            summary["evidence"].append(summary["evidence"][0])
        elif change == "extra":
            summary["evidence"].append({"key": "unrelated", "value": True})
        elif change == "passed":
            summary["passed"] = False
        else:
            summary["errors"] = ["failed validation"]
        conn.execute(text("UPDATE t_harness_gate_results SET summary_json=:summary WHERE id=1"),
                     {"summary": json.dumps(summary)})
    with pytest.raises(RuntimeError, match="unambiguous"):
        _plan(engine, kwargs, control)


def test_post_write_control_drift_rolls_back(migration):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)
    calls = iter([control, control | {"writer_fenced": False}])
    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
        with pytest.raises(RuntimeError, match="control plane changed"):
            _apply(engine, kwargs, control, before, reader=lambda: next(calls))
    assert _plan(engine, kwargs, control) == before


def test_post_write_fact_mutation_rolls_back(migration):
    engine, kwargs, control = migration
    before = _plan(engine, kwargs, control)

    def corrupt_fact(conn, cfg, **values):
        result = _sqlite_upsert(conn, cfg, **values)
        conn.exec_driver_sql("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE id=1")
        return result

    with patch.object(repo, "_upsert_scheme_version_conn", side_effect=corrupt_fact):
        with pytest.raises(RuntimeError, match="changed facts"):
            _apply(engine, kwargs, control, before)
    assert _plan(engine, kwargs, control) == before


def test_current_environment_and_input_must_match_evidence(migration):
    engine, kwargs, control = migration
    for changed in (
        control | {"blackbox_environment_fingerprint": "f" * 64},
        control | {"databridge": {"generation_id": "next-generation", "data_snapshot_id": "snapshot-1"}},
    ):
        with pytest.raises(RuntimeError, match="evidence identity mismatch"):
            _plan(engine, kwargs, changed)


@pytest.mark.parametrize("runtime,status,accepted", [
    ("native_adapter", "retired", True),
    ("native_adapter", "active", False),
    ("native_adapter", "paused", False),
    ("unknown", "retired", False),
])
def test_normal_revision_accepts_only_retired_native_history(runtime, status, accepted):
    engine, cfg = _revision_fixture()
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t_scheme_versions(scheme_id,scheme_version,runtime_type,status) VALUES (:id,'native-history',:runtime,:status)"),
                         {"id": cfg.scheme_id, "runtime": runtime, "status": status})
        if accepted:
            result = repo.read_blackbox_revision_activation_preflight(engine, cfg)
            assert result.prior_scheme_version == "version-1"
            with (
                patch.object(repo, "_blackbox_activation_advisory_lock", return_value=nullcontext()),
                patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_revision_upsert),
            ):
                state = _activate_revision_repository(engine, cfg)
            assert state.version_status == "active"
            with engine.connect() as conn:
                assert conn.execute(text("SELECT status FROM t_scheme_versions WHERE scheme_version='native-history'")).scalar_one() == "retired"
        else:
            with pytest.raises(ValueError, match="non-Blackbox"):
                repo.read_blackbox_revision_activation_preflight(engine, cfg)
    finally:
        engine.dispose()
