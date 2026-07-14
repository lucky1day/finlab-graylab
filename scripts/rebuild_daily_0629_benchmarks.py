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

from backtests.daily_0629_reproduction import (
    BENCHMARK_ID,
    DATA_SOURCE,
    DEFAULT_GRAY_START_DATE,
    DEFAULT_SOURCE_RUN_DATE,
    DEFAULT_START_DATE,
    SOURCE_ORIGINAL_DATA_SOURCE,
    _summary,
    build_daily_0629_rows,
)
from backtests.repository import clean_json
from harness.config_loader import load_config_raw
from scheduler.repository import create_engine_from_env
from shared.daily_0629_predict_adapter import DAILY_0629_INTERNAL_FIELDS
from shared.daily_0629_source_evidence import DAILY_0629_SOURCE_ROLE, PLATFORM_CURRENT_DAILY_0629_ROLE


BASE_FIELDNAMES = [
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
    "direction",
    "confidence",
    "label",
    "is_correct",
]
STRICT_KEY_FIELDS = ("feature_date", "target_date", "target_tenor", "horizon")


@dataclass(frozen=True)
class Daily0629BenchmarkSpec:
    scheme_id: str
    target_tenor: str
    current_runner_module: str
    current_runner_function: str
    benchmark_id: str = BENCHMARK_ID


SPECS: dict[str, Daily0629BenchmarkSpec] = {
    "daily_1y_xgb_1y13_0629": Daily0629BenchmarkSpec(
        scheme_id="daily_1y_xgb_1y13_0629",
        target_tenor="1Y",
        current_runner_module="backtests.daily_1y_xgb_1y13_0629_reproduction",
        current_runner_function="run_daily_1y_xgb_1y13_0629_reproduction",
    ),
    "daily_5y_lgbm_5y10_0629": Daily0629BenchmarkSpec(
        scheme_id="daily_5y_lgbm_5y10_0629",
        target_tenor="5Y",
        current_runner_module="backtests.daily_5y_lgbm_5y10_0629_reproduction",
        current_runner_function="run_daily_5y_lgbm_5y10_0629_reproduction",
    ),
    "daily_10y_lgbm_10y04_0629": Daily0629BenchmarkSpec(
        scheme_id="daily_10y_lgbm_10y04_0629",
        target_tenor="10Y",
        current_runner_module="backtests.daily_10y_lgbm_10y04_0629_reproduction",
        current_runner_function="run_daily_10y_lgbm_10y04_0629_reproduction",
    ),
}


def rebuild_daily_0629_benchmarks(
    scheme_ids: Iterable[str],
    *,
    source_run_date: str = DEFAULT_SOURCE_RUN_DATE,
    start_date: str = DEFAULT_START_DATE,
    gray_start_date: str = DEFAULT_GRAY_START_DATE,
) -> dict[str, Any]:
    requested = _normalize_scheme_ids(scheme_ids)
    engine = create_engine_from_env()
    try:
        results = [
            rebuild_one(
                SPECS[scheme_id],
                engine=engine,
                source_run_date=source_run_date,
                start_date=start_date,
                gray_start_date=gray_start_date,
            )
            for scheme_id in requested
        ]
    finally:
        engine.dispose()
    return {"schemes": results}


def rebuild_one(
    spec: Daily0629BenchmarkSpec,
    *,
    engine: Any,
    source_run_date: str,
    start_date: str,
    gray_start_date: str,
) -> dict[str, Any]:
    original_rows = build_daily_0629_rows(
        spec.scheme_id,
        source_run_date=source_run_date,
        start_date=start_date,
        gray_start_date=gray_start_date,
        engine=engine,
        benchmark_role=DAILY_0629_SOURCE_ROLE,
        data_source=SOURCE_ORIGINAL_DATA_SOURCE,
    )
    current_payload = _call_runner(
        spec.current_runner_module,
        spec.current_runner_function,
        source_run_date=source_run_date,
        start_date=start_date,
        gray_start_date=gray_start_date,
        engine=engine,
    )
    current_rows = list(current_payload.get("rows") or [])
    _assert_same_keys(spec.scheme_id, original_rows, current_rows)
    required_internal_fields = _required_internal_fields(spec.scheme_id)
    _assert_required_fields(spec.scheme_id, original_rows, required_internal_fields, "original")
    _assert_required_fields(spec.scheme_id, current_rows, required_internal_fields, "current")

    bench_dir = PROJECT_ROOT / "schemes" / spec.scheme_id / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = BASE_FIELDNAMES + list(DAILY_0629_INTERNAL_FIELDS)
    _write_csv(bench_dir / "original_predictions_sample.csv", fieldnames, original_rows)
    _write_csv(bench_dir / "current_predictions_sample.csv", fieldnames, current_rows)

    original_summary = _summary(
        spec.scheme_id,
        original_rows,
        source_role=DAILY_0629_SOURCE_ROLE,
        data_source=SOURCE_ORIGINAL_DATA_SOURCE,
        source_run_date=source_run_date,
    )
    current_summary = _summary(
        spec.scheme_id,
        current_rows,
        source_role=PLATFORM_CURRENT_DAILY_0629_ROLE,
        data_source=DATA_SOURCE,
        source_run_date=source_run_date,
    )
    _write_json(bench_dir / "original_backtest_summary.json", original_summary)
    _write_json(bench_dir / "current_backtest_summary.json", current_summary)
    return {
        "scheme_id": spec.scheme_id,
        "benchmark_dir": str(bench_dir),
        "rows": len(current_rows),
        "source_original_daily_0629_algorithm_rows": len(original_rows),
        "platform_current_daily_0629_adapter_rows": len(current_rows),
    }


def _call_runner(
    module_name: str,
    function_name: str,
    *,
    source_run_date: str,
    start_date: str,
    gray_start_date: str,
    engine: Any,
) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    run_func: Callable[..., dict[str, Any]] = getattr(module, function_name)
    return run_func(
        source_run_date=source_run_date,
        start_date=start_date,
        gray_start_date=gray_start_date,
        engine=engine,
        persist=False,
    )


def _normalize_scheme_ids(scheme_ids: Iterable[str]) -> tuple[str, ...]:
    requested = tuple(scheme_ids)
    if not requested:
        return tuple(SPECS)
    unknown = sorted(set(requested) - set(SPECS))
    if unknown:
        raise ValueError(f"unknown daily 0629 scheme ids: {unknown}")
    return requested


def _required_internal_fields(scheme_id: str) -> list[str]:
    config = load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")
    backtest = config.get("backtest")
    raw = backtest.get("required_internal_fields") if isinstance(backtest, dict) else []
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{scheme_id}: backtest.required_internal_fields must be a non-empty list")
    return [str(field) for field in raw]


def _assert_same_keys(scheme_id: str, original_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> None:
    original = {_key(row) for row in original_rows}
    current = {_key(row) for row in current_rows}
    if original != current:
        raise RuntimeError(
            f"{scheme_id}: benchmark key mismatch: "
            f"missing={sorted(original - current)[:5]}, extra={sorted(current - original)[:5]}"
        )


def _assert_required_fields(
    scheme_id: str,
    rows: list[dict[str, Any]],
    required_internal_fields: list[str],
    role: str,
) -> None:
    for idx, row in enumerate(rows):
        for field in BASE_FIELDNAMES + required_internal_fields:
            value = row.get(field)
            if value is None or value == "":
                raise RuntimeError(f"{scheme_id}: {role} row {idx} missing required field {field}")


def _key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "") for field in STRICT_KEY_FIELDS)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return clean_json(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Rebuild strict daily 0629 benchmark files.")
    parser.add_argument("--scheme-id", action="append", choices=sorted(SPECS), help="Limit to one scheme")
    parser.add_argument("--source-run-date", default=DEFAULT_SOURCE_RUN_DATE)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--gray-start-date", default=DEFAULT_GRAY_START_DATE)
    args = parser.parse_args(argv)
    payload = rebuild_daily_0629_benchmarks(
        args.scheme_id or (),
        source_run_date=args.source_run_date,
        start_date=args.start_date,
        gray_start_date=args.gray_start_date,
    )
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
