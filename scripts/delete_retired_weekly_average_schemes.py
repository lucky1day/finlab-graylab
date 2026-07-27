from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Connection, Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env


RETIRED_SCHEME_SPECS = {
    "weekly_avg_5y_direct_0529": "5Y",
    "weekly_avg_7y_cross_d_overlay_0529": "7Y",
    "weekly_avg_10y_d_overlay_0529": "10Y",
}

PROTECTED_REFERENCES = {
    "t_input_artifacts": "scheme_id",
    "t_schedule_item_targets": "base_scheme_id",
    "t_schedule_items": "base_scheme_id",
    "t_scheme_serving_pointer": "scheme_id",
}

DIRECT_SCHEME_REFERENCES = {
    "t_backtest_predictions": "scheme_id",
    "t_backtest_runs": "scheme_id",
    "t_harness_runs": "scheme_id",
    "t_scheme_predictions": "scheme_id",
    "t_scheme_registry": "base_scheme_id",
    "t_scheme_run_log": "scheme_id",
    "t_scheme_runs": "scheme_id",
    "t_scheme_versions": "scheme_id",
}


def delete_retired_weekly_average_schemes(
    engine: Engine,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """精确删除三个已被替代的 point-backed 周平均身份及其数据闭包。"""
    context = engine.begin() if apply else engine.connect()
    with context as conn:
        registry_rows = _registry_rows(conn)
        if not registry_rows:
            residual = _direct_reference_counts(conn)
            if any(residual.values()):
                raise RuntimeError(
                    "retired Registry identities are absent but residual rows remain: "
                    f"{_nonzero_counts(residual)}"
                )
            return {
                "applied": bool(apply),
                "already_absent": True,
                "scheme_ids": sorted(RETIRED_SCHEME_SPECS),
                "counts": residual,
            }

        _require_exact_paused_registry_rows(registry_rows)
        protected = _protected_reference_counts(conn)
        if any(protected.values()):
            raise RuntimeError(
                "retired schemes still have protected references: "
                f"{_nonzero_counts(protected)}"
            )
        _require_no_unknown_scheme_references(conn)

        harness_run_ids = _column_values(
            conn,
            "t_harness_runs",
            "harness_run_id",
            scheme_column="scheme_id",
        )
        backtest_run_ids = _column_values(
            conn,
            "t_backtest_runs",
            "id",
            scheme_column="scheme_id",
        )
        counts = _deletion_counts(
            conn,
            harness_run_ids=harness_run_ids,
            backtest_run_ids=backtest_run_ids,
        )

        if apply:
            _delete_rows(
                conn,
                harness_run_ids=harness_run_ids,
                backtest_run_ids=backtest_run_ids,
            )
            residual = _direct_reference_counts(conn)
            if any(residual.values()):
                raise RuntimeError(
                    "retired scheme deletion left residual rows: "
                    f"{_nonzero_counts(residual)}"
                )

        return {
            "applied": bool(apply),
            "already_absent": False,
            "scheme_ids": sorted(RETIRED_SCHEME_SPECS),
            "counts": counts,
        }


def _registry_rows(conn: Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "t_scheme_registry"):
        raise RuntimeError("t_scheme_registry is missing")
    statement = _with_ids(
        """
        SELECT scheme_id, base_scheme_id, runtime_type, status, task_type,
               target_tenor, horizon
        FROM t_scheme_registry
        WHERE base_scheme_id IN :scheme_ids
        ORDER BY base_scheme_id
        """,
        "scheme_ids",
    )
    return [
        dict(row)
        for row in conn.execute(
            statement,
            {"scheme_ids": sorted(RETIRED_SCHEME_SPECS)},
        ).mappings()
    ]


def _require_exact_paused_registry_rows(
    rows: list[dict[str, Any]],
) -> None:
    expected_ids = set(RETIRED_SCHEME_SPECS)
    actual_ids = {str(row["base_scheme_id"]) for row in rows}
    valid = len(rows) == len(expected_ids) and actual_ids == expected_ids
    for row in rows:
        scheme_id = str(row["base_scheme_id"])
        tenor = RETIRED_SCHEME_SPECS.get(scheme_id)
        expected_registry_id = (
            f"{scheme_id}__h6__{tenor}" if tenor is not None else None
        )
        valid = valid and (
            row["scheme_id"] == expected_registry_id
            and row["runtime_type"] == "native_adapter"
            and row["status"] == "paused"
            and row["task_type"] == "weekly_average"
            and row["target_tenor"] == tenor
            and int(row["horizon"]) == 6
        )
    if not valid:
        raise RuntimeError(
            "retired Registry identities must be exact paused "
            "weekly_average rows"
        )


def _protected_reference_counts(conn: Connection) -> dict[str, int]:
    return {
        table: _count_scheme_rows(conn, table, column)
        for table, column in PROTECTED_REFERENCES.items()
        if _table_exists(conn, table)
    }


def _direct_reference_counts(conn: Connection) -> dict[str, int]:
    return {
        table: _count_scheme_rows(conn, table, column)
        for table, column in DIRECT_SCHEME_REFERENCES.items()
        if _table_exists(conn, table)
    }


def _require_no_unknown_scheme_references(conn: Connection) -> None:
    known = {
        (table, column)
        for table, column in {
            **DIRECT_SCHEME_REFERENCES,
            **PROTECTED_REFERENCES,
            "t_backtest_monthly_metrics": "scheme_id",
        }.items()
    }
    db_inspector = inspect(conn)
    unknown: dict[str, int] = {}
    for table in db_inspector.get_table_names():
        columns = {
            str(column["name"])
            for column in db_inspector.get_columns(table)
        }
        for column in ("scheme_id", "base_scheme_id"):
            if column not in columns or (table, column) in known:
                continue
            count = _count_scheme_rows(conn, table, column)
            if count:
                unknown[f"{table}.{column}"] = count
    if unknown:
        raise RuntimeError(
            "retired schemes have unknown direct references: "
            f"{unknown}"
        )


def _deletion_counts(
    conn: Connection,
    *,
    harness_run_ids: list[Any],
    backtest_run_ids: list[Any],
) -> dict[str, int]:
    counts = {
        "t_backtest_monthly_metrics": _count_by_values(
            conn,
            "t_backtest_monthly_metrics",
            "run_id",
            backtest_run_ids,
        ),
        "t_backtest_predictions": _count_by_values(
            conn,
            "t_backtest_predictions",
            "run_id",
            backtest_run_ids,
        ),
        "t_backtest_runs": len(backtest_run_ids),
        "t_harness_gate_results": _count_by_values(
            conn,
            "t_harness_gate_results",
            "harness_run_id",
            harness_run_ids,
        ),
        "t_harness_runs": len(harness_run_ids),
        "t_scheme_predictions": _count_scheme_rows(
            conn,
            "t_scheme_predictions",
            "scheme_id",
        ),
        "t_scheme_registry": _count_scheme_rows(
            conn,
            "t_scheme_registry",
            "base_scheme_id",
        ),
        "t_scheme_run_log": _count_scheme_rows(
            conn,
            "t_scheme_run_log",
            "scheme_id",
        ),
        "t_scheme_runs": _count_scheme_rows(
            conn,
            "t_scheme_runs",
            "scheme_id",
        ),
        "t_scheme_versions": _count_scheme_rows(
            conn,
            "t_scheme_versions",
            "scheme_id",
        ),
    }
    reproduction_column = _reproduction_check_run_column(conn)
    if reproduction_column is not None:
        counts["t_backtest_reproduction_checks"] = _count_by_values(
            conn,
            "t_backtest_reproduction_checks",
            reproduction_column,
            backtest_run_ids,
        )
    return {
        table: count
        for table, count in sorted(counts.items())
        if _table_exists(conn, table)
    }


def _delete_rows(
    conn: Connection,
    *,
    harness_run_ids: list[Any],
    backtest_run_ids: list[Any],
) -> None:
    _delete_by_values(
        conn,
        "t_backtest_monthly_metrics",
        "run_id",
        backtest_run_ids,
    )
    _delete_by_values(
        conn,
        "t_backtest_predictions",
        "run_id",
        backtest_run_ids,
    )
    reproduction_column = _reproduction_check_run_column(conn)
    if reproduction_column is not None:
        _delete_by_values(
            conn,
            "t_backtest_reproduction_checks",
            reproduction_column,
            backtest_run_ids,
        )
    _delete_scheme_rows(conn, "t_backtest_runs", "scheme_id")
    _delete_scheme_rows(conn, "t_scheme_predictions", "scheme_id")
    _delete_scheme_rows(conn, "t_scheme_run_log", "scheme_id")
    _delete_scheme_rows(conn, "t_scheme_runs", "scheme_id")
    _delete_by_values(
        conn,
        "t_harness_gate_results",
        "harness_run_id",
        harness_run_ids,
    )
    _delete_scheme_rows(conn, "t_harness_runs", "scheme_id")
    _delete_scheme_rows(conn, "t_scheme_registry", "base_scheme_id")
    _delete_scheme_rows(conn, "t_scheme_versions", "scheme_id")


def _column_values(
    conn: Connection,
    table: str,
    value_column: str,
    *,
    scheme_column: str,
) -> list[Any]:
    if not _table_exists(conn, table):
        return []
    statement = _with_ids(
        f"""
        SELECT {value_column}
        FROM {table}
        WHERE {scheme_column} IN :scheme_ids
        ORDER BY {value_column}
        """,
        "scheme_ids",
    )
    return list(
        conn.execute(
            statement,
            {"scheme_ids": sorted(RETIRED_SCHEME_SPECS)},
        ).scalars()
    )


def _count_scheme_rows(
    conn: Connection,
    table: str,
    column: str,
) -> int:
    if not _table_exists(conn, table):
        return 0
    statement = _with_ids(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN :scheme_ids",
        "scheme_ids",
    )
    return int(
        conn.execute(
            statement,
            {"scheme_ids": sorted(RETIRED_SCHEME_SPECS)},
        ).scalar_one()
    )


def _count_by_values(
    conn: Connection,
    table: str,
    column: str,
    values: Iterable[Any],
) -> int:
    values_list = list(values)
    if not values_list or not _table_exists(conn, table):
        return 0
    statement = _with_ids(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN :values",
        "values",
    )
    return int(
        conn.execute(statement, {"values": values_list}).scalar_one()
    )


def _delete_scheme_rows(
    conn: Connection,
    table: str,
    column: str,
) -> None:
    if not _table_exists(conn, table):
        return
    statement = _with_ids(
        f"DELETE FROM {table} WHERE {column} IN :scheme_ids",
        "scheme_ids",
    )
    conn.execute(
        statement,
        {"scheme_ids": sorted(RETIRED_SCHEME_SPECS)},
    )


def _delete_by_values(
    conn: Connection,
    table: str,
    column: str,
    values: Iterable[Any],
) -> None:
    values_list = list(values)
    if not values_list or not _table_exists(conn, table):
        return
    statement = _with_ids(
        f"DELETE FROM {table} WHERE {column} IN :values",
        "values",
    )
    conn.execute(statement, {"values": values_list})


def _reproduction_check_run_column(conn: Connection) -> str | None:
    table = "t_backtest_reproduction_checks"
    if not _table_exists(conn, table):
        return None
    columns = {
        str(column["name"])
        for column in inspect(conn).get_columns(table)
    }
    if "run_id" in columns:
        return "run_id"
    if "backtest_run_id" in columns:
        return "backtest_run_id"
    return None


def _table_exists(conn: Connection, table: str) -> bool:
    return inspect(conn).has_table(table)


def _with_ids(sql: str, parameter: str):
    return text(sql).bindparams(bindparam(parameter, expanding=True))


def _nonzero_counts(counts: dict[str, int]) -> dict[str, int]:
    return {
        table: count
        for table, count in counts.items()
        if count
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the exact three superseded point-backed weekly-average "
            "schemes and their database closure."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印精确命中行数；默认行为",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="在单事务中真正执行删除",
    )
    args = parser.parse_args(argv)

    engine = create_engine_from_env()
    try:
        summary = delete_retired_weekly_average_schemes(
            engine,
            apply=args.apply,
        )
        print("scheme_ids=" + ",".join(summary["scheme_ids"]))
        for table, count in summary["counts"].items():
            print(f"{table}={count}")
        print(f"already_absent={str(summary['already_absent']).lower()}")
        print(f"applied={str(summary['applied']).lower()}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
