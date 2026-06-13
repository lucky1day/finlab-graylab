from __future__ import annotations

import argparse
import ast
import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import RunOutput, make_run_output, persist_run_output
from backtests.weekly_base_runner import (
    WeeklyBacktestSpec,
    WeeklyPredictionPoint,
    build_weekly_backtest_rows,
)
from backtests.repository import clean_json
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import build_weekly_input_artifact
from shared.prediction_context import WEEKLY_TARGET_RULE
from schemes.weekly_7y_cross_d_overlay_0529.core.cross_d_overlay import build_cross_d_overlay


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "schemes" / "weekly_7y_cross_d_overlay_0529" / "config.yaml"

SCHEME_ID = "weekly_7y_cross_d_overlay_0529"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "7Y"
TARGET_COL = "TB7YWI3C"
HORIZON_DAYS = 6
TARGET_RULE = WEEKLY_TARGET_RULE
MODEL_VERSION = "cross_d_overlay_0529"
SCHEMA_COLUMNS = ["week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]
LIVE_TARGET_START_DATE = "2026-06-01"
BACKTEST_MAX_AS_OF_DATE = "2026-05-29"


def _load_config_raw(config_path: Path) -> dict[str, Any]:
    return _parse_project_yaml_subset(config_path.read_text(encoding="utf-8"))


def _parse_project_yaml_subset(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            current[key] = child
            stack.append((indent, child))
        else:
            current[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str) -> Any:
    try:
        return ast.literal_eval(value)
    except Exception:
        pass
    if value.isdigit():
        return int(value)
    return value


_CONFIG_RAW = _load_config_raw(CONFIG_PATH)
_BACKTEST_CONFIG = _CONFIG_RAW.get("backtest") if isinstance(_CONFIG_RAW.get("backtest"), dict) else {}
BENCHMARK_ID = str(_BACKTEST_CONFIG.get("benchmark_id") or "model_muti_0529")
BACKTEST_PREDICT_START_DATE = str(_BACKTEST_CONFIG.get("predict_start_date") or "2025-01-01")
BACKTEST_START_WEEK = int(_BACKTEST_CONFIG.get("start_week") or 200901)
BACKTEST_END_WEEK = int(_BACKTEST_CONFIG.get("end_week") or 202622)
BACKTEST_DATA_SOURCE = str(_BACKTEST_CONFIG.get("data_source") or DATA_SOURCE)


def build_backtest_rows(
    weekly_df: pd.DataFrame,
    *,
    calendar: Any,
    artifact_path: Path,
    artifact_source: str,
    weekly_frame_for_feature: Callable[[int, str], pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """逐周模拟 7Y Cross-D 实盘预测并转换为统一回测行。"""
    spec = WeeklyBacktestSpec(
        benchmark_id=BENCHMARK_ID,
        scheme_id=SCHEME_ID,
        target_tenor=TARGET_TENOR,
        horizon_days=HORIZON_DAYS,
        target_column=TARGET_COL,
        target_rule=TARGET_RULE,
        model_version=MODEL_VERSION,
        predict_start_date=BACKTEST_PREDICT_START_DATE,
        live_target_start_date=LIVE_TARGET_START_DATE,
    )

    def predict_for_feature(history: pd.DataFrame, feature_week_id: int) -> WeeklyPredictionPoint | None:
        try:
            prediction_df = build_cross_d_overlay(history)
        except RuntimeError:
            return None
        feature_predictions = prediction_df[prediction_df["week_id"].astype(int).eq(feature_week_id)]
        if feature_predictions.empty:
            return None
        prediction_row = feature_predictions.iloc[-1]
        return WeeklyPredictionPoint(
            source_row=prediction_row.to_dict(),
            predicted_direction=_int_or_none(prediction_row.get("cross_d_pred_label")),
            confidence=_float_or_none(prediction_row.get("cross_d_prob_up")),
            extra={
                "cross_d_overlay": bool(prediction_row.get("cross_d_overlay")),
                "cross_d_signal_source": _str_or_none(prediction_row.get("cross_d_signal_source")),
                "main_pred_label": _int_or_none(prediction_row.get("main_pred_label")),
                "main_prob_up": _float_or_none(prediction_row.get("main_prob_up")),
                "d5_d_pred_label": _int_or_none(prediction_row.get("d5_d_pred_label")),
                "d5_d_prob_up": _float_or_none(prediction_row.get("d5_d_prob_up")),
            },
        )

    return build_weekly_backtest_rows(
        weekly_df,
        calendar=calendar,
        spec=spec,
        artifact_path=artifact_path,
        artifact_source=artifact_source,
        normalize_frame=_normalize_weekly_frame,
        predict_for_feature=predict_for_feature,
        weekly_frame_for_feature=weekly_frame_for_feature,
    )


def compact_prediction_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 与人工验收用紧凑预测序列。"""
    compact: list[dict[str, Any]] = []
    for row in rows:
        compact.append(
            {
                "predict_date": str(row["predict_date"]),
                "target_date": str(row["target_date"]),
                "target_tenor": str(row["target_tenor"]),
                "predicted_direction": _int_or_none(row.get("predicted_direction")),
                "confidence": _float_or_none(row.get("confidence")),
            }
        )
    return compact


def run_weekly_7y_cross_d_overlay_0529_reproduction(
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行 weekly_7y_cross_d_overlay_0529 DB-aligned 历史复现。"""
    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            schema_columns=SCHEMA_COLUMNS,
            start_week=BACKTEST_START_WEEK,
            end_week=BACKTEST_END_WEEK,
            as_of_date=BACKTEST_MAX_AS_OF_DATE,
            engine=engine,
        )
        weekly_df = artifact.dataframe
        calendar = get_calendar(engine)
        pit_artifacts: list[Any] = []

        def weekly_frame_for_feature(feature_week_id: int, feature_date: str) -> pd.DataFrame:
            pit_artifact = build_weekly_input_artifact(
                scheme_id=SCHEME_ID,
                predict_date=f"historical_backtest_{feature_week_id}",
                schema_columns=SCHEMA_COLUMNS,
                start_week=BACKTEST_START_WEEK,
                end_week=feature_week_id,
                as_of_date=feature_date,
                engine=engine,
            )
            pit_artifacts.append(pit_artifact)
            return pit_artifact.dataframe

        full_rows = build_backtest_rows(
            weekly_df,
            calendar=calendar,
            artifact_path=Path(artifact.path),
            artifact_source=str(artifact.source),
            weekly_frame_for_feature=weekly_frame_for_feature,
        )
        if not full_rows:
            raise RuntimeError("weekly_7y_cross_d_overlay_0529 reproduction produced no rows")

        start_date = min(str(row["predict_date"]) for row in full_rows)
        end_date = max(str(row["predict_date"]) for row in full_rows)
        output = make_weekly_run_output(start_date, end_date, full_rows)
        _annotate_summary(output, artifact, weekly_df, pit_artifacts=pit_artifacts)

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


def make_weekly_run_output(start_date: str, end_date: str, rows: list[dict[str, Any]]) -> RunOutput:
    return make_run_output(
        SCHEME_ID,
        BACKTEST_DATA_SOURCE,
        start_date,
        end_date,
        rows,
        benchmark_id=BENCHMARK_ID,
    )


def _annotate_summary(output: RunOutput, artifact: Any, weekly_df: pd.DataFrame, *, pit_artifacts: list[Any] | None = None) -> None:
    summary = output.summary
    summary["frequency"] = "weekly"
    summary["target_rule"] = TARGET_RULE
    summary["model_version"] = MODEL_VERSION
    summary["data_version"] = getattr(artifact, "data_version", "shared_data_service_weekly.v1")
    summary["weekly_input_rows"] = int(len(weekly_df))
    summary["weekly_input_week_min"] = _int_or_none(weekly_df["week_id"].min()) if "week_id" in weekly_df else None
    summary["weekly_input_week_max"] = _int_or_none(weekly_df["week_id"].max()) if "week_id" in weekly_df else None
    summary["weekly_input_artifact_path"] = str(artifact.path)
    summary["weekly_input_artifact_source"] = str(artifact.source)
    summary["backtest_predict_start_date"] = BACKTEST_PREDICT_START_DATE
    summary["backtest_start_week"] = BACKTEST_START_WEEK
    summary["backtest_end_week"] = BACKTEST_END_WEEK
    summary["backtest_point_in_time"] = True
    summary["backtest_max_as_of_date"] = BACKTEST_MAX_AS_OF_DATE
    summary["point_in_time_artifact_count"] = len(pit_artifacts or [])


def _normalize_weekly_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    missing = [col for col in SCHEMA_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"weekly input missing required columns: {missing}")
    df = df[SCHEMA_COLUMNS].copy()
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    df = df[(df["week_id"] >= BACKTEST_START_WEEK) & (df["week_id"] <= BACKTEST_END_WEEK)].copy()
    for col in SCHEMA_COLUMNS:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").drop_duplicates("week_id", keep="last").reset_index(drop=True)


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


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return str(value)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run weekly_7y_cross_d_overlay_0529 historical reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    args = parser.parse_args(argv)
    payload = run_weekly_7y_cross_d_overlay_0529_reproduction(persist=not args.no_persist)
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
