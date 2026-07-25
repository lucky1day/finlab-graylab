from __future__ import annotations

import math
import os
import re
from typing import Any

from shared.calendar_service import get_calendar
from shared.daily_0629_source_evidence import DAILY_0629_SOURCE_ROLE, require_daily_0629_source_evidence
from shared.daily_0629_source_runner import run_source_daily_live
from shared.input_artifacts import (
    LIVE_SOURCE_FEATURE_DATE_ENV,
    LIVE_SOURCE_FENCE_GENERATION_ID_ENV,
    LIVE_SOURCE_INPUT_MODE,
    LIVE_SOURCE_INPUT_MODE_ENV,
    LIVE_SOURCE_PACKAGE_SHA256_ENV,
    build_daily_input_artifact,
    create_input_engine,
)
from shared.models import PredictionRecord
from shared.prediction_context import build_daily_live_context
from shared.source_runtime_database import (
    load_source_runtime_database_config,
)


HORIZON_DAYS = 1
DAILY_0629_SOURCE_START_DATE = "2010-01-01"
DAILY_0629_INTERNAL_FIELDS = (
    "frequency",
    "final_select_id",
    "candidate_id",
    "target_col",
    "prediction_mode",
    "pred_label",
    "candidate_pred",
    "raw_direction",
    "model_pred",
    "vote_pred",
    "raw_vote_pred",
    "prob_up",
    "threshold",
    "active_score",
    "decision",
    "signal_sum",
    "vote_sum",
    "model_pred_combo",
    "vote_pred_combo",
    "signal_sum_combo",
    "vote_sum_combo",
    "pre_quota_candidate_pred",
    "candidate_pred_before_direction_tuning",
    "direction_tuning_rule",
    "tie_lowconf_flip_width",
    "tie_lowconf_flipped",
    "fit_n",
    "cal_n",
)


def run_daily_0629_prediction(scheme_id: str, predict_date: str) -> list[PredictionRecord]:
    """执行日度 0629 source-original T+1 binary runner adapter。"""
    live_source_context = _scheduled_live_source_context()
    evidence = require_daily_0629_source_evidence(scheme_id)
    if (
        live_source_context is not None
        and evidence.source_package_hash
        != live_source_context["source_package_sha256"]
    ):
        raise RuntimeError(
            f"{scheme_id}: live source package hash differs from "
            "frozen occurrence policy"
        )
    database_config = load_source_runtime_database_config()
    engine = create_input_engine(
        database_config=database_config,
    )
    try:
        calendar = get_calendar(engine)
        context = build_daily_live_context(calendar, predict_date, horizon=HORIZON_DAYS)
        input_artifact = build_daily_input_artifact(
            scheme_id=scheme_id,
            predict_date=predict_date,
            start_date=DAILY_0629_SOURCE_START_DATE,
            end_date=context.feature_date,
            engine=engine,
        )
    finally:
        engine.dispose()

    if live_source_context is not None:
        if (
            live_source_context["feature_date"]
            != context.feature_date
        ):
            raise RuntimeError(
                f"{scheme_id}: frozen compatibility feature_date "
                f"{live_source_context['feature_date']} differs from "
                f"live context {context.feature_date}"
            )
        if input_artifact.source_watermark != context.feature_date:
            raise RuntimeError(
                f"{scheme_id}: live source data watermark "
                f"{input_artifact.source_watermark} differs from "
                f"feature_date {context.feature_date}"
            )
    source_rows = run_source_daily_live(
        evidence,
        predict_date=predict_date,
        database_config=database_config,
    )
    source = _select_frequency_row(
        source_rows,
        evidence.frequency,
        scheme_id,
    )
    if live_source_context is not None:
        source_watermark = _str_or_none(source.get("prediction_date"))
        if source_watermark != context.feature_date:
            raise RuntimeError(
                f"{scheme_id}: source output data watermark "
                f"{source_watermark!r} differs from feature_date "
                f"{context.feature_date}"
            )
    _assert_source_context_matches(source, context.feature_date, context.target_date, scheme_id)
    direction = _direction_from_source(source)
    confidence = _float_or_none(source.get("prob_up"))
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
            model_version=_model_version_from_evidence(evidence),
            extra=_extra_from_source(
                evidence,
                source,
                feature_date=context.feature_date,
                target_date=context.target_date,
                input_artifact_path=str(input_artifact.path),
                input_artifact_source=str(input_artifact.source),
                input_artifact_watermark=(
                    input_artifact.source_watermark
                ),
                live_source_context=live_source_context,
            ),
        )
    ]


def _select_frequency_row(source_rows: list[dict[str, Any]], frequency: str, scheme_id: str) -> dict[str, Any]:
    matches = [row for row in source_rows if str(row.get("frequency")) == frequency]
    if len(matches) != 1:
        raise RuntimeError(f"{scheme_id}: expected exactly one source row for {frequency}, got {len(matches)}")
    return matches[0]


def _assert_source_context_matches(source: dict[str, Any], feature_date: str, target_date: str, scheme_id: str) -> None:
    source_feature = _str_or_none(source.get("prediction_date") or source.get("expected_prediction_date"))
    source_target = _str_or_none(source.get("rdate"))
    if source_feature and source_feature != feature_date:
        raise RuntimeError(f"{scheme_id}: source prediction_date expected {feature_date}, got {source_feature}")
    if source_target and source_target != target_date:
        raise RuntimeError(f"{scheme_id}: source rdate expected target_date {target_date}, got {source_target}")


def _extra_from_source(
    evidence: Any,
    source: dict[str, Any],
    *,
    feature_date: str,
    target_date: str,
    input_artifact_path: str,
    input_artifact_source: str,
    input_artifact_watermark: str | None,
    live_source_context: dict[str, str] | None,
) -> dict[str, Any]:
    extra = {
        "source_role": DAILY_0629_SOURCE_ROLE,
        "source_package_hash": evidence.source_package_hash,
        "source_model_id": evidence.model_id,
        "source_output_date": _str_or_none(source.get("source_output_date")),
        "source_prediction_date": _str_or_none(source.get("prediction_date")),
        "source_rdate": _str_or_none(source.get("rdate")),
        "input_cutoff_date": feature_date,
        "feature_date": feature_date,
        "target_date": target_date,
        "input_artifact_path": input_artifact_path,
        "input_artifact_source": input_artifact_source,
    }
    if live_source_context is not None:
        extra.update(
            {
                "input_mode": LIVE_SOURCE_INPUT_MODE,
                "data_watermark":
                    _str_or_none(source.get("prediction_date")),
                "data_watermark_basis":
                    "source_output_prediction_date",
                "input_artifact_watermark":
                    input_artifact_watermark,
                "live_source_fence_generation_id":
                    live_source_context["generation_id"],
            }
        )
    for field in DAILY_0629_INTERNAL_FIELDS:
        extra[field] = _clean_internal_field(field, source.get(field))
    return extra


def _scheduled_live_source_context() -> dict[str, str] | None:
    """读取协调器显式注入的 0629 live-source compatibility fence。"""
    environment = {
        "input_mode": os.environ.get(LIVE_SOURCE_INPUT_MODE_ENV),
        "generation_id": os.environ.get(
            LIVE_SOURCE_FENCE_GENERATION_ID_ENV
        ),
        "feature_date": os.environ.get(LIVE_SOURCE_FEATURE_DATE_ENV),
        "source_package_sha256": os.environ.get(
            LIVE_SOURCE_PACKAGE_SHA256_ENV
        ),
    }
    configured = {
        key: value
        for key, value in environment.items()
        if value is not None
    }
    if not configured:
        return None
    missing = sorted(set(environment) - set(configured))
    empty = sorted(
        key
        for key, value in configured.items()
        if not isinstance(value, str) or not value.strip()
    )
    if missing or empty:
        raise RuntimeError(
            "partial daily 0629 live source compatibility environment: "
            f"missing={missing}, empty={empty}"
        )
    if configured["input_mode"] != LIVE_SOURCE_INPUT_MODE:
        raise RuntimeError(
            "unsupported daily 0629 live source input mode: "
            f"{configured['input_mode']}"
        )
    if re.fullmatch(
        r"[0-9a-f]{64}",
        str(configured["source_package_sha256"]),
    ) is None:
        raise RuntimeError(
            "invalid daily 0629 live source package sha256"
        )
    return {
        "generation_id": str(configured["generation_id"]),
        "feature_date": str(configured["feature_date"]),
        "source_package_sha256": str(
            configured["source_package_sha256"]
        ),
    }


def _model_version_from_evidence(evidence: Any) -> str:
    return str(evidence.final_select_id)


def _direction_from_source(source: dict[str, Any]) -> int:
    for field in ("pred_label", "candidate_pred", "vote_pred", "model_pred"):
        parsed = _int_or_none(source.get(field))
        if parsed in {-1, 0, 1}:
            return int(parsed)
    raise RuntimeError("daily 0629 source row missing final prediction direction")


def _clean_internal_field(field: str, value: Any) -> Any:
    if field in {
        "pred_label",
        "candidate_pred",
        "raw_direction",
        "model_pred",
        "vote_pred",
        "raw_vote_pred",
        "signal_sum",
        "vote_sum",
        "model_pred_combo",
        "vote_pred_combo",
        "signal_sum_combo",
        "pre_quota_candidate_pred",
        "candidate_pred_before_direction_tuning",
        "fit_n",
        "cal_n",
    }:
        return _int_or_none(value)
    if field in {"prob_up", "threshold", "active_score", "vote_sum_combo", "tie_lowconf_flip_width"}:
        return _float_or_none(value)
    if field == "tie_lowconf_flipped":
        return _bool_or_none(value)
    return _clean_value(value)


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


def _bool_or_none(value: Any) -> bool | None:
    value = _clean_value(value)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


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
