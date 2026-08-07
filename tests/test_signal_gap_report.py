"""只读 signal-gap 报告的行为回归。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, text

from shared.signal_gap_report import (
    SignalGapReport,
    latest_due_signal_statuses,
    load_signal_gap_report,
    read_latest_due_signal_statuses,
    read_signal_gap_report,
    serialize_signal_gap_report,
)


class SignalGapReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        self._schema()
        self._calendar("2026-07-31", "2026-09-18")

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_onboarding_origin_exact_blackbox_version_and_live_phases(self) -> None:
        self._registry("blackbox__h5__10Y", "blackbox", "blackbox_v2", "daily", "T+5", "10Y", 5, "2026-01-01")
        self._registry("native__h1__5Y", "native", "native_adapter", "daily", "T+1", "5Y", 1, "2026-08-04")
        self._version("blackbox", "old", "retired", "2026-01-01")
        self._version("blackbox", "current", "active", "2026-08-03")
        self._prediction("blackbox", "old", "10Y", 5, "2026-08-04", "2026-08-03", "2026-08-10", "gray_live")
        self._prediction("blackbox", "current", "10Y", 5, "2026-08-04", "2026-08-03", "2026-08-10", "historical_backtest")
        self._prediction("blackbox", "current", "10Y", 5, "2026-08-05", "2026-08-04", "2026-08-11", "scheduled_live")
        self._prediction("native", "current", "5Y", 1, "2026-08-05", "2026-08-04", "2026-08-05", "gray_live")

        report = self._report("2026-08-03", "2026-08-05")
        blackbox = [item for item in report.expected if item.base_scheme_id == "blackbox"]

        self.assertEqual([item.predict_date for item in blackbox], ["2026-08-04", "2026-08-05"])
        self.assertEqual([item.predict_date for item in report.present], ["2026-08-05", "2026-08-05"])
        self.assertEqual(
            [(item.base_scheme_id, item.predict_date) for item in report.missing],
            [("blackbox", "2026-08-04")],
        )
        self.assertEqual(
            [item.predict_date for item in report.expected if item.base_scheme_id == "native"],
            ["2026-08-05"],
        )

    def test_daily_weekly_and_monthly_cadence_use_shared_contexts(self) -> None:
        self._registry("daily__h1__5Y", "daily", "native_adapter", "daily", "T+1", "5Y", 1, "2026-08-01")
        self._registry("weekly__h6__10Y", "weekly", "native_adapter", "weekly", "weekly_point", "10Y", 6, "2026-08-01")
        self._registry("monthly__h30__7Y", "monthly", "native_adapter", "monthly", "monthly", "7Y", 30, "2026-08-01")

        cases = self._report("2026-08-01", "2026-08-16").expected
        weekly = next(item for item in cases if item.base_scheme_id == "weekly" and item.predict_date == "2026-08-08")
        monthly = next(item for item in cases if item.base_scheme_id == "monthly")

        self.assertEqual((weekly.feature_date, weekly.target_date), ("2026-08-07", "2026-08-14"))
        self.assertEqual((monthly.predict_date, monthly.feature_date, monthly.target_date), ("2026-08-15", "2026-08-14", "2026-09-15"))

    def test_safe_failure_categories_and_historic_gap_survive_later_present_signal(self) -> None:
        self._registry("native__h1__5Y", "native", "native_adapter", "daily", "T+1", "5Y", 1, "2026-08-03")
        self._run("native", "2026-08-04", "failed", "secret endpoint detail")
        self._run("native", "2026-08-05", "success")
        self._prediction("native", "current", "5Y", 1, "2026-08-06", "2026-08-05", "2026-08-06", "scheduled_live")

        report = self._report("2026-08-03", "2026-08-06")
        status = latest_due_signal_statuses(report)[0]

        self.assertEqual(
            {item.predict_date: item.failure_category for item in report.missing},
            {"2026-08-04": "run_failed", "2026-08-05": "prediction_missing"},
        )
        self.assertEqual((status.state, status.open_missing_count, status.latest_missing_predict_date), ("missing", 2, "2026-08-05"))
        with self.engine.connect() as connection:
            connection_status = read_latest_due_signal_statuses(connection, start_date="2026-08-03", as_of_date="2026-08-06")[0]
        self.assertEqual(connection_status.open_missing_count, 2)
        self.assertNotIn("secret endpoint detail", json.dumps(serialize_signal_gap_report(report)))

    def test_missing_blackbox_activation_is_generic_unavailable(self) -> None:
        self._registry("blackbox__h5__10Y", "blackbox", "blackbox_v2", "daily", "T+5", "10Y", 5, "2026-01-01")

        status = latest_due_signal_statuses(self._report("2026-08-03", "2026-08-05"))[0]

        self.assertEqual((status.state, status.failure_category), ("unavailable", "activation_unavailable"))

    def test_sorted_core_is_read_only_and_never_reads_deployed_at(self) -> None:
        self._registry("zeta__h1__5Y", "zeta", "native_adapter", "daily", "T+1", "5Y", 1, "2026-08-03")
        self._registry("alpha__h1__5Y", "alpha", "native_adapter", "daily", "T+1", "5Y", 1, "2026-08-03")
        statements: list[str] = []

        def capture(*args: object) -> None:
            statements.append(str(args[2]))

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            report = self._report("2026-08-03", "2026-08-04")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual([item.registry_scheme_id for item in report.expected], ["alpha__h1__5Y", "zeta__h1__5Y"])
        self.assertTrue(all("DEPLOYED_AT" not in item.upper() for item in statements))
        self.assertTrue(all(not item.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP")) for item in statements))

    def test_snapshot_wrapper_and_cli_default_range(self) -> None:
        report = SignalGapReport("2026-08-03", "2026-08-04", (), (), (), ())
        connection = _FakeConnection()
        with patch("shared.signal_gap_report.read_signal_gap_report", return_value=report) as reader:
            self.assertIs(load_signal_gap_report(_FakeEngine(connection), start_date="2026-08-03", end_date="2026-08-04"), report)
        self.assertEqual(connection.commands, ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"])
        self.assertEqual(connection.rollbacks, 1)
        reader.assert_called_once_with(connection, start_date="2026-08-03", end_date="2026-08-04")

        from scripts import report_signal_gaps

        output = io.StringIO()
        engine = SimpleNamespace(dispose=lambda: None)
        with (
            patch.object(report_signal_gaps, "create_engine_from_env", return_value=engine),
            patch.object(report_signal_gaps, "load_signal_gap_report", return_value=report) as load_report,
            patch("sys.argv", ["report_signal_gaps.py", "--as-of", "2026-08-04"]),
            redirect_stdout(output),
        ):
            self.assertEqual(report_signal_gaps.main(), 0)
        load_report.assert_called_once_with(engine, start_date="2025-01-01", end_date="2026-08-04")
        self.assertEqual(json.loads(output.getvalue())["end_date"], "2026-08-04")

    def _report(self, start: str, end: str) -> SignalGapReport:
        with self.engine.connect() as connection:
            return read_signal_gap_report(connection, start_date=start, end_date=end)

    def _schema(self) -> None:
        statements = (
            "CREATE TABLE t_scheme_registry (scheme_id TEXT, base_scheme_id TEXT, runtime_type TEXT, frequency TEXT, task_type TEXT, target_tenor TEXT, horizon INTEGER, status TEXT, created_at TEXT, deployed_at TEXT)",
            "CREATE TABLE t_scheme_versions (scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, status TEXT, approved_at TEXT)",
            "CREATE TABLE t_scheme_predictions (scheme_id TEXT, scheme_version TEXT, target_tenor TEXT, horizon INTEGER, predict_date TEXT, feature_date TEXT, target_date TEXT, prediction_phase TEXT)",
            "CREATE TABLE t_scheme_runs (scheme_id TEXT, scheme_version TEXT, predict_date TEXT, prediction_phase TEXT, status TEXT, error_message TEXT)",
            "CREATE TABLE t_trade_calendar (rdate TEXT, trade_flag TEXT)",
            "CREATE TABLE api_wind_date (rdate TEXT, week_id INTEGER)",
        )
        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    def _calendar(self, start: str, end: str) -> None:
        current, finish = date.fromisoformat(start), date.fromisoformat(end)
        rows = []
        while current <= finish:
            rows.append({"rdate": current.isoformat(), "trade_flag": "1" if current.weekday() < 5 else "0", "week_id": current.isocalendar().week})
            current += timedelta(days=1)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"), rows)
            connection.execute(text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"), rows)

    def _registry(self, registry: str, base: str, runtime: str, frequency: str, task: str, tenor: str, horizon: int, created: str) -> None:
        self._insert("t_scheme_registry", scheme_id=registry, base_scheme_id=base, runtime_type=runtime, frequency=frequency, task_type=task, target_tenor=tenor, horizon=horizon, status="active", created_at=f"{created} 00:00:00", deployed_at="2026-01-01")

    def _version(self, base: str, version: str, status: str, approved: str) -> None:
        self._insert("t_scheme_versions", scheme_id=base, scheme_version=version, runtime_type="blackbox_v2", status=status, approved_at=f"{approved} 09:00:00")

    def _prediction(self, base: str, version: str, tenor: str, horizon: int, predict: str, feature: str, target: str, phase: str) -> None:
        self._insert("t_scheme_predictions", scheme_id=base, scheme_version=version, target_tenor=tenor, horizon=horizon, predict_date=predict, feature_date=feature, target_date=target, prediction_phase=phase)

    def _run(self, base: str, predict: str, status: str, error: str | None = None) -> None:
        self._insert("t_scheme_runs", scheme_id=base, scheme_version="current", predict_date=predict, prediction_phase="gray_live", status=status, error_message=error)

    def _insert(self, table: str, **row: object) -> None:
        columns = ", ".join(row)
        values = ", ".join(f":{key}" for key in row)
        with self.engine.begin() as connection:
            connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({values})"), row)


@dataclass
class _FakeConnection:
    commands: list[str] | None = None
    rollbacks: int = 0

    def __post_init__(self) -> None:
        self.commands = []

    def exec_driver_sql(self, command: str) -> None:
        self.commands.append(command)

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> "_FakeEngine":
        return self

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
