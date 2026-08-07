from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine


SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPTURED_AT = datetime(2026, 7, 22, 12, 34, 56, tzinfo=SHANGHAI)


@dataclass
class SqlTrace:
    checkouts: list[int] = field(default_factory=list)
    checkins: list[int] = field(default_factory=list)
    connection_ids: list[int] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    parameters: list[Any] = field(default_factory=list)
    transaction_flags: list[bool] = field(default_factory=list)

    def reset(self) -> None:
        self.checkouts.clear()
        self.checkins.clear()
        self.connection_ids.clear()
        self.statements.clear()
        self.parameters.clear()
        self.transaction_flags.clear()


def _create_dashboard_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT NOT NULL,
                    runtime_type TEXT,
                    name TEXT NOT NULL,
                    description TEXT,
                    horizon INTEGER NOT NULL,
                    task_type TEXT,
                    frequency TEXT,
                    target_tenor TEXT NOT NULL,
                    schedule_cron TEXT,
                    schedule_timezone TEXT,
                    status TEXT NOT NULL,
                    deployed_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_versions (
                    scheme_id TEXT NOT NULL,
                    scheme_version TEXT NOT NULL,
                    runtime_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_at TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_target_registry (
                    target_code TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    asset_class TEXT,
                    target_type TEXT,
                    sort_order INTEGER,
                    status TEXT NOT NULL,
                    extra TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER PRIMARY KEY,
                    benchmark_id TEXT NOT NULL,
                    scheme_id TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT,
                    report_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE VIEW v_latest_backtest_run AS
                SELECT *
                FROM (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY benchmark_id, scheme_id, data_source
                               ORDER BY updated_at DESC, id DESC
                           ) AS latest_rank
                    FROM t_backtest_runs r
                    WHERE status = 'success'
                ) ranked
                WHERE latest_rank = 1
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    benchmark_id TEXT NOT NULL,
                    scheme_id TEXT NOT NULL,
                    target_tenor TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    predict_date TEXT NOT NULL,
                    feature_date TEXT,
                    target_date TEXT,
                    label INTEGER,
                    predicted_direction INTEGER,
                    confidence REAL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY,
                    scheme_id TEXT NOT NULL,
                    scheme_version TEXT,
                    target_tenor TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    predict_date TEXT NOT NULL,
                    feature_date TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    prediction_phase TEXT NOT NULL,
                    predicted_direction INTEGER NOT NULL,
                    extra TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_runs (
                    scheme_id TEXT NOT NULL,
                    scheme_version TEXT,
                    predict_date TEXT NOT NULL,
                    prediction_phase TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_trade_calendar (
                    rdate TEXT NOT NULL,
                    trade_flag TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE api_wind_date (
                    rdate TEXT NOT NULL,
                    week_id INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_actuals (
                    tenor TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    direction_1d INTEGER,
                    direction_5d INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_weekly_actuals (
                    tenor TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    direction_weekly INTEGER,
                    target_rule TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE t_scheme_monthly_actuals (
                    tenor TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    direction_monthly INTEGER,
                    target_rule TEXT
                )
                """
            )
        )


def _seed_dashboard_rows(engine: Engine) -> None:
    registry_rows = [
        {
            "scheme_id": "daily_t1__h5__5Y",
            "base_scheme_id": "daily_t1",
            "runtime_type": "native_adapter",
            "name": "日频 T+1",
            "horizon": 5,
            "task_type": "T+1",
            "frequency": "weekly",
            "target_tenor": "5Y",
            "status": "active",
        },
        {
            "scheme_id": "daily_t5__h1__5Y",
            "base_scheme_id": "daily_t5",
            "runtime_type": "native_adapter",
            "name": "日频 T+5",
            "horizon": 1,
            "task_type": "T+5",
            "frequency": "monthly",
            "target_tenor": "5Y",
            "status": "active",
        },
        {
            "scheme_id": "weekly_point__h1__10Y",
            "base_scheme_id": "weekly_point",
            "runtime_type": "blackbox_v2",
            "name": "周度单点",
            "horizon": 1,
            "task_type": "weekly_point",
            "frequency": "daily",
            "target_tenor": "10Y",
            "status": "active",
        },
        {
            "scheme_id": "weekly_average__h1__10Y",
            "base_scheme_id": "weekly_average",
            "runtime_type": "native_adapter",
            "name": "周度均值",
            "horizon": 1,
            "task_type": "weekly_average",
            "frequency": "daily",
            "target_tenor": "10Y",
            "status": "active",
        },
        {
            "scheme_id": "monthly__h1__10Y",
            "base_scheme_id": "monthly",
            "runtime_type": "native_adapter",
            "name": "月频",
            "horizon": 1,
            "task_type": "monthly",
            "frequency": "daily",
            "target_tenor": "10Y",
            "status": "active",
        },
        {
            "scheme_id": "paused__h1__5Y",
            "base_scheme_id": "paused",
            "runtime_type": "native_adapter",
            "name": "暂停方案",
            "horizon": 1,
            "task_type": "T+1",
            "frequency": "daily",
            "target_tenor": "5Y",
            "status": "paused",
        },
    ]
    for row in registry_rows:
        row["description"] = ""
        row["deployed_at"] = "2026-06-09"
        row["schedule_cron"] = "0 7 * * 1-5"
        row["schedule_timezone"] = "Asia/Shanghai"
        row["created_at"] = "2026-06-01T09:00:00"
        row["updated_at"] = "2026-06-09T09:00:00"

    predictions = [
        (10, "daily_t1", "5Y", 5, "2026-07-20", "2026-07-17", "2026-07-21", "gray_live", -1, "{}"),
        (
            11, "daily_t1", "5Y", 5, "2026-07-20", "2026-07-17",
            "2026-07-21", "scheduled_live", 1, "{}",
        ),
        (
            12, "daily_t1", "5Y", 5, "2026-07-21", "2026-07-20",
            "2026-08-01", "scheduled_live", -1, "{}",
        ),
        (
            13, "daily_t1", "5Y", 5, "2026-07-23", "2026-07-22",
            "2026-08-03", "scheduled_live", 1, "{}",
        ),
        (
            20, "daily_t5", "5Y", 1, "2026-07-20", "2026-07-17",
            "2026-07-21", "scheduled_live", -1, "{}",
        ),
        (
            30, "weekly_point", "10Y", 1, "2026-07-12", "2026-07-10",
            "2026-07-18", "scheduled_live", 1, '{"frequency":"weekly"}',
        ),
        (
            40, "weekly_average", "10Y", 1, "2026-07-12", "2026-07-10",
            "2026-07-18", "gray_live", -1, '{"frequency":"weekly"}',
        ),
        (
            50, "monthly", "10Y", 1, "2026-06-15", "2026-06-15",
            "2026-07-15", "gray_live", 1, '{"frequency":"monthly"}',
        ),
        (
            60, "paused", "5Y", 1, "2026-07-20", "2026-07-17",
            "2026-07-21", "scheduled_live", 1, "{}",
        ),
    ]

    with engine.begin() as connection:
        calendar_rows = []
        calendar_date = date(2026, 5, 1)
        calendar_end = date(2026, 8, 31)
        while calendar_date <= calendar_end:
            calendar_rows.append(
                {
                    "rdate": calendar_date.isoformat(),
                    "trade_flag": "1" if calendar_date.weekday() < 5 else "0",
                    "week_id": calendar_date.isocalendar().week,
                }
            )
            calendar_date += timedelta(days=1)
        connection.execute(
            text(
                "INSERT INTO t_trade_calendar (rdate, trade_flag) "
                "VALUES (:rdate, :trade_flag)"
            ),
            calendar_rows,
        )
        connection.execute(
            text(
                "INSERT INTO api_wind_date (rdate, week_id) "
                "VALUES (:rdate, :week_id)"
            ),
            calendar_rows,
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                    (scheme_id, scheme_version, runtime_type, status, approved_at)
                VALUES
                    ('weekly_point', 'current', 'blackbox_v2', 'active',
                     '2026-06-01T09:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_target_registry
                    (target_code, display_name, asset_class, target_type,
                     sort_order, status, extra, created_at, updated_at)
                VALUES
                    ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 1,
                     'active', '{}', NULL, NULL),
                    ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 2,
                     'active', '{}', NULL, NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, runtime_type, name, description,
                     horizon, task_type, frequency, target_tenor, schedule_cron,
                     schedule_timezone, status, deployed_at, created_at, updated_at)
                VALUES
                    (:scheme_id, :base_scheme_id, :runtime_type, :name,
                     :description, :horizon, :task_type, :frequency,
                     :target_tenor, :schedule_cron, :schedule_timezone,
                     :status, :deployed_at, :created_at, :updated_at)
                """
            ),
            registry_rows,
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, scheme_id, target_tenor, horizon, predict_date,
                     feature_date, target_date, prediction_phase,
                     predicted_direction, extra)
                VALUES
                    (:id, :scheme_id, :target_tenor, :horizon, :predict_date,
                     :feature_date, :target_date, :prediction_phase,
                     :predicted_direction, :extra)
                """
            ),
            [
                {
                    "id": row[0],
                    "scheme_id": row[1],
                    "target_tenor": row[2],
                    "horizon": row[3],
                    "predict_date": row[4],
                    "feature_date": row[5],
                    "target_date": row[6],
                    "prediction_phase": row[7],
                    "predicted_direction": row[8],
                    "extra": row[9],
                }
                for row in predictions
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_actuals
                    (tenor, trade_date, direction_1d, direction_5d)
                VALUES
                    ('5Y', '2026-07-21', 1, -1),
                    ('10Y', '2026-07-15', -1, -1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_weekly_actuals
                    (tenor, target_date, direction_weekly, target_rule)
                VALUES
                    ('10Y', '2026-07-18', 1,
                     'next_week_last_trading_day_vs_current_week_last_trading_day'),
                    ('10Y', '2026-07-18', -1,
                     'next_week_average_yield_vs_current_week_average_yield')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_monthly_actuals
                    (tenor, target_date, direction_monthly, target_rule)
                VALUES
                    ('10Y', '2026-07-15', 1,
                     'next_month_observation_yield_vs_feature_month_observation_yield'),
                    ('10Y', '2026-07-15', 1,
                     'next_month_observation_yield_vs_feature_month_observation_yield')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (100, 'native-old', 'daily_t1', 'framework_db_aligned',
                     '2025-01-01', '2026-05-30', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T12:00:00'),
                    (101, 'native-old', 'daily_t1', 'framework_db_aligned',
                     '2025-01-01', '2026-05-31', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T12:00:00'),
                    (102, 'native-old', 'daily_t1', 'framework_db_aligned',
                     '2025-01-01', '2026-06-01', 'failed', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T12:00:00'),
                    (103, 'native-new', 'daily_t1', 'framework_db_aligned',
                     '2025-01-01', '2026-05-31', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T11:00:00'),
                    (104, 'native-new', 'daily_t1', 'framework_db_aligned',
                     '2025-01-01', '2026-06-02', 'partial', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T13:00:00'),
                    (105, 'native-new', 'daily_t1',
                     'blackbox_v2_current_snapshot_as_of', '2025-01-01',
                     '2026-06-03', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T14:00:00'),
                    (200, 'blackbox-old', 'weekly_point',
                     'blackbox_v2_current_snapshot_as_of', '2025-01-01',
                     '2026-05-30', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T11:00:00'),
                    (201, 'blackbox-new', 'weekly_point',
                     'blackbox_v2_current_snapshot_as_of', '2025-01-01',
                     '2026-05-31', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T11:00:00'),
                    (202, 'blackbox-new', 'weekly_point',
                     'framework_db_aligned', '2025-01-01', '2026-06-01',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T12:00:00'),
                    (203, 'blackbox-new', 'weekly_point',
                     'blackbox_v2_current_snapshot_as_of', '2025-01-01',
                     '2026-06-02', 'failed', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T13:00:00'),
                    (300, 'weekly-average', 'weekly_average',
                     'framework_db_aligned', '2025-01-01', '2026-05-31',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T10:00:00'),
                    (400, 'monthly-current', 'monthly',
                     'framework_db_aligned', '2025-01-01', '2026-05-31',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T10:00:00'),
                    (500, 'daily-t5-failed', 'daily_t5',
                     'framework_db_aligned', '2025-01-01', '2026-05-31',
                     'failed', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T15:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (1001, 100, 'native-old', 'daily_t1', '5Y', 5,
                     '2026-05-20', '2026-05-20', '2026-05-27', 1, -1, NULL),
                    (1011, 101, 'native-old', 'daily_t1', '5Y', 5,
                     '2026-05-21', '2026-05-21', '2026-05-28', 1, 1, NULL),
                    (1012, 101, 'native-old', 'daily_t1', '5Y', 5,
                     '2026-05-22', '2026-05-22', '2026-05-29', -1, -1, NULL),
                    (1031, 103, 'native-new', 'daily_t1', '5Y', 5,
                     '2026-05-22', '2026-05-22', '2026-05-29', -1, -1, NULL),
                    (1033, 103, 'native-new', 'daily_t1', '5Y', 5,
                     '2026-05-21', '2026-05-21', '2026-05-28', 1, -1, NULL),
                    (1032, 101, 'native-old', 'daily_t1', '30Y', 5,
                     '2026-05-22', '2026-05-22', '2026-05-29', 1, 1, NULL),
                    (1051, 105, 'native-new', 'daily_t1', '5Y', 5,
                     '2026-05-23', '2026-05-23', '2026-05-30', 1, 1, NULL),
                    (2001, 200, 'blackbox-old', 'weekly_point', '10Y', 1,
                     '2026-05-20', '2026-05-20', '2026-05-27', -1, -1, NULL),
                    (2011, 201, 'blackbox-new', 'weekly_point', '10Y', 1,
                     '2026-05-21', '2026-05-21', '2026-05-28', 1, 1, NULL),
                    (2021, 202, 'blackbox-new', 'weekly_point', '10Y', 1,
                     '2026-05-22', '2026-05-22', '2026-05-29', -1, -1, NULL),
                    (3001, 300, 'weekly-average', 'weekly_average', '10Y', 1,
                     '2026-05-21', '2026-05-21', '2026-05-28', -1, 1, NULL),
                    (4001, 400, 'monthly-current', 'monthly', '10Y', 1,
                     '2026-04-15', '2026-04-15', '2026-05-15', 0, 0, NULL)
                """
            )
        )


def _attach_trace(engine: Engine) -> SqlTrace:
    trace = SqlTrace()

    @event.listens_for(engine, "checkout")
    def _checkout(dbapi_connection: Any, *_args: Any) -> None:
        trace.checkouts.append(id(dbapi_connection))

    @event.listens_for(engine, "checkin")
    def _checkin(dbapi_connection: Any, *_args: Any) -> None:
        trace.checkins.append(id(dbapi_connection))

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        trace.connection_ids.append(id(connection.connection.driver_connection))
        trace.statements.append(statement)
        trace.parameters.append(parameters)
        trace.transaction_flags.append(connection.in_transaction())

    return trace


@pytest.fixture
def dashboard_db() -> Iterator[tuple[Engine, SqlTrace]]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_dashboard_schema(engine)
    _seed_dashboard_rows(engine)
    trace = _attach_trace(engine)
    yield engine, trace
    engine.dispose()


def _schemes_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {scheme["scheme_id"]: scheme for scheme in payload["schemes"]}


def test_builds_live_snapshot_from_active_registry_task_types(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard

    engine, trace = dashboard_db

    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert payload["schema_version"] == "factor-lab-dashboard-v1"
    assert isinstance(payload["snapshot_id"], str) and payload["snapshot_id"]
    assert payload["generated_at"] == "2026-07-22T12:34:56+08:00"
    assert payload["display_until"] == "2026-07-22"
    assert payload["stale"] is False
    assert payload["snapshot_age_ms"] == 0
    assert payload["target_labels"] == {
        "5Y": "5Y国债活跃",
        "10Y": "10Y国债活跃",
    }

    schemes = _schemes_by_id(payload)
    assert set(schemes) == {
        "daily_t1__h5__5Y",
        "daily_t5__h1__5Y",
        "weekly_point__h1__10Y",
        "weekly_average__h1__10Y",
        "monthly__h1__10Y",
    }
    assert {(item["target_tenor"], item["task_type"]) for item in schemes.values()} == {
        ("5Y", "T+1"),
        ("5Y", "T+5"),
        ("10Y", "weekly_point"),
        ("10Y", "weekly_average"),
        ("10Y", "monthly"),
    }
    assert schemes["daily_t1__h5__5Y"]["target_label"] == "5Y国债活跃"
    assert schemes["daily_t1__h5__5Y"]["signal_status"] == "missing"
    assert (
        schemes["daily_t1__h5__5Y"]["signal_failure_category"] == "no_run"
    )

    assert schemes["daily_t1__h5__5Y"]["live_rows"] == [
        ["2026-07-20", "2026-07-17", "2026-07-21", "scheduled_live", 1, 1],
        ["2026-07-21", "2026-07-20", "2026-08-01", "scheduled_live", -1, None],
    ]
    assert schemes["daily_t5__h1__5Y"]["live_rows"][0][-1] == -1
    assert schemes["weekly_point__h1__10Y"]["live_rows"][0][-1] == 1
    assert schemes["weekly_average__h1__10Y"]["live_rows"][0][-1] == -1
    assert schemes["monthly__h1__10Y"]["live_rows"] == [
        ["2026-06-15", "2026-06-15", "2026-07-15", "gray_live", 1, 1]
    ]
    assert schemes["daily_t5__h1__5Y"]["backtest"] is None
    assert schemes["daily_t1__h5__5Y"]["backtest"] == {
        "benchmark_id": "native-old",
        "benchmark_label": "native-old",
        "data_source": "framework_db_aligned",
        "data_source_label": "当前DB对齐回测",
        "latest_run_date": "2026-05-31",
        "rows": [
            ["2026-05-21", "2026-05-21", "2026-05-28", None, 1, 1],
            ["2026-05-22", "2026-05-22", "2026-05-29", None, -1, -1],
        ],
    }
    assert schemes["weekly_point__h1__10Y"]["backtest"]["benchmark_id"] == (
        "blackbox-new"
    )
    assert schemes["monthly__h1__10Y"]["backtest"]["rows"] == [
        ["2026-04-15", "2026-04-15", "2026-05-15", None, 0, 0]
    ]
    for scheme in schemes.values():
        backtest = scheme["backtest"]
        if backtest is not None:
            assert set(backtest) == {
                "benchmark_id",
                "benchmark_label",
                "data_source",
                "data_source_label",
                "latest_run_date",
                "rows",
            }
    assert [scheme["scheme_id"] for scheme in payload["schemes"]] == sorted(schemes)

    # 未来 target 保留、未来 predict 过滤；月份按 target_date，不按 predict 月裁切。
    assert all(
        row[0] <= payload["display_until"]
        for scheme in schemes.values()
        for row in scheme["live_rows"]
    )
    assert any(
        row[2] > payload["display_until"]
        for scheme in schemes.values()
        for row in scheme["live_rows"]
    )
    assert schemes["monthly__h1__10Y"]["live_rows"][0][0][:7] == "2026-06"
    assert schemes["monthly__h1__10Y"]["live_rows"][0][2][:7] == "2026-07"

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1
    assert len(set(trace.connection_ids)) == 1
    assert trace.transaction_flags and all(trace.transaction_flags)
    assert len(trace.statements) == 11
    assert sum(
        statement.lstrip().upper().startswith("SELECT")
        for statement in trace.statements
    ) <= 11
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in trace.statements)
    assert not any(
        re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", statement, re.I)
        for statement in trace.statements
    )
    assert not any(
        "START TRANSACTION WITH CONSISTENT SNAPSHOT" in statement.upper()
        for statement in trace.statements
    )
    assert not any("daily_t1" in statement for statement in trace.statements)
    assert any("daily_t1" in str(parameters) for parameters in trace.parameters)


def test_dashboard_excludes_all_pre_policy_rows_without_deleting_audit_data(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import (
        build_factor_lab_dashboard,
        dashboard_build_diagnostics,
    )

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (1010, 101, 'native-old', 'daily_t1', '5Y', 5,
                     '2024-12-27', '2024-12-27', '2025-01-03', 1, 1, NULL),
                    (2010, 201, 'blackbox-new', 'weekly_point', '10Y', 1,
                     '2024-12-27', '2024-12-27', '2025-01-03', 1, 1, NULL),
                    (4010, 400, 'monthly-current', 'monthly', '10Y', 1,
                     '2024-12-15', '2024-12-15', '2025-01-15', 1, 1, NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, scheme_id, target_tenor, horizon, predict_date,
                     feature_date, target_date, prediction_phase,
                     predicted_direction, extra)
                VALUES
                    (70, 'daily_t1', '5Y', 5, '2024-12-27', '2024-12-26',
                     '2025-01-03', 'gray_live', 1, '{}'),
                    (71, 'weekly_point', '10Y', 1, '2024-12-29',
                     '2024-12-27', '2025-01-03', 'gray_live', 1,
                     '{"frequency":"weekly"}'),
                    (72, 'monthly', '10Y', 1, '2024-12-15', '2024-12-15',
                     '2025-01-15', 'gray_live', 1,
                     '{"frequency":"monthly"}')
                """
            )
        )

    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    schemes = _schemes_by_id(payload)
    diagnostics = dashboard_build_diagnostics(payload["snapshot_id"])

    assert all(
        row[0] >= "2025-01-01"
        for scheme in schemes.values()
        for row in (
            list(scheme["live_rows"])
            + list((scheme["backtest"] or {}).get("rows") or [])
        )
    )
    assert diagnostics is not None
    assert diagnostics["history_backtest_rows_excluded_before_policy_start"] == 3
    assert diagnostics["history_live_rows_excluded_before_policy_start"] == 3
    with engine.connect() as connection:
        stored_backtest = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_backtest_predictions
                WHERE run_id IN (101, 201, 400)
                  AND predict_date < '2025-01-01'
                """
            )
        ).scalar_one()
        stored_live = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_scheme_predictions
                WHERE id IN (70, 71, 72)
                  AND predict_date < '2025-01-01'
                """
            )
        ).scalar_one()
    assert stored_backtest == 3
    assert stored_live == 3


def test_weekly_policy_filter_still_validates_excluded_audit_row_structure(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (2010, 201, 'blackbox-new', 'weekly_point', '10Y', 99,
                     '2024-12-27', '2024-12-27', '2025-01-03', 1, 1, NULL)
                """
            )
        )

    with pytest.raises(
        DashboardDataError,
        match="backtest detail horizon does not match Registry",
    ):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)


def test_weekly_coverage_drift_is_diagnostic_and_pending_counts_as_signal(
    dashboard_db: tuple[Engine, SqlTrace],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.factor_lab_dashboard import (
        build_factor_lab_dashboard,
        dashboard_build_diagnostics,
    )

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, runtime_type, name, description,
                     horizon, task_type, frequency, target_tenor, schedule_cron,
                     schedule_timezone, status, deployed_at, created_at, updated_at)
                VALUES
                    ('weekly_point_other__h1__10Y', 'weekly_point_other',
                     'blackbox_v2', '周度单点缺口', '', 1, 'weekly_point',
                     'weekly', '10Y', '0 7 * * 1', 'Asia/Shanghai', 'active',
                     '2026-07-22', '2026-07-22T09:00:00',
                     '2026-07-22T09:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (600, 'weekly-other', 'weekly_point_other',
                     'blackbox_v2_current_snapshot_as_of', '2025-01-01',
                     '2026-05-31', 'success', '{}', NULL,
                     '2026-07-22T09:00:00', '2026-07-22T10:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (6001, 600, 'weekly-other', 'weekly_point_other', '10Y', 1,
                     '2026-05-21', '2026-05-21', '2026-05-28', 1, 1, NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, scheme_id, target_tenor, horizon, predict_date,
                     feature_date, target_date, prediction_phase,
                     predicted_direction, extra)
                VALUES
                    (31, 'weekly_point', '10Y', 1, '2026-07-20',
                     '2026-07-18', '2026-07-25', 'gray_live', -1,
                     '{"frequency":"weekly"}')
                """
            )
        )

    with caplog.at_level(
        logging.WARNING,
        logger="backend.factor_lab_dashboard",
    ):
        payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    diagnostics = dashboard_build_diagnostics(payload["snapshot_id"])

    assert diagnostics is not None
    coverage = diagnostics["weekly_coverage"]
    primary = coverage["candidates"]["weekly_point__h1__10Y"]
    assert primary["signal_count"] == 3
    assert primary["valid_sample_count"] == 2
    drift = next(
        item
        for item in coverage["drifts"]
        if item["scheme_id"] == "weekly_point_other__h1__10Y"
    )
    assert drift["reference_scheme_id"] == "weekly_point__h1__10Y"
    assert drift["missing_target_dates"] == ["2026-07-18", "2026-07-25"]
    assert drift["extra_target_dates"] == []
    assert "factor_lab_weekly_coverage_drift" in caplog.text


def test_choose_latest_backtest_runs_applies_two_stage_runtime_selection(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    import backend.factor_lab_dashboard_semantics as semantics

    assert hasattr(semantics, "choose_latest_backtest_runs")
    chooser = semantics.choose_latest_backtest_runs
    engine, _trace = dashboard_db
    with engine.connect() as connection:
        registry_rows = connection.execute(
            text(
                """
                SELECT scheme_id, base_scheme_id, runtime_type, target_tenor,
                       horizon, status
                FROM t_scheme_registry
                WHERE status = 'active'
                """
            )
        ).mappings().all()
        run_rows = connection.execute(
            text("SELECT * FROM t_backtest_runs")
        ).mappings().all()

    selected = chooser(run_rows, registry_rows)

    assert {scheme_id: int(row["id"]) for scheme_id, row in selected.items()} == {
        "daily_t1__h5__5Y": 101,
        "weekly_point__h1__10Y": 201,
        "weekly_average__h1__10Y": 300,
        "monthly__h1__10Y": 400,
    }
    assert "daily_t5__h1__5Y" not in selected


def test_successful_rebuild_gets_new_snapshot_id_and_preserves_empty_sources(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM t_scheme_predictions WHERE scheme_id = 'daily_t5'")
        )

    first = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    second = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert first["snapshot_id"] != second["snapshot_id"]
    scheme = _schemes_by_id(first)["daily_t5__h1__5Y"]
    assert scheme["live_rows"] == []
    assert scheme["backtest"] is None


def test_choose_latest_backtest_runs_rejects_inconsistent_runtime_for_base() -> None:
    import backend.factor_lab_dashboard_semantics as semantics

    assert hasattr(semantics, "choose_latest_backtest_runs")
    registry_rows = [
        {
            "scheme_id": "multi__h1__5Y",
            "base_scheme_id": "multi",
            "runtime_type": "native_adapter",
            "target_tenor": "5Y",
            "horizon": 1,
        },
        {
            "scheme_id": "multi__h1__10Y",
            "base_scheme_id": "multi",
            "runtime_type": "blackbox_v2",
            "target_tenor": "10Y",
            "horizon": 1,
        },
    ]

    with pytest.raises(
        semantics.DashboardDataError,
        match="runtime_type is inconsistent",
    ):
        semantics.choose_latest_backtest_runs([], registry_rows)


def test_dashboard_and_legacy_default_backtest_projections_are_equal(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.services import backtest_factor_lab_results

    engine, trace = dashboard_db
    dashboard = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    dashboard_selects = sum(
        statement.lstrip().upper().startswith("SELECT")
        for statement in trace.statements
    )
    legacy = backtest_factor_lab_results(engine)

    legacy_projection = {
        (
            scheme["scheme_id"],
            row["target_date"],
            row["predicted_direction"],
            row["actual_direction"],
        )
        for scheme in legacy["schemes"]
        for row in scheme["daily_rows"]
    }
    row_index = {
        field: index for index, field in enumerate(dashboard["row_fields"])
    }
    dashboard_projection = {
        (
            scheme["scheme_id"],
            row[row_index["target_date"]],
            row[row_index["predicted_direction"]],
            row[row_index["actual_direction"]],
        )
        for scheme in dashboard["schemes"]
        if scheme["backtest"] is not None
        for row in scheme["backtest"]["rows"]
    }

    assert dashboard_projection == legacy_projection
    assert len(dashboard["schemes"]) == 5
    assert len(legacy["schemes"]) == sum(
        scheme["backtest"] is not None for scheme in dashboard["schemes"]
    ) == 4
    assert sum(len(scheme["live_rows"]) for scheme in dashboard["schemes"]) == 6
    assert sum(
        len(scheme["backtest"]["rows"])
        for scheme in dashboard["schemes"]
        if scheme["backtest"] is not None
    ) == len(legacy_projection) == 5
    assert dashboard_selects <= 11


def test_legacy_default_backtest_order_is_stable_for_shuffled_inputs(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services as services

    engine, _trace = dashboard_db
    explicit_before = services.backtest_factor_lab_results(
        engine,
        data_source="framework_db_aligned",
    )
    benchmark_before = services.backtest_factor_lab_results(
        engine,
        benchmark_id="native-old",
    )
    scheme_meta = services._backtest_scheme_meta(engine)
    candidate_rows = services._backtest_run_candidates(
        engine,
        base_scheme_ids=sorted(
            {str(row["base_scheme_id"]) for row in scheme_meta.values()}
        ),
    )
    monkeypatch.setattr(
        services,
        "_backtest_scheme_meta",
        lambda _engine: dict(reversed(list(scheme_meta.items()))),
    )
    monkeypatch.setattr(
        services,
        "_backtest_run_candidates",
        lambda _engine, *, base_scheme_ids: list(reversed(candidate_rows)),
    )

    default = services.backtest_factor_lab_results(engine)
    explicit_after = services.backtest_factor_lab_results(
        engine,
        data_source="framework_db_aligned",
    )
    benchmark_after = services.backtest_factor_lab_results(
        engine,
        benchmark_id="native-old",
    )

    default_scheme_ids = [item["scheme_id"] for item in default["schemes"]]
    assert default_scheme_ids == sorted(default_scheme_ids)
    assert [item["scheme_id"] for item in explicit_after["schemes"]] == [
        item["scheme_id"] for item in explicit_before["schemes"]
    ]
    assert [item["scheme_id"] for item in benchmark_after["schemes"]] == [
        item["scheme_id"] for item in benchmark_before["schemes"]
    ]


def test_selected_backtest_run_without_any_detail_fails_closed(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (600, 'native-corrupt', 'daily_t1',
                     'framework_db_aligned', '2025-01-01', '2026-06-01',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T16:00:00')
                """
            )
        )
    trace.reset()

    with pytest.raises(DashboardDataError, match="daily_t1.*5Y.*no detail"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1
    assert sum(
        statement.lstrip().upper().startswith("SELECT")
        for statement in trace.statements
    ) <= 11


def test_selected_multi_target_run_missing_one_active_target_fails_closed(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError
    from backend.services import backtest_factor_lab_results

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, runtime_type, name, description,
                     horizon, task_type, frequency, target_tenor, schedule_cron,
                     schedule_timezone, status, deployed_at, created_at, updated_at)
                VALUES
                    ('multi__h1__5Y', 'multi', 'native_adapter', 'Multi 5Y',
                     '', 1, 'T+1', 'daily', '5Y', '0 7 * * 1-5',
                     'Asia/Shanghai', 'active', '2026-06-09', NULL, NULL),
                    ('multi__h1__10Y', 'multi', 'native_adapter', 'Multi 10Y',
                     '', 1, 'T+1', 'daily', '10Y', '0 7 * * 1-5',
                     'Asia/Shanghai', 'active', '2026-06-09', NULL, NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (700, 'multi-current', 'multi', 'framework_db_aligned',
                     '2025-01-01', '2026-05-31', 'success', '{}', NULL,
                     '2026-07-20T09:00:00', '2026-07-20T10:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (7001, 700, 'multi-current', 'multi', '5Y', 1,
                     '2026-05-20', '2026-05-20', '2026-05-21', 1, 1, NULL)
                """
            )
        )
    trace.reset()

    with pytest.raises(DashboardDataError, match="multi.*10Y.*no detail"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1

    with pytest.raises(ValueError, match="multi.*10Y.*no detail"):
        backtest_factor_lab_results(engine)

    explicit = backtest_factor_lab_results(
        engine,
        benchmark_id="multi-current",
    )
    assert [scheme["scheme_id"] for scheme in explicit["schemes"]] == [
        "multi__h1__5Y"
    ]


def test_selected_backtest_detail_horizon_must_match_registry(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError
    from backend.services import backtest_factor_lab_results

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (601, 'native-wrong-horizon', 'daily_t1',
                     'framework_db_aligned', '2025-01-01', '2026-06-01',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T16:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (6011, 601, 'native-wrong-horizon', 'daily_t1', '5Y', 1,
                     '2026-05-20', '2026-05-20', '2026-05-21', 1, 1, NULL)
                """
            )
        )

    with pytest.raises(DashboardDataError, match="horizon.*daily_t1__h5__5Y"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    with pytest.raises(ValueError, match="horizon.*daily_t1__h5__5Y"):
        backtest_factor_lab_results(engine)

    explicit = backtest_factor_lab_results(
        engine,
        benchmark_id="native-wrong-horizon",
    )
    assert explicit["schemes"][0]["horizon"] == 1


def test_all_selected_backtest_detail_horizons_must_match_registry(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError
    from backend.services import backtest_factor_lab_results

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, benchmark_id, scheme_id, data_source, start_date,
                     end_date, status, summary, report_path, created_at,
                     updated_at)
                VALUES
                    (602, 'native-mixed-horizon', 'daily_t1',
                     'framework_db_aligned', '2025-01-01', '2026-06-01',
                     'success', '{}', NULL, '2026-07-20T09:00:00',
                     '2026-07-20T17:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (6021, 602, 'native-mixed-horizon', 'daily_t1', '5Y', 5,
                     '2026-05-20', '2026-05-20', '2026-05-21', 1, 1, NULL),
                    (6022, 602, 'native-mixed-horizon', 'daily_t1', '5Y', 1,
                     '2026-05-21', '2026-05-21', '2026-05-22', -1, -1, NULL)
                """
            )
        )

    with pytest.raises(DashboardDataError, match="horizon.*daily_t1__h5__5Y"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    with pytest.raises(ValueError, match="horizon.*daily_t1__h5__5Y"):
        backtest_factor_lab_results(engine)

    explicit = backtest_factor_lab_results(
        engine,
        benchmark_id="native-mixed-horizon",
    )
    assert explicit["schemes"][0]["horizon"] == 5
    assert [
        row["horizon"] for row in explicit["schemes"][0]["daily_rows"]
    ] == [5, 1]


def test_duplicate_backtest_prediction_point_fails_closed(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, _trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_backtest_predictions
                    (id, run_id, benchmark_id, scheme_id, target_tenor,
                     horizon, predict_date, feature_date, target_date, label,
                     predicted_direction, confidence)
                VALUES
                    (1034, 101, 'native-old', 'daily_t1', '5Y', 5,
                     '2026-05-20', '2026-05-20', '2026-05-28', 1, 1, NULL)
                """
            )
        )

    with pytest.raises(DashboardDataError, match="duplicate.*backtest"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)


def test_canonical_and_dto_work_happens_after_snapshot_connection_closes(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, trace = dashboard_db
    called: list[str] = []

    def _after_close(name: str, original: Any) -> Any:
        def checked(*args: Any, **kwargs: Any) -> Any:
            assert len(trace.checkins) == 1
            called.append(name)
            return original(*args, **kwargs)

        return checked

    monkeypatch.setattr(
        dashboard,
        "collapse_actual_facts_with_diagnostics",
        _after_close(
            "collapse",
            dashboard.collapse_actual_facts_with_diagnostics,
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "choose_live_prediction_rows",
        _after_close("canonical", dashboard.choose_live_prediction_rows),
    )
    monkeypatch.setattr(
        dashboard,
        "compact_detail_row",
        _after_close("compact", dashboard.compact_detail_row),
    )

    dashboard.build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert {"collapse", "canonical", "compact"}.issubset(called)


def test_build_diagnostics_report_actual_folding_by_frequency_without_extra_sql(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import (
        build_factor_lab_dashboard,
        dashboard_build_diagnostics,
    )

    engine, trace = dashboard_db

    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    diagnostics = dashboard_build_diagnostics(payload["snapshot_id"])

    assert diagnostics is not None
    assert diagnostics["actual_same_direction_duplicates_folded"] == {
        "daily": 0,
        "weekly": 0,
        "monthly": 1,
    }
    assert diagnostics["actual_direction_conflicts"] == {
        "daily": 0,
        "weekly": 0,
        "monthly": 0,
    }
    assert len(trace.statements) == 11


def test_build_diagnostics_nested_counts_are_defensive_copies(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import (
        build_factor_lab_dashboard,
        dashboard_build_diagnostics,
    )

    engine, _trace = dashboard_db
    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    first = dashboard_build_diagnostics(payload["snapshot_id"])
    assert first is not None
    first["actual_same_direction_duplicates_folded"]["monthly"] = 999

    second = dashboard_build_diagnostics(payload["snapshot_id"])
    assert second is not None
    assert second["actual_same_direction_duplicates_folded"]["monthly"] == 1


def test_payload_validator_rejects_identity_top_level_and_uniqueness_damage(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import (
        DashboardDataError,
        validate_dashboard_payload,
    )

    engine, _trace = dashboard_db
    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)
    required_top = {
        "schema_version",
        "snapshot_id",
        "generated_at",
        "display_until",
        "stale",
        "snapshot_age_ms",
        "row_fields",
        "target_labels",
        "schemes",
    }
    assert required_top <= set(payload)
    assert validate_dashboard_payload(payload) is None

    invalid_payloads: list[tuple[str, dict[str, Any]]] = []
    missing_complete_top_contract = deepcopy(payload)
    for field in (
        "snapshot_id",
        "generated_at",
        "display_until",
        "stale",
        "snapshot_age_ms",
        "target_labels",
    ):
        missing_complete_top_contract.pop(field)
    invalid_payloads.append(("snapshot_id", missing_complete_top_contract))

    invalid_snapshot_id = deepcopy(payload)
    invalid_snapshot_id["snapshot_id"] = ""
    invalid_payloads.append(("snapshot_id", invalid_snapshot_id))

    invalid_generated_at = deepcopy(payload)
    invalid_generated_at["generated_at"] = "2026-07-22T12:34:56"
    invalid_payloads.append(("generated_at", invalid_generated_at))

    invalid_display_until = deepcopy(payload)
    invalid_display_until["display_until"] = "2026-7-22"
    invalid_payloads.append(("display_until", invalid_display_until))

    invalid_stale = deepcopy(payload)
    invalid_stale["stale"] = 0
    invalid_payloads.append(("stale", invalid_stale))

    invalid_age = deepcopy(payload)
    invalid_age["snapshot_age_ms"] = -1
    invalid_payloads.append(("snapshot_age_ms", invalid_age))

    invalid_composite = deepcopy(payload)
    invalid_composite["schemes"][0]["scheme_id"] = "wrong"
    invalid_payloads.append(("composite", invalid_composite))

    invalid_deployed_at = deepcopy(payload)
    invalid_deployed_at["schemes"][0]["deployed_at"] = ""
    invalid_payloads.append(("deployed_at", invalid_deployed_at))

    duplicate_scheme = deepcopy(payload)
    duplicate_scheme["schemes"].append(deepcopy(duplicate_scheme["schemes"][0]))
    invalid_payloads.append(("duplicate scheme_id", duplicate_scheme))

    invalid_target_label = deepcopy(payload)
    invalid_target_label["schemes"][0]["target_label"] = "wrong label"
    invalid_payloads.append(("target_label", invalid_target_label))

    duplicate_live = deepcopy(payload)
    live_scheme = next(
        scheme for scheme in duplicate_live["schemes"] if scheme["live_rows"]
    )
    live_scheme["live_rows"].append(deepcopy(live_scheme["live_rows"][0]))
    invalid_payloads.append(("duplicate.*live", duplicate_live))

    for message, candidate in invalid_payloads:
        with pytest.raises(DashboardDataError, match=message):
            validate_dashboard_payload(candidate)


def test_canonical_snapshot_encoder_is_single_budget_source_after_connection_closes(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, trace = dashboard_db
    real_encode = dashboard.encode_canonical_snapshot
    encodings: list[dashboard.SnapshotEncoding] = []

    def tracking_encode(payload: dict[str, Any]) -> dashboard.SnapshotEncoding:
        assert len(trace.checkins) == 1
        encoding = real_encode(payload)
        encodings.append(encoding)
        return encoding

    monkeypatch.setattr(dashboard, "encode_canonical_snapshot", tracking_encode)

    dashboard.build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(encodings) == 1
    assert encodings[0].raw_size == len(encodings[0].raw_body)
    assert encodings[0].gzip_size == len(encodings[0].gzip_body)


def test_canonical_snapshot_encoder_uses_route_json_and_gzip_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gzip as stdlib_gzip

    import backend.factor_lab_dashboard as dashboard

    real_dumps = dashboard.json.dumps
    real_compress = stdlib_gzip.compress
    calls: list[tuple[str, Any]] = []

    def tracking_dumps(*args: Any, **kwargs: Any) -> str:
        calls.append(("json", kwargs))
        return real_dumps(*args, **kwargs)

    class TrackingGzip:
        @staticmethod
        def compress(data: bytes, *, compresslevel: int) -> bytes:
            calls.append(("gzip", compresslevel))
            return real_compress(data, compresslevel=compresslevel)

    monkeypatch.setattr(dashboard.json, "dumps", tracking_dumps)
    monkeypatch.setattr(dashboard, "gzip", TrackingGzip, raising=False)

    encoding = dashboard.encode_canonical_snapshot({"label": "国债"})

    assert encoding.raw_body == '{"label":"国债"}'.encode()
    assert stdlib_gzip.decompress(encoding.gzip_body) == encoding.raw_body
    assert encoding.raw_size == len(encoding.raw_body)
    assert encoding.gzip_size == len(encoding.gzip_body)
    assert calls == [
        ("json", {"ensure_ascii": False, "separators": (",", ":")}),
        ("gzip", 6),
    ]


@pytest.mark.parametrize(
    ("constant", "dataset", "select_count"),
    [
        ("MAX_REGISTRY_SOURCE_ROWS", "active_registry", 1),
        ("MAX_TARGET_SOURCE_ROWS", "active_targets", 2),
        ("MAX_LIVE_PREDICTION_SOURCE_ROWS", "live_predictions", 3),
        ("MAX_ACTUAL_SOURCE_ROWS", "live_actuals", 4),
        ("MAX_BACKTEST_RUN_SOURCE_ROWS", "backtest_run_candidates", 5),
        ("MAX_BACKTEST_DETAIL_SOURCE_ROWS", "selected_backtest_details", 6),
    ],
)
def test_dashboard_source_row_caps_fail_closed_before_dto(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    dataset: str,
    select_count: int,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, trace = dashboard_db
    monkeypatch.setattr(dashboard, constant, 0, raising=False)

    with pytest.raises(
        dashboard.DashboardDataError,
        match=rf"source row limit.*{dataset}",
    ):
        dashboard.build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.statements) == select_count
    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1


def test_dashboard_primary_source_queries_use_cap_plus_one_limits(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, trace = dashboard_db

    dashboard.build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    expected_limits = {
        "FROM t_scheme_registry\n        WHERE status": (
            dashboard.MAX_REGISTRY_SOURCE_ROWS + 1
        ),
        "FROM t_target_registry": dashboard.MAX_TARGET_SOURCE_ROWS + 1,
        "FROM t_scheme_predictions\n        WHERE (": (
            dashboard.MAX_LIVE_PREDICTION_SOURCE_ROWS + 1
        ),
        "FROM t_scheme_actuals": dashboard.MAX_ACTUAL_SOURCE_ROWS + 1,
        "FROM t_backtest_runs": dashboard.MAX_BACKTEST_RUN_SOURCE_ROWS + 1,
        "FROM t_backtest_predictions": (
            dashboard.MAX_BACKTEST_DETAIL_SOURCE_ROWS + 1
        ),
    }
    assert len(trace.statements) == 11
    for marker, expected_limit in expected_limits.items():
        matches = [
            (statement, parameters)
            for statement, parameters in zip(
                trace.statements,
                trace.parameters,
                strict=True,
            )
            if marker in statement
        ]
        assert len(matches) == 1
        statement, parameters = matches[0]
        assert "LIMIT" in statement.upper()
        assert parameters[-1] == expected_limit


def test_empty_registry_avoids_empty_in_clause(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(text("UPDATE t_scheme_registry SET status = 'paused'"))
    trace.reset()

    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert payload["schemes"] == []
    assert len(trace.statements) <= 6
    assert not any("IN ()" in statement.upper() for statement in trace.statements)


@pytest.mark.parametrize(
    ("constant", "limit", "message"),
    [
        ("MAX_DETAIL_ROWS", 0, "detail rows"),
        ("MAX_RAW_JSON_BYTES", 1, "raw JSON"),
        ("MAX_GZIP_JSON_BYTES", 1, "gzip JSON"),
    ],
)
def test_dashboard_response_budgets_fail_closed_without_truncation(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    message: str,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, _trace = dashboard_db
    monkeypatch.setattr(dashboard, constant, limit, raising=False)

    with pytest.raises(dashboard.DashboardDataError, match=message):
        dashboard.build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)


def test_conflicting_actual_fails_whole_snapshot_with_one_checkout(
    dashboard_db: tuple[Engine, SqlTrace],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_monthly_actuals
                    (tenor, target_date, direction_monthly, target_rule)
                VALUES
                    ('10Y', '2026-07-15', -1,
                     'next_month_observation_yield_vs_feature_month_observation_yield')
                """
            )
        )
    trace.reset()

    with caplog.at_level(
        logging.ERROR,
        logger="backend.factor_lab_dashboard",
    ):
        with pytest.raises(
            DashboardDataError,
            match="monthly actuals.*conflicting",
        ) as captured:
            build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert captured.value.diagnostics == {
        "actual_same_direction_duplicates_folded": {
            "daily": 0,
            "weekly": 0,
            "monthly": 1,
        },
        "actual_direction_conflicts": {
            "daily": 0,
            "weekly": 0,
            "monthly": 1,
        },
        "actual_conflict_locator": {
            "frequency": "monthly",
            "target_tenor": "10Y",
            "target_date": "2026-07-15",
            "target_rule": (
                "next_month_observation_yield_vs_"
                "feature_month_observation_yield"
            ),
        },
    }
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith(
            "factor_lab_dashboard_actual_conflict "
        )
    ]
    assert len(records) == 1
    event = json.loads(records[0].getMessage().split(" ", 1)[1])
    assert records[0].dashboard_event == event
    assert event == captured.value.diagnostics
    assert "conflicting directions" not in records[0].getMessage()
    assert " != " not in records[0].getMessage()
    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1
    assert len(set(trace.connection_ids)) == 1


def test_actual_conflict_event_hashes_unsafe_locator_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    unsafe_tenor = "secret\n" + ("X" * 200)
    unsafe_rule = "sql=SELECT * FROM private"
    rows = [
        {
            "actual_kind": "monthly",
            "target_tenor": unsafe_tenor,
            "target_date": "2026-07-15",
            "target_rule": unsafe_rule,
            "actual_direction": direction,
        }
        for direction in (1, -1)
    ]

    with caplog.at_level(
        logging.ERROR,
        logger="backend.factor_lab_dashboard",
    ):
        with pytest.raises(dashboard.DashboardDataError):
            dashboard._collapse_actual_rows(
                rows,
                active_actual_scopes={
                    (unsafe_tenor, "monthly", unsafe_rule)
                },
            )

    record = next(
        item
        for item in caplog.records
        if item.getMessage().startswith(
            "factor_lab_dashboard_actual_conflict "
        )
    )
    event = record.dashboard_event
    assert set(event) == {
        "actual_same_direction_duplicates_folded",
        "actual_direction_conflicts",
        "actual_conflict_locator",
    }
    assert re.fullmatch(
        r"redacted-sha256:[0-9a-f]{16}",
        event["actual_conflict_locator"]["target_tenor"],
    )
    assert re.fullmatch(
        r"redacted-sha256:[0-9a-f]{16}",
        event["actual_conflict_locator"]["target_rule"],
    )
    assert unsafe_tenor not in record.getMessage()
    assert unsafe_rule not in record.getMessage()


def test_unconsumed_actual_scope_conflict_does_not_fail_snapshot(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE t_scheme_registry
                SET status = 'paused'
                WHERE scheme_id = 'daily_t5__h1__5Y'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_monthly_actuals
                    (tenor, target_date, direction_monthly, target_rule)
                VALUES
                    ('5Y', '2026-07-15', 1,
                     'next_month_observation_yield_vs_feature_month_observation_yield'),
                    ('5Y', '2026-07-15', -1,
                     'next_month_observation_yield_vs_feature_month_observation_yield'),
                    ('5Y', '2026-07-16', 1, ''),
                    ('5Y', '2026-07-17', 1, NULL),
                    ('5Y', '2026-07-18', 7,
                     'next_month_observation_yield_vs_feature_month_observation_yield')
                """
            )
        )
    trace.reset()

    payload = build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    schemes = _schemes_by_id(payload)
    assert "daily_t5__h1__5Y" not in schemes
    assert schemes["daily_t1__h5__5Y"]["live_rows"] == [
        ["2026-07-20", "2026-07-17", "2026-07-21", "scheduled_live", 1, 1],
        ["2026-07-21", "2026-07-20", "2026-08-01", "scheduled_live", -1, None],
    ]
    assert schemes["monthly__h1__10Y"]["live_rows"] == [
        ["2026-06-15", "2026-06-15", "2026-07-15", "gray_live", 1, 1]
    ]
    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1


def test_invalid_direction_in_active_actual_scope_fails_snapshot(
    dashboard_db: tuple[Engine, SqlTrace],
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_monthly_actuals
                    (tenor, target_date, direction_monthly, target_rule)
                VALUES
                    ('10Y', '2026-07-16', 7,
                     'next_month_observation_yield_vs_feature_month_observation_yield')
                """
            )
        )
    trace.reset()

    with pytest.raises(DashboardDataError, match="direction is invalid: 7"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("task_type = NULL", "task_type"),
        ("task_type = 'weekly'", "task_type"),
        ("deployed_at = NULL", "deployed_at"),
        ("runtime_type = NULL", "runtime_type"),
        ("runtime_type = 'native_v2'", "runtime_type"),
    ],
)
def test_incomplete_active_registry_fails_closed(
    dashboard_db: tuple[Engine, SqlTrace],
    assignment: str,
    message: str,
) -> None:
    from backend.factor_lab_dashboard import build_factor_lab_dashboard
    from backend.factor_lab_dashboard_semantics import DashboardDataError

    engine, trace = dashboard_db
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                UPDATE t_scheme_registry
                SET {assignment}
                WHERE scheme_id = 'daily_t1__h5__5Y'
                """
            )
        )
    trace.reset()

    with pytest.raises(DashboardDataError, match=message):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1


def test_default_capture_reads_shanghai_clock_once(
    dashboard_db: tuple[Engine, SqlTrace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.factor_lab_dashboard as dashboard

    engine, _trace = dashboard_db

    class FixedDateTime:
        calls = 0

        @classmethod
        def now(cls, timezone: ZoneInfo) -> datetime:
            cls.calls += 1
            assert timezone.key == "Asia/Shanghai"
            return CAPTURED_AT

    monkeypatch.setattr(dashboard, "datetime", FixedDateTime)

    payload = dashboard.build_factor_lab_dashboard(engine)

    assert FixedDateTime.calls == 1
    assert payload["display_until"] == "2026-07-22"


class _MySqlDialect:
    name = "mysql"


class _MySqlConnectionStub:
    dialect = _MySqlDialect()

    def __init__(self, events: list[Any], *, fail_on: str | None = None) -> None:
        self.events = events
        self.fail_on = fail_on

    def execution_options(self, **options: Any) -> _MySqlConnectionStub:
        self.events.append(("execution_options", options))
        if self.fail_on == "execution_options":
            raise RuntimeError("isolation setup failed")
        return self

    def exec_driver_sql(self, statement: str) -> None:
        self.events.append(("driver_sql", statement))
        if self.fail_on == "start_transaction":
            raise RuntimeError("snapshot start failed")

    def execute(self, statement: Any) -> None:
        self.events.append(("business_sql", str(statement)))

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


class _MySqlEngineStub:
    dialect = _MySqlDialect()

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.events: list[Any] = []
        self.connection = _MySqlConnectionStub(self.events, fail_on=fail_on)

    def connect(self) -> _MySqlConnectionStub:
        self.events.append("connect")
        return self.connection


def test_mysql_snapshot_sets_isolation_and_starts_consistent_read_first() -> None:
    from backend.factor_lab_dashboard import dashboard_read_connection

    engine = _MySqlEngineStub()

    with dashboard_read_connection(engine) as connection:
        connection.execute(text("SELECT registry business rows"))

    assert engine.events == [
        "connect",
        ("execution_options", {"isolation_level": "REPEATABLE READ"}),
        (
            "driver_sql",
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
        ),
        ("business_sql", "SELECT registry business rows"),
        "rollback",
        "close",
    ]


def test_mysql_snapshot_rolls_back_and_closes_on_error() -> None:
    from backend.factor_lab_dashboard import dashboard_read_connection

    engine = _MySqlEngineStub()

    with pytest.raises(RuntimeError, match="query failed"):
        with dashboard_read_connection(engine):
            raise RuntimeError("query failed")

    assert engine.events[-2:] == ["rollback", "close"]
    assert engine.events.count("connect") == 1


@pytest.mark.parametrize(
    ("fail_on", "message"),
    [
        ("execution_options", "isolation setup failed"),
        ("start_transaction", "snapshot start failed"),
    ],
)
def test_mysql_snapshot_cleans_up_when_transaction_setup_fails(
    fail_on: str,
    message: str,
) -> None:
    from backend.factor_lab_dashboard import dashboard_read_connection

    engine = _MySqlEngineStub(fail_on=fail_on)

    with pytest.raises(RuntimeError, match=message):
        with dashboard_read_connection(engine):
            pytest.fail("snapshot context must not yield after setup failure")

    assert engine.events[-2:] == ["rollback", "close"]
    assert engine.events.count("connect") == 1
