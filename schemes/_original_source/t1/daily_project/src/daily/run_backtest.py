from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from .config import DATA_DAILY, OUTPUT_DAILY, TENOR_CONFIGS
from .lgbm_predictor import PredictionResult, make_labels, predict_latest_for_config


@dataclass(frozen=True)
class DailyBacktestOutputs:
    start_date: str
    end_date: str
    predictions_path: Path
    summary_path: Path
    audit_csv_path: Path
    audit_metadata_path: Path
    db_write_skipped: bool


def _date_tag(start_date: str, end_date: str) -> str:
    return f"{start_date.replace('-', '')}_{end_date.replace('-', '')}"


def _trading_dates(daily_df: pd.DataFrame, start_date: str, end_date: str) -> list[str]:
    dates = pd.to_datetime(daily_df["date"])
    mask = dates.between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return dates[mask].dt.strftime("%Y-%m-%d").tolist()


def _write_audit_files(
    daily_df: pd.DataFrame,
    start_date: str,
    end_date: str,
    audit_base_dir: str | Path,
) -> tuple[Path, Path]:
    tag = _date_tag(start_date, end_date)
    audit_dir = Path(audit_base_dir) / tag
    audit_dir.mkdir(parents=True, exist_ok=True)
    csv_path = audit_dir / "daily_model_input.csv"
    metadata_path = audit_dir / "daily_model_input_metadata.json"
    audit_df = daily_df.copy()
    audit_df["date"] = pd.to_datetime(audit_df["date"])
    audit_df = audit_df.sort_values("date").reset_index(drop=True)
    audit_df.to_csv(csv_path, index=False)
    metadata = {
        "start_date": start_date,
        "end_date": end_date,
        "row_count": int(len(audit_df)),
        "column_count": int(len(audit_df.columns)),
        "date_min": audit_df["date"].min().strftime("%Y-%m-%d") if len(audit_df) else None,
        "date_max": audit_df["date"].max().strftime("%Y-%m-%d") if len(audit_df) else None,
        "source": "database_daily_output",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, metadata_path


def _prediction_to_row(daily_df: pd.DataFrame, result: PredictionResult) -> dict:
    config = result.config
    dates = pd.to_datetime(daily_df["date"])
    feature_idx = int(dates[dates.eq(pd.Timestamp(result.feature_date))].index[-1])
    target_matches = dates[dates.eq(pd.Timestamp(result.target_date))]
    close = pd.to_numeric(daily_df[config.close_col], errors="coerce")
    if len(target_matches):
        target_idx = int(target_matches.index[-1])
        future_return = close.iloc[target_idx] / close.iloc[feature_idx] - 1.0
        if future_return > config.threshold:
            label = 1.0
        elif future_return < -config.threshold:
            label = -1.0
        else:
            label = 0.0
    else:
        future_return = None
        label = None
    return {
        "date": result.target_date,
        "target_date": result.target_date,
        "feature_date": result.feature_date,
        "frequency": result.frequency,
        "tenor": result.tenor,
        "future_return": future_return,
        "label": label,
        "pred_label": result.pred_label,
        "prob_up": result.prob_up,
        "threshold_used": result.threshold_used,
        "base_pred": result.base_pred,
        "base_decision": result.base_decision,
        "vote_sum": result.vote_sum,
        "decision": result.decision,
        "train_start": result.train_start,
        "train_end": result.train_end,
        "feature_count": len(result.feature_columns),
    }


def _predict_task(daily_df: pd.DataFrame, run_date: str, frequency: str) -> tuple[PredictionResult, dict]:
    config = TENOR_CONFIGS[frequency]
    result = predict_latest_for_config(daily_df, config, current_date=run_date)
    row = _prediction_to_row(daily_df, result)
    return result, row


def _run_tasks(daily_df: pd.DataFrame, dates: list[str], frequencies: Iterable[str], workers: int) -> tuple[list[PredictionResult], list[dict]]:
    tasks = [(run_date, frequency) for run_date in dates for frequency in frequencies]
    results: list[PredictionResult] = []
    rows: list[dict] = []
    if workers <= 1:
        for run_date, frequency in tasks:
            result, row = _predict_task(daily_df, run_date, frequency)
            results.append(result)
            rows.append(row)
        return results, rows

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_predict_task, daily_df, run_date, frequency): (run_date, frequency)
            for run_date, frequency in tasks
        }
        for future in as_completed(future_map):
            result, row = future.result()
            results.append(result)
            rows.append(row)
    return results, rows


def _write_outputs(rows: list[dict], start_date: str, end_date: str, base_output_dir: str | Path) -> tuple[Path, Path]:
    tag = _date_tag(start_date, end_date)
    output_dir = Path(base_output_dir) / tag
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = output_dir / "daily_backtest_predictions.csv"
    summary_path = output_dir / "daily_backtest_summary.csv"

    predictions = pd.DataFrame(rows).sort_values(["target_date", "frequency"]).reset_index(drop=True)
    predictions.to_csv(predictions_path, index=False)

    summary_rows = []
    for frequency, group in predictions.groupby("frequency", sort=True):
        direction_mask = group["label"].isin([-1.0, 1.0])
        direction_total = int(direction_mask.sum())
        direction_correct = int((direction_mask & group["label"].eq(group["pred_label"])).sum())
        all_mask = group["label"].isin([-1.0, 0.0, 1.0])
        all_total = int(all_mask.sum())
        all_correct = int((all_mask & group["label"].eq(group["pred_label"])).sum())
        summary_rows.append(
            {
                "frequency": frequency,
                "rows": int(len(group)),
                "all_correct": all_correct,
                "all_total": all_total,
                "all_accuracy": all_correct / all_total if all_total else None,
                "direction_correct": direction_correct,
                "direction_total": direction_total,
                "direction_accuracy": direction_correct / direction_total if direction_total else None,
                "pred_up": int(group["pred_label"].eq(1).sum()),
                "pred_down": int(group["pred_label"].eq(-1).sum()),
            }
        )
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    return predictions_path, summary_path


def run_backtest(
    daily_df: pd.DataFrame,
    start_date: str,
    end_date: str,
    workers: int = 1,
    dry_run: bool = False,
    frequencies: Iterable[str] = ("D1Y", "D5Y", "D10Y"),
    base_output_dir: str | Path = OUTPUT_DAILY / "backtest",
    audit_base_dir: str | Path = DATA_DAILY / "audit" / "backtest",
    write_results_func: Callable[[Iterable[PredictionResult]], None] | None = None,
) -> DailyBacktestOutputs:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    daily = daily_df.copy()
    if "date" not in daily.columns:
        raise ValueError("daily dataframe must contain date")
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values("date").reset_index(drop=True)

    dates = _trading_dates(daily, start_date, end_date)
    if not dates:
        raise ValueError(f"no daily data found between {start_date} and {end_date}")

    audit_csv_path, audit_metadata_path = _write_audit_files(daily, start_date, end_date, audit_base_dir)
    results, rows = _run_tasks(daily, dates, frequencies, workers)
    predictions_path, summary_path = _write_outputs(rows, start_date, end_date, base_output_dir)

    if dry_run:
        db_write_skipped = True
    else:
        if write_results_func is None:
            from .write_db import write_prediction_results

            write_results_func = write_prediction_results
        write_results_func(results)
        db_write_skipped = False

    return DailyBacktestOutputs(
        start_date=start_date,
        end_date=end_date,
        predictions_path=predictions_path,
        summary_path=summary_path,
        audit_csv_path=audit_csv_path,
        audit_metadata_path=audit_metadata_path,
        db_write_skipped=db_write_skipped,
    )


def main(argv: list[str] | None = None) -> DailyBacktestOutputs:
    parser = argparse.ArgumentParser(description="Run daily LightGBM rolling backtest.")
    parser.add_argument("start_date")
    parser.add_argument("end_date")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Skip database writes")
    args = parser.parse_args(argv)

    datetime.strptime(args.start_date, "%Y-%m-%d")
    datetime.strptime(args.end_date, "%Y-%m-%d")

    from data_service import build_daily_output_from_db

    history_start = (datetime.strptime(args.start_date, "%Y-%m-%d") - timedelta(days=7 * 365)).strftime("%Y-%m-%d")
    daily_df = build_daily_output_from_db(start_date=history_start, end_date=args.end_date)
    outputs = run_backtest(
        daily_df=daily_df,
        start_date=args.start_date,
        end_date=args.end_date,
        workers=args.workers,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "start_date": outputs.start_date,
                "end_date": outputs.end_date,
                "predictions_path": str(outputs.predictions_path),
                "summary_path": str(outputs.summary_path),
                "db_write_skipped": outputs.db_write_skipped,
            },
            ensure_ascii=False,
        )
    )
    return outputs


if __name__ == "__main__":
    main()
