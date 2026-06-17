from __future__ import annotations

import argparse
import csv
import importlib
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
STRICT_WEEKLY_FIELDS = (
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "direction",
    "confidence",
    "label",
    "is_correct",
    "feature_week_id",
    "target_week_id",
)
SCHEME_RUNNERS = {
    "weekly_5y_direct_0529": {
        "module": "backtests.weekly_5y_direct_0529_reproduction",
        "function": "run_weekly_5y_direct_0529_reproduction",
        "rows": 71,
    },
    "weekly_7y_cross_d_overlay_0529": {
        "module": "backtests.weekly_7y_cross_d_overlay_0529_reproduction",
        "function": "run_weekly_7y_cross_d_overlay_0529_reproduction",
        "rows": 42,
    },
    "weekly_10y_d_overlay_0529": {
        "module": "backtests.weekly_10y_d_overlay_0529_reproduction",
        "function": "run_weekly_10y_d_overlay_0529_reproduction",
        "rows": 45,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize weekly scheme benchmark CSV files to strict schema.")
    parser.add_argument(
        "--scheme-id",
        action="append",
        choices=sorted(SCHEME_RUNNERS),
        help="Scheme to normalize; defaults to all weekly 0529 benchmark schemes.",
    )
    args = parser.parse_args()

    scheme_ids = args.scheme_id or sorted(SCHEME_RUNNERS)
    for scheme_id in scheme_ids:
        summary = normalize_scheme(scheme_id)
        print(f"{scheme_id}: normalized {summary['rows']} rows in original/current benchmark CSV")
    return 0


def normalize_scheme(scheme_id: str) -> dict[str, Any]:
    spec = SCHEME_RUNNERS[scheme_id]
    module = importlib.import_module(str(spec["module"]))
    run_func = getattr(module, str(spec["function"]))
    result = run_func(persist=False)
    generated = result.get("rows") or []
    expected_rows = int(spec["rows"])
    if len(generated) < expected_rows:
        raise RuntimeError(
            f"{scheme_id}: generated {len(generated)} rows, fewer than benchmark rows {expected_rows}"
        )

    generated_by_week = {_require_week_id(row, scheme_id): row for row in generated}
    bench_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
    for name in ("original_predictions_sample.csv", "current_predictions_sample.csv"):
        path = bench_dir / name
        old_rows = _read_csv(path)
        if len(old_rows) != expected_rows:
            raise RuntimeError(f"{scheme_id}: {name} has {len(old_rows)} rows, expected {expected_rows}")
        normalized = [_normalize_row(scheme_id, name, old, generated_by_week) for old in old_rows]
        _write_csv(path, normalized)
    return {"rows": expected_rows}


def _normalize_row(
    scheme_id: str,
    filename: str,
    old: dict[str, str],
    generated_by_week: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    week_id = _int_value(old.get("feature_week_id"), f"{scheme_id} {filename}: missing feature_week_id")
    generated = generated_by_week.get(week_id)
    if generated is None:
        raise RuntimeError(f"{scheme_id} {filename}: missing generated row for feature_week_id={week_id}")

    _assert_optional_equal(
        _first_present(old, "target_date", "framework_target_date"),
        generated.get("target_date"),
        f"{scheme_id} {filename}: target_date mismatch feature_week_id={week_id}",
    )
    _assert_optional_equal(
        _first_present(old, "target_tenor", "tenor"),
        generated.get("target_tenor"),
        f"{scheme_id} {filename}: target_tenor mismatch feature_week_id={week_id}",
    )
    _assert_optional_equal(
        old.get("direction"),
        generated.get("direction"),
        f"{scheme_id} {filename}: direction mismatch feature_week_id={week_id}",
    )
    _assert_optional_equal(
        old.get("label"),
        generated.get("label"),
        f"{scheme_id} {filename}: label mismatch feature_week_id={week_id}",
    )

    return {
        "feature_date": generated["feature_date"],
        "target_date": generated["target_date"],
        "target_tenor": generated["target_tenor"],
        "horizon": generated["horizon"],
        "direction": old.get("direction") or generated["direction"],
        "confidence": old.get("confidence") or generated["confidence"],
        "label": old.get("label") or generated["label"],
        "is_correct": old.get("is_correct") or generated["is_correct"],
        "feature_week_id": week_id,
        "target_week_id": generated["target_week_id"],
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(STRICT_WEEKLY_FIELDS))
        writer.writeheader()
        writer.writerows(rows)


def _require_week_id(row: dict[str, Any], scheme_id: str) -> int:
    value = _int_value(row.get("feature_week_id"), f"{scheme_id}: generated row missing feature_week_id")
    return value


def _int_value(value: Any, message: str) -> int:
    if value in (None, ""):
        raise RuntimeError(message)
    return int(value)


def _first_present(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _assert_optional_equal(left: Any, right: Any, message: str) -> None:
    if left in (None, ""):
        return
    if str(left) != str(right):
        raise RuntimeError(f"{message}: old={left!r} generated={right!r}")


if __name__ == "__main__":
    raise SystemExit(main())
