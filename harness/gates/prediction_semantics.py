from __future__ import annotations

from datetime import date, datetime
from typing import Any

from shared.models import PredictionRecord
from shared.prediction_context import (
    LIVE_PREDICTION_PHASES,
    build_daily_live_context,
    build_monthly_live_context,
    build_period_average_live_context,
    build_weekly_live_context,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


def validate_live_record_semantics(
    record: PredictionRecord,
    *,
    expected_predict_date: str,
    prefix: str,
    frequency: str | None = None,
    horizon: int | None = None,
    calendar: Any | None = None,
    expected_weekly_target_rule: str | None = None,
    task_type: str | None = None,
) -> list[str]:
    """校验 live/dry-run 预测记录的日期与 phase 语义。"""
    errors: list[str] = []
    if _date_string(record.predict_date) != expected_predict_date:
        errors.append(f"{prefix}.predict_date expected {expected_predict_date}, got {record.predict_date}")

    feature_date = _date_string(record.feature_date)
    if not feature_date:
        errors.append(f"{prefix}.feature_date is required")
    elif frequency == "monthly" or task_type in PERIOD_AVERAGE_TASK_TYPES:
        if feature_date > expected_predict_date:
            errors.append(
                f"{prefix}.feature_date must be on or before predict_date for "
                f"monthly/period-average, "
                f"got {feature_date} > {expected_predict_date}"
            )
    elif feature_date >= expected_predict_date:
        errors.append(f"{prefix}.feature_date must be before predict_date, got {feature_date} >= {expected_predict_date}")

    target_date = _date_string(record.target_date)
    if not target_date:
        errors.append(f"{prefix}.target_date is required")
    elif feature_date and target_date <= feature_date:
        errors.append(f"{prefix}.target_date must be after feature_date, got {target_date} <= {feature_date}")

    phase = str(record.prediction_phase or "").strip()
    if phase and phase not in LIVE_PREDICTION_PHASES:
        errors.append(f"{prefix}.prediction_phase invalid: {record.prediction_phase}")
    if calendar is not None and frequency:
        errors.extend(
            _validate_against_calendar_context(
                record,
                expected_predict_date=expected_predict_date,
                prefix=prefix,
                frequency=frequency,
                horizon=horizon,
                calendar=calendar,
                feature_date=feature_date,
                target_date=target_date,
                expected_weekly_target_rule=expected_weekly_target_rule,
                task_type=task_type,
            )
        )
    return errors


def _validate_against_calendar_context(
    record: PredictionRecord,
    *,
    expected_predict_date: str,
    prefix: str,
    frequency: str,
    horizon: int | None,
    calendar: Any,
    feature_date: str,
    target_date: str,
    expected_weekly_target_rule: str | None,
    task_type: str | None,
) -> list[str]:
    errors: list[str] = []
    try:
        if task_type in PERIOD_AVERAGE_TASK_TYPES:
            expected = build_period_average_live_context(
                calendar,
                expected_predict_date,
                task_type=task_type,
            )
            if feature_date and feature_date != expected.feature_date:
                errors.append(
                    f"{prefix}.feature_date expected {expected.feature_date}, got {feature_date}"
                )
            if target_date and target_date != expected.target_date:
                errors.append(
                    f"{prefix}.target_date expected {expected.target_date}, got {target_date}"
                )
        elif frequency == "daily":
            if not horizon:
                return [f"{prefix}.horizon is required for daily date semantics"]
            expected = build_daily_live_context(calendar, expected_predict_date, horizon=int(horizon))
            if feature_date and feature_date != expected.feature_date:
                errors.append(f"{prefix}.feature_date expected {expected.feature_date}, got {feature_date}")
            if target_date and target_date != expected.target_date:
                errors.append(f"{prefix}.target_date expected {expected.target_date}, got {target_date}")
        elif frequency == "weekly":
            expected = build_weekly_live_context(calendar, expected_predict_date)
            expected_target_rule = expected_weekly_target_rule or expected.target_rule
            extra = record.extra or {}
            if feature_date and feature_date != expected.feature_date:
                errors.append(f"{prefix}.feature_date expected {expected.feature_date}, got {feature_date}")
            if target_date and target_date != expected.target_date:
                errors.append(f"{prefix}.target_date expected {expected.target_date}, got {target_date}")
            if str(extra.get("feature_week_id", "")) and int(extra["feature_week_id"]) != expected.feature_week_id:
                errors.append(
                    f"{prefix}.extra.feature_week_id expected {expected.feature_week_id}, got {extra.get('feature_week_id')}"
                )
            if str(extra.get("target_week_id", "")) and int(extra["target_week_id"]) != expected.target_week_id:
                errors.append(
                    f"{prefix}.extra.target_week_id expected {expected.target_week_id}, got {extra.get('target_week_id')}"
                )
            if extra.get("target_rule") and extra.get("target_rule") != expected_target_rule:
                errors.append(
                    f"{prefix}.extra.target_rule expected {expected_target_rule}, got {extra.get('target_rule')}"
                )
        elif frequency == "monthly":
            expected = build_monthly_live_context(calendar, expected_predict_date)
            extra = record.extra or {}
            if feature_date and feature_date != expected.feature_date:
                errors.append(f"{prefix}.feature_date expected {expected.feature_date}, got {feature_date}")
            if target_date and target_date != expected.target_date:
                errors.append(f"{prefix}.target_date expected {expected.target_date}, got {target_date}")
            expected_values = {
                "trigger_date": expected.trigger_date,
                "scheduled_trigger_date": expected.scheduled_trigger_date,
                "db_rdate": expected.db_rdate,
                "input_cutoff_date": expected.feature_date,
                "feature_month_id": expected.feature_month_id,
                "target_month_id": expected.target_month_id,
                "target_rule": expected.target_rule,
            }
            for key, expected_value in expected_values.items():
                if extra.get(key) and str(extra.get(key)) != str(expected_value):
                    errors.append(f"{prefix}.extra.{key} expected {expected_value}, got {extra.get(key)}")
    except Exception as exc:
        errors.append(f"{prefix}.date_semantics calendar validation failed: {exc}")
    return errors


def _date_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
