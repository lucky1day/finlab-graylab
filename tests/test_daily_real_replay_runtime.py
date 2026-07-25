from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import text
from scheduler.daily_ledger import freeze_active_daily_registry
from scheduler.daily_runtime import _policy_payload
from tests.test_daily_native_coordinator_mysql import (
    MIGRATIONS,
    _seed_test_registry,
    _temporary_mysql,
    _real_policy_and_configs,
    apply_migration_files,
)
from tests.test_daily_real_replay_gate import (
    TEST_EPOCH,
    _generation_fixture,
    _isolated_engine,
    _tamper_generation_file,
    _verified_isolation,
)


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


@contextmanager
def _replay_inputs():
    from harness.daily_real_replay import open_real_replay_generations

    fixture = _generation_fixture()
    with tempfile.TemporaryDirectory() as tmpdir:
        native, databridge = fixture._delivery_generation(Path(tmpdir))
        yield open_real_replay_generations(
            native_manifest=native.manifest_path,
            databridge_manifest=databridge.manifest_path,
        )


def _next_midnight_utc(observed_at: datetime) -> datetime:
    local_date = observed_at.astimezone(SHANGHAI_TIMEZONE).date()
    return datetime.combine(
        local_date + timedelta(days=1),
        time.min,
        tzinfo=SHANGHAI_TIMEZONE,
    ).astimezone(timezone.utc)


def _runtime_snapshot(
    *,
    policy,
    configs,
    inputs,
    observed_at: datetime,
    states: dict[str, str] | None = None,
    projection_overrides: dict[str, object] | None = None,
    input_generation_overrides: dict[str, str] | None = None,
):
    from harness.daily_real_replay import (
        REAL_REPLAY_SCHEMA_VERSION,
        _build_real_replay_occurrence_args,
        _iso_utc_datetime,
    )

    occurrence_args = _build_real_replay_occurrence_args(
        policy=policy,
        configs=configs,
        inputs=inputs,
        schedule_key="isolated-real-replay-v1-runtime",
        opened_at=observed_at,
        epoch_payload=TEST_EPOCH,
    )
    policy_json = occurrence_args["policy_json"]
    projection = policy_json["real_replay_projection"]
    projection.update(projection_overrides or {})
    state_by_scheme = dict(states or {})
    generation_overrides = dict(input_generation_overrides or {})
    item_summaries = []
    envelope_parts: dict[int, tuple[object, tuple[object, ...]]] = {}
    registry_rows: list[dict[str, object]] = []
    accepted_target_count = 0
    state_counts: dict[str, int] = {}
    for item_id, scheme_id in enumerate(
        sorted(policy.schemes),
        start=1,
    ):
        scheme_policy = policy.schemes[scheme_id]
        config = configs[scheme_id]
        state = state_by_scheme.get(scheme_id, "PENDING")
        target_count = len(scheme_policy.target_tenors)
        accepted_count = target_count if state == "SUCCESS" else 0
        accepted_target_count += accepted_count
        state_counts[state] = state_counts.get(state, 0) + 1
        policy_item = occurrence_args["item_policy_by_base"][scheme_id]
        default_generation_id = (
            inputs.databridge_generation.generation_id
            if scheme_policy.runtime_type == "blackbox_v2"
            else inputs.native_generation.generation_id
        )
        v2_started = (
            scheme_policy.runtime_type == "blackbox_v2"
            and state != "PENDING"
        )
        initial_release_at = policy_item["release_at"]
        finalized_release_at = (
            max(
                initial_release_at,
                _iso_utc_datetime(
                    inputs.databridge_generation.sealed_at,
                    field="databridge.sealed_at",
                )
                + timedelta(
                    minutes=int(
                        scheme_policy.v2_release_offset_min or 0
                    )
                ),
            )
            if scheme_policy.runtime_type == "blackbox_v2"
            else initial_release_at
        )
        item = SimpleNamespace(
                    item_id=item_id,
                    occurrence_id=41,
                    base_scheme_id=scheme_id,
                    runtime_type=scheme_policy.runtime_type,
                    scheme_version=config.scheme_version,
                    code_sha256=config.code_hash,
                    config_sha256=config.config_hash,
                    cache_group=scheme_policy.cache_group,
                    input_generation_id=generation_overrides.get(
                        scheme_id,
                        default_generation_id,
                    ),
                    resource_class=scheme_policy.resource_class,
                    internal_workers=scheme_policy.internal_workers,
                    release_offset_minutes=int(
                        scheme_policy.v2_release_offset_min or 0
                    ),
                    release_at=finalized_release_at.replace(
                        tzinfo=None
                    ),
                    deadline_at=policy_item["deadline_at"].replace(
                        tzinfo=None
                    ),
                    recovery_cutoff_at=occurrence_args[
                        "recovery_cutoff_at"
                    ].replace(tzinfo=None),
                    occurrence_sla_deadline_at=occurrence_args[
                        "sla_deadline_at"
                    ].replace(tzinfo=None),
                    state=state,
                    sla_status="LATE" if v2_started else "PENDING",
                    late_reason=(
                        "V2_STARTED_AFTER_0745"
                        if v2_started
                        else None
                    ),
                    sla_evaluated_at=(
                        observed_at.replace(tzinfo=None)
                        if v2_started
                        else None
                    ),
                    attempt_no=0 if state == "PENDING" else 1,
                    current_run_id=(
                        None if state == "PENDING" else item_id
                    ),
                )
        targets = tuple(
            SimpleNamespace(
                registry_scheme_id=(
                    f"{scheme_id}__h"
                    f"{int(scheme_policy.horizon)}__{tenor}"
                ),
                base_scheme_id=scheme_id,
                runtime_type=scheme_policy.runtime_type,
                task_type=scheme_policy.task_type,
                target_tenor=tenor,
                horizon=int(scheme_policy.horizon),
                target_date=occurrence_args["target_dates"][
                    (
                        f"{scheme_id}__h"
                        f"{int(scheme_policy.horizon)}__{tenor}"
                    )
                ],
            )
            for tenor in scheme_policy.target_tenors
        )
        envelope_parts[item_id] = (item, targets)
        for target in targets:
            registry_rows.append(
                {
                    "status": "active",
                    "frequency": "daily",
                    "scheme_id": target.registry_scheme_id,
                    "base_scheme_id": scheme_id,
                    "runtime_type": scheme_policy.runtime_type,
                    "task_type": target.task_type,
                    "target_tenor": target.target_tenor,
                    "horizon": target.horizon,
                    "target_date": target.target_date,
                    "scheme_version": config.scheme_version,
                    "code_sha256": config.code_hash,
                    "config_sha256": config.config_hash,
                    "cache_group": scheme_policy.cache_group,
                    "resource_class": scheme_policy.resource_class,
                    "internal_workers": scheme_policy.internal_workers,
                    "release_offset_minutes": int(
                        scheme_policy.v2_release_offset_min or 0
                    ),
                    "release_at": policy_item["release_at"]
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(sep=" "),
                    "deadline_at": policy_item["deadline_at"]
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(sep=" "),
                }
            )
        item_summaries.append(
            SimpleNamespace(
                item=item,
                target_count=target_count,
                accepted_target_count=accepted_count,
            )
        )
    completion_state = (
        "SUCCESS"
        if accepted_target_count == 25
        else (
            "FAILED"
            if any(
                state in {"FAILED_TERMINAL", "EXPIRED"}
                for state in state_by_scheme.values()
            )
            else "PENDING"
        )
    )
    cutoff = occurrence_args["recovery_cutoff_at"].replace(tzinfo=None)
    occurrence = SimpleNamespace(
        occurrence_id=41,
        schedule_key="isolated-real-replay-v1-runtime",
        predict_date=inputs.business_date,
        feature_date=inputs.feature_date,
        policy_version=policy.version,
        policy_sha256=hashlib.sha256(
            json.dumps(
                policy_json,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        policy_json=policy_json,
        registry_digest=freeze_active_daily_registry(
            registry_rows
        ).registry_digest,
        completion_state=completion_state,
        expected_item_count=21,
        expected_target_count=25,
        accepted_target_count=accepted_target_count,
        sla_accepted_target_count=None,
        sla_deadline_at=cutoff,
        recovery_cutoff_at=cutoff,
        sla_outcome="PENDING",
        sla_evaluated_at=None,
    )
    snapshot = SimpleNamespace(
        occurrence=occurrence,
        items=tuple(item_summaries),
        actual_item_count=21,
        actual_target_count=25,
        actual_accepted_target_count=accepted_target_count,
        item_state_counts=tuple(sorted(state_counts.items())),
    )
    native_generation = SimpleNamespace(
        generation_id=inputs.native_generation.generation_id,
        manifest_sha256=inputs.native_generation.manifest_sha256,
        business_date=inputs.business_date,
        feature_date=inputs.feature_date,
        state="SEALED",
        sealed_at=_iso_utc_datetime(
            inputs.native_generation.sealed_at,
            field="native.sealed_at",
        ),
    )
    databridge_generation = SimpleNamespace(
        generation_id=inputs.databridge_generation.generation_id,
        manifest_sha256=inputs.databridge_generation.manifest_sha256,
        business_date=inputs.business_date,
        feature_date=inputs.feature_date,
        state="SEALED",
        sealed_at=_iso_utc_datetime(
            inputs.databridge_generation.sealed_at,
            field="databridge.sealed_at",
        ),
    )
    snapshot.execution_envelopes = {
        item_id: SimpleNamespace(
            occurrence=occurrence,
            item=item,
            generation=(
                databridge_generation
                if item.runtime_type == "blackbox_v2"
                else native_generation
            ),
            calendar_generation=native_generation,
            targets=targets,
        )
        for item_id, (item, targets) in envelope_parts.items()
    }
    return snapshot


class _AcquiredOwnerLock:
    acquired = True

    def acquire(self):
        return self

    def release(self) -> None:
        self.acquired = False


@contextmanager
def _runtime_isolation(engine):
    from harness.daily_real_replay import bind_real_replay_epoch

    with _verified_isolation(engine) as (isolation, listen):
        bind_real_replay_epoch(
            engine,
            isolation=isolation,
            epoch_payload=TEST_EPOCH,
        )
        with patch(
            "harness.daily_real_replay._real_replay_lock_root",
            return_value=Path(isolation.datadir).resolve().parent,
        ):
            yield isolation, listen


@contextmanager
def _runtime_repository(snapshot_or_provider):
    provider = (
        snapshot_or_provider
        if callable(snapshot_or_provider)
        else lambda: snapshot_or_provider
    )

    def read_snapshot(*_args, **_kwargs):
        return provider()

    def read_envelope(*_args, item_id, **_kwargs):
        return provider().execution_envelopes[int(item_id)]

    with (
        patch(
            "harness.daily_real_replay."
            "_repository_read_schedule_occurrence_snapshot",
            side_effect=read_snapshot,
        ),
        patch(
            "harness.daily_real_replay."
            "_repository_read_schedule_execution_envelope",
            side_effect=read_envelope,
        ),
    ):
        yield


class DailyRealReplayRuntimeContractTests(unittest.TestCase):
    def test_public_surface_has_no_dependency_injection_seams(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        constructor = inspect.signature(RealReplayRuntime).parameters
        run = inspect.signature(RealReplayRuntime.run).parameters

        self.assertEqual(
            tuple(constructor),
            (
                "engine",
                "isolation",
                "occurrence_id",
                "policy",
                "configs",
                "inputs",
            ),
        )
        self.assertEqual(tuple(run), ("self",))
        self.assertFalse(hasattr(RealReplayRuntime, "execute_item"))
        forbidden = {
            "services",
            "executor",
            "runner",
            "trusted_verifier",
            "clock",
            "now",
            "callbacks",
            "project_root",
            "databridge_schema_path",
            "algo_env",
            "trigger_origin",
        }
        self.assertFalse(forbidden & set(constructor))
        self.assertFalse(forbidden & set(run))
        self.assertFalse(
            any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in (*constructor.values(), *run.values())
            )
        )

    def test_constructor_rehashes_generation_before_ledger_reads(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        engine = _isolated_engine()
        with _replay_inputs() as inputs:
            _tamper_generation_file(
                inputs.databridge_generation.data_dir
                / "daily_output.csv"
            )
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                patch(
                    "harness.daily_real_replay."
                    "_repository_read_schedule_occurrence_snapshot",
                ) as read_snapshot,
                self.assertRaisesRegex(
                    DailyRealReplayError,
                    "generation manifest revalidation failed",
                ),
            ):
                RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                )
            read_snapshot.assert_not_called()

    def test_historical_predict_date_uses_canonical_executor(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        v2_scheme = next(
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "blackbox_v2"
        )
        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )
            initial = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: (
                        "PENDING"
                        if scheme_id == v2_scheme
                        else "SUCCESS"
                    )
                    for scheme_id in policy.schemes
                },
            )
            complete = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            engine = _isolated_engine()
            executed = False

            def read_snapshot(*_args, **_kwargs):
                return complete if executed else initial

            def execute(*_args, **_kwargs):
                nonlocal executed
                executed = True
                return SimpleNamespace(
                    status="success",
                    scheme_id=v2_scheme,
                )

            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.qualification, "EXCLUDED")
        canonical.assert_called_once()
        args, kwargs = canonical.call_args
        self.assertEqual(args, (engine,))
        self.assertEqual(kwargs["trigger_origin"], "operator_recovery")
        self.assertNotIn("trusted_verifier", kwargs)

    def test_revalidates_item_envelope_after_constructor(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        v2_scheme = next(
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "blackbox_v2"
        )
        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: (
                        "PENDING"
                        if scheme_id == v2_scheme
                        else "SUCCESS"
                    )
                    for scheme_id in policy.schemes
                },
            )
            pending_item = next(
                summary.item
                for summary in snapshot.items
                if summary.item.base_scheme_id == v2_scheme
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                runtime = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                )
                envelope = snapshot.execution_envelopes[
                    pending_item.item_id
                ]
                envelope.targets[0].target_tenor = "FORGED"
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "execution envelope target identity drifted",
                ):
                    runtime.run()

        canonical.assert_not_called()

    def test_exact_cutoff_starts_no_executor(self) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        before_cutoff = datetime.now(timezone.utc)
        cutoff = _next_midnight_utc(before_cutoff)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=before_cutoff,
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    side_effect=(before_cutoff, before_cutoff, cutoff),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "cutoff_incomplete")
        canonical.assert_not_called()

    def test_owner_lock_blocks_all_executor_calls(self) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import (
            OccurrenceLockUnavailable,
        )

        policy, configs = _real_policy_and_configs()
        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
            )
            lock = SimpleNamespace(
                acquire=Mock(
                    side_effect=OccurrenceLockUnavailable("held")
                ),
                release=Mock(),
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=lock,
                ) as lock_factory,
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "recovery_blocked")
        canonical.assert_not_called()
        lock.release.assert_not_called()
        lock_path = lock_factory.call_args.args[0]
        self.assertEqual(lock_path.name, "real-replay-runtime.lock")
        self.assertNotIn("41", lock_path.name)

    def test_retry_wait_does_not_block_independent_first_attempt(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        candidates = dispatch_order(
            policy,
            (
                scheme_id
                for scheme_id, item in policy.schemes.items()
                if item.runtime_type == "blackbox_v2"
            ),
        )[:2]
        failed_scheme, successful_scheme = candidates
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in candidates
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )
            item_id_by_scheme = {
                scheme_id: item_id
                for item_id, scheme_id in enumerate(
                    sorted(policy.schemes),
                    start=1,
                )
            }

            def read_snapshot(*_args, **_kwargs):
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=states,
                )

            def execute(_engine, *, item_id, **_kwargs):
                scheme_id = next(
                    scheme
                    for scheme, expected_id
                    in item_id_by_scheme.items()
                    if expected_id == item_id
                )
                if scheme_id == failed_scheme:
                    states[scheme_id] = "RETRY_WAIT"
                    return SimpleNamespace(
                        status="retry_wait",
                        scheme_id=scheme_id,
                    )
                states[scheme_id] = "SUCCESS"
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            result.dispatched_scheme_ids,
            (failed_scheme, successful_scheme),
        )
        self.assertEqual(result.failed_scheme_ids, (failed_scheme,))
        self.assertEqual(canonical.call_count, 2)

    def test_waits_for_frozen_future_v2_release(self) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        v2_scheme = next(
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "blackbox_v2"
            and int(item.v2_release_offset_min or 0) == 2
        )
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id == v2_scheme
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        with _replay_inputs() as inputs:
            observed_at = datetime.now(timezone.utc)

            def read_snapshot(*_args, **_kwargs):
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=states,
                )

            initial = read_snapshot()
            release_at = next(
                summary.item.release_at.replace(tzinfo=timezone.utc)
                for summary in initial.items
                if summary.item.base_scheme_id == v2_scheme
            )
            self.assertGreater(release_at, observed_at)
            after_release = release_at + timedelta(microseconds=1)
            item_id = next(
                summary.item.item_id
                for summary in initial.items
                if summary.item.base_scheme_id == v2_scheme
            )

            def execute(_engine, *, item_id: int, **_kwargs):
                states[v2_scheme] = "SUCCESS"
                return SimpleNamespace(
                    status="success",
                    scheme_id=v2_scheme,
                    item_id=item_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    side_effect=(
                        observed_at,
                        observed_at,
                        after_release,
                        after_release,
                        after_release,
                    ),
                ),
                patch(
                    "harness.daily_real_replay._wait_for_release"
                ) as wait_for_release,
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        wait_for_release.assert_called()
        canonical.assert_called_once()
        self.assertEqual(canonical.call_args.kwargs["item_id"], item_id)

    def test_runs_approved_native_and_v2_pools_with_two_lanes(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        ordered = dispatch_order(policy, policy.schemes)
        v2_schemes = tuple(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "blackbox_v2"
        )[:2]
        native_heavy = next(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "native_adapter"
            and policy.schemes[scheme_id].resource_class == "native_heavy"
            and policy.schemes[scheme_id].cache_prerequisite
        )
        native_light = next(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "native_adapter"
            and policy.schemes[scheme_id].resource_class == "native_light"
        )
        selected = (*v2_schemes, native_heavy, native_light)
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        item_id_by_scheme = {
            scheme_id: item_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for scheme_id, item_id in item_id_by_scheme.items()
        }
        active_by_pool = {"native": 0, "v2": 0}
        max_active_by_pool = {"native": 0, "v2": 0}
        active_total = 0
        max_active_total = 0
        all_started = threading.Event()
        state_lock = threading.Lock()

        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=current_states,
                )

            def execute(_engine, *, item_id: int, **_kwargs):
                nonlocal active_total, max_active_total
                scheme_id = scheme_by_item_id[item_id]
                pool = (
                    "v2"
                    if policy.schemes[scheme_id].runtime_type
                    == "blackbox_v2"
                    else "native"
                )
                with state_lock:
                    states[scheme_id] = "RUNNING"
                    active_by_pool[pool] += 1
                    active_total += 1
                    max_active_by_pool[pool] = max(
                        max_active_by_pool[pool],
                        active_by_pool[pool],
                    )
                    max_active_total = max(
                        max_active_total,
                        active_total,
                    )
                    if active_total == 4:
                        all_started.set()
                all_started.wait(timeout=1.0)
                with state_lock:
                    states[scheme_id] = "SUCCESS"
                    active_by_pool[pool] -= 1
                    active_total -= 1
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                    item_id=item_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        self.assertEqual(max_active_by_pool, {"native": 2, "v2": 2})
        self.assertEqual(max_active_total, 4)
        self.assertEqual(canonical.call_count, 4)
        self.assertEqual(
            set(result.dispatched_scheme_ids),
            set(selected),
        )

    def test_worker_exception_drains_active_and_stops_new_dispatch(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        selected = dispatch_order(
            policy,
            (
                scheme_id
                for scheme_id, item in policy.schemes.items()
                if item.runtime_type == "blackbox_v2"
            ),
        )[:3]
        failed_scheme = selected[0]
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        active: set[str] = set()
        both_started = threading.Event()
        state_lock = threading.Lock()

        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=current_states,
                )

            def execute(_engine, *, item_id: int, **_kwargs):
                scheme_id = scheme_by_item_id[item_id]
                with state_lock:
                    states[scheme_id] = "RUNNING"
                    active.add(scheme_id)
                    if len(active) == 2:
                        both_started.set()
                both_started.wait(timeout=1.0)
                with state_lock:
                    active.discard(scheme_id)
                    states[scheme_id] = (
                        "ABANDONED"
                        if scheme_id == failed_scheme
                        else "SUCCESS"
                    )
                if scheme_id == failed_scheme:
                    raise RuntimeError("worker audit failed")
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                )

            engine = _isolated_engine()
            owner = _AcquiredOwnerLock()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=owner,
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "integrity_error")
        self.assertEqual(canonical.call_count, 2)
        self.assertNotIn(selected[2], result.dispatched_scheme_ids)
        self.assertIn(failed_scheme, result.blocked_scheme_ids)
        self.assertFalse(owner.acquired)

    def test_cross_pool_failure_fence_blocks_newly_released_v2(
        self,
    ) -> None:
        import harness.daily_real_replay as replay_module
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        ordered = dispatch_order(policy, policy.schemes)
        heavy = next(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "native_adapter"
            and policy.schemes[scheme_id].resource_class == "native_heavy"
            and policy.schemes[scheme_id].cache_prerequisite
        )
        light = next(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "native_adapter"
            and policy.schemes[scheme_id].resource_class == "native_light"
        )
        future_v2 = next(
            scheme_id
            for scheme_id in ordered
            if policy.schemes[scheme_id].runtime_type == "blackbox_v2"
            and int(
                policy.schemes[scheme_id].v2_release_offset_min or 0
            )
            == 2
        )
        selected = (heavy, light, future_v2)
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        state_lock = threading.Lock()
        native_started: set[str] = set()
        both_native_started = threading.Event()
        release_failure = threading.Event()
        failure_fenced = threading.Event()
        finish_light = threading.Event()

        with _replay_inputs() as inputs:
            before_release = datetime.now(timezone.utc)

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=before_release,
                    states=current_states,
                )

            initial = read_snapshot()
            v2_release_at = next(
                summary.item.release_at.replace(
                    tzinfo=timezone.utc
                )
                for summary in initial.items
                if summary.item.base_scheme_id == future_v2
            )
            self.assertGreater(v2_release_at, before_release)
            after_release = v2_release_at + timedelta(microseconds=1)

            def execute(_engine, *, item_id: int, **_kwargs):
                scheme_id = scheme_by_item_id[item_id]
                with state_lock:
                    states[scheme_id] = "RUNNING"
                    native_started.add(scheme_id)
                    if native_started == {heavy, light}:
                        both_native_started.set()
                if scheme_id == heavy:
                    release_failure.wait(timeout=2.0)
                    with state_lock:
                        states[scheme_id] = "ABANDONED"
                    raise RuntimeError("cross-pool worker failed")
                finish_light.wait(timeout=2.0)
                with state_lock:
                    states[scheme_id] = "SUCCESS"
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                )

            original_signal = replay_module._set_replay_stop_signal

            def set_stop_signal(dispatch_lock, stop_signal):
                original_signal(dispatch_lock, stop_signal)
                failure_fenced.set()

            def now_utc():
                if (
                    threading.current_thread().name
                    == "MainThread"
                    and both_native_started.is_set()
                ):
                    release_failure.set()
                    failure_fenced.wait(timeout=2.0)
                    finish_light.set()
                    return after_release
                return before_release

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "harness.daily_real_replay."
                    "_set_replay_stop_signal",
                    side_effect=set_stop_signal,
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    side_effect=now_utc,
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        called_schemes = {
            scheme_by_item_id[call.kwargs["item_id"]]
            for call in canonical.call_args_list
        }
        self.assertEqual(result.status, "integrity_error")
        self.assertEqual(called_schemes, {heavy, light})
        self.assertNotIn(future_v2, result.dispatched_scheme_ids)
        self.assertTrue(failure_fenced.is_set())

    def test_integrity_failure_is_not_overwritten_by_complete(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        selected = dispatch_order(
            policy,
            (
                scheme_id
                for scheme_id, item in policy.schemes.items()
                if item.runtime_type == "blackbox_v2"
            ),
        )[:2]
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        status_by_scheme = {
            selected[0]: "claim_rejected",
            selected[1]: "unexpected_status",
        }
        state_lock = threading.Lock()
        both_started = threading.Barrier(2)

        with _replay_inputs() as inputs:
            observed_at = (
                datetime.now(timezone.utc) + timedelta(minutes=7)
            )

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=current_states,
                )

            def execute(_engine, *, item_id: int, **_kwargs):
                scheme_id = scheme_by_item_id[item_id]
                with state_lock:
                    states[scheme_id] = "RUNNING"
                both_started.wait(timeout=1.0)
                with state_lock:
                    states[scheme_id] = "SUCCESS"
                return SimpleNamespace(
                    status=status_by_scheme[scheme_id],
                    scheme_id=scheme_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "integrity_error")
        self.assertEqual(
            set(result.blocked_scheme_ids),
            set(selected),
        )

    def test_pool_creation_failure_releases_owner_and_first_pool(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        with _replay_inputs() as inputs:
            observed_at = datetime.now(timezone.utc)
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
            )
            engine = _isolated_engine()
            owner = _AcquiredOwnerLock()
            first_pool = Mock()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=owner,
                ),
                patch(
                    "harness.daily_real_replay.ThreadPoolExecutor",
                    side_effect=(
                        first_pool,
                        RuntimeError("v2 pool unavailable"),
                    ),
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                runtime = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "v2 pool unavailable",
                ):
                    runtime.run()

        first_pool.shutdown.assert_called_once_with(
            wait=True,
            cancel_futures=False,
        )
        self.assertFalse(owner.acquired)

    def test_pool_shutdown_failure_still_releases_owner(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        with _replay_inputs() as inputs:
            observed_at = datetime.now(timezone.utc)
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            engine = _isolated_engine()
            owner = _AcquiredOwnerLock()
            native_pool = Mock()
            native_pool.shutdown.side_effect = RuntimeError(
                "native shutdown failed"
            )
            v2_pool = Mock()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=owner,
                ),
                patch(
                    "harness.daily_real_replay.ThreadPoolExecutor",
                    side_effect=(native_pool, v2_pool),
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                runtime = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "native shutdown failed",
                ):
                    runtime.run()

        native_pool.shutdown.assert_called_once()
        v2_pool.shutdown.assert_called_once()
        self.assertFalse(owner.acquired)

    def test_worker_cutoff_handoff_returns_structured_result(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        v2_scheme = next(
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "blackbox_v2"
        )
        with _replay_inputs() as inputs:
            before_cutoff = datetime.now(timezone.utc)
            cutoff = _next_midnight_utc(before_cutoff)
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=before_cutoff,
                states={
                    scheme_id: (
                        "PENDING"
                        if scheme_id == v2_scheme
                        else "SUCCESS"
                    )
                    for scheme_id in policy.schemes
                },
            )

            def now_utc():
                if threading.current_thread().name.startswith(
                    "real-replay-"
                ):
                    return cutoff
                return before_cutoff

            engine = _isolated_engine()
            owner = _AcquiredOwnerLock()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=owner,
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    side_effect=now_utc,
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "cutoff_incomplete")
        self.assertEqual(result.blocked_scheme_ids, (v2_scheme,))
        canonical.assert_not_called()
        self.assertFalse(owner.acquired)

    def test_disallowed_native_heavy_pair_never_overlaps(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime
        from scheduler.daily_coordinator import dispatch_order

        policy, configs = _real_policy_and_configs()
        selected = tuple(
            scheme_id
            for scheme_id in dispatch_order(policy, policy.schemes)
            if policy.schemes[scheme_id].runtime_type == "native_adapter"
            and policy.schemes[scheme_id].resource_class == "native_heavy"
            and policy.schemes[scheme_id].cache_prerequisite
        )[:2]
        self.assertEqual(len(selected), 2)
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        active_count = 0
        max_active = 0
        state_lock = threading.Lock()
        with _replay_inputs() as inputs:
            observed_at = datetime.now(timezone.utc)

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=current_states,
                )

            def execute(_engine, *, item_id: int, **_kwargs):
                nonlocal active_count, max_active
                scheme_id = scheme_by_item_id[item_id]
                with state_lock:
                    states[scheme_id] = "RUNNING"
                    active_count += 1
                    max_active = max(max_active, active_count)
                threading.Event().wait(0.05)
                with state_lock:
                    states[scheme_id] = "SUCCESS"
                    active_count -= 1
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        self.assertEqual(max_active, 1)
        self.assertEqual(
            result.dispatched_scheme_ids,
            selected,
        )

    def test_cache_consumer_waits_for_prewarmer_success(
        self,
    ) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        prewarmer = next(
            item
            for item in policy.schemes.values()
            if item.runtime_type == "native_adapter"
            and item.cache_prerequisite
            and any(
                peer.runtime_type == "native_adapter"
                and peer.cache_group == item.cache_group
                and not peer.cache_prerequisite
                for peer in policy.schemes.values()
            )
        )
        consumer = next(
            item
            for item in policy.schemes.values()
            if item.runtime_type == "native_adapter"
            and item.cache_group == prewarmer.cache_group
            and not item.cache_prerequisite
        )
        selected = (prewarmer.scheme_id, consumer.scheme_id)
        states = {
            scheme_id: (
                "PENDING"
                if scheme_id in selected
                else "SUCCESS"
            )
            for scheme_id in policy.schemes
        }
        scheme_by_item_id = {
            item_id: scheme_id
            for item_id, scheme_id in enumerate(
                sorted(policy.schemes),
                start=1,
            )
        }
        execution_order: list[str] = []
        active_count = 0
        max_active = 0
        state_lock = threading.Lock()
        with _replay_inputs() as inputs:
            observed_at = datetime.now(timezone.utc)

            def read_snapshot(*_args, **_kwargs):
                with state_lock:
                    current_states = dict(states)
                return _runtime_snapshot(
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    observed_at=observed_at,
                    states=current_states,
                )

            def execute(_engine, *, item_id: int, **_kwargs):
                nonlocal active_count, max_active
                scheme_id = scheme_by_item_id[item_id]
                with state_lock:
                    execution_order.append(scheme_id)
                    states[scheme_id] = "RUNNING"
                    active_count += 1
                    max_active = max(max_active, active_count)
                threading.Event().wait(0.05)
                with state_lock:
                    states[scheme_id] = "SUCCESS"
                    active_count -= 1
                return SimpleNamespace(
                    status="success",
                    scheme_id=scheme_id,
                )

            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(read_snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                    side_effect=execute,
                ),
                patch(
                    "harness.daily_real_replay._now_utc",
                    return_value=observed_at,
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        self.assertEqual(execution_order, list(selected))
        self.assertEqual(max_active, 1)

    def test_rejects_incomplete_generation_identity(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        native_scheme = next(
            scheme_id
            for scheme_id, item in policy.schemes.items()
            if item.runtime_type == "native_adapter"
        )
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                input_generation_overrides={
                    native_scheme: "forged-native-generation"
                },
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "generation",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )


    def test_rejects_non_excluded_projection(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                projection_overrides={
                    "sla_qualification": "ADMITTED",
                },
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "qualification",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )
        canonical.assert_not_called()

    def test_complete_reentry_dispatches_nothing(self) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        canonical.assert_not_called()

    def test_does_not_enter_production_control_plane(self) -> None:
        from harness.daily_real_replay import RealReplayRuntime

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
                patch("scheduler.daily_runtime.DailyRuntime") as production,
                patch(
                    "scheduler.daily_runtime."
                    "DefaultDailyRuntimeServices"
                ) as services,
                patch(
                    "scheduler.repository.expire_schedule_items"
                ) as expire,
                patch(
                    "scheduler.repository."
                    "evaluate_schedule_occurrence_target_sla"
                ) as occurrence_sla,
                patch(
                    "scheduler.repository."
                    "evaluate_schedule_item_start_sla"
                ) as item_sla,
                patch(
                    "scheduler.repository."
                    "upsert_scheduler_heartbeat"
                ) as heartbeat,
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()

        self.assertEqual(result.status, "complete")
        production.assert_not_called()
        services.assert_not_called()
        expire.assert_not_called()
        occurrence_sla.assert_not_called()
        item_sla.assert_not_called()
        heartbeat.assert_not_called()

    def test_snapshot_cardinality_cannot_be_self_asserted(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
            )
            snapshot.items = tuple(
                SimpleNamespace(
                    item=summary.item,
                    target_count=1,
                    accepted_target_count=0,
                )
                for summary in snapshot.items
            )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "target cardinality",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )

    def test_same_total_with_wrong_item_target_count_is_rejected(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
            )
            multi = next(
                summary
                for summary in snapshot.items
                if summary.target_count > 1
            )
            single = next(
                summary
                for summary in snapshot.items
                if summary.target_count == 1
            )
            multi.target_count -= 1
            single.target_count += 1
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
                patch(
                    "scheduler.scheduled_executor."
                    "execute_scheduled_item",
                ) as canonical,
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "target identity",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )
        canonical.assert_not_called()

    def test_accepts_v2_on_time_but_rejects_native_on_time(
        self,
    ) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            complete = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            for summary in complete.items:
                if summary.item.runtime_type == "blackbox_v2":
                    summary.item.sla_status = "ON_TIME"
                    summary.item.late_reason = None
                    summary.item.sla_evaluated_at = (
                        observed_at.replace(tzinfo=None)
                    )
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(complete),
                patch(
                    "harness.daily_real_replay.OccurrenceFileLock",
                    return_value=_AcquiredOwnerLock(),
                ),
            ):
                result = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=41,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                ).run()
            self.assertEqual(result.status, "complete")

            invalid = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
            )
            native = next(
                summary.item
                for summary in invalid.items
                if summary.item.runtime_type == "native_adapter"
            )
            native.sla_status = "ON_TIME"
            native.sla_evaluated_at = observed_at.replace(tzinfo=None)
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(invalid),
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "item SLA state",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )

    def test_rejects_second_attempt_from_first_round_gate(self) -> None:
        from harness.daily_real_replay import (
            DailyRealReplayError,
            RealReplayRuntime,
        )

        policy, configs = _real_policy_and_configs()
        observed_at = datetime.now(timezone.utc)
        with _replay_inputs() as inputs:
            snapshot = _runtime_snapshot(
                policy=policy,
                configs=configs,
                inputs=inputs,
                observed_at=observed_at,
                states={
                    scheme_id: "SUCCESS"
                    for scheme_id in policy.schemes
                },
            )
            snapshot.items[0].item.attempt_no = 2
            engine = _isolated_engine()
            with (
                _runtime_isolation(engine) as (
                    isolation,
                    _listen,
                ),
                _runtime_repository(snapshot),
            ):
                with self.assertRaisesRegex(
                    DailyRealReplayError,
                    "attempt fence",
                ):
                    RealReplayRuntime(
                        engine,
                        isolation=isolation,
                        occurrence_id=41,
                        policy=policy,
                        configs=configs,
                        inputs=inputs,
                    )


@unittest.skipUnless(
    os.environ.get("BFL_DAILY_REAL_REPLAY_MYSQL") == "1",
    "set BFL_DAILY_REAL_REPLAY_MYSQL=1 for replay runtime MySQL guard",
)
class DailyRealReplayRuntimeMySQLTests(unittest.TestCase):
    def test_cutoff_reentry_keeps_real_ledger_unclaimed(self) -> None:
        from harness.daily_real_replay import (
            RealReplayRuntime,
            bind_real_replay_epoch,
            create_real_replay_occurrence,
            open_real_replay_generations,
            register_real_replay_generations,
            verify_real_replay_database,
        )

        policy, configs = _real_policy_and_configs()
        fixture = _generation_fixture()
        with (
            _temporary_mysql() as server,
            tempfile.TemporaryDirectory() as generation_root,
        ):
            suffix = uuid.uuid4().hex[:10]
            schema = f"bfl_real_replay_{suffix}"
            username = f"bfl_rr_{suffix}"
            password = uuid.uuid4().hex + uuid.uuid4().hex
            admin = server._socket_admin_engine()
            try:
                with admin.begin() as connection:
                    connection.exec_driver_sql(
                        f"CREATE DATABASE `{schema}` "
                        "CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_0900_ai_ci"
                    )
                    connection.exec_driver_sql(
                        f"CREATE USER '{username}'@'127.0.0.1' "
                        f"IDENTIFIED BY '{password}'"
                    )
                    connection.exec_driver_sql(
                        f"GRANT ALL PRIVILEGES ON `{schema}`.* "
                        f"TO '{username}'@'127.0.0.1'"
                    )
            finally:
                admin.dispose()
            server._schema_credentials[schema] = (username, password)
            engine = server.engine(schema)
            try:
                isolation = verify_real_replay_database(
                    engine,
                    expected_database_name=schema,
                    expected_server_uuid=(
                        server.expected_server_uuid or ""
                    ),
                    expected_port=server.port,
                    expected_private_root=server.root,
                )
                apply_migration_files(engine, MIGRATIONS)
                _seed_test_registry(engine, policy, configs)
                native, databridge = fixture._delivery_generation(
                    Path(generation_root)
                )
                inputs = open_real_replay_generations(
                    native_manifest=native.manifest_path,
                    databridge_manifest=databridge.manifest_path,
                )
                bind_real_replay_epoch(
                    engine,
                    isolation=isolation,
                    epoch_payload=TEST_EPOCH,
                )
                opened_at = datetime.now(timezone.utc)
                occurrence_id = create_real_replay_occurrence(
                    engine,
                    isolation=isolation,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                    schedule_key=(
                        f"isolated-real-replay-v1-{suffix}"
                    ),
                    opened_at=opened_at,
                    epoch_payload=TEST_EPOCH,
                )
                register_real_replay_generations(
                    engine,
                    occurrence_id=occurrence_id,
                    inputs=inputs,
                    isolation=isolation,
                )
                runtime = RealReplayRuntime(
                    engine,
                    isolation=isolation,
                    occurrence_id=occurrence_id,
                    policy=policy,
                    configs=configs,
                    inputs=inputs,
                )
                cutoff = _next_midnight_utc(opened_at)
                with (
                    patch(
                        "harness.daily_real_replay._now_utc",
                        return_value=cutoff,
                    ),
                    patch(
                        "harness.daily_real_replay."
                        "_real_replay_lock_root",
                        return_value=server.root,
                    ),
                    patch(
                        "scheduler.scheduled_executor."
                        "execute_scheduled_item",
                    ) as canonical,
                ):
                    result = runtime.run()
                with engine.connect() as connection:
                    run_count = int(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) FROM t_scheme_runs "
                                "WHERE schedule_item_id IS NOT NULL"
                            )
                        ).scalar_one()
                    )
                    pending_count = int(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) "
                                "FROM t_schedule_items "
                                "WHERE occurrence_id = :occurrence_id "
                                "AND state = 'PENDING' "
                                "AND attempt_no = 0 "
                                "AND current_run_id IS NULL"
                            ),
                            {"occurrence_id": occurrence_id},
                        ).scalar_one()
                    )
            finally:
                engine.dispose()

        self.assertEqual(result.status, "cutoff_incomplete")
        self.assertEqual(run_count, 0)
        self.assertEqual(pending_count, 21)
        canonical.assert_not_called()
