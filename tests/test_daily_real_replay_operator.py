from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


class DailyRealReplayOperatorTests(unittest.TestCase):
    def test_forced_cold_cache_environment_hides_warm_cache_and_restores(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _liwei_forced_cold_cache_environment,
        )

        variable = "LIWEI_0616_PHASE_A_CACHE_ROOT"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            warm = root / "warm"
            warm.mkdir(mode=0o700)
            (warm / "global-cache-sentinel").write_text(
                "must-not-be-visible",
                encoding="utf-8",
            )
            cold = root / "cold"
            cold.mkdir(mode=0o700)
            previous = os.environ.get(variable)
            os.environ[variable] = str(warm)
            try:
                with _liwei_forced_cold_cache_environment(cold):
                    self.assertEqual(
                        os.environ[variable],
                        str(cold.resolve()),
                    )
                    self.assertFalse(
                        (
                            Path(os.environ[variable])
                            / "global-cache-sentinel"
                        ).exists()
                    )
                self.assertEqual(os.environ[variable], str(warm))
            finally:
                if previous is None:
                    os.environ.pop(variable, None)
                else:
                    os.environ[variable] = previous

    def test_capacity_projection_keeps_native_and_v2_pools_parallel(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _build_replay_timing_evidence,
        )

        inputs = SimpleNamespace(
            business_date="2026-07-28",
            native_generation=SimpleNamespace(
                created_at="2026-07-28T08:00:00+00:00",
                sealed_at="2026-07-28T08:10:00+00:00",
            ),
            databridge_generation=SimpleNamespace(
                refresh_started_at="2026-07-28T12:00:00+08:00",
                published_at="2026-07-28T12:20:00+08:00",
                sealed_at="2026-07-28T12:20:00+08:00",
            ),
        )

        evidence = _build_replay_timing_evidence(
            inputs,
            native_pool_started_at=datetime(
                2026, 7, 28, 8, 30, tzinfo=timezone.utc
            ),
            native_pool_last_visible_at=datetime(
                2026, 7, 28, 9, 30, tzinfo=timezone.utc
            ),
            v2_pool_started_at=datetime(
                2026, 7, 28, 9, 15, tzinfo=timezone.utc
            ),
            v2_pool_last_visible_at=datetime(
                2026, 7, 28, 9, 45, tzinfo=timezone.utc
            ),
            db_last_visible_at=datetime(
                2026, 7, 28, 9, 45, tzinfo=timezone.utc
            ),
            max_v2_release_offset_minutes=14,
        )

        self.assertEqual(evidence.native_prepare_seconds, 600.0)
        self.assertEqual(
            evidence.databridge_prepare_seconds,
            1200.0,
        )
        self.assertEqual(evidence.parallel_readiness_seconds, 1200.0)
        self.assertEqual(
            evidence.native_pool_observed_seconds,
            3600.0,
        )
        self.assertEqual(evidence.v2_pool_observed_seconds, 1800.0)
        self.assertEqual(evidence.release_guard_seconds, 840.0)
        self.assertEqual(evidence.end_to_end_seconds, 4200.0)
        self.assertEqual(
            evidence.projected_readiness_at,
            "2026-07-28T06:50:00+08:00",
        )
        self.assertEqual(
            evidence.projected_native_last_visible_at,
            "2026-07-28T07:40:00+08:00",
        )
        self.assertEqual(
            evidence.projected_v2_last_visible_at,
            "2026-07-28T07:34:00+08:00",
        )
        self.assertEqual(
            evidence.projected_last_visible_at,
            "2026-07-28T07:40:00+08:00",
        )
        self.assertEqual(
            evidence.db_last_visible_at,
            "2026-07-28T09:45:00+00:00",
        )
        old_serial_projection_seconds = (
            1200.0
            + 840.0
            + (
                datetime(
                    2026, 7, 28, 9, 45, tzinfo=timezone.utc
                )
                - datetime(
                    2026, 7, 28, 8, 30, tzinfo=timezone.utc
                )
            ).total_seconds()
        )
        self.assertGreater(old_serial_projection_seconds, 5100.0)
        self.assertTrue(evidence.within_capacity_limit)
        self.assertTrue(evidence.within_visibility_deadline)

    def test_capacity_projection_rejects_input_preparation_overrun(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _build_replay_timing_evidence,
            _require_replay_timing_evidence,
            DailyRealReplayPreflightError,
        )

        inputs = SimpleNamespace(
            business_date="2026-07-28",
            native_generation=SimpleNamespace(
                created_at="2026-07-28T08:00:00+00:00",
                sealed_at="2026-07-28T08:50:00+00:00",
            ),
            databridge_generation=SimpleNamespace(
                refresh_started_at="2026-07-28T12:00:00+08:00",
                published_at="2026-07-28T12:20:00+08:00",
                sealed_at="2026-07-28T12:20:00+08:00",
            ),
        )

        evidence = _build_replay_timing_evidence(
            inputs,
            native_pool_started_at=datetime(
                2026, 7, 28, 8, 30, tzinfo=timezone.utc
            ),
            native_pool_last_visible_at=datetime(
                2026, 7, 28, 9, 6, tzinfo=timezone.utc
            ),
            v2_pool_started_at=datetime(
                2026, 7, 28, 8, 45, tzinfo=timezone.utc
            ),
            v2_pool_last_visible_at=datetime(
                2026, 7, 28, 9, 0, tzinfo=timezone.utc
            ),
            db_last_visible_at=datetime(
                2026, 7, 28, 9, 6, tzinfo=timezone.utc
            ),
            max_v2_release_offset_minutes=14,
        )

        self.assertEqual(evidence.end_to_end_seconds, 5160.0)
        self.assertEqual(
            evidence.projected_last_visible_at,
            "2026-07-28T07:56:00+08:00",
        )
        self.assertFalse(evidence.within_capacity_limit)
        self.assertFalse(evidence.within_visibility_deadline)
        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            _require_replay_timing_evidence(evidence)
        self.assertEqual(
            raised.exception.code,
            "REPLAY_EXECUTION_ACCEPTANCE_FAILED",
        )

    def test_capacity_projection_includes_databridge_publish_to_seal(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _build_replay_timing_evidence,
        )

        inputs = SimpleNamespace(
            business_date="2026-07-28",
            native_generation=SimpleNamespace(
                created_at="2026-07-28T08:00:00+00:00",
                sealed_at="2026-07-28T08:10:00+00:00",
            ),
            databridge_generation=SimpleNamespace(
                refresh_started_at="2026-07-28T12:00:00+08:00",
                published_at="2026-07-28T12:20:00+08:00",
                sealed_at="2026-07-28T12:50:00+08:00",
            ),
        )

        evidence = _build_replay_timing_evidence(
            inputs,
            native_pool_started_at=datetime(
                2026, 7, 28, 8, 30, tzinfo=timezone.utc
            ),
            native_pool_last_visible_at=datetime(
                2026, 7, 28, 8, 40, tzinfo=timezone.utc
            ),
            v2_pool_started_at=datetime(
                2026, 7, 28, 8, 30, tzinfo=timezone.utc
            ),
            v2_pool_last_visible_at=datetime(
                2026, 7, 28, 8, 52, tzinfo=timezone.utc
            ),
            db_last_visible_at=datetime(
                2026, 7, 28, 8, 52, tzinfo=timezone.utc
            ),
            max_v2_release_offset_minutes=14,
        )

        self.assertEqual(evidence.databridge_prepare_seconds, 3000.0)
        self.assertEqual(evidence.end_to_end_seconds, 5160.0)
        self.assertEqual(
            evidence.projected_last_visible_at,
            "2026-07-28T07:56:00+08:00",
        )
        self.assertFalse(evidence.within_capacity_limit)
        self.assertFalse(evidence.within_visibility_deadline)

    def test_capacity_projection_rejects_missing_db_visible_receipt(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _build_replay_timing_evidence,
        )

        inputs = SimpleNamespace(
            business_date="2026-07-28",
            native_generation=SimpleNamespace(
                created_at="2026-07-28T08:00:00+00:00",
                sealed_at="2026-07-28T08:10:00+00:00",
            ),
            databridge_generation=SimpleNamespace(
                refresh_started_at="2026-07-28T12:00:00+08:00",
                published_at="2026-07-28T12:20:00+08:00",
                sealed_at="2026-07-28T12:20:00+08:00",
            ),
        )
        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            _build_replay_timing_evidence(
                inputs,
                native_pool_started_at=datetime(
                    2026, 7, 28, 8, 30, tzinfo=timezone.utc
                ),
                native_pool_last_visible_at=datetime(
                    2026, 7, 28, 8, 40, tzinfo=timezone.utc
                ),
                v2_pool_started_at=datetime(
                    2026, 7, 28, 8, 45, tzinfo=timezone.utc
                ),
                v2_pool_last_visible_at=None,
                db_last_visible_at=datetime(
                    2026, 7, 28, 8, 40, tzinfo=timezone.utc
                ),
                max_v2_release_offset_minutes=14,
            )
        self.assertEqual(
            raised.exception.code,
            "REPLAY_TIMING_EVIDENCE_INVALID",
        )

    def test_capacity_projection_rejects_pool_visible_before_start(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _build_replay_timing_evidence,
        )

        inputs = SimpleNamespace(
            business_date="2026-07-28",
            native_generation=SimpleNamespace(
                created_at="2026-07-28T08:00:00+00:00",
                sealed_at="2026-07-28T08:10:00+00:00",
            ),
            databridge_generation=SimpleNamespace(
                refresh_started_at="2026-07-28T12:00:00+08:00",
                published_at="2026-07-28T12:20:00+08:00",
                sealed_at="2026-07-28T12:20:00+08:00",
            ),
        )
        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            _build_replay_timing_evidence(
                inputs,
                native_pool_started_at=datetime(
                    2026, 7, 28, 9, 0, tzinfo=timezone.utc
                ),
                native_pool_last_visible_at=datetime(
                    2026, 7, 28, 8, 59, tzinfo=timezone.utc
                ),
                v2_pool_started_at=datetime(
                    2026, 7, 28, 8, 45, tzinfo=timezone.utc
                ),
                v2_pool_last_visible_at=datetime(
                    2026, 7, 28, 9, 0, tzinfo=timezone.utc
                ),
                db_last_visible_at=datetime(
                    2026, 7, 28, 9, 0, tzinfo=timezone.utc
                ),
                max_v2_release_offset_minutes=14,
            )
        self.assertEqual(
            raised.exception.code,
            "REPLAY_TIMING_EVIDENCE_INVALID",
        )

    def test_execution_audit_preserves_linked_max_visible_as_utc(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _read_replay_execution_audit,
        )

        visible_at = datetime(2026, 7, 28, 1, 2, 3)
        row = {
            "successful_item_count": 25,
            "accepted_target_count": 29,
            "run_count": 25,
            "scheduled_live_run_count": 25,
            "prediction_count": 29,
            "scheduled_live_prediction_count": 29,
            "valid_receipt_count": 29,
            "duplicate_prediction_count": 0,
            "nonterminal_run_count": 0,
            "native_pool_started_at": datetime(
                2026, 7, 28, 0, 30
            ),
            "native_pool_last_visible_at": datetime(
                2026, 7, 28, 0, 50
            ),
            "v2_pool_started_at": datetime(
                2026, 7, 28, 0, 45
            ),
            "v2_pool_last_visible_at": datetime(
                2026, 7, 28, 1, 0
            ),
            "db_last_visible_at": visible_at,
        }
        result = Mock()
        result.mappings.return_value.one.return_value = row
        connection = Mock()
        connection.execute.return_value = result
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        engine = Mock()
        engine.connect.return_value = connection

        audit = _read_replay_execution_audit(
            engine,
            occurrence_id=41,
        )

        self.assertEqual(
            audit["db_last_visible_at"],
            visible_at.replace(tzinfo=timezone.utc),
        )
        self.assertEqual(
            audit["native_pool_started_at"],
            datetime(2026, 7, 28, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            audit["native_pool_last_visible_at"],
            datetime(2026, 7, 28, 0, 50, tzinfo=timezone.utc),
        )
        self.assertEqual(
            audit["v2_pool_started_at"],
            datetime(2026, 7, 28, 0, 45, tzinfo=timezone.utc),
        )
        self.assertEqual(
            audit["v2_pool_last_visible_at"],
            datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc),
        )
        statement = str(connection.execute.call_args.args[0])
        self.assertIn("MAX(t.visible_at)", statement)
        self.assertIn("MIN(r.started_at)", statement)
        self.assertIn("i.runtime_type = 'native_adapter'", statement)
        self.assertIn("i.runtime_type = 'blackbox_v2'", statement)
        self.assertIn(
            "p.id = t.accepted_prediction_id",
            statement,
        )

    def test_candidate_creates_cold_cache_and_final_identity_drift_blocks(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _execute_real_replay_candidate,
        )
        from scheduler.daily_policy import POLICY_V2_PATH

        cache_root = Path("/private/tmp/replay/liwei-phase-a-cache")
        server = MagicMock()
        server.__enter__.return_value = server
        server.__exit__.return_value = None
        server.create_replay_database.return_value = (
            "bfl_real_replay_0123456789abcdef0123",
            Mock(),
        )
        server.create_forced_cold_cache_root.return_value = cache_root
        session = SimpleNamespace(assert_held=Mock())
        preflight = SimpleNamespace(
            qualification="EXCLUDED",
            expected_item_count=25,
            expected_target_count=29,
            v2_item_count=8,
        )
        execution = SimpleNamespace(status="REHEARSAL_PASSED")
        with (
            patch(
                "harness.daily_real_replay_operator."
                "IsolatedReplayMySQL",
                return_value=server,
            ),
            patch(
                "harness.daily_real_replay_operator."
                "_execute_real_replay_on_isolated_database",
                return_value=execution,
            ) as isolated_execute,
            patch(
                "harness.daily_real_replay_operator."
                "assert_real_replay_dispatch_identity_current",
                side_effect=DailyRealReplayPreflightError(
                    "REPLAY_DISPATCH_IDENTITY_DRIFT"
                ),
            ) as final_recheck,
            self.assertRaises(
                DailyRealReplayPreflightError
            ) as raised,
        ):
            _execute_real_replay_candidate(
                session,
                preflight_report=preflight,
                native_manifest="/private/native.json",
                databridge_manifest="/private/databridge.json",
                policy_path=POLICY_V2_PATH,
            )

        self.assertEqual(
            raised.exception.code,
            "REPLAY_DISPATCH_IDENTITY_DRIFT",
        )
        server.create_forced_cold_cache_root.assert_called_once_with()
        self.assertEqual(
            isolated_execute.call_args.kwargs[
                "forced_cold_cache_root"
            ],
            cache_root,
        )
        final_recheck.assert_called_once_with(session)

    def test_execute_only_keeps_preflight_session_and_fixed_v2_policy(
        self,
    ) -> None:
        import inspect

        from harness.daily_real_replay_operator import (
            run_real_replay_execute,
        )
        from scheduler.daily_policy import POLICY_V2_PATH

        session = MagicMock()
        report = SimpleNamespace(
            qualification="EXCLUDED",
            expected_item_count=25,
            expected_target_count=29,
        )
        execution = SimpleNamespace(
            status="REHEARSAL_PASSED",
            qualification="REHEARSAL",
            capacity_qualification="EXCLUDED",
        )
        with (
            patch(
                "harness.daily_real_replay_operator."
                "_require_operator_service_uid",
                return_value=501,
            ),
            patch(
                "harness.daily_real_replay_operator."
                "_preflight_session",
                return_value=nullcontext(session),
            ),
            patch(
                "harness.daily_real_replay_operator."
                "_run_real_replay_preflight_locked",
                return_value=report,
            ) as preflight,
            patch(
                "harness.daily_real_replay_operator."
                "_execute_real_replay_candidate",
                return_value=execution,
            ) as execute,
        ):
            result = run_real_replay_execute(
                native_manifest="/tmp/native.json",
                databridge_manifest="/tmp/databridge.json",
            )

        self.assertIs(result, execution)
        self.assertEqual(
            tuple(inspect.signature(run_real_replay_execute).parameters),
            ("native_manifest", "databridge_manifest"),
        )
        preflight.assert_called_once_with(
            session,
            service_uid=501,
            native_manifest="/tmp/native.json",
            databridge_manifest="/tmp/databridge.json",
            policy_path=POLICY_V2_PATH,
        )
        execute.assert_called_once_with(
            session,
            preflight_report=report,
            native_manifest="/tmp/native.json",
            databridge_manifest="/tmp/databridge.json",
            policy_path=POLICY_V2_PATH,
        )

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
                environment_fingerprint=(
                    "9" * 64 if index >= 17 else None
                ),
            )
            for index in range(21)
        )
        return ReplayDefinitionSnapshot(
            policy_version="daily-scheduler-policy-v1",
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
                        (
                            row.environment_fingerprint
                            or (
                                "9" * 64
                                if row.runtime_type == "blackbox_v2"
                                else None
                            )
                        ),
                    "data_snapshot_id":
                        (
                            row.data_snapshot_id
                            or (
                                "snapshot-" + "a" * 24
                                if row.runtime_type == "blackbox_v2"
                                else None
                            )
                        ),
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
        locked_session.bind_dispatch_identity.assert_called_once()
        dispatch_identity = (
            locked_session.bind_dispatch_identity.call_args.args[0]
        )
        self.assertEqual(dispatch_identity.service_uid, os.getuid())
        self.assertEqual(
            dispatch_identity.business_date,
            inputs.business_date,
        )
        self.assertEqual(
            dispatch_identity.native_manifest_path,
            "/private/native/manifest.json",
        )
        self.assertEqual(
            dispatch_identity.databridge_manifest_path,
            "/private/databridge/manifest.json",
        )
        for value in (
            dispatch_identity.candidate_digest,
            dispatch_identity.generation_digest,
            dispatch_identity.definition_digest,
            dispatch_identity.control_plane_digest,
            dispatch_identity.production_digest,
            dispatch_identity.source_database_digest,
            dispatch_identity.source_watermark_digest,
        ):
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        expected_watermark_digest = hashlib.sha256(
            json.dumps(
                {
                    "start": {
                        "feature_date": "2026-07-24",
                        "source_commit_token": "7" * 64,
                    },
                    "end": {
                        "feature_date": "2026-07-24",
                        "source_commit_token": "8" * 64,
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            dispatch_identity.source_watermark_digest,
            expected_watermark_digest,
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

    def test_blackbox_activation_evidence_is_bound_without_repo_fields(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        rows = []
        for row in production.version_rows:
            current = dict(row)
            if current["runtime_type"] == "blackbox_v2":
                current["environment_fingerprint"] = "9" * 64
                current["data_snapshot_id"] = "snapshot-" + "a" * 24
            rows.append(current)

        digest = validate_production_daily_snapshot(
            replace(production, version_rows=tuple(rows)),
            definitions=definitions,
        )
        drifted_rows = [dict(row) for row in rows]
        drifted_rows[-1]["data_snapshot_id"] = (
            "snapshot-" + "b" * 24
        )
        drifted_digest = validate_production_daily_snapshot(
            replace(
                production,
                version_rows=tuple(drifted_rows),
            ),
            definitions=definitions,
        )

        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, drifted_digest)

    def test_blackbox_activation_evidence_must_be_complete(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        valid_rows = []
        for row in production.version_rows:
            current = dict(row)
            if current["runtime_type"] == "blackbox_v2":
                current["environment_fingerprint"] = "9" * 64
                current["data_snapshot_id"] = "snapshot-" + "a" * 24
            valid_rows.append(current)
        for field, value in (
            ("environment_fingerprint", None),
            ("environment_fingerprint", "not-a-sha"),
            ("data_snapshot_id", None),
            ("data_snapshot_id", ""),
        ):
            with self.subTest(field=field, value=value):
                rows = [dict(row) for row in valid_rows]
                changed = False
                for current in rows:
                    if (
                        not changed
                        and current["runtime_type"] == "blackbox_v2"
                    ):
                        current[field] = value
                        changed = True
                with self.assertRaises(
                    DailyRealReplayPreflightError
                ) as raised:
                    validate_production_daily_snapshot(
                        replace(
                            production,
                            version_rows=tuple(rows),
                        ),
                        definitions=definitions,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "PRODUCTION_VERSION_DRIFT",
                )

    def test_blackbox_environment_mismatch_is_rejected(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = replace(
            self._definitions(),
            version_rows=tuple(
                replace(
                    row,
                    environment_fingerprint=(
                        "8" * 64
                        if row.runtime_type == "blackbox_v2"
                        else None
                    ),
                )
                for row in self._definitions().version_rows
            ),
        )
        production = self._production_snapshot(definitions)
        first = dict(production.version_rows[17])
        first["environment_fingerprint"] = "9" * 64

        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            validate_production_daily_snapshot(
                replace(
                    production,
                    version_rows=(
                        *production.version_rows[:17],
                        first,
                        *production.version_rows[18:],
                    ),
                ),
                definitions=definitions,
            )

        self.assertEqual(
            raised.exception.code,
            "PRODUCTION_VERSION_DRIFT",
        )

    def test_blackbox_snapshot_id_must_be_canonical(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        rows = [dict(row) for row in production.version_rows]
        rows[17]["data_snapshot_id"] = "snapshot-v2"

        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            validate_production_daily_snapshot(
                replace(production, version_rows=tuple(rows)),
                definitions=definitions,
            )

        self.assertEqual(
            raised.exception.code,
            "PRODUCTION_VERSION_DRIFT",
        )

    def test_native_activation_evidence_is_rejected(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            validate_production_daily_snapshot,
        )

        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        first = dict(production.version_rows[0])
        first["environment_fingerprint"] = "9" * 64
        first["data_snapshot_id"] = "snapshot-" + "a" * 24

        with self.assertRaises(
            DailyRealReplayPreflightError
        ) as raised:
            validate_production_daily_snapshot(
                replace(
                    production,
                    version_rows=(
                        first,
                        *production.version_rows[1:],
                    ),
                ),
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

    def test_dispatch_identity_is_write_once_and_requires_held_session(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            _ReplayDispatchIdentity,
            _preflight_session,
        )

        identity = _ReplayDispatchIdentity(
            service_uid=os.getuid(),
            business_date="2026-07-27",
            native_manifest_path="/private/native/manifest.json",
            databridge_manifest_path=(
                "/private/databridge/manifest.json"
            ),
            candidate_digest="1" * 64,
            generation_digest="2" * 64,
            definition_digest="3" * 64,
            control_plane_digest="4" * 64,
            production_digest="5" * 64,
            source_database_digest="6" * 64,
            source_watermark_digest="7" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.realpath(temporary)
            os.chmod(root, 0o700)
            with patch(
                "harness.daily_real_replay_operator."
                "_real_replay_lock_root",
                return_value=__import__("pathlib").Path(root),
            ):
                with _preflight_session() as session:
                    with self.assertRaises(
                        DailyRealReplayPreflightError
                    ) as unbound:
                        session.require_dispatch_identity()
                    self.assertEqual(
                        unbound.exception.code,
                        "REPLAY_DISPATCH_IDENTITY_UNBOUND",
                    )
                    session.bind_dispatch_identity(identity)
                    self.assertIs(
                        session.require_dispatch_identity(),
                        identity,
                    )
                    with self.assertRaises(
                        DailyRealReplayPreflightError
                    ) as rebound:
                        session.bind_dispatch_identity(identity)
                    self.assertEqual(
                        rebound.exception.code,
                        "REPLAY_DISPATCH_IDENTITY_ALREADY_BOUND",
                    )

                with self.assertRaises(
                    DailyRealReplayPreflightError
                ) as released:
                    session.require_dispatch_identity()

        self.assertEqual(
            released.exception.code,
            "PREFLIGHT_SESSION_NOT_HELD",
        )

    def test_dispatch_identity_recheck_detects_candidate_drift(
        self,
    ) -> None:
        from shared.data_contract import (
            CALENDAR_SOURCE_TABLES,
            FACTOR_SOURCE_TABLES,
            METADATA_SOURCE_TABLE,
        )
        from harness.daily_real_replay_operator import (
            DailyRealReplayPreflightError,
            ReplaySourceInputEvidence,
            _build_replay_dispatch_identity,
            _preflight_session,
            assert_real_replay_dispatch_identity_current,
            validate_production_daily_snapshot,
        )

        candidate = self._candidate()
        inputs = self._inputs()
        definitions = self._definitions()
        production = self._production_snapshot(definitions)
        registry_digest = validate_production_daily_snapshot(
            production,
            definitions=definitions,
        )
        control_plane = SimpleNamespace(digest="6" * 64)
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
        identity = _build_replay_dispatch_identity(
            service_uid=os.getuid(),
            native_manifest="/private/native/manifest.json",
            databridge_manifest="/private/databridge/manifest.json",
            candidate=candidate,
            inputs=inputs,
            definitions=definitions,
            control_plane=control_plane,
            production=production,
            production_registry_digest=registry_digest,
            source_config=source_config,
            source_preflight=source_preflight,
            source_start=ReplaySourceInputEvidence(
                feature_date="2026-07-24",
                source_commit_token="7" * 64,
            ),
            source_end=ReplaySourceInputEvidence(
                feature_date="2026-07-24",
                source_commit_token="8" * 64,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.realpath(temporary)
            os.chmod(root, 0o700)
            with (
                patch(
                    "harness.daily_real_replay_operator."
                    "_real_replay_lock_root",
                    return_value=__import__("pathlib").Path(root),
                ),
                _preflight_session() as session,
            ):
                session.bind_dispatch_identity(identity)
                stable_patches = (
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_candidate_identity",
                        return_value=candidate,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_generation_inputs",
                        return_value=inputs,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_definition_snapshot",
                        return_value=definitions,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_read_control_plane_boundary",
                        return_value=control_plane,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_read_production_daily_snapshot",
                        return_value=production,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "load_source_runtime_database_config",
                        return_value=source_config,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "preflight_source_runtime_database_access",
                        return_value=source_preflight,
                    ),
                )
                with (
                    stable_patches[0],
                    stable_patches[1],
                    stable_patches[2],
                    stable_patches[3],
                    stable_patches[4],
                    stable_patches[5],
                    stable_patches[6],
                ):
                    self.assertIs(
                        assert_real_replay_dispatch_identity_current(
                            session
                        ),
                        identity,
                    )

                drifted = replace(
                    candidate,
                    tree_sha256="9" * 64,
                )
                with (
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_candidate_identity",
                        return_value=drifted,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_generation_inputs",
                        return_value=inputs,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_stable_definition_snapshot",
                        return_value=definitions,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_read_control_plane_boundary",
                        return_value=control_plane,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "_read_production_daily_snapshot",
                        return_value=production,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "load_source_runtime_database_config",
                        return_value=source_config,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "preflight_source_runtime_database_access",
                        return_value=source_preflight,
                    ),
                    self.assertRaises(
                        DailyRealReplayPreflightError
                    ) as raised,
                ):
                    assert_real_replay_dispatch_identity_current(
                        session
                    )

        self.assertEqual(
            raised.exception.code,
            "REPLAY_DISPATCH_IDENTITY_DRIFT",
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
            snapshot = _read_production_daily_snapshot(
                expected_scheme_versions={"daily": "v2"},
            )

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

    def test_production_snapshot_reader_returns_all_active_daily_versions(
        self,
    ) -> None:
        from harness.daily_real_replay_operator import (
            _read_production_daily_snapshot,
            _expected_production_migrations,
        )

        class _Result:
            def __init__(self, *, rows=()):
                self._rows = rows

            def mappings(self):
                return self

            def __iter__(self):
                return iter(self._rows)

            def one(self):
                if len(self._rows) != 1:
                    raise AssertionError("expected exactly one row")
                return self._rows[0]

        exact = {
            "scheme_id": "daily",
            "scheme_version": "v2",
            "runtime_type": "blackbox_v2",
            "code_sha256": "1" * 64,
            "config_sha256": "2" * 64,
            "manifest_sha256": "3" * 64,
            "algorithm_version": "algorithm-v2",
            "contract_version": "1.0",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "4" * 64,
            "data_snapshot_id": "snapshot-v2",
            "status": "active",
        }
        stale = {**exact, "scheme_version": "v1"}
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
            _Result(rows=(exact, stale)),
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

        self.assertEqual(snapshot.version_rows, (exact, stale))
        version_statement = str(
            connection.execute.call_args_list[3].args[0]
        ).casefold()
        self.assertIn("exists", version_statement)
        self.assertIn("frequency = 'daily'", version_statement)
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

    def test_cli_requires_exactly_one_replay_action(self) -> None:
        from harness.cli import main

        common = [
            "daily-real-replay",
            "--native-manifest",
            "/private/native/manifest.json",
            "--databridge-manifest",
            "/private/databridge/manifest.json",
        ]
        for action_flags in ((), ("--check-only", "--execute-only")):
            with (
                self.subTest(action_flags=action_flags),
                self.assertRaises(SystemExit) as raised,
            ):
                main([*common, *action_flags])
            self.assertEqual(raised.exception.code, 2)

    def test_cli_execute_only_emits_rehearsal_report(self) -> None:
        from harness.cli import main

        report = {
            "schema_version": "daily-real-replay-execution-v1",
            "status": "REHEARSAL_PASSED",
            "qualification": "REHEARSAL",
            "capacity_qualification": "EXCLUDED",
        }
        stdout = io.StringIO()
        with (
            patch(
                "harness.cli.run_real_replay_execute",
                return_value=report,
            ) as execute,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "daily-real-replay",
                    "--execute-only",
                    "--native-manifest",
                    "/private/native/manifest.json",
                    "--databridge-manifest",
                    "/private/databridge/manifest.json",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_version": "daily-real-replay-execution-v1",
                "status": "REHEARSAL_PASSED",
                "qualification": "REHEARSAL",
                "capacity_qualification": "EXCLUDED",
            },
        )
        execute.assert_called_once_with(
            native_manifest=__import__("pathlib").Path(
                "/private/native/manifest.json"
            ),
            databridge_manifest=__import__("pathlib").Path(
                "/private/databridge/manifest.json"
            ),
        )


if __name__ == "__main__":
    unittest.main()
