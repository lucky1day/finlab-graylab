from __future__ import annotations

import importlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shared.liwei_0616_cache_contract import CACHE_MUTATION_POLICY_ENV
from shared.liwei_0616_phase_a_cache import (
    DAILY_REVISION_SUFFIX_PROOF_V1,
    FULL_COMPARE_FIELDS,
    PhaseACacheSpec,
    _load_current_generation,
    _phase_a_caches_equal,
    _revision_build_decision,
    prepare_phase_a_caches,
)


DATES = [
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


def _spec(cache_family: str = "test_revision_suffix") -> PhaseACacheSpec:
    return PhaseACacheSpec(
        cache_family=cache_family,
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


def _inputs(*, revised: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = DATES if revised else DATES[:-1]
    daily = pd.DataFrame(
        {
            "date": dates,
            "close": [
                2.0 if revised and day == "2026-08-17" else 1.0
                for day in dates
            ],
        }
    )
    weekly = pd.DataFrame({"week_id": [202632], "value": [1.0]})
    monthly = pd.DataFrame({"month_id": ["2026-08"], "value": [1.0]})
    return daily, weekly, monthly


def _cache(dates: list[str], values: list[int]) -> dict[str, object]:
    return {
        "test_dates": dates,
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray(values, dtype=np.int32),
                "probs": np.asarray(
                    [0.5 + value / 10 for value in values],
                    dtype=np.float64,
                ),
            }
        ],
    }


def _dates_from_ranges(ranges: tuple[tuple[str, str], ...]) -> list[str]:
    return [start for start, end in ranges if start == end]


def _full_values(dates: list[str]) -> list[int]:
    return [1 if day < "2026-08-10" else 2 for day in dates]


def _build_parent(root: Path) -> tuple[PhaseACacheSpec, Path]:
    spec = _spec()
    legacy_spec = replace(
        spec,
        daily_dependency_lookback_rows=None,
        daily_dependency_proof=None,
    )
    daily, weekly, monthly = _inputs(revised=False)
    prepare_phase_a_caches(
        spec=legacy_spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-2]),),
        train_missing=lambda _baseline, ranges: _cache(
            _dates_from_ranges(ranges),
            [1] * len(_dates_from_ranges(ranges)),
        ),
        cache_consumer_id="publisher",
        cache_root=root,
    )
    return spec, root / spec.cache_family / spec.tenor.lower() / "current.json"


def _full_output() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "direction": [1],
            "vote_score": [0.75],
            "baseline_scores": [{"baseline": 0.75}],
            "baseline_signs": [{"baseline": 1}],
            "probability": [0.8],
            "confidence": [0.6],
            "fallback": [False],
            "internal_fields": [{"selector": "baseline", "raw": 0.75}],
        }
    )


def test_publisher_build_is_reused_by_read_only_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    root.chmod(0o700)
    spec = _spec("test_publisher_consumer")
    daily, weekly, monthly = _inputs(revised=True)
    calls: list[list[str]] = []

    def train(_baseline: str, ranges: tuple[tuple[str, str], ...]):
        dates = _dates_from_ranges(ranges)
        calls.append(dates)
        return _cache(dates, _full_values(dates))

    published, publish_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-1]),),
        train_missing=train,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    consumed, consume_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-1]),),
        train_missing=lambda *_args: pytest.fail("consumer must not train"),
        cache_consumer_id="consumer",
        cache_root=root,
    )

    assert publish_audit["status"] == "cold_build"
    assert consume_audit["status"] == "hit"
    assert consume_audit["build_reason"] == "consumer_validated_hit"
    assert len(calls) == 1
    assert _phase_a_caches_equal(published["baseline"], consumed["baseline"])


def test_daily_revision_uses_bounded_suffix_equal_to_full_cold_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    suffix_root = (tmp_path / "suffix").resolve()
    cold_root = (tmp_path / "cold").resolve()
    suffix_root.mkdir(mode=0o700)
    cold_root.mkdir(mode=0o700)
    spec, _current = _build_parent(suffix_root)
    daily, weekly, monthly = _inputs(revised=True)
    recomputed: list[str] = []

    def train_suffix(_baseline: str, ranges: tuple[tuple[str, str], ...]):
        dates = _dates_from_ranges(ranges)
        recomputed.extend(dates)
        return _cache(dates, [2] * len(dates))

    suffix_caches, suffix_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-1]),),
        train_missing=train_suffix,
        cache_consumer_id="publisher",
        cache_root=suffix_root,
    )
    cold_caches, cold_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-1]),),
        train_missing=lambda _baseline, ranges: _cache(
            _dates_from_ranges(ranges),
            _full_values(_dates_from_ranges(ranges)),
        ),
        cache_consumer_id="publisher",
        cache_root=cold_root,
    )

    assert suffix_audit["build_mode"] == "suffix"
    assert suffix_audit["build_reason"] == "proven_daily_input_revision"
    assert suffix_audit["input_change"]["suffix_start_date"] == "2026-08-10"
    assert recomputed == DATES[5:]
    assert len(recomputed) <= 20
    assert cold_audit["build_mode"] == "full"
    assert _phase_a_caches_equal(
        suffix_caches["baseline"],
        cold_caches["baseline"],
    )


def test_suffix_failure_does_not_switch_current_pointer(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    spec, current = _build_parent(root)
    before = current.read_bytes()
    daily, weekly, monthly = _inputs(revised=True)

    with pytest.raises(RuntimeError, match="suffix failed"):
        prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=((DATES[0], DATES[-1]),),
            train_missing=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("suffix failed")
            ),
            cache_consumer_id="publisher",
            cache_root=root,
        )

    assert current.read_bytes() == before


def test_compare_qualification_covers_exact_full_output(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    spec = _spec("test_full_output_qualification")
    daily, weekly, monthly = _inputs(revised=True)

    def train(_baseline: str, ranges: tuple[tuple[str, str], ...]):
        dates = _dates_from_ranges(ranges)
        return _cache(dates, _full_values(dates))

    def cold(_baseline: str, ranges: tuple[tuple[str, str], ...]):
        dates = _dates_from_ranges(ranges)
        return _cache(dates, _full_values(dates))

    output = _full_output()
    prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=((DATES[0], DATES[-1]),),
        train_missing=train,
        compare_cold=cold,
        compare_full_output=lambda _caches: (
            output,
            output.copy(deep=True),
        ),
        cache_consumer_id="publisher",
        cache_root=root,
    )
    generation, error = _load_current_generation(
        root / spec.cache_family / spec.tenor.lower()
    )

    assert error is None
    assert generation is not None
    evidence = generation.manifest["compare_gate_evidence"]
    assert evidence["qualification_status"] == "qualified"
    assert evidence["full_output_qualification"]["comparison_fields"] == list(
        FULL_COMPARE_FIELDS
    )

    mismatched = output.copy(deep=True)
    mismatched.at[0, "internal_fields"] = {"selector": "other", "raw": 0.75}
    with pytest.raises(ValueError, match="full output CompareGate mismatch"):
        prepare_phase_a_caches(
            spec=replace(spec, cache_family="test_full_output_mismatch"),
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=((DATES[0], DATES[-1]),),
            train_missing=train,
            compare_cold=cold,
            compare_full_output=lambda _caches: (output, mismatched),
            cache_consumer_id="publisher",
            cache_root=root,
        )


@pytest.mark.parametrize(
    ("rows", "proof"),
    (
        (0, DAILY_REVISION_SUFFIX_PROOF_V1),
        (4, DAILY_REVISION_SUFFIX_PROOF_V1),
        (5, "typo"),
        (None, "nonempty"),
    ),
)
def test_invalid_dependency_proof_cannot_select_suffix(
    rows: object,
    proof: object,
) -> None:
    spec = replace(
        _spec(),
        daily_dependency_lookback_rows=rows,
        daily_dependency_proof=proof,
    )
    change = {
        "frames": {
            "daily": {
                "change_type": "revision",
                "earliest_changed_key": "2026-08-17",
                "schema_changed": False,
            },
            "weekly": {"change_type": "unchanged", "schema_changed": False},
            "monthly": {"change_type": "unchanged", "schema_changed": False},
        },
        "_daily_union_keys": DATES[5:],
    }

    assert _revision_build_decision(spec=spec, input_change=change) == (
        "full",
        "input_revision",
        None,
    )


@pytest.mark.parametrize(
    "module_name",
    (
        "schemes.liwei_0616_10y01_full_oos_k3_div_k10.inference",
        "schemes.liwei_0616_10y01_cons_say_k3_div_k10.inference",
        "schemes.liwei_0616_10y02_cons_say_k3_div_k5.inference",
        "schemes.liwei_0616_5y01_full_oos_k3_div_k10.inference",
        "schemes.liwei_0616_cons_sda_k3_div_k10.inference",
        "schemes.liwei_0616_5y_auc_static_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_ic_yearly_all_k3_div_k10.inference",
        "schemes.liwei_0616_7y01_cons_say_k3_div_k10.inference",
        "schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference",
    ),
)
def test_all_incremental_consumers_declare_the_bounded_daily_contract(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    module = importlib.import_module(module_name)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "prepare_phase_a_caches",
        lambda **kwargs: (captured.update(kwargs) or ({}, {})),
    )
    monkeypatch.setattr(
        module,
        "build_auxiliary_dependency_projection",
        lambda **_kwargs: None,
    )
    module._prepare_incremental_phase_a_caches(
        daily_df=pd.DataFrame(),
        weekly_df=pd.DataFrame(),
        monthly_df=pd.DataFrame(),
        date_to_week=None,
        test_ranges=(("2026-08-18", "2026-08-18"),),
        n_workers=1,
        cache_root=None,
    )

    spec = captured["spec"]
    assert spec.daily_dependency_lookback_rows == max(
        module.HORIZON,
        module.PURGE_GAP,
    )
    assert spec.daily_dependency_proof == DAILY_REVISION_SUFFIX_PROOF_V1
