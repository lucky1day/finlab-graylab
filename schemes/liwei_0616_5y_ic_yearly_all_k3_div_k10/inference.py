from __future__ import annotations

import os
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from shared.liwei_0616_phase_a_cache import (
    DEFAULT_CACHE_ROOT,
    PhaseACacheSpec,
    prepare_phase_a_caches,
)

from .core.v31_common import (
    HORIZON,
    MODEL_VERSION as SOURCE_MODEL_VERSION,
    PROD_CONFIG,
    PURGE_GAP,
    SOURCE_IC_SCREEN_START,
    model_config,
    run_5y_all_for_feature_window,
    run_prediction,
)


DEFAULT_N_WORKERS = 10
SOURCE_OOS_START = "2024-01-01"
CACHE_FAMILY = "liwei_0616_5y_allk10_ic_yearly_v1"
MODEL_VERSION = "liwei_5y_allk10_ic_yearly_v1"


@dataclass(frozen=True)
class PitWindow:
    """5Y experimental live-safe continuous full-OOS window."""

    prior_start: str
    prior_end: str
    latest_start: str
    source_end: str
    current_start: str
    current_end: str
    test_ranges: tuple[tuple[str, str], ...]


def full_oos_window(
    feature_date: str,
    *,
    source_end: str | None = None,
    source_start: str = SOURCE_OOS_START,
    current_start: str | None = None,
    current_end: str | None = None,
) -> PitWindow:
    """返回从 2024-01-01 到 source_end 的连续 source 测试序列。"""
    datetime.strptime(str(feature_date), "%Y-%m-%d")
    parsed_source_start = datetime.strptime(str(source_start), "%Y-%m-%d")
    parsed_source_end = datetime.strptime(str(source_end or feature_date), "%Y-%m-%d")
    parsed_current_start = datetime.strptime(
        str(current_start or feature_date),
        "%Y-%m-%d",
    )
    parsed_current_end = datetime.strptime(str(current_end or feature_date), "%Y-%m-%d")
    if parsed_source_start > parsed_source_end:
        raise ValueError(
            f"source_start={parsed_source_start.date()} cannot be later than source_end={parsed_source_end.date()}"
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
    prior_start = str((pd.Timestamp(latest_start) - pd.DateOffset(years=1)).date())
    prior_end = str(((pd.Timestamp(source_end_str) - pd.DateOffset(years=1)) + pd.offsets.MonthEnd(0)).date())
    return PitWindow(
        prior_start=prior_start,
        prior_end=prior_end,
        latest_start=latest_start,
        source_end=source_end_str,
        current_start=parsed_current_start.strftime("%Y-%m-%d"),
        current_end=parsed_current_end.strftime("%Y-%m-%d"),
        test_ranges=((parsed_source_start.strftime("%Y-%m-%d"), source_end_str),),
    )


def run_for_feature_date(
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
    """运行 5Y IC yearly ALL_K10 live-safe Full-OOS 并选择精确站位日。"""
    window = full_oos_window(feature_date)
    phase_a_caches: dict[str, dict[str, Any]] | None = None
    cache_audit: dict[str, Any] | None = None
    if use_incremental_cache:
        phase_a_caches, cache_audit = _prepare_incremental_phase_a_caches(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            test_ranges=window.test_ranges,
            n_workers=n_workers,
            cache_root=cache_root,
        )
    detail = run_for_window_silent(
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
        phase_a_caches=phase_a_caches,
    )
    matched = detail[detail["anchor_date"].astype(str) == feature_date]
    if matched.empty:
        raise RuntimeError(
            "liwei_0616 5Y IC yearly ALL_K10 produced no row "
            f"for feature_date={feature_date}"
        )
    row = matched.iloc[-1].to_dict()
    row["source_model_version"] = str(row.get("model_version") or SOURCE_MODEL_VERSION)
    row["model_version"] = MODEL_VERSION
    if cache_audit is not None:
        row["phase_a_cache_audit"] = {
            **cache_audit,
            "cache_family": CACHE_FAMILY,
            "cache_root": str(family_cache_root(cache_root)),
        }
    return row


def family_cache_root(cache_root: str | Path | None = None) -> Path:
    """返回当前 experimental variant 独占的 Phase A cache 根目录。"""
    base_root = (
        cache_root
        or os.getenv("LIWEI_0616_PHASE_A_CACHE_ROOT")
        or DEFAULT_CACHE_ROOT
    )
    return Path(base_root) / CACHE_FAMILY


def _prepare_incremental_phase_a_caches(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    test_ranges: tuple[tuple[str, str], ...],
    n_workers: int,
    cache_root: str | Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    baselines = tuple(str(name) for name in PROD_CONFIG["baselines"])
    spec = PhaseACacheSpec(
        cache_family=CACHE_FAMILY,
        tenor="5Y",
        baselines=baselines,
        baseline_configs={name: model_config(name) for name in baselines},
        source_ic_screen_start=SOURCE_IC_SCREEN_START,
        horizon=HORIZON,
        purge_gap=PURGE_GAP,
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
            raise RuntimeError(f"5Y baseline {baseline} did not return Phase A cache")
        return context["phase_a_cache"]

    return prepare_phase_a_caches(
        spec=spec,
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        test_ranges=test_ranges,
        train_missing=train_missing,
        cache_root=family_cache_root(cache_root),
    )


def run_for_window_silent(**kwargs: Any) -> pd.DataFrame:
    """运行窗口并把源风格报告重定向到 stderr，避免污染 JSON stdout。"""
    with redirect_stdout(sys.stderr):
        return run_5y_all_for_feature_window(**kwargs)


liwei_0616_pit_window = full_oos_window
