from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from backtests.actuals import build_actual_records
from backtests.repository import (
    clean_json,
    create_engine_from_env,
    create_backtest_run,
    replace_backtest_predictions,
    update_backtest_run_summary,
)
from shared.daily_0629_predict_adapter import (
    DAILY_0629_INTERNAL_FIELDS,
    HORIZON_DAYS,
    _clean_internal_field,
    _direction_from_source,
)
from shared.daily_0629_source_evidence import (
    PLATFORM_CURRENT_DAILY_0629_ROLE,
    require_daily_0629_source_evidence,
)
from shared.daily_0629_source_runner import run_source_daily_backtest_panel


BENCHMARK_ID = "daily_0629"
DATA_SOURCE = "framework_db_aligned"
SOURCE_ORIGINAL_DATA_SOURCE = "source_original_daily_0629_binary_runner"
DEFAULT_START_DATE = "2025-01-01"
DEFAULT_GRAY_START_DATE = "2026-06-01"
DEFAULT_SOURCE_RUN_DATE = "2026-06-10"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_daily_0629_reproduction(
    scheme_id: str,
    *,
    source_run_date: str = DEFAULT_SOURCE_RUN_DATE,
    start_date: str = DEFAULT_START_DATE,
    gray_start_date: str = DEFAULT_GRAY_START_DATE,
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行日度 0629 source-backed T+1 历史复现。"""
    started = time.time()
    owned_engine = engine is None
    db_engine = engine or create_engine_from_env()
    try:
        rows = build_daily_0629_rows(
            scheme_id,
            source_run_date=source_run_date,
            start_date=start_date,
            gray_start_date=gray_start_date,
            engine=db_engine,
            benchmark_role=PLATFORM_CURRENT_DAILY_0629_ROLE,
            data_source=DATA_SOURCE,
        )
        summary = _summary(
            scheme_id,
            rows,
            source_role=PLATFORM_CURRENT_DAILY_0629_ROLE,
            data_source=DATA_SOURCE,
            source_run_date=source_run_date,
        )
        payload = {
            "status": "success",
            "scheme_id": scheme_id,
            "benchmark_id": BENCHMARK_ID,
            "data_source": DATA_SOURCE,
            "row_count": len(rows),
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
                start_date=min(row["predict_date"] for row in rows) if rows else start_date,
                end_date=max(row["target_date"] for row in rows) if rows else gray_start_date,
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


def build_daily_0629_rows(
    scheme_id: str,
    *,
    source_run_date: str,
    start_date: str,
    gray_start_date: str,
    engine: Engine,
    benchmark_role: str,
    data_source: str,
) -> list[dict[str, Any]]:
    """从 source backtest 面板构建平台 backtest rows。"""
    evidence = require_daily_0629_source_evidence(scheme_id, project_root=PROJECT_ROOT)
    source_rows = run_source_daily_backtest_panel(evidence, source_run_date=source_run_date)
    if not source_rows:
        raise RuntimeError(f"{scheme_id}: daily 0629 source backtest produced no rows")
    raw_rows: list[dict[str, Any]] = []
    ordered_sources = sorted(source_rows, key=lambda row: str(row.get("date") or ""))
    for idx, source in enumerate(ordered_sources):
        feature_date = str(source.get("date") or "")[:10]
        if not feature_date:
            continue
        next_source = ordered_sources[idx + 1] if idx + 1 < len(ordered_sources) else None
        target_date = str((next_source or {}).get("date") or "")[:10]
        if not target_date:
            continue
        if feature_date < start_date or target_date >= gray_start_date:
            continue
        raw_rows.append(
            _row_from_source(
                scheme_id,
                evidence,
                source,
                feature_date=feature_date,
                target_date=target_date,
                benchmark_role=benchmark_role,
                data_source=data_source,
            )
        )
    actuals = _actual_lookup(engine, raw_rows, evidence.target_tenor)
    rows = [_attach_label(row, actuals) for row in raw_rows]
    if not rows:
        raise RuntimeError(f"{scheme_id}: no daily 0629 rows after target_date filters")
    return clean_json(rows)


def _row_from_source(
    scheme_id: str,
    evidence: Any,
    source: dict[str, Any],
    *,
    feature_date: str,
    target_date: str,
    benchmark_role: str,
    data_source: str,
) -> dict[str, Any]:
    direction = _direction_from_source(source)
    extra = {
        "data_source": data_source,
        "source_original_data_source": SOURCE_ORIGINAL_DATA_SOURCE,
        "benchmark_role": benchmark_role,
        "source_role": evidence.source_role,
        "source_package_hash": evidence.source_package_hash,
        "source_output_date": source.get("source_output_date"),
        "source_run_date": source.get("source_output_date"),
        "source_feature_date": feature_date,
        "source_model_dir": source.get("source_model_dir"),
        "input_cutoff_date": feature_date,
    }
    for field in DAILY_0629_INTERNAL_FIELDS:
        extra[field] = _clean_internal_field(field, source.get(field))
    extra["frequency"] = extra.get("frequency") or evidence.frequency
    extra["final_select_id"] = extra.get("final_select_id") or evidence.final_select_id
    extra["candidate_id"] = extra.get("candidate_id") or evidence.candidate_id
    extra["target_col"] = extra.get("target_col") or evidence.target_col
    row = {
        "benchmark_id": BENCHMARK_ID,
        "scheme_id": scheme_id,
        "target_tenor": evidence.target_tenor,
        "horizon": HORIZON_DAYS,
        "predict_date": feature_date,
        "feature_date": feature_date,
        "target_date": target_date,
        "label": None,
        "predicted_direction": direction,
        "model_pred": extra.get("model_pred") if extra.get("model_pred") is not None else direction,
        "direction": direction,
        "is_correct": None,
        "source_row": extra,
        "extra": extra,
        "benchmark_role": benchmark_role,
    }
    for field in DAILY_0629_INTERNAL_FIELDS:
        row[field] = extra.get(field)
    source_label = _int_or_none(source.get("true_label"))
    row["source_true_label"] = source_label
    return row


def _actual_lookup(engine: Engine, rows: list[dict[str, Any]], target_tenor: str) -> dict[tuple[str, str], int]:
    if not rows:
        return {}
    end_date = max(str(row["target_date"]) for row in rows)
    start_date = min(str(row["target_date"]) for row in rows)
    actuals = build_actual_records(engine, start_date=start_date, end_date=end_date, tenors=[target_tenor])
    return {
        (record.tenor, record.trade_date): int(record.direction_1d)
        for record in actuals
        if record.direction_1d is not None
    }


def _attach_label(row: dict[str, Any], actuals: dict[tuple[str, str], int]) -> dict[str, Any]:
    key = (str(row["target_tenor"]), str(row["target_date"]))
    if key not in actuals:
        raise RuntimeError(f"daily 0629 actual label missing for key={key}")
    label = int(actuals[key])
    source_label = row.pop("source_true_label", None)
    if source_label is not None and int(source_label) != label:
        raise RuntimeError(
            f"daily 0629 source label mismatch for key={key}: source={source_label}, db_actual={label}"
        )
    row["label"] = label
    row["is_correct"] = int(row["predicted_direction"]) == label
    return row


def _summary(
    scheme_id: str,
    rows: list[dict[str, Any]],
    *,
    source_role: str,
    data_source: str,
    source_run_date: str,
) -> dict[str, Any]:
    labeled = [row for row in rows if row.get("label") is not None]
    correct = [row for row in labeled if row.get("is_correct") is True]
    evidence = require_daily_0629_source_evidence(scheme_id, project_root=PROJECT_ROOT)
    return {
        "rows": len(rows),
        "row_count": len(rows),
        "labeled_rows": len(labeled),
        "correct": len(correct),
        "accuracy": (len(correct) / len(labeled)) if labeled else None,
        "horizon": HORIZON_DAYS,
        "data_source": data_source,
        "source_original_data_source": SOURCE_ORIGINAL_DATA_SOURCE,
        "source_run_date": source_run_date,
        "benchmark_provenance": {
            "source_role": source_role,
            "generator": f"backtests.daily_0629_reproduction.run_daily_0629_reproduction:{scheme_id}",
            "bootstrap_source": source_role,
            "data_source": data_source,
            "source_original_data_source": SOURCE_ORIGINAL_DATA_SOURCE,
            "source_family": "daily_0629_binary_runner",
            "source_package_hash": evidence.source_package_hash,
            "source_package_path": str(evidence.source_package_path.relative_to(PROJECT_ROOT)),
            "source_frequency": evidence.frequency,
            "source_target_tenor": evidence.target_tenor,
            "source_final_select_id": evidence.final_select_id,
            "source_candidate_id": evidence.candidate_id,
            "source_target_col": evidence.target_col,
            "source_runner_module": evidence.runner_module,
            "source_live_runner_module": evidence.live_runner_module,
            "manifest_path": str(evidence.manifest_path.relative_to(PROJECT_ROOT)),
            "cross_frequency_reuse": False,
        },
    }


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def main_for_scheme(scheme_id: str, argv: list[str] | None = None) -> dict[str, Any]:
    import argparse

    parser = argparse.ArgumentParser(description=f"Run {scheme_id} daily 0629 reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    parser.add_argument("--source-run-date", default=DEFAULT_SOURCE_RUN_DATE)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--gray-start-date", default=DEFAULT_GRAY_START_DATE)
    args = parser.parse_args(argv)
    payload = run_daily_0629_reproduction(
        scheme_id,
        source_run_date=args.source_run_date,
        start_date=args.start_date,
        gray_start_date=args.gray_start_date,
        persist=not args.no_persist,
    )
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload
