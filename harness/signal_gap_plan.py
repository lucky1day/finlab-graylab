from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
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
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)


PLAN_SCHEMA_VERSION = "active-signal-gap-plan-v1"
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
    state: str
    feature_date: str
    business_date: str
    manifest_sha256: str
    parent_generation_id: str | None = None
    parent_manifest_sha256: str | None = None
    dataset_content_id: str | None = None
    source_commit_token: str | None = None
    cutoff_feature_dates: tuple[str, ...] = ()


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


SnapshotReader = Callable[..., SignalGapSnapshot]


def plan_signal_gaps(
    engine: Any,
    *,
    start_date: str,
    as_of_date: str,
    snapshot_reader: SnapshotReader | None = None,
    execution_authority: Sequence[DiscoveredSchemeIdentity] | None = None,
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
    blocker_codes_by_base: dict[str, set[str]] = {}
    for blocker in control_plane_blockers:
        blocker_codes_by_base.setdefault(
            str(blocker["base_scheme_id"]),
            set(),
        ).add(str(blocker["code"]))
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
    if unexpected:
        raise SignalGapPlanError(
            "OBSERVED_SIGNAL_OUTSIDE_AUTHORITY",
            f"observed rows are outside expected authority: {unexpected[:3]}",
        )

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
        observation_error = _observation_contract_error(
            item,
            target,
            observed=observed,
            opposite=opposite,
        )
        action, generation, reason = _resolve_action(
            item,
            target=target,
            control_plane_error=(
                ",".join(
                    sorted(blocker_codes_by_base[item.base_scheme_id])
                )
                if item.base_scheme_id in blocker_codes_by_base
                else None
            ),
            valid_present=(len(observed) == 1 and observation_error is None),
            observation_error=observation_error,
            generations=snapshot.input_generations,
            as_of_date=normalized_as_of,
        )
        actions.append(
            _action_row(
                item,
                action=action,
                reason=reason,
                generation=generation,
            )
        )

    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in VALID_ACTIONS
    }
    present = action_counts["SKIP_PRESENT"]
    open_gap = len(actions) - present
    actionable = (
        action_counts["GRAY_LIVE_GAP"]
        + action_counts["FULL_CANONICAL_RUN_REQUIRED"]
    )
    blocked = (
        action_counts["BLOCKED_NO_GENERATION"]
        + action_counts["BLOCKED_DATA_CONTRACT"]
    )
    unsigned: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": (
            "BLOCKED"
            if blocked or control_plane_blockers
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
            **action_counts,
        },
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


def read_signal_gap_snapshot(
    connection: Any,
    *,
    start_date: str,
    as_of_date: str,
    execution_authority: Sequence[DiscoveredSchemeIdentity],
    discovery_identity_sha256: str,
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
    canonical_cases, canonical_signals, canonical_watermarks = (
        _read_persisted_canonical_authority(
            connection,
            raw_registry,
            registry_targets,
        )
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
    live_cases = build_expected_live_cases(
        registry_targets,
        calendar=calendar,
        as_of_date=as_of_date,
    )
    raw_live, live_signals = _read_live_signals(
        connection,
        registry_targets,
        as_of_date=as_of_date,
        expected_business_keys={
            case.business_key
            for case in (*canonical_cases, *live_cases)
        },
    )
    generations = _read_input_generations(connection)
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
        "active_registry_count": len(registry_targets),
        "raw_live_row_count": len(raw_live),
        "selected_live_row_count": len(live_signals),
        "trade_calendar_max": max(
            calendar._trading_days,
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
        self._week_calendar = build_week_calendar(week_calendar_rows)
        self.daily_predict_dates = tuple(
            day
            for day in self._trading_days
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

    def week_id_for_date(self, value: str) -> int | None:
        return self._week_calendar.week_id_for_date(value)

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self._week_calendar.week_id_to_last_trading_day(week_id)


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
        if len(active_versions) != 1:
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


def _read_persisted_canonical_authority(
    connection: Any,
    raw_registry: Sequence[Mapping[str, Any]],
    targets: Sequence[RegistryTarget],
) -> tuple[
    tuple[ExpectedSignalCase, ...],
    tuple[ObservedSignal, ...],
    dict[str, int],
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
    if set(selected_by_registry) != {
        target.registry_scheme_id for target in targets
    }:
        missing = sorted(
            {
                target.registry_scheme_id for target in targets
            }
            - set(selected_by_registry)
        )
        raise SignalGapPlanError(
            "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
            f"missing latest persisted run for {missing[:5]}",
        )
    unique_runs = {
        int(row["id"]): dict(row)
        for row in selected_by_registry.values()
    }
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
    registry_ids_by_run: dict[int, list[str]] = {}
    for registry_id, run in selected_by_registry.items():
        registry_ids_by_run.setdefault(int(run["id"]), []).append(
            registry_id
        )
    for run_id, run in sorted(unique_runs.items()):
        _validate_canonical_run_manifest(
            run_id=run_id,
            summary=_json_object(run.get("summary")),
            details=details_by_run.get(run_id, ()),
            expected_target_pairs={
                (
                    target_by_registry[registry_id].target_tenor,
                    target_by_registry[registry_id].horizon,
                )
                for registry_id in registry_ids_by_run.get(run_id, ())
            },
        )
    cases: list[ExpectedSignalCase] = []
    signals: list[ObservedSignal] = []
    for registry_id, run in sorted(selected_by_registry.items()):
        target = target_by_registry[registry_id]
        run_details = [
            row
            for row in details_by_run.get(int(run["id"]), ())
            if (
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            == (target.target_tenor, target.horizon)
        ]
        if not run_details:
            raise SignalGapPlanError(
                "CANONICAL_CASE_AUTHORITY_UNAVAILABLE",
                f"run={run['id']} has no target details for {registry_id}",
            )
        summary = _json_object(run.get("summary"))
        version_error = _canonical_run_version_error(
            target,
            run,
            summary,
        )
        for row in run_details:
            case = _case_from_target(
                target,
                predict_date=str(row["predict_date"])[:10],
                feature_date=str(row["feature_date"])[:10],
                target_date=str(row["target_date"])[:10],
                segment="canonical",
            )
            cases.append(case)
            signals.append(
                ObservedSignal(
                    base_scheme_id=target.base_scheme_id,
                    target_tenor=target.target_tenor,
                    horizon=target.horizon,
                    target_date=case.target_date,
                    predict_date=case.predict_date,
                    feature_date=case.feature_date,
                    phase="canonical",
                    scheme_version=target.scheme_version,
                    run_status=str(run["status"]),
                    contract_error=version_error,
                )
            )
    return (
        tuple(sorted(cases, key=_case_sort_key)),
        tuple(sorted(signals, key=_observed_sort_key)),
        {
            "candidate_backtest_run_count": len(runs),
            "selected_backtest_run_count": len(unique_runs),
            "selected_backtest_detail_count": len(details),
        },
    )


def _read_input_generations(
    connection: Any,
) -> tuple[InputGeneration, ...]:
    rows = connection.execute(
        text(
            """
            SELECT generation_id, generation_type, state, feature_date,
                   business_date, manifest_sha256, native_generation_id,
                   native_manifest_sha256, dataset_content_id,
                   source_commit_token
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
            state=str(row["state"]),
            feature_date=str(row["feature_date"])[:10],
            business_date=str(row["business_date"])[:10],
            manifest_sha256=str(row["manifest_sha256"] or ""),
            parent_generation_id=_optional_text(
                row.get("native_generation_id")
            ),
            parent_manifest_sha256=_optional_text(
                row.get("native_manifest_sha256")
            ),
            dataset_content_id=_optional_text(
                row.get("dataset_content_id")
            ),
            source_commit_token=_optional_text(
                row.get("source_commit_token")
            ),
            cutoff_feature_dates=(),
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
    as_of_date: str,
) -> tuple[Action, InputGeneration | None, str]:
    if control_plane_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            f"CONTROL_PLANE_BLOCKER:{control_plane_error}",
        )
    if valid_present:
        return "SKIP_PRESENT", None, "BUSINESS_KEY_PRESENT"
    if observation_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            observation_error,
        )
    if item.segment == "canonical":
        return (
            "FULL_CANONICAL_RUN_REQUIRED",
            None,
            "CANONICAL_BUSINESS_KEY_MISSING",
        )
    if item.data_contract_error:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            item.data_contract_error,
        )
    if target.input_mode == "live_source_0629":
        return "GRAY_LIVE_GAP", None, "LIVE_BUSINESS_KEY_MISSING"
    if target.runtime_type == "blackbox_v2":
        return _blackbox_generation_eligibility(
            item,
            generations,
            as_of_date=as_of_date,
        )
    return _native_generation_eligibility(
        item,
        generations,
        as_of_date=as_of_date,
    )


def _blackbox_generation_eligibility(
    item: ExpectedSignalCase,
    generations: Sequence[InputGeneration],
    *,
    as_of_date: str,
) -> tuple[Action, InputGeneration | None, str]:
    candidates = [
        row
        for row in generations
        if row.generation_type == "databridge_v1"
    ]
    if not candidates:
        return (
            "BLOCKED_NO_GENERATION",
            None,
            "NO_DATABRIDGE_GENERATION",
        )
    valid_fences = [
        row
        for row in candidates
        if row.state == "SEALED"
        and _is_sha256(row.manifest_sha256)
        and row.business_date <= as_of_date
        and bool(row.parent_generation_id)
        and _is_sha256(row.parent_manifest_sha256 or "")
        and bool(row.dataset_content_id)
        and bool(row.source_commit_token)
    ]
    if not valid_fences:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "GENERATION_CONTRACT_INVALID",
        )
    eligible = [
        row
        for row in valid_fences
        if row.feature_date >= item.feature_date
        and item.feature_date in row.cutoff_feature_dates
    ]
    if not eligible:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "DATABRIDGE_CUTOFF_AUTHORITY_MISSING",
        )
    selected = max(
        eligible,
        key=lambda row: (
            row.feature_date,
            row.business_date,
            row.generation_id,
        ),
    )
    return "GRAY_LIVE_GAP", selected, "LIVE_BUSINESS_KEY_MISSING"


def _native_generation_eligibility(
    item: ExpectedSignalCase,
    generations: Sequence[InputGeneration],
    *,
    as_of_date: str,
) -> tuple[Action, InputGeneration | None, str]:
    candidates = [
        row
        for row in generations
        if row.generation_type == "native_source"
    ]
    if not candidates:
        return (
            "BLOCKED_NO_GENERATION",
            None,
            "NO_NATIVE_GENERATION",
        )
    eligible = [
        row
        for row in candidates
        if row.state == "SEALED"
        and _is_sha256(row.manifest_sha256)
        and row.feature_date == item.feature_date
        and row.business_date <= as_of_date
        and bool(row.dataset_content_id)
        and bool(row.source_commit_token)
    ]
    if not eligible:
        return (
            "BLOCKED_DATA_CONTRACT",
            None,
            "GENERATION_CONTRACT_INVALID",
        )
    return (
        "GRAY_LIVE_GAP",
        max(eligible, key=lambda row: (row.business_date, row.generation_id)),
        "LIVE_BUSINESS_KEY_MISSING",
    )


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
        and item.predict_date != item.feature_date
    ):
        raise SignalGapPlanError(
            "CASE_CONTRACT_INVALID",
            "canonical predict_date must equal feature_date",
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
    action: Action,
    reason: str,
    generation: InputGeneration | None,
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
        "business_key": [
            item.base_scheme_id,
            item.target_tenor,
            item.horizon,
            item.target_date,
        ],
        "action": action,
        "reason": reason,
        "generation": (
            {
                "generation_id": generation.generation_id,
                "generation_type": generation.generation_type,
                "feature_date": generation.feature_date,
                "business_date": generation.business_date,
                "manifest_sha256": generation.manifest_sha256,
            }
            if generation is not None
            else None
        ),
    }


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
    if summary_version is not None:
        return (
            None
            if summary_version == target.scheme_version
            else "CANONICAL_SCHEME_VERSION_DRIFT"
        )
    if (
        target.code_sha256
        and target.config_sha256
        and str(run.get("code_hash") or "") == target.code_sha256
        and str(run.get("config_hash") or "") == target.config_sha256
    ):
        return None
    return "CANONICAL_VERSION_AUTHORITY_UNAVAILABLE"


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
    identities = tuple(
        DiscoveredSchemeIdentity(
            base_scheme_id=str(config.scheme_id),
            scheme_version=str(config.scheme_version),
            runtime_type=str(config.runtime_type),
            code_sha256=str(config.code_hash),
            config_sha256=str(config.config_hash),
            status=str(config.status),
        )
        for config in discover_schemes(strict=True)
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
