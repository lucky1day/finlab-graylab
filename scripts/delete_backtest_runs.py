from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env


def delete_backtest_run(
    engine: Engine,
    *,
    scheme_id: str,
    run_id: int,
    benchmark_id: str | None = None,
    data_source: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """受控删除单个 backtest run 及其 run-scoped 子表记录。"""
    run = _load_run(engine, run_id)
    if run is None:
        raise SystemExit(f"backtest run not found: {run_id}")
    if run["scheme_id"] != scheme_id:
        raise SystemExit(f"scheme_id mismatch: run has {run['scheme_id']!r}, requested {scheme_id!r}")
    if benchmark_id is not None and run["benchmark_id"] != benchmark_id:
        raise SystemExit(
            f"benchmark_id mismatch: run has {run['benchmark_id']!r}, requested {benchmark_id!r}"
        )
    if data_source is not None and run["data_source"] != data_source:
        raise SystemExit(f"data_source mismatch: run has {run['data_source']!r}, requested {data_source!r}")

    reproduction_check_column = _reproduction_check_run_column(engine)
    counts = {
        "t_backtest_monthly_metrics": _count_by_run_id(engine, "t_backtest_monthly_metrics", run["id"]),
        "t_backtest_predictions": _count_by_run_id(engine, "t_backtest_predictions", run["id"]),
        "t_backtest_reproduction_checks": _count_reproduction_checks(engine, run, reproduction_check_column),
        "t_backtest_runs": 1,
    }
    if apply:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM t_backtest_monthly_metrics WHERE run_id = :run_id"), {"run_id": run["id"]})
            conn.execute(text("DELETE FROM t_backtest_predictions WHERE run_id = :run_id"), {"run_id": run["id"]})
            _delete_reproduction_checks(conn, run, reproduction_check_column)
            conn.execute(text("DELETE FROM t_backtest_runs WHERE id = :run_id"), {"run_id": run["id"]})

    return {"applied": bool(apply), "run": run, "counts": counts}


def _load_run(engine: Engine, run_id: int) -> dict[str, Any] | None:
    sql = text(
        """
        SELECT id, backtest_run_id, benchmark_id, scheme_id, data_source,
               start_date, end_date, status
        FROM t_backtest_runs
        WHERE id = :run_id OR backtest_run_id = :run_id
        ORDER BY id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"run_id": run_id}).mappings().first()
    return dict(row) if row else None


def _count_by_run_id(engine: Engine, table: str, run_id: int) -> int:
    if not _table_exists(engine, table):
        return 0
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE run_id = :run_id"), {"run_id": run_id}).scalar_one())


def _reproduction_check_run_column(engine: Engine) -> str | None:
    if not _table_exists(engine, "t_backtest_reproduction_checks"):
        return None
    columns = {column["name"] for column in inspect(engine).get_columns("t_backtest_reproduction_checks")}
    if "run_id" in columns:
        return "run_id"
    if "backtest_run_id" in columns:
        return "backtest_run_id"
    return None


def _count_reproduction_checks(engine: Engine, run: dict[str, Any], run_column: str | None) -> int:
    if run_column == "run_id":
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM t_backtest_reproduction_checks WHERE run_id = :run_id"), {"run_id": run["id"]}).scalar_one())
    if run_column == "backtest_run_id":
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM t_backtest_reproduction_checks WHERE backtest_run_id = :run_id"),
                    {"run_id": run["backtest_run_id"] or run["id"]},
                ).scalar_one()
            )
    return 0


def _delete_reproduction_checks(conn: Any, run: dict[str, Any], run_column: str | None) -> None:
    if run_column == "run_id":
        conn.execute(text("DELETE FROM t_backtest_reproduction_checks WHERE run_id = :run_id"), {"run_id": run["id"]})
    elif run_column == "backtest_run_id":
        conn.execute(
            text("DELETE FROM t_backtest_reproduction_checks WHERE backtest_run_id = :run_id"),
            {"run_id": run["backtest_run_id"] or run["id"]},
        )


def _table_exists(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Delete one explicit backtest run and its run-scoped children.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--benchmark-id")
    parser.add_argument("--data-source")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印命中行数；默认行为")
    mode.add_argument("--apply", action="store_true", help="真正执行 DELETE")
    args = parser.parse_args(argv)

    engine = create_engine_from_env()
    try:
        summary = delete_backtest_run(
            engine,
            scheme_id=args.scheme_id,
            run_id=args.run_id,
            benchmark_id=args.benchmark_id,
            data_source=args.data_source,
            apply=args.apply,
        )
        run = summary["run"]
        print(
            f"selected run_id={run['id']} benchmark_id={run['benchmark_id']} "
            f"scheme_id={run['scheme_id']} data_source={run['data_source']} "
            f"start_date={run['start_date']} end_date={run['end_date']}"
        )
        for table, count in summary["counts"].items():
            print(f"{table}={count}")
        print(f"applied={str(summary['applied']).lower()}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
