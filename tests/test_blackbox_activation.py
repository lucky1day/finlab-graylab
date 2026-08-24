from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from harness.blackbox_v2.activation import activate_blackbox
from harness.context import GateContext
from harness.operation import build_direct_operation
from harness.result import GateStatus


def _revision_fixture():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={
            "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        },
    )
    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        name="Trial 10Y",
        description="trial",
        scheme_version="version-2",
        runtime_type="blackbox_v2",
        status="active",
        version_status="active",
        algorithm_version="1.2.3",
        contract_version="1.0",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-2",
        code_hash="c" * 64,
        config_hash="f" * 64,
        manifest_hash="m" * 64,
        horizon=1,
        task_type="T+1",
        tenors=["10Y"],
        frequency="daily",
        schedule=SimpleNamespace(
            cron="3 7 * * 1-5",
            timezone="Asia/Shanghai",
        ),
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE t_harness_runs ("
            "harness_run_id TEXT PRIMARY KEY, scheme_id TEXT, "
            "scheme_version TEXT, stage TEXT, status TEXT, finished_at timestamp)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_versions ("
            "scheme_id TEXT NOT NULL, scheme_version TEXT NOT NULL, "
            "runtime_type TEXT, algorithm_version TEXT, contract_version TEXT, "
            "runtime_profile TEXT, environment_fingerprint TEXT, "
            "data_snapshot_id TEXT, code_hash TEXT, config_hash TEXT, "
            "manifest_hash TEXT, git_commit TEXT, status TEXT, created_by TEXT, "
            "approved_by TEXT, approved_at timestamp, "
            "PRIMARY KEY (scheme_id, scheme_version))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_registry ("
            "scheme_id TEXT PRIMARY KEY, base_scheme_id TEXT, name TEXT, "
            "description TEXT, horizon INTEGER, task_type TEXT, runtime_type TEXT, "
            "tenors TEXT, frequency TEXT, target_tenor TEXT, schedule_cron TEXT, "
            "schedule_timezone TEXT, status TEXT, deployed_at timestamp)"
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_runs VALUES "
                "('hr-candidate', :scheme_id, :version, 'all', 'passed', :finished)"
            ),
            {
                "scheme_id": cfg.scheme_id,
                "version": cfg.scheme_version,
                "finished": datetime(2026, 8, 25, 1, 0),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, status, created_by, "
                "approved_by, approved_at) VALUES "
                "(:scheme_id, 'version-1', 'blackbox_v2', 'active', 'test', "
                "'prior-operator', :approved_at), "
                "(:scheme_id, 'version-shadow', 'blackbox_v2', 'shadow', 'test', "
                "NULL, NULL)"
            ),
            {
                "scheme_id": cfg.scheme_id,
                "approved_at": datetime(2026, 8, 24, 1, 0),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "('trial_10y__h1__10Y', :scheme_id, :name, :description, 1, "
                "'T+1', 'blackbox_v2', '[\"10Y\"]', 'daily', '10Y', "
                "'3 7 * * 1-5', 'Asia/Shanghai', 'active', :deployed_at)"
            ),
            {
                "scheme_id": cfg.scheme_id,
                "name": cfg.name,
                "description": cfg.description,
                "deployed_at": datetime(2026, 8, 24, 1, 0),
            },
        )
    return engine, cfg


def _sqlite_revision_upsert(
    conn,
    cfg,
    *,
    trusted_status,
    approved_by,
    approved_at,
):
    conn.execute(
        text(
            "INSERT INTO t_scheme_versions "
            "(scheme_id, scheme_version, runtime_type, algorithm_version, "
            "contract_version, runtime_profile, environment_fingerprint, "
            "data_snapshot_id, code_hash, config_hash, manifest_hash, git_commit, "
            "status, created_by, approved_by, approved_at) VALUES "
            "(:scheme_id, :scheme_version, :runtime_type, :algorithm_version, "
            ":contract_version, :runtime_profile, :environment_fingerprint, "
            ":data_snapshot_id, :code_hash, :config_hash, :manifest_hash, NULL, "
            ":status, 'test', :approved_by, :approved_at)"
        ),
        {
            "scheme_id": cfg.scheme_id,
            "scheme_version": cfg.scheme_version,
            "runtime_type": cfg.runtime_type,
            "algorithm_version": cfg.algorithm_version,
            "contract_version": cfg.contract_version,
            "runtime_profile": cfg.runtime_profile,
            "environment_fingerprint": cfg.environment_fingerprint,
            "data_snapshot_id": cfg.data_snapshot_id,
            "code_hash": cfg.code_hash,
            "config_hash": cfg.config_hash,
            "manifest_hash": cfg.manifest_hash,
            "status": trusted_status,
            "approved_by": approved_by,
            "approved_at": approved_at,
        },
    )
    return cfg.scheme_version


def _revision_states(engine) -> list[tuple[str, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT scheme_version, status FROM t_scheme_versions "
                "ORDER BY scheme_version"
            )
        ).all()
    return [(str(version), str(status)) for version, status in rows]


def _activate_revision_repository(engine, cfg):
    from scheduler.repository import activate_blackbox_revision

    return activate_blackbox_revision(
        engine,
        cfg,
        prior_scheme_version="version-1",
        pending_scheme_versions=("version-shadow",),
        expected_harness_run_id="hr-candidate",
        approved_by="operator",
        approved_at=datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
    )


def test_activate_dispatches_initial_and_revision_through_one_entry(tmp_path) -> None:
    ctx = GateContext(
        scheme_id="trial_10y",
        predict_date="activate",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
    )
    initial = SimpleNamespace(status="paused", version_status="shadow")
    revision = SimpleNamespace(status="active", version_status="active")
    with (
        patch("harness.blackbox_v2.activation._config", return_value=initial),
        patch(
            "harness.blackbox_v2.activation._activate_initial",
            return_value="initial-result",
        ) as initial_call,
    ):
        assert activate_blackbox(ctx) == "initial-result"
        initial_call.assert_called_once()
    with (
        patch("harness.blackbox_v2.activation._config", return_value=revision),
        patch(
            "harness.blackbox_v2.activation._activate_revision",
            return_value="revision-result",
        ) as revision_call,
    ):
        assert activate_blackbox(ctx) == "revision-result"
        revision_call.assert_called_once()


def test_revision_activation_uses_direct_operation_and_atomic_repository(tmp_path) -> None:
    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        scheme_version="version-2",
        status="active",
        version_status="active",
        runtime_type="blackbox_v2",
        runtime_profile="blackbox-v2-v1",
        path=Path(tmp_path),
    )
    passed_run = SimpleNamespace(
        harness_run_id="hr-passed",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        generation_id="generation-1",
        data_snapshot_id="snapshot-1",
    )
    preflight = SimpleNamespace(
        prior_scheme_version="version-1",
        pending_scheme_versions=("version-pending",),
    )
    state = SimpleNamespace(
        scheme_id=cfg.scheme_id,
        scheme_version=cfg.scheme_version,
        version_status="active",
        registry_status="active",
        registry_scheme_ids=("trial_10y__h1__10Y",),
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
        approved_by="operator",
        approved_at=None,
    )
    engine = SimpleNamespace(dispose=lambda: None)
    operation = build_direct_operation(
        cfg.scheme_id,
        "blackbox_activate",
        scheme_version=cfg.scheme_version,
        issued_by="operator",
    )
    ctx = GateContext(
        scheme_id=cfg.scheme_id,
        predict_date="activate",
        project_root=tmp_path,
        report_dir=tmp_path / "reports",
        config=cfg,
        operation=operation,
        engine_factory=lambda: engine,
    )

    with (
        patch(
            "harness.blackbox_v2.activation.replace",
            side_effect=lambda value, **updates: SimpleNamespace(
                **{**vars(value), **updates}
            ),
        ),
        patch("harness.blackbox_v2.activation.lifecycle_operation_lock", return_value=nullcontext()),
        patch("harness.blackbox_v2.activation.assert_lifecycle_clear"),
        patch("harness.blackbox_v2.activation._reload_pinned_canonical", return_value=cfg),
        patch("harness.blackbox_v2.activation._verify_passed_all", return_value=passed_run),
        patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_revision_activation_preflight",
            return_value=preflight,
        ),
        patch(
            "harness.blackbox_v2.activation.activate_blackbox_revision",
            return_value=state,
        ) as activate_revision,
    ):
        result = activate_blackbox(ctx)

    assert result.status == GateStatus.PASSED
    evidence = {item.key: item.value for item in result.evidence}
    assert evidence["activation_mode"] == "revision"
    assert evidence["prior_scheme_version"] == "version-1"
    activate_revision.assert_called_once()


def test_revision_repository_atomically_switches_versions() -> None:
    engine, cfg = _revision_fixture()
    try:
        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=_sqlite_revision_upsert,
            ),
        ):
            state = _activate_revision_repository(engine, cfg)

        assert state.scheme_version == cfg.scheme_version
        assert _revision_states(engine) == [
            ("version-1", "retired"),
            ("version-2", "active"),
            ("version-shadow", "retired"),
        ]
        with engine.begin() as conn:
            assert conn.execute(
                text("SELECT status FROM t_scheme_registry")
            ).scalar_one() == "active"
    finally:
        engine.dispose()


def test_revision_repository_rolls_back_after_mid_transaction_failure() -> None:
    engine, cfg = _revision_fixture()

    def insert_then_fail(*args, **kwargs):
        _sqlite_revision_upsert(*args, **kwargs)
        raise RuntimeError("injected revision failure")

    try:
        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=insert_then_fail,
            ),
            pytest.raises(RuntimeError, match="injected revision failure"),
        ):
            _activate_revision_repository(engine, cfg)

        assert _revision_states(engine) == [
            ("version-1", "active"),
            ("version-shadow", "shadow"),
        ]
    finally:
        engine.dispose()


def test_revision_repository_rejects_latest_run_drift_before_write() -> None:
    engine, cfg = _revision_fixture()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO t_harness_runs VALUES "
                    "('hr-newer', :scheme_id, :version, 'all', 'passed', :finished)"
                ),
                {
                    "scheme_id": cfg.scheme_id,
                    "version": cfg.scheme_version,
                    "finished": datetime(2026, 8, 25, 3, 0),
                },
            )
        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn"
            ) as upsert,
            pytest.raises(RuntimeError, match="latest passed all-stage harness run changed"),
        ):
            _activate_revision_repository(engine, cfg)

        upsert.assert_not_called()
        assert _revision_states(engine) == [
            ("version-1", "active"),
            ("version-shadow", "shadow"),
        ]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "phase",
    ("prepared", "config_written", "db_committed", "unresolved"),
)
def test_lifecycle_reconcile_only_restores_previous_state(tmp_path, phase) -> None:
    from shared.blackbox_v2.lifecycle import (
        LifecycleJournal,
        LifecycleState,
        load_journal,
        pending_journals,
        reconcile_journal,
        write_journal,
    )

    previous = LifecycleState("paused", "shadow", "paused")
    active = LifecycleState("active", "active", "active")
    journal = LifecycleJournal.prepare(
        action="activate",
        scheme_id="trial_10y",
        scheme_version="version-2",
        harness_run_id="hr-candidate",
        previous=previous,
        target=active,
        operation_scope_sha256="a" * 64,
    )
    for next_phase in ("config_written", "db_committed"):
        if phase in {next_phase, "db_committed", "unresolved"}:
            journal = journal.transition(next_phase)
    if phase == "unresolved":
        journal = journal.transition("unresolved", error="injected")
    original_path = write_journal(tmp_path, journal)
    original_bytes = original_path.read_bytes()
    state = {"value": active if phase != "prepared" else previous}

    restored = reconcile_journal(
        original_path,
        config_path=tmp_path / "unused-config.yaml",
        apply_database=lambda value: state.__setitem__("value", value),
        read_state=lambda: state["value"],
        operation_scope_sha256="b" * 64,
    )

    assert restored == previous
    assert state["value"] == previous
    assert original_path.read_bytes() == original_bytes
    linked = [
        load_journal(path)
        for path in original_path.parent.glob("*.json")
        if path != original_path
    ]
    assert len(linked) == 1
    assert linked[0].phase == "verified"
    assert linked[0].target == previous
    assert pending_journals(tmp_path, "trial_10y") == []
