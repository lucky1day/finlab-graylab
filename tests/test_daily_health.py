from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from scheduler import daily_health, repository as scheduler_repository
except (ImportError, ModuleNotFoundError):
    daily_health = None
    scheduler_repository = None

from scheduler.repository import (
    ScheduleExecutionEnvelope,
    ScheduleInputGenerationEnvelope,
    ScheduleItemEnvelope,
    ScheduleOccurrenceEnvelope,
    ScheduleOccurrenceItemSummary,
    ScheduleOccurrenceSnapshot,
    ScheduleTargetEnvelope,
    SchedulerHeartbeat,
)

ScheduleHealthEnvelope = (
    None
    if scheduler_repository is None
    else getattr(scheduler_repository, "ScheduleHealthEnvelope", None)
)


UTC = timezone.utc
SLA_DEADLINE = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 24, 0, 1, tzinfo=UTC)
V2_SCHEME_IDS = (
    "v2_alpha",
    "v2_beta",
    "v2_gamma",
    "v2_delta",
)


class DailyHealthProjectionTests(unittest.TestCase):
    def test_public_heartbeat_exposes_coordinator_mode(self) -> None:
        snapshot, heartbeat, envelopes = _fixture()
        heartbeat = replace(
            heartbeat,
            details={
                **dict(heartbeat.details),
                "coordinator_mode": "ledger",
            },
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(
            result["scheduler_heartbeat"]["details"][
                "coordinator_mode"
            ],
            "ledger",
        )

    def test_projects_json_safe_healthy_ledger_with_four_v2_items(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        json.dumps(result)
        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["occurrence"]["sla_outcome"], "MET")
        self.assertEqual(
            result["items"],
            {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 0,
                "failed": 0,
                "failed_base_scheme_ids": [],
                "details": [
                    {
                        "base_scheme_id": summary.item.base_scheme_id,
                        "runtime_type": summary.item.runtime_type,
                        "state": summary.item.state,
                        "failure_code": summary.item.failure_code,
                        "input_generation_id": (
                            summary.item.input_generation_id
                        ),
                        "generation_state": "SEALED",
                        "generation_issue": None,
                        "expected_targets": summary.target_count,
                        "committed_targets": (
                            summary.accepted_target_count
                        ),
                        "linked_targets": summary.accepted_target_count,
                        "accepted_targets": (
                            summary.accepted_target_count
                        ),
                        "receipt_missing_registry_ids": [],
                    }
                    for summary in snapshot.items
                ],
            },
        )
        self.assertEqual(
            result["targets"],
            {
                "expected": 25,
                "actual": 25,
                "accepted": 25,
                "committed": 25,
                "linked": 25,
                "late": 0,
                "missing": 0,
                "missing_registry_ids": [],
                "receipt_missing_registry_ids": [],
                "availability_basis": "db_commit_visibility_receipt",
            },
        )
        self.assertEqual(
            result["api_visibility"],
            {
                "asserted": False,
                "proof_required": "uncached_external_api_probe",
                "probe_endpoint": "/api/daily-schedule/visibility",
                "canonical_endpoint": "/api/predictions",
                "cache_policy": "no-store",
            },
        )
        self.assertEqual(
            result["generations"]["native_source"]["generation_id"],
            "native-20260724-g1",
        )
        self.assertEqual(
            result["generations"]["native_source"]["feature_date"],
            "2026-07-23",
        )
        self.assertEqual(
            result["generations"]["native_source"]["manifest_sha256"],
            "a" * 64,
        )
        self.assertEqual(
            result["generations"]["databridge_v1"]["generation_id"],
            "databridge-20260724-g1",
        )
        self.assertEqual(
            result["generations"]["databridge_readiness"]["status"],
            "ON_TIME",
        )
        self.assertEqual(
            [item["base_scheme_id"] for item in result["blackbox_v2"]],
            list(V2_SCHEME_IDS),
        )
        self.assertTrue(
            all(item["accepted_at"] is not None for item in result["blackbox_v2"])
        )
        self.assertEqual(result["scheduler_heartbeat"]["status"], "ok")
        self.assertNotIn(
            "process_id",
            result["scheduler_heartbeat"],
        )
        self.assertNotIn(
            "host_name",
            result["scheduler_heartbeat"],
        )

    def test_databridge_sealed_after_0655_is_explicitly_late(self) -> None:
        snapshot, heartbeat, envelopes = _fixture()
        late_generation = replace(
            next(
                envelope.generation
                for envelope in envelopes
                if envelope.item.runtime_type == "blackbox_v2"
            ),
            sealed_at=datetime(2026, 7, 23, 22, 56, tzinfo=UTC),
        )
        late_envelopes = tuple(
            replace(envelope, generation=late_generation)
            if envelope.item.runtime_type == "blackbox_v2"
            else envelope
            for envelope in envelopes
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=late_envelopes,
            now=NOW,
        )

        readiness = result["generations"]["databridge_readiness"]
        self.assertEqual(readiness["status"], "LATE")
        self.assertEqual(
            readiness["reason"],
            "DATABRIDGE_SEALED_AFTER_GUARDRAIL",
        )
        self.assertEqual(
            readiness["guardrail_at"],
            "2026-07-23T22:55:00Z",
        )
        self.assertIn(
            "DATABRIDGE_READINESS_LATE",
            result["reasons"],
        )

    def test_accepted_target_without_visibility_receipt_is_not_healthy(
        self,
    ) -> None:
        snapshot, heartbeat, envelopes = _fixture()
        envelopes = list(envelopes)
        first_v2_index = next(
            index
            for index, envelope in enumerate(envelopes)
            if envelope.item.runtime_type == "blackbox_v2"
        )
        first_v2 = envelopes[first_v2_index]
        envelopes[first_v2_index] = replace(
            first_v2,
            targets=tuple(
                replace(target, visible_at=None)
                for target in first_v2.targets
            ),
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("VISIBILITY_RECEIPT_MISSING", result["reasons"])
        self.assertIn("SLA_EVIDENCE_MISMATCH", result["reasons"])
        receipt_missing_ids = [
            target.registry_scheme_id
            for target in first_v2.targets
        ]
        self.assertEqual(result["targets"]["accepted"], 24)
        self.assertEqual(result["targets"]["committed"], 25)
        self.assertEqual(result["targets"]["linked"], 25)
        self.assertEqual(result["targets"]["missing"], 1)
        self.assertEqual(
            result["targets"]["missing_registry_ids"],
            receipt_missing_ids,
        )
        self.assertEqual(
            result["targets"]["receipt_missing_registry_ids"],
            receipt_missing_ids,
        )
        first_v2_projection = next(
            item
            for item in result["blackbox_v2"]
            if item["base_scheme_id"] == first_v2.item.base_scheme_id
        )
        self.assertIsNone(first_v2_projection["accepted_at"])
        self.assertEqual(first_v2_projection["accepted_targets"], 0)
        self.assertEqual(first_v2_projection["committed_targets"], 1)
        self.assertEqual(
            first_v2_projection["receipt_missing_registry_ids"],
            receipt_missing_ids,
        )

    def test_health_envelopes_project_unbound_building_invalidated_and_unsupported(
        self,
    ) -> None:
        self.assertIsNotNone(
            ScheduleHealthEnvelope,
            "scheduler.repository.ScheduleHealthEnvelope is missing",
        )
        snapshot, heartbeat, execution_envelopes = _fixture(
            missing_registry_ids={
                "native_00__h1__10Y",
                "native_01__h1__10Y",
                "native_02__h1__10Y",
            },
            sla_outcome="PENDING",
        )
        health_envelopes = [
            ScheduleHealthEnvelope(
                occurrence=envelope.occurrence,
                item=envelope.item,
                generation=envelope.generation,
                calendar_generation=envelope.calendar_generation,
                targets=envelope.targets,
                generation_issue=None,
            )
            for envelope in execution_envelopes
        ]
        unbound = health_envelopes[0]
        building = health_envelopes[1]
        invalidated = health_envelopes[2]
        unsupported = health_envelopes[3]
        health_envelopes[0] = replace(
            unbound,
            item=replace(
                unbound.item,
                input_generation_id=None,
            ),
            generation=None,
            calendar_generation=None,
            generation_issue="UNBOUND_INPUT_GENERATION",
        )
        health_envelopes[1] = replace(
            building,
            generation=replace(
                building.generation,
                state="BUILDING",
                sealed_at=None,
            ),
            calendar_generation=replace(
                building.calendar_generation,
                state="BUILDING",
                sealed_at=None,
            ),
        )
        health_envelopes[2] = replace(
            invalidated,
            generation=replace(
                invalidated.generation,
                state="INVALIDATED",
                invalidated_at=NOW,
                invalid_reason="manifest revoked",
            ),
            calendar_generation=replace(
                invalidated.calendar_generation,
                state="INVALIDATED",
                invalidated_at=NOW,
                invalid_reason="manifest revoked",
            ),
        )
        unsupported_item = replace(
            unsupported.item,
            state="FAILED_TERMINAL",
            failure_code="NATIVE_GENERATION_UNSUPPORTED",
        )
        health_envelopes[3] = replace(
            unsupported,
            item=unsupported_item,
            generation=None,
            calendar_generation=None,
            generation_issue="UNBOUND_INPUT_GENERATION",
        )
        summaries = list(snapshot.items)
        summaries[0] = replace(
            summaries[0],
            item=health_envelopes[0].item,
        )
        summaries[1] = replace(
            summaries[1],
            item=health_envelopes[1].item,
        )
        summaries[2] = replace(
            summaries[2],
            item=health_envelopes[2].item,
        )
        summaries[3] = replace(
            summaries[3],
            item=unsupported_item,
        )
        snapshot = replace(snapshot, items=tuple(summaries))

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=health_envelopes,
            now=SLA_DEADLINE - timedelta(minutes=30),
        )

        self.assertEqual(result["overall"], "error")
        self.assertNotIn("LEDGER_HEALTH_UNAVAILABLE", result["reasons"])
        self.assertIn("GENERATION_UNBOUND", result["reasons"])
        self.assertIn("GENERATION_NOT_SEALED", result["reasons"])
        self.assertIn("GENERATION_INVALIDATED", result["reasons"])
        self.assertIn("ITEM_FAILURE", result["reasons"])
        details = {
            item["base_scheme_id"]: item
            for item in result["items"]["details"]
        }
        self.assertEqual(
            details[unbound.item.base_scheme_id]["generation_state"],
            "UNBOUND",
        )
        self.assertEqual(
            details[building.item.base_scheme_id]["generation_state"],
            "BUILDING",
        )
        self.assertEqual(
            details[invalidated.item.base_scheme_id]["generation_state"],
            "INVALIDATED",
        )
        self.assertEqual(
            details[unsupported.item.base_scheme_id]["failure_code"],
            "NATIVE_GENERATION_UNSUPPORTED",
        )

    def test_visibility_receipt_before_acceptance_is_ledger_mismatch(
        self,
    ) -> None:
        snapshot, heartbeat, envelopes = _fixture()
        envelopes = list(envelopes)
        first = envelopes[0]
        target = first.targets[0]
        self.assertIsNotNone(target.accepted_at)
        envelopes[0] = replace(
            first,
            targets=(
                replace(
                    target,
                    visible_at=target.accepted_at - timedelta(seconds=1),
                ),
                *first.targets[1:],
            ),
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("LEDGER_EVIDENCE_MISMATCH", result["reasons"])
        self.assertEqual(result["targets"]["accepted"], 24)
        self.assertEqual(result["targets"]["linked"], 24)
        self.assertEqual(result["targets"]["missing"], 1)
        self.assertEqual(
            result["targets"]["missing_registry_ids"],
            [first.targets[0].registry_scheme_id],
        )

    def test_sealed_databridge_with_unaccepted_v2_target_is_error(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        missing_id = "v2_delta__h5__1Y"
        snapshot, heartbeat, envelopes = _fixture(
            missing_registry_ids={missing_id},
            sla_outcome="BREACHED",
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("DATABRIDGE_READY_V2_INCOMPLETE", result["reasons"])
        self.assertIn("SLA_BREACHED", result["reasons"])
        self.assertEqual(result["occurrence"]["sla_outcome"], "BREACHED")
        self.assertEqual(result["targets"]["accepted"], 24)
        self.assertEqual(result["targets"]["missing"], 1)
        self.assertEqual(
            result["targets"]["missing_registry_ids"],
            [missing_id],
        )
        delta = next(
            item
            for item in result["blackbox_v2"]
            if item["base_scheme_id"] == "v2_delta"
        )
        self.assertEqual(delta["accepted_targets"], 0)
        self.assertIsNone(delta["accepted_at"])

    def test_pre_deadline_native_target_gap_is_not_healthy(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        missing_id = "native_16__h1__10Y"
        checked_at = SLA_DEADLINE - timedelta(minutes=5)
        snapshot, heartbeat, envelopes = _fixture(
            missing_registry_ids={missing_id},
            sla_outcome="PENDING",
        )

        result = daily_health.project_daily_health(
            snapshot,
            replace(
                heartbeat,
                heartbeat_at=checked_at - timedelta(seconds=15),
            ),
            execution_envelopes=envelopes,
            now=checked_at,
        )

        self.assertNotEqual(result["overall"], "ok")
        self.assertIn("DAILY_TARGETS_INCOMPLETE", result["reasons"])
        self.assertNotIn("SLA_DEADLINE_INCOMPLETE", result["reasons"])
        self.assertEqual(result["targets"]["accepted"], 24)
        self.assertEqual(
            result["targets"]["missing_registry_ids"],
            [missing_id],
        )

    def test_progress_watchdog_eta_overline_is_not_healthy(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()
        heartbeat = replace(
            heartbeat,
            state="WATCHDOG_PROGRESS",
            details={
                "accepted_target_count": 24,
                "expected_target_count": 25,
                "eta_overline": True,
            },
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertNotEqual(result["overall"], "ok")
        self.assertIn("WATCHDOG_ETA_OVERLINE", result["reasons"])
        self.assertEqual(
            result["scheduler_heartbeat"]["status"],
            "degraded",
        )
        self.assertIs(
            result["scheduler_heartbeat"]["details"]["eta_overline"],
            True,
        )

    def test_progress_watchdog_zero_accepted_targets_is_not_healthy(
        self,
    ) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()
        heartbeat = replace(
            heartbeat,
            state="WATCHDOG_PROGRESS",
            details={
                "accepted_target_count": 0,
                "expected_target_count": 25,
                "eta_overline": False,
            },
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertNotEqual(result["overall"], "ok")
        self.assertIn("WATCHDOG_NO_PROGRESS", result["reasons"])
        self.assertEqual(
            result["scheduler_heartbeat"]["status"],
            "degraded",
        )

    def test_terminal_item_failure_remains_fail_closed(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        missing_id = "native_16__h1__10Y"
        snapshot, heartbeat, envelopes = _fixture(
            missing_registry_ids={missing_id},
            sla_outcome="PENDING",
        )
        failed_summary = snapshot.items[16]
        failed_item = replace(
            failed_summary.item,
            state="FAILED_TERMINAL",
            failure_code="ALGORITHM",
        )
        snapshot = replace(
            snapshot,
            items=(
                *snapshot.items[:16],
                replace(failed_summary, item=failed_item),
                *snapshot.items[17:],
            ),
        )
        failed_envelope = envelopes[16]
        envelopes = (
            *envelopes[:16],
            replace(failed_envelope, item=failed_item),
            *envelopes[17:],
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("ITEM_FAILURE", result["reasons"])
        self.assertEqual(
            result["items"]["failed_base_scheme_ids"],
            ["native_16"],
        )

    def test_late_completion_does_not_rewrite_breached_sla_to_met(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        late_id = "v2_delta__h5__1Y"
        snapshot, heartbeat, envelopes = _fixture(
            late_registry_ids={late_id},
            sla_outcome="BREACHED",
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW + timedelta(minutes=5),
        )

        self.assertEqual(result["overall"], "error")
        self.assertEqual(result["occurrence"]["sla_outcome"], "BREACHED")
        self.assertEqual(result["targets"]["accepted"], 25)
        self.assertEqual(result["targets"]["late"], 1)
        self.assertEqual(result["targets"]["missing"], 0)
        self.assertIn("SLA_BREACHED", result["reasons"])

    def test_complete_late_targets_with_pending_sla_fail_after_deadline(
        self,
    ) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        _, _, baseline_envelopes = _fixture()
        all_registry_ids = {
            target.registry_scheme_id
            for envelope in baseline_envelopes
            for target in envelope.targets
        }
        snapshot, heartbeat, envelopes = _fixture(
            late_registry_ids=all_registry_ids,
            sla_outcome="PENDING",
        )
        evaluated_at = datetime(
            2026,
            7,
            24,
            8,
            4,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )

        result = daily_health.project_daily_health(
            snapshot,
            replace(
                heartbeat,
                heartbeat_at=evaluated_at - timedelta(seconds=15),
            ),
            execution_envelopes=envelopes,
            now=evaluated_at,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn(
            "SLA_OUTCOME_PENDING_AFTER_DEADLINE",
            result["reasons"],
        )
        self.assertEqual(result["occurrence"]["sla_outcome"], "PENDING")
        self.assertEqual(result["targets"]["expected"], 25)
        self.assertEqual(result["targets"]["accepted"], 25)
        self.assertEqual(result["targets"]["late"], 25)
        self.assertEqual(result["targets"]["missing"], 0)

    def test_stale_or_error_heartbeat_is_fail_closed(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()
        stale = replace(
            heartbeat,
            heartbeat_at=NOW - timedelta(seconds=121),
        )

        stale_result = daily_health.project_daily_health(
            snapshot,
            stale,
            execution_envelopes=envelopes,
            now=NOW,
            heartbeat_stale_after=timedelta(seconds=120),
        )
        error_result = daily_health.project_daily_health(
            snapshot,
            replace(heartbeat, state="ERROR"),
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertEqual(stale_result["overall"], "error")
        self.assertEqual(
            stale_result["scheduler_heartbeat"]["status"],
            "stale",
        )
        self.assertIn("HEARTBEAT_STALE", stale_result["reasons"])
        self.assertEqual(error_result["overall"], "error")
        self.assertEqual(
            error_result["scheduler_heartbeat"]["status"],
            "error",
        )
        self.assertIn("HEARTBEAT_ERROR", error_result["reasons"])

    def test_envelopes_are_cross_checked_against_snapshot_aggregates(self) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes[:-1],
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("LEDGER_EVIDENCE_INCOMPLETE", result["reasons"])
        self.assertNotIn("v2_delta", {
            item["base_scheme_id"] for item in result["blackbox_v2"]
        })

    def test_accepted_target_prediction_linkage_drift_is_unhealthy(
        self,
    ) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()
        first = envelopes[0]
        corrupted = replace(
            first,
            targets=(
                replace(
                    first.targets[0],
                    accepted_linkage_valid=False,
                ),
                *first.targets[1:],
            ),
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=(corrupted, *envelopes[1:]),
            now=NOW,
        )

        self.assertEqual(result["overall"], "error")
        self.assertIn("LEDGER_EVIDENCE_MISMATCH", result["reasons"])

    def test_public_projection_redacts_internal_failure_and_heartbeat_details(
        self,
    ) -> None:
        self.assertIsNotNone(daily_health, "scheduler.daily_health is missing")
        snapshot, heartbeat, envelopes = _fixture()
        secret = "mysql://operator:secret@localhost/bond_db"
        snapshot = replace(
            snapshot,
            occurrence=replace(
                snapshot.occurrence,
                failure_code="GENERATION_BUILD_FAILED",
                failure_message=secret,
            ),
        )
        heartbeat = replace(
            heartbeat,
            details={
                **dict(heartbeat.details),
                "Authorization": "Bearer top-secret",
                "manifest_uri": "/Users/operator/private/manifest.json",
                "error": secret,
            },
        )

        result = daily_health.project_daily_health(
            snapshot,
            heartbeat,
            execution_envelopes=envelopes,
            now=NOW,
        )

        self.assertNotIn(
            "failure_message",
            result["occurrence"],
        )
        self.assertEqual(
            result["occurrence"]["failure_code"],
            "GENERATION_BUILD_FAILED",
        )
        self.assertEqual(
            result["scheduler_heartbeat"]["details"],
            {
                "last_progress_at": "2026-07-24T00:00:40Z",
                "phase": "acceptance",
                "running_pools": ["native", "v2"],
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("/Users/operator/private", serialized)
        self.assertNotIn("operator:secret", serialized)


def _fixture(
    *,
    missing_registry_ids: set[str] | None = None,
    late_registry_ids: set[str] | None = None,
    sla_outcome: str = "MET",
) -> tuple[
    ScheduleOccurrenceSnapshot,
    SchedulerHeartbeat,
    tuple[ScheduleExecutionEnvelope, ...],
]:
    missing_registry_ids = missing_registry_ids or set()
    late_registry_ids = late_registry_ids or set()
    native_generation = ScheduleInputGenerationEnvelope(
        generation_id="native-20260724-g1",
        generation_type="native_source",
        business_date="2026-07-24",
        feature_date="2026-07-23",
        readiness_basis="CLOCK_CONTRACT",
        source_commit_token="native-source-seal",
        dataset_content_id="native-content",
        schema_version="native-generation-v1",
        exporter_version="native-exporter-v1",
        manifest_uri="/tmp/native-20260724-g1.json",
        manifest_sha256="a" * 64,
        native_generation_id=None,
        native_manifest_sha256=None,
        state="SEALED",
        sealed_at=datetime(2026, 7, 23, 22, 45, tzinfo=UTC),
        invalidated_at=None,
        invalid_reason=None,
    )
    databridge_generation = ScheduleInputGenerationEnvelope(
        generation_id="databridge-20260724-g1",
        generation_type="databridge_v1",
        business_date="2026-07-24",
        feature_date="2026-07-23",
        readiness_basis="UPSTREAM_SEAL",
        source_commit_token="databridge-source-seal",
        dataset_content_id="databridge-content",
        schema_version="data-bridge-v1",
        exporter_version="databridge-exporter-v1",
        manifest_uri="/tmp/databridge-20260724-g1.json",
        manifest_sha256="b" * 64,
        native_generation_id=native_generation.generation_id,
        native_manifest_sha256=native_generation.manifest_sha256,
        state="SEALED",
        sealed_at=datetime(2026, 7, 23, 22, 50, tzinfo=UTC),
        invalidated_at=None,
        invalid_reason=None,
    )
    item_specs: list[tuple[str, str, tuple[str, ...], int]] = []
    for index in range(17):
        tenors = ("10Y", "5Y") if index < 4 else ("10Y",)
        item_specs.append(
            (f"native_{index:02d}", "native_adapter", tenors, 0)
        )
    for index, scheme_id in enumerate(V2_SCHEME_IDS):
        item_specs.append((scheme_id, "blackbox_v2", ("1Y",), index * 2))

    occurrence = ScheduleOccurrenceEnvelope(
        occurrence_id=101,
        schedule_key="daily-signals",
        predict_date="2026-07-24",
        feature_date="2026-07-23",
        policy_version="daily-scheduler-v1",
        policy_sha256="c" * 64,
        policy_json={
            "timezone": "Asia/Shanghai",
            "times": {
                "databridge_readiness_guardrail": "06:55",
            },
        },
        registry_digest="d" * 64,
        completion_state=(
            "SUCCESS"
            if not missing_registry_ids
            else "RUNNING"
        ),
        expected_item_count=21,
        expected_target_count=25,
        accepted_target_count=25 - len(missing_registry_ids),
        sla_accepted_target_count=(
            25 - len(missing_registry_ids) - len(late_registry_ids)
        ),
        sla_deadline_at=SLA_DEADLINE,
        recovery_cutoff_at=datetime(2026, 7, 24, 0, 30, tzinfo=UTC),
        sla_outcome=sla_outcome,
        sla_evaluated_at=SLA_DEADLINE,
        sla_reason=None if sla_outcome == "MET" else "TARGETS_MISSING_AT_DEADLINE",
        failure_code=None,
        failure_message=None,
        started_at=datetime(2026, 7, 23, 22, 30, tzinfo=UTC),
        completed_at=(
            datetime(2026, 7, 23, 23, 50, tzinfo=UTC)
            if not missing_registry_ids
            else None
        ),
    )
    summaries: list[ScheduleOccurrenceItemSummary] = []
    envelopes: list[ScheduleExecutionEnvelope] = []
    accepted_total = 0
    for item_id, (
        scheme_id,
        runtime_type,
        tenors,
        release_offset,
    ) in enumerate(item_specs, start=1):
        release_at = datetime(
            2026,
            7,
            23,
            22,
            50,
            tzinfo=UTC,
        ) + timedelta(minutes=release_offset)
        targets: list[ScheduleTargetEnvelope] = []
        for target_id, tenor in enumerate(tenors, start=1):
            horizon = 5 if runtime_type == "blackbox_v2" else 1
            registry_id = f"{scheme_id}__h{horizon}__{tenor}"
            is_missing = registry_id in missing_registry_ids
            is_late = registry_id in late_registry_ids
            accepted_at = None
            if not is_missing:
                accepted_at = (
                    SLA_DEADLINE + timedelta(minutes=3)
                    if is_late
                    else datetime(2026, 7, 23, 23, 30, tzinfo=UTC)
                )
                accepted_total += 1
            targets.append(
                ScheduleTargetEnvelope(
                    target_id=item_id * 10 + target_id,
                    occurrence_id=occurrence.occurrence_id,
                    item_id=item_id,
                    registry_scheme_id=registry_id,
                    base_scheme_id=scheme_id,
                    runtime_type=runtime_type,
                    task_type="T+5" if horizon == 5 else "T+1",
                    target_tenor=tenor,
                    horizon=horizon,
                    target_date="2026-07-31",
                    status="PENDING" if is_missing else "ACCEPTED",
                    accepted_run_id=None if is_missing else item_id * 100,
                    accepted_prediction_id=(
                        None if is_missing else item_id * 1000 + target_id
                    ),
                    accepted_at=accepted_at,
                    visible_at=accepted_at,
                    accepted_linkage_valid=not is_missing,
                )
            )
        accepted_count = sum(target.status == "ACCEPTED" for target in targets)
        item = ScheduleItemEnvelope(
            item_id=item_id,
            occurrence_id=occurrence.occurrence_id,
            base_scheme_id=scheme_id,
            runtime_type=runtime_type,
            scheme_version="v1",
            code_sha256="e" * 64,
            config_sha256="f" * 64,
            cache_group=f"cache-{scheme_id}",
            input_generation_id=(
                databridge_generation.generation_id
                if runtime_type == "blackbox_v2"
                else native_generation.generation_id
            ),
            resource_class="cpu-light",
            internal_workers=1,
            release_offset_minutes=release_offset,
            release_at=release_at,
            deadline_at=datetime(2026, 7, 23, 23, 55, tzinfo=UTC),
            recovery_cutoff_at=occurrence.recovery_cutoff_at,
            occurrence_sla_deadline_at=occurrence.sla_deadline_at,
            state="SUCCESS" if accepted_count == len(targets) else "RUNNING",
            sla_status="ON_TIME",
            late_reason=None,
            sla_evaluated_at=datetime(2026, 7, 23, 23, 30, tzinfo=UTC),
            attempt_no=1,
            current_run_id=item_id * 100,
            started_at=release_at,
            completed_at=(
                max(
                    target.accepted_at
                    for target in targets
                    if target.accepted_at is not None
                )
                if accepted_count == len(targets)
                else None
            ),
            failure_code=None,
            failure_message=None,
        )
        summaries.append(
            ScheduleOccurrenceItemSummary(
                item=item,
                target_count=len(targets),
                accepted_target_count=accepted_count,
            )
        )
        envelopes.append(
            ScheduleExecutionEnvelope(
                occurrence=occurrence,
                item=item,
                generation=(
                    databridge_generation
                    if runtime_type == "blackbox_v2"
                    else native_generation
                ),
                calendar_generation=native_generation,
                targets=tuple(targets),
            )
        )
    snapshot = ScheduleOccurrenceSnapshot(
        occurrence=replace(
            occurrence,
            accepted_target_count=accepted_total,
        ),
        items=tuple(summaries),
        actual_item_count=len(summaries),
        actual_target_count=sum(item.target_count for item in summaries),
        actual_accepted_target_count=accepted_total,
        item_state_counts=tuple(
            sorted(
                {
                    state: sum(summary.item.state == state for summary in summaries)
                    for state in {summary.item.state for summary in summaries}
                }.items()
            )
        ),
    )
    heartbeat = SchedulerHeartbeat(
        service_name="daily-coordinator",
        process_id=4242,
        host_name="mac-studio",
        state="RUNNING",
        occurrence_id=occurrence.occurrence_id,
        heartbeat_at=NOW - timedelta(seconds=15),
        details={
            "phase": "acceptance",
            "last_progress_at": NOW - timedelta(seconds=20),
            "running_pools": {"native", "v2"},
        },
    )
    return snapshot, heartbeat, tuple(envelopes)


if __name__ == "__main__":
    unittest.main()
