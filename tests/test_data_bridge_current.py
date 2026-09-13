from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
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

    def test_publication_deadline_fences_preserve_existing_current(self) -> None:
        """备份 current 前后超时，都必须保留最后成功的 generation 和 state。"""
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

            def candidate(name: str, source_frames: dict[str, pd.DataFrame]) -> Path:
                directory = root / name
                directory.mkdir()
                for filename, frame in source_frames.items():
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
            store.publish(candidate("initial", frames), initial_state)
            current_before = {
                path.name: path.read_bytes()
                for path in store.current_dir.iterdir()
            }
            state_before = store.state_path.read_bytes()
            next_frames = _bridge_frames(include_new_week=True)
            next_state = _build_state(
                validate_dataset(next_frames, schema_path=SCHEMA_PATH),
                "2026-07-21",
                2,
                refresh_started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                duration_sec=1.0,
            )

            for after_backup in (False, True):
                def deadline_fence(_deadline) -> None:
                    if not after_backup or store.previous_dir.exists():
                        raise DataBridgeRefreshError(
                            "DataBridge refresh deadline has passed"
                        )

                with self.subTest(after_backup=after_backup):
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
                        store.publish(
                            candidate(f"next-{after_backup}", next_frames),
                            next_state,
                            deadline_at=datetime(
                                2026, 7, 21, 7, 0,
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
