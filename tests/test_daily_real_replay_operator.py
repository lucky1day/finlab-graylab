from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


class DailyRealReplayOperatorTests(unittest.TestCase):
    def _candidate(self):
        from harness.daily_real_replay_operator import ReplayCandidateIdentity

        return ReplayCandidateIdentity(
            branch="codex/audit-bugfixes-20260613",
            git_head="a" * 40,
            tree_sha256="b" * 64,
            policy_sha256="c" * 64,
        )

    def _inputs(self):
        return SimpleNamespace(
            business_date="2026-07-27",
            feature_date="2026-07-24",
            native_generation=SimpleNamespace(
                generation_id="native-20260727",
                manifest_sha256="d" * 64,
            ),
            databridge_generation=SimpleNamespace(
                generation_id="databridge-20260727",
                manifest_sha256="e" * 64,
            ),
        )

    def _definitions(self):
        from harness.daily_real_replay_operator import (
            ReplayDefinitionSnapshot,
            ReplayExpectedRegistryRow,
            ReplayExpectedVersionRow,
        )

        one_target_rows = tuple(
            ReplayExpectedRegistryRow(
                registry_scheme_id=f"scheme_{index}__h1__1Y",
                base_scheme_id=f"scheme_{index}",
                runtime_type=(
                    "blackbox_v2"
                    if 17 <= index < 21
                    else "native_adapter"
                ),
                task_type="T+1",
                target_tenor="1Y",
                horizon=1,
            )
            for index in range(21)
        )
        extra_native_rows = tuple(
            ReplayExpectedRegistryRow(
                registry_scheme_id=(
                    f"scheme_{index}__h1__{target_tenor}"
                ),
                base_scheme_id=f"scheme_{index}",
                runtime_type="native_adapter",
                task_type="T+1",
                target_tenor=target_tenor,
                horizon=1,
            )
            for index, target_tenor in enumerate(
                ("3Y", "5Y", "7Y", "10Y")
            )
        )
        registry_rows = one_target_rows + extra_native_rows
        version_rows = tuple(
            ReplayExpectedVersionRow(
                scheme_id=f"scheme_{index}",
                scheme_version=f"version-{index}",
                runtime_type=(
                    "blackbox_v2" if index >= 17 else "native_adapter"
                ),
                code_sha256=f"{index + 1:064x}",
                config_sha256=f"{index + 101:064x}",
            )
            for index in range(21)
        )
        return ReplayDefinitionSnapshot(
            policy_version="daily-policy-v1",
            policy_sha256="c" * 64,
            expected_item_count=21,
            expected_target_count=25,
            native_item_count=17,
            v2_item_count=4,
            input_mode_counts=MappingProxyType(
                {
                    "generation_v1": 14,
                    "live_source_0629": 3,
                    "databridge_v1": 4,
                }
            ),
            registry_rows=registry_rows,
            version_rows=version_rows,
        )

    def _production_snapshot(self, definitions=None):
        from harness.daily_real_replay_operator import (
            ProductionDailySnapshot,
            _expected_production_migrations,
        )

        definition = definitions or self._definitions()
        return ProductionDailySnapshot(
            database_name="bond_db",
            server_identity_sha256="0" * 64,
            migration_rows=tuple(
                {
                    "version": version,
                    "filename": filename,
                    "checksum_sha256": checksum,
                    "state": state,
                }
                for version, filename, checksum, state
                in _expected_production_migrations()
            ),
            registry_rows=tuple(
                {
                    "scheme_id": row.registry_scheme_id,
                    "base_scheme_id": row.base_scheme_id,
                    "runtime_type": row.runtime_type,
                    "status": "active",
                    "frequency": "daily",
                    "task_type": row.task_type,
                    "target_tenor": row.target_tenor,
                    "horizon": row.horizon,
                }
                for row in definition.registry_rows
            ),
            version_rows=tuple(
                {
                    "scheme_id": row.scheme_id,
                    "scheme_version": row.scheme_version,
                    "runtime_type": row.runtime_type,
                    "code_sha256": row.code_sha256,
                    "config_sha256": row.config_sha256,
                    "manifest_sha256": row.manifest_sha256,
                    "algorithm_version": row.algorithm_version,
                    "contract_version": row.contract_version,
                    "runtime_profile": row.runtime_profile,
                    "environment_fingerprint":
                        row.environment_fingerprint,
                    "data_snapshot_id": row.data_snapshot_id,
                    "status": "active",
                }
                for row in definition.version_rows
            ),
        )

    def test_check_only_report_binds_candidate_inputs_registry_and_quiescence(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            ReplayQuiescenceSnapshot,
            ReplaySourceInputEvidence,
            run_real_replay_preflight,
        )

        candidate = self._candidate()
        inputs = self._inputs()
        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        from shared.data_contract import (
            CALENDAR_SOURCE_TABLES,
            FACTOR_SOURCE_TABLES,
            METADATA_SOURCE_TABLE,
        )

        source_config = SimpleNamespace(
            cache_identity="f" * 64,
            database="source_db",
            user="source_readonly",
        )
        source_preflight = SimpleNamespace(
            database="source_db",
            authenticated_user="source_readonly",
            tables=(
                *FACTOR_SOURCE_TABLES,
                METADATA_SOURCE_TABLE,
                *CALENDAR_SOURCE_TABLES,
            ),
        )
        quiescence = ReplayQuiescenceSnapshot(
            loaded_launchagent_labels=(),
            counters=MappingProxyType(
                {
                    "active_occurrence_count": 0,
                    "nonterminal_item_count": 0,
                    "running_ledger_run_count": 0,
                    "cleanup_pending_count": 0,
                    "running_legacy_scheduled_live_run_count": 0,
                    "registered_process_alive_count": 0,
                    "project_process_count": 0,
                }
            ),
            digest="1" * 64,
        )
        control_plane = SimpleNamespace(digest="6" * 64)
        locked_session = MagicMock()
        locked_session.assert_held = Mock()
        locked_session.__enter__.return_value = locked_session

        with (
            patch(
                "harness.daily_real_replay_operator._preflight_session",
                return_value=nullcontext(locked_session),
            ),
            patch(
                "harness.daily_real_replay_operator._freeze_candidate_identity",
                side_effect=(candidate, candidate),
            ) as freeze_candidate,
            patch(
                "harness.daily_real_replay_operator.open_real_replay_generations",
                side_effect=(inputs, inputs),
            ) as open_generations,
            patch(
                "harness.daily_real_replay_operator._load_definition_snapshot",
                side_effect=(definitions, definitions),
            ),
            patch(
                "harness.daily_real_replay_operator._read_control_plane_boundary",
                side_effect=(control_plane, control_plane),
            ),
            patch(
                "harness.daily_real_replay_operator.load_source_runtime_database_config",
                return_value=source_config,
            ),
            patch(
                "harness.daily_real_replay_operator.preflight_source_runtime_database_access",
                side_effect=(source_preflight, source_preflight),
            ),
            patch(
                "harness.daily_real_replay_operator._read_source_input_evidence",
                side_effect=(
                    ReplaySourceInputEvidence(
                        feature_date="2026-07-24",
                        source_commit_token="7" * 64,
                    ),
                    ReplaySourceInputEvidence(
                        feature_date="2026-07-24",
                        source_commit_token="8" * 64,
                    ),
                ),
            ),
            patch(
                "harness.daily_real_replay_operator._read_production_daily_snapshot",
                side_effect=(production, production),
            ),
            patch(
                "harness.daily_real_replay_operator._probe_replay_quiescence",
                side_effect=(quiescence, quiescence),
            ),
        ):
            report = run_real_replay_preflight(
                native_manifest="/private/native/manifest.json",
                databridge_manifest="/private/databridge/manifest.json",
            )

        self.assertEqual(report.status, "CHECK_PASSED")
        self.assertEqual(report.qualification, "EXCLUDED")
        self.assertEqual(report.business_date, "2026-07-27")
        self.assertEqual(report.feature_date, "2026-07-24")
        self.assertEqual(report.expected_item_count, 21)
        self.assertEqual(report.expected_target_count, 25)
        self.assertEqual(report.native_item_count, 17)
        self.assertEqual(report.v2_item_count, 4)
        self.assertEqual(
            dict(report.input_mode_counts),
            {
                "generation_v1": 14,
                "live_source_0629": 3,
                "databridge_v1": 4,
            },
        )
        self.assertEqual(report.source_database_identity_sha256, "f" * 64)
        self.assertEqual(report.source_table_count, 9)
        self.assertEqual(
            report.source_watermark_start_sha256,
            "7" * 64,
        )
        self.assertEqual(
            report.source_watermark_end_sha256,
            "8" * 64,
        )
        self.assertEqual(report.control_plane_digest, "6" * 64)
        self.assertEqual(len(report.production_registry_digest), 64)
        self.assertEqual(len(report.preflight_digest), 64)
        self.assertNotEqual(
            report.production_registry_digest,
            report.preflight_digest,
        )
        self.assertNotIn("host", repr(report).casefold())
        self.assertNotIn("password", repr(report).casefold())
        self.assertEqual(freeze_candidate.call_count, 2)
        self.assertEqual(open_generations.call_count, 2)
        self.assertEqual(
            locked_session.assert_held.call_args_list,
            [unittest.mock.call(), unittest.mock.call()],
        )

    def test_registry_drift_is_rejected_without_reporting_database_rows(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        first = dict(production.registry_rows[0])
        first["runtime_type"] = "blackbox_v2"
        drifted = replace(
            production,
            registry_rows=(first, *production.registry_rows[1:]),
        )

        with self.assertRaises(DailyRealReplayPreflightError) as raised:
            validate_production_daily_snapshot(
                drifted,
                definitions=definitions,
            )

        self.assertEqual(raised.exception.code, "PRODUCTION_REGISTRY_DRIFT")
        self.assertEqual(str(raised.exception), "PRODUCTION_REGISTRY_DRIFT")

    def test_version_manifest_or_runtime_profile_drift_is_rejected(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        for field, value in (
            ("manifest_sha256", "9" * 64),
            ("runtime_profile", "unexpected-runtime"),
        ):
            with self.subTest(field=field):
                production = self._production_snapshot(definitions)
                first = dict(production.version_rows[0])
                first[field] = value
                drifted = replace(
                    production,
                    version_rows=(
                        first,
                        *production.version_rows[1:],
                    ),
                )
                with self.assertRaises(
                    DailyRealReplayPreflightError
                ) as raised:
                    validate_production_daily_snapshot(
                        drifted,
                        definitions=definitions,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "PRODUCTION_VERSION_DRIFT",
                )

    def test_production_snapshot_requires_exact_applied_001_to_017_prefix(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = replace(
            self._production_snapshot(definitions),
            migration_rows=self._production_snapshot(
                definitions
            ).migration_rows[:-1],
        )

        with self.assertRaises(DailyRealReplayPreflightError) as raised:
            validate_production_daily_snapshot(
                production,
                definitions=definitions,
            )

        self.assertEqual(raised.exception.code, "PRODUCTION_MIGRATION_DRIFT")

    def test_loaded_service_alone_blocks_check_only(self) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            ReplayQuiescenceSnapshot,
            validate_replay_quiescence,
        )

        snapshot = ReplayQuiescenceSnapshot(
            loaded_launchagent_labels=("com.bond-factor-lab.scheduler",),
            counters=MappingProxyType(
                {
                    "active_occurrence_count": 0,
                    "nonterminal_item_count": 0,
                    "running_ledger_run_count": 0,
                    "cleanup_pending_count": 0,
                    "running_legacy_scheduled_live_run_count": 0,
                    "registered_process_alive_count": 0,
                    "project_process_count": 0,
                }
            ),
            digest="2" * 64,
        )

        with self.assertRaises(DailyRealReplayPreflightError) as raised:
            validate_replay_quiescence(snapshot)

        self.assertEqual(raised.exception.code, "PLATFORM_NOT_QUIESCENT")
        self.assertEqual(str(raised.exception), "PLATFORM_NOT_QUIESCENT")

    def test_database_or_process_counter_alone_blocks_check_only(self) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            ReplayQuiescenceSnapshot,
            validate_replay_quiescence,
        )

        snapshot = ReplayQuiescenceSnapshot(
            loaded_launchagent_labels=(),
            counters=MappingProxyType(
                {
                    "active_occurrence_count": 0,
                    "nonterminal_item_count": 0,
                    "running_ledger_run_count": 0,
                    "cleanup_pending_count": 1,
                    "running_legacy_scheduled_live_run_count": 0,
                    "registered_process_alive_count": 0,
                    "project_process_count": 0,
                }
            ),
            digest="4" * 64,
        )

        with self.assertRaises(DailyRealReplayPreflightError) as raised:
            validate_replay_quiescence(snapshot)

        self.assertEqual(raised.exception.code, "PLATFORM_NOT_QUIESCENT")

    def test_operator_refuses_root_or_non_service_uid_before_probing(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _require_operator_service_uid,
        )

        with (
            patch(
                "harness.daily_real_replay_operator.os.getuid",
                return_value=501,
            ),
            patch(
                "harness.daily_real_replay_operator.os.geteuid",
                return_value=0,
            ),
            self.assertRaises(DailyRealReplayPreflightError) as raised,
        ):
            _require_operator_service_uid()

        self.assertEqual(
            raised.exception.code,
            "OPERATOR_SERVICE_UID_MISMATCH",
        )

    def test_preflight_session_rejects_active_runtime_owner(self) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _preflight_session,
        )
        from scheduler.daily_coordinator import OccurrenceFileLock

        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.realpath(temporary)
            os.chmod(root, 0o700)
            runtime_lock = OccurrenceFileLock(
                os.path.join(root, "real-replay-runtime.lock")
            )
            runtime_lock.acquire()
            try:
                with (
                    patch(
                        "harness.daily_real_replay_operator._real_replay_lock_root",
                        return_value=__import__("pathlib").Path(root),
                    ),
                    self.assertRaises(
                        DailyRealReplayPreflightError
                    ) as raised,
                ):
                    with _preflight_session():
                        pass
            finally:
                runtime_lock.release()

        self.assertEqual(
            raised.exception.code,
            "PREFLIGHT_SESSION_ALREADY_HELD",
        )

    def test_preflight_session_yields_owned_double_lock_until_exit(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _preflight_session,
        )
        from scheduler.daily_coordinator import (
            OccurrenceFileLock,
            OccurrenceLockUnavailable,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.realpath(temporary)
            os.chmod(root, 0o700)
            operator_path = os.path.join(
                root,
                "real-replay-operator.lock",
            )
            runtime_path = os.path.join(
                root,
                "real-replay-runtime.lock",
            )
            with patch(
                "harness.daily_real_replay_operator._real_replay_lock_root",
                return_value=__import__("pathlib").Path(root),
            ):
                with _preflight_session() as session:
                    session.assert_held()
                    self.assertEqual(session.owner_pid, os.getpid())
                    self.assertTrue(session.operator_lock.acquired)
                    self.assertTrue(session.runtime_lock.acquired)
                    with session as borrowed:
                        self.assertIs(borrowed, session)
                    self.assertTrue(session.operator_lock.acquired)
                    self.assertTrue(session.runtime_lock.acquired)
                    with self.assertRaises(OccurrenceLockUnavailable):
                        OccurrenceFileLock(operator_path).acquire()
                    with self.assertRaises(OccurrenceLockUnavailable):
                        OccurrenceFileLock(runtime_path).acquire()

                self.assertFalse(session.operator_lock.acquired)
                self.assertFalse(session.runtime_lock.acquired)
                with self.assertRaises(
                    DailyRealReplayPreflightError
                ) as raised:
                    session.assert_held()

        self.assertEqual(
            raised.exception.code,
            "PREFLIGHT_SESSION_NOT_HELD",
        )

    def test_second_candidate_or_generation_read_detects_drift(self) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            ReplayQuiescenceSnapshot,
            ReplaySourceInputEvidence,
            run_real_replay_preflight,
        )

        candidate = self._candidate()
        drifted_candidate = replace(candidate, git_head="9" * 40)
        inputs = self._inputs()
        definitions = self._definitions()
        locked_session = MagicMock()
        locked_session.assert_held = Mock()
        locked_session.__enter__.return_value = locked_session

        with (
            patch(
                "harness.daily_real_replay_operator._preflight_session",
                return_value=nullcontext(locked_session),
            ),
            patch(
                "harness.daily_real_replay_operator._freeze_candidate_identity",
                side_effect=(candidate, drifted_candidate),
            ),
            patch(
                "harness.daily_real_replay_operator.open_real_replay_generations",
                side_effect=(inputs, inputs),
            ),
            patch(
                "harness.daily_real_replay_operator._load_definition_snapshot",
                return_value=definitions,
            ),
            patch(
                "harness.daily_real_replay_operator._read_control_plane_boundary",
                return_value=SimpleNamespace(digest="6" * 64),
            ),
            patch(
                "harness.daily_real_replay_operator.load_source_runtime_database_config",
                return_value=SimpleNamespace(
                    cache_identity="f" * 64,
                    database="source_db",
                    user="source_readonly",
                ),
            ),
            patch(
                "harness.daily_real_replay_operator.preflight_source_runtime_database_access",
                return_value=SimpleNamespace(
                    database="source_db",
                    authenticated_user="source_readonly",
                    tables=(
                        "api_wind_daily",
                        "api_wind_derivative_daily",
                        "api_wind_weekly",
                        "api_wind_derivative_weekly",
                        "api_wind_monthly",
                        "api_wind_derivative_monthly",
                        "api_wind_indicators_all",
                        "api_wind_date",
                        "t_trade_calendar",
                    ),
                ),
            ),
            patch(
                "harness.daily_real_replay_operator._read_source_input_evidence",
                return_value=ReplaySourceInputEvidence(
                    feature_date="2026-07-24",
                    source_commit_token="7" * 64,
                ),
            ),
            patch(
                "harness.daily_real_replay_operator._read_production_daily_snapshot",
                side_effect=(
                    self._production_snapshot(definitions),
                    self._production_snapshot(definitions),
                ),
            ),
            patch(
                "harness.daily_real_replay_operator._probe_replay_quiescence",
                side_effect=(
                    ReplayQuiescenceSnapshot(
                        loaded_launchagent_labels=(),
                        counters=MappingProxyType(
                            {
                                "active_occurrence_count": 0,
                                "nonterminal_item_count": 0,
                                "running_ledger_run_count": 0,
                                "cleanup_pending_count": 0,
                                "running_legacy_scheduled_live_run_count": 0,
                                "registered_process_alive_count": 0,
                                "project_process_count": 0,
                            }
                        ),
                        digest="3" * 64,
                    ),
                    ReplayQuiescenceSnapshot(
                        loaded_launchagent_labels=(),
                        counters=MappingProxyType(
                            {
                                "active_occurrence_count": 0,
                                "nonterminal_item_count": 0,
                                "running_ledger_run_count": 0,
                                "cleanup_pending_count": 0,
                                "running_legacy_scheduled_live_run_count": 0,
                                "registered_process_alive_count": 0,
                                "project_process_count": 0,
                            }
                        ),
                        digest="3" * 64,
                    ),
                ),
            ),
        ):
            with self.assertRaises(DailyRealReplayPreflightError) as raised:
                run_real_replay_preflight(
                    native_manifest="/private/native/manifest.json",
                    databridge_manifest="/private/databridge/manifest.json",
                )

        self.assertEqual(raised.exception.code, "PREFLIGHT_IDENTITY_DRIFT")
        locked_session.assert_held.assert_called_once_with()

    def test_source_preflight_rejects_same_count_with_wrong_tables(self) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_source_database_preflight,
        )

        config = SimpleNamespace(
            database="source_db",
            user="source_readonly",
        )
        preflight = SimpleNamespace(
            database="source_db",
            authenticated_user="source_readonly",
            tables=tuple(f"wrong_{index}" for index in range(9)),
        )

        with self.assertRaises(DailyRealReplayPreflightError) as raised:
            validate_source_database_preflight(
                config,
                preflight=preflight,
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_DATABASE_IDENTITY_DRIFT",
        )

    def test_source_tables_exist_but_feature_date_not_ready_is_blocked(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _read_source_input_evidence,
        )

        engine = Mock()
        calendar = Mock()
        calendar.is_trading_day.return_value = True
        calendar.previous_trading_day.return_value = "2026-07-24"
        inputs = self._inputs()
        with (
            patch(
                "harness.daily_real_replay_operator.create_input_engine",
                return_value=engine,
            ),
            patch(
                "harness.daily_real_replay_operator.get_calendar",
                return_value=calendar,
            ),
            patch(
                "harness.daily_real_replay_operator.inspect_native_input_readiness",
                return_value=SimpleNamespace(ready=False),
            ),
            self.assertRaises(
                DailyRealReplayPreflightError
            ) as raised,
        ):
            _read_source_input_evidence(
                SimpleNamespace(),
                inputs=inputs,
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_INPUT_NOT_READY",
        )
        engine.dispose.assert_called_once_with()

    def test_live_calendar_must_match_frozen_generation_calendar(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _read_source_input_evidence,
        )

        engine = Mock()
        calendar = Mock()
        calendar.is_trading_day.return_value = True
        calendar.previous_trading_day.return_value = "2026-07-23"
        with (
            patch(
                "harness.daily_real_replay_operator.create_input_engine",
                return_value=engine,
            ),
            patch(
                "harness.daily_real_replay_operator.get_calendar",
                return_value=calendar,
            ),
            self.assertRaises(
                DailyRealReplayPreflightError
            ) as raised,
        ):
            _read_source_input_evidence(
                SimpleNamespace(),
                inputs=self._inputs(),
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_FROZEN_CALENDAR_DRIFT",
        )
        engine.dispose.assert_called_once_with()

    def test_production_snapshot_reader_is_select_only_and_rolls_back(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _read_production_daily_snapshot,
            _expected_production_migrations,
        )

        class _Result:
            def __init__(self, *, scalar=None, rows=()):
                self._scalar = scalar
                self._rows = rows

            def scalar_one(self):
                return self._scalar

            def mappings(self):
                return self

            def __iter__(self):
                return iter(self._rows)

            def one(self):
                if len(self._rows) != 1:
                    raise AssertionError("expected exactly one row")
                return self._rows[0]

        connection = Mock()
        connection.execute.side_effect = (
            _Result(
                rows=(
                    {
                        "database_name": "bond_db",
                        "server_uuid":
                            "11111111-1111-1111-1111-111111111111",
                        "server_port": 3306,
                    },
                )
            ),
            _Result(
                rows=tuple(
                    {
                        "version": version,
                        "filename": filename,
                        "checksum_sha256": checksum,
                        "state": state,
                    }
                    for version, filename, checksum, state
                    in _expected_production_migrations()
                )
            ),
            _Result(rows=()),
            _Result(rows=()),
        )
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        engine = Mock()
        engine.connect.return_value = connection

        with patch(
            "harness.daily_real_replay_operator.create_engine_from_env",
            return_value=engine,
        ):
            snapshot = _read_production_daily_snapshot()

        self.assertEqual(snapshot.database_name, "bond_db")
        for call in connection.exec_driver_sql.call_args_list:
            statement = str(call.args[0]).strip().upper()
            self.assertTrue(
                statement.startswith("SET ")
                or statement.startswith("START TRANSACTION ")
            )
        for call in connection.execute.call_args_list:
            statement = str(call.args[0]).strip().upper()
            self.assertTrue(statement.startswith("SELECT "))
        migration_statement = str(
            connection.execute.call_args_list[1].args[0]
        ).casefold()
        self.assertIn("sha256 as checksum_sha256", migration_statement)
        self.assertNotIn(
            "select version, filename,\n"
            "                                   checksum_sha256",
            migration_statement,
        )
        connection.rollback.assert_called_once_with()
        engine.dispose.assert_called_once_with()

    def test_production_snapshot_reader_rolls_back_and_disposes_on_failure(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _read_production_daily_snapshot,
        )

        connection = Mock()
        connection.execute.side_effect = RuntimeError("unique-secret")
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        engine = Mock()
        engine.connect.return_value = connection

        with (
            patch(
                "harness.daily_real_replay_operator.create_engine_from_env",
                return_value=engine,
            ),
            self.assertRaises(RuntimeError),
        ):
            _read_production_daily_snapshot()

        connection.rollback.assert_called_once_with()
        engine.dispose.assert_called_once_with()

    def test_cli_check_only_emits_stable_json_and_redacts_block_reason(
        self,
    ) -> None:
        from harness.cli import main
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
        )

        stdout = io.StringIO()
        with (
            patch(
                "harness.cli.run_real_replay_preflight",
                side_effect=DailyRealReplayPreflightError(
                    "SOURCE_DATABASE_PREFLIGHT_FAILED"
                ),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "daily-real-replay",
                    "--check-only",
                    "--native-manifest",
                    "/private/native/manifest.json",
                    "--databridge-manifest",
                    "/private/databridge/manifest.json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(
            payload["failure_code"],
            "SOURCE_DATABASE_PREFLIGHT_FAILED",
        )
        self.assertNotIn("message", payload)
        self.assertNotIn("password", stdout.getvalue().casefold())


if __name__ == "__main__":
    unittest.main()
