from __future__ import annotations

import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from .core.v31_common import MODEL_VERSION, run_7y01_for_feature_window


DEFAULT_N_WORKERS = 10


@dataclass(frozen=True)
class PitWindow:
    """7Y_01 PIT 推理窗口。"""

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


def run_7y01_for_feature_date(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    feature_date: str,
    require_labels: bool,
    n_workers: int = DEFAULT_N_WORKERS,
) -> dict[str, Any]:
    """按单个 feature_date 的 PIT 窗口运行 7Y_01，并返回该日明细。"""
    window = liwei_0616_pit_window(feature_date)
    detail = run_7y01_for_window_silent(
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
        raise RuntimeError(f"liwei_0616 7Y_01 produced no row for feature_date={feature_date}")
    row = matched.iloc[-1].to_dict()
    row["model_version"] = str(row.get("model_version") or MODEL_VERSION)
    return row


def run_7y01_for_window_silent(**kwargs: Any) -> pd.DataFrame:
    """运行窗口并把源风格报告重定向到 stderr，避免污染 JSON stdout。"""
    with redirect_stdout(sys.stderr):
        return run_7y01_for_feature_window(**kwargs)
