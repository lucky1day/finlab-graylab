from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from shared.artifact_paths import benchmark_data_check_root, benchmark_source_evidence_root
from shared.data_service import build_daily_output_from_db as build_shared_daily_output_from_db
from shared.data_service import create_sqlalchemy_engine

SOURCE_EVIDENCE_DAILY_CSV = benchmark_source_evidence_root("model_muti_0529") / "daily_output.csv"
ARTIFACT_ROOT = benchmark_data_check_root("model_muti_0529")
UPSTREAM_DAILY_TARGETS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")


def _read_daily(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result.columns = [str(col).strip().lstrip("\ufeff") for col in result.columns]
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    result = result.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in result.columns:
        if col != "date":
            result[col] = pd.to_numeric(result[col], errors="coerce")
    return result


def _align_like(reference: pd.DataFrame, candidate: pd.DataFrame) -> pd.DataFrame:
    aligned = candidate.set_index("date").reindex(reference["date"])
    for col in reference.columns:
        if col != "date" and col not in aligned.columns:
            aligned[col] = np.nan
    aligned = aligned[[col for col in reference.columns if col != "date"]].reset_index()
    return aligned.rename(columns={"index": "date"})


def _frame_profile(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "date_min": None if df.empty else df["date"].min().strftime("%Y-%m-%d"),
        "date_max": None if df.empty else df["date"].max().strftime("%Y-%m-%d"),
    }


def _compare(left: pd.DataFrame, right: pd.DataFrame, left_name: str, right_name: str) -> dict[str, Any]:
    left_cols = left.columns.tolist()
    right_cols = right.columns.tolist()
    max_abs = 0.0
    missing_diff_count = 0
    first_diff: dict[str, Any] | None = None
    above_threshold: list[dict[str, Any]] = []

    for col in [item for item in left_cols if item != "date"]:
        left_values = pd.to_numeric(left[col], errors="coerce")
        if col in right.columns:
            right_values = pd.to_numeric(right[col], errors="coerce")
        else:
            right_values = pd.Series(np.nan, index=left.index)
        left_na = left_values.isna()
        right_na = right_values.isna()
        missing_mask = left_na.ne(right_na)
        missing_count = int(missing_mask.sum())
        missing_diff_count += missing_count
        if first_diff is None and missing_count:
            idx = int(missing_mask[missing_mask].index[0])
            first_diff = {
                "column": col,
                "date": left.loc[idx, "date"].strftime("%Y-%m-%d"),
                f"{left_name}_is_null": bool(left_na.loc[idx]),
                f"{right_name}_is_null": bool(right_na.loc[idx]),
            }
        both = ~(left_na | right_na)
        if bool(both.any()):
            diff = (left_values[both] - right_values[both]).abs()
            col_max = float(diff.max()) if len(diff) else 0.0
            max_abs = max(max_abs, col_max)
            diff_mask = diff.gt(1e-8)
            if bool(diff_mask.any()) and len(above_threshold) < 20:
                idx = int(diff[diff_mask].index[0])
                row = {
                    "column": col,
                    "date": left.loc[idx, "date"].strftime("%Y-%m-%d"),
                    left_name: None if pd.isna(left_values.loc[idx]) else float(left_values.loc[idx]),
                    right_name: None if pd.isna(right_values.loc[idx]) else float(right_values.loc[idx]),
                    "abs_diff": float(diff.loc[idx]),
                }
                above_threshold.append(row)
                if first_diff is None:
                    first_diff = row

    return {
        "left": left_name,
        "right": right_name,
        "left_profile": _frame_profile(left),
        "right_profile": _frame_profile(right),
        "date_match": left["date"].dt.strftime("%Y-%m-%d").tolist() == right["date"].dt.strftime("%Y-%m-%d").tolist(),
        "column_order_match": left_cols == right_cols,
        "left_only_columns": sorted(set(left_cols) - set(right_cols)),
        "right_only_columns": sorted(set(right_cols) - set(left_cols)),
        "overall_max_abs_diff": float(max_abs),
        "missing_diff_count": int(missing_diff_count),
        "first_diff": first_diff,
        "above_threshold_sample": above_threshold,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="只读审计框架日频 data_service 生成链路")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output-dir", type=Path, default=ARTIFACT_ROOT)
    args = parser.parse_args()

    source_evidence = _read_daily(SOURCE_EVIDENCE_DAILY_CSV)
    start_date = args.start_date or source_evidence["date"].min().strftime("%Y-%m-%d")
    end_date = args.end_date or source_evidence["date"].max().strftime("%Y-%m-%d")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = create_sqlalchemy_engine()
    try:
        shared_input_full = _normalize_daily(
            build_shared_daily_output_from_db(start_date=start_date, end_date=end_date, engine=engine)
        )
    finally:
        engine.dispose()

    shared_input_aligned = _align_like(source_evidence, shared_input_full)

    shared_input_path = output_dir / "shared_daily_data_service_generated_daily_output.csv"
    shared_input_aligned_path = output_dir / "shared_daily_data_service_generated_daily_output_aligned.csv"
    summary_path = output_dir / "daily_data_service_audit_summary.json"

    shared_input_full.to_csv(shared_input_path, index=False)
    shared_input_aligned.to_csv(shared_input_aligned_path, index=False)

    summary = {
        "source": {
            "daily_data_service": "shared/data_service.py",
            "source_evidence_daily": str(SOURCE_EVIDENCE_DAILY_CSV),
            "start_date": start_date,
            "end_date": end_date,
            "upstream_daily_targets": list(UPSTREAM_DAILY_TARGETS),
        },
        "outputs": {
            "shared_input_full": str(shared_input_path),
            "shared_input_aligned": str(shared_input_aligned_path),
        },
        "comparisons": {
            "source_evidence_vs_shared_input_aligned": _compare(
                source_evidence,
                shared_input_aligned,
                "source_evidence",
                "shared_input",
            ),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
