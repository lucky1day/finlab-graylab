from __future__ import annotations

import math
from typing import Any

import pandas as pd

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_weekly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, next_calendar_week_id
from shared.weekly_average_lgbm_source_runner import run_source_weekly_live
from shared.weekly_average_source_evidence import (
    WEEKLY_AVERAGE_SOURCE_ROLE,
    require_weekly_average_source_evidence,
)


HORIZON_DAYS = 6
TARGET_RULE = WEEKLY_AVERAGE_TARGET_RULE
INTERNAL_FIELDS = (
    "frequency",
    "model_id",
    "source",
    "close_col",
    "prob_up",
    "threshold_used",
    "model_margin",
    "training_rows",
    "calibration_rows",
    "feature_count",
    "effective_week_id",
)


def run_weekly_average_lgbm_prediction(scheme_id: str, predict_date: str) -> list[PredictionRecord]:
    """执行 source-original 周平均 LGBM live adapter。"""
    evidence = require_weekly_average_source_evidence(scheme_id)
    source_rows = run_source_weekly_live(evidence, predict_date=predict_date)
    source = _select_frequency_row(source_rows, evidence.frequency, scheme_id)
    feature_week_id = _required_int(source.get("effective_week_id"), "effective_week_id")

    engine = create_input_engine()
    try:
        calendar = get_calendar(engine)
        feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
        target_week_id = next_calendar_week_id(calendar, feature_week_id)
        target_date = calendar.week_id_to_last_trading_day(target_week_id)
        input_artifact = build_weekly_input_artifact(
            scheme_id=scheme_id,
            predict_date=predict_date,
            schema_columns=["week_id", evidence.target_column],
            start_week=feature_week_id - 600,
            end_week=feature_week_id,
            as_of_date=feature_date,
            engine=engine,
        )
    finally:
        engine.dispose()

    direction = _required_int(source.get("pred_label"), "pred_label")
    confidence = _float_or_none(source.get("prob_up"))
    return [
        PredictionRecord(
            scheme_id=scheme_id,
            target_tenor=evidence.target_tenor,
            horizon=HORIZON_DAYS,
            predict_date=predict_date,
            target_date=target_date,
            predicted_direction=direction,
            feature_date=feature_date,
            confidence=confidence,
            model_version=evidence.model_id,
            extra=_extra_from_source(
                evidence,
                source,
                feature_week_id,
                target_week_id,
                feature_date,
                target_date,
                input_artifact_path=str(input_artifact.path),
                input_artifact_source=str(input_artifact.source),
            ),
        )
    ]


def _select_frequency_row(
    source_rows: list[dict[str, Any]],
    frequency: str,
    scheme_id: str,
) -> dict[str, Any]:
    matches = [row for row in source_rows if str(row.get("frequency")) == frequency]
    if len(matches) != 1:
        raise RuntimeError(f"{scheme_id}: expected exactly one source row for {frequency}, got {len(matches)}")
    return matches[0]


def _extra_from_source(
    evidence: Any,
    source: dict[str, Any],
    feature_week_id: int,
    target_week_id: int,
    feature_date: str,
    target_date: str,
    input_artifact_path: str,
    input_artifact_source: str,
) -> dict[str, Any]:
    extra = {
        "feature_week_id": feature_week_id,
        "target_week_id": target_week_id,
        "feature_date": feature_date,
        "target_date": target_date,
        "target_rule": TARGET_RULE,
        "source_role": WEEKLY_AVERAGE_SOURCE_ROLE,
        "source_package_hash": evidence.source_package_hash,
        "source_output_date": _str_or_none(source.get("source_output_date")),
        "original_rdate": _str_or_none(source.get("rdate")),
        "input_artifact_path": input_artifact_path,
        "input_artifact_source": input_artifact_source,
    }
    for field in INTERNAL_FIELDS:
        extra[field] = _clean_value(source.get(field))
    return extra


def _required_int(value: Any, field: str) -> int:
    parsed = _int_or_none(value)
    if parsed is None:
        raise RuntimeError(f"source weekly average row missing integer {field}")
    return parsed


def _int_or_none(value: Any) -> int | None:
    value = _clean_value(value)
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    value = _clean_value(value)
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) or math.isinf(parsed) else parsed


def _str_or_none(value: Any) -> str | None:
    value = _clean_value(value)
    return None if value is None else str(value)


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value
