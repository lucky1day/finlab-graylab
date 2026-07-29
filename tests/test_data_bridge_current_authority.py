from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pandas as pd

from shared.blackbox_v2.snapshot import CutoffKeys
from shared.data_bridge.refresh import (
    CurrentDataset,
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeRefreshConfig,
)
from shared.data_bridge.validation import (
    DataBridgeFileProfile,
    ValidatedDataBridgeDataset,
)


def _profile(
    filename: str,
    *,
    sha256: str,
    business_hash: str,
) -> DataBridgeFileProfile:
    keys = {
        "daily_output.csv": ("2026-07-14", "2026-07-15"),
        "weekly_output.csv": ("202627", "202628"),
        "monthly_output.csv": ("202606", "202607"),
    }[filename]
    return DataBridgeFileProfile(
        filename=filename,
        rows=2,
        columns=3,
        min_key=keys[0],
        max_key=keys[-1],
        sha256=sha256,
        business_hash=business_hash,
        keys=frozenset(keys),
    )


def _current(
    *,
    generation_id: str = "generation-1",
    business_digest: str = "d" * 64,
    file_order: tuple[str, ...] = (
        "daily_output.csv",
        "weekly_output.csv",
        "monthly_output.csv",
    ),
    daily_sha256: str = "1" * 64,
    occurrence_id: int = 17,
    published_at: str = "2026-07-15T06:40:00+08:00",
) -> CurrentDataset:
    profiles = {
        "daily_output.csv": _profile(
            "daily_output.csv",
            sha256=daily_sha256,
            business_hash="4" * 64,
        ),
        "weekly_output.csv": _profile(
            "weekly_output.csv",
            sha256="2" * 64,
            business_hash="5" * 64,
        ),
        "monthly_output.csv": _profile(
            "monthly_output.csv",
            sha256="3" * 64,
            business_hash="6" * 64,
        ),
    }
    return CurrentDataset(
        state={
            "generation_id": generation_id,
            "refresh_date": "2026-07-15",
            "schema_version": "data-bridge-v1",
            "business_digest": business_digest,
            "publication_capability": {
                "occurrence_id": occurrence_id,
                "business_date": "2026-07-15",
                "daily_coordinator_epoch": {
                    "epoch": 3,
                    "mode": "ledger",
                    "record_sha256": "7" * 64,
                },
            },
            "published_at": published_at,
            "refreshed_at": "2026-07-15T06:39:00+08:00",
            "last_attempt": {
                "finished_at": "2026-07-15T06:39:00+08:00",
            },
        },
        dataset=ValidatedDataBridgeDataset(
            schema_version="data-bridge-v1",
            frames={},
            files={
                filename: profiles[filename]
                for filename in file_order
            },
            business_digest=business_digest,
        ),
    )


def _cutoffs(
    *,
    weekly: str = "202628",
) -> dict[str, CutoffKeys]:
    return {
        "2026-07-14": CutoffKeys(
            daily_cutoff_key="2026-07-14",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        ),
        "2026-07-15": CutoffKeys(
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key=weekly,
            monthly_cutoff_key="202607",
        ),
    }


class StableDataBridgeCurrentAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DataBridgeRefreshConfig(
            data_root=Path("/nonexistent/data-bridge"),
            runtime_root=Path("/nonexistent/data-bridge-runtime"),
            schema_path=Path("/nonexistent/schema.json"),
        )
        self.connection = object()

    def _resolve(
        self,
        current: CurrentDataset,
        *,
        feature_dates=("2026-07-15", "2026-07-14"),
        cutoffs=None,
    ):
        from shared.data_bridge.authority import (
            resolve_stable_databridge_current_authority,
        )

        with (
            patch(
                "shared.data_bridge.authority.check_current_dataset",
                return_value=current,
            ) as check,
            patch(
                "shared.data_bridge.authority."
                "_resolve_blackbox_input_cutoffs_bulk_from_keys",
                return_value=cutoffs or _cutoffs(),
            ) as resolve_cutoffs,
            patch(
                "shared.input_artifacts.create_snapshot_from_frames",
                side_effect=AssertionError("must not materialize snapshot"),
            ),
            patch(
                "shared.input_artifacts.tempfile.mkdtemp",
                side_effect=AssertionError("must not create temp input"),
            ),
        ):
            authority = resolve_stable_databridge_current_authority(
                self.config,
                feature_dates=feature_dates,
                connection=self.connection,
            )
        return authority, check, resolve_cutoffs

    def test_returns_immutable_stable_current_identity_and_exact_cutoffs(
        self,
    ) -> None:
        authority, check, resolve_cutoffs = self._resolve(_current())

        self.assertEqual(check.call_count, 1)
        self.assertEqual(authority.generation_id, "generation-1")
        self.assertEqual(authority.refresh_date, "2026-07-15")
        self.assertEqual(authority.schema_version, "data-bridge-v1")
        self.assertEqual(authority.business_digest, "d" * 64)
        self.assertRegex(authority.stable_identity_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            [item.filename for item in authority.files],
            [
                "daily_output.csv",
                "monthly_output.csv",
                "weekly_output.csv",
            ],
        )
        self.assertEqual(
            [item.feature_date for item in authority.cutoffs],
            ["2026-07-14", "2026-07-15"],
        )
        self.assertEqual(
            authority.cutoffs[-1].weekly_cutoff_key,
            "202628",
        )
        self.assertEqual(
            authority.publication_capability.occurrence_id,
            17,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            authority.generation_id = "replacement"
        resolve_cutoffs.assert_called_once()
        self.assertIs(
            resolve_cutoffs.call_args.kwargs["connection"],
            self.connection,
        )
        self.assertEqual(
            resolve_cutoffs.call_args.kwargs["feature_dates"],
            ("2026-07-14", "2026-07-15"),
        )

    def test_input_order_and_mutable_timestamps_do_not_change_identity(
        self,
    ) -> None:
        first, _, _ = self._resolve(
            _current(),
            feature_dates=("2026-07-15", "2026-07-14"),
        )
        second, _, _ = self._resolve(
            _current(
                file_order=(
                    "monthly_output.csv",
                    "daily_output.csv",
                    "weekly_output.csv",
                ),
                published_at="2026-07-15T07:20:00+08:00",
            ),
            feature_dates=(
                "2026-07-14",
                "2026-07-15",
                "2026-07-14",
            ),
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.stable_identity_sha256,
            second.stable_identity_sha256,
        )

    def test_every_stable_identity_component_changes_digest(self) -> None:
        baseline, _, _ = self._resolve(_current())
        variants = {
            "generation": self._resolve(
                _current(generation_id="generation-2"),
            )[0],
            "business_digest": self._resolve(
                _current(business_digest="e" * 64),
            )[0],
            "file_hash": self._resolve(
                _current(daily_sha256="8" * 64),
            )[0],
            "publication_capability": self._resolve(
                _current(occurrence_id=18),
            )[0],
            "cutoff": self._resolve(
                _current(),
                cutoffs=_cutoffs(weekly="202627"),
            )[0],
        }

        for field, changed in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    baseline.stable_identity_sha256,
                    changed.stable_identity_sha256,
                )

    def test_missing_and_invalid_checker_types_are_propagated(self) -> None:
        from shared.data_bridge.authority import (
            resolve_stable_databridge_current_authority,
        )

        for error_type in (
            DataBridgeCurrentMissingError,
            DataBridgeCurrentInvalidError,
        ):
            with (
                self.subTest(error_type=error_type.__name__),
                patch(
                    "shared.data_bridge.authority.check_current_dataset",
                    side_effect=error_type("checker classification"),
                ),
                self.assertRaises(error_type),
            ):
                resolve_stable_databridge_current_authority(
                    self.config,
                    feature_dates=("2026-07-15",),
                    connection=self.connection,
                )

    def test_refresh_continuity_cutoffs_use_exact_current_authority(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            StableDataBridgeCutoff,
            resolve_databridge_continuity_cutoffs,
        )

        authority = SimpleNamespace(
            cutoffs=(
                StableDataBridgeCutoff(
                    feature_date="2026-07-28",
                    daily_cutoff_key="2026-07-27",
                    weekly_cutoff_key="202629",
                    monthly_cutoff_key="202607",
                ),
            )
        )
        with patch(
            "shared.data_bridge.authority."
            "resolve_stable_databridge_current_authority",
            return_value=authority,
        ) as resolve:
            actual = resolve_databridge_continuity_cutoffs(
                self.config,
                feature_date="2026-07-28",
                connection=self.connection,
            )

        self.assertEqual(
            actual,
            {
                "daily_output.csv": "2026-07-27",
                "weekly_output.csv": "202629",
                "monthly_output.csv": "202607",
            },
        )
        resolve.assert_called_once_with(
            self.config,
            feature_dates=("2026-07-28",),
            connection=self.connection,
        )

    def test_refresh_continuity_cutoffs_allow_only_missing_current(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_cutoffs,
        )

        for error_type, expected in (
            (DataBridgeCurrentMissingError, None),
            (DataBridgeCurrentInvalidError, "raise"),
        ):
            with (
                self.subTest(error_type=error_type.__name__),
                patch(
                    "shared.data_bridge.authority."
                    "resolve_stable_databridge_current_authority",
                    side_effect=error_type("current classification"),
                ),
            ):
                if expected is None:
                    self.assertIsNone(
                        resolve_databridge_continuity_cutoffs(
                            self.config,
                            feature_date="2026-07-28",
                            connection=self.connection,
                        )
                    )
                else:
                    with self.assertRaises(DataBridgeCurrentInvalidError):
                        resolve_databridge_continuity_cutoffs(
                            self.config,
                            feature_date="2026-07-28",
                            connection=self.connection,
                        )

    def test_cutoff_sql_helpers_receive_the_caller_connection(self) -> None:
        from shared.input_artifacts import (
            BLACKBOX_SCHEMA_PATH,
            _resolve_blackbox_input_cutoffs_bulk_from_keys,
        )

        connection = object()
        metadata = pd.DataFrame(
            {
                "indicators_code": ["monthly_factor"],
                "frequency": ["monthly"],
            }
        )
        weekly_index = pd.DataFrame(
            {
                "week_id": ["202628"],
                "available_date": ["2026-07-15"],
            }
        )
        monthly_index = pd.DataFrame(
            {
                "month_id": ["202607"],
                "available_date": ["2026-07-15"],
            }
        )
        with (
            patch(
                "shared.input_artifacts."
                "_data_service.read_factor_metadata_from_db",
                return_value=metadata,
            ) as read_metadata,
            patch(
                "shared.input_artifacts."
                "_data_service.read_weekly_long_from_db",
                return_value=pd.DataFrame(),
            ) as read_weekly,
            patch(
                "shared.input_artifacts."
                "_data_service.read_monthly_long_from_db",
                return_value=pd.DataFrame(),
            ) as read_monthly,
            patch(
                "shared.input_artifacts."
                "_data_service.select_factor_metadata",
                return_value=metadata,
            ),
            patch(
                "shared.input_artifacts."
                "_data_service.build_weekly_cutoff_index_from_frames",
                return_value=weekly_index,
            ),
            patch(
                "shared.input_artifacts."
                "_data_service.build_monthly_cutoff_index_from_frames",
                return_value=monthly_index,
            ),
        ):
            result = _resolve_blackbox_input_cutoffs_bulk_from_keys(
                {
                    "date": ["2026-07-15"],
                    "week_id": {"202628"},
                    "month_id": {"202607"},
                },
                feature_dates=("2026-07-15",),
                connection=connection,
                schema_path=BLACKBOX_SCHEMA_PATH,
            )

        self.assertEqual(result["2026-07-15"].daily_cutoff_key, "2026-07-15")
        read_metadata.assert_called_once_with(connection)
        self.assertEqual(
            read_weekly.call_args_list,
            [
                call(
                    unittest.mock.ANY,
                    "api_wind_weekly",
                    connection,
                ),
                call(
                    unittest.mock.ANY,
                    "api_wind_derivative_weekly",
                    connection,
                ),
            ],
        )
        self.assertEqual(
            read_monthly.call_args_list,
            [
                call(
                    unittest.mock.ANY,
                    "api_wind_monthly",
                    connection,
                ),
                call(
                    unittest.mock.ANY,
                    "api_wind_derivative_monthly",
                    connection,
                    include_month_id=True,
                ),
            ],
        )
