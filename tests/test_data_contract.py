from __future__ import annotations

import unittest
from unittest import mock
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine


SHANGHAI = ZoneInfo("Asia/Shanghai")
FACTOR_TABLES = (
    "api_wind_daily",
    "api_wind_derivative_daily",
    "api_wind_weekly",
    "api_wind_derivative_weekly",
    "api_wind_monthly",
    "api_wind_derivative_monthly",
)


class DataContractAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            future=True,
        )
        with self.engine.begin() as connection:
            for table in FACTOR_TABLES:
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE {table} (
                        rdate TEXT NOT NULL,
                        indicators_code TEXT NOT NULL,
                        indicators_value REAL,
                        create_time DATETIME
                    )
                    """
                )
            connection.exec_driver_sql(
                """
                CREATE TABLE api_wind_indicators_all (
                    indicators_code TEXT NOT NULL,
                    create_time DATETIME,
                    update_time DATETIME
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE TABLE api_wind_date "
                "(rdate TEXT NOT NULL, week_id TEXT)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE t_trade_calendar "
                "(rdate TEXT NOT NULL, trade_flag TEXT)"
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def _insert_factor(
        self,
        table: str,
        *,
        rdate: str,
        create_time: str,
        code: str,
    ) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                f"INSERT INTO {table} "
                "(rdate, indicators_code, indicators_value, create_time) "
                "VALUES (?, ?, ?, ?)",
                (rdate, code, 1.0, create_time),
            )

    def _insert_ready_native_anchor(
        self,
        *,
        include_all_curve_tenors: bool = True,
    ) -> None:
        codes = ["TB1YWI0C", "TB5YWI0C", "TB0YWI0C"]
        if include_all_curve_tenors:
            codes.extend(["TB3YWI0C", "TB7YWI0C"])
        for code in codes:
            self._insert_factor(
                "api_wind_daily",
                rdate="2026-07-23",
                create_time="2026-07-24 06:42:00",
                code=code,
            )
        self._insert_factor(
            "api_wind_weekly",
            rdate="2026-07-18",
            create_time="2026-07-24 06:41:00",
            code="WEEKLY_FACTOR",
        )
        self._insert_factor(
            "api_wind_monthly",
            rdate="2026-06-30",
            create_time="2026-07-24 06:40:00",
            code="MONTHLY_FACTOR",
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO api_wind_indicators_all
                    (indicators_code, create_time, update_time)
                VALUES (?, ?, ?)
                """,
                (
                    "FACTOR_A",
                    "2026-07-24 06:39:00",
                    "2026-07-24 06:39:00",
                ),
            )
            connection.exec_driver_sql(
                "INSERT INTO api_wind_date (rdate, week_id) VALUES (?, ?)",
                ("2026-07-23", "202629"),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO t_trade_calendar (rdate, trade_flag)
                VALUES (?, ?)
                """,
                ("2026-07-23", "1"),
            )

    def test_native_readiness_accepts_complete_post_0630_snapshot(
        self,
    ) -> None:
        from shared.data_contract import inspect_native_input_readiness

        self._insert_ready_native_anchor()

        readiness = inspect_native_input_readiness(
            self.engine,
            feature_date="2026-07-23",
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.feature_date, "2026-07-23")
        self.assertEqual(readiness.missing_requirements, ())



    def test_detects_feature_day_write_after_0630_but_not_earlier_row(
        self,
    ) -> None:
        from shared.data_contract import detect_late_source_writes

        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 05:15:00",
            code="EARLY",
        )
        self._insert_factor(
            "api_wind_daily",
            rdate="2026/7/23",
            create_time="2026-07-24 07:10:12",
            code="LATE",
        )
        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-24",
            create_time="2026-07-24 07:11:00",
            code="NEXT_DAY",
        )

        findings = detect_late_source_writes(
            self.engine,
            feature_date="2026-07-23",
            cutoff_at=datetime(
                2026,
                7,
                24,
                6,
                30,
                tzinfo=SHANGHAI,
            ),
        )

        by_table = {finding.table_name: finding for finding in findings}
        self.assertEqual(by_table["api_wind_daily"].late_row_count, 1)
        self.assertEqual(
            by_table["api_wind_daily"].latest_write_at,
            "2026-07-24T07:10:12+08:00",
        )

    def test_detects_late_historical_revision_inside_full_input_domain(
        self,
    ) -> None:
        from shared.data_contract import detect_late_source_writes

        self._insert_factor(
            "api_wind_daily",
            rdate="2026/6/3",
            create_time="2026-07-24 06:45:00",
            code="HISTORICAL_REVISION",
        )
        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-24",
            create_time="2026-07-24 06:50:00",
            code="FUTURE_OUTSIDE_INPUT",
        )

        findings = detect_late_source_writes(
            self.engine,
            feature_date="2026-07-23",
            cutoff_at=datetime(
                2026,
                7,
                24,
                6,
                30,
                tzinfo=SHANGHAI,
            ),
        )

        by_table = {finding.table_name: finding for finding in findings}
        self.assertEqual(by_table["api_wind_daily"].late_row_count, 1)
        self.assertEqual(
            by_table["api_wind_daily"].latest_write_at,
            "2026-07-24T06:45:00+08:00",
        )


    def test_commit_evidence_is_stable_and_changes_with_source_rows(
        self,
    ) -> None:
        from shared.data_contract import capture_source_commit_evidence

        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 05:15:00",
            code="A",
        )
        first = capture_source_commit_evidence(
            self.engine,
            feature_date="2026-07-23",
        )
        repeated = capture_source_commit_evidence(
            self.engine,
            feature_date="2026-07-23",
        )
        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 05:16:00",
            code="B",
        )
        changed = capture_source_commit_evidence(
            self.engine,
            feature_date="2026-07-23",
        )

        self.assertEqual(first.source_commit_token, repeated.source_commit_token)
        self.assertNotEqual(
            first.source_commit_token,
            changed.source_commit_token,
        )
        self.assertEqual(len(first.source_commit_token), 64)
        self.assertEqual(first.feature_date, "2026-07-23")


    def test_commit_evidence_pushes_factor_cutoff_into_sql(self) -> None:
        from shared.data_contract import (
            FACTOR_SOURCE_TABLES,
            capture_source_commit_evidence_from_connection,
        )

        factor_results = []
        for _ in FACTOR_SOURCE_TABLES:
            result = mock.MagicMock()
            result.mappings.return_value.all.return_value = []
            factor_results.append(result)
        metadata_result = mock.MagicMock()
        metadata_result.mappings.return_value.one.return_value = {
            "row_count": 0,
            "latest_create_time": None,
            "latest_update_time": None,
        }
        calendar_results = []
        for _ in range(2):
            result = mock.MagicMock()
            result.mappings.return_value.one.return_value = {
                "row_count": 0,
                "latest_business_key": None,
            }
            calendar_results.append(result)
        connection = mock.MagicMock()
        connection.execute.side_effect = [
            *factor_results,
            metadata_result,
            *calendar_results,
        ]

        capture_source_commit_evidence_from_connection(
            connection,
            feature_date="2026-07-23",
        )

        factor_calls = connection.execute.call_args_list[
            :len(FACTOR_SOURCE_TABLES)
        ]
        self.assertEqual(len(factor_calls), len(FACTOR_SOURCE_TABLES))
        for call in factor_calls:
            self.assertIn("WHERE rdate <= :feature_date", str(call.args[0]))
            self.assertEqual(call.args[1], {"feature_date": "2026-07-23"})

    def test_commit_evidence_rejects_rows_after_contract_cutoff(
        self,
    ) -> None:
        from shared.data_contract import (
            assert_source_commit_evidence_at_cutoff,
            capture_source_commit_evidence,
        )

        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 07:10:12",
            code="LATE",
        )
        evidence = capture_source_commit_evidence(
            self.engine,
            feature_date="2026-07-23",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "after the supplied cutoff",
        ):
            assert_source_commit_evidence_at_cutoff(
                evidence,
                cutoff_at=datetime(
                    2026,
                    7,
                    24,
                    6,
                    30,
                    tzinfo=SHANGHAI,
                ),
            )


    def test_cutoff_must_be_timezone_aware(self) -> None:
        from shared.data_contract import detect_late_source_writes

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            detect_late_source_writes(
                self.engine,
                feature_date="2026-07-23",
                cutoff_at=datetime(2026, 7, 24, 6, 30),
            )
