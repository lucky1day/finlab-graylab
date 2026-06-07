from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_DATA_SERVICE = PROJECT_ROOT / "schemes" / "_original_source" / "data_service.py"
CANONICAL_DAILY = PROJECT_ROOT / "benchmarks" / "model_muti_0529" / "daily_output.csv"
ARTIFACT_ROOT = PROJECT_ROOT / "backtest_artifacts" / "model_muti_0529"

sys.path.insert(0, str(PROJECT_ROOT))

from shared.data_service import build_daily_output_from_db as build_shared_daily_output_from_db
from shared.data_service import create_sqlalchemy_engine


def _load_original_data_service():
    """动态加载原始 data_service.py，保持原文件只读不改。"""
    spec = importlib.util.spec_from_file_location("bfl_original_daily_data_service", ORIGINAL_DATA_SERVICE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load original data_service: {ORIGINAL_DATA_SERVICE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    parser = argparse.ArgumentParser(description="只读审计原始日频 data_service 生成链路")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output-dir", type=Path, default=ARTIFACT_ROOT)
    args = parser.parse_args()

    canonical = _read_daily(CANONICAL_DAILY)
    start_date = args.start_date or canonical["date"].min().strftime("%Y-%m-%d")
    end_date = args.end_date or canonical["date"].max().strftime("%Y-%m-%d")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    original_service = _load_original_data_service()
    engine = create_sqlalchemy_engine()
    try:
        original_full = _normalize_daily(
            original_service.build_daily_output_from_db(start_date=start_date, end_date=end_date, engine=engine)
        )
        shared_upstream_full = _normalize_daily(
            build_shared_daily_output_from_db(
                start_date=start_date,
                end_date=end_date,
                target_columns=tuple(original_service.DAILY_TARGETS),
                engine=engine,
            )
        )
        shared_default_full = _normalize_daily(
            build_shared_daily_output_from_db(start_date=start_date, end_date=end_date, engine=engine)
        )
    finally:
        engine.dispose()

    original_aligned = _align_like(canonical, original_full)
    shared_upstream_aligned = _align_like(canonical, shared_upstream_full)
    shared_default_aligned = _align_like(canonical, shared_default_full)

    original_path = output_dir / "original_data_service_generated_daily_output.csv"
    original_aligned_path = output_dir / "original_data_service_generated_daily_output_aligned.csv"
    shared_upstream_path = output_dir / "shared_upstream_generated_daily_output_aligned.csv"
    shared_default_path = output_dir / "shared_default_generated_daily_output_aligned.csv"
    summary_path = output_dir / "original_daily_data_service_audit_summary.json"

    original_full.to_csv(original_path, index=False)
    original_aligned.to_csv(original_aligned_path, index=False)
    shared_upstream_aligned.to_csv(shared_upstream_path, index=False)
    shared_default_aligned.to_csv(shared_default_path, index=False)

    summary = {
        "source": {
            "original_data_service": str(ORIGINAL_DATA_SERVICE),
            "canonical_daily": str(CANONICAL_DAILY),
            "start_date": start_date,
            "end_date": end_date,
            "original_daily_targets": list(original_service.DAILY_TARGETS),
        },
        "outputs": {
            "original_full": str(original_path),
            "original_aligned": str(original_aligned_path),
            "shared_upstream_aligned": str(shared_upstream_path),
            "shared_default_aligned": str(shared_default_path),
        },
        "comparisons": {
            "canonical_vs_original_aligned": _compare(canonical, original_aligned, "canonical", "original_db"),
            "original_aligned_vs_shared_upstream": _compare(
                original_aligned,
                shared_upstream_aligned,
                "original_db",
                "shared_upstream",
            ),
            "original_aligned_vs_shared_default": _compare(
                original_aligned,
                shared_default_aligned,
                "original_db",
                "shared_default",
            ),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
