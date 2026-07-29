from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from scheduler.daily_runtime import (
    DailyRuntime,
    DefaultDailyRuntimeServices,
    GenerationBuildOutcome,
    NativeReadinessPending,
    OccurrenceInputs,
    WatchdogResult,
    _GenerationAvailability,
    _process_rows,
    _policy_payload,
    _runtime_services,
    _terminate_verified_orphan,
    run_daily_occurrence,
    run_daily_watchdog,
    run_operator_recovery,
    run_scheduler_heartbeat,
)
from scheduler.daily_coordinator import OccurrenceLockUnavailable


SHANGHAI = ZoneInfo("Asia/Shanghai")
TEST_COORDINATOR_EPOCH = {
    "epoch": 7,
    "mode": "ledger",
    "record_sha256": "e" * 64,
}


class DailyRuntimeDefaultServiceTests(unittest.TestCase):
    def test_default_service_loads_policy_v2_explicitly(self) -> None:
        from scheduler import daily_runtime as module
        from scheduler.daily_policy import POLICY_V2_PATH

        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        policy = SimpleNamespace(schemes={})
        with (
            patch.object(module, "discover_schemes", return_value=()),
            patch.object(
                module,
                "load_daily_policy",
                return_value=policy,
            ) as load_policy,
        ):
            self.assertIs(services.load_policy(), policy)

        load_policy.assert_called_once_with(
            POLICY_V2_PATH,
            discovered=(),
        )

    def test_production_authority_uses_policy_v2_for_both_branches(
        self,
    ) -> None:
        from scheduler import daily_runtime as module
        from scheduler.daily_policy import POLICY_V2_PATH

        engine = object()
        with (
            patch(
                "shared.daily_coordinator_mode."
                "bootstrap_deployment_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "require_current_capacity_admission",
                return_value={"status": "ADMITTED"},
            ) as current,
            patch(
                "scheduler.capacity_admission."
                "require_daily_capacity_admission",
                return_value={"status": "ADMITTED"},
            ) as signed,
            patch.object(
                module,
                "preflight_schedule_run_started_at_nullable",
            ) as started_at_preflight,
        ):
            module._require_production_entry_authority(
                engine=engine,
                verify_current=True,
            )
            module._require_production_entry_authority(
                engine=engine,
                verify_current=False,
            )

        current.assert_called_once_with(
            engine,
            policy_path=POLICY_V2_PATH,
            algo_env="forecast_env",
        )
        started_at_preflight.assert_called_once_with(engine)
        signed.assert_called_once_with(policy_path=POLICY_V2_PATH)

    def test_execute_item_reuses_canonical_process_start_guard(
        self,
    ) -> None:
        from scheduler import daily_runtime as module
        from scheduler.process_control import ProcessStartGuard

        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        with patch.object(
            module,
            "execute_scheduled_item",
            return_value=object(),
        ) as execute:
            services.execute_item(
                item_id=11,
                trigger_origin="apscheduler",
            )
            services.execute_item(
                item_id=12,
                trigger_origin="apscheduler",
            )

        guards = [
            call.kwargs["process_start_guard"]
            for call in execute.call_args_list
        ]
        self.assertEqual(len(guards), 2)
        self.assertIs(guards[0], guards[1])
        self.assertIsInstance(guards[0], ProcessStartGuard)

    def test_generation_builds_pass_policy_hard_storage_limits(self) -> None:
        from scheduler import daily_runtime as module

        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        services._policy = SimpleNamespace(
            generation_min_free_bytes=2 * 1024**3,
            generation_max_total_bytes=50 * 1024**3,
        )
        capture_deadline = datetime(
            2026,
            7,
            24,
            6,
            31,
            tzinfo=SHANGHAI,
        )
        contract_cutoff = datetime(
            2026,
            7,
            24,
            6,
            30,
            tzinfo=SHANGHAI,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            native_root = Path(tmpdir) / "native"
            databridge_root = Path(tmpdir) / "databridge"
            with (
                patch.object(
                    module,
                    "NATIVE_GENERATION_ROOT",
                    native_root,
                ),
                patch.object(
                    module,
                    "DATABRIDGE_GENERATION_ROOT",
                    databridge_root,
                ),
                patch.object(
                    module,
                    "_ensure_private_directory",
                ),
                patch.object(
                    module,
                    "create_native_generation",
                    return_value=object(),
                ) as create_native,
                patch.object(
                    module,
                    "create_databridge_generation",
                    return_value=object(),
                ) as create_databridge,
            ):
                services.build_native_generation(
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    capture_not_after=capture_deadline,
                    source_contract_cutoff=contract_cutoff,
                )
                services.build_databridge_generation(
                    current=object(),
                    native_generation=object(),
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                )

        for create_call in (
            create_native.call_args,
            create_databridge.call_args,
        ):
            self.assertEqual(
                create_call.kwargs["max_total_bytes"],
                50 * 1024**3,
            )
            self.assertEqual(
                create_call.kwargs["min_free_bytes"],
                2 * 1024**3,
            )

    def test_generation_resources_require_explicit_approved_co_residency(
        self,
    ) -> None:
        availability = _GenerationAvailability(
            {
                ("native_export",),
                ("databridge_refresh",),
            }
        )

        with availability.occupy("native_export"):
            with self.assertRaisesRegex(
                RuntimeError,
                "not admitted",
            ):
                with availability.occupy("databridge_refresh"):
                    self.fail("unadmitted input-input pair must not start")
            _outcome, _done, active = availability.snapshot()
            self.assertEqual(active, ("native_export",))

    def test_generation_resources_allow_versioned_approved_pair(
        self,
    ) -> None:
        availability = _GenerationAvailability(
            {
                ("native_export",),
                ("databridge_refresh",),
                ("databridge_refresh", "native_export"),
            }
        )

        with availability.occupy("native_export"):
            with availability.occupy("databridge_refresh"):
                _outcome, _done, active = availability.snapshot()
                self.assertEqual(
                    active,
                    ("databridge_refresh", "native_export"),
                )

    def test_storage_hard_count_fails_when_no_db_candidate_is_reclaimable(
        self,
    ) -> None:
        from scheduler import daily_runtime as module

        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        services._policy = SimpleNamespace(
            generation_min_free_bytes=0,
            generation_max_total_bytes=10**9,
            generation_max_count=1,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            native_root = root / "native"
            native_root.mkdir(mode=0o700)
            (native_root / f"native-{'a' * 24}").mkdir()
            databridge_root = root / "databridge"
            databridge_root.mkdir(mode=0o700)
            refresh_config = SimpleNamespace(
                data_root=root / "current",
                runtime_root=root / "refresh-runtime",
            )
            with (
                patch.object(
                    module,
                    "NATIVE_GENERATION_ROOT",
                    native_root,
                ),
                patch.object(
                    module,
                    "DATABRIDGE_GENERATION_ROOT",
                    databridge_root,
                ),
                patch.object(
                    module.DataBridgeRefreshConfig,
                    "from_env",
                    return_value=refresh_config,
                ),
                patch.object(
                    module,
                    "resolve_reclaimable_generation_payloads",
                    return_value=(),
                ),
                patch.object(
                    module,
                    "delete_reclaimable_native_generation",
                ) as delete_native,
                patch.object(
                    module,
                    "delete_reclaimable_databridge_generation",
                ) as delete_databridge,
                self.assertRaisesRegex(OSError, "generation count"),
            ):
                services.maintain_generation_storage()

            delete_native.assert_not_called()
            delete_databridge.assert_not_called()
            self.assertTrue(
                (native_root / f"native-{'a' * 24}").is_dir()
            )

    def test_storage_validates_entire_reclaim_wave_before_deleting(
        self,
    ) -> None:
        from scheduler import daily_runtime as module

        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        services._policy = SimpleNamespace(
            generation_min_free_bytes=0,
            generation_max_total_bytes=10**9,
            generation_max_count=64,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            native_root = root / "native"
            native_root.mkdir(mode=0o700)
            databridge_root = root / "databridge"
            databridge_root.mkdir(mode=0o700)
            good_id = f"native-{'a' * 24}"
            unsafe_id = f"native-{'b' * 24}"
            candidates = (
                SimpleNamespace(
                    generation_id=good_id,
                    generation_type="native_source",
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    manifest_uri=str(
                        native_root / good_id / "manifest.json"
                    ),
                    manifest_sha256="a" * 64,
                ),
                SimpleNamespace(
                    generation_id=unsafe_id,
                    generation_type="native_source",
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    manifest_uri=str(root / "outside" / "manifest.json"),
                    manifest_sha256="b" * 64,
                ),
            )
            refresh_config = SimpleNamespace(
                data_root=root / "current",
                runtime_root=root / "refresh-runtime",
            )
            with (
                patch.object(
                    module,
                    "NATIVE_GENERATION_ROOT",
                    native_root,
                ),
                patch.object(
                    module,
                    "DATABRIDGE_GENERATION_ROOT",
                    databridge_root,
                ),
                patch.object(
                    module.DataBridgeRefreshConfig,
                    "from_env",
                    return_value=refresh_config,
                ),
                patch.object(
                    module,
                    "resolve_reclaimable_generation_payloads",
                    return_value=candidates,
                ),
                patch.object(
                    module,
                    "delete_reclaimable_native_generation",
                ) as delete_native,
                self.assertRaisesRegex(
                    RuntimeError,
                    "outside its exact storage identity",
                ),
            ):
                services.maintain_generation_storage()

            delete_native.assert_not_called()

    def test_two_worktrees_contend_on_same_machine_global_occurrence_lock(
        self,
    ) -> None:
        source = Path(__file__).resolve().parents[1] / "scheduler" / (
            "daily_runtime.py"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service_home = root / "service-home"
            service_home.mkdir()
            modules = []
            try:
                for suffix in ("a", "b"):
                    module_path = (
                        root
                        / f"worktree-{suffix}"
                        / "scheduler"
                        / "daily_runtime.py"
                    )
                    module_path.parent.mkdir(parents=True)
                    shutil.copy2(source, module_path)
                    module_name = f"_daily_runtime_worktree_{suffix}"
                    spec = importlib.util.spec_from_file_location(
                        module_name,
                        module_path,
                    )
                    assert spec is not None and spec.loader is not None
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    with patch(
                        "shared.daily_coordinator_mode.pwd",
                        SimpleNamespace(
                            getpwuid=lambda _uid: SimpleNamespace(
                                pw_dir=str(service_home),
                            )
                        ),
                        create=True,
                    ):
                        spec.loader.exec_module(module)
                    modules.append(module)

                first_services = modules[0].DefaultDailyRuntimeServices(
                    engine=create_engine("sqlite://")
                )
                second_services = modules[1].DefaultDailyRuntimeServices(
                    engine=create_engine("sqlite://")
                )
                first_lock = first_services.occurrence_lock(
                    date(2026, 7, 24)
                )
                second_lock = second_services.occurrence_lock(
                    date(2026, 7, 24)
                )

                self.assertEqual(first_lock.path, second_lock.path)
                first_lock.acquire()
                try:
                    first_inode = first_lock.path.stat().st_ino
                    with self.assertRaises(OccurrenceLockUnavailable):
                        second_lock.acquire()
                    self.assertEqual(
                        second_lock.path.stat().st_ino,
                        first_inode,
                    )
                finally:
                    second_lock.release()
                    first_lock.release()
            finally:
                for module in modules:
                    sys.modules.pop(module.__name__, None)

    def _dispatch_snapshot_with_frozen_v2_release(
        self,
        *,
        services: DefaultDailyRuntimeServices,
        generation_id: str,
        release_at: datetime,
    ):
        policy = services.load_policy()
        v2_scheme_id = min(
            (
                scheme_id
                for scheme_id, scheme in policy.schemes.items()
                if scheme.runtime_type == "blackbox_v2"
            ),
            key=lambda scheme_id: (
                policy.schemes[scheme_id].v2_release_offset_min,
                scheme_id,
            ),
        )
        summaries = []
        for item_id, (scheme_id, scheme) in enumerate(
            policy.schemes.items(),
            start=1,
        ):
            is_target = scheme_id == v2_scheme_id
            summaries.append(
                SimpleNamespace(
                    item=SimpleNamespace(
                        item_id=item_id,
                        base_scheme_id=scheme_id,
                        runtime_type=scheme.runtime_type,
                        state="PENDING" if is_target else "SUCCESS",
                        attempt_no=0,
                        failure_code=None,
                        input_generation_id=(
                            generation_id
                            if scheme.runtime_type == "blackbox_v2"
                            else "native-generation"
                        ),
                        release_at=release_at,
                    )
                )
            )
        return (
            policy,
            v2_scheme_id,
            SimpleNamespace(items=tuple(summaries)),
        )

    @patch(
        "scheduler.daily_runtime."
        "assert_daily_coordinator_epoch_matches_policy",
        return_value=SimpleNamespace(
            policy_payload=lambda: dict(TEST_COORDINATOR_EPOCH)
        ),
    )
    def test_capacity_candidate_fingerprint_is_frozen_with_policy(
        self,
        _epoch_fence,
    ) -> None:
        from scheduler.daily_policy import load_daily_policy
        from scheduler.discovery import discover_schemes

        policy = load_daily_policy(
            discovered=discover_schemes(strict=True),
        )
        fingerprint = "c" * 64
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        services.bind_capacity_admission(
            {"candidate_fingerprint": fingerprint}
        )
        frozen = _policy_payload(
            policy,
            daily_coordinator_epoch=TEST_COORDINATOR_EPOCH,
            capacity_candidate_fingerprint=fingerprint,
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                policy_version=policy.version,
                policy_json=frozen,
            )
        )

        services.validate_occurrence_policy(
            snapshot=snapshot,
            policy=policy,
        )
        self.assertEqual(
            frozen["capacity_candidate_fingerprint"],
            fingerprint,
        )
        self.assertEqual(
            frozen["times"]["databridge_readiness_guardrail"],
            "06:55",
        )
        self.assertEqual(
            frozen["generation_storage"],
            {
                "max_generation_count": 64,
                "max_total_bytes": 50 * 1024**3,
                "min_free_bytes": 2 * 1024**3,
            },
        )

        drifted_services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        drifted_services.bind_capacity_admission(
            {"candidate_fingerprint": "d" * 64}
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "policy payload differs",
        ):
            drifted_services.validate_occurrence_policy(
                snapshot=snapshot,
                policy=policy,
            )

        drifted_storage = copy.deepcopy(frozen)
        drifted_storage["generation_storage"][
            "max_generation_count"
        ] = 65
        with self.assertRaisesRegex(
            RuntimeError,
            "policy payload differs",
        ):
            services.validate_occurrence_policy(
                snapshot=SimpleNamespace(
                    occurrence=SimpleNamespace(
                        policy_version=policy.version,
                        policy_json=drifted_storage,
                    )
                ),
                policy=policy,
            )

    @patch(
        "scheduler.daily_runtime."
        "assert_daily_coordinator_epoch_matches_policy",
        return_value=SimpleNamespace(
            policy_payload=lambda: dict(TEST_COORDINATOR_EPOCH)
        ),
    )
    def test_frozen_hard_runtime_bound_drift_is_fail_closed(
        self,
        _epoch_fence,
    ) -> None:
        from scheduler.daily_policy import load_daily_policy
        from scheduler.discovery import discover_schemes

        policy = load_daily_policy(
            discovered=discover_schemes(strict=True),
        )
        fingerprint = "c" * 64
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        services.bind_capacity_admission(
            {"candidate_fingerprint": fingerprint}
        )
        frozen = _policy_payload(
            policy,
            daily_coordinator_epoch=TEST_COORDINATOR_EPOCH,
            capacity_candidate_fingerprint=fingerprint,
        )
        drifted = copy.deepcopy(frozen)
        scheme = next(
            row
            for row in drifted["schemes"]
            if row["scheme_id"] == "t1_daily"
        )
        self.assertEqual(
            scheme["admitted_hard_runtime_sec"],
            600,
        )
        scheme["admitted_hard_runtime_sec"] = 30

        with self.assertRaisesRegex(
            RuntimeError,
            "policy payload differs",
        ):
            services.validate_occurrence_policy(
                snapshot=SimpleNamespace(
                    occurrence=SimpleNamespace(
                        policy_version=policy.version,
                        policy_json=drifted,
                    )
                ),
                policy=policy,
            )

    def test_dispatch_uses_db_frozen_v2_release_not_manifest_sealed_at(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        frozen_generation_id = "databridge-frozen-generation"
        policy, v2_scheme_id, snapshot = (
            self._dispatch_snapshot_with_frozen_v2_release(
                services=services,
                generation_id=frozen_generation_id,
                # DB DATETIME(6) 为 naive UTC；即上海 07:00。
                release_at=datetime(2026, 7, 23, 23, 0),
            )
        )
        cases = (
            (
                "manifest_early",
                datetime(2026, 7, 24, 6, 0, tzinfo=SHANGHAI),
                datetime(2026, 7, 24, 6, 59, tzinfo=SHANGHAI),
                "WAIT_RELEASE",
            ),
            (
                "manifest_late",
                datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
                datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
                "DISPATCH",
            ),
        )

        for label, manifest_sealed_at, now, expected_action in cases:
            with self.subTest(label=label):
                generation = SimpleNamespace(
                    generation_id=frozen_generation_id,
                    business_date="2026-07-24",
                    sealed_at=manifest_sealed_at,
                )
                decisions = services.dispatch_decisions(
                    business_date=date(2026, 7, 24),
                    snapshot=snapshot,
                    native_generation=None,
                    databridge_generation=generation,
                    running_scheme_ids=(),
                    running_resource_classes=(),
                    now=now,
                )
                by_id = {
                    decision.scheme_id: decision
                    for decision in decisions
                }

                self.assertEqual(
                    by_id[v2_scheme_id].action,
                    expected_action,
                )
                self.assertEqual(
                    by_id[v2_scheme_id].release_at,
                    datetime(
                        2026,
                        7,
                        24,
                        7,
                        0,
                        tzinfo=SHANGHAI,
                    ),
                )

    def test_dispatch_rejects_generation_not_bound_to_frozen_v2_items(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        _policy, v2_scheme_id, snapshot = (
            self._dispatch_snapshot_with_frozen_v2_release(
                services=services,
                generation_id="databridge-frozen-generation",
                release_at=datetime(2026, 7, 23, 23, 0),
            )
        )
        drifted_generation = SimpleNamespace(
            generation_id="databridge-drifted-generation",
            business_date="2026-07-24",
            sealed_at=datetime(
                2026,
                7,
                24,
                6,
                0,
                tzinfo=SHANGHAI,
            ),
        )

        decisions = services.dispatch_decisions(
            business_date=date(2026, 7, 24),
            snapshot=snapshot,
            native_generation=None,
            databridge_generation=drifted_generation,
            running_scheme_ids=(),
            running_resource_classes=(),
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        by_id = {
            decision.scheme_id: decision
            for decision in decisions
        }

        self.assertEqual(
            by_id[v2_scheme_id].action,
            "WAIT_GENERATION",
        )
        self.assertEqual(
            by_id[v2_scheme_id].reason,
            "DATABRIDGE_GENERATION_MISMATCH",
        )

    def test_runtime_service_factory_binds_current_admission(
        self,
    ) -> None:
        admission = {
            "status": "ADMITTED",
            "candidate_fingerprint": "e" * 64,
        }
        with patch(
            "scheduler.daily_runtime."
            "_require_production_entry_authority",
            return_value=admission,
        ) as authority:
            services, dispose = _runtime_services(
                None,
                algo_env="forecast_env",
                verify_current_capacity=True,
            )
        try:
            authority.assert_called_once_with(
                engine=services.engine,
                verify_current=True,
                algo_env="forecast_env",
            )
            self.assertEqual(
                services._capacity_candidate_fingerprint,
                "e" * 64,
            )
        finally:
            dispose()

    def test_revalidation_rejects_candidate_change_after_freeze(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            algo_env="forecast_env",
            engine=create_engine("sqlite://"),
        )
        services.bind_capacity_admission(
            {"candidate_fingerprint": "e" * 64}
        )
        with (
            patch(
                "scheduler.daily_runtime."
                "_require_production_entry_authority",
                return_value={"candidate_fingerprint": "f" * 64},
            ) as authority,
            self.assertRaisesRegex(
                RuntimeError,
                "candidate_fingerprint changed",
            ),
        ):
            services.revalidate_capacity_admission()

        authority.assert_called_once_with(
            engine=services.engine,
            verify_current=True,
            algo_env="forecast_env",
        )

    def test_capacity_revalidation_ignores_checked_at_but_rejects_evidence_drift(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        base = {
            "candidate_fingerprint": "e" * 64,
            "decision_id": "decision-1",
            "evidence_sha256": "a" * 64,
            "collector_signer_sha256": "b" * 64,
            "operator_signer_sha256": "c" * 64,
            "checked_at": "2026-07-24T00:00:00+00:00",
        }
        services.bind_capacity_admission(base)
        services.bind_capacity_admission(
            {
                **base,
                "checked_at": "2026-07-24T00:00:01+00:00",
            }
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "stable identity changed",
        ):
            services.bind_capacity_admission(
                {
                    **base,
                    "evidence_sha256": "d" * 64,
                    "checked_at": "2026-07-24T00:00:02+00:00",
                }
            )

    def test_v2_guardrail_adapter_delegates_time_sampling_to_repository(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        expected = SimpleNamespace(status="ON_TIME")

        with patch(
            "scheduler.daily_runtime.evaluate_schedule_item_start_sla",
            return_value=expected,
        ) as evaluate:
            result = services.evaluate_v2_start(
                item_id=41,
                evaluated_at=datetime(
                    2026,
                    7,
                    24,
                    7,
                    45,
                    tzinfo=SHANGHAI,
                ),
            )

        self.assertIs(result, expected)
        evaluate.assert_called_once_with(services.engine, item_id=41)

    def test_target_sla_adapter_delegates_time_sampling_to_repository(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        expected = SimpleNamespace(status="MET")

        with patch(
            "scheduler.daily_runtime.evaluate_schedule_occurrence_target_sla",
            return_value=expected,
        ) as evaluate:
            result = services.evaluate_target_sla(
                occurrence_id=17,
                evaluated_at=datetime(
                    2026,
                    7,
                    24,
                    8,
                    0,
                    tzinfo=SHANGHAI,
                ),
            )

        self.assertIs(result, expected)
        evaluate.assert_called_once_with(
            services.engine,
            occurrence_id=17,
        )

    def test_visibility_reconciliation_adapter_delegates_to_repository(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )

        with patch(
            "scheduler.daily_runtime."
            "reconcile_schedule_occurrence_visibility_receipts",
            return_value=2,
            create=True,
        ) as reconcile:
            result = services.reconcile_occurrence_visibility_receipts(
                occurrence_id=17,
            )

        self.assertEqual(result, 2)
        reconcile.assert_called_once_with(
            services.engine,
            occurrence_id=17,
        )

    def test_expiry_adapter_delegates_time_sampling_to_repository(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )

        with patch(
            "scheduler.daily_runtime.expire_schedule_items",
            return_value=3,
        ) as expire:
            result = services.expire_items(
                occurrence_id=23,
                evaluated_at=datetime(
                    2026,
                    7,
                    24,
                    8,
                    30,
                    tzinfo=SHANGHAI,
                ),
            )

        self.assertEqual(result, 3)
        expire.assert_called_once_with(
            services.engine,
            occurrence_id=23,
        )

    def test_cleanup_adapter_passes_fenced_execution_token(self) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        item = SimpleNamespace(base_scheme_id="native")
        fenced_attempt = SimpleNamespace(
            execution_token="attempt_token_41",
            process_group_id=502,
            process_id=503,
        )

        with patch(
            "scheduler.daily_runtime._terminate_verified_orphan",
            return_value=True,
        ) as terminate:
            cleaned = services.cleanup_fenced_attempt(
                item=item,
                fenced_attempt=fenced_attempt,
            )

        self.assertIs(cleaned, True)
        terminate.assert_called_once_with(
            scheme_id="native",
            execution_token="attempt_token_41",
            process_group_id=502,
            process_id=503,
        )

    def test_heartbeat_stamps_ledger_coordinator_mode(self) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )

        with patch(
            "scheduler.daily_runtime.upsert_scheduler_heartbeat",
            return_value=SimpleNamespace(),
        ) as upsert, patch(
            "scheduler.daily_runtime."
            "require_current_daily_coordinator_identity",
            return_value=SimpleNamespace(
                mode="ledger",
                policy_payload=lambda: dict(TEST_COORDINATOR_EPOCH),
            ),
        ):
            services.heartbeat(
                occurrence_id=41,
                state="RUNNING",
                details={"phase": "dispatch"},
            )

        self.assertEqual(
            upsert.call_args.kwargs["details"],
            {
                "phase": "dispatch",
                "coordinator_mode": "ledger",
                "daily_coordinator_epoch": TEST_COORDINATOR_EPOCH,
            },
        )

    def test_heartbeat_tick_preserves_watchdog_eta_until_completion(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                completion_state="RUNNING",
                expected_target_count=25,
            ),
            actual_accepted_target_count=10,
        )
        previous = SimpleNamespace(
            state="WATCHDOG_PROGRESS",
            occurrence_id=41,
            details={
                "accepted_target_count": 0,
                "expected_target_count": 25,
                "eta_overline": True,
            },
        )

        with (
            patch.object(
                services,
                "now",
                return_value=datetime(
                    2026,
                    7,
                    24,
                    7,
                    1,
                    tzinfo=SHANGHAI,
                ),
            ),
            patch.object(
                services,
                "find_occurrence_id",
                return_value=41,
            ),
            patch.object(
                services,
                "read_snapshot",
                return_value=snapshot,
            ),
            patch(
                "scheduler.daily_runtime.read_scheduler_heartbeat",
                return_value=previous,
                create=True,
            ),
            patch.object(services, "heartbeat") as heartbeat,
        ):
            result = services.heartbeat_tick("2026-07-24")

        self.assertEqual(result["state"], "WATCHDOG_PROGRESS")
        self.assertIs(result["eta_overline"], True)
        heartbeat.assert_called_once()
        self.assertEqual(
            heartbeat.call_args.kwargs["state"],
            "WATCHDOG_PROGRESS",
        )

    def test_heartbeat_tick_preserves_native_readiness_wait_details(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                completion_state="RUNNING",
                expected_target_count=25,
            ),
            items=(
                SimpleNamespace(
                    item=SimpleNamespace(
                        runtime_type="native_adapter",
                        input_generation_id=None,
                    )
                ),
            ),
            actual_accepted_target_count=0,
        )
        previous = SimpleNamespace(
            state="WAITING_NATIVE_READINESS",
            occurrence_id=41,
            details={
                "feature_date": "2026-07-23",
                "missing_requirements": [
                    "daily_target:TB5YWI0C",
                ],
            },
        )

        with (
            patch.object(
                services,
                "now",
                return_value=datetime(
                    2026,
                    7,
                    24,
                    6,
                    31,
                    tzinfo=SHANGHAI,
                ),
            ),
            patch.object(
                services,
                "find_occurrence_id",
                return_value=41,
            ),
            patch.object(
                services,
                "read_snapshot",
                return_value=snapshot,
            ),
            patch(
                "scheduler.daily_runtime.read_scheduler_heartbeat",
                return_value=previous,
            ),
            patch.object(services, "heartbeat") as heartbeat,
        ):
            result = services.heartbeat_tick("2026-07-24")

        self.assertEqual(result["state"], "WAITING_NATIVE_READINESS")
        self.assertEqual(result["feature_date"], "2026-07-23")
        self.assertEqual(
            result["missing_requirements"],
            ["daily_target:TB5YWI0C"],
        )
        self.assertEqual(
            heartbeat.call_args.kwargs["state"],
            "WAITING_NATIVE_READINESS",
        )

    def test_heartbeat_tick_preserves_calendar_wait_without_occurrence(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        previous = SimpleNamespace(
            state="WAITING_NATIVE_READINESS",
            occurrence_id=None,
            details={
                "business_date": "2026-07-24",
                "missing_requirements": [
                    "calendar:business_date",
                ],
            },
        )

        with (
            patch.object(
                services,
                "now",
                return_value=datetime(
                    2026,
                    7,
                    24,
                    6,
                    31,
                    tzinfo=SHANGHAI,
                ),
            ),
            patch.object(
                services,
                "find_occurrence_id",
                return_value=None,
            ),
            patch(
                "scheduler.daily_runtime.read_scheduler_heartbeat",
                return_value=previous,
            ),
            patch.object(services, "heartbeat") as heartbeat,
        ):
            result = services.heartbeat_tick("2026-07-24")

        self.assertEqual(result["state"], "WAITING_NATIVE_READINESS")
        self.assertEqual(
            result["missing_requirements"],
            ["calendar:business_date"],
        )
        self.assertEqual(
            heartbeat.call_args.kwargs["state"],
            "WAITING_NATIVE_READINESS",
        )

    def test_heartbeat_tick_preserves_zero_progress_until_progress_occurs(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                completion_state="RUNNING",
                expected_target_count=25,
            ),
            actual_accepted_target_count=0,
        )
        previous = SimpleNamespace(
            state="WATCHDOG_PROGRESS",
            occurrence_id=41,
            details={
                "accepted_target_count": 0,
                "expected_target_count": 25,
                "eta_overline": False,
            },
        )

        with (
            patch.object(
                services,
                "now",
                return_value=datetime(
                    2026,
                    7,
                    24,
                    7,
                    1,
                    tzinfo=SHANGHAI,
                ),
            ),
            patch.object(
                services,
                "find_occurrence_id",
                return_value=41,
            ),
            patch.object(
                services,
                "read_snapshot",
                return_value=snapshot,
            ),
            patch(
                "scheduler.daily_runtime.read_scheduler_heartbeat",
                return_value=previous,
            ),
            patch.object(services, "heartbeat") as heartbeat,
        ):
            result = services.heartbeat_tick("2026-07-24")

        self.assertEqual(result["state"], "WATCHDOG_PROGRESS")
        self.assertEqual(result["accepted_target_count"], 0)
        heartbeat.assert_called_once()

    def test_unbound_building_lookup_excludes_bound_generation(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_input_generations (
                        generation_id TEXT PRIMARY KEY,
                        generation_type TEXT NOT NULL,
                        business_date TEXT NOT NULL,
                        state TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_items (
                        item_id INTEGER PRIMARY KEY,
                        input_generation_id TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_input_generations
                        (generation_id, generation_type, business_date, state)
                    VALUES
                        ('native-stuck', 'native_source',
                         '2026-07-24', 'BUILDING'),
                        ('databridge-bound', 'databridge_v1',
                         '2026-07-24', 'BUILDING'),
                        ('old-stuck', 'native_source',
                         '2026-07-23', 'BUILDING'),
                        ('native-sealed', 'native_source',
                         '2026-07-24', 'SEALED')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_items
                        (item_id, input_generation_id)
                    VALUES (1, 'databridge-bound')
                    """
                )
            )
        services = DefaultDailyRuntimeServices(engine=engine)

        generations = services.find_unbound_building_generations(
            business_date=date(2026, 7, 24),
        )

        self.assertEqual(
            generations,
            (
                {
                    "generation_id": "native-stuck",
                    "generation_type": "native_source",
                },
            ),
        )

    def test_native_recovery_prefers_exact_db_fence_over_filesystem_scan(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        fence = {
            "generation_id": "native-fenced",
            "generation_type": "native_source",
        }
        context = SimpleNamespace(generation_id="native-fenced")

        with (
            patch.object(
                services,
                "find_generation_fences",
                return_value=(fence,),
            ),
            patch(
                "scheduler.daily_runtime._ensure_private_directory",
            ),
            patch(
                "scheduler.daily_runtime._open_native_fence",
                return_value=context,
            ) as opener,
            patch(
                "scheduler.daily_runtime."
                "_validate_generation_fence_identity",
            ) as validate,
            patch(
                "scheduler.daily_runtime."
                "find_published_native_generation",
                side_effect=AssertionError(
                    "DB fence must ignore unrelated final directories"
                ),
            ),
        ):
            recovered = services.recover_native_generation(
                business_date="2026-07-24",
                feature_date="2026-07-23",
            )

        self.assertIs(recovered, context)
        opener.assert_called_once_with(
            fence,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        validate.assert_called_once_with(context, fence)

    def test_native_recovery_rejects_multiple_db_fences_without_scanning(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        with (
            patch.object(
                services,
                "find_generation_fences",
                return_value=(
                    {
                        "generation_id": "native-a",
                        "generation_type": "native_source",
                    },
                    {
                        "generation_id": "native-b",
                        "generation_type": "native_source",
                    },
                ),
            ),
            patch(
                "scheduler.daily_runtime."
                "find_published_native_generation",
            ) as scanner,
        ):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                services.recover_native_generation(
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                )
        scanner.assert_not_called()

    def test_native_recovery_without_db_fence_does_not_adopt_disk_final(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        with (
            patch.object(
                services,
                "find_generation_fences",
                return_value=(),
            ),
            patch(
                "scheduler.daily_runtime."
                "find_published_native_generation",
                return_value=SimpleNamespace(generation_id="native-rogue"),
            ) as scanner,
        ):
            recovered = services.recover_native_generation(
                business_date="2026-07-24",
                feature_date="2026-07-23",
            )

        self.assertIsNone(recovered)
        scanner.assert_not_called()

    def test_databridge_recovery_without_db_fence_does_not_adopt_disk_final(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        native = SimpleNamespace(
            generation_id="native-expected",
            manifest_sha256="a" * 64,
        )
        with (
            patch.object(
                services,
                "find_generation_fences",
                return_value=(),
            ),
            patch(
                "scheduler.daily_runtime."
                "find_published_databridge_generation",
                return_value=SimpleNamespace(
                    generation_id="databridge-rogue",
                    native_generation_id="native-expected",
                    native_manifest_sha256="a" * 64,
                ),
            ) as scanner,
        ):
            recovered = services.recover_databridge_generation(
                business_date="2026-07-24",
                feature_date="2026-07-23",
                native_generation=native,
            )

        self.assertIsNone(recovered)
        scanner.assert_not_called()

    def test_databridge_recovery_rejects_parent_generation_drift(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        native = SimpleNamespace(
            generation_id="native-expected",
            manifest_sha256="a" * 64,
        )
        drifted = SimpleNamespace(
            generation_id="databridge-final",
            native_generation_id="native-other",
            native_manifest_sha256="b" * 64,
        )
        with (
            patch.object(
                services,
                "find_generation_fences",
                return_value=(
                    {
                        "generation_id": "databridge-final",
                        "generation_type": "databridge_v1",
                    },
                ),
            ),
            patch(
                "scheduler.daily_runtime._open_databridge_fence",
                return_value=drifted,
            ),
            patch(
                "scheduler.daily_runtime."
                "_validate_generation_fence_identity",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "parent"):
                services.recover_databridge_generation(
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    native_generation=native,
                )

    def test_read_frozen_inputs_uses_occurrence_feature_date_without_calendar(
        self,
    ) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_item_targets (
                        occurrence_id INTEGER NOT NULL,
                        registry_scheme_id TEXT NOT NULL,
                        target_date TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_item_targets
                        (occurrence_id, registry_scheme_id, target_date)
                    VALUES (41, 'native__h1__5Y', '2026-07-24')
                    """
                )
            )
        services = DefaultDailyRuntimeServices(engine=engine)
        item = SimpleNamespace(
            base_scheme_id="native",
            scheme_version="native-v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
            cache_group="native",
            resource_class="native_light",
            internal_workers=1,
            release_offset_minutes=0,
            release_at=datetime(2026, 7, 23, 22, 30),
            deadline_at=datetime(2026, 7, 24, 0, 0),
            input_generation_id=None,
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                policy_json={"version": "frozen"},
            ),
            items=(SimpleNamespace(item=item),),
        )
        policy = SimpleNamespace(
            schemes={"native": SimpleNamespace()},
        )

        with (
            patch(
                "scheduler.daily_runtime."
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch(
                "scheduler.daily_runtime."
                "CalendarService.previous_trading_day",
                side_effect=AssertionError(
                    "existing occurrence must not query live calendar"
                ),
            ),
        ):
            inputs = services.read_frozen_inputs(
                occurrence_id=41,
                policy=policy,
            )

        self.assertEqual(inputs.feature_date, "2026-07-23")

    def test_create_occurrence_persists_explicit_feature_date(self) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        policy = SimpleNamespace(
            timezone="Asia/Shanghai",
            version="policy-v1",
            sla_deadline=time(8, 0),
            recovery_cutoff=time(8, 30),
        )
        inputs = OccurrenceInputs(
            feature_date="2026-07-23",
            target_dates={"native__h1__5Y": "2026-07-24"},
            item_policy_by_base={"native": {"scheme_version": "v1"}},
            policy_json={"version": "policy-v1"},
        )
        with patch(
            "scheduler.daily_runtime.create_schedule_occurrence",
            return_value=41,
        ) as create:
            occurrence_id = services.create_occurrence(
                business_date=date(2026, 7, 24),
                inputs=inputs,
                policy=policy,
            )

        self.assertEqual(occurrence_id, 41)
        self.assertEqual(
            create.call_args.kwargs["feature_date"],
            "2026-07-23",
        )

    def test_bound_generation_resolution_rejects_partial_runtime_binding(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        snapshot = SimpleNamespace(
            items=(
                SimpleNamespace(
                    item=SimpleNamespace(
                        runtime_type="native_adapter",
                        input_generation_id="native-gen",
                    )
                ),
                SimpleNamespace(
                    item=SimpleNamespace(
                        runtime_type="native_adapter",
                        input_generation_id=None,
                    )
                ),
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "partial runtime generation binding",
        ):
            services.resolve_bound_generations(
                snapshot=snapshot,
                inputs=OccurrenceInputs(
                    feature_date="2026-07-23",
                    target_dates={},
                    item_policy_by_base={},
                    policy_json={},
                ),
                policy=SimpleNamespace(),
            )

    def test_bound_generation_resolution_rehashes_each_generation_once_per_call(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        native_envelope = SimpleNamespace(
            generation_id="native-shared",
            generation_type="native_source",
            manifest_uri="/frozen/native/manifest.json",
            manifest_sha256="a" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        databridge_envelope = SimpleNamespace(
            generation_id="databridge-shared",
            generation_type="databridge_v1",
            manifest_uri="/frozen/databridge/manifest.json",
            manifest_sha256="b" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        native_context = SimpleNamespace(
            generation_id="native-shared",
            manifest_sha256="a" * 64,
            feature_date="2026-07-23",
        )
        databridge_context = SimpleNamespace(
            generation_id="databridge-shared",
            native_generation_id="native-shared",
            native_manifest_sha256="a" * 64,
        )
        items = tuple(
            SimpleNamespace(
                item=SimpleNamespace(
                    item_id=item_id,
                    runtime_type="native_adapter",
                    input_generation_id="native-shared",
                )
            )
            for item_id in range(1, 18)
        ) + tuple(
            SimpleNamespace(
                item=SimpleNamespace(
                    item_id=item_id,
                    runtime_type="blackbox_v2",
                    input_generation_id="databridge-shared",
                )
            )
            for item_id in range(18, 22)
        )

        def envelope_for_item(_engine, *, item_id: int):
            if item_id < 18:
                return SimpleNamespace(
                    generation=native_envelope,
                    calendar_generation=native_envelope,
                )
            return SimpleNamespace(
                generation=databridge_envelope,
                calendar_generation=native_envelope,
            )

        with (
            patch(
                "scheduler.daily_runtime.read_schedule_execution_envelope",
                side_effect=envelope_for_item,
            ) as read_envelope,
            patch(
                "scheduler.daily_runtime._open_native_envelope",
                return_value=native_context,
            ) as open_native,
            patch(
                "scheduler.daily_runtime.open_databridge_generation",
                return_value=databridge_context,
            ) as open_databridge,
        ):
            for _ in range(2):
                resolved_native, resolved_databridge = (
                    services.resolve_bound_generations(
                        snapshot=SimpleNamespace(items=items),
                        inputs=OccurrenceInputs(
                            feature_date="2026-07-23",
                            target_dates={},
                            item_policy_by_base={},
                            policy_json={},
                        ),
                        policy=SimpleNamespace(),
                    )
                )
                self.assertIs(resolved_native, native_context)
                self.assertIs(resolved_databridge, databridge_context)

        self.assertEqual(read_envelope.call_count, 42)
        self.assertEqual(open_native.call_count, 2)
        self.assertEqual(open_databridge.call_count, 2)

    def test_refresh_databridge_reopens_only_the_exact_published_generation(
        self,
    ) -> None:
        engine = create_engine("sqlite://")
        services = DefaultDailyRuntimeServices(engine=engine)
        services._policy = SimpleNamespace(
            recovery_cutoff=time(8, 30),
            timezone="Asia/Shanghai",
        )
        config = SimpleNamespace()
        client_config = SimpleNamespace()
        refresh_result = SimpleNamespace(
            state={
                "generation_id": "refresh-owned",
                "business_digest": "a" * 64,
            }
        )
        current = SimpleNamespace(
            state=refresh_result.state,
            dataset=SimpleNamespace(),
        )
        continuity_authority = object()

        with (
            patch(
                "scheduler.daily_runtime.DataBridgeRefreshConfig.from_env",
                return_value=config,
            ),
            patch(
                "scheduler.daily_runtime.DataBridgeClientConfig.from_env",
                return_value=client_config,
            ),
            patch(
                "scheduler.daily_runtime.DataBridgeClient",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.daily_runtime.run_full_refresh",
                return_value=refresh_result,
            ) as refresh,
            patch(
                "scheduler.daily_runtime."
                "resolve_databridge_continuity_authority_from_engine",
                return_value=continuity_authority,
                create=True,
            ) as resolve_authority,
            patch(
                "scheduler.daily_runtime.check_current_dataset",
                return_value=current,
            ) as check_current,
        ):
            actual = services.refresh_databridge(
                business_date="2026-07-24",
                feature_date="2026-07-23",
                publication_capability=(
                    __import__(
                        "shared.data_bridge.refresh",
                        fromlist=[
                            "DailyCoordinatorPublicationCapability"
                        ],
                    ).DailyCoordinatorPublicationCapability(
                        occurrence_id=41,
                        business_date="2026-07-24",
                        epoch=1,
                        mode="ledger",
                        record_sha256="1" * 64,
                    )
                ),
            )

        self.assertIs(actual, current)
        resolve_authority.assert_called_once_with(
            config,
            feature_date="2026-07-23",
            engine=engine,
        )
        self.assertIs(
            refresh.call_args.kwargs["continuity_authority"],
            continuity_authority,
        )
        self.assertEqual(
            check_current.call_args.kwargs["expected_generation_id"],
            "refresh-owned",
        )
        self.assertEqual(
            check_current.call_args.kwargs["expected_business_digest"],
            "a" * 64,
        )
        self.assertEqual(
            refresh.call_args.kwargs["deadline_at"],
            datetime(
                2026,
                7,
                24,
                8,
                30,
                tzinfo=SHANGHAI,
            ),
        )


class DailyRuntimeOrphanIdentityTests(unittest.TestCase):
    def _blackbox_script(self, scheme_id: str) -> Path:
        return (
            Path(__file__).resolve().parents[1]
            / "schemes"
            / scheme_id
            / "delivery"
            / f"{scheme_id}.py"
        )

    def test_native_group_allows_auxiliary_members_with_exact_token(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                503,
                502,
                "conda run -n forecast_env python -m "
                "scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
            (
                504,
                502,
                "bash /opt/conda/bin/activate "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]

        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=502,
                process_id=503,
            )

        self.assertIs(cleaned, True)
        killpg.assert_called_once_with(502, 15)

    def test_group_with_different_token_fails_closed(self) -> None:
        rows = [
            (
                503,
                502,
                "python -m scheduler.scheme_runner --scheme-id native "
                "BOND_SCHEDULE_EXECUTION_TOKEN=another_attempt",
            ),
        ]

        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token="attempt_token_41",
                process_group_id=502,
                process_id=503,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_unsafe_token_fails_closed_even_when_process_is_gone(
        self,
    ) -> None:
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=False,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token="attempt token with spaces",
                process_group_id=502,
                process_id=503,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_missing_process_identity_with_no_token_match_is_clean(
        self,
    ) -> None:
        services = DefaultDailyRuntimeServices(
            engine=create_engine("sqlite://")
        )
        with patch(
            "scheduler.daily_runtime._process_rows",
            return_value=[],
        ) as process_rows:
            cleaned = services.cleanup_fenced_attempt(
                item=SimpleNamespace(base_scheme_id="native"),
                fenced_attempt=SimpleNamespace(
                    execution_token="attempt_token_41",
                    process_group_id=None,
                    process_id=None,
                ),
            )

        self.assertIs(cleaned, True)
        process_rows.assert_called_once_with()

    def test_missing_process_identity_cleans_unique_owned_group(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
            (
                8504,
                8502,
                f"bash helper BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime.os.getpgrp",
                return_value=9000,
            ),
            patch(
                "scheduler.daily_runtime.os.getpid",
                return_value=9001,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=None,
                process_id=None,
            )

        self.assertIs(cleaned, True)
        killpg.assert_called_once_with(8502, 15)

    def test_lookalike_blackbox_script_path_fails_closed(self) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "/runtime/python /tmp/native.py predict "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_current_pid_is_rejected_even_with_a_different_group(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime.os.getpid",
                return_value=9001,
            ),
            patch(
                "scheduler.daily_runtime.os.getpgrp",
                return_value=9002,
            ),
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=9001,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_unsafe_scheme_id_fails_closed_before_process_lookup(
        self,
    ) -> None:
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists"
            ) as group_exists,
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=[],
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="../native",
                execution_token="attempt_token_41",
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        group_exists.assert_not_called()
        killpg.assert_not_called()

    def test_pid_only_cleanup_requires_exact_execution_token(
        self,
    ) -> None:
        rows = [
            (
                8503,
                8503,
                "python -m scheduler.scheme_runner --scheme-id native "
                "BOND_SCHEDULE_EXECUTION_TOKEN=another_attempt",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.kill") as kill,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token="attempt_token_41",
                process_group_id=None,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        kill.assert_not_called()

    def test_pid_only_cleanup_rejects_current_process_group(self) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime.os.getpgrp",
                return_value=8502,
            ),
            patch(
                "scheduler.daily_runtime._process_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.kill") as kill,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=None,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        kill.assert_not_called()

    def test_pid_only_cleanup_terminates_the_verified_process_group(
        self,
    ) -> None:
        token = "attempt_token_41"
        process_row = (
            8503,
            8502,
            "python -m scheduler.scheme_runner --scheme-id native "
            f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
        )
        group_rows = [
            process_row,
            (
                8504,
                8502,
                f"bash helper BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                side_effect=[[process_row], group_rows],
            ) as process_rows,
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.kill") as kill,
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=None,
                process_id=8503,
            )

        self.assertIs(cleaned, True)
        self.assertEqual(
            process_rows.call_args_list,
            [
                unittest.mock.call(process_id=8503),
                unittest.mock.call(process_group_id=8502),
            ],
        )
        kill.assert_not_called()
        killpg.assert_called_once_with(8502, 15)

    def test_process_scan_explicitly_requests_environment_without_output(
        self,
    ) -> None:
        sensitive_row = (
            "8503 8502 python -m scheduler.scheme_runner "
            "--scheme-id native "
            "BOND_SCHEDULE_EXECUTION_TOKEN=attempt_token_41\n"
        )
        completed = SimpleNamespace(stdout=sensitive_row)

        with (
            patch(
                "scheduler.daily_runtime.subprocess.run",
                return_value=completed,
            ) as run,
            patch("builtins.print") as print_output,
        ):
            rows = _process_rows()

        self.assertEqual(
            rows,
            [
                (
                    8503,
                    8502,
                    sensitive_row.split(maxsplit=2)[2].strip(),
                )
            ],
        )
        command = run.call_args.args[0]
        self.assertIn("-E", command)
        self.assertIn("-ww", command)
        self.assertIs(run.call_args.kwargs["capture_output"], True)
        print_output.assert_not_called()

    def test_registered_pid_reused_outside_group_fails_closed(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_exists",
                return_value=True,
            ) as process_exists,
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=8603,
            )

        self.assertIs(cleaned, False)
        process_exists.assert_called_once_with(8603)
        killpg.assert_not_called()

    def test_blackbox_delivery_script_is_a_scheme_specific_member(
        self,
    ) -> None:
        scheme_id = "one_y_t5_liq_excess_a_v1"
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                f"/runtime/python {self._blackbox_script(scheme_id)} "
                "predict --request /tmp/request.json "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
            (
                8504,
                8502,
                f"bash helper BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=True,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id=scheme_id,
                execution_token=token,
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, True)
        killpg.assert_called_once_with(8502, 15)

    def test_token_scan_with_multiple_process_groups_is_ambiguous(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
            (
                8603,
                8602,
                f"bash leaked BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=None,
                process_id=None,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_group_member_without_exact_token_fails_closed(self) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
            (8504, 8502, "bash helper"),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_group_without_scheme_specific_member_fails_closed(
        self,
    ) -> None:
        token = "attempt_token_41"
        rows = [
            (
                8503,
                8502,
                f"bash helper BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                return_value=rows,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        killpg.assert_not_called()

    def test_group_identity_is_rechecked_before_sigkill(self) -> None:
        token = "attempt_token_41"
        owned_rows = [
            (
                8503,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                f"BOND_SCHEDULE_EXECUTION_TOKEN={token}",
            ),
        ]
        reused_rows = [
            (
                8603,
                8502,
                "python -m scheduler.scheme_runner --scheme-id native "
                "BOND_SCHEDULE_EXECUTION_TOKEN=another_attempt",
            ),
        ]
        with (
            patch(
                "scheduler.daily_runtime._process_group_exists",
                return_value=True,
            ),
            patch(
                "scheduler.daily_runtime._process_rows",
                side_effect=[owned_rows, reused_rows],
            ),
            patch(
                "scheduler.daily_runtime._wait_until_gone",
                return_value=False,
            ),
            patch("scheduler.daily_runtime.os.killpg") as killpg,
        ):
            cleaned = _terminate_verified_orphan(
                scheme_id="native",
                execution_token=token,
                process_group_id=8502,
                process_id=8503,
            )

        self.assertIs(cleaned, False)
        self.assertEqual(killpg.call_args_list, [unittest.mock.call(8502, 15)])


class _FakeLock:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def __enter__(self):
        return self.acquire()

    def acquire(self):
        self._events.append("lock:enter")
        return self

    def __exit__(self, *_args):
        self.release()

    def release(self):
        self._events.append("lock:exit")


class _FakeServices:
    def __init__(self, *, now: datetime) -> None:
        self.current_time = now
        self.events: list[object] = []
        self.states = {"native": "PENDING", "v2": "PENDING"}
        self.item_sla_status = {
            "native": "PENDING",
            "v2": "PENDING",
        }
        self.sla_outcome = "PENDING"
        self.attempts = Counter()
        self.bindings: dict[int, str] = {}
        self.failures: list[tuple[int, str]] = []
        self.heartbeat_calls: list[dict[str, object]] = []
        self.alert_calls: list[dict[str, object]] = []
        self._generation_barrier = Barrier(2)
        self._generation_lock = Lock()
        self.generation_running = 0
        self.max_generation_running = 0
        self.existing_occurrence_id: int | None = None
        self.unbound_building_generations: tuple[object, ...] = ()
        self.recovered_native_generation = None
        self.recovered_databridge_generation = None
        self.databridge_sealed_at: datetime | None = None
        self.native_recovery_error: Exception | None = None
        self.databridge_recovery_error: Exception | None = None
        self.visibility_reconcile_error: Exception | None = None
        self.capacity_revalidation_error: Exception | None = None
        self.generation_storage_error: Exception | None = None
        self.trading_day_status: bool | None = True
        self.occurrence_input_error: Exception | None = None
        self.native_readiness_ready = True
        self.native_readiness_missing: tuple[str, ...] = ()
        self.last_publication_capability = None
        native_policy = SimpleNamespace(
            scheme_id="native",
            runtime_type="native_adapter",
            task_type="T+1",
            horizon=1,
            target_tenors=("5Y",),
            input_compatibility="generation_v1",
            cache_group="native:5Y",
            resource_class="native_light",
            internal_workers=1,
            estimated_cold_sec=3600,
            v2_release_offset_min=None,
        )
        v2_policy = SimpleNamespace(
            scheme_id="v2",
            runtime_type="blackbox_v2",
            task_type="T+5",
            horizon=5,
            target_tenors=("1Y",),
            input_compatibility="databridge_v1",
            cache_group="databridge:1Y",
            resource_class="blackbox_v2",
            internal_workers=1,
            estimated_cold_sec=15,
            v2_release_offset_min=0,
        )
        self.policy = SimpleNamespace(
            version="daily-scheduler-policy-v2",
            expected_item_count=2,
            expected_target_count=2,
            timezone="Asia/Shanghai",
            not_before=time(6, 30),
            native_capture_deadline=time(8, 30),
            databridge_readiness_guardrail=time(6, 55),
            target_ready=time(7, 55),
            v2_start_guardrail=time(7, 45),
            sla_deadline=time(8, 0),
            recovery_cutoff=time(8, 30),
            native_max_concurrency=2,
            v2_max_concurrency=2,
            v2_timeout_sec=120,
            generation_max_count=64,
            generation_max_total_bytes=50 * 1024**3,
            generation_min_free_bytes=2 * 1024**3,
            allowed_resource_combinations=frozenset(
                {
                    ("native_export",),
                    ("databridge_refresh",),
                    ("databridge_pack",),
                    ("databridge_refresh", "native_export"),
                    ("blackbox_v2",),
                    ("native_light",),
                }
            ),
            schemes={"native": native_policy, "v2": v2_policy},
        )
        self.snapshot_policy_version = self.policy.version
        self.snapshot_policy_json_version = self.policy.version
        self.snapshot_expected_item_count = 2
        self.snapshot_expected_target_count = 2
        self.snapshot_actual_item_count = 2
        self.snapshot_actual_target_count = 2

    def now(self) -> datetime:
        return self.current_time

    def load_policy(self):
        self.events.append("policy")
        return self.policy

    def occurrence_lock(self, business_date: date):
        self.events.append(("lock", business_date.isoformat()))
        return _FakeLock(self.events)

    def maintain_generation_storage(self):
        self.events.append("generation-storage-maintenance")
        if self.generation_storage_error is not None:
            raise self.generation_storage_error
        return {
            "native_total_bytes": 0,
            "databridge_total_bytes": 0,
        }

    def is_trading_day(self, business_date: date) -> bool | None:
        self.events.append(("trading-day", business_date.isoformat()))
        return self.trading_day_status

    def prepare_occurrence_inputs(
        self,
        policy,
        business_date: date,
    ) -> OccurrenceInputs:
        self.events.append("prepare")
        if self.occurrence_input_error is not None:
            raise self.occurrence_input_error
        return OccurrenceInputs(
            feature_date="2026-07-23",
            target_dates={
                "native__h1__5Y": "2026-07-24",
                "v2__h5__1Y": "2026-07-30",
            },
            item_policy_by_base={
                "native": {"scheme_version": "n-v1"},
                "v2": {"scheme_version": "b-v1"},
            },
            policy_json={"version": policy.version},
        )

    def check_native_readiness(
        self,
        *,
        feature_date: str,
    ):
        self.events.append(("native-readiness", feature_date))
        return SimpleNamespace(
            ready=self.native_readiness_ready,
            feature_date=feature_date,
            missing_requirements=self.native_readiness_missing,
        )

    def find_occurrence_id(self, business_date: date) -> int | None:
        self.events.append(("find-occurrence", business_date.isoformat()))
        return self.existing_occurrence_id

    def read_frozen_inputs(
        self,
        *,
        occurrence_id: int,
        policy,
    ) -> OccurrenceInputs:
        self.events.append(("read-frozen-inputs", occurrence_id))
        return OccurrenceInputs(
            feature_date="2026-07-23",
            target_dates={
                "native__h1__5Y": "2026-07-24",
                "v2__h5__1Y": "2026-07-30",
            },
            item_policy_by_base={},
            policy_json={"version": policy.version},
        )

    def create_occurrence(
        self,
        *,
        business_date: date,
        inputs: OccurrenceInputs,
        policy,
    ) -> int:
        self.events.append(("create", business_date.isoformat(), inputs.feature_date))
        return 41

    def revalidate_capacity_admission(self) -> None:
        self.events.append("capacity-revalidated")
        if self.capacity_revalidation_error is not None:
            raise self.capacity_revalidation_error

    def read_snapshot(self, occurrence_id: int):
        self.events.append(("read-snapshot", occurrence_id))
        items = []
        for item_id, scheme_id in enumerate(("native", "v2"), start=1):
            policy_item = self.policy.schemes[scheme_id]
            items.append(
                SimpleNamespace(
                    item=SimpleNamespace(
                        item_id=item_id,
                        base_scheme_id=scheme_id,
                        runtime_type=policy_item.runtime_type,
                        state=self.states[scheme_id],
                        sla_status=self.item_sla_status[scheme_id],
                        attempt_no=self.attempts[scheme_id],
                        failure_code=None,
                        input_generation_id=self.bindings.get(item_id),
                        current_run_id=None,
                    ),
                    target_count=1,
                    accepted_target_count=(
                        1 if self.states[scheme_id] == "SUCCESS" else 0
                    ),
                )
            )
        completed = sum(
            state == "SUCCESS" for state in self.states.values()
        )
        return SimpleNamespace(
            occurrence=SimpleNamespace(
                occurrence_id=occurrence_id,
                predict_date="2026-07-24",
                feature_date="2026-07-23",
                policy_version=self.snapshot_policy_version,
                expected_item_count=self.snapshot_expected_item_count,
                expected_target_count=self.snapshot_expected_target_count,
                completion_state=(
                    "SUCCESS" if completed == 2 else "RUNNING"
                ),
                sla_outcome=self.sla_outcome,
                policy_json={
                    "version": self.snapshot_policy_json_version,
                    "daily_coordinator_epoch":
                        dict(TEST_COORDINATOR_EPOCH),
                    "times": {
                        "databridge_readiness_guardrail": "06:55",
                    }
                },
            ),
            items=tuple(items),
            actual_item_count=self.snapshot_actual_item_count,
            actual_target_count=self.snapshot_actual_target_count,
            actual_accepted_target_count=completed,
        )

    def reconcile_occurrence_visibility_receipts(
        self,
        *,
        occurrence_id: int,
    ) -> int:
        self.events.append(("reconcile-visibility", occurrence_id))
        if self.visibility_reconcile_error is not None:
            raise self.visibility_reconcile_error
        return 0

    def validate_occurrence_policy(self, *, snapshot, policy) -> None:
        del snapshot, policy
        self.events.append("occurrence-policy-validated")

    def validate_occurrence_epoch(self, *, snapshot) -> None:
        del snapshot
        self.events.append("occurrence-epoch-validated")

    def resolve_bound_generations(
        self,
        *,
        snapshot,
        inputs: OccurrenceInputs,
        policy,
    ):
        del snapshot, inputs, policy
        return None, None

    def find_unbound_building_generations(
        self,
        *,
        business_date: date,
    ) -> tuple[object, ...]:
        self.events.append(
            ("find-unbound-building", business_date.isoformat())
        )
        return self.unbound_building_generations

    def fence_item(self, *, item_id: int):
        scheme_id = "native" if item_id == 1 else "v2"
        self.states[scheme_id] = "ABANDONED"
        self.events.append(("fence", scheme_id))
        return SimpleNamespace(
            item_id=item_id,
            run_id=100 + item_id,
            execution_token=f"token-{item_id}",
            process_id=200 + item_id,
            process_group_id=300 + item_id,
        )

    def cleanup_fenced_attempt(self, *, item, fenced_attempt) -> bool:
        self.events.append(
            (
                "cleanup-orphan",
                item.base_scheme_id,
                fenced_attempt.run_id,
            )
        )
        return True

    def confirm_orphan_cleanup(self, *, fenced_attempt) -> None:
        self.events.append(
            ("confirm-cleanup", fenced_attempt.run_id)
        )

    def expire_items(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ) -> int:
        count = 0
        for scheme_id, state in tuple(self.states.items()):
            if state in {"PENDING", "RETRY_WAIT", "ABANDONED"}:
                self.states[scheme_id] = "EXPIRED"
                count += 1
        self.events.append(("expire", occurrence_id, evaluated_at.hour))
        return count

    def detect_late_writes(
        self,
        *,
        business_date: date,
        feature_date: str,
    ):
        del business_date, feature_date
        return ()

    def evaluate_v2_start(self, *, item_id: int, evaluated_at: datetime):
        self.events.append(("evaluate-v2", item_id, evaluated_at.hour))
        scheme_id = "native" if item_id == 1 else "v2"
        self.item_sla_status[scheme_id] = "ON_TIME"
        return SimpleNamespace(status="ON_TIME", reason=None)

    def project_databridge_readiness(
        self,
        *,
        snapshot,
        guardrail_at: datetime,
    ):
        del snapshot
        generation_id = self.bindings.get(2)
        sealed_at = self.databridge_sealed_at
        if generation_id is None or sealed_at is None:
            return SimpleNamespace(
                status="LATE",
                reason="DATABRIDGE_NOT_SEALED_BY_GUARDRAIL",
                generation_id=generation_id,
                sealed_at=sealed_at,
                guardrail_at=guardrail_at,
            )
        normalized_sealed = sealed_at.astimezone(guardrail_at.tzinfo)
        return SimpleNamespace(
            status=(
                "ON_TIME"
                if normalized_sealed <= guardrail_at
                else "LATE"
            ),
            reason=(
                None
                if normalized_sealed <= guardrail_at
                else "DATABRIDGE_SEALED_AFTER_GUARDRAIL"
            ),
            generation_id=generation_id,
            sealed_at=sealed_at,
            guardrail_at=guardrail_at,
        )

    def evaluate_target_sla(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ):
        self.events.append(("evaluate-sla", occurrence_id, evaluated_at.hour))
        self.sla_outcome = "MET"
        return SimpleNamespace(
            status="MET",
            accepted_target_count=2,
            expected_target_count=2,
            reason=None,
        )

    def _generation_started(self, name: str) -> None:
        with self._generation_lock:
            self.generation_running += 1
            self.max_generation_running = max(
                self.max_generation_running,
                self.generation_running,
            )
        self.events.append((name, "started"))
        self._generation_barrier.wait(timeout=2)

    def _generation_finished(self, name: str) -> None:
        self.events.append((name, "finished"))
        with self._generation_lock:
            self.generation_running -= 1

    def recover_native_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
    ):
        self.events.append(
            ("recover-native", business_date, feature_date)
        )
        if self.native_recovery_error is not None:
            raise self.native_recovery_error
        return self.recovered_native_generation

    def recover_databridge_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
        native_generation,
    ):
        self.events.append(
            (
                "recover-databridge",
                business_date,
                feature_date,
                native_generation.generation_id,
            )
        )
        if self.databridge_recovery_error is not None:
            raise self.databridge_recovery_error
        return self.recovered_databridge_generation

    def build_native_generation(
        self,
        *,
        business_date: str,
        feature_date: str,
        capture_not_after: datetime,
        source_contract_cutoff: datetime,
    ):
        self.events.append(
            (
                "native-capture-window",
                capture_not_after.isoformat(),
                source_contract_cutoff.isoformat(),
            )
        )
        self._generation_started("native-generation")
        self._generation_finished("native-generation")
        return SimpleNamespace(
            generation_id="native-gen",
            business_date=business_date,
            feature_date=feature_date,
            sealed_at="2026-07-23T22:31:00Z",
        )

    def refresh_databridge(
        self,
        *,
        business_date: str,
        feature_date: str,
        publication_capability,
    ):
        self.last_publication_capability = publication_capability
        self.events.append(
            (
                "databridge-publication-capability",
                publication_capability.occurrence_id,
                publication_capability.business_date,
            )
        )
        self._generation_started("databridge-refresh")
        self._generation_finished("databridge-refresh")
        return SimpleNamespace(state={"refresh_date": business_date})

    def build_databridge_generation(
        self,
        *,
        current,
        native_generation,
        business_date: str,
        feature_date: str,
    ):
        self.events.append("databridge-generation")
        return SimpleNamespace(
            generation_id="databridge-gen",
            business_date=business_date,
            feature_date=feature_date,
            sealed_at="2026-07-23T22:32:00Z",
        )

    def validate_frozen_calendar(
        self,
        *,
        native_generation,
        inputs: OccurrenceInputs,
        policy,
    ) -> None:
        self.events.append("calendar-validated")

    def register_native_generation(
        self,
        context,
        *,
        occurrence_id: int,
    ) -> str:
        self.assert_occurrence_id(occurrence_id)
        self.bindings[1] = context.generation_id
        self.events.append(
            ("seal-bind-native", occurrence_id, context.generation_id)
        )
        return context.generation_id

    def register_databridge_generation(
        self,
        context,
        *,
        occurrence_id: int,
    ) -> str:
        self.assert_occurrence_id(occurrence_id)
        self.bindings[2] = context.generation_id
        self.events.append(
            (
                "seal-bind-databridge",
                occurrence_id,
                context.generation_id,
            )
        )
        return context.generation_id

    def assert_occurrence_id(self, occurrence_id: int) -> None:
        if occurrence_id != 41:
            raise AssertionError(
                f"unexpected occurrence_id: {occurrence_id}"
            )

    def fail_item(
        self,
        *,
        item_id: int,
        failure_code: str,
        message: str,
    ) -> None:
        del message
        scheme_id = "native" if item_id == 1 else "v2"
        self.states[scheme_id] = "FAILED_TERMINAL"
        self.failures.append((item_id, failure_code))

    def dispatch_decisions(
        self,
        *,
        business_date: date,
        snapshot,
        native_generation,
        databridge_generation,
        running_scheme_ids: tuple[str, ...],
        running_resource_classes: tuple[str, ...],
        now: datetime,
    ):
        del business_date, native_generation, databridge_generation, now
        self.events.append(
            (
                "dispatch-resources",
                tuple(running_resource_classes),
            )
        )
        decisions = []
        for summary in snapshot.items:
            item = summary.item
            if (
                item.state in {"PENDING", "ABANDONED", "RETRY_WAIT"}
                and item.input_generation_id
            ):
                decisions.append(
                    SimpleNamespace(
                        scheme_id=item.base_scheme_id,
                        action="DISPATCH",
                        pool=(
                            "native"
                            if item.runtime_type == "native_adapter"
                            else "v2"
                        ),
                        failure_code=None,
                        reason="POLICY_APPROVED",
                    )
                )
            else:
                decisions.append(
                    SimpleNamespace(
                        scheme_id=item.base_scheme_id,
                        action="SKIP_SUCCESS",
                        pool="native",
                        failure_code=None,
                        reason="DONE",
                    )
                )
        return tuple(decisions)

    def execute_item(
        self,
        *,
        item_id: int,
        trigger_origin: str,
    ):
        scheme_id = "native" if item_id == 1 else "v2"
        self.attempts[scheme_id] += 1
        self.states[scheme_id] = "SUCCESS"
        self.events.append(("execute", scheme_id, trigger_origin))
        return SimpleNamespace(status="success", scheme_id=scheme_id)

    def heartbeat(self, *, occurrence_id: int | None, state: str, details):
        self.heartbeat_calls.append(
            {
                "occurrence_id": occurrence_id,
                "state": state,
                "details": details,
            }
        )
        self.events.append(("heartbeat", occurrence_id, state))

    def alert(
        self,
        *,
        code: str,
        severity: str,
        business_date: date,
        occurrence_id: int | None,
        message: str,
        scheme_id: str | None = None,
        details=None,
    ) -> None:
        self.alert_calls.append(
            {
                "code": code,
                "severity": severity,
                "business_date": business_date,
                "occurrence_id": occurrence_id,
                "message": message,
                "scheme_id": scheme_id,
                "details": details,
            }
        )
        self.events.append(("alert", code))

    def wait_for_progress(self, seconds: float) -> None:
        self.events.append(("wait", seconds))
        Event().wait(min(max(float(seconds), 0.0), 0.001))


class DailyRuntimeFrozenPolicyVersionTests(unittest.TestCase):
    @staticmethod
    def _legacy_snapshot_services() -> _FakeServices:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.policy.expected_item_count = 25
        services.policy.expected_target_count = 29
        services.snapshot_expected_item_count = 25
        services.snapshot_expected_target_count = 29
        services.snapshot_actual_item_count = 25
        services.snapshot_actual_target_count = 29
        services.snapshot_policy_version = "daily-scheduler-policy-v1"
        services.snapshot_policy_json_version = (
            "daily-scheduler-policy-v1"
        )
        return services

    def test_occurrence_rejects_legacy_version_before_side_effects(
        self,
    ) -> None:
        services = self._legacy_snapshot_services()

        with self.assertRaisesRegex(
            RuntimeError,
            "frozen occurrence policy version differs from runtime policy",
        ):
            DailyRuntime(services).run_occurrence(
                run_date="2026-07-24",
            )

        self.assertEqual(services.attempts, Counter())
        self.assertEqual(services.bindings, {})
        self.assertEqual(services.heartbeat_calls, [])
        self.assertFalse(
            any(
                event == "prepare"
                or (
                    isinstance(event, tuple)
                    and event[0]
                    in {
                        "execute",
                        "fence",
                        "cleanup-orphan",
                        "reconcile-visibility",
                        "read-frozen-inputs",
                    }
                )
                or (
                    isinstance(event, tuple)
                    and isinstance(event[0], str)
                    and (
                        "generation" in event[0]
                        or "databridge" in event[0]
                    )
                )
                for event in services.events
            )
        )

    def test_operator_recovery_rejects_legacy_version_before_side_effects(
        self,
    ) -> None:
        services = self._legacy_snapshot_services()

        with self.assertRaisesRegex(
            RuntimeError,
            "frozen occurrence policy version differs from runtime policy",
        ):
            DailyRuntime(services).run_operator_recovery(
                scheme_id="native",
                run_date="2026-07-24",
            )

        self.assertEqual(services.attempts, Counter())
        self.assertEqual(services.bindings, {})
        self.assertEqual(services.heartbeat_calls, [])
        self.assertFalse(
            any(
                isinstance(event, tuple)
                and event[0]
                in {
                    "execute",
                    "fence",
                    "cleanup-orphan",
                    "reconcile-visibility",
                    "read-frozen-inputs",
                }
                for event in services.events
            )
        )


class DailyRuntimeProcessCleanupBoundaryTests(unittest.TestCase):
    def test_cleanup_blocked_result_stops_dispatch_loop_and_alerts(
        self,
    ) -> None:
        cases = (
            (
                "fenced_pending_cleanup",
                "ABANDONED",
                "ABANDONED_FENCE_PENDING_CLEANUP",
            ),
            ("recovery_blocked", "RUNNING", None),
        )
        for execution_status, stored_state, failure_code in cases:
            with self.subTest(execution_status=execution_status):
                services = _FakeServices(
                    now=datetime(
                        2026,
                        7,
                        24,
                        7,
                        0,
                        tzinfo=SHANGHAI,
                    ),
                )
                services.states = {
                    "native": "PENDING",
                    "v2": "SUCCESS",
                }
                services.bindings = {1: "native-generation"}
                calls = 0

                def execute_item(*, item_id: int, trigger_origin: str):
                    nonlocal calls
                    calls += 1
                    if calls > 1:
                        raise AssertionError(
                            "cleanup-blocked item was dispatched again"
                        )
                    services.events.append(
                        ("execute", "native", trigger_origin)
                    )
                    services.states["native"] = stored_state
                    return SimpleNamespace(
                        status=execution_status,
                        item_id=item_id,
                        scheme_id="native",
                        run_id=901,
                        attempt_no=1,
                        failure_code=failure_code,
                        error_message="process group cleanup unconfirmed",
                    )

                services.execute_item = execute_item
                services.wait_for_progress = lambda _seconds: self.fail(
                    "cleanup-blocked drive must not enter a polling loop"
                )
                availability = _GenerationAvailability()
                availability.finish(
                    GenerationBuildOutcome(
                        native_generation=SimpleNamespace(
                            generation_id="native-generation"
                        ),
                        databridge_generation=None,
                    )
                )

                result = DailyRuntime(services)._drive_items(
                    occurrence_id=41,
                    business_date=date(2026, 7, 24),
                    policy=services.policy,
                    generation_availability=availability,
                    trigger_origin="apscheduler",
                )

                self.assertEqual(calls, 1)
                self.assertEqual(result.status, "recovery_blocked")
                self.assertEqual(
                    result.dispatched_scheme_ids,
                    ("native",),
                )
                self.assertIn(
                    ("alert", "SCHEDULE_ATTEMPT_CLEANUP_BLOCKED"),
                    services.events,
                )
                self.assertIn(
                    ("heartbeat", 41, "RECOVERY_BLOCKED"),
                    services.events,
                )

    def test_next_coordinator_recovery_converges_running_and_pending_fence(
        self,
    ) -> None:
        for state, failure_code in (
            ("RUNNING", None),
            (
                "ABANDONED",
                "ABANDONED_FENCE_PENDING_CLEANUP",
            ),
        ):
            with self.subTest(state=state):
                services = _FakeServices(
                    now=datetime(
                        2026,
                        7,
                        24,
                        7,
                        1,
                        tzinfo=SHANGHAI,
                    ),
                )
                snapshot = SimpleNamespace(
                    items=(
                        SimpleNamespace(
                            item=SimpleNamespace(
                                item_id=1,
                                base_scheme_id="native",
                                state=state,
                                failure_code=failure_code,
                            )
                        ),
                    )
                )

                recovered = DailyRuntime(
                    services
                )._recover_running_items(
                    snapshot=snapshot,
                    business_date=date(2026, 7, 24),
                    occurrence_id=41,
                )

                self.assertIs(recovered, True)
                self.assertIn(("fence", "native"), services.events)
                self.assertIn(
                    ("cleanup-orphan", "native", 101),
                    services.events,
                )
                self.assertIn(("confirm-cleanup", 101), services.events)


class DailyRuntimeNotBeforeTests(unittest.TestCase):
    def test_before_0630_does_not_acquire_owner_or_create_occurrence(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 29, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "not_before")
        self.assertIsNone(result.occurrence_id)
        self.assertEqual(services.events, ["policy"])


class DailyRuntimeGenerationTests(unittest.TestCase):
    def test_missing_business_calendar_waits_without_occurrence_or_attempt(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.trading_day_status = None

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "waiting_for_native_readiness")
        self.assertIsNone(result.occurrence_id)
        self.assertNotIn("prepare", services.events)
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "create"
                for event in services.events
            )
        )
        self.assertEqual(services.attempts, Counter())
        self.assertEqual(
            services.heartbeat_calls[-1]["state"],
            "WAITING_NATIVE_READINESS",
        )

    def test_unready_native_inputs_wait_without_generation_or_attempt(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.native_readiness_ready = False
        services.native_readiness_missing = (
            "daily_target:TB5YWI0C",
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "waiting_for_native_readiness")
        self.assertEqual(result.occurrence_id, 41)
        self.assertIn(
            ("native-readiness", "2026-07-23"),
            services.events,
        )
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )
        self.assertEqual(services.attempts, Counter())
        self.assertEqual(services.failures, [])
        self.assertEqual(
            services.heartbeat_calls[-1]["state"],
            "WAITING_NATIVE_READINESS",
        )

    def test_incomplete_target_calendar_waits_without_occurrence(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.occurrence_input_error = NativeReadinessPending(
            "not enough trading days after 2026-07-23"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "waiting_for_native_readiness")
        self.assertIsNone(result.occurrence_id)
        self.assertEqual(services.attempts, Counter())
        self.assertEqual(
            services.heartbeat_calls[-1]["state"],
            "WAITING_NATIVE_READINESS",
        )

    def test_non_readiness_input_error_is_not_masked_as_waiting(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.occurrence_input_error = ValueError(
            "invalid frozen policy payload"
        )

        with self.assertRaisesRegex(
            ValueError,
            "invalid frozen policy payload",
        ):
            DailyRuntime(services).run_occurrence(
                run_date="2026-07-24",
            )

    def test_bound_native_generation_ignores_later_source_corrections(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 45, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings[1] = "native-gen"
        frozen_native = SimpleNamespace(
            generation_id="native-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:43:00Z",
        )
        services.resolve_bound_generations = lambda **_kwargs: (
            frozen_native,
            None,
        )
        services.refresh_databridge = lambda **_kwargs: SimpleNamespace(
            state={"refresh_date": "2026-07-24"}
        )

        def forbidden_readiness(**_kwargs):
            raise AssertionError(
                "bound generation must not re-read mutable readiness"
            )

        services.check_native_readiness = forbidden_readiness

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(services.bindings[1], "native-gen")
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )

    def test_owner_storage_preflight_failure_blocks_generation_build(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.generation_storage_error = OSError(
            "generation storage quota exceeded"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_storage_failed")
        maintenance_index = services.events.index(
            "generation-storage-maintenance"
        )
        self.assertLess(
            services.events.index("lock:enter"),
            maintenance_index,
        )
        self.assertLess(
            maintenance_index,
            services.events.index("lock:exit"),
        )
        self.assertIn(
            ("alert", "GENERATION_STORAGE_UNAVAILABLE"),
            services.events,
        )
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )

    def test_new_occurrence_revalidates_capacity_before_any_followup_work(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.capacity_revalidation_error = RuntimeError(
            "capacity candidate drifted after freeze"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "capacity candidate drifted",
        ):
            DailyRuntime(services).run_occurrence(
                run_date="2026-07-24",
            )

        self.assertIn(
            ("create", "2026-07-24", "2026-07-23"),
            services.events,
        )
        self.assertIn("capacity-revalidated", services.events)
        self.assertNotIn(("reconcile-visibility", 41), services.events)
        self.assertNotIn(("native-generation", "started"), services.events)

    def test_claim_rejection_resnapshots_without_stopping_other_items(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        original_execute = services.execute_item
        rejected_once = False

        def execute_with_claim_race(
            *,
            item_id: int,
            trigger_origin: str,
        ):
            nonlocal rejected_once
            if item_id == 1 and not rejected_once:
                rejected_once = True
                return SimpleNamespace(
                    status="claim_rejected",
                    scheme_id="native",
                    failure_code="STALE_ATTEMPT",
                    error_message="first-attempt barrier changed",
                )
            return original_execute(
                item_id=item_id,
                trigger_origin=trigger_origin,
            )

        services.execute_item = execute_with_claim_race

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "complete")
        self.assertTrue(rejected_once)
        self.assertTrue(
            any(
                event == ("alert", "ITEM_CLAIM_REJECTED")
                for event in services.events
            )
        )
        self.assertTrue(
            any(
                isinstance(event, tuple) and event[0] == "wait"
                for event in services.events
            )
        )

    def test_new_occurrence_builds_native_and_fresh_databridge_in_parallel(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.occurrence_id, 41)
        self.assertEqual(services.max_generation_running, 2)
        self.assertEqual(
            services.bindings,
            {1: "native-gen", 2: "databridge-gen"},
        )
        self.assertIn(
            (
                "native-capture-window",
                "2026-07-24T08:30:00+08:00",
                "2026-07-24T06:30:00+08:00",
            ),
            services.events,
        )
        self.assertIn("calendar-validated", services.events)
        capability = services.last_publication_capability
        self.assertIsNotNone(capability)
        self.assertTrue(
            hasattr(capability, "occurrence_validator"),
            "scheduler must inject a fresh occurrence snapshot validator",
        )
        snapshots_before = services.events.count(
            ("read-snapshot", 41)
        )
        fresh = capability.occurrence_validator()
        self.assertEqual(fresh["occurrence_id"], 41)
        self.assertEqual(fresh["business_date"], "2026-07-24")
        self.assertEqual(
            fresh["daily_coordinator_epoch"],
            TEST_COORDINATOR_EPOCH,
        )
        self.assertEqual(
            services.events.count(("read-snapshot", 41)),
            snapshots_before + 1,
        )
        self.assertTrue(
            any(
                event[:3] == ("heartbeat", 41, "RUNNING")
                for event in services.events
                if isinstance(event, tuple) and len(event) >= 3
            )
        )

    def test_restart_after_popen_crash_scans_before_capture_then_rebuilds(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        recovery_index = services.events.index(
            ("recover-native", "2026-07-24", "2026-07-23")
        )
        capture_index = next(
            index
            for index, event in enumerate(services.events)
            if (
                isinstance(event, tuple)
                and event[0] == "native-capture-window"
            )
        )
        build_index = services.events.index(
            ("native-generation", "started")
        )
        self.assertLess(recovery_index, capture_index)
        self.assertLess(capture_index, build_index)
        self.assertIn(
            "2026-07-24T08:30:00+08:00",
            services.events[capture_index],
        )

    def test_after_0630_builds_when_native_inputs_are_ready(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 31, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(
            (
                "native-capture-window",
                "2026-07-24T08:30:00+08:00",
                "2026-07-24T06:30:00+08:00",
            ),
            services.events,
        )

    def test_restart_after_final_rename_recovers_without_live_rebuild(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.recovered_native_generation = SimpleNamespace(
            generation_id="native-renamed-final",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            manifest_sha256="a" * 64,
        )
        services.recovered_databridge_generation = SimpleNamespace(
            generation_id="databridge-renamed-final",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            native_generation_id="native-renamed-final",
            native_manifest_sha256="a" * 64,
        )
        services.build_native_generation = lambda **_kwargs: self.fail(
            "renamed Native final must be recovered, not re-exported"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "renamed DataBridge final must skip full refresh"
        )
        services.build_databridge_generation = lambda **_kwargs: self.fail(
            "renamed DataBridge final must not be repacked"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            services.bindings,
            {
                1: "native-renamed-final",
                2: "databridge-renamed-final",
            },
        )
        self.assertIn(
            (
                "seal-bind-native",
                41,
                "native-renamed-final",
            ),
            services.events,
        )
        self.assertIn(
            (
                "seal-bind-databridge",
                41,
                "databridge-renamed-final",
            ),
            services.events,
        )

    def test_restart_recovers_exact_building_fence_instead_of_terminal_failure(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            {
                "generation_id": "native-building-exact",
                "generation_type": "native_source",
            },
        )
        services.recovered_native_generation = SimpleNamespace(
            generation_id="native-building-exact",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            manifest_sha256="b" * 64,
        )
        services.recovered_databridge_generation = SimpleNamespace(
            generation_id="databridge-after-building",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            native_generation_id="native-building-exact",
            native_manifest_sha256="b" * 64,
        )
        services.build_native_generation = lambda **_kwargs: self.fail(
            "exact BUILDING fence must be resumed"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "fully recovered generations must not refresh"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(services.failures, [])
        self.assertEqual(
            services.bindings[1],
            "native-building-exact",
        )

    def test_recovery_identity_drift_fails_closed_without_live_export(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.native_recovery_error = RuntimeError(
            "generation DB fence identity drifted"
        )
        services.build_native_generation = lambda **_kwargs: self.fail(
            "identity drift must not fall through to live export"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "identity drift must not start full refresh"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertCountEqual(
            services.failures,
            [
                (1, "GENERATION_BUILD_FAILED"),
                (2, "GENERATION_BUILD_FAILED"),
            ],
        )

    def test_native_dispatches_as_soon_as_native_generation_is_sealed(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        refresh_can_finish = Event()
        native_started = Event()

        def build_native(**kwargs):
            services.events.append(
                (
                    "native-capture-window",
                    kwargs["capture_not_after"].isoformat(),
                    kwargs["source_contract_cutoff"].isoformat(),
                )
            )
            return SimpleNamespace(
                generation_id="native-gen",
                business_date="2026-07-24",
                feature_date="2026-07-23",
                sealed_at="2026-07-23T22:31:00Z",
            )

        def refresh(**_kwargs):
            services.events.append("databridge-refresh-waiting")
            if not refresh_can_finish.wait(timeout=2):
                raise TimeoutError("test did not release DataBridge refresh")
            services.events.append("databridge-refresh-released")
            return SimpleNamespace(
                state={"refresh_date": "2026-07-24"}
            )

        original_execute = services.execute_item

        def execute(*, item_id: int, trigger_origin: str):
            if item_id == 1:
                native_started.set()
            return original_execute(
                item_id=item_id,
                trigger_origin=trigger_origin,
            )

        services.build_native_generation = build_native
        services.refresh_databridge = refresh
        services.execute_item = execute

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                DailyRuntime(services).run_occurrence,
                run_date="2026-07-24",
            )
            try:
                self.assertTrue(
                    native_started.wait(timeout=1),
                    "Native stayed blocked behind DataBridge refresh",
                )
            finally:
                refresh_can_finish.set()
            result = future.result(timeout=2)

        self.assertEqual(result.status, "complete")
        self.assertLess(
            services.events.index(("execute", "native", "apscheduler")),
            services.events.index("databridge-refresh-released"),
        )
        self.assertTrue(
            any(
                event == (
                    "dispatch-resources",
                    ("databridge_refresh",),
                )
                for event in services.events
            ),
            "Native dispatch did not account for active DataBridge refresh",
        )

    def test_generation_failure_terminals_every_unexecutable_item(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.policy.schemes["native"].input_compatibility = "unsupported"

        def fail_refresh(**_kwargs):
            services._generation_started("databridge-refresh")
            services._generation_finished("databridge-refresh")
            raise OSError("download unavailable")

        services.refresh_databridge = fail_refresh

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertCountEqual(
            services.failures,
            [
                (1, "NATIVE_GENERATION_UNSUPPORTED"),
                (2, "GENERATION_BUILD_FAILED"),
            ],
        )
        self.assertIn(("alert", "GENERATION_BUILD_FAILED"), services.events)
        self.assertNotIn(("execute", "native", "apscheduler"), services.events)
        self.assertNotIn(("execute", "v2", "apscheduler"), services.events)

    def test_source_evidence_failure_still_starts_fresh_databridge_branch(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        services.build_native_generation = lambda **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("watermark unavailable"))
        )

        def refresh(**_kwargs):
            services.events.append("databridge-refresh-attempted")
            return SimpleNamespace(state={"refresh_date": "2026-07-24"})

        services.refresh_databridge = refresh

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertIn("databridge-refresh-attempted", services.events)
        self.assertCountEqual(
            services.failures,
            [
                (1, "GENERATION_BUILD_FAILED"),
                (2, "GENERATION_BUILD_FAILED"),
            ],
        )

    def test_ready_startup_catchup_can_create_first_native_generation(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(
            (
                "native-capture-window",
                "2026-07-24T08:30:00+08:00",
                "2026-07-24T06:30:00+08:00",
            ),
            services.events,
        )

    def test_terminal_execution_failure_alerts_without_retry(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        original_execute = services.execute_item

        def execute(*, item_id: int, trigger_origin: str):
            if item_id == 2:
                services.attempts["v2"] += 1
                services.states["v2"] = "FAILED_TERMINAL"
                return SimpleNamespace(
                    status="failed",
                    scheme_id="v2",
                    failure_code="ALGORITHM",
                    error_message="model failed",
                )
            return original_execute(
                item_id=item_id,
                trigger_origin=trigger_origin,
            )

        services.execute_item = execute

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(services.attempts["v2"], 1)
        self.assertIn(
            ("alert", "ITEM_TERMINAL_FAILURE"),
            services.events,
        )

    def test_recovery_reuses_exact_bound_generations_without_refresh(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.existing_occurrence_id = 41
        native = SimpleNamespace(
            generation_id="native-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:31:00Z",
        )
        databridge = SimpleNamespace(
            generation_id="databridge-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:32:00Z",
        )

        def resolve(**_kwargs):
            services.events.append("resolved-bound")
            return native, databridge

        services.resolve_bound_generations = resolve
        services.build_native_generation = lambda **_kwargs: self.fail(
            "recovery must not build another Native generation"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "recovery must not refresh or switch DataBridge generation"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn("resolved-bound", services.events)
        self.assertNotIn("prepare", services.events)
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )

    def test_completed_v2_occurrence_reentry_is_idempotent(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.states = {"native": "SUCCESS", "v2": "SUCCESS"}
        services.existing_occurrence_id = 41
        services.build_native_generation = lambda **_kwargs: self.fail(
            "completed occurrence must not rebuild Native input"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "completed occurrence must not refresh DataBridge"
        )

        runtime = DailyRuntime(services)
        first = runtime.run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )
        second = runtime.run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(first.status, "complete")
        self.assertEqual(second.status, "complete")
        self.assertEqual(first.dispatched_scheme_ids, ())
        self.assertEqual(second.dispatched_scheme_ids, ())
        self.assertEqual(services.attempts, Counter())
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "execute"
                for event in services.events
            )
        )
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )
        first_snapshot = services.events.index(("read-snapshot", 41))
        first_validation = services.events.index(
            "occurrence-policy-validated"
        )
        first_reconcile = services.events.index(
            ("reconcile-visibility", 41)
        )
        self.assertLess(first_snapshot, first_validation)
        self.assertLess(first_validation, first_reconcile)

    def test_occurrence_reconciles_again_before_final_complete_snapshot(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "complete")
        reconcile_indexes = [
            index
            for index, event in enumerate(services.events)
            if event == ("reconcile-visibility", 41)
        ]
        snapshot_indexes = [
            index
            for index, event in enumerate(services.events)
            if event == ("read-snapshot", 41)
        ]
        self.assertGreaterEqual(len(reconcile_indexes), 2)
        self.assertLess(snapshot_indexes[0], reconcile_indexes[0])
        self.assertIn(
            "occurrence-policy-validated",
            services.events[
                snapshot_indexes[0] + 1:reconcile_indexes[0]
            ],
        )
        self.assertLess(reconcile_indexes[-1], snapshot_indexes[-1])

    def test_occurrence_reconciliation_failure_is_fail_closed(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.visibility_reconcile_error = RuntimeError(
            "visibility receipt database unavailable"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(
            result.status,
            "visibility_reconciliation_failed",
        )
        self.assertEqual(
            services.events.count(("read-snapshot", 41)),
            1,
        )
        self.assertLess(
            services.events.index("occurrence-policy-validated"),
            services.events.index(("reconcile-visibility", 41)),
        )
        self.assertIn(
            ("heartbeat", 41, "VISIBILITY_RECONCILIATION_FAILED"),
            services.events,
        )
        self.assertIn(
            ("alert", "VISIBILITY_RECEIPT_RECONCILE_FAILED"),
            services.events,
        )
        failure_heartbeat = services.heartbeat_calls[-1]
        self.assertEqual(
            failure_heartbeat["details"]["phase"],
            "run_occurrence_pre_snapshot",
        )
        self.assertEqual(
            failure_heartbeat["details"]["reason"],
            "VISIBILITY_RECEIPT_RECONCILE_FAILED",
        )
        self.assertIn(
            "visibility receipt database unavailable",
            failure_heartbeat["details"]["error"],
        )
        self.assertNotIn(
            ("execute", "native", "startup_catchup"),
            services.events,
        )

    def test_final_reconciliation_failure_blocks_complete_projection(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )
        call_count = 0

        def reconcile(*, occurrence_id: int) -> int:
            nonlocal call_count
            call_count += 1
            services.events.append(
                ("reconcile-visibility", occurrence_id)
            )
            if call_count == 2:
                raise RuntimeError("post-dispatch receipt failure")
            return 0

        services.reconcile_occurrence_visibility_receipts = reconcile

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(
            result.status,
            "visibility_reconciliation_failed",
        )
        self.assertEqual(call_count, 2)
        self.assertEqual(
            set(result.dispatched_scheme_ids),
            {"native", "v2"},
        )
        self.assertNotIn(
            ("heartbeat", 41, "COMPLETE"),
            services.events,
        )
        self.assertEqual(
            services.alert_calls[-1]["details"]["phase"],
            "run_occurrence_pre_final_snapshot",
        )

    def test_completed_occurrence_ignores_unrelated_building_generation(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.states = {"native": "SUCCESS", "v2": "SUCCESS"}
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertNotIn(
            ("alert", "GENERATION_BUILD_FAILED"),
            services.events,
        )

    def test_recovery_with_unrecoverable_building_fence_fails_closed(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )
        services.native_recovery_error = RuntimeError(
            "BUILDING fence manifest is missing"
        )
        services.build_native_generation = lambda **_kwargs: self.fail(
            "recovery must not rebuild Native input"
        )
        services.refresh_databridge = lambda **_kwargs: self.fail(
            "recovery must not refresh DataBridge input"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertCountEqual(
            services.failures,
            [
                (1, "GENERATION_BUILD_FAILED"),
                (2, "GENERATION_BUILD_FAILED"),
            ],
        )
        self.assertIn(
            ("find-unbound-building", "2026-07-24"),
            services.events,
        )
        self.assertIn(("read-frozen-inputs", 41), services.events)
        self.assertNotIn(
            ("trading-day", "2026-07-24"),
            services.events,
        )
        self.assertIn(
            ("alert", "GENERATION_BUILD_FAILED"),
            services.events,
        )

    def test_atomic_registration_requires_every_runtime_item_to_be_bound(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )

        def seal_without_binding(context, *, occurrence_id: int) -> str:
            services.assert_occurrence_id(occurrence_id)
            return context.generation_id

        services.register_databridge_generation = seal_without_binding

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertEqual(
            services.failures,
            [(2, "GENERATION_BUILD_FAILED")],
        )
        self.assertIn(
            ("execute", "native", "apscheduler"),
            services.events,
        )
        self.assertNotIn(
            ("execute", "v2", "apscheduler"),
            services.events,
        )

    def test_binding_postcondition_preserves_original_hash_failure_code(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 1, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.resolve_bound_generations = lambda **_kwargs: (
            (_ for _ in ()).throw(
                RuntimeError("manifest sha256 mismatch")
            )
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertCountEqual(
            services.failures,
            [
                (1, "GENERATION_HASH_MISMATCH"),
                (2, "GENERATION_HASH_MISMATCH"),
            ],
        )


class DailyRuntimeRecoveryTests(unittest.TestCase):
    def _bound_services(self) -> _FakeServices:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 5, tzinfo=SHANGHAI),
        )
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.existing_occurrence_id = 41
        native = SimpleNamespace(
            generation_id="native-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:31:00Z",
        )
        databridge = SimpleNamespace(
            generation_id="databridge-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:32:00Z",
        )
        services.resolve_bound_generations = lambda **_kwargs: (
            native,
            databridge,
        )
        return services

    def test_startup_recovery_fences_before_cleanup_and_confirmation(self) -> None:
        services = self._bound_services()
        services.states["native"] = "RUNNING"
        services.states["v2"] = "SUCCESS"

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        fence_index = services.events.index(("fence", "native"))
        cleanup_index = services.events.index(
            ("cleanup-orphan", "native", 101)
        )
        confirm_index = services.events.index(("confirm-cleanup", 101))
        execute_index = services.events.index(
            ("execute", "native", "startup_catchup")
        )
        self.assertLess(fence_index, cleanup_index)
        self.assertLess(cleanup_index, confirm_index)
        self.assertLess(confirm_index, execute_index)

    def test_abandoned_second_attempt_keeps_startup_recovery_origin(
        self,
    ) -> None:
        services = self._bound_services()
        services.states["native"] = "RUNNING"
        services.states["v2"] = "SUCCESS"
        services.attempts["native"] = 1
        services.attempts["v2"] = 1

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(
            ("execute", "native", "startup_catchup"),
            services.events,
        )
        self.assertNotIn(
            ("execute", "native", "auto_retry"),
            services.events,
        )

    def test_recovery_fails_closed_when_orphan_cleanup_is_unconfirmed(self) -> None:
        services = self._bound_services()
        services.states["native"] = "RUNNING"
        services.states["v2"] = "SUCCESS"
        services.cleanup_fenced_attempt = lambda **_kwargs: False

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "recovery_blocked")
        self.assertIn(("fence", "native"), services.events)
        self.assertNotIn(("confirm-cleanup", 101), services.events)
        self.assertNotIn(
            ("execute", "native", "startup_catchup"),
            services.events,
        )

    def test_recovery_fences_all_running_attempts_before_any_cleanup(
        self,
    ) -> None:
        services = self._bound_services()
        services.states = {"native": "RUNNING", "v2": "RUNNING"}

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        first_cleanup = min(
            index
            for index, event in enumerate(services.events)
            if isinstance(event, tuple) and event[0] == "cleanup-orphan"
        )
        for event in (("fence", "native"), ("fence", "v2")):
            self.assertLess(services.events.index(event), first_cleanup)


class DailyRuntimeDeadlineCatchupTests(unittest.TestCase):
    def test_0800_unready_inputs_persist_breach_before_waiting(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.native_readiness_ready = False
        services.native_readiness_missing = (
            "daily_target:TB3YWI0C",
        )

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-unready", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "waiting_for_native_readiness")
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertIn(("evaluate-sla-unready", 41), services.events)
        self.assertIn(
            ("alert", "DAILY_TARGET_SLA_BREACHED"),
            services.events,
        )

    def test_replayed_deadline_projections_do_not_realert(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.evaluate_v2_start = lambda **_kwargs: SimpleNamespace(
            status="LATE",
            reason="V2_NOT_STARTED_BY_0745",
            newly_persisted=False,
        )
        services.evaluate_target_sla = lambda **_kwargs: SimpleNamespace(
            status="BREACHED",
            accepted_target_count=0,
            expected_target_count=2,
            reason="TARGETS_INCOMPLETE_AT_DEADLINE",
            newly_persisted=False,
        )

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "breached")
        self.assertFalse(
            any(
                call["code"]
                in {"V2_START_LATE", "DAILY_TARGET_SLA_BREACHED"}
                for call in services.alert_calls
            )
        )

    def test_0810_reentry_catches_up_once_after_visibility_reconcile(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 10, tzinfo=SHANGHAI),
        )
        services.states = {"native": "SUCCESS", "v2": "SUCCESS"}
        services.existing_occurrence_id = 41

        def evaluate_v2(**kwargs):
            services.events.append(
                ("evaluate-v2-catchup", kwargs["item_id"])
            )
            services.item_sla_status["v2"] = "LATE"
            return SimpleNamespace(
                status="LATE",
                reason="NOT_STARTED_BY_GUARDRAIL",
            )

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-catchup", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=1,
                expected_target_count=2,
                reason="TARGETS_MISSING_AT_DEADLINE",
            )

        services.evaluate_v2_start = evaluate_v2
        services.evaluate_target_sla = evaluate_sla
        runtime = DailyRuntime(services)

        first = runtime.run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )
        second = runtime.run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(first.status, "complete")
        self.assertEqual(second.status, "complete")
        self.assertEqual(
            services.events.count(("evaluate-v2-catchup", 2)),
            1,
        )
        self.assertEqual(
            services.events.count(("evaluate-sla-catchup", 41)),
            1,
        )
        self.assertEqual(
            sum(
                call["code"] == "V2_START_LATE"
                for call in services.alert_calls
            ),
            1,
        )
        self.assertEqual(
            sum(
                call["code"] == "DAILY_TARGET_SLA_BREACHED"
                for call in services.alert_calls
            ),
            1,
        )
        self.assertLess(
            services.events.index(("reconcile-visibility", 41)),
            services.events.index(("evaluate-sla-catchup", 41)),
        )

    def test_0759_reentry_does_not_evaluate_0800_sla(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 59, tzinfo=SHANGHAI),
        )
        services.states = {"native": "SUCCESS", "v2": "SUCCESS"}
        services.existing_occurrence_id = 41
        services.evaluate_target_sla = lambda **_kwargs: self.fail(
            "08:00 SLA must not be evaluated early"
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertFalse(
            any(
                isinstance(event, tuple)
                and event[0] == "evaluate-sla"
                for event in services.events
            )
        )

    def test_0830_watchdog_catches_up_sla_before_cutoff_audit(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-catchup", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_MISSING_AT_DEADLINE",
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_watchdog(
            stage="recovery_cutoff",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertLess(
            services.events.index(("reconcile-visibility", 41)),
            services.events.index(("evaluate-sla-catchup", 41)),
        )

    def test_0830_reconciliation_failure_still_catches_up_sla(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.visibility_reconcile_error = RuntimeError(
            "receipt reconciliation unavailable"
        )

        def evaluate_sla(**kwargs):
            services.events.append(
                (
                    "evaluate-sla-after-reconcile-failure",
                    kwargs["occurrence_id"],
                )
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_watchdog(
            stage="recovery_cutoff",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertIsNone(
            result.details["visibility_receipts_reconciled"]
        )
        self.assertIn(
            ("evaluate-sla-after-reconcile-failure", 41),
            services.events,
        )
        self.assertIn(
            ("alert", "VISIBILITY_RECEIPT_RECONCILE_FAILED"),
            services.events,
        )
        self.assertIn(
            ("alert", "DAILY_TARGET_SLA_BREACHED"),
            services.events,
        )


class DailyRuntimeCutoffTests(unittest.TestCase):
    def test_0830_unready_inputs_expire_before_readiness_return(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.native_readiness_ready = False
        services.native_readiness_missing = (
            "daily_target:TB3YWI0C",
        )

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-unready", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "recovery_cutoff")
        self.assertEqual(
            services.states,
            {"native": "EXPIRED", "v2": "EXPIRED"},
        )
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )
        self.assertIn(
            ("alert", "RECOVERY_CUTOFF_INCOMPLETE"),
            services.events,
        )

    def test_0830_expires_pending_without_building_or_starting(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "recovery_cutoff")
        self.assertEqual(services.states, {"native": "EXPIRED", "v2": "EXPIRED"})
        self.assertNotIn(
            ("native-generation", "started"),
            services.events,
        )
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "execute"
                for event in services.events
            )
        )


class DailyRuntimeOwnerTests(unittest.TestCase):
    def test_second_owner_returns_without_touching_occurrence(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 30, tzinfo=SHANGHAI),
        )

        class HeldLock:
            def acquire(self):
                raise OccurrenceLockUnavailable("held")

            def release(self):
                raise AssertionError("unacquired lock must not be released")

        services.occurrence_lock = lambda _business_date: HeldLock()

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "already_owned")
        self.assertNotIn("prepare", services.events)

    def test_cross_day_request_is_rejected_before_lock(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 25, 7, 0, tzinfo=SHANGHAI),
        )

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "cross_day_rejected")
        self.assertIsNone(result.occurrence_id)
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "lock"
                for event in services.events
            )
        )


class DailyRuntimeWatchdogTests(unittest.TestCase):
    def test_0655_unsealed_databridge_is_late_but_not_aborted(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 55, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        result = DailyRuntime(services).run_watchdog(
            stage="databridge_readiness_guardrail",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "late")
        self.assertEqual(
            result.details["databridge_readiness_status"],
            "LATE",
        )
        self.assertEqual(
            result.details["databridge_readiness_reason"],
            "DATABRIDGE_NOT_SEALED_BY_GUARDRAIL",
        )
        self.assertIn(
            ("alert", "DATABRIDGE_READINESS_LATE"),
            services.events,
        )

    def test_0655_sealed_databridge_is_on_time(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 55, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings[2] = "databridge-gen"
        services.databridge_sealed_at = datetime(
            2026,
            7,
            24,
            6,
            54,
            tzinfo=SHANGHAI,
        )

        result = DailyRuntime(services).run_watchdog(
            stage="databridge_readiness_guardrail",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            result.details["databridge_readiness_status"],
            "ON_TIME",
        )
        self.assertNotIn(
            ("alert", "DATABRIDGE_READINESS_LATE"),
            services.events,
        )

    def test_0655_catchup_alert_does_not_stop_new_generation(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 56, tzinfo=SHANGHAI),
        )
        services.recovered_native_generation = SimpleNamespace(
            generation_id="native-before-0631",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            manifest_sha256="a" * 64,
        )

        def refresh(**_kwargs):
            services.events.append("databridge-refresh-after-0655")
            return SimpleNamespace(
                state={"refresh_date": "2026-07-24"}
            )

        services.refresh_databridge = refresh

        result = DailyRuntime(services).run_occurrence(
            run_date="2026-07-24",
            trigger_origin="startup_catchup",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(
            ("alert", "DATABRIDGE_READINESS_LATE"),
            services.events,
        )
        self.assertIn(
            "databridge-refresh-after-0655",
            services.events,
        )
        self.assertIn(
            "databridge-generation",
            services.events,
        )

    def test_0745_existing_occurrence_audits_before_live_control_plane(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 45, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.is_trading_day = lambda _business_date: False

        def load_current_audit_policy():
            services.events.append("current-control-plane")
            raise RuntimeError("current capacity admission drifted")

        def evaluate_v2(**kwargs):
            services.events.append(("evaluate-v2-frozen", kwargs["item_id"]))
            services.item_sla_status["v2"] = "LATE"
            return SimpleNamespace(
                status="LATE",
                reason="V2_NOT_STARTED_BY_0745",
                newly_persisted=True,
            )

        services.load_current_audit_policy = load_current_audit_policy
        services.evaluate_v2_start = evaluate_v2

        result = DailyRuntime(services).run_watchdog(
            stage="v2_start_guardrail",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "late")
        self.assertEqual(services.item_sla_status["v2"], "LATE")
        self.assertNotIn(
            ("trading-day", "2026-07-24"),
            services.events,
        )
        self.assertLess(
            services.events.index(("evaluate-v2-frozen", 2)),
            services.events.index("current-control-plane"),
        )
        self.assertIn(
            ("alert", "DAILY_CONTROL_PLANE_DRIFT"),
            services.events,
        )

    def test_0745_generation_diagnostics_cannot_block_guardrail(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 45, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )

        def evaluate_v2(**kwargs):
            services.events.append(("evaluate-v2-frozen", kwargs["item_id"]))
            services.item_sla_status["v2"] = "LATE"
            return SimpleNamespace(
                status="LATE",
                reason="V2_NOT_STARTED_BY_0745",
                newly_persisted=True,
            )

        services.evaluate_v2_start = evaluate_v2

        result = DailyRuntime(services).run_watchdog(
            stage="v2_start_guardrail",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "late")
        self.assertEqual(services.item_sla_status["v2"], "LATE")
        self.assertLess(
            services.events.index(("evaluate-v2-frozen", 2)),
            services.events.index(
                ("find-unbound-building", "2026-07-24")
            ),
        )
        self.assertIn(
            ("alert", "GENERATION_BUILD_FAILED"),
            services.events,
        )

    def test_0800_existing_occurrence_ignores_live_calendar_flip(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.is_trading_day = lambda _business_date: False

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-frozen", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "breached")
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertNotIn(
            ("trading-day", "2026-07-24"),
            services.events,
        )

    def test_0830_expires_before_generation_diagnostics(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )

        result = DailyRuntime(services).run_watchdog(
            stage="recovery_cutoff",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            services.states,
            {"native": "EXPIRED", "v2": "EXPIRED"},
        )
        self.assertLess(
            services.events.index("occurrence-epoch-validated"),
            services.events.index(("expire", 41, 8)),
        )
        self.assertLess(
            services.events.index(("expire", 41, 8)),
            services.events.index(
                ("find-unbound-building", "2026-07-24")
            ),
        )

    def test_epoch_drift_blocks_cutoff_writes_before_expiry(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        def reject_epoch(*, snapshot) -> None:
            del snapshot
            services.events.append("occurrence-epoch-rejected")
            raise RuntimeError("daily occurrence coordinator epoch differs")

        services.validate_occurrence_epoch = reject_epoch

        with self.assertRaisesRegex(RuntimeError, "epoch differs"):
            DailyRuntime(services).run_watchdog(
                stage="recovery_cutoff",
                run_date="2026-07-24",
            )

        self.assertIn("occurrence-epoch-rejected", services.events)
        self.assertFalse(
            any(
                isinstance(event, tuple)
                and event[0] in {
                    "expire",
                    "reconcile-visibility",
                    "evaluate-sla",
                }
                for event in services.events
            )
        )

    def test_0830_late_write_probe_failure_cannot_block_expiry(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 30, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.detect_late_writes = lambda **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("source audit unavailable"))
        )

        result = DailyRuntime(services).run_watchdog(
            stage="recovery_cutoff",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            services.states,
            {"native": "EXPIRED", "v2": "EXPIRED"},
        )
        self.assertIn(
            ("alert", "ITEM_EXECUTION_AUDIT_FAILURE"),
            services.events,
        )

    def test_all_watchdog_stages_are_idle_on_non_trading_day(self) -> None:
        stage_times = {
            "progress": (7, 0),
            "v2_start_guardrail": (7, 45),
            "target_sla": (8, 0),
            "recovery_cutoff": (8, 30),
        }
        for stage, (hour, minute) in stage_times.items():
            with self.subTest(stage=stage):
                services = _FakeServices(
                    now=datetime(
                        2026,
                        7,
                        24,
                        hour,
                        minute,
                        tzinfo=SHANGHAI,
                    ),
                )
                services.is_trading_day = lambda business_date: False

                result = DailyRuntime(services).run_watchdog(
                    stage=stage,
                    run_date="2026-07-24",
                )

                self.assertEqual(result.status, "non_trading_day")
                self.assertEqual(
                    result.details["reason"],
                    "NON_TRADING_DAY",
                )
                self.assertIn(
                    ("heartbeat", None, "IDLE"),
                    services.events,
                )
                self.assertNotIn(
                    ("alert", "DAILY_OCCURRENCE_MISSING"),
                    services.events,
                )
                occurrence_lookups = [
                    event
                    for event in services.events
                    if (
                        isinstance(event, tuple)
                        and event[0] == "find-occurrence"
                    )
                ]
                self.assertEqual(
                    occurrence_lookups,
                    [("find-occurrence", "2026-07-24")],
                )

    def test_progress_watchdog_uses_frozen_occurrence_before_live_calendar(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.is_trading_day = lambda _business_date: self.fail(
            "frozen occurrence must win over mutable live calendar"
        )

        result = DailyRuntime(services).run_watchdog(
            stage="progress",
            run_date="2026-07-24",
        )

        self.assertEqual(result.occurrence_id, 41)
        self.assertNotEqual(result.status, "non_trading_day")

    def test_missing_live_calendar_is_waiting_not_non_trading_day(
        self,
    ) -> None:
        stage_times = {
            "progress": (7, 0),
            "target_sla": (8, 0),
        }
        for stage, (hour, minute) in stage_times.items():
            with self.subTest(stage=stage):
                services = _FakeServices(
                    now=datetime(
                        2026,
                        7,
                        24,
                        hour,
                        minute,
                        tzinfo=SHANGHAI,
                    ),
                )
                services.trading_day_status = None

                result = DailyRuntime(services).run_watchdog(
                    stage=stage,
                    run_date="2026-07-24",
                )

                self.assertEqual(
                    result.status,
                    "waiting_for_native_readiness",
                )
                self.assertEqual(
                    result.details["missing_requirements"],
                    ["calendar:business_date"],
                )
                self.assertIn(
                    ("heartbeat", None, "WAITING_NATIVE_READINESS"),
                    services.events,
                )
                self.assertIn(
                    ("alert", "DAILY_OCCURRENCE_MISSING"),
                    services.events,
                )

    def test_watchdog_probes_late_writes_from_frozen_occurrence_when_unbound(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.read_frozen_inputs = lambda **_kwargs: self.fail(
            "watchdog must not use live calendar fallback"
        )
        observed: list[dict[str, object]] = []

        def detect(**kwargs):
            observed.append(kwargs)
            return (
                SimpleNamespace(
                    table_name="api_wind_daily",
                    late_row_count=2,
                    latest_write_at="2026-07-24T07:10:00+08:00",
                ),
            )

        services.detect_late_writes = detect

        result = DailyRuntime(services).run_watchdog(
            stage="progress",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "behind")
        self.assertEqual(
            observed,
            [
                {
                    "business_date": date(2026, 7, 24),
                    "feature_date": "2026-07-23",
                }
            ],
        )
        self.assertNotIn(
            "late_source_writes_skipped_reason",
            result.details,
        )
        self.assertEqual(
            result.details["late_source_writes"][0]["late_row_count"],
            2,
        )
        self.assertIn(("alert", "DATA_CONTRACT_BREACH"), services.events)

    def test_watchdog_treats_building_with_active_owner_as_in_progress(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-active",
                generation_type="native_source",
            ),
        )

        class HeldLock:
            def acquire(self):
                raise OccurrenceLockUnavailable("active coordinator")

            def release(self):
                raise AssertionError("unacquired lock must not be released")

        services.occurrence_lock = lambda _business_date: HeldLock()
        services.read_frozen_inputs = lambda **_kwargs: self.fail(
            "watchdog must not read live inputs during active registration"
        )

        result = DailyRuntime(services).run_watchdog(
            stage="progress",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "coordinator_active")
        self.assertNotIn(
            ("alert", "GENERATION_BUILD_FAILED"),
            services.events,
        )

    def test_watchdog_probes_late_writes_even_when_generation_build_failed(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )
        services.read_frozen_inputs = lambda **_kwargs: self.fail(
            "watchdog must not fall back to the live calendar"
        )
        services.detect_late_writes = lambda **_kwargs: (
            SimpleNamespace(
                table_name="api_wind_monthly",
                late_row_count=1,
                latest_write_at="2026-07-24T07:11:00+08:00",
            ),
        )

        result = DailyRuntime(services).run_watchdog(
            stage="progress",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_failed")
        self.assertEqual(
            result.details["reason"],
            "UNBOUND_BUILDING_GENERATION",
        )
        self.assertIn(
            ("alert", "GENERATION_BUILD_FAILED"),
            services.events,
        )
        self.assertIn(
            ("alert", "DATA_CONTRACT_BREACH"),
            services.events,
        )
        self.assertEqual(
            result.details["late_source_writes"][0]["table_name"],
            "api_wind_monthly",
        )

    def test_0700_static_eta_overline_alerts_without_runtime_autotuning(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        result = DailyRuntime(services).run_watchdog(
            stage="progress",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "behind")
        self.assertTrue(result.details["eta_overline"])
        self.assertEqual(
            result.details["eta_basis"],
            "STATIC_TWO_LANE_OPTIMISTIC_LOWER_BOUND",
        )
        self.assertIn(("alert", "DAILY_ETA_OVERLINE"), services.events)

    def test_0745_marks_unstarted_v2_late_and_reports_late_source_writes(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 45, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.detect_late_writes = lambda **_kwargs: (
            SimpleNamespace(
                table_name="api_wind_daily",
                late_row_count=9,
                latest_write_at="2026-07-24T07:10:12+08:00",
            ),
        )
        services.evaluate_v2_start = lambda **_kwargs: SimpleNamespace(
            status="LATE",
            reason="NOT_STARTED_BY_GUARDRAIL",
        )

        result = DailyRuntime(services).run_watchdog(
            stage="v2_start_guardrail",
            run_date="2026-07-24",
        )

        self.assertIsInstance(result, WatchdogResult)
        self.assertEqual(result.status, "late")
        self.assertIn(("alert", "DATA_CONTRACT_BREACH"), services.events)
        self.assertIn(("alert", "V2_START_LATE"), services.events)
        heartbeat = [
            event
            for event in services.events
            if isinstance(event, tuple) and event[0] == "heartbeat"
        ][-1]
        self.assertEqual(heartbeat[2], "WATCHDOG_V2_START_GUARDRAIL")

    def test_0745_reports_stored_late_without_reevaluate_or_realert(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 46, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.item_sla_status["v2"] = "LATE"
        services.evaluate_v2_start = lambda **_kwargs: self.fail(
            "stored LATE must not be evaluated again"
        )

        result = DailyRuntime(services).run_watchdog(
            stage="v2_start_guardrail",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "late")
        self.assertEqual(result.details["late_v2_scheme_ids"], ["v2"])
        self.assertFalse(
            any(
                call["code"] == "V2_START_LATE"
                for call in services.alert_calls
            )
        )

    def test_0800_persists_breached_sla_and_alerts(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        def evaluate(**_kwargs):
            services.events.append(("evaluate-sla-explicit", 41))
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=28,
                expected_target_count=29,
                reason="TARGETS_MISSING_AT_DEADLINE",
            )

        services.evaluate_target_sla = evaluate

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "breached")
        self.assertIn(
            ("alert", "DAILY_TARGET_SLA_BREACHED"),
            services.events,
        )
        breach_alert = next(
            call
            for call in services.alert_calls
            if call["code"] == "DAILY_TARGET_SLA_BREACHED"
        )
        self.assertEqual(
            breach_alert["message"],
            "Daily occurrence accepted 28/29 targets at 08:00",
        )
        self.assertLess(
            services.events.index(("reconcile-visibility", 41)),
            services.events.index(("evaluate-sla-explicit", 41)),
        )

    def test_target_sla_pending_is_not_reported_as_met(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.evaluate_target_sla = lambda **_kwargs: SimpleNamespace(
            status="PENDING",
            accepted_target_count=0,
            expected_target_count=2,
            reason=None,
        )

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.details["sla_outcome"], "PENDING")
        self.assertFalse(
            any(
                call["code"] == "DAILY_TARGET_SLA_BREACHED"
                for call in services.alert_calls
            )
        )

    def test_0800_reconciliation_failure_still_persists_conservative_breach(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.visibility_reconcile_error = RuntimeError(
            "receipt reconciliation unavailable"
        )

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-after-reconcile-failure", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=1,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "breached")
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertIsNone(
            result.details["visibility_receipts_reconciled"]
        )
        self.assertIn(
            ("heartbeat", 41, "VISIBILITY_RECONCILIATION_FAILED"),
            services.events,
        )
        self.assertIn(
            ("alert", "VISIBILITY_RECEIPT_RECONCILE_FAILED"),
            services.events,
        )
        failure_alert = next(
            call
            for call in services.alert_calls
            if call["code"] == "VISIBILITY_RECEIPT_RECONCILE_FAILED"
        )
        self.assertEqual(
            failure_alert["details"]["phase"],
            "target_sla_pre_evaluate",
        )
        self.assertEqual(
            failure_alert["details"]["reason"],
            "VISIBILITY_RECEIPT_RECONCILE_FAILED",
        )
        self.assertIn(
            ("evaluate-sla-after-reconcile-failure", 41),
            services.events,
        )
        self.assertIn(
            ("alert", "DAILY_TARGET_SLA_BREACHED"),
            services.events,
        )

    def test_0800_reconciliation_failure_does_not_override_proven_met(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.visibility_reconcile_error = RuntimeError(
            "receipt reconciliation unavailable"
        )

        def evaluate_sla(**_kwargs):
            services.sla_outcome = "MET"
            return SimpleNamespace(
                status="MET",
                accepted_target_count=2,
                expected_target_count=2,
                reason=None,
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_watchdog(
            stage="target_sla",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "met")
        self.assertEqual(services.sla_outcome, "MET")
        self.assertNotIn(
            ("alert", "DAILY_TARGET_SLA_BREACHED"),
            services.events,
        )


class DailyRuntimeOperatorRecoveryTests(unittest.TestCase):
    def test_operator_recovery_after_cutoff_still_catches_up_sla(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 35, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        def evaluate_sla(**kwargs):
            services.events.append(
                ("evaluate-sla-catchup", kwargs["occurrence_id"])
            )
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_MISSING_AT_DEADLINE",
            )

        services.evaluate_target_sla = evaluate_sla

        result = DailyRuntime(services).run_operator_recovery(
            scheme_id="native",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "recovery_cutoff")
        self.assertEqual(result.occurrence_id, 41)
        self.assertEqual(services.sla_outcome, "BREACHED")
        self.assertLess(
            services.events.index(("reconcile-visibility", 41)),
            services.events.index(("evaluate-sla-catchup", 41)),
        )

    def test_operator_recovery_ignores_unrelated_building_when_bound(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        services.unbound_building_generations = (
            SimpleNamespace(
                generation_id="native-building-stuck",
                generation_type="native_source",
            ),
        )
        native = SimpleNamespace(
            generation_id="native-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:31:00Z",
        )
        databridge = SimpleNamespace(
            generation_id="databridge-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:32:00Z",
        )
        services.resolve_bound_generations = lambda **_kwargs: (
            native,
            databridge,
        )

        result = DailyRuntime(services).run_operator_recovery(
            scheme_id="v2",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(
            ("execute", "v2", "operator_recovery"),
            services.events,
        )

    def test_operator_recovery_executes_only_requested_bound_item(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.bindings = {1: "native-gen", 2: "databridge-gen"}
        native = SimpleNamespace(
            generation_id="native-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:31:00Z",
        )
        databridge = SimpleNamespace(
            generation_id="databridge-gen",
            business_date="2026-07-24",
            feature_date="2026-07-23",
            sealed_at="2026-07-23T22:32:00Z",
        )
        services.resolve_bound_generations = lambda **_kwargs: (
            native,
            databridge,
        )

        result = DailyRuntime(services).run_operator_recovery(
            scheme_id="v2",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            [
                event
                for event in services.events
                if isinstance(event, tuple) and event[0] == "execute"
            ],
            [("execute", "v2", "operator_recovery")],
        )
        self.assertNotIn("prepare", services.events)

    def test_operator_recovery_refuses_unbound_item(self) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41

        result = DailyRuntime(services).run_operator_recovery(
            scheme_id="v2",
            run_date="2026-07-24",
        )

        self.assertEqual(result.status, "generation_unbound")
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "execute"
                for event in services.events
            )
        )


class DailyRuntimePublicEntryTests(unittest.TestCase):
    def test_write_entries_recheck_storage_before_occurrence_work(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
        )

        entries = (
            lambda: run_daily_occurrence(run_date="2026-07-24"),
            lambda: run_operator_recovery(
                scheme_id="v2",
                run_date="2026-07-24",
            ),
        )
        for entry in entries:
            services = _FakeServices(
                now=datetime(
                    2026,
                    7,
                    24,
                    7,
                    20,
                    tzinfo=SHANGHAI,
                ),
            )
            dispose = Mock()
            with (
                self.subTest(entry=entry),
                patch(
                    "scheduler.daily_runtime._runtime_services",
                    return_value=(services, dispose),
                ),
                patch(
                    "scheduler.daily_runtime."
                    "preflight_daily_storage",
                    side_effect=DailyStoragePreflightError(
                        "DAILY_STORAGE_ROOT_NOT_PRIVATE",
                        label="liwei_cache",
                    ),
                ),
                self.assertRaises(
                    DailyStoragePreflightError
                ),
            ):
                entry()
            self.assertEqual(services.events, [])
            dispose.assert_called_once_with()

    def test_dispatch_entries_require_inner_runtime_authority(
        self,
    ) -> None:
        entries = (
            lambda: run_daily_occurrence(run_date="2026-07-24"),
            lambda: run_daily_watchdog(
                stage="progress",
                run_date="2026-07-24",
            ),
            lambda: run_operator_recovery(
                scheme_id="v2",
                run_date="2026-07-24",
            ),
            lambda: run_scheduler_heartbeat(
                run_date="2026-07-24",
            ),
        )
        with patch(
            "scheduler.daily_runtime._require_production_entry_authority",
            side_effect=RuntimeError("capacity blocked"),
            create=True,
        ) as authority:
            for entry in entries:
                with self.subTest(entry=entry), self.assertRaisesRegex(
                    RuntimeError,
                    "capacity blocked",
                ):
                    entry()

        self.assertEqual(authority.call_count, len(entries))

    def test_watchdog_audits_before_current_capacity_authority(
        self,
    ) -> None:
        services = _FakeServices(
            now=datetime(2026, 7, 24, 8, 0, tzinfo=SHANGHAI),
        )
        services.existing_occurrence_id = 41
        services.close = lambda: None

        def evaluate_sla(**_kwargs):
            services.sla_outcome = "BREACHED"
            return SimpleNamespace(
                status="BREACHED",
                accepted_target_count=0,
                expected_target_count=2,
                reason="TARGETS_INCOMPLETE_AT_DEADLINE",
                newly_persisted=True,
            )

        services.evaluate_target_sla = evaluate_sla
        with (
            patch(
                "scheduler.daily_runtime.DefaultDailyRuntimeServices",
                return_value=services,
            ),
            patch(
                "scheduler.daily_runtime._require_ledger_runtime_mode",
            ) as ledger_mode,
            patch(
                "scheduler.daily_runtime."
                "_require_production_entry_authority",
                side_effect=RuntimeError("capacity blocked"),
            ) as authority,
        ):
            result = run_daily_watchdog(
                stage="target_sla",
                run_date="2026-07-24",
            )

        self.assertEqual(result.status, "breached")
        ledger_mode.assert_called_once_with()
        authority.assert_not_called()

    def test_public_entries_share_injected_runtime_without_external_state(
        self,
    ) -> None:
        occurrence_services = _FakeServices(
            now=datetime(2026, 7, 24, 6, 29, tzinfo=SHANGHAI),
        )
        occurrence = run_daily_occurrence(
            run_date="2026-07-24",
            _services=occurrence_services,
        )
        self.assertEqual(occurrence.status, "not_before")

        watchdog_services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 0, tzinfo=SHANGHAI),
        )
        watchdog = run_daily_watchdog(
            stage="progress",
            run_date="2026-07-24",
            _services=watchdog_services,
        )
        self.assertEqual(watchdog.status, "missing")

        recovery_services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        recovery = run_operator_recovery(
            scheme_id="v2",
            run_date="2026-07-24",
            _services=recovery_services,
        )
        self.assertEqual(recovery.status, "occurrence_missing")

        heartbeat_services = _FakeServices(
            now=datetime(2026, 7, 24, 7, 20, tzinfo=SHANGHAI),
        )
        heartbeat = run_scheduler_heartbeat(
            run_date="2026-07-24",
            _services=heartbeat_services,
        )
        self.assertEqual(heartbeat["state"], "IDLE")
        self.assertIn(("heartbeat", None, "IDLE"), heartbeat_services.events)


if __name__ == "__main__":
    unittest.main()
