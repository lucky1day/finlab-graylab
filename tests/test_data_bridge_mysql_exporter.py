from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError


@dataclass
class _FakeConnection:
    commands: list[str]
    rolled_back: bool = False

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def exec_driver_sql(self, statement: str) -> None:
        self.commands.append(statement)

    def rollback(self) -> None:
        self.rolled_back = True


@dataclass
class _FakeEngine:
    connection: _FakeConnection

    def connect(self) -> _FakeConnection:
        return self.connection


class DataBridgeMySqlExporterTests(unittest.TestCase):
    def test_sqlalchemy_snapshot_error_preserves_cli_configuration_classification(
        self,
    ) -> None:
        from shared.data_bridge.mysql_exporter import (
            MySqlDataBridgeRoundBuilder,
        )
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            error = SQLAlchemyError("connection setup failed")
            engine = type("BrokenEngine", (), {"connect": lambda self: (_ for _ in ()).throw(error)})()

            with self.assertRaises(SQLAlchemyError) as caught:
                MySqlDataBridgeRoundBuilder(engine=engine, config=config).build(
                    "round-1",
                    expected_daily_date="2026-07-18",
                    previous_keys=None,
                )
            self.assertIs(caught.exception, error)

    def test_os_snapshot_error_preserves_cli_configuration_classification(self) -> None:
        from shared.data_bridge.mysql_exporter import (
            MySqlDataBridgeRoundBuilder,
        )
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            error = OSError("socket unavailable")
            engine = type("BrokenEngine", (), {"connect": lambda self: (_ for _ in ()).throw(error)})()

            with self.assertRaises(OSError) as caught:
                MySqlDataBridgeRoundBuilder(engine=engine, config=config).build(
                    "round-1",
                    expected_daily_date="2026-07-18",
                    previous_keys=None,
                )
            self.assertIs(caught.exception, error)

    def test_builds_all_outputs_from_one_read_only_rr_snapshot(self) -> None:
        from shared.data_bridge.mysql_exporter import (
            MySqlDataBridgeRoundBuilder,
        )
        from shared.data_bridge.refresh import DataBridgeRefreshConfig
        from shared.data_contract import (
            SourceCommitEvidence,
            SourceTableEvidence,
            source_commit_evidence_sha256,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                daily_start_date="2026-01-01",
            )
            connection = _FakeConnection(commands=[])
            engine = _FakeEngine(connection)
            evidence_without_token = SourceCommitEvidence(
                feature_date="2026-07-18",
                source_commit_token="",
                tables=(
                    SourceTableEvidence(
                        table_name="api_wind_daily",
                        row_count=1,
                        latest_create_time=None,
                    ),
                ),
            )
            evidence = SourceCommitEvidence(
                feature_date=evidence_without_token.feature_date,
                source_commit_token=source_commit_evidence_sha256(
                    evidence_without_token
                ),
                tables=evidence_without_token.tables,
            )

            with (
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "_assert_required_source_tables",
                ) as tables,
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_daily_output_from_db",
                    return_value=_frames()["daily_output.csv"],
                ) as daily,
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_weekly_output_from_db",
                    return_value=_frames()["weekly_output.csv"],
                ) as weekly,
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_monthly_output_from_db",
                    return_value=_frames()["monthly_output.csv"],
                ) as monthly,
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "capture_source_commit_evidence_from_connection",
                    return_value=evidence,
                ),
            ):
                result = MySqlDataBridgeRoundBuilder(
                    engine=engine,
                    config=config,
                ).build(
                    "round-1",
                    expected_daily_date="2026-07-18",
                    previous_keys=None,
                )

            self.assertEqual(
                connection.commands,
                [
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
                ],
            )
            self.assertTrue(connection.rolled_back)
            tables.assert_called_once_with(connection)
            daily.assert_called_once_with(
                start_date="2026-01-01",
                end_date="2026-07-18",
                engine=connection,
            )
            weekly.assert_called_once_with(
                as_of_date="2026-07-18",
                engine=connection,
            )
            monthly.assert_called_once_with(
                start_date="2026-01-01",
                end_date="2026-07-18",
                engine=connection,
            )
            self.assertEqual(result.source_mode, "local_mysql")
            self.assertEqual(
                result.source_provenance["feature_date"],
                "2026-07-18",
            )
            self.assertEqual(
                result.source_provenance["source_commit_token"],
                evidence.source_commit_token,
            )

    def test_data_service_sql_filters_source_rows_at_feature_cutoff(self) -> None:
        from shared import data_service

        engine = object()
        with patch.object(
            data_service.pd,
            "read_sql",
            return_value=pd.DataFrame(),
        ) as read_sql:
            data_service._read_long_from_db(
                ["factor_a", "factor_b"],
                "api_wind_daily",
                ["rdate", "indicators_code", "indicators_value"],
                engine=engine,
                end_date="2026-07-18",
            )

        self.assertIn("rdate <= %s", read_sql.call_args.args[0])
        self.assertEqual(
            read_sql.call_args.kwargs["params"],
            ("factor_a", "factor_b", "2026-07-18"),
        )

    def test_required_source_table_probe_fails_closed_for_each_of_nine_tables(
        self,
    ) -> None:
        from shared.data_bridge.mysql_exporter import (
            LOCAL_MYSQL_REQUIRED_TABLES,
            _assert_required_source_tables,
        )
        from shared.data_bridge.refresh import DataBridgeRefreshError

        expected_tables = {
            "api_wind_daily",
            "api_wind_derivative_daily",
            "api_wind_weekly",
            "api_wind_derivative_weekly",
            "api_wind_monthly",
            "api_wind_derivative_monthly",
            "api_wind_indicators_all",
            "api_wind_date",
            "t_trade_calendar",
        }
        self.assertEqual(set(LOCAL_MYSQL_REQUIRED_TABLES), expected_tables)
        self.assertEqual(len(LOCAL_MYSQL_REQUIRED_TABLES), 9)

        for unavailable_table in LOCAL_MYSQL_REQUIRED_TABLES:
            with self.subTest(unavailable_table=unavailable_table):
                connection = _RequiredTableProbeConnection(unavailable_table)

                with self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    f"required source table is unavailable: {unavailable_table}",
                ):
                    _assert_required_source_tables(connection)

                self.assertEqual(connection.probed_tables[-1], unavailable_table)


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026-07-17", "2026-07-18"], "factor": ["1", "2"]}
        ),
        "weekly_output.csv": pd.DataFrame(
            {"week_id": ["202628", "202629"], "factor": ["3", "4"]}
        ),
        "monthly_output.csv": pd.DataFrame(
            {"month_id": ["202606", "202607"], "factor": ["5", "6"]}
        ),
    }


@dataclass
class _RequiredTableProbeConnection:
    unavailable_table: str
    probed_tables: list[str] | None = None

    def __post_init__(self) -> None:
        self.probed_tables = []

    def exec_driver_sql(self, statement: str) -> None:
        table_name = statement.split("`")[1]
        self.probed_tables.append(table_name)
        if table_name == self.unavailable_table:
            raise RuntimeError("table is unavailable")


def _write_schema(root: Path) -> Path:
    path = root / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {
                    "daily_output.csv": {"columns": ["date", "factor"]},
                    "weekly_output.csv": {"columns": ["week_id", "factor"]},
                    "monthly_output.csv": {"columns": ["month_id", "factor"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
