from __future__ import annotations

import unittest

from shared.models import PredictionRecord


class PredictionSemanticsTests(unittest.TestCase):
    def test_weekly_average_target_rule_can_override_calendar_default_rule(self) -> None:
        from harness.gates.prediction_semantics import validate_live_record_semantics
        from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE

        calendar = _Calendar()
        record = PredictionRecord(
            scheme_id="weekly_avg_5y_direct_0529",
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


if __name__ == "__main__":
    unittest.main()
