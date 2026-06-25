from __future__ import annotations

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

from .core.v31_common import MODEL_VERSION, PROD_CONFIG, SOURCE_MODEL_ID
from .inference import liwei_0616_pit_window, run_5y01_for_feature_date


SCHEME_ID = "liwei_0616_cons_sda_k3_div_k10"
TARGET_TENOR = "5Y"
HORIZON = 5
DEFAULT_N_WORKERS = 10
INPUT_START_DATE = "2010-07-27"


def run(predict_date: str) -> list[PredictionRecord]:
    """执行 liwei_0616 5Y_01 日频 T+5 实盘预测。"""
    signal_date = predict_date
    engine = create_input_engine()
    try:
        calendar = get_calendar(engine=engine)
        feature_date = calendar.previous_trading_day(signal_date)
        target_date = calendar.nth_trading_day_after(feature_date, HORIZON)
        feature_week_id = calendar.week_id_for_date(feature_date)
        if feature_week_id is None:
            raise RuntimeError(f"无法从 DB 日历解析 feature_date={feature_date} 的 week_id")

        daily_artifact = build_daily_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            start_date=INPUT_START_DATE,
            end_date=feature_date,
            engine=engine,
        )
        weekly_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            end_week=int(feature_week_id),
            as_of_date=feature_date,
            engine=engine,
        )
        monthly_artifact = build_monthly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date=signal_date,
            start_date=INPUT_START_DATE,
            end_date=feature_date,
            engine=engine,
        )
        date_to_week = _date_to_week_map(daily_artifact.dataframe, calendar)
        result = run_5y01_for_feature_date(
            daily_df=daily_artifact.dataframe,
            weekly_df=weekly_artifact.dataframe,
            monthly_df=monthly_artifact.dataframe,
            date_to_week=date_to_week,
            feature_date=feature_date,
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
                feature_date=feature_date,
                confidence=confidence,
                model_version=str(result.get("model_version") or MODEL_VERSION),
                extra=_record_extra(
                    feature_date=feature_date,
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
    feature_date: str,
    signal_date: str,
    target_date: str,
    result: dict[str, Any],
    daily_artifact,
    weekly_artifact,
    monthly_artifact,
) -> dict[str, Any]:
    window = liwei_0616_pit_window(feature_date)
    return {
        "feature_date": feature_date,
        "anchor_date": feature_date,
        "signal_date": signal_date,
        "target_date": target_date,
        "source_model_id": SOURCE_MODEL_ID,
        "true_label": _clean_optional(result.get("true_label")),
        "vote_score": _clean_optional(result.get("vote_score")),
        "baseline_signs": _clean_optional(result.get("baseline_signs")),
        "baseline_scores": _clean_optional(result.get("baseline_scores")),
        "model_scope": "liwei_0616_5y01_source_compatible_context",
        "vote_baselines": list(PROD_CONFIG["baselines"]),
        "fallback_baseline": str(PROD_CONFIG["fallback"]),
        "streak_K": int(PROD_CONFIG["streak_K"]),
        "model_prior_start": window.prior_start,
        "model_prior_end": window.prior_end,
        "model_latest_start": window.latest_start,
        "model_source_end": window.source_end,
        "model_current_start": window.current_start,
        "model_current_end": window.current_end,
        "model_test_ranges": [list(item) for item in window.test_ranges],
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
    except (TypeError, ValueError):
        pass
    return value
