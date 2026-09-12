from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from harness.native_successor_migration import (
    load_native_successor_waves,
    select_native_successor_wave,
    validate_control_plane_evidence,
)
from scheduler.repository import (
    NativeSuccessorBacktestEvidence,
    NativeSuccessorTarget,
    _validate_native_successor_equivalence_evidence,
    _validate_native_successor_target_configs,
    apply_native_successor_migration,
    canonical_native_successor_plan,
    native_successor_plan_sha256,
    read_native_successor_migration_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _full_request(scheme_id: str) -> dict[str, str]:
    return {
        "request_id": f"{scheme_id}:2026-05-22:2026-05-22:2026-05-29",
        "predict_date": "2026-05-22",
        "feature_date": "2026-05-22",
        "target_date": "2026-05-29",
        "daily_cutoff_key": "2026-05-22",
        "weekly_cutoff_key": "202621",
        "monthly_cutoff_key": "202605",
    }


def test_locked_mapping_contains_26_to_30_identities() -> None:
    waves = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    targets = [target for wave in waves.values() for target in wave.targets]
    assert len({target.old_base_scheme_id for target in targets}) == 26
    assert len({target.new_base_scheme_id for target in targets}) == 30
    assert len(targets) == 30
    assert waves["W1A"].ecs_mode == "active"
    assert waves["W4A"].ecs_mode == "not_deployed_mac3_only"
    with pytest.raises(ValueError, match="requires --old-scheme-id"):
        select_native_successor_wave(waves, "W3C")
    selected = select_native_successor_wave(
        waves,
        "W3C",
        old_scheme_id="liwei_0616_5y_auc_static_all_k3_div_k10",
    )
    assert len(selected.targets) == 1


@pytest.mark.parametrize(("task_type", "old_horizon", "old_rule"), [
    ("T+1", 1, None), ("T+5", 5, None), ("weekly_point", 6, "next_week_last"),
])
def test_mapping_normalizes_native_rule_to_blackbox_task_contract(
    task_type: str, old_horizon: int, old_rule: str | None,
) -> None:
    from shared.task_specs import TASK_COMBINATIONS

    horizon, rule, frequency = TASK_COMBINATIONS[task_type]
    old = SimpleNamespace(scheme_id="native_contract_fixture", runtime_type="native_adapter",
                          task_type=task_type, horizon=old_horizon, target_rule=old_rule,
                          frequency=frequency, tenors=["5Y", "10Y"])
    targets = tuple(NativeSuccessorTarget(
        old_base_scheme_id=old.scheme_id, new_base_scheme_id=f"blackbox_contract_{tenor.lower()}",
        task_type=task_type, target_tenor=tenor, target_rule=old_rule,
        old_horizon=old_horizon, new_horizon=horizon,
    ) for tenor in old.tenors)
    new_configs = {target.new_base_scheme_id: SimpleNamespace(
        scheme_id=target.new_base_scheme_id, runtime_type="blackbox_v2", task_type=task_type,
        horizon=horizon, target_rule=rule, frequency=frequency, tenors=[target.target_tenor],
    ) for target in targets}
    _validate_native_successor_target_configs(
        targets,
        old_configs={old.scheme_id: old},
        new_configs=new_configs,
    )
    with pytest.raises(ValueError, match="every Native target"):
        _validate_native_successor_target_configs(
            targets[:1], old_configs={old.scheme_id: old}, new_configs=new_configs,
        )


def _fixture():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    old = SimpleNamespace(
        scheme_id="native_multi",
        scheme_version="native-v1",
        runtime_type="native_adapter",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        task_type="weekly_point",
        target_rule="next_week_last",
        horizon=6,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    new = SimpleNamespace(
        scheme_id="native_multi_bbv2",
        scheme_version="bb-v1",
        runtime_type="blackbox_v2",
        algorithm_version="1.0.0",
        contract_version="2.0",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
        code_hash="d" * 64,
        config_hash="f" * 64,
        manifest_hash="1" * 64,
        task_type="weekly_point",
        target_rule="target_week_end_yield_vs_feature_week_end_yield",
        horizon=1,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    target = NativeSuccessorTarget(
        old_base_scheme_id=old.scheme_id,
        new_base_scheme_id=new.scheme_id,
        task_type="weekly_point",
        target_tenor="5Y",
        target_rule="next_week_last",
        old_horizon=6,
        new_horizon=1,
    )
    evidence = NativeSuccessorBacktestEvidence(
        backtest_run_id=42,
        benchmark_id="bbv2-migration",
        data_snapshot_id="snapshot-1",
        generation_id="generation-1",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        code_hash=new.code_hash,
        config_hash=new.config_hash,
        manifest_hash=new.manifest_hash,
        validator_policy_digest="validator-1",
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE t_schema_migrations ("
            "version INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO t_schema_migrations (version, state) VALUES (24, 'APPLIED')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_versions ("
            "scheme_id TEXT NOT NULL, scheme_version TEXT NOT NULL, "
            "runtime_type TEXT, algorithm_version TEXT, contract_version TEXT, "
            "runtime_profile TEXT, environment_fingerprint TEXT, "
            "data_snapshot_id TEXT, code_hash TEXT, config_hash TEXT, "
            "manifest_hash TEXT, git_commit TEXT, status TEXT, created_by TEXT, "
            "approved_by TEXT, approved_at TEXT, "
            "PRIMARY KEY (scheme_id, scheme_version))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_registry ("
            "scheme_id TEXT PRIMARY KEY, base_scheme_id TEXT, name TEXT, owner TEXT, "
            "description TEXT, horizon INTEGER, task_type TEXT, runtime_type TEXT, "
            "tenors TEXT, frequency TEXT, target_tenor TEXT, schedule_cron TEXT, "
            "schedule_timezone TEXT, status TEXT, deployed_at TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_runs ("
            "run_id INTEGER PRIMARY KEY, scheme_id TEXT, predict_date TEXT, "
            "status TEXT)"
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
            "target_tenor TEXT, horizon INTEGER, predict_date TEXT, feature_date TEXT, "
            "target_date TEXT, label INTEGER, predicted_direction INTEGER, "
            "confidence REAL, source_row TEXT, extra TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_predictions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, "
            "backtest_run_id INTEGER, scheme_version TEXT, scheme_id TEXT, "
            "target_tenor TEXT, horizon INTEGER, predict_date TEXT, feature_date TEXT, "
            "target_date TEXT, predicted_direction INTEGER, "
            "backtest_actual_direction INTEGER, confidence REAL, "
            "model_version TEXT, extra TEXT)"
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, :scheme_version, 'native_adapter', :code_hash, "
                ":config_hash, :manifest_hash, 'active')"
            ),
            vars(old),
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "('native_multi__h6__5Y', :scheme_id, 'Old display', 'owner-a', "
                "'old description', 6, 'weekly_point', 'native_adapter', '[\"5Y\"]', "
                "'weekly', '5Y', '0 8 * * 5', 'Asia/Shanghai', 'active', '2026-01-01')"
            ),
            {"scheme_id": old.scheme_id},
        )
        summary = {
            "scheme_version": new.scheme_version,
            "manifest_hash": new.manifest_hash,
            "script_validator_policy_digest": evidence.validator_policy_digest,
            "data_snapshot_id": evidence.data_snapshot_id,
            "generation_id": evidence.generation_id,
            "runtime_profile": evidence.runtime_profile,
            "environment_fingerprint": evidence.environment_fingerprint,
            "request_count": 1,
            "backtest_start_date": "2025-01-01",
            "target_date_before": "2026-06-01",
            "actual_predict_date_min": "2026-05-22",
            "actual_predict_date_max": "2026-05-22",
            "actual_target_date_min": "2026-05-29",
            "actual_target_date_max": "2026-05-29",
        }
        conn.execute(
            text(
                "INSERT INTO t_backtest_runs VALUES "
                "(42, :benchmark_id, :scheme_id, "
                "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                ":code_hash, :config_hash, :input_hash, :summary)"
            ),
            {
                "benchmark_id": evidence.benchmark_id,
                "scheme_id": new.scheme_id,
                "code_hash": new.code_hash,
                "config_hash": new.config_hash,
                "input_hash": evidence.data_snapshot_id,
                "summary": __import__("json").dumps(summary),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_backtest_predictions "
                "(id, run_id, scheme_id, target_tenor, horizon, predict_date, "
                "feature_date, target_date, label, predicted_direction, confidence, "
                "source_row, extra) VALUES "
                "(1, 42, :scheme_id, '5Y', 1, '2026-05-22', '2026-05-22', "
                "'2026-05-29', 1, -1, NULL, :source_row, '{}')"
            ),
            {
                "scheme_id": new.scheme_id,
                "source_row": json.dumps(
                    _full_request(new.scheme_id) | {"actual": {}}
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_predictions "
                "(run_id, backtest_run_id, scheme_version, scheme_id, "
                "target_tenor, horizon, "
                "predict_date, feature_date, target_date, predicted_direction, "
                "model_version, extra) VALUES "
                "(NULL, 99, :scheme_version, :scheme_id, '5Y', 6, "
                "'2026-05-22', '2026-05-22', '2026-05-29', 1, "
                ":scheme_version, '{}')"
            ),
            {
                "scheme_id": old.scheme_id,
                "scheme_version": old.scheme_version,
            },
        )
    return engine, old, new, target, evidence


def _sqlite_upsert(conn, cfg, *, trusted_status, approved_by, approved_at):
    conn.execute(
        text(
            "INSERT INTO t_scheme_versions "
            "(scheme_id, scheme_version, runtime_type, algorithm_version, "
            "contract_version, runtime_profile, environment_fingerprint, "
            "data_snapshot_id, code_hash, config_hash, manifest_hash, status, "
            "created_by, approved_by, approved_at) VALUES "
            "(:scheme_id, :scheme_version, :runtime_type, :algorithm_version, "
            ":contract_version, :runtime_profile, :environment_fingerprint, "
            ":data_snapshot_id, :code_hash, :config_hash, :manifest_hash, :status, "
            "'test', :approved_by, :approved_at) "
            "ON CONFLICT(scheme_id, scheme_version) DO UPDATE SET "
            "status=excluded.status, approved_by=excluded.approved_by, "
            "approved_at=excluded.approved_at"
        ),
        {
            **vars(cfg),
            "status": trusted_status,
            "approved_by": approved_by,
            "approved_at": approved_at.isoformat(),
        },
    )
    return cfg.scheme_version


def _inputs(old, new, target, evidence):
    control_plane_evidence = {
            "schema_version": "native-successor-control-plane-evidence-v1",
            "wave": "W-test",
            "deployment_target": "aliyun-gray",
            "host": {
                "hostname": "isolated-test",
                "current_link": "/opt/bond-factor-lab/current",
                "release_root": "/opt/bond-factor-lab/releases/test",
            },
            "release": {
                "current_commit": "a" * 40,
                "archive_sha256": "b" * 64,
                "install_record_sha256": "c" * 64,
                "comparator_source_sha256": "6" * 64,
            },
            "databridge": {
                "generation_id": "generation-1",
                "data_snapshot_id": "snapshot-1",
                "business_digest": "d" * 64,
                "files": {
                    filename: str(index) * 64
                    for index, filename in enumerate(
                        (
                            "daily_output.csv",
                            "weekly_output.csv",
                            "monthly_output.csv",
                            "api_wind_date.csv",
                            "factor_catalog.csv",
                        ),
                        start=1,
                    )
                },
            },
            "native_runtime": {
                "conda_env": "forecast_env",
                "environment_fingerprint": "8" * 64,
            },
            "scheduler": {
                "control_plane": "systemd_one_shot",
                "cadence": "weekly",
                "timer_fenced": True,
                "unique_writer": True,
                "installed_unit_hash": "e" * 64,
                "observed_state_sha256": "a" * 64,
            },
            "dashboard": {
                "active_scheme_set_sha256": "f" * 64,
                "response_sha256": "1" * 64,
            },
            "captured_at": "2026-09-05T00:00:00Z",
            "verified_by": "native-successor-controlled-capture-v1",
        }
    control_plane_evidence["_capture_sha256"] = hashlib.sha256(
        json.dumps(
            control_plane_evidence,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    full_request = _full_request(target.new_base_scheme_id)
    request = {
        key: full_request[key]
        for key in ("request_id", "predict_date", "feature_date", "target_date")
    }
    request_sha256 = hashlib.sha256(
        canonical_native_successor_plan({"requests": [request]}).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_native_successor_plan(
            {"results": [{**request, "predicted_direction": -1}]}
        ).encode("utf-8")
    ).hexdigest()
    equivalence_evidence = {
        "schema_version": "native-successor-equivalence-v2",
        "wave": "W-test",
        "producer": {
            "tool": "native-successor-controlled-comparator",
            "tool_version": "2",
            "comparator_source_sha256": "6" * 64,
            "generated_at": "2026-09-05T00:00:00Z",
        },
        "runtime_environment_fingerprint": evidence.environment_fingerprint,
        "targets": [
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "task_type": target.task_type,
                "target_tenor": target.target_tenor,
                "old_horizon": target.old_horizon,
                "new_horizon": target.new_horizon,
                "old_code_hash": old.code_hash,
                "new_code_hash": new.code_hash,
                "input_identity": {
                    "generation_id": evidence.generation_id,
                    "data_snapshot_id": evidence.data_snapshot_id,
                    "data_files_sha256": dict(
                        control_plane_evidence["databridge"]["files"]
                    ),
                },
                "native_runtime_profile": "native",
                "native_runtime_environment_fingerprint": "8" * 64,
                "request_artifact_sha256": hashlib.sha256(
                    canonical_native_successor_plan(
                        {"requests": [full_request]}
                    ).encode("utf-8")
                ).hexdigest(),
                "request_sha256": request_sha256,
                "request_count": 1,
                "native_result_sha256": result_sha256,
                "successor_result_sha256": result_sha256,
                "request_id_mismatch_count": 0,
                "predict_date_mismatch_count": 0,
                "feature_date_mismatch_count": 0,
                "target_date_mismatch_count": 0,
                "direction_mismatch_count": 0,
            }
        ],
    }
    return {
        "wave": "W-test",
        "targets": (target,),
        "old_configs": {old.scheme_id: old},
        "new_configs": {new.scheme_id: new},
        "backtests": {new.scheme_id: evidence},
        "equivalence_evidence": equivalence_evidence,
        "control_plane_evidence": control_plane_evidence,
    }


def _apply_inputs(inputs, *, recaptured=None):
    payload = dict(inputs)
    control_plane = payload.pop("control_plane_evidence")
    payload["control_plane_evidence_reader"] = lambda: (
        recaptured if recaptured is not None else control_plane
    )
    return payload


def _mixed_input_fixture():
    engine, old, new, target, evidence = _fixture()
    old.tenors = ["5Y", "10Y"]
    other = deepcopy(new)
    other.scheme_id = "native_multi_10y_bbv2"
    other.tenors = ["10Y"]
    other_target = replace(
        target, new_base_scheme_id=other.scheme_id, target_tenor="10Y",
    )
    other_evidence = replace(
        evidence, backtest_run_id=43, benchmark_id="bbv2-migration-10y",
    )
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO t_scheme_registry SELECT "
            "'native_multi__h6__10Y', base_scheme_id, name, owner, description, "
            "horizon, task_type, runtime_type, '[\"10Y\"]', frequency, '10Y', "
            "schedule_cron, schedule_timezone, status, deployed_at "
            "FROM t_scheme_registry WHERE target_tenor='5Y'"
        ))
        conn.execute(text(
            "INSERT INTO t_backtest_runs SELECT 43, :benchmark_id, :scheme_id, "
            "data_source, status, run_mode, code_hash, config_hash, "
            "input_artifact_hash, summary FROM t_backtest_runs WHERE id=42"
        ), {"benchmark_id": other_evidence.benchmark_id, "scheme_id": other.scheme_id})
        conn.execute(text(
            "INSERT INTO t_backtest_predictions SELECT 2, 43, :scheme_id, '10Y', "
            "horizon, predict_date, feature_date, target_date, label, "
            "predicted_direction, confidence, :source_row, extra "
            "FROM t_backtest_predictions WHERE id=1"
        ), {
            "scheme_id": other.scheme_id,
            "source_row": json.dumps(_full_request(other.scheme_id) | {"actual": {}}),
        })
        conn.execute(text(
            "INSERT INTO t_scheme_predictions SELECT NULL, run_id, backtest_run_id, "
            "scheme_version, scheme_id, '10Y', horizon, predict_date, feature_date, "
            "target_date, predicted_direction, backtest_actual_direction, confidence, "
            "model_version, extra FROM t_scheme_predictions WHERE target_tenor='5Y'"
        ))
    inputs = _inputs(old, new, target, evidence)
    other_inputs = _inputs(old, other, other_target, other_evidence)
    inputs["targets"] = (target, other_target)
    inputs["new_configs"].update(other_inputs["new_configs"])
    inputs["backtests"].update(other_inputs["backtests"])
    inputs["equivalence_evidence"]["targets"].extend(
        other_inputs["equivalence_evidence"]["targets"]
    )
    frozen = inputs["equivalence_evidence"]["targets"][0]
    frozen["input_identity"]["generation_id"] = "frozen-algorithm-generation"
    frozen["input_identity"]["data_snapshot_id"] = "frozen-algorithm-snapshot"
    frozen["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    frozen["request_artifact_sha256"] = "b" * 64
    frozen["native_result_sha256"] = frozen["successor_result_sha256"] = "c" * 64
    return engine, inputs


def _rehash_control_plane(control_plane):
    control_plane.pop("_capture_sha256", None)
    control_plane["_capture_sha256"] = hashlib.sha256(
        json.dumps(
            control_plane,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return control_plane


def test_mysql_preflight_uses_read_only_repeatable_read_snapshot() -> None:
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"),
        connect=MagicMock(return_value=context),
    )
    with patch(
        "scheduler.repository._native_successor_migration_plan_conn",
        return_value={"schema_version": "test"},
    ):
        plan = read_native_successor_migration_plan(
            engine,
            wave="W-test",
            targets=(),
            old_configs={},
            new_configs={},
            backtests={},
            equivalence_evidence={},
            control_plane_evidence={"bound": True},
        )
    assert plan == {"schema_version": "test"}
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
    connection.rollback.assert_called_once_with()


def test_control_plane_evidence_requires_fenced_matching_cadence() -> None:
    engine, old, new, target, evidence = _fixture()
    del engine
    control = _inputs(
        old,
        new,
        target,
        evidence,
    )["control_plane_evidence"]
    wave = SimpleNamespace(wave="W-test", cadence="weekly", ecs_mode="active")
    now = datetime(2026, 9, 5, 0, 10, tzinfo=timezone.utc)
    assert validate_control_plane_evidence(control, wave=wave, now=now) == control
    invalid = __import__("copy").deepcopy(control)
    invalid["scheduler"]["timer_fenced"] = False
    _rehash_control_plane(invalid)
    with pytest.raises(ValueError, match="timer to be fenced"):
        validate_control_plane_evidence(invalid, wave=wave, now=now)
    with pytest.raises(ValueError, match="older than 15 minutes"):
        validate_control_plane_evidence(
            control,
            wave=wave,
            now=datetime(2026, 9, 5, 0, 16, tzinfo=timezone.utc),
        )
    future = __import__("copy").deepcopy(control)
    future["captured_at"] = "2026-09-05T00:11:00Z"
    _rehash_control_plane(future)
    with pytest.raises(ValueError, match="must not be in the future"):
        validate_control_plane_evidence(future, wave=wave, now=now)


def test_cutover_rollback_and_recutover_reuse_exact_facts() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    assert plan["action"] == "cutover"
    assert set(plan["database"]) == {
        "database_identity_sha256",
        "schema_migration_version",
    }
    assert plan["database"]["schema_migration_version"] == 24
    digest = native_successor_plan_sha256(plan)
    recaptured = __import__("copy").deepcopy(inputs["control_plane_evidence"])
    recaptured["captured_at"] = "2026-09-05T00:00:01Z"
    _rehash_control_plane(recaptured)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=digest,
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs, recaptured=recaptured),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT status FROM t_scheme_versions WHERE scheme_id='native_multi'")
        ).scalar_one() == "retired"
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi'"
            )
        ).scalar_one() == 1
        successor = conn.execute(
            text(
                "SELECT name, owner, horizon, runtime_type, status "
                "FROM t_scheme_registry WHERE base_scheme_id='native_multi_bbv2'"
            )
        ).one()
        assert tuple(successor) == (
            "Old display",
            "owner-a",
            1,
            "blackbox_v2",
            "active",
        )

    inputs["control_plane_evidence"]["databridge"][
        "generation_id"
    ] = "generation-2"
    inputs["control_plane_evidence"]["databridge"][
        "data_snapshot_id"
    ] = "snapshot-2"
    rollback_plan = read_native_successor_migration_plan(engine, **inputs)
    assert rollback_plan["action"] == "rollback"
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="rollback",
            expected_plan_sha256=native_successor_plan_sha256(rollback_plan),
            approved_by="operator-b",
            approved_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1
        assert conn.execute(
            text(
                "SELECT status FROM t_scheme_registry "
                "WHERE base_scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == "archived"

    retry_plan = read_native_successor_migration_plan(engine, **inputs)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(retry_plan),
            approved_by="operator-c",
            approved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1


def test_stale_plan_hash_and_mid_transaction_failure_leave_state_unchanged() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    with pytest.raises(RuntimeError, match="plan changed"):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256="0" * 64,
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    plan = read_native_successor_migration_plan(engine, **inputs)
    with (
        patch(
            "scheduler.repository._upsert_scheme_version_conn",
            side_effect=_sqlite_upsert,
        ),
        patch(
            "scheduler.repository._upsert_successor_registry_from_old_conn",
            side_effect=RuntimeError("forced failure"),
        ),
        pytest.raises(RuntimeError, match="forced failure"),
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT status FROM t_scheme_versions WHERE scheme_id='native_multi'")
        ).scalar_one() == "active"
        assert conn.execute(
            text("SELECT COUNT(*) FROM t_scheme_versions WHERE scheme_id='native_multi_bbv2'")
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi'"
            )
        ).scalar_one() == 1


def test_cutover_preserves_historical_active_native_version_rows() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, 'historical-native-v0', 'native_adapter', "
                "'old-code', 'old-config', NULL, 'active')"
            ),
            {"scheme_id": old.scheme_id},
        )
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        statuses = dict(
            conn.execute(
                text(
                    "SELECT scheme_version, status FROM t_scheme_versions "
                    "WHERE scheme_id = :scheme_id"
                ),
                {"scheme_id": old.scheme_id},
            ).all()
        )
    assert statuses == {
        "historical-native-v0": "active",
        old.scheme_version: "retired",
    }


def test_preflight_rejects_other_active_successor_version() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, 'other-bb-version', 'blackbox_v2', "
                "'other-code', 'other-config', 'other-manifest', 'active')"
            ),
            {"scheme_id": new.scheme_id},
        )
    with pytest.raises(RuntimeError, match="unexpected active exact version"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_registry_schedule_drift_blocks_cutover() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_scheme_registry SET schedule_cron = '5 8 * * 5' "
                "WHERE base_scheme_id = :scheme_id"
            ),
            {"scheme_id": old.scheme_id},
        )
    with pytest.raises(RuntimeError, match="Registry identity mismatch"):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )


@pytest.mark.parametrize(
    ("version", "state"),
    [(23, "APPLIED"), (24, "APPLYING"), (25, "APPLIED")],
)
def test_preflight_rejects_non_024_or_incomplete_schema(
    version: int,
    state: str,
) -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM t_schema_migrations"))
        conn.execute(
            text(
                "INSERT INTO t_schema_migrations (version, state) "
                "VALUES (:version, :state)"
            ),
            {"version": version, "state": state},
        )
    with pytest.raises(RuntimeError, match="schema migration 024"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_preflight_binds_backtest_to_current_databridge_identity() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["control_plane_evidence"]["databridge"][
        "generation_id"
    ] = "different-generation"
    with pytest.raises(RuntimeError, match="DataBridge identity differs"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_requires_zero_difference_equivalence_receipt() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0][
        "direction_mismatch_count"
    ] = 1
    with pytest.raises(RuntimeError, match="mismatch count is non-zero"):
        read_native_successor_migration_plan(engine, **inputs)


def test_mixed_equivalence_inputs_survive_atomic_cutover_rollback_and_recutover() -> None:
    engine, inputs = _mixed_input_fixture()
    receipt = inputs["equivalence_evidence"]
    plan = read_native_successor_migration_plan(engine, **inputs)
    assert {item["target_tenor"]: item for item in plan["equivalence"]["targets"]} == {
        item["target_tenor"]: item for item in receipt["targets"]
    }
    assert {item["generation_id"] for item in plan["backtests"]} == {"generation-1"}
    approved_digest = native_successor_plan_sha256(plan)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE t_backtest_predictions SET source_row=:source_row WHERE run_id=42"),
            {"source_row": json.dumps(_full_request("native_multi_bbv2") | {
                "weekly_cutoff_key": "202620", "actual": {},
            })},
        )
    changed_plan = read_native_successor_migration_plan(engine, **inputs)
    assert native_successor_plan_sha256(changed_plan) != approved_digest
    with pytest.raises(RuntimeError, match="plan.*(changed|mismatch|differs)"):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=approved_digest, approved_by="test-operator",
            approved_at=datetime.now(timezone.utc),
        )
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_registry WHERE status='active'"
        )).scalar_one() == 2
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id='native_multi_bbv2'"
        )).scalar_one() == 0
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER reject_second_successor BEFORE INSERT ON t_scheme_registry "
            "WHEN NEW.runtime_type='blackbox_v2' AND NEW.target_tenor='10Y' "
            "BEGIN SELECT RAISE(ABORT, 'forced second target failure'); END"
        )
    with (
        patch("scheduler.repository._upsert_scheme_version_conn", side_effect=_sqlite_upsert),
        pytest.raises(IntegrityError, match="forced second target failure"),
    ):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(changed_plan),
            approved_by="test-operator", approved_at=datetime.now(timezone.utc),
        )
    with engine.begin() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_registry WHERE status='active'"
        )).scalar_one() == 2
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_versions WHERE runtime_type='blackbox_v2'"
        )).scalar_one() == 0
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id<>'native_multi'"
        )).scalar_one() == 0
        conn.exec_driver_sql("DROP TRIGGER reject_second_successor")
    for action in ("cutover", "rollback", "cutover"):
        plan = read_native_successor_migration_plan(engine, **inputs)
        assert plan["action"] == action
        with patch(
            "scheduler.repository._upsert_scheme_version_conn",
            side_effect=_sqlite_upsert,
        ):
            apply_native_successor_migration(
                engine,
                **_apply_inputs(inputs),
                action=action,
                expected_plan_sha256=native_successor_plan_sha256(plan),
                approved_by="test-operator",
                approved_at=datetime.now(timezone.utc),
            )
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT runtime_type FROM t_scheme_registry WHERE status='active'"
            )).scalars().all() == [
                "native_adapter" if action == "rollback" else "blackbox_v2"
            ] * 2
            assert conn.execute(text(
                "SELECT scheme_id, target_tenor, predicted_direction "
                "FROM t_scheme_predictions ORDER BY scheme_id, target_tenor"
            )).all() == [
                ("native_multi", "10Y", 1),
                ("native_multi", "5Y", 1),
                ("native_multi_10y_bbv2", "10Y", -1),
                ("native_multi_bbv2", "5Y", -1),
            ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("direction", "standardized result digests"),
        ("cutoff", "complete Request digest"),
        ("file", "differs from current DataBridge"),
        ("input_missing", "target fields"),
        ("input_field_missing", "input_identity fields"),
        ("input_file_missing", "all five DataBridge files"),
        ("profile_missing", "target fields"),
        ("profile_invalid", "runtime profile is invalid"),
        ("profile_fingerprint", "Native runtime differs"),
        ("profile_outside_family", "complete locked W3A family"),
        ("legacy_top_input", "evidence fields"),
    ],
)
def test_mixed_equivalence_inputs_fail_closed_per_target(
    change: str, message: str,
) -> None:
    engine, inputs = _mixed_input_fixture()
    receipt = inputs["equivalence_evidence"]
    same_input = receipt["targets"][1]
    if change == "direction":
        same_input["native_result_sha256"] = "a" * 64
        same_input["successor_result_sha256"] = "a" * 64
    elif change == "cutoff":
        same_input["request_artifact_sha256"] = "b" * 64
    elif change == "file":
        same_input["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    elif change == "input_missing":
        same_input.pop("input_identity")
    elif change == "input_field_missing":
        same_input["input_identity"].pop("data_snapshot_id")
    elif change == "input_file_missing":
        same_input["input_identity"]["data_files_sha256"].pop("daily_output.csv")
    elif change == "profile_missing":
        same_input.pop("native_runtime_profile")
    elif change == "profile_invalid":
        same_input["native_runtime_profile"] = "forecast_env"
    elif change == "profile_fingerprint":
        receipt["targets"][0]["native_runtime_environment_fingerprint"] = "e" * 64
    elif change == "profile_outside_family":
        same_input["native_runtime_profile"] = "blackbox-v2-v1"
        same_input["native_runtime_environment_fingerprint"] = "e" * 64
    elif change == "legacy_top_input":
        receipt.update(same_input["input_identity"])
    with pytest.raises(RuntimeError, match=message):
        read_native_successor_migration_plan(engine, **inputs)


@pytest.mark.parametrize("target_index", [0, 1])
@pytest.mark.parametrize(
    "change", ["generation", "snapshot", "file", "environment"],
)
def test_plan_authorization_binds_each_target_input_and_environment(
    target_index: int, change: str,
) -> None:
    engine, inputs = _mixed_input_fixture()
    approved_plan = read_native_successor_migration_plan(engine, **inputs)
    approved_digest = native_successor_plan_sha256(approved_plan)
    item = inputs["equivalence_evidence"]["targets"][target_index]
    if change == "generation":
        item["input_identity"]["generation_id"] = "other-generation"
    elif change == "snapshot":
        item["input_identity"]["data_snapshot_id"] = "other-snapshot"
    elif change == "file":
        item["input_identity"]["data_files_sha256"]["daily_output.csv"] = "f" * 64
    elif change == "environment":
        item["native_runtime_environment_fingerprint"] = "9" * 64
    # 同输入文件或不匹配环境会先拒绝；其余合法变化必须改变授权摘要。
    if change == "environment" or (change == "file" and target_index == 1):
        message = "Native runtime differs|differs from current DataBridge"
    else:
        changed_plan = read_native_successor_migration_plan(engine, **inputs)
        assert native_successor_plan_sha256(changed_plan) != approved_digest
        message = "plan changed"
    with pytest.raises(RuntimeError, match=message):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=approved_digest, approved_by="test-operator",
            approved_at=datetime.now(timezone.utc),
        )


@pytest.mark.parametrize("scope_change", [None, "wave", "partial", "target", "environment"])
def test_reviewed_reference_runtime_is_bound_to_locked_family(scope_change: str | None) -> None:
    targets = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )["W3A"].targets
    engine, old_template, new_template, _, backtest = _fixture()
    inputs = None
    fact_rows = {}
    for target in targets:
        old = SimpleNamespace(**(vars(old_template) | {
            "scheme_id": target.old_base_scheme_id,
        }))
        new = SimpleNamespace(**(vars(new_template) | {
            "scheme_id": target.new_base_scheme_id,
        }))
        item_inputs = _inputs(old, new, target, backtest)
        if inputs is None:
            inputs = item_inputs
        else:
            for key in ("old_configs", "new_configs", "backtests"):
                inputs[key].update(item_inputs[key])
            inputs["equivalence_evidence"]["targets"].extend(
                item_inputs["equivalence_evidence"]["targets"]
            )
        full_request = _full_request(new.scheme_id)
        fact_rows[new.scheme_id] = [{
            **full_request, "_source_request": full_request,
            "target_tenor": target.target_tenor, "horizon": target.new_horizon,
            "predicted_direction": -1,
        }]
    engine.dispose()
    assert inputs is not None
    inputs["targets"] = targets
    inputs["wave"] = inputs["equivalence_evidence"]["wave"] = "W3A"
    evidence = inputs.pop("equivalence_evidence")
    for item in evidence["targets"]:
        item["native_runtime_profile"] = "blackbox-v2-v1"
        item["native_runtime_environment_fingerprint"] = "e" * 64
    inputs.update(successor_fact_rows=fact_rows, require_current_databridge=True)
    if scope_change == "wave":
        inputs["wave"] = evidence["wave"] = "W3B"
    elif scope_change == "partial":
        inputs["targets"] = targets[:1]
        evidence["targets"] = evidence["targets"][:1]
    elif scope_change == "target":
        inputs["targets"] = (replace(targets[0], new_horizon=1), targets[1])
        evidence["targets"][0]["new_horizon"] = 1
    elif scope_change == "environment":
        evidence["targets"][0]["native_runtime_environment_fingerprint"] = "8" * 64
    if scope_change is not None:
        message = (
            "Native runtime differs" if scope_change == "environment"
            else "complete locked W3A family"
        )
        with pytest.raises(RuntimeError, match=message):
            _validate_native_successor_equivalence_evidence(evidence, **inputs)
        return
    normalized = _validate_native_successor_equivalence_evidence(evidence, **inputs)
    approved_digest = native_successor_plan_sha256({"equivalence": normalized})
    for item in evidence["targets"]:
        item["native_runtime_profile"] = "native"
        item["native_runtime_environment_fingerprint"] = "8" * 64
        normalized = _validate_native_successor_equivalence_evidence(evidence, **inputs)
        changed_digest = native_successor_plan_sha256({"equivalence": normalized})
        assert changed_digest != approved_digest
        approved_digest = changed_digest


def test_distinct_input_does_not_allow_native_successor_result_mismatch() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    item = inputs["equivalence_evidence"]["targets"][0]
    item["input_identity"]["generation_id"] = "frozen-algorithm-generation"
    item["input_identity"]["data_snapshot_id"] = "frozen-algorithm-snapshot"
    item["native_result_sha256"] = "c" * 64
    with pytest.raises(RuntimeError, match="standardized result digests"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_equivalence_to_complete_persisted_fact_set() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0]["request_count"] = 2
    with pytest.raises(RuntimeError, match="persisted backtest facts"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_all_request_cutoffs_to_persisted_source_row() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    changed = _full_request(new.scheme_id) | {
        "weekly_cutoff_key": "202620",
        "actual": {},
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(changed)},
        )
    with pytest.raises(RuntimeError, match="complete Request digest"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_recomputes_standardized_result_digest() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0][
        "native_result_sha256"
    ] = "7" * 64
    inputs["equivalence_evidence"]["targets"][0][
        "successor_result_sha256"
    ] = "7" * 64
    with pytest.raises(RuntimeError, match="standardized result digests"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_comparator_source_to_current_release() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["producer"][
        "comparator_source_sha256"
    ] = "b" * 64
    with pytest.raises(RuntimeError, match="producer identity mismatch"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_native_environment_to_current_control_plane() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["control_plane_evidence"]["native_runtime"][
        "environment_fingerprint"
    ] = "9" * 64
    with pytest.raises(RuntimeError, match="Native runtime differs"):
        read_native_successor_migration_plan(engine, **inputs)


def test_first_cutover_binds_equivalence_to_current_five_files() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    item = inputs["equivalence_evidence"]["targets"][0]
    item["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    with pytest.raises(RuntimeError, match="differs from current DataBridge"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_rejects_backtest_rows_in_gray_live_range() -> None:
    engine, old, new, target, evidence = _fixture()
    full_request = _full_request(target.new_base_scheme_id) | {
        "request_id": (
            f"{target.new_base_scheme_id}:2026-05-22:2026-05-22:2026-06-01"
        ),
        "target_date": "2026-06-01",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET target_date = '2026-06-01', "
                "source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(full_request | {"actual": {}})},
        )
        raw = conn.execute(
            text("SELECT summary FROM t_backtest_runs WHERE id = 42")
        ).scalar_one()
        summary = json.loads(raw)
        summary["actual_target_date_min"] = "2026-06-01"
        summary["actual_target_date_max"] = "2026-06-01"
        conn.execute(
            text("UPDATE t_backtest_runs SET summary = :summary WHERE id = 42"),
            {"summary": json.dumps(summary)},
        )
    with pytest.raises(RuntimeError, match="crosses gray-live target boundary"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_preflight_records_historical_grid_date_drift() -> None:
    engine, old, new, target, evidence = _fixture()
    full_request = _full_request(target.new_base_scheme_id) | {
        "request_id": (
            f"{target.new_base_scheme_id}:2026-05-22:2026-05-21:2026-05-29"
        ),
        "feature_date": "2026-05-21",
        "daily_cutoff_key": "2026-05-21",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET feature_date = '2026-05-21', "
                "source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(full_request | {"actual": {}})},
        )
    inputs = _inputs(old, new, target, evidence)
    request = {
        key: full_request[key]
        for key in ("request_id", "predict_date", "feature_date", "target_date")
    }
    item = inputs["equivalence_evidence"]["targets"][0]
    item["request_artifact_sha256"] = hashlib.sha256(
        canonical_native_successor_plan(
            {"requests": [full_request]}
        ).encode("utf-8")
    ).hexdigest()
    item["request_sha256"] = hashlib.sha256(
        canonical_native_successor_plan({"requests": [request]}).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_native_successor_plan(
            {"results": [{**request, "predicted_direction": -1}]}
        ).encode("utf-8")
    ).hexdigest()
    item["native_result_sha256"] = result_sha256
    item["successor_result_sha256"] = result_sha256
    plan = read_native_successor_migration_plan(engine, **inputs)
    coverage = plan["grid_coverage"][0]
    assert coverage["old_base_scheme_id"] == old.scheme_id
    assert coverage["new_base_scheme_id"] == new.scheme_id
    assert coverage["old_date_count"] == 1
    assert coverage["new_date_count"] == 1
    assert coverage["old_date_identity_sha256"] != coverage[
        "new_date_identity_sha256"
    ]
    assert coverage["data_vintage_drift"] is True


def test_preflight_rejects_truncated_successor_backtest_summary() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        raw = conn.execute(
            text("SELECT summary FROM t_backtest_runs WHERE id = 42")
        ).scalar_one()
        summary = json.loads(raw)
        summary["request_count"] = 2
        conn.execute(
            text("UPDATE t_backtest_runs SET summary = :summary WHERE id = 42"),
            {"summary": json.dumps(summary)},
        )
    with pytest.raises(ValueError, match="backtest coverage summary mismatch"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )
