from __future__ import annotations

import unittest


class _FakeCalendar:
    def __init__(self) -> None:
        self.date_to_week_id = {
            "2026-05-21": 202619,
            "2026-05-22": 202619,
            "2026-05-28": 202620,
            "2026-05-29": 202620,
        }
        self.week_last_trading_day = {
            202619: "2026-05-22",
            202620: "2026-05-29",
        }
        self.trading_days = ("2026-05-21", "2026-05-22", "2026-05-28", "2026-05-29")

    def week_id_for_date(self, value: str) -> int | None:
        return self.date_to_week_id.get(str(value))

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self.week_last_trading_day[int(week_id)]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        return [day for day in self.trading_days if day > value][:count]


class WeeklyAverageBacktestLabelTests(unittest.TestCase):
    def test_average_label_map_uses_target_week_mean_vs_feature_week_mean(self) -> None:
        from backtests.weekly_base_runner import build_weekly_average_label_map_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-21", "close_yield": 1.00},
            {"tenor": "10Y", "trade_date": "2026-05-22", "close_yield": 3.00},
            {"tenor": "10Y", "trade_date": "2026-05-28", "close_yield": 2.50},
            {"tenor": "10Y", "trade_date": "2026-05-29", "close_yield": 2.70},
        ]

        labels = build_weekly_average_label_map_from_rows(
            rows,
            calendar=_FakeCalendar(),
            target_tenor="10Y",
            live_target_start_date="2026-06-01",
        )

        label = labels[202619]
        self.assertEqual(label.feature_week_id, 202619)
        self.assertEqual(label.target_week_id, 202620)
        self.assertEqual(label.feature_date, "2026-05-22")
        self.assertEqual(label.target_date, "2026-05-29")
        self.assertEqual(label.feature_yield, 2.00)
        self.assertEqual(label.target_yield, 2.60)
        self.assertEqual(label.label, 1)
        self.assertAlmostEqual(label.future_return, 0.30)


if __name__ == "__main__":
    unittest.main()
