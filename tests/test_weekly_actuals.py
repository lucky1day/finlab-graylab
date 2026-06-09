from __future__ import annotations

import unittest


def _calendar_rows() -> list[dict]:
    rows = []
    for rdate, week_id, trade_flag in [
        ("2026-05-18", 202619, "1"),
        ("2026-05-19", 202619, "1"),
        ("2026-05-20", 202619, "1"),
        ("2026-05-21", 202619, "1"),
        ("2026-05-22", 202619, "1"),
        ("2026-05-23", 202619, "0"),
        ("2026-05-24", 202619, "0"),
        ("2026-05-25", 202620, "1"),
        ("2026-05-26", 202620, "1"),
        ("2026-05-27", 202620, "1"),
        ("2026-05-28", 202620, "1"),
        ("2026-05-29", 202620, "1"),
        ("2026-05-30", 202620, "0"),
        ("2026-05-31", 202620, "0"),
        ("2026-06-01", 202621, "1"),
        ("2026-06-02", 202621, "1"),
        ("2026-06-03", 202621, "1"),
        ("2026-06-04", 202621, "1"),
        ("2026-06-05", 202621, "1"),
        ("2026-06-06", 202621, "0"),
        ("2026-06-07", 202621, "0"),
    ]:
        rows.append({"rdate": rdate, "week_id": week_id, "trade_flag": trade_flag})
    return rows


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

        records = build_weekly_actual_records_from_rows(rows, _calendar_rows())

        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first.tenor, "10Y")
        self.assertEqual(first.feature_week_id, 202619)
        self.assertEqual(first.feature_date, "2026-05-22")
        self.assertEqual(first.predict_date, "2026-05-23")
        self.assertEqual(first.target_week_id, 202620)
        self.assertEqual(first.target_date, "2026-05-29")
        self.assertEqual(first.feature_yield, 1.70)
        self.assertEqual(first.target_yield, 1.75)
        self.assertEqual(first.direction_weekly, 1)
        self.assertEqual(first.price_signal, "空")

        second = records[1]
        self.assertEqual(second.feature_week_id, 202620)
        self.assertEqual(second.target_week_id, 202621)
        self.assertEqual(second.direction_weekly, 0)
        self.assertEqual(second.price_signal, "平")

    def test_build_weekly_actuals_uses_available_last_trading_day_when_friday_missing(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-21", "close_yield": 1.72},
            {"tenor": "10Y", "trade_date": "2026-05-28", "close_yield": 1.69},
            {"tenor": "10Y", "trade_date": "2026-06-01", "close_yield": 1.68},
        ]

        records = build_weekly_actual_records_from_rows(rows, _calendar_rows())

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

        records = build_weekly_actual_records_from_rows(rows, _calendar_rows())

        self.assertEqual(records, [])

    def test_build_weekly_actuals_reads_canonical_week_id_from_db(self) -> None:
        from sqlalchemy import create_engine, text

        from scheduler.weekly_actuals_updater import build_weekly_actual_records

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE api_wind_date (rdate TEXT PRIMARY KEY, week_id TEXT)"))
            conn.execute(text("CREATE TABLE t_trade_calendar (rdate TEXT PRIMARY KEY, trade_flag TEXT)"))
            conn.execute(
                text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"),
                [{"rdate": row["rdate"], "week_id": str(row["week_id"])} for row in _calendar_rows()],
            )
            conn.execute(
                text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"),
                [{"rdate": row["rdate"], "trade_flag": row["trade_flag"]} for row in _calendar_rows()],
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE api_wind_daily (
                        rdate TEXT,
                        indicators_code TEXT,
                        indicators_value REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_daily (rdate, indicators_code, indicators_value)
                    VALUES (:rdate, 'TB0YWI0C', :value)
                    """
                ),
                [
                    {"rdate": "2026-05-22", "value": 1.70},
                    {"rdate": "2026-05-29", "value": 1.75},
                    {"rdate": "2026-06-05", "value": 1.75},
                ],
            )

        try:
            records = build_weekly_actual_records(engine, tenors=["10Y"])
        finally:
            engine.dispose()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].feature_week_id, 202619)
        self.assertEqual(records[0].target_week_id, 202620)
        self.assertEqual(records[0].predict_date, "2026-05-23")
        self.assertEqual(records[1].feature_week_id, 202620)
        self.assertEqual(records[1].target_week_id, 202621)


if __name__ == "__main__":
    unittest.main()
