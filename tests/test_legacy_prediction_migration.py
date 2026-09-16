from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.legacy_prediction_migration import (
    load_legacy_corrected_exact_evidence,
    load_legacy_prediction_migrations,
)


def _entry() -> dict[str, object]:
    return {
        "prediction_scheme_id": "legacy_scheme",
        "historical_source_scheme_id": "legacy_scheme",
        "designated_source_scheme_id": "legacy_scheme_bbv2",
        "designated_exact": "0123456789ab",
        "designated_code_hash": "a" * 64,
        "designated_config_hash": "b" * 64,
        "designated_manifest_hash": "c" * 64,
        "designated_input_artifact_id": "snapshot-" + "9" * 24,
        "designated_corrected_facts_sha256": "d" * 64,
        "target_tenor": "5Y",
        "horizon": 5,
        "expected_fact_count": 333,
        "input_artifact_hash": "e" * 64,
        "backtest_summary_sha256": "f" * 64,
        "source_facts_sha256": "1" * 64,
        "product_facts_sha256": "1" * 64,
        "target_contract_mismatch_count": 14,
        "target_contract_mismatch_sha256": "2" * 64,
        "reason": "Approved immutable migration evidence.",
    }


def _write_manifest(root: Path, entries: list[dict[str, object]]) -> None:
    deploy = root / "deploy"
    deploy.mkdir(exist_ok=True)
    (deploy / "legacy_prediction_migration_compatibility_v1.json").write_text(
        json.dumps(
            {
                "schema_version": "legacy-prediction-migration-compatibility-v1",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def test_loads_precise_legacy_prediction_migration(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [_entry()])

    entries = load_legacy_prediction_migrations(tmp_path)

    assert len(entries) == 1
    entry = next(iter(entries))
    assert entry.prediction_scheme_id == "legacy_scheme"
    assert entry.designated_source_scheme_id == "legacy_scheme_bbv2"
    assert entry.designated_exact == "0123456789ab"
    assert entry.expected_fact_count == 333
    assert entry.target_contract_mismatch_count == 14


def test_rejects_duplicate_or_non_exact_migration_entries(tmp_path: Path) -> None:
    duplicate = _entry()
    _write_manifest(tmp_path, [_entry(), duplicate])
    with pytest.raises(ValueError, match="duplicate"):
        load_legacy_prediction_migrations(tmp_path)

    duplicate["designated_exact"] = "not-an-exact"
    _write_manifest(tmp_path, [duplicate])
    with pytest.raises(ValueError, match="fields are invalid"):
        load_legacy_prediction_migrations(tmp_path)


def test_rejects_unknown_manifest_fields(tmp_path: Path) -> None:
    entry = _entry()
    entry["allow_any_legacy"] = True
    _write_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="entry is invalid"):
        load_legacy_prediction_migrations(tmp_path)


def test_repository_corrected_evidence_binds_designated_exact() -> None:
    root = Path(__file__).resolve().parents[1]

    evidence = next(iter(load_legacy_corrected_exact_evidence(root)))

    assert evidence.source_scheme_id.endswith("_bbv2")
    assert evidence.registry_status == "archived"
    assert evidence.version_status == "retired"
    assert evidence.designated_exact == "3ee3dd2334fd"
    assert evidence.persisted_prediction_count == 333
    assert len(evidence.corrected_facts) == 333
    assert (
        evidence.corrected_facts_sha256
        == "0caaa51c08a1a65b9e5998c73a3cd6d181f9c14e81ddea2978c9080c45fac749"
    )


def test_corrected_evidence_rejects_fact_or_hash_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (
            root
            / "deploy"
            / "legacy_prediction_migration_evidence_v1.json"
        ).read_text(encoding="utf-8")
    )
    payload["entries"][0]["corrected_facts"][0]["predicted_direction"] *= -1
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "legacy_prediction_migration_evidence_v1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fields are invalid"):
        load_legacy_corrected_exact_evidence(tmp_path)
