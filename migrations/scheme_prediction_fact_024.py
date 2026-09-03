from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import text


EXPECTED_LIVE_VERSION = "e257093fb4f3"
EXPECTED_LEGACY_RUN_IDS = (15, 16, 17)


_SELECTED_BACKTEST_CTE = """
WITH active_runtime AS (
    SELECT base_scheme_id, MIN(runtime_type) AS runtime_type
    FROM t_scheme_registry
    WHERE status = 'active'
    GROUP BY base_scheme_id
    HAVING COUNT(DISTINCT runtime_type) = 1
),
ranked_benchmark AS (
    SELECT r.*,
           ROW_NUMBER() OVER (
               PARTITION BY r.benchmark_id, r.scheme_id, r.data_source
               ORDER BY r.updated_at DESC, r.id DESC
           ) AS benchmark_rank
    FROM t_backtest_runs r
    JOIN active_runtime a ON a.base_scheme_id = r.scheme_id
    WHERE r.status = 'success'
      AND r.data_source = CASE a.runtime_type
          WHEN 'native_adapter' THEN 'framework_db_aligned'
          WHEN 'blackbox_v2' THEN 'blackbox_v2_current_snapshot_as_of'
      END
),
selected_runs AS (
    SELECT b.*,
           ROW_NUMBER() OVER (
               PARTITION BY b.scheme_id
               ORDER BY b.updated_at DESC, b.id DESC
           ) AS base_rank
    FROM ranked_benchmark b
    WHERE b.benchmark_rank = 1
),
selected_points AS (
    SELECT p.run_id AS backtest_run_id, p.scheme_id,
           r.scheme_id AS parent_scheme_id, p.target_tenor,
           p.horizon, p.predict_date, p.feature_date, p.target_date,
           p.predicted_direction, p.label
    FROM selected_runs r
    JOIN t_backtest_predictions p ON p.run_id = r.id
    JOIN t_scheme_registry registry
      ON registry.status = 'active'
     AND registry.base_scheme_id = r.scheme_id
     AND registry.target_tenor = p.target_tenor
     AND registry.horizon = p.horizon
    WHERE r.base_rank = 1
      AND p.predict_date >= '2025-01-01'
)
"""


def read_scheme_prediction_fact_state(connection: Any) -> dict[str, object]:
    """读取 024 触及的 schema 与 canonical 数据不变量。"""
    column_rows = connection.execute(
        text(
            """
            SELECT column_name AS column_name,
                   column_type AS column_type,
                   is_nullable AS is_nullable,
                   column_default AS column_default,
                   extra AS extra,
                   ordinal_position AS ordinal_position
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_predictions'
              AND column_name IN (
                  'run_id', 'backtest_run_id', 'prediction_phase',
                  'backtest_actual_direction', 'updated_at', 'created_at'
              )
            ORDER BY ordinal_position
            """
        )
    ).mappings().all()
    columns = {
        str(row["column_name"]): {
            "column_type": str(row["column_type"]).lower(),
            "nullable": str(row["is_nullable"]).upper(),
            "default": row["column_default"],
            "extra": str(row["extra"] or "").lower(),
            "ordinal": int(row["ordinal_position"]),
        }
        for row in column_rows
    }
    index_rows = connection.execute(
        text(
            """
            SELECT index_name AS index_name,
                   non_unique AS non_unique,
                   seq_in_index AS seq_in_index,
                   column_name AS column_name
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_predictions'
              AND index_name IN (
                  'idx_scheme_predictions_phase',
                  'idx_scheme_predictions_backtest_run'
              )
            ORDER BY index_name, seq_in_index
            """
        )
    ).mappings().all()
    indexes: dict[str, list[str]] = {}
    for row in index_rows:
        indexes.setdefault(str(row["index_name"]), []).append(
            str(row["column_name"])
        )
    fk_rows = connection.execute(
        text(
            """
            SELECT k.constraint_name AS constraint_name,
                   k.column_name AS column_name,
                   k.referenced_table_name AS referenced_table_name,
                   k.referenced_column_name AS referenced_column_name,
                   r.delete_rule AS delete_rule,
                   r.update_rule AS update_rule
            FROM information_schema.key_column_usage k
            JOIN information_schema.referential_constraints r
              ON r.constraint_schema = k.constraint_schema
             AND r.table_name = k.table_name
             AND r.constraint_name = k.constraint_name
            WHERE k.constraint_schema = DATABASE()
              AND k.table_name = 't_scheme_predictions'
              AND k.constraint_name IN (
                  'fk_scheme_predictions_run',
                  'fk_scheme_predictions_backtest_run'
              )
            ORDER BY k.constraint_name
            """
        )
    ).mappings().all()
    foreign_keys = {
        str(row["constraint_name"]): {
            "column": str(row["column_name"]),
            "referenced_table": str(row["referenced_table_name"]),
            "referenced_column": str(row["referenced_column_name"]),
            "delete_rule": str(row["delete_rule"]).upper(),
            "update_rule": str(row["update_rule"]).upper(),
        }
        for row in fk_rows
    }
    check_rows = connection.execute(
        text(
            """
            SELECT t.constraint_name AS constraint_name,
                   t.enforced AS enforced,
                   c.check_clause AS check_clause
            FROM information_schema.table_constraints t
            JOIN information_schema.check_constraints c
              ON c.constraint_schema = t.constraint_schema
             AND c.constraint_name = t.constraint_name
            WHERE t.constraint_schema = DATABASE()
              AND t.table_name = 't_scheme_predictions'
              AND t.constraint_name = 'ck_scheme_predictions_source'
            """
        )
    ).mappings().all()
    checks = {
        str(row["constraint_name"]): {
            "enforced": str(row["enforced"]).upper() == "YES",
            "clause": "".join(
                str(row["check_clause"]).lower().replace("`", "").split()
            ),
        }
        for row in check_rows
    }

    has_backtest_column = "backtest_run_id" in columns
    has_backtest_actual = "backtest_actual_direction" in columns
    if has_backtest_column and has_backtest_actual:
        source_stats = connection.execute(
            text(
                """
                SELECT
                    SUM(NOT ((run_id IS NOT NULL AND backtest_run_id IS NULL)
                         OR (run_id IS NULL AND backtest_run_id IS NOT NULL)))
                        AS invalid_source_rows,
                    SUM(run_id IS NOT NULL AND scheme_version IS NULL)
                        AS live_rows_without_version,
                    SUM(run_id IS NOT NULL AND backtest_actual_direction IS NOT NULL)
                        AS live_rows_with_backtest_actual,
                    SUM(backtest_run_id IS NOT NULL AND
                        (backtest_actual_direction IS NULL OR
                         backtest_actual_direction NOT IN (-1, 0, 1)))
                        AS invalid_backtest_actual_rows,
                    SUM(JSON_CONTAINS_PATH(extra, 'one', '$.prediction_phase') = 1)
                        AS extra_phase_rows
                FROM t_scheme_predictions
                """
            )
        ).mappings().one()
        missing_count = int(
            connection.execute(
                text(
                    _SELECTED_BACKTEST_CTE
                    + """
                    SELECT COUNT(*)
                    FROM selected_points s
                    LEFT JOIN t_scheme_predictions p
                      ON p.scheme_id = s.scheme_id
                     AND p.target_tenor = s.target_tenor
                     AND p.horizon = s.horizon
                     AND p.target_date = s.target_date
                    WHERE p.id IS NULL
                    """
                )
            ).scalar_one()
        )
    else:
        source_stats = connection.execute(
            text(
                """
                SELECT SUM(run_id IS NULL) AS invalid_source_rows,
                       SUM(run_id IS NOT NULL AND scheme_version IS NULL)
                           AS live_rows_without_version,
                       0 AS live_rows_with_backtest_actual,
                       0 AS invalid_backtest_actual_rows,
                       SUM(JSON_CONTAINS_PATH(extra, 'one', '$.prediction_phase') = 1)
                           AS extra_phase_rows
                FROM t_scheme_predictions
                """
            )
        ).mappings().one()
        missing_count = -1

    orphan_run_count = int(
        connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_scheme_predictions p
                LEFT JOIN t_scheme_runs r ON r.run_id = p.run_id
                WHERE p.run_id IS NOT NULL AND r.run_id IS NULL
                """
            )
        ).scalar_one()
    )
    selected_validation = connection.execute(
        text(
            _SELECTED_BACKTEST_CTE
            + """
            SELECT
                COALESCE(SUM(
                    predict_date IS NULL OR feature_date IS NULL OR
                    target_date IS NULL OR
                    predicted_direction IS NULL OR
                    predicted_direction NOT IN (-1, 0, 1) OR
                    label IS NULL OR label NOT IN (-1, 0, 1)
                ), 0) AS invalid_selected_rows,
                COALESCE(SUM(scheme_id <> parent_scheme_id), 0)
                    AS mismatched_selected_scheme_rows,
                (
                    SELECT COUNT(*) FROM (
                        SELECT scheme_id, target_tenor, horizon, target_date
                        FROM selected_points
                        GROUP BY scheme_id, target_tenor, horizon, target_date
                        HAVING COUNT(*) > 1
                    ) duplicate_groups
                ) AS duplicate_selected_keys
            FROM selected_points
            """
        )
    ).mappings().one()
    invalid_active_runtime_count = int(
        connection.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                    SELECT base_scheme_id
                    FROM t_scheme_registry
                    WHERE status = 'active'
                    GROUP BY base_scheme_id
                    HAVING COUNT(DISTINCT runtime_type) <> 1
                       OR MIN(runtime_type) NOT IN (
                           'native_adapter', 'blackbox_v2'
                       )
                ) invalid_runtime
                """
            )
        ).scalar_one()
    )
    legacy_rows = connection.execute(
        text(
            """
            SELECT r.run_id, r.scheme_id, r.scheme_version, r.predict_date,
                   r.started_at, COUNT(p.id) AS prediction_count,
                   SUM(p.scheme_version IS NULL) AS null_prediction_versions,
                   SUM(
                       p.id IS NOT NULL AND (
                           p.scheme_id <> r.scheme_id OR
                           p.predict_date <> r.predict_date
                       )
                   ) AS mismatched_prediction_rows
            FROM t_scheme_runs r
            LEFT JOIN t_scheme_predictions p ON p.run_id = r.run_id
            WHERE r.run_id IN (15, 16, 17)
            GROUP BY r.run_id, r.scheme_id, r.scheme_version,
                     r.predict_date, r.started_at
            ORDER BY r.run_id
            """
        )
    ).mappings().all()
    return {
        "columns": columns,
        "indexes": {name: tuple(value) for name, value in indexes.items()},
        "foreign_keys": foreign_keys,
        "checks": checks,
        "invalid_source_rows": int(source_stats["invalid_source_rows"] or 0),
        "live_rows_without_version": int(
            source_stats["live_rows_without_version"] or 0
        ),
        "live_rows_with_backtest_actual": int(
            source_stats["live_rows_with_backtest_actual"] or 0
        ),
        "invalid_backtest_actual_rows": int(
            source_stats["invalid_backtest_actual_rows"] or 0
        ),
        "extra_phase_rows": int(source_stats["extra_phase_rows"] or 0),
        "missing_canonical_rows": missing_count,
        "orphan_run_count": orphan_run_count,
        "invalid_selected_rows": int(
            selected_validation["invalid_selected_rows"] or 0
        ),
        "duplicate_selected_keys": int(
            selected_validation["duplicate_selected_keys"] or 0
        ),
        "mismatched_selected_scheme_rows": int(
            selected_validation["mismatched_selected_scheme_rows"] or 0
        ),
        "invalid_active_runtime_count": invalid_active_runtime_count,
        "legacy_rows": [dict(row) for row in legacy_rows],
    }


def classify_scheme_prediction_fact_state(state: Mapping[str, object]) -> str:
    """将 024 状态分类为可安全重放、完整或漂移。"""
    columns = state.get("columns")
    indexes = state.get("indexes")
    foreign_keys = state.get("foreign_keys")
    checks = state.get("checks")
    if not all(
        isinstance(value, Mapping)
        for value in (columns, indexes, foreign_keys, checks)
    ):
        return "UNSAFE"
    columns = dict(columns)
    indexes = dict(indexes)
    foreign_keys = dict(foreign_keys)
    checks = dict(checks)
    if "run_id" not in columns or "created_at" not in columns:
        return "UNSAFE"
    allowed_columns = {
        "run_id", "backtest_run_id", "prediction_phase",
        "backtest_actual_direction", "updated_at", "created_at"
    }
    if not set(columns).issubset(allowed_columns):
        return "UNSAFE"
    if "backtest_run_id" in columns:
        definition = columns["backtest_run_id"]
        if not isinstance(definition, Mapping) or (
            definition.get("column_type"), definition.get("nullable")
        ) != ("bigint", "YES"):
            return "UNSAFE"
    if "backtest_actual_direction" in columns:
        definition = columns["backtest_actual_direction"]
        if not isinstance(definition, Mapping) or (
            definition.get("column_type"), definition.get("nullable")
        ) != ("tinyint", "YES"):
            return "UNSAFE"
    if indexes.get("idx_scheme_predictions_phase") not in {
        None,
        ("scheme_id", "prediction_phase", "predict_date"),
    }:
        return "UNSAFE"
    if indexes.get("idx_scheme_predictions_backtest_run") not in {
        None,
        ("backtest_run_id",),
    }:
        return "UNSAFE"
    expected_fks = {
        "fk_scheme_predictions_run": {
            "column": "run_id",
            "referenced_table": "t_scheme_runs",
            "referenced_column": "run_id",
            "delete_rule": "RESTRICT",
            "update_rule": "NO ACTION",
        },
        "fk_scheme_predictions_backtest_run": {
            "column": "backtest_run_id",
            "referenced_table": "t_backtest_runs",
            "referenced_column": "id",
            "delete_rule": "RESTRICT",
            "update_rule": "NO ACTION",
        },
    }
    if not set(foreign_keys).issubset(expected_fks):
        return "UNSAFE"
    if any(dict(value) != expected_fks[name] for name, value in foreign_keys.items()):
        return "UNSAFE"
    if set(checks) - {"ck_scheme_predictions_source"}:
        return "UNSAFE"
    check = checks.get("ck_scheme_predictions_source")
    if check is not None:
        clause = str(check.get("clause") or "") if isinstance(check, Mapping) else ""
        required = (
            "run_idisnotnull", "backtest_run_idisnull",
            "backtest_actual_directionisnull", "run_idisnull",
            "backtest_run_idisnotnull", "backtest_actual_directionin(",
            "-(1),0,1",
        )
        if not check.get("enforced") or any(token not in clause for token in required):
            return "UNSAFE"
    for key in (
        "invalid_source_rows", "orphan_run_count",
        "live_rows_with_backtest_actual", "invalid_backtest_actual_rows",
        "invalid_selected_rows", "duplicate_selected_keys",
        "mismatched_selected_scheme_rows",
        "invalid_active_runtime_count",
    ):
        if int(state.get(key, -1)) != 0:
            return "UNSAFE"
    live_nulls = int(state.get("live_rows_without_version", -1))
    if not _legacy_version_state_is_expected(state, live_nulls=live_nulls):
        return "UNSAFE"

    target_schema = (
        "backtest_run_id" in columns
        and "backtest_actual_direction" in columns
        and "prediction_phase" not in columns
        and "updated_at" not in columns
        and indexes.get("idx_scheme_predictions_backtest_run") == ("backtest_run_id",)
        and "idx_scheme_predictions_phase" not in indexes
        and foreign_keys == expected_fks
        and check is not None
    )
    target_data = (
        int(state.get("live_rows_without_version", -1)) == 0
        and int(state.get("extra_phase_rows", -1)) == 0
        and int(state.get("missing_canonical_rows", -1)) == 0
    )
    if target_schema and target_data:
        return "COMPLETE"

    source_schema = (
        "backtest_run_id" not in columns
        and "backtest_actual_direction" not in columns
        and "prediction_phase" in columns
        and "updated_at" in columns
        and indexes.get("idx_scheme_predictions_phase")
        == ("scheme_id", "prediction_phase", "predict_date")
        and "idx_scheme_predictions_backtest_run" not in indexes
        and not foreign_keys
        and not checks
    )
    if source_schema and live_nulls in {0, 12}:
        return "COMPATIBLE_PARTIAL"
    if (
        "backtest_run_id" in columns
        and live_nulls in {0, 12}
    ):
        return "COMPATIBLE_PARTIAL"
    return "UNSAFE"


def _legacy_version_state_is_expected(
    state: Mapping[str, object],
    *,
    live_nulls: int,
) -> bool:
    """只接受空库或已证明的三个 t5_daily 历史 run。"""
    rows = state.get("legacy_rows")
    if not isinstance(rows, list):
        return False
    if not rows:
        return live_nulls == 0
    if len(rows) != 3 or live_nulls not in {0, 12}:
        return False
    expected_dates = {
        15: "2026-06-05",
        16: "2026-06-08",
        17: "2026-06-09",
    }
    for raw in rows:
        if not isinstance(raw, Mapping):
            return False
        try:
            run_id = int(raw.get("run_id"))
            prediction_count = int(raw.get("prediction_count"))
            null_predictions = int(raw.get("null_prediction_versions"))
            mismatched_predictions = int(
                raw.get("mismatched_prediction_rows")
            )
        except (TypeError, ValueError):
            return False
        if (
            run_id not in expected_dates
            or str(raw.get("scheme_id") or "") != "t5_daily"
            or str(raw.get("predict_date")) != expected_dates[run_id]
            or prediction_count != 4
            or null_predictions != (4 if live_nulls == 12 else 0)
            or mismatched_predictions != 0
        ):
            return False
        version = raw.get("scheme_version")
        if live_nulls == 12:
            if version not in {None, EXPECTED_LIVE_VERSION}:
                return False
        elif version != EXPECTED_LIVE_VERSION:
            return False
        started_at = str(raw.get("started_at") or "")
        if not (
            "2026-06-10 00:01:02" <= started_at
            < "2026-06-10 18:53:35"
        ):
            return False
    return True
