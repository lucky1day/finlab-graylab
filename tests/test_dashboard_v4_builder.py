from __future__ import annotations

from sqlalchemy import create_engine, event, text

from backend.factor_lab_dashboard import (
    build_factor_lab_dashboard,
    build_factor_lab_dashboard_detail,
)
from backend.factor_lab_dashboard_semantics import validate_dashboard_payload


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    statements = [
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
            prediction_phase TEXT, predicted_direction INTEGER, extra TEXT
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
        """CREATE TABLE t_backtest_predictions (
            run_id INTEGER, target_tenor TEXT, horizon INTEGER,
            predict_date DATE, feature_date DATE, target_date DATE,
            label INTEGER, predicted_direction INTEGER
        )""",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                """INSERT INTO t_scheme_registry VALUES
                ('demo_daily__h1__5Y','demo_daily','blackbox_v2',
                 'Demo','lw','demo scheme',1,'T+1','daily','5Y','active',
                 '2026-06-01')"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO t_target_registry VALUES
                ('5Y','5年','bond','yield',1,'active','{}')"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (1,'demo_daily','5Y',1,'2026-06-02','2026-06-01',
                 '2026-06-03','scheduled_live',1,'{}')"""
            )
        )
        connection.execute(
            text("INSERT INTO t_scheme_actuals VALUES ('5Y','2026-06-03',1,1)")
        )
        connection.execute(
            text(
                """INSERT INTO t_backtest_runs VALUES
                (7,'fixture','demo_daily','blackbox_v2_current_snapshot_as_of',
                 '2025-01-01','2026-05-29','success',
                 '2026-05-29 10:00:00','2026-05-29 10:00:00')"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO t_backtest_predictions VALUES
                (7,'5Y',1,'2026-01-02','2025-12-31','2026-01-05',1,1),
                (7,'5Y',1,'2026-06-02','2026-06-01','2026-06-04',-1,-1)"""
            )
        )
    return engine


def test_v4_summary_aggregates_rows_and_reads_owner() -> None:
    engine = _engine()
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_statement(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        statements.append(" ".join(statement.split()))

    payload = build_factor_lab_dashboard(engine)

    validate_dashboard_payload(payload)
    assert payload["schema_version"] == "factor-lab-dashboard-v4"
    assert payload["representation"] == "summary"
    scheme = payload["schemes"][0]
    assert scheme["owner"] == "lw"
    assert "live_rows" not in scheme
    assert scheme["monthly_rows"] == [
        ["2026-01", "backtest", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0],
        ["2026-06", "live", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0],
    ]
    assert scheme["phase_ranges"] == [
        {
            "prediction_phase": "scheduled_live",
            "start_predict_date": "2026-06-02",
            "end_predict_date": "2026-06-02",
            "start_target_date": "2026-06-03",
            "end_target_date": "2026-06-03",
            "rows": 1,
        }
    ]
    assert sum(
        "FROM t_scheme_predictions" in sql for sql in statements
    ) == 1


def test_v4_detail_reads_only_requested_active_scheme_month_and_source() -> None:
    engine = _engine()
    payload = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="all",
    )

    assert payload is not None
    validate_dashboard_payload(payload)
    assert payload["representation"] == "detail"
    assert payload["rows"] == [
        [
            "live",
            "2026-06-02",
            "2026-06-01",
            "2026-06-03",
            "scheduled_live",
            1,
            1,
        ]
    ]
    assert (
        build_factor_lab_dashboard_detail(
            engine,
            scheme_id="missing__h1__5Y",
            month="2026-06",
            source="all",
        )
        is None
    )


def test_v4_detail_pushes_month_and_source_into_database_queries() -> None:
    engine = _engine()
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_statement(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        statements.append(" ".join(statement.split()))

    payload = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="backtest",
    )

    assert payload is not None
    assert payload["rows"] == []
    assert not any(
        "SELECT id, scheme_id" in sql and "FROM t_scheme_predictions" in sql
        for sql in statements
    )
    assert not any("FROM t_scheme_actuals" in sql for sql in statements)
    detail_sql = next(
        sql for sql in statements if "FROM t_backtest_predictions" in sql
    )
    assert "target_date >= ?" in detail_sql
    assert "target_date < ?" in detail_sql
    assert any(
        "SELECT MIN(target_date) FROM t_scheme_predictions" in sql
        for sql in statements
    )
