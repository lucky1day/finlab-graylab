from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text


class MonthlyActualsTests(unittest.TestCase):
    def test_update_monthly_actuals_uses_all_active_registry_tenors(self) -> None:
        from scheduler.monthly_actuals_updater import update_monthly_actuals

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
                        (:scheme_id, 'active', 'monthly', :target_tenor)
                    """
                ),
                [
                    {"scheme_id": f"monthly-{tenor}", "target_tenor": tenor}
                    for tenor in ("1Y", "3Y", "5Y", "7Y", "10Y")
                ],
            )

        captured: dict[str, list[str]] = {}

        def _build(_engine, **kwargs):
            captured["tenors"] = kwargs["tenors"]
            return []

        try:
            with (
                patch(
                    "scheduler.monthly_actuals_updater.create_engine_from_env",
                    return_value=engine,
                ),
                patch(
                    "scheduler.daily_actuals_updater.configured_active_scheme_tenors",
                    return_value=["1Y", "5Y", "10Y"],
                ),
                patch(
                    "scheduler.monthly_actuals_updater.build_monthly_actual_records",
                    side_effect=_build,
                ),
                patch(
                    "scheduler.monthly_actuals_updater.upsert_monthly_actuals",
                    return_value=0,
                ),
            ):
                self.assertEqual(update_monthly_actuals(), 0)
        finally:
            engine.dispose()

        self.assertEqual(captured["tenors"], ["1Y", "3Y", "5Y", "7Y", "10Y"])

    def test_update_monthly_actuals_with_no_active_registry_scope_writes_nothing(self) -> None:
        from scheduler.monthly_actuals_updater import update_monthly_actuals

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

        try:
            with (
                patch(
                    "scheduler.monthly_actuals_updater.create_engine_from_env",
                    return_value=engine,
                ),
                patch(
                    "scheduler.monthly_actuals_updater.build_monthly_actual_records",
                ) as build_records,
                patch(
                    "scheduler.monthly_actuals_updater.upsert_monthly_actuals",
                ) as upsert_records,
                self.assertLogs(
                    "scheduler.daily_actuals_updater",
                    level="WARNING",
                ) as captured,
            ):
                self.assertEqual(update_monthly_actuals(), 0)
        finally:
            engine.dispose()

        build_records.assert_not_called()
        upsert_records.assert_not_called()
        self.assertIn("ACTUAL_TENOR_SCOPE_EMPTY", "\n".join(captured.output))

    def test_build_monthly_actual_records_uses_target_month_observation_vs_feature_month(self) -> None:
        from scheduler.monthly_actuals_updater import build_monthly_actual_records_from_rows
        from shared.prediction_context import MONTHLY_TARGET_RULE

        rows = [
            {"tenor": "10Y", "trade_date": "2026-04-15", "close_yield": 1.77},
            {"tenor": "10Y", "trade_date": "2026-05-15", "close_yield": 1.82},
        ]
        calendar_rows = [
            {"rdate": "2026-04-15", "trade_flag": "1"},
            {"rdate": "2026-05-15", "trade_flag": "1"},
        ]

        records = build_monthly_actual_records_from_rows(rows, calendar_rows)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.tenor, "10Y")
        self.assertEqual(record.predict_date, "2026-04-15")
        self.assertEqual(record.feature_date, "2026-04-15")
        self.assertEqual(record.target_date, "2026-05-15")
        self.assertEqual(record.feature_month_id, "2026-04")
        self.assertEqual(record.target_month_id, "2026-05")
        self.assertEqual(record.direction_monthly, 1)
        self.assertEqual(record.price_signal, "空")
        self.assertEqual(record.target_rule, MONTHLY_TARGET_RULE)

    def test_build_monthly_actual_records_keeps_natural_15th_predict_date(self) -> None:
        from scheduler.monthly_actuals_updater import build_monthly_actual_records_from_rows
        from shared.prediction_context import MONTHLY_TARGET_RULE

        rows = [
            {"tenor": "10Y", "trade_date": "2025-02-14", "close_yield": 1.77},
            {"tenor": "10Y", "trade_date": "2025-03-14", "close_yield": 1.82},
        ]
        calendar_rows = [
            {"rdate": "2025-02-14", "trade_flag": "1"},
            {"rdate": "2025-02-15", "trade_flag": "0"},
            {"rdate": "2025-03-14", "trade_flag": "1"},
            {"rdate": "2025-03-15", "trade_flag": "0"},
        ]

        records = build_monthly_actual_records_from_rows(rows, calendar_rows)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.predict_date, "2025-02-15")
        self.assertEqual(record.feature_date, "2025-02-14")
        self.assertEqual(record.target_date, "2025-03-14")
        self.assertEqual(record.feature_month_id, "2025-02")
        self.assertEqual(record.target_month_id, "2025-03")
        self.assertEqual(record.direction_monthly, 1)
        self.assertEqual(record.target_rule, MONTHLY_TARGET_RULE)

    def test_upsert_monthly_actuals_writes_json_extra(self) -> None:
        from scheduler.repository import upsert_monthly_actuals
        from shared.models import MonthlyActualRecord
        from shared.prediction_context import MONTHLY_TARGET_RULE

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_monthly_actuals (
                        tenor TEXT,
                        feature_month_id TEXT,
                        target_month_id TEXT,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        feature_yield REAL,
                        target_yield REAL,
                        direction_monthly INTEGER,
                        price_signal TEXT,
                        target_rule TEXT,
                        extra TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )

        count = upsert_monthly_actuals(
            engine,
            [
                MonthlyActualRecord(
                    tenor="10Y",
                    feature_month_id="2026-04",
                    target_month_id="2026-05",
                    predict_date="2026-04-15",
                    feature_date="2026-04-15",
                    target_date="2026-05-15",
                    feature_yield=1.77,
                    target_yield=1.82,
                    direction_monthly=1,
                    price_signal="空",
                    target_rule=MONTHLY_TARGET_RULE,
                    extra={"direction_basis": "yield"},
                )
            ],
        )

        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM t_scheme_monthly_actuals")).mappings().one()

        self.assertEqual(count, 1)
        self.assertEqual(row["direction_monthly"], 1)
        self.assertIn("direction_basis", row["extra"])


if __name__ == "__main__":
    unittest.main()
