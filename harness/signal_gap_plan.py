from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from sqlalchemy import bindparam, text

from backend.factor_lab_dashboard_semantics import (
    choose_latest_backtest_runs,
)
from scheduler.capacity_attestation import canonical_json_sha256
from scheduler.discovery import discover_schemes
from shared.actual_facts import build_week_calendar
from shared.blackbox_v2.contracts import TASK_COMBINATIONS
from shared.calendar_service import read_calendar_snapshot_from_connection
from shared.data_bridge.authority import (
    StableDataBridgeCurrentAuthority,
    StableDataBridgeCutoff,
    resolve_stable_databridge_current_authority,
)
from shared.data_bridge.refresh import (
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeRefreshConfig,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
    open_native_generation,
)
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)


PLAN_SCHEMA_VERSION = "active-signal-gap-plan-v2"
PLATFORM_LIVE_BOUNDARY_VERSION = "platform_live_boundary_v1"
PLATFORM_LIVE_TARGET_START_DATE = "2026-06-01"
EXPECTED_ACTIVE_TARGET_COUNT = 44
EXPECTED_ACTIVE_EXECUTION_COUNT = 40
EXPECTED_ACTIVE_FREQUENCY_COUNTS = {
    "daily": 29,
    "weekly": 7,
    "monthly": 8,
}
NATIVE_TASK_COMBINATIONS = {
    "T+1": (1, "daily"),
    "T+5": (5, "daily"),
    "weekly_point": (6, "weekly"),
    "weekly_average": (6, "weekly"),
    "monthly": (30, "monthly"),
}
VALID_ACTIONS = (
    "SKIP_PRESENT",
    "GRAY_LIVE_GAP",
    "FULL_CANONICAL_RUN_REQUIRED",
    "BLOCKED_NO_GENERATION",
    "BLOCKED_DATA_CONTRACT",
)
APPROVED_0629_LIVE_SOURCE_SCHEMES = frozenset(
    {
        "daily_1y_xgb_1y13_0629",
        "daily_5y_lgbm_5y10_0629",
        "daily_10y_lgbm_10y04_0629",
    }
)
Action = Literal[
    "SKIP_PRESENT",
    "GRAY_LIVE_GAP",
    "FULL_CANONICAL_RUN_REQUIRED",
    "BLOCKED_NO_GENERATION",
    "BLOCKED_DATA_CONTRACT",
]
Segment = Literal["canonical", "live"]
_NATIVE_GENERATION_ID_PATTERN = re.compile(
    r"^native-[0-9a-f]{24}$"
)
_INPUT_GENERATION_FINGERPRINT_FIELDS = (
    "generation_id",
    "generation_type",
    "business_date",
    "feature_date",
    "readiness_basis",
    "source_commit_token",
    "dataset_content_id",
    "schema_version",
    "exporter_version",
    "manifest_uri",
    "manifest_sha256",
    "native_generation_id",
    "native_manifest_sha256",
    "state",
    "sealed_at",
)


class SignalGapPlanError(RuntimeError):
    """缺口计划的权威输入缺失或违反 fail-closed 契约。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DiscoveredSchemeIdentity:
    base_scheme_id: str
    scheme_version: str
    runtime_type: str
    code_sha256: str
    config_sha256: str
    status: str
    source_package_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryTarget:
    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    frequency: str
    task_type: str
    target_tenor: str
    horizon: int
    scheme_version: str
    live_target_start_date: str
    live_boundary_source: str = "explicit"
    input_mode: str | None = None
    code_sha256: str | None = None
    config_sha256: str | None = None
    source_package_sha256: str | None = None

    @property
    def business_identity(self) -> tuple[str, str, int]:
        return (self.base_scheme_id, self.target_tenor, self.horizon)


@dataclass(frozen=True, slots=True)
class ExpectedSignalCase:
    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    frequency: str
    task_type: str
    target_tenor: str
    horizon: int
    predict_date: str
    feature_date: str
    target_date: str
    segment: Segment
    data_contract_error: str | None = None

    @property
    def business_key(self) -> tuple[str, str, int, str]:
        return (
            self.base_scheme_id,
            self.target_tenor,
            self.horizon,
            self.target_date,
        )


@dataclass(frozen=True, slots=True)
class ObservedSignal:
    base_scheme_id: str
    target_tenor: str
    horizon: int
    target_date: str
    predict_date: str
    feature_date: str
    phase: str
    scheme_version: str
    run_status: str
    contract_error: str | None = None

    @property
    def business_key(self) -> tuple[str, str, int, str]:
        return (
            self.base_scheme_id,
            self.target_tenor,
            self.horizon,
            self.target_date,
        )


@dataclass(frozen=True, slots=True)
class InputGeneration:
    generation_id: str
    generation_type: str
    business_date: str
    feature_date: str
    readiness_basis: str
    source_commit_token: str
    dataset_content_id: str
    schema_version: str
    exporter_version: str
    manifest_uri: str
    manifest_sha256: str
    native_generation_id: str | None
    native_manifest_sha256: str | None
    state: str
    sealed_at: str | None


@dataclass(frozen=True, slots=True)
class _NativeArtifactVerification:
    """单次 plan 内可安全复用的紧凑校验结果。"""

    authority_json: str | None
    failure_code: str | None

    def authority(self) -> dict[str, Any] | None:
        if self.authority_json is None:
            return None
        return json.loads(self.authority_json)


class _NativeArtifactVerifier:
    """逐 plan 重开 Native artifact；不保留 context 或 DataFrame。"""

    def __init__(
        self,
        generations: Sequence[InputGeneration],
    ) -> None:
        self._memo: dict[
            tuple[Any, ...],
            _NativeArtifactVerification,
        ] = {}
        fingerprints_by_id: dict[
            str,
            set[tuple[Any, ...]],
        ] = {}
        for generation in generations:
            if generation.generation_type != "native_source":
                continue
            fingerprints_by_id.setdefault(
                generation.generation_id,
                set(),
            ).add(_input_generation_fingerprint(generation))
        self._conflicting_ids = frozenset(
            generation_id
            for generation_id, fingerprints in fingerprints_by_id.items()
            if len(fingerprints) > 1
        )

    def verify(
        self,
        generation: InputGeneration,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        fingerprint = _input_generation_fingerprint(generation)
        cached = self._memo.get(fingerprint)
        if cached is not None:
            return cached.authority(), cached.failure_code
        if generation.generation_id in self._conflicting_ids:
            result = _NativeArtifactVerification(
                authority_json=None,
                failure_code="NATIVE_GENERATION_DB_CONTEXT_DRIFT",
            )
            self._memo[fingerprint] = result
            return None, result.failure_code

        context = None
        try:
            context = open_native_generation(
                Path(generation.manifest_uri),
                expected_generation_id=generation.generation_id,
                expected_manifest_sha256=generation.manifest_sha256,
                expected_business_date=generation.business_date,
                expected_feature_date=generation.feature_date,
            )
            database_uri = str(
                Path(generation.manifest_uri).resolve(strict=True)
            )
            artifact_uri = str(
                context.manifest_path.resolve(strict=True)
            )
            artifact = {
                "generation_id": context.generation_id,
                "generation_type": context.generation_type,
                "business_date": context.business_date,
                "feature_date": context.feature_date,
                "readiness_basis": context.readiness_basis,
                "source_commit_token": context.source_commit_token,
                "dataset_content_id": context.dataset_content_id,
                "schema_version": context.schema_version,
                "exporter_version": context.exporter_version,
                "manifest_uri": artifact_uri,
                "manifest_sha256": context.manifest_sha256,
                "sealed_at": context.sealed_at,
            }
            expected = {
                "generation_id": generation.generation_id,
                "generation_type": generation.generation_type,
                "business_date": generation.business_date,
                "feature_date": generation.feature_date,
                "readiness_basis": generation.readiness_basis,
                "source_commit_token": generation.source_commit_token,
                "dataset_content_id": generation.dataset_content_id,
                "schema_version": generation.schema_version,
                "exporter_version": generation.exporter_version,
                "manifest_uri": database_uri,
                "manifest_sha256": generation.manifest_sha256,
            }
            comparable_artifact = {
                key: artifact[key]
                for key in expected
            }
            if comparable_artifact != expected:
                result = _NativeArtifactVerification(
                    authority_json=None,
                    failure_code=(
                        "NATIVE_GENERATION_DB_CONTEXT_DRIFT"
                    ),
                )
            else:
                authority = {
                    "database": _input_generation_authority(
                        generation
                    ),
                    "artifact": artifact,
                }
                result = _NativeArtifactVerification(
                    authority_json=json.dumps(
                        authority,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    failure_code=None,
                )
        except (ValueError, OSError):
            result = _NativeArtifactVerification(
                authority_json=None,
                failure_code="NATIVE_GENERATION_ARTIFACT_INVALID",
            )
        finally:
            if context is not None:
                context.dispose()
                del context
        self._memo[fingerprint] = result
        return result.authority(), result.failure_code


@dataclass(frozen=True, slots=True)
class SignalGapSnapshot:
    registry_targets: tuple[RegistryTarget, ...]
    expected_cases: tuple[ExpectedSignalCase, ...]
    canonical_signals: tuple[ObservedSignal, ...]
    live_signals: tuple[ObservedSignal, ...]
    input_generations: tuple[InputGeneration, ...]
    input_watermarks: Mapping[str, str | int | None]
    source_identity_sha256: str
    discovery_identity_sha256: str
    active_version_identity_sha256: str
    control_plane_blockers: tuple[Mapping[str, Any], ...] = ()
    canonical_authorities: tuple[Mapping[str, Any], ...] = ()
    databridge_authority: (
        StableDataBridgeCurrentAuthority | None
    ) = None
    databridge_authority_error: (
        Literal["MISSING", "INVALID"] | None
    ) = None


SnapshotReader = Callable[..., SignalGapSnapshot]


def plan_signal_gaps(
    engine: Any,
    *,
    start_date: str,
    as_of_date: str,
    snapshot_reader: SnapshotReader | None = None,
    execution_authority: Sequence[DiscoveredSchemeIdentity] | None = None,
    databridge_config: DataBridgeRefreshConfig,
) -> dict[str, Any]:
    """在一个 RR consistent snapshot/read-only 事务中规划全部缺口。"""
    normalized_start = _canonical_date(start_date, "start_date")
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    if normalized_start > normalized_as_of:
        raise SignalGapPlanError(
            "INVALID_DATE_RANGE",
            "start_date must not be after as_of_date",
        )
    authority = (
        tuple(execution_authority)
        if execution_authority is not None
        else _discover_execution_authority()
    )
    discovery_identity_sha256 = _discovery_identity_sha256(authority)
    reader = snapshot_reader or read_signal_gap_snapshot
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            try:
                snapshot = reader(
                    connection,
                    start_date=normalized_start,
                    as_of_date=normalized_as_of,
                    execution_authority=authority,
                    discovery_identity_sha256=discovery_identity_sha256,
                    databridge_config=databridge_config,
                )
            except SignalGapPlanError:
                raise
            except Exception as exc:
                raise SignalGapPlanError(
                    "CASE_BUILDER_FAILED",
                    type(exc).__name__,
                ) from exc
            return build_signal_gap_plan(
                snapshot,
                start_date=normalized_start,
                as_of_date=normalized_as_of,
            )
        finally:
            connection.rollback()


def build_signal_gap_plan(
    snapshot: SignalGapSnapshot,
    *,
    start_date: str,
    as_of_date: str,
) -> dict[str, Any]:
    """将权威应有集合与现存结果按业务键做确定性差集。"""
    normalized_start = _canonical_date(start_date, "start_date")
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    target_by_registry = _validate_snapshot(snapshot)
    control_plane_blockers = _normalized_control_plane_blockers(
        snapshot.control_plane_blockers
    )
    blocker_codes_by_base_segment: dict[
        tuple[str, str],
        set[str],
    ] = {}
    for blocker in control_plane_blockers:
        for segment in blocker["segment_scope"]:
            blocker_codes_by_base_segment.setdefault(
                (str(blocker["base_scheme_id"]), str(segment)),
                set(),
            ).add(str(blocker["code"]))
    native_artifact_verifier = _NativeArtifactVerifier(
        snapshot.input_generations
    )
    expected_keys = {
        (case.segment, case.business_key)
        for case in snapshot.expected_cases
        if case.predict_date >= normalized_start
        and case.predict_date <= normalized_as_of
    }
    expected_business_keys = {
        business_key for _, business_key in expected_keys
    }
    canonical = _index_observations(
        _observations_in_scope(
            snapshot.canonical_signals,
            expected_business_keys=expected_business_keys,
            start_date=normalized_start,
            as_of_date=normalized_as_of,
        )
    )
    live = _index_observations(
        _observations_in_scope(
            snapshot.live_signals,
            expected_business_keys=expected_business_keys,
            start_date=normalized_start,
            as_of_date=normalized_as_of,
        )
    )
    unexpected = sorted(
        {
            ("canonical", key)
            for key in canonical
            if key not in expected_business_keys
        }
        | {
            ("live", key)
            for key in live
            if key not in expected_business_keys
        }
    )
    observed_contract_anomalies = [
        {
            "segment": segment,
            "business_key": list(business_key),
            "reason": "OBSERVED_SIGNAL_OUTSIDE_AUTHORITY",
        }
        for segment, business_key in unexpected
    ]

    actions: list[dict[str, Any]] = []
    for item in sorted(snapshot.expected_cases, key=_case_sort_key):
        if not normalized_start <= item.predict_date <= normalized_as_of:
            continue
        target = target_by_registry.get(item.registry_scheme_id)
        if target is None:
            raise SignalGapPlanError(
                "CASE_REGISTRY_DRIFT",
                f"expected case references inactive target "
                f"{item.registry_scheme_id}",
            )
        _validate_case_matches_target(item, target)
        expected_source = canonical if item.segment == "canonical" else live
        opposite_source = live if item.segment == "canonical" else canonical
        observed = expected_source.get(item.business_key, ())
        opposite = opposite_source.get(item.business_key, ())
        business_key_present = bool(observed)
        observation_error = _observation_contract_error(
            item,
            target,
            observed=observed,
            opposite=opposite,
        )
        action, input_authority, reason = _resolve_action(
            item,
            target=target,
            control_plane_error=(
                ",".join(
                    sorted(
                        blocker_codes_by_base_segment[
                            (item.base_scheme_id, item.segment)
                        ]
                    )
                )
                if (
                    item.base_scheme_id,
                    item.segment,
                ) in blocker_codes_by_base_segment
                else None
            ),
            valid_present=(len(observed) == 1 and observation_error is None),
            observation_error=observation_error,
            generations=snapshot.input_generations,
            databridge_authority=snapshot.databridge_authority,
            databridge_authority_error=(
                snapshot.databridge_authority_error
            ),
            native_artifact_verifier=(
                native_artifact_verifier.verify
            ),
        )
        if (
            business_key_present
            and action == "BLOCKED_DATA_CONTRACT"
        ):
            observed_contract_anomalies.append(
                {
                    "segment": item.segment,
                    "business_key": list(item.business_key),
                    "reason": reason,
                }
            )
        actions.append(
            _action_row(
                item,
                target=target,
                action=action,
                reason=reason,
                input_authority=input_authority,
                business_key_present=business_key_present,
            )
        )

    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in VALID_ACTIONS
    }
    present = sum(row["business_key_present"] for row in actions)
    open_gap = len(actions) - present
    actionable = (
        action_counts["GRAY_LIVE_GAP"]
        + action_counts["FULL_CANONICAL_RUN_REQUIRED"]
    )
    blocked = (
        action_counts["BLOCKED_NO_GENERATION"]
        + action_counts["BLOCKED_DATA_CONTRACT"]
    )
    canonical_rebuild_groups = _canonical_rebuild_groups(
        actions,
        snapshot.registry_targets,
    )
    action_segments = {
        str(row["segment"])
        for row in actions
    }
    relevant_control_plane_blocker = bool(
        control_plane_blockers
    ) and (
        not action_segments
        or any(
            action_segments.intersection(blocker["segment_scope"])
            for blocker in control_plane_blockers
        )
    )
    unsigned: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": (
            "BLOCKED"
            if (
                blocked
                or relevant_control_plane_blocker
                or observed_contract_anomalies
            )
            else "READY"
        ),
        "start_date": normalized_start,
        "as_of_date": normalized_as_of,
        "scope": {
            "active_target_count": len(snapshot.registry_targets),
            "execution_count": len(
                {
                    target.base_scheme_id
                    for target in snapshot.registry_targets
                }
            ),
            "frequency_counts": {
                frequency: sum(
                    target.frequency == frequency
                    for target in snapshot.registry_targets
                )
                for frequency in ("daily", "weekly", "monthly")
            },
        },
        "control_plane": {
            "read_only": True,
            "transaction_isolation": "REPEATABLE READ",
            "consistent_snapshot": True,
            "as_of_semantics": "predict_date_lte_as_of_date",
            "business_key_fields": [
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ],
            "blockers": control_plane_blockers,
        },
        "source_snapshot": {
            "identity_sha256": snapshot.source_identity_sha256,
            "discovery_identity_sha256":
                snapshot.discovery_identity_sha256,
            "active_version_identity_sha256":
                snapshot.active_version_identity_sha256,
            "registry_digest_sha256": _registry_digest_sha256(
                snapshot.registry_targets
            ),
            "watermarks": dict(
                sorted(snapshot.input_watermarks.items())
            ),
            "canonical_row_count": len(snapshot.canonical_signals),
            "live_row_count": len(snapshot.live_signals),
            "generation_row_count": len(snapshot.input_generations),
            "canonical_authorities": [
                dict(item)
                for item in sorted(
                    snapshot.canonical_authorities,
                    key=lambda row: str(row["base_scheme_id"]),
                )
            ],
        },
        "counts": {
            "active_target": len(snapshot.registry_targets),
            "expected": len(actions),
            "expected_total": len(actions),
            "canonical_expected": sum(
                row["segment"] == "canonical" for row in actions
            ),
            "live_expected": sum(
                row["segment"] == "live" for row in actions
            ),
            "present": present,
            "open_gap": open_gap,
            "actionable": actionable,
            "blocked": blocked,
            "observed_contract_anomaly":
                len(observed_contract_anomalies),
            **action_counts,
        },
        "observed_contract_anomalies": observed_contract_anomalies,
        "canonical_rebuild_groups": canonical_rebuild_groups,
        "actions": actions,
    }
    return {
        **unsigned,
        "plan_sha256": canonical_plan_sha256(unsigned),
    }


def canonical_plan_sha256(unsigned_plan: Mapping[str, Any]) -> str:
    """复用平台 canonical JSON 规则计算 plan SHA-256。"""
    payload = dict(unsigned_plan)
    payload.pop("plan_sha256", None)
    return canonical_json_sha256(payload)


def build_expected_live_cases(
    registry_targets: Sequence[RegistryTarget],
    *,
    calendar: Any,
    as_of_date: str,
    live_target_start_date: str | None = None,
) -> tuple[ExpectedSignalCase, ...]:
    """复用现有日/周/月 context builder，并使用逐 target 边界。"""
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    cases: list[ExpectedSignalCase] = []
    by_frequency = {
        "daily": tuple(calendar.daily_predict_dates),
        "weekly": tuple(calendar.weekly_predict_dates),
        "monthly": tuple(calendar.monthly_predict_dates),
    }
    for target in registry_targets:
        target_start = _canonical_date(
            live_target_start_date or target.live_target_start_date,
            "live_target_start_date",
        )
        try:
            predict_dates = by_frequency[target.frequency]
        except KeyError as exc:
            raise SignalGapPlanError(
                "INVALID_REGISTRY_TARGET",
                f"unsupported frequency={target.frequency!r}",
            ) from exc
        for raw_predict_date in predict_dates:
            predict_date = _canonical_date(
                raw_predict_date,
                "predict_date",
            )
            if predict_date > normalized_as_of:
                continue
            if target.frequency == "daily":
                context = build_daily_live_context(
                    calendar,
                    predict_date,
                    horizon=target.horizon,
                )
            elif target.frequency == "weekly":
                context = build_weekly_live_context(
                    calendar,
                    predict_date,
                )
            else:
                context = build_monthly_live_context(
                    calendar,
                    predict_date,
                )
            if str(context.target_date) < target_start:
                continue
            cases.append(
                _case_from_target(
                    target,
                    predict_date=predict_date,
                    feature_date=str(context.feature_date),
                    target_date=str(context.target_date),
                    segment="live",
                )
            )
    return tuple(sorted(cases, key=_case_sort_key))


def build_expected_canonical_cases(
    registry_targets: Sequence[RegistryTarget],
    *,
    calendar: Any,
    start_date: str,
) -> tuple[ExpectedSignalCase, ...]:
    """仅由 Registry、逐 target 边界和冻结日历枚举历史应有集合。"""
    normalized_start = _canonical_date(start_date, "start_date")
    cases: list[ExpectedSignalCase] = []
    weekly_pairs = tuple(calendar.canonical_week_pairs())
    for target in registry_targets:
        boundary = _canonical_date(
            target.live_target_start_date,
            "live_target_start_date",
        )
        if target.frequency == "daily":
            for feature_date in calendar.canonical_daily_feature_dates:
                if feature_date < normalized_start:
                    continue
                target_date = calendar.canonical_nth_trading_day_after(
                    feature_date,
                    target.horizon,
                )
                if target_date >= boundary:
                    continue
                cases.append(
                    _case_from_target(
                        target,
                        predict_date=feature_date,
                        feature_date=feature_date,
                        target_date=target_date,
                        segment="canonical",
                    )
                )
        elif target.frequency == "weekly":
            for feature_date, target_date in weekly_pairs:
                if feature_date < normalized_start:
                    continue
                if target_date >= boundary:
                    continue
                cases.append(
                    _case_from_target(
                        target,
                        predict_date=feature_date,
                        feature_date=feature_date,
                        target_date=target_date,
                        segment="canonical",
                    )
                )
        elif target.frequency == "monthly":
            for predict_date in calendar.monthly_predict_dates:
                if predict_date < normalized_start:
                    continue
                context = build_monthly_live_context(
                    calendar,
                    predict_date,
                )
                if str(context.target_date) >= boundary:
                    continue
                cases.append(
                    _case_from_target(
                        target,
                        predict_date=predict_date,
                        feature_date=str(context.feature_date),
                        target_date=str(context.target_date),
                        segment="canonical",
                    )
                )
        else:
            raise SignalGapPlanError(
                "INVALID_REGISTRY_TARGET",
                f"unsupported frequency={target.frequency!r}",
            )
    return tuple(sorted(cases, key=_case_sort_key))


def read_signal_gap_snapshot(
    connection: Any,
    *,
    start_date: str,
    as_of_date: str,
    execution_authority: Sequence[DiscoveredSchemeIdentity],
    discovery_identity_sha256: str,
    databridge_config: DataBridgeRefreshConfig,
) -> SignalGapSnapshot:
    """读取真实 active/version/result 快照并建立可审计 authority。"""
    (
        raw_registry,
        registry_targets,
        control_plane_blockers,
        active_version_identity_sha256,
    ) = _read_registry_versions(
        connection,
        execution_authority=execution_authority,
    )
    calendar_snapshot = read_calendar_snapshot_from_connection(connection)
    calendar = _FrozenCalendar(
        trade_calendar_rows=calendar_snapshot[
            "t_trade_calendar.csv"
        ].to_dict("records"),
        week_calendar_rows=[
            {
                "rdate": row["rdate"],
                "week_id": row["week_id"],
                "trade_flag": trade_flags.get(
                    str(row["rdate"])[:10],
                    "0",
                ),
            }
            for row in calendar_snapshot[
                "api_wind_date.csv"
            ].to_dict("records")
            for trade_flags in [
                {
                    str(item["rdate"])[:10]: item["trade_flag"]
                    for item in calendar_snapshot[
                        "t_trade_calendar.csv"
                    ].to_dict("records")
                }
            ]
        ],
        start_date=start_date,
        as_of_date=as_of_date,
    )
    blackbox_targets = tuple(
        target
        for target in registry_targets
        if target.runtime_type == "blackbox_v2"
    )
    blackbox_cases = build_expected_canonical_cases(
        blackbox_targets,
        calendar=calendar,
        start_date=start_date,
    )
    blackbox_cases, actual_watermarks = _read_canonical_actual_facts(
        connection,
        blackbox_cases,
    )
    (
        native_cases,
        canonical_signals,
        canonical_watermarks,
        native_blockers,
        native_authorities,
    ) = (
        _read_persisted_canonical_observations(
            connection,
            raw_registry,
            registry_targets,
        )
    )
    canonical_cases = tuple(
        sorted(
            (*native_cases, *blackbox_cases),
            key=_case_sort_key,
        )
    )
    canonical_authorities = tuple(
        sorted(
            (
                *native_authorities,
                *_calendar_canonical_authorities(blackbox_cases),
            ),
            key=lambda row: str(row["base_scheme_id"]),
        )
    )
    control_plane_blockers = tuple(
        _normalized_control_plane_blockers(
            (*control_plane_blockers, *native_blockers)
        )
    )
    live_cases = build_expected_live_cases(
        registry_targets,
        calendar=calendar,
        as_of_date=as_of_date,
    )
    raw_live, live_signals = _read_live_signals(
        connection,
        registry_targets,
        as_of_date=as_of_date,
        expected_business_keys=_expected_business_keys_for_date_scope(
            (*canonical_cases, *live_cases),
            start_date=start_date,
            as_of_date=as_of_date,
        ),
    )
    generations = _read_input_generations(connection)
    observed_live_keys = {
        signal.business_key for signal in live_signals
    }
    required_databridge_feature_dates = tuple(
        sorted(
            {
                case.feature_date
                for case in live_cases
                if case.runtime_type == "blackbox_v2"
                and case.business_key not in observed_live_keys
            }
        )
    )
    databridge_authority = None
    databridge_authority_error = None
    if required_databridge_feature_dates:
        try:
            databridge_authority = (
                resolve_stable_databridge_current_authority(
                    databridge_config,
                    feature_dates=required_databridge_feature_dates,
                    connection=connection,
                )
            )
        except DataBridgeCurrentMissingError:
            databridge_authority_error = "MISSING"
        except DataBridgeCurrentInvalidError:
            databridge_authority_error = "INVALID"
    identity = connection.execute(
        text(
            """
            SELECT DATABASE() AS database_name,
                   @@server_uuid AS server_uuid,
                   @@port AS server_port
            """
        )
    ).mappings().one()
    source_identity_sha256 = canonical_json_sha256(
        {
            "database_name": str(identity["database_name"] or ""),
            "server_uuid": str(identity["server_uuid"] or ""),
            "server_port": int(identity["server_port"]),
        }
    )
    watermarks = {
        **canonical_watermarks,
        **actual_watermarks,
        "active_registry_count": len(registry_targets),
        "raw_live_row_count": len(raw_live),
        "selected_live_row_count": len(live_signals),
        "trade_calendar_max": max(
            (
                day
                for day in calendar._trading_days
                if day <= calendar._canonical_as_of_date
            ),
            default=None,
        ),
        "sealed_generation_count": sum(
            generation.state == "SEALED"
            for generation in generations
        ),
    }
    return SignalGapSnapshot(
        registry_targets=registry_targets,
        expected_cases=tuple((*canonical_cases, *live_cases)),
        canonical_signals=canonical_signals,
        live_signals=live_signals,
        input_generations=generations,
        input_watermarks=watermarks,
        source_identity_sha256=source_identity_sha256,
        discovery_identity_sha256=discovery_identity_sha256,
        active_version_identity_sha256=
            active_version_identity_sha256,
        control_plane_blockers=control_plane_blockers,
        canonical_authorities=canonical_authorities,
        databridge_authority=databridge_authority,
        databridge_authority_error=databridge_authority_error,
    )


class _FrozenCalendar:
    """只使用 connection-bound 日历快照的 context builder 适配器。"""

    def __init__(
        self,
        *,
        trade_calendar_rows: Sequence[Mapping[str, Any]],
        week_calendar_rows: Sequence[Mapping[str, Any]],
        start_date: str,
        as_of_date: str,
    ) -> None:
        self._trading_days = tuple(
            sorted(
                str(row["rdate"])[:10]
                for row in trade_calendar_rows
                if str(row.get("trade_flag", "")).strip() == "1"
            )
        )
        if not self._trading_days:
            raise SignalGapPlanError(
                "DATA_CONTRACT_INVALID",
                "trade calendar has no trading days",
            )
        self._trading_day_set = frozenset(self._trading_days)
        self._canonical_daily_days = tuple(
            day
            for day in self._trading_days
            if date.fromisoformat(day).weekday() < 5
        )
        self._canonical_as_of_date = _canonical_date(
            as_of_date,
            "as_of_date",
        )
        self._week_calendar = build_week_calendar(week_calendar_rows)
        self.daily_predict_dates = tuple(
            day
            for day in self._trading_days
            if start_date <= day <= as_of_date
        )
        self.canonical_daily_feature_dates = tuple(
            day
            for day in self._canonical_daily_days
            if start_date <= day <= as_of_date
        )
        self.weekly_predict_dates = tuple(
            sorted(
                predict_date
                for predict_date
                in self._week_calendar.week_predict_date.values()
                if start_date <= predict_date <= as_of_date
            )
        )
        self.monthly_predict_dates = tuple(
            _monthly_trigger_dates(start_date, as_of_date)
        )

    def is_trading_day(self, value: str) -> bool:
        return str(value)[:10] in self._trading_day_set

    def previous_trading_day(self, value: str) -> str:
        candidates = [
            day for day in self._trading_days if day < str(value)[:10]
        ]
        if not candidates:
            raise ValueError(f"no previous trading day before {value}")
        return candidates[-1]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        return [
            day for day in self._trading_days if day > str(value)[:10]
        ][:count]

    def nth_trading_day_after(self, value: str, n: int) -> str:
        days = self.next_trading_days(value, n)
        if len(days) != n:
            raise ValueError(
                f"not enough trading days after {value} for horizon={n}"
            )
        return days[-1]

    def canonical_nth_trading_day_after(
        self,
        value: str,
        n: int,
    ) -> str:
        days = [
            day
            for day in self._canonical_daily_days
            if day > str(value)[:10]
        ][:n]
        if len(days) != n:
            raise ValueError(
                f"not enough canonical trading days after {value} "
                f"for horizon={n}"
            )
        return days[-1]

    def week_id_for_date(self, value: str) -> int | None:
        return self._week_calendar.week_id_for_date(value)

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self._week_calendar.week_id_to_last_trading_day(week_id)

    def canonical_week_pairs(self) -> tuple[tuple[str, str], ...]:
        """按历史算法 weekday-only 周历返回相邻实际周末。"""
        last_day_by_week: dict[int, str] = {}
        for day in self._canonical_daily_days:
            if day > self._canonical_as_of_date:
                continue
            week_id = self._week_calendar.week_id_for_date(day)
            if week_id is not None:
                last_day_by_week[int(week_id)] = day
        last_days = sorted(last_day_by_week.values())
        return tuple(zip(last_days, last_days[1:]))


def _read_registry_versions(
    connection: Any,
    *,
    execution_authority: Sequence[DiscoveredSchemeIdentity],
) -> tuple[
    list[dict[str, Any]],
    tuple[RegistryTarget, ...],
    tuple[Mapping[str, Any], ...],
    str,
]:
    registry_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT r.scheme_id, r.base_scheme_id, r.runtime_type,
                       r.frequency, r.task_type, r.target_tenor, r.horizon
                FROM t_scheme_registry r
                WHERE r.status = 'active'
                ORDER BY r.scheme_id
                """
            )
        ).mappings().all()
    ]
    version_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT v.scheme_id, v.scheme_version, v.runtime_type,
                       v.code_hash, v.config_hash
                FROM t_scheme_versions v
                WHERE v.status = 'active'
                ORDER BY v.scheme_id, v.scheme_version
                """
            )
        ).mappings().all()
    ]
    authority_by_base = _authority_by_base(execution_authority)
    active_base_ids = {str(row["base_scheme_id"]) for row in registry_rows}
    active_version_identity_sha256 = (
        _active_version_identity_sha256(version_rows)
    )
    versions_by_base: dict[str, list[dict[str, Any]]] = {}
    for row in version_rows:
        scheme_id = str(row["scheme_id"])
        if scheme_id in active_base_ids:
            versions_by_base.setdefault(scheme_id, []).append(row)
    resolved_targets: list[RegistryTarget] = []
    blockers: list[dict[str, Any]] = [
        {
            "code": "DISCOVERY_IDENTITY_NOT_REGISTERED",
            "base_scheme_id": base_scheme_id,
        }
        for base_scheme_id in sorted(
            set(authority_by_base) - active_base_ids
        )
    ]
    for registry_row in registry_rows:
        base_scheme_id = str(registry_row["base_scheme_id"])
        authority = authority_by_base.get(base_scheme_id)
        authority_missing = authority is None
        if authority_missing:
            blockers.append(
                {
                    "code": "DISCOVERY_IDENTITY_MISSING",
                    "base_scheme_id": base_scheme_id,
                }
            )
            authority = _missing_discovery_identity(registry_row)
        active_versions = versions_by_base.get(base_scheme_id, [])
        exact_versions = [
            version
            for version in active_versions
            if str(version["scheme_version"])
            == authority.scheme_version
        ]
        if (
            authority.runtime_type == "blackbox_v2"
            and len(active_versions) != 1
        ):
            blockers.append(
                {
                    "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
                    "base_scheme_id": base_scheme_id,
                    "active_version_count": len(active_versions),
                }
            )
        if not authority_missing and len(exact_versions) != 1:
            blockers.append(
                {
                    "code": "ACTIVE_VERSION_EXACT_IDENTITY_MISSING",
                    "base_scheme_id": base_scheme_id,
                    "expected_scheme_version":
                        authority.scheme_version,
                }
            )
        exact_version = exact_versions[0] if len(exact_versions) == 1 else None
        if str(registry_row["runtime_type"]) != authority.runtime_type:
            blockers.append(
                {
                    "code": "REGISTRY_DISCOVERY_RUNTIME_DRIFT",
                    "base_scheme_id": base_scheme_id,
                }
            )
        if exact_version is not None:
            if str(exact_version["runtime_type"]) != authority.runtime_type:
                blockers.append(
                    {
                        "code": "REGISTRY_VERSION_RUNTIME_DRIFT",
                        "base_scheme_id": base_scheme_id,
                    }
                )
            if (
                str(exact_version.get("code_hash") or "")
                != authority.code_sha256
                or str(exact_version.get("config_hash") or "")
                != authority.config_sha256
            ):
                blockers.append(
                    {
                        "code": "ACTIVE_VERSION_DIGEST_DRIFT",
                        "base_scheme_id": base_scheme_id,
                    }
                )
        resolved_targets.append(
            RegistryTarget(
                registry_scheme_id=str(registry_row["scheme_id"]),
                base_scheme_id=base_scheme_id,
                runtime_type=authority.runtime_type,
                frequency=str(registry_row["frequency"]),
                task_type=str(registry_row["task_type"]),
                target_tenor=str(registry_row["target_tenor"]),
                horizon=int(registry_row["horizon"]),
                scheme_version=authority.scheme_version,
                live_target_start_date=PLATFORM_LIVE_TARGET_START_DATE,
                live_boundary_source=PLATFORM_LIVE_BOUNDARY_VERSION,
                input_mode=_input_mode_for_identity(
                    base_scheme_id,
                    authority.runtime_type,
                ),
                code_sha256=authority.code_sha256,
                config_sha256=authority.config_sha256,
                source_package_sha256=
                    authority.source_package_sha256,
            )
        )
    targets = tuple(resolved_targets)
    _validate_active_scope(targets)
    return (
        registry_rows,
        targets,
        tuple(_normalized_control_plane_blockers(blockers)),
        active_version_identity_sha256,
    )


def _read_live_signals(
    connection: Any,
    targets: Sequence[RegistryTarget],
    *,
    as_of_date: str,
    expected_business_keys: set[tuple[str, str, int, str]],
) -> tuple[list[dict[str, Any]], tuple[ObservedSignal, ...]]:
    base_ids = sorted({target.base_scheme_id for target in targets})
    raw_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT p.id, p.scheme_id, p.target_tenor, p.horizon,
                       p.predict_date, p.feature_date, p.target_date,
                       p.prediction_phase, p.scheme_version, p.run_id,
                       r.status AS run_status,
                       r.scheme_id AS run_scheme_id,
                       r.scheme_version AS run_scheme_version,
                       r.runtime_type AS run_runtime_type,
                       r.prediction_phase AS run_prediction_phase,
                       r.predict_date AS run_predict_date
                FROM t_scheme_predictions p
                LEFT JOIN t_scheme_runs r ON r.run_id = p.run_id
                WHERE p.scheme_id IN :scheme_ids
                ORDER BY p.scheme_id, p.target_tenor, p.horizon,
                         p.target_date, p.predict_date, p.id
                """
            ).bindparams(bindparam("scheme_ids", expanding=True)),
            {"scheme_ids": base_ids},
        ).mappings().all()
    ]
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    rows = [
        row
        for row in raw_rows
        if (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"])[:10],
        )
        in expected_business_keys
        or str(row["predict_date"])[:10] <= normalized_as_of
    ]
    target_by_identity = {
        target.business_identity: target for target in targets
    }
    signals = tuple(
        ObservedSignal(
            base_scheme_id=str(row["scheme_id"]),
            target_tenor=str(row["target_tenor"]),
            horizon=int(row["horizon"]),
            target_date=str(row["target_date"])[:10],
            predict_date=str(row["predict_date"])[:10],
            feature_date=str(row["feature_date"])[:10],
            phase=str(row["prediction_phase"]),
            scheme_version=str(row["scheme_version"] or ""),
            run_status=str(row["run_status"] or ""),
            contract_error=_live_row_contract_error(
                row,
                target_by_identity.get(
                    (
                        str(row["scheme_id"]),
                        str(row["target_tenor"]),
                        int(row["horizon"]),
                    )
                ),
            ),
        )
        for row in rows
    )
    return rows, signals


def _read_canonical_actual_facts(
    connection: Any,
    cases: Sequence[ExpectedSignalCase],
) -> tuple[tuple[ExpectedSignalCase, ...], dict[str, int]]:
    """从同一 connection 读取 Actual，仅校验而不枚举 expected。"""
    if not cases:
        return (), {
            "daily_actual_fact_count": 0,
            "weekly_actual_fact_count": 0,
            "monthly_actual_fact_count": 0,
        }
    tenors = sorted({case.target_tenor for case in cases})
    min_target_date = min(case.target_date for case in cases)
    max_target_date = max(case.target_date for case in cases)
    daily_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT tenor, trade_date, direction_1d, direction_5d
                FROM t_scheme_actuals
                WHERE tenor IN :tenors
                  AND trade_date BETWEEN :start_date AND :end_date
                ORDER BY tenor, trade_date
                """
            ).bindparams(bindparam("tenors", expanding=True)),
            {
                "tenors": tenors,
                "start_date": min_target_date,
                "end_date": max_target_date,
            },
        ).mappings().all()
    ]
    weekly_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT tenor, predict_date, feature_date, target_date,
                       direction_weekly, target_rule
                FROM t_scheme_weekly_actuals
                WHERE tenor IN :tenors
                  AND target_date BETWEEN :start_date AND :end_date
                ORDER BY tenor, predict_date, target_rule, target_date
                """
            ).bindparams(bindparam("tenors", expanding=True)),
            {
                "tenors": tenors,
                "start_date": min_target_date,
                "end_date": max_target_date,
            },
        ).mappings().all()
    ]
    monthly_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT tenor, predict_date, feature_date, target_date,
                       direction_monthly, target_rule
                FROM t_scheme_monthly_actuals
                WHERE tenor IN :tenors
                  AND target_date BETWEEN :start_date AND :end_date
                ORDER BY tenor, predict_date, target_rule, target_date
                """
            ).bindparams(bindparam("tenors", expanding=True)),
            {
                "tenors": tenors,
                "start_date": min_target_date,
                "end_date": max_target_date,
            },
        ).mappings().all()
    ]
    return (
        _validate_canonical_actual_facts(
            cases,
            daily_actual_rows=daily_rows,
            weekly_actual_rows=weekly_rows,
            monthly_actual_rows=monthly_rows,
        ),
        {
            "daily_actual_fact_count": len(daily_rows),
            "weekly_actual_fact_count": len(weekly_rows),
            "monthly_actual_fact_count": len(monthly_rows),
        },
    )


def _validate_canonical_actual_facts(
    cases: Sequence[ExpectedSignalCase],
    *,
    daily_actual_rows: Sequence[Mapping[str, Any]],
    weekly_actual_rows: Sequence[Mapping[str, Any]],
    monthly_actual_rows: Sequence[Mapping[str, Any]],
) -> tuple[ExpectedSignalCase, ...]:
    """将 Actual 契约错误附着到既有 Calendar authority case。"""
    daily = _group_rows(
        daily_actual_rows,
        lambda row: (
            str(row["tenor"]),
            str(row["trade_date"])[:10],
        ),
    )
    weekly = _group_rows(
        weekly_actual_rows,
        lambda row: (
            str(row["tenor"]),
            str(row["feature_date"])[:10],
            str(row["target_date"])[:10],
            str(row["target_rule"]),
        ),
    )
    monthly = _group_rows(
        monthly_actual_rows,
        lambda row: (
            str(row["tenor"]),
            str(row["predict_date"])[:10],
            str(row["target_rule"]),
        ),
    )
    validated: list[ExpectedSignalCase] = []
    for case in cases:
        error: str | None
        if case.frequency == "daily":
            rows = daily.get((case.target_tenor, case.target_date), ())
            direction_field = (
                "direction_1d"
                if case.task_type == "T+1"
                else "direction_5d"
            )
            error = _actual_fact_error(
                rows,
                prefix="DAILY_ACTUAL",
                expected_dates={
                    "trade_date": case.target_date,
                },
                direction_field=direction_field,
            )
        elif case.frequency == "weekly":
            target_rule = (
                WEEKLY_AVERAGE_TARGET_RULE
                if case.task_type == "weekly_average"
                else WEEKLY_TARGET_RULE
            )
            rows = weekly.get(
                (
                    case.target_tenor,
                    case.feature_date,
                    case.target_date,
                    target_rule,
                ),
                (),
            )
            error = _actual_fact_error(
                rows,
                prefix="WEEKLY_ACTUAL",
                expected_dates={
                    "feature_date": case.feature_date,
                    "target_date": case.target_date,
                },
                direction_field="direction_weekly",
            )
        else:
            rows = monthly.get(
                (
                    case.target_tenor,
                    case.predict_date,
                    MONTHLY_TARGET_RULE,
                ),
                (),
            )
            error = _actual_fact_error(
                rows,
                prefix="MONTHLY_ACTUAL",
                expected_dates={
                    "predict_date": case.predict_date,
                    "feature_date": case.feature_date,
                    "target_date": case.target_date,
                },
                direction_field="direction_monthly",
            )
        validated.append(
            ExpectedSignalCase(
                registry_scheme_id=case.registry_scheme_id,
                base_scheme_id=case.base_scheme_id,
                runtime_type=case.runtime_type,
                frequency=case.frequency,
                task_type=case.task_type,
                target_tenor=case.target_tenor,
                horizon=case.horizon,
                predict_date=case.predict_date,
                feature_date=case.feature_date,
                target_date=case.target_date,
                segment=case.segment,
                data_contract_error=error or case.data_contract_error,
            )
        )
    return tuple(sorted(validated, key=_case_sort_key))


def _calendar_canonical_authorities(
    cases: Sequence[ExpectedSignalCase],
) -> tuple[Mapping[str, Any], ...]:
    by_base: dict[str, list[ExpectedSignalCase]] = {}
    for case in cases:
        by_base.setdefault(case.base_scheme_id, []).append(case)
    return tuple(
        {
            "base_scheme_id": base_scheme_id,
            "mode": "calendar_weekday_actual_validated_v1",
            "status": (
                "BLOCKED"
                if any(
                    case.data_contract_error
                    for case in by_base[base_scheme_id]
                )
                else "VALID"
            ),
            "expected_count": len(by_base[base_scheme_id]),
            "digest_sha256": canonical_json_sha256(
                [
                    {
                        "registry_scheme_id": case.registry_scheme_id,
                        "business_key": list(case.business_key),
                        "predict_date": case.predict_date,
                        "feature_date": case.feature_date,
                        "data_contract_error":
                            case.data_contract_error,
                    }
                    for case in sorted(
                        by_base[base_scheme_id],
                        key=_case_sort_key,
                    )
                ]
            ),
        }
        for base_scheme_id in sorted(by_base)
    )


def _group_rows(
    rows: Sequence[Mapping[str, Any]],
    key_builder: Callable[[Mapping[str, Any]], tuple[Any, ...]],
) -> dict[tuple[Any, ...], tuple[Mapping[str, Any], ...]]:
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(key_builder(row), []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _actual_fact_error(
    rows: Sequence[Mapping[str, Any]],
    *,
    prefix: str,
    expected_dates: Mapping[str, str],
    direction_field: str,
) -> str | None:
    if not rows:
        return f"{prefix}_FACT_MISSING"
    if len(rows) != 1:
        return f"{prefix}_FACT_DUPLICATE"
    row = rows[0]
    if any(
        str(row.get(field) or "")[:10] != expected
        for field, expected in expected_dates.items()
    ):
        return f"{prefix}_DATE_CONTRACT_INVALID"
    direction = row.get(direction_field)
    if (
        isinstance(direction, bool)
        or direction is None
        or int(direction) not in {-1, 0, 1}
    ):
        return f"{prefix}_DIRECTION_CONTRACT_INVALID"
    return None


def _read_persisted_canonical_observations(
    connection: Any,
    raw_registry: Sequence[Mapping[str, Any]],
    targets: Sequence[RegistryTarget],
) -> tuple[
    tuple[ExpectedSignalCase, ...],
    tuple[ObservedSignal, ...],
    dict[str, int],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    runs = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT id, benchmark_id, scheme_id, data_source,
                       start_date, end_date, status, summary,
                       code_hash, config_hash, created_at, updated_at
                FROM t_backtest_runs
                WHERE scheme_id IN :scheme_ids
                ORDER BY scheme_id, data_source, benchmark_id,
                         updated_at, created_at, id
                """
            ).bindparams(bindparam("scheme_ids", expanding=True)),
            {
                "scheme_ids": sorted(
                    {target.base_scheme_id for target in targets}
                )
            },
        ).mappings().all()
    ]
    selected_by_registry = choose_latest_backtest_runs(
        runs,
        raw_registry,
    )
    unique_runs = {
        int(row["id"]): dict(row)
        for row in selected_by_registry.values()
    }
    details = []
    if unique_runs:
        details = [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT run_id, target_tenor, horizon, predict_date,
                           feature_date, target_date
                    FROM t_backtest_predictions
                    WHERE run_id IN :run_ids
                    ORDER BY run_id, target_tenor, horizon,
                             target_date, predict_date
                    """
                ).bindparams(bindparam("run_ids", expanding=True)),
                {"run_ids": sorted(unique_runs)},
            ).mappings().all()
        ]
    details_by_run: dict[int, list[dict[str, Any]]] = {}
    for row in details:
        details_by_run.setdefault(int(row["run_id"]), []).append(row)
    target_by_registry = {
        target.registry_scheme_id: target for target in targets
    }
    native_targets_by_base: dict[str, list[RegistryTarget]] = {}
    for target in targets:
        if target.runtime_type == "native_adapter":
            native_targets_by_base.setdefault(
                target.base_scheme_id,
                [],
            ).append(target)
    native_cases: list[ExpectedSignalCase] = []
    signals: list[ObservedSignal] = []
    blockers: list[Mapping[str, Any]] = []
    authorities: list[Mapping[str, Any]] = []
    for base_scheme_id, base_targets in sorted(
        native_targets_by_base.items()
    ):
        selected = {
            int(selected_by_registry[target.registry_scheme_id]["id"]):
                selected_by_registry[target.registry_scheme_id]
            for target in base_targets
            if target.registry_scheme_id in selected_by_registry
        }
        if len(selected) != 1 or any(
            target.registry_scheme_id not in selected_by_registry
            for target in base_targets
        ):
            blockers.append(
                {
                    "code": "NATIVE_CANONICAL_RUN_MISSING",
                    "base_scheme_id": base_scheme_id,
                    "segment_scope": ["canonical"],
                }
            )
            authorities.append(
                _blocked_native_authority(
                    base_scheme_id,
                    "NATIVE_CANONICAL_RUN_MISSING",
                )
            )
            continue
        run_id, run = next(iter(selected.items()))
        run_details = details_by_run.get(run_id, ())
        summary = _json_object(run.get("summary"))
        try:
            _validate_canonical_run_manifest(
                run_id=run_id,
                summary=summary,
                details=run_details,
                expected_target_pairs={
                    (target.target_tenor, target.horizon)
                    for target in base_targets
                },
            )
        except SignalGapPlanError:
            blockers.append(
                {
                    "code": "NATIVE_CANONICAL_MANIFEST_INVALID",
                    "base_scheme_id": base_scheme_id,
                    "run_id": run_id,
                    "segment_scope": ["canonical"],
                }
            )
            authorities.append(
                _blocked_native_authority(
                    base_scheme_id,
                    "NATIVE_CANONICAL_MANIFEST_INVALID",
                    run_id=run_id,
                    summary=summary,
                    details=run_details,
                )
            )
            continue
        version_error = next(
            (
                error
                for error in (
                    _canonical_run_version_error(
                        target,
                        run,
                        summary,
                    )
                    for target in base_targets
                )
                if error is not None
            ),
            None,
        )
        if version_error is not None:
            blockers.append(
                {
                    "code": "NATIVE_CANONICAL_VERSION_IDENTITY_INVALID",
                    "base_scheme_id": base_scheme_id,
                    "run_id": run_id,
                    "reason": version_error,
                    "segment_scope": ["canonical"],
                }
            )
        authorities.append(
            _native_manifest_authority(
                base_scheme_id,
                run_id=run_id,
                run=run,
                summary=summary,
                details=run_details,
                failure_code=version_error,
            )
        )
        persisted_scheme_version = (
            _optional_text(summary.get("scheme_version")) or ""
        )
        for target in sorted(
            base_targets,
            key=lambda item: item.registry_scheme_id,
        ):
            for row in run_details:
                if (
                    str(row["target_tenor"]),
                    int(row["horizon"]),
                ) != (target.target_tenor, target.horizon):
                    continue
                case = _case_from_target(
                    target,
                    predict_date=str(row["predict_date"])[:10],
                    feature_date=str(row["feature_date"])[:10],
                    target_date=str(row["target_date"])[:10],
                    segment="canonical",
                )
                native_cases.append(case)
                signals.append(
                    _canonical_observation_from_case(
                        case,
                        run_status=str(run["status"]),
                        scheme_version=persisted_scheme_version,
                        contract_error=version_error,
                    )
                )
    for registry_id, run in sorted(selected_by_registry.items()):
        target = target_by_registry[registry_id]
        if target.runtime_type != "blackbox_v2":
            continue
        run_details = [
            row
            for row in details_by_run.get(int(run["id"]), ())
            if (
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            == (target.target_tenor, target.horizon)
        ]
        summary = _json_object(run.get("summary"))
        version_error = _canonical_run_version_error(
            target,
            run,
            summary,
        )
        for row in run_details:
            signals.append(
                ObservedSignal(
                    base_scheme_id=target.base_scheme_id,
                    target_tenor=target.target_tenor,
                    horizon=target.horizon,
                    target_date=str(row["target_date"])[:10],
                    predict_date=str(row["predict_date"])[:10],
                    feature_date=str(row["feature_date"])[:10],
                    phase="canonical",
                    scheme_version=target.scheme_version,
                    run_status=str(run["status"]),
                    contract_error=version_error,
                )
            )
    return (
        tuple(sorted(native_cases, key=_case_sort_key)),
        tuple(sorted(signals, key=_observed_sort_key)),
        {
            "candidate_backtest_run_count": len(runs),
            "selected_backtest_run_count": len(unique_runs),
            "selected_backtest_detail_count": len(details),
        },
        tuple(blockers),
        tuple(authorities),
    )


def _native_manifest_authority(
    base_scheme_id: str,
    *,
    run_id: int,
    run: Mapping[str, Any],
    summary: Mapping[str, Any],
    details: Sequence[Mapping[str, Any]],
    failure_code: str | None,
) -> Mapping[str, Any]:
    payload = {
        "run_id": run_id,
        "run_identity": {
            "benchmark_id": str(run.get("benchmark_id") or ""),
            "scheme_id": str(run.get("scheme_id") or ""),
            "data_source": str(run.get("data_source") or ""),
            "code_hash": str(run.get("code_hash") or ""),
            "config_hash": str(run.get("config_hash") or ""),
        },
        "summary": dict(summary),
        "canonical_keys": [
            {
                "target_tenor": str(row["target_tenor"]),
                "horizon": int(row["horizon"]),
                "predict_date": str(row["predict_date"])[:10],
                "feature_date": str(row["feature_date"])[:10],
                "target_date": str(row["target_date"])[:10],
            }
            for row in sorted(
                details,
                key=lambda item: (
                    str(item["target_tenor"]),
                    int(item["horizon"]),
                    str(item["target_date"])[:10],
                    str(item["predict_date"])[:10],
                ),
            )
        ],
    }
    return {
        "base_scheme_id": base_scheme_id,
        "mode": "persisted_source_run_manifest_v1",
        "status": "BLOCKED" if failure_code else "VALID",
        "failure_code": failure_code,
        "run_id": run_id,
        "expected_count": len(details),
        "digest_sha256": canonical_json_sha256(payload),
    }


def _blocked_native_authority(
    base_scheme_id: str,
    failure_code: str,
    *,
    run_id: int | None = None,
    summary: Mapping[str, Any] | None = None,
    details: Sequence[Mapping[str, Any]] = (),
) -> Mapping[str, Any]:
    payload = {
        "failure_code": failure_code,
        "run_id": run_id,
        "summary": dict(summary or {}),
        "details": [
            {
                "target_tenor": str(row["target_tenor"]),
                "horizon": int(row["horizon"]),
                "predict_date": str(row["predict_date"])[:10],
                "feature_date": str(row["feature_date"])[:10],
                "target_date": str(row["target_date"])[:10],
            }
            for row in details
        ],
    }
    return {
        "base_scheme_id": base_scheme_id,
        "mode": "persisted_source_run_manifest_v1",
        "status": "BLOCKED",
        "failure_code": failure_code,
        "run_id": run_id,
        "expected_count": 0,
        "digest_sha256": canonical_json_sha256(payload),
    }


def _canonical_observation_from_case(
    case: ExpectedSignalCase,
    *,
    run_status: str,
    scheme_version: str,
    contract_error: str | None,
) -> ObservedSignal:
    return ObservedSignal(
        base_scheme_id=case.base_scheme_id,
        target_tenor=case.target_tenor,
        horizon=case.horizon,
        target_date=case.target_date,
        predict_date=case.predict_date,
        feature_date=case.feature_date,
        phase="canonical",
        scheme_version=scheme_version,
        run_status=run_status,
        contract_error=contract_error,
    )


def _read_input_generations(
    connection: Any,
) -> tuple[InputGeneration, ...]:
    rows = connection.execute(
        text(
            """
            SELECT generation_id, generation_type, business_date,
                   feature_date, readiness_basis, source_commit_token,
                   dataset_content_id, schema_version, exporter_version,
                   manifest_uri, manifest_sha256, native_generation_id,
                   native_manifest_sha256, state, sealed_at
            FROM t_input_generations
            ORDER BY generation_type, feature_date, business_date,
                     generation_id
            """
        )
    ).mappings().all()
    return tuple(
        InputGeneration(
            generation_id=str(row["generation_id"]),
            generation_type=str(row["generation_type"]),
            business_date=str(row["business_date"])[:10],
            feature_date=str(row["feature_date"])[:10],
            readiness_basis=str(row["readiness_basis"] or ""),
            source_commit_token=str(
                row["source_commit_token"] or ""
            ),
            dataset_content_id=str(
                row["dataset_content_id"] or ""
            ),
            schema_version=str(row["schema_version"] or ""),
            exporter_version=str(row["exporter_version"] or ""),
            manifest_uri=str(row["manifest_uri"] or ""),
            manifest_sha256=str(row["manifest_sha256"] or ""),
            native_generation_id=_optional_text(
                row.get("native_generation_id")
            ),
            native_manifest_sha256=_optional_text(
                row.get("native_manifest_sha256")
            ),
            state=str(row["state"]),
            sealed_at=_optional_datetime_text(
                row.get("sealed_at")
            ),
        )
        for row in rows
    )


def _resolve_action(
    item: ExpectedSignalCase,
    *,
    target: RegistryTarget,
    control_plane_error: str | None,
    valid_present: bool,
    observation_error: str | None,
    generations: Sequence[InputGeneration],
    databridge_authority: StableDataBridgeCurrentAuthority | None,
    databridge_authority_error: str | None,
    native_artifact_verifier: Callable[
        [InputGeneration],
        tuple[Mapping[str, Any] | None, str | None],
    ],
) -> tuple[Action, Mapping[str, Any] | None, str]:
    if item.data_contract_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            item.data_contract_error,
        )
    if valid_present:
        return "SKIP_PRESENT", None, "BUSINESS_KEY_PRESENT"
    if observation_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            observation_error,
        )
    if control_plane_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            f"CONTROL_PLANE_BLOCKER:{control_plane_error}",
        )
    if item.segment == "canonical":
        return (
            "FULL_CANONICAL_RUN_REQUIRED",
            None,
            "CANONICAL_BUSINESS_KEY_MISSING",
        )
    if target.input_mode == "live_source_0629" and not _is_sha256(
        str(target.source_package_sha256 or "")
    ):
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "LIVE_SOURCE_0629_ATTESTATION_REQUIRED",
        )
    if target.runtime_type == "blackbox_v2":
        return _blackbox_generation_eligibility(
            item,
            authority=databridge_authority,
            authority_error=databridge_authority_error,
        )
    return _native_generation_eligibility(
        item,
        generations,
        artifact_verifier=native_artifact_verifier,
    )


def _blackbox_generation_eligibility(
    item: ExpectedSignalCase,
    *,
    authority: StableDataBridgeCurrentAuthority | None,
    authority_error: str | None,
) -> tuple[Action, Mapping[str, Any] | None, str]:
    if authority_error == "MISSING" or (
        authority is None and authority_error is None
    ):
        return (
            "BLOCKED_NO_GENERATION",
            None,
            "NO_DATABRIDGE_CURRENT",
        )
    if authority_error == "INVALID" or authority is None:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "DATABRIDGE_CURRENT_INVALID",
        )
    try:
        selected = _databridge_authority_payload(
            authority,
            feature_date=item.feature_date,
        )
    except (SignalGapPlanError, ValueError):
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "DATABRIDGE_CURRENT_INVALID",
        )
    if item.predict_date >= authority.refresh_date:
        return (
            "BLOCKED_NO_GENERATION",
            selected,
            "DATABRIDGE_CURRENT_REFRESH_REQUIRED",
        )
    if (
        selected["cutoff"]["daily_cutoff_key"]
        != item.feature_date
    ):
        return (
            "BLOCKED_DATA_CONTRACT",
            selected,
            "DATABRIDGE_CUTOFF_AUTHORITY_INVALID",
        )
    return "GRAY_LIVE_GAP", selected, "LIVE_BUSINESS_KEY_MISSING"


def _native_generation_eligibility(
    item: ExpectedSignalCase,
    generations: Sequence[InputGeneration],
    *,
    artifact_verifier: Callable[
        [InputGeneration],
        tuple[Mapping[str, Any] | None, str | None],
    ],
) -> tuple[Action, Mapping[str, Any] | None, str]:
    feature_scoped = [
        row
        for row in generations
        if row.generation_type == "native_source"
        and row.feature_date == item.feature_date
    ]
    normal_candidates = [
        row
        for row in feature_scoped
        if row.exporter_version
        == NATIVE_GENERATION_EXPORTER_VERSION
        and row.business_date == item.predict_date
    ]
    if normal_candidates:
        if len(normal_candidates) != 1:
            return (
                "BLOCKED_DATA_CONTRACT",
                None,
                "DUPLICATE_EXACT_NATIVE_GENERATION",
            )
        return _verified_native_generation_eligibility(
            normal_candidates[0],
            item=item,
            expected_exporter_version=(
                NATIVE_GENERATION_EXPORTER_VERSION
            ),
            require_later_business_date=False,
            artifact_verifier=artifact_verifier,
        )

    snapshot_candidates = [
        row
        for row in feature_scoped
        if row.exporter_version
        == SIGNAL_GAP_NATIVE_EXPORTER_VERSION
        and row.business_date > item.predict_date
    ]
    if not snapshot_candidates:
        if any(
            row.business_date > item.predict_date
            for row in feature_scoped
        ):
            return (
                "BLOCKED_DATA_CONTRACT",
                None,
                "GENERATION_CONTRACT_INVALID",
            )
        return (
            "BLOCKED_NO_GENERATION",
            None,
            "NO_EXACT_NATIVE_GENERATION",
        )
    if len(snapshot_candidates) != 1:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "DUPLICATE_EXACT_NATIVE_GENERATION",
        )
    return _verified_native_generation_eligibility(
        snapshot_candidates[0],
        item=item,
        expected_exporter_version=(
            SIGNAL_GAP_NATIVE_EXPORTER_VERSION
        ),
        require_later_business_date=True,
        artifact_verifier=artifact_verifier,
    )


def _verified_native_generation_eligibility(
    selected: InputGeneration,
    *,
    item: ExpectedSignalCase,
    expected_exporter_version: str,
    require_later_business_date: bool,
    artifact_verifier: Callable[
        [InputGeneration],
        tuple[Mapping[str, Any] | None, str | None],
    ],
) -> tuple[Action, Mapping[str, Any] | None, str]:
    if not _valid_native_generation_fence(
        selected,
        expected_predict_date=item.predict_date,
        expected_feature_date=item.feature_date,
        expected_exporter_version=expected_exporter_version,
        require_later_business_date=require_later_business_date,
    ):
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "GENERATION_CONTRACT_INVALID",
        )
    authority, failure_code = artifact_verifier(selected)
    if failure_code is not None:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            failure_code,
        )
    return (
        "GRAY_LIVE_GAP",
        authority,
        "LIVE_BUSINESS_KEY_MISSING",
    )


def _valid_native_generation_fence(
    generation: InputGeneration,
    *,
    expected_predict_date: str,
    expected_feature_date: str,
    expected_exporter_version: str,
    require_later_business_date: bool,
) -> bool:
    try:
        business_date = _canonical_date(
            generation.business_date,
            "generation.business_date",
        )
        feature_date = _canonical_date(
            generation.feature_date,
            "generation.feature_date",
        )
        sealed_at = datetime.fromisoformat(
            str(generation.sealed_at)
        )
    except (SignalGapPlanError, TypeError, ValueError):
        return False
    return (
        _NATIVE_GENERATION_ID_PATTERN.fullmatch(
            generation.generation_id
        )
        is not None
        and generation.generation_type == "native_source"
        and business_date == generation.business_date
        and feature_date == generation.feature_date
        and (
            business_date > expected_predict_date
            if require_later_business_date
            else business_date == expected_predict_date
        )
        and feature_date == expected_feature_date
        and generation.readiness_basis == "CLOCK_CONTRACT"
        and _is_sha256(generation.source_commit_token)
        and _is_sha256(generation.dataset_content_id)
        and bool(generation.schema_version.strip())
        and generation.exporter_version
        == expected_exporter_version
        and Path(generation.manifest_uri).is_absolute()
        and Path(generation.manifest_uri).name == "manifest.json"
        and _is_sha256(generation.manifest_sha256)
        and generation.native_generation_id is None
        and generation.native_manifest_sha256 is None
        and generation.state == "SEALED"
        and sealed_at.isoformat(timespec="microseconds")
        == str(generation.sealed_at)
    )


def _input_generation_fingerprint(
    generation: InputGeneration,
) -> tuple[Any, ...]:
    return tuple(
        getattr(generation, field)
        for field in _INPUT_GENERATION_FINGERPRINT_FIELDS
    )


def _input_generation_authority(
    generation: InputGeneration,
) -> dict[str, Any]:
    return {
        "generation_id": generation.generation_id,
        "generation_type": generation.generation_type,
        "business_date": generation.business_date,
        "feature_date": generation.feature_date,
        "readiness_basis": generation.readiness_basis,
        "source_commit_token": generation.source_commit_token,
        "dataset_content_id": generation.dataset_content_id,
        "schema_version": generation.schema_version,
        "exporter_version": generation.exporter_version,
        "manifest_uri": generation.manifest_uri,
        "manifest_sha256": generation.manifest_sha256,
        "native_generation_id": generation.native_generation_id,
        "native_manifest_sha256":
            generation.native_manifest_sha256,
        "state": generation.state,
        "sealed_at": generation.sealed_at,
    }


def _databridge_authority_payload(
    authority: StableDataBridgeCurrentAuthority,
    *,
    feature_date: str,
) -> dict[str, Any]:
    normalized_feature_date = _canonical_date(
        feature_date,
        "databridge feature_date",
    )
    refresh_date = _canonical_date(
        authority.refresh_date,
        "databridge refresh_date",
    )
    if (
        authority.authority_schema_version
        != "stable-databridge-current-authority-v1"
        or not authority.generation_id.strip()
        or not authority.schema_version.strip()
        or not _is_sha256(authority.business_digest)
        or not _is_sha256(authority.stable_identity_sha256)
    ):
        raise ValueError("DataBridge current authority fence is invalid")
    files = sorted(
        authority.files,
        key=lambda row: row.filename,
    )
    if (
        {row.filename for row in files}
        != {
            "daily_output.csv",
            "weekly_output.csv",
            "monthly_output.csv",
        }
        or len(files) != 3
    ):
        raise ValueError("DataBridge current file authority is invalid")
    file_payload = []
    for row in files:
        if (
            row.rows < 0
            or row.columns <= 0
            or not row.min_key
            or not row.max_key
            or not _is_sha256(row.sha256)
            or not _is_sha256(row.business_hash)
        ):
            raise ValueError(
                "DataBridge current file authority is invalid"
            )
        file_payload.append(
            {
                "filename": row.filename,
                "rows": row.rows,
                "columns": row.columns,
                "min_key": row.min_key,
                "max_key": row.max_key,
                "sha256": row.sha256,
                "business_hash": row.business_hash,
            }
        )
    matching_cutoffs = [
        row
        for row in authority.cutoffs
        if row.feature_date == normalized_feature_date
    ]
    if len(matching_cutoffs) != 1:
        raise ValueError(
            "DataBridge current cutoff authority is not unique"
        )
    cutoff = matching_cutoffs[0]
    _validate_databridge_cutoff(cutoff)
    capability_payload = None
    if authority.publication_capability is not None:
        capability = authority.publication_capability
        if (
            capability.occurrence_id <= 0
            or _canonical_date(
                capability.business_date,
                "publication business_date",
            )
            != capability.business_date
            or capability.epoch <= 0
            or capability.mode != "ledger"
            or not _is_sha256(capability.record_sha256)
        ):
            raise ValueError(
                "DataBridge publication capability is invalid"
            )
        capability_payload = {
            "occurrence_id": capability.occurrence_id,
            "business_date": capability.business_date,
            "daily_coordinator_epoch": {
                "epoch": capability.epoch,
                "mode": capability.mode,
                "record_sha256": capability.record_sha256,
            },
        }
    return {
        "authority_type": "stable_databridge_current",
        "authority_schema_version":
            authority.authority_schema_version,
        "stable_identity_sha256":
            authority.stable_identity_sha256,
        "generation_id": authority.generation_id,
        "refresh_date": refresh_date,
        "schema_version": authority.schema_version,
        "business_digest": authority.business_digest,
        "publication_capability": capability_payload,
        "files": file_payload,
        "cutoff": {
            "feature_date": cutoff.feature_date,
            "daily_cutoff_key": cutoff.daily_cutoff_key,
            "weekly_cutoff_key": cutoff.weekly_cutoff_key,
            "monthly_cutoff_key": cutoff.monthly_cutoff_key,
        },
    }


def _validate_databridge_cutoff(
    cutoff: StableDataBridgeCutoff,
) -> None:
    if (
        _canonical_date(
            cutoff.feature_date,
            "cutoff.feature_date",
        )
        != cutoff.feature_date
        or _canonical_date(
            cutoff.daily_cutoff_key,
            "cutoff.daily_cutoff_key",
        )
        != cutoff.daily_cutoff_key
        or len(cutoff.weekly_cutoff_key) != 6
        or not cutoff.weekly_cutoff_key.isdigit()
        or len(cutoff.monthly_cutoff_key) != 6
        or not cutoff.monthly_cutoff_key.isdigit()
    ):
        raise ValueError("DataBridge cutoff authority is invalid")


def _validate_snapshot(
    snapshot: SignalGapSnapshot,
) -> dict[str, RegistryTarget]:
    if not _is_sha256(snapshot.source_identity_sha256):
        raise SignalGapPlanError(
            "SOURCE_IDENTITY_INVALID",
            "source identity must be a SHA-256 digest",
        )
    if not _is_sha256(snapshot.discovery_identity_sha256):
        raise SignalGapPlanError(
            "DISCOVERY_IDENTITY_INVALID",
            "discovery identity must be a SHA-256 digest",
        )
    if not _is_sha256(snapshot.active_version_identity_sha256):
        raise SignalGapPlanError(
            "ACTIVE_VERSION_IDENTITY_INVALID",
            "active version identity must be a SHA-256 digest",
        )
    by_registry: dict[str, RegistryTarget] = {}
    seen_business: set[tuple[str, str, int]] = set()
    for target in sorted(
        snapshot.registry_targets,
        key=lambda row: row.registry_scheme_id,
    ):
        _validate_registry_target(target)
        if target.business_identity in seen_business:
            raise SignalGapPlanError(
                "DUPLICATE_ACTIVE_BUSINESS_TARGET",
                f"duplicate active business target "
                f"{target.business_identity}",
            )
        if target.registry_scheme_id in by_registry:
            raise SignalGapPlanError(
                "DUPLICATE_ACTIVE_REGISTRY_ID",
                target.registry_scheme_id,
            )
        seen_business.add(target.business_identity)
        by_registry[target.registry_scheme_id] = target
    seen_cases: set[tuple[str, str, int, str, str]] = set()
    for item in snapshot.expected_cases:
        if item.segment not in {"canonical", "live"}:
            raise SignalGapPlanError(
                "INVALID_EXPECTED_SEGMENT",
                f"{item.registry_scheme_id}:{item.segment}",
            )
        key = (*item.business_key, item.segment)
        if key in seen_cases:
            raise SignalGapPlanError(
                "DUPLICATE_EXPECTED_CASE",
                str(key),
            )
        seen_cases.add(key)
    return by_registry


def _validate_registry_target(target: RegistryTarget) -> None:
    if target.runtime_type not in {"native_adapter", "blackbox_v2"}:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            f"unsupported runtime_type={target.runtime_type!r}",
        )
    expected = (
        NATIVE_TASK_COMBINATIONS.get(target.task_type)
        if target.runtime_type == "native_adapter"
        else _blackbox_task_contract(target.task_type)
    )
    if expected is None:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            f"unsupported task_type={target.task_type!r}",
        )
    expected_horizon, expected_frequency = expected
    if (
        target.horizon != expected_horizon
        or target.frequency != expected_frequency
    ):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            f"task contract drift for {target.registry_scheme_id}",
        )
    expected_registry_id = (
        f"{target.base_scheme_id}__h{target.horizon}__"
        f"{target.target_tenor}"
    )
    if target.registry_scheme_id != expected_registry_id:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            f"registry composite identity drift "
            f"expected={expected_registry_id}",
        )
    if not target.scheme_version.strip():
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "active target has empty scheme_version",
        )
    if not _is_sha256(target.code_sha256 or ""):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "active target has invalid code SHA-256",
        )
    if not _is_sha256(target.config_sha256 or ""):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "active target has invalid config SHA-256",
        )
    if target.input_mode not in {
        "generation_v1",
        "live_source_0629",
        "databridge_v1",
    }:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "active target has invalid input_mode",
        )
    if (
        target.input_mode == "live_source_0629"
        and not _is_sha256(
            str(target.source_package_sha256 or "")
        )
    ):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "live_source_0629 target has no package SHA-256",
        )
    if (
        target.input_mode != "live_source_0629"
        and target.source_package_sha256 is not None
    ):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            "source package SHA-256 is only valid for live_source_0629",
        )
    _canonical_date(
        target.live_target_start_date,
        "live_target_start_date",
    )
    if not target.live_boundary_source.strip():
        raise SignalGapPlanError(
            "LIVE_BOUNDARY_AUTHORITY_UNAVAILABLE",
            target.registry_scheme_id,
        )
    if (
        target.live_boundary_source != PLATFORM_LIVE_BOUNDARY_VERSION
        or target.live_target_start_date
        != PLATFORM_LIVE_TARGET_START_DATE
    ):
        raise SignalGapPlanError(
            "LIVE_BOUNDARY_AUTHORITY_INVALID",
            target.registry_scheme_id,
        )


def _observation_contract_error(
    item: ExpectedSignalCase,
    target: RegistryTarget,
    *,
    observed: Sequence[ObservedSignal],
    opposite: Sequence[ObservedSignal],
) -> str | None:
    if opposite:
        return "OBSERVED_SIGNAL_SEGMENT_OVERLAP"
    if len(observed) > 1:
        return "DUPLICATE_OBSERVED_BUSINESS_KEY"
    if not observed:
        return None
    row = observed[0]
    expected_phase = (
        {"canonical"}
        if item.segment == "canonical"
        else {"gray_live", "scheduled_live"}
    )
    if (
        row.contract_error
        or row.predict_date != item.predict_date
        or row.feature_date != item.feature_date
        or row.phase not in expected_phase
        or row.run_status != "success"
        or row.scheme_version != target.scheme_version
    ):
        return (
            "OBSERVED_SIGNAL_CONTRACT_DRIFT"
            + (
                f":{row.contract_error}"
                if row.contract_error
                else ""
            )
        )
    return None


def _index_observations(
    rows: Sequence[ObservedSignal],
) -> dict[tuple[str, str, int, str], tuple[ObservedSignal, ...]]:
    grouped: dict[
        tuple[str, str, int, str],
        list[ObservedSignal],
    ] = {}
    for row in rows:
        _canonical_date(row.predict_date, "observed.predict_date")
        _canonical_date(row.feature_date, "observed.feature_date")
        _canonical_date(row.target_date, "observed.target_date")
        grouped.setdefault(row.business_key, []).append(row)
    return {
        key: tuple(sorted(value, key=_observed_sort_key))
        for key, value in grouped.items()
    }


def _observations_in_scope(
    rows: Sequence[ObservedSignal],
    *,
    expected_business_keys: set[tuple[str, str, int, str]],
    start_date: str,
    as_of_date: str,
) -> tuple[ObservedSignal, ...]:
    return tuple(
        row
        for row in rows
        if row.business_key in expected_business_keys
        or start_date <= row.predict_date <= as_of_date
    )


def _expected_business_keys_for_date_scope(
    cases: Sequence[ExpectedSignalCase],
    *,
    start_date: str,
    as_of_date: str,
) -> set[tuple[str, str, int, str]]:
    normalized_start = _canonical_date(start_date, "start_date")
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    return {
        case.business_key
        for case in cases
        if normalized_start <= case.predict_date <= normalized_as_of
    }


def _validate_case_matches_target(
    item: ExpectedSignalCase,
    target: RegistryTarget,
) -> None:
    actual = (
        item.base_scheme_id,
        item.runtime_type,
        item.frequency,
        item.task_type,
        item.target_tenor,
        item.horizon,
    )
    expected = (
        target.base_scheme_id,
        target.runtime_type,
        target.frequency,
        target.task_type,
        target.target_tenor,
        target.horizon,
    )
    if actual != expected:
        raise SignalGapPlanError(
            "CASE_REGISTRY_DRIFT",
            item.registry_scheme_id,
        )
    for field, value in (
        ("predict_date", item.predict_date),
        ("feature_date", item.feature_date),
        ("target_date", item.target_date),
    ):
        _canonical_date(value, f"case.{field}")
    if (
        item.segment == "canonical"
        and item.task_type != "monthly"
        and item.predict_date != item.feature_date
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            f"{item.task_type} canonical predict_date must equal feature_date",
        )
    if (
        item.segment == "canonical"
        and item.task_type == "monthly"
        and not item.predict_date.endswith("-15")
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            "monthly canonical predict_date must be natural month 15",
        )
    if not (
        item.feature_date <= item.predict_date <= item.target_date
        and item.feature_date < item.target_date
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            "case dates violate feature/predict/target ordering",
        )
    if (
        item.segment == "canonical"
        and item.target_date >= target.live_target_start_date
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            "canonical target_date crosses live boundary",
        )
    if (
        item.segment == "live"
        and item.target_date < target.live_target_start_date
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            "live target_date precedes live boundary",
        )


def _action_row(
    item: ExpectedSignalCase,
    *,
    target: RegistryTarget,
    action: Action,
    reason: str,
    input_authority: Mapping[str, Any] | None,
    business_key_present: bool,
) -> dict[str, Any]:
    return {
        "registry_scheme_id": item.registry_scheme_id,
        "base_scheme_id": item.base_scheme_id,
        "runtime_type": item.runtime_type,
        "frequency": item.frequency,
        "task_type": item.task_type,
        "target_tenor": item.target_tenor,
        "horizon": item.horizon,
        "predict_date": item.predict_date,
        "feature_date": item.feature_date,
        "target_date": item.target_date,
        "segment": item.segment,
        "prediction_phase": (
            "gray_live"
            if item.segment == "live"
            else "historical_backtest"
        ),
        "scheme_version": target.scheme_version,
        "code_sha256": target.code_sha256,
        "config_sha256": target.config_sha256,
        "input_mode": target.input_mode,
        "source_package_sha256": target.source_package_sha256,
        "business_key": [
            item.base_scheme_id,
            item.target_tenor,
            item.horizon,
            item.target_date,
        ],
        "action": action,
        "reason": reason,
        "business_key_present": business_key_present,
        "rebuild_group_id": (
            item.base_scheme_id
            if action == "FULL_CANONICAL_RUN_REQUIRED"
            else None
        ),
        "input_authority": (
            dict(input_authority)
            if input_authority is not None
            else None
        ),
    }


def _canonical_rebuild_groups(
    actions: Sequence[Mapping[str, Any]],
    targets: Sequence[RegistryTarget],
) -> list[dict[str, Any]]:
    missing_by_base: dict[str, list[list[Any]]] = {}
    for row in actions:
        if row["action"] != "FULL_CANONICAL_RUN_REQUIRED":
            continue
        missing_by_base.setdefault(
            str(row["base_scheme_id"]),
            [],
        ).append(list(row["business_key"]))
    registry_ids_by_base: dict[str, list[str]] = {}
    for target in targets:
        registry_ids_by_base.setdefault(
            target.base_scheme_id,
            [],
        ).append(target.registry_scheme_id)
    return [
        {
            "base_scheme_id": base_scheme_id,
            "persistence_mode": "FULL_RUN_ONLY",
            "registry_scheme_ids": sorted(
                registry_ids_by_base[base_scheme_id]
            ),
            "missing_business_keys": sorted(missing_by_base[base_scheme_id]),
        }
        for base_scheme_id in sorted(missing_by_base)
    ]


def _case_from_target(
    target: RegistryTarget,
    *,
    predict_date: str,
    feature_date: str,
    target_date: str,
    segment: Segment,
) -> ExpectedSignalCase:
    return ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date=_canonical_date(predict_date, "predict_date"),
        feature_date=_canonical_date(feature_date, "feature_date"),
        target_date=_canonical_date(target_date, "target_date"),
        segment=segment,
    )


def _canonical_run_version_error(
    target: RegistryTarget,
    run: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str | None:
    summary_version = _optional_text(summary.get("scheme_version"))
    if (
        summary_version is not None
        and summary_version != target.scheme_version
    ):
        return "CANONICAL_SCHEME_VERSION_DRIFT"
    if (
        target.code_sha256
        and target.config_sha256
        and (
            str(run.get("code_hash") or "") != target.code_sha256
            or str(run.get("config_hash") or "")
            != target.config_sha256
        )
    ):
        return "CANONICAL_VERSION_DIGEST_DRIFT"
    if (
        summary_version is None
        and not (
            target.code_sha256
            and target.config_sha256
        )
    ):
        return "CANONICAL_VERSION_AUTHORITY_UNAVAILABLE"
    return None


def _validate_canonical_run_manifest(
    *,
    run_id: int,
    summary: Mapping[str, Any],
    details: Sequence[Mapping[str, Any]],
    expected_target_pairs: set[tuple[str, int]],
) -> None:
    """校验 persisted run manifest 的总数和 target 多重性。"""
    count_fields = (
        "persisted_prediction_count",
        "row_count",
        "written_predictions",
        "rows",
    )
    declared_counts = [
        summary[field] for field in count_fields if field in summary
    ]
    if not declared_counts or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        for value in declared_counts
    ):
        raise SignalGapPlanError(
            "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
            f"run={run_id} manifest/detail count mismatch",
        )
    if len(set(declared_counts)) != 1:
        raise SignalGapPlanError(
            "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
            f"run={run_id} manifest count fields disagree",
        )
    if declared_counts[0] != len(details):
        raise SignalGapPlanError(
            "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
            f"run={run_id} manifest/detail count mismatch",
        )
    actual_target_pairs = {
        (str(row["target_tenor"]), int(row["horizon"]))
        for row in details
    }
    if actual_target_pairs != expected_target_pairs:
        raise SignalGapPlanError(
            "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
            f"run={run_id} target multiplicity mismatch",
        )


def _json_object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise SignalGapPlanError(
            "CANONICAL_RUN_MANIFEST_INVALID",
            "backtest summary is not valid JSON",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise SignalGapPlanError(
            "CANONICAL_RUN_MANIFEST_INVALID",
            "backtest summary must be an object",
        )
    return parsed


def _case_sort_key(item: ExpectedSignalCase) -> tuple[Any, ...]:
    return (
        item.registry_scheme_id,
        item.segment,
        item.target_date,
        item.predict_date,
        item.feature_date,
    )


def _observed_sort_key(item: ObservedSignal) -> tuple[Any, ...]:
    return (
        item.base_scheme_id,
        item.target_tenor,
        item.horizon,
        item.target_date,
        item.predict_date,
        item.feature_date,
        item.phase,
    )


def _monthly_trigger_dates(
    start_date: str,
    as_of_date: str,
) -> tuple[str, ...]:
    current = date.fromisoformat(start_date).replace(day=15)
    if current < date.fromisoformat(start_date):
        current = (
            date(current.year + 1, 1, 15)
            if current.month == 12
            else date(current.year, current.month + 1, 15)
        )
    end = date.fromisoformat(as_of_date)
    result: list[str] = []
    while current <= end:
        result.append(current.isoformat())
        current = (
            date(current.year + 1, 1, 15)
            if current.month == 12
            else date(current.year, current.month + 1, 15)
        )
    return tuple(result)


def _canonical_date(value: Any, field: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise SignalGapPlanError(
            "INVALID_DATE",
            f"{field} must use canonical YYYY-MM-DD",
        ) from exc
    if parsed.isoformat() != str(value):
        raise SignalGapPlanError(
            "INVALID_DATE",
            f"{field} must use canonical YYYY-MM-DD",
        )
    return parsed.isoformat()


def _optional_text(value: Any) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _optional_datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value))
        )
    except (TypeError, ValueError):
        return str(value)
    return parsed.isoformat(timespec="microseconds")


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _blackbox_task_contract(
    task_type: str,
) -> tuple[int, str] | None:
    contract = TASK_COMBINATIONS.get(task_type)
    if contract is None:
        return None
    horizon, _, frequency = contract
    return horizon, frequency


def _discover_execution_authority(
) -> tuple[DiscoveredSchemeIdentity, ...]:
    discovered = tuple(discover_schemes(strict=True))
    source_packages = _load_0629_source_packages()
    identities = tuple(
        DiscoveredSchemeIdentity(
            base_scheme_id=str(config.scheme_id),
            scheme_version=str(config.scheme_version),
            runtime_type=str(config.runtime_type),
            code_sha256=str(config.code_hash),
            config_sha256=str(config.config_hash),
            status=str(config.status),
            source_package_sha256=source_packages.get(
                str(config.scheme_id)
            ),
        )
        for config in discovered
    )
    _authority_by_base(identities)
    return tuple(
        sorted(identities, key=lambda item: item.base_scheme_id)
    )


def _authority_by_base(
    identities: Sequence[DiscoveredSchemeIdentity],
) -> dict[str, DiscoveredSchemeIdentity]:
    by_base: dict[str, DiscoveredSchemeIdentity] = {}
    for identity in identities:
        if not identity.base_scheme_id.strip():
            raise SignalGapPlanError(
                "DISCOVERY_IDENTITY_INVALID",
                "discovery identity has empty scheme ID",
            )
        if identity.base_scheme_id in by_base:
            raise SignalGapPlanError(
                "DISCOVERY_IDENTITY_DUPLICATE",
                identity.base_scheme_id,
            )
        if (
            not identity.scheme_version.strip()
            or identity.runtime_type
            not in {"native_adapter", "blackbox_v2"}
            or identity.status != "active"
            or not _is_sha256(identity.code_sha256)
            or not _is_sha256(identity.config_sha256)
        ):
            raise SignalGapPlanError(
                "DISCOVERY_IDENTITY_INVALID",
                identity.base_scheme_id,
            )
        by_base[identity.base_scheme_id] = identity
    return by_base


def _discovery_identity_sha256(
    identities: Sequence[DiscoveredSchemeIdentity],
) -> str:
    _authority_by_base(identities)
    return canonical_json_sha256(
        [
            {
                "base_scheme_id": identity.base_scheme_id,
                "scheme_version": identity.scheme_version,
                "runtime_type": identity.runtime_type,
                "code_sha256": identity.code_sha256,
                "config_sha256": identity.config_sha256,
                "status": identity.status,
                "source_package_sha256":
                    identity.source_package_sha256,
            }
            for identity in sorted(
                identities,
                key=lambda item: item.base_scheme_id,
            )
        ]
    )


def _normalized_control_plane_blockers(
    blockers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in blockers:
        blocker = dict(raw)
        code = str(blocker.get("code") or "").strip()
        base_scheme_id = str(
            blocker.get("base_scheme_id") or ""
        ).strip()
        if not code or not base_scheme_id:
            raise SignalGapPlanError(
                "CONTROL_PLANE_BLOCKER_INVALID",
                "blocker requires code and base_scheme_id",
            )
        blocker["code"] = code
        blocker["base_scheme_id"] = base_scheme_id
        raw_scope = blocker.get(
            "segment_scope",
            ("canonical", "live"),
        )
        if (
            not isinstance(raw_scope, (list, tuple))
            or not raw_scope
            or any(
                str(segment) not in {"canonical", "live"}
                for segment in raw_scope
            )
        ):
            raise SignalGapPlanError(
                "CONTROL_PLANE_BLOCKER_INVALID",
                "blocker segment_scope must contain canonical/live",
            )
        blocker["segment_scope"] = sorted(
            {str(segment) for segment in raw_scope}
        )
        digest = canonical_json_sha256(blocker)
        if digest not in seen:
            seen.add(digest)
            normalized.append(blocker)
    return sorted(
        normalized,
        key=lambda item: (
            str(item["base_scheme_id"]),
            str(item["code"]),
            canonical_json_sha256(item),
        ),
    )


def _missing_discovery_identity(
    registry_row: Mapping[str, Any],
) -> DiscoveredSchemeIdentity:
    base_scheme_id = str(registry_row["base_scheme_id"])
    marker = canonical_json_sha256(
        {
            "authority": "missing_discovery_identity",
            "base_scheme_id": base_scheme_id,
        }
    )
    return DiscoveredSchemeIdentity(
        base_scheme_id=base_scheme_id,
        scheme_version=f"missing-{marker[:12]}",
        runtime_type=str(registry_row["runtime_type"]),
        code_sha256=marker,
        config_sha256=marker,
        status="active",
        source_package_sha256=(
            _load_0629_source_packages().get(base_scheme_id)
            if base_scheme_id
            in APPROVED_0629_LIVE_SOURCE_SCHEMES
            else None
        ),
    )


def _active_version_identity_sha256(
    version_rows: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_json_sha256(
        [
            {
                "scheme_id": str(row.get("scheme_id") or ""),
                "scheme_version":
                    str(row.get("scheme_version") or ""),
                "runtime_type": str(row.get("runtime_type") or ""),
                "code_hash": (
                    None
                    if row.get("code_hash") is None
                    else str(row["code_hash"])
                ),
                "config_hash": (
                    None
                    if row.get("config_hash") is None
                    else str(row["config_hash"])
                ),
            }
            for row in sorted(
                version_rows,
                key=lambda item: (
                    str(item.get("scheme_id") or ""),
                    str(item.get("scheme_version") or ""),
                    str(item.get("runtime_type") or ""),
                    str(item.get("code_hash") or ""),
                    str(item.get("config_hash") or ""),
                ),
            )
        ]
    )


def _load_0629_source_packages() -> dict[str, str]:
    policy_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "daily_scheduler_policy_v2.json"
    )
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
        rows = payload["schemes"]
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise SignalGapPlanError(
            "LIVE_SOURCE_0629_POLICY_INVALID",
            "daily scheduler v2 policy is unavailable",
        ) from exc
    if not isinstance(rows, list):
        raise SignalGapPlanError(
            "LIVE_SOURCE_0629_POLICY_INVALID",
            "daily scheduler v2 policy schemes are invalid",
        )
    result: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("scheme_id")
            not in APPROVED_0629_LIVE_SOURCE_SCHEMES
        ):
            continue
        scheme_id = str(row["scheme_id"])
        package_sha256 = str(
            row.get("source_package_sha256") or ""
        )
        if (
            row.get("input_compatibility") != "live_source_0629"
            or not _is_sha256(package_sha256)
            or scheme_id in result
        ):
            raise SignalGapPlanError(
                "LIVE_SOURCE_0629_POLICY_INVALID",
                scheme_id,
            )
        result[scheme_id] = package_sha256
    if set(result) != set(APPROVED_0629_LIVE_SOURCE_SCHEMES):
        raise SignalGapPlanError(
            "LIVE_SOURCE_0629_POLICY_INVALID",
            "approved 0629 policy identities are incomplete",
        )
    return result


def _input_mode_for_identity(
    base_scheme_id: str,
    runtime_type: str,
) -> str:
    if runtime_type == "blackbox_v2":
        return "databridge_v1"
    if base_scheme_id in APPROVED_0629_LIVE_SOURCE_SCHEMES:
        return "live_source_0629"
    return "generation_v1"


def _validate_active_scope(
    targets: Sequence[RegistryTarget],
) -> None:
    actual_frequency_counts = {
        frequency: sum(
            target.frequency == frequency for target in targets
        )
        for frequency in ("daily", "weekly", "monthly")
    }
    actual_execution_count = len(
        {target.base_scheme_id for target in targets}
    )
    if (
        len(targets) != EXPECTED_ACTIVE_TARGET_COUNT
        or actual_execution_count != EXPECTED_ACTIVE_EXECUTION_COUNT
        or actual_frequency_counts != EXPECTED_ACTIVE_FREQUENCY_COUNTS
    ):
        raise SignalGapPlanError(
            "ACTIVE_REGISTRY_SCOPE_DRIFT",
            "active Registry must be exactly 44 targets/40 executions "
            "with daily=29, weekly=7, monthly=8",
        )


def _registry_digest_sha256(
    targets: Sequence[RegistryTarget],
) -> str:
    return canonical_json_sha256(
        [
            {
                "registry_scheme_id": target.registry_scheme_id,
                "base_scheme_id": target.base_scheme_id,
                "runtime_type": target.runtime_type,
                "frequency": target.frequency,
                "task_type": target.task_type,
                "target_tenor": target.target_tenor,
                "horizon": target.horizon,
                "scheme_version": target.scheme_version,
                "code_sha256": target.code_sha256,
                "config_sha256": target.config_sha256,
                "live_target_start_date":
                    target.live_target_start_date,
                "live_boundary_source": target.live_boundary_source,
                "input_mode": target.input_mode,
                "source_package_sha256":
                    target.source_package_sha256,
            }
            for target in sorted(
                targets,
                key=lambda item: item.registry_scheme_id,
            )
        ]
    )


def _live_row_contract_error(
    row: Mapping[str, Any],
    target: RegistryTarget | None,
) -> str | None:
    errors: list[str] = []
    phase = str(row.get("prediction_phase") or "")
    if phase not in {"gray_live", "scheduled_live"}:
        errors.append("LIVE_PHASE_INVALID")
    if target is None:
        errors.append("LIVE_TARGET_NOT_ACTIVE")
    else:
        if str(row.get("scheme_version") or "") != target.scheme_version:
            errors.append("LIVE_SCHEME_VERSION_DRIFT")
        if str(row.get("run_runtime_type") or "") != target.runtime_type:
            errors.append("RUN_RUNTIME_TYPE_DRIFT")
    if row.get("run_id") is None:
        errors.append("RUN_LINK_MISSING")
    if str(row.get("run_status") or "") != "success":
        errors.append("RUN_STATUS_NOT_SUCCESS")
    if str(row.get("run_scheme_id") or "") != str(
        row.get("scheme_id") or ""
    ):
        errors.append("RUN_SCHEME_ID_DRIFT")
    if str(row.get("run_scheme_version") or "") != str(
        row.get("scheme_version") or ""
    ):
        errors.append("RUN_SCHEME_VERSION_DRIFT")
    if str(row.get("run_prediction_phase") or "") != phase:
        errors.append("RUN_PHASE_DRIFT")
    if str(row.get("run_predict_date") or "")[:10] != str(
        row.get("predict_date") or ""
    )[:10]:
        errors.append("RUN_PREDICT_DATE_DRIFT")
    return ",".join(errors) or None
