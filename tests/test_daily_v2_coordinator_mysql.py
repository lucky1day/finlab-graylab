from __future__ import annotations

import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scheduler.daily_runtime import (
    DailyRuntime,
    GenerationBuildOutcome,
    _GenerationAvailability,
)
from scheduler.repository import (
    create_schedule_occurrence,
    evaluate_schedule_item_start_sla,
    fail_schedule_item_without_attempt,
    read_schedule_occurrence_snapshot,
    register_seal_and_bind_schedule_occurrence_generation,
    start_schedule_attempt,
)
from tests.test_daily_native_coordinator_mysql import (
    FEATURE_DATE,
    _FixedClock,
    _MySQLCoordinatorServices,
    _assert_test_epoch,
    _occurrence_args,
    _real_policy_and_configs,
    _register_and_complete,
    _run_count,
    _seed_test_registry,
    _temporary_mysql,
)


BUSINESS_DATE = date(2026, 7, 24)
SHANGHAI = ZoneInfo("Asia/Shanghai")
V2_IDS = (
    "one_y_t5_liq_excess_a_v1",
    "one_y_t5_liq_excess_a_w252_l7_v1",
    "one_y_t5_liq_excess_a_w350_l7_v1",
    "one_y_t5_liq_excess_b_w252_l7_v1",
)
NATIVE_MANIFEST_SHA256 = "a" * 64
DATABRIDGE_MANIFEST_SHA256 = "b" * 64


class _MutableClock(_FixedClock):
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now_utc(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class _V2MySQLServices(_MySQLCoordinatorServices):
    def __init__(
        self,
        *,
        engine,
        policy,
        clock: _MutableClock,
        current_time: datetime,
        failure_scheme_id: str | None = None,
    ) -> None:
        super().__init__(
            engine=engine,
            policy=policy,
            clock=clock,
            failure_scheme_id=failure_scheme_id,
        )
        self.current_time = current_time

    def now(self) -> datetime:
        return self.current_time


def _create_v2_occurrence(
    engine,
    policy,
    configs,
    *,
    schedule_key: str,
    manifest_root: Path,
    clock: _MutableClock,
):
    occurrence_id = create_schedule_occurrence(
        engine,
        **_occurrence_args(
            policy,
            configs,
            schedule_key=schedule_key,
        ),
    )
    snapshot = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=occurrence_id,
    )
    if (
        snapshot.actual_item_count != 21
        or snapshot.actual_target_count != 25
    ):
        raise AssertionError("Step 7 occurrence is not the real 21/25 matrix")

    suffix = schedule_key.rsplit("-", 1)[-1]
    native_generation_id = f"native-step7-{suffix}"
    databridge_generation_id = f"databridge-step7-{suffix}"
    clock.set(
        datetime(
            2026,
            7,
            23,
            22,
            40,
            tzinfo=timezone.utc,
        )
    )
    registered_native = (
        register_seal_and_bind_schedule_occurrence_generation(
            engine,
            occurrence_id=occurrence_id,
            generation_id=native_generation_id,
            generation_type="native_source",
            business_date=BUSINESS_DATE.isoformat(),
            feature_date=FEATURE_DATE,
            readiness_basis="CLOCK_CONTRACT",
            source_commit_token="c" * 64,
            dataset_content_id="d" * 64,
            schema_version="native-generation-v1",
            exporter_version="step7-native-fixture-v1",
            manifest_uri=os.fspath(
                manifest_root
                / native_generation_id
                / "manifest.json"
            ),
            manifest_sha256=NATIVE_MANIFEST_SHA256,
            expected_feature_date=FEATURE_DATE,
            _clock=clock,
        )
    )
    if registered_native != (native_generation_id, 17):
        raise AssertionError(
            f"unexpected Native generation binding: {registered_native}"
        )

    clock.set(
        datetime(
            2026,
            7,
            23,
            22,
            50,
            tzinfo=timezone.utc,
        )
    )
    registered_databridge = (
        register_seal_and_bind_schedule_occurrence_generation(
            engine,
            occurrence_id=occurrence_id,
            generation_id=databridge_generation_id,
            generation_type="databridge_v1",
            business_date=BUSINESS_DATE.isoformat(),
            feature_date=FEATURE_DATE,
            readiness_basis="UPSTREAM_SEAL",
            source_commit_token="e" * 64,
            dataset_content_id="f" * 64,
            schema_version="databridge-generation-v1",
            exporter_version="step7-databridge-fixture-v1",
            manifest_uri=os.fspath(
                manifest_root
                / databridge_generation_id
                / "manifest.json"
            ),
            manifest_sha256=DATABRIDGE_MANIFEST_SHA256,
            native_generation_id=native_generation_id,
            native_manifest_sha256=NATIVE_MANIFEST_SHA256,
            expected_feature_date=FEATURE_DATE,
            _clock=clock,
        )
    )
    if registered_databridge != (databridge_generation_id, 4):
        raise AssertionError(
            "unexpected DataBridge generation binding: "
            f"{registered_databridge}"
        )

    snapshot = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=occurrence_id,
    )
    for summary in snapshot.items:
        if summary.item.runtime_type == "native_adapter":
            fail_schedule_item_without_attempt(
                engine,
                item_id=summary.item.item_id,
                failure_code="GENERATION_BUILD_FAILED",
                failure_message="Step 7 isolates V2 execution",
                _clock=clock,
            )
    isolated = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=occurrence_id,
    )
    native_items = [
        summary.item
        for summary in isolated.items
        if summary.item.runtime_type == "native_adapter"
    ]
    if len(native_items) != 17 or any(
        item.state != "FAILED_TERMINAL" for item in native_items
    ):
        raise AssertionError(
            "Step 7 did not isolate exactly 17 terminal Native items"
        )
    return (
        occurrence_id,
        SimpleNamespace(
            generation_id=native_generation_id,
            business_date=BUSINESS_DATE.isoformat(),
        ),
        SimpleNamespace(
            generation_id=databridge_generation_id,
            business_date=BUSINESS_DATE.isoformat(),
        ),
    )


def _availability(policy, native_generation, databridge_generation):
    availability = _GenerationAvailability(
        policy.allowed_resource_combinations
    )
    availability.finish(
        GenerationBuildOutcome(
            native_generation=native_generation,
            databridge_generation=databridge_generation,
        )
    )
    return availability


def _v2_items(snapshot):
    return {
        summary.item.base_scheme_id: summary.item
        for summary in snapshot.items
        if summary.item.runtime_type == "blackbox_v2"
    }


def _assert_generation_rows(
    testcase: unittest.TestCase,
    engine,
    *,
    occurrence_id: int,
    native_generation_id: str,
    databridge_generation_id: str,
) -> None:
    with engine.connect() as connection:
        generations = {
            row["generation_id"]: row
            for row in connection.execute(
                text(
                    """
                    SELECT generation_id, generation_type, state,
                           native_generation_id,
                           native_manifest_sha256, sealed_at
                    FROM t_input_generations
                    WHERE generation_id IN (
                        :native_generation_id,
                        :databridge_generation_id
                    )
                    """
                ),
                {
                    "native_generation_id": native_generation_id,
                    "databridge_generation_id":
                        databridge_generation_id,
                },
            ).mappings()
        }
        v2_rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT base_scheme_id, input_generation_id,
                           release_offset_minutes, release_at
                    FROM t_schedule_items
                    WHERE occurrence_id = :occurrence_id
                      AND runtime_type = 'blackbox_v2'
                    ORDER BY release_offset_minutes, base_scheme_id
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).mappings()
        )
    testcase.assertEqual(set(generations), {
        native_generation_id,
        databridge_generation_id,
    })
    testcase.assertTrue(
        all(row["state"] == "SEALED" for row in generations.values())
    )
    databridge = generations[databridge_generation_id]
    testcase.assertEqual(
        databridge["native_generation_id"],
        native_generation_id,
    )
    testcase.assertEqual(
        databridge["native_manifest_sha256"],
        NATIVE_MANIFEST_SHA256,
    )
    testcase.assertEqual(
        tuple(row["base_scheme_id"] for row in v2_rows),
        V2_IDS,
    )
    testcase.assertTrue(
        all(
            row["input_generation_id"] == databridge_generation_id
            for row in v2_rows
        )
    )
    sealed_at = databridge["sealed_at"]
    testcase.assertEqual(
        tuple(row["release_offset_minutes"] for row in v2_rows),
        (0, 2, 4, 6),
    )
    testcase.assertEqual(
        tuple(row["release_at"] for row in v2_rows),
        tuple(
            sealed_at + timedelta(minutes=offset)
            for offset in (0, 2, 4, 6)
        ),
    )


def _v2_run_counts(engine, occurrence_id: int) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            row["base_scheme_id"]: int(row["run_count"])
            for row in connection.execute(
                text(
                    """
                    SELECT i.base_scheme_id, COUNT(*) AS run_count
                    FROM t_schedule_items AS i
                    LEFT JOIN t_scheme_runs AS r
                      ON r.schedule_item_id = i.item_id
                    WHERE i.occurrence_id = :occurrence_id
                      AND i.runtime_type = 'blackbox_v2'
                    GROUP BY i.item_id, i.base_scheme_id
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).mappings()
        }


@unittest.skipUnless(
    os.environ.get("BFL_STEP7_MYSQL") == "1",
    "set BFL_STEP7_MYSQL=1 to run isolated MySQL Step 7 verification",
)
class DailyV2CoordinatorMySQLTests(unittest.TestCase):
    def test_v2_claim_reentry_late_and_failure_isolation(self) -> None:
        policy, configs = _real_policy_and_configs()
        clock = _MutableClock(
            datetime(
                2026,
                7,
                23,
                22,
                40,
                tzinfo=timezone.utc,
            )
        )
        with (
            _temporary_mysql() as server,
            patch(
                "scheduler.repository."
                "assert_daily_coordinator_epoch_payload_matches_current",
                side_effect=_assert_test_epoch,
            ),
        ):
            success_schema, success_engine = server.create_schema(
                "success"
            )
            try:
                _seed_test_registry(success_engine, policy, configs)
                (
                    occurrence_id,
                    native_generation,
                    databridge_generation,
                ) = _create_v2_occurrence(
                    success_engine,
                    policy,
                    configs,
                    schedule_key=(
                        f"step7-v2success-{success_schema[-10:]}"
                    ),
                    manifest_root=server.root,
                    clock=clock,
                )
                _assert_generation_rows(
                    self,
                    success_engine,
                    occurrence_id=occurrence_id,
                    native_generation_id=(
                        native_generation.generation_id
                    ),
                    databridge_generation_id=(
                        databridge_generation.generation_id
                    ),
                )
                snapshot = read_schedule_occurrence_snapshot(
                    success_engine,
                    occurrence_id=occurrence_id,
                )
                v2_items = _v2_items(snapshot)
                race_item_id = v2_items[V2_IDS[0]].item_id
                clock.set(
                    datetime(
                        2026,
                        7,
                        23,
                        22,
                        56,
                        tzinfo=timezone.utc,
                    )
                )
                claim_barrier = threading.Barrier(2)
                claim_engines = [
                    server.engine(success_schema),
                    server.engine(success_schema),
                ]

                def claim(index: int):
                    claim_barrier.wait(timeout=5)
                    try:
                        return start_schedule_attempt(
                            claim_engines[index],
                            item_id=race_item_id,
                            execution_token=(
                                f"step7-v2-race-{index}-"
                                f"{uuid.uuid4().hex}"
                            ),
                            _clock=clock,
                        )
                    except RuntimeError as exc:
                        return exc

                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        outcomes = list(pool.map(claim, (0, 1)))
                finally:
                    for claim_engine in claim_engines:
                        claim_engine.dispose()
                winners = [
                    outcome
                    for outcome in outcomes
                    if not isinstance(outcome, Exception)
                ]
                rejected = [
                    outcome
                    for outcome in outcomes
                    if isinstance(outcome, Exception)
                ]
                self.assertEqual(len(winners), 1)
                self.assertEqual(len(rejected), 1)
                self.assertEqual(winners[0].attempt_no, 1)
                _register_and_complete(
                    success_engine,
                    winners[0],
                    clock,
                )

                services = _V2MySQLServices(
                    engine=success_engine,
                    policy=policy,
                    clock=clock,
                    current_time=datetime(
                        2026,
                        7,
                        24,
                        6,
                        56,
                        tzinfo=SHANGHAI,
                    ),
                )
                runtime = DailyRuntime(services)
                first = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        native_generation,
                        databridge_generation,
                    ),
                    trigger_origin="apscheduler",
                )
                runs_after_first = _run_count(
                    success_engine,
                    occurrence_id,
                )
                second = runtime._drive_items(
                    occurrence_id=occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        native_generation,
                        databridge_generation,
                    ),
                    trigger_origin="startup_catchup",
                )
                replay_id = create_schedule_occurrence(
                    success_engine,
                    **_occurrence_args(
                        policy,
                        configs,
                        schedule_key=(
                            f"step7-v2success-"
                            f"{success_schema[-10:]}"
                        ),
                    ),
                )
                final_snapshot = read_schedule_occurrence_snapshot(
                    success_engine,
                    occurrence_id=occurrence_id,
                )
                final_v2 = _v2_items(final_snapshot)

                self.assertEqual(replay_id, occurrence_id)
                self.assertEqual(
                    set(first.dispatched_scheme_ids),
                    set(V2_IDS[1:]),
                )
                self.assertEqual(second.dispatched_scheme_ids, ())
                self.assertEqual(runs_after_first, 4)
                self.assertEqual(
                    _run_count(success_engine, occurrence_id),
                    4,
                )
                self.assertEqual(
                    _v2_run_counts(success_engine, occurrence_id),
                    {scheme_id: 1 for scheme_id in V2_IDS},
                )
                self.assertTrue(
                    all(
                        item.state == "SUCCESS"
                        and item.attempt_no == 1
                        and item.input_generation_id
                        == databridge_generation.generation_id
                        for item in final_v2.values()
                    )
                )
            finally:
                success_engine.dispose()

            failure_schema, failure_engine = server.create_schema(
                "failure"
            )
            try:
                _seed_test_registry(failure_engine, policy, configs)
                (
                    failure_occurrence_id,
                    failure_native,
                    failure_databridge,
                ) = _create_v2_occurrence(
                    failure_engine,
                    policy,
                    configs,
                    schedule_key=(
                        f"step7-v2failure-{failure_schema[-10:]}"
                    ),
                    manifest_root=server.root,
                    clock=clock,
                )
                clock.set(
                    datetime(
                        2026,
                        7,
                        23,
                        23,
                        45,
                        tzinfo=timezone.utc,
                    )
                )
                before_late = read_schedule_occurrence_snapshot(
                    failure_engine,
                    occurrence_id=failure_occurrence_id,
                )
                for item in _v2_items(before_late).values():
                    projection = evaluate_schedule_item_start_sla(
                        failure_engine,
                        item_id=item.item_id,
                        _clock=clock,
                    )
                    self.assertEqual(projection.status, "LATE")
                    self.assertEqual(
                        projection.reason,
                        "V2_NOT_STARTED_BY_0745",
                    )
                    self.assertEqual(
                        projection.deadline_at,
                        datetime(2026, 7, 23, 23, 45),
                    )
                    self.assertEqual(
                        projection.evaluated_at,
                        projection.deadline_at,
                    )
                    self.assertTrue(projection.newly_persisted)

                failure_services = _V2MySQLServices(
                    engine=failure_engine,
                    policy=policy,
                    clock=clock,
                    current_time=datetime(
                        2026,
                        7,
                        24,
                        7,
                        45,
                        tzinfo=SHANGHAI,
                    ),
                    failure_scheme_id=V2_IDS[0],
                )
                failure_result = DailyRuntime(
                    failure_services
                )._drive_items(
                    occurrence_id=failure_occurrence_id,
                    business_date=BUSINESS_DATE,
                    policy=policy,
                    generation_availability=_availability(
                        policy,
                        failure_native,
                        failure_databridge,
                    ),
                    trigger_origin="apscheduler",
                )
                failure_snapshot = read_schedule_occurrence_snapshot(
                    failure_engine,
                    occurrence_id=failure_occurrence_id,
                )
                failure_v2 = _v2_items(failure_snapshot)

                self.assertEqual(failure_result.status, "finished")
                self.assertEqual(
                    set(failure_result.dispatched_scheme_ids),
                    set(V2_IDS),
                )
                self.assertEqual(
                    failure_v2[V2_IDS[0]].state,
                    "FAILED_TERMINAL",
                )
                self.assertEqual(
                    failure_v2[V2_IDS[0]].failure_code,
                    "ALGORITHM",
                )
                self.assertTrue(
                    all(
                        failure_v2[scheme_id].state == "SUCCESS"
                        for scheme_id in V2_IDS[1:]
                    )
                )
                self.assertTrue(
                    all(
                        item.sla_status == "LATE"
                        and item.attempt_no == 1
                        for item in failure_v2.values()
                    )
                )
                self.assertEqual(
                    _v2_run_counts(
                        failure_engine,
                        failure_occurrence_id,
                    ),
                    {scheme_id: 1 for scheme_id in V2_IDS},
                )
                terminal_alerts = [
                    alert
                    for alert in failure_services.alerts
                    if alert["code"] == "ITEM_TERMINAL_FAILURE"
                ]
                self.assertEqual(len(terminal_alerts), 1)
                self.assertEqual(
                    terminal_alerts[0]["scheme_id"],
                    V2_IDS[0],
                )
            finally:
                failure_engine.dispose()


if __name__ == "__main__":
    unittest.main()
