from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtests._base_runner import make_run_output
from backtests.repository import clean_json
from backtests.weekly_avg_lgbm_0529_reproduction import run_weekly_avg_lgbm_0529_reproduction
from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE
from shared.weekly_average_source_evidence import (
    PLATFORM_CURRENT_SOURCE_ROLE,
    WEEKLY_AVERAGE_SOURCE_ROLE,
    WeeklyAverageSourceEvidence,
    require_weekly_average_source_evidence,
)


SCHEMES_ROOT = PROJECT_ROOT / "schemes"
BASE_FIELDNAMES = [
    "feature_week_id",
    "feature_date",
    "target_week_id",
    "target_date",
    "target_tenor",
    "horizon",
    "target_rule",
    "benchmark_role",
    "direction",
    "confidence",
    "label",
    "is_correct",
]


@dataclass(frozen=True)
class WeeklyAverageBenchmarkSpec:
    scheme_id: str
    target_tenor: str
    current_runner_module: str
    current_runner_function: str
    internal_fields: tuple[str, ...]
    benchmark_id: str = "model_muti_0529"
    data_source: str = "framework_db_aligned"
    horizon_days: int = 6
    live_target_start_date: str = "2026-06-01"


SPECS: dict[str, WeeklyAverageBenchmarkSpec] = {
    "weekly_avg_1y_lgbm_0529": WeeklyAverageBenchmarkSpec(
        scheme_id="weekly_avg_1y_lgbm_0529",
        target_tenor="1Y",
        current_runner_module="backtests.weekly_avg_1y_lgbm_0529_reproduction",
        current_runner_function="run_weekly_avg_1y_lgbm_0529_reproduction",
        internal_fields=(
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ),
    ),
    "weekly_avg_5y_lgbm_0529": WeeklyAverageBenchmarkSpec(
        scheme_id="weekly_avg_5y_lgbm_0529",
        target_tenor="5Y",
        current_runner_module="backtests.weekly_avg_5y_lgbm_0529_reproduction",
        current_runner_function="run_weekly_avg_5y_lgbm_0529_reproduction",
        internal_fields=(
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ),
    ),
    "weekly_avg_10y_lgbm_0529": WeeklyAverageBenchmarkSpec(
        scheme_id="weekly_avg_10y_lgbm_0529",
        target_tenor="10Y",
        current_runner_module="backtests.weekly_avg_10y_lgbm_0529_reproduction",
        current_runner_function="run_weekly_avg_10y_lgbm_0529_reproduction",
        internal_fields=(
            "frequency",
            "model_id",
            "source",
            "close_col",
            "prob_up",
            "threshold_used",
            "model_margin",
            "training_rows",
            "calibration_rows",
            "feature_count",
            "effective_week_id",
        ),
    ),
}


def rebuild_weekly_average_0529_benchmarks(scheme_ids: Iterable[str]) -> dict[str, Any]:
    requested = _normalize_scheme_ids(scheme_ids)
    results = []
    for scheme_id in requested:
        spec = SPECS[scheme_id]
        result = rebuild_one(spec)
        results.append(result)
    return {"schemes": results}


def rebuild_one(spec: WeeklyAverageBenchmarkSpec) -> dict[str, Any]:
    evidence = require_weekly_average_source_evidence(spec.scheme_id, project_root=PROJECT_ROOT)
    original_payload = run_weekly_avg_lgbm_0529_reproduction(spec.scheme_id, persist=False)
    current_payload = _call_runner(spec.current_runner_module, spec.current_runner_function)

    current_rows = [_normalize_current_row(spec, row) for row in current_payload.get("rows") or []]
    original_rows = [_normalize_original_row(spec, row) for row in original_payload.get("rows") or []]
    strict_rows = sum(1 for row in original_rows if row.get("benchmark_role") == "historical/source-original")
    extension_rows = sum(1 for row in original_rows if row.get("benchmark_role") == "source-compatible-extension")

    _assert_same_keys(spec.scheme_id, original_rows, current_rows)
    _assert_required_fields(spec, original_rows, "original")
    _assert_required_fields(spec, current_rows, "current")

    bench_dir = SCHEMES_ROOT / spec.scheme_id / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = BASE_FIELDNAMES + list(spec.internal_fields)
    _write_csv(bench_dir / "original_predictions_sample.csv", fieldnames, original_rows)
    _write_csv(bench_dir / "current_predictions_sample.csv", fieldnames, current_rows)

    original_summary = _summary_for_export(
        spec,
        _summary_from_original_rows(spec, original_rows, original_payload.get("summary") or {}),
        source_role=WEEKLY_AVERAGE_SOURCE_ROLE,
        generator=evidence.generator,
        evidence=evidence,
        strict_rows=strict_rows,
        extension_rows=extension_rows,
        row_count=len(original_rows),
    )
    current_summary = _summary_for_export(
        spec,
        current_payload.get("summary") or {},
        source_role=PLATFORM_CURRENT_SOURCE_ROLE,
        generator=f"{spec.current_runner_module}.{spec.current_runner_function}",
        evidence=evidence,
        strict_rows=strict_rows,
        extension_rows=extension_rows,
        row_count=len(current_rows),
    )
    _write_json(bench_dir / "original_backtest_summary.json", original_summary)
    _write_json(bench_dir / "current_backtest_summary.json", current_summary)
    return {
        "scheme_id": spec.scheme_id,
        "benchmark_dir": str(bench_dir),
        "rows": len(current_rows),
        "strict_source_original_rows": strict_rows,
        "source_compatible_extension_rows": extension_rows,
    }


def _call_runner(module_name: str, function_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    run_func: Callable[..., dict[str, Any]] = getattr(module, function_name)
    return run_func(persist=False)


def _normalize_current_row(spec: WeeklyAverageBenchmarkSpec, row: dict[str, Any]) -> dict[str, Any]:
    out = _base_row(row, benchmark_role="platform-current")
    for field in spec.internal_fields:
        out[field] = _format_value(row.get(field))
    return out


def _normalize_original_row(spec: WeeklyAverageBenchmarkSpec, row: dict[str, Any]) -> dict[str, Any]:
    out = _base_row(row, benchmark_role="historical/source-original")
    for field in spec.internal_fields:
        out[field] = _format_value(row.get(field))
    return out


def _base_row(row: dict[str, Any], *, benchmark_role: str) -> dict[str, Any]:
    direction = _int_or_none(row.get("direction"))
    label = _int_or_none(row.get("label"))
    return {
        "feature_week_id": _format_value(row.get("feature_week_id")),
        "feature_date": _format_value(row.get("feature_date")),
        "target_week_id": _format_value(row.get("target_week_id")),
        "target_date": _format_value(row.get("target_date")),
        "target_tenor": _format_value(row.get("target_tenor")),
        "horizon": _format_value(row.get("horizon")),
        "target_rule": WEEKLY_AVERAGE_TARGET_RULE,
        "benchmark_role": benchmark_role,
        "direction": _format_value(row.get("direction")),
        "confidence": _format_value(row.get("confidence")),
        "label": _format_value(row.get("label")),
        "is_correct": _format_bool(direction == label) if direction is not None and label is not None else "",
    }


SUMMARY_CALCULATED_KEYS = {
    "by_tenor",
    "periods_by_tenor",
    "row_count",
    "raw_row_count",
    "excluded_row_count",
    "evaluation_filter",
    "monthly_count",
    "target_rule",
    "original_benchmark_validation",
}


def _summary_from_original_rows(
    spec: WeeklyAverageBenchmarkSpec,
    rows: list[dict[str, Any]],
    source_summary: dict[str, Any],
) -> dict[str, Any]:
    full_rows = [_full_backtest_row(spec, row) for row in rows]
    if not full_rows:
        raise RuntimeError(f"{spec.scheme_id}: original benchmark produced no rows")
    start_date = min(str(row["predict_date"]) for row in full_rows)
    end_date = max(str(row["predict_date"]) for row in full_rows)
    output = make_run_output(
        spec.scheme_id,
        spec.data_source,
        start_date,
        end_date,
        full_rows,
        benchmark_id=spec.benchmark_id,
    )
    summary = dict(output.summary)
    for key, value in source_summary.items():
        if key in SUMMARY_CALCULATED_KEYS or key == "comparison":
            continue
        summary[key] = value
    summary["target_rule"] = WEEKLY_AVERAGE_TARGET_RULE
    return clean_json(summary)


def _full_backtest_row(spec: WeeklyAverageBenchmarkSpec, row: dict[str, Any]) -> dict[str, Any]:
    direction = _int_or_none(row.get("direction"))
    return {
        "benchmark_id": spec.benchmark_id,
        "scheme_id": spec.scheme_id,
        "target_tenor": spec.target_tenor,
        "horizon": spec.horizon_days,
        "predict_date": str(row["feature_date"]),
        "feature_date": str(row["feature_date"]),
        "target_date": str(row["target_date"]),
        "label": _int_or_none(row.get("label")),
        "predicted_direction": direction,
        "model_pred": direction,
        "confidence": _float_or_none(row.get("confidence")),
        "source_row": {},
        "extra": {
            "frequency": "weekly",
            "model_version": WEEKLY_AVERAGE_SOURCE_ROLE,
            "target_rule": WEEKLY_AVERAGE_TARGET_RULE,
            "feature_week_id": _int_or_none(row.get("feature_week_id")),
            "target_week_id": _int_or_none(row.get("target_week_id")),
            "feature_date": str(row["feature_date"]),
            "target_date": str(row["target_date"]),
        },
    }


def _summary_for_export(
    spec: WeeklyAverageBenchmarkSpec,
    source_summary: dict[str, Any],
    *,
    source_role: str,
    generator: str,
    evidence: WeeklyAverageSourceEvidence,
    strict_rows: int,
    extension_rows: int,
    row_count: int,
) -> dict[str, Any]:
    summary = {key: value for key, value in source_summary.items() if key != "comparison"}
    summary["target_rule"] = WEEKLY_AVERAGE_TARGET_RULE
    summary["original_benchmark_validation"] = {
        "benchmark_rows": row_count,
        "matched_rows": row_count,
        "strict_key": ["feature_week_id", "feature_date", "target_date", "target_tenor", "horizon"],
    }
    summary["benchmark_provenance"] = {
        "source_role": source_role,
        "generator": generator,
        "bootstrap_source": WEEKLY_AVERAGE_SOURCE_ROLE,
        "target_rule": WEEKLY_AVERAGE_TARGET_RULE,
        "strict_source_original_rows": strict_rows,
        "source_compatible_extension_rows": extension_rows,
        "source_package_hash": evidence.source_package_hash,
        "source_package_path": str(evidence.source_package_path.relative_to(PROJECT_ROOT)),
        "source_frequency": evidence.frequency,
        "source_model_id": evidence.model_id,
        "source_target_column": evidence.target_column,
        "source_runner_module": evidence.runner_module,
        "manifest_path": str(evidence.manifest_path.relative_to(PROJECT_ROOT)),
        "current_generator": f"{spec.current_runner_module}.{spec.current_runner_function}",
    }
    return clean_json(summary)


def _assert_same_keys(
    scheme_id: str,
    original_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
) -> None:
    original_keys = {_strict_key(row) for row in original_rows}
    current_keys = {_strict_key(row) for row in current_rows}
    if original_keys != current_keys:
        missing = sorted(original_keys - current_keys)[:10]
        extra = sorted(current_keys - original_keys)[:10]
        raise RuntimeError(f"{scheme_id}: original/current strict keys differ; missing={missing}, extra={extra}")


def _assert_required_fields(
    spec: WeeklyAverageBenchmarkSpec,
    rows: list[dict[str, Any]],
    label: str,
) -> None:
    required = set(BASE_FIELDNAMES) | set(spec.internal_fields)
    for index, row in enumerate(rows, start=2):
        missing = [field for field in required if _is_blank(row.get(field))]
        if missing:
            raise RuntimeError(f"{spec.scheme_id}: {label} row {index} missing fields {missing}")


def _strict_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row["feature_week_id"]),
        str(row["feature_date"]),
        str(row["target_date"]),
        str(row["target_tenor"]),
        str(row["horizon"]),
    )


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_scheme_ids(scheme_ids: Iterable[str]) -> list[str]:
    requested = list(scheme_ids) or sorted(SPECS)
    unknown = sorted(set(requested) - set(SPECS))
    if unknown:
        raise ValueError(f"unsupported weekly average scheme_id: {unknown}")
    return requested


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _int_value(value: Any, message: str) -> int:
    parsed = _int_or_none(value)
    if parsed is None:
        raise RuntimeError(message)
    return parsed


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Rebuild strict weekly-average 0529 benchmark files.")
    parser.add_argument("--scheme-id", action="append", choices=sorted(SPECS), default=[])
    args = parser.parse_args(argv)
    result = rebuild_weekly_average_0529_benchmarks(args.scheme_id)
    print(json.dumps(clean_json(result), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
