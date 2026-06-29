from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


class MonthlyActualsTests(unittest.TestCase):
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
