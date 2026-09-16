from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from shared.scheme_config_schema import SCHEME_ID_PATTERN


_EXACT_PATTERN = re.compile(r"[0-9a-f]{12}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_ENTRY_FIELDS = {
    "prediction_scheme_id",
    "historical_source_scheme_id",
    "designated_source_scheme_id",
    "designated_exact",
    "designated_code_hash",
    "designated_config_hash",
    "designated_manifest_hash",
    "designated_input_artifact_id",
    "designated_corrected_facts_sha256",
    "target_tenor",
    "horizon",
    "expected_fact_count",
    "input_artifact_hash",
    "backtest_summary_sha256",
    "source_facts_sha256",
    "product_facts_sha256",
    "target_contract_mismatch_count",
    "target_contract_mismatch_sha256",
    "live_target_date_from",
    "live_target_date_through",
    "expected_live_fact_count",
    "designated_live_facts_sha256",
    "product_live_facts_sha256",
    "reason",
}


@dataclass(frozen=True, slots=True)
class LegacyPredictionMigration:
    """经业务确认、由完整事实摘要约束的只读历史迁移关系。"""

    prediction_scheme_id: str
    historical_source_scheme_id: str
    designated_source_scheme_id: str
    designated_exact: str
    designated_code_hash: str
    designated_config_hash: str
    designated_manifest_hash: str
    designated_input_artifact_id: str
    designated_corrected_facts_sha256: str
    target_tenor: str
    horizon: int
    expected_fact_count: int
    input_artifact_hash: str
    backtest_summary_sha256: str
    source_facts_sha256: str
    product_facts_sha256: str
    target_contract_mismatch_count: int
    target_contract_mismatch_sha256: str
    live_target_date_from: str
    live_target_date_through: str
    expected_live_fact_count: int
    designated_live_facts_sha256: str
    product_live_facts_sha256: str
    reason: str


@dataclass(frozen=True, slots=True)
class LegacyCorrectedFact:
    """指定纠正版回测的一条不可变事实。"""

    scheme_id: str
    target_tenor: str
    horizon: int
    predict_date: str
    feature_date: str
    target_date: str
    predicted_direction: int
    actual_direction: int


@dataclass(frozen=True, slots=True)
class LegacyCorrectedExactEvidence:
    """从只读 archived/retired 来源冻结的纠正版证据。"""

    source_registry_scheme_id: str
    source_scheme_id: str
    registry_status: str
    designated_exact: str
    runtime_type: str
    version_status: str
    code_hash: str
    config_hash: str
    manifest_hash: str
    input_artifact_id: str
    backtest_status: str
    backtest_benchmark_id: str
    backtest_data_source: str
    backtest_run_mode: str
    backtest_summary_sha256: str
    persisted_prediction_count: int
    corrected_facts: tuple[LegacyCorrectedFact, ...]
    corrected_facts_sha256: str


def load_legacy_prediction_migrations(
    project_root: str | Path,
) -> frozenset[LegacyPredictionMigration]:
    """严格读取只适用于已发布旧事实的迁移兼容清单。"""
    path = (
        Path(project_root)
        / "deploy"
        / "legacy_prediction_migration_compatibility_v1.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "entries"}
        or raw.get("schema_version")
        != "legacy-prediction-migration-compatibility-v1"
        or not isinstance(raw.get("entries"), list)
    ):
        raise ValueError("legacy prediction migration schema is invalid")

    result: set[LegacyPredictionMigration] = set()
    identities: set[tuple[str, str, int]] = set()
    for value in raw["entries"]:
        if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
            raise ValueError("legacy prediction migration entry is invalid")
        entry = LegacyPredictionMigration(**value)
        if not _valid_entry(entry):
            raise ValueError("legacy prediction migration fields are invalid")
        identity = (
            entry.prediction_scheme_id,
            entry.target_tenor,
            entry.horizon,
        )
        if identity in identities:
            raise ValueError("duplicate legacy prediction migration entry")
        identities.add(identity)
        result.add(entry)
    return frozenset(result)


def load_legacy_corrected_exact_evidence(
    project_root: str | Path,
) -> frozenset[LegacyCorrectedExactEvidence]:
    """读取由 archived/retired 来源冻结的纠正版逐事实证据。"""
    path = (
        Path(project_root)
        / "deploy"
        / "legacy_prediction_migration_evidence_v1.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "entries"}
        or raw.get("schema_version") != "legacy-corrected-exact-evidence-v1"
        or not isinstance(raw.get("entries"), list)
    ):
        raise ValueError("legacy corrected exact evidence schema is invalid")
    result: set[LegacyCorrectedExactEvidence] = set()
    identities: set[tuple[str, str]] = set()
    for value in raw["entries"]:
        evidence = _parse_corrected_exact_evidence(value)
        identity = (evidence.source_scheme_id, evidence.designated_exact)
        if identity in identities:
            raise ValueError("duplicate legacy corrected exact evidence")
        identities.add(identity)
        result.add(evidence)
    return frozenset(result)


def _valid_entry(entry: LegacyPredictionMigration) -> bool:
    scheme_ids = (
        entry.prediction_scheme_id,
        entry.historical_source_scheme_id,
        entry.designated_source_scheme_id,
    )
    hashes = (
        entry.designated_code_hash,
        entry.designated_config_hash,
        entry.designated_manifest_hash,
        entry.designated_corrected_facts_sha256,
        entry.input_artifact_hash,
        entry.backtest_summary_sha256,
        entry.source_facts_sha256,
        entry.product_facts_sha256,
        entry.target_contract_mismatch_sha256,
        entry.designated_live_facts_sha256,
        entry.product_live_facts_sha256,
    )
    return (
        all(
            isinstance(value, str) and SCHEME_ID_PATTERN.fullmatch(value)
            for value in scheme_ids
        )
        and entry.designated_source_scheme_id != entry.prediction_scheme_id
        and isinstance(entry.designated_exact, str)
        and _EXACT_PATTERN.fullmatch(entry.designated_exact) is not None
        and all(
            isinstance(value, str) and _SHA256_PATTERN.fullmatch(value)
            for value in hashes
        )
        and isinstance(entry.designated_input_artifact_id, str)
        and bool(
            re.fullmatch(
                r"snapshot-[0-9a-f]{24}",
                entry.designated_input_artifact_id,
            )
        )
        and isinstance(entry.target_tenor, str)
        and bool(re.fullmatch(r"[1-9][0-9]*Y", entry.target_tenor))
        and isinstance(entry.horizon, int)
        and not isinstance(entry.horizon, bool)
        and entry.horizon > 0
        and isinstance(entry.expected_fact_count, int)
        and not isinstance(entry.expected_fact_count, bool)
        and entry.expected_fact_count > 0
        and isinstance(entry.target_contract_mismatch_count, int)
        and not isinstance(entry.target_contract_mismatch_count, bool)
        and 0 <= entry.target_contract_mismatch_count <= entry.expected_fact_count
        and isinstance(entry.expected_live_fact_count, int)
        and not isinstance(entry.expected_live_fact_count, bool)
        and entry.expected_live_fact_count > 0
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            for value in (
                entry.live_target_date_from,
                entry.live_target_date_through,
            )
        )
        and entry.live_target_date_from <= entry.live_target_date_through
        and isinstance(entry.reason, str)
        and bool(entry.reason.strip())
        and entry.reason == entry.reason.strip()
    )


def _parse_corrected_exact_evidence(
    value: object,
) -> LegacyCorrectedExactEvidence:
    if not isinstance(value, dict) or set(value) != {
        "source_registry",
        "source_version",
        "source_backtest",
        "corrected_facts",
        "corrected_facts_sha256",
    }:
        raise ValueError("legacy corrected exact evidence entry is invalid")
    registry = value["source_registry"]
    version = value["source_version"]
    backtest = value["source_backtest"]
    facts = value["corrected_facts"]
    if (
        not isinstance(registry, dict)
        or set(registry) != {"base_scheme_id", "scheme_id", "status"}
        or not isinstance(version, dict)
        or set(version)
        != {
            "scheme_id",
            "scheme_version",
            "runtime_type",
            "code_hash",
            "config_hash",
            "manifest_hash",
            "status",
        }
        or not isinstance(backtest, dict)
        or set(backtest)
        != {
            "status",
            "benchmark_id",
            "data_source",
            "run_mode",
            "code_hash",
            "config_hash",
            "input_artifact_hash",
            "manifest_hash",
            "summary_sha256",
            "persisted_prediction_count",
        }
        or not isinstance(facts, list)
    ):
        raise ValueError("legacy corrected exact evidence entry is invalid")
    parsed_facts = tuple(_parse_corrected_fact(item) for item in facts)
    digest = value["corrected_facts_sha256"]
    evidence = LegacyCorrectedExactEvidence(
        source_registry_scheme_id=registry["scheme_id"],
        source_scheme_id=registry["base_scheme_id"],
        registry_status=registry["status"],
        designated_exact=version["scheme_version"],
        runtime_type=version["runtime_type"],
        version_status=version["status"],
        code_hash=version["code_hash"],
        config_hash=version["config_hash"],
        manifest_hash=version["manifest_hash"],
        input_artifact_id=backtest["input_artifact_hash"],
        backtest_status=backtest["status"],
        backtest_benchmark_id=backtest["benchmark_id"],
        backtest_data_source=backtest["data_source"],
        backtest_run_mode=backtest["run_mode"],
        backtest_summary_sha256=backtest["summary_sha256"],
        persisted_prediction_count=backtest["persisted_prediction_count"],
        corrected_facts=parsed_facts,
        corrected_facts_sha256=digest,
    )
    if (
        not SCHEME_ID_PATTERN.fullmatch(evidence.source_scheme_id)
        or not re.fullmatch(
            r"[A-Za-z0-9_]+", evidence.source_registry_scheme_id
        )
        or evidence.registry_status != "archived"
        or evidence.runtime_type != "blackbox_v2"
        or evidence.version_status != "retired"
        or evidence.backtest_status != "success"
        or not isinstance(evidence.backtest_benchmark_id, str)
        or not evidence.backtest_benchmark_id.strip()
        or evidence.backtest_data_source
        != "blackbox_v2_current_snapshot_as_of"
        or evidence.backtest_run_mode != "persist"
        or _EXACT_PATTERN.fullmatch(evidence.designated_exact) is None
        or not all(
            _SHA256_PATTERN.fullmatch(item)
            for item in (
                evidence.code_hash,
                evidence.config_hash,
                evidence.manifest_hash,
                evidence.backtest_summary_sha256,
                evidence.corrected_facts_sha256,
            )
        )
        or not re.fullmatch(r"snapshot-[0-9a-f]{24}", evidence.input_artifact_id)
        or evidence.persisted_prediction_count != len(parsed_facts)
        or _corrected_facts_digest(parsed_facts)
        != evidence.corrected_facts_sha256
        or any(fact.scheme_id != evidence.source_scheme_id for fact in parsed_facts)
        or backtest["code_hash"] != evidence.code_hash
        or backtest["config_hash"] != evidence.config_hash
        or backtest["manifest_hash"] != evidence.manifest_hash
        or version["scheme_id"] != evidence.source_scheme_id
    ):
        raise ValueError("legacy corrected exact evidence fields are invalid")
    return evidence


def _parse_corrected_fact(value: object) -> LegacyCorrectedFact:
    fields = {
        "scheme_id",
        "target_tenor",
        "horizon",
        "predict_date",
        "feature_date",
        "target_date",
        "predicted_direction",
        "actual_direction",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("legacy corrected fact is invalid")
    try:
        fact = LegacyCorrectedFact(**value)
    except TypeError as exc:
        raise ValueError("legacy corrected fact is invalid") from exc
    if (
        not SCHEME_ID_PATTERN.fullmatch(fact.scheme_id)
        or not re.fullmatch(r"[1-9][0-9]*Y", fact.target_tenor)
        or not isinstance(fact.horizon, int)
        or isinstance(fact.horizon, bool)
        or fact.horizon <= 0
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item)
            for item in (fact.predict_date, fact.feature_date, fact.target_date)
        )
        or fact.predicted_direction not in {-1, 0, 1}
        or fact.actual_direction not in {-1, 0, 1}
    ):
        raise ValueError("legacy corrected fact fields are invalid")
    return fact


def _corrected_facts_digest(
    facts: tuple[LegacyCorrectedFact, ...],
) -> str:
    payload = [
        {
            "scheme_id": fact.scheme_id,
            "target_tenor": fact.target_tenor,
            "horizon": fact.horizon,
            "predict_date": fact.predict_date,
            "feature_date": fact.feature_date,
            "target_date": fact.target_date,
            "predicted_direction": fact.predicted_direction,
            "actual_direction": fact.actual_direction,
        }
        for fact in facts
    ]
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
