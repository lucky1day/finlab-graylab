from __future__ import annotations

import copy
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
from shared.liwei_0616_cache_migration import authorized_cache_rebind
from shared.liwei_0616_phase_a_cache import (
    PhaseACacheSpec,
    _baseline_fingerprint,
    _build_generation_acceptance_evidence,
    _frame_prefix_fingerprint,
    _input_generation_state,
    _spec_fingerprint,
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


def _migration_receipt(
    approved_entry: dict[str, object],
) -> dict[str, object]:
    dummy_entries = [
        _migration_receipt_entry(
            cache_family=f"zz-dummy-family-{index}",
            tenor=f"{index + 1}Y",
            publisher_consumer_id=f"dummy-publisher-{index}",
            spec_fingerprint="1" * 64,
            parent_generation_id=f"generation-dummy-{index}",
            parent_manifest_sha256="2" * 64,
            parent_generation_content_id="3" * 64,
            parent_input_content_id="4" * 64,
            target_input_content_id="5" * 64,
            baselines={
                "baseline": {
                    "cache_content_sha256": "6" * 64,
                    "field_sha256": {
                        "test_dates": "7" * 64,
                        "results[].config": "8" * 64,
                        "results[].preds": "9" * 64,
                        "results[].probs": "a" * 64,
                    },
                    "test_date_count": 1,
                    "result_config_count": 1,
                }
            },
        )
        for index in range(6)
    ]
    entries = sorted(
        [approved_entry, *dummy_entries],
        key=lambda item: (item["cache_family"], item["tenor"]),
    )
    receipt: dict[str, object] = {
        "schema_version": "liwei-0616-migration-rebind-v1",
        "migration_id": "aliyun-linux-x86_64-20260817-v1",
        "entries": entries,
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipt)
    ).hexdigest()
    return receipt


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


def test_phase_a_migration_rebind_reuses_parent_without_training(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    daily = pd.DataFrame(
        {"date": ["2026-01-02"], "close": [2.0]}
    )
    revised_daily = daily.copy()
    revised_daily.loc[0, "close"] = np.nextafter(2.0, np.inf)
    weekly = pd.DataFrame(
        {"week_id": [202601], "value": [1.0]}
    )
    monthly = pd.DataFrame(
        {"month_id": ["2026-01"], "value": [1.0]}
    )
    spec = PhaseACacheSpec(
        cache_family="test_migration_rebind",
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

    def initial_train(
        baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        train_calls.append(baseline)
        return cache

    parent_caches, _parent_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=initial_train,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    family_root = root / spec.cache_family / spec.tenor.lower()
    parent_pointer = json.loads(
        (family_root / "current.json").read_text()
    )
    parent_manifest = json.loads(
        (
            family_root
            / "generations"
            / parent_pointer["generation_id"]
            / "manifest.json"
        ).read_text()
    )
    target_input_state = _input_generation_state(
        daily_df=revised_daily,
        weekly_df=weekly,
        monthly_df=monthly,
        auxiliary_dependency_projection=None,
    )
    approved_entry = _migration_receipt_entry(
        cache_family=spec.cache_family,
        tenor=spec.tenor,
        publisher_consumer_id=spec.publisher_consumer_id,
        spec_fingerprint=_spec_fingerprint(spec),
        parent_generation_id=parent_pointer["generation_id"],
        parent_manifest_sha256=parent_pointer["manifest_sha256"],
        parent_generation_content_id=(
            parent_manifest["generation_content_id"]
        ),
        parent_input_content_id=(
            parent_manifest["input_state"]["content_id"]
        ),
        target_input_content_id=target_input_state["content_id"],
        baselines={
            name: entry["evidence"]
            for name, entry in parent_manifest["baselines"].items()
        },
    )

    def forbidden_train(
        baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        train_calls.append(baseline)
        raise AssertionError("migration rebind must not train")

    wrong_entry = copy.deepcopy(approved_entry)
    wrong_entry["target_input_content_id"] = "f" * 64
    wrong_entry["entry_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {
                key: value
                for key, value in wrong_entry.items()
                if key != "entry_sha256"
            }
        )
    ).hexdigest()
    with authorized_cache_rebind(_migration_receipt(wrong_entry)):
        with pytest.raises(
            RuntimeError,
            match="receipt identity mismatch",
        ):
            prepare_phase_a_caches(
                spec=spec,
                daily_df=revised_daily,
                weekly_df=weekly,
                monthly_df=monthly,
                test_ranges=(("2026-01-02", "2026-01-02"),),
                train_missing=forbidden_train,
                cache_consumer_id="publisher",
                cache_root=root,
            )
    assert train_calls == ["baseline"]
    assert json.loads(
        (family_root / "current.json").read_text()
    )["generation_id"] == parent_pointer["generation_id"]

    with authorized_cache_rebind(
        _migration_receipt(approved_entry)
    ):
        rebound_caches, rebound_audit = prepare_phase_a_caches(
            spec=spec,
            daily_df=revised_daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(("2026-01-02", "2026-01-02"),),
            train_missing=forbidden_train,
            cache_consumer_id="publisher",
            cache_root=root,
        )

    assert train_calls == ["baseline"]
    assert rebound_audit["build_mode"] == "migration_rebind"
    np.testing.assert_array_equal(
        rebound_caches["baseline"]["results"][0]["preds"],
        parent_caches["baseline"]["results"][0]["preds"],
    )
    np.testing.assert_array_equal(
        rebound_caches["baseline"]["results"][0]["probs"],
        parent_caches["baseline"]["results"][0]["probs"],
    )

    child_pointer = json.loads(
        (family_root / "current.json").read_text()
    )
    assert child_pointer["generation_id"] != parent_pointer["generation_id"]
    assert (
        family_root
        / "generations"
        / parent_pointer["generation_id"]
    ).is_dir()

    _hit_caches, hit_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=revised_daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=forbidden_train,
        cache_consumer_id="consumer",
        cache_root=root,
    )
    assert hit_audit["status"] == "hit"
    assert hit_audit["build_mode"] == "hit"
    assert train_calls == ["baseline"]

    child_manifest_path = (
        family_root
        / "generations"
        / child_pointer["generation_id"]
        / "manifest.json"
    )
    child_manifest = json.loads(child_manifest_path.read_text())
    assert child_manifest["build_mode"] == "migration_rebind"
    assert "migration_rebind_evidence" in child_manifest
    del child_manifest["migration_rebind_evidence"]
    tampered_manifest_bytes = (
        json.dumps(
            child_manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    child_manifest_path.write_bytes(tampered_manifest_bytes)
    child_pointer["manifest_sha256"] = hashlib.sha256(
        tampered_manifest_bytes
    ).hexdigest()
    (family_root / "current.json").write_text(
        json.dumps(
            child_pointer,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )

    with pytest.raises(RuntimeError, match="CACHE_PUBLISHER_REQUIRED"):
        prepare_phase_a_caches(
            spec=spec,
            daily_df=revised_daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(("2026-01-02", "2026-01-02"),),
            train_missing=forbidden_train,
            cache_consumer_id="consumer",
            cache_root=root,
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
