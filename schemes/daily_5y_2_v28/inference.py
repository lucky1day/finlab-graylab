from __future__ import annotations

import sys
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from .core.v28_common import MODEL_VERSION, model_config, run_prediction


DEFAULT_N_WORKERS = 10


def v28_feature_month_window(feature_date: str) -> tuple[str, str]:
    """返回 V28 源算法月度测试窗口：当月首日到 feature_date。"""
    parsed = datetime.strptime(str(feature_date), "%Y-%m-%d")
    return parsed.replace(day=1).strftime("%Y-%m-%d"), parsed.strftime("%Y-%m-%d")


def run_v28_for_feature_date(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    feature_date: str,
    require_labels: bool,
    n_workers: int = DEFAULT_N_WORKERS,
) -> dict[str, Any]:
    """按单个 feature_date 的源算法月度窗口运行 V28，并返回该日明细。"""
    window_start, window_end = v28_feature_month_window(feature_date)
    detail = run_v28_for_feature_window(
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        date_to_week=date_to_week,
        window_start=window_start,
        window_end=window_end,
        require_labels=require_labels,
        n_workers=n_workers,
    )
    matched = detail[detail["anchor_date"].astype(str) == feature_date]
    if matched.empty:
        raise RuntimeError(f"daily_5y_2_v28 produced no row for feature_date={feature_date}")
    return matched.iloc[-1].to_dict()


def run_v28_for_feature_window(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: Mapping[str, int | str] | None,
    window_start: str,
    window_end: str,
    require_labels: bool,
    n_workers: int = DEFAULT_N_WORKERS,
) -> pd.DataFrame:
    """运行一个 V28 月度窗口，返回逐 feature_date 明细。"""
    with redirect_stdout(sys.stderr):
        detail = run_prediction(
            model_config(
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                test_start=window_start,
                test_end=window_end,
                require_labels=require_labels,
                emit_report=False,
                return_details=True,
                n_workers=n_workers,
            )
        )
    if not isinstance(detail, pd.DataFrame) or detail.empty:
        raise RuntimeError(
            "daily_5y_2_v28 produced no prediction detail "
            f"for window {window_start}..{window_end}"
        )
    detail = detail.copy()
    detail["model_version"] = MODEL_VERSION
    return detail
