from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


class TenorMappingTests(unittest.TestCase):
    def test_tenor_mapping_normalizes_product_tenor(self) -> None:
        from shared.tenor_mapping import indicator_map_for_tenors

        self.assertEqual(
            indicator_map_for_tenors(["10y"]),
            {"TB0YWI0C": "10Y"},
        )

    def test_registry_scope_is_the_only_runtime_authority(self) -> None:
        from scheduler.daily_actuals_updater import resolve_actual_tenors

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
            selected = resolve_actual_tenors(engine, frequency="monthly")
        finally:
            engine.dispose()

        self.assertEqual(selected, ["1Y", "3Y", "5Y", "7Y", "10Y"])

    def test_registry_query_failure_does_not_fall_back_to_config(self) -> None:
        from sqlalchemy.exc import OperationalError

        from scheduler.daily_actuals_updater import resolve_actual_tenors

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        try:
            with self.assertRaises(OperationalError):
                resolve_actual_tenors(engine, frequency="monthly")
        finally:
            engine.dispose()

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
