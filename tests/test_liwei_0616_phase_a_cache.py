from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shared.liwei_0616_cache_contract import CACHE_MUTATION_POLICY_ENV
from shared.liwei_0616_phase_a_cache import (
    DAILY_REVISION_SUFFIX_PROOF_V1,
    PhaseACacheSpec,
    _phase_a_caches_equal,
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
