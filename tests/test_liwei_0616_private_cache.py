from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from shared.liwei_0616_cache_contract import (
    CACHE_MUTATION_POLICY_ENV,
    CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    canonical_json_bytes,
    validate_generation_acceptance_record,
)
from shared.liwei_0616_phase_a_cache import (
    PhaseACacheSpec,
    _LoadedGeneration,
    _baseline_fingerprint,
    _build_generation_acceptance_evidence,
    _frame_prefix_fingerprint,
    _input_generation_state,
    _legacy_build_decision,
    _lineage_build_mode,
    _phase_a_cache_evidence,
    _revision_build_decision,
    _spec_fingerprint,
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
        daily_dependency_proof="liwei_0616_daily_revision_suffix_v1",
    )


def test_daily_revision_uses_bounded_suffix() -> None:
    assert _revision_build_decision(
        spec=_bounded_spec(),
        input_change=_daily_revision_change(),
    ) == (
        "suffix",
        "proven_daily_input_revision",
        "2026-08-10",
    )


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
