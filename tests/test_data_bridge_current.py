from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _current_dataset(
    *,
    upstream_generation_id: str = "full-20260724-065000-abcdef123456",
):
    from shared.data_bridge.refresh import CurrentDataset
    from shared.data_bridge.validation import validate_dataset

    frames = {
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
    }
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




class DataBridgeCurrentTests(unittest.TestCase):
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
            patch.object(mysql_exporter, "build_daily_output_from_db"),
            patch.object(mysql_exporter, "build_weekly_output_from_db"),
            patch.object(mysql_exporter, "build_monthly_output_from_db") as monthly_builder,
            patch.object(
                mysql_exporter,
                "capture_source_commit_evidence_from_connection",
                return_value=object(),
            ),
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

        monthly_builder.assert_called_once_with(
            start_date="2025-01-01",
            end_date="2026-03-01",
            engine=connection,
            include_databridge_additions=True,
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

    def test_blackbox_snapshot_rejects_current_monthly_output_without_macro_additions(
        self,
    ) -> None:
        from shared import input_artifacts

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            input_artifacts,
            "_load_current_data_bridge_dataset",
            return_value=_current_dataset(),
        ), self.assertRaisesRegex(ValueError, "M0041340.*M0041341.*M0041342"):
            input_artifacts.build_blackbox_input_snapshot(
                snapshot_date="2026-07-24",
                output_root=Path(tmpdir),
            )


    def test_v3_producer_bootstrap_freezes_new_week_for_candidate_validation(
        self,
    ) -> None:
        """跨周首日只允许 producer 用旧快照作连续性基线。"""
        from shared import input_artifacts
        from shared.blackbox_v2.snapshot import CutoffKeys
        from shared.data_bridge import authority
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

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.weekly_cutoff_key, "202630")
        self.assertEqual(result.required_weekly_key, "202631")
        self.assertTrue(
            resolve_cutoffs.call_args.kwargs[
                "allow_legacy_v1_period_fallback"
            ]
        )

    def test_v3_consumer_does_not_enable_producer_period_bootstrap(
        self,
    ) -> None:
        """消费者继续严格拒绝缺少当前周键的 v3 current。"""
        from shared import input_artifacts
        from shared.blackbox_v2.snapshot import CutoffKeys
        from shared.data_bridge import authority
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
            authority.resolve_stable_databridge_current_authority(
                config,
                feature_dates=("2026-08-10",),
                connection=object(),
            )

        self.assertFalse(
            resolve_cutoffs.call_args.kwargs[
                "allow_legacy_v1_period_fallback"
            ]
        )

    def test_launchd_publisher_enables_producer_period_bootstrap(self) -> None:
        """只有 launchd producer 可请求跨周首键 bootstrap。"""
        from scripts import refresh_data_bridge_current as refresh_script

        engine = MagicMock()
        config = SimpleNamespace()
        result = object()
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
        ):
            actual = refresh_script.refresh_current(
                refresh_date="2026-08-11",
                expected_feature_date="2026-08-10",
                publish=True,
                config=config,
            )

        self.assertIs(actual, result)
        self.assertTrue(
            resolve_authority.call_args.kwargs[
                "allow_producer_period_bootstrap"
            ]
        )
