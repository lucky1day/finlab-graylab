from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
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
                    name TEXT NOT NULL,
                    description TEXT,
                    horizon INTEGER NOT NULL,
                    task_type TEXT,
                    frequency TEXT,
                    target_tenor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deployed_at TEXT
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
                    extra TEXT
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
        connection.execute(
            text(
                """
                INSERT INTO t_target_registry
                    (target_code, display_name, asset_class, target_type,
                     sort_order, status, extra)
                VALUES
                    ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 1, 'active', '{}'),
                    ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 2, 'active', '{}')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, description, horizon,
                     task_type, frequency, target_tenor, status, deployed_at)
                VALUES
                    (:scheme_id, :base_scheme_id, :name, :description, :horizon,
                     :task_type, :frequency, :target_tenor, :status, :deployed_at)
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

    assert payload["generated_at"] == "2026-07-22T12:34:56+08:00"
    assert payload["display_until"] == "2026-07-22"
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
    assert all(item["backtest"] is None for item in schemes.values())

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
    assert len(trace.statements) == 4
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
        "collapse_actual_facts",
        _after_close("collapse", dashboard.collapse_actual_facts),
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


def test_conflicting_actual_fails_whole_snapshot_with_one_checkout(
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
                    ('10Y', '2026-07-15', -1,
                     'next_month_observation_yield_vs_feature_month_observation_yield')
                """
            )
        )
    trace.reset()

    with pytest.raises(DashboardDataError, match="monthly actuals.*conflicting"):
        build_factor_lab_dashboard(engine, captured_at=CAPTURED_AT)

    assert len(trace.checkouts) == 1
    assert len(trace.checkins) == 1
    assert len(set(trace.connection_ids)) == 1


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
