from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtests.daily_0529_reproduction import (
    build_db_aligned_daily,
    build_daily0529_benchmark_frame,
    read_daily_csv,
    run_t1_reproduction,
    run_t5_reproduction,
)
from backtests.repository import clean_json
from shared.data_service import create_sqlalchemy_engine


SCHEMES_ROOT = PROJECT_ROOT / "schemes"
VALID_SCHEME_IDS = ("t1_daily", "t5_daily")
BENCHMARK_FIELDNAMES = [
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "direction",
    "confidence",
    "label",
    "is_correct",
]


def benchmark_row(row: dict[str, Any]) -> dict[str, str]:
    """将 backtest row 转成 CompareGate 严格格式。"""
    required = ("feature_date", "target_date", "target_tenor", "horizon", "predicted_direction", "confidence")
    missing = [field for field in required if row.get(field) is None or str(row.get(field)).strip() == ""]
    if missing:
        raise ValueError(f"row missing required fields for benchmark export: {missing}")

    label = row.get("label")
    direction = int(row["predicted_direction"])
    is_correct = "" if label is None else ("true" if int(label) == direction else "false")
    return {
        "feature_date": _format_value(row["feature_date"]),
        "target_date": _format_value(row["target_date"]),
        "target_tenor": _format_value(row["target_tenor"]),
        "horizon": str(int(row["horizon"])),
        "direction": str(direction),
        "confidence": _format_value(row["confidence"]),
        "label": "" if label is None else str(int(label)),
        "is_correct": is_correct,
    }


def rebuild_daily0529_scheme_benchmarks(scheme_ids: Iterable[str], n_jobs: int = 4) -> dict[str, Any]:
    """重建 t1/t5 逐方案 original/current benchmark 文件，不写 DB。"""
    requested = _normalize_scheme_ids(scheme_ids)
    engine = create_sqlalchemy_engine()
    try:
        csv_df = read_daily_csv()
        benchmark_df = build_daily0529_benchmark_frame(csv_df, engine=engine)
        _, db_aligned = build_db_aligned_daily(
            benchmark_df,
            engine=engine,
            upstream_mode=True,
            artifact_scheme_id="daily0529_benchmark_rebuild",
        )

        outputs_by_scheme: dict[str, dict[str, Any]] = {}
        if "t5_daily" in requested:
            outputs_by_scheme["t5_daily"] = _outputs_by_data_source(
                run_t5_reproduction(engine=engine, db_aligned=db_aligned, n_jobs=n_jobs)
            )
        if "t1_daily" in requested:
            outputs_by_scheme["t1_daily"] = _outputs_by_data_source(
                run_t1_reproduction(engine=engine, db_aligned=db_aligned)
            )

        results = []
        for scheme_id in requested:
            outputs = outputs_by_scheme[scheme_id]
            original = outputs["baseline_original_csv"]
            current = outputs["framework_db_aligned"]
            original_rows = [benchmark_row(row) for row in original.rows]
            current_rows = [benchmark_row(row) for row in current.rows]
            _assert_same_strict_keys(scheme_id, original_rows, current_rows)

            bench_dir = SCHEMES_ROOT / scheme_id / "benchmarks"
            bench_dir.mkdir(parents=True, exist_ok=True)
            _write_predictions_csv(bench_dir / "original_predictions_sample.csv", original_rows)
            _write_predictions_csv(bench_dir / "current_predictions_sample.csv", current_rows)
            _write_json(bench_dir / "original_backtest_summary.json", summary_for_export(original.summary))
            _write_json(bench_dir / "current_backtest_summary.json", summary_for_export(current.summary))
            results.append(
                {
                    "scheme_id": scheme_id,
                    "benchmark_dir": str(bench_dir),
                    "original_rows": len(original_rows),
                    "current_rows": len(current_rows),
                }
            )

        return {"schemes": results}
    finally:
        engine.dispose()


def _normalize_scheme_ids(scheme_ids: Iterable[str]) -> list[str]:
    requested = list(scheme_ids) or list(VALID_SCHEME_IDS)
    unknown = sorted(set(requested) - set(VALID_SCHEME_IDS))
    if unknown:
        raise ValueError(f"unsupported scheme_id for daily0529 benchmark rebuild: {unknown}")
    return requested


def _outputs_by_data_source(outputs: Iterable[Any]) -> dict[str, Any]:
    result = {output.data_source: output for output in outputs}
    required = {"baseline_original_csv", "framework_db_aligned"}
    missing = sorted(required - set(result))
    if missing:
        raise ValueError(f"daily0529 reproduction missing required outputs: {missing}")
    return result


def _assert_same_strict_keys(scheme_id: str, original_rows: list[dict[str, str]], current_rows: list[dict[str, str]]) -> None:
    original_keys = {_strict_key(row) for row in original_rows}
    current_keys = {_strict_key(row) for row in current_rows}
    if original_keys != current_keys:
        missing = sorted(original_keys - current_keys)[:10]
        extra = sorted(current_keys - original_keys)[:10]
        raise ValueError(f"{scheme_id} original/current strict keys differ; missing={missing}, extra={extra}")


def _strict_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (row["feature_date"], row["target_date"], row["target_tenor"], row["horizon"])


def _write_predictions_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BENCHMARK_FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summary_for_export(summary: dict[str, Any]) -> dict[str, Any]:
    """去掉 runner 诊断字段，只保留 original/current 可比较摘要。"""
    return {key: value for key, value in summary.items() if key != "comparison"}


def _format_value(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Rebuild strict benchmark baselines for daily 0529 schemes.")
    parser.add_argument("--scheme-id", action="append", choices=VALID_SCHEME_IDS, default=[])
    parser.add_argument("--n-jobs", type=int, default=4)
    args = parser.parse_args(argv)
    result = rebuild_daily0529_scheme_benchmarks(args.scheme_id, n_jobs=args.n_jobs)
    print(json.dumps(clean_json(result), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
