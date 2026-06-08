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
from schemes.weekly_10y_d_overlay.core.predictors import date_to_week_id, next_week_id, predict_w10y, week_id_to_friday


SCHEME_ID = "weekly_10y_d_overlay"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "10Y"
HORIZON_DAYS = 6
SOURCE_NAME = "10y_d_overlay_0529"
TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"


def prediction_frame_to_backtest_rows(predictions: pd.DataFrame) -> list[dict[str, Any]]:
    """把原始周度预测结果转换为统一 backtest prediction 行。"""
    rows: list[dict[str, Any]] = []
    if predictions.empty:
        return rows

    frame = predictions.sort_values("week_id").reset_index(drop=True)
    source_items: list[tuple[pd.Series, int, pd.Timestamp, bool]] = []
    for _, raw in frame.iterrows():
        week_id = int(raw["week_id"])
        feature_timestamp, from_source_date = _feature_timestamp(raw, week_id)
        source_items.append((raw, week_id, feature_timestamp, from_source_date))
    if any(item[3] for item in source_items):
        source_items = sorted(source_items, key=lambda item: (item[2], item[1]))

    for index, (raw, week_id, feature_timestamp, from_source_date) in enumerate(source_items):
        feature_date = feature_timestamp.strftime("%Y-%m-%d")
        predict_date = (feature_timestamp + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        next_item = source_items[index + 1] if index + 1 < len(source_items) else None
        if from_source_date and next_item is not None:
            target_week_id = next_item[1]
            target_timestamp = next_item[2]
        else:
            target_week_id = next_week_id(week_id)
            target_timestamp = feature_timestamp + pd.Timedelta(days=7) if from_source_date else week_id_to_friday(target_week_id)
        target_date = target_timestamp.strftime("%Y-%m-%d")
        future_return = _float_or_none(raw.get("future_return"))
        label = _int_or_none(raw.get("actual_label"))
        if next_item is not None:
            calculated_future_return = _weekly_future_return(raw, next_item[0])
            if calculated_future_return is not None:
                future_return = calculated_future_return
                label = _label_from_future_return(calculated_future_return)
        source_row = clean_json(raw.to_dict())
        rows.append(
            {
                "benchmark_id": BENCHMARK_ID,
                "scheme_id": SCHEME_ID,
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON_DAYS,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": target_date,
                "label": label,
                "predicted_direction": _int_or_none(raw.get("d_pred_label")),
                "model_pred": _int_or_none(raw.get("score_pred_label")),
                "confidence": _float_or_none(raw.get("d_prob_up")),
                "source_row": source_row,
                "extra": {
                    "frequency": "weekly",
                    "source": SOURCE_NAME,
                    "target_rule": TARGET_RULE,
                    "feature_week_id": week_id,
                    "target_week_id": target_week_id,
                    "future_return": future_return,
                    "score_pred_label": _int_or_none(raw.get("score_pred_label")),
                    "model2_pred_label": _int_or_none(raw.get("d_model2_pred_label")),
                    "model_disagree": bool(raw.get("d_model_disagree")) if pd.notna(raw.get("d_model_disagree")) else None,
                    "overlay_applied": bool(raw.get("d_overlay")) if pd.notna(raw.get("d_overlay")) else None,
                },
            }
        )
    return _deduplicate_prediction_date_rows(rows)


def _feature_timestamp(raw: pd.Series, week_id: int) -> tuple[pd.Timestamp, bool]:
    for column in ("month_date", "week_date"):
        if column not in raw:
            continue
        parsed = pd.to_datetime(raw.get(column), errors="coerce")
        if pd.notna(parsed):
            return pd.Timestamp(parsed).normalize(), True
    return pd.Timestamp(week_id_to_friday(week_id)).normalize(), False


def _weekly_future_return(raw: pd.Series, target_raw: pd.Series) -> float | None:
    current_close = _float_or_none(raw.get("TB0YWI3C"))
    target_close = _float_or_none(target_raw.get("TB0YWI3C"))
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
        if existing is None or _prefer_weekly_row(row, existing):
            deduped[key] = row
    return list(deduped.values())


def _prefer_weekly_row(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_canonical = _is_canonical_weekly_row(candidate)
    existing_canonical = _is_canonical_weekly_row(existing)
    if candidate_canonical != existing_canonical:
        return candidate_canonical
    return int(candidate["extra"]["feature_week_id"]) > int(existing["extra"]["feature_week_id"])


def _is_canonical_weekly_row(row: dict[str, Any]) -> bool:
    feature_week_id = int(row["extra"]["feature_week_id"])
    return feature_week_id == date_to_week_id(str(row["predict_date"]))


def run_weekly_10y_d_overlay_reproduction(engine=None, persist: bool = True) -> dict[str, Any]:
    """用 DB 周频源表复现 weekly_10y_d_overlay，并可写入回测表。"""
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
        result = predict_w10y(weekly_df, rdate="historical_backtest")
        rows = prediction_frame_to_backtest_rows(result.predictions)
        if not rows:
            raise RuntimeError("weekly 10Y reproduction produced no rows")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run weekly 10Y D-overlay DB-aligned reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只运行算法，不写入 t_backtest_*")
    args = parser.parse_args()
    payload = run_weekly_10y_d_overlay_reproduction(persist=not args.no_persist)
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
