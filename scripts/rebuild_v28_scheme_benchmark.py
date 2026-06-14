from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from schemes.daily_5y_2_v28.inference import run_v28_for_feature_window


SCHEME_ID = "daily_5y_2_v28"
TARGET_TENOR = "5Y"
HORIZON = 5
SOURCE_PREDICTIONS = Path(
    "/Users/macstudio0/Desktop/models-liwei-0606/outputs/"
    "reproduce_eval_0608_may_20260608/predictions/predict_5y_2_v28_may_2026_predictions.csv"
)
ARTIFACT_ROOT = (
    PROJECT_ROOT
    / "backtest_artifacts"
    / "backtests"
    / "v28_daily_5y_2_may_benchmark"
    / "runtime_inputs"
    / SCHEME_ID
)
BENCHMARK_DIR = PROJECT_ROOT / "schemes" / SCHEME_ID / "benchmarks"
STRICT_FIELDS = [
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "direction",
    "confidence",
    "label",
    "is_correct",
]


def rebuild_v28_scheme_benchmark(*, n_workers: int = 10) -> dict[str, Any]:
    """重建 daily_5y_2_v28 严格 benchmark 文件。"""
    source = _read_source_predictions(SOURCE_PREDICTIONS)
    daily_df = pd.read_csv(ARTIFACT_ROOT / "daily_output_may_aux_benchmark.csv")
    weekly_df = pd.read_csv(ARTIFACT_ROOT / "weekly_output_may_aux_benchmark.csv")
    monthly_df = pd.read_csv(ARTIFACT_ROOT / "monthly_output_may_aux_benchmark.csv")

    engine = create_engine_from_env()
    try:
        calendar = get_calendar(engine=engine)
        date_to_week = _date_to_week_map(daily_df, calendar)
        target_dates = _target_dates(source["feature_date"].tolist(), calendar)
    finally:
        engine.dispose()

    original_rows = _source_rows_to_strict(source, target_dates)
    current_rows = _platform_rows_to_strict(
        source=source,
        target_dates=target_dates,
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        date_to_week=date_to_week,
        n_workers=n_workers,
    )
    _assert_rows_match(original_rows, current_rows)

    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    _write_csv(BENCHMARK_DIR / "original_predictions_sample.csv", original_rows)
    _write_csv(BENCHMARK_DIR / "current_predictions_sample.csv", current_rows)
    summary = _summary(original_rows)
    _write_json(BENCHMARK_DIR / "original_backtest_summary.json", summary | {"source_role": "source_original"})
    _write_json(BENCHMARK_DIR / "current_backtest_summary.json", summary | {"source_role": "platform_inference"})
    return {
        "scheme_id": SCHEME_ID,
        "row_count": len(original_rows),
        "feature_date_start": original_rows[0]["feature_date"],
        "feature_date_end": original_rows[-1]["feature_date"],
        "matched": True,
    }


def _read_source_predictions(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    required = {"date", "tenor", "horizon", "true_label", "prediction", "correct"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"source predictions missing columns: {sorted(missing)}")
    df = df.copy()
    df["feature_date"] = pd.to_datetime(df["date"], errors="raise").dt.strftime("%Y-%m-%d")
    return df.sort_values("feature_date").reset_index(drop=True)


def _date_to_week_map(daily_df: pd.DataFrame, calendar) -> dict[str, int | str]:
    dates = pd.to_datetime(daily_df["date"], errors="raise").dt.strftime("%Y-%m-%d")
    result: dict[str, int | str] = {}
    for day in dates.dropna().unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is None:
            raise RuntimeError(f"missing DB week_id for {day}")
        result[str(day)] = int(week_id)
    return result


def _target_dates(feature_dates: list[str], calendar) -> dict[str, str]:
    targets: dict[str, str] = {}
    for feature_date in feature_dates:
        target_date = calendar.nth_trading_day_after(feature_date, HORIZON)
        if not target_date:
            raise RuntimeError(f"missing target_date for {feature_date}")
        targets[feature_date] = str(target_date)
    return targets


def _source_rows_to_strict(source: pd.DataFrame, target_dates: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in source.to_dict("records"):
        feature_date = str(record["feature_date"])
        direction = int(record["prediction"])
        label = int(record["true_label"])
        rows.append(
            {
                "feature_date": feature_date,
                "target_date": target_dates[feature_date],
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON,
                "direction": direction,
                "confidence": float(abs(direction)),
                "label": label,
                "is_correct": str(bool(int(record["correct"]))).lower(),
            }
        )
    return rows


def _platform_rows_to_strict(
    *,
    source: pd.DataFrame,
    target_dates: dict[str, str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str],
    n_workers: int,
) -> list[dict[str, Any]]:
    feature_dates = source["feature_date"].tolist()
    detail = run_v28_for_feature_window(
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        date_to_week=date_to_week,
        window_start="2026-05-01",
        window_end=max(feature_dates),
        require_labels=True,
        n_workers=n_workers,
    )
    by_date = {str(row["anchor_date"]): row for row in detail.to_dict("records")}
    rows: list[dict[str, Any]] = []
    for feature_date in feature_dates:
        if feature_date not in by_date:
            raise RuntimeError(f"platform inference missing feature_date={feature_date}")
        row = by_date[feature_date]
        direction = int(row["prediction"])
        label = int(row["true_label"])
        rows.append(
            {
                "feature_date": feature_date,
                "target_date": target_dates[feature_date],
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON,
                "direction": direction,
                "confidence": float(row.get("confidence", abs(direction))),
                "label": label,
                "is_correct": str(direction == label).lower(),
            }
        )
    return rows


def _assert_rows_match(original_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> None:
    if len(original_rows) != len(current_rows):
        raise AssertionError(f"row count mismatch: original={len(original_rows)} current={len(current_rows)}")
    for original, current in zip(original_rows, current_rows, strict=True):
        if original != current:
            raise AssertionError(
                "V28 platform current does not match source original: "
                f"feature_date={original.get('feature_date')} original={original} current={current}"
            )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    directions = [int(row["direction"]) for row in rows]
    labels = [int(row["label"]) for row in rows]
    trade_mask = [direction != 0 for direction in directions]
    correct_all = sum(direction == label for direction, label in zip(directions, labels, strict=True))
    correct_trade = sum(
        direction == label
        for direction, label, is_trade in zip(directions, labels, trade_mask, strict=True)
        if is_trade
    )
    trade_count = sum(trade_mask)
    return {
        "benchmark_scope": "may2026_auxiliary_source",
        "source": str(SOURCE_PREDICTIONS),
        "data_cutoff": "2026-06-06",
        "test_start": "2026-05-01",
        "test_end": rows[-1]["feature_date"],
        "row_count": len(rows),
        "eval_samples": trade_count,
        "direction_accuracy": correct_trade / trade_count,
        "all_accuracy": correct_all / len(rows),
        "trade_rate": trade_count / len(rows),
        "pred_up": sum(direction == 1 for direction in directions),
        "pred_down": sum(direction == -1 for direction in directions),
        "pred_flat": sum(direction == 0 for direction in directions),
        "true_up": sum(label == 1 for label in labels),
        "true_down": sum(label == -1 for label in labels),
        "true_flat": sum(label == 0 for label in labels),
        "correct_trade": correct_trade,
        "correct_all": correct_all,
        "config": {
            "combo": "pol_tp_bf",
            "K": 15,
            "lgbm_w": 1.3,
            "vt": 0.15,
            "rebal": "monthly",
            "ew": 378,
            "min_acc": 0.35,
            "seeds": [42, 314, 159],
            "ml_mode": "prob",
            "ens_mode": "standard",
        },
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STRICT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild strict daily_5y_2_v28 benchmark files.")
    parser.add_argument("--n-workers", type=int, default=10)
    args = parser.parse_args()
    summary = rebuild_v28_scheme_benchmark(n_workers=args.n_workers)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
