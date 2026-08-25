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

    def test_commit_evidence_is_stable_and_changes_with_source_rows(
        self,
    ) -> None:
        from shared.data_contract import (
            capture_source_commit_evidence_from_connection,
        )

        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 05:15:00",
            code="A",
        )
        with self.engine.connect() as connection:
            first = capture_source_commit_evidence_from_connection(
                connection,
                feature_date="2026-07-23",
            )
            repeated = capture_source_commit_evidence_from_connection(
                connection,
                feature_date="2026-07-23",
            )
        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 05:16:00",
            code="B",
        )
        with self.engine.connect() as connection:
            changed = capture_source_commit_evidence_from_connection(
                connection,
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
            capture_source_commit_evidence_from_connection,
        )

        self._insert_factor(
            "api_wind_daily",
            rdate="2026-07-23",
            create_time="2026-07-24 07:10:12",
            code="LATE",
        )
        with self.engine.connect() as connection:
            evidence = capture_source_commit_evidence_from_connection(
                connection,
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
