from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.engine import Engine

from backtests.actuals import build_monthly_actual_records
from backtests.repository import (
    clean_json,
    create_engine_from_env,
    create_backtest_run,
    replace_backtest_predictions,
    update_backtest_run_summary,
)
from shared.models import PredictionRecord
from shared.monthly_predict_adapter import run_monthly_prediction
from shared.monthly_source_evidence import PLATFORM_CURRENT_MONTHLY_ROLE, require_monthly_source_evidence
from shared.prediction_context import MONTHLY_TARGET_RULE


BENCHMARK_ID = "monthly_0629"
DATA_SOURCE = "framework_db_aligned"
SOURCE_ORIGINAL_DATA_SOURCE = "source_original_monthly_binary_runner"
DEFAULT_PREDICT_DATES = (
    "2025-01-15",
    "2025-02-15",
    "2025-03-15",
    "2025-04-15",
    "2025-05-15",
    "2025-06-15",
    "2025-07-15",
    "2025-08-15",
    "2025-09-15",
    "2025-10-15",
    "2025-11-15",
    "2025-12-15",
    "2026-01-15",
    "2026-02-15",
    "2026-03-15",
    "2026-04-15",
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_monthly_0629_reproduction(
    scheme_id: str,
    *,
    predict_dates: Iterable[str] | None = None,
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行月度 0629 source-backed 历史复现。"""
    started = time.time()
    dates = list(predict_dates or DEFAULT_PREDICT_DATES)
    owned_engine = engine is None
    db_engine = engine or create_engine_from_env()
    try:
        records = [record for predict_date in dates for record in run_monthly_prediction(scheme_id, predict_date)]
        actuals = _monthly_actual_lookup(db_engine, records)
        rows = [_row_from_record(record, actuals) for record in records]
        summary = _summary(scheme_id, rows)
        payload = {
            "status": "success",
            "scheme_id": scheme_id,
            "benchmark_id": BENCHMARK_ID,
            "data_source": DATA_SOURCE,
            "row_count": len(rows),
            "monthly_count": len(rows),
            "rows": rows,
            "summary": summary,
            "elapsed_sec": round(time.time() - started, 3),
        }
        if persist:
            run_id = create_backtest_run(
                db_engine,
                benchmark_id=BENCHMARK_ID,
                scheme_id=scheme_id,
                data_source=DATA_SOURCE,
                start_date=min(row["predict_date"] for row in rows) if rows else min(dates),
                end_date=max(row["target_date"] for row in rows) if rows else max(dates),
                summary=summary,
            )
            written = replace_backtest_predictions(db_engine, run_id, rows)
            summary = {**summary, "backtest_run_id": run_id, "written_predictions": written}
            update_backtest_run_summary(db_engine, run_id=run_id, summary=summary)
            payload["backtest_run_id"] = run_id
            payload["summary"] = summary
        return clean_json(payload)
    finally:
        if owned_engine:
            db_engine.dispose()


def _monthly_actual_lookup(
    engine: Engine,
    records: list[PredictionRecord],
) -> dict[tuple[str, str, str], int]:
    if not records:
        return {}
    tenors = sorted({record.target_tenor for record in records})
    end_date = max(record.target_date for record in records)
    actuals = build_monthly_actual_records(engine, tenors=tenors, end_date=end_date)
    return {
        (record.tenor, record.target_date, record.target_rule): record.direction_monthly
        for record in actuals
    }


def _row_from_record(
    record: PredictionRecord,
    actuals: dict[tuple[str, str, str], int],
) -> dict[str, Any]:
    extra = dict(record.extra or {})
    target_rule = str(extra.get("target_rule") or MONTHLY_TARGET_RULE)
    extra["data_source"] = DATA_SOURCE
    extra["source_original_data_source"] = SOURCE_ORIGINAL_DATA_SOURCE
    extra["benchmark_role"] = PLATFORM_CURRENT_MONTHLY_ROLE
    label = actuals.get((record.target_tenor, record.target_date, target_rule))
    is_correct = None if label is None else record.predicted_direction == label
    row = {
        "benchmark_id": BENCHMARK_ID,
        "scheme_id": record.scheme_id,
        "target_tenor": record.target_tenor,
        "horizon": record.horizon,
        "predict_date": record.predict_date,
        "feature_date": record.feature_date,
        "target_date": record.target_date,
        "feature_month_id": extra.get("feature_month_id"),
        "target_month_id": extra.get("target_month_id"),
        "target_rule": target_rule,
        "label": label,
        "predicted_direction": record.predicted_direction,
        "model_pred": extra.get("y_pred"),
        "direction": record.predicted_direction,
        "confidence": record.confidence,
        "is_correct": is_correct,
        "source_row": extra,
        "extra": extra,
    }
    for field in (
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
    ):
        row[field] = extra.get(field)
    return row


def _summary(scheme_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    labeled = [row for row in rows if row.get("label") is not None]
    correct = [row for row in labeled if row.get("is_correct") is True]
    evidence = require_monthly_source_evidence(scheme_id, project_root=PROJECT_ROOT)
    return {
        "rows": len(rows),
        "labeled_rows": len(labeled),
        "correct": len(correct),
        "accuracy": (len(correct) / len(labeled)) if labeled else None,
        "target_rule": MONTHLY_TARGET_RULE,
        "data_source": DATA_SOURCE,
        "source_original_data_source": SOURCE_ORIGINAL_DATA_SOURCE,
        "benchmark_provenance": {
            "source_role": PLATFORM_CURRENT_MONTHLY_ROLE,
            "generator": f"backtests.monthly_0629_reproduction.run_monthly_0629_reproduction:{scheme_id}",
            "bootstrap_source": PLATFORM_CURRENT_MONTHLY_ROLE,
            "target_rule": MONTHLY_TARGET_RULE,
            "data_source": DATA_SOURCE,
            "source_original_data_source": SOURCE_ORIGINAL_DATA_SOURCE,
            "source_family": "monthly_0629_binary_runner",
            "source_package_hash": evidence.source_package_hash,
            "source_package_path": str(evidence.source_package_path.relative_to(PROJECT_ROOT)),
            "source_frequency": evidence.frequency,
            "source_target_tenor": evidence.target_tenor,
            "source_final_select_id": evidence.final_select_id,
            "source_model_id": evidence.model_id,
            "source_candidate_id": evidence.candidate_id,
            "source_runner_module": evidence.runner_module,
            "manifest_path": str(evidence.manifest_path.relative_to(PROJECT_ROOT)),
            "cross_frequency_reuse": False,
        },
    }


def main_for_scheme(scheme_id: str, argv: list[str] | None = None) -> dict[str, Any]:
    import argparse

    parser = argparse.ArgumentParser(description=f"Run {scheme_id} monthly 0629 reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    parser.add_argument("--predict-date", action="append", help="Limit to one monthly predict_date")
    args = parser.parse_args(argv)
    payload = run_monthly_0629_reproduction(
        scheme_id,
        predict_dates=args.predict_date,
        persist=not args.no_persist,
    )
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload
