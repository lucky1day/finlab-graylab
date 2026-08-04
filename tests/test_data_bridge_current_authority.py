from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

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


def _source_cutoffs(
    cutoffs: dict[str, CutoffKeys],
) -> dict[str, SimpleNamespace]:
    return {
        feature_date: SimpleNamespace(
            cutoff_keys=cutoff_keys,
            source_weekly_cutoff_key=cutoff_keys.weekly_cutoff_key,
            source_monthly_cutoff_key=cutoff_keys.monthly_cutoff_key,
        )
        for feature_date, cutoff_keys in cutoffs.items()
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
                "_resolve_blackbox_input_cutoffs_with_source_keys_"
                "bulk_from_keys",
                return_value=_source_cutoffs(cutoffs or _cutoffs()),
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

    def test_refresh_continuity_authority_binds_exact_current_identity(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            StableDataBridgeCutoff,
            resolve_databridge_continuity_authority,
        )
        from shared.data_bridge.refresh import (
            data_bridge_continuity_authority_sha256,
        )

        authority = SimpleNamespace(
            generation_id="generation-current",
            business_digest="a" * 64,
            publication_identity_sha256="b" * 64,
            stable_identity_sha256="c" * 64,
            cutoffs=(
                StableDataBridgeCutoff(
                    feature_date="2026-07-28",
                    daily_cutoff_key="2026-07-27",
                    weekly_cutoff_key="202629",
                    monthly_cutoff_key="202607",
                    source_weekly_cutoff_key="202630",
                    source_monthly_cutoff_key="202607",
                ),
            )
        )
        with patch(
            "shared.data_bridge.authority."
            "resolve_stable_databridge_current_authority",
            return_value=authority,
        ) as resolve:
            actual = resolve_databridge_continuity_authority(
                self.config,
                feature_date="2026-07-28",
                connection=self.connection,
            )

        self.assertEqual(actual.generation_id, "generation-current")
        self.assertEqual(actual.business_digest, "a" * 64)
        self.assertEqual(
            actual.publication_identity_sha256,
            "b" * 64,
        )
        self.assertEqual(
            actual.stable_identity_sha256,
            data_bridge_continuity_authority_sha256(
                generation_id="generation-current",
                business_digest="a" * 64,
                publication_identity_sha256="b" * 64,
                daily_cutoff_key="2026-07-27",
                weekly_cutoff_key="202629",
                monthly_cutoff_key="202607",
                required_weekly_key="202630",
            ),
        )
        self.assertEqual(actual.required_weekly_key, "202630")
        self.assertIsNone(actual.required_monthly_key)
        self.assertEqual(
            actual.continuity_cutoffs,
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

    def test_refresh_continuity_authority_allows_only_missing_current(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_authority,
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
                        resolve_databridge_continuity_authority(
                            self.config,
                            feature_date="2026-07-28",
                            connection=self.connection,
                        )
                    )
                else:
                    with self.assertRaises(DataBridgeCurrentInvalidError):
                        resolve_databridge_continuity_authority(
                            self.config,
                            feature_date="2026-07-28",
                            connection=self.connection,
                        )

    def test_refresh_authority_engine_wrapper_uses_one_read_only_snapshot(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_authority_from_engine,
        )

        connection = Mock()
        connection_context = Mock()
        connection_context.__enter__ = Mock(
            return_value=connection
        )
        connection_context.__exit__ = Mock(return_value=False)
        engine = Mock()
        engine.connect.return_value = connection_context
        expected = object()
        with (
            patch("shared.data_bridge.authority.DataBridgeStore"),
            patch(
                "shared.data_bridge.authority."
                "resolve_databridge_continuity_authority",
                return_value=expected,
            ) as resolve,
        ):
            actual = (
                resolve_databridge_continuity_authority_from_engine(
                    self.config,
                    feature_date="2026-07-28",
                    engine=engine,
                )
            )

        self.assertIs(actual, expected)
        self.assertEqual(
            connection.exec_driver_sql.call_args_list,
            [
                call(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                ),
                call(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, "
                    "READ ONLY"
                ),
            ],
        )
        resolve.assert_called_once_with(
            self.config,
            feature_date="2026-07-28",
            connection=connection,
        )
        connection.rollback.assert_called_once_with()

    def test_refresh_authority_recovers_current_before_strict_resolution(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_authority_from_engine,
        )

        events: list[str] = []
        store = Mock()
        store.recover.side_effect = lambda **_kwargs: events.append(
            "recover"
        )
        connection = Mock()
        connection_context = Mock()
        connection_context.__enter__ = Mock(
            side_effect=lambda: (
                events.append("connect")
                or connection
            )
        )
        connection_context.__exit__ = Mock(return_value=False)
        engine = Mock()
        engine.connect.return_value = connection_context
        with (
            patch(
                "shared.data_bridge.authority.DataBridgeStore",
                return_value=store,
                create=True,
            ),
            patch(
                "shared.data_bridge.authority."
                "resolve_databridge_continuity_authority",
                return_value=None,
            ),
        ):
            resolve_databridge_continuity_authority_from_engine(
                self.config,
                feature_date="2026-07-28",
                engine=engine,
            )

        self.assertEqual(events[:2], ["recover", "connect"])
        store.recover.assert_called_once_with(
            schema_path=self.config.schema_path,
        )

    def test_authority_treats_verified_no_current_failure_audit_as_missing(
        self,
    ) -> None:
        """首次 publish 失败留下的 audit-only state 允许下一轮无连续性基线。"""
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_authority_from_engine,
        )
        from shared.data_bridge.refresh import DataBridgeStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=root / "schema.json",
            )
            DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            ).record_failed_attempt(
                refresh_date="2026-07-29",
                error="source_io_failed",
                duration_sec=1.0,
            )
            connection = Mock()
            connection_context = Mock()
            connection_context.__enter__ = Mock(return_value=connection)
            connection_context.__exit__ = Mock(return_value=False)
            engine = Mock()
            engine.connect.return_value = connection_context

            actual = resolve_databridge_continuity_authority_from_engine(
                config,
                feature_date="2026-07-28",
                engine=engine,
            )

        self.assertIsNone(actual)
        self.assertEqual(
            connection.exec_driver_sql.call_args_list,
            [
                call("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"),
                call(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, "
                    "READ ONLY"
                ),
            ],
        )
        connection.rollback.assert_called_once_with()

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

    def test_legacy_v1_period_fallback_is_opt_in_and_never_advances(
        self,
    ) -> None:
        """仅受控 legacy 路径可向后选旧快照周/月 cutoff。"""
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
                "week_id": ["202629", "202630"],
                "available_date": ["2026-07-27", "2026-08-03"],
            }
        )
        monthly_index = pd.DataFrame(
            {
                "month_id": ["202607", "202608"],
                "available_date": ["2026-06-22", "2026-07-20"],
            }
        )
        current_keys = {
            "date": ["2026-07-31"],
            "week_id": {"202629"},
            "month_id": {"202608"},
        }
        with (
            patch(
                "shared.input_artifacts."
                "_data_service.read_factor_metadata_from_db",
                return_value=metadata,
            ),
            patch(
                "shared.input_artifacts."
                "_data_service.read_weekly_long_from_db",
                return_value=pd.DataFrame(),
            ),
            patch(
                "shared.input_artifacts."
                "_data_service.read_monthly_long_from_db",
                return_value=pd.DataFrame(),
            ),
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
            with self.assertRaisesRegex(
                ValueError,
                "platform week_id cutoff 202630 does not exist",
            ):
                _resolve_blackbox_input_cutoffs_bulk_from_keys(
                    current_keys,
                    feature_dates=("2026-08-03",),
                    connection=connection,
                    schema_path=BLACKBOX_SCHEMA_PATH,
                )

            resolved = _resolve_blackbox_input_cutoffs_bulk_from_keys(
                current_keys,
                feature_dates=("2026-08-03",),
                connection=connection,
                schema_path=BLACKBOX_SCHEMA_PATH,
                allow_legacy_v1_period_fallback=True,
            )
            self.assertEqual(
                resolved["2026-08-03"],
                CutoffKeys(
                    daily_cutoff_key="2026-07-31",
                    weekly_cutoff_key="202629",
                    monthly_cutoff_key="202608",
                ),
            )

            with self.assertRaisesRegex(
                ValueError,
                "platform week_id cutoff 202630 does not exist",
            ):
                _resolve_blackbox_input_cutoffs_bulk_from_keys(
                    {
                        **current_keys,
                        "week_id": {"202631"},
                    },
                    feature_dates=("2026-08-03",),
                    connection=connection,
                    schema_path=BLACKBOX_SCHEMA_PATH,
                    allow_legacy_v1_period_fallback=True,
                )

    def test_stable_authority_fallback_uses_verified_manifest_version(
        self,
    ) -> None:
        """v1/v2 分类由已验证 manifest 决定，而不是可变 source_mode。"""
        from shared.data_bridge.authority import (
            resolve_stable_databridge_current_authority,
        )
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST_VERSION,
            LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION,
        )

        legacy_current = _current()
        # 无 source_provenance 的严格 current 规范化后就是 v1；
        # 可变 state.source_mode 不应否决这个 read-only marker 语义。
        legacy_current.state["source_mode"] = "local_mysql"
        legacy_current = dataclasses.replace(
            legacy_current,
            publication_manifest={
                "manifest_version": (
                    LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION
                ),
            },
        )
        sealed_v2_current = _current()
        sealed_v2_current.state["source_mode"] = "local_mysql"
        sealed_v2_current = dataclasses.replace(
            sealed_v2_current,
            publication_manifest={
                "manifest_version": CURRENT_PUBLICATION_MANIFEST_VERSION,
            },
        )

        def resolve_from_source_keys(*_args, **kwargs):
            if not kwargs["allow_legacy_v1_period_fallback"]:
                raise ValueError(
                    "platform week_id cutoff 202630 does not exist "
                    "in weekly_output.csv"
                )
            return {
                "2026-07-15": SimpleNamespace(
                    cutoff_keys=CutoffKeys(
                        daily_cutoff_key="2026-07-15",
                        weekly_cutoff_key="202629",
                        monthly_cutoff_key="202607",
                    ),
                    source_weekly_cutoff_key="202630",
                    source_monthly_cutoff_key="202607",
                ),
            }

        for current, expected_fallback in (
            (legacy_current, True),
            (sealed_v2_current, False),
        ):
            with self.subTest(
                manifest_version=current.publication_manifest[
                    "manifest_version"
                ],
            ):
                with (
                    patch(
                        "shared.data_bridge.authority.check_current_dataset",
                        return_value=current,
                    ),
                    patch(
                        "shared.data_bridge.authority."
                        "_resolve_blackbox_input_cutoffs_bulk_from_keys",
                        return_value={
                            "2026-07-15": _cutoffs()["2026-07-15"],
                        },
                        create=True,
                    ) as old_resolve_cutoffs,
                    patch(
                        "shared.data_bridge.authority."
                        "_resolve_blackbox_input_cutoffs_with_source_keys_"
                        "bulk_from_keys",
                        side_effect=resolve_from_source_keys,
                        create=True,
                    ) as resolve_cutoffs,
                ):
                    if expected_fallback:
                        authority = (
                            resolve_stable_databridge_current_authority(
                                self.config,
                                feature_dates=("2026-07-15",),
                                connection=self.connection,
                                allow_legacy_v1_period_fallback=True,
                            )
                        )
                    else:
                        with self.assertRaisesRegex(
                            DataBridgeCurrentInvalidError,
                            "cutoff authority is invalid",
                        ):
                            resolve_stable_databridge_current_authority(
                                self.config,
                                feature_dates=("2026-07-15",),
                                connection=self.connection,
                                allow_legacy_v1_period_fallback=True,
                            )

                self.assertEqual(
                    resolve_cutoffs.call_args.kwargs[
                        "allow_legacy_v1_period_fallback"
                    ],
                    expected_fallback,
                )
                old_resolve_cutoffs.assert_not_called()
                if expected_fallback:
                    self.assertEqual(
                        authority.cutoffs[0].weekly_cutoff_key,
                        "202629",
                    )
