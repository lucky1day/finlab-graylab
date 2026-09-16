from __future__ import annotations

import hashlib
import json

from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyCorrectedFact,
    LegacyPredictionMigration,
)
from shared.prediction_history_replacement import (
    PredictionHistoryReplacementError,
    live_fact_digest,
    resolve_prediction_history_projection,
    validate_prediction_history_source_runs,
)


def _fixture():
    corrected_fact = LegacyCorrectedFact(
        scheme_id="legacy_bbv2",
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
    digest = live_fact_digest([live])
    migration = LegacyPredictionMigration(
        prediction_scheme_id="legacy",
        historical_source_scheme_id="legacy",
        designated_source_scheme_id="legacy_bbv2",
        designated_exact="0123456789ab",
        designated_code_hash="a" * 64,
        designated_config_hash="b" * 64,
        designated_manifest_hash="c" * 64,
        designated_input_artifact_id="snapshot-" + "9" * 24,
        designated_corrected_facts_sha256="d" * 64,
        target_tenor="5Y",
        horizon=5,
        expected_fact_count=1,
        input_artifact_hash="e" * 64,
        backtest_summary_sha256="f" * 64,
        source_facts_sha256="1" * 64,
        product_facts_sha256="2" * 64,
        target_contract_mismatch_count=0,
        target_contract_mismatch_sha256="3" * 64,
        live_target_date_from="2026-06-01",
        live_target_date_through="2026-06-30",
        expected_live_fact_count=1,
        designated_live_facts_sha256=digest,
        product_live_facts_sha256=digest,
        reason="fixture",
    )
    corrected = LegacyCorrectedExactEvidence(
        source_registry_scheme_id="legacy_bbv2__h5__5Y",
        source_scheme_id="legacy_bbv2",
        registry_status="archived",
        designated_exact="0123456789ab",
        runtime_type="blackbox_v2",
        version_status="retired",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        input_artifact_id="snapshot-" + "9" * 24,
        backtest_status="success",
        backtest_benchmark_id="bbv2-fixture",
        backtest_data_source="blackbox_v2_current_snapshot_as_of",
        backtest_run_mode="persist",
        backtest_summary_sha256=_summary_sha256(),
        persisted_prediction_count=1,
        corrected_facts=(corrected_fact,),
        corrected_facts_sha256="d" * 64,
    )
    source = [
        {
            "id": 10,
            "scheme_id": "legacy_bbv2",
            "scheme_version": "0123456789ab",
            "run_id": None,
            "backtest_run_id": 8,
            "target_tenor": "5Y",
            "horizon": 5,
            "predict_date": "2026-05-20",
            "feature_date": "2026-05-20",
            "target_date": "2026-05-27",
            "predicted_direction": 1,
            "backtest_actual_direction": -1,
        },
        {
            "id": 11,
            "scheme_id": "legacy_bbv2",
            "scheme_version": "0123456789ab",
            "run_id": 9,
            "backtest_run_id": None,
            **live,
        },
    ]
    return migration, corrected, source, live


def _summary():
    return {
        "scheme_version": "0123456789ab",
        "manifest_hash": "c" * 64,
        "input_artifact_hash": "snapshot-" + "9" * 24,
        "persisted_prediction_count": 1,
        "row_count": 1,
    }


def _summary_sha256() -> str:
    raw = json.dumps(
        _summary(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_projects_complete_replacement_and_marks_only_approved_rows_deletable():
    migration, corrected, source, live = _fixture()
    legacy_live = {
        "id": 2,
        "scheme_id": "legacy",
        "scheme_version": "oldexact0001",
        "run_id": 7,
        "backtest_run_id": None,
        **live,
    }

    projection = resolve_prediction_history_projection(
        [legacy_live],
        source,
        entry=migration,
        corrected=corrected,
    )

    assert projection is not None
    assert projection.deletable_product_ids == frozenset({2})
    assert projection.deletable_live_run_ids == frozenset({7})
    assert [row["scheme_id"] for row in projection.source_rows] == [
        "legacy",
        "legacy",
    ]
    assert all(
        row["lineage_scheme_id"] == "legacy_bbv2"
        for row in projection.source_rows
    )


def test_absent_source_keeps_native_history_and_partial_source_fails():
    migration, corrected, source, live = _fixture()
    assert (
        resolve_prediction_history_projection(
            [], [], entry=migration, corrected=corrected
        )
        is None
    )
    source[1]["predicted_direction"] = 1
    try:
        resolve_prediction_history_projection(
            [], source, entry=migration, corrected=corrected
        )
    except PredictionHistoryReplacementError as exc:
        assert "live replacement facts changed" in str(exc)
    else:
        raise AssertionError("source drift must fail closed")


def test_conflicting_product_business_key_fails_closed():
    migration, corrected, source, _live = _fixture()
    conflicting = {
        **source[0],
        "id": 99,
        "scheme_id": migration.prediction_scheme_id,
        "scheme_version": "newer0000001",
        "backtest_run_id": 77,
        "predicted_direction": -1,
    }

    try:
        resolve_prediction_history_projection(
            [conflicting], source, entry=migration, corrected=corrected
        )
    except PredictionHistoryReplacementError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("conflicting product business key must fail closed")


def test_source_run_lineage_is_required_and_drift_fails_closed():
    migration, corrected, source, _live = _fixture()
    projection = resolve_prediction_history_projection(
        [], source, entry=migration, corrected=corrected
    )
    assert projection is not None
    live_run = {
        "run_id": 9,
        "scheme_id": "legacy_bbv2",
        "scheme_version": "0123456789ab",
        "runtime_type": "blackbox_v2",
        "status": "success",
        "prediction_phase": "scheduled_live",
        "predict_date": "2026-06-01",
        "data_snapshot_id": "snapshot-" + "8" * 24,
        "records_written": 1,
    }
    backtest_run = {
        "id": 8,
        "scheme_id": "legacy_bbv2",
        "status": "success",
        "benchmark_id": "bbv2-fixture",
        "data_source": "blackbox_v2_current_snapshot_as_of",
        "run_mode": "persist",
        "code_hash": "a" * 64,
        "config_hash": "b" * 64,
        "input_artifact_hash": "snapshot-" + "9" * 24,
        "summary": _summary(),
    }

    validate_prediction_history_source_runs(
        projection,
        [live_run],
        [backtest_run],
        [
            {
                "run_id": 8,
                "scheme_id": "legacy_bbv2",
                "target_tenor": "5Y",
                "horizon": 5,
                "predict_date": "2026-05-20",
                "feature_date": "2026-05-20",
                "target_date": "2026-05-27",
                "predicted_direction": 1,
                "label": -1,
            }
        ],
        entry=migration,
        corrected=corrected,
    )
    live_run["status"] = "failed"
    try:
        validate_prediction_history_source_runs(
            projection,
            [live_run],
            [backtest_run],
            [],
            entry=migration,
            corrected=corrected,
        )
    except PredictionHistoryReplacementError as exc:
        assert "live replacement run lineage changed" in str(exc)
    else:
        raise AssertionError("source run drift must fail closed")
