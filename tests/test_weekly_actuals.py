from __future__ import annotations

import unittest


class WeeklyActualsTests(unittest.TestCase):
    def test_build_weekly_actuals_uses_last_trading_day_and_price_signal(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-21", "close_yield": 1.71},
            {"tenor": "10Y", "trade_date": "2026-05-22", "close_yield": 1.70},
            {"tenor": "10Y", "trade_date": "2026-05-28", "close_yield": 1.74},
            {"tenor": "10Y", "trade_date": "2026-05-29", "close_yield": 1.75},
            {"tenor": "10Y", "trade_date": "2026-06-05", "close_yield": 1.75},
        ]

        records = build_weekly_actual_records_from_rows(rows)

        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first.tenor, "10Y")
        self.assertEqual(first.feature_week_id, 202620)
        self.assertEqual(first.feature_date, "2026-05-22")
        self.assertEqual(first.predict_date, "2026-05-23")
        self.assertEqual(first.target_week_id, 202621)
        self.assertEqual(first.target_date, "2026-05-29")
        self.assertEqual(first.feature_yield, 1.70)
        self.assertEqual(first.target_yield, 1.75)
        self.assertEqual(first.direction_weekly, 1)
        self.assertEqual(first.price_signal, "空")

        second = records[1]
        self.assertEqual(second.feature_week_id, 202621)
        self.assertEqual(second.target_week_id, 202622)
        self.assertEqual(second.direction_weekly, 0)
        self.assertEqual(second.price_signal, "平")

    def test_build_weekly_actuals_uses_available_last_trading_day_when_friday_missing(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-21", "close_yield": 1.72},
            {"tenor": "10Y", "trade_date": "2026-05-28", "close_yield": 1.69},
            {"tenor": "10Y", "trade_date": "2026-06-01", "close_yield": 1.68},
        ]

        records = build_weekly_actual_records_from_rows(rows)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].feature_date, "2026-05-21")
        self.assertEqual(records[0].target_date, "2026-05-28")
        self.assertEqual(records[0].direction_weekly, -1)
        self.assertEqual(records[0].price_signal, "多")

    def test_build_weekly_actuals_skips_incomplete_target_week(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-29", "close_yield": 1.707},
            {"tenor": "10Y", "trade_date": "2026-06-03", "close_yield": 1.714},
        ]

        records = build_weekly_actual_records_from_rows(rows)

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
