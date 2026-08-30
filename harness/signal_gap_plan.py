from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import bindparam, text

from scheduler.discovery import discover_schemes
from shared.actual_facts import build_week_calendar
from shared.calendar_service import (
    is_trading_day_row,
    read_calendar_snapshot_from_connection,
)
from shared.data_bridge.authority import (
    StableDataBridgeCurrentAuthority,
    StableDataBridgeCutoff,
    blackbox_gray_replay_source_identity,
    resolve_stable_databridge_current_authority,
)
from shared.data_bridge.refresh import (
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeRefreshConfig,
)
from shared.prediction_context import (
    LIVE_PREDICTION_PHASES,
    build_daily_live_context,
    build_monthly_live_context,
    build_period_average_live_context,
    build_weekly_live_context,
    is_weekly_signal_date,
)
from shared.scheme_config_schema import ALLOWED_RUNTIME_TYPES, SCHEME_ID_PATTERN
from shared.period_average_buckets import period_anchor_dates
from shared.task_specs import (
    ALLOWED_FREQUENCIES,
    PERIOD_AVERAGE_TASK_TYPES,
    TASK_COMBINATIONS,
)


PLAN_SCHEMA_VERSION = "single-date-active-live-gap-plan-v1"
RANGE_PLAN_SCHEMA_VERSION = "target-range-active-live-gap-plan-v1"
PLATFORM_LIVE_TARGET_START_DATE = "2026-06-01"
NATIVE_TASK_COMBINATIONS = {
    "T+1": (1, "daily"),
    "T+5": (5, "daily"),
    "weekly_point": (6, "weekly"),
    "weekly_average": (6, "weekly"),
    "monthly": (30, "monthly"),
}
VALID_ACTIONS = (
    "SKIP_PRESENT",
    "SKIP_NOT_DUE",
    "GRAY_LIVE_GAP",
    "BLOCKED_NO_GENERATION",
    "BLOCKED_DATA_CONTRACT",
)
Action = Literal[
    "SKIP_PRESENT",
    "SKIP_NOT_DUE",
    "GRAY_LIVE_GAP",
    "BLOCKED_NO_GENERATION",
    "BLOCKED_DATA_CONTRACT",
]


class SignalGapPlanError(RuntimeError):
    """单日缺口计划的调用参数或权威读取违反 fail-closed 契约。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DiscoveredSchemeIdentity:
    base_scheme_id: str
    scheme_version: str
    runtime_type: str
    frequency: str
    horizon: int
    task_type: str
    target_tenors: tuple[str, ...]
    version_status: str
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
class SignalGapSnapshot:
    registry_targets: tuple[RegistryTarget, ...]
    expected_cases: tuple[ExpectedSignalCase, ...]
    live_signals: tuple[ObservedSignal, ...]
    control_plane_blockers: tuple[Mapping[str, Any], ...] = ()
    databridge_authority: StableDataBridgeCurrentAuthority | None = None
    databridge_authority_error: Literal["MISSING", "INVALID"] | None = None


@dataclass(frozen=True, slots=True)
class _BlackboxGapAuthorityIndex:
    cutoffs_by_feature_date: Mapping[
        str,
        tuple[StableDataBridgeCutoff, ...],
    ]
    source_identity: Mapping[str, Any] | None
    source_identity_invalid: bool


def plan_signal_gaps(
    engine: Any,
    *,
    predict_date: str,
    base_scheme_id: str | None = None,
    databridge_config: DataBridgeRefreshConfig,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """在单个只读 consistent snapshot 中规划单日 active live 缺口。"""
    normalized_date = _canonical_date(predict_date, "predict_date")
    normalized_base = _normalize_base_scheme_id(base_scheme_id)
    configs = tuple(
        _discover_scheme_configs()
        if project_root is None
        else _discover_scheme_configs(project_root)
    )
    authority, selection_error = _select_execution_authority(
        configs,
        base_scheme_id=normalized_base,
    )
    if selection_error is not None:
        return _blocked_plan(
            predict_date=normalized_date,
            base_scheme_id=normalized_base,
            failure_code=selection_error,
        )
    if normalized_base is None and not authority:
        return _build_signal_gap_plan(
            SignalGapSnapshot(
                registry_targets=(),
                expected_cases=(),
                live_signals=(),
            ),
            predict_date=normalized_date,
            base_scheme_id=None,
        )

    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            try:
                snapshot = read_signal_gap_snapshot(
                    connection,
                    predict_date=normalized_date,
                    base_scheme_id=normalized_base,
                    execution_authority=authority,
                    databridge_config=databridge_config,
                )
            except SignalGapPlanError:
                raise
            except Exception as exc:
                raise SignalGapPlanError(
                    "SIGNAL_GAP_SNAPSHOT_FAILED",
                    type(exc).__name__,
                ) from exc
            return _build_signal_gap_plan(
                snapshot,
                predict_date=normalized_date,
                base_scheme_id=normalized_base,
            )
        finally:
            connection.rollback()


def plan_signal_gap_target_range(
    engine: Any,
    *,
    target_date_from: str,
    target_date_before: str,
    base_scheme_id: str,
    databridge_config: DataBridgeRefreshConfig,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """在一个只读快照中规划单个受支持 Blackbox 的 target 日期区间。"""
    normalized_from = _canonical_date(target_date_from, "target_date_from")
    normalized_before = _canonical_date(
        target_date_before,
        "target_date_before",
    )
    if normalized_from >= normalized_before:
        raise SignalGapPlanError(
            "TARGET_RANGE_INVALID",
            "target_date_from must be before target_date_before",
        )
    normalized_base = _normalize_base_scheme_id(base_scheme_id)
    if normalized_base is None:
        raise SignalGapPlanError(
            "TARGET_RANGE_SCHEME_REQUIRED",
            "target range requires one exact base scheme id",
        )
    if normalized_from < PLATFORM_LIVE_TARGET_START_DATE:
        return _blocked_range_plan(
            target_date_from=normalized_from,
            target_date_before=normalized_before,
            base_scheme_id=normalized_base,
            failure_code="TARGET_RANGE_BEFORE_PLATFORM_LIVE_START",
        )
    configs = tuple(
        _discover_scheme_configs()
        if project_root is None
        else _discover_scheme_configs(project_root)
    )
    authority, selection_error = _select_execution_authority(
        configs,
        base_scheme_id=normalized_base,
    )
    if selection_error is not None:
        return _blocked_range_plan(
            target_date_from=normalized_from,
            target_date_before=normalized_before,
            base_scheme_id=normalized_base,
            failure_code=selection_error,
        )
    if len(authority) != 1 or (
        authority[0].runtime_type,
        authority[0].frequency,
        authority[0].task_type,
        authority[0].horizon,
    ) not in {
        ("blackbox_v2", "weekly", "weekly_point", 1),
        ("blackbox_v2", "daily", "T+5", 5),
    }:
        return _blocked_range_plan(
            target_date_from=normalized_from,
            target_date_before=normalized_before,
            base_scheme_id=normalized_base,
            failure_code="TARGET_RANGE_CONTRACT_UNSUPPORTED",
        )

    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            calendar = _read_calendar(connection)
            targets, blockers = _read_registry_targets(
                connection,
                execution_authority=authority,
                targeted=True,
            )
            if blockers:
                return _blocked_range_plan(
                    target_date_from=normalized_from,
                    target_date_before=normalized_before,
                    base_scheme_id=normalized_base,
                    failure_code=str(blockers[0]["code"]),
                    blockers=blockers,
                )
            cases = _target_range_cases(
                targets,
                calendar=calendar,
                target_date_from=normalized_from,
                target_date_before=normalized_before,
            )
            if not cases:
                return _blocked_range_plan(
                    target_date_from=normalized_from,
                    target_date_before=normalized_before,
                    base_scheme_id=normalized_base,
                    failure_code="TARGET_RANGE_EMPTY",
                )
            expected_keys = {item.business_key for item in cases}
            live_signals = _read_live_signals(
                connection,
                targets,
                predict_date=cases[0].predict_date,
                expected_business_keys=expected_keys,
            )
            observed = _index_observations(live_signals)
            if any(observed.get(item.business_key) for item in cases):
                return _blocked_range_plan(
                    target_date_from=normalized_from,
                    target_date_before=normalized_before,
                    base_scheme_id=normalized_base,
                    failure_code="TARGET_RANGE_BUSINESS_KEY_PRESENT",
                )
            try:
                databridge_authority = resolve_stable_databridge_current_authority(
                    databridge_config,
                    feature_dates=tuple(
                        sorted({item.feature_date for item in cases})
                    ),
                    connection=connection,
                )
                authority_error = None
            except DataBridgeCurrentMissingError:
                databridge_authority = None
                authority_error = "MISSING"
            except DataBridgeCurrentInvalidError:
                databridge_authority = None
                authority_error = "INVALID"
            snapshot = SignalGapSnapshot(
                registry_targets=targets,
                expected_cases=cases,
                live_signals=live_signals,
                databridge_authority=databridge_authority,
                databridge_authority_error=authority_error,
            )
            return _build_signal_gap_range_plan(
                snapshot,
                target_date_from=normalized_from,
                target_date_before=normalized_before,
                base_scheme_id=normalized_base,
            )
        finally:
            connection.rollback()


def verify_signal_gap_fill_readback(
    engine: Any,
    *,
    actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """在一次只读快照中校验 range gap-fill 的全部业务键。"""
    targets: dict[str, RegistryTarget] = {}
    cases: list[ExpectedSignalCase] = []
    seen_business_keys: set[tuple[str, str, int, str]] = set()
    for raw in actions:
        if not isinstance(raw, Mapping):
            raise SignalGapPlanError(
                "POSTFILL_READBACK_SCOPE_INVALID",
                "action must be a mapping",
            )
        target = RegistryTarget(
            registry_scheme_id=str(raw["registry_scheme_id"]),
            base_scheme_id=str(raw["base_scheme_id"]),
            runtime_type=str(raw["runtime_type"]),
            frequency=str(raw["frequency"]),
            task_type=str(raw["task_type"]),
            target_tenor=str(raw["target_tenor"]),
            horizon=int(raw["horizon"]),
            scheme_version=str(raw["scheme_version"]),
        )
        _validate_registry_target(target)
        prior = targets.get(target.registry_scheme_id)
        if prior is not None and prior != target:
            raise SignalGapPlanError(
                "POSTFILL_READBACK_SCOPE_INVALID",
                target.registry_scheme_id,
            )
        targets[target.registry_scheme_id] = target
        case = ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id=target.base_scheme_id,
            runtime_type=target.runtime_type,
            frequency=target.frequency,
            task_type=target.task_type,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date=_canonical_date(raw["predict_date"], "predict_date"),
            feature_date=_canonical_date(raw["feature_date"], "feature_date"),
            target_date=_canonical_date(raw["target_date"], "target_date"),
        )
        if list(case.business_key) != list(raw.get("business_key") or ()):
            raise SignalGapPlanError(
                "POSTFILL_READBACK_SCOPE_INVALID",
                target.registry_scheme_id,
            )
        if case.business_key in seen_business_keys:
            raise SignalGapPlanError(
                "DUPLICATE_EXPECTED_BUSINESS_KEY",
                target.registry_scheme_id,
            )
        seen_business_keys.add(case.business_key)
        cases.append(case)

    ordered_cases = tuple(sorted(cases, key=_case_sort_key))
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            live_signals = _read_live_signals_for_scope(
                connection,
                tuple(targets.values()),
                predict_dates=tuple(
                    item.predict_date for item in ordered_cases
                ),
                expected_business_keys=seen_business_keys,
            )
        finally:
            connection.rollback()

    observed = _index_observations(live_signals)
    unexpected_scopes = {
        (row.base_scheme_id, row.predict_date)
        for row in live_signals
        if row.business_key not in seen_business_keys
    }
    unexpected_target_scopes = {
        (row.base_scheme_id, row.target_date)
        for row in live_signals
        if row.business_key not in seen_business_keys
    }
    readback_actions: list[dict[str, Any]] = []
    for item in ordered_cases:
        target = targets[item.registry_scheme_id]
        rows = observed.get(item.business_key, ())
        if (
            (item.base_scheme_id, item.predict_date) in unexpected_scopes
            or (item.base_scheme_id, item.target_date)
            in unexpected_target_scopes
        ):
            action: Action = "BLOCKED_DATA_CONTRACT"
            reason = "OBSERVED_SIGNAL_TARGET_DRIFT"
        elif len(rows) > 1:
            action = "BLOCKED_DATA_CONTRACT"
            reason = "DUPLICATE_OBSERVED_BUSINESS_KEY"
        elif len(rows) == 1:
            reason = _observation_contract_error(item, target, rows[0])
            action = "SKIP_PRESENT" if reason is None else "BLOCKED_DATA_CONTRACT"
            reason = reason or "VALID_LIVE_RESULT_PRESENT"
        else:
            action = "GRAY_LIVE_GAP"
            reason = "LIVE_BUSINESS_KEY_MISSING"
        readback_actions.append(
            _action_row(
                item,
                target=target,
                action=action,
                reason=reason,
                input_authority=None,
                business_key_present=bool(rows),
            )
        )
    blocked = any(
        row["action"] == "BLOCKED_DATA_CONTRACT"
        for row in readback_actions
    )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": "BLOCKED" if blocked else "READY",
        "actions": readback_actions,
    }


def read_signal_gap_snapshot(
    connection: Any,
    *,
    predict_date: str,
    base_scheme_id: str | None,
    execution_authority: Sequence[DiscoveredSchemeIdentity],
    databridge_config: DataBridgeRefreshConfig,
) -> SignalGapSnapshot:
    """从当前事务读取 active version、Registry、日历及 live 业务键。"""
    calendar = _read_calendar(connection)
    due_authority = _due_execution_authority(
        execution_authority,
        predict_date=predict_date,
        calendar=calendar,
        targeted=base_scheme_id is not None,
    )
    registry_targets, blockers = _read_registry_targets(
        connection,
        execution_authority=due_authority,
        targeted=base_scheme_id is not None,
    )
    expected_cases: list[ExpectedSignalCase] = []
    context_blockers: list[Mapping[str, Any]] = []
    for target in registry_targets:
        if not _is_due(target, predict_date=predict_date, calendar=calendar):
            continue
        try:
            case = _expected_case(
                target,
                predict_date=predict_date,
                calendar=calendar,
            )
        except (SignalGapPlanError, ValueError) as exc:
            context_blockers.append(
                {
                    "code": "PREDICTION_CONTEXT_INVALID",
                    "base_scheme_id": target.base_scheme_id,
                    "detail": type(exc).__name__,
                }
            )
            continue
        if _case_is_in_platform_live_scope(case):
            expected_cases.append(case)

    live_signals = _read_live_signals(
        connection,
        registry_targets,
        predict_date=predict_date,
        expected_business_keys={item.business_key for item in expected_cases},
    )
    signals_by_key = _index_observations(live_signals)
    required_blackbox_dates = tuple(
        sorted(
            {
                item.feature_date
                for item in expected_cases
                if item.runtime_type == "blackbox_v2"
                and not signals_by_key.get(item.business_key)
            }
        )
    )
    authority: StableDataBridgeCurrentAuthority | None = None
    authority_error: Literal["MISSING", "INVALID"] | None = None
    if required_blackbox_dates:
        try:
            authority = resolve_stable_databridge_current_authority(
                databridge_config,
                feature_dates=required_blackbox_dates,
                connection=connection,
            )
        except DataBridgeCurrentMissingError:
            authority_error = "MISSING"
        except DataBridgeCurrentInvalidError:
            authority_error = "INVALID"

    return SignalGapSnapshot(
        registry_targets=registry_targets,
        expected_cases=tuple(
            sorted(expected_cases, key=_case_sort_key)
        ),
        live_signals=live_signals,
        control_plane_blockers=tuple((*blockers, *context_blockers)),
        databridge_authority=authority,
        databridge_authority_error=authority_error,
    )


def _build_signal_gap_plan(
    snapshot: SignalGapSnapshot,
    *,
    predict_date: str,
    base_scheme_id: str | None,
) -> dict[str, Any]:
    targets = _validate_snapshot(snapshot, predict_date=predict_date)
    blockers = _normalize_blockers(snapshot.control_plane_blockers)
    if blockers:
        return _blocked_plan(
            predict_date=predict_date,
            base_scheme_id=base_scheme_id,
            failure_code=str(blockers[0]["code"]),
            blockers=blockers,
        )

    expected_keys = {item.business_key for item in snapshot.expected_cases}
    scoped_live_signals = (
        ()
        if base_scheme_id is not None and not snapshot.expected_cases
        else snapshot.live_signals
    )
    observed = _index_observations(scoped_live_signals)
    blackbox_authority_index = _index_blackbox_gap_authority(snapshot)
    unexpected = tuple(
        row
        for row in scoped_live_signals
        if row.business_key not in expected_keys
    )
    unexpected_by_base = {
        row.base_scheme_id
        for row in unexpected
        if row.predict_date == predict_date
    }
    actions: list[dict[str, Any]] = []

    for item in sorted(snapshot.expected_cases, key=_case_sort_key):
        target = targets[item.registry_scheme_id]
        rows = observed.get(item.business_key, ())
        if len(rows) > 1:
            action: Action = "BLOCKED_DATA_CONTRACT"
            reason = "DUPLICATE_OBSERVED_BUSINESS_KEY"
            input_authority = None
        elif len(rows) == 1:
            error = _observation_contract_error(item, target, rows[0])
            if error is None:
                action = "SKIP_PRESENT"
                reason = "VALID_LIVE_RESULT_PRESENT"
            else:
                action = "BLOCKED_DATA_CONTRACT"
                reason = error
            input_authority = None
        elif item.base_scheme_id in unexpected_by_base:
            action = "BLOCKED_DATA_CONTRACT"
            reason = "OBSERVED_SIGNAL_TARGET_DRIFT"
            input_authority = None
        elif item.runtime_type == "native_adapter":
            action = "GRAY_LIVE_GAP"
            reason = "LIVE_BUSINESS_KEY_MISSING"
            input_authority = None
        else:
            action, reason, input_authority = _blackbox_gap_action(
                item,
                snapshot=snapshot,
                authority_index=blackbox_authority_index,
            )
        actions.append(
            _action_row(
                item,
                target=target,
                action=action,
                reason=reason,
                input_authority=input_authority,
                business_key_present=bool(rows),
            )
        )

    if base_scheme_id is not None and not snapshot.expected_cases:
        for target in sorted(
            snapshot.registry_targets,
            key=lambda item: item.registry_scheme_id,
        ):
            actions.append(_not_due_action(target, predict_date=predict_date))

    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in VALID_ACTIONS
    }
    unexpected_count = len(unexpected)
    blocked = (
        action_counts["BLOCKED_NO_GENERATION"]
        + action_counts["BLOCKED_DATA_CONTRACT"]
        + unexpected_count
    )
    status = "BLOCKED" if blocked else "READY"
    failure_code = None
    if unexpected_count:
        failure_code = "OBSERVED_SIGNAL_TARGET_DRIFT"
    if failure_code is None:
        failure_code = next(
            (
                str(row["reason"])
                for row in actions
                if row["action"].startswith("BLOCKED_")
            ),
            None,
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": status,
        "failure_code": failure_code,
        "predict_date": predict_date,
        "base_scheme_id": base_scheme_id,
        "control_plane": {
            "read_only": True,
            "transaction_isolation": "REPEATABLE READ",
            "consistent_snapshot": True,
            "business_key_fields": [
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ],
            "blockers": [],
        },
        "counts": {
            "active_target": len(snapshot.registry_targets),
            "expected": len(snapshot.expected_cases),
            "present": action_counts["SKIP_PRESENT"],
            "actionable": action_counts["GRAY_LIVE_GAP"],
            "blocked": blocked,
            **action_counts,
        },
        "actions": actions,
    }


def _target_range_cases(
    targets: Sequence[RegistryTarget],
    *,
    calendar: _SingleDateCalendar,
    target_date_from: str,
    target_date_before: str,
) -> tuple[ExpectedSignalCase, ...]:
    if not targets:
        return ()
    cases: list[ExpectedSignalCase] = []
    frequency = targets[0].frequency
    if frequency == "daily":
        if not calendar.covers(target_date_from) or not calendar.covers(
            target_date_before
        ):
            raise SignalGapPlanError(
                "DATA_CONTRACT_INVALID",
                "target range is outside trade calendar coverage",
            )
        trading_days = calendar.trading_days()
        predict_dates_list: list[str] = []
        for target_index, target_date in enumerate(trading_days):
            if not target_date_from <= target_date < target_date_before:
                continue
            predict_index = target_index - targets[0].horizon + 1
            if predict_index < 1:
                raise SignalGapPlanError(
                    "DATA_CONTRACT_INVALID",
                    "trade calendar does not cover target range lookback",
                )
            predict_dates_list.append(trading_days[predict_index])
        predict_dates = tuple(predict_dates_list)
    else:
        start = date.fromisoformat(target_date_from) - timedelta(days=14)
        stop = date.fromisoformat(target_date_before)
        weekly_dates: list[str] = []
        current = start
        while current <= stop:
            predict_date = current.isoformat()
            if current.weekday() == 5 and is_weekly_signal_date(
                calendar,
                predict_date,
            ):
                weekly_dates.append(predict_date)
            current += timedelta(days=1)
        predict_dates = tuple(weekly_dates)
    for predict_date in predict_dates:
        for target in targets:
            item = _expected_case(
                target,
                predict_date=predict_date,
                calendar=calendar,
            )
            if target_date_from <= item.target_date < target_date_before:
                cases.append(item)
    ordered = tuple(
        sorted(cases, key=lambda item: (item.predict_date, item.target_tenor))
    )
    keys = [item.business_key for item in ordered]
    if len(keys) != len(set(keys)):
        raise SignalGapPlanError(
            "DUPLICATE_EXPECTED_BUSINESS_KEY",
            "target range produced duplicate business keys",
        )
    return ordered


def _build_signal_gap_range_plan(
    snapshot: SignalGapSnapshot,
    *,
    target_date_from: str,
    target_date_before: str,
    base_scheme_id: str,
) -> dict[str, Any]:
    targets = {
        target.registry_scheme_id: target
        for target in snapshot.registry_targets
    }
    actions: list[dict[str, Any]] = []
    blackbox_authority_index = _index_blackbox_gap_authority(snapshot)
    for item in snapshot.expected_cases:
        target = targets.get(item.registry_scheme_id)
        if target is None:
            raise SignalGapPlanError(
                "EXPECTED_CASE_REGISTRY_DRIFT",
                item.registry_scheme_id,
            )
        action, reason, input_authority = _blackbox_gap_action(
            item,
            snapshot=snapshot,
            authority_index=blackbox_authority_index,
        )
        actions.append(
            _action_row(
                item,
                target=target,
                action=action,
                reason=reason,
                input_authority=input_authority,
                business_key_present=False,
            )
        )
    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in VALID_ACTIONS
    }
    blocked = (
        action_counts["BLOCKED_NO_GENERATION"]
        + action_counts["BLOCKED_DATA_CONTRACT"]
    )
    failure_code = next(
        (
            str(row["reason"])
            for row in actions
            if row["action"].startswith("BLOCKED_")
        ),
        None,
    )
    return {
        "schema_version": RANGE_PLAN_SCHEMA_VERSION,
        "status": "BLOCKED" if blocked else "READY",
        "failure_code": failure_code,
        "predict_date": None,
        "target_date_from": target_date_from,
        "target_date_before": target_date_before,
        "base_scheme_id": base_scheme_id,
        "control_plane": {
            "read_only": True,
            "transaction_isolation": "REPEATABLE READ",
            "consistent_snapshot": True,
            "business_key_fields": [
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ],
            "blockers": [],
        },
        "counts": {
            "active_target": len(snapshot.registry_targets),
            "expected": len(snapshot.expected_cases),
            "present": 0,
            "actionable": action_counts["GRAY_LIVE_GAP"],
            "blocked": blocked,
            **action_counts,
        },
        "actions": actions,
    }


def _blocked_range_plan(
    *,
    target_date_from: str,
    target_date_before: str,
    base_scheme_id: str,
    failure_code: str,
    blockers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    counts = {action: 0 for action in VALID_ACTIONS}
    return {
        "schema_version": RANGE_PLAN_SCHEMA_VERSION,
        "status": "BLOCKED",
        "failure_code": failure_code,
        "predict_date": None,
        "target_date_from": target_date_from,
        "target_date_before": target_date_before,
        "base_scheme_id": base_scheme_id,
        "control_plane": {
            "read_only": True,
            "transaction_isolation": "REPEATABLE READ",
            "consistent_snapshot": True,
            "business_key_fields": [
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ],
            "blockers": [dict(item) for item in blockers],
        },
        "counts": {
            "active_target": 0,
            "expected": 0,
            "present": 0,
            "actionable": 0,
            "blocked": 1,
            **counts,
        },
        "actions": [],
    }


def _blackbox_gap_action(
    item: ExpectedSignalCase,
    *,
    snapshot: SignalGapSnapshot,
    authority_index: _BlackboxGapAuthorityIndex | None = None,
) -> tuple[Action, str, Mapping[str, Any] | None]:
    if snapshot.databridge_authority_error == "MISSING":
        return (
            "BLOCKED_NO_GENERATION",
            "DATABRIDGE_CURRENT_MISSING",
            None,
        )
    if snapshot.databridge_authority_error == "INVALID":
        return (
            "BLOCKED_DATA_CONTRACT",
            "DATABRIDGE_CURRENT_INVALID",
            None,
        )
    authority = snapshot.databridge_authority
    if authority is None:
        return (
            "BLOCKED_NO_GENERATION",
            "DATABRIDGE_CURRENT_MISSING",
            None,
        )
    index = authority_index or _index_blackbox_gap_authority(snapshot)
    matching_cutoffs = index.cutoffs_by_feature_date.get(
        item.feature_date,
        (),
    )
    if len(matching_cutoffs) != 1:
        return (
            "BLOCKED_DATA_CONTRACT",
            "DATABRIDGE_CUTOFF_INVALID",
            None,
        )
    if index.source_identity_invalid or index.source_identity is None:
        return (
            "BLOCKED_DATA_CONTRACT",
            "DATABRIDGE_SOURCE_IDENTITY_INVALID",
            None,
        )
    cutoff = matching_cutoffs[0]
    return (
        "GRAY_LIVE_GAP",
        "LIVE_BUSINESS_KEY_MISSING",
        {
            **index.source_identity,
            "cutoff": _cutoff_payload(cutoff),
        },
    )


def _index_blackbox_gap_authority(
    snapshot: SignalGapSnapshot,
) -> _BlackboxGapAuthorityIndex:
    authority = snapshot.databridge_authority
    if authority is None:
        return _BlackboxGapAuthorityIndex({}, None, False)
    grouped: dict[str, list[StableDataBridgeCutoff]] = {}
    for cutoff in authority.cutoffs:
        grouped.setdefault(cutoff.feature_date, []).append(cutoff)
    try:
        source_identity = blackbox_gray_replay_source_identity(authority)
    except ValueError:
        source_identity = None
        invalid = True
    else:
        invalid = False
    return _BlackboxGapAuthorityIndex(
        {
            feature_date: tuple(cutoffs)
            for feature_date, cutoffs in grouped.items()
        },
        source_identity,
        invalid,
    )


def _cutoff_payload(cutoff: StableDataBridgeCutoff) -> dict[str, Any]:
    return {
        "feature_date": cutoff.feature_date,
        "daily_cutoff_key": cutoff.daily_cutoff_key,
        "weekly_cutoff_key": cutoff.weekly_cutoff_key,
        "monthly_cutoff_key": cutoff.monthly_cutoff_key,
        "source_weekly_cutoff_key": cutoff.source_weekly_cutoff_key,
        "source_monthly_cutoff_key": cutoff.source_monthly_cutoff_key,
    }


def _blocked_plan(
    *,
    predict_date: str,
    base_scheme_id: str | None,
    failure_code: str,
    blockers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    counts = {action: 0 for action in VALID_ACTIONS}
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": "BLOCKED",
        "failure_code": failure_code,
        "predict_date": predict_date,
        "base_scheme_id": base_scheme_id,
        "control_plane": {
            "read_only": True,
            "transaction_isolation": "REPEATABLE READ",
            "consistent_snapshot": True,
            "business_key_fields": [
                "base_scheme_id",
                "target_tenor",
                "horizon",
                "target_date",
            ],
            "blockers": [dict(item) for item in blockers],
        },
        "counts": {
            "active_target": 0,
            "expected": 0,
            "present": 0,
            "actionable": 0,
            "blocked": 1,
            **counts,
        },
        "actions": [],
    }


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
        "prediction_phase": "gray_live",
        "scheme_version": target.scheme_version,
        "business_key": list(item.business_key),
        "action": action,
        "reason": reason,
        "business_key_present": business_key_present,
        "input_authority": (
            dict(input_authority) if input_authority is not None else None
        ),
    }


def _not_due_action(
    target: RegistryTarget,
    *,
    predict_date: str,
) -> dict[str, Any]:
    return {
        "registry_scheme_id": target.registry_scheme_id,
        "base_scheme_id": target.base_scheme_id,
        "runtime_type": target.runtime_type,
        "frequency": target.frequency,
        "task_type": target.task_type,
        "target_tenor": target.target_tenor,
        "horizon": target.horizon,
        "predict_date": predict_date,
        "feature_date": None,
        "target_date": None,
        "prediction_phase": "gray_live",
        "scheme_version": target.scheme_version,
        "business_key": None,
        "action": "SKIP_NOT_DUE",
        "reason": "SCHEME_NOT_DUE",
        "business_key_present": False,
        "input_authority": None,
    }


def _discover_scheme_configs(project_root: Path | None = None) -> tuple[Any, ...]:
    if project_root is None:
        return tuple(discover_schemes())
    return tuple(discover_schemes(Path(project_root).resolve() / "schemes"))


def _select_execution_authority(
    configs: Sequence[Any],
    *,
    base_scheme_id: str | None,
) -> tuple[tuple[DiscoveredSchemeIdentity, ...], str | None]:
    by_base: dict[str, Any] = {}
    for config in configs:
        scheme_id = str(config.scheme_id)
        if scheme_id in by_base:
            raise SignalGapPlanError(
                "SCHEME_CONFIG_DUPLICATE",
                scheme_id,
            )
        by_base[scheme_id] = config
    if base_scheme_id is not None:
        selected = by_base.get(base_scheme_id)
        if selected is None:
            return (), "SCHEME_CONFIG_NOT_FOUND"
        if (
            str(selected.runtime_type) != "blackbox_v2"
            and str(selected.status) != "active"
        ):
            return (), "SCHEME_CONFIG_NOT_ACTIVE"
        selected_configs = (selected,)
    else:
        selected_configs = tuple(
            config
            for config in configs
            if str(config.runtime_type) == "blackbox_v2"
            or str(config.status) == "active"
        )
    authority = tuple(
        sorted(
            (
                DiscoveredSchemeIdentity(
                    base_scheme_id=str(config.scheme_id),
                    scheme_version=str(config.scheme_version),
                    runtime_type=str(config.runtime_type),
                    frequency=str(config.frequency),
                    horizon=int(config.horizon),
                    task_type=str(config.task_type),
                    target_tenors=tuple(
                        sorted(str(item) for item in config.tenors)
                    ),
                    version_status=str(config.version_status),
                    status=str(config.status),
                )
                for config in selected_configs
            ),
            key=lambda item: item.base_scheme_id,
        )
    )
    for item in authority:
        if (
            item.runtime_type not in ALLOWED_RUNTIME_TYPES
            or item.frequency not in ALLOWED_FREQUENCIES
            or item.horizon < 1
            or not item.task_type.strip()
            or not item.target_tenors
            or len(set(item.target_tenors)) != len(item.target_tenors)
            or not item.version_status.strip()
            or not item.scheme_version.strip()
        ):
            raise SignalGapPlanError(
                "SCHEME_CONFIG_INVALID",
                item.base_scheme_id,
            )
    return authority, None


def _read_registry_targets(
    connection: Any,
    *,
    execution_authority: Sequence[DiscoveredSchemeIdentity],
    targeted: bool,
) -> tuple[tuple[RegistryTarget, ...], tuple[Mapping[str, Any], ...]]:
    if not execution_authority:
        return (), ()
    base_ids = sorted(item.base_scheme_id for item in execution_authority)
    registry_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT r.scheme_id, r.base_scheme_id, r.runtime_type,
                       r.frequency, r.task_type, r.target_tenor, r.horizon,
                       r.status
                FROM t_scheme_registry r
                WHERE r.base_scheme_id IN :base_scheme_ids
                ORDER BY r.scheme_id
                """
            ).bindparams(bindparam("base_scheme_ids", expanding=True)),
            {"base_scheme_ids": base_ids},
        ).mappings().all()
    ]
    version_rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT v.scheme_id, v.scheme_version, v.runtime_type,
                       v.status
                FROM t_scheme_versions v
                WHERE v.scheme_id IN :base_scheme_ids
                ORDER BY v.scheme_id, v.scheme_version
                """
            ).bindparams(bindparam("base_scheme_ids", expanding=True)),
            {"base_scheme_ids": base_ids},
        ).mappings().all()
    ]
    registries_by_base: dict[str, list[dict[str, Any]]] = {}
    for row in registry_rows:
        registries_by_base.setdefault(str(row["base_scheme_id"]), []).append(row)
    versions_by_base: dict[str, list[dict[str, Any]]] = {}
    for row in version_rows:
        versions_by_base.setdefault(str(row["scheme_id"]), []).append(row)

    targets: list[RegistryTarget] = []
    blockers: list[Mapping[str, Any]] = []
    for identity in execution_authority:
        if (
            identity.runtime_type != "blackbox_v2"
            and identity.version_status != "active"
        ):
            blockers.append(
                {
                    "code": "SCHEME_CONFIG_VERSION_NOT_ACTIVE",
                    "base_scheme_id": identity.base_scheme_id,
                }
            )
            continue
        active_versions = [
            row
            for row in versions_by_base.get(identity.base_scheme_id, ())
            if str(row.get("status") or "") == "active"
        ]
        if identity.runtime_type == "blackbox_v2" and len(active_versions) != 1:
            if targeted:
                blockers.append(
                    {
                        "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
                        "base_scheme_id": identity.base_scheme_id,
                    }
                )
            continue
        exact_active_versions = [
            row
            for row in active_versions
            if str(row.get("scheme_version") or "") == identity.scheme_version
        ]
        if len(exact_active_versions) != 1:
            if targeted or identity.runtime_type != "blackbox_v2":
                blockers.append(
                    {
                        "code": "ACTIVE_VERSION_EXACT_IDENTITY_MISSING",
                        "base_scheme_id": identity.base_scheme_id,
                    }
                )
            continue
        if str(exact_active_versions[0]["runtime_type"]) != identity.runtime_type:
            blockers.append(
                {
                    "code": "ACTIVE_VERSION_RUNTIME_DRIFT",
                    "base_scheme_id": identity.base_scheme_id,
                }
            )
            continue
        active_registry = [
            row
            for row in registries_by_base.get(identity.base_scheme_id, ())
            if str(row.get("status") or "") == "active"
        ]
        if not active_registry:
            blockers.append(
                {
                    "code": "ACTIVE_REGISTRY_TARGET_MISSING",
                    "base_scheme_id": identity.base_scheme_id,
                }
            )
            continue
        expected_registry_identity = sorted(
            (
                f"{identity.base_scheme_id}__h{identity.horizon}__{tenor}",
                identity.base_scheme_id,
                identity.runtime_type,
                identity.frequency,
                identity.task_type,
                tenor,
                identity.horizon,
            )
            for tenor in identity.target_tenors
        )
        actual_registry_identity = sorted(
            (
                str(row["scheme_id"]),
                str(row["base_scheme_id"]),
                str(row["runtime_type"]),
                str(row["frequency"]),
                str(row["task_type"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            for row in active_registry
        )
        if actual_registry_identity != expected_registry_identity:
            blockers.append(
                {
                    "code": "ACTIVE_REGISTRY_CONFIG_IDENTITY_MISMATCH",
                    "base_scheme_id": identity.base_scheme_id,
                }
            )
            continue
        for row in active_registry:
            try:
                target = RegistryTarget(
                    registry_scheme_id=str(row["scheme_id"]),
                    base_scheme_id=identity.base_scheme_id,
                    runtime_type=str(row["runtime_type"]),
                    frequency=str(row["frequency"]),
                    task_type=str(row["task_type"]),
                    target_tenor=str(row["target_tenor"]),
                    horizon=int(row["horizon"]),
                    scheme_version=identity.scheme_version,
                )
                _validate_registry_target(target)
                if (
                    target.runtime_type != identity.runtime_type
                    or target.frequency != identity.frequency
                ):
                    raise SignalGapPlanError(
                        "INVALID_REGISTRY_TARGET",
                        "runtime_type drift",
                    )
            except (SignalGapPlanError, TypeError, ValueError):
                blockers.append(
                    {
                        "code": "INVALID_REGISTRY_TARGET",
                        "base_scheme_id": identity.base_scheme_id,
                    }
                )
                continue
            targets.append(target)
    return (
        tuple(sorted(targets, key=lambda item: item.registry_scheme_id)),
        tuple(_normalize_blockers(blockers)),
    )


def _read_live_signals(
    connection: Any,
    targets: Sequence[RegistryTarget],
    *,
    predict_date: str,
    expected_business_keys: set[tuple[str, str, int, str]],
) -> tuple[ObservedSignal, ...]:
    return _read_live_signals_for_scope(
        connection,
        targets,
        predict_dates=(predict_date,),
        expected_business_keys=expected_business_keys,
    )


def _read_live_signals_for_scope(
    connection: Any,
    targets: Sequence[RegistryTarget],
    *,
    predict_dates: Sequence[str],
    expected_business_keys: set[tuple[str, str, int, str]],
) -> tuple[ObservedSignal, ...]:
    if not targets:
        return ()
    base_ids = sorted({target.base_scheme_id for target in targets})
    normalized_predict_dates = sorted(set(predict_dates))
    if not normalized_predict_dates:
        raise SignalGapPlanError(
            "DATA_CONTRACT_INVALID",
            "live signal scope requires predict dates",
        )
    expected_target_dates = sorted(
        {business_key[3] for business_key in expected_business_keys}
    )
    target_date_clause = (
        " OR p.target_date IN :expected_target_dates"
        if expected_target_dates
        else ""
    )
    statement = text(
        f"""
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
        WHERE p.scheme_id IN :base_scheme_ids
          AND (
              p.predict_date IN :predict_dates
              {target_date_clause}
          )
        ORDER BY p.scheme_id, p.target_tenor, p.horizon,
                 p.target_date, p.predict_date, p.id
        """
    )
    bind_parameters = [
        bindparam("base_scheme_ids", expanding=True),
        bindparam("predict_dates", expanding=True),
    ]
    parameters: dict[str, Any] = {
        "base_scheme_ids": base_ids,
        "predict_dates": normalized_predict_dates,
    }
    if expected_target_dates:
        bind_parameters.append(
            bindparam("expected_target_dates", expanding=True)
        )
        parameters["expected_target_dates"] = expected_target_dates
    statement = statement.bindparams(*bind_parameters)
    rows = [
        dict(row)
        for row in connection.execute(
            statement,
            parameters,
        ).mappings().all()
    ]
    target_by_identity = {
        target.business_identity: target for target in targets
    }
    selected: list[ObservedSignal] = []
    for row in rows:
        business_key = (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"])[:10],
        )
        row_predict_date = str(row["predict_date"])[:10]
        if (
            business_key not in expected_business_keys
            and row_predict_date not in normalized_predict_dates
        ):
            continue
        target = target_by_identity.get(business_key[:3])
        selected.append(
            ObservedSignal(
                base_scheme_id=business_key[0],
                target_tenor=business_key[1],
                horizon=business_key[2],
                target_date=business_key[3],
                predict_date=row_predict_date,
                feature_date=str(row["feature_date"])[:10],
                phase=str(row["prediction_phase"] or ""),
                scheme_version=str(row["scheme_version"] or ""),
                run_status=str(row["run_status"] or ""),
                contract_error=_live_row_contract_error(row, target),
            )
        )
    return tuple(sorted(selected, key=_observed_sort_key))


class _SingleDateCalendar:
    """只使用 connection-bound 日历快照的 live context 适配器。"""

    def __init__(
        self,
        *,
        trade_calendar_rows: Sequence[Mapping[str, Any]],
        week_calendar_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        self._period_rows = tuple(dict(row) for row in trade_calendar_rows)
        self._trading_days = tuple(
            sorted(
                str(row["rdate"])[:10]
                for row in trade_calendar_rows
                if is_trading_day_row(str(row["rdate"])[:10], row.get("trade_flag"))
            )
        )
        if not self._trading_days:
            raise SignalGapPlanError(
                "DATA_CONTRACT_INVALID",
                "trade calendar has no trading days",
            )
        self._trading_day_set = frozenset(self._trading_days)
        self._calendar_date_set = frozenset(
            str(row["rdate"])[:10] for row in trade_calendar_rows
        )
        self._week_calendar = build_week_calendar(week_calendar_rows)

    def covers(self, value: str) -> bool:
        return str(value)[:10] in self._calendar_date_set

    def is_trading_day(self, value: str) -> bool:
        return str(value)[:10] in self._trading_day_set

    def trading_days(self) -> tuple[str, ...]:
        return self._trading_days

    def previous_trading_day(self, value: str) -> str:
        candidates = [day for day in self._trading_days if day < str(value)[:10]]
        if not candidates:
            raise ValueError(f"no previous trading day before {value}")
        return candidates[-1]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        return [day for day in self._trading_days if day > str(value)[:10]][:count]

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

    def period_calendar_rows(self) -> tuple[dict[str, Any], ...]:
        return self._period_rows


def _read_calendar(connection: Any) -> _SingleDateCalendar:
    snapshot = read_calendar_snapshot_from_connection(connection)
    trade_rows = snapshot["t_trade_calendar.csv"].to_dict("records")
    trade_flags = {
        str(row["rdate"])[:10]: row["trade_flag"] for row in trade_rows
    }
    week_rows = [
        {
            "rdate": row["rdate"],
            "week_id": row["week_id"],
            "trade_flag": trade_flags.get(str(row["rdate"])[:10], "0"),
        }
        for row in snapshot["api_wind_date.csv"].to_dict("records")
    ]
    return _SingleDateCalendar(
        trade_calendar_rows=trade_rows,
        week_calendar_rows=week_rows,
    )


def _is_due(
    target: RegistryTarget,
    *,
    predict_date: str,
    calendar: _SingleDateCalendar,
) -> bool:
    return _is_frequency_due(
        target.frequency,
        predict_date=predict_date,
        calendar=calendar,
        task_type=target.task_type,
    )


def _case_is_in_platform_live_scope(case: ExpectedSignalCase) -> bool:
    """按任务的业务目标身份判断是否进入平台实盘区间。"""
    if case.task_type == "quarterly_average":
        pointer = date.fromisoformat(case.target_date)
        live_start = date.fromisoformat(PLATFORM_LIVE_TARGET_START_DATE)
        target_quarter = (pointer.year, (pointer.month - 1) // 3 + 1)
        live_start_quarter = (
            live_start.year,
            (live_start.month - 1) // 3 + 1,
        )
        return target_quarter >= live_start_quarter
    if case.task_type == "annual_average":
        pointer = date.fromisoformat(case.target_date)
        live_start = date.fromisoformat(PLATFORM_LIVE_TARGET_START_DATE)
        return pointer.year >= live_start.year
    if case.task_type != "monthly_average":
        return case.target_date >= PLATFORM_LIVE_TARGET_START_DATE
    pointer = date.fromisoformat(case.target_date)
    if pointer.month == 12:
        target_month = f"{pointer.year + 1:04d}-01"
    else:
        target_month = f"{pointer.year:04d}-{pointer.month + 1:02d}"
    return target_month >= PLATFORM_LIVE_TARGET_START_DATE[:7]


def _is_frequency_due(
    frequency: str,
    *,
    predict_date: str,
    calendar: Any,
    task_type: str | None = None,
) -> bool:
    if task_type in PERIOD_AVERAGE_TASK_TYPES:
        try:
            return predict_date in period_anchor_dates(
                task_type,
                calendar.period_calendar_rows(),
                start_date=predict_date,
                end_date=predict_date,
            )
        except ValueError as exc:
            raise SignalGapPlanError("DATA_CONTRACT_INVALID", str(exc)) from exc
    if frequency == "daily":
        return calendar.is_trading_day(predict_date)
    if frequency == "weekly":
        return is_weekly_signal_date(calendar, predict_date)
    if frequency == "monthly":
        return date.fromisoformat(predict_date).day == 15
    raise SignalGapPlanError(
        "INVALID_REGISTRY_TARGET",
        f"unsupported frequency={frequency!r}",
    )


def _due_execution_authority(
    authority: Sequence[Any],
    *,
    predict_date: str,
    calendar: Any,
    targeted: bool,
) -> tuple[Any, ...]:
    if targeted:
        return tuple(authority)
    return tuple(
        item
        for item in authority
        if _is_frequency_due(
            str(item.frequency),
            predict_date=predict_date,
            calendar=calendar,
            task_type=str(getattr(item, "task_type", "") or ""),
        )
    )


def _expected_case(
    target: RegistryTarget,
    *,
    predict_date: str,
    calendar: _SingleDateCalendar,
) -> ExpectedSignalCase:
    if target.task_type in PERIOD_AVERAGE_TASK_TYPES:
        context = build_period_average_live_context(
            calendar,
            predict_date,
            task_type=target.task_type,
        )
    elif target.frequency == "daily":
        context = build_daily_live_context(
            calendar,
            predict_date,
            horizon=target.horizon,
        )
    elif target.frequency == "weekly":
        context = build_weekly_live_context(calendar, predict_date)
    elif target.frequency == "monthly":
        context = build_monthly_live_context(calendar, predict_date)
    else:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            target.frequency,
        )
    return ExpectedSignalCase(
        registry_scheme_id=target.registry_scheme_id,
        base_scheme_id=target.base_scheme_id,
        runtime_type=target.runtime_type,
        frequency=target.frequency,
        task_type=target.task_type,
        target_tenor=target.target_tenor,
        horizon=target.horizon,
        predict_date=predict_date,
        feature_date=_canonical_date(context.feature_date, "feature_date"),
        target_date=_canonical_date(context.target_date, "target_date"),
    )


def _validate_snapshot(
    snapshot: SignalGapSnapshot,
    *,
    predict_date: str,
) -> dict[str, RegistryTarget]:
    targets: dict[str, RegistryTarget] = {}
    for target in snapshot.registry_targets:
        _validate_registry_target(target)
        if target.registry_scheme_id in targets:
            raise SignalGapPlanError(
                "DUPLICATE_REGISTRY_TARGET",
                target.registry_scheme_id,
            )
        targets[target.registry_scheme_id] = target
    seen_cases: set[tuple[str, str, int, str]] = set()
    for item in snapshot.expected_cases:
        if item.predict_date != predict_date:
            raise SignalGapPlanError(
                "EXPECTED_CASE_DATE_DRIFT",
                item.registry_scheme_id,
            )
        target = targets.get(item.registry_scheme_id)
        if target is None or any(
            (
                item.base_scheme_id != target.base_scheme_id,
                item.runtime_type != target.runtime_type,
                item.frequency != target.frequency,
                item.task_type != target.task_type,
                item.target_tenor != target.target_tenor,
                item.horizon != target.horizon,
            )
        ):
            raise SignalGapPlanError(
                "EXPECTED_CASE_REGISTRY_DRIFT",
                item.registry_scheme_id,
            )
        if item.business_key in seen_cases:
            raise SignalGapPlanError(
                "DUPLICATE_EXPECTED_BUSINESS_KEY",
                item.registry_scheme_id,
            )
        seen_cases.add(item.business_key)
    return targets


def _validate_registry_target(target: RegistryTarget) -> None:
    if target.runtime_type == "native_adapter":
        contract = NATIVE_TASK_COMBINATIONS.get(target.task_type)
    elif target.runtime_type == "blackbox_v2":
        raw = TASK_COMBINATIONS.get(target.task_type)
        contract = (raw[0], raw[2]) if raw is not None else None
    else:
        contract = None
    if contract != (target.horizon, target.frequency):
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            target.registry_scheme_id,
        )
    expected_id = (
        f"{target.base_scheme_id}__h{target.horizon}__{target.target_tenor}"
    )
    if target.registry_scheme_id != expected_id or not target.scheme_version.strip():
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            target.registry_scheme_id,
        )


def _observation_contract_error(
    item: ExpectedSignalCase,
    target: RegistryTarget,
    row: ObservedSignal,
) -> str | None:
    if row.contract_error:
        return f"OBSERVED_SIGNAL_CONTRACT_DRIFT:{row.contract_error}"
    if (
        row.predict_date != item.predict_date
        or row.feature_date != item.feature_date
        or row.phase not in LIVE_PREDICTION_PHASES
        or row.run_status != "success"
        or row.scheme_version != target.scheme_version
    ):
        return "OBSERVED_SIGNAL_CONTRACT_DRIFT"
    return None


def _live_row_contract_error(
    row: Mapping[str, Any],
    target: RegistryTarget | None,
) -> str | None:
    errors: list[str] = []
    phase = str(row.get("prediction_phase") or "")
    if phase not in LIVE_PREDICTION_PHASES:
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
    if str(row.get("run_scheme_id") or "") != str(row.get("scheme_id") or ""):
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


def _normalize_blockers(
    blockers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in blockers:
        code = str(raw.get("code") or "").strip()
        base_scheme_id = str(raw.get("base_scheme_id") or "").strip()
        if not code or not base_scheme_id:
            raise SignalGapPlanError(
                "CONTROL_PLANE_BLOCKER_INVALID",
                "blocker requires code and base_scheme_id",
            )
        key = (base_scheme_id, code)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(dict(raw))
    return sorted(
        normalized,
        key=lambda item: (str(item["base_scheme_id"]), str(item["code"])),
    )


def _normalize_base_scheme_id(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or "," in value
        or re.fullmatch(
            r"[a-z][a-z0-9_]*__h\d+__(?:1Y|3Y|5Y|7Y|10Y)",
            value,
        )
        is not None
        or SCHEME_ID_PATTERN.fullmatch(value) is None
    ):
        raise SignalGapPlanError(
            "INVALID_BASE_SCHEME_ID",
            "base_scheme_id must be one exact base scheme id",
        )
    return value


def _canonical_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SignalGapPlanError(
            "INVALID_DATE",
            f"{field} must use canonical YYYY-MM-DD",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SignalGapPlanError(
            "INVALID_DATE",
            f"{field} must use canonical YYYY-MM-DD",
        ) from exc
    if parsed.isoformat() != value:
        raise SignalGapPlanError(
            "INVALID_DATE",
            f"{field} must use canonical YYYY-MM-DD",
        )
    return value


def _case_sort_key(item: ExpectedSignalCase) -> tuple[Any, ...]:
    return (
        item.base_scheme_id,
        item.target_tenor,
        item.horizon,
        item.target_date,
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
