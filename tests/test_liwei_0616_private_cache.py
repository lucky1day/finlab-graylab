from __future__ import annotations

import copy
import hashlib
import json
import pickle
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import shared.liwei_0616_phase_a_cache as phase_a_cache_module
from shared.liwei_0616_cache_contract import (
    CACHE_MUTATION_POLICY_ENV,
    CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    canonical_json_bytes,
    validate_generation_acceptance_record,
)
from shared.liwei_0616_phase_a_cache import (
    CacheCapacityError,
    DAILY_REVISION_SUFFIX_PROOF_V1,
    PhaseACacheSpec,
    _LoadedGeneration,
    _baseline_fingerprint,
    _build_generation_acceptance_evidence,
    _frame_prefix_fingerprint,
    _input_change_analysis,
    _input_generation_state,
    _legacy_build_decision,
    _lineage_build_mode,
    _load_current_generation,
    _matching_generation_spec,
    _phase_a_cache_evidence,
    _phase_a_caches_equal,
    _revision_build_decision,
    _spec_fingerprint,
    _validate_daily_dependency_proof,
    _verify_migration_rebind_lineage,
    prepare_phase_a_caches,
    runtime_compare_gate_callbacks,
)


def _acceptance_record() -> dict[str, object]:
    frame = {
        "change_type": "initial",
        "earliest_changed_key": None,
        "schema_changed": False,
    }
    return _build_generation_acceptance_evidence(
        input_content_id="a" * 64,
        parent_generation=None,
        candidate_content_id="b" * 64,
        build_mode="full",
        input_change={
            "change_type": "initial",
            "raw_change_type": "initial",
            "frames": {
                name: dict(frame)
                for name in ("daily", "weekly", "monthly")
            },
            "effective_auxiliary": {
                "change_type": "unavailable",
                "earliest_changed_key": None,
                "schema_changed": False,
            },
            "date_to_week": {
                "change_type": "unavailable",
                "earliest_changed_key": None,
            },
            "projection_status": "absent",
            "suffix_start_date": None,
            "native_generation_changed": False,
        },
        scopes={
            "baseline": {
                "affected_dates": [],
                "authoritative_scope_sha256": None,
                "candidate_scope_sha256": "c" * 64,
                "preserved_dates": [],
                "parent_preserved_sha256": None,
                "candidate_preserved_sha256": None,
                "scope_equal": True,
                "preserved_equal": True,
            }
        },
    )


def _refresh_evidence_sha(record: dict[str, object]) -> None:
    payload = {
        key: value
        for key, value in record.items()
        if key != "evidence_sha256"
    }
    record["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()


def _migration_receipt_entry(
    *,
    cache_family: str,
    tenor: str,
    publisher_consumer_id: str,
    spec_fingerprint: str,
    parent_generation_id: str,
    parent_manifest_sha256: str,
    parent_generation_content_id: str,
    parent_input_content_id: str,
    target_input_content_id: str,
    baselines: dict[str, object],
) -> dict[str, object]:
    entry: dict[str, object] = {
        "cache_family": cache_family,
        "tenor": tenor,
        "publisher_consumer_id": publisher_consumer_id,
        "spec_fingerprint": spec_fingerprint,
        "parent_generation_id": parent_generation_id,
        "parent_manifest_sha256": parent_manifest_sha256,
        "parent_generation_content_id": parent_generation_content_id,
        "parent_input_content_id": parent_input_content_id,
        "target_input_content_id": target_input_content_id,
        "baselines": baselines,
    }
    entry["entry_sha256"] = hashlib.sha256(
        canonical_json_bytes(entry)
    ).hexdigest()
    return entry


def test_phase_a_acceptance_is_permanently_unbound() -> None:
    record = _acceptance_record()

    assert validate_generation_acceptance_record(record) == record
    assert record["native_generation"] is None
    assert record["status"] == "NON_PRODUCTION"


def test_phase_a_rejects_generation_binding_and_rebind() -> None:
    bound = _acceptance_record()
    bound["native_generation"] = {"generation_id": "retired"}
    bound["status"] = "ACCEPTED"
    _refresh_evidence_sha(bound)
    with pytest.raises(ValueError, match="unbound"):
        validate_generation_acceptance_record(bound)

    changed = _acceptance_record()
    changed["input_change"]["native_generation_changed"] = True
    _refresh_evidence_sha(changed)
    with pytest.raises(ValueError, match="must be false"):
        validate_generation_acceptance_record(changed)

    rebound = _acceptance_record()
    rebound["build_mode"] = "rebind"
    _refresh_evidence_sha(rebound)
    with pytest.raises(ValueError, match="build_mode"):
        validate_generation_acceptance_record(rebound)

    migration_rebind = _acceptance_record()
    migration_rebind["build_mode"] = "migration_rebind"
    _refresh_evidence_sha(migration_rebind)
    assert (
        validate_generation_acceptance_record(migration_rebind)
        == migration_rebind
    )


def test_existing_migration_rebind_lineage_remains_readable() -> None:
    spec = PhaseACacheSpec(
        cache_family="historical_migration_rebind",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    cache = {
        "test_dates": ["2026-01-02"],
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray([1], dtype=np.int32),
                "probs": np.asarray([0.75], dtype=np.float64),
            }
        ],
    }
    baseline_evidence = _phase_a_cache_evidence(cache)
    spec_fingerprint = _spec_fingerprint(spec)
    parent = _LoadedGeneration(
        generation_id="generation-parent",
        path=Path("/unused/parent"),
        manifest={
            "generation_content_id": "a" * 64,
            "input_state": {"content_id": "b" * 64},
            "spec_fingerprint": spec_fingerprint,
            "build_mode": "full",
            "baselines": {
                "baseline": {"evidence": baseline_evidence}
            },
        },
        manifest_sha256="c" * 64,
        caches={"baseline": cache},
    )
    generation = _LoadedGeneration(
        generation_id="generation-child",
        path=Path("/unused/child"),
        manifest={
            "generation_content_id": "d" * 64,
            "input_state": {"content_id": "e" * 64},
            "spec_fingerprint": spec_fingerprint,
            "build_mode": "migration_rebind",
            "baselines": {
                "baseline": {"evidence": baseline_evidence}
            },
        },
        manifest_sha256="f" * 64,
        caches={"baseline": cache},
    )
    entry = _migration_receipt_entry(
        cache_family=spec.cache_family,
        tenor=spec.tenor,
        publisher_consumer_id=spec.publisher_consumer_id,
        spec_fingerprint=spec_fingerprint,
        parent_generation_id=parent.generation_id,
        parent_manifest_sha256=parent.manifest_sha256,
        parent_generation_content_id=parent.manifest[
            "generation_content_id"
        ],
        parent_input_content_id=parent.manifest["input_state"][
            "content_id"
        ],
        target_input_content_id=generation.manifest["input_state"][
            "content_id"
        ],
        baselines={"baseline": baseline_evidence},
    )

    _verify_migration_rebind_lineage(
        generation=generation,
        parent=parent,
        spec=spec,
        migration_rebind_evidence={
            "schema_version": "liwei-0616-migration-rebind-v1",
            "migration_id": "aliyun-linux-x86_64-20260817-v1",
            "receipt_sha256": "1" * 64,
            "entry": entry,
        },
    )


def test_private_consumer_can_build_in_explicit_private_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setenv(
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )
    spec = PhaseACacheSpec(
        cache_family="test_private_family",
        tenor="5Y",
        publisher_consumer_id="declared_publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    expected = ({"baseline": {"ok": True}}, {"status": "built"})

    with patch(
        "shared.liwei_0616_phase_a_cache._prepare_under_family_lock",
        return_value=expected,
    ) as prepare:
        actual = prepare_phase_a_caches(
            spec=spec,
            daily_df=pd.DataFrame(),
            weekly_df=pd.DataFrame(),
            monthly_df=pd.DataFrame(),
            test_ranges=(("2026-01-01", "2026-01-31"),),
            train_missing=lambda *_args: {},
            cache_consumer_id="ordinary_consumer",
            cache_root=root,
        )

    assert actual == expected
    assert prepare.call_args.kwargs["can_mutate_cache"] is True
    assert prepare.call_args.kwargs["cache_consumer_id"] == (
        "ordinary_consumer"
    )
    assert "root" not in prepare.call_args.kwargs


def test_consumer_securely_reads_current_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    root.chmod(0o700)
    daily = pd.DataFrame(
        {"date": ["2026-01-02"], "close": [2.0]}
    )
    weekly = pd.DataFrame(
        {"week_id": [202601], "value": [1.0]}
    )
    monthly = pd.DataFrame(
        {"month_id": ["2026-01"], "value": [1.0]}
    )
    spec = PhaseACacheSpec(
        cache_family="test_consumer_secure",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    cache = {
        "test_dates": ["2026-01-02"],
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray([1], dtype=np.int32),
                "probs": np.asarray([0.75], dtype=np.float64),
            }
        ],
    }
    train_calls: list[str] = []

    def train_missing(
        baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        train_calls.append(baseline)
        return cache

    _publisher_caches, publisher_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=train_missing,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    assert publisher_audit["status"] == "cold_build"
    assert train_calls == ["baseline"]

    _consumer_caches, consumer_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=train_missing,
        cache_consumer_id="consumer",
        cache_root=root,
    )
    assert consumer_audit["status"] == "hit"
    assert consumer_audit["build_reason"] == "consumer_validated_hit"
    assert train_calls == ["baseline"]

    current_path = (
        root
        / spec.cache_family
        / spec.tenor.lower()
        / "current.json"
    )
    external_pointer = root / "external-current.json"
    external_pointer.write_bytes(current_path.read_bytes())
    current_path.unlink()
    current_path.symlink_to(external_pointer)

    with pytest.raises(RuntimeError, match="CACHE_PUBLISHER_REQUIRED"):
        prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(("2026-01-02", "2026-01-02"),),
            train_missing=train_missing,
            cache_consumer_id="consumer",
            cache_root=root,
        )


def test_private_build_requires_absolute_cache_root(monkeypatch) -> None:
    monkeypatch.setenv(
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )
    spec = PhaseACacheSpec(
        cache_family="test_private_family",
        tenor="5Y",
        publisher_consumer_id="declared_publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )

    try:
        prepare_phase_a_caches(
            spec=spec,
            daily_df=pd.DataFrame(),
            weekly_df=pd.DataFrame(),
            monthly_df=pd.DataFrame(),
            test_ranges=(("2026-01-01", "2026-01-31"),),
            train_missing=lambda *_args: {},
            cache_consumer_id="ordinary_consumer",
            cache_root="relative/cache",
        )
    except ValueError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("relative private cache root must fail")


def test_private_build_enables_independent_cold_output_comparison(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        CACHE_MUTATION_POLICY_ENV,
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )
    train = lambda baseline, ranges: {"baseline": baseline, "ranges": ranges}
    full = lambda caches: (caches, caches)

    compare_cold, compare_full = runtime_compare_gate_callbacks(
        train_phase_a=train,
        run_full_output=full,
    )

    assert compare_cold is not None
    assert compare_cold is not train
    assert compare_cold("b", (("a", "z"),)) == train(
        "b", (("a", "z"),)
    )
    assert compare_full is not None
    assert compare_full({"b": {}}) == ({"b": {}}, {"b": {}})


def test_missing_current_rebuilds_instead_of_importing_v1_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    daily = pd.DataFrame(
        {"date": ["2026-01-02"], "close": [2.0]}
    )
    weekly = pd.DataFrame(
        {"week_id": [202601], "value": [1.0]}
    )
    monthly = pd.DataFrame(
        {"month_id": ["2026-01"], "value": [1.0]}
    )
    spec = PhaseACacheSpec(
        cache_family="test_v1_retirement",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    cache = {
        "test_dates": ["2026-01-02"],
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray([1], dtype=np.int32),
                "probs": np.asarray([0.75], dtype=np.float64),
            }
        ],
    }
    bounds = {
        "daily": "2026-01-02",
        "weekly": 202601,
        "monthly": "2026-01",
    }
    legacy_dir = root / "5y"
    legacy_dir.mkdir(parents=True)
    with (legacy_dir / "baseline.pkl").open("wb") as handle:
        pickle.dump(
            {
                "schema_version": 1,
                "cache_family": spec.cache_family,
                "tenor": spec.tenor,
                "baseline": "baseline",
                "baseline_fingerprint": _baseline_fingerprint(
                    spec, "baseline"
                ),
                "watermark": "2026-01-02",
                "phase_a_cache": cache,
                "input_prefix": {
                    "bounds": bounds,
                    "fingerprints": {
                        "daily": _frame_prefix_fingerprint(
                            daily, "date", bounds["daily"]
                        ),
                        "weekly": _frame_prefix_fingerprint(
                            weekly, "week_id", bounds["weekly"]
                        ),
                        "monthly": _frame_prefix_fingerprint(
                            monthly, "month_id", bounds["monthly"]
                        ),
                    },
                },
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    train_calls: list[
        tuple[str, tuple[tuple[str, str], ...]]
    ] = []

    def train_missing(
        baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        train_calls.append((baseline, ranges))
        return cache

    _caches, audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=train_missing,
        cache_consumer_id="publisher",
        cache_root=root,
    )

    assert train_calls == [
        ("baseline", (("2026-01-02", "2026-01-02"),))
    ]
    assert audit["build_mode"] == "full"
    assert audit["build_reason"] == "no_current_generation"


def _daily_revision_change() -> dict[str, object]:
    return {
        "change_type": "revision",
        "raw_change_type": "revision",
        "frames": {
            "daily": {
                "change_type": "revision",
                "earliest_changed_key": "2026-08-17",
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
        "effective_auxiliary": {
            "change_type": "unchanged",
            "earliest_changed_key": None,
            "schema_changed": False,
        },
        "date_to_week": {
            "change_type": "unchanged",
            "earliest_changed_key": None,
        },
        "projection_status": "absent",
        "suffix_start_date": None,
        "native_generation_changed": False,
        "_daily_union_keys": [
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
            "2026-08-14",
            "2026-08-17",
            "2026-08-18",
        ],
    }


def _bounded_spec() -> PhaseACacheSpec:
    return PhaseACacheSpec(
        cache_family="test_revision_suffix",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=3,
        daily_dependency_lookback_rows=5,
        daily_dependency_proof=DAILY_REVISION_SUFFIX_PROOF_V1,
    )


_REVISION_DATES = [
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-17",
    "2026-08-18",
]


def _cache_for_dates(
    dates: list[str],
    value: int,
) -> dict[str, object]:
    return {
        "test_dates": dates,
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray(
                    [value] * len(dates),
                    dtype=np.int32,
                ),
                "probs": np.asarray(
                    [0.5 + value / 10] * len(dates),
                    dtype=np.float64,
                ),
            }
        ],
    }


def _revision_inputs(
    revised: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    daily_dates = _REVISION_DATES if revised else _REVISION_DATES[:-1]
    daily = pd.DataFrame(
        {
            "date": daily_dates,
            "close": [
                2.0 if revised and day == "2026-08-17" else 1.0
                for day in daily_dates
            ],
        }
    )
    weekly = pd.DataFrame(
        {"week_id": [202632], "value": [1.0]}
    )
    monthly = pd.DataFrame(
        {"month_id": ["2026-08"], "value": [1.0]}
    )
    return daily, weekly, monthly


def _full_compare_output() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "direction": [1],
            "vote_score": [0.75],
            "baseline_scores": [{"baseline": 0.75}],
            "baseline_signs": [{"baseline": 1}],
            "probability": [0.8],
            "confidence": [0.6],
        }
    )


def _publish_qualification_generation(
    root: Path,
    *,
    legacy_parent: bool,
) -> tuple[
    PhaseACacheSpec,
    tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
    _LoadedGeneration,
    _LoadedGeneration,
]:
    spec = _bounded_spec()
    parent_spec = (
        replace(
            spec,
            daily_dependency_lookback_rows=None,
            daily_dependency_proof=None,
        )
        if legacy_parent
        else spec
    )
    daily, weekly, monthly = _revision_inputs(revised=False)
    expected_dates = _REVISION_DATES[:-1]
    training_calls: list[str] = []

    def train_parent(
        baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        training_calls.append(baseline)
        return _cache_for_dates(
            [start for start, end in ranges if start == end],
            1,
        )

    prepare_phase_a_caches(
        spec=parent_spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((expected_dates[0], expected_dates[-1]),),
        train_missing=train_parent,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    family_root = root / spec.cache_family / spec.tenor.lower()
    parent, parent_error = _load_current_generation(family_root)
    assert parent_error is None
    assert parent is not None
    assert parent.manifest["compare_gate_evidence"][
        "qualification_status"
    ] == "unqualified"

    def unexpected_training(
        _baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        raise AssertionError("qualification must reuse the parent cache")

    expected_cache = _cache_for_dates(expected_dates, 1)

    def compare_cold(
        baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        assert baseline == "baseline"
        assert [start for start, end in ranges if start == end] == (
            expected_dates
        )
        return expected_cache

    output = _full_compare_output()
    _caches, qualification_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((expected_dates[0], expected_dates[-1]),),
        train_missing=unexpected_training,
        compare_cold=compare_cold,
        compare_full_output=lambda _caches: (
            output,
            output.copy(deep=True),
        ),
        cache_consumer_id="publisher",
        cache_root=root,
    )
    child, child_error = _load_current_generation(family_root)
    assert child_error is None
    assert child is not None
    assert child.generation_id != parent.generation_id
    assert qualification_audit["build_mode"] == "qualification"
    assert qualification_audit["input_change"]["change_type"] == (
        "unchanged"
    )
    assert child.manifest["parent_generation_id"] == parent.generation_id
    assert child.manifest["spec_fingerprint"] == _spec_fingerprint(spec)
    assert child.manifest["input_state"] == parent.manifest["input_state"]
    assert child.manifest["compare_gate_evidence"][
        "qualification_status"
    ] == "qualified"
    assert child.manifest["compare_gate_evidence"][
        "capacity_eligible"
    ] is True
    scope = child.manifest["generation_acceptance_evidence"][
        "baselines"
    ]["baseline"]
    assert scope["affected_dates"] == []
    assert scope["preserved_dates"] == expected_dates
    assert _phase_a_caches_equal(
        parent.caches["baseline"],
        child.caches["baseline"],
    )
    assert training_calls == ["baseline"]
    return spec, (daily, weekly, monthly), parent, child


def _assert_secure_consumer_replays_qualification(
    root: Path,
    *,
    legacy_parent: bool,
) -> None:
    spec, inputs, parent, child = _publish_qualification_generation(
        root,
        legacy_parent=legacy_parent,
    )
    daily, weekly, monthly = inputs

    def unexpected_training(
        _baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        raise AssertionError("secure consumer must not train")

    consumer_caches, consumer_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((_REVISION_DATES[0], _REVISION_DATES[-2]),),
        train_missing=unexpected_training,
        cache_consumer_id="consumer",
        cache_root=root,
    )

    assert consumer_audit["status"] == "hit"
    assert consumer_audit["build_reason"] == "consumer_validated_hit"
    assert consumer_audit["generation_id"] == child.generation_id
    assert _phase_a_caches_equal(
        consumer_caches["baseline"],
        parent.caches["baseline"],
    )


def test_secure_consumer_replays_legacy_parent_qualification(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)

    _assert_secure_consumer_replays_qualification(
        root,
        legacy_parent=True,
    )


def test_secure_consumer_replays_exact_parent_qualification(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)

    _assert_secure_consumer_replays_qualification(
        root,
        legacy_parent=False,
    )


def test_qualification_lineage_rejects_noncanonical_transitions(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)
    spec, _inputs, parent, child = _publish_qualification_generation(
        root,
        legacy_parent=False,
    )
    unchanged = _input_change_analysis(
        parent.manifest["input_state"],
        child.manifest["input_state"],
    )

    changed_cache = copy.deepcopy(child.caches)
    changed_cache["baseline"]["results"][0]["preds"][0] = -1
    cache_changed_child = _LoadedGeneration(
        generation_id=child.generation_id,
        path=child.path,
        manifest=child.manifest,
        manifest_sha256=child.manifest_sha256,
        caches=changed_cache,
    )
    assert _lineage_build_mode(
        parent=parent,
        generation=cache_changed_child,
        input_change=copy.deepcopy(unchanged),
        spec=spec,
    ) != "qualification"

    changed_input = copy.deepcopy(unchanged)
    changed_input["change_type"] = "append"
    changed_input["raw_change_type"] = "append"
    changed_input["frames"]["daily"]["change_type"] = "append"
    assert _lineage_build_mode(
        parent=parent,
        generation=child,
        input_change=changed_input,
        spec=spec,
    ) != "qualification"

    for invalid_evidence in (
        None,
        {
            "qualification_status": "qualified",
            "capacity_eligible": True,
        },
    ):
        invalid_manifest = copy.deepcopy(child.manifest)
        invalid_manifest["compare_gate_evidence"] = invalid_evidence
        invalid_child = _LoadedGeneration(
            generation_id=child.generation_id,
            path=child.path,
            manifest=invalid_manifest,
            manifest_sha256=child.manifest_sha256,
            caches=child.caches,
        )
        assert _lineage_build_mode(
            parent=parent,
            generation=invalid_child,
            input_change=copy.deepcopy(unchanged),
            spec=spec,
        ) != "qualification"

    next_manifest = copy.deepcopy(child.manifest)
    next_manifest["build_mode"] = "qualification"
    next_child = _LoadedGeneration(
        generation_id="generation-next",
        path=child.path,
        manifest=next_manifest,
        manifest_sha256=child.manifest_sha256,
        caches=child.caches,
    )
    assert _lineage_build_mode(
        parent=child,
        generation=next_child,
        input_change=_input_change_analysis(
            child.manifest["input_state"],
            next_child.manifest["input_state"],
        ),
        spec=spec,
    ) != "qualification"


def _prepare_revision_parent(
    root: Path,
) -> tuple[PhaseACacheSpec, Path, str]:
    spec = _bounded_spec()
    legacy_spec = replace(
        spec,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )
    daily, weekly, monthly = _revision_inputs(revised=False)

    def train_parent(
        _baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        return _cache_for_dates(
            [start for start, end in ranges if start == end],
            1,
        )

    prepare_phase_a_caches(
        spec=legacy_spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((_REVISION_DATES[0], _REVISION_DATES[-2]),),
        train_missing=train_parent,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    family_root = root / spec.cache_family / spec.tenor.lower()
    current_path = family_root / "current.json"
    parent_generation_id = json.loads(
        current_path.read_text(encoding="utf-8")
    )["generation_id"]
    return spec, current_path, parent_generation_id


def _publish_retention_generations(
    root: Path,
    *,
    count: int,
) -> tuple[
    PhaseACacheSpec,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    Path,
]:
    spec = PhaseACacheSpec(
        cache_family="test_publish_prune",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    dates = pd.date_range("2026-01-05", periods=count + 1, freq="B")
    weekly = pd.DataFrame({"week_id": [202602], "value": [1.0]})
    monthly = pd.DataFrame({"month_id": ["2026-01"], "value": [1.0]})
    daily = pd.DataFrame()

    for size in range(1, count + 1):
        daily = pd.DataFrame(
            {
                "date": dates[:size].strftime("%Y-%m-%d").tolist(),
                "close": [1.0] * size,
            }
        )

        def train_missing(
            _baseline: str,
            ranges: tuple[tuple[str, str], ...],
            *,
            value: int = size,
        ) -> dict[str, object]:
            return _cache_for_dates(
                [start for start, end in ranges if start == end],
                value,
            )

        prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(
                (
                    str(daily.iloc[0]["date"]),
                    str(daily.iloc[-1]["date"]),
                ),
            ),
            train_missing=train_missing,
            cache_consumer_id="publisher",
            cache_root=root,
        )

    family_root = root / spec.cache_family / spec.tenor.lower()
    return spec, daily, weekly, monthly, family_root


def _generation_snapshot(generation_root: Path) -> dict[str, dict[str, bytes]]:
    return {
        generation.name: {
            str(path.relative_to(generation)): path.read_bytes()
            for path in sorted(generation.rglob("*"))
            if path.is_file()
        }
        for generation in sorted(generation_root.iterdir())
        if generation.is_dir()
    }


def _publish_next_retention_generation(
    *,
    spec: PhaseACacheSpec,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    monthly: pd.DataFrame,
    root: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    next_date = (
        pd.Timestamp(daily.iloc[-1]["date"]) + pd.offsets.BDay(1)
    ).strftime("%Y-%m-%d")
    extended = pd.concat(
        [
            daily,
            pd.DataFrame({"date": [next_date], "close": [9.0]}),
        ],
        ignore_index=True,
    )

    def train_missing(
        _baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        return _cache_for_dates(
            [start for start, end in ranges if start == end],
            9,
        )

    return prepare_phase_a_caches(
        spec=spec,
        daily_df=extended,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((str(extended.iloc[0]["date"]), next_date),),
        train_missing=train_missing,
        cache_consumer_id="publisher",
        cache_root=root,
    )


def test_pointer_failure_at_retention_limit_preserves_all_generations(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    pointer_bytes = current_path.read_bytes()
    generations_before = _generation_snapshot(generation_root)
    real_replace = phase_a_cache_module.os.replace

    def fail_pointer_replace(source: Path, target: Path) -> None:
        if Path(target) == current_path:
            raise OSError("pointer replace failed")
        real_replace(source, target)

    with patch.object(
        phase_a_cache_module.os,
        "replace",
        side_effect=fail_pointer_replace,
    ):
        with pytest.raises(OSError, match="pointer replace failed"):
            _publish_next_retention_generation(
                spec=spec,
                daily=daily,
                weekly=weekly,
                monthly=monthly,
                root=root,
            )

    assert current_path.read_bytes() == pointer_bytes
    assert _generation_snapshot(generation_root) == generations_before
    assert not list(family_root.glob(".building-*"))


def test_prune_preflight_failure_preserves_pointer_and_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    pointer_bytes = current_path.read_bytes()
    generations_before = _generation_snapshot(generation_root)

    monkeypatch.setattr(
        phase_a_cache_module,
        "CACHE_GENERATION_RETENTION",
        1,
    )
    with pytest.raises(
        CacheCapacityError,
        match="protected current and candidate",
    ):
        _publish_next_retention_generation(
            spec=spec,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            root=root,
        )

    assert current_path.read_bytes() == pointer_bytes
    assert _generation_snapshot(generation_root) == generations_before
    assert not list(family_root.glob(".building-*"))


def test_family_byte_prune_plan_uses_real_directory_sizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family_root = tmp_path / "family"
    generation_root = family_root / "generations"
    sizes = {"old": 4, "current": 4, "candidate": 4}
    for index, (generation_id, size) in enumerate(sizes.items(), start=1):
        path = generation_root / generation_id
        path.mkdir(parents=True)
        (path / "payload").write_bytes(b"x" * size)
        phase_a_cache_module.os.utime(path, ns=(index, index))
    protected = {"current", "candidate"}
    monkeypatch.setattr(
        phase_a_cache_module,
        "CACHE_GENERATION_RETENTION",
        10,
    )
    monkeypatch.setattr(
        phase_a_cache_module,
        "MAX_CACHE_FAMILY_BYTES",
        8,
    )

    assert phase_a_cache_module._plan_generation_prune(
        family_root,
        protected_generation_ids=protected,
    ) == ("old",)

    monkeypatch.setattr(
        phase_a_cache_module,
        "MAX_CACHE_FAMILY_BYTES",
        7,
    )
    with pytest.raises(
        CacheCapacityError,
        match="protected current and candidate",
    ):
        phase_a_cache_module._plan_generation_prune(
            family_root,
            protected_generation_ids=protected,
        )


def test_generation_root_fsync_failure_discards_moved_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    pointer_bytes = current_path.read_bytes()
    generations_before = _generation_snapshot(generation_root)
    real_fsync_directory = phase_a_cache_module._fsync_directory

    def fail_generation_root_fsync(path: Path) -> None:
        if path == generation_root:
            raise OSError("generation root fsync failed")
        real_fsync_directory(path)

    with patch.object(
        phase_a_cache_module,
        "_fsync_directory",
        side_effect=fail_generation_root_fsync,
    ):
        with pytest.raises(OSError, match="generation root fsync failed"):
            _publish_next_retention_generation(
                spec=spec,
                daily=daily,
                weekly=weekly,
                monthly=monthly,
                root=root,
            )

    assert current_path.read_bytes() == pointer_bytes
    assert _generation_snapshot(generation_root) == generations_before
    assert not list(family_root.glob(".building-*"))


def test_post_replace_runtime_fsync_failure_keeps_publication_successful(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    old_current = json.loads(current_path.read_text(encoding="utf-8"))[
        "generation_id"
    ]
    real_fsync_directory = phase_a_cache_module._fsync_directory

    def fail_pointer_directory_fsync(path: Path) -> None:
        if path == family_root:
            raise RuntimeError("pointer directory fsync failed")
        real_fsync_directory(path)

    with patch.object(
        phase_a_cache_module,
        "_fsync_directory",
        side_effect=fail_pointer_directory_fsync,
    ):
        _caches, audit = _publish_next_retention_generation(
            spec=spec,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            root=root,
        )

    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    assert pointer["generation_id"] == audit["generation_id"]
    assert pointer["generation_id"] != old_current
    assert (generation_root / pointer["generation_id"]).is_dir()
    assert not list(family_root.glob(".building-*"))


def test_postcommit_prune_failure_keeps_published_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    old_pointer = json.loads(current_path.read_text(encoding="utf-8"))
    generations_before = _generation_snapshot(generation_root)

    with patch(
        "shared.liwei_0616_phase_a_cache._prune_generations",
        side_effect=RuntimeError("postcommit prune failed"),
    ):
        _caches, audit = _publish_next_retention_generation(
            spec=spec,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            root=root,
        )

    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    assert pointer["generation_id"] == audit["generation_id"]
    assert pointer["generation_id"] != old_pointer["generation_id"]
    assert (generation_root / pointer["generation_id"]).is_dir()
    assert set(_generation_snapshot(generation_root)) == (
        set(generations_before) | {pointer["generation_id"]}
    )
    assert not list(family_root.glob(".building-*"))


def test_postcommit_prune_continues_after_one_removal_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    old_current = json.loads(current_path.read_text(encoding="utf-8"))[
        "generation_id"
    ]
    for index, generation_id in enumerate(("extra-old-a", "extra-old-b")):
        path = generation_root / generation_id
        path.mkdir()
        (path / "payload").write_bytes(b"old")
        phase_a_cache_module.os.utime(path, ns=(index + 1, index + 1))
    real_rmtree = phase_a_cache_module.shutil.rmtree
    removal_attempts: list[str] = []

    def fail_one_removal(path: Path) -> None:
        generation_id = Path(path).name
        removal_attempts.append(generation_id)
        if generation_id == "extra-old-a":
            raise OSError("one old generation is busy")
        real_rmtree(path)

    with patch.object(
        phase_a_cache_module.shutil,
        "rmtree",
        side_effect=fail_one_removal,
    ):
        _caches, audit = _publish_next_retention_generation(
            spec=spec,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            root=root,
        )

    assert removal_attempts[:2] == ["extra-old-a", "extra-old-b"]
    assert (generation_root / "extra-old-a").is_dir()
    assert not (generation_root / "extra-old-b").exists()
    assert (generation_root / old_current).is_dir()
    assert (generation_root / audit["generation_id"]).is_dir()
    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    assert pointer["generation_id"] == audit["generation_id"]


def test_successful_publication_prunes_after_pointer_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, daily, weekly, monthly, family_root = (
        _publish_retention_generations(root, count=3)
    )
    current_path = family_root / "current.json"
    generation_root = family_root / "generations"
    old_current = json.loads(current_path.read_text(encoding="utf-8"))[
        "generation_id"
    ]

    _caches, audit = _publish_next_retention_generation(
        spec=spec,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        root=root,
    )

    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    remaining = {
        path.name for path in generation_root.iterdir() if path.is_dir()
    }
    assert pointer["generation_id"] == audit["generation_id"]
    assert len(remaining) == 3
    assert pointer["generation_id"] in remaining
    assert old_current in remaining
    assert not list(family_root.glob(".building-*"))


def test_revision_suffix_preserves_parent_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    root.chmod(0o700)
    spec, current_path, parent_generation_id = _prepare_revision_parent(root)
    legacy_spec = replace(
        spec,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )
    daily, weekly, monthly = _revision_inputs(revised=True)
    trained_dates: list[str] = []

    def train_suffix(
        _baseline: str,
        ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        trained_dates.extend(
            start for start, end in ranges if start == end
        )
        return _cache_for_dates(trained_dates, 2)

    caches, audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((_REVISION_DATES[0], _REVISION_DATES[-1]),),
        train_missing=train_suffix,
        cache_consumer_id="publisher",
        cache_root=root,
    )

    assert audit["build_mode"] == "suffix"
    assert audit["build_reason"] == "proven_daily_input_revision"
    assert audit["input_change"]["suffix_start_date"] == "2026-08-10"
    assert trained_dates == _REVISION_DATES[5:]
    cache = caches["baseline"]
    assert cache["test_dates"] == _REVISION_DATES
    np.testing.assert_array_equal(
        cache["results"][0]["preds"],
        np.asarray([1] * 5 + [2] * 7, dtype=np.int32),
    )
    np.testing.assert_allclose(
        cache["results"][0]["probs"],
        np.asarray([0.6] * 5 + [0.7] * 7, dtype=np.float64),
    )

    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    candidate_generation_id = pointer["generation_id"]
    family_root = current_path.parent
    candidate_manifest_path = (
        family_root
        / "generations"
        / candidate_generation_id
        / "manifest.json"
    )
    candidate_manifest_bytes = candidate_manifest_path.read_bytes()
    candidate_manifest = json.loads(candidate_manifest_bytes)
    parent_manifest_path = (
        family_root
        / "generations"
        / parent_generation_id
        / "manifest.json"
    )
    parent_manifest_bytes = parent_manifest_path.read_bytes()
    parent_manifest = json.loads(parent_manifest_bytes)
    scope = candidate_manifest["generation_acceptance_evidence"][
        "baselines"
    ]["baseline"]

    assert pointer["manifest_sha256"] == hashlib.sha256(
        candidate_manifest_bytes
    ).hexdigest()
    assert candidate_manifest["parent_generation_id"] == parent_generation_id
    assert candidate_manifest["generation_acceptance_evidence"]["parent"] == {
        "generation_id": parent_generation_id,
        "manifest_sha256": hashlib.sha256(
            parent_manifest_bytes
        ).hexdigest(),
        "generation_content_id": parent_manifest[
            "generation_content_id"
        ],
    }
    assert scope["preserved_dates"] == _REVISION_DATES[:5]
    assert scope["parent_preserved_sha256"] is not None
    assert scope["parent_preserved_sha256"] == scope[
        "candidate_preserved_sha256"
    ]
    assert (family_root / "generations" / parent_generation_id).is_dir()
    assert candidate_generation_id != parent_generation_id
    assert candidate_manifest["spec_fingerprint"] == _spec_fingerprint(spec)
    assert parent_manifest["spec_fingerprint"] == _spec_fingerprint(
        legacy_spec
    )

    def unexpected_training(
        _baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        raise AssertionError("consumer must not train")

    consumer_caches, consumer_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((_REVISION_DATES[0], _REVISION_DATES[-1]),),
        train_missing=unexpected_training,
        cache_consumer_id="consumer",
        cache_root=root,
    )

    consumer_cache = consumer_caches["baseline"]
    assert consumer_audit["status"] == "hit"
    assert consumer_audit["build_reason"] == "consumer_validated_hit"
    assert consumer_cache["test_dates"] == _REVISION_DATES
    np.testing.assert_array_equal(
        consumer_cache["results"][0]["preds"],
        np.asarray([1] * 5 + [2] * 7, dtype=np.int32),
    )
    np.testing.assert_allclose(
        consumer_cache["results"][0]["probs"],
        np.asarray([0.6] * 5 + [0.7] * 7, dtype=np.float64),
    )


def test_revision_suffix_failure_keeps_parent_pointer(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    spec, current_path, _parent_generation_id = _prepare_revision_parent(root)
    daily, weekly, monthly = _revision_inputs(revised=True)
    current_bytes = current_path.read_bytes()
    family_root = current_path.parent
    generation_root = family_root / "generations"
    generation_names = sorted(
        path.name for path in generation_root.iterdir() if path.is_dir()
    )

    def fail_training(
        _baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        raise RuntimeError("suffix training failed")

    with pytest.raises(RuntimeError, match="suffix training failed"):
        prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=((_REVISION_DATES[0], _REVISION_DATES[-1]),),
            train_missing=fail_training,
            cache_consumer_id="publisher",
            cache_root=root,
        )

    assert current_path.read_bytes() == current_bytes
    assert sorted(
        path.name for path in generation_root.iterdir() if path.is_dir()
    ) == generation_names
    assert not list(family_root.glob(".building-*"))


def test_only_legacy_empty_proof_fingerprint_is_compatible() -> None:
    current = _bounded_spec()
    legacy = replace(
        current,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )
    legacy_generation = _LoadedGeneration(
        generation_id="legacy",
        path=Path("/unused/legacy"),
        manifest={"spec_fingerprint": _spec_fingerprint(legacy)},
        manifest_sha256="a" * 64,
        caches={"baseline": {"test_dates": []}},
    )
    current_generation = _LoadedGeneration(
        generation_id="current",
        path=Path("/unused/current"),
        manifest={"spec_fingerprint": _spec_fingerprint(current)},
        manifest_sha256="b" * 64,
        caches={"baseline": {"test_dates": []}},
    )
    missing_fingerprint_generation = _LoadedGeneration(
        generation_id="missing-fingerprint",
        path=Path("/unused/missing-fingerprint"),
        manifest={},
        manifest_sha256="c" * 64,
        caches={"baseline": {"test_dates": []}},
    )

    assert _spec_fingerprint(legacy) != _spec_fingerprint(current)
    assert _matching_generation_spec(legacy_generation, current) == legacy
    assert (
        _matching_generation_spec(
            legacy_generation,
            replace(current, horizon=current.horizon + 1),
        )
        is None
    )
    assert (
        _matching_generation_spec(
            legacy_generation,
            replace(current, purge_gap=current.purge_gap + 1),
        )
        is None
    )
    assert (
        _matching_generation_spec(
            legacy_generation,
            replace(current, daily_dependency_proof=" "),
        )
        is None
    )
    assert _matching_generation_spec(
        missing_fingerprint_generation,
        legacy,
    ) is None
    assert _matching_generation_spec(
        missing_fingerprint_generation,
        replace(current, daily_dependency_proof=" "),
    ) is None
    assert _matching_generation_spec(current_generation, current) == current


def test_daily_dependency_proof_accepts_only_exact_contracts() -> None:
    canonical = _bounded_spec()
    legacy = replace(
        canonical,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )

    _validate_daily_dependency_proof(canonical)
    _validate_daily_dependency_proof(legacy)


@pytest.mark.parametrize(
    ("rows", "proof"),
    (
        (0, DAILY_REVISION_SUFFIX_PROOF_V1),
        (4, DAILY_REVISION_SUFFIX_PROOF_V1),
        (6, DAILY_REVISION_SUFFIX_PROOF_V1),
        (True, DAILY_REVISION_SUFFIX_PROOF_V1),
        (5.0, DAILY_REVISION_SUFFIX_PROOF_V1),
        (5, "typo"),
        (5, ""),
        (5, f" {DAILY_REVISION_SUFFIX_PROOF_V1} "),
        (5, 1),
        (None, "nonempty"),
        (5, None),
        (None, ""),
    ),
)
def test_daily_dependency_proof_rejects_noncanonical_declarations(
    rows: object,
    proof: object,
) -> None:
    spec = replace(
        _bounded_spec(),
        daily_dependency_lookback_rows=rows,
        daily_dependency_proof=proof,
    )

    with pytest.raises(
        ValueError,
        match="literal None/None legacy declaration or canonical proof",
    ):
        _validate_daily_dependency_proof(spec)


@pytest.mark.parametrize(
    ("rows", "proof"),
    (
        (0, DAILY_REVISION_SUFFIX_PROOF_V1),
        (5, "typo"),
    ),
)
def test_invalid_daily_dependency_proof_cannot_select_suffix(
    rows: object,
    proof: object,
) -> None:
    invalid = replace(
        _bounded_spec(),
        daily_dependency_lookback_rows=rows,
        daily_dependency_proof=proof,
    )

    assert _revision_build_decision(
        spec=invalid,
        input_change=_daily_revision_change(),
    ) == ("full", "input_revision", None)


@pytest.mark.parametrize(
    ("rows", "proof"),
    (
        (0, DAILY_REVISION_SUFFIX_PROOF_V1),
        (4, DAILY_REVISION_SUFFIX_PROOF_V1),
        (5, "typo"),
    ),
)
def test_invalid_daily_dependency_proof_cannot_match_legacy_generation(
    rows: object,
    proof: object,
) -> None:
    canonical = _bounded_spec()
    legacy = replace(
        canonical,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )
    legacy_generation = _LoadedGeneration(
        generation_id="legacy",
        path=Path("/unused/legacy"),
        manifest={"spec_fingerprint": _spec_fingerprint(legacy)},
        manifest_sha256="a" * 64,
        caches={"baseline": {"test_dates": []}},
    )
    invalid = replace(
        canonical,
        daily_dependency_lookback_rows=rows,
        daily_dependency_proof=proof,
    )

    assert _matching_generation_spec(legacy_generation, invalid) is None


def test_daily_revision_uses_bounded_suffix() -> None:
    assert _revision_build_decision(
        spec=_bounded_spec(),
        input_change=_daily_revision_change(),
    ) == (
        "suffix",
        "proven_daily_input_revision",
        "2026-08-10",
    )


def test_legacy_daily_revision_stays_full() -> None:
    legacy = replace(
        _bounded_spec(),
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )

    assert _revision_build_decision(
        spec=legacy,
        input_change=_daily_revision_change(),
    ) == ("full", "input_revision", None)


@pytest.mark.parametrize(
    ("frame_name", "change_type"),
    (("weekly", "revision"), ("monthly", "append")),
)
def test_unmapped_auxiliary_change_stays_full(
    frame_name: str,
    change_type: str,
) -> None:
    change = _daily_revision_change()
    change["frames"][frame_name]["change_type"] = change_type
    assert _legacy_build_decision(
        spec=_bounded_spec(),
        input_change=change,
    )[0] == "full"


def test_daily_schema_change_stays_full() -> None:
    change = _daily_revision_change()
    change["frames"]["daily"]["schema_changed"] = True
    assert _revision_build_decision(
        spec=_bounded_spec(),
        input_change=change,
    ) == ("full", "input_revision", None)


def test_lineage_rejects_baseline_set_outside_spec() -> None:
    spec = _bounded_spec()
    fingerprint = _spec_fingerprint(spec)
    parent = _LoadedGeneration(
        generation_id="parent",
        path=Path("/unused/parent"),
        manifest={"spec_fingerprint": fingerprint},
        manifest_sha256="a" * 64,
        caches={"unexpected": {"test_dates": []}},
    )
    candidate = _LoadedGeneration(
        generation_id="candidate",
        path=Path("/unused/candidate"),
        manifest={"spec_fingerprint": fingerprint},
        manifest_sha256="b" * 64,
        caches={"unexpected": {"test_dates": []}},
    )
    input_change = _daily_revision_change()
    input_change["change_type"] = "append"
    input_change["raw_change_type"] = "append"
    input_change["frames"]["daily"]["change_type"] = "append"
    input_change["frames"]["daily"]["earliest_changed_key"] = None

    assert _lineage_build_mode(
        parent=parent,
        generation=candidate,
        input_change=input_change,
        spec=spec,
    ) == "full"


def test_daily_revision_lineage_replays_same_suffix_decision() -> None:
    spec = _bounded_spec()
    fingerprint = _spec_fingerprint(spec)
    parent = _LoadedGeneration(
        generation_id="parent",
        path=Path("/unused/parent"),
        manifest={"spec_fingerprint": fingerprint},
        manifest_sha256="a" * 64,
        caches={"baseline": {"test_dates": []}},
    )
    candidate = _LoadedGeneration(
        generation_id="candidate",
        path=Path("/unused/candidate"),
        manifest={"spec_fingerprint": fingerprint},
        manifest_sha256="b" * 64,
        caches={"baseline": {"test_dates": []}},
    )
    input_change = _daily_revision_change()

    assert _lineage_build_mode(
        parent=parent,
        generation=candidate,
        input_change=input_change,
        spec=spec,
    ) == "suffix"
    assert input_change["suffix_start_date"] == "2026-08-10"
