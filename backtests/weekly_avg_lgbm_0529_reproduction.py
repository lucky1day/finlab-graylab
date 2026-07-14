from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import RunOutput, make_run_output, persist_run_output
from backtests.repository import clean_json
from backtests.weekly_base_runner import (
    WeeklyAverageLabel,
    build_weekly_average_label_map_from_rows,
    compact_weekly_benchmark_rows,
    read_weekly_average_label_rows,
)
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE
from shared.weekly_average_lgbm_source_runner import run_source_weekly_backtest
from shared.weekly_average_source_evidence import (
    WEEKLY_AVERAGE_SOURCE_ROLE,
    require_weekly_average_source_evidence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ID = "model_muti_0529"
DATA_SOURCE = "framework_db_aligned"
HORIZON_DAYS = 6
BACKTEST_START_DATE = "2025-01-01"
BACKTEST_END_DATE = "2026-05-23"
LABEL_END_DATE = "2026-05-29"
LIVE_TARGET_START_DATE = "2026-06-01"
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


def run_weekly_avg_lgbm_0529_reproduction(
    scheme_id: str,
    *,
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行周平均 0529 LGBM source package 历史复现。"""
    started = time.time()
    evidence = require_weekly_average_source_evidence(scheme_id, project_root=PROJECT_ROOT)
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        calendar = get_calendar(engine)
        label_rows = read_weekly_average_label_rows(
            engine,
            target_tenor=evidence.target_tenor,
            end_date=LABEL_END_DATE,
        )
        labels = build_weekly_average_label_map_from_rows(
            label_rows,
            calendar=calendar,
            target_tenor=evidence.target_tenor,
            live_target_start_date=LIVE_TARGET_START_DATE,
        )
        source_rows = run_source_weekly_backtest(
            evidence,
            start_date=BACKTEST_START_DATE,
            end_date=BACKTEST_END_DATE,
        )
        full_rows = _platform_rows_from_source(scheme_id, evidence, source_rows, labels)
        if not full_rows:
            raise RuntimeError(f"{scheme_id} source weekly average reproduction produced no rows")

        start_date = min(str(row["predict_date"]) for row in full_rows)
        end_date = max(str(row["predict_date"]) for row in full_rows)
        output = make_run_output(
            scheme_id,
            DATA_SOURCE,
            start_date,
            end_date,
            full_rows,
            benchmark_id=BENCHMARK_ID,
        )
        _annotate_summary(output, evidence, source_rows, full_rows)
        run_id = persist_run_output(engine, output, benchmark_id=BENCHMARK_ID) if persist else None
        compact_rows = compact_prediction_rows(output.rows)
        run_payload = {
            "scheme_id": output.scheme_id,
            "data_source": output.data_source,
            "start_date": output.start_date,
            "end_date": output.end_date,
            "rows": compact_rows,
            "row_count": len(output.rows),
            "monthly_count": len(output.monthly_metrics),
            "summary": output.summary,
        }
        return {
            "status": "success",
            "run_id": run_id,
            "benchmark_id": BENCHMARK_ID,
            "scheme_id": output.scheme_id,
            "data_source": output.data_source,
            "start_date": output.start_date,
            "end_date": output.end_date,
            "row_count": len(output.rows),
            "monthly_count": len(output.monthly_metrics),
            "summary": output.summary,
            "rows": compact_rows,
            "runs": [run_payload],
            "elapsed_sec": round(time.time() - started, 3),
        }
    finally:
        if own_engine:
            engine.dispose()


def compact_prediction_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 与人工验收用紧凑预测序列。"""
    return compact_weekly_benchmark_rows(list(rows))


def _platform_rows_from_source(
    scheme_id: str,
    evidence: Any,
    source_rows: list[dict[str, Any]],
    labels: dict[int, WeeklyAverageLabel],
) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[int, str, str, str, int], dict[str, Any]] = {}
    for source in source_rows:
        if str(source.get("frequency")) != evidence.frequency:
            continue
        feature_week_id = _int_or_none(source.get("effective_week_id"))
        if feature_week_id is None:
            continue
        label = labels.get(feature_week_id)
        if label is None:
            continue
        direction = _int_or_none(source.get("pred_label"))
        confidence = _float_or_none(source.get("prob_up"))
        extra = _extra_from_source(evidence, source, label)
        row = {
            "benchmark_id": BENCHMARK_ID,
            "scheme_id": scheme_id,
            "target_tenor": evidence.target_tenor,
            "horizon": HORIZON_DAYS,
            "predict_date": label.feature_date,
            "feature_date": label.feature_date,
            "target_date": label.target_date,
            "label": label.label,
            "predicted_direction": direction,
            "model_pred": direction,
            "confidence": confidence,
            "source_row": clean_json(source),
            "extra": extra,
        }
        key = (
            label.feature_week_id,
            label.feature_date,
            label.target_date,
            evidence.target_tenor,
            HORIZON_DAYS,
        )
        previous = rows_by_key.get(key)
        if previous is not None:
            if _dedupe_signature(previous) != _dedupe_signature(row):
                raise RuntimeError(
                    f"{scheme_id}: conflicting duplicate source weekly-average rows for key={key}"
                )
            if _source_output_sort_key(row) >= _source_output_sort_key(previous):
                rows_by_key[key] = row
            continue
        rows_by_key[key] = row
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def _extra_from_source(
    evidence: Any,
    source: dict[str, Any],
    label: WeeklyAverageLabel,
) -> dict[str, Any]:
    extra = {
        "frequency": str(source.get("frequency")),
        "model_version": evidence.model_id,
        "target_rule": TARGET_RULE,
        "feature_week_id": label.feature_week_id,
        "target_week_id": label.target_week_id,
        "feature_date": label.feature_date,
        "target_date": label.target_date,
        "feature_yield": label.feature_yield,
        "target_yield": label.target_yield,
        "future_return": label.future_return,
        "source_role": WEEKLY_AVERAGE_SOURCE_ROLE,
        "source_package_hash": evidence.source_package_hash,
        "source_output_date": _str_or_none(source.get("source_output_date")),
        "original_rdate": _str_or_none(source.get("rdate")),
    }
    for field in INTERNAL_FIELDS:
        extra[field] = _clean_source_value(source.get(field))
    return clean_json(extra)


def _annotate_summary(
    output: RunOutput,
    evidence: Any,
    source_rows: list[dict[str, Any]],
    full_rows: list[dict[str, Any]],
) -> None:
    summary = output.summary
    summary["frequency"] = "weekly"
    summary["target_rule"] = TARGET_RULE
    summary["model_version"] = evidence.model_id
    summary["source_frequency"] = evidence.frequency
    summary["source_target_column"] = evidence.target_column
    summary["source_role"] = WEEKLY_AVERAGE_SOURCE_ROLE
    summary["source_package_hash"] = evidence.source_package_hash
    summary["source_runner_module"] = evidence.runner_module
    summary["source_detail_rows"] = len(source_rows)
    summary["source_detail_rows_for_frequency"] = sum(
        1 for row in source_rows if str(row.get("frequency")) == evidence.frequency
    )
    duplicate_stats = _source_duplicate_effective_week_stats(source_rows, evidence.frequency)
    summary["source_detail_unique_effective_weeks_for_frequency"] = duplicate_stats["unique_effective_weeks"]
    summary["source_duplicate_effective_week_count"] = duplicate_stats["duplicate_effective_week_count"]
    summary["source_duplicate_effective_week_rows_collapsed"] = duplicate_stats["duplicate_rows_collapsed"]
    summary["backtest_predict_start_date"] = BACKTEST_START_DATE
    summary["backtest_source_start_date"] = BACKTEST_START_DATE
    summary["backtest_source_end_date"] = BACKTEST_END_DATE
    summary["backtest_label_end_date"] = LABEL_END_DATE
    summary["backtest_mode"] = "source_package_weekly_lgbm_reproduction"
    summary["backtest_point_in_time"] = False
    summary["historical_backtest_exception"] = True
    summary["original_benchmark_validation"] = {
        "benchmark_rows": len(full_rows),
        "matched_rows": len(full_rows),
        "source_generated": True,
    }


def _dedupe_signature(row: dict[str, Any]) -> dict[str, Any]:
    extra = dict(row.get("extra") or {})
    ignored_extra = {"source_output_date", "original_rdate"}
    return clean_json(
        {
            "target_tenor": row.get("target_tenor"),
            "horizon": row.get("horizon"),
            "feature_date": row.get("feature_date"),
            "target_date": row.get("target_date"),
            "label": row.get("label"),
            "predicted_direction": row.get("predicted_direction"),
            "model_pred": row.get("model_pred"),
            "confidence": row.get("confidence"),
            "extra": {key: value for key, value in extra.items() if key not in ignored_extra},
        }
    )


def _source_output_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    extra = row.get("extra") or {}
    return (
        str(extra.get("source_output_date") or ""),
        str(extra.get("original_rdate") or ""),
    )


def _source_duplicate_effective_week_stats(source_rows: list[dict[str, Any]], frequency: str) -> dict[str, int]:
    counts: dict[int, int] = {}
    for row in source_rows:
        if str(row.get("frequency")) != frequency:
            continue
        feature_week_id = _int_or_none(row.get("effective_week_id"))
        if feature_week_id is None:
            continue
        counts[feature_week_id] = counts.get(feature_week_id, 0) + 1
    duplicate_rows = sum(count - 1 for count in counts.values() if count > 1)
    return {
        "unique_effective_weeks": len(counts),
        "duplicate_effective_week_count": sum(1 for count in counts.values() if count > 1),
        "duplicate_rows_collapsed": duplicate_rows,
    }


def _clean_source_value(value: Any) -> Any:
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


def _int_or_none(value: Any) -> int | None:
    value = _clean_source_value(value)
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    value = _clean_source_value(value)
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) or math.isinf(parsed) else parsed


def _str_or_none(value: Any) -> str | None:
    value = _clean_source_value(value)
    return None if value is None else str(value)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run weekly_avg_*_lgbm_0529 historical reproduction.")
    parser.add_argument("scheme_id", choices=["weekly_avg_1y_lgbm_0529", "weekly_avg_5y_lgbm_0529", "weekly_avg_10y_lgbm_0529"])
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    args = parser.parse_args(argv)
    payload = run_weekly_avg_lgbm_0529_reproduction(args.scheme_id, persist=not args.no_persist)
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
