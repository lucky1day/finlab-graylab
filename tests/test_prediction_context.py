from __future__ import annotations

import unittest


class PredictionContextTests(unittest.TestCase):
    def test_build_daily_live_context_uses_previous_trading_day_and_horizon_target(self) -> None:
        from shared.prediction_context import build_daily_live_context

        calendar = _Calendar(
            previous={"2026-06-12": "2026-06-11"},
            nth={("2026-06-11", 5): "2026-06-18"},
        )

        context = build_daily_live_context(calendar, "2026-06-12", horizon=5)

        self.assertEqual(context.feature_date, "2026-06-11")
        self.assertEqual(context.target_date, "2026-06-18")

    def test_build_weekly_live_context_uses_feature_date_week_and_next_calendar_week(self) -> None:
        from shared.prediction_context import (
            WEEKLY_TARGET_RULE,
            build_weekly_live_context,
        )

        calendar = _Calendar(
            previous={"2026-06-13": "2026-06-12"},
            week_for_date={"2026-06-12": 202624, "2026-06-15": 202625},
            next_days={"2026-06-12": ["2026-06-15"]},
            last_day={202624: "2026-06-12", 202625: "2026-06-19"},
        )

        context = build_weekly_live_context(calendar, "2026-06-13")

        self.assertEqual(context.feature_date, "2026-06-12")
        self.assertEqual(context.feature_week_id, 202624)
        self.assertEqual(context.target_week_id, 202625)
        self.assertEqual(context.target_date, "2026-06-19")
        self.assertEqual(context.target_rule, WEEKLY_TARGET_RULE)

    def test_build_weekly_live_context_falls_back_to_predict_week_when_latest_week_has_no_next_week(self) -> None:
        from shared.prediction_context import build_weekly_live_context

        calendar = _Calendar(
            previous={"2026-07-04": "2026-07-03"},
            week_for_date={
                "2026-07-02": 202625,
                "2026-07-03": 202626,
                "2026-07-04": 202625,
                "2026-07-06": 202626,
            },
            next_days={
                "2026-07-02": ["2026-07-06"],
                "2026-07-10": [],
            },
            last_day={202625: "2026-07-02", 202626: "2026-07-10"},
        )

        context = build_weekly_live_context(calendar, "2026-07-04")

        self.assertEqual(context.feature_date, "2026-07-02")
        self.assertEqual(context.feature_week_id, 202625)
        self.assertEqual(context.target_week_id, 202626)
        self.assertEqual(context.target_date, "2026-07-10")

    def test_next_calendar_week_id_rejects_missing_next_week(self) -> None:
        from shared.prediction_context import next_calendar_week_id

        calendar = _Calendar(
            last_day={202624: "2026-06-12"},
            next_days={"2026-06-12": ["2026-06-15"]},
            week_for_date={"2026-06-15": 202624},
        )

        with self.assertRaisesRegex(ValueError, "下一周"):
            next_calendar_week_id(calendar, 202624)


class _Calendar:
    def __init__(
        self,
        *,
        previous: dict[str, str] | None = None,
        nth: dict[tuple[str, int], str] | None = None,
        week_for_date: dict[str, int | None] | None = None,
        next_days: dict[str, list[str]] | None = None,
        last_day: dict[int, str] | None = None,
    ) -> None:
        self.previous = previous or {}
        self.nth = nth or {}
        self.week_for_date = week_for_date or {}
        self.next_days = next_days or {}
        self.last_day = last_day or {}

    def previous_trading_day(self, day: str) -> str:
        return self.previous[day]

    def nth_trading_day_after(self, day: str, horizon: int) -> str:
        return self.nth[(day, horizon)]

    def week_id_for_date(self, day: str) -> int | None:
        return self.week_for_date.get(day)

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return self.next_days.get(day, [])[:count]

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self.last_day[int(week_id)]


if __name__ == "__main__":
    unittest.main()
