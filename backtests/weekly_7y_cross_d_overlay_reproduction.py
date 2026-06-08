from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

import pandas as pd

from backtests.repository import clean_json
from backtests.daily_0529_reproduction import BENCHMARK_ID, RunOutput, make_run_output, persist_run_output
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import build_weekly_input_artifact
from shared.weekly_calendar import next_week_id, week_id_to_friday
from schemes.weekly_7y_cross_d_overlay.core.predictors import (
    SOURCE_NAME,
    TARGET_COL,
    TARGET_RULE,
    build_cross_d_predictions,
    normalize_weekly_frame,
)


SCHEME_ID = "weekly_7y_cross_d_overlay"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "7Y"
HORIZON_DAYS = 6
BACKTEST_START_DATE = "2025-07-01"
BACKTEST_END_DATE = "2026-05-31"


def build_weekly_7y_predictions(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """用 DB 周频宽表复现 weekly_7y_cross_d_overlay 原始 cross-D overlay。"""
    weekly = normalize_weekly_frame(weekly_df)
    predictions = build_cross_d_predictions(weekly)
    start = pd.Timestamp(BACKTEST_START_DATE)
    end = pd.Timestamp(BACKTEST_END_DATE)
    return predictions[
        (pd.to_datetime(predictions["month_date"], errors="coerce") >= start)
        & (pd.to_datetime(predictions["month_date"], errors="coerce") <= end)
    ].copy()


def prediction_frame_to_backtest_rows(predictions: pd.DataFrame) -> list[dict[str, Any]]:
    """把 7Y 周度预测结果转换为统一 backtest prediction 行。"""
    rows: list[dict[str, Any]] = []
    if predictions.empty:
        return rows

    frame = predictions.sort_values("week_id").reset_index(drop=True)
    source_items: list[tuple[pd.Series, int, pd.Timestamp]] = []
    for _, raw in frame.iterrows():
        week_id = int(raw["week_id"])
        source_items.append((raw, week_id, _feature_timestamp(raw, week_id)))

    source_items = sorted(source_items, key=lambda item: (item[2], item[1]))
    for index, (raw, week_id, feature_timestamp) in enumerate(source_items):
        label = _int_or_none(raw.get("actual_label"))
        if label is None:
            continue

        feature_date = feature_timestamp.strftime("%Y-%m-%d")
        predict_date = (feature_timestamp + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        next_item = source_items[index + 1] if index + 1 < len(source_items) else None
        if next_item is not None:
            target_week_id = next_item[1]
            target_timestamp = next_item[2]
            calculated_future_return = _weekly_future_return(raw, next_item[0])
        else:
            target_week_id = next_week_id(week_id)
            target_timestamp = feature_timestamp + pd.Timedelta(days=7)
            calculated_future_return = None
        future_return = calculated_future_return if calculated_future_return is not None else _float_or_none(raw.get("future_return"))
        if future_return is not None:
            label = _label_from_future_return(future_return)

        source_row = clean_json(raw.to_dict())
        rows.append(
            {
                "benchmark_id": BENCHMARK_ID,
                "scheme_id": SCHEME_ID,
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON_DAYS,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": target_timestamp.strftime("%Y-%m-%d"),
                "label": label,
                "predicted_direction": _int_or_none(raw.get("cross_d_pred_label")),
                "model_pred": _int_or_none(raw.get("main_pred_label")),
                "confidence": _float_or_none(raw.get("cross_d_prob_up")),
                "source_row": source_row,
                "extra": {
                    "frequency": "weekly",
                    "source": SOURCE_NAME,
                    "target_rule": TARGET_RULE,
                    "feature_week_id": week_id,
                    "target_week_id": target_week_id,
                    "future_return": future_return,
                    "main_pred_label": _int_or_none(raw.get("main_pred_label")),
                    "main_prob_up": _float_or_none(raw.get("main_prob_up")),
                    "cross_d_overlay": _bool_or_none(raw.get("cross_d_overlay")),
                    "cross_d_signal_source": _str_or_none(raw.get("cross_d_signal_source")),
                    "label_overlay_applied": _bool_or_none(raw.get("label_overlay_applied")),
                    "source_spec": _str_or_none(raw.get("source_spec")),
                    "score_spec": _str_or_none(raw.get("score_spec")),
                },
            }
        )
    return _deduplicate_prediction_date_rows(rows)


def run_weekly_7y_cross_d_overlay_reproduction(engine=None, persist: bool = True) -> dict[str, Any]:
    """用 DB 周频源表复现 weekly_7y_cross_d_overlay，并可写入回测表。"""
    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            engine=engine,
        )
        weekly_df = artifact.dataframe
        predictions = build_weekly_7y_predictions(weekly_df)
        rows = prediction_frame_to_backtest_rows(predictions)
        if not rows:
            raise RuntimeError("weekly 7Y reproduction produced no rows")

        start_date = min(row["predict_date"] for row in rows)
        end_date = max(row["predict_date"] for row in rows)
        output = make_weekly_run_output(start_date, end_date, rows)
        output.summary["frequency"] = "weekly"
        output.summary["target_rule"] = TARGET_RULE
        output.summary["source"] = SOURCE_NAME
        output.summary["weekly_input_rows"] = int(len(weekly_df))
        output.summary["weekly_input_week_min"] = int(weekly_df["week_id"].min()) if not weekly_df.empty else None
        output.summary["weekly_input_week_max"] = int(weekly_df["week_id"].max()) if not weekly_df.empty else None
        output.summary["weekly_input_artifact_path"] = str(artifact.path)
        output.summary["weekly_input_artifact_source"] = artifact.source

        run_id = persist_run_output(engine, output) if persist else None
        elapsed = round(time.time() - started, 3)
        return {
            "status": "success",
            "run_id": run_id,
            "benchmark_id": BENCHMARK_ID,
            "scheme_id": SCHEME_ID,
            "data_source": DATA_SOURCE,
            "start_date": start_date,
            "end_date": end_date,
            "row_count": len(output.rows),
            "monthly_count": len(output.monthly_metrics),
            "summary": output.summary,
            "elapsed_sec": elapsed,
        }
    finally:
        if own_engine:
            engine.dispose()


def make_weekly_run_output(start_date: str, end_date: str, rows: list[dict[str, Any]]) -> RunOutput:
    return make_run_output(SCHEME_ID, DATA_SOURCE, start_date, end_date, rows)


def _feature_timestamp(raw: pd.Series, week_id: int) -> pd.Timestamp:
    for column in ("month_date", "week_date"):
        if column not in raw:
            continue
        parsed = pd.to_datetime(raw.get(column), errors="coerce")
        if pd.notna(parsed):
            return pd.Timestamp(parsed).normalize()
    return pd.Timestamp(week_id_to_friday(week_id)).normalize()


def _weekly_future_return(raw: pd.Series, target_raw: pd.Series) -> float | None:
    current_close = _float_or_none(raw.get(TARGET_COL))
    target_close = _float_or_none(target_raw.get(TARGET_COL))
    if current_close is None or target_close is None or current_close == 0:
        return None
    return (target_close - current_close) / current_close


def _label_from_future_return(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _deduplicate_prediction_date_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["target_tenor"]), str(row["predict_date"]))
        existing = deduped.get(key)
        if existing is None or int(row["extra"]["feature_week_id"]) > int(existing["extra"]["feature_week_id"]):
            deduped[key] = row
    return list(deduped.values())


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def _bool_or_none(value: Any) -> bool | None:
    if value is None or pd.isna(value):
        return None
    return bool(value)


def _str_or_none(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run weekly 7Y cross-D-overlay DB-aligned reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只运行算法，不写入 t_backtest_*")
    args = parser.parse_args()
    payload = run_weekly_7y_cross_d_overlay_reproduction(persist=not args.no_persist)
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
