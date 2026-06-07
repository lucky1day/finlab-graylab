from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import DATA_DAILY, MODEL_STORE, OUTPUT_DAILY, TENOR_CONFIGS
from .lgbm_predictor import PredictionResult, predict_latest_for_config
from .model_store import save_model_artifacts


@dataclass(frozen=True)
class DailyRunOutputs:
    rdate: str
    target_date: str
    feature_date: str
    signals: dict[str, int]
    signal_path: Path
    detail_path: Path
    shap_path: Path
    audit_csv_path: Path
    audit_metadata_path: Path
    db_write_skipped: bool


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _result_detail(result: PredictionResult) -> dict:
    return {
        "rdate": result.rdate,
        "target_date": result.target_date,
        "feature_date": result.feature_date,
        "tenor": result.tenor,
        "frequency": result.frequency,
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


def _write_output_files(results: list[PredictionResult], base_output_dir: str | Path) -> tuple[Path, Path]:
    if not results:
        raise ValueError("no prediction results to write")
    rdate = results[0].rdate
    output_dir = Path(base_output_dir) / "trade_data" / rdate
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = {item.frequency: item.pred_label for item in results}
    details = {item.frequency: _result_detail(item) for item in results}
    signal_path = output_dir / "daily_prediction_signal.json"
    detail_path = output_dir / "daily_prediction_detail.json"
    signal_path.write_text(json.dumps(signals, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    detail_path.write_text(json.dumps(details, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return signal_path, detail_path


def _write_shap_file(
    shap_records_by_frequency: dict[str, list[dict[str, object]]],
    rdate: str,
    base_output_dir: str | Path,
) -> Path:
    output_dir = Path(base_output_dir) / "trade_data" / rdate
    output_dir.mkdir(parents=True, exist_ok=True)
    shap_path = output_dir / "daily_shap_summary.json"
    shap_path.write_text(
        json.dumps(shap_records_by_frequency, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return shap_path


def _write_audit_files(
    daily_df: pd.DataFrame,
    target_date: str,
    feature_date: str,
    audit_base_dir: str | Path,
) -> tuple[Path, Path]:
    audit_dir = Path(audit_base_dir) / target_date
    audit_dir.mkdir(parents=True, exist_ok=True)
    date_tag = target_date.replace("-", "")
    csv_path = audit_dir / f"daily_model_input_{date_tag}.csv"
    metadata_path = audit_dir / f"daily_model_input_{date_tag}_metadata.json"

    audit_df = daily_df.copy()
    if "date" not in audit_df.columns:
        raise ValueError("audit daily dataframe must contain date")
    audit_df["date"] = pd.to_datetime(audit_df["date"])
    audit_df = audit_df.sort_values("date").reset_index(drop=True)
    audit_df.to_csv(csv_path, index=False)

    metadata = {
        "rdate": target_date,
        "target_date": target_date,
        "feature_date": feature_date,
        "row_count": int(len(audit_df)),
        "column_count": int(len(audit_df.columns)),
        "date_min": audit_df["date"].min().strftime("%Y-%m-%d") if len(audit_df) else None,
        "date_max": audit_df["date"].max().strftime("%Y-%m-%d") if len(audit_df) else None,
        "source": "database_daily_output",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, metadata_path


def run_daily_predictions(
    daily_df: pd.DataFrame,
    current_date: str | None = None,
    base_output_dir: str | Path = OUTPUT_DAILY,
    model_store_dir: str | Path = MODEL_STORE,
    audit_base_dir: str | Path = DATA_DAILY / "audit",
    frequencies: Iterable[str] = ("D1Y", "D5Y", "D10Y"),
    dry_run: bool = False,
) -> DailyRunOutputs:
    results: list[PredictionResult] = []
    shap_records_by_frequency: dict[str, list[dict[str, object]]] = {}
    for frequency in frequencies:
        config = TENOR_CONFIGS[frequency]
        result = predict_latest_for_config(daily_df, config, current_date=current_date)
        save_model_artifacts(result, model_store_dir)
        results.append(result)
        from .shap_analysis import compute_shap_records_for_result

        shap_records_by_frequency[result.frequency] = compute_shap_records_for_result(daily_df, result)

    signal_path, detail_path = _write_output_files(results, base_output_dir)
    shap_path = _write_shap_file(shap_records_by_frequency, results[0].rdate, base_output_dir)
    audit_csv_path, audit_metadata_path = _write_audit_files(
        daily_df,
        results[0].target_date,
        results[0].feature_date,
        audit_base_dir,
    )

    if dry_run:
        db_write_skipped = True
    else:
        from .write_db import write_prediction_results, write_shap_results

        write_prediction_results(results)
        write_shap_results([row for rows in shap_records_by_frequency.values() for row in rows])
        db_write_skipped = False

    return DailyRunOutputs(
        rdate=results[0].rdate,
        target_date=results[0].target_date,
        feature_date=results[0].feature_date,
        signals={item.frequency: item.pred_label for item in results},
        signal_path=signal_path,
        detail_path=detail_path,
        shap_path=shap_path,
        audit_csv_path=audit_csv_path,
        audit_metadata_path=audit_metadata_path,
        db_write_skipped=db_write_skipped,
    )


def main_daily_process(current_date: str | None = None, dry_run: bool = False) -> DailyRunOutputs:
    if current_date is not None:
        datetime.strptime(current_date, "%Y-%m-%d")
    end_date = current_date or datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=7 * 365)).strftime("%Y-%m-%d")
    from data_service import build_daily_output_from_db

    logging.info("building daily output from database: %s to %s", start_date, end_date)
    daily_df = build_daily_output_from_db(start_date=start_date, end_date=end_date)
    return run_daily_predictions(daily_df, current_date=end_date, dry_run=dry_run)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run daily LightGBM production forecasts.")
    parser.add_argument("date", nargs="?", default=None, help="Run date in YYYY-MM-DD format")
    parser.add_argument("--dry-run", action="store_true", help="Do not write forecast rows to database")
    args = parser.parse_args()
    outputs = main_daily_process(args.date, dry_run=args.dry_run)
    print(json.dumps({"rdate": outputs.rdate, "signals": outputs.signals}, ensure_ascii=False))
