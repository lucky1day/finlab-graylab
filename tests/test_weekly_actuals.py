from __future__ import annotations

import unittest


def _point_records(records):
    from shared.prediction_context import WEEKLY_TARGET_RULE

    return [record for record in records if record.target_rule == WEEKLY_TARGET_RULE]


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


def _create_weekly_actuals_table(conn) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            """
            CREATE TABLE t_scheme_weekly_actuals (
                id INTEGER PRIMARY KEY,
                tenor TEXT NOT NULL,
                predict_date TEXT NOT NULL,
                target_rule TEXT NOT NULL
            )
            """
        )
    )


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

        point_records = _point_records(records)

        self.assertEqual(len(point_records), 2)
        first = point_records[0]
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

        second = point_records[1]
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

        point_records = _point_records(records)

        self.assertEqual(len(point_records), 1)
        self.assertEqual(point_records[0].feature_date, "2026-05-21")
        self.assertEqual(point_records[0].target_date, "2026-05-28")
        self.assertEqual(point_records[0].direction_weekly, -1)
        self.assertEqual(point_records[0].price_signal, "多")

    def test_build_weekly_actuals_emits_average_rule_using_weekly_means(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows
        from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, WEEKLY_TARGET_RULE

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-21", "close_yield": 1.00},
            {"tenor": "10Y", "trade_date": "2026-05-22", "close_yield": 3.00},
            {"tenor": "10Y", "trade_date": "2026-05-28", "close_yield": 2.50},
            {"tenor": "10Y", "trade_date": "2026-05-29", "close_yield": 2.70},
        ]

        records = build_weekly_actual_records_from_rows(rows, _calendar_rows())
        by_rule = {record.target_rule: record for record in records}

        self.assertEqual(set(by_rule), {WEEKLY_TARGET_RULE, WEEKLY_AVERAGE_TARGET_RULE})
        self.assertEqual(by_rule[WEEKLY_TARGET_RULE].feature_yield, 3.00)
        self.assertEqual(by_rule[WEEKLY_TARGET_RULE].target_yield, 2.70)
        self.assertEqual(by_rule[WEEKLY_TARGET_RULE].direction_weekly, -1)
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].feature_date, "2026-05-22")
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].target_date, "2026-05-29")
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].feature_yield, 2.00)
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].target_yield, 2.60)
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].direction_weekly, 1)
        self.assertEqual(by_rule[WEEKLY_AVERAGE_TARGET_RULE].price_signal, "空")

    def test_build_weekly_actuals_skips_incomplete_target_week(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        rows = [
            {"tenor": "10Y", "trade_date": "2026-05-29", "close_yield": 1.707},
            {"tenor": "10Y", "trade_date": "2026-06-03", "close_yield": 1.714},
        ]

        records = build_weekly_actual_records_from_rows(rows, _calendar_rows())

        self.assertEqual(records, [])

    def test_build_weekly_actuals_normalizes_isolated_week_id_jump(self) -> None:
        from scheduler.weekly_actuals_updater import build_weekly_actual_records_from_rows

        calendar_rows = _calendar_rows() + [
            {"rdate": "2026-06-08", "week_id": 202622, "trade_flag": "1"},
            {"rdate": "2026-06-09", "week_id": 202622, "trade_flag": "1"},
            {"rdate": "2026-06-10", "week_id": 202622, "trade_flag": "1"},
            {"rdate": "2026-06-11", "week_id": 202622, "trade_flag": "1"},
            {"rdate": "2026-06-12", "week_id": 202622, "trade_flag": "1"},
            {"rdate": "2026-06-13", "week_id": 202622, "trade_flag": "0"},
            {"rdate": "2026-06-14", "week_id": 202622, "trade_flag": "0"},
            {"rdate": "2026-06-15", "week_id": 202623, "trade_flag": "1"},
            {"rdate": "2026-06-16", "week_id": 202623, "trade_flag": "1"},
            {"rdate": "2026-06-17", "week_id": 202623, "trade_flag": "1"},
            {"rdate": "2026-06-18", "week_id": 202623, "trade_flag": "1"},
            {"rdate": "2026-06-19", "week_id": 202623, "trade_flag": "1"},
            {"rdate": "2026-06-20", "week_id": 202623, "trade_flag": "0"},
            {"rdate": "2026-06-21", "week_id": 202623, "trade_flag": "0"},
            {"rdate": "2026-06-22", "week_id": 202624, "trade_flag": "1"},
            {"rdate": "2026-06-23", "week_id": 202624, "trade_flag": "1"},
            {"rdate": "2026-06-24", "week_id": 202624, "trade_flag": "1"},
            {"rdate": "2026-06-25", "week_id": 202624, "trade_flag": "1"},
            {"rdate": "2026-06-26", "week_id": 202624, "trade_flag": "1"},
            {"rdate": "2026-06-27", "week_id": 202624, "trade_flag": "0"},
            {"rdate": "2026-06-28", "week_id": 202624, "trade_flag": "0"},
            {"rdate": "2026-06-29", "week_id": 202625, "trade_flag": "1"},
            {"rdate": "2026-06-30", "week_id": 202625, "trade_flag": "1"},
            {"rdate": "2026-07-01", "week_id": 202625, "trade_flag": "1"},
            {"rdate": "2026-07-02", "week_id": 202625, "trade_flag": "1"},
            {"rdate": "2026-07-03", "week_id": 202626, "trade_flag": "1"},
            {"rdate": "2026-07-04", "week_id": 202625, "trade_flag": "0"},
            {"rdate": "2026-07-05", "week_id": 202625, "trade_flag": "0"},
            {"rdate": "2026-07-06", "week_id": 202626, "trade_flag": "1"},
            {"rdate": "2026-07-07", "week_id": 202626, "trade_flag": "1"},
            {"rdate": "2026-07-08", "week_id": 202626, "trade_flag": "1"},
            {"rdate": "2026-07-09", "week_id": 202626, "trade_flag": "1"},
            {"rdate": "2026-07-10", "week_id": 202626, "trade_flag": "1"},
        ]
        rows = [
            {"tenor": "10Y", "trade_date": "2026-06-26", "close_yield": 1.80},
            {"tenor": "10Y", "trade_date": "2026-06-29", "close_yield": 1.81},
            {"tenor": "10Y", "trade_date": "2026-06-30", "close_yield": 1.82},
            {"tenor": "10Y", "trade_date": "2026-07-01", "close_yield": 1.83},
            {"tenor": "10Y", "trade_date": "2026-07-02", "close_yield": 1.84},
            {"tenor": "10Y", "trade_date": "2026-07-03", "close_yield": 1.85},
            {"tenor": "10Y", "trade_date": "2026-07-10", "close_yield": 1.86},
        ]

        records = build_weekly_actual_records_from_rows(rows, calendar_rows)
        point_records = _point_records(records)
        actual = next(record for record in point_records if record.feature_week_id == 202624)

        self.assertEqual(actual.target_week_id, 202625)
        self.assertEqual(actual.target_date, "2026-07-03")
        self.assertEqual(actual.target_yield, 1.85)
        self.assertEqual(actual.direction_weekly, 1)

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

        point_records = _point_records(records)

        self.assertEqual(len(point_records), 2)
        self.assertEqual(point_records[0].feature_week_id, 202619)
        self.assertEqual(point_records[0].target_week_id, 202620)
        self.assertEqual(point_records[0].predict_date, "2026-05-23")
        self.assertEqual(point_records[1].feature_week_id, 202620)
        self.assertEqual(point_records[1].target_week_id, 202621)

    def test_weekly_actual_write_guard_rejects_legacy_unique_key(self) -> None:
        from sqlalchemy import create_engine, text

        from scheduler.repository import _assert_weekly_actuals_target_rule_unique_key

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            _create_weekly_actuals_table(conn)
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX uk_weekly_actual_predict
                    ON t_scheme_weekly_actuals (tenor, predict_date)
                    """
                )
            )
            with self.assertRaisesRegex(RuntimeError, "uk_weekly_actual_predict_rule"):
                _assert_weekly_actuals_target_rule_unique_key(conn)
        engine.dispose()

    def test_weekly_actual_write_guard_accepts_target_rule_unique_key(self) -> None:
        from sqlalchemy import create_engine, text

        from scheduler.repository import _assert_weekly_actuals_target_rule_unique_key

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            _create_weekly_actuals_table(conn)
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX uk_weekly_actual_predict_rule
                    ON t_scheme_weekly_actuals (tenor, predict_date, target_rule)
                    """
                )
            )
            _assert_weekly_actuals_target_rule_unique_key(conn)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
