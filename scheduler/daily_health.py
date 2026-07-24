"""日批 ledger 的只读、JSON-safe 健康投影。

本模块不查询 Registry、数据库或文件系统。
调用方必须提供同一只读账本中取得的 occurrence 快照、
每个 item 的执行信封，以及独立 scheduler 心跳。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from scheduler.daily_ledger import (
    GENERATION_INVALIDATED,
    GENERATION_SEALED,
    ITEM_EXPIRED,
    ITEM_FAILED_TERMINAL,
    ITEM_SLA_LATE,
    ITEM_SUCCESS,
    SLA_BREACHED,
    SLA_MET,
    SLA_PENDING,
    TARGET_ACCEPTED,
)
from scheduler.repository import (
    ScheduleExecutionEnvelope,
    ScheduleHealthEnvelope,
    ScheduleInputGenerationEnvelope,
    ScheduleOccurrenceItemSummary,
    ScheduleOccurrenceSnapshot,
    SchedulerHeartbeat,
)


DEFAULT_HEARTBEAT_STALE_AFTER = timedelta(seconds=120)
SLA_PENDING_AFTER_DEADLINE_REASON = (
    "SLA_OUTCOME_PENDING_AFTER_DEADLINE"
)
_HEARTBEAT_ERROR_STATES = frozenset(
    {"ERROR", "FAILED", "FATAL", "STOPPED"}
)
_FAILED_ITEM_STATES = frozenset(
    {ITEM_FAILED_TERMINAL, ITEM_EXPIRED}
)
_HealthEnvelope = ScheduleExecutionEnvelope | ScheduleHealthEnvelope
_PUBLIC_HEARTBEAT_DETAIL_FIELDS = frozenset(
    {
        "accepted_target_count",
        "business_date",
        "coordinator_mode",
        "execution_status",
        "eta_overline",
        "expected_target_count",
        "expired_item_count",
        "failed_scheme_ids",
        "failure_codes",
        "feature_date",
        "last_progress_at",
        "missing_requirements",
        "phase",
        "reason",
        "running_pools",
        "running_scheme_ids",
        "scheme_id",
        "stage",
        "trigger_origin",
    }
)


def project_daily_health(
    snapshot: ScheduleOccurrenceSnapshot,
    heartbeat: SchedulerHeartbeat | None,
    *,
    execution_envelopes: Sequence[_HealthEnvelope],
    now: datetime,
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
) -> dict[str, object]:
    """把冻结日批账本投影为可直接返回 API 的健康状态。

    ``snapshot`` 是 occurrence/item 聚合的权威来源；
    ``execution_envelopes`` 提供同一 ledger 内冻结的 target 与 generation
    证据。两者会被逐项交叉校验。``heartbeat.details`` 只投影明确
    allowlist 中的非敏感运行字段，不参与业务判定。
    """
    if heartbeat_stale_after <= timedelta(0):
        raise ValueError("heartbeat_stale_after must be positive")
    projected_now = _as_utc(now, "now")
    occurrence = snapshot.occurrence
    sla_deadline_at = _as_utc(
        occurrence.sla_deadline_at,
        "sla_deadline_at",
    )
    deadline_reached = projected_now >= sla_deadline_at
    reasons: list[str] = []

    def add_reason(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    summaries_by_id = _index_snapshot_items(snapshot, add_reason)
    envelopes_by_id = _index_envelopes(
        execution_envelopes,
        occurrence_id=occurrence.occurrence_id,
        add_reason=add_reason,
    )
    expected_item_ids = set(summaries_by_id)
    actual_envelope_ids = set(envelopes_by_id)
    if expected_item_ids != actual_envelope_ids:
        add_reason("LEDGER_EVIDENCE_INCOMPLETE")

    target_rows = []
    valid_linked_target_objects: set[int] = set()
    seen_registry_ids: set[str] = set()
    for item_id in sorted(expected_item_ids & actual_envelope_ids):
        summary = summaries_by_id[item_id]
        envelope = envelopes_by_id[item_id]
        if (
            envelope.item.base_scheme_id
            != summary.item.base_scheme_id
            or envelope.item.runtime_type != summary.item.runtime_type
        ):
            add_reason("LEDGER_EVIDENCE_MISMATCH")
        committed_for_item = 0
        linked_for_item = 0
        for target in envelope.targets:
            target_relationship_valid = not (
                target.occurrence_id != occurrence.occurrence_id
                or target.item_id != item_id
                or target.base_scheme_id != envelope.item.base_scheme_id
                or target.runtime_type != envelope.item.runtime_type
            )
            if not target_relationship_valid:
                add_reason("LEDGER_EVIDENCE_MISMATCH")
            if target.registry_scheme_id in seen_registry_ids:
                add_reason("LEDGER_EVIDENCE_MISMATCH")
            seen_registry_ids.add(target.registry_scheme_id)
            if target.status == TARGET_ACCEPTED:
                committed_for_item += 1
                linkage_valid = (
                    target_relationship_valid
                    and _target_has_valid_acceptance_evidence(target)
                )
                if not linkage_valid:
                    add_reason("LEDGER_EVIDENCE_MISMATCH")
                else:
                    linked_for_item += 1
                    valid_linked_target_objects.add(id(target))
                if target.visible_at is None and linkage_valid:
                    add_reason("VISIBILITY_RECEIPT_MISSING")
                elif (
                    target.visible_at is not None
                    and target.accepted_at is not None
                    and _as_utc(target.visible_at, "target.visible_at")
                    < _as_utc(target.accepted_at, "target.accepted_at")
                ):
                    add_reason("LEDGER_EVIDENCE_MISMATCH")
            elif (
                target.accepted_at is not None
                or target.visible_at is not None
                or target.accepted_run_id is not None
                or target.accepted_prediction_id is not None
            ):
                add_reason("LEDGER_EVIDENCE_MISMATCH")
            target_rows.append(target)
        if (
            len(envelope.targets) != summary.target_count
            or linked_for_item != summary.accepted_target_count
        ):
            add_reason("LEDGER_EVIDENCE_MISMATCH")
        item_is_complete = (
            bool(envelope.targets)
            and linked_for_item == len(envelope.targets)
        )
        if (envelope.item.state == ITEM_SUCCESS) != item_is_complete:
            add_reason("LEDGER_EVIDENCE_MISMATCH")

    actual_item_count = len(snapshot.items)
    completed_items = [
        summary.item
        for summary in snapshot.items
        if summary.item.state == ITEM_SUCCESS
    ]
    late_items = [
        summary.item
        for summary in snapshot.items
        if summary.item.sla_status == ITEM_SLA_LATE
    ]
    failed_items = [
        summary.item
        for summary in snapshot.items
        if summary.item.state in _FAILED_ITEM_STATES
    ]
    if failed_items:
        add_reason("ITEM_FAILURE")
    if (
        snapshot.actual_item_count != actual_item_count
        or occurrence.expected_item_count != actual_item_count
    ):
        add_reason("LEDGER_CARDINALITY_MISMATCH")

    committed_targets = [
        target for target in target_rows if target.status == TARGET_ACCEPTED
    ]
    linked_targets = [
        target
        for target in committed_targets
        if id(target) in valid_linked_target_objects
    ]
    accepted_targets = [
        target
        for target in linked_targets
        if target.visible_at is not None
    ]
    missing_targets = sorted(
        (
            target
            for target in target_rows
            if (
                id(target) not in valid_linked_target_objects
                or target.visible_at is None
            )
        ),
        key=lambda target: target.registry_scheme_id,
    )
    if len(accepted_targets) < occurrence.expected_target_count:
        add_reason("DAILY_TARGETS_INCOMPLETE")
    late_targets = [
        target
        for target in accepted_targets
        if _as_utc(target.visible_at, "target.visible_at")
        > sla_deadline_at
    ]
    receipt_missing_targets = [
        target for target in linked_targets if target.visible_at is None
    ]
    if (
        snapshot.actual_target_count != len(target_rows)
        or occurrence.expected_target_count != len(target_rows)
        or snapshot.actual_accepted_target_count != len(linked_targets)
        or occurrence.accepted_target_count != len(linked_targets)
    ):
        add_reason("LEDGER_CARDINALITY_MISMATCH")
    accepted_by_deadline_count = sum(
        _as_utc(target.visible_at, "target.visible_at")
        <= sla_deadline_at
        for target in accepted_targets
    )
    if (
        occurrence.sla_outcome in {SLA_MET, SLA_BREACHED}
        and (
            occurrence.sla_accepted_target_count is None
            or occurrence.sla_accepted_target_count
            != accepted_by_deadline_count
        )
    ):
        add_reason("SLA_EVIDENCE_MISMATCH")

    native_generation = _select_generation(
        (
            envelope.calendar_generation
            for envelope in envelopes_by_id.values()
            if envelope.calendar_generation is not None
        ),
        expected_type="native_source",
        add_reason=add_reason,
    )
    databridge_generation = _select_generation(
        (
            envelope.generation
            for envelope in envelopes_by_id.values()
            if envelope.item.runtime_type == "blackbox_v2"
            and envelope.generation is not None
        ),
        expected_type="databridge_v1",
        add_reason=add_reason,
    )
    _validate_bound_generations(
        envelopes_by_id.values(),
        native_generation=native_generation,
        databridge_generation=databridge_generation,
        add_reason=add_reason,
    )
    databridge_readiness = _project_databridge_readiness(
        occurrence=occurrence,
        generation=databridge_generation,
        now=projected_now,
        add_reason=add_reason,
    )

    blackbox_v2 = _project_v2_items(envelopes_by_id.values())
    expected_v2_ids = {
        summary.item.base_scheme_id
        for summary in snapshot.items
        if summary.item.runtime_type == "blackbox_v2"
    }
    observed_v2_ids = {
        str(item["base_scheme_id"]) for item in blackbox_v2
    }
    if (
        databridge_generation is not None
        and databridge_generation.state == GENERATION_SEALED
        and (
            observed_v2_ids != expected_v2_ids
            or any(
                item["accepted_targets"] != item["expected_targets"]
                for item in blackbox_v2
            )
        )
    ):
        add_reason("DATABRIDGE_READY_V2_INCOMPLETE")

    heartbeat_projection = project_scheduler_heartbeat(
        heartbeat,
        expected_occurrence_id=occurrence.occurrence_id,
        now=projected_now,
        heartbeat_stale_after=heartbeat_stale_after,
    )
    if heartbeat_projection["status"] == "missing":
        add_reason("HEARTBEAT_MISSING")
    elif heartbeat_projection["status"] == "stale":
        add_reason("HEARTBEAT_STALE")
    elif heartbeat_projection["status"] == "error":
        add_reason("HEARTBEAT_ERROR")
    for heartbeat_reason in _heartbeat_operational_reasons(heartbeat):
        add_reason(heartbeat_reason)

    if occurrence.completion_state == "FAILED":
        add_reason("OCCURRENCE_FAILED")
    if occurrence.completion_state == "SUCCESS" and (
        len(completed_items) != occurrence.expected_item_count
        or len(linked_targets) != occurrence.expected_target_count
    ):
        add_reason("LEDGER_EVIDENCE_MISMATCH")
    if occurrence.sla_outcome == SLA_BREACHED:
        add_reason("SLA_BREACHED")
    elif occurrence.sla_outcome == SLA_MET and (
        missing_targets or late_targets or receipt_missing_targets
    ):
        add_reason("SLA_EVIDENCE_MISMATCH")
    elif (
        occurrence.sla_outcome == SLA_PENDING
        and deadline_reached
    ):
        add_reason(SLA_PENDING_AFTER_DEADLINE_REASON)
    if (
        deadline_reached
        and accepted_by_deadline_count < occurrence.expected_target_count
    ):
        add_reason("SLA_DEADLINE_INCOMPLETE")

    result: dict[str, object] = {
        "overall": "error" if reasons else "ok",
        "reasons": reasons,
        "checked_at": _isoformat(projected_now),
        "occurrence": {
            "occurrence_id": occurrence.occurrence_id,
            "schedule_key": occurrence.schedule_key,
            "predict_date": occurrence.predict_date,
            "completion_state": occurrence.completion_state,
            "policy_version": occurrence.policy_version,
            "registry_digest": occurrence.registry_digest,
            "sla_deadline_at": _isoformat(occurrence.sla_deadline_at),
            "recovery_cutoff_at": _isoformat(
                occurrence.recovery_cutoff_at
            ),
            "sla_outcome": occurrence.sla_outcome,
            "sla_evaluated_at": _optional_datetime(
                occurrence.sla_evaluated_at
            ),
            "sla_reason": occurrence.sla_reason,
            "failure_code": occurrence.failure_code,
            "started_at": _optional_datetime(occurrence.started_at),
            "completed_at": _optional_datetime(occurrence.completed_at),
        },
        "scheduler_heartbeat": heartbeat_projection,
        "items": {
            "expected": occurrence.expected_item_count,
            "actual": snapshot.actual_item_count,
            "completed": len(completed_items),
            "late": len(late_items),
            "failed": len(failed_items),
            "failed_base_scheme_ids": sorted(
                item.base_scheme_id for item in failed_items
            ),
            "details": _project_item_details(
                summaries_by_id,
                envelopes_by_id,
            ),
        },
        "targets": {
            "expected": occurrence.expected_target_count,
            "actual": snapshot.actual_target_count,
            "accepted": len(accepted_targets),
            "committed": len(committed_targets),
            "linked": len(linked_targets),
            "late": len(late_targets),
            "missing": max(
                occurrence.expected_target_count
                - len(accepted_targets),
                0,
            ),
            "missing_registry_ids": [
                target.registry_scheme_id for target in missing_targets
            ],
            "receipt_missing_registry_ids": [
                target.registry_scheme_id
                for target in sorted(
                    receipt_missing_targets,
                    key=lambda target: target.registry_scheme_id,
                )
            ],
            "availability_basis": "db_commit_visibility_receipt",
        },
        "generations": {
            "native_source": _project_generation(native_generation),
            "databridge_v1": _project_generation(databridge_generation),
            "databridge_readiness": databridge_readiness,
        },
        "blackbox_v2": blackbox_v2,
        "api_visibility": {
            "asserted": False,
            "proof_required": "uncached_external_api_probe",
            "probe_endpoint": "/api/daily-schedule/visibility",
            "canonical_endpoint": "/api/predictions",
            "cache_policy": "no-store",
        },
    }
    return result


def _index_snapshot_items(
    snapshot: ScheduleOccurrenceSnapshot,
    add_reason: Callable[[str], None],
) -> dict[int, ScheduleOccurrenceItemSummary]:
    indexed: dict[int, ScheduleOccurrenceItemSummary] = {}
    for summary in snapshot.items:
        item_id = summary.item.item_id
        if item_id in indexed:
            add_reason("LEDGER_EVIDENCE_MISMATCH")
        indexed[item_id] = summary
    return indexed


def _index_envelopes(
    envelopes: Sequence[_HealthEnvelope],
    *,
    occurrence_id: int,
    add_reason: Callable[[str], None],
) -> dict[int, _HealthEnvelope]:
    indexed: dict[int, _HealthEnvelope] = {}
    for envelope in envelopes:
        item_id = envelope.item.item_id
        if (
            envelope.occurrence.occurrence_id != occurrence_id
            or envelope.item.occurrence_id != occurrence_id
        ):
            add_reason("LEDGER_EVIDENCE_MISMATCH")
        if item_id in indexed:
            add_reason("LEDGER_EVIDENCE_MISMATCH")
        indexed[item_id] = envelope
    return indexed


def _select_generation(
    generations: Iterable[ScheduleInputGenerationEnvelope],
    *,
    expected_type: str,
    add_reason: Callable[[str], None],
) -> ScheduleInputGenerationEnvelope | None:
    selected: ScheduleInputGenerationEnvelope | None = None
    for generation in generations:
        if generation.generation_type != expected_type:
            add_reason("GENERATION_IDENTITY_MISMATCH")
        if selected is None:
            selected = generation
            continue
        if _generation_identity(generation) != _generation_identity(selected):
            add_reason("GENERATION_IDENTITY_MISMATCH")
    if selected is not None:
        if selected.state != GENERATION_SEALED:
            add_reason(
                "GENERATION_INVALIDATED"
                if selected.state == GENERATION_INVALIDATED
                else "GENERATION_NOT_SEALED"
            )
        if selected.sealed_at is None:
            add_reason("GENERATION_IDENTITY_MISMATCH")
    return selected


def _validate_bound_generations(
    envelopes: Iterable[_HealthEnvelope],
    *,
    native_generation: ScheduleInputGenerationEnvelope | None,
    databridge_generation: ScheduleInputGenerationEnvelope | None,
    add_reason: Callable[[str], None],
) -> None:
    for envelope in envelopes:
        generation_issue = getattr(envelope, "generation_issue", None)
        if generation_issue is not None:
            add_reason(
                "GENERATION_UNBOUND"
                if generation_issue == "UNBOUND_INPUT_GENERATION"
                else "GENERATION_IDENTITY_MISMATCH"
            )
        if envelope.generation is None:
            if generation_issue is None:
                add_reason("GENERATION_IDENTITY_MISMATCH")
            continue
        if envelope.generation.state == GENERATION_INVALIDATED:
            add_reason("GENERATION_INVALIDATED")
        elif envelope.generation.state != GENERATION_SEALED:
            add_reason("GENERATION_NOT_SEALED")
        if envelope.calendar_generation is None:
            add_reason("GENERATION_IDENTITY_MISMATCH")
            continue
        if envelope.calendar_generation.state == GENERATION_INVALIDATED:
            add_reason("GENERATION_INVALIDATED")
        elif envelope.calendar_generation.state != GENERATION_SEALED:
            add_reason("GENERATION_NOT_SEALED")
        if native_generation is None:
            add_reason("GENERATION_IDENTITY_MISMATCH")
            continue
        if (
            _generation_identity(envelope.calendar_generation)
            != _generation_identity(native_generation)
        ):
            add_reason("GENERATION_IDENTITY_MISMATCH")
        if envelope.item.runtime_type == "native_adapter":
            if (
                _generation_identity(envelope.generation)
                != _generation_identity(native_generation)
            ):
                add_reason("GENERATION_IDENTITY_MISMATCH")
        elif envelope.item.runtime_type == "blackbox_v2":
            if (
                databridge_generation is None
                or _generation_identity(envelope.generation)
                != _generation_identity(databridge_generation)
                or envelope.generation.native_generation_id
                != native_generation.generation_id
                or envelope.generation.native_manifest_sha256
                != native_generation.manifest_sha256
            ):
                add_reason("GENERATION_IDENTITY_MISMATCH")
        else:
            add_reason("LEDGER_EVIDENCE_MISMATCH")


def _target_has_valid_acceptance_evidence(target: object) -> bool:
    """判断 target 是否具备可计数的完整 acceptance 关系证据。"""
    accepted_at = getattr(target, "accepted_at", None)
    visible_at = getattr(target, "visible_at", None)
    if (
        getattr(target, "status", None) != TARGET_ACCEPTED
        or accepted_at is None
        or getattr(target, "accepted_run_id", None) is None
        or getattr(target, "accepted_prediction_id", None) is None
        or not bool(getattr(target, "accepted_linkage_valid", False))
    ):
        return False
    return (
        visible_at is None
        or _as_utc(visible_at, "target.visible_at")
        >= _as_utc(accepted_at, "target.accepted_at")
    )


def _target_matches_envelope(
    target: object,
    envelope: _HealthEnvelope,
) -> bool:
    return (
        getattr(target, "occurrence_id", None)
        == envelope.occurrence.occurrence_id
        and getattr(target, "item_id", None) == envelope.item.item_id
        and getattr(target, "base_scheme_id", None)
        == envelope.item.base_scheme_id
        and getattr(target, "runtime_type", None)
        == envelope.item.runtime_type
    )


def _project_v2_items(
    envelopes: Iterable[_HealthEnvelope],
) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    v2_envelopes = sorted(
        (
            envelope
            for envelope in envelopes
            if envelope.item.runtime_type == "blackbox_v2"
        ),
        key=lambda envelope: (
            envelope.item.release_offset_minutes,
            envelope.item.base_scheme_id,
        ),
    )
    for envelope in v2_envelopes:
        committed = [
            target
            for target in envelope.targets
            if target.status == TARGET_ACCEPTED
        ]
        linked = [
            target
            for target in committed
            if (
                _target_matches_envelope(target, envelope)
                and _target_has_valid_acceptance_evidence(target)
            )
        ]
        accepted = [
            target
            for target in linked
            if target.visible_at is not None
        ]
        missing_ids = sorted(
            target.registry_scheme_id
            for target in envelope.targets
            if (
                target not in accepted
            )
        )
        receipt_missing_ids = sorted(
            target.registry_scheme_id
            for target in linked
            if target.visible_at is None
        )
        accepted_at = None
        if len(accepted) == len(envelope.targets) and accepted:
            accepted_timestamps = [
                _as_utc(target.visible_at, "target.visible_at")
                for target in accepted
                if target.visible_at is not None
            ]
            if len(accepted_timestamps) == len(accepted):
                accepted_at = _isoformat(max(accepted_timestamps))
        projected.append(
            {
                "base_scheme_id": envelope.item.base_scheme_id,
                "release_offset_minutes": (
                    envelope.item.release_offset_minutes
                ),
                "release_at": _isoformat(envelope.item.release_at),
                "started_at": _optional_datetime(
                    envelope.item.started_at
                ),
                "accepted_at": accepted_at,
                "expected_targets": len(envelope.targets),
                "accepted_targets": len(accepted),
                "committed_targets": len(committed),
                "linked_targets": len(linked),
                "missing_registry_ids": missing_ids,
                "receipt_missing_registry_ids": receipt_missing_ids,
                "state": envelope.item.state,
                "sla_status": envelope.item.sla_status,
            }
        )
    return projected


def _project_item_details(
    summaries_by_id: Mapping[int, ScheduleOccurrenceItemSummary],
    envelopes_by_id: Mapping[int, _HealthEnvelope],
) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    for item_id in sorted(summaries_by_id):
        summary = summaries_by_id[item_id]
        envelope = envelopes_by_id.get(item_id)
        targets = () if envelope is None else envelope.targets
        committed = [
            target
            for target in targets
            if target.status == TARGET_ACCEPTED
        ]
        linked = [
            target
            for target in committed
            if (
                envelope is not None
                and _target_matches_envelope(target, envelope)
                and _target_has_valid_acceptance_evidence(target)
            )
        ]
        accepted = [
            target
            for target in linked
            if target.visible_at is not None
        ]
        generation = None if envelope is None else envelope.generation
        generation_issue = (
            "HEALTH_ENVELOPE_MISSING"
            if envelope is None
            else getattr(envelope, "generation_issue", None)
        )
        details.append(
            {
                "base_scheme_id": summary.item.base_scheme_id,
                "runtime_type": summary.item.runtime_type,
                "state": summary.item.state,
                "failure_code": summary.item.failure_code,
                "input_generation_id": summary.item.input_generation_id,
                "generation_state": (
                    generation.state
                    if generation is not None
                    else (
                        "UNBOUND"
                        if generation_issue == "UNBOUND_INPUT_GENERATION"
                        else "UNAVAILABLE"
                    )
                ),
                "generation_issue": generation_issue,
                "expected_targets": summary.target_count,
                "committed_targets": len(committed),
                "linked_targets": len(linked),
                "accepted_targets": len(accepted),
                "receipt_missing_registry_ids": sorted(
                    target.registry_scheme_id
                    for target in linked
                    if target.visible_at is None
                ),
            }
        )
    return details


def _project_generation(
    generation: ScheduleInputGenerationEnvelope | None,
) -> dict[str, object] | None:
    if generation is None:
        return None
    return {
        "generation_id": generation.generation_id,
        "generation_type": generation.generation_type,
        "business_date": generation.business_date,
        "feature_date": generation.feature_date,
        "manifest_sha256": generation.manifest_sha256,
        "state": generation.state,
        "sealed_at": _optional_datetime(generation.sealed_at),
        "ready": generation.state == GENERATION_SEALED,
    }


def _project_databridge_readiness(
    *,
    occurrence: object,
    generation: ScheduleInputGenerationEnvelope | None,
    now: datetime,
    add_reason: Callable[[str], None],
) -> dict[str, object]:
    policy_json = getattr(occurrence, "policy_json", None)
    try:
        if not isinstance(policy_json, Mapping):
            raise ValueError("policy_json is missing")
        timezone_name = policy_json.get("timezone")
        times = policy_json.get("times")
        if (
            not isinstance(timezone_name, str)
            or not isinstance(times, Mapping)
        ):
            raise ValueError("policy timezone/times are missing")
        raw_guardrail = times.get(
            "databridge_readiness_guardrail"
        )
        if not isinstance(raw_guardrail, str):
            raise ValueError("readiness guardrail is missing")
        guardrail_clock = datetime.strptime(
            raw_guardrail,
            "%H:%M",
        ).time()
        guardrail_at = datetime.combine(
            date.fromisoformat(str(occurrence.predict_date)),
            guardrail_clock,
            tzinfo=ZoneInfo(timezone_name),
        ).astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        add_reason("DATABRIDGE_READINESS_POLICY_INVALID")
        return {
            "status": "UNKNOWN",
            "reason": "DATABRIDGE_READINESS_POLICY_INVALID",
            "guardrail_at": None,
            "generation_id": (
                None if generation is None else generation.generation_id
            ),
            "sealed_at": (
                None
                if generation is None
                else _optional_datetime(generation.sealed_at)
            ),
        }
    if now < guardrail_at:
        status = "PENDING"
        reason = None
    elif (
        generation is None
        or generation.state != GENERATION_SEALED
        or generation.sealed_at is None
    ):
        status = "LATE"
        reason = "DATABRIDGE_NOT_SEALED_BY_GUARDRAIL"
    elif (
        _as_utc(
            generation.sealed_at,
            "databridge_generation.sealed_at",
        )
        > guardrail_at
    ):
        status = "LATE"
        reason = "DATABRIDGE_SEALED_AFTER_GUARDRAIL"
    else:
        status = "ON_TIME"
        reason = None
    if status == "LATE":
        add_reason("DATABRIDGE_READINESS_LATE")
    return {
        "status": status,
        "reason": reason,
        "guardrail_at": _isoformat(guardrail_at),
        "generation_id": (
            None if generation is None else generation.generation_id
        ),
        "sealed_at": (
            None
            if generation is None
            else _optional_datetime(generation.sealed_at)
        ),
    }


def project_scheduler_heartbeat(
    heartbeat: SchedulerHeartbeat | None,
    *,
    expected_occurrence_id: int | None,
    now: datetime,
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
) -> dict[str, object]:
    """投影不含内部错误文本的公共 scheduler 心跳。"""
    if heartbeat_stale_after <= timedelta(0):
        raise ValueError("heartbeat_stale_after must be positive")
    return _project_heartbeat(
        heartbeat,
        occurrence_id=expected_occurrence_id,
        now=_as_utc(now, "now"),
        stale_after=heartbeat_stale_after,
    )


def _project_heartbeat(
    heartbeat: SchedulerHeartbeat | None,
    *,
    occurrence_id: int | None,
    now: datetime,
    stale_after: timedelta,
) -> dict[str, object]:
    if heartbeat is None:
        return {
            "status": "missing",
            "service_name": None,
            "state": None,
            "occurrence_id": None,
            "heartbeat_at": None,
            "age_seconds": None,
            "details": {},
        }
    heartbeat_at = _as_utc(heartbeat.heartbeat_at, "heartbeat_at")
    age_seconds = max((now - heartbeat_at).total_seconds(), 0.0)
    state = heartbeat.state.upper()
    operational_reasons = _heartbeat_operational_reasons(heartbeat)
    if (
        state in _HEARTBEAT_ERROR_STATES
        or heartbeat.occurrence_id != occurrence_id
    ):
        status = "error"
    elif age_seconds > stale_after.total_seconds():
        status = "stale"
    elif operational_reasons:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "service_name": heartbeat.service_name,
        "state": heartbeat.state,
        "occurrence_id": heartbeat.occurrence_id,
        "heartbeat_at": _isoformat(heartbeat_at),
        "age_seconds": round(age_seconds, 3),
        "details": _public_heartbeat_details(heartbeat.details),
    }


def _public_heartbeat_details(
    details: Mapping[str, object],
) -> dict[str, object]:
    """仅保留公共 health 契约明确允许的运行字段。"""
    return {
        key: _json_safe(details[key])
        for key in sorted(_PUBLIC_HEARTBEAT_DETAIL_FIELDS)
        if key in details
    }


def _heartbeat_operational_reasons(
    heartbeat: SchedulerHeartbeat | None,
) -> tuple[str, ...]:
    """识别 watchdog 已经判定的业务进度风险。"""
    if (
        heartbeat is None
        or str(heartbeat.state).upper() != "WATCHDOG_PROGRESS"
    ):
        return ()
    details = heartbeat.details
    reasons: list[str] = []
    if details.get("eta_overline") is True:
        reasons.append("WATCHDOG_ETA_OVERLINE")
    accepted = details.get("accepted_target_count")
    expected = details.get("expected_target_count")
    if (
        isinstance(accepted, int)
        and not isinstance(accepted, bool)
        and isinstance(expected, int)
        and not isinstance(expected, bool)
        and accepted == 0
        and expected > 0
    ):
        reasons.append("WATCHDOG_NO_PROGRESS")
    return tuple(reasons)


def _generation_identity(
    generation: ScheduleInputGenerationEnvelope,
) -> tuple[object, ...]:
    return (
        generation.generation_id,
        generation.generation_type,
        generation.business_date,
        generation.feature_date,
        generation.manifest_sha256,
        generation.native_generation_id,
        generation.native_manifest_sha256,
        generation.state,
        generation.sealed_at,
    )


def _optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _isoformat(value)


def _isoformat(value: datetime) -> str:
    return _as_utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
