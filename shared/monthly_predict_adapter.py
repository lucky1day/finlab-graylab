from __future__ import annotations

import math
from typing import Any

from shared.calendar_service import get_calendar
from shared.input_artifacts import build_monthly_input_artifact, create_input_engine
from shared.models import PredictionRecord
from shared.monthly_source_evidence import MONTHLY_SOURCE_ROLE, require_monthly_source_evidence
from shared.monthly_source_runner import run_source_monthly_live
from shared.prediction_context import MONTHLY_TARGET_RULE, build_monthly_live_context
from shared.source_runtime_database import (
    load_source_runtime_database_config,
)


HORIZON_DAYS = 30
MONTHLY_SOURCE_START_DATE = "2010-01-01"
INTERNAL_FIELDS = (
    "frequency",
    "final_select_id",
    "candidate_id",
    "model_name",
    "top_n",
    "y_pred",
    "pred_proba_up",
    "pred_proba_down",
    "param_index",
    "params_json",
    "training_rows",
    "validation_rows",
    "validation_overall_accuracy",
    "param_selection",
)


def run_monthly_prediction(scheme_id: str, predict_date: str) -> list[PredictionRecord]:
    """执行 source-original 月度 binary runner adapter。"""
    database_config = load_source_runtime_database_config()
    evidence = require_monthly_source_evidence(scheme_id)
    source_rows = run_source_monthly_live(
        evidence,
        predict_date=predict_date,
        database_config=database_config,
    )
    source = _select_frequency_row(source_rows, evidence.frequency, scheme_id)

    engine = create_input_engine(
        database_config=database_config,
    )
    try:
        calendar = get_calendar(engine)
        context = build_monthly_live_context(calendar, predict_date)
        input_artifact = build_monthly_input_artifact(
            scheme_id=scheme_id,
            predict_date=predict_date,
            start_date=MONTHLY_SOURCE_START_DATE,
            end_date=context.feature_date,
            engine=engine,
        )
    finally:
        engine.dispose()

    _assert_source_context_matches(source, context.feature_month_id, context.target_month_id, scheme_id)
    direction = _direction_from_y_pred(source.get("y_pred"))
    confidence = _confidence_for_direction(source, direction)
    return [
        PredictionRecord(
            scheme_id=scheme_id,
            target_tenor=evidence.target_tenor,
            horizon=HORIZON_DAYS,
            predict_date=predict_date,
            feature_date=context.feature_date,
            target_date=context.target_date,
            predicted_direction=direction,
            confidence=confidence,
            model_version=evidence.model_id,
            extra=_extra_from_source(
                evidence,
                source,
                context,
                input_artifact_path=str(input_artifact.path),
                input_artifact_source=str(input_artifact.source),
            ),
        )
    ]


def _select_frequency_row(source_rows: list[dict[str, Any]], frequency: str, scheme_id: str) -> dict[str, Any]:
    matches = [row for row in source_rows if str(row.get("frequency")) == frequency]
    if len(matches) != 1:
        raise RuntimeError(f"{scheme_id}: expected exactly one source row for {frequency}, got {len(matches)}")
    return matches[0]


def _assert_source_context_matches(source: dict[str, Any], feature_month_id: str, target_month_id: str, scheme_id: str) -> None:
    source_feature = _str_or_none(source.get("feature_month_id"))
    source_target = _str_or_none(source.get("target_month_id"))
    if source_feature and source_feature != feature_month_id:
        raise RuntimeError(f"{scheme_id}: source feature_month_id expected {feature_month_id}, got {source_feature}")
    if source_target and source_target != target_month_id:
        raise RuntimeError(f"{scheme_id}: source target_month_id expected {target_month_id}, got {source_target}")


def _extra_from_source(
    evidence: Any,
    source: dict[str, Any],
    context: Any,
    *,
    input_artifact_path: str,
    input_artifact_source: str,
) -> dict[str, Any]:
    extra = {
        "db_rdate": context.db_rdate,
        "trigger_date": context.trigger_date,
        "scheduled_trigger_date": context.scheduled_trigger_date,
        "input_cutoff_date": context.feature_date,
        "feature_month_id": context.feature_month_id,
        "target_month_id": context.target_month_id,
        "feature_date": context.feature_date,
        "target_date": context.target_date,
        "target_rule": MONTHLY_TARGET_RULE,
        "source_role": MONTHLY_SOURCE_ROLE,
        "source_package_hash": evidence.source_package_hash,
        "source_output_date": _str_or_none(source.get("source_output_date")),
        "input_artifact_path": input_artifact_path,
        "input_artifact_source": input_artifact_source,
    }
    for field in INTERNAL_FIELDS:
        extra[field] = _clean_internal_field(field, source.get(field))
    for field in (
        "feature_observation_date",
        "target_observation_date",
        "T_yield_close",
        "T_plus_1_yield_close",
        "yield_diff",
        "yield_direction",
        "business_direction_pred",
        "result_mark",
    ):
        if field in source:
            extra[field] = _clean_value(source.get(field))
    return extra


def _direction_from_y_pred(value: Any) -> int:
    parsed = _int_or_none(value)
    if parsed == 1:
        return 1
    if parsed == 0:
        return -1
    return 0


def _clean_internal_field(field: str, value: Any) -> Any:
    if field in {"y_pred", "param_index", "training_rows", "validation_rows"}:
        return _int_or_none(value)
    if field in {"pred_proba_up", "pred_proba_down", "validation_overall_accuracy"}:
        return _float_or_none(value)
    return _clean_value(value)


def _confidence_for_direction(source: dict[str, Any], direction: int) -> float | None:
    if direction > 0:
        return _float_or_none(source.get("pred_proba_up"))
    if direction < 0:
        return _float_or_none(source.get("pred_proba_down"))
    up = _float_or_none(source.get("pred_proba_up"))
    down = _float_or_none(source.get("pred_proba_down"))
    values = [value for value in (up, down) if value is not None]
    return max(values) if values else None


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
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        return None if value == "" else value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value
