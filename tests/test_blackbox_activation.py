from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timezone
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
from scheduler.repository import BlackboxLifecycleIdentityAbsent


@pytest.fixture(autouse=True)
def _sqlite_datetime_codecs():
    adapter_key = (datetime, sqlite3.PrepareProtocol)
    converter_key = "TIMESTAMP"
    date_converter_key = "DATE"
    previous_adapter = sqlite3.adapters.get(adapter_key)
    previous_converter = sqlite3.converters.get(converter_key)
    previous_date_converter = sqlite3.converters.get(date_converter_key)
    sqlite3.register_adapter(
        datetime,
        lambda value: value.isoformat(sep=" "),
    )
    sqlite3.register_converter(
        "timestamp",
        lambda value: datetime.fromisoformat(value.decode("utf-8")),
    )
    sqlite3.register_converter(
        "date",
        lambda value: date.fromisoformat(value.decode("utf-8")),
    )
    try:
        yield
    finally:
        if previous_adapter is None:
            sqlite3.adapters.pop(adapter_key, None)
        else:
            sqlite3.adapters[adapter_key] = previous_adapter
        if previous_converter is None:
            sqlite3.converters.pop(converter_key, None)
        else:
            sqlite3.converters[converter_key] = previous_converter
        if previous_date_converter is None:
            sqlite3.converters.pop(date_converter_key, None)
        else:
            sqlite3.converters[date_converter_key] = previous_date_converter


def test_activation_gate_reuses_loaded_blackbox_config(tmp_path) -> None:
    from harness.gates.activate_gate import ActivationGate

    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        runtime_type="blackbox_v2",
    )
    ctx = GateContext(
        scheme_id=cfg.scheme_id,
        predict_date="activate",
        project_root=tmp_path,
        config=cfg,
    )
    expected = SimpleNamespace(status="passed")

    with patch(
        "harness.blackbox_v2.activation.activate_blackbox",
        return_value=expected,
    ) as activate:
        result = ActivationGate().run(ctx)

    assert result is expected
    activate.assert_called_once_with(ctx)


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
        conn.exec_driver_sql(
            "CREATE TABLE t_backtest_runs ("
            "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
            "data_source TEXT, status TEXT, run_mode TEXT, code_hash TEXT, "
            "config_hash TEXT, input_artifact_hash TEXT, summary TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_backtest_predictions ("
            "id INTEGER PRIMARY KEY, run_id INTEGER, scheme_id TEXT, "
            "target_tenor TEXT, "
            "horizon INTEGER, predict_date DATE, feature_date DATE, "
            "target_date DATE, label INTEGER, predicted_direction INTEGER, confidence REAL, "
            "extra TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_predictions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, "
            "backtest_run_id INTEGER, scheme_version TEXT, scheme_id TEXT, "
            "target_tenor TEXT, horizon INTEGER, predict_date DATE, "
            "feature_date DATE, target_date DATE, predicted_direction INTEGER, "
            "backtest_actual_direction INTEGER, confidence REAL, "
            "model_version TEXT, extra TEXT)"
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
        approved_by="operator",
        approved_at=datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
    )


def _initial_fixture():
    engine, cfg = _revision_fixture()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM t_scheme_registry"))
        conn.execute(text("DELETE FROM t_scheme_versions"))
    cfg.status = "paused"
    cfg.version_status = "draft"
    cfg.scheme_version = "version-1"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_backtest_runs VALUES "
                "(42, 'bbv2-test', :scheme_id, "
                "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                ":code_hash, :config_hash, 'snapshot-1', :summary)"
            ),
            {
                "scheme_id": cfg.scheme_id,
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
                "summary": (
                    '{"scheme_version":"version-1",'
                    '"manifest_hash":"' + cfg.manifest_hash + '",'
                    '"script_validator_policy_digest":"validator-digest",'
                    '"data_snapshot_id":"snapshot-1",'
                    '"generation_id":"generation-1",'
                    '"runtime_profile":"blackbox-v2-v1",'
                    '"environment_fingerprint":"' + ("e" * 64) + '"}'
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_backtest_predictions VALUES "
                "(1, 42, :scheme_id, '10Y', 1, '2026-08-24', '2026-08-21', "
                "'2026-08-25', 1, 1, 0.8, '{}')"
            ),
            {"scheme_id": cfg.scheme_id},
        )
    return engine, cfg


def _initial_backtest_evidence(cfg) -> dict[str, object]:
    return {
        "backtest_run_id": 42,
        "backtest_benchmark_id": "bbv2-test",
        "backtest_data_snapshot_id": "snapshot-1",
        "backtest_generation_id": "generation-1",
        "backtest_runtime_profile": "blackbox-v2-v1",
        "backtest_environment_fingerprint": "e" * 64,
        "backtest_code_hash": cfg.code_hash,
        "backtest_config_hash": cfg.config_hash,
        "backtest_manifest_hash": cfg.manifest_hash,
        "backtest_validator_policy_digest": "validator-digest",
    }


def _sqlite_initial_registry_sync(conn, schemes, *, effective_statuses) -> None:
    cfg = list(schemes)[0]
    registry_id = f"{cfg.scheme_id}__h{cfg.horizon}__{cfg.tenors[0]}"
    conn.execute(
        text(
            "INSERT INTO t_scheme_registry VALUES "
            "(:registry_id, :scheme_id, :name, :description, :horizon, "
            ":task_type, 'blackbox_v2', :tenors, :frequency, :target_tenor, "
            ":cron, :timezone, :status, :deployed_at)"
        ),
        {
            "registry_id": registry_id,
            "scheme_id": cfg.scheme_id,
            "name": cfg.name,
            "description": cfg.description,
            "horizon": cfg.horizon,
            "task_type": cfg.task_type,
            "tenors": '["10Y"]',
            "frequency": cfg.frequency,
            "target_tenor": cfg.tenors[0],
            "cron": cfg.schedule.cron,
            "timezone": cfg.schedule.timezone,
            "status": effective_statuses[registry_id],
            "deployed_at": datetime(2026, 8, 25, 2, 0),
        },
    )


def test_initial_activation_uses_one_atomic_repository_call(tmp_path) -> None:
    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        scheme_version="version-1",
        status="paused",
        version_status="draft",
        runtime_type="blackbox_v2",
        runtime_profile="blackbox-v2-v1",
        path=Path(tmp_path),
    )
    passed_backtest = SimpleNamespace(
        backtest_run_id=42,
        benchmark_id="bbv2-test",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        generation_id="generation-1",
        data_snapshot_id="snapshot-1",
        code_hash="c" * 64,
        config_hash="f" * 64,
        manifest_hash="m" * 64,
        script_validator_policy_digest="validator-digest",
    )
    active_state = SimpleNamespace(
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
        config=cfg,
        operation=operation,
        engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
    )

    with (
        patch(
            "harness.blackbox_v2.activation.replace",
            side_effect=lambda value, **updates: SimpleNamespace(
                **{**vars(value), **updates}
            ),
        ),
        patch(
            "harness.blackbox_v2.activation._reload_pinned_canonical",
            return_value=cfg,
        ),
        patch(
            "harness.blackbox_v2.activation._verify_passed_backtest",
            return_value=passed_backtest,
        ),
        patch(
            "harness.blackbox_v2.activation._environment_fingerprint",
            return_value="e" * 64,
        ),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_lifecycle_state",
            side_effect=BlackboxLifecycleIdentityAbsent(),
        ),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_revision_activation_preflight",
            side_effect=ValueError("no prior active identity"),
        ),
        patch(
            "harness.blackbox_v2.activation.activate_blackbox_initial",
            return_value=active_state,
        ) as activate_initial,
    ):
        result = activate_blackbox(ctx)

    assert result.status == GateStatus.PASSED
    assert {item.key: item.value for item in result.evidence}["identity_created"] is True
    activate_initial.assert_called_once()


def test_existing_non_active_identity_is_not_promoted(tmp_path) -> None:
    cfg = SimpleNamespace(
        scheme_id="trial_10y",
        scheme_version="version-1",
        status="paused",
        version_status="draft",
        runtime_type="blackbox_v2",
        runtime_profile="blackbox-v2-v1",
        path=Path(tmp_path),
    )
    passed_backtest = SimpleNamespace(
        backtest_run_id=42,
        benchmark_id="bbv2-test",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        generation_id="generation-1",
        data_snapshot_id="snapshot-1",
    )
    existing_state = SimpleNamespace(
        version_status="draft",
        registry_status="paused",
    )
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
        config=cfg,
        operation=operation,
        engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
    )

    with (
        patch(
            "harness.blackbox_v2.activation.replace",
            side_effect=lambda value, **updates: SimpleNamespace(
                **{**vars(value), **updates}
            ),
        ),
        patch("harness.blackbox_v2.activation._reload_pinned_canonical", return_value=cfg),
        patch(
            "harness.blackbox_v2.activation._verify_passed_backtest",
            return_value=passed_backtest,
        ),
        patch(
            "harness.blackbox_v2.activation._environment_fingerprint",
            return_value="e" * 64,
        ),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_revision_activation_preflight",
            side_effect=ValueError("no prior active identity"),
        ),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_lifecycle_state",
            return_value=existing_state,
        ),
        patch("harness.blackbox_v2.activation.activate_blackbox_initial") as activate_initial,
        patch("harness.blackbox_v2.activation.activate_blackbox_revision") as activate_revision,
    ):
        result = activate_blackbox(ctx)

    assert result.status == GateStatus.BLOCKED
    assert result.errors == [
        "existing non-active Blackbox identity cannot be promoted: got=draft+paused"
    ]
    activate_initial.assert_not_called()
    activate_revision.assert_not_called()


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
    passed_backtest = SimpleNamespace(
        backtest_run_id=42,
        benchmark_id="bbv2-test",
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
        patch("harness.blackbox_v2.activation._reload_pinned_canonical", return_value=cfg),
        patch(
            "harness.blackbox_v2.activation._verify_passed_backtest",
            return_value=passed_backtest,
        ),
        patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
        patch(
            "harness.blackbox_v2.activation.read_blackbox_lifecycle_state",
            side_effect=BlackboxLifecycleIdentityAbsent(),
        ),
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
                "scheduler.repository._blackbox_activation_advisory_lock",
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


def test_initial_repository_atomically_creates_active_identity() -> None:
    from scheduler.repository import activate_blackbox_initial

    engine, cfg = _initial_fixture()
    try:
        with (
            patch(
                "scheduler.repository._blackbox_activation_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=_sqlite_revision_upsert,
            ),
            patch(
                "scheduler.repository._sync_scheme_registry_conn",
                side_effect=_sqlite_initial_registry_sync,
            ),
        ):
            state = activate_blackbox_initial(
                engine,
                cfg,
                **_initial_backtest_evidence(cfg),
                approved_by="operator",
                approved_at=datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
            )

        assert state.version_status == "active"
        assert _revision_states(engine) == [("version-1", "active")]
        with engine.begin() as conn:
            assert conn.execute(
                text("SELECT status FROM t_scheme_registry")
            ).scalar_one() == "active"
            assert conn.execute(
                text("SELECT COUNT(*) FROM t_scheme_predictions")
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_initial_repository_rolls_back_if_registry_insert_fails() -> None:
    from scheduler.repository import activate_blackbox_initial

    engine, cfg = _initial_fixture()
    try:
        with (
            patch(
                "scheduler.repository._blackbox_activation_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=_sqlite_revision_upsert,
            ),
            patch(
                "scheduler.repository._sync_scheme_registry_conn",
                side_effect=RuntimeError("injected registry failure"),
            ),
            pytest.raises(RuntimeError, match="injected registry failure"),
        ):
            activate_blackbox_initial(
                engine,
                cfg,
                **_initial_backtest_evidence(cfg),
                approved_by="operator",
                approved_at=datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
            )

        assert _revision_states(engine) == []
        with engine.begin() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_registry")).scalar_one() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_predictions")).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("tamper_sql", "message"),
    [
        (
            "UPDATE t_backtest_runs SET input_artifact_hash = 'changed'",
            "backtest identity mismatch",
        ),
        (
            "UPDATE t_backtest_predictions SET scheme_id = 'other'",
            "scheme identity mismatch",
        ),
    ],
)
def test_initial_repository_revalidates_locked_backtest_evidence(
    tamper_sql: str,
    message: str,
) -> None:
    from scheduler.repository import activate_blackbox_initial

    engine, cfg = _initial_fixture()
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(tamper_sql)
        with (
            patch(
                "scheduler.repository._blackbox_activation_advisory_lock",
                return_value=nullcontext(),
            ),
            pytest.raises(ValueError, match=message),
        ):
            activate_blackbox_initial(
                engine,
                cfg,
                **_initial_backtest_evidence(cfg),
                approved_by="operator",
                approved_at=datetime(
                    2026, 8, 25, 2, 0, tzinfo=timezone.utc
                ),
            )

        with engine.begin() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM t_scheme_predictions")
            ).scalar_one() == 0
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
                "scheduler.repository._blackbox_activation_advisory_lock",
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
