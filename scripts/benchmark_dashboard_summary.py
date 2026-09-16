#!/usr/bin/env python3
"""用确定性合成事实测量 Dashboard Summary 的读取、聚合与编码成本。"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import gc
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Sequence

from sqlalchemy import create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard import (  # noqa: E402
    build_factor_lab_dashboard,
    dashboard_build_diagnostics,
    encode_canonical_snapshot,
)


DEFAULT_FACT_COUNTS = (10_000, 50_000, 100_001)
FACTS_PER_SCHEME = 500


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the current Dashboard Summary implementation",
    )
    parser.add_argument(
        "--rows",
        nargs="+",
        type=int,
        default=list(DEFAULT_FACT_COUNTS),
        help="synthetic prediction fact counts",
    )
    args = parser.parse_args(argv)
    if any(value <= 0 for value in args.rows):
        parser.error("--rows values must be positive")
    return args


def _create_schema(connection: Any) -> None:
    statements = (
        """CREATE TABLE t_scheme_registry (
            scheme_id TEXT PRIMARY KEY, base_scheme_id TEXT, runtime_type TEXT,
            name TEXT, owner TEXT, description TEXT, horizon INTEGER,
            task_type TEXT, frequency TEXT, target_tenor TEXT, status TEXT,
            deployed_at DATE
        )""",
        """CREATE TABLE t_target_registry (
            target_code TEXT, display_name TEXT, asset_class TEXT,
            target_type TEXT, sort_order INTEGER, status TEXT, extra TEXT
        )""",
        """CREATE TABLE t_scheme_predictions (
            id INTEGER, scheme_id TEXT, target_tenor TEXT, horizon INTEGER,
            predict_date DATE, feature_date DATE, target_date DATE,
            predicted_direction INTEGER, backtest_actual_direction INTEGER,
            extra TEXT
        )""",
        """CREATE TABLE t_scheme_actuals (
            tenor TEXT, trade_date DATE, direction_1d INTEGER,
            direction_5d INTEGER
        )""",
        """CREATE TABLE t_scheme_weekly_actuals (
            tenor TEXT, target_date DATE, target_rule TEXT,
            direction_weekly INTEGER
        )""",
        """CREATE TABLE t_scheme_monthly_actuals (
            tenor TEXT, target_date DATE, target_rule TEXT,
            direction_monthly INTEGER
        )""",
        """CREATE TABLE t_scheme_period_average_actuals (
            tenor TEXT, target_date DATE, target_rule TEXT,
            actual_direction INTEGER
        )""",
        """CREATE TABLE t_backtest_runs (
            id INTEGER, benchmark_id TEXT, scheme_id TEXT, data_source TEXT,
            start_date DATE, end_date DATE, status TEXT,
            created_at DATETIME, updated_at DATETIME
        )""",
    )
    for statement in statements:
        connection.execute(text(statement))
    connection.execute(
        text(
            "INSERT INTO t_target_registry VALUES "
            "('5Y','5年','bond','yield',1,'active','{}')"
        )
    )


def _load_fixture(engine: Any, fact_count: int) -> None:
    scheme_count = math.ceil(fact_count / FACTS_PER_SCHEME)
    registry_rows: list[dict[str, Any]] = []
    prediction_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    row_id = 1
    first_target = date(2025, 1, 1)
    for scheme_index in range(scheme_count):
        base_scheme_id = f"summary_benchmark_{scheme_index:04d}"
        registry_rows.append(
            {
                "scheme_id": f"{base_scheme_id}__h1__5Y",
                "base_scheme_id": base_scheme_id,
            }
        )
        run_rows.append({"id": scheme_index + 1, "scheme_id": base_scheme_id})
        rows_in_scheme = min(
            FACTS_PER_SCHEME,
            fact_count - scheme_index * FACTS_PER_SCHEME,
        )
        for offset in range(rows_in_scheme):
            target = first_target + timedelta(days=offset)
            prediction_rows.append(
                {
                    "id": row_id,
                    "scheme_id": base_scheme_id,
                    "predict_date": (target - timedelta(days=1)).isoformat(),
                    "feature_date": (target - timedelta(days=2)).isoformat(),
                    "target_date": target.isoformat(),
                    "direction": (-1, 0, 1)[row_id % 3],
                }
            )
            row_id += 1

    with engine.begin() as connection:
        _create_schema(connection)
        connection.execute(
            text(
                """INSERT INTO t_scheme_registry VALUES
                (:scheme_id,:base_scheme_id,'blackbox_v2',:base_scheme_id,
                 'benchmark','',1,'T+1','daily','5Y','active','2025-01-01')"""
            ),
            registry_rows,
        )
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (:id,:scheme_id,'5Y',1,:predict_date,:feature_date,
                 :target_date,:direction,:direction,'{}')"""
            ),
            prediction_rows,
        )
        connection.execute(
            text(
                """INSERT INTO t_backtest_runs VALUES
                (:id,'benchmark',:scheme_id,
                 'blackbox_v2_current_snapshot_as_of',
                 '2025-01-01','2026-05-15','success',
                 '2026-05-16','2026-05-16')"""
            ),
            run_rows,
        )


def _benchmark(fact_count: int) -> dict[str, Any]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        _load_fixture(engine, fact_count)
        gc.collect()
        build_started_at = time.perf_counter()
        payload = build_factor_lab_dashboard(engine)
        build_seconds = time.perf_counter() - build_started_at
        diagnostics = dashboard_build_diagnostics(payload["snapshot_id"])
        if diagnostics is None:
            raise RuntimeError("dashboard diagnostics are missing")
        encode_started_at = time.perf_counter()
        encoding = encode_canonical_snapshot(payload)
        encode_seconds = time.perf_counter() - encode_started_at
        gc.collect()
        tracemalloc.start()
        memory_probe_started_at = time.perf_counter()
        build_factor_lab_dashboard(engine)
        memory_probe_seconds = time.perf_counter() - memory_probe_started_at
        peak_bytes = tracemalloc.get_traced_memory()[1]
        return {
            "facts": fact_count,
            "db_read_seconds": round(float(diagnostics["db_read_seconds"]), 4),
            "canonical_build_seconds": round(
                float(diagnostics["canonical_build_seconds"]), 4
            ),
            "total_build_seconds": round(build_seconds, 4),
            "encode_seconds": round(encode_seconds, 4),
            "peak_allocated_mib": round(peak_bytes / (1024 * 1024), 2),
            "memory_probe_build_seconds": round(memory_probe_seconds, 4),
            "raw_bytes": encoding.raw_size,
            "gzip_bytes": encoding.gzip_size,
            "prediction_source_rows": diagnostics[
                "prediction_source_row_count"
            ],
        }
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    previous_runtime_root = os.environ.get("BFL_RUNTIME_ROOT")
    try:
        with tempfile.TemporaryDirectory(prefix="bfl-summary-benchmark-") as root:
            config_path = Path(root) / "config" / "production_schemes.json"
            config_path.parent.mkdir()
            config_path.write_text("{}", encoding="utf-8")
            os.environ["BFL_RUNTIME_ROOT"] = root
            for fact_count in args.rows:
                print(json.dumps(_benchmark(fact_count), sort_keys=True))
    finally:
        if previous_runtime_root is None:
            os.environ.pop("BFL_RUNTIME_ROOT", None)
        else:
            os.environ["BFL_RUNTIME_ROOT"] = previous_runtime_root
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
