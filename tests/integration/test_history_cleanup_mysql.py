from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import bindparam, text

from scheduler.persistence import history_cleanup
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyCorrectedFact,
    LegacyPredictionMigration,
)
from shared.prediction_history_replacement import live_fact_digest


pytestmark = pytest.mark.mysql_integration


BASE_ID = "integration_history_cleanup"
SOURCE_ID = f"{BASE_ID}_bbv2"
SOURCE_EXACT = "0123456789ab"
OLD_EXACT = "111111111111"
SOURCE_REGISTRY_ID = f"{SOURCE_ID}__h5__5Y"


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _source_summary() -> dict[str, object]:
    return {
        "scheme_version": SOURCE_EXACT,
        "manifest_hash": "c" * 64,
        "input_artifact_hash": "snapshot-" + "9" * 24,
        "persisted_prediction_count": 1,
        "row_count": 1,
    }


def _evidence() -> tuple[
    LegacyPredictionMigration,
    LegacyCorrectedExactEvidence,
]:
    corrected_fact = LegacyCorrectedFact(
        scheme_id=SOURCE_ID,
        target_tenor="5Y",
        horizon=5,
        predict_date="2026-05-20",
        feature_date="2026-05-20",
        target_date="2026-05-27",
        predicted_direction=1,
        actual_direction=-1,
    )
    live = {
        "target_tenor": "5Y",
        "horizon": 5,
        "predict_date": "2026-06-01",
        "feature_date": "2026-05-29",
        "target_date": "2026-06-08",
        "predicted_direction": -1,
        "backtest_actual_direction": None,
    }
    live_digest = live_fact_digest([live])
    legacy_fact = {
        "scheme_id": BASE_ID,
        "target_tenor": "5Y",
        "horizon": 5,
        "predict_date": "2026-05-20",
        "feature_date": "2026-05-20",
        "target_date": "2026-05-27",
        "predicted_direction": 1,
        "actual_direction": -1,
    }
    corrected_digest = _sha256(
        [
            {
                "scheme_id": corrected_fact.scheme_id,
                "target_tenor": corrected_fact.target_tenor,
                "horizon": corrected_fact.horizon,
                "predict_date": corrected_fact.predict_date,
                "feature_date": corrected_fact.feature_date,
                "target_date": corrected_fact.target_date,
                "predicted_direction": corrected_fact.predicted_direction,
                "actual_direction": corrected_fact.actual_direction,
            }
        ]
    )
    migration = LegacyPredictionMigration(
        prediction_scheme_id=BASE_ID,
        historical_source_scheme_id=BASE_ID,
        designated_source_scheme_id=SOURCE_ID,
        designated_exact=SOURCE_EXACT,
        designated_code_hash="a" * 64,
        designated_config_hash="b" * 64,
        designated_manifest_hash="c" * 64,
        designated_input_artifact_id="snapshot-" + "9" * 24,
        designated_corrected_facts_sha256=corrected_digest,
        target_tenor="5Y",
        horizon=5,
        expected_fact_count=1,
        input_artifact_hash="d" * 64,
        backtest_summary_sha256="e" * 64,
        source_facts_sha256="f" * 64,
        product_facts_sha256=_sha256([legacy_fact]),
        target_contract_mismatch_count=0,
        target_contract_mismatch_sha256=_sha256([]),
        live_target_date_from="2026-06-01",
        live_target_date_through="2026-06-30",
        expected_live_fact_count=1,
        designated_live_facts_sha256=live_digest,
        product_live_facts_sha256=live_digest,
        reason="integration fixture",
    )
    corrected = LegacyCorrectedExactEvidence(
        source_registry_scheme_id=SOURCE_REGISTRY_ID,
        source_scheme_id=SOURCE_ID,
        registry_status="archived",
        designated_exact=SOURCE_EXACT,
        runtime_type="blackbox_v2",
        version_status="retired",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        input_artifact_id="snapshot-" + "9" * 24,
        backtest_status="success",
        backtest_benchmark_id="cleanup-source",
        backtest_data_source="blackbox_v2_current_snapshot_as_of",
        backtest_run_mode="persist",
        backtest_summary_sha256=_sha256(_source_summary()),
        persisted_prediction_count=1,
        corrected_facts=(corrected_fact,),
        corrected_facts_sha256=corrected_digest,
    )
    return migration, corrected


def _seed(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_registry "
                "(scheme_id, base_scheme_id, name, owner, horizon, task_type, "
                "runtime_type, tenors, frequency, target_tenor, schedule_cron, "
                "status) VALUES "
                "(:registry_id, :base_id, 'cleanup source', 'integration', 5, "
                "'T+5', 'blackbox_v2', JSON_ARRAY('5Y'), 'daily', '5Y', "
                "'0 0 * * *', 'archived')"
            ),
            {"registry_id": SOURCE_REGISTRY_ID, "base_id": SOURCE_ID},
        )
        connection.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, "
                "config_hash, manifest_hash, status) VALUES "
                "(:source_id, :source_exact, 'blackbox_v2', :code_hash, "
                ":config_hash, :manifest_hash, 'retired'), "
                "(:base_id, :old_exact, 'blackbox_v2', :old_code_hash, "
                ":old_config_hash, :old_manifest_hash, 'retired')"
            ),
            {
                "source_id": SOURCE_ID,
                "source_exact": SOURCE_EXACT,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "manifest_hash": "c" * 64,
                "base_id": BASE_ID,
                "old_exact": OLD_EXACT,
                "old_code_hash": "1" * 64,
                "old_config_hash": "2" * 64,
                "old_manifest_hash": "3" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_runs "
                "(benchmark_id, scheme_id, data_source, start_date, end_date, "
                "status, code_hash, config_hash, input_artifact_hash, run_mode, "
                "summary) VALUES "
                "('cleanup-old', :base_id, 'blackbox_v2_current_snapshot_as_of', "
                "'2026-05-20', '2026-05-20', 'success', :old_code_hash, "
                ":old_config_hash, :input_artifact, 'persist', :summary), "
                "('cleanup-source', :source_id, "
                "'blackbox_v2_current_snapshot_as_of', '2026-05-20', "
                "'2026-05-20', 'success', :code_hash, :config_hash, "
                ":input_artifact, 'persist', :summary)"
            ),
            {
                "base_id": BASE_ID,
                "source_id": SOURCE_ID,
                "old_code_hash": "1" * 64,
                "old_config_hash": "2" * 64,
                "code_hash": "a" * 64,
                "config_hash": "b" * 64,
                "input_artifact": "snapshot-" + "9" * 24,
                "summary": json.dumps(_source_summary()),
            },
        )
        backtests = {
            row["scheme_id"]: int(row["id"])
            for row in connection.execute(
                text(
                    "SELECT id, scheme_id FROM t_backtest_runs "
                    "WHERE benchmark_id IN ('cleanup-old', 'cleanup-source')"
                )
            ).mappings()
        }
        connection.execute(
            text(
                "INSERT INTO t_scheme_runs "
                "(scheme_id, scheme_version, runtime_type, run_type, "
                "prediction_phase, predict_date, status, data_snapshot_id, "
                "records_written) VALUES "
                "(:base_id, :old_exact, 'blackbox_v2', 'active', "
                "'gray_live', '2026-06-01', 'success', :snapshot_id, 1), "
                "(:source_id, :source_exact, 'blackbox_v2', 'active', "
                "'gray_live', '2026-06-01', 'success', :snapshot_id, 1)"
            ),
            {
                "base_id": BASE_ID,
                "old_exact": OLD_EXACT,
                "source_id": SOURCE_ID,
                "source_exact": SOURCE_EXACT,
                "snapshot_id": "snapshot-" + "9" * 24,
            },
        )
        live_runs = {
            row["scheme_id"]: int(row["run_id"])
            for row in connection.execute(
                text(
                    "SELECT run_id, scheme_id FROM t_scheme_runs "
                    "WHERE scheme_id IN (:base_id, :source_id)"
                ),
                {"base_id": BASE_ID, "source_id": SOURCE_ID},
            ).mappings()
        }
        connection.execute(
            text(
                "INSERT INTO t_backtest_predictions "
                "(run_id, benchmark_id, scheme_id, target_tenor, horizon, "
                "predict_date, feature_date, target_date, label, "
                "predicted_direction) VALUES "
                "(:old_backtest, 'cleanup-old', :base_id, '5Y', 5, "
                "'2026-05-20', '2026-05-20', '2026-05-27', -1, 1), "
                "(:source_backtest, 'cleanup-source', :source_id, '5Y', 5, "
                "'2026-05-20', '2026-05-20', '2026-05-27', -1, 1)"
            ),
            {
                "old_backtest": backtests[BASE_ID],
                "base_id": BASE_ID,
                "source_backtest": backtests[SOURCE_ID],
                "source_id": SOURCE_ID,
            },
        )
        connection.execute(
            text(
                "INSERT INTO t_backtest_monthly_metrics "
                "(run_id, benchmark_id, scheme_id, target_tenor, horizon, "
                "month, sample_count, correct_count) VALUES "
                "(:old_backtest, 'cleanup-old', :base_id, '5Y', 5, "
                "'2026-05', 1, 1)"
            ),
            {"old_backtest": backtests[BASE_ID], "base_id": BASE_ID},
        )
        prediction_sql = text(
            "INSERT INTO t_scheme_predictions "
            "(run_id, backtest_run_id, scheme_version, scheme_id, "
            "target_tenor, horizon, predict_date, feature_date, target_date, "
            "predicted_direction, backtest_actual_direction) VALUES "
            "(:run_id, :backtest_run_id, :scheme_version, :scheme_id, '5Y', "
            "5, :predict_date, :feature_date, :target_date, :direction, :actual)"
        )
        connection.execute(
            prediction_sql,
            [
                {
                    "run_id": None,
                    "backtest_run_id": backtests[BASE_ID],
                    "scheme_version": None,
                    "scheme_id": BASE_ID,
                    "predict_date": "2026-05-20",
                    "feature_date": "2026-05-20",
                    "target_date": "2026-05-27",
                    "direction": 1,
                    "actual": -1,
                },
                {
                    "run_id": live_runs[BASE_ID],
                    "backtest_run_id": None,
                    "scheme_version": OLD_EXACT,
                    "scheme_id": BASE_ID,
                    "predict_date": "2026-06-01",
                    "feature_date": "2026-05-29",
                    "target_date": "2026-06-08",
                    "direction": -1,
                    "actual": None,
                },
                {
                    "run_id": None,
                    "backtest_run_id": backtests[SOURCE_ID],
                    "scheme_version": SOURCE_EXACT,
                    "scheme_id": SOURCE_ID,
                    "predict_date": "2026-05-20",
                    "feature_date": "2026-05-20",
                    "target_date": "2026-05-27",
                    "direction": 1,
                    "actual": -1,
                },
                {
                    "run_id": live_runs[SOURCE_ID],
                    "backtest_run_id": None,
                    "scheme_version": SOURCE_EXACT,
                    "scheme_id": SOURCE_ID,
                    "predict_date": "2026-06-01",
                    "feature_date": "2026-05-29",
                    "target_date": "2026-06-08",
                    "direction": -1,
                    "actual": None,
                },
            ],
        )


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        for table, column in (
            ("t_scheme_predictions", "scheme_id"),
            ("t_scheme_runs", "scheme_id"),
            ("t_backtest_runs", "scheme_id"),
            ("t_scheme_versions", "scheme_id"),
            ("t_scheme_registry", "base_scheme_id"),
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE {column} IN :scheme_ids").bindparams(
                    bindparam("scheme_ids", expanding=True)
                ),
                {"scheme_ids": [BASE_ID, SOURCE_ID]},
            )


def test_history_cleanup_is_exact_and_rolls_back_injected_failure(
    mysql_test_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, corrected = _evidence()
    _seed(mysql_test_engine)
    monkeypatch.setattr(
        history_cleanup,
        "_load_entry",
        lambda _scheme_id: (migration, corrected),
    )
    try:
        plan = history_cleanup.plan_replaced_prediction_history_cleanup(
            mysql_test_engine,
            prediction_scheme_id=BASE_ID,
        )
        assert len(plan["scope"]["product_ids"]) == 2
        assert len(plan["scope"]["source_live_run_ids"]) == 1
        assert len(plan["scope"]["source_backtest_run_ids"]) == 1
        assert len(plan["scope"]["source_backtest_prediction_ids"]) == 1
        assert len(plan["backup"]["backtest_monthly_metrics"]) == 1
        assert len(plan["backup"]["source_live_runs"]) == 1
        assert len(plan["backup"]["source_backtest_runs"]) == 1
        assert len(plan["backup"]["source_backtest_predictions"]) == 1
        source_live_run_id = plan["scope"]["source_live_run_ids"][0]
        with mysql_test_engine.begin() as connection:
            connection.execute(
                text("UPDATE t_scheme_runs SET status='failed' WHERE run_id=:run_id"),
                {"run_id": source_live_run_id},
            )
        with pytest.raises(ValueError, match="run lineage changed"):
            history_cleanup.apply_replaced_prediction_history_cleanup(
                mysql_test_engine,
                plan,
            )
        with mysql_test_engine.begin() as connection:
            connection.execute(
                text("UPDATE t_scheme_runs SET status='success' WHERE run_id=:run_id"),
                {"run_id": source_live_run_id},
            )
        original_delete_ids = history_cleanup._delete_ids
        with monkeypatch.context() as failure_patch:
            def fail_after_backtest_delete(*args, **kwargs):
                deleted = original_delete_ids(*args, **kwargs)
                if kwargs.get("table") == "t_backtest_runs":
                    raise RuntimeError("injected after backtest cascade")
                return deleted

            failure_patch.setattr(
                history_cleanup,
                "_delete_ids",
                fail_after_backtest_delete,
            )
            with pytest.raises(RuntimeError, match="backtest cascade"):
                history_cleanup.apply_replaced_prediction_history_cleanup(
                    mysql_test_engine,
                    plan,
                )
        with mysql_test_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_predictions "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_backtest_monthly_metrics "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_backtest_runs "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_backtest_predictions "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 1

        stats = history_cleanup.apply_replaced_prediction_history_cleanup(
            mysql_test_engine,
            plan,
        )
        assert stats.predictions_deleted == 2
        assert stats.backtest_runs_deleted == 1
        assert stats.live_runs_deleted == 1
        assert stats.versions_deleted == 1
        with mysql_test_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_predictions "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_predictions "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": SOURCE_ID},
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_backtest_monthly_metrics "
                    "WHERE scheme_id=:scheme_id"
                ),
                {"scheme_id": BASE_ID},
            ).scalar_one() == 0
    finally:
        _cleanup(mysql_test_engine)
