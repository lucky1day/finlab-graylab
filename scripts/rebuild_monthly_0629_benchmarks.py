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

from backtests.repository import clean_json
from harness.config_loader import load_config_raw
from scheduler.monthly_actuals_updater import build_monthly_actual_records
from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from shared.monthly_predict_adapter import HORIZON_DAYS, INTERNAL_FIELDS
from shared.monthly_source_evidence import (
    MONTHLY_SOURCE_BATCH,
    MONTHLY_SOURCE_ROLE,
    PLATFORM_CURRENT_MONTHLY_ROLE,
    MonthlySourceEvidence,
    require_monthly_source_evidence,
)
from shared.monthly_source_runner import run_source_monthly_live
from shared.prediction_context import MONTHLY_TARGET_RULE, build_monthly_live_context


BASE_FIELDNAMES = [
    "feature_month_id",
    "feature_date",
    "target_month_id",
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
STRICT_KEY_FIELDS = (
    "feature_month_id",
    "feature_date",
    "target_month_id",
    "target_date",
    "target_tenor",
    "horizon",
)
DEFAULT_PREDICT_DATES = ("2026-04-15",)


@dataclass(frozen=True)
class MonthlyBenchmarkSpec:
    scheme_id: str
    target_tenor: str
    current_runner_module: str
    current_runner_function: str
    benchmark_id: str = MONTHLY_SOURCE_BATCH
    data_source: str = "source_original_monthly_binary_runner"


SPECS: dict[str, MonthlyBenchmarkSpec] = {
    "monthly_1y_rf_top30_0629": MonthlyBenchmarkSpec(
        scheme_id="monthly_1y_rf_top30_0629",
        target_tenor="1Y",
        current_runner_module="backtests.monthly_1y_rf_top30_0629_reproduction",
        current_runner_function="run_monthly_1y_rf_top30_0629_reproduction",
    ),
    "monthly_5y_knn_top20_0629": MonthlyBenchmarkSpec(
        scheme_id="monthly_5y_knn_top20_0629",
        target_tenor="5Y",
        current_runner_module="backtests.monthly_5y_knn_top20_0629_reproduction",
        current_runner_function="run_monthly_5y_knn_top20_0629_reproduction",
    ),
    "monthly_10y_rf_top5_0629": MonthlyBenchmarkSpec(
        scheme_id="monthly_10y_rf_top5_0629",
        target_tenor="10Y",
        current_runner_module="backtests.monthly_10y_rf_top5_0629_reproduction",
        current_runner_function="run_monthly_10y_rf_top5_0629_reproduction",
    ),
}


def rebuild_monthly_0629_benchmarks(
    scheme_ids: Iterable[str],
    *,
    predict_dates: Iterable[str] = DEFAULT_PREDICT_DATES,
) -> dict[str, Any]:
    requested = _normalize_scheme_ids(scheme_ids)
    dates = tuple(predict_dates)
    if not dates:
        raise ValueError("at least one --predict-date is required")
    results = [rebuild_one(SPECS[scheme_id], predict_dates=dates) for scheme_id in requested]
    return {"schemes": results}


def rebuild_one(spec: MonthlyBenchmarkSpec, *, predict_dates: tuple[str, ...]) -> dict[str, Any]:
    evidence = require_monthly_source_evidence(spec.scheme_id, project_root=PROJECT_ROOT)
    engine = create_engine_from_env()
    try:
        calendar = get_calendar(engine)
        original_rows = [
            _original_row_from_source(spec, evidence, calendar, engine, predict_date)
            for predict_date in predict_dates
        ]
        current_payload = _call_runner(
            spec.current_runner_module,
            spec.current_runner_function,
            predict_dates=predict_dates,
            engine=engine,
        )
    finally:
        engine.dispose()

    current_rows = [_normalize_current_row(row) for row in current_payload.get("rows") or []]
    _assert_same_keys(spec.scheme_id, original_rows, current_rows)
    required_internal_fields = _required_internal_fields(spec.scheme_id)
    _assert_required_fields(spec.scheme_id, original_rows, required_internal_fields, "original")
    _assert_required_fields(spec.scheme_id, current_rows, required_internal_fields, "current")

    bench_dir = PROJECT_ROOT / "schemes" / spec.scheme_id / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = BASE_FIELDNAMES + list(INTERNAL_FIELDS)
    _write_csv(bench_dir / "original_predictions_sample.csv", fieldnames, original_rows)
    _write_csv(bench_dir / "current_predictions_sample.csv", fieldnames, current_rows)

    original_summary = _summary_for_export(
        spec,
        original_rows,
        source_role=MONTHLY_SOURCE_ROLE,
        generator=evidence.generator,
        evidence=evidence,
        current_generator=f"{spec.current_runner_module}.{spec.current_runner_function}",
    )
    current_summary = _summary_for_export(
        spec,
        current_rows,
        source_role=PLATFORM_CURRENT_MONTHLY_ROLE,
        generator=f"{spec.current_runner_module}.{spec.current_runner_function}",
        evidence=evidence,
        current_generator=f"{spec.current_runner_module}.{spec.current_runner_function}",
    )
    _write_json(bench_dir / "original_backtest_summary.json", original_summary)
    _write_json(bench_dir / "current_backtest_summary.json", current_summary)
    return {
        "scheme_id": spec.scheme_id,
        "benchmark_dir": str(bench_dir),
        "rows": len(current_rows),
        "source_original_monthly_algorithm_rows": len(original_rows),
        "platform_current_monthly_adapter_rows": len(current_rows),
    }


def _original_row_from_source(
    spec: MonthlyBenchmarkSpec,
    evidence: MonthlySourceEvidence,
    calendar: Any,
    engine: Any,
    predict_date: str,
) -> dict[str, Any]:
    source_rows = run_source_monthly_live(evidence, predict_date=predict_date)
    source = _select_frequency_row(source_rows, evidence.frequency, spec.scheme_id)
    context = build_monthly_live_context(calendar, predict_date)
    _assert_source_context_matches(source, context.feature_month_id, context.target_month_id, spec.scheme_id)
    label = _monthly_label(engine, spec.target_tenor, context)
    direction = _direction_from_y_pred(source.get("y_pred"))
    confidence = _confidence_for_direction(source, direction)
    row = {
        "feature_month_id": context.feature_month_id,
        "feature_date": context.feature_date,
        "target_month_id": context.target_month_id,
        "target_date": context.target_date,
        "target_tenor": spec.target_tenor,
        "horizon": HORIZON_DAYS,
        "target_rule": MONTHLY_TARGET_RULE,
        "benchmark_role": MONTHLY_SOURCE_ROLE,
        "direction": direction,
        "confidence": confidence,
        "label": label,
        "is_correct": _format_bool(direction == label),
    }
    for field in INTERNAL_FIELDS:
        row[field] = _clean_source_internal_field(field, source.get(field))
    return clean_json(row)


def _normalize_current_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "feature_month_id": row.get("feature_month_id"),
        "feature_date": row.get("feature_date"),
        "target_month_id": row.get("target_month_id"),
        "target_date": row.get("target_date"),
        "target_tenor": row.get("target_tenor"),
        "horizon": row.get("horizon"),
        "target_rule": row.get("target_rule"),
        "benchmark_role": PLATFORM_CURRENT_MONTHLY_ROLE,
        "direction": row.get("direction"),
        "confidence": row.get("confidence"),
        "label": row.get("label"),
        "is_correct": _format_bool(bool(row.get("is_correct"))),
    }
    for field in INTERNAL_FIELDS:
        out[field] = row.get(field)
    return clean_json(out)


def _call_runner(
    module_name: str,
    function_name: str,
    *,
    predict_dates: tuple[str, ...],
    engine: Any,
) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    run_func: Callable[..., dict[str, Any]] = getattr(module, function_name)
    return run_func(predict_dates=predict_dates, engine=engine, persist=False)


def _monthly_label(engine: Any, target_tenor: str, context: Any) -> int:
    actuals = build_monthly_actual_records(engine, tenors=[target_tenor], end_date=context.target_date)
    key = (target_tenor, context.scheduled_trigger_date, context.target_date, MONTHLY_TARGET_RULE)
    labels = {
        (record.tenor, record.predict_date, record.target_date, record.target_rule): record.direction_monthly
        for record in actuals
    }
    if key not in labels:
        raise RuntimeError(f"monthly actual label missing for key={key}")
    return int(labels[key])


def _select_frequency_row(source_rows: list[dict[str, Any]], frequency: str, scheme_id: str) -> dict[str, Any]:
    matches = [row for row in source_rows if str(row.get("frequency")) == frequency]
    if len(matches) != 1:
        raise RuntimeError(f"{scheme_id}: expected one source row for {frequency}, got {len(matches)}")
    return matches[0]


def _assert_source_context_matches(
    source: dict[str, Any],
    feature_month_id: str,
    target_month_id: str,
    scheme_id: str,
) -> None:
    source_feature = _str_or_none(source.get("feature_month_id"))
    source_target = _str_or_none(source.get("target_month_id"))
    if source_feature and source_feature != feature_month_id:
        raise RuntimeError(f"{scheme_id}: source feature_month_id expected {feature_month_id}, got {source_feature}")
    if source_target and source_target != target_month_id:
        raise RuntimeError(f"{scheme_id}: source target_month_id expected {target_month_id}, got {source_target}")


def _direction_from_y_pred(value: Any) -> int:
    parsed = _int_or_none(value)
    if parsed == 1:
        return 1
    if parsed == 0:
        return -1
    return 0


def _confidence_for_direction(source: dict[str, Any], direction: int) -> float | None:
    if direction > 0:
        return _float_or_none(source.get("pred_proba_up"))
    if direction < 0:
        return _float_or_none(source.get("pred_proba_down"))
    values = [_float_or_none(source.get("pred_proba_up")), _float_or_none(source.get("pred_proba_down"))]
    clean = [value for value in values if value is not None]
    return max(clean) if clean else None


def _required_internal_fields(scheme_id: str) -> list[str]:
    config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
    backtest = config.get("backtest") if isinstance(config.get("backtest"), dict) else {}
    raw = backtest.get("required_internal_fields") if isinstance(backtest, dict) else []
    if not isinstance(raw, list):
        return []
    return [str(field) for field in raw if str(field).strip()]


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
    scheme_id: str,
    rows: list[dict[str, Any]],
    required_internal_fields: list[str],
    label: str,
) -> None:
    required = set(BASE_FIELDNAMES) | set(required_internal_fields)
    for index, row in enumerate(rows, start=2):
        missing = [field for field in required if _is_blank(row.get(field))]
        if missing:
            raise RuntimeError(f"{scheme_id}: {label} row {index} missing fields {missing}")


def _strict_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_format_value(row.get(field)) for field in STRICT_KEY_FIELDS)


def _summary_for_export(
    spec: MonthlyBenchmarkSpec,
    rows: list[dict[str, Any]],
    *,
    source_role: str,
    generator: str,
    evidence: MonthlySourceEvidence,
    current_generator: str,
) -> dict[str, Any]:
    labeled = [row for row in rows if not _is_blank(row.get("label"))]
    correct = [row for row in labeled if str(row.get("is_correct")).lower() == "true"]
    row_count = len(rows)
    summary = {
        "status": "success",
        "scheme_id": spec.scheme_id,
        "benchmark_id": spec.benchmark_id,
        "data_source": spec.data_source,
        "rows": row_count,
        "row_count": row_count,
        "monthly_count": row_count,
        "labeled_rows": len(labeled),
        "correct": len(correct),
        "accuracy": (len(correct) / len(labeled)) if labeled else None,
        "target_rule": MONTHLY_TARGET_RULE,
        "original_benchmark_validation": {
            "benchmark_rows": row_count,
            "matched_rows": row_count,
            "strict_key": list(STRICT_KEY_FIELDS),
        },
        "benchmark_provenance": {
            "source_role": source_role,
            "generator": generator,
            "bootstrap_source": source_role,
            "source_family": "monthly_0629_binary_runner",
            "target_rule": MONTHLY_TARGET_RULE,
            "source_original_monthly_algorithm_rows": row_count,
            "platform_current_monthly_adapter_rows": row_count,
            "source_package_hash": evidence.source_package_hash,
            "source_package_path": str(evidence.source_package_path.relative_to(PROJECT_ROOT)),
            "source_frequency": evidence.frequency,
            "source_target_tenor": evidence.target_tenor,
            "source_final_select_id": evidence.final_select_id,
            "source_model_id": evidence.model_id,
            "source_candidate_id": evidence.candidate_id,
            "source_runner_module": evidence.runner_module,
            "manifest_path": str(evidence.manifest_path.relative_to(PROJECT_ROOT)),
            "current_generator": current_generator,
            "cross_frequency_reuse": False,
        },
    }
    return clean_json(summary)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _format_value(row.get(field)) for field in fieldnames})


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_scheme_ids(scheme_ids: Iterable[str]) -> list[str]:
    requested = list(scheme_ids) or sorted(SPECS)
    unknown = sorted(set(requested) - set(SPECS))
    if unknown:
        raise ValueError(f"unsupported monthly scheme_id: {unknown}")
    return requested


def _clean_source_value(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if value == "":
        return None
    parsed_int = _int_or_none(value)
    if parsed_int is not None and str(value).strip() in {str(parsed_int), f"{parsed_int}.0"}:
        return parsed_int
    parsed_float = _float_or_none(value)
    if parsed_float is not None and _looks_float(value):
        return parsed_float
    return value


def _clean_source_internal_field(field: str, value: Any) -> Any:
    if field in {"y_pred", "param_index", "training_rows", "validation_rows"}:
        return _int_or_none(value)
    if field in {"pred_proba_up", "pred_proba_down", "validation_overall_accuracy"}:
        return _float_or_none(value)
    return _clean_source_value(value)


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


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


def _looks_float(value: Any) -> bool:
    if not isinstance(value, str):
        return isinstance(value, float)
    return any(ch in value for ch in (".", "e", "E"))


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return _format_bool(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Rebuild strict monthly 0629 benchmark files.")
    parser.add_argument("--scheme-id", action="append", choices=sorted(SPECS), default=[])
    parser.add_argument("--predict-date", action="append", default=[])
    args = parser.parse_args(argv)
    payload = rebuild_monthly_0629_benchmarks(
        args.scheme_id,
        predict_dates=tuple(args.predict_date or DEFAULT_PREDICT_DATES),
    )
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
