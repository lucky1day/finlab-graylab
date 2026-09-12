"""固定 W2 历史实盘结果物化的隔离合同验收。"""

from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler import repository as repo
from test_same_id_runtime_upgrade import migration, _sqlite_upsert
from test_same_id_runtime_upgrade_reclaim import (
    reclaim_scope, reclaim_plan, reclaim_apply, seed_preserved_historical_compare, preserved_compare_snapshot,
)


def preservation_fixture(migration_fixture):
    engine, reclaim_args, prior_control = reclaim_scope(migration_fixture, "W2")
    if engine.dialect.name == "sqlite":
        with patch.object(repo, "_upsert_scheme_version_conn", side_effect=_sqlite_upsert):
            reclaim_apply(engine, reclaim_args, prior_control, reclaim_plan(engine, reclaim_args, prior_control))
    else:
        reclaim_apply(engine, reclaim_args, prior_control, reclaim_plan(engine, reclaim_args, prior_control))
    source_configs, target_configs = reclaim_args["source_configs"], reclaim_args["new_configs"]
    control = {
        "schema_version": "same-id-live-preservation-control-plane-v1", "wave": "W2", "deployment_target": "aliyun-gray",
        "scheduler": {"control_plane": "systemd_one_shot", "timer_fenced": True, "unique_writer": True},
        "source_canonical_selection": {key: repo._same_id_reclaim_identity(cfg) for key, cfg in source_configs.items()},
        "target_canonical_selection": {key: repo._same_id_reclaim_identity(cfg) for key, cfg in target_configs.items()},
        "source_inputs": {}, "identity_conversions": {},
    }
    with engine.begin() as conn:
        run_type = "TEXT" if engine.dialect.name == "sqlite" else "ENUM('dry_run','shadow','active','manual')"
        phase = "TEXT" if engine.dialect.name == "sqlite" else "ENUM('gray_live','scheduled_live')"
        for definition in ("scheme_version VARCHAR(64)", "runtime_type VARCHAR(32)", f"run_type {run_type}",
                           f"prediction_phase {phase}", "data_snapshot_id VARCHAR(128)", "input_artifact_id VARCHAR(128)",
                           "started_at DATETIME", "finished_at DATETIME", "records_expected INT", "records_returned INT", "records_written INT"):
            conn.exec_driver_sql("ALTER TABLE t_scheme_runs ADD " + definition)
        # 旧通用 fixture 只保留 prediction 的历史 FK 数值，在这里补齐独立 run 以启用真实 FK。
        missing = conn.execute(text("SELECT DISTINCT p.run_id FROM t_scheme_predictions p LEFT JOIN t_scheme_runs r ON r.run_id=p.run_id "
                                    "WHERE p.run_id IS NOT NULL AND r.run_id IS NULL")).scalars().all()
        for run_id in missing:
            conn.execute(text("INSERT INTO t_scheme_runs (run_id,scheme_id,predict_date,status) VALUES (:id,'unrelated','2026-01-01','success')"), {"id": run_id})
        if engine.dialect.name == "mysql":
            conn.exec_driver_sql("ALTER TABLE t_scheme_predictions ADD CONSTRAINT preservation_run_fk FOREIGN KEY(run_id) REFERENCES t_scheme_runs(run_id) ON DELETE RESTRICT")
        for key in sorted(source_configs):
            source, target = source_configs[key], target_configs[key]
            control["identity_conversions"][key] = {
                "schema_version": "native-runtime-identity-conversion-v1", "source_scheme_id": source.scheme_id,
                "scheme_id": key, "source_code_sha256": source.code_hash, "candidate_code_sha256": target.code_hash,
                "source_metadata_sha256": source.manifest_hash, "candidate_metadata_sha256": target.manifest_hash,
                "algorithm_executions": 0,
            }
            for predict, target_date in (("2026-09-09", "2026-09-16"), ("2026-09-10", "2026-09-17")):
                snapshot = "snapshot-" + predict
                control["source_inputs"][snapshot] = {
                    "snapshot_id": snapshot, "generation_id": "generation-" + predict, "manifest_sha256": "a" * 64,
                    "files": {name: "b" * 64 for name in ("daily_output.csv", "weekly_output.csv", "monthly_output.csv", "factor_catalog.csv", "api_wind_date.csv")},
                }
                inserted = conn.execute(text("INSERT INTO t_scheme_runs (scheme_id,scheme_version,runtime_type,run_type,prediction_phase,predict_date,"
                    "status,data_snapshot_id,started_at,finished_at,records_expected,records_returned,records_written) "
                    "VALUES (:id,:version,'blackbox_v2','active','scheduled_live',:predict,'success',:snapshot,:stamp,:stamp,1,1,1)"),
                    {"id": source.scheme_id, "version": source.scheme_version, "predict": predict,
                     "snapshot": snapshot, "stamp": predict + " 07:03:00"})
                extra = {"request_id": ":".join((source.scheme_id, predict, predict, target_date)),
                         "data_snapshot_id": snapshot, "data_generation_id": "generation-" + predict,
                         "old_extra": {"keep": "保留原值"}}
                repo._insert_run_predictions_conn(conn, [{
                    "scheme_id": source.scheme_id, "scheme_version": source.scheme_version,
                    "run_id": inserted.lastrowid, "backtest_run_id": None, "backtest_actual_direction": None,
                    "target_tenor": source.tenors[0], "horizon": 5, "predict_date": predict,
                    "feature_date": predict, "target_date": target_date, "predicted_direction": 1,
                    "confidence": None, "model_version": source.algorithm_version, "extra": json.dumps(extra),
                }])
    kwargs = {"source_configs": source_configs, "target_configs": target_configs,
              "expected_database_name": reclaim_args["expected_database_name"], "expected_server_uuid": reclaim_args["expected_server_uuid"]}
    kwargs["backup_evidence"] = backup_for_preservation(engine, kwargs)
    return engine, kwargs, control


def backup_for_preservation(engine, kwargs):
    ids = sorted(list(kwargs["source_configs"]) + [cfg.scheme_id for cfg in kwargs["source_configs"].values()])
    params = {f"id_{index}": key for index, key in enumerate(ids)}
    scope = ",".join(":" + key for key in params)
    with engine.connect() as conn:
        identity = (dict(conn.execute(text("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")).mappings().one())
                    if engine.dialect.name == "mysql" else {"isolated_test": "sqlite"})
        sources = []
        for key, cfg in sorted(kwargs["source_configs"].items()):
            for target in ("2026-09-16", "2026-09-17"):
                prediction = dict(conn.execute(text("SELECT * FROM t_scheme_predictions WHERE scheme_id=:id AND target_date=:target"),
                                                {"id": cfg.scheme_id, "target": target}).mappings().one())
                run = dict(conn.execute(text("SELECT * FROM t_scheme_runs WHERE run_id=:run"), {"run": prediction["run_id"]}).mappings().one())
                sources.append({"prediction": prediction, "run": run})
        facts = repo._same_id_fact_snapshot_conn(conn, ids, for_update=False)
        versions = repo._same_id_rows_conn(conn, "t_scheme_versions", f"scheme_id IN ({scope})", params, order="scheme_id,scheme_version", for_update=False)
        registry = repo._same_id_rows_conn(conn, "t_scheme_registry", f"base_scheme_id IN ({scope})", params, order="scheme_id", for_update=False)
    return {"schema_version": "same-id-live-preservation-backup-v1", "backup_uri": "/isolated/backup.json",
            "backup_sha256": "c" * 64, "restore_receipt_sha256": "d" * 64, "restoration_verified": True,
            "source_database_identity_sha256": repo.native_successor_plan_sha256(identity),
            "source_rows_sha256": repo.native_successor_plan_sha256({"sources": sources}),
            "existing_facts_sha256": repo.native_successor_plan_sha256({"facts": facts, "versions": versions, "registry": registry})}


def preservation_plan(engine, kwargs, control):
    return repo.read_same_id_live_preservation_plan(engine, **kwargs, control_plane_evidence=control)


def preservation_apply(engine, kwargs, control, plan, reader=None):
    return repo.apply_same_id_live_preservation(engine, **kwargs, expected_plan_sha256=repo.native_successor_plan_sha256(plan),
        approved_by="isolated-preservation-test", approved_at=datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
        control_plane_evidence_reader=reader or (lambda: control))


@pytest.fixture
def preservation(migration):
    return preservation_fixture(migration)


def assert_preservation_result(engine, kwargs, plan, result):
    assert result["records_written"] == result["manual_runs_created"] == 4
    assert result["algorithm_executions"] == 0
    source_rows = {item["prediction"]["id"]: item for item in plan["sources"]}
    with engine.connect() as conn:
        for mapping in result["imports"]:
            source = source_rows[mapping["source_prediction_id"]]
            run = dict(conn.execute(text("SELECT * FROM t_scheme_runs WHERE run_id=:id"), {"id": mapping["run_id"]}).mappings().one())
            row = dict(conn.execute(text("SELECT * FROM t_scheme_predictions WHERE id=:id"), {"id": mapping["prediction_id"]}).mappings().one())
            assert mapping["run_id"] != mapping["source_run_id"]
            assert mapping["prediction_id"] != mapping["source_prediction_id"]
            assert run["run_type"] == "manual" and run["prediction_phase"] is None
            assert run["status"] == "success" and run["input_artifact_id"] is None
            assert row["scheme_version"] == kwargs["target_configs"][row["scheme_id"]].scheme_version
            assert row["backtest_run_id"] is row["backtest_actual_direction"] is None
            for field in ("predict_date", "feature_date", "target_date", "predicted_direction", "model_version", "confidence"):
                assert row[field] == source["prediction"][field]
            extra = repo._json_mapping(row["extra"])
            imported = extra.pop("migration_import")
            assert extra == repo._json_mapping(source["prediction"]["extra"])
            assert imported["operation"] == "history-materialization" and imported["algorithm_executions"] == 0
            assert imported["source_prediction_sha256"] == repo.native_successor_plan_sha256(source["prediction"])
            assert imported["source_run_sha256"] == repo.native_successor_plan_sha256(source["run"])
            assert dict(conn.execute(text("SELECT * FROM t_scheme_predictions WHERE id=:id"), {"id": mapping["source_prediction_id"]}).mappings().one()) == source["prediction"]
            assert dict(conn.execute(text("SELECT * FROM t_scheme_runs WHERE run_id=:id"), {"id": mapping["source_run_id"]}).mappings().one()) == source["run"]


def test_four_live_facts_are_materialized_without_new_natural_execution(preservation):
    engine, kwargs, control = preservation
    before = preservation_plan(engine, kwargs, control)
    result = preservation_apply(engine, kwargs, control, before)
    assert_preservation_result(engine, kwargs, before, result)
    with pytest.raises(RuntimeError, match="existing target"):
        preservation_apply(engine, kwargs, control, before)


@pytest.mark.parametrize("failure", ["fourth_insert", "old_prediction", "old_run", "new_run", "new_prediction", "version", "control"])
def test_preservation_any_failure_rolls_back_runs_predictions_and_old_mutations(preservation, monkeypatch, failure):
    engine, kwargs, control = preservation
    before = preservation_plan(engine, kwargs, control)
    original = repo._insert_run_predictions_conn
    calls = 0
    def fail(conn, rows):
        nonlocal calls
        calls += 1
        result = original(conn, rows)
        if failure == "fourth_insert" and calls == 4:
            raise RuntimeError("injected fourth insert failure")
        if failure == "old_prediction":
            conn.execute(text("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE id=:id"), {"id": before["sources"][0]["prediction"]["id"]})
        elif failure == "old_run":
            conn.execute(text("UPDATE t_scheme_runs SET records_written=2 WHERE run_id=:id"), {"id": before["sources"][0]["run"]["run_id"]})
        elif failure == "new_run":
            conn.execute(text("UPDATE t_scheme_runs SET prediction_phase='scheduled_live' WHERE run_type='manual'"))
        elif failure == "new_prediction" and calls == 4:
            conn.execute(text("UPDATE t_scheme_predictions SET predicted_direction=-1 WHERE run_id IN "
                              "(SELECT run_id FROM t_scheme_runs WHERE run_type='manual') AND target_tenor='5Y'"))
        elif failure == "version":
            conn.execute(text("UPDATE t_scheme_versions SET created_by='changed' WHERE scheme_id=:id"), {"id": next(iter(kwargs["target_configs"]))})
        return result
    monkeypatch.setattr(repo, "_insert_run_predictions_conn", fail)
    controls = iter([control, control | {"drift": True}])
    with pytest.raises(RuntimeError, match="injected|changed"):
        preservation_apply(engine, kwargs, control, before, reader=(lambda: next(controls)) if failure == "control" else None)
    assert preservation_plan(engine, kwargs, control) == before


@pytest.mark.parametrize("failure", ["source_run", "source_phase", "missing_source", "backtest_source", "source_version", "source_request", "existing_key", "backup", "backup_database", "restore", "input", "scope", "fence"])
def test_preservation_rejects_invalid_source_existing_key_or_evidence(preservation, failure):
    engine, kwargs, control = preservation
    before = preservation_plan(engine, kwargs, control)
    source = before["sources"][0]
    with engine.begin() as conn:
        if failure == "source_run":
            conn.execute(text("UPDATE t_scheme_runs SET status='failed' WHERE run_id=:id"), {"id": source["run"]["run_id"]})
        elif failure == "source_phase":
            conn.execute(text("UPDATE t_scheme_runs SET prediction_phase='gray_live' WHERE run_id=:id"), {"id": source["run"]["run_id"]})
        elif failure == "missing_source":
            conn.execute(text("DELETE FROM t_scheme_predictions WHERE id=:id"), {"id": source["prediction"]["id"]})
        elif failure == "backtest_source":
            conn.execute(text("UPDATE t_scheme_predictions SET run_id=NULL,backtest_run_id=42,backtest_actual_direction=1 WHERE id=:id"), {"id": source["prediction"]["id"]})
        elif failure == "source_version":
            conn.execute(text("UPDATE t_scheme_predictions SET scheme_version='wrong' WHERE id=:id"), {"id": source["prediction"]["id"]})
        elif failure == "source_request":
            conn.execute(text("UPDATE t_scheme_predictions SET extra='{}' WHERE id=:id"), {"id": source["prediction"]["id"]})
        elif failure == "existing_key":
            row = {k: v for k, v in source["prediction"].items() if k != "id"}
            row["scheme_id"] = row["scheme_id"].removesuffix("_bbv2")
            repo._insert_run_predictions_conn(conn, [row])
        elif failure == "backup":
            kwargs["backup_evidence"]["source_rows_sha256"] = "0" * 64
        elif failure == "backup_database":
            kwargs["backup_evidence"]["source_database_identity_sha256"] = "0" * 64
        elif failure == "restore":
            kwargs["backup_evidence"].pop("restore_receipt_sha256")
        elif failure == "input":
            next(iter(control["source_inputs"].values()))["files"]["daily_output.csv"] = "wrong"
        elif failure == "scope":
            kwargs["target_configs"].pop(next(iter(kwargs["target_configs"])))
        else:
            control["scheduler"]["timer_fenced"] = False
    with pytest.raises((ValueError, RuntimeError)):
        preservation_apply(engine, kwargs, control, before)


def test_source_extra_and_snapshot_drift_rejects_approved_plan(preservation):
    engine, kwargs, control = preservation
    before = preservation_plan(engine, kwargs, control)
    next(iter(control["source_inputs"].values()))["manifest_sha256"] = "e" * 64
    with pytest.raises(RuntimeError, match="plan changed"):
        preservation_apply(engine, kwargs, control, before)


def test_preservation_keeps_the_exact_june_harness_audit_unchanged(preservation, monkeypatch):
    engine, kwargs, control = preservation
    audit = seed_preserved_historical_compare(engine, monkeypatch)
    kwargs["backup_evidence"] = backup_for_preservation(engine, kwargs)
    before = preservation_plan(engine, kwargs, control)
    preservation_apply(engine, kwargs, control, before)
    assert preserved_compare_snapshot(engine) == audit
