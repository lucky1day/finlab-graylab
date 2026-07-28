"""Mac Studio 日频 08:00 SLA 的离线容量验收门禁。

本模块只计算版本化观测证据，不读取数据库、不启动任务，也不会根据观测值
修改 scheduler policy。门禁使用两个不同结论：

* ``status``：切换门禁，要求一次真实 forced-cold 与一次绑定生产候选的
  execute-only observation 满足 25/29、07:55 和 85 分钟硬门槛；
* ``claims.approx_95_zero_failure_reliability``：约 95% 零失败可靠性声明资格。
  只有显式要求该声明时，不足 59 个有效日批才会使顶层门禁失败。

v2 观测契约不把 ``forced_cold``、``compare_passed`` 等裸布尔值作为证据。
每个样本必须绑定不可重复的 occurrence/run/artifact 身份和规范 SHA-256；
revision 是否真实由 before/after generation 身份与内容摘要共同推导。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


OBSERVATION_SCHEMA_VERSION = "daily-capacity-observations-v2"
SUPPORTED_POLICY_VERSION = "daily-scheduler-policy-v2"
EXPECTED_TARGET_COUNT = 29
EXPECTED_V2_SCHEME_IDS = frozenset(
    {
        "one_y_t5_liq_excess_a_v1",
        "one_y_t5_liq_excess_a_w252_l7_v1",
        "one_y_t5_liq_excess_a_w350_l7_v1",
        "one_y_t5_liq_excess_b_w252_l7_v1",
        "ten_y_t5_maj3_k3_ic_static_v1",
        "ten_y_t5_maj4_k3_ic_static_v1",
        "ten_y_t5_maj4_k3_ic_yearly_v1",
        "ten_y_t5_say_k5_sharpe_static_v1",
    }
)
_FORMAL_V2_SCHEME_IDS = frozenset(
    {
        "one_y_t5_liq_excess_a_v1",
        "one_y_t5_liq_excess_a_w252_l7_v1",
        "one_y_t5_liq_excess_a_w350_l7_v1",
        "one_y_t5_liq_excess_b_w252_l7_v1",
    }
)
_GRAY_V2_SCHEME_IDS = EXPECTED_V2_SCHEME_IDS - _FORMAL_V2_SCHEME_IDS
MIN_FORCED_COLD_SAMPLES = 1
MIN_REVISION_SAMPLES = 0
MIN_PRODUCTION_OBSERVATIONS = 1
MIN_RELIABILITY_SAMPLES = 59
MAX_FORCED_COLD_MINUTES = 85.0

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATABRIDGE_DEADLINE = time(6, 55)
_FORMAL_V2_DEADLINE = time(7, 10)
_GRAY_V2_DEADLINE = time(7, 20)
_TARGET_DEADLINE = time(7, 55)
_INCIDENT_FIELDS = frozenset(
    {
        "misfire",
        "timeout",
        "partial",
        "duplicate_prediction",
        "orphan_process",
        "reentry_new_results",
        "manual_intervention",
    }
)


class CapacityObservationError(ValueError):
    """容量观测文件格式无效，无法形成可信验收结论。"""


@dataclass(frozen=True)
class _VisibleResult:
    identity: str
    db_visible_at: datetime
    api_visible_at: datetime
    input_generation_id: str | None = None


@dataclass(frozen=True)
class _ArtifactEvidence:
    artifact_id: str
    sha256: str


@dataclass(frozen=True)
class _GenerationEvidence:
    generation_id: str
    sha256: str


@dataclass(frozen=True)
class _CompareGateEvidence:
    run_id: str
    status: str
    evidence: _ArtifactEvidence


@dataclass(frozen=True)
class _Batch:
    batch_id: str
    observation_kind: str
    occurrence_id: str
    run_id: str
    execution_evidence: _ArtifactEvidence
    cold_start_evidence: _ArtifactEvidence | None
    business_date: date
    previous_trading_date: date | None
    machine_id: str
    policy_version: str
    databridge_generation_id: str
    databridge_published_at: datetime
    v2_results: tuple[_VisibleResult, ...]
    targets: tuple[_VisibleResult, ...]
    incidents: Mapping[str, int]
    forced_cold_end_to_end_minutes: float | None


@dataclass(frozen=True)
class _RevisionTrial:
    trial_id: str
    run_id: str
    business_date: date
    machine_id: str
    policy_version: str
    before_generation: _GenerationEvidence
    after_generation: _GenerationEvidence
    compare_gate: _CompareGateEvidence


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """返回不插值的 nearest-rank 百分位。"""

    samples = [float(value) for value in values]
    if not samples:
        raise ValueError("nearest_rank requires at least one sample")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    if any(not math.isfinite(value) for value in samples):
        raise ValueError("nearest_rank samples must be finite")
    ordered = sorted(samples)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def evaluate_capacity_observations(
    payload: Mapping[str, object],
    *,
    expected_machine_id: str | None = None,
    expected_policy_version: str | None = None,
    require_approx_95_reliability: bool = False,
) -> dict[str, object]:
    """校验离线证据并返回 JSON-safe 的容量门禁结果。"""

    root = _require_mapping(payload, "root")
    schema_version = _required_text(root, "schema_version", "root")
    if schema_version != OBSERVATION_SCHEMA_VERSION:
        raise CapacityObservationError(
            "root.schema_version must be "
            f"{OBSERVATION_SCHEMA_VERSION}, got {schema_version}"
        )
    machine_id = _required_text(root, "machine_id", "root")
    policy_version = _required_text(root, "policy_version", "root")
    batches = _parse_batches(_required_list(root, "daily_batches", "root"))
    revisions = _parse_revisions(
        _required_list(root, "revision_trials", "root")
    )
    _validate_unique_ids(batches, revisions)

    violations: list[dict[str, object]] = []
    if expected_machine_id is not None and machine_id != expected_machine_id:
        _add_violation(
            violations,
            "MACHINE_ID_MISMATCH",
            "evidence is not bound to the expected Mac Studio",
            expected=expected_machine_id,
            actual=machine_id,
        )
    if policy_version != SUPPORTED_POLICY_VERSION:
        _add_violation(
            violations,
            "UNSUPPORTED_POLICY_VERSION",
            "observation schema v2 only admits the versioned scheduler "
            "policy v2; runtime overrides are forbidden",
            expected=SUPPORTED_POLICY_VERSION,
            actual=policy_version,
        )
    fixed_policy = expected_policy_version or SUPPORTED_POLICY_VERSION
    if policy_version != fixed_policy:
        _add_violation(
            violations,
            "POLICY_VERSION_MISMATCH",
            "evidence policy version differs from the fixed admission policy",
            expected=fixed_policy,
            actual=policy_version,
        )
    root_binding_valid = (
        (
            expected_machine_id is None
            or machine_id == expected_machine_id
        )
        and policy_version == SUPPORTED_POLICY_VERSION
        and policy_version == fixed_policy
    )

    for batch in batches:
        _check_binding(
            violations,
            observation_id=batch.batch_id,
            observation_machine_id=batch.machine_id,
            observation_policy_version=batch.policy_version,
            machine_id=machine_id,
            policy_version=policy_version,
        )
        _check_batch_deadlines(violations, batch)
    for trial in revisions:
        _check_binding(
            violations,
            observation_id=trial.trial_id,
            observation_machine_id=trial.machine_id,
            observation_policy_version=trial.policy_version,
            machine_id=machine_id,
            policy_version=policy_version,
        )

    duplicate_daily_batch_ids = _check_daily_evidence_uniqueness(
        violations,
        batches,
    )
    _check_target_set_stability(violations, batches)

    forced_cold_raw = tuple(
        batch for batch in batches if batch.observation_kind == "forced_cold"
    )
    forced_cold = _qualified_forced_cold_samples(
        violations,
        forced_cold_raw,
        disqualified_batch_ids=duplicate_daily_batch_ids,
    )
    forced_minutes = tuple(
        batch.forced_cold_end_to_end_minutes
        for batch in forced_cold
        if batch.forced_cold_end_to_end_minutes is not None
    )
    # MVP 的一个 forced-cold 样本只证明硬上界，不构成 P95 统计声明。
    forced_p95 = None
    forced_max = max(forced_minutes) if forced_minutes else None
    if len(forced_cold) < MIN_FORCED_COLD_SAMPLES:
        _add_violation(
            violations,
            "FORCED_COLD_SAMPLE_COUNT",
            "forced-cold capacity evidence is below the required sample count",
            expected=MIN_FORCED_COLD_SAMPLES,
            actual=len(forced_cold),
        )
    forced_cold_incidents = [
        batch.batch_id
        for batch in forced_cold
        if any(
            batch.incidents[field] != 0
            for field in _INCIDENT_FIELDS
        )
    ]
    if forced_cold_incidents:
        _add_violation(
            violations,
            "FORCED_COLD_OBSERVATION_NOT_CLEAN",
            "forced-cold observations must have zero incidents, duplicates, "
            "orphans, partials, or reentry results",
            batch_ids=forced_cold_incidents,
        )
    if forced_max is not None and forced_max > MAX_FORCED_COLD_MINUTES:
        _add_violation(
            violations,
            "FORCED_COLD_MAX_EXCEEDED",
            "forced-cold maximum exceeds 85 minutes",
            expected_max=MAX_FORCED_COLD_MINUTES,
            actual=forced_max,
        )

    real_revisions = _qualified_revision_samples(
        violations,
        revisions,
    )
    revision_compare_failures = tuple(
        trial
        for trial in real_revisions
        if trial.compare_gate.status != "PASS"
    )
    if len(real_revisions) < MIN_REVISION_SAMPLES:
        _add_violation(
            violations,
            "REVISION_SAMPLE_COUNT",
            "real T-1 revision evidence is below the required sample count",
            expected=MIN_REVISION_SAMPLES,
            actual=len(real_revisions),
        )
    if revision_compare_failures:
        _add_violation(
            violations,
            "REVISION_COMPARE_FAILED",
            "one or more real T-1 revision trials failed CompareGate",
            failed_trial_ids=[
                trial.trial_id for trial in revision_compare_failures
            ],
        )

    production = tuple(
        sorted(
            (
                batch
                for batch in batches
                if batch.observation_kind == "production"
            ),
            key=lambda batch: (batch.business_date, batch.batch_id),
        )
    )
    _check_production_date_uniqueness(production)
    trailing_clean = _trailing_clean_count(
        production,
        machine_id=machine_id,
        policy_version=policy_version,
        disqualified_batch_ids=duplicate_daily_batch_ids,
    )
    _check_production_observations(
        violations,
        production,
        machine_id=machine_id,
        policy_version=policy_version,
        disqualified_batch_ids=duplicate_daily_batch_ids,
    )

    effective_production = tuple(
        batch
        for batch in production
        if batch.machine_id == machine_id
        and batch.policy_version == policy_version
    )
    reliability_failed = tuple(
        batch
        for batch in effective_production
        if not _batch_is_clean(
            batch,
            machine_id=machine_id,
            policy_version=policy_version,
            disqualified_batch_ids=duplicate_daily_batch_ids,
        )
    )
    reliability_eligible = (
        root_binding_valid
        and len(effective_production) >= MIN_RELIABILITY_SAMPLES
        and not reliability_failed
        and len(effective_production) == len(production)
    )
    if require_approx_95_reliability:
        if not root_binding_valid:
            _add_violation(
                violations,
                "RELIABILITY_BINDING_MISMATCH",
                "reliability evidence is not bound to the admitted Mac and "
                "fixed policy version",
            )
        if len(effective_production) < MIN_RELIABILITY_SAMPLES:
            _add_violation(
                violations,
                "RELIABILITY_SAMPLE_COUNT",
                "approximately 95% zero-failure reliability claim requires "
                "at least 59 effective daily batches",
                expected=MIN_RELIABILITY_SAMPLES,
                actual=len(effective_production),
            )
        if reliability_failed or len(effective_production) != len(production):
            _add_violation(
                violations,
                "RELIABILITY_FAILURES_PRESENT",
                "zero-failure reliability claim contains failed or "
                "wrong-binding effective batches",
                failed_batch_ids=[
                    batch.batch_id for batch in reliability_failed
                ],
                excluded_binding_count=(
                    len(production) - len(effective_production)
                ),
            )

    metrics: dict[str, object] = {
        "daily_batch_count": len(batches),
        "databridge_deadline": "06:55",
        "v2_deadline": "07:10 formal / 07:20 gray",
        "target_deadline": "07:55",
        "expected_v2_count": len(EXPECTED_V2_SCHEME_IDS),
        "expected_target_count": EXPECTED_TARGET_COUNT,
        "forced_cold_raw_count": len(forced_cold_raw),
        "forced_cold_count": len(forced_cold),
        "forced_cold_p95_minutes": forced_p95,
        "forced_cold_p95_claim": "NOT_EVALUATED",
        "forced_cold_max_minutes": forced_max,
        "revision_raw_count": len(revisions),
        "real_revision_count": len(real_revisions),
        "revision_compare_pass_count": (
            len(real_revisions) - len(revision_compare_failures)
        ),
        "production_batch_count": len(production),
        "trailing_clean_trading_days": trailing_clean,
        "effective_reliability_batch_count": len(effective_production),
        "failed_reliability_batch_count": len(reliability_failed),
    }
    claim: dict[str, object] = {
        "requested": bool(require_approx_95_reliability),
        "eligible": reliability_eligible,
        "minimum_effective_batches": MIN_RELIABILITY_SAMPLES,
        "effective_batches": len(effective_production),
        "failed_batches": len(reliability_failed),
    }
    return {
        "status": "PASS" if not violations else "FAIL",
        "schema_version": schema_version,
        "binding": {
            "machine_id": machine_id,
            "policy_version": policy_version,
        },
        "metrics": metrics,
        "claims": {
            "approx_95_zero_failure_reliability": claim,
        },
        "violations": violations,
    }


def _parse_batches(rows: Sequence[object]) -> tuple[_Batch, ...]:
    parsed: list[_Batch] = []
    for index, raw in enumerate(rows):
        path = f"root.daily_batches[{index}]"
        row = _require_mapping(raw, path)
        kind = _required_text(row, "observation_kind", path)
        if kind not in {"forced_cold", "production"}:
            raise CapacityObservationError(
                f"{path}.observation_kind must be forced_cold or production"
            )
        if (
            kind == "production"
            and row.get("capacity_qualification") != "PRODUCTION_BOUND"
        ):
            raise CapacityObservationError(
                f"{path}.capacity_qualification must be PRODUCTION_BOUND"
            )
        forced_minutes_raw = row.get("forced_cold_end_to_end_minutes")
        if kind == "forced_cold":
            forced_minutes = _positive_finite_number(
                forced_minutes_raw,
                f"{path}.forced_cold_end_to_end_minutes",
            )
        elif forced_minutes_raw is not None:
            raise CapacityObservationError(
                f"{path}.forced_cold_end_to_end_minutes is only valid for "
                "forced_cold observations"
            )
        else:
            forced_minutes = None
        execution_evidence = _parse_artifact_evidence(
            row.get("execution_evidence"),
            f"{path}.execution_evidence",
        )
        cold_start_raw = row.get("cold_start_evidence")
        if kind == "forced_cold":
            cold_start_evidence = _parse_artifact_evidence(
                cold_start_raw,
                f"{path}.cold_start_evidence",
            )
        elif cold_start_raw is not None:
            raise CapacityObservationError(
                f"{path}.cold_start_evidence is only valid for forced_cold "
                "observations"
            )
        else:
            cold_start_evidence = None
        business_date = _parse_date(
            row.get("business_date"),
            f"{path}.business_date",
        )
        previous_raw = row.get("previous_trading_date")
        previous = (
            None
            if previous_raw is None
            else _parse_date(previous_raw, f"{path}.previous_trading_date")
        )
        parsed.append(
            _Batch(
                batch_id=_required_text(row, "batch_id", path),
                observation_kind=kind,
                occurrence_id=_required_text(row, "occurrence_id", path),
                run_id=_required_text(row, "run_id", path),
                execution_evidence=execution_evidence,
                cold_start_evidence=cold_start_evidence,
                business_date=business_date,
                previous_trading_date=previous,
                machine_id=_required_text(row, "machine_id", path),
                policy_version=_required_text(row, "policy_version", path),
                databridge_generation_id=_required_text(
                    row,
                    "databridge_generation_id",
                    path,
                ),
                databridge_published_at=_parse_timestamp(
                    row.get("databridge_published_at"),
                    f"{path}.databridge_published_at",
                ),
                v2_results=_parse_visible_results(
                    _required_list(row, "v2_results", path),
                    path=f"{path}.v2_results",
                    identity_field="scheme_id",
                    include_generation=True,
                ),
                targets=_parse_visible_results(
                    _required_list(row, "targets", path),
                    path=f"{path}.targets",
                    identity_field="target_id",
                    include_generation=False,
                ),
                incidents=_parse_incidents(row.get("incidents"), path),
                forced_cold_end_to_end_minutes=forced_minutes,
            )
        )
    return tuple(parsed)


def _parse_visible_results(
    rows: Sequence[object],
    *,
    path: str,
    identity_field: str,
    include_generation: bool,
) -> tuple[_VisibleResult, ...]:
    parsed: list[_VisibleResult] = []
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = _require_mapping(raw, item_path)
        parsed.append(
            _VisibleResult(
                identity=_required_text(row, identity_field, item_path),
                input_generation_id=(
                    _required_text(
                        row,
                        "input_generation_id",
                        item_path,
                    )
                    if include_generation
                    else None
                ),
                db_visible_at=_parse_timestamp(
                    row.get("db_visible_at"),
                    f"{item_path}.db_visible_at",
                ),
                api_visible_at=_parse_timestamp(
                    row.get("api_visible_at"),
                    f"{item_path}.api_visible_at",
                ),
            )
        )
    return tuple(parsed)


def _parse_incidents(
    raw: object,
    batch_path: str,
) -> Mapping[str, int]:
    path = f"{batch_path}.incidents"
    incidents = _require_mapping(raw, path)
    actual_fields = frozenset(incidents)
    if actual_fields != _INCIDENT_FIELDS:
        raise CapacityObservationError(
            f"{path} fields must be exactly {sorted(_INCIDENT_FIELDS)}, "
            f"got {sorted(str(field) for field in actual_fields)}"
        )
    result: dict[str, int] = {}
    for field in sorted(_INCIDENT_FIELDS):
        value = incidents[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CapacityObservationError(
                f"{path}.{field} must be a non-negative integer"
            )
        result[field] = value
    return result


def _parse_revisions(rows: Sequence[object]) -> tuple[_RevisionTrial, ...]:
    parsed: list[_RevisionTrial] = []
    for index, raw in enumerate(rows):
        path = f"root.revision_trials[{index}]"
        row = _require_mapping(raw, path)
        parsed.append(
            _RevisionTrial(
                trial_id=_required_text(row, "trial_id", path),
                run_id=_required_text(row, "run_id", path),
                business_date=_parse_date(
                    row.get("business_date"),
                    f"{path}.business_date",
                ),
                machine_id=_required_text(row, "machine_id", path),
                policy_version=_required_text(row, "policy_version", path),
                before_generation=_parse_generation_evidence(
                    row.get("before_generation"),
                    f"{path}.before_generation",
                ),
                after_generation=_parse_generation_evidence(
                    row.get("after_generation"),
                    f"{path}.after_generation",
                ),
                compare_gate=_parse_compare_gate_evidence(
                    row.get("compare_gate"),
                    f"{path}.compare_gate",
                ),
            )
        )
    return tuple(parsed)


def _parse_artifact_evidence(
    raw: object,
    path: str,
) -> _ArtifactEvidence:
    row = _require_mapping(raw, path)
    return _ArtifactEvidence(
        artifact_id=_required_text(row, "artifact_id", path),
        sha256=_required_sha256(row, "sha256", path),
    )


def _parse_generation_evidence(
    raw: object,
    path: str,
) -> _GenerationEvidence:
    row = _require_mapping(raw, path)
    return _GenerationEvidence(
        generation_id=_required_text(row, "generation_id", path),
        sha256=_required_sha256(row, "sha256", path),
    )


def _parse_compare_gate_evidence(
    raw: object,
    path: str,
) -> _CompareGateEvidence:
    row = _require_mapping(raw, path)
    status = _required_text(row, "status", path)
    if status not in {"PASS", "FAIL"}:
        raise CapacityObservationError(
            f"{path}.status must be PASS or FAIL"
        )
    return _CompareGateEvidence(
        run_id=_required_text(row, "run_id", path),
        status=status,
        evidence=_parse_artifact_evidence(
            row.get("evidence"),
            f"{path}.evidence",
        ),
    )


def _validate_unique_ids(
    batches: Sequence[_Batch],
    revisions: Sequence[_RevisionTrial],
) -> None:
    _reject_duplicates(
        [batch.batch_id for batch in batches],
        "root.daily_batches batch_id",
    )
    _reject_duplicates(
        [trial.trial_id for trial in revisions],
        "root.revision_trials trial_id",
    )


def _check_daily_evidence_uniqueness(
    violations: list[dict[str, object]],
    batches: Sequence[_Batch],
) -> frozenset[str]:
    seen: dict[str, set[str]] = {
        "occurrence_id": set(),
        "run_id": set(),
        "execution_artifact_id": set(),
        "execution_sha256": set(),
    }
    duplicates: set[str] = set()
    for batch in batches:
        values = {
            "occurrence_id": batch.occurrence_id,
            "run_id": batch.run_id,
            "execution_artifact_id": batch.execution_evidence.artifact_id,
            "execution_sha256": batch.execution_evidence.sha256,
        }
        reused = sorted(
            field for field, value in values.items() if value in seen[field]
        )
        for field, value in values.items():
            seen[field].add(value)
        if reused:
            duplicates.add(batch.batch_id)
            _add_violation(
                violations,
                "DUPLICATE_DAILY_EVIDENCE",
                "daily capacity evidence reuses an occurrence, run, artifact, "
                "or evidence digest",
                batch_id=batch.batch_id,
                duplicate_fields=reused,
            )
    return frozenset(duplicates)


def _qualified_forced_cold_samples(
    violations: list[dict[str, object]],
    samples: Sequence[_Batch],
    *,
    disqualified_batch_ids: frozenset[str],
) -> tuple[_Batch, ...]:
    seen_artifact_ids: set[str] = set()
    seen_digests: set[str] = set()
    qualified: list[_Batch] = []
    for batch in samples:
        evidence = batch.cold_start_evidence
        if evidence is None:
            # Parser already prevents this path; retain fail-closed behavior.
            continue
        duplicate_fields: list[str] = []
        if evidence.artifact_id in seen_artifact_ids:
            duplicate_fields.append("cold_start_artifact_id")
        if evidence.sha256 in seen_digests:
            duplicate_fields.append("cold_start_sha256")
        seen_artifact_ids.add(evidence.artifact_id)
        seen_digests.add(evidence.sha256)
        if duplicate_fields:
            _add_violation(
                violations,
                "DUPLICATE_FORCED_COLD_EVIDENCE",
                "forced-cold sample reuses cold-start proof",
                batch_id=batch.batch_id,
                duplicate_fields=duplicate_fields,
            )
        if (
            batch.batch_id not in disqualified_batch_ids
            and not duplicate_fields
        ):
            qualified.append(batch)
    return tuple(qualified)


def _qualified_revision_samples(
    violations: list[dict[str, object]],
    revisions: Sequence[_RevisionTrial],
) -> tuple[_RevisionTrial, ...]:
    seen: dict[str, set[str]] = {
        "run_id": set(),
        "after_generation_id": set(),
        "after_generation_sha256": set(),
        "compare_gate_run_id": set(),
        "compare_artifact_id": set(),
        "compare_evidence_sha256": set(),
    }
    qualified: list[_RevisionTrial] = []
    for trial in revisions:
        generation_changed = (
            trial.before_generation.generation_id
            != trial.after_generation.generation_id
            and trial.before_generation.sha256
            != trial.after_generation.sha256
        )
        if not generation_changed:
            _add_violation(
                violations,
                "REVISION_NO_CONTENT_CHANGE",
                "revision evidence must bind distinct before/after generation "
                "identities and content digests",
                trial_id=trial.trial_id,
            )
        values = {
            "run_id": trial.run_id,
            "after_generation_id": trial.after_generation.generation_id,
            "after_generation_sha256": trial.after_generation.sha256,
            "compare_gate_run_id": trial.compare_gate.run_id,
            "compare_artifact_id": (
                trial.compare_gate.evidence.artifact_id
            ),
            "compare_evidence_sha256": trial.compare_gate.evidence.sha256,
        }
        duplicate_fields = sorted(
            field for field, value in values.items() if value in seen[field]
        )
        for field, value in values.items():
            seen[field].add(value)
        if duplicate_fields:
            _add_violation(
                violations,
                "DUPLICATE_REVISION_EVIDENCE",
                "revision sample reuses a run, after-generation, or "
                "CompareGate artifact/digest",
                trial_id=trial.trial_id,
                duplicate_fields=duplicate_fields,
            )
        if generation_changed and not duplicate_fields:
            qualified.append(trial)
    return tuple(qualified)


def _check_binding(
    violations: list[dict[str, object]],
    *,
    observation_id: str,
    observation_machine_id: str,
    observation_policy_version: str,
    machine_id: str,
    policy_version: str,
) -> None:
    if observation_machine_id != machine_id:
        _add_violation(
            violations,
            "OBSERVATION_MACHINE_DRIFT",
            "observation was captured on a different machine",
            observation_id=observation_id,
            expected=machine_id,
            actual=observation_machine_id,
        )
    if observation_policy_version != policy_version:
        _add_violation(
            violations,
            "OBSERVATION_POLICY_DRIFT",
            "observation used a different scheduler policy version",
            observation_id=observation_id,
            expected=policy_version,
            actual=observation_policy_version,
        )


def _check_batch_deadlines(
    violations: list[dict[str, object]],
    batch: _Batch,
) -> None:
    databridge_deadline = _local_deadline(
        batch.business_date,
        _DATABRIDGE_DEADLINE,
    )
    if _timestamp_date(batch.databridge_published_at) != batch.business_date:
        _add_violation(
            violations,
            "DATABRIDGE_TIMESTAMP_DATE_MISMATCH",
            "DataBridge publish timestamp is not on the batch business date",
            batch_id=batch.batch_id,
        )
    elif batch.databridge_published_at > databridge_deadline:
        _add_violation(
            violations,
            "DATABRIDGE_DEADLINE_MISSED",
            "DataBridge generation was published after 06:55",
            batch_id=batch.batch_id,
            actual=_isoformat(batch.databridge_published_at),
        )

    v2_ids = [result.identity for result in batch.v2_results]
    if (
        len(v2_ids) != len(EXPECTED_V2_SCHEME_IDS)
        or set(v2_ids) != EXPECTED_V2_SCHEME_IDS
        or len(v2_ids) != len(set(v2_ids))
    ):
        _add_violation(
            violations,
            "V2_RESULT_SET_MISMATCH",
            "batch must contain exactly the fixed eight Blackbox V2 schemes",
            batch_id=batch.batch_id,
            expected=sorted(EXPECTED_V2_SCHEME_IDS),
            actual=sorted(set(v2_ids)),
            actual_count=len(v2_ids),
        )
    for result in batch.v2_results:
        if result.input_generation_id != batch.databridge_generation_id:
            _add_violation(
                violations,
                "V2_GENERATION_MISMATCH",
                "V2 result is not bound to the batch DataBridge generation",
                batch_id=batch.batch_id,
                scheme_id=result.identity,
                expected=batch.databridge_generation_id,
                actual=result.input_generation_id,
            )
        if not _both_on_business_date(result, batch.business_date):
            _add_violation(
                violations,
                "V2_TIMESTAMP_DATE_MISMATCH",
                "V2 DB/API visibility timestamp is not on the business date",
                batch_id=batch.batch_id,
                scheme_id=result.identity,
            )
        scheme_deadline = _v2_deadline(result.identity)
        if scheme_deadline is None:
            continue
        deadline_at = _local_deadline(
            batch.business_date,
            scheme_deadline,
        )
        if max(result.db_visible_at, result.api_visible_at) > deadline_at:
            _add_violation(
                violations,
                "V2_DEADLINE_MISSED",
                "V2 result missed its exact scheme deadline",
                batch_id=batch.batch_id,
                scheme_id=result.identity,
                expected_deadline=scheme_deadline.strftime("%H:%M"),
                actual=_isoformat(
                    max(result.db_visible_at, result.api_visible_at)
                ),
            )

    target_ids = [result.identity for result in batch.targets]
    if (
        len(target_ids) != EXPECTED_TARGET_COUNT
        or len(target_ids) != len(set(target_ids))
    ):
        _add_violation(
            violations,
            "TARGET_COUNT_MISMATCH",
            "batch must contain exactly 29 unique accepted targets",
            batch_id=batch.batch_id,
            expected=EXPECTED_TARGET_COUNT,
            actual_count=len(target_ids),
            unique_count=len(set(target_ids)),
        )
    target_deadline = _local_deadline(batch.business_date, _TARGET_DEADLINE)
    for result in batch.targets:
        if not _both_on_business_date(result, batch.business_date):
            _add_violation(
                violations,
                "TARGET_TIMESTAMP_DATE_MISMATCH",
                "target DB/API visibility timestamp is not on the business date",
                batch_id=batch.batch_id,
                target_id=result.identity,
            )
        elif max(result.db_visible_at, result.api_visible_at) > target_deadline:
            _add_violation(
                violations,
                "TARGET_DEADLINE_MISSED",
                "target was not visible in both DB and API by 07:55",
                batch_id=batch.batch_id,
                target_id=result.identity,
                actual=_isoformat(
                    max(result.db_visible_at, result.api_visible_at)
                ),
            )


def _check_target_set_stability(
    violations: list[dict[str, object]],
    batches: Sequence[_Batch],
) -> None:
    if not batches:
        return
    expected = frozenset(result.identity for result in batches[0].targets)
    for batch in batches[1:]:
        actual = frozenset(result.identity for result in batch.targets)
        if actual != expected:
            _add_violation(
                violations,
                "TARGET_SET_DRIFT",
                "target identity set changed inside one fixed-policy evidence set",
                batch_id=batch.batch_id,
                missing=sorted(expected - actual),
                unexpected=sorted(actual - expected),
            )


def _check_production_date_uniqueness(
    production: Sequence[_Batch],
) -> None:
    dates = [batch.business_date.isoformat() for batch in production]
    _reject_duplicates(dates, "production business_date")


def _check_production_observations(
    violations: list[dict[str, object]],
    production: Sequence[_Batch],
    *,
    machine_id: str,
    policy_version: str,
    disqualified_batch_ids: frozenset[str],
) -> None:
    if len(production) < MIN_PRODUCTION_OBSERVATIONS:
        _add_violation(
            violations,
            "PRODUCTION_OBSERVATION_COUNT",
            "no production-bound isolated observation was provided",
            expected=MIN_PRODUCTION_OBSERVATIONS,
            actual=len(production),
        )
        return
    dirty = [
        batch.batch_id
        for batch in production
        if not _batch_is_clean(
            batch,
            machine_id=machine_id,
            policy_version=policy_version,
            disqualified_batch_ids=disqualified_batch_ids,
        )
    ]
    if dirty:
        _add_violation(
            violations,
            "PRODUCTION_OBSERVATION_NOT_CLEAN",
            "production-bound observations must all be 29/29 with zero "
            "incident, duplicate, orphan, partial, or reentry result",
            batch_ids=dirty,
        )


def _trailing_clean_count(
    production: Sequence[_Batch],
    *,
    machine_id: str,
    policy_version: str,
    disqualified_batch_ids: frozenset[str],
) -> int:
    count = 0
    later: _Batch | None = None
    for batch in reversed(production):
        if later is not None and later.previous_trading_date != batch.business_date:
            break
        if not _batch_is_clean(
            batch,
            machine_id=machine_id,
            policy_version=policy_version,
            disqualified_batch_ids=disqualified_batch_ids,
        ):
            break
        count += 1
        later = batch
    return count


def _batch_is_clean(
    batch: _Batch,
    *,
    machine_id: str,
    policy_version: str,
    disqualified_batch_ids: frozenset[str],
) -> bool:
    if batch.batch_id in disqualified_batch_ids:
        return False
    if batch.machine_id != machine_id or batch.policy_version != policy_version:
        return False
    if any(batch.incidents[field] != 0 for field in _INCIDENT_FIELDS):
        return False
    if _timestamp_date(batch.databridge_published_at) != batch.business_date:
        return False
    if batch.databridge_published_at > _local_deadline(
        batch.business_date,
        _DATABRIDGE_DEADLINE,
    ):
        return False
    v2_ids = [result.identity for result in batch.v2_results]
    if (
        len(v2_ids) != len(EXPECTED_V2_SCHEME_IDS)
        or set(v2_ids) != EXPECTED_V2_SCHEME_IDS
        or len(v2_ids) != len(set(v2_ids))
    ):
        return False
    for result in batch.v2_results:
        if result.input_generation_id != batch.databridge_generation_id:
            return False
        scheme_deadline = _v2_deadline(result.identity)
        if scheme_deadline is None:
            return False
        if not _visible_by(
            result,
            business_date=batch.business_date,
            deadline=scheme_deadline,
        ):
            return False
    target_ids = [result.identity for result in batch.targets]
    if (
        len(target_ids) != EXPECTED_TARGET_COUNT
        or len(target_ids) != len(set(target_ids))
    ):
        return False
    return all(
        _visible_by(
            result,
            business_date=batch.business_date,
            deadline=_TARGET_DEADLINE,
        )
        for result in batch.targets
    )


def _visible_by(
    result: _VisibleResult,
    *,
    business_date: date,
    deadline: time,
) -> bool:
    return (
        _both_on_business_date(result, business_date)
        and max(result.db_visible_at, result.api_visible_at)
        <= _local_deadline(business_date, deadline)
    )


def _v2_deadline(scheme_id: str) -> time | None:
    if scheme_id in _FORMAL_V2_SCHEME_IDS:
        return _FORMAL_V2_DEADLINE
    if scheme_id in _GRAY_V2_SCHEME_IDS:
        return _GRAY_V2_DEADLINE
    return None


def _both_on_business_date(
    result: _VisibleResult,
    business_date: date,
) -> bool:
    return (
        _timestamp_date(result.db_visible_at) == business_date
        and _timestamp_date(result.api_visible_at) == business_date
    )


def _timestamp_date(value: datetime) -> date:
    return value.astimezone(_SHANGHAI).date()


def _local_deadline(day: date, deadline: time) -> datetime:
    return datetime.combine(day, deadline, tzinfo=_SHANGHAI)


def _isoformat(value: datetime) -> str:
    return value.astimezone(_SHANGHAI).isoformat()


def _add_violation(
    violations: list[dict[str, object]],
    code: str,
    message: str,
    **details: object,
) -> None:
    violation: dict[str, object] = {"code": code, "message": message}
    violation.update(details)
    violations.append(violation)


def _require_mapping(
    value: object,
    path: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CapacityObservationError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise CapacityObservationError(f"{path} keys must be strings")
    return value


def _required_text(
    row: Mapping[str, object],
    field: str,
    path: str,
) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapacityObservationError(
            f"{path}.{field} must be a non-empty string"
        )
    return value.strip()


def _required_sha256(
    row: Mapping[str, object],
    field: str,
    path: str,
) -> str:
    value = _required_text(row, field, path)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CapacityObservationError(
            f"{path}.{field} must be a canonical lowercase SHA-256"
        )
    return value


def _required_list(
    row: Mapping[str, object],
    field: str,
    path: str,
) -> Sequence[object]:
    value = row.get(field)
    if not isinstance(value, list):
        raise CapacityObservationError(f"{path}.{field} must be an array")
    return value


def _parse_date(value: object, path: str) -> date:
    if not isinstance(value, str):
        raise CapacityObservationError(f"{path} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CapacityObservationError(f"{path} must be an ISO date") from exc
    if value != parsed.isoformat():
        raise CapacityObservationError(
            f"{path} must use canonical YYYY-MM-DD format"
        )
    return parsed


def _parse_timestamp(value: object, path: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CapacityObservationError(
            f"{path} must be a timezone-aware ISO timestamp"
        )
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CapacityObservationError(
            f"{path} must be a timezone-aware ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CapacityObservationError(
            f"{path} must be a timezone-aware ISO timestamp"
        )
    return parsed.astimezone(_SHANGHAI)


def _positive_finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityObservationError(
            f"{path} must be a positive finite number"
        )
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise CapacityObservationError(
            f"{path} must be a positive finite number"
        )
    return parsed


def _reject_duplicates(values: Sequence[str], label: str) -> None:
    duplicates = sorted(
        value for value in set(values) if values.count(value) > 1
    )
    if duplicates:
        raise CapacityObservationError(
            f"{label} values must be unique; duplicates={duplicates}"
        )
