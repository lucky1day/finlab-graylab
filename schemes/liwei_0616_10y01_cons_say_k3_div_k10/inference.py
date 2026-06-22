from __future__ import annotations

import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from .core.v31_common import MODEL_VERSION, run_10y01_for_feature_window


DEFAULT_N_WORKERS = 10


@dataclass(frozen=True)
class PitWindow:
    """10Y_01 PIT 推理窗口。"""

    prior_start: str
    prior_end: str
    current_start: str
    current_end: str
    test_ranges: tuple[tuple[str, str], ...]


def liwei_0616_pit_window(feature_date: str) -> PitWindow:
    """返回同月去年窗口 + 当前月截至 feature_date 的 PIT 测试窗口。"""
    parsed = datetime.strptime(str(feature_date), "%Y-%m-%d")
    current_start = parsed.replace(day=1).strftime("%Y-%m-%d")
    current_end = parsed.strftime("%Y-%m-%d")
    prior_start_ts = pd.Timestamp(current_start) - pd.DateOffset(years=1)
    prior_end_ts = (pd.Timestamp(current_end) - pd.DateOffset(years=1)) + pd.offsets.MonthEnd(0)
    prior_start = str(prior_start_ts.date())
    prior_end = str(prior_end_ts.date())
    return PitWindow(
        prior_start=prior_start,
        prior_end=prior_end,
        current_start=current_start,
        current_end=current_end,
        test_ranges=((prior_start, prior_end), (current_start, current_end)),
    )


def run_10y01_for_feature_date(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    feature_date: str,
    require_labels: bool,
    n_workers: int = DEFAULT_N_WORKERS,
) -> dict[str, Any]:
    """按单个 feature_date 的 PIT 窗口运行 10Y_01，并返回该日明细。"""
    window = liwei_0616_pit_window(feature_date)
    detail = run_10y01_for_window_silent(
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
    )
    matched = detail[detail["anchor_date"].astype(str) == feature_date]
    if matched.empty:
        raise RuntimeError(f"liwei_0616 10Y_01 produced no row for feature_date={feature_date}")
    row = matched.iloc[-1].to_dict()
    row["model_version"] = str(row.get("model_version") or MODEL_VERSION)
    return row


def run_10y01_for_window_silent(**kwargs: Any) -> pd.DataFrame:
    """运行窗口并把源风格报告重定向到 stderr，避免污染 JSON stdout。"""
    with redirect_stdout(sys.stderr):
        return run_10y01_for_feature_window(**kwargs)
