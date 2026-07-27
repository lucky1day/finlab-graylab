from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Literal, Mapping, Sequence

from sqlalchemy import bindparam, text

from backend.factor_lab_dashboard_semantics import (
    choose_latest_backtest_runs,
    choose_live_prediction_rows,
)
from scheduler.capacity_attestation import canonical_json_sha256
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


SnapshotReader = Callable[..., SignalGapSnapshot]


def plan_signal_gaps(
    engine: Any,
    *,
    start_date: str,
    as_of_date: str,
    snapshot_reader: SnapshotReader | None = None,
) -> dict[str, Any]:
    """在一个 RR consistent snapshot/read-only 事务中规划全部缺口。"""
    normalized_start = _canonical_date(start_date, "start_date")
    normalized_as_of = _canonical_date(as_of_date, "as_of_date")
    if normalized_start > normalized_as_of:
        raise SignalGapPlanError(
            "INVALID_DATE_RANGE",
            "start_date must not be after as_of_date",
        )
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
    canonical = _index_observations(
        tuple(
            row
            for row in snapshot.canonical_signals
            if normalized_start <= row.predict_date <= normalized_as_of
        )
    )
    live = _index_observations(
        tuple(
            row
            for row in snapshot.live_signals
            if normalized_start <= row.predict_date <= normalized_as_of
        )
    )
    expected_keys = {
        (case.segment, case.business_key)
        for case in snapshot.expected_cases
        if case.predict_date >= normalized_start
        and case.predict_date <= normalized_as_of
    }
    unexpected = sorted(
        {
            ("canonical", key)
            for key in canonical
            if ("canonical", key) not in expected_keys
        }
        | {
            ("live", key)
            for key in live
            if ("live", key) not in expected_keys
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
        },
        "source_snapshot": {
            "identity_sha256": snapshot.source_identity_sha256,
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
) -> SignalGapSnapshot:
    """读取真实 active/version/result 快照并建立可审计 authority。"""
    raw_registry, registry_targets = _read_registry_versions(connection)
    raw_live, live_signals = _read_live_signals(
        connection,
        registry_targets,
        as_of_date=as_of_date,
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
) -> tuple[list[dict[str, Any]], tuple[RegistryTarget, ...]]:
    rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT r.scheme_id, r.base_scheme_id, r.runtime_type,
                       r.frequency, r.task_type, r.target_tenor, r.horizon,
                       v.scheme_version, v.code_hash, v.config_hash
                FROM t_scheme_registry r
                JOIN t_scheme_versions v
                  ON v.scheme_id = r.base_scheme_id
                 AND v.status = 'active'
                WHERE r.status = 'active'
                ORDER BY r.scheme_id, v.scheme_version
                """
            )
        ).mappings().all()
    ]
    targets = tuple(
        RegistryTarget(
            registry_scheme_id=str(row["scheme_id"]),
            base_scheme_id=str(row["base_scheme_id"]),
            runtime_type=str(row["runtime_type"]),
            frequency=str(row["frequency"]),
            task_type=str(row["task_type"]),
            target_tenor=str(row["target_tenor"]),
            horizon=int(row["horizon"]),
            scheme_version=str(row["scheme_version"]),
            live_target_start_date=PLATFORM_LIVE_TARGET_START_DATE,
            live_boundary_source=PLATFORM_LIVE_BOUNDARY_VERSION,
            input_mode=(
                "live_source_0629"
                if str(row["base_scheme_id"])
                in APPROVED_0629_LIVE_SOURCE_SCHEMES
                else None
            ),
            code_sha256=_optional_text(row.get("code_hash")),
            config_sha256=_optional_text(row.get("config_hash")),
        )
        for row in rows
    )
    return rows, targets


def _read_live_signals(
    connection: Any,
    targets: Sequence[RegistryTarget],
    *,
    as_of_date: str,
) -> tuple[list[dict[str, Any]], tuple[ObservedSignal, ...]]:
    base_ids = sorted({target.base_scheme_id for target in targets})
    rows = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT p.id, p.scheme_id, p.target_tenor, p.horizon,
                       p.predict_date, p.feature_date, p.target_date,
                       p.prediction_phase, p.scheme_version, p.run_id,
                       r.status AS run_status
                FROM t_scheme_predictions p
                LEFT JOIN t_scheme_runs r ON r.run_id = p.run_id
                WHERE p.scheme_id IN :scheme_ids
                  AND p.prediction_phase IN
                      ('gray_live', 'scheduled_live')
                ORDER BY p.scheme_id, p.target_tenor, p.horizon,
                         p.target_date, p.predict_date, p.id
                """
            ).bindparams(bindparam("scheme_ids", expanding=True)),
            {"scheme_ids": base_ids},
        ).mappings().all()
    ]
    selected = choose_live_prediction_rows(
        rows,
        display_until=as_of_date,
    )
    multiplicity: dict[tuple[str, str, int, str], int] = {}
    for row in rows:
        key = (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"])[:10],
        )
        multiplicity[key] = multiplicity.get(key, 0) + 1
    target_keys = {
        target.business_identity for target in targets
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
            contract_error=(
                "DUPLICATE_LIVE_BUSINESS_KEY"
                if multiplicity[
                    (
                        str(row["scheme_id"]),
                        str(row["target_tenor"]),
                        int(row["horizon"]),
                        str(row["target_date"])[:10],
                    )
                ]
                > 1
                else None
            ),
        )
        for row in selected
        if (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        in target_keys
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
    valid_present: bool,
    observation_error: str | None,
    generations: Sequence[InputGeneration],
    as_of_date: str,
) -> tuple[Action, InputGeneration | None, str]:
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
    expected = TASK_COMBINATIONS.get(target.task_type)
    if expected is None:
        raise SignalGapPlanError(
            "INVALID_REGISTRY_TARGET",
            f"unsupported task_type={target.task_type!r}",
        )
    expected_horizon, _, expected_frequency = expected
    valid_horizon = (
        target.horizon == expected_horizon
        or (
            target.runtime_type == "native_adapter"
            and target.frequency == "weekly"
            and target.horizon == 6
        )
    )
    if not valid_horizon or target.frequency != expected_frequency:
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
    expected_count = summary.get(
        "persisted_prediction_count",
        summary.get("row_count"),
    )
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
        or expected_count != len(details)
    ):
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
