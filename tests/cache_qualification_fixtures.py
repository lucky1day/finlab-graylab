from __future__ import annotations

from shared import liwei_0616_phase_a_cache as cache_module
from shared.liwei_0616_cache_contract import (
    cache_use_qualification_sha256,
    qualification_corpus_sha256,
)
from shared.liwei_0616_phase_a_cache import PhaseACacheSpec


def trusted_cache_use_qualification(
    spec: PhaseACacheSpec,
    *,
    base_scheme_id: str,
) -> dict[str, object]:
    """构造执行器边界测试共用的可信缓存资格证明。"""
    forced_ids = [f"forced-{index:02d}" for index in range(20)]
    revision_ids = [f"revision-{index:02d}" for index in range(20)]
    coverage = ["forced_cold", "append", "full_rebuild"]
    qualification = {
        "schema_version": "liwei-0616-cache-use-qualification-v1",
        "status": "PASSED",
        "source": "signed_capacity_corpus",
        "qualification_id": f"qualification-{base_scheme_id}",
        "base_scheme_id": base_scheme_id,
        "scheme_version": "scheme-version-v1",
        "code_sha256": "3" * 64,
        "config_sha256": "4" * 64,
        "cache_group": f"{spec.cache_family}:{spec.tenor}",
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "cache_adapter_sha256": "5" * 64,
        "cache_core_sha256": "6" * 64,
        "spec_fingerprint": cache_module._spec_fingerprint(spec),
        "cache_abi_version": "liwei_0616.phase_a.v1",
        "algorithm_environment_sha256": "7" * 64,
        "capacity_candidate_fingerprint": "8" * 64,
        "native_exporter_sha256": "9" * 64,
        "native_generation_schema_version": "native-generation-v1",
        "native_exporter_version": "native-generation-exporter-v1",
        "daily_dependency_lookback_rows": spec.daily_dependency_lookback_rows,
        "daily_dependency_proof": spec.daily_dependency_proof,
        "comparison_fields": [
            "direction",
            "vote_score",
            "baseline_score",
            "baseline_sign",
            "probability",
            "confidence",
            "internal_fields",
            "phase_a_cache",
        ],
        "comparison_evidence_sha256": "a" * 64,
        "corpus": {
            "forced_cold_count": 20,
            "revision_count": 20,
            "coverage_types": coverage,
            "forced_cold_trial_ids": forced_ids,
            "revision_trial_ids": revision_ids,
            "evidence_sha256": qualification_corpus_sha256(
                forced_cold_trial_ids=forced_ids,
                revision_trial_ids=revision_ids,
                coverage_types=coverage,
            ),
        },
    }
    return {
        "schema_version": "liwei-0616-trusted-cache-use-qualification-v1",
        "qualification": qualification,
        "qualification_sha256": cache_use_qualification_sha256(qualification),
        "admission_decision_id": "decision-20260724",
        "admission_evidence_sha256": "c" * 64,
        "collector_signer_sha256": "d" * 64,
        "operator_signer_sha256": "e" * 64,
        "candidate_fingerprint": "8" * 64,
    }
