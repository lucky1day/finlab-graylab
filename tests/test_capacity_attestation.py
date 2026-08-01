from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy
from datetime import date, timedelta


MACHINE_ID = "mac-studio-production-01"
POLICY_VERSION = "daily-scheduler-policy-v2"
V2_SCHEME_IDS = (
    "one_y_t5_liq_excess_a_v1",
    "one_y_t5_liq_excess_a_w252_l7_v1",
    "one_y_t5_liq_excess_a_w350_l7_v1",
    "one_y_t5_liq_excess_b_w252_l7_v1",
    "ten_y_t5_maj3_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_static_v1",
    "ten_y_t5_maj4_k3_ic_yearly_v1",
    "ten_y_t5_say_k5_sharpe_static_v1",
)
REQUIRED_FAULT_SCENARIOS = (
    "child_running_kill",
    "post_result_kill",
    "pre_commit_kill",
    "post_commit_pre_ack_kill",
    "timeout",
    "disk_full",
    "databridge_delay",
    "registry_drift",
    "generation_tamper",
    "late_source_write",
)
FAULT_EXPECTED_OUTCOMES = {
    "child_running_kill": "RECOVERED_SINGLE_WINNER",
    "post_result_kill": "RECOVERED_SINGLE_WINNER",
    "pre_commit_kill": "RECOVERED_SINGLE_WINNER",
    "post_commit_pre_ack_kill": "COMMIT_RECOGNIZED_NO_REEXECUTE",
    "timeout": "TERMINAL_TIMEOUT_NO_RETRY",
    "disk_full": "FAIL_CLOSED_NO_PARTIAL_PUBLISH",
    "databridge_delay": "WAITED_FOR_SAME_DAY_GENERATION",
    "registry_drift": "FROZEN_REGISTRY_REJECTED_DRIFT",
    "generation_tamper": "HASH_MISMATCH_FAIL_CLOSED",
    "late_source_write": "DATA_CONTRACT_BREACH_FROZEN",
}


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _batch(
    index: int,
    *,
    kind: str,
    forced_cold_minutes: float | None = None,
) -> dict[str, object]:
    business_day = date(2026, 1, 1) + timedelta(days=index)
    business_date = business_day.isoformat()
    previous = (
        None
        if index == 0
        else (business_day - timedelta(days=1)).isoformat()
    )
    generation_id = f"databridge-{business_date}"
    evidence_label = f"{kind}-{index:03d}"
    result = {
        "batch_id": f"{kind}-{index:03d}",
        "observation_kind": kind,
        **(
            {"capacity_qualification": "PRODUCTION_BOUND"}
            if kind == "production"
            else {}
        ),
        "occurrence_id": f"occurrence-{evidence_label}",
        "run_id": f"run-{evidence_label}",
        "execution_evidence": {
            "artifact_id": f"capacity-run-evidence-{evidence_label}",
            "sha256": _sha256(f"execution-evidence:{evidence_label}"),
        },
        "business_date": business_date,
        "previous_trading_date": previous,
        "machine_id": MACHINE_ID,
        "policy_version": POLICY_VERSION,
        "databridge_generation_id": generation_id,
        "databridge_published_at": f"{business_date}T06:54:00+08:00",
        "v2_results": [
            {
                "scheme_id": scheme_id,
                "input_generation_id": generation_id,
                "db_visible_at": f"{business_date}T07:08:00+08:00",
                "api_visible_at": f"{business_date}T07:09:00+08:00",
            }
            for scheme_id in V2_SCHEME_IDS
        ],
        "targets": [
            {
                "target_id": f"target-{target_index:02d}",
                "db_visible_at": f"{business_date}T07:53:00+08:00",
                "api_visible_at": f"{business_date}T07:54:00+08:00",
            }
            for target_index in range(29)
        ],
        "incidents": {
            "misfire": 0,
            "timeout": 0,
            "partial": 0,
            "duplicate_prediction": 0,
            "orphan_process": 0,
            "reentry_new_results": 0,
            "manual_intervention": 0,
        },
        **(
            {"forced_cold_end_to_end_minutes": forced_cold_minutes}
            if forced_cold_minutes is not None
            else {}
        ),
    }
    if kind == "forced_cold":
        result["cold_start_evidence"] = {
            "artifact_id": f"cold-start-evidence-{evidence_label}",
            "sha256": _sha256(f"cold-start-evidence:{evidence_label}"),
        }
    return result


def _revision(index: int, *, compare_passed: bool = True) -> dict[str, object]:
    label = f"revision-{index:03d}"
    return {
        "trial_id": label,
        "run_id": f"revision-run-{index:03d}",
        "business_date": f"2026-05-{index + 1:02d}",
        "machine_id": MACHINE_ID,
        "policy_version": POLICY_VERSION,
        "before_generation": {
            "generation_id": f"before-generation-{index:03d}",
            "sha256": _sha256(f"before-generation:{index:03d}"),
        },
        "after_generation": {
            "generation_id": f"after-generation-{index:03d}",
            "sha256": _sha256(f"after-generation:{index:03d}"),
        },
        "compare_gate": {
            "run_id": f"compare-gate-run-{index:03d}",
            "status": "PASS" if compare_passed else "FAIL",
            "evidence": {
                "artifact_id": f"compare-gate-evidence-{index:03d}",
                "sha256": _sha256(f"compare-gate-evidence:{index:03d}"),
            },
        },
    }


def _passing_payload(
    *,
    production_days: int = 1,
    forced_cold_samples: int = 1,
    revision_samples: int = 0,
) -> dict[str, object]:
    cold_minutes = [85.0] * forced_cold_samples
    forced_cold = [
        _batch(index, kind="forced_cold", forced_cold_minutes=minutes)
        for index, minutes in enumerate(cold_minutes)
    ]
    production = [
        _batch(index, kind="production")
        for index in range(production_days)
    ]
    return {
        "schema_version": "daily-capacity-observations-v2",
        "machine_id": MACHINE_ID,
        "policy_version": POLICY_VERSION,
        "daily_batches": forced_cold + production,
        "revision_trials": [
            _revision(index) for index in range(revision_samples)
        ],
    }


def _target_manifest() -> list[dict[str, object]]:
    """构造 25 个 execution / 29 个真实 composite target。"""
    rows: list[dict[str, object]] = []
    for scheme_id in V2_SCHEME_IDS:
        target_tenor = (
            "10Y"
            if scheme_id.startswith("ten_y_t5_")
            else "1Y"
        )
        rows.append(
            {
                "registry_scheme_id":
                    f"{scheme_id}__h5__{target_tenor}",
                "base_scheme_id": scheme_id,
                "runtime_type": "blackbox_v2",
                "target_tenor": target_tenor,
                "horizon": 5,
                "task_type": "T+5",
                "scheme_version": "contract-1.0",
                "code_sha256": _sha256(f"code:{scheme_id}"),
                "config_sha256": _sha256(f"config:{scheme_id}"),
                "cache_group": "databridge_v1:1Y",
                "cache_spec_fingerprint": None,
                "cache_adapter_sha256": None,
                "cache_core_sha256": None,
            }
        )
    native_tenors = {
        0: ("5Y", "10Y"),
        1: ("3Y", "5Y", "7Y", "10Y"),
    }
    for index in range(17):
        scheme_id = f"native-{index:02d}"
        cache_qualified = index < 10
        for tenor in native_tenors.get(index, ("5Y",)):
            rows.append(
                {
                    "registry_scheme_id":
                        f"{scheme_id}__h1__{tenor}",
                    "base_scheme_id": scheme_id,
                    "runtime_type": "native_adapter",
                    "target_tenor": tenor,
                    "horizon": 1,
                    "task_type": "T+1",
                    "scheme_version": "native-v1",
                    "code_sha256": _sha256(f"code:{scheme_id}"),
                    "config_sha256": _sha256(f"config:{scheme_id}"),
                    "cache_group": (
                        f"cache-{scheme_id}:5Y"
                        if cache_qualified
                        else f"native:{scheme_id}"
                    ),
                    "cache_spec_fingerprint": (
                        _sha256(f"spec:{scheme_id}")
                        if cache_qualified
                        else None
                    ),
                    "cache_adapter_sha256": (
                        _sha256(f"adapter:{scheme_id}")
                        if cache_qualified
                        else None
                    ),
                    "cache_core_sha256": (
                        _sha256(f"core:{scheme_id}")
                        if cache_qualified
                        else None
                    ),
                }
            )
    return sorted(rows, key=lambda row: str(row["registry_scheme_id"]))


def _candidate(*, policy_sha256: str | None = None) -> dict[str, object]:
    from scheduler.capacity_attestation import (
        CANDIDATE_SCHEMA_VERSION,
        canonical_json_sha256,
    )

    targets = _target_manifest()
    registry_manifest_sha256 = canonical_json_sha256(targets)
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "machine_id": MACHINE_ID,
        "hardware_model": "Mac14,13",
        "os_build": "25G88",
        "memory_bytes": 64 * 1024**3,
        "policy_version": POLICY_VERSION,
        "policy_sha256": policy_sha256 or _sha256("policy"),
        "registry_manifest_sha256": registry_manifest_sha256,
        "expected_item_count": 25,
        "expected_target_count": 29,
        "target_manifest": targets,
        "scheduler_release_sha256": _sha256("scheduler-release"),
        "scheme_bundle_sha256": _sha256("scheme-bundle"),
        "service_environment_sha256": _sha256("service-environment"),
        "algorithm_environment_sha256": _sha256(
            "algorithm-environment"
        ),
        "runtime_profile_sha256": _sha256("runtime-profile"),
        "migration_set_sha256": _sha256("migration-set"),
        "native_exporter_sha256": _sha256("native-exporter"),
        "databridge_exporter_sha256": _sha256(
            "databridge-exporter"
        ),
        "database_identity_sha256": _sha256("database-identity"),
        "control_plane_identity_sha256": _sha256(
            "control-plane-identity"
        ),
    }


def _attested_payload(
    *,
    production_days: int = 1,
    policy_sha256: str | None = None,
) -> dict[str, object]:
    from scheduler.capacity_attestation import candidate_fingerprint

    candidate = _candidate(policy_sha256=policy_sha256)
    fingerprint = candidate_fingerprint(candidate)
    observations = _passing_payload(production_days=production_days)
    target_ids = [
        str(row["registry_scheme_id"])
        for row in candidate["target_manifest"]
    ]
    for batch in observations["daily_batches"]:
        batch["candidate_fingerprint"] = fingerprint
        business_date = batch["business_date"]
        batch["targets"] = [
            {
                "target_id": target_id,
                "db_visible_at": f"{business_date}T07:53:00+08:00",
                "api_visible_at": f"{business_date}T07:54:00+08:00",
            }
            for target_id in target_ids
        ]
    for trial in observations["revision_trials"]:
        trial["candidate_fingerprint"] = fingerprint
    fault_trials = [
        {
            "trial_id": f"fault-{index:02d}",
            "scenario": scenario,
            "business_date": f"2026-06-{index + 1:02d}",
            "candidate_fingerprint": fingerprint,
            "run_id": f"fault-run-{index:02d}",
            "expected_outcome_code": FAULT_EXPECTED_OUTCOMES[scenario],
            "observed_outcome_code": FAULT_EXPECTED_OUTCOMES[scenario],
            "status": "PASS",
            "evidence": {
                "artifact_id": f"fault-evidence-{index:02d}",
                "sha256": _sha256(f"fault-evidence:{index:02d}"),
            },
        }
        for index, scenario in enumerate(REQUIRED_FAULT_SCENARIOS)
    ]
    from shared.liwei_0616_cache_contract import (
        qualification_corpus_sha256,
    )

    forced_ids = sorted(
        str(row["batch_id"])
        for row in observations["daily_batches"]
        if row["observation_kind"] == "forced_cold"
    )
    revision_ids = sorted(
        str(row["trial_id"])
        for row in observations["revision_trials"]
    )
    coverage = ["forced_cold", "append", "full_rebuild"]
    qualification_rows = []
    cached_targets = {
        str(row["base_scheme_id"]): row
        for row in candidate["target_manifest"]
        if row["cache_spec_fingerprint"] is not None
    }
    for base_id, target in sorted(cached_targets.items()):
        cache_family, cache_tenor = str(
            target["cache_group"]
        ).rsplit(":", 1)
        qualification_rows.append(
            {
                "schema_version":
                    "liwei-0616-cache-use-qualification-v1",
                "status": "PASSED",
                "source": "signed_capacity_corpus",
                "qualification_id": f"qualification-{base_id}",
                "base_scheme_id": base_id,
                "scheme_version": target["scheme_version"],
                "code_sha256": target["code_sha256"],
                "config_sha256": target["config_sha256"],
                "cache_group": target["cache_group"],
                "cache_family": cache_family,
                "tenor": cache_tenor,
                "cache_adapter_sha256":
                    target["cache_adapter_sha256"],
                "cache_core_sha256": target["cache_core_sha256"],
                "spec_fingerprint":
                    target["cache_spec_fingerprint"],
                "cache_abi_version": "liwei_0616.phase_a.v1",
                "algorithm_environment_sha256":
                    candidate["algorithm_environment_sha256"],
                "capacity_candidate_fingerprint": fingerprint,
                "native_exporter_sha256":
                    candidate["native_exporter_sha256"],
                "native_generation_schema_version":
                    "native-generation-v1",
                "native_exporter_version":
                    "native-generation-exporter-v1",
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
                "comparison_evidence_sha256":
                    _sha256(f"comparison:{base_id}"),
                "corpus": {
                    "forced_cold_count": len(forced_ids),
                    "revision_count": len(revision_ids),
                    "coverage_types": coverage,
                    "forced_cold_trial_ids": forced_ids,
                    "revision_trial_ids": revision_ids,
                    "evidence_sha256":
                        qualification_corpus_sha256(
                            forced_cold_trial_ids=forced_ids,
                            revision_trial_ids=revision_ids,
                            coverage_types=coverage,
                        ),
                },
            }
        )
    return {
        "schema_version": "daily-capacity-attested-evidence-v2",
        "candidate": candidate,
        "candidate_fingerprint": fingerprint,
        "observations": observations,
        "fault_trials": fault_trials,
        "cache_use_qualifications": qualification_rows,
    }



class CapacityAttestationContractTests(unittest.TestCase):
    def test_daily_policy_v2_constants_are_authoritative(self) -> None:
        from scheduler import capacity_attestation, daily_policy

        self.assertTrue(
            hasattr(daily_policy, "DAILY_POLICY_V2_VERSION"),
            "daily policy must expose one authoritative v2 identity",
        )
        self.assertEqual(
            capacity_attestation.SUPPORTED_POLICY_VERSION,
            daily_policy.DAILY_POLICY_V2_VERSION,
        )
        self.assertEqual(
            capacity_attestation.EXPECTED_TARGET_COUNT,
            daily_policy.EXPECTED_V2_TARGET_COUNT,
        )
        self.assertEqual(
            capacity_attestation.EXPECTED_V2_SCHEME_IDS,
            frozenset(
                daily_policy.EXPECTED_POLICY_V2_RELEASE_OFFSETS_BY_SCHEME
            ),
        )

    def test_attested_evidence_binds_exact_candidate_targets_and_faults(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            validate_attested_capacity_evidence,
        )

        validated = validate_attested_capacity_evidence(
            _attested_payload(),
            expected_machine_id=MACHINE_ID,
            expected_policy_version=POLICY_VERSION,
            expected_policy_sha256=_sha256("policy"),
        )

        self.assertEqual(
            len(validated.candidate["target_manifest"]),
            29,
        )
        self.assertEqual(
            len(
                {
                    row["base_scheme_id"]
                    for row in validated.candidate["target_manifest"]
                }
            ),
            25,
        )
        self.assertEqual(
            {row["scenario"] for row in validated.fault_trials},
            set(REQUIRED_FAULT_SCENARIOS),
        )
        self.assertEqual(
            set(validated.cache_use_qualifications),
            {f"native-{index:02d}" for index in range(10)},
        )

    def test_candidate_rejects_legacy_cardinality_and_v2_set(self) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            canonical_json_sha256,
            validate_capacity_candidate,
        )

        v1 = _candidate()
        v1["policy_version"] = "daily-scheduler-policy-v1"
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "daily-scheduler-policy-v2",
        ):
            validate_capacity_candidate(v1)

        legacy = _candidate()
        legacy["expected_item_count"] = 21
        legacy["expected_target_count"] = 25
        legacy["target_manifest"] = [
            row
            for row in legacy["target_manifest"]
            if not str(row["base_scheme_id"]).startswith("ten_y_t5_")
        ][:25]
        legacy["registry_manifest_sha256"] = canonical_json_sha256(
            legacy["target_manifest"]
        )

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "25 items / 29 targets",
        ):
            validate_capacity_candidate(legacy)

    def test_cache_qualification_set_and_candidate_binding_are_exact(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )
        from shared.liwei_0616_cache_contract import (
            qualification_corpus_sha256,
        )

        cases = {}
        missing = _attested_payload()
        missing["cache_use_qualifications"].pop()
        cases["missing"] = missing
        drifted = _attested_payload()
        drifted["cache_use_qualifications"][0][
            "cache_adapter_sha256"
        ] = _sha256("different-adapter")
        cases["adapter"] = drifted

        for name, payload in cases.items():
            with self.subTest(name=name), self.assertRaises(
                CapacityAttestationError
            ):
                validate_attested_capacity_evidence(payload)

        forged_corpus = _attested_payload()
        corpus = forged_corpus["cache_use_qualifications"][0]["corpus"]
        corpus["revision_trial_ids"] = ["revision-not-in-observations"]
        corpus["revision_count"] = 1
        corpus["evidence_sha256"] = qualification_corpus_sha256(
            forced_cold_trial_ids=corpus["forced_cold_trial_ids"],
            revision_trial_ids=corpus["revision_trial_ids"],
            coverage_types=corpus["coverage_types"],
        )
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "corpus IDs do not match signed observations",
        ):
            validate_attested_capacity_evidence(forged_corpus)

    def test_fake_or_non_composite_target_cannot_qualify(self) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )

        payload = _attested_payload()
        payload["observations"]["daily_batches"][0]["targets"][0][
            "target_id"
        ] = "target-00"

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "target manifest",
        ):
            validate_attested_capacity_evidence(payload)

    def test_all_required_fault_scenarios_must_be_present_once(self) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )

        missing = _attested_payload()
        missing["fault_trials"].pop()
        duplicated = _attested_payload()
        duplicated_scenario = duplicated["fault_trials"][0]["scenario"]
        duplicated["fault_trials"][-1]["scenario"] = duplicated_scenario
        duplicated["fault_trials"][-1]["expected_outcome_code"] = (
            FAULT_EXPECTED_OUTCOMES[duplicated_scenario]
        )
        duplicated["fault_trials"][-1]["observed_outcome_code"] = (
            FAULT_EXPECTED_OUTCOMES[duplicated_scenario]
        )

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "fault scenario",
        ):
            validate_attested_capacity_evidence(missing)
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "fault scenario",
        ):
            validate_attested_capacity_evidence(duplicated)

    def test_every_observation_is_bound_to_candidate_fingerprint(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )

        payload = _attested_payload()
        payload["observations"]["daily_batches"][0][
            "candidate_fingerprint"
        ] = _sha256("another-candidate")

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "candidate_fingerprint",
        ):
            validate_attested_capacity_evidence(payload)

    def test_fault_trials_cannot_self_declare_an_arbitrary_outcome(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )

        payload = _attested_payload()
        payload["fault_trials"][0]["expected_outcome_code"] = "MADE_UP"
        payload["fault_trials"][0]["observed_outcome_code"] = "MADE_UP"

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "expected outcome",
        ):
            validate_attested_capacity_evidence(payload)

    def test_exact_policy_hash_is_part_of_candidate_binding(self) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_attested_capacity_evidence,
        )

        with self.assertRaisesRegex(
            CapacityAttestationError,
            "policy_sha256",
        ):
            validate_attested_capacity_evidence(
                _attested_payload(),
                expected_policy_sha256=_sha256("different-policy"),
            )

    def test_runtime_can_recompute_and_verify_the_signed_candidate(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            validate_capacity_candidate,
            verify_current_candidate,
        )

        candidate = _candidate()
        parsed = validate_capacity_candidate(candidate)
        current = verify_current_candidate(
            parsed.payload,
            lambda: deepcopy(candidate),
        )
        self.assertEqual(current.fingerprint, parsed.fingerprint)
        self.assertEqual(len(current.target_ids), 29)

        drifted = deepcopy(candidate)
        drifted["scheduler_release_sha256"] = _sha256(
            "different-release"
        )
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "differs from signed candidate",
        ):
            verify_current_candidate(parsed.payload, drifted)

    def test_canonical_candidate_builder_defines_every_digest(
        self,
    ) -> None:
        from scheduler.capacity_attestation import (
            COMPONENT_DIGEST_FIELDS,
            CapacityAttestationError,
            build_capacity_candidate,
            canonical_artifact_set_sha256,
            validate_capacity_candidate,
        )

        artifacts = {
            field: {
                "z-last.bin": _sha256(f"{field}:z"),
                "a-first.bin": _sha256(f"{field}:a"),
            }
            for field in COMPONENT_DIGEST_FIELDS
        }
        built = build_capacity_candidate(
            machine_id=MACHINE_ID,
            hardware_model="Mac14,13",
            os_build="25G88",
            memory_bytes=64 * 1024**3,
            policy_version=POLICY_VERSION,
            policy_bytes=b'{"version":"daily-scheduler-policy-v2"}',
            target_manifest=_target_manifest(),
            component_artifacts=artifacts,
        )
        reordered = {
            field: dict(reversed(tuple(entries.items())))
            for field, entries in reversed(tuple(artifacts.items()))
        }
        rebuilt = build_capacity_candidate(
            machine_id=MACHINE_ID,
            hardware_model="Mac14,13",
            os_build="25G88",
            memory_bytes=64 * 1024**3,
            policy_version=POLICY_VERSION,
            policy_bytes=b'{"version":"daily-scheduler-policy-v2"}',
            target_manifest=deepcopy(_target_manifest()),
            component_artifacts=reordered,
        )

        self.assertEqual(built.payload, rebuilt.payload)
        self.assertEqual(built.fingerprint, rebuilt.fingerprint)
        for field, entries in artifacts.items():
            self.assertEqual(
                built.payload[field],
                canonical_artifact_set_sha256(field, entries),
            )

        invalid_memory = _candidate()
        invalid_memory["memory_bytes"] = 0
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "memory_bytes",
        ):
            validate_capacity_candidate(invalid_memory)


if __name__ == "__main__":
    unittest.main()
