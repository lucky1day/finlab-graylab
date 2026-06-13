from __future__ import annotations

from datetime import date, datetime
from typing import Any

from shared.models import PredictionRecord


LIVE_PHASES = {"gray_live", "scheduled_live"}


def validate_live_record_semantics(
    record: PredictionRecord,
    *,
    expected_predict_date: str,
    prefix: str,
    require_phase: bool,
) -> list[str]:
    """校验 live/dry-run 预测记录的日期与 phase 语义。"""
    errors: list[str] = []
    if _date_string(record.predict_date) != expected_predict_date:
        errors.append(f"{prefix}.predict_date expected {expected_predict_date}, got {record.predict_date}")

    feature_date = _date_string(record.feature_date)
    if not feature_date:
        errors.append(f"{prefix}.feature_date is required")
    elif feature_date >= expected_predict_date:
        errors.append(f"{prefix}.feature_date must be before predict_date, got {feature_date} >= {expected_predict_date}")

    target_date = _date_string(record.target_date)
    if not target_date:
        errors.append(f"{prefix}.target_date is required")
    elif feature_date and target_date <= feature_date:
        errors.append(f"{prefix}.target_date must be after feature_date, got {target_date} <= {feature_date}")

    phase = str(record.prediction_phase or "").strip()
    if require_phase and phase not in LIVE_PHASES:
        errors.append(f"{prefix}.prediction_phase must be one of {sorted(LIVE_PHASES)}, got {record.prediction_phase}")
    elif phase and phase not in LIVE_PHASES:
        errors.append(f"{prefix}.prediction_phase invalid: {record.prediction_phase}")
    return errors


def _date_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
