from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "blackbox_v2"
    / "data_bridge_v1_schema.json"
)


def _frame(filename: str, keys: list[str]) -> pd.DataFrame:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    columns = list(schema["files"][filename]["columns"])
    return pd.DataFrame(
        [
            {
                column: (
                    key
                    if column == columns[0]
                    else str(index + 1)
                )
                for index, column in enumerate(columns)
            }
            for key in keys
        ],
        columns=columns,
    )


def _with_factor_catalog(
    frames: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    rows = []
    for frequency, filename in (
        ("daily", "daily_output.csv"),
        ("weekly", "weekly_output.csv"),
        ("monthly", "monthly_output.csv"),
    ):
        rows.extend(
            {
                "indicators_code": column,
                "frequency": frequency,
                "factor_version": "V1.0",
            }
            for column in frames[filename].columns[1:]
        )
    return {
        **frames,
        "factor_catalog.csv": pd.DataFrame(rows),
    }


def _current_dataset(
    *,
    upstream_generation_id: str = "full-20260724-065000-abcdef123456",
):
    from shared.data_bridge.refresh import CurrentDataset
    from shared.data_bridge.validation import validate_dataset

    frames = _with_factor_catalog({
        "daily_output.csv": _frame(
            "daily_output.csv",
            ["2026-07-22", "2026-07-23"],
        ),
        "weekly_output.csv": _frame(
            "weekly_output.csv",
            ["202628", "202629", "202630"],
        ),
        "monthly_output.csv": _frame(
            "monthly_output.csv",
            ["202606", "202607", "202608"],
        ),
        "api_wind_date.csv": pd.DataFrame(
            {
                "rdate": ["2026-07-22", "2026-07-23"],
                "week_id": ["202630", "202630"],
            }
        ),
    })
    dataset = validate_dataset(
        frames,
        schema_path=SCHEMA_PATH,
        expected_daily_date="2026-07-23",
    )
    state = {
        "generation_id": upstream_generation_id,
        "refresh_date": "2026-07-24",
        "refresh_started_at": "2026-07-24T06:30:00+08:00",
        "refreshed_at": "2026-07-24T06:50:00+08:00",
        "published_at": "2026-07-24T06:50:01+08:00",
        "source_mode": "full_export",
        "stability_rounds": 2,
        "last_attempt": {
            "status": "success",
            "refresh_date": "2026-07-24",
            "started_at": "2026-07-24T06:30:00+08:00",
            "finished_at": "2026-07-24T06:50:00+08:00",
            "duration_sec": 1200.0,
            "error": None,
        },
        "schema_version": dataset.schema_version,
        "business_digest": dataset.business_digest,
        "files": {
            filename: {
                "sha256": profile.sha256,
                "rows": profile.rows,
                "columns": profile.columns,
            }
            for filename, profile in dataset.files.items()
        },
    }
    return CurrentDataset(
        state=MappingProxyType(state),
        dataset=dataset,
    )


def _v3_period_bootstrap_inputs():
    from shared import input_artifacts
    from shared.blackbox_v2.snapshot import CutoffKeys
    from shared.data_bridge.refresh import DataBridgeRefreshConfig

    current = _current_dataset()
    state = dict(current.state)
    state["publication_manifest_version"] = "data-bridge-current-v3"
    current = replace(
        current,
        state=MappingProxyType(state),
        publication_manifest={"manifest_version": "data-bridge-current-v3"},
    )
    resolved = input_artifacts._ResolvedBlackboxInputCutoffs(
        cutoff_keys=CutoffKeys(
            daily_cutoff_key="2026-08-07",
            weekly_cutoff_key="202630",
            monthly_cutoff_key="202608",
        ),
        source_weekly_cutoff_key="202631",
        source_monthly_cutoff_key="202608",
    )
    config = DataBridgeRefreshConfig(
        data_root=Path("/tmp/data"),
        runtime_root=Path("/tmp/runtime"),
        schema_path=SCHEMA_PATH,
    )
    return current, resolved, config


class _StaticDataBridgeRoundBuilder:
    """用固定五文件数据驱动真实 refresh/publish 链路。"""

    staging_root: Path
    frames: dict[str, pd.DataFrame]

    def __init__(self, *, engine=None, config=None) -> None:  # noqa: ARG002
        self.calls = 0

    def build(
        self,
        round_id: str,
        *,
        end_date: str,  # noqa: ARG002
        expected_daily_date: str,
        previous_keys=None,
        continuity_cutoffs=None,
    ):
        from shared.data_bridge.refresh import DownloadRound
        from shared.data_bridge.validation import (
            validate_dataset,
            write_validated_dataset,
        )

        self.calls += 1
        dataset = validate_dataset(
            self.frames,
            schema_path=SCHEMA_PATH,
            expected_daily_date=expected_daily_date,
            previous_keys=previous_keys,
            continuity_cutoffs=continuity_cutoffs,
        )
        directory = self.staging_root / f"{round_id}-{self.calls}"
        directory.parent.mkdir(parents=True, exist_ok=True)
        write_validated_dataset(dataset, directory)
        return DownloadRound(
            round_id=round_id,
            directory=directory,
            dataset=dataset,
            digest=dataset.business_digest,
        )


def _bridge_frames(*, include_new_week: bool) -> dict[str, pd.DataFrame]:
    daily_keys = ["2026-07-17"]
    week_keys = ["202629"]
    if include_new_week:
        daily_keys.append("2026-07-20")
        week_keys.append("202630")
    return _with_factor_catalog({
        "daily_output.csv": _frame("daily_output.csv", daily_keys),
        "weekly_output.csv": _frame("weekly_output.csv", week_keys),
        "monthly_output.csv": _frame("monthly_output.csv", ["202607"]),
        "api_wind_date.csv": pd.DataFrame(
            {
                "rdate": daily_keys,
                "week_id": week_keys,
            }
        ),
    })


@contextmanager
def _cross_week_cutoff_source():
    """保留真实 cutoff 解析，仅替换数据库读取。"""
    from shared import data_service

    metadata = pd.DataFrame({"indicators_code": ["X0000001"]})
    with (
        patch.object(
            data_service,
            "read_factor_metadata_from_db",
            return_value=metadata,
        ),
        patch.object(
            data_service,
            "select_monthly_factor_metadata",
            return_value=metadata,
        ),
        patch.object(
            data_service,
            "read_weekly_long_from_db",
            return_value=pd.DataFrame(),
        ),
        patch.object(
            data_service,
            "read_monthly_long_from_db",
            return_value=pd.DataFrame(),
        ),
        patch.object(
            data_service,
            "build_weekly_cutoff_index_from_frames",
            return_value=pd.DataFrame(
                [
                    {"week_id": "202629", "available_date": "2026-07-13"},
                    {"week_id": "202630", "available_date": "2026-07-20"},
                ]
            ),
        ),
        patch.object(
            data_service,
            "build_monthly_cutoff_index_from_frames",
            return_value=pd.DataFrame(
                [{"month_id": "202607", "available_date": "2026-07-01"}]
            ),
        ),
    ):
        yield


class DataBridgeCurrentTests(unittest.TestCase):
    def test_calendar_future_tail_cannot_disappear(self) -> None:
        from shared.data_bridge.validation import (
            DataBridgeValidationError,
            validate_dataset,
        )

        previous = validate_dataset(
            _with_factor_catalog({
                "daily_output.csv": _frame(
                    "daily_output.csv", ["2026-08-07"]
                ),
                "weekly_output.csv": _frame(
                    "weekly_output.csv", ["202632"]
                ),
                "monthly_output.csv": _frame(
                    "monthly_output.csv", ["202608"]
                ),
                "api_wind_date.csv": pd.DataFrame(
                    {
                        "rdate": [
                            "2026-08-07",
                            "2026-08-08",
                            "2026-08-09",
                        ],
                        "week_id": ["202632", "202632", "202632"],
                    }
                ),
            }),
            schema_path=SCHEMA_PATH,
            expected_daily_date="2026-08-07",
        )
        candidate = _with_factor_catalog({
            "daily_output.csv": _frame(
                "daily_output.csv", ["2026-08-07"]
            ),
            "weekly_output.csv": _frame(
                "weekly_output.csv", ["202632"]
            ),
            "monthly_output.csv": _frame(
                "monthly_output.csv", ["202608"]
            ),
            "api_wind_date.csv": pd.DataFrame(
                {"rdate": ["2026-08-07"], "week_id": ["202632"]}
            ),
        })

        with self.assertRaisesRegex(
            DataBridgeValidationError,
            "historical keys disappeared",
        ):
            validate_dataset(
                candidate,
                schema_path=SCHEMA_PATH,
                expected_daily_date="2026-08-07",
                previous_keys={
                    filename: profile.keys
                    for filename, profile in previous.files.items()
                    if filename != "factor_catalog.csv"
                },
                continuity_cutoffs={
                    "daily_output.csv": "2026-08-07",
                    "weekly_output.csv": "202632",
                    "monthly_output.csv": "202608",
                    "api_wind_date.csv": "2026-08-07",
                },
            )

    def test_v2_daily_gate_v1_keeps_stable_audit_shape(self) -> None:
        from scheduler.v2_daily_gate import write_gate_record

        with tempfile.TemporaryDirectory() as tmpdir:
            record = write_gate_record(
                SimpleNamespace(runtime_root=Path(tmpdir)),
                run_date="2026-07-24",
                status="ready",
                checked_at="2026-07-24T06:50:01+08:00",
                generation_id="full-20260724-065000-abcdef123456",
                refresh_date="2026-07-24",
                expected_daily_date="2026-07-23",
                business_digest="digest",
                checks=[{"name": "strict_current_read", "status": "passed"}],
            )

        self.assertEqual(record["schema_version"], "v2-scheduler-gate-v1")
        self.assertEqual(
            record["restart"],
            {"requested": False, "verified": False},
        )

    @staticmethod
    def _monthly_metadata_with_macro_additions() -> pd.DataFrame:
        rows: list[dict[str, object]] = [
            {
                "indicators_code": "MONTHLY_SELECTED_A",
                "frequency": "monthly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
            {
                "indicators_code": "MONTHLY_SELECTED_B",
                "frequency": "monthly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
        ]
        rows.extend(
            {
                "indicators_code": code,
                "frequency": "monthly",
                "status": 1,
                "pre_forecast_flag": 0,
                "lag_length": 2,
                "indicators_source": "raw",
            }
            for code in ("M0041340", "M0041341", "M0041342")
        )
        return pd.DataFrame(rows)

    @staticmethod
    def _monthly_macro_long_frame() -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for row_number, rdate in enumerate(
            ("2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"),
            start=1,
        ):
            rows.extend(
                {
                    "rdate": rdate,
                    "month_id": rdate[:7].replace("-", ""),
                    "indicators_code": code,
                    "indicators_value": float(row_number * 10 + column_number),
                }
                for column_number, code in enumerate(
                    (
                        "MONTHLY_SELECTED_A",
                        "MONTHLY_SELECTED_B",
                        "M0041340",
                        "M0041341",
                        "M0041342",
                    ),
                    start=1,
                )
            )
        return pd.DataFrame(rows)

    def test_monthly_databridge_appends_stale_flag_raw_macro_additions_in_source_order(
        self,
    ) -> None:
        from shared.data_service import build_monthly_output_from_frames

        output = build_monthly_output_from_frames(
            self._monthly_metadata_with_macro_additions(),
            self._monthly_macro_long_frame(),
            end_date="2026-03-01",
            include_databridge_additions=True,
        )

        self.assertEqual(
            output.columns.tolist(),
            [
                "month_id",
                "MONTHLY_SELECTED_A",
                "MONTHLY_SELECTED_B",
                "M0041340",
                "M0041341",
                "M0041342",
            ],
        )
        self.assertEqual(output["month_id"].tolist(), ["202601", "202602", "202603"])
        for column_number, code in enumerate(
            ("M0041340", "M0041341", "M0041342"),
            start=3,
        ):
            self.assertTrue(pd.isna(output.loc[0, code]))
            self.assertTrue(pd.isna(output.loc[1, code]))
            self.assertEqual(output.loc[2, code], float(10 + column_number))


    def test_monthly_databridge_rejects_unusable_macro_addition_metadata(
        self,
    ) -> None:
        from shared.data_service import (
            build_monthly_cutoff_index_from_frames,
            build_monthly_output_from_frames,
        )

        valid_metadata = self._monthly_metadata_with_macro_additions()
        cases: tuple[tuple[str, pd.DataFrame, str], ...] = (
            (
                "missing",
                valid_metadata[valid_metadata["indicators_code"].ne("M0041340")],
                "M0041340",
            ),
            (
                "non_monthly",
                valid_metadata.assign(
                    frequency=lambda frame: frame["frequency"].mask(
                        frame["indicators_code"].eq("M0041341"),
                        "weekly",
                    )
                ),
                "M0041341",
            ),
            (
                "non_raw",
                valid_metadata.assign(
                    indicators_source=lambda frame: frame["indicators_source"].mask(
                        frame["indicators_code"].eq("M0041342"),
                        "derivative",
                    )
                ),
                "M0041342",
            ),
            (
                "inactive",
                valid_metadata.assign(
                    status=lambda frame: frame["status"].mask(
                        frame["indicators_code"].eq("M0041340"),
                        0,
                    )
                ),
                "M0041340",
            ),
            (
                "missing_status",
                valid_metadata.drop(columns=["status"]),
                "M0041341",
            ),
            (
                "unrecognized_source",
                valid_metadata.assign(
                    indicators_source=lambda frame: frame[
                        "indicators_source"
                    ].mask(
                        frame["indicators_code"].eq("M0041342"),
                        "external",
                    )
                ),
                "M0041342",
            ),
        )
        raw = self._monthly_macro_long_frame()

        for case_name, metadata, code in cases:
            with self.subTest(case=case_name, builder="output"):
                with self.assertRaisesRegex(ValueError, code):
                    build_monthly_output_from_frames(
                        metadata,
                        raw,
                        end_date="2026-03-01",
                        include_databridge_additions=True,
                    )
            with self.subTest(case=case_name, builder="cutoff_index"):
                with self.assertRaisesRegex(ValueError, code):
                    build_monthly_cutoff_index_from_frames(
                        metadata,
                        raw,
                        end_date="2026-03-01",
                        include_databridge_additions=True,
                    )

    def test_monthly_databridge_db_builder_reads_macro_additions_with_shared_selection(
        self,
    ) -> None:
        from shared import data_service

        metadata = self._monthly_metadata_with_macro_additions()
        raw = self._monthly_macro_long_frame()
        with (
            patch.object(
                data_service,
                "read_factor_metadata_from_db",
                return_value=metadata,
            ),
            patch.object(
                data_service,
                "read_monthly_long_from_db",
                side_effect=[raw, pd.DataFrame()],
            ) as read_monthly,
        ):
            output = data_service.build_monthly_output_from_db(
                end_date="2026-03-01",
                engine=object(),
                include_databridge_additions=True,
            )
        expected_codes = [
            "MONTHLY_SELECTED_A",
            "MONTHLY_SELECTED_B",
            "M0041340",
            "M0041341",
            "M0041342",
        ]
        self.assertEqual(output.columns.tolist(), ["month_id", *expected_codes])
        self.assertEqual(list(read_monthly.call_args_list[0].args[0]), expected_codes)
        self.assertEqual(list(read_monthly.call_args_list[1].args[0]), expected_codes)


    def test_local_mysql_databridge_export_enables_monthly_macro_additions(
        self,
    ) -> None:
        from shared.data_bridge import mysql_exporter

        connection = MagicMock()
        connection.__enter__.return_value = connection
        engine = MagicMock()
        engine.connect.return_value = connection

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(
                mysql_exporter,
                "read_factor_metadata_from_db",
                return_value=pd.DataFrame(
                    {
                        "indicators_code": ["D", "W", "M"],
                        "factor_version": ["V1.0", "V1.0", "V1.0"],
                    }
                ),
            ) as metadata_reader,
            patch.object(
                mysql_exporter,
                "build_daily_output_from_db",
                return_value=pd.DataFrame(columns=["date", "D"]),
            ),
            patch.object(
                mysql_exporter,
                "build_weekly_output_from_db",
                return_value=pd.DataFrame(columns=["week_id", "W"]),
            ),
            patch.object(
                mysql_exporter,
                "build_monthly_output_from_db",
                return_value=pd.DataFrame(columns=["month_id", "M"]),
            ) as monthly_builder,
            patch.object(
                mysql_exporter,
                "capture_source_commit_evidence_from_connection",
                return_value=object(),
            ) as evidence_reader,
            patch.object(mysql_exporter, "assert_source_commit_evidence_at_cutoff"),
            patch.object(
                mysql_exporter,
                "validate_dataset",
                return_value=SimpleNamespace(business_digest="digest"),
            ),
            patch.object(mysql_exporter, "write_validated_dataset"),
            patch.object(mysql_exporter, "_source_provenance", return_value={}),
        ):
            builder = mysql_exporter.MySqlDataBridgeRoundBuilder(
                engine=engine,
                config=SimpleNamespace(
                    daily_start_date="2025-01-01",
                    round_timeout_sec=900,
                    runtime_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                ),
            )
            builder.build(
                "round-1",
                expected_daily_date="2026-03-01",
                previous_keys=None,
            )

        monthly_builder.assert_called_once()
        monthly_kwargs = dict(monthly_builder.call_args.kwargs)
        shared_metadata = monthly_kwargs.pop("metadata")
        self.assertEqual(
            monthly_kwargs,
            {
                "start_date": "2025-01-01",
                "end_date": "2026-03-01",
                "engine": connection,
                "include_databridge_additions": True,
            },
        )
        self.assertEqual(
            shared_metadata["indicators_code"].tolist(),
            ["D", "W", "M"],
        )
        metadata_reader.assert_called_once_with(connection)
        self.assertIs(
            evidence_reader.call_args.kwargs["metadata"],
            shared_metadata,
        )


    def test_blackbox_bulk_cutoff_resolution_uses_monthly_macro_selection(
        self,
    ) -> None:
        from shared import input_artifacts

        metadata = self._monthly_metadata_with_macro_additions()
        monthly_raw = self._monthly_macro_long_frame()
        monthly_derivative = pd.DataFrame()
        period = input_artifacts._ResolvedPeriodCutoff(
            source_key="202603",
            effective_key="202603",
        )
        with (
            patch.object(
                input_artifacts,
                "_load_blackbox_schema",
                return_value=(
                    "data-bridge-v1",
                    {
                        "weekly_output.csv": ["week_id", "WEEKLY_A"],
                        "monthly_output.csv": ["month_id"],
                    },
                ),
            ),
            patch.object(
                input_artifacts._data_service,
                "read_factor_metadata_from_db",
                return_value=metadata,
            ),
            patch.object(
                input_artifacts._data_service,
                "read_weekly_long_from_db",
                return_value=pd.DataFrame(),
            ),
            patch.object(
                input_artifacts._data_service,
                "read_monthly_long_from_db",
                side_effect=[monthly_raw, monthly_derivative],
            ) as read_monthly,
            patch.object(
                input_artifacts._data_service,
                "select_monthly_factor_metadata",
                wraps=input_artifacts._data_service.select_monthly_factor_metadata,
            ) as select_monthly,
            patch.object(
                input_artifacts._data_service,
                "build_weekly_cutoff_index_from_frames",
                return_value=pd.DataFrame(),
            ),
            patch.object(
                input_artifacts._data_service,
                "build_monthly_cutoff_index_from_frames",
                return_value=pd.DataFrame(),
            ) as monthly_index,
            patch.object(
                input_artifacts,
                "_resolve_period_cutoffs_bulk",
                return_value={"2026-03-01": period},
            ),
        ):
            result = input_artifacts._resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
                {
                    "date": ["2026-03-01"],
                    "week_id": {"202609"},
                    "month_id": {"202603"},
                },
                feature_dates=["2026-03-01"],
                connection=object(),
            )

        self.assertEqual(result["2026-03-01"].cutoff_keys.monthly_cutoff_key, "202603")
        select_monthly.assert_called_once_with(
            metadata,
            include_databridge_additions=True,
        )
        expected_codes = [
            "MONTHLY_SELECTED_A",
            "MONTHLY_SELECTED_B",
            "M0041340",
            "M0041341",
            "M0041342",
        ]
        self.assertEqual(list(read_monthly.call_args_list[0].args[0]), expected_codes)
        self.assertEqual(list(read_monthly.call_args_list[1].args[0]), expected_codes)
        monthly_index.assert_called_once()
        monthly_index_args, monthly_index_kwargs = monthly_index.call_args
        self.assertIs(monthly_index_args[0], metadata)
        self.assertIs(monthly_index_args[1], monthly_raw)
        self.assertIs(monthly_index_args[2], monthly_derivative)
        self.assertEqual(
            monthly_index_kwargs,
            {
                "end_date": "2026-03-01",
                "include_databridge_additions": True,
            },
        )

    def test_producer_rejects_monthly_output_without_macro_additions(
        self,
    ) -> None:
        from shared import input_artifacts

        frames = _current_dataset().dataset.frames
        frames = {name: frame.copy() for name, frame in frames.items()}
        missing = {"M0041340", "M0041341", "M0041342"}
        frames["monthly_output.csv"] = frames["monthly_output.csv"].drop(
            columns=sorted(missing)
        )
        frames["factor_catalog.csv"] = frames["factor_catalog.csv"][
            ~frames["factor_catalog.csv"]["indicators_code"].isin(missing)
        ]
        with self.assertRaisesRegex(
            ValueError,
            "M0041340.*M0041341.*M0041342",
        ):
            input_artifacts._require_blackbox_databridge_monthly_additions(
                frames
            )

    def test_cross_week_producer_resolves_and_publishes_new_week(self) -> None:
        """周一首刷必须贯通真实 cutoff、refresh 与五文件 publish。"""
        from scripts import refresh_data_bridge_current as refresh_script
        from shared.data_bridge import mysql_exporter
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=SCHEMA_PATH,
            )
            config.data_root.mkdir()
            config.runtime_root.mkdir()
            config.data_root.chmod(0o700)
            config.runtime_root.chmod(0o700)

            _StaticDataBridgeRoundBuilder.staging_root = root / "old"
            _StaticDataBridgeRoundBuilder.frames = _bridge_frames(
                include_new_week=False
            )
            run_full_refresh(
                config=config,
                expected_daily_date="2026-07-17",
                refresh_date="2026-07-18",
                publish=True,
                round_builder=_StaticDataBridgeRoundBuilder(),
            )

            _StaticDataBridgeRoundBuilder.staging_root = root / "new"
            _StaticDataBridgeRoundBuilder.frames = _bridge_frames(
                include_new_week=True
            )
            connection = MagicMock()
            connection.exec_driver_sql.return_value = None
            engine = MagicMock()
            engine.connect.return_value.__enter__.return_value = connection
            engine.connect.return_value.__exit__.return_value = False
            with (
                _cross_week_cutoff_source(),
                patch.dict(
                    os.environ,
                    {
                        refresh_script.LAUNCHD_PUBLISHER_ENV: (
                            refresh_script.LAUNCHD_PUBLISHER_VALUE
                        )
                    },
                ),
                patch.object(
                    refresh_script,
                    "create_sqlalchemy_engine",
                    return_value=engine,
                ),
                patch.object(
                    refresh_script,
                    "MySqlDataBridgeRoundBuilder",
                    _StaticDataBridgeRoundBuilder,
                ),
                patch.object(
                    mysql_exporter,
                    "MySqlDataBridgeRoundBuilder",
                    _StaticDataBridgeRoundBuilder,
                ),
                patch.object(
                    refresh_script,
                    "invalidate_ready_blackbox_snapshot",
                ),
                patch.object(
                    refresh_script,
                    "prepare_blackbox_generation_snapshot",
                ),
            ):
                result = refresh_script.refresh_current(
                    refresh_date="2026-07-21",
                    expected_feature_date="2026-07-20",
                    publish=True,
                    config=config,
                )

            self.assertTrue(result.published)
            self.assertEqual(
                result.state["files"]["weekly_output.csv"]["max_key"],
                "202630",
            )
            published = pd.read_csv(
                config.data_root / "current" / "weekly_output.csv"
            )
            self.assertIn(202630, published["week_id"].tolist())

    def test_refresh_rechecks_deadline_at_publication_commit(self) -> None:
        """candidate copy/fsync 期间越过 deadline 时不得切换 current。"""
        from shared.data_bridge import refresh as refresh_module
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=SCHEMA_PATH,
            )
            config.data_root.mkdir()
            config.runtime_root.mkdir()
            config.data_root.chmod(0o700)
            config.runtime_root.chmod(0o700)
            _StaticDataBridgeRoundBuilder.staging_root = root / "staging"
            _StaticDataBridgeRoundBuilder.frames = _bridge_frames(
                include_new_week=True
            )
            checks = 0

            def deadline_fence(_deadline) -> None:
                nonlocal checks
                checks += 1
                if checks == 8:
                    raise DataBridgeRefreshError(
                        "DataBridge refresh deadline has passed"
                    )

            with (
                patch.object(
                    refresh_module,
                    "_ensure_before_deadline",
                    side_effect=deadline_fence,
                ),
                self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    "deadline has passed",
                ),
            ):
                run_full_refresh(
                    config=config,
                    expected_daily_date="2026-07-20",
                    refresh_date="2026-07-21",
                    publish=True,
                    deadline_at=datetime(
                        2026,
                        7,
                        21,
                        7,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    round_builder=_StaticDataBridgeRoundBuilder(),
                )

            self.assertEqual(checks, 8)
            self.assertFalse((config.data_root / "current").exists())

    def test_first_publication_fence_preserves_existing_current(self) -> None:
        """copy/fsync 后首个 fence 失败不得删除最后成功 generation。"""
        from shared.data_bridge import refresh as refresh_module
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DataBridgeStore,
            _build_state,
        )
        from shared.data_bridge.validation import validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            runtime_root = root / "runtime"
            data_root.mkdir(mode=0o700)
            runtime_root.mkdir(mode=0o700)
            store = DataBridgeStore(
                data_root=data_root,
                runtime_root=runtime_root,
            )
            frames = _bridge_frames(include_new_week=False)
            dataset = validate_dataset(frames, schema_path=SCHEMA_PATH)

            def candidate(name: str) -> Path:
                directory = root / name
                directory.mkdir()
                for filename, frame in frames.items():
                    frame.to_csv(directory / filename, index=False)
                return directory

            initial_state = _build_state(
                dataset,
                "2026-07-20",
                2,
                refresh_started_at=datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ),
                duration_sec=1.0,
            )
            store.publish(candidate("initial"), initial_state)
            current_before = {
                path.name: path.read_bytes()
                for path in store.current_dir.iterdir()
            }
            state_before = store.state_path.read_bytes()

            with (
                patch.object(
                    refresh_module,
                    "_ensure_before_deadline",
                    side_effect=DataBridgeRefreshError(
                        "DataBridge refresh deadline has passed"
                    ),
                ),
                self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    "deadline has passed",
                ),
            ):
                store.publish(
                    candidate("next"),
                    initial_state,
                    deadline_at=datetime(
                        2026,
                        7,
                        21,
                        7,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                )

            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in store.current_dir.iterdir()
                },
                current_before,
            )
            self.assertEqual(store.state_path.read_bytes(), state_before)
            self.assertFalse(store.previous_dir.exists())

    def test_legacy_four_file_current_upgrades_atomically(self) -> None:
        """升级前四文件 current 必须一次替换为完整五文件。"""
        from shared.data_bridge.refresh import (
            DataBridgeContinuityAuthority,
            DataBridgeRefreshConfig,
            DataBridgeStore,
            _build_state,
            _write_publication_manifest,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
            run_full_refresh,
        )
        from shared.data_bridge.validation import (
            validate_legacy_four_file_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=SCHEMA_PATH,
            )
            config.data_root.mkdir()
            config.runtime_root.mkdir()
            config.data_root.chmod(0o700)
            config.runtime_root.chmod(0o700)
            legacy_frames = {
                name: frame
                for name, frame in _bridge_frames(
                    include_new_week=False
                ).items()
                if name != "factor_catalog.csv"
            }
            legacy = validate_legacy_four_file_dataset(
                legacy_frames,
                schema_path=SCHEMA_PATH,
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.current_dir.mkdir()
            for filename, frame in legacy.frames.items():
                frame.to_csv(store.current_dir / filename, index=False)
            state = _build_state(
                legacy,
                "2026-07-18",
                2,
                refresh_started_at=(
                    datetime.now(ZoneInfo("Asia/Shanghai"))
                    - timedelta(seconds=1)
                ),
                duration_sec=1.0,
            )
            _write_publication_manifest(store.current_dir, state=state)
            store.state_path.write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            publication_identity = data_bridge_publication_identity_sha256(
                state
            )
            authority = DataBridgeContinuityAuthority(
                generation_id=str(state["generation_id"]),
                business_digest=legacy.business_digest,
                publication_identity_sha256=publication_identity,
                stable_identity_sha256=(
                    data_bridge_continuity_authority_sha256(
                        generation_id=str(state["generation_id"]),
                        business_digest=legacy.business_digest,
                        publication_identity_sha256=publication_identity,
                        daily_cutoff_key="2026-07-17",
                        weekly_cutoff_key="202629",
                        monthly_cutoff_key="202607",
                    )
                ),
                daily_cutoff_key="2026-07-17",
                weekly_cutoff_key="202629",
                monthly_cutoff_key="202607",
            )
            _StaticDataBridgeRoundBuilder.staging_root = root / "upgrade"
            _StaticDataBridgeRoundBuilder.frames = _bridge_frames(
                include_new_week=True
            )
            result = run_full_refresh(
                config=config,
                expected_daily_date="2026-07-20",
                refresh_date="2026-07-21",
                publish=True,
                continuity_authority=authority,
                round_builder=_StaticDataBridgeRoundBuilder(),
            )

            self.assertTrue(result.published)
            self.assertEqual(
                {path.name for path in store.current_dir.iterdir()},
                {
                    "daily_output.csv",
                    "weekly_output.csv",
                    "monthly_output.csv",
                    "api_wind_date.csv",
                    "factor_catalog.csv",
                    ".publication-manifest.json",
                },
            )


    def test_only_v3_producer_enables_period_bootstrap(self) -> None:
        from shared import input_artifacts
        from shared.data_bridge import authority

        current, resolved, config = _v3_period_bootstrap_inputs()

        with (
            patch.object(
                authority,
                "check_current_dataset",
                return_value=current,
            ),
            patch.object(
                input_artifacts,
                "_resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys",
                return_value={"2026-08-10": resolved},
            ) as resolve_cutoffs,
        ):
            result = authority.resolve_databridge_continuity_authority(
                config,
                feature_date="2026-08-10",
                connection=object(),
                allow_producer_period_bootstrap=True,
            )
            authority.resolve_stable_databridge_current_authority(
                config,
                feature_dates=("2026-08-10",),
                connection=object(),
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.weekly_cutoff_key, "202630")
        self.assertEqual(result.required_weekly_key, "202631")
        self.assertEqual(
            [
                call.kwargs["allow_legacy_v1_period_fallback"]
                for call in resolve_cutoffs.call_args_list
            ],
            [True, False],
        )

    def test_launchd_publisher_enables_producer_period_bootstrap(self) -> None:
        """只有 launchd producer 可请求跨周首键 bootstrap。"""
        from scripts import refresh_data_bridge_current as refresh_script

        engine = MagicMock()
        config = SimpleNamespace(
            schema_path=Path("/tmp/schema.json"),
            data_root=Path("/tmp/data"),
            runtime_root=Path("/tmp/runtime"),
        )
        result = SimpleNamespace(
            published=True,
            state={"generation_id": "generation-test"},
            dataset=object(),
        )
        with (
            patch.dict(
                os.environ,
                {
                    refresh_script.LAUNCHD_PUBLISHER_ENV: (
                        refresh_script.LAUNCHD_PUBLISHER_VALUE
                    )
                },
                clear=False,
            ),
            patch.object(
                refresh_script,
                "create_sqlalchemy_engine",
                return_value=engine,
            ),
            patch.object(
                refresh_script,
                "resolve_databridge_continuity_authority_from_engine",
                return_value=object(),
            ) as resolve_authority,
            patch.object(refresh_script, "MySqlDataBridgeRoundBuilder"),
            patch.object(
                refresh_script,
                "run_full_refresh",
                return_value=result,
            ),
            patch.object(
                refresh_script,
                "invalidate_ready_blackbox_snapshot",
            ) as invalidate_snapshot,
            patch.object(
                refresh_script,
                "prepare_blackbox_generation_snapshot",
            ) as prepare_snapshot,
        ):
            actual = refresh_script.refresh_current(
                refresh_date="2026-08-11",
                expected_feature_date="2026-08-10",
                publish=True,
                config=config,
            )

        self.assertIs(actual, result)
        invalidate_snapshot.assert_called_once_with()
        self.assertTrue(
            resolve_authority.call_args.kwargs[
                "allow_producer_period_bootstrap"
            ]
        )
        prepare_snapshot.assert_called_once_with(
            state=result.state,
            dataset=result.dataset,
            schema_path=config.schema_path,
        )
