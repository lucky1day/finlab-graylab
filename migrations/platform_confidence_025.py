from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import inspect, text


TABLES = ("t_scheme_predictions", "t_backtest_predictions")
SOURCE_CHECK = (
    "(((run_idisnotnull)and(backtest_run_idisnull)and"
    "(backtest_actual_directionisnull))or((run_idisnull)and"
    "(backtest_run_idisnotnull)and(backtest_actual_directionin(-(1),0,1))))"
)


def expected_prediction_schema(*, with_confidence: bool = False) -> dict[str, object]:
    """定义 025 两张事实表闭世界；旧 migration 合同不作任何修改。"""
    def columns(specs: tuple[tuple, ...]) -> tuple[tuple, ...]:
        return tuple(
            (name, kind, nullable, default)
            for name, kind, nullable, default in specs
            if with_confidence or name != "confidence"
        )

    common = (
        ("scheme_id", "VARCHAR(64)", False, None),
        ("target_tenor", "VARCHAR(64)", False, None),
        ("horizon", "INTEGER", False, None),
        ("predict_date", "DATE", False, None),
    )
    live = columns((
        ("id", "BIGINT", False, None),
        ("run_id", "BIGINT", True, None),
        ("backtest_run_id", "BIGINT", True, None),
        ("scheme_version", "VARCHAR(64)", True, None),
        *common,
        ("target_date", "DATE", False, None),
        ("feature_date", "DATE", True, None),
        ("predicted_direction", "TINYINT", False, None),
        ("backtest_actual_direction", "TINYINT", True, None),
        ("confidence", "FLOAT", True, None),
        ("model_version", "VARCHAR(64)", True, None),
        ("extra", "JSON", True, None),
        ("created_at", "DATETIME", False, "CURRENT_TIMESTAMP"),
    ))
    backtest = columns((
        ("id", "BIGINT", False, None),
        ("run_id", "BIGINT", False, None),
        ("benchmark_id", "VARCHAR(128)", False, None),
        *common,
        ("feature_date", "DATE", True, None),
        ("target_date", "DATE", True, None),
        ("label", "TINYINT", True, None),
        ("predicted_direction", "TINYINT", True, None),
        ("model_pred", "TINYINT", True, None),
        ("confidence", "DOUBLE", True, None),
        ("source_row", "JSON", True, None),
        ("extra", "JSON", True, None),
        ("created_at", "DATETIME", False, "CURRENT_TIMESTAMP"),
        ("updated_at", "DATETIME", False, "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    ))
    return {
        TABLES[0]: {
            "columns": live,
            "indexes": {
                "fk_scheme_predictions_run": (("run_id",), False),
                "idx_scheme_predict_date": (("scheme_id", "predict_date"), False),
                "idx_scheme_predictions_backtest_run": (("backtest_run_id",), False),
                "idx_target_date": (("target_date",), False),
                "idx_tenor_target_date": (("target_tenor", "target_date"), False),
                "uk_scheme_tenor_target": (
                    ("scheme_id", "target_tenor", "horizon", "target_date"), True
                ),
            },
            "foreign_keys": {
                "fk_scheme_predictions_run": (
                    ("run_id",), "t_scheme_runs", ("run_id",), "RESTRICT", "NO ACTION"
                ),
                "fk_scheme_predictions_backtest_run": (
                    ("backtest_run_id",), "t_backtest_runs", ("id",), "RESTRICT", "NO ACTION"
                ),
            },
            "checks": {"ck_scheme_predictions_source": (SOURCE_CHECK, "YES")},
        },
        TABLES[1]: {
            "columns": backtest,
            "indexes": {
                "idx_backtest_predictions_run": (("run_id",), False),
                "idx_backtest_predictions_scope": (
                    ("scheme_id", "target_tenor", "predict_date"), False
                ),
                "uk_backtest_prediction": (("run_id", "target_tenor", "predict_date"), True),
            },
            "foreign_keys": {
                "fk_backtest_predictions_run": (
                    ("run_id",), "t_backtest_runs", ("id",), "CASCADE", "NO ACTION"
                ),
            },
            "checks": {},
        },
    }


def read_prediction_schema(connection: Any) -> dict[str, object]:
    """只读检查两表的全部字段、约束和索引，不读取或改写算法审计值。"""
    inspector = inspect(connection)
    result = {}
    for table in TABLES:
        if table not in inspector.get_table_names():
            return {}
        reflected = inspector.get_columns(table)
        indexes = inspector.get_indexes(table)
        foreign_keys = inspector.get_foreign_keys(table)
        options = inspector.get_table_options(table)
        if (
            options.get("mysql_engine") != "InnoDB"
            or options.get("mysql_collate") != "utf8mb4_0900_ai_ci"
            or inspector.get_pk_constraint(table)["constrained_columns"] != ["id"]
            or any(c.get("computed") for c in reflected)
            or not reflected[0].get("autoincrement")
            or any(i.get("dialect_options") for i in indexes)
            or any(f.get("referred_schema") not in {None, connection.engine.url.database}
                   for f in foreign_keys)
        ):
            return {}
        checks = connection.execute(text("""
            SELECT t.constraint_name, c.check_clause, t.enforced
            FROM information_schema.table_constraints t
            JOIN information_schema.check_constraints c
              ON c.constraint_schema=t.constraint_schema
             AND c.constraint_name=t.constraint_name
            WHERE t.constraint_schema=DATABASE() AND t.table_name=:table
        """), {"table": table}).all()
        result[table] = {
            "columns": tuple((c["name"], c["type"].compile(dialect=connection.dialect),
                              c["nullable"], c["default"])
                             for c in reflected),
            "indexes": {i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
                        for i in indexes},
            "foreign_keys": {
                f["name"]: (tuple(f["constrained_columns"]), f["referred_table"],
                            tuple(f["referred_columns"]), f["options"].get("ondelete", "NO ACTION"),
                            f["options"].get("onupdate", "NO ACTION"))
                for f in foreign_keys
            },
            "checks": {name: ("".join(clause.lower().replace("`", "").split()), enforced)
                       for name, clause, enforced in checks},
        }
        # 任何数据库端 confidence 消费者都不能因删列被静默破坏。
        dependencies = connection.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM information_schema.triggers
               WHERE event_object_schema=DATABASE() AND event_object_table=:table) +
              (SELECT COUNT(*) FROM information_schema.views
               WHERE LOWER(view_definition) LIKE :field
                 AND LOWER(view_definition) LIKE :qualified_table
                 AND LOWER(view_definition) LIKE :table_match) +
              (SELECT COUNT(*) FROM information_schema.routines
               WHERE routine_schema=DATABASE() AND LOWER(routine_definition) LIKE :field
                 AND LOWER(routine_definition) LIKE :table_match) +
              (SELECT COUNT(*) FROM information_schema.events
               WHERE event_schema=DATABASE() AND LOWER(event_definition) LIKE :field
                 AND LOWER(event_definition) LIKE :table_match) +
              (SELECT COUNT(*) FROM information_schema.statistics
               WHERE table_schema=DATABASE() AND table_name=:table
                 AND (is_visible <> 'YES' OR sub_part IS NOT NULL
                      OR expression IS NOT NULL OR index_type <> 'BTREE'
                      OR collation <> 'A')) +
              (SELECT COUNT(*) FROM information_schema.key_column_usage
               WHERE referenced_table_schema=DATABASE() AND referenced_table_name=:table
                 AND referenced_column_name='confidence')
        """), {
            "table": table,
            "field": "%confidence%",
            "table_match": f"%{table}%",
            "qualified_table": f"%`{connection.engine.url.database}`.`{table}`%",
        }).scalar_one()
        if int(dependencies):
            return {}
    return result


def classify_prediction_schema(schema: Mapping[str, object]) -> str:
    """两列各自存在或缺失均可恢复；其余结构不符即拒绝。"""
    expected = expected_prediction_schema()
    if set(schema) != set(expected):
        return "UNSAFE"
    remaining = 0
    source = expected_prediction_schema(with_confidence=True)
    for table, observed in schema.items():
        if not isinstance(observed, Mapping):
            return "UNSAFE"
        if dict(observed) == source[table]:
            remaining += 1
        elif dict(observed) != expected[table]:
            return "UNSAFE"
    return "COMPATIBLE_PARTIAL" if remaining else "COMPLETE"
