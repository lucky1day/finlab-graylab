from __future__ import annotations

import argparse
import ast
import json
import math
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import RunOutput, make_run_output, persist_run_output
from backtests.repository import clean_json
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import build_weekly_input_artifact
from schemes.weekly_10y_d_overlay_0529.core.d_overlay import build_d_overlay


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "schemes" / "weekly_10y_d_overlay_0529" / "config.yaml"

SCHEME_ID = "weekly_10y_d_overlay_0529"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "10Y"
TARGET_COL = "TB0YWI3C"
HORIZON_DAYS = 6
TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"
MODEL_VERSION = "10y_d_overlay_0529"
REQUIRED_COLUMNS = ["week_id", TARGET_COL, "TB1YWI3C", "TB5YWI3C"]
LIVE_TARGET_START_DATE = "2026-06-01"


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
) -> list[dict[str, Any]]:
    """将 10Y D-overlay 全历史预测转换为统一回测行。"""
    weekly = _attach_db_week_dates(_normalize_weekly_frame(weekly_df), calendar)
    if weekly.empty:
        return []

    try:
        prediction_df = build_d_overlay(weekly)
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError("weekly_10y_d_overlay_0529 reproduction produced no predictions") from exc
    if prediction_df.empty:
        return []

    prediction_df = prediction_df.copy()
    prediction_df["week_id"] = pd.to_numeric(prediction_df["week_id"], errors="coerce").astype("Int64")
    prediction_df = prediction_df.dropna(subset=["week_id"]).copy()
    prediction_df["week_id"] = prediction_df["week_id"].astype(int)
    prediction_by_week = {
        int(row["week_id"]): row
        for _, row in prediction_df.sort_values("week_id").drop_duplicates("week_id", keep="last").iterrows()
    }

    rows: list[dict[str, Any]] = []
    weekly_by_id = {int(row["week_id"]): row for _, row in weekly.iterrows()}

    for feature_week_id, prediction_row in sorted(prediction_by_week.items()):
        if feature_week_id not in weekly_by_id:
            continue

        try:
            target_week_id = _next_calendar_week_id(calendar, feature_week_id)
        except ValueError:
            continue
        target_row = weekly_by_id.get(target_week_id)
        if target_row is None:
            continue
        feature_row = weekly_by_id[feature_week_id]
        feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
        target_date = calendar.week_id_to_last_trading_day(target_week_id)
        if target_date >= LIVE_TARGET_START_DATE:
            continue
        future_return = _weekly_future_return(feature_row, target_row)
        label = _label_from_future_return(future_return) if future_return is not None else None
        predicted_direction = _int_or_none(prediction_row.get("d_pred_label"))
        confidence = _float_or_none(prediction_row.get("d_prob_up"))
        source_row = clean_json({**feature_row.to_dict(), **prediction_row.to_dict()})
        predict_date = _predict_date_for_feature_date(feature_date)
        if predict_date < BACKTEST_PREDICT_START_DATE:
            continue

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
                "predicted_direction": predicted_direction,
                "model_pred": predicted_direction,
                "confidence": confidence,
                "source_row": source_row,
                "extra": {
                    "source": MODEL_VERSION,
                    "frequency": "weekly",
                    "model_version": MODEL_VERSION,
                    "target_rule": TARGET_RULE,
                    "feature_week_id": feature_week_id,
                    "target_week_id": target_week_id,
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "future_return": future_return,
                    "model_disagree": _bool_or_none(prediction_row.get("d_model_disagree")),
                    "overlay_applied": _bool_or_none(prediction_row.get("d_model2_overlay")),
                    "score_pred_label": _int_or_none(prediction_row.get("score_pred_label")),
                    "score_prob_up": _float_or_none(prediction_row.get("score_prob_up")),
                    "model2_prob_up": _float_or_none(prediction_row.get("model2_prob_up")),
                    "d_model2_pred_label": _int_or_none(prediction_row.get("d_model2_pred_label")),
                    "input_artifact_path": str(artifact_path),
                    "input_artifact_source": artifact_source,
                },
            }
        )

    return rows


def _next_calendar_week_id(calendar: Any, feature_week_id: int) -> int:
    """从 DB 日历读取 feature_week_id 后的下一实际 week_id。"""
    feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
    for day in calendar.next_trading_days(feature_date, 15):
        next_week = calendar.week_id_for_date(day)
        if next_week is not None and int(next_week) != int(feature_week_id):
            return int(next_week)
    raise ValueError(f"无法在 DB 日历中找到 week_id={feature_week_id} 的下一周")


def compact_prediction_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 与人工验收用紧凑预测序列。"""
    compact: list[dict[str, Any]] = []
    for row in rows:
        direction = _int_or_none(row.get("predicted_direction"))
        compact.append(
            {
                "predict_date": str(row["predict_date"]),
                "target_date": str(row["target_date"]),
                "tenor": str(row["target_tenor"]),
                "target_tenor": str(row["target_tenor"]),
                "direction": direction,
                "predicted_direction": direction,
                "confidence": _float_or_none(row.get("confidence")),
            }
        )
    return compact


def run_weekly_10y_d_overlay_0529_reproduction(
    engine: Engine | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行 weekly_10y_d_overlay_0529 DB-aligned 历史复现。"""
    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            schema_columns=None,
            start_week=BACKTEST_START_WEEK,
            end_week=BACKTEST_END_WEEK,
            engine=engine,
        )
        weekly_df = artifact.dataframe
        calendar = get_calendar(engine)
        full_rows = build_backtest_rows(
            weekly_df,
            calendar=calendar,
            artifact_path=Path(artifact.path),
            artifact_source=str(artifact.source),
        )
        if not full_rows:
            raise RuntimeError("weekly_10y_d_overlay_0529 reproduction produced no rows")

        start_date = min(str(row["predict_date"]) for row in full_rows)
        end_date = max(str(row["predict_date"]) for row in full_rows)
        output = make_weekly_run_output(start_date, end_date, full_rows)
        _annotate_summary(output, artifact, weekly_df)

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


def _annotate_summary(output: RunOutput, artifact: Any, weekly_df: pd.DataFrame) -> None:
    summary = output.summary
    summary["source"] = MODEL_VERSION
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


def _normalize_weekly_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"weekly input missing required columns: {missing}")
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    df = df[(df["week_id"] >= BACKTEST_START_WEEK) & (df["week_id"] <= BACKTEST_END_WEEK)].copy()
    for col in df.columns:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").drop_duplicates("week_id", keep="last").reset_index(drop=True)


def _attach_db_week_dates(weekly_df: pd.DataFrame, calendar: Any) -> pd.DataFrame:
    df = weekly_df.copy()
    week_ids = pd.to_numeric(df["week_id"], errors="coerce").dropna().astype(int).unique()
    week_dates = {int(wid): calendar.week_id_to_last_trading_day(int(wid)) for wid in week_ids}
    model_dates = {int(wid): _legacy_segment_anchor_date(int(wid)) for wid in week_ids}
    df["week_date"] = df["week_id"].astype(int).map(week_dates)
    df["model_date"] = df["week_id"].astype(int).map(model_dates)
    return df


def _legacy_segment_anchor_date(week_id: int) -> str:
    """保留原始算法的模型分段锚点；不用于 target/display 日期。"""
    text = str(int(week_id))
    year = int(text[:4])
    ordinal = int(text[4:])
    jan1 = pd.Timestamp(f"{year}-01-01")
    first_thu = jan1 + pd.Timedelta(days=(3 - jan1.weekday()) % 7)
    first_anchor = first_thu - pd.Timedelta(days=3)
    return (first_anchor + pd.Timedelta(weeks=ordinal - 1)).strftime("%Y-%m-%d")


def _weekly_future_return(feature_row: pd.Series, target_row: pd.Series) -> float | None:
    current_close = _float_or_none(feature_row.get(TARGET_COL))
    target_close = _float_or_none(target_row.get(TARGET_COL))
    if current_close is None or target_close is None or current_close == 0:
        return None
    return (target_close - current_close) / current_close


def _label_from_future_return(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _predict_date_for_feature_date(feature_date: str) -> str:
    return (date.fromisoformat(feature_date) + timedelta(days=1)).isoformat()


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
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return bool(value)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run weekly_10y_d_overlay_0529 historical reproduction.")
    parser.add_argument("--no-persist", action="store_true", help="只输出 JSON，不写 t_backtest_*")
    args = parser.parse_args(argv)
    payload = run_weekly_10y_d_overlay_0529_reproduction(persist=not args.no_persist)
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
