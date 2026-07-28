from __future__ import annotations

import os
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from scheduler import repository
from scheduler.daily_policy import POLICY_V2_PATH
from scheduler.daily_runtime import DailyRuntime
from scheduler.repository import (
    complete_scheduled_attempt,
    create_schedule_occurrence,
    read_schedule_occurrence_snapshot,
    register_schedule_attempt_process,
    start_schedule_attempt,
)
from scheduler.scheduled_executor import execute_scheduled_item
from tests.test_daily_coordinator_mvp_mysql import (
    BUSINESS_DATE,
    SHANGHAI,
    _MVPMySQLServices,
    _controlled_executor_patches,
    _create_mvp_occurrence,
    _database_counts,
    _mvp_occurrence_args,
    _registered_process_count,
    _valid_linkage_count,
)
from tests.test_daily_native_coordinator_mysql import (
    _ExpectationVerifier,
    _assert_test_epoch,
    _real_policy_and_configs,
    _records_for_item,
    _run_count,
    _seed_test_registry,
    _temporary_mysql,
)
from tests.test_daily_v2_coordinator_mysql import (
    _MutableClock,
    _availability,
)
from tests.test_daily_policy_v2 import (
    FORMAL_V2_OFFSETS,
    GRAY_V2_OFFSETS,
)


def _clock() -> _MutableClock:
    return _MutableClock(
        datetime(2026, 7, 23, 22, 40, tzinfo=timezone.utc)
    )


def _create_v2_occurrence(
    server,
    engine,
    policy,
    configs,
    *,
    schedule_key: str,
    clock: _MutableClock,
):
    _seed_test_registry(engine, policy, configs)
    return _create_mvp_occurrence(
        engine,
        policy,
        configs,
        schedule_key=schedule_key,
        manifest_root=server.root,
        clock=clock,
    )


def _scheduled_live_phase_counts(
    engine,
    occurrence_id: int,
) -> dict[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM t_scheme_runs AS r
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id)
                        AS run_count,
                    (SELECT COUNT(*)
                     FROM t_scheme_runs AS r
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id
                       AND r.prediction_phase = 'scheduled_live')
                        AS scheduled_live_runs,
                    (SELECT COUNT(*)
                     FROM t_scheme_predictions AS p
                     JOIN t_scheme_runs AS r ON r.run_id = p.run_id
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id)
                        AS prediction_count,
                    (SELECT COUNT(*)
                     FROM t_scheme_predictions AS p
                     JOIN t_scheme_runs AS r ON r.run_id = p.run_id
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id
                       AND p.prediction_phase = 'scheduled_live')
                        AS scheduled_live_predictions
                """
            ),
            {"occurrence_id": occurrence_id},
        ).mappings().one()
    return {key: int(value) for key, value in row.items()}


@unittest.skipUnless(
    os.environ.get("BFL_DAILY_POLICY_V2_MYSQL") == "1",
    "set BFL_DAILY_POLICY_V2_MYSQL=1 to run isolated 25/29 proof",
)
class DailyPolicyV2CoordinatorMySQLTests(unittest.TestCase):
    def test_concurrent_final_completions_keep_occurrence_aggregate_current(
        self,
    ) -> None:
        policy, configs = _real_policy_and_configs(POLICY_V2_PATH)
        clock = _clock()
        with (
            _temporary_mysql() as server,
            patch(
                "scheduler.repository."
                "assert_daily_coordinator_epoch_payload_matches_current",
                side_effect=_assert_test_epoch,
            ),
        ):
            schema, engine = server.create_schema("aggregate")
            worker_engines = []
            try:
                occurrence_id, _native, _databridge = (
                    _create_v2_occurrence(
                        server,
                        engine,
                        policy,
                        configs,
                        schedule_key=(
                            f"daily-v2-aggregate-{schema[-10:]}"
                        ),
                        clock=clock,
                    )
                )
                clock.set(
                    datetime(
                        2026,
                        7,
                        23,
                        23,
                        5,
                        tzinfo=timezone.utc,
                    )
                )
                frozen = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )
                attempts = []
                records_by_run = {}
                for summary in frozen.items:
                    attempt = start_schedule_attempt(
                        engine,
                        item_id=summary.item.item_id,
                        execution_token=(
                            f"aggregate-{summary.item.item_id}"
                        ),
                        _clock=clock,
                    )
                    register_schedule_attempt_process(
                        engine,
                        run_id=attempt.run_id,
                        execution_token=attempt.execution_token,
                        process_id=900_000 + attempt.run_id,
                        process_group_id=900_000 + attempt.run_id,
                        _clock=clock,
                    )
                    attempts.append(attempt)
                    records_by_run[attempt.run_id] = _records_for_item(
                        engine,
                        attempt.item_id,
                    )

                for attempt in attempts[:-2]:
                    complete_scheduled_attempt(
                        engine,
                        run_id=attempt.run_id,
                        records=records_by_run[attempt.run_id],
                        trusted_verifier=_ExpectationVerifier(),
                        _clock=clock,
                    )

                real_resolve = (
                    repository._resolve_schedule_item_id_for_run
                )
                snapshot_barrier = threading.Barrier(2)

                def resolve_after_snapshot(conn, *, run_id):
                    item_id = real_resolve(conn, run_id=run_id)
                    snapshot_barrier.wait(timeout=10)
                    return item_id

                worker_engines = [
                    server.engine(schema),
                    server.engine(schema),
                ]

                def complete_final(index: int) -> None:
                    attempt = attempts[-2 + index]
                    complete_scheduled_attempt(
                        worker_engines[index],
                        run_id=attempt.run_id,
                        records=records_by_run[attempt.run_id],
                        trusted_verifier=_ExpectationVerifier(),
                        _clock=clock,
                    )

                with (
                    patch.object(
                        repository,
                        "_resolve_schedule_item_id_for_run",
                        side_effect=resolve_after_snapshot,
                    ),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    list(pool.map(complete_final, (0, 1)))

                completed = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )
                self.assertEqual(
                    completed.actual_accepted_target_count,
                    29,
                )
                self.assertEqual(
                    completed.occurrence.accepted_target_count,
                    29,
                )
                self.assertEqual(
                    completed.occurrence.completion_state,
                    "SUCCESS",
                )
                self.assertEqual(
                    Counter(
                        summary.item.state
                        for summary in completed.items
                    ),
                    Counter({"SUCCESS": 25}),
                )
            finally:
                for worker_engine in worker_engines:
                    worker_engine.dispose()
                engine.dispose()

    def test_policy_v2_happy_path_and_late_fill_share_one_mysql(
        self,
    ) -> None:
        policy, configs = _real_policy_and_configs(POLICY_V2_PATH)
        self.assertEqual(
            (policy.expected_item_count, policy.expected_target_count),
            (25, 29),
        )
        with (
            _temporary_mysql() as server,
            patch(
                "scheduler.repository."
                "assert_daily_coordinator_epoch_payload_matches_current",
                side_effect=_assert_test_epoch,
            ),
        ):
            self._prove_complete_and_reentrant(server, policy, configs)
            self._prove_breach_is_write_once_after_late_fill(
                server,
                policy,
                configs,
            )

    def _prove_complete_and_reentrant(
        self,
        server,
        policy,
        configs,
    ) -> None:
        schema, engine = server.create_schema("policyvcomplete")
        clock = _clock()
        schedule_key = f"daily-v2-complete-{schema[-10:]}"
        try:
            (
                occurrence_id,
                native_generation,
                databridge_generation,
            ) = _create_v2_occurrence(
                server,
                engine,
                policy,
                configs,
                schedule_key=schedule_key,
                clock=clock,
            )
            frozen = read_schedule_occurrence_snapshot(
                engine,
                occurrence_id=occurrence_id,
            )
            self.assertEqual(
                (frozen.actual_item_count, frozen.actual_target_count),
                (25, 29),
            )
            self.assertEqual(
                Counter(
                    summary.item.runtime_type
                    for summary in frozen.items
                ),
                Counter({"native_adapter": 17, "blackbox_v2": 8}),
            )
            expected_offsets = {
                **FORMAL_V2_OFFSETS,
                **GRAY_V2_OFFSETS,
            }
            blackbox_items = {
                summary.item.base_scheme_id: summary.item
                for summary in frozen.items
                if summary.item.runtime_type == "blackbox_v2"
            }
            self.assertEqual(
                {
                    scheme_id: item.release_offset_minutes
                    for scheme_id, item in blackbox_items.items()
                },
                expected_offsets,
            )
            self.assertEqual(
                {
                    item.input_generation_id
                    for item in blackbox_items.values()
                },
                {databridge_generation.generation_id},
            )
            sealed_at = datetime(
                2026,
                7,
                23,
                22,
                50,
            )
            self.assertEqual(
                {
                    scheme_id: item.release_at
                    for scheme_id, item in blackbox_items.items()
                },
                {
                    scheme_id: sealed_at + timedelta(minutes=offset)
                    for scheme_id, offset in expected_offsets.items()
                },
            )

            # DataBridge 在 06:50 SEALED；07:05 已覆盖最后一个 +14
            # 分钟 release，确保八个 Blackbox 都可调度。
            clock.set(
                datetime(2026, 7, 23, 23, 5, tzinfo=timezone.utc)
            )
            services = _MVPMySQLServices(
                engine=engine,
                policy=policy,
                clock=clock,
                occurrence_id=occurrence_id,
            )
            services.current_time = clock.value.astimezone(SHANGHAI)
            runtime = DailyRuntime(services)
            availability = _availability(
                policy,
                native_generation,
                databridge_generation,
            )
            with _controlled_executor_patches(services, clock):
                first = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=availability,
                    trigger_origin="apscheduler",
                )
                second = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=availability,
                    trigger_origin="startup_catchup",
                )

            counts = _database_counts(engine, occurrence_id)
            self.assertEqual(first.status, "finished")
            self.assertEqual(len(first.dispatched_scheme_ids), 25)
            self.assertEqual(second.dispatched_scheme_ids, ())
            self.assertEqual(
                services.max_active_by_runtime,
                Counter({"native_adapter": 2, "blackbox_v2": 2}),
            )
            self.assertEqual(
                services.attempts,
                Counter({scheme_id: 1 for scheme_id in policy.schemes}),
            )
            self.assertEqual(
                counts,
                {
                    "successful_items": 25,
                    "accepted_targets": 29,
                    "successful_runs": 25,
                    "predictions": 29,
                },
            )
            self.assertEqual(
                _scheduled_live_phase_counts(engine, occurrence_id),
                {
                    "run_count": 25,
                    "scheduled_live_runs": 25,
                    "prediction_count": 29,
                    "scheduled_live_predictions": 29,
                },
            )
            self.assertEqual(_run_count(engine, occurrence_id), 25)
            self.assertEqual(
                _valid_linkage_count(engine, occurrence_id),
                29,
            )
            self.assertEqual(
                _registered_process_count(engine, occurrence_id),
                25,
            )
            replay_id = create_schedule_occurrence(
                engine,
                **_mvp_occurrence_args(
                    policy,
                    configs,
                    schedule_key=schedule_key,
                ),
            )
            self.assertEqual(replay_id, occurrence_id)
            self.assertEqual(_database_counts(engine, occurrence_id), counts)
        finally:
            engine.dispose()

    def _prove_breach_is_write_once_after_late_fill(
        self,
        server,
        policy,
        configs,
    ) -> None:
        schema, engine = server.create_schema("policyvbreach")
        clock = _clock()
        try:
            (
                occurrence_id,
                _native_generation,
                _databridge_generation,
            ) = _create_v2_occurrence(
                server,
                engine,
                policy,
                configs,
                schedule_key=f"daily-v2-breach-{schema[-10:]}",
                clock=clock,
            )
            snapshot = read_schedule_occurrence_snapshot(
                engine,
                occurrence_id=occurrence_id,
            )
            retry_summary = next(
                summary
                for summary in snapshot.items
                if summary.item.runtime_type == "blackbox_v2"
                and summary.target_count == 1
            )
            retry_scheme_id = retry_summary.item.base_scheme_id
            clock.set(
                datetime(2026, 7, 23, 23, 5, tzinfo=timezone.utc)
            )
            services = _MVPMySQLServices(
                engine=engine,
                policy=policy,
                clock=clock,
                occurrence_id=occurrence_id,
                transient_once_scheme_id=retry_scheme_id,
            )
            services.current_time = clock.value.astimezone(SHANGHAI)
            ordered = sorted(
                snapshot.items,
                key=lambda summary: (
                    summary.item.base_scheme_id == retry_scheme_id,
                    summary.item.base_scheme_id,
                ),
            )
            with _controlled_executor_patches(services, clock):
                first_results = [
                    execute_scheduled_item(
                        engine,
                        item_id=summary.item.item_id,
                        trigger_origin="apscheduler",
                        trusted_verifier=_ExpectationVerifier(),
                    )
                    for summary in ordered
                ]
                after_first = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )
                self.assertEqual(
                    Counter(result.status for result in first_results),
                    Counter({"success": 24, "retry_wait": 1}),
                )
                self.assertEqual(
                    after_first.actual_accepted_target_count,
                    28,
                )

                deadline = datetime(
                    2026, 7, 24, 0, 0, tzinfo=timezone.utc
                )
                clock.set(deadline)
                services.current_time = deadline.astimezone(SHANGHAI)
                watchdog = DailyRuntime(services).run_watchdog(
                    stage="target_sla",
                    run_date=BUSINESS_DATE,
                )
                breach_alert = next(
                    alert
                    for alert in services.alerts
                    if alert["code"] == "DAILY_TARGET_SLA_BREACHED"
                )
                self.assertEqual(watchdog.status, "breached")
                self.assertEqual(
                    (
                        watchdog.details["accepted_target_count"],
                        watchdog.details["expected_target_count"],
                    ),
                    (28, 29),
                )
                self.assertEqual(
                    breach_alert["message"],
                    "Daily occurrence accepted 28/29 targets at 08:00",
                )

                clock.set(
                    datetime(2026, 7, 24, 0, 1, tzinfo=timezone.utc)
                )
                retry = execute_scheduled_item(
                    engine,
                    item_id=retry_summary.item.item_id,
                    trigger_origin="auto_retry",
                    trusted_verifier=_ExpectationVerifier(),
                )

            completed = read_schedule_occurrence_snapshot(
                engine,
                occurrence_id=occurrence_id,
            )
            services.current_time = clock.value.astimezone(SHANGHAI)
            replayed = DailyRuntime(services).run_watchdog(
                stage="target_sla",
                run_date=BUSINESS_DATE,
            )
            self.assertEqual(retry.status, "success")
            self.assertEqual(retry.attempt_no, 2)
            self.assertEqual(
                completed.actual_accepted_target_count,
                29,
            )
            self.assertEqual(
                completed.occurrence.completion_state,
                "SUCCESS",
            )
            self.assertEqual(
                completed.occurrence.sla_outcome,
                "BREACHED",
            )
            self.assertEqual(
                completed.occurrence.sla_accepted_target_count,
                28,
            )
            self.assertEqual(replayed.status, "breached")
            self.assertEqual(
                sum(
                    alert["code"] == "DAILY_TARGET_SLA_BREACHED"
                    for alert in services.alerts
                ),
                1,
            )
            self.assertEqual(_run_count(engine, occurrence_id), 26)
            self.assertEqual(
                _database_counts(engine, occurrence_id),
                {
                    "successful_items": 25,
                    "accepted_targets": 29,
                    "successful_runs": 25,
                    "predictions": 29,
                },
            )
            self.assertEqual(
                _valid_linkage_count(engine, occurrence_id),
                29,
            )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
