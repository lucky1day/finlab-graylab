from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from backend.factor_lab_dashboard import (
    DashboardDataError,
    DashboardQueryError,
    _monthly_rows,
    build_factor_lab_dashboard,
    build_factor_lab_dashboard_detail,
    parse_dashboard_month,
)
from backend.db import (
    RequestBudgetExceeded,
    bind_http_request_context,
    create_http_request_context,
)
from backend.factor_lab_dashboard_semantics import (
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
    dashboard_result_source,
    iter_grouped_live_prediction_rows,
    live_actual_selector,
)
from backend.factor_lab_dashboard_queries import (
    MAX_ACTUAL_SOURCE_ROWS,
    read_bounded_source_rows,
    read_live_actuals,
    read_product_predictions,
    read_selected_backtest_runs,
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


@pytest.mark.parametrize("month", ("0001-02", "2026-12", "9999-11"))
def test_dashboard_month_accepts_representable_adjacent_boundaries(
    month: str,
) -> None:
    assert parse_dashboard_month(month) == month


@pytest.mark.parametrize("month", ("0000-01", "0001-01", "9999-12"))
def test_dashboard_month_rejects_unrepresentable_adjacent_boundaries(
    month: str,
) -> None:
    with pytest.raises(DashboardQueryError):
        parse_dashboard_month(month)


def test_cumulative_request_budget_stops_before_the_next_query() -> None:
    class Clock:
        value = 10.0

        def __call__(self) -> float:
            return self.value

    class Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class Connection:
        calls = 0

        def execute(self, _statement, _params):
            self.calls += 1
            clock.value += 0.06
            return Result()

    clock = Clock()
    connection = Connection()
    context = create_http_request_context(
        "budget-test",
        clock=clock,
        budget_seconds=0.05,
    )
    with bind_http_request_context(context):
        assert read_bounded_source_rows(
            connection,
            text("SELECT 1"),
            {},
            dataset="first",
            cap=1,
        ) == []
        with pytest.raises(RequestBudgetExceeded):
            read_bounded_source_rows(
                connection,
                text("SELECT 1"),
                {},
                dataset="second",
                cap=1,
            )

    assert connection.calls == 1


def test_v5_summary_aggregates_rows_and_reads_owner() -> None:
    engine = _engine()
    payload = build_factor_lab_dashboard(engine)

    assert payload["schema_version"] == "factor-lab-dashboard-v6"
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


def test_backtest_query_returns_only_deterministic_latest_candidate() -> None:
    engine = _engine()
    registry_rows = [
        {
            "scheme_id": "demo_daily__h1__5Y",
            "base_scheme_id": "demo_daily",
            "runtime_type": "blackbox_v2",
            "status": "active",
        }
    ]
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO t_backtest_runs VALUES
                (8,'older-other-source','demo_daily','framework_db_aligned',
                 '2025-01-01','2026-05-30','success',
                 '2026-06-01 10:00:00','2026-06-01 10:00:00'),
                (9,'latest-by-id','demo_daily','framework_db_aligned',
                 '2025-01-01','2026-05-31','success',
                 '2026-06-02 10:00:00','2026-06-02 10:00:00'),
                (10,'same-time-lower-id','demo_daily',
                 'blackbox_v2_current_snapshot_as_of',
                 '2025-01-01','2026-05-31','success',
                 '2026-06-02 10:00:00','2026-06-02 10:00:00')"""
            )
        )
        rows = read_selected_backtest_runs(connection, registry_rows)

    assert len(rows) == 1
    assert int(rows[0]["id"]) == 10
    selected = choose_latest_backtest_runs(rows, registry_rows)
    assert int(selected["demo_daily__h1__5Y"]["id"]) == 10


def test_summary_streams_across_fetch_boundaries_without_changing_counts(
    monkeypatch,
) -> None:
    from backend import factor_lab_dashboard as dashboard
    from backend.factor_lab_dashboard_queries import (
        iter_summary_product_predictions,
    )

    engine = _engine()
    monkeypatch.setattr(
        dashboard,
        "iter_summary_product_predictions",
        lambda connection, registry_rows, *, stats, replacement_plan=None: (
            iter_summary_product_predictions(
                connection,
                registry_rows,
                stats=stats,
                replacement_plan=replacement_plan,
                fetch_rows=2,
            )
        ),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (10,'demo_daily','5Y',1,'2026-02-01','2026-01-30',
                 '2026-02-02',1,1,'{}'),
                (11,'demo_daily','5Y',1,'2026-02-02','2026-01-31',
                 '2026-02-02',-1,-1,'{}'),
                (12,'demo_daily','5Y',1,'2026-03-01','2026-02-27',
                 '2026-03-02',0,NULL,'{}')"""
            )
        )

    rows = build_factor_lab_dashboard(engine)["schemes"][0]["monthly_rows"]

    assert ["2026-02", "backtest", 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1] in rows
    assert ["2026-03", "backtest", *([0] * 11)] in rows


def test_streaming_canonical_selection_matches_previous_fixed_oracle() -> None:
    rows = [
        {
            "id": 1,
            "scheme_id": "daily",
            "target_tenor": "5Y",
            "horizon": 1,
            "predict_date": "2026-01-02",
            "feature_date": "2026-01-01",
            "target_date": "2026-01-05",
        },
        {
            "id": 2,
            "scheme_id": "daily",
            "target_tenor": "5Y",
            "horizon": 1,
            "predict_date": "2026-01-03",
            "feature_date": "2026-01-02",
            "target_date": "2026-01-05",
        },
        {
            "id": 3,
            "scheme_id": "weekly",
            "target_tenor": "10Y",
            "horizon": 1,
            "predict_date": "2026-01-10",
            "feature_date": "2026-01-08",
            "target_date": "2026-01-16",
        },
        {
            "id": 4,
            "scheme_id": "weekly",
            "target_tenor": "10Y",
            "horizon": 1,
            "predict_date": "2026-01-09",
            "feature_date": "2026-01-08",
            "target_date": "2026-01-16",
        },
    ]
    task_types = {
        ("daily", "5Y", 1): "T+1",
        ("weekly", "10Y", 1): "weekly_point",
    }
    grouped_rows = sorted(
        rows,
        key=lambda row: (
            row["scheme_id"],
            row["target_tenor"],
            row["horizon"],
            row["target_date"],
            row["predict_date"],
            row["id"],
        ),
    )

    previous = choose_live_prediction_rows(
        rows,
        display_until="2026-12-31",
        task_type_by_scheme=task_types,
    )
    streaming = list(
        iter_grouped_live_prediction_rows(
            grouped_rows,
            display_until="2026-12-31",
            task_type_by_scheme=task_types,
        )
    )

    assert {int(row["id"]) for row in streaming} == {
        int(row["id"]) for row in previous
    } == {2, 4}


def test_summary_still_fails_closed_on_conflicting_actual_facts() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_actuals VALUES "
                "('5Y','2026-06-03',-1,-1)"
            )
        )

    with pytest.raises(DashboardDataError, match="conflicting directions"):
        build_factor_lab_dashboard(engine)


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
        rows = read_product_predictions(
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


def test_detail_does_not_read_unrelated_backtest_run_metadata() -> None:
    engine = _engine()

    def reject_backtest_runs(
        _conn, _cursor, statement, _parameters, _context, _executemany,
    ) -> None:
        if "FROM t_backtest_runs" in statement:
            raise AssertionError("detail must not read backtest run metadata")

    event.listen(engine, "before_cursor_execute", reject_backtest_runs)
    payload = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="all",
    )

    assert payload is not None
    assert len(payload["rows"]) == 2


def test_detail_source_partition_is_pushed_into_target_date_range() -> None:
    engine = _engine()
    prediction_queries: list[str] = []

    def observe_predictions(
        _conn, _cursor, statement, _parameters, _context, _executemany,
    ) -> None:
        if "FROM t_scheme_predictions" in statement:
            prediction_queries.append(statement)

    event.listen(engine, "before_cursor_execute", observe_predictions)

    backtest = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="backtest",
    )
    live = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-01",
        source="live",
    )

    assert backtest is not None and backtest["rows"] == []
    assert live is not None and live["rows"] == []
    assert prediction_queries == []


def test_detail_reads_actual_only_when_selected_fact_needs_join() -> None:
    engine = _engine()
    actual_queries: list[str] = []

    def observe_actuals(
        _conn, _cursor, statement, _parameters, _context, _executemany,
    ) -> None:
        if "FROM t_scheme_actuals" in statement:
            actual_queries.append(statement)

    event.listen(engine, "before_cursor_execute", observe_actuals)
    backtest = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-01",
        source="backtest",
    )
    assert backtest is not None and len(backtest["rows"]) == 1
    assert actual_queries == []

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_scheme_actuals VALUES "
                "('5Y','2026-06-04',1,1)"
            )
        )
    live = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="live",
    )
    assert live is not None and len(live["rows"]) == 2
    assert live["rows"][1][-1] == -1
    assert len(actual_queries) == 1


def test_monthly_average_detail_reverses_display_month_before_source_filter() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE t_scheme_registry SET task_type='monthly_average', "
                "frequency='monthly'"
            )
        )
        connection.execute(
            text(
                """INSERT INTO t_scheme_predictions VALUES
                (8,'demo_daily','5Y',1,'2026-05-29','2026-05-29',
                 '2026-05-31',1,1,'{}')"""
            )
        )

    backtest = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="backtest",
    )
    live = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2026-06",
        source="live",
    )

    assert backtest is not None
    assert [row[3] for row in backtest["rows"]] == ["2026-05-31"]
    assert live is not None and live["rows"] == []


def test_detail_known_scheme_legal_empty_month_returns_rows_empty() -> None:
    engine = _engine()

    payload = build_factor_lab_dashboard_detail(
        engine,
        scheme_id="demo_daily__h1__5Y",
        month="2030-02",
        source="all",
    )

    assert payload is not None
    assert payload["rows"] == []


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

        rows = read_live_actuals(connection, registry_rows)

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


@pytest.fixture(autouse=True)
def production_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(tmp_path))
    path = tmp_path / "config" / "production_schemes.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    return path


def test_production_membership_replaces_without_changing_facts(production_file, caplog):
    import json

    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO t_scheme_registry
            SELECT 'demo_daily__h1__10Y', base_scheme_id, runtime_type, name,
            owner, description, horizon, task_type, frequency, '10Y', 'paused', deployed_at
            FROM t_scheme_registry"""))
    baseline = build_factor_lab_dashboard(engine)["schemes"]
    active = "demo_daily__h1__5Y"
    paused = "demo_daily__h1__10Y"
    production_file.write_text(json.dumps({"scheme_ids": [active, paused, "absent"]}))
    selected = build_factor_lab_dashboard(engine)["schemes"]
    assert selected == [{**baseline[0], "is_production": True}]
    assert "absent" in caplog.text and paused not in caplog.text
    production_file.write_text(json.dumps({"scheme_ids": [paused]}))
    assert build_factor_lab_dashboard(engine)["schemes"] == baseline
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_scheme_registry SET status='active' WHERE scheme_id=:id"), {"id": paused})
        conn.execute(text("INSERT INTO t_target_registry VALUES ('10Y','10年','bond','yield',2,'active','{}')"))
    restored = build_factor_lab_dashboard(engine)["schemes"]
    assert {row["scheme_id"] for row in restored if row["is_production"]} == {paused}
    for content in ("{}", '{"scheme_ids": []}', 'broken', '{"scheme_ids": ["demo_daily__h1__5Y"]}'):
        production_file.write_text(content)
        result = build_factor_lab_dashboard(engine)["schemes"]
        assert {row["scheme_id"] for row in result if row["is_production"]} == (
            {active} if content.startswith('{"scheme_ids": ["') else set()
        )
    production_file.unlink()
    assert not any(row["is_production"] for row in build_factor_lab_dashboard(engine)["schemes"])
    engine.dispose()


@pytest.mark.parametrize("payload", [[], {"other": []}, {"scheme_ids": None},
    {"scheme_ids": [1]}, {"scheme_ids": [""]}, {"scheme_ids": [" A"]},
    {"scheme_ids": ["A", "A"]}])
def test_production_structure_errors_hide_markers(production_file, payload):
    import json

    production_file.write_text(json.dumps(payload))
    engine = _engine()
    assert build_factor_lab_dashboard(engine)["schemes"][0]["is_production"] is False
    engine.dispose()


@pytest.mark.parametrize("root", [None, "relative", "/nonexistent-marker-runtime"])
def test_production_runtime_has_no_fallback(monkeypatch, root):
    from backend import factor_lab_dashboard as dashboard

    if root is None:
        monkeypatch.delenv("BFL_RUNTIME_ROOT")
    else:
        monkeypatch.setenv("BFL_RUNTIME_ROOT", root)
    monkeypatch.setattr(dashboard, "resolve_runtime_state_path", lambda **_: pytest.fail("fallback"))
    engine = _engine()
    assert build_factor_lab_dashboard(engine)["schemes"][0]["is_production"] is False
    engine.dispose()


def test_marker_lookup_is_batched_and_database_failure_is_not_hidden(production_file):
    from sqlalchemy import event

    engine = _engine()
    lookups = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        if "WHERE scheme_id IN" in statement:
            lookups.append(statement)
            if len(lookups) > 1:
                raise RuntimeError("marker database failure")
    event.listen(engine, "before_cursor_execute", observe)
    build_factor_lab_dashboard(engine)
    assert lookups == []
    production_file.write_text('{"scheme_ids": ["demo_daily__h1__5Y", "absent"]}')
    build_factor_lab_dashboard(engine)
    assert len(lookups) == 1
    build_factor_lab_dashboard_detail(engine, scheme_id="demo_daily__h1__5Y", month="2026-06", source="all")
    assert len(lookups) == 1
    with pytest.raises(RuntimeError, match="marker database failure"):
        build_factor_lab_dashboard(engine)
    engine.dispose()
