from __future__ import annotations

import copy
import hashlib
import json
import unittest


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _qualification() -> dict[str, object]:
    from shared.liwei_0616_cache_contract import (
        qualification_corpus_sha256,
    )

    forced_ids = ["forced-00"]
    revision_ids: list[str] = []
    coverage = ["forced_cold", "append", "full_rebuild"]
    return {
        "schema_version": "liwei-0616-cache-use-qualification-v1",
        "status": "PASSED",
        "source": "signed_capacity_corpus",
        "qualification_id": "qual-liwei-10y01-20260724",
        "base_scheme_id": "liwei_0616_10y01_cons_say_k3_div_k10",
        "scheme_version": "scheme-version-1",
        "code_sha256": SHA_A,
        "config_sha256": SHA_B,
        "cache_group": "liwei_0616_10y_v61:10Y",
        "cache_family": "liwei_0616_10y_v61",
        "tenor": "10Y",
        "cache_adapter_sha256": SHA_C,
        "cache_core_sha256": SHA_D,
        "spec_fingerprint": SHA_E,
        "cache_abi_version": "liwei_0616.phase_a.v1",
        "algorithm_environment_sha256": SHA_F,
        "capacity_candidate_fingerprint": SHA_A,
        "native_exporter_sha256": SHA_B,
        "native_generation_schema_version": "native-source-generation-v1",
        "native_exporter_version": "native-source-exporter-v1",
        "daily_dependency_lookback_rows": None,
        "daily_dependency_proof": None,
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
        "comparison_evidence_sha256": SHA_C,
        "corpus": {
            "forced_cold_count": 1,
            "revision_count": 0,
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


def _generation_acceptance() -> dict[str, object]:
    payload = {
        "schema_version":
            "liwei-0616-generation-acceptance-v1",
        "status": "ACCEPTED",
        "native_generation": {
            "generation_id": "native-20260724",
            "manifest_sha256": SHA_B,
            "dataset_content_id": SHA_C,
            "business_date": "2026-07-24",
            "feature_date": "2026-07-23",
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        },
        "input_content_id": SHA_A,
        "parent": {
            "generation_id": "cache-parent",
            "manifest_sha256": SHA_E,
            "generation_content_id": SHA_F,
        },
        "candidate_content_id": SHA_D,
        "build_mode": "append",
        "input_change": {
            "change_type": "append",
            "frames": {
                "daily": {
                    "change_type": "append",
                    "earliest_changed_key": "2026-07-23",
                    "schema_changed": False,
                },
                "weekly": {
                    "change_type": "unchanged",
                    "earliest_changed_key": None,
                    "schema_changed": False,
                },
                "monthly": {
                    "change_type": "unchanged",
                    "earliest_changed_key": None,
                    "schema_changed": False,
                },
            },
            "suffix_start_date": None,
            "native_generation_changed": True,
        },
        "baselines": {
            "STD": {
                "affected_dates": ["2026-07-23"],
                "authoritative_scope_sha256": SHA_A,
                "candidate_scope_sha256": SHA_A,
                "preserved_dates": ["2026-07-22"],
                "parent_preserved_sha256": SHA_B,
                "candidate_preserved_sha256": SHA_B,
                "scope_equal": True,
                "preserved_equal": True,
            }
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        **payload,
        "evidence_sha256": hashlib.sha256(encoded).hexdigest(),
    }


class CacheUseQualificationContractTests(unittest.TestCase):
    def test_valid_qualification_is_per_consumer_and_corpus_gated(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            cache_use_qualification_sha256,
            validate_cache_use_qualification,
        )

        validated = validate_cache_use_qualification(
            _qualification(),
            expected_base_scheme_id=(
                "liwei_0616_10y01_cons_say_k3_div_k10"
            ),
            expected_candidate_fingerprint=SHA_A,
        )

        self.assertEqual(
            validated["base_scheme_id"],
            "liwei_0616_10y01_cons_say_k3_div_k10",
        )
        self.assertEqual(len(cache_use_qualification_sha256(validated)), 64)

    def test_qualification_rejects_other_consumer_even_same_cache_family(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_cache_use_qualification,
        )

        with self.assertRaisesRegex(ValueError, "base_scheme_id"):
            validate_cache_use_qualification(
                _qualification(),
                expected_base_scheme_id=(
                    "liwei_0616_10y02_cons_say_k3_div_k5"
                ),
                expected_candidate_fingerprint=SHA_A,
            )

    def test_qualification_rejects_insufficient_or_incomplete_corpus(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_cache_use_qualification,
        )

        cases = {
            "forced": ("forced_cold_count", 0),
            "coverage": (
                "coverage_types",
                ["forced_cold", "append"],
            ),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                raw = _qualification()
                raw["corpus"][field] = value
                with self.assertRaises(ValueError):
                    validate_cache_use_qualification(raw)

    def test_suffix_dependency_requires_real_suffix_corpus(self) -> None:
        from shared.liwei_0616_cache_contract import (
            qualification_corpus_sha256,
            validate_cache_use_qualification,
        )

        raw = _qualification()
        raw["daily_dependency_lookback_rows"] = 1
        raw["daily_dependency_proof"] = "one-row causal suffix proof"
        with self.assertRaisesRegex(ValueError, "proven_suffix"):
            validate_cache_use_qualification(raw)

        coverage = [
            "forced_cold",
            "append",
            "proven_suffix",
            "full_rebuild",
        ]
        raw["corpus"]["coverage_types"] = coverage
        raw["corpus"]["evidence_sha256"] = (
            qualification_corpus_sha256(
                forced_cold_trial_ids=raw["corpus"][
                    "forced_cold_trial_ids"
                ],
                revision_trial_ids=raw["corpus"][
                    "revision_trial_ids"
                ],
                coverage_types=coverage,
            )
        )
        validate_cache_use_qualification(raw)

    def test_trusted_envelope_binds_double_signed_admission_identity(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            cache_use_qualification_sha256,
            validate_trusted_cache_use_qualification,
        )

        qualification = _qualification()
        raw = {
            "schema_version":
                "liwei-0616-trusted-cache-use-qualification-v1",
            "qualification": qualification,
            "qualification_sha256":
                cache_use_qualification_sha256(qualification),
            "admission_decision_id": "capacity-decision-42",
            "admission_evidence_sha256": SHA_E,
            "collector_signer_sha256": SHA_F,
            "operator_signer_sha256": SHA_D,
            "candidate_fingerprint": SHA_A,
        }

        validated = validate_trusted_cache_use_qualification(
            raw,
            expected_base_scheme_id=(
                "liwei_0616_10y01_cons_say_k3_div_k10"
            ),
            expected_candidate_fingerprint=SHA_A,
        )
        self.assertEqual(
            validated["qualification_sha256"],
            cache_use_qualification_sha256(qualification),
        )

        forged = copy.deepcopy(raw)
        forged["qualification"]["comparison_evidence_sha256"] = SHA_F
        with self.assertRaisesRegex(ValueError, "qualification_sha256"):
            validate_trusted_cache_use_qualification(forged)

    def test_prediction_cache_audit_must_match_frozen_consumer_and_admission(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            cache_use_qualification_sha256,
            validate_prediction_cache_audit,
        )

        qualification = _qualification()
        expected = {
            "schema_version":
                "liwei-0616-trusted-cache-use-qualification-v1",
            "qualification": qualification,
            "qualification_sha256":
                cache_use_qualification_sha256(qualification),
            "admission_decision_id": "capacity-decision-42",
            "admission_evidence_sha256": SHA_E,
            "collector_signer_sha256": SHA_F,
            "operator_signer_sha256": SHA_D,
            "candidate_fingerprint": SHA_A,
        }
        audit = {
            "capacity_eligible": True,
            "cache_use_qualification": {
                "base_scheme_id": qualification["base_scheme_id"],
                "qualification_id": qualification["qualification_id"],
                "qualification_sha256":
                    expected["qualification_sha256"],
                "admission_decision_id":
                    expected["admission_decision_id"],
                "admission_evidence_sha256":
                    expected["admission_evidence_sha256"],
                "candidate_fingerprint":
                    expected["candidate_fingerprint"],
            },
            "generation_acceptance": _generation_acceptance(),
        }

        validate_prediction_cache_audit(
            {"phase_a_cache": audit},
            expected_qualification=expected,
            expected_native_generation=(
                _generation_acceptance()["native_generation"]
            ),
        )

        stale = copy.deepcopy(audit)
        stale["cache_use_qualification"]["admission_decision_id"] = (
            "old-decision"
        )
        with self.assertRaisesRegex(ValueError, "admission_decision_id"):
            validate_prediction_cache_audit(
                {"phase_a_cache": stale},
                expected_qualification=expected,
                expected_native_generation=(
                    _generation_acceptance()["native_generation"]
                ),
            )
        unknown = copy.deepcopy(audit)
        unknown["cache_use_qualification"]["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            validate_prediction_cache_audit(
                {"phase_a_cache": unknown},
                expected_qualification=expected,
                expected_native_generation=(
                    _generation_acceptance()["native_generation"]
                ),
            )

    def test_generation_acceptance_recomputes_digest_and_exact_scopes(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_generation_acceptance_record,
        )

        raw = _generation_acceptance()
        validated = validate_generation_acceptance_record(
            raw,
            expected_native_generation=raw["native_generation"],
        )
        self.assertEqual(validated, raw)

        for mutation in ("digest", "parent", "scope", "unknown"):
            with self.subTest(mutation=mutation):
                forged = copy.deepcopy(raw)
                if mutation == "digest":
                    forged["evidence_sha256"] = SHA_A
                elif mutation == "parent":
                    forged["parent"]["manifest_sha256"] = SHA_A
                elif mutation == "scope":
                    forged["baselines"]["STD"][
                        "candidate_scope_sha256"
                    ] = SHA_B
                else:
                    forged["unexpected"] = True
                with self.assertRaises(ValueError):
                    validate_generation_acceptance_record(forged)


if __name__ == "__main__":
    unittest.main()
