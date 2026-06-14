from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from shared.calendar_service import get_calendar
from shared.input_artifacts import (
    build_daily_input_artifact,
    build_monthly_input_artifact,
    build_weekly_input_artifact,
    create_input_engine,
)
from shared.models import PredictionRecord

from .core.v28_common import MODEL_VERSION
from .inference import run_v28_for_feature_date, v28_feature_month_window


SCHEME_ID = "daily_5y_2_v28"
TARGET_TENOR = "5Y"
HORIZON = 5
DEFAULT_N_WORKERS = 10


def run_latest_prediction(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str],
    anchor_date: str,
    n_workers: int = DEFAULT_N_WORKERS,
) -> dict[str, Any]:
    """运行 V28 源算法月度窗口预测，返回 anchor_date 对应的一条明细。"""
    return run_v28_for_feature_date(
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        date_to_week=date_to_week,
        feature_date=anchor_date,
        require_labels=False,
        n_workers=n_workers,
    )


def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行日频 5Y_2 v28 实盘预测。

    predict_date 是发信日 T+1；模型特征截止到 predict_date 之前的最后一个交易日 T。
    """
    signal_date = predict_date
    engine = create_input_engine()
    try:
        calendar = get_calendar(engine=engine)
        anchor_date = _previous_trading_day(signal_date, calendar)
        target_date = calendar.nth_trading_day_after(anchor_date, HORIZON)
        start_date = (datetime.strptime(anchor_date, "%Y-%m-%d") - timedelta(days=8 * 365)).strftime("%Y-%m-%d")
        feature_week_id = calendar.week_id_for_date(anchor_date)
        if feature_week_id is None:
            raise RuntimeError(f"无法从 DB 日历解析 feature_date={anchor_date} 的 week_id")

        daily_artifact = build_daily_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            start_date=start_date,
            end_date=anchor_date,
            engine=engine,
        )
        weekly_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            end_week=int(feature_week_id),
            as_of_date=anchor_date,
            engine=engine,
        )
        monthly_artifact = build_monthly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            start_date=start_date,
            end_date=anchor_date,
            engine=engine,
        )
        date_to_week = _date_to_week_map(daily_artifact.dataframe, calendar)
        result = run_v28_for_feature_date(
            daily_df=daily_artifact.dataframe,
            weekly_df=weekly_artifact.dataframe,
            monthly_df=monthly_artifact.dataframe,
            date_to_week=date_to_week,
            feature_date=anchor_date,
            require_labels=False,
        )
        prediction = int(result["prediction"])
        confidence = float(result.get("confidence", abs(prediction)))
        return [
            PredictionRecord(
                scheme_id=SCHEME_ID,
                target_tenor=TARGET_TENOR,
                horizon=HORIZON,
                predict_date=signal_date,
                target_date=target_date,
                predicted_direction=prediction,
                feature_date=anchor_date,
                confidence=confidence,
                model_version=str(result.get("model_version") or MODEL_VERSION),
                extra=_record_extra(
                    anchor_date=anchor_date,
                    signal_date=signal_date,
                    target_date=target_date,
                    result=result,
                    daily_artifact=daily_artifact,
                    weekly_artifact=weekly_artifact,
                    monthly_artifact=monthly_artifact,
                ),
            )
        ]
    finally:
        engine.dispose()


def _previous_trading_day(signal_date: str, calendar) -> str:
    start = (datetime.strptime(signal_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    candidates = [day for day in calendar.next_trading_days(start, 60) if day < signal_date]
    if not candidates:
        raise ValueError(f"no previous trading day before {signal_date}")
    return candidates[-1]


def _date_to_week_map(daily_df: pd.DataFrame, calendar) -> dict[str, int | str]:
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result: dict[str, int | str] = {}
    for day in dates.dropna().unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is not None:
            result[str(day)] = int(week_id)
    return result


def _record_extra(
    *,
    anchor_date: str,
    signal_date: str,
    target_date: str,
    result: dict[str, Any],
    daily_artifact,
    weekly_artifact,
    monthly_artifact,
) -> dict[str, Any]:
    model_test_start, model_test_end = v28_feature_month_window(anchor_date)
    return {
        "feature_date": anchor_date,
        "anchor_date": anchor_date,
        "signal_date": signal_date,
        "target_date": target_date,
        "true_label": _clean_optional(result.get("true_label")),
        "vote_score": _clean_optional(result.get("vote_score")),
        "ens_prob": _clean_optional(result.get("ens_prob")),
        "model_scope": "v28_feature_month_window",
        "model_test_start": model_test_start,
        "model_test_end": model_test_end,
        "input_artifact_path": str(daily_artifact.path),
        "input_artifact_source": daily_artifact.source,
        "input_artifact_data_version": daily_artifact.data_version,
        "daily_input_artifact_path": str(daily_artifact.path),
        "daily_input_artifact_source": daily_artifact.source,
        "daily_input_artifact_data_version": daily_artifact.data_version,
        "weekly_input_artifact_path": str(weekly_artifact.path),
        "weekly_input_artifact_source": weekly_artifact.source,
        "weekly_input_artifact_data_version": weekly_artifact.data_version,
        "monthly_input_artifact_path": str(monthly_artifact.path),
        "monthly_input_artifact_source": monthly_artifact.source,
        "monthly_input_artifact_data_version": monthly_artifact.data_version,
    }


def _clean_optional(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value
