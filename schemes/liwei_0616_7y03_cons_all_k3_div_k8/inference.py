from __future__ import annotations

import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from shared.liwei_0616_cache_projection import (
    build_auxiliary_dependency_projection,
)
from shared.liwei_0616_phase_a_cache import (
    DAILY_REVISION_SUFFIX_PROOF_V1,
    PhaseACacheSpec,
    prepare_phase_a_caches,
    runtime_compare_gate_callbacks,
)

from .core import data_alignment, v31_common
from .core.v31_common import (
    BASELINE_CONFIGS,
    HORIZON,
    MODEL_VERSION,
    PURGE_GAP,
    SOURCE_IC_SCREEN_START,
    model_config,
    required_baselines,
    run_7y03_for_feature_window,
    run_prediction,
)


DEFAULT_N_WORKERS = 10
CACHE_FAMILY = "liwei_0616_7y03_v31"
CACHE_CONSUMER_ID = "liwei_0616_7y03_cons_all_k3_div_k8"
CACHE_PUBLISHER_CONSUMER_ID = CACHE_CONSUMER_ID


@dataclass(frozen=True)
class PitWindow:
    """7Y_03 PIT 推理窗口。"""

    prior_start: str
    prior_end: str
    latest_start: str
    source_end: str
    current_start: str
    current_end: str
    test_ranges: tuple[tuple[str, str], ...]


def liwei_0616_pit_window(
    feature_date: str,
    *,
    source_end: str | None = None,
    current_start: str | None = None,
    current_end: str | None = None,
) -> PitWindow:
    """返回与原始 latest_oos runner 等价的 PIT/source batch 测试窗口。"""
    datetime.strptime(str(feature_date), "%Y-%m-%d")
    parsed_current_end = datetime.strptime(str(current_end or feature_date), "%Y-%m-%d")
    parsed_source_end = datetime.strptime(str(source_end or feature_date), "%Y-%m-%d")
    parsed_current_start = datetime.strptime(
        str(current_start or pd.Timestamp(feature_date).replace(day=1).date()),
        "%Y-%m-%d",
    )
    if parsed_source_end < parsed_current_end:
        raise ValueError(
            f"source_end={parsed_source_end.date()} cannot be earlier than current_end={parsed_current_end.date()}"
        )
    if parsed_current_start > parsed_current_end:
        raise ValueError(
            f"current_start={parsed_current_start.date()} cannot be later than current_end={parsed_current_end.date()}"
        )
    latest_start = parsed_current_start.strftime("%Y-%m-%d")
    source_end_str = parsed_source_end.strftime("%Y-%m-%d")
    prior_start_ts = pd.Timestamp(latest_start) - pd.DateOffset(years=1)
    prior_end_ts = (pd.Timestamp(source_end_str) - pd.DateOffset(years=1)) + pd.offsets.MonthEnd(0)
    prior_start = str(prior_start_ts.date())
    prior_end = str(prior_end_ts.date())
    return PitWindow(
        prior_start=prior_start,
        prior_end=prior_end,
        latest_start=latest_start,
        source_end=source_end_str,
        current_start=parsed_current_start.strftime("%Y-%m-%d"),
        current_end=parsed_current_end.strftime("%Y-%m-%d"),
        test_ranges=((prior_start, prior_end), (latest_start, source_end_str)),
    )


def run_7y03_for_feature_date(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    feature_date: str,
    require_labels: bool,
    n_workers: int = DEFAULT_N_WORKERS,
    use_incremental_cache: bool = False,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """按单个 feature_date 的 PIT 窗口运行 7Y_03，并返回该日明细。"""
    window = liwei_0616_pit_window(feature_date)
    phase_a_caches: dict[str, dict[str, Any]] | None = None
    cache_audit: dict[str, Any] | None = None

    def run_window(
        caches: Mapping[str, Mapping[str, Any]] | None,
    ) -> pd.DataFrame:
        return run_7y03_for_window_silent(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            feature_date=feature_date,
            test_ranges=window.test_ranges,
            current_start=window.current_start,
            current_end=window.current_end,
            require_labels=require_labels,
            n_workers=n_workers,
            phase_a_caches=caches,
        )

    if use_incremental_cache:
        phase_a_caches, cache_audit = _prepare_incremental_phase_a_caches(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            test_ranges=window.test_ranges,
            n_workers=n_workers,
            cache_root=cache_root,
            full_output_compare_runner=run_window,
        )
    detail = run_window(phase_a_caches)
    matched = detail[detail["anchor_date"].astype(str) == feature_date]
    if matched.empty:
        raise RuntimeError(f"liwei_0616 7Y_03 produced no row for feature_date={feature_date}")
    row = matched.iloc[-1].to_dict()
    row["model_version"] = str(row.get("model_version") or MODEL_VERSION)
    if cache_audit is not None:
        row["phase_a_cache_audit"] = cache_audit
    return row


def _prepare_incremental_phase_a_caches(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    test_ranges: tuple[tuple[str, str], ...],
    n_workers: int,
    cache_root: str | Path | None,
    full_output_compare_runner: (
        Callable[
            [Mapping[str, Mapping[str, Any]] | None],
            pd.DataFrame,
        ]
        | None
    ) = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    baselines = tuple(required_baselines())
    spec = PhaseACacheSpec(
        cache_family=CACHE_FAMILY,
        tenor="7Y",
        publisher_consumer_id=CACHE_PUBLISHER_CONSUMER_ID,
        baselines=baselines,
        baseline_configs={name: dict(BASELINE_CONFIGS[name]) for name in baselines},
        source_ic_screen_start=SOURCE_IC_SCREEN_START,
        horizon=HORIZON,
        purge_gap=PURGE_GAP,
        daily_dependency_lookback_rows=max(HORIZON, PURGE_GAP),
        daily_dependency_proof=DAILY_REVISION_SUFFIX_PROOF_V1,
    )

    def train_missing(baseline: str, missing_ranges: tuple[tuple[str, str], ...]) -> Mapping[str, Any]:
        with redirect_stdout(sys.stderr):
            output = run_prediction(
                model_config(
                    baseline,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    test_start=missing_ranges[0][0],
                    test_end=missing_ranges[-1][1],
                    test_ranges=missing_ranges,
                    require_labels=False,
                    emit_report=False,
                    phase_a_only=True,
                    return_ctx=True,
                    n_workers=n_workers,
                )
            )
        context = output[1] if isinstance(output, tuple) and len(output) == 2 else output
        if not isinstance(context, Mapping) or "phase_a_cache" not in context:
            raise RuntimeError(f"7Y baseline {baseline} did not return Phase A cache")
        return context["phase_a_cache"]

    compare_cold, compare_full_output = runtime_compare_gate_callbacks(
        train_phase_a=train_missing,
        run_full_output=full_output_compare_runner,
    )
    auxiliary_dependency_projection = (
        build_auxiliary_dependency_projection(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            prepare_model_frames=v31_common.prepare_model_frames,
            build_wkmo_features=v31_common.build_wkmo_features,
            proof_files=(
                Path(v31_common.__file__),
                Path(data_alignment.__file__),
            ),
        )
    )
    return prepare_phase_a_caches(
        spec=spec,
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        test_ranges=test_ranges,
        train_missing=train_missing,
        compare_cold=compare_cold,
        compare_full_output=compare_full_output,
        cache_consumer_id=CACHE_CONSUMER_ID,
        auxiliary_dependency_projection=(
            auxiliary_dependency_projection
        ),
        cache_root=cache_root,
    )


def run_7y03_for_window_silent(**kwargs: Any) -> pd.DataFrame:
    """运行窗口并把源风格报告重定向到 stderr，避免污染 JSON stdout。"""
    with redirect_stdout(sys.stderr):
        return run_7y03_for_feature_window(**kwargs)
