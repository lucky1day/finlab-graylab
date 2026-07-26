from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import unittest
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scheduler.daily_runtime import DailyRuntime
from scheduler.daily_health import project_daily_health
from scheduler import repository as schedule_repository
from scheduler.repository import (
    SchedulerHeartbeat,
    create_schedule_occurrence,
    evaluate_schedule_occurrence_target_sla,
    read_schedule_health_envelope,
    read_schedule_execution_envelope,
    read_schedule_occurrence_snapshot,
    register_seal_and_bind_schedule_occurrence_generation,
)
from scheduler.scheduled_executor import execute_scheduled_item
from tests.test_daily_native_coordinator_mysql import (
    FEATURE_DATE,
    _ExpectationVerifier,
    _assert_test_epoch,
    _occurrence_args,
    _real_policy_and_configs,
    _records_for_item,
    _run_count,
    _seed_test_registry,
    _temporary_mysql,
)
from tests.test_daily_v2_coordinator_mysql import (
    DATABRIDGE_MANIFEST_SHA256,
    NATIVE_MANIFEST_SHA256,
    _MutableClock,
    _V2MySQLServices,
    _availability,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMISSION_PATH = (
    PROJECT_ROOT / "deploy" / "daily_capacity_admission_v2.json"
)
BUSINESS_DATE = date(2026, 7, 24)
SHANGHAI = ZoneInfo("Asia/Shanghai")
LIVE_SOURCE_0629 = {
    "daily_10y_lgbm_10y04_0629",
    "daily_1y_xgb_1y13_0629",
    "daily_5y_lgbm_5y10_0629",
}


def _mvp_occurrence_args(policy, configs, *, schedule_key: str):
    args = _occurrence_args(
        policy,
        configs,
        schedule_key=schedule_key,
    )
    policy_json = dict(args["policy_json"])
    step6_projection = policy_json.pop("step6_test_projection")
    if (
        step6_projection["cache_completion_gate"]
        != "excluded_admission_blocked"
    ):
        raise AssertionError("unexpected inherited cache gate projection")
    admission = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    if admission.get("status") != "BLOCKED":
        raise AssertionError(
            "MVP fixture must not bypass capacity admission"
        )
    policy_json["daily_mvp_test_projection"] = {
        "purpose": "complete_21_25_ledger_function_verification",
        "algorithm_execution": "controlled_canonical_recorder",
        "cache_completion_qualification": "EXCLUDED",
        "exclusion_reason": "CAPACITY_ADMISSION_BLOCKED",
        "capacity_admission_status": "BLOCKED",
        "production_cache_gate_passed": False,
        "excluded_scheme_ids":
            step6_projection["excluded_scheme_ids"],
    }
    return {
        **args,
        "policy_json": policy_json,
    }


def _create_mvp_occurrence(
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
        **_mvp_occurrence_args(
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
        raise AssertionError("MVP occurrence is not the real 21/25 matrix")

    suffix = schedule_key.rsplit("-", 1)[-1]
    native_generation_id = f"native-mvp-{suffix}"
    databridge_generation_id = f"databridge-mvp-{suffix}"
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
    native_binding = (
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
            exporter_version="daily-mvp-native-fixture-v1",
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
    databridge_binding = (
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
            exporter_version="daily-mvp-databridge-fixture-v1",
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
    if (
        native_binding != (native_generation_id, 17)
        or databridge_binding != (databridge_generation_id, 4)
    ):
        raise AssertionError(
            "MVP generation binding cardinality drifted: "
            f"{native_binding}/{databridge_binding}"
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


class _MVPMySQLServices(_V2MySQLServices):
    """记录真实双池驻留，同时复用真实 claim/commit repository 路径。"""

    def __init__(
        self,
        *,
        engine,
        policy,
        clock: _MutableClock,
        occurrence_id: int,
        transient_once_scheme_id: str | None = None,
    ) -> None:
        super().__init__(
            engine=engine,
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
        self._active_lock = threading.Lock()
        self._barriers = {
            "native_adapter": threading.Barrier(2),
            "blackbox_v2": threading.Barrier(2),
        }
        self._started_by_runtime: Counter[str] = Counter()
        self._active_by_runtime: Counter[str] = Counter()
        self.max_active_by_runtime: Counter[str] = Counter()
        self.observed_resource_signatures: set[
            tuple[str, ...]
        ] = set()
        snapshot = read_schedule_occurrence_snapshot(
            engine,
            occurrence_id=occurrence_id,
        )
        self._item_id_by_scheme = {
            summary.item.base_scheme_id: summary.item.item_id
            for summary in snapshot.items
        }
        self.occurrence_id = occurrence_id
        self.process_started_callbacks = 0
        self.transient_once_scheme_id = transient_once_scheme_id

    def find_occurrence_id(self, business_date: date) -> int | None:
        if business_date != BUSINESS_DATE:
            return None
        return self.occurrence_id

    def validate_occurrence_epoch(self, *, snapshot) -> None:
        _assert_test_epoch(
            snapshot.occurrence.policy_json.get(
                "daily_coordinator_epoch"
            ),
            label="daily occurrence coordinator epoch",
            engine=self.engine,
        )

    def evaluate_target_sla(
        self,
        *,
        occurrence_id: int,
        evaluated_at: datetime,
    ):
        return evaluate_schedule_occurrence_target_sla(
            self.engine,
            occurrence_id=occurrence_id,
            evaluated_at=evaluated_at,
            _clock=self._clock,
        )

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
        decisions = super().dispatch_decisions(
            business_date=business_date,
            snapshot=snapshot,
            native_generation=native_generation,
            databridge_generation=databridge_generation,
            running_scheme_ids=running_scheme_ids,
            running_resource_classes=running_resource_classes,
            now=now,
        )
        selected = list(running_scheme_ids)
        for decision in decisions:
            if decision.action != "DISPATCH":
                continue
            selected.append(decision.scheme_id)
            signature = tuple(
                sorted(
                    (
                        *(
                            self._policy.schemes[
                                scheme_id
                            ].resource_class
                            for scheme_id in selected
                        ),
                        *running_resource_classes,
                    )
                )
            )
            self.observed_resource_signatures.add(signature)
        return decisions

    def execute_item(self, *, item_id: int, trigger_origin: str):
        envelope = read_schedule_execution_envelope(
            self.engine,
            item_id=item_id,
        )
        runtime_type = envelope.item.runtime_type
        with self._active_lock:
            self._started_by_runtime[runtime_type] += 1
            first_wave = (
                self._started_by_runtime[runtime_type] <= 2
            )
            self._active_by_runtime[runtime_type] += 1
            self.max_active_by_runtime[runtime_type] = max(
                self.max_active_by_runtime[runtime_type],
                self._active_by_runtime[runtime_type],
            )
        try:
            if first_wave:
                self._barriers[runtime_type].wait(timeout=5)
            return execute_scheduled_item(
                self.engine,
                item_id=item_id,
                trigger_origin=trigger_origin,
                trusted_verifier=_ExpectationVerifier(),
            )
        finally:
            with self._active_lock:
                self._active_by_runtime[runtime_type] -= 1

    def controlled_canonical_recorder(
        self,
        config,
        predict_date: str,
        *,
        process_started,
        process_fence,
        **_kwargs,
    ):
        """以真实子进程回调替代算法计算，保留生产执行器其余路径。"""
        item_id = self._item_id_by_scheme[config.scheme_id]
        process_fence()
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.02)",
            ],
            start_new_session=True,
        )
        try:
            process_started(process.pid, process.pid)
            with self._counter_lock:
                self.process_started_callbacks += 1
                self.attempts[config.scheme_id] += 1
            return_code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if return_code != 0:
            raise RuntimeError(
                f"controlled recorder exited with {return_code}"
            )
        process_fence()
        if (
            config.scheme_id == self.transient_once_scheme_id
            and self.attempts[config.scheme_id] == 1
        ):
            raise TimeoutError(
                errno.ETIMEDOUT,
                "injected transient recorder timeout",
            )
        records = _records_for_item(self.engine, item_id)
        envelope = read_schedule_execution_envelope(
            self.engine,
            item_id=item_id,
        )
        policy_row = next(
            row
            for row in envelope.occurrence.policy_json["schemes"]
            if row["scheme_id"] == config.scheme_id
        )
        if policy_row["input_compatibility"] == "live_source_0629":
            for record in records:
                record.extra.update(
                    {
                        "input_mode": "live_source_0629",
                        "data_watermark":
                            envelope.occurrence.feature_date,
                        "data_watermark_basis":
                            "source_output_prediction_date",
                        "live_source_fence_generation_id":
                            envelope.generation.generation_id,
                        "source_package_hash":
                            policy_row["source_package_sha256"],
                    }
                )
        return records


def _controlled_open_frozen_generations(
    envelope,
    *,
    databridge_schema_path,
):
    """MVP 不重跑算法文件验证，仅保留冻结 generation 绑定语义。"""
    del databridge_schema_path
    generation = SimpleNamespace(
        generation_id=envelope.generation.generation_id
    )
    if envelope.item.runtime_type == "native_adapter":
        return generation, None, None
    return (
        None,
        generation,
        SimpleNamespace(
            generation_id=envelope.calendar_generation.generation_id
        ),
    )


def _repository_call_with_clock(function, clock):
    def call(*args, **kwargs):
        kwargs["_clock"] = clock
        return function(*args, **kwargs)

    return call


@contextmanager
def _controlled_executor_patches(
    services: _MVPMySQLServices,
    clock: _MutableClock,
):
    """返回保持生产执行控制流、仅替换算法与测试时钟的 patch 集。"""
    patches = (
        patch(
            "scheduler.scheduled_executor."
            "assert_daily_coordinator_epoch_matches_policy",
            side_effect=lambda policy_json: _assert_test_epoch(
                policy_json.get("daily_coordinator_epoch"),
                label="daily occurrence coordinator epoch",
                engine=services.engine,
            ),
        ),
        patch(
            "scheduler.scheduled_executor._open_frozen_generations",
            side_effect=_controlled_open_frozen_generations,
        ),
        patch(
            "scheduler.scheduled_executor.run_configured_scheme",
            side_effect=services.controlled_canonical_recorder,
        ),
        patch(
            "scheduler.scheduled_executor.start_schedule_attempt",
            side_effect=_repository_call_with_clock(
                schedule_repository.start_schedule_attempt,
                clock,
            ),
        ),
        patch(
            "scheduler.scheduled_executor."
            "register_schedule_attempt_process",
            side_effect=_repository_call_with_clock(
                schedule_repository.register_schedule_attempt_process,
                clock,
            ),
        ),
        patch(
            "scheduler.scheduled_executor.complete_scheduled_attempt",
            side_effect=_repository_call_with_clock(
                schedule_repository.complete_scheduled_attempt,
                clock,
            ),
        ),
        patch(
            "scheduler.scheduled_executor."
            "mark_schedule_attempt_retry_wait",
            side_effect=_repository_call_with_clock(
                schedule_repository.mark_schedule_attempt_retry_wait,
                clock,
            ),
        ),
        patch(
            "scheduler.scheduled_executor."
            "mark_schedule_attempt_terminal_failure",
            side_effect=_repository_call_with_clock(
                schedule_repository.mark_schedule_attempt_terminal_failure,
                clock,
            ),
        ),
    )
    with ExitStack() as stack:
        for controlled_patch in patches:
            stack.enter_context(controlled_patch)
        yield


def _health_projection(
    engine,
    *,
    occurrence_id: int,
    now: datetime,
) -> dict[str, object]:
    snapshot = read_schedule_occurrence_snapshot(
        engine,
        occurrence_id=occurrence_id,
    )
    envelopes = tuple(
        read_schedule_health_envelope(
            engine,
            item_id=summary.item.item_id,
        )
        for summary in snapshot.items
    )
    heartbeat = SchedulerHeartbeat(
        service_name="daily-coordinator",
        process_id=os.getpid(),
        host_name="isolated-mvp-mysql",
        state="RUNNING",
        occurrence_id=occurrence_id,
        heartbeat_at=now,
        details={
            "business_date": BUSINESS_DATE.isoformat(),
            "coordinator_mode": "ledger",
        },
    )
    return project_daily_health(
        snapshot,
        heartbeat,
        execution_envelopes=envelopes,
        now=now,
    )


def _database_counts(engine, occurrence_id: int) -> dict[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM t_schedule_items
                     WHERE occurrence_id = :occurrence_id
                       AND state = 'SUCCESS') AS successful_items,
                    (SELECT COUNT(*)
                     FROM t_schedule_item_targets
                     WHERE occurrence_id = :occurrence_id
                       AND status = 'ACCEPTED') AS accepted_targets,
                    (SELECT COUNT(*)
                     FROM t_scheme_runs AS r
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id
                       AND r.status = 'success') AS successful_runs,
                    (SELECT COUNT(*)
                     FROM t_scheme_predictions AS p
                     JOIN t_scheme_runs AS r ON r.run_id = p.run_id
                     JOIN t_schedule_items AS i
                       ON i.item_id = r.schedule_item_id
                     WHERE i.occurrence_id = :occurrence_id)
                        AS predictions
                """
            ),
            {"occurrence_id": occurrence_id},
        ).mappings().one()
    return {key: int(value) for key, value in row.items()}


def _valid_linkage_count(engine, occurrence_id: int) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_schedule_item_targets AS t
                    JOIN t_schedule_items AS i
                      ON i.item_id = t.item_id
                     AND i.occurrence_id = t.occurrence_id
                    JOIN t_scheme_runs AS r
                      ON r.run_id = t.accepted_run_id
                     AND r.schedule_item_id = i.item_id
                    JOIN t_scheme_predictions AS p
                      ON p.id = t.accepted_prediction_id
                     AND p.run_id = r.run_id
                     AND p.scheme_id = t.base_scheme_id
                     AND p.target_tenor = t.target_tenor
                     AND p.horizon = t.horizon
                     AND p.target_date = t.target_date
                    WHERE t.occurrence_id = :occurrence_id
                      AND t.status = 'ACCEPTED'
                      AND t.accepted_at IS NOT NULL
                      AND t.visible_at IS NOT NULL
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        )


def _registered_process_count(engine, occurrence_id: int) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_scheme_runs AS r
                    JOIN t_schedule_items AS i
                      ON i.item_id = r.schedule_item_id
                    WHERE i.occurrence_id = :occurrence_id
                      AND r.execution_token IS NOT NULL
                      AND r.process_id IS NOT NULL
                      AND r.process_group_id IS NOT NULL
                      AND r.queued_at IS NOT NULL
                      AND r.started_at IS NOT NULL
                      AND r.finished_at IS NOT NULL
                    """
                ),
                {"occurrence_id": occurrence_id},
            ).scalar_one()
        )


@unittest.skipUnless(
    os.environ.get("BFL_DAILY_MVP_MYSQL") == "1",
    "set BFL_DAILY_MVP_MYSQL=1 to run isolated daily MVP verification",
)
class DailyCoordinatorMVPMySQLTests(unittest.TestCase):
    def test_complete_21_item_25_target_ledger(self) -> None:
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
            schema, engine = server.create_schema("mvp")
            try:
                _seed_test_registry(engine, policy, configs)
                schedule_key = f"daily-mvp-{schema[-10:]}"
                (
                    occurrence_id,
                    native_generation,
                    databridge_generation,
                ) = _create_mvp_occurrence(
                    engine,
                    policy,
                    configs,
                    schedule_key=schedule_key,
                    manifest_root=server.root,
                    clock=clock,
                )
                frozen = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )
                native_items = [
                    summary
                    for summary in frozen.items
                    if summary.item.runtime_type == "native_adapter"
                ]
                v2_items = [
                    summary
                    for summary in frozen.items
                    if summary.item.runtime_type == "blackbox_v2"
                ]
                frozen_schemes = {
                    row["scheme_id"]: row
                    for row in frozen.occurrence.policy_json["schemes"]
                }
                projection = frozen.occurrence.policy_json[
                    "daily_mvp_test_projection"
                ]

                self.assertEqual(len(native_items), 17)
                self.assertEqual(
                    sum(item.target_count for item in native_items),
                    21,
                )
                self.assertEqual(len(v2_items), 4)
                self.assertEqual(
                    sum(item.target_count for item in v2_items),
                    4,
                )
                self.assertEqual(
                    {
                        scheme_id
                        for scheme_id, item in frozen_schemes.items()
                        if item["input_compatibility"]
                        == "live_source_0629"
                    },
                    LIVE_SOURCE_0629,
                )
                self.assertEqual(
                    sum(
                        item["input_compatibility"] == "generation_v1"
                        for item in frozen_schemes.values()
                    ),
                    14,
                )
                self.assertEqual(
                    projection["cache_completion_qualification"],
                    "EXCLUDED",
                )
                self.assertEqual(
                    projection["capacity_admission_status"],
                    "BLOCKED",
                )
                self.assertFalse(
                    projection["production_cache_gate_passed"]
                )

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
                services = _MVPMySQLServices(
                    engine=engine,
                    policy=policy,
                    clock=clock,
                    occurrence_id=occurrence_id,
                )
                runtime = DailyRuntime(services)
                with _controlled_executor_patches(services, clock):
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
                counts_after_first = _database_counts(
                    engine,
                    occurrence_id,
                )
                runs_after_first = _run_count(engine, occurrence_id)
                replay_id = create_schedule_occurrence(
                    engine,
                    **_mvp_occurrence_args(
                        policy,
                        configs,
                        schedule_key=schedule_key,
                    ),
                )
                final = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=occurrence_id,
                )

                self.assertEqual(first.status, "finished")
                self.assertEqual(
                    set(first.dispatched_scheme_ids),
                    set(policy.schemes),
                )
                self.assertEqual(len(first.dispatched_scheme_ids), 21)
                self.assertEqual(second.dispatched_scheme_ids, ())
                self.assertEqual(replay_id, occurrence_id)
                self.assertEqual(
                    services.attempts,
                    Counter(
                        {
                            scheme_id: 1
                            for scheme_id in policy.schemes
                        }
                    ),
                )
                self.assertEqual(
                    services.max_active_by_runtime["native_adapter"],
                    2,
                )
                self.assertEqual(
                    services.max_active_by_runtime["blackbox_v2"],
                    2,
                )
                self.assertTrue(services.observed_resource_signatures)
                self.assertTrue(
                    services.observed_resource_signatures.issubset(
                        policy.allowed_resource_combinations
                    )
                )
                self.assertEqual(
                    counts_after_first,
                    {
                        "successful_items": 21,
                        "accepted_targets": 25,
                        "successful_runs": 21,
                        "predictions": 25,
                    },
                )
                self.assertEqual(
                    _database_counts(engine, occurrence_id),
                    counts_after_first,
                )
                self.assertEqual(runs_after_first, 21)
                self.assertEqual(_run_count(engine, occurrence_id), 21)
                self.assertEqual(
                    _valid_linkage_count(engine, occurrence_id),
                    25,
                )
                self.assertEqual(
                    _registered_process_count(engine, occurrence_id),
                    21,
                )
                self.assertEqual(
                    services.process_started_callbacks,
                    21,
                )
                self.assertEqual(
                    final.actual_accepted_target_count,
                    25,
                )
                self.assertEqual(
                    final.occurrence.accepted_target_count,
                    25,
                )
                self.assertEqual(
                    final.occurrence.completion_state,
                    "SUCCESS",
                )
                self.assertTrue(
                    all(
                        summary.item.state == "SUCCESS"
                        and summary.item.attempt_no == 1
                        and summary.item.current_run_id is not None
                        and summary.accepted_target_count
                        == summary.target_count
                        for summary in final.items
                    )
                )
            finally:
                engine.dispose()

    def test_sla_breach_is_write_once_after_late_completion(self) -> None:
        policy, configs = _real_policy_and_configs()
        clock = _MutableClock(
            datetime(2026, 7, 23, 22, 40, tzinfo=timezone.utc)
        )
        with (
            _temporary_mysql() as server,
            patch(
                "scheduler.repository."
                "assert_daily_coordinator_epoch_payload_matches_current",
                side_effect=_assert_test_epoch,
            ),
        ):
            schema, engine = server.create_schema("sla")
            normal_engine = None
            try:
                _seed_test_registry(engine, policy, configs)
                breach_id, _, _ = _create_mvp_occurrence(
                    engine,
                    policy,
                    configs,
                    schedule_key=f"daily-mvp-breach-{schema[-10:]}b",
                    manifest_root=server.root,
                    clock=clock,
                )
                breach_snapshot = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=breach_id,
                )
                failing_summary = next(
                    summary
                    for summary in breach_snapshot.items
                    if summary.item.runtime_type == "blackbox_v2"
                )
                failing_scheme_id = (
                    failing_summary.item.base_scheme_id
                )
                failing_envelope = read_schedule_execution_envelope(
                    engine,
                    item_id=failing_summary.item.item_id,
                )
                missing_registry_id = (
                    failing_envelope.targets[0].registry_scheme_id
                )
                services = _MVPMySQLServices(
                    engine=engine,
                    policy=policy,
                    clock=clock,
                    occurrence_id=breach_id,
                    transient_once_scheme_id=failing_scheme_id,
                )
                clock.set(
                    datetime(2026, 7, 23, 22, 56, tzinfo=timezone.utc)
                )
                ordered = sorted(
                    breach_snapshot.items,
                    key=lambda summary: (
                        summary.item.base_scheme_id
                        == failing_scheme_id,
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
                        occurrence_id=breach_id,
                    )
                    self.assertEqual(
                        after_first.actual_accepted_target_count,
                        24,
                    )
                    self.assertEqual(
                        Counter(result.status for result in first_results),
                        Counter({"success": 20, "retry_wait": 1}),
                    )
                    self.assertTrue(
                        all(
                            summary.item.attempt_no == 1
                            for summary in after_first.items
                        )
                    )
                    retry_item = next(
                        summary.item
                        for summary in after_first.items
                        if summary.item.base_scheme_id
                        == failing_scheme_id
                    )
                    self.assertEqual(retry_item.state, "RETRY_WAIT")

                    deadline = datetime(
                        2026,
                        7,
                        24,
                        0,
                        0,
                        tzinfo=timezone.utc,
                    )
                    clock.set(deadline)
                    services.current_time = deadline.astimezone(
                        SHANGHAI
                    )
                    watchdog = DailyRuntime(services).run_watchdog(
                        stage="target_sla",
                        run_date=BUSINESS_DATE,
                    )
                    self.assertEqual(watchdog.status, "breached")
                    self.assertEqual(
                        watchdog.details["accepted_target_count"],
                        24,
                    )
                    self.assertEqual(
                        watchdog.details["sla_outcome"],
                        "BREACHED",
                    )
                    self.assertTrue(
                        any(
                            alert["code"]
                            == "DAILY_TARGET_SLA_BREACHED"
                            for alert in services.alerts
                        )
                    )
                    health = _health_projection(
                        engine,
                        occurrence_id=breach_id,
                        now=deadline,
                    )
                    self.assertEqual(health["overall"], "error")
                    self.assertEqual(
                        health["targets"]["missing_registry_ids"],
                        [missing_registry_id],
                    )

                    clock.set(
                        datetime(
                            2026,
                            7,
                            24,
                            0,
                            1,
                            tzinfo=timezone.utc,
                        )
                    )
                    retry = execute_scheduled_item(
                        engine,
                        item_id=retry_item.item_id,
                        trigger_origin="auto_retry",
                        trusted_verifier=_ExpectationVerifier(),
                    )

                self.assertEqual(retry.status, "success")
                self.assertEqual(retry.attempt_no, 2)
                services.current_time = clock.value.astimezone(SHANGHAI)
                replayed_watchdog = DailyRuntime(services).run_watchdog(
                    stage="target_sla",
                    run_date=BUSINESS_DATE,
                )
                self.assertEqual(replayed_watchdog.status, "breached")
                self.assertEqual(
                    replayed_watchdog.details["sla_outcome"],
                    "BREACHED",
                )
                self.assertEqual(
                    sum(
                        alert["code"] == "DAILY_TARGET_SLA_BREACHED"
                        for alert in services.alerts
                    ),
                    1,
                )
                completed = read_schedule_occurrence_snapshot(
                    engine,
                    occurrence_id=breach_id,
                )
                self.assertEqual(
                    completed.actual_accepted_target_count,
                    25,
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
                    24,
                )
                self.assertEqual(
                    completed.occurrence.sla_evaluated_at,
                    deadline.replace(tzinfo=None),
                )
                self.assertEqual(_run_count(engine, breach_id), 22)
                self.assertEqual(
                    _database_counts(engine, breach_id),
                    {
                        "successful_items": 21,
                        "accepted_targets": 25,
                        "successful_runs": 21,
                        "predictions": 25,
                    },
                )

                normal_schema, normal_engine = server.create_schema("met")
                _seed_test_registry(normal_engine, policy, configs)
                normal_id, _, _ = _create_mvp_occurrence(
                    normal_engine,
                    policy,
                    configs,
                    schedule_key=(
                        f"daily-mvp-normal-{normal_schema[-10:]}n"
                    ),
                    manifest_root=server.root,
                    clock=clock,
                )
                normal_snapshot = read_schedule_occurrence_snapshot(
                    normal_engine,
                    occurrence_id=normal_id,
                )
                normal_services = _MVPMySQLServices(
                    engine=normal_engine,
                    policy=policy,
                    clock=clock,
                    occurrence_id=normal_id,
                )
                clock.set(
                    datetime(2026, 7, 23, 22, 56, tzinfo=timezone.utc)
                )
                with _controlled_executor_patches(
                    normal_services,
                    clock,
                ):
                    normal_results = [
                        execute_scheduled_item(
                            normal_engine,
                            item_id=summary.item.item_id,
                            trigger_origin="apscheduler",
                            trusted_verifier=_ExpectationVerifier(),
                        )
                        for summary in normal_snapshot.items
                    ]
                self.assertTrue(
                    all(
                        result.status == "success"
                        for result in normal_results
                    ),
                    [
                        (
                            result.scheme_id,
                            result.status,
                            result.failure_code,
                            result.error_message,
                        )
                        for result in normal_results
                        if result.status != "success"
                    ],
                )
                before_deadline = datetime(
                    2026,
                    7,
                    23,
                    23,
                    59,
                    tzinfo=timezone.utc,
                )
                clock.set(before_deadline)
                met = evaluate_schedule_occurrence_target_sla(
                    normal_engine,
                    occurrence_id=normal_id,
                    evaluated_at=before_deadline,
                    _clock=clock,
                )
                self.assertEqual(met.status, "MET")
                self.assertEqual(met.accepted_by_deadline_count, 25)

                with normal_engine.begin() as connection:
                    receipt = connection.execute(
                        text(
                            """
                            SELECT t.target_id,
                                   t.registry_scheme_id,
                                   t.accepted_prediction_id
                            FROM t_schedule_item_targets AS t
                            JOIN t_schedule_items AS i
                              ON i.item_id = t.item_id
                            WHERE t.occurrence_id = :occurrence_id
                              AND i.runtime_type = 'blackbox_v2'
                            ORDER BY t.target_id
                            LIMIT 1
                            """
                        ),
                        {"occurrence_id": normal_id},
                    ).mappings().one()
                    prediction_exists = int(
                        connection.execute(
                            text(
                                "SELECT COUNT(*) "
                                "FROM t_scheme_predictions "
                                "WHERE id = :prediction_id"
                            ),
                            {
                                "prediction_id":
                                    receipt["accepted_prediction_id"]
                            },
                        ).scalar_one()
                    )
                    connection.execute(
                        text(
                            "UPDATE t_schedule_item_targets "
                            "SET visible_at = NULL "
                            "WHERE target_id = :target_id"
                        ),
                        {"target_id": receipt["target_id"]},
                    )
                self.assertEqual(prediction_exists, 1)
                receipt_health = _health_projection(
                    normal_engine,
                    occurrence_id=normal_id,
                    now=before_deadline,
                )
                self.assertEqual(receipt_health["overall"], "error")
                self.assertEqual(
                    receipt_health["targets"]["missing_registry_ids"],
                    [receipt["registry_scheme_id"]],
                )
                self.assertEqual(
                    receipt_health["targets"][
                        "receipt_missing_registry_ids"
                    ],
                    [receipt["registry_scheme_id"]],
                )
            finally:
                if normal_engine is not None:
                    normal_engine.dispose()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
