from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, text

from scheduler.repository import (
    PREDICTION_CORRECTION_ATTESTATION_ID,
    PREDICTION_CORRECTION_ATTESTATION_SHA256,
    PREDICTION_CORRECTION_EVIDENCE_SCHEMA,
    PREDICTION_CORRECTION_REASON,
    PredictionCorrection,
    insert_prediction_corrections_atomic,
)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE t_scheme_predictions (
                id INTEGER PRIMARY KEY,
                scheme_id TEXT NOT NULL,
                target_tenor TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                predict_date TEXT NOT NULL,
                feature_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                prediction_phase TEXT NOT NULL,
                predicted_direction INTEGER NOT NULL,
                scheme_version TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE t_scheme_registry (
                scheme_id TEXT NOT NULL,
                base_scheme_id TEXT NOT NULL,
                target_tenor TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE t_scheme_prediction_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL UNIQUE,
                scheme_id TEXT NOT NULL,
                target_tenor TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                predict_date TEXT NOT NULL,
                feature_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                original_direction INTEGER NOT NULL,
                corrected_direction INTEGER NOT NULL,
                scheme_version TEXT NOT NULL,
                prediction_phase TEXT NOT NULL,
                correction_reason TEXT NOT NULL,
                operation_id TEXT NOT NULL UNIQUE,
                operation_scope_sha256 TEXT NOT NULL UNIQUE,
                release_commit TEXT NOT NULL,
                evidence TEXT NOT NULL,
                created_by TEXT NOT NULL
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, scheme_id, target_tenor, horizon, predict_date,
                     feature_date, target_date, prediction_phase,
                     predicted_direction, scheme_version)
                VALUES
                    (41, 'cgb_causal_wk_3y', '3Y', 1, '2026-08-22',
                     '2026-08-21', '2026-08-28', 'scheduled_live',
                     1, '4b8db29b2f74'),
                    (42, 'five_y_t5_lgbm_3y_z_anti180_b12_v1', '5Y', 5,
                     '2026-08-21', '2026-08-20', '2026-08-27',
                     'scheduled_live', 1, 'ee62a61b4424')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, target_tenor, horizon, status)
                VALUES
                    ('cgb_causal_wk_3y__h1__3Y', 'cgb_causal_wk_3y',
                     '3Y', 1, 'active'),
                    ('five_y_t5_lgbm_3y_z_anti180_b12_v1__h5__5Y',
                     'five_y_t5_lgbm_3y_z_anti180_b12_v1',
                     '5Y', 5, 'active')
                """
            )
        )
    return engine


def _correction(**overrides) -> PredictionCorrection:
    values = {
        "scheme_id": "cgb_causal_wk_3y",
        "target_tenor": "3Y",
        "horizon": 1,
        "predict_date": "2026-08-22",
        "feature_date": "2026-08-21",
        "target_date": "2026-08-28",
        "original_direction": 1,
        "corrected_direction": -1,
        "scheme_version": "4b8db29b2f74",
        "prediction_phase": "scheduled_live",
        "correction_reason": PREDICTION_CORRECTION_REASON,
        "created_by": "test-operator",
    }
    values.update(overrides)
    if "evidence" not in values:
        database_identity = hashlib.sha256(
            json.dumps(
                {"database": "sqlite", "server_uuid": "test"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request_id = ":".join(
            str(values[field])
            for field in (
                "scheme_id",
                "predict_date",
                "feature_date",
                "target_date",
            )
        )
        mac3_prediction_ids = {
            "cgb_causal_wk_3y:2026-08-22:2026-08-21:2026-08-28": 3353,
            (
                "five_y_t5_lgbm_3y_z_anti180_b12_v1:"
                "2026-08-21:2026-08-20:2026-08-27"
            ): 3327,
        }
        normalized = {
            "schema_version": PREDICTION_CORRECTION_EVIDENCE_SCHEMA,
            "attestation_id": PREDICTION_CORRECTION_ATTESTATION_ID,
            "attestation_sha256": PREDICTION_CORRECTION_ATTESTATION_SHA256,
            "release_commit": "a" * 40,
            "deployment_target": "aliyun-gray",
            "database_identity_sha256": database_identity,
            "replay_generation_id": "full-20260824-152846-6a868b34c27c",
            "replay_parent_snapshot_id": "snapshot-25d3e8ea728f8e420a4c713c",
            "replay_combined_snapshot_id": "snapshot-0ba8488e01f845fe30337d3a",
            "replay_business_digest": (
                "6a868b34c27c1125d68ca622b5a05640"
                "1975efdea6af8b36a34fa055603585c0"
            ),
            "replay_request_id": request_id,
            "replay_direction": values["corrected_direction"],
            "mac3_direction": values["corrected_direction"],
            "mac3_prediction_id": mac3_prediction_ids[request_id],
            "comparison_status": "matched",
            "replay_cutoffs": {
                "daily": values["feature_date"],
                "weekly": "202632",
                "monthly": "202609",
            },
        }
        scope = {
            key: values[key]
            for key in (
                "scheme_id",
                "target_tenor",
                "horizon",
                "predict_date",
                "feature_date",
                "target_date",
                "original_direction",
                "corrected_direction",
                "scheme_version",
                "prediction_phase",
                "correction_reason",
                "created_by",
            )
        }
        normalized["replay_result_sha256"] = hashlib.sha256(
            json.dumps(
                {**scope, **normalized},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        values["evidence"] = normalized
    return PredictionCorrection(**values)


def _rehash_evidence(
    correction: PredictionCorrection,
    evidence: dict[str, object],
) -> dict[str, object]:
    normalized = dict(evidence)
    normalized.pop("replay_result_sha256", None)
    scope = {
        field: getattr(correction, field)
        for field in (
            "scheme_id",
            "target_tenor",
            "horizon",
            "predict_date",
            "feature_date",
            "target_date",
            "original_direction",
            "corrected_direction",
            "scheme_version",
            "prediction_phase",
            "correction_reason",
            "created_by",
        )
    }
    normalized["replay_result_sha256"] = hashlib.sha256(
        json.dumps(
            {**scope, **normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized


@pytest.fixture(autouse=True)
def _correction_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFL_RELEASE_COMMIT", "a" * 40)
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "aliyun-gray")
    monkeypatch.setenv("BFL_OPERATOR_ID", "test-operator")


def test_insert_prediction_correction_is_append_only() -> None:
    engine = _engine()
    assert insert_prediction_corrections_atomic(engine, [_correction()]) == 1

    with engine.connect() as connection:
        source_direction = connection.execute(
            text("SELECT predicted_direction FROM t_scheme_predictions WHERE id=41")
        ).scalar_one()
        stored = connection.execute(
            text(
                """
                SELECT prediction_id, original_direction, corrected_direction,
                       correction_reason, operation_id,
                       operation_scope_sha256, release_commit,
                       evidence, created_by
                FROM t_scheme_prediction_corrections
                """
            )
        ).mappings().one()
    assert source_direction == 1
    assert stored["prediction_id"] == 41
    assert stored["original_direction"] == 1
    assert stored["corrected_direction"] == -1
    assert stored["operation_id"].startswith("prediction-correction-")
    assert stored["operation_id"].endswith(stored["operation_scope_sha256"])
    assert stored["release_commit"] == "a" * 40
    assert json.loads(stored["evidence"])["comparison_status"] == "matched"


def test_source_identity_drift_rolls_back_entire_batch() -> None:
    engine = _engine()
    with pytest.raises(RuntimeError, match="source identity mismatch"):
        insert_prediction_corrections_atomic(
            engine,
            [
                _correction(),
                _correction(
                    scheme_id="five_y_t5_lgbm_3y_z_anti180_b12_v1",
                    target_tenor="5Y",
                    horizon=5,
                    predict_date="2026-08-21",
                    feature_date="2026-08-20",
                    target_date="2026-08-27",
                    original_direction=-1,
                    corrected_direction=0,
                    scheme_version="ee62a61b4424",
                ),
            ],
        )
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM t_scheme_prediction_corrections")
        ).scalar_one() == 0


def test_existing_correction_is_rejected_without_overwrite() -> None:
    engine = _engine()
    insert_prediction_corrections_atomic(engine, [_correction()])
    with pytest.raises(RuntimeError, match="already exists"):
        insert_prediction_corrections_atomic(engine, [_correction()])
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM t_scheme_prediction_corrections")
        ).scalar_one() == 1


def test_contradictory_evidence_is_rejected() -> None:
    engine = _engine()
    correction = _correction()
    evidence = dict(correction.evidence)
    evidence["mac3_direction"] = 1
    with pytest.raises(ValueError, match="directions must equal"):
        insert_prediction_corrections_atomic(
            engine,
            [_correction(evidence=evidence)],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "unknown-schema"),
        ("deployment_target", "mac3-production"),
        ("database_identity_sha256", "not-a-digest"),
        ("replay_generation_id", ""),
        ("replay_parent_snapshot_id", ""),
        ("replay_combined_snapshot_id", ""),
        ("replay_business_digest", "not-a-digest"),
        ("replay_request_id", "wrong-request"),
        ("comparison_status", "different"),
        ("replay_result_sha256", "0" * 64),
    ],
)
def test_invalid_evidence_receipt_is_rejected(field: str, value: object) -> None:
    engine = _engine()
    correction = _correction()
    evidence = dict(correction.evidence)
    evidence[field] = value
    with pytest.raises(ValueError):
        insert_prediction_corrections_atomic(
            engine,
            [_correction(evidence=evidence)],
        )


def test_incomplete_cutoff_evidence_is_rejected() -> None:
    engine = _engine()
    correction = _correction()
    evidence = dict(correction.evidence)
    evidence["replay_cutoffs"] = {"daily": "2026-08-21"}
    with pytest.raises(ValueError, match="cutoffs are invalid"):
        insert_prediction_corrections_atomic(
            engine,
            [_correction(evidence=evidence)],
        )


def test_self_consistent_but_unattested_cutoff_is_rejected() -> None:
    engine = _engine()
    correction = _correction()
    evidence = dict(correction.evidence)
    evidence["replay_cutoffs"] = {
        "daily": "2026-08-21",
        "weekly": "202633",
        "monthly": "202609",
    }
    evidence = _rehash_evidence(correction, evidence)
    with pytest.raises(ValueError, match="not in the release attestation"):
        insert_prediction_corrections_atomic(
            engine,
            [_correction(evidence=evidence)],
        )


def test_wrong_release_authority_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine()
    monkeypatch.setenv("BFL_RELEASE_COMMIT", "c" * 40)
    with pytest.raises(RuntimeError, match="authority mismatch"):
        insert_prediction_corrections_atomic(engine, [_correction()])


def test_inactive_registry_is_rejected() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE t_scheme_registry SET status = 'paused'")
        )
    with pytest.raises(RuntimeError, match="exact active Registry"):
        insert_prediction_corrections_atomic(engine, [_correction()])


def test_scheme_version_drift_is_rejected() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_predictions "
                "SET scheme_version = 'v2' WHERE id = 41"
            )
        )
    with pytest.raises(RuntimeError, match="source identity mismatch"):
        insert_prediction_corrections_atomic(
            engine,
            [_correction()],
        )
