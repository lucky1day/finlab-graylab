from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import infer_target_date, make_run_output, persist_run_output
from backtests.repository import clean_json
from shared.artifact_paths import benchmark_input_root
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import (
    build_daily_input_artifact,
    build_monthly_input_artifact,
    build_weekly_input_artifact,
)
from schemes.daily_5y_2_v28.core.v28_common import MODEL_VERSION, model_config, run_prediction


SCHEME_ID = "daily_5y_2_v28"
BENCHMARK_ID = "v28_daily_5y_2"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "5Y"
HORIZON = 5
BACKTEST_INPUT_START = "2010-07-27"
BACKTEST_INPUT_END = "2026-05-29"
BACKTEST_START = "2025-01-01"
BACKTEST_END = "2026-05-29"
BACKTEST_PREDICT_START_DATE = "2025-01-01"
LIVE_TARGET_CUTOFF = "2026-06-01"
DEFAULT_N_WORKERS = 10


def run_historical_prediction(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None = None,
    n_workers: int = DEFAULT_N_WORKERS,
) -> pd.DataFrame:
    """运行历史连续预测，返回逐 anchor 明细。"""
    detail = run_prediction(
        model_config(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            test_start=BACKTEST_START,
            test_end=BACKTEST_END,
            require_labels=True,
            emit_report=False,
            return_details=True,
            n_workers=n_workers,
        )
    )
    if not isinstance(detail, pd.DataFrame):
        raise RuntimeError("daily_5y_2_v28 historical prediction did not return a detail frame")
    return detail


def build_backtest_rows(
    detail: pd.DataFrame,
    *,
    target_date_for_anchor: Callable[[str], str | None],
    daily_artifact,
    weekly_artifact,
    monthly_artifact,
) -> list[dict[str, Any]]:
    """把算法明细转换为 backtests 标准逐样本行。"""
    rows: list[dict[str, Any]] = []
    for record in detail.to_dict("records"):
        anchor_date = str(record["anchor_date"])
        if anchor_date < BACKTEST_PREDICT_START_DATE:
            continue
        target_date = target_date_for_anchor(anchor_date)
        if target_date is None or target_date >= LIVE_TARGET_CUTOFF:
            continue
        prediction = _int_or_none(record.get("prediction"))
        true_label = _int_or_none(record.get("true_label"))
        row = {
            "benchmark_id": BENCHMARK_ID,
            "scheme_id": SCHEME_ID,
            "target_tenor": TARGET_TENOR,
            "horizon": HORIZON,
            "predict_date": anchor_date,
            "feature_date": anchor_date,
            "target_date": target_date,
            "predicted_direction": prediction,
            "label": true_label,
            "confidence": _float_or_default(record.get("confidence"), abs(prediction or 0)),
            "model_version": MODEL_VERSION,
            "data_source": DATA_SOURCE,
            "extra": _row_extra(record, daily_artifact, weekly_artifact, monthly_artifact),
        }
        rows.append(row)
    return rows


def run_daily_5y_2_v28_reproduction(
    *,
    persist: bool = True,
    n_workers: int = DEFAULT_N_WORKERS,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """执行 daily_5y_2_v28 DB-aligned 历史复现。"""
    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        output_root = benchmark_input_root(BENCHMARK_ID)
        daily_artifact = build_daily_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            start_date=BACKTEST_INPUT_START,
            end_date=BACKTEST_INPUT_END,
            engine=engine,
            output_root=output_root,
        )
        calendar = get_calendar(engine)
        weekly_end_week = calendar.week_id_for_date(BACKTEST_INPUT_END)
        if weekly_end_week is None:
            raise RuntimeError(f"无法从 DB 日历解析 backtest end week: {BACKTEST_INPUT_END}")
        weekly_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            end_week=int(weekly_end_week),
            as_of_date=BACKTEST_INPUT_END,
            engine=engine,
            output_root=output_root,
        )
        monthly_artifact = build_monthly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            start_date=BACKTEST_INPUT_START,
            end_date=BACKTEST_INPUT_END,
            engine=engine,
            output_root=output_root,
        )
        date_to_week = _date_to_week_map(daily_artifact.dataframe, calendar)
        detail = run_historical_prediction(
            daily_df=daily_artifact.dataframe,
            weekly_df=weekly_artifact.dataframe,
            monthly_df=monthly_artifact.dataframe,
            date_to_week=date_to_week,
            n_workers=n_workers,
        )
        rows = build_backtest_rows(
            detail,
            target_date_for_anchor=lambda anchor: _target_date_from_daily(daily_artifact.dataframe, anchor),
            daily_artifact=daily_artifact,
            weekly_artifact=weekly_artifact,
            monthly_artifact=monthly_artifact,
        )
        if not rows:
            raise RuntimeError("daily_5y_2_v28 reproduction produced no rows")
        output = make_run_output(
            scheme_id=SCHEME_ID,
            data_source=DATA_SOURCE,
            start_date=min(str(row["predict_date"]) for row in rows),
            end_date=max(str(row["predict_date"]) for row in rows),
            rows=rows,
            benchmark_id=BENCHMARK_ID,
        )
        output.summary.update(
            {
                "daily_input_artifact_path": str(daily_artifact.path),
                "weekly_input_artifact_path": str(weekly_artifact.path),
                "monthly_input_artifact_path": str(monthly_artifact.path),
                "input_artifact_hash": getattr(daily_artifact, "content_hash", None),
                "weekly_input_artifact_hash": getattr(weekly_artifact, "content_hash", None),
                "monthly_input_artifact_hash": getattr(monthly_artifact, "content_hash", None),
                "weekly_input_end_week": int(weekly_end_week),
                "weekly_input_as_of_date": BACKTEST_INPUT_END,
            }
        )
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


def compact_prediction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "predict_date": row["predict_date"],
            "feature_date": row["feature_date"],
            "target_date": row["target_date"],
            "tenor": row["target_tenor"],
            "direction": row["predicted_direction"],
            "label": row["label"],
            "confidence": row["confidence"],
        }
        for row in rows
    ]


def _target_date_from_daily(daily_df: pd.DataFrame, anchor_date: str) -> str | None:
    return infer_target_date(daily_df, anchor_date, HORIZON)


def _date_to_week_map(daily_df: pd.DataFrame, calendar) -> dict[str, int | str]:
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result: dict[str, int | str] = {}
    for day in dates.dropna().unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is not None:
            result[str(day)] = int(week_id)
    return result


def _row_extra(record: dict[str, Any], daily_artifact, weekly_artifact, monthly_artifact) -> dict[str, Any]:
    return {
        "feature_date": record.get("anchor_date"),
        "anchor_date": record.get("anchor_date"),
        "vote_score": record.get("vote_score"),
        "ens_prob": record.get("ens_prob"),
        "model_scope": "continuous_from_2024_07",
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


def _int_or_none(value: Any) -> int | None:
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if value is None:
        return None
    return int(value)


def _float_or_default(value: Any, default: float) -> float:
    try:
        if pd.isna(value):
            return float(default)
    except TypeError:
        pass
    if value is None:
        return float(default)
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--n-workers", type=int, default=DEFAULT_N_WORKERS)
    args = parser.parse_args()
    payload = run_daily_5y_2_v28_reproduction(
        persist=not args.no_persist,
        n_workers=args.n_workers,
    )
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
