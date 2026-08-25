from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text


class TenorMappingTests(unittest.TestCase):
    def test_shared_mapping_is_single_source_for_actuals_updaters(self) -> None:
        from shared.tenor_mapping import TENOR_TO_INDICATOR
        from scheduler import daily_actuals_updater, weekly_actuals_updater

        self.assertEqual(TENOR_TO_INDICATOR["10Y"], "TB0YWI0C")
        self.assertIs(daily_actuals_updater.TENOR_TO_INDICATOR, TENOR_TO_INDICATOR)
        self.assertIs(weekly_actuals_updater.TENOR_TO_INDICATOR, TENOR_TO_INDICATOR)

    def test_configured_active_scheme_tenors_are_diagnostic_only(self) -> None:
        from scheduler.daily_actuals_updater import configured_active_scheme_tenors

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme(root, "daily_live", "daily", "active", ["5Y", "10Y"])
            _write_scheme(root, "daily_paused", "daily", "paused", ["1Y"])
            _write_scheme(root, "weekly_live", "weekly", "active", ["10Y"])

            self.assertEqual(
                configured_active_scheme_tenors(root, frequency="daily"),
                ["10Y", "5Y"],
            )

    def test_active_registry_tenors_selects_only_active_requested_task_types(self) -> None:
        from scheduler.daily_actuals_updater import active_registry_tenors

        engine = _registry_engine(
            [
                ("monthly-1y", "active", "monthly", "1Y"),
                ("monthly-3y", "active", "monthly", "3Y"),
                ("monthly-5y", "active", "monthly", "5Y"),
                ("monthly-7y", "active", "monthly", "7Y"),
                ("monthly-10y", "active", "monthly", "10Y"),
                ("paused-monthly", "paused", "monthly", "30Y"),
                ("archived-monthly", "archived", "monthly", "2Y"),
                ("active-daily", "active", "T+1", "30Y"),
            ]
        )
        try:
            self.assertEqual(
                active_registry_tenors(engine, ("monthly",)),
                ["1Y", "3Y", "5Y", "7Y", "10Y"],
            )
        finally:
            engine.dispose()

    def test_registry_scope_wins_and_emits_structured_drift_warning(self) -> None:
        from unittest.mock import patch

        from scheduler.daily_actuals_updater import resolve_actual_tenors

        engine = _registry_engine(
            [
                ("monthly-1y", "active", "monthly", "1Y"),
                ("monthly-3y", "active", "monthly", "3Y"),
                ("monthly-5y", "active", "monthly", "5Y"),
                ("monthly-7y", "active", "monthly", "7Y"),
                ("monthly-10y", "active", "monthly", "10Y"),
            ]
        )
        try:
            with (
                patch(
                    "scheduler.daily_actuals_updater.configured_active_scheme_tenors",
                    return_value=["1Y", "5Y", "10Y"],
                ),
                self.assertLogs(
                    "scheduler.daily_actuals_updater",
                    level="WARNING",
                ) as captured,
            ):
                selected = resolve_actual_tenors(
                    engine,
                    frequency="monthly",
                )
        finally:
            engine.dispose()

        self.assertEqual(selected, ["1Y", "3Y", "5Y", "7Y", "10Y"])
        warning = "\n".join(captured.output)
        self.assertIn("ACTUAL_TENOR_SCOPE_DRIFT", warning)
        self.assertIn('"missing_from_config": ["3Y", "7Y"]', warning)
        self.assertIn('"registry_tenors": ["1Y", "3Y", "5Y", "7Y", "10Y"]', warning)

    def test_registry_query_failure_does_not_fall_back_to_config(self) -> None:
        from unittest.mock import patch

        from sqlalchemy.exc import OperationalError

        from scheduler.daily_actuals_updater import resolve_actual_tenors

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        try:
            with patch(
                "scheduler.daily_actuals_updater.configured_active_scheme_tenors",
                return_value=["1Y", "5Y", "10Y"],
            ):
                with self.assertRaises(OperationalError):
                    resolve_actual_tenors(engine, frequency="monthly")
        finally:
            engine.dispose()

    def test_local_config_diagnostic_failure_does_not_override_registry(self) -> None:
        from unittest.mock import patch

        from scheduler.daily_actuals_updater import resolve_actual_tenors

        engine = _registry_engine(
            [
                ("monthly-3y", "active", "monthly", "3Y"),
                ("monthly-7y", "active", "monthly", "7Y"),
            ]
        )
        try:
            with (
                patch(
                    "scheduler.daily_actuals_updater.configured_active_scheme_tenors",
                    side_effect=RuntimeError("broken local config"),
                ),
                self.assertLogs(
                    "scheduler.daily_actuals_updater",
                    level="WARNING",
                ) as captured,
            ):
                selected = resolve_actual_tenors(engine, frequency="monthly")
        finally:
            engine.dispose()

        self.assertEqual(selected, ["3Y", "7Y"])
        self.assertIn(
            "ACTUAL_TENOR_SCOPE_DIAGNOSTIC_FAILED",
            "\n".join(captured.output),
        )
        self.assertNotIn("broken local config", "\n".join(captured.output))

    def test_explicit_tenor_override_is_normalized_without_registry_access(self) -> None:
        from scheduler.daily_actuals_updater import resolve_actual_tenors

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        try:
            self.assertEqual(
                resolve_actual_tenors(
                    engine,
                    frequency="monthly",
                    tenors=["10y", "3Y", "3y"],
                ),
                ["3Y", "10Y"],
            )
        finally:
            engine.dispose()

    def test_frequency_task_type_mapping_is_explicit(self) -> None:
        from scheduler.daily_actuals_updater import ACTUAL_TASK_TYPES_BY_FREQUENCY

        self.assertEqual(ACTUAL_TASK_TYPES_BY_FREQUENCY["daily"], ("T+1", "T+5"))
        self.assertEqual(
            ACTUAL_TASK_TYPES_BY_FREQUENCY["weekly"],
            ("weekly_point", "weekly_average"),
        )
        self.assertEqual(ACTUAL_TASK_TYPES_BY_FREQUENCY["monthly"], ("monthly",))
        self.assertEqual(
            ACTUAL_TASK_TYPES_BY_FREQUENCY["period_average"],
            ("monthly_average", "quarterly_average", "annual_average"),
        )


def _registry_engine(rows: list[tuple[str, str, str, str]]):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    target_tenor TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, status, task_type, target_tenor)
                VALUES
                    (:scheme_id, :status, :task_type, :target_tenor)
                """
            ),
            [
                {
                    "scheme_id": scheme_id,
                    "status": status,
                    "task_type": task_type,
                    "target_tenor": target_tenor,
                }
                for scheme_id, status, task_type, target_tenor in rows
            ],
        )
    return engine


def _write_scheme(root: Path, scheme_id: str, frequency: str, status: str, tenors: list[str]) -> None:
    scheme_dir = root / scheme_id
    scheme_dir.mkdir(parents=True)
    scheme_dir.joinpath("config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                "name: Demo",
                "description: Demo",
                "horizon: 1",
                f"task_type: {'weekly_point' if frequency == 'weekly' else 'T+1'}",
                f"tenors: {tenors!r}",
                f"frequency: {frequency}",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "input_spec:",
                f"  data_version: shared_data_service_{frequency}.v1",
                (
                    '  required_columns: ["week_id", "TB0YWI3C"]'
                    if frequency == "weekly"
                    else '  required_columns: ["date", "TB0YWI0C"]'
                ),
                *(["  weekly_variant: yield_curve"] if frequency == "weekly" else []),
                *(["target_rule: next_week_last_trading_day_vs_current_week_last_trading_day"] if frequency == "weekly" else []),
                f"status: {status}",
            ]
        ),
        encoding="utf-8",
    )

