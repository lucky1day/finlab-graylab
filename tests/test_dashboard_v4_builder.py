from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from backend.factor_lab_dashboard import (
    MAX_ACTUAL_SOURCE_ROWS,
    DashboardDataError,
    _phase_ranges,
    _read_live_actuals,
    build_factor_lab_dashboard,
    build_factor_lab_dashboard_detail,
)
from backend.factor_lab_dashboard_semantics import (
    live_actual_selector,
    validate_dashboard_payload,
)


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


def test_phase_ranges_use_date_extrema_and_stable_phase_order() -> None:
    rows = [
        {
            "prediction_phase": "scheduled_live",
            "predict_date": "2026-06-05",
            "target_date": "2026-06-09",
        },
        {
            "prediction_phase": "gray_live",
            "predict_date": "2026-05-03",
            "target_date": "2026-05-07",
        },
        {
            "prediction_phase": "scheduled_live",
            "predict_date": "2026-06-02",
            "target_date": "2026-06-04",
        },
        {
            "prediction_phase": "ignored",
            "predict_date": "not-a-date",
            "target_date": "not-a-date",
        },
        {
            "prediction_phase": "gray_live",
            "predict_date": "2026-05-01",
            "target_date": "2026-05-08",
        },
    ]

    assert _phase_ranges(rows) == [
        {
            "prediction_phase": "gray_live",
            "start_predict_date": "2026-05-01",
            "end_predict_date": "2026-05-03",
            "start_target_date": "2026-05-07",
            "end_target_date": "2026-05-08",
            "rows": 2,
        },
        {
            "prediction_phase": "scheduled_live",
            "start_predict_date": "2026-06-02",
            "end_predict_date": "2026-06-05",
            "start_target_date": "2026-06-04",
            "end_target_date": "2026-06-09",
            "rows": 2,
        },
    ]


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


def test_live_actual_query_reads_exact_scopes_for_all_task_types() -> None:
    engine = _engine()
    task_scopes = (
        ("T+1", "1Y"),
        ("T+5", "3Y"),
        ("weekly_point", "5Y"),
        ("weekly_average", "7Y"),
        ("monthly", "10Y"),
        ("monthly_average", "1Y"),
        ("quarterly_average", "3Y"),
        ("annual_average", "5Y"),
    )
    registry_rows = [
        {"task_type": task_type, "target_tenor": tenor}
        for task_type, tenor in task_scopes
    ]
    selectors = {
        task_type: live_actual_selector(task_type)
        for task_type, _tenor in task_scopes
    }
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM t_scheme_actuals"))
        connection.execute(
            text(
                """INSERT INTO t_scheme_actuals VALUES
                ('1Y','2026-06-03',1,1),
                ('3Y','2026-06-03',-1,-1)"""
            )
        )
        connection.execute(text("DELETE FROM t_scheme_weekly_actuals"))
        connection.execute(
            text(
                """INSERT INTO t_scheme_weekly_actuals VALUES
                ('5Y','2026-06-05',:point_rule,1),
                ('7Y','2026-06-05',:average_rule,-1),
                ('5Y','2026-06-05',:average_rule,0),
                ('7Y','2026-06-05',:point_rule,0)"""
            ),
            {
                "point_rule": selectors["weekly_point"][1],
                "average_rule": selectors["weekly_average"][1],
            },
        )
        connection.execute(text("DELETE FROM t_scheme_monthly_actuals"))
        connection.execute(
            text(
                """INSERT INTO t_scheme_monthly_actuals VALUES
                ('10Y','2026-06-30',:monthly_rule,1)"""
            ),
            {"monthly_rule": selectors["monthly"][1]},
        )
        connection.execute(text("DELETE FROM t_scheme_period_average_actuals"))
        connection.execute(
            text(
                """INSERT INTO t_scheme_period_average_actuals VALUES
                ('1Y','2026-06-30',:monthly_average_rule,1),
                ('3Y','2026-06-30',:quarterly_average_rule,-1),
                ('5Y','2026-06-30',:annual_average_rule,0),
                ('1Y','2026-06-30',:quarterly_average_rule,0),
                ('3Y','2026-06-30',:annual_average_rule,0)"""
            ),
            {
                "monthly_average_rule": selectors["monthly_average"][1],
                "quarterly_average_rule": selectors["quarterly_average"][1],
                "annual_average_rule": selectors["annual_average"][1],
            },
        )

        rows = _read_live_actuals(connection, registry_rows)

    assert {
        (
            str(row["target_tenor"]),
            str(row["actual_kind"]),
            str(row["target_rule"]),
        )
        for row in rows
    } == {
        (tenor, *selectors[task_type])
        for task_type, tenor in task_scopes
    }
    assert len(rows) == len(task_scopes)


def test_unrelated_actual_history_does_not_consume_active_scope_budget() -> None:
    engine = _engine()
    point_rule = live_actual_selector("weekly_point")[1]
    unrelated_rows = [
        ("5Y", "2026-06-06", "unused_weekly_rule", 1)
    ] * (MAX_ACTUAL_SOURCE_ROWS + 1)
    with engine.begin() as connection:
        connection.execute(
            text(
                """UPDATE t_scheme_registry
                SET task_type = 'weekly_point', frequency = 'weekly'"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO t_scheme_weekly_actuals VALUES
                ('5Y','2026-06-06',:point_rule,1)"""
            ),
            {"point_rule": point_rule},
        )
        connection.exec_driver_sql(
            "INSERT INTO t_scheme_weekly_actuals VALUES (?, ?, ?, ?)",
            unrelated_rows,
        )
        selected_rows = _read_live_actuals(
            connection,
            [{"task_type": "weekly_point", "target_tenor": "5Y"}],
        )

    payload = build_factor_lab_dashboard(engine)

    validate_dashboard_payload(payload)
    assert len(selected_rows) == 1
    assert selected_rows[0]["target_rule"] == point_rule


def test_related_actual_history_still_fails_closed_at_source_budget() -> None:
    engine = _engine()
    related_rows = [
        ("5Y", "2026-06-03", 1, 1)
    ] * MAX_ACTUAL_SOURCE_ROWS
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO t_scheme_actuals VALUES (?, ?, ?, ?)",
            related_rows,
        )

    with pytest.raises(
        DashboardDataError,
        match="dataset=live_actuals limit=80000",
    ):
        build_factor_lab_dashboard(engine)
