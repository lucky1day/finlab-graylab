from __future__ import annotations

import unittest

from shared.models import PredictionRecord


class PredictionSemanticsTests(unittest.TestCase):
    def test_period_average_semantics_accepts_anchor_day_signal(self) -> None:
        from datetime import date, timedelta

        from harness.gates.prediction_semantics import validate_live_record_semantics

        rows = []
        current = date(2024, 1, 1)
        while current <= date(2024, 6, 30):
            rows.append({"rdate": current.isoformat(), "trade_flag": "1"})
            current += timedelta(days=1)

        class PeriodCalendar:
            def period_calendar_rows(self):
                return tuple(rows)

        record = PredictionRecord(
            scheme_id="quarterly-demo",
            target_tenor="10Y",
            horizon=1,
            predict_date="2024-03-29",
            feature_date="2024-03-29",
            target_date="2024-03-30",
            predicted_direction=1,
        )

        errors = validate_live_record_semantics(
            record,
            expected_predict_date="2024-03-29",
            prefix="record[0]",
            require_phase=False,
            frequency="quarterly",
            horizon=1,
            calendar=PeriodCalendar(),
            task_type="quarterly_average",
        )

        self.assertEqual(errors, [])

    def test_monthly_semantics_accepts_source_trigger_day_context(self) -> None:
        from harness.gates.prediction_semantics import validate_live_record_semantics
        from shared.prediction_context import MONTHLY_TARGET_RULE

        calendar = _MonthlyCalendar()
        record = PredictionRecord(
            scheme_id="monthly_10y_rf_top5_0629",
            target_tenor="10Y",
            horizon=30,
            predict_date="2026-04-15",
            feature_date="2026-04-15",
            target_date="2026-05-15",
            predicted_direction=-1,
            extra={
                "frequency": "monthly",
                "db_rdate": "2026-04-15",
                "trigger_date": "2026-04-15",
                "scheduled_trigger_date": "2026-04-15",
                "input_cutoff_date": "2026-04-15",
                "feature_month_id": "2026-04",
                "target_month_id": "2026-05",
                "target_rule": MONTHLY_TARGET_RULE,
            },
        )

        errors = validate_live_record_semantics(
            record,
            expected_predict_date="2026-04-15",
            prefix="record[0]",
            require_phase=False,
            frequency="monthly",
            horizon=30,
            calendar=calendar,
        )

        self.assertEqual(errors, [])

    def test_monthly_semantics_keeps_natural_15th_predict_date_on_non_trading_day(self) -> None:
        from harness.gates.prediction_semantics import validate_live_record_semantics
        from shared.prediction_context import MONTHLY_TARGET_RULE

        calendar = _NonTradingMonthlyCalendar()
        record = PredictionRecord(
            scheme_id="monthly_10y_rf_top5_0629",
            target_tenor="10Y",
            horizon=30,
            predict_date="2025-02-15",
            feature_date="2025-02-14",
            target_date="2025-03-14",
            predicted_direction=1,
            extra={
                "frequency": "monthly",
                "db_rdate": "2025-02-15",
                "trigger_date": "2025-02-15",
                "scheduled_trigger_date": "2025-02-15",
                "input_cutoff_date": "2025-02-14",
                "feature_month_id": "2025-02",
                "target_month_id": "2025-03",
                "target_rule": MONTHLY_TARGET_RULE,
            },
        )

        errors = validate_live_record_semantics(
            record,
            expected_predict_date="2025-02-15",
            prefix="record[0]",
            require_phase=False,
            frequency="monthly",
            horizon=30,
            calendar=calendar,
        )

        self.assertEqual(errors, [])

    def test_weekly_average_target_rule_can_override_calendar_default_rule(self) -> None:
        from harness.gates.prediction_semantics import validate_live_record_semantics
        from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE

        calendar = _Calendar()
        record = PredictionRecord(
            scheme_id="weekly_avg_5y_lgbm_0529",
            target_tenor="5Y",
            horizon=6,
            predict_date="2026-06-13",
            feature_date="2026-06-12",
            target_date="2026-06-19",
            predicted_direction=1,
            extra={
                "feature_week_id": 202624,
                "target_week_id": 202625,
                "target_rule": WEEKLY_AVERAGE_TARGET_RULE,
            },
        )

        errors = validate_live_record_semantics(
            record,
            expected_predict_date="2026-06-13",
            prefix="record[0]",
            require_phase=False,
            frequency="weekly",
            horizon=6,
            calendar=calendar,
            expected_weekly_target_rule=WEEKLY_AVERAGE_TARGET_RULE,
        )

        self.assertEqual(errors, [])


class _Calendar:
    def previous_trading_day(self, day: str) -> str:
        return {"2026-06-13": "2026-06-12"}[day]

    def week_id_for_date(self, day: str) -> int | None:
        return {"2026-06-12": 202624, "2026-06-15": 202625}.get(day)

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return {"2026-06-12": ["2026-06-15"]}.get(day, [])[:count]

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return {202624: "2026-06-12", 202625: "2026-06-19"}[int(week_id)]


class _MonthlyCalendar:
    def covers(self, day: str) -> bool:
        return day in {"2026-04-15", "2026-05-15"}

    def is_trading_day(self, day: str) -> bool:
        return day in {"2026-04-15", "2026-05-15"}

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return {
            "2026-04-14": ["2026-04-15"],
            "2026-05-14": ["2026-05-15"],
        }.get(day, [])[:count]

    def previous_trading_day(self, day: str) -> str:
        return {
            "2026-04-16": "2026-04-15",
            "2026-05-16": "2026-05-15",
        }[day]


class _NonTradingMonthlyCalendar:
    def covers(self, day: str) -> bool:
        return day in {"2025-02-15", "2025-03-15"}

    def is_trading_day(self, day: str) -> bool:
        return day in {"2025-02-14", "2025-03-14"}

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return {
            "2025-02-13": ["2025-02-14"],
            "2025-03-13": ["2025-03-14"],
        }.get(day, [])[:count]

    def previous_trading_day(self, day: str) -> str:
        return {
            "2025-02-15": "2025-02-14",
            "2025-03-15": "2025-03-14",
        }[day]

