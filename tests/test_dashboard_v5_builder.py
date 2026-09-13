from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from backend.factor_lab_dashboard import (
    MAX_ACTUAL_SOURCE_ROWS,
    DashboardDataError,
    _monthly_rows,
    _read_live_actuals,
    _read_product_predictions,
    build_factor_lab_dashboard,
    build_factor_lab_dashboard_detail,
)
from backend.factor_lab_dashboard_semantics import (
    dashboard_result_source,
    live_actual_selector,
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
                 '2026-06-03',1,NULL,'{}'),
                (2,'demo_daily','5Y',1,'2026-01-02','2025-12-31',
                 '2026-01-05',1,1,'{}'),
                (3,'demo_daily','5Y',1,'2026-06-02','2026-06-01',
                 '2026-06-04',-1,-1,'{}')"""
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
        connection.execute(text("INSERT INTO t_scheme_actuals VALUES ('5Y','2026-01-05',1,1)"))
        connection.execute(text("INSERT INTO t_scheme_actuals VALUES ('5Y','2026-06-04',-1,-1)"))
    return engine


def test_v5_summary_aggregates_rows_and_reads_owner() -> None:
    engine = _engine()
    payload = build_factor_lab_dashboard(engine)

    assert payload["schema_version"] == "factor-lab-dashboard-v5"
    assert payload["representation"] == "summary"
    assert payload["live_target_start_date"] == "2026-06-01"
    scheme = payload["schemes"][0]
    assert scheme["owner"] == "lw"
    assert scheme["monthly_rows"] == [
        ["2026-01", "backtest", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0],
        ["2026-06", "live", 2, 2, 2, 1, 1, 0, 1, 1, 0, 1, 1],
    ]


@pytest.mark.parametrize(
    "target_date,month,source",
    [
        ("2026-05-29", "2026-05", "backtest"),
        ("2026-07-03", "2026-07", "live"),
        ("2026-06-05", "2026-06", "live"),
    ],
)
def test_pending_predictions_keep_month_accessible_without_changing_metrics(
    target_date: str, month: str, source: str,
) -> None:
    engine = _engine()
    baseline = build_factor_lab_dashboard(engine)["schemes"][0]["monthly_rows"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (4,'demo_daily','5Y',1,'2026-05-28','2026-05-27',
                 :target_date,-1,NULL,'{}')"""
            ),
            {"target_date": target_date},
        )
    summary = build_factor_lab_dashboard(engine)
    rows = summary["schemes"][0]["monthly_rows"]
    assert all(row in rows for row in baseline)
    if month != "2026-06":
        assert [month, source, *([0] * 11)] in rows
    else:
        assert rows == baseline
    detail = build_factor_lab_dashboard_detail(
        engine, scheme_id="demo_daily__h1__5Y", month=month, source=source,
    )
    assert detail is not None
    assert [source, "2026-05-28", "2026-05-27", target_date, -1, None] in detail["rows"]


def test_same_id_runtime_upgrade_preserves_backtest_provenance() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_registry SET runtime_type='native_adapter'"))
        conn.execute(text("UPDATE t_backtest_runs SET data_source='framework_db_aligned'"))
    before = build_factor_lab_dashboard(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_registry SET runtime_type='blackbox_v2'"))
    after = build_factor_lab_dashboard(engine)
    assert after['schemes'] == before['schemes']
    assert after['schemes'][0]['backtest']['data_source'] == 'framework_db_aligned'
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t_backtest_runs VALUES "
                          "(8,'unpublished-source','demo_daily','source_original',"
                          "'2025-01-01','2026-05-29','success','2026-06-01','2026-06-01')"))
    assert build_factor_lab_dashboard(engine)['schemes'] == after['schemes']


@pytest.mark.parametrize(
    ("task_type", "expected_months"),
    (
        ("T+1", ["2026-05", "2026-06"]),
        ("monthly_average", ["2026-06", "2026-07"]),
    ),
)
def test_monthly_rows_use_target_month_except_monthly_average(
    task_type: str,
    expected_months: list[str],
) -> None:
    details = [
            {
                "target_date": "2026-05-31",
                "predict_date": "2026-06-02",
                "predicted_direction": 1,
                "actual_direction": 1,
            },
            {
                "target_date": "2026-06-01",
                "predict_date": "2026-05-29",
                "predicted_direction": -1,
                "actual_direction": -1,
            },
        ]
    backtest_details = [
        row for row in details
        if dashboard_result_source(row["target_date"]) == "backtest"
    ]
    live_details = [
        row for row in details
        if dashboard_result_source(row["target_date"]) == "live"
    ]

    rows = _monthly_rows(
        backtest_details=backtest_details,
        live_details=live_details,
        task_type=task_type,
    )

    assert [row[0] for row in rows] == expected_months
    assert [row[1] for row in rows] == ["backtest", "live"]


def test_live_prediction_query_matches_full_registry_scope() -> None:
    engine = _engine()

    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (2,'another_daily','10Y',1,'2026-06-01','2026-05-29',
                 '2026-06-02',-1,NULL,'{}'),
                (4,'demo_daily','5Y',5,'2026-06-02','2026-06-01',
                 '2026-06-09',-1,NULL,'{}')"""
            )
        )
        rows = _read_product_predictions(
            connection,
            [
                {
                    "base_scheme_id": "demo_daily",
                    "target_tenor": "5Y",
                    "horizon": 1,
                },
                {
                    "base_scheme_id": "another_daily",
                    "target_tenor": "10Y",
                    "horizon": 1,
                },
            ],
        )

    assert [int(row["id"]) for row in rows] == [2, 2, 1, 3]
    assert all(int(row["horizon"]) == 1 for row in rows)
    assert {
        (str(row["scheme_id"]), str(row["target_tenor"]))
        for row in rows
    } == {("demo_daily", "5Y"), ("another_daily", "10Y")}


def test_v5_detail_reads_requested_active_scheme_month_and_source() -> None:
    engine = _engine()
    payload = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="all",
    )

    assert payload is not None
    assert payload["representation"] == "detail"
    assert payload["live_target_start_date"] == "2026-06-01"
    assert payload["rows"] == [
        [
            "live",
            "2026-06-02",
            "2026-06-01",
            "2026-06-03",
            1,
            1,
        ],
        [
            "live",
            "2026-06-02",
            "2026-06-01",
            "2026-06-04",
            -1,
            -1,
        ],
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
