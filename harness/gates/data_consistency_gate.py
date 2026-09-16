from __future__ import annotations

import hashlib
import json
import re
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.dashboard_gate import ApiProbeError, fetch_json
from harness.http_target import validate_api_target
from harness.result import Evidence, GateResult, GateStatus
from harness.signal_gap_plan import (
    RegistryTarget,
    SignalGapPlanError,
    expected_signal_case_from_calendar_rows,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import registry_scheme_id
from shared.historical_reference_compatibility import (
    HistoricalBacktestReference,
    load_historical_backtest_references,
)
from shared.legacy_prediction_migration import (
    LegacyCorrectedExactEvidence,
    LegacyPredictionMigration,
    load_legacy_corrected_exact_evidence,
    load_legacy_prediction_migrations,
)
from shared.legacy_backtest_lineage import (
    LegacyBacktestLineage,
    load_legacy_backtest_lineages,
)
from shared.prediction_history_replacement import (
    PredictionHistoryReplacementError,
    resolve_prediction_history_projection,
)
from shared.calendar_service import is_trading_day_row
from shared.period_average_buckets import build_period_buckets
from shared.prediction_context import (
    LIVE_PREDICTION_PHASES,
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import (
    TASK_COMBINATIONS,
    load_legacy_native_scheme_ids,
    runtime_task_contract,
)
from shared.week_calendar_normalizer import normalize_week_calendar_rows


HISTORY_START_DATE = "2025-01-01"
LIVE_TARGET_START_DATE = date(2026, 6, 1)
MAX_RESPONSE_BYTES = 1_500_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MONTHLY_ROW_FIELDS = (
    "month",
    "source",
    "samples",
    "metric_samples",
    "correct",
    "predicted_up",
    "predicted_down",
    "predicted_flat",
    "actual_up",
    "actual_down",
    "actual_flat",
    "up_true_positive",
    "down_true_positive",
)
DETAIL_ROW_FIELDS = (
    "source",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
    "actual_direction",
)


@dataclass(frozen=True, slots=True)
class ConsistencyFact:
    scheme_id: str
    target_tenor: str
    horizon: int
    task_type: str
    predict_date: str
    feature_date: str
    target_date: str
    predicted_direction: int
    actual_direction: int | None


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    registry_rows: tuple[dict[str, Any], ...]
    prediction_rows: tuple[dict[str, Any], ...]
    actual_rows: tuple[dict[str, Any], ...]
    live_runs: tuple[dict[str, Any], ...]
    backtest_runs: tuple[dict[str, Any], ...]
    calendar_rows: tuple[dict[str, Any], ...]
    digest: str
    backtest_prediction_rows: tuple[dict[str, Any], ...] = ()
    version_rows: tuple[dict[str, Any], ...] = ()
    input_artifact_rows: tuple[dict[str, Any], ...] = ()
    actual_watermarks: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class _LegacyMigrationMatch:
    entry: LegacyPredictionMigration
    backtest_run_id: int
    mismatch_business_keys: frozenset[tuple[str, str, int, str]]


@dataclass(frozen=True, slots=True)
class _LegacyBacktestLineageMatch:
    entry: LegacyBacktestLineage
    backtest_run_id: int


@dataclass(frozen=True, slots=True)
class _FrozenCalendar:
    rows: tuple[dict[str, Any], ...]
    trading_days: tuple[str, ...]
    covered_dates: frozenset[str]
    week_ids_by_date: Mapping[str, int]
    week_last_trading_days: Mapping[int, str]

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]]) -> _FrozenCalendar:
        if not rows:
            raise DataConsistencyError("trade calendar snapshot is empty")
        canonical: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        for raw in rows:
            rdate = _iso_date(raw.get("rdate"), field="calendar rdate")
            if rdate in seen_dates:
                raise DataConsistencyError(
                    f"duplicate trade calendar date: {rdate}"
                )
            seen_dates.add(rdate)
            canonical.append(
                {
                    "rdate": rdate,
                    "trade_flag": raw.get("trade_flag"),
                    "week_id": raw.get("week_id"),
                }
            )
        canonical.sort(key=lambda item: item["rdate"])
        first = date.fromisoformat(canonical[0]["rdate"])
        last = date.fromisoformat(canonical[-1]["rdate"])
        if len(canonical) != (last - first).days + 1:
            raise DataConsistencyError("trade calendar snapshot is not contiguous")
        trading_days = tuple(
            item["rdate"]
            for item in canonical
            if is_trading_day_row(item["rdate"], item["trade_flag"])
        )
        if not trading_days:
            raise DataConsistencyError("trade calendar snapshot has no trading days")
        normalized_week_rows = normalize_week_calendar_rows(canonical)
        week_ids_by_date: dict[str, int] = {}
        week_last_trading_days: dict[int, str] = {}
        for item in normalized_week_rows:
            raw_week_id = item.get("week_id")
            if raw_week_id is None:
                continue
            try:
                week_id = int(str(raw_week_id).replace(".0", ""))
            except ValueError as exc:
                raise DataConsistencyError(
                    f"invalid calendar week_id for {item.get('rdate')!r}"
                ) from exc
            rdate = str(item["rdate"])
            week_ids_by_date[rdate] = week_id
            if is_trading_day_row(rdate, item.get("trade_flag")):
                previous = week_last_trading_days.get(week_id)
                if previous is None or rdate > previous:
                    week_last_trading_days[week_id] = rdate
        return cls(
            rows=tuple(canonical),
            trading_days=trading_days,
            covered_dates=frozenset(seen_dates),
            week_ids_by_date=week_ids_by_date,
            week_last_trading_days=week_last_trading_days,
        )

    def previous_trading_day(self, value: str) -> str:
        position = bisect_left(self.trading_days, value)
        if position == 0:
            raise DataConsistencyError(
                f"trade calendar has no trading day before {value}"
            )
        return self.trading_days[position - 1]

    def is_trading_day(self, value: str) -> bool:
        position = bisect_left(self.trading_days, value)
        return (
            position < len(self.trading_days)
            and self.trading_days[position] == value
        )

    def nth_trading_day_after(self, value: str, count: int) -> str:
        position = bisect_right(self.trading_days, value)
        target = position + count - 1
        if count <= 0 or target >= len(self.trading_days):
            raise DataConsistencyError(
                f"trade calendar has fewer than {count} trading days after {value}"
            )
        return self.trading_days[target]

    def last_trading_day_on_or_before(self, value: str) -> str:
        position = bisect_right(self.trading_days, value)
        if position == 0:
            raise DataConsistencyError(
                f"trade calendar has no trading day on or before {value}"
            )
        return self.trading_days[position - 1]

    def next_week_last_trading_day(self, feature_date: str) -> str:
        feature_week_id = self.week_ids_by_date.get(feature_date)
        if feature_week_id is None:
            raise DataConsistencyError(
                f"trade calendar has no week_id for feature_date={feature_date}"
            )
        if self.week_last_trading_days.get(feature_week_id) != feature_date:
            raise DataConsistencyError(
                f"weekly feature_date is not its week last trading day: {feature_date}"
            )
        position = bisect_right(self.trading_days, feature_date)
        for candidate in self.trading_days[position:]:
            candidate_week_id = self.week_ids_by_date.get(candidate)
            if (
                candidate_week_id is not None
                and candidate_week_id != feature_week_id
            ):
                return self.week_last_trading_days[candidate_week_id]
        raise DataConsistencyError(
            f"trade calendar has no target week after feature_date={feature_date}"
        )


class DataConsistencyError(ValueError):
    """表示数据库事实或公开表示违反数据一致性合同。"""


class DataConsistencyTimeout(DataConsistencyError):
    """表示独立对账已经耗尽本次调用的总时间预算。"""


class DataConsistencyAuthorityUnavailable(DataConsistencyError):
    """表示无法从冻结事实建立权威预期，Gate 必须 BLOCKED。"""


def _subresult(
    status: str,
    errors: Sequence[str],
    **details: Any,
) -> dict[str, Any]:
    if status not in {"PASSED", "FAILED", "BLOCKED"}:
        raise ValueError("invalid data consistency subresult status")
    return {"status": status, "errors": list(errors), **details}


def _merge_subresult_error(
    result: Mapping[str, Any],
    error: str,
    status: str,
) -> dict[str, Any]:
    merged = dict(result)
    merged["status"] = status
    merged["errors"] = [*result.get("errors", []), error]
    return merged


class DataConsistencyGate(Gate):
    """独立对账预测、Actual 与 Dashboard Summary/Detail。"""

    name = "data-consistency"

    def __init__(
        self,
        *,
        fetcher=fetch_json,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))
        self._monotonic = monotonic or time.monotonic

    def run(self, ctx: GateContext) -> GateResult:
        result = guarded_result(
            self.name,
            lambda started_at: self._run(ctx, started_at),
        )
        return _redact_gate_result(result, ctx.dashboard_session_token)

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        deadline = self._monotonic() + ctx.timeout_sec
        _remaining_budget(deadline, self._monotonic)
        selected_base_ids, registry_ids = _selected_registry_ids(ctx)
        if ctx.engine_factory is None:
            raise ValueError(
                "data consistency gate requires same-host engine_factory"
            )
        target = validate_api_target(ctx.api_base_url, ctx.api_prefix)
        summary_url = target.url("/api/factor-lab/dashboard")
        display_until = _clock_display_date(self._clock)
        engine = ctx.engine_factory()
        final_snapshot: DatabaseSnapshot | None = None
        final_snapshot_failed = False
        operation_error: Exception | None = None
        summary: Mapping[str, Any] | None = None
        summary_metadata: Mapping[str, Any] = {}
        validated_facts: list[ConsistencyFact] = []
        representation_facts: list[ConsistencyFact] = []
        detail_metadata: list[dict[str, Any]] = []
        completeness = _subresult("BLOCKED", ["completeness not evaluated"])
        lineage = _subresult("BLOCKED", ["lineage not evaluated"])
        representation = _subresult(
            "BLOCKED", ["representation not evaluated"]
        )
        try:
            _remaining_budget(deadline, self._monotonic)
            snapshot = _capture_snapshot(
                engine,
                registry_ids,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            _remaining_budget(deadline, self._monotonic)
            legacy_migrations = load_legacy_prediction_migrations(
                PROJECT_ROOT
            )
            corrected_exact_evidence = (
                load_legacy_corrected_exact_evidence(PROJECT_ROOT)
            )
            legacy_backtest_lineages = load_legacy_backtest_lineages(
                PROJECT_ROOT
            )
            historical_references = load_historical_backtest_references(
                PROJECT_ROOT
            )
            try:
                completeness_errors, completeness_details = (
                    validate_snapshot_completeness(
                        snapshot,
                        display_until=display_until,
                        historical_references=historical_references,
                        legacy_migrations=legacy_migrations,
                        corrected_exact_evidence=corrected_exact_evidence,
                    )
                )
                completeness_authority_gaps = completeness_details.get(
                    "authority_gaps", []
                )
                completeness = _subresult(
                    (
                        "FAILED"
                        if completeness_errors
                        else "BLOCKED"
                        if completeness_authority_gaps
                        else "PASSED"
                    ),
                    [
                        *completeness_errors,
                        *completeness_authority_gaps,
                    ],
                    **completeness_details,
                )
            except DataConsistencyAuthorityUnavailable as exc:
                completeness = _subresult("BLOCKED", [str(exc)])
            except DataConsistencyError as exc:
                completeness = _subresult("FAILED", [str(exc)])

            try:
                lineage_errors, lineage_details = validate_snapshot_lineage(
                    snapshot,
                    historical_references=historical_references,
                    legacy_migrations=legacy_migrations,
                    corrected_exact_evidence=corrected_exact_evidence,
                    legacy_backtest_lineages=legacy_backtest_lineages,
                )
                lineage_authority_gaps = lineage_details.get(
                    "authority_gaps", []
                )
                lineage = _subresult(
                    (
                        "FAILED"
                        if lineage_errors
                        else "BLOCKED"
                        if lineage_authority_gaps
                        else "PASSED"
                    ),
                    [*lineage_errors, *lineage_authority_gaps],
                    **lineage_details,
                )
            except DataConsistencyAuthorityUnavailable as exc:
                lineage = _subresult("BLOCKED", [str(exc)])
            except DataConsistencyError as exc:
                lineage = _subresult("FAILED", [str(exc)])

            try:
                validated_facts = validate_and_join_facts(
                    snapshot,
                    display_until=display_until,
                    legacy_native_scheme_ids=load_legacy_native_scheme_ids(
                        PROJECT_ROOT
                    ),
                    historical_references=historical_references,
                    legacy_migrations=legacy_migrations,
                    corrected_exact_evidence=corrected_exact_evidence,
                )
            except DataConsistencyError as exc:
                lineage = _merge_subresult_error(lineage, str(exc), "FAILED")

            try:
                representation_facts = build_representation_facts(
                    snapshot,
                    display_until=display_until,
                )
                summary, summary_metadata = _fetch_json_object(
                    self._fetcher,
                    summary_url,
                    ctx,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
                summary_display_until = _iso_date(
                    summary.get("display_until"),
                    field="summary display_until",
                )
                expected_monthly = aggregate_display_facts(
                    representation_facts
                )
                _remaining_budget(deadline, self._monotonic)
                representation_errors = _compare_summary(
                    summary,
                    registry_ids=registry_ids,
                    expected=expected_monthly,
                )
                if summary_display_until != display_until:
                    representation_errors.append(
                        "Dashboard Summary display_until does not match the "
                        "independent Shanghai business date"
                    )

                expected_details = _detail_rows_by_partition(
                    representation_facts
                )
                for (scheme_id, month, source), rows in sorted(
                    expected_details.items()
                ):
                    detail_url = summary_url + "?" + urlencode(
                        {
                            "scheme-id": scheme_id,
                            "month": month,
                            "source": source,
                        }
                    )
                    detail, metadata = _fetch_json_object(
                        self._fetcher,
                        detail_url,
                        ctx,
                        deadline=deadline,
                        monotonic=self._monotonic,
                    )
                    representation_errors.extend(
                        _compare_detail(
                            detail,
                            scheme_id=scheme_id,
                            month=month,
                            source=source,
                            expected_rows=rows,
                            display_until=display_until,
                        )
                    )
                    detail_metadata.append(
                        {
                            "scheme_id": scheme_id,
                            "month": month,
                            "source": source,
                            "snapshot_id": detail.get("snapshot_id"),
                            "request_id": metadata.get("request_id"),
                            "fetched_at": metadata.get("fetched_at"),
                        }
                    )
                representation = _subresult(
                    "PASSED" if not representation_errors else "FAILED",
                    representation_errors,
                    summary_snapshot_id=summary.get("snapshot_id"),
                    detail_request_count=len(detail_metadata),
                )
            except DataConsistencyTimeout:
                raise
            except Exception as exc:
                representation = _subresult("FAILED", [str(exc)])
            finally:
                try:
                    _remaining_budget(deadline, self._monotonic)
                    final_snapshot = _capture_snapshot(
                        engine,
                        registry_ids,
                        deadline=deadline,
                        monotonic=self._monotonic,
                    )
                    _remaining_budget(deadline, self._monotonic)
                except DataConsistencyTimeout as exc:
                    operation_error = exc
                    final_snapshot_failed = True
                except Exception:
                    final_snapshot_failed = True
        finally:
            engine.dispose()

        evidence = [
            Evidence("base_scheme_ids", list(selected_base_ids)),
            Evidence("registry_ids", list(registry_ids)),
            Evidence("database_snapshot_digest", snapshot.digest),
            Evidence("prediction_fact_count", len(snapshot.prediction_rows)),
            Evidence("joined_fact_count", len(representation_facts)),
            Evidence(
                "business_contract_validated_fact_count",
                len(validated_facts),
            ),
            Evidence(
                "summary_snapshot_id",
                summary.get("snapshot_id") if summary is not None else None,
            ),
            Evidence("summary_request_id", summary_metadata.get("request_id")),
            Evidence("summary_fetched_at", summary_metadata.get("fetched_at")),
            Evidence("detail_requests", detail_metadata),
            Evidence("completeness", completeness),
            Evidence("lineage", lineage),
            Evidence("representation", representation),
        ]
        if isinstance(operation_error, DataConsistencyTimeout):
            raise operation_error
        if (
            final_snapshot_failed
            or final_snapshot is None
            or final_snapshot.digest != snapshot.digest
            or _clock_display_date(self._clock) != display_until
        ):
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                evidence=evidence,
                errors=[
                    "selected database facts or the Shanghai display date changed "
                    "during HTTP reconciliation; retry against a stable input window"
                ],
                started_at=started_at,
                finished_at=utc_now(),
            )
        if operation_error is not None:
            raise operation_error
        subresults = (completeness, lineage, representation)
        errors = [
            str(error)
            for subresult in subresults
            for error in subresult["errors"]
        ]
        if any(item["status"] == "FAILED" for item in subresults):
            status = GateStatus.FAILED
        elif any(item["status"] == "BLOCKED" for item in subresults):
            status = GateStatus.BLOCKED
        else:
            status = GateStatus.PASSED
        return GateResult(
            gate_name=self.name,
            status=status,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=utc_now(),
        )


def validate_snapshot_completeness(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
    historical_references: frozenset[
        HistoricalBacktestReference
    ] = frozenset(),
    legacy_migrations: frozenset[
        LegacyPredictionMigration
    ] = frozenset(),
    corrected_exact_evidence: frozenset[
        LegacyCorrectedExactEvidence
    ] = frozenset(),
) -> tuple[list[str], dict[str, Any]]:
    """用成功 run 与不可变回测事实建立预期键并核对产品事实。"""
    errors: list[str] = []
    authority_gaps: set[str] = set()
    legacy_matches = _resolve_legacy_migration_matches(
        snapshot,
        legacy_migrations,
        corrected_exact_evidence,
    )
    product_rows = list(snapshot.prediction_rows)
    compatible_pairs = {
        (item.prediction_scheme_id, item.backtest_source_scheme_id)
        for item in historical_references
    }
    product_by_backtest_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in product_rows:
        if row.get("backtest_run_id") is not None:
            product_by_backtest_run[int(row["backtest_run_id"])].append(row)

    registry_scopes = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in snapshot.registry_rows
    }
    referenced_backtest_scopes = {
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in product_rows
        if row.get("backtest_run_id") is not None
    }
    missing_backtest_authority = sorted(
        registry_scopes - referenced_backtest_scopes
    )
    if missing_backtest_authority:
        authority_gaps.add(
            "selected Registry scopes have no durable backtest product "
            f"reference: {missing_backtest_authority!r}"
        )

    source_by_backtest_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        source_by_backtest_run[int(row["run_id"])].append(row)
    backtest_expected = 0
    for run in snapshot.backtest_runs:
        run_id = int(run["reference_id"])
        source_rows = source_by_backtest_run.get(run_id, [])
        if not source_rows:
            authority_gaps.add(
                f"backtest run {run_id} has no immutable source predictions"
            )
            continue
        summary = _json_mapping(run.get("summary"), field="backtest summary")
        declared_count = summary.get("persisted_prediction_count")
        if not isinstance(declared_count, int) or declared_count < 0:
            legacy_match = legacy_matches.get(run_id)
            if legacy_match is None:
                authority_gaps.add(
                    f"backtest run {run_id} lacks persisted_prediction_count authority"
                )
                declared_count = None
            else:
                declared_count = legacy_match.entry.expected_fact_count
        if declared_count is not None and declared_count != len(source_rows):
            errors.append(
                f"backtest source count mismatch: run_id={run_id} "
                f"summary={declared_count} source={len(source_rows)}"
            )
        source_index = {
            _backtest_source_key(row): row for row in source_rows
        }
        if len(source_index) != len(source_rows):
            errors.append(f"duplicate immutable backtest key: run_id={run_id}")
        product_for_run = product_by_backtest_run.get(run_id, [])
        product_index = {
            _product_reference_key(row): row for row in product_for_run
        }
        if len(product_index) != len(product_for_run):
            errors.append(f"duplicate product backtest key: run_id={run_id}")
        product_scheme_ids = {
            str(row["scheme_id"]) for row in product_for_run
        }
        is_approved_cross_source = bool(product_scheme_ids) and all(
            (product_scheme_id, str(run["scheme_id"])) in compatible_pairs
            for product_scheme_id in product_scheme_ids
        )
        missing = (
            []
            if is_approved_cross_source
            else sorted(set(source_index) - set(product_index))
        )
        extra = sorted(set(product_index) - set(source_index))
        if missing:
            errors.append(
                f"missing product predictions from backtest run {run_id}: {missing!r}"
            )
        if extra:
            errors.append(
                f"extra product predictions for backtest run {run_id}: {extra!r}"
            )
        for key in sorted(set(source_index) & set(product_index)):
            source = source_index[key]
            product = product_index[key]
            expected_values = (
                _iso_date(source.get("predict_date"), field="source predict_date"),
                _iso_date(source.get("feature_date"), field="source feature_date"),
                _direction(source.get("predicted_direction"), field="source direction"),
                _direction(source.get("label"), field="source label"),
            )
            product_values = (
                _iso_date(product.get("predict_date"), field="predict_date"),
                _iso_date(product.get("feature_date"), field="feature_date"),
                _direction(product.get("predicted_direction"), field="direction"),
                _direction(
                    product.get("backtest_actual_direction"),
                    field="backtest_actual_direction",
                ),
            )
            if product_values != expected_values:
                errors.append(
                    f"backtest product fact drift: run_id={run_id} key={key!r}"
                )
        backtest_expected += len(source_rows)

    registry_by_base: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.registry_rows:
        registry_by_base[str(row["base_scheme_id"])].append(row)
    replacement_base_by_live_run = {
        int(row["run_id"]): str(row["scheme_id"])
        for row in snapshot.prediction_rows
        if row.get("run_id") is not None
        and row.get("lineage_scheme_id") is not None
    }
    expected_live_by_key: dict[
        tuple[str, str, int, str], set[int]
    ] = defaultdict(set)
    unresolved_live_run_ids: set[int] = set()
    for run in snapshot.live_runs:
        if run.get("status") != "success":
            continue
        predict_date = _iso_date(run.get("predict_date"), field="run predict_date")
        if predict_date > display_until:
            continue
        base_scheme_id = replacement_base_by_live_run.get(
            int(run["reference_id"]),
            _required_text(run.get("scheme_id"), field="run scheme_id"),
        )
        targets = registry_by_base.get(base_scheme_id, [])
        run_id = int(run["reference_id"])
        if not targets:
            authority_gaps.add(
                f"live run {run_id} has no selected Registry targets"
            )
            unresolved_live_run_ids.add(run_id)
            continue
        run_expected_keys: list[tuple[str, str, int, str]] = []
        for registry in targets:
            target = RegistryTarget(
                registry_scheme_id=str(registry["scheme_id"]),
                base_scheme_id=base_scheme_id,
                runtime_type=str(registry["runtime_type"]),
                frequency=str(registry["frequency"]),
                task_type=str(registry["task_type"]),
                target_tenor=str(registry["target_tenor"]),
                horizon=int(registry["horizon"]),
                scheme_version=str(run.get("scheme_version") or ""),
            )
            try:
                expected = expected_signal_case_from_calendar_rows(
                    target,
                    predict_date=predict_date,
                    calendar_rows=snapshot.calendar_rows,
                )
            except (SignalGapPlanError, ValueError) as exc:
                authority_gaps.add(
                    f"live run {run_id} date authority unavailable: {exc}"
                )
                unresolved_live_run_ids.add(run_id)
                continue
            run_expected_keys.append(expected.business_key)
        if run_id not in unresolved_live_run_ids:
            for business_key in run_expected_keys:
                expected_live_by_key[business_key].add(run_id)
    observed_live_by_key = {
        _product_business_key(row): row
        for row in product_rows
        if row.get("run_id") is not None
        and int(row["run_id"]) not in unresolved_live_run_ids
    }
    missing_live = sorted(
        set(expected_live_by_key) - set(observed_live_by_key)
    )
    extra_live = sorted(
        set(observed_live_by_key) - set(expected_live_by_key)
    )
    if missing_live:
        errors.append(f"missing live product predictions: keys={missing_live!r}")
    if extra_live:
        errors.append(f"extra live product predictions: keys={extra_live!r}")
    for business_key, product in observed_live_by_key.items():
        expected_run_ids = expected_live_by_key.get(business_key)
        if expected_run_ids is None:
            continue
        run_id = int(product["run_id"])
        if run_id not in expected_run_ids:
            errors.append(
                "live product run binding drift: "
                f"key={business_key!r} run_id={run_id} "
                f"expected_run_ids={sorted(expected_run_ids)!r}"
            )
    live_expected = len(expected_live_by_key)

    actual_keys = {
        (
            str(row["actual_kind"]),
            str(row["target_tenor"]),
            _iso_date(row.get("target_date"), field="actual target_date"),
            str(row["target_rule"]),
        )
        for row in snapshot.actual_rows
        if row.get("actual_direction") is not None
    }
    actual_watermarks: dict[tuple[str, str, str], str] = {}
    for row in snapshot.actual_watermarks:
        if row.get("max_target_date") is None:
            continue
        scope = (
            str(row["actual_kind"]),
            str(row["target_tenor"]),
            str(row["target_rule"]),
        )
        actual_watermarks[scope] = _iso_date(
            row["max_target_date"],
            field="Actual watermark",
        )
    if not snapshot.actual_watermarks:
        for kind, tenor, target_date, rule in actual_keys:
            scope = (kind, tenor, rule)
            actual_watermarks[scope] = max(
                target_date,
                actual_watermarks.get(scope, target_date),
            )
    mature_actual_missing = 0
    pending_actual = 0
    registry_by_scope = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        ): row
        for row in snapshot.registry_rows
    }
    for row in product_rows:
        if row.get("backtest_actual_direction") is not None:
            continue
        registry = registry_by_scope.get(
            (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        )
        if registry is None:
            continue
        actual_kind, target_rule = _actual_selector(str(registry["task_type"]))
        target_date = _iso_date(row.get("target_date"), field="target_date")
        actual_key = (
            actual_kind,
            str(row["target_tenor"]),
            target_date,
            target_rule,
        )
        watermark = actual_watermarks.get(actual_key[:2] + actual_key[3:])
        if actual_key in actual_keys:
            continue
        if watermark is not None and target_date <= watermark:
            mature_actual_missing += 1
            errors.append(f"mature Actual is missing: {actual_key!r}")
        else:
            pending_actual += 1

    return errors, {
        "expected_live_fact_count": live_expected,
        "expected_backtest_fact_count": backtest_expected,
        "observed_product_fact_count": len(product_rows),
        "authority_gaps": sorted(authority_gaps),
        "mature_actual_missing_count": mature_actual_missing,
        "pending_actual_count": pending_actual,
        "legacy_migration_fact_count": sum(
            match.entry.expected_fact_count
            for match in legacy_matches.values()
        ),
    }


def validate_snapshot_lineage(
    snapshot: DatabaseSnapshot,
    *,
    historical_references: frozenset[HistoricalBacktestReference],
    legacy_migrations: frozenset[
        LegacyPredictionMigration
    ] = frozenset(),
    corrected_exact_evidence: frozenset[
        LegacyCorrectedExactEvidence
    ] = frozenset(),
    legacy_backtest_lineages: frozenset[
        LegacyBacktestLineage
    ] = frozenset(),
) -> tuple[list[str], dict[str, Any]]:
    """核对产品事实引用的 exact、run、版本与输入工件血缘。"""
    errors: list[str] = []
    authority_gaps: set[str] = set()
    legacy_matches = _resolve_legacy_migration_matches(
        snapshot,
        legacy_migrations,
        corrected_exact_evidence,
    )
    legacy_lineage_matches = _resolve_legacy_backtest_lineage_matches(
        snapshot,
        legacy_backtest_lineages,
    )
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    artifacts = {
        str(row["artifact_id"]): row for row in snapshot.input_artifact_rows
    }
    live_runs = {int(row["reference_id"]): row for row in snapshot.live_runs}
    backtest_runs = {
        int(row["reference_id"]): row for row in snapshot.backtest_runs
    }
    registry = {
        (str(row["base_scheme_id"]), str(row["target_tenor"]), int(row["horizon"])): row
        for row in snapshot.registry_rows
    }
    compatibility = {
        (item.prediction_scheme_id, item.backtest_source_scheme_id)
        for item in historical_references
    }
    checked_artifacts: set[str] = set()
    checked_versions: set[tuple[str, str]] = set()
    legacy_fact_count = 0

    def validate_version(
        identity: tuple[str, str],
        version: Mapping[str, Any] | None,
    ) -> None:
        if version is None:
            errors.append(
                "prediction exact has no version row: "
                f"scheme_id={identity[0]} exact={identity[1]}"
            )
            return
        if identity in checked_versions:
            return
        checked_versions.add(identity)
        runtime_type = str(version.get("runtime_type") or "")
        if runtime_type not in {"native_adapter", "blackbox_v2"}:
            errors.append(
                f"version runtime is invalid: scheme_id={identity[0]} "
                f"exact={identity[1]}"
            )
        for field in ("code_hash", "config_hash", "manifest_hash"):
            value = version.get(field)
            if value in {None, ""}:
                authority_gaps.add(
                    f"version lineage is missing: scheme_id={identity[0]} "
                    f"exact={identity[1]} field={field}"
                )
            elif not _is_sha256(value):
                errors.append(
                    f"version lineage hash invalid: scheme_id={identity[0]} "
                    f"exact={identity[1]} field={field}"
                )
        if runtime_type == "blackbox_v2":
            snapshot_id = version.get("data_snapshot_id")
            if snapshot_id in {None, ""}:
                authority_gaps.add(
                    f"version data snapshot is missing: scheme_id={identity[0]} "
                    f"exact={identity[1]}"
                )
            elif not _is_snapshot_id(snapshot_id):
                errors.append(
                    f"version data snapshot is invalid: scheme_id={identity[0]} "
                    f"exact={identity[1]}"
                )

    for row in snapshot.prediction_rows:
        scope = (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        registry_row = registry.get(scope)
        if registry_row is None:
            continue
        backtest_run_id = row.get("backtest_run_id")
        legacy_match = (
            legacy_matches.get(int(backtest_run_id))
            if backtest_run_id is not None
            else None
        )
        if (
            legacy_match is not None
            and not row.get("scheme_version")
            and scope
            == (
                legacy_match.entry.prediction_scheme_id,
                legacy_match.entry.target_tenor,
                legacy_match.entry.horizon,
            )
        ):
            legacy_fact_count += 1
            continue
        exact = _required_text(row.get("scheme_version"), field="prediction exact")
        lineage_scheme_id = str(
            row.get("lineage_scheme_id") or scope[0]
        )
        version_identity = (lineage_scheme_id, exact)
        version = versions.get(version_identity)
        validate_version(version_identity, version)
        if row.get("run_id") is not None:
            run = live_runs.get(int(row["run_id"]))
            if run is None:
                continue
            if str(run.get("scheme_id") or "") != lineage_scheme_id:
                errors.append(f"live run scheme drift: run_id={row['run_id']}")
            if run.get("scheme_version") != exact:
                errors.append(f"live run exact drift: run_id={row['run_id']}")
            version_runtime = version.get("runtime_type") if version else None
            if run.get("runtime_type") != version_runtime:
                errors.append(f"live run runtime drift: run_id={row['run_id']}")
            if run.get("status") != "success":
                errors.append(f"live run is not successful: run_id={row['run_id']}")
            if run.get("prediction_phase") not in LIVE_PREDICTION_PHASES:
                errors.append(f"live run phase is invalid: run_id={row['run_id']}")
            if version_runtime == "blackbox_v2":
                snapshot_id = run.get("data_snapshot_id")
                if snapshot_id in {None, ""}:
                    authority_gaps.add(
                        f"live run data snapshot is missing: run_id={row['run_id']}"
                    )
                elif not _is_snapshot_id(snapshot_id):
                    errors.append(
                        f"live run data snapshot drift: run_id={row['run_id']}"
                    )
            else:
                artifact_id = str(run.get("input_artifact_id") or "")
                artifact = artifacts.get(artifact_id)
                if not artifact_id or artifact is None:
                    authority_gaps.add(
                        f"live run input artifact is missing: run_id={row['run_id']}"
                    )
                elif artifact_id not in checked_artifacts:
                    checked_artifacts.add(artifact_id)
                    if (
                        artifact.get("scheme_id") != scope[0]
                        or artifact.get("scheme_version") != exact
                        or not _is_sha256(artifact.get("content_hash"))
                        or not _is_sha256(artifact.get("schema_hash"))
                    ):
                        errors.append(
                            "live input artifact lineage drift: "
                            f"artifact_id={artifact_id}"
                        )
        elif row.get("backtest_run_id") is not None:
            run = backtest_runs.get(int(row["backtest_run_id"]))
            if run is None:
                continue
            legacy_lineage = legacy_lineage_matches.get(
                int(row["backtest_run_id"])
            )
            source_scheme_id = str(run.get("scheme_id") or "")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            source_exact = str(summary.get("scheme_version") or "")
            if source_scheme_id == scope[0]:
                if source_exact != exact:
                    errors.append(
                        f"backtest exact drift: run_id={row['backtest_run_id']}"
                    )
            elif (scope[0], source_scheme_id) not in compatibility:
                errors.append(
                    f"unapproved backtest lineage source: prediction={scope[0]} "
                    f"source={source_scheme_id}"
                )
            source_identity = (source_scheme_id, source_exact)
            source_version = versions.get(source_identity)
            validate_version(source_identity, source_version)
            if (
                source_scheme_id != scope[0]
                and source_version is not None
                and source_version.get("status") != "retired"
            ):
                errors.append(
                    "historical backtest source exact is not retired: "
                    f"scheme_id={source_scheme_id} exact={source_exact}"
                )
            for field in ("code_hash", "config_hash"):
                value = run.get(field)
                if value in {None, ""}:
                    if legacy_lineage is None:
                        authority_gaps.add(
                            "backtest lineage is missing: "
                            f"run_id={row['backtest_run_id']} field={field}"
                        )
                elif not _is_sha256(value):
                    errors.append(
                        f"backtest lineage hash invalid: run_id={row['backtest_run_id']} "
                        f"field={field}"
                    )
                elif source_version is not None and value != source_version.get(field):
                    errors.append(
                        f"backtest lineage drift: run_id={row['backtest_run_id']} "
                        f"field={field}"
                    )
            manifest_hash = summary.get("manifest_hash")
            if manifest_hash in {None, ""}:
                if legacy_lineage is None:
                    authority_gaps.add(
                        "backtest lineage is missing: "
                        f"run_id={row['backtest_run_id']} field=manifest_hash"
                    )
            elif not _is_sha256(manifest_hash):
                errors.append(
                    f"backtest lineage hash invalid: run_id={row['backtest_run_id']} "
                    "field=manifest_hash"
                )
            elif (
                source_version is not None
                and manifest_hash != source_version.get("manifest_hash")
            ):
                errors.append(
                    f"backtest manifest drift: run_id={row['backtest_run_id']}"
                )
            input_identity = run.get("input_artifact_hash")
            if input_identity in {None, ""}:
                if legacy_lineage is None:
                    authority_gaps.add(
                        f"backtest input identity is missing: "
                        f"run_id={row['backtest_run_id']}"
                    )
            elif _is_snapshot_id(input_identity):
                if (
                    summary.get("data_snapshot_id") != input_identity
                    or (
                        source_version is not None
                        and source_version.get("runtime_type") == "blackbox_v2"
                        and source_version.get("data_snapshot_id") != input_identity
                    )
                ):
                    errors.append(
                        f"backtest input snapshot drift: "
                        f"run_id={row['backtest_run_id']}"
                    )
            elif not _is_sha256(input_identity):
                errors.append(
                    f"backtest input identity is invalid: "
                    f"run_id={row['backtest_run_id']}"
                )
    return errors, {
        "version_count": len(versions),
        "live_run_count": len(live_runs),
        "backtest_run_count": len(backtest_runs),
        "input_artifact_count": len(artifacts),
        "authority_gaps": sorted(authority_gaps),
        "legacy_migration_fact_count": legacy_fact_count,
        "legacy_migrations": _legacy_migration_evidence(legacy_matches),
        "legacy_backtest_lineages": _legacy_backtest_lineage_evidence(
            legacy_lineage_matches
        ),
    }


def _product_business_key(row: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(row["scheme_id"]),
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="target_date"),
    )


def _source_registry_scope_status(
    run: Mapping[str, Any],
    *,
    target_tenor: str,
    horizon: int,
) -> str | None:
    """返回历史来源精确 tenor/horizon Registry 状态。"""
    scopes = run.get("source_registry_scopes")
    if isinstance(scopes, (list, tuple)):
        matches = {
            str(item.get("status"))
            for item in scopes
            if isinstance(item, Mapping)
            and str(item.get("target_tenor")) == target_tenor
            and int(item.get("horizon") or 0) == horizon
        }
        if len(matches) == 1:
            return next(iter(matches))
    return None


def _product_reference_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="target_date"),
    )


def _backtest_source_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["target_tenor"]),
        int(row["horizon"]),
        _iso_date(row.get("target_date"), field="source target_date"),
    )


def _json_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DataConsistencyError(f"{field} is invalid JSON") from exc
        if isinstance(loaded, Mapping):
            return loaded
    raise DataConsistencyError(f"{field} must be an object")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_snapshot_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"snapshot-[0-9a-f]{24}", value
    ) is not None


def _canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _legacy_fact(
    row: Mapping[str, Any],
    *,
    product: bool,
) -> dict[str, Any]:
    return {
        "scheme_id": _required_text(row.get("scheme_id"), field="scheme_id"),
        "target_tenor": _required_text(
            row.get("target_tenor"), field="target_tenor"
        ),
        "horizon": _positive_int(row.get("horizon"), field="horizon"),
        "predict_date": _iso_date(row.get("predict_date"), field="predict_date"),
        "feature_date": _iso_date(row.get("feature_date"), field="feature_date"),
        "target_date": _iso_date(row.get("target_date"), field="target_date"),
        "predicted_direction": _direction(
            row.get("predicted_direction"), field="predicted_direction"
        ),
        "actual_direction": _direction(
            row.get("backtest_actual_direction" if product else "label"),
            field="backtest actual direction",
        ),
    }


def _resolve_legacy_migration_matches(
    snapshot: DatabaseSnapshot,
    entries: frozenset[LegacyPredictionMigration],
    corrected_exact_evidence: frozenset[LegacyCorrectedExactEvidence],
) -> dict[int, _LegacyMigrationMatch]:
    """只在整批不可变证据逐字段命中时接受已批准迁移例外。"""
    if not entries:
        return {}
    calendar = _FrozenCalendar.from_rows(snapshot.calendar_rows)
    runs = {int(row["reference_id"]): row for row in snapshot.backtest_runs}
    sources_by_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        sources_by_run[int(row["run_id"])].append(row)
    evidence_by_identity = {
        (item.source_scheme_id, item.designated_exact): item
        for item in corrected_exact_evidence
    }

    matches: dict[int, _LegacyMigrationMatch] = {}
    for entry in entries:
        products = [
            row
            for row in snapshot.prediction_rows
            if str(row.get("scheme_id") or "") == entry.prediction_scheme_id
            and str(row.get("target_tenor") or "") == entry.target_tenor
            and int(row.get("horizon") or 0) == entry.horizon
            and row.get("backtest_run_id") is not None
            and not row.get("scheme_version")
        ]
        if not products:
            continue
        run_ids = {int(row["backtest_run_id"]) for row in products}
        if len(run_ids) != 1:
            raise DataConsistencyError(
                "legacy migration evidence drift: expected one immutable "
                f"backtest run for {entry.prediction_scheme_id}"
            )
        run_id = next(iter(run_ids))
        if run_id in matches:
            raise DataConsistencyError(
                f"legacy migration evidence drift: duplicate run {run_id}"
            )
        run = runs.get(run_id)
        sources = sources_by_run.get(run_id, [])
        try:
            corrected = evidence_by_identity.get(
                (
                    entry.designated_source_scheme_id,
                    entry.designated_exact,
                )
            )
            if corrected is None:
                raise ValueError("designated corrected exact evidence is missing")
            if (
                corrected.source_registry_scheme_id
                != registry_scheme_id(
                    entry.designated_source_scheme_id,
                    entry.horizon,
                    entry.target_tenor,
                )
                or corrected.registry_status != "archived"
                or corrected.runtime_type != "blackbox_v2"
                or corrected.version_status != "retired"
                or corrected.code_hash != entry.designated_code_hash
                or corrected.config_hash != entry.designated_config_hash
                or corrected.manifest_hash != entry.designated_manifest_hash
                or corrected.input_artifact_id
                != entry.designated_input_artifact_id
                or corrected.backtest_status != "success"
                or corrected.persisted_prediction_count
                != entry.expected_fact_count
                or corrected.corrected_facts_sha256
                != entry.designated_corrected_facts_sha256
            ):
                raise ValueError("designated corrected exact identity changed")
            if run is None:
                raise ValueError("backtest run is missing")
            if (
                str(run.get("scheme_id") or "")
                != entry.historical_source_scheme_id
                or run.get("status") != "success"
                or run.get("code_hash") is not None
                or run.get("config_hash") is not None
                or run.get("input_artifact_hash") != entry.input_artifact_hash
            ):
                raise ValueError("historical run identity changed")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            if (
                summary.get("scheme_version") is not None
                or summary.get("manifest_hash") is not None
                or _canonical_json_sha256(summary)
                != entry.backtest_summary_sha256
            ):
                raise ValueError("historical summary changed")
            summary_counts = (
                summary.get("raw_row_count"),
                summary.get("row_count"),
                (
                    summary.get("evaluation_filter", {}).get(
                        "included_row_count"
                    )
                    if isinstance(summary.get("evaluation_filter"), Mapping)
                    else None
                ),
            )
            if summary_counts != (entry.expected_fact_count,) * 3:
                raise ValueError("historical summary count authority changed")
            if (
                len(products) != entry.expected_fact_count
                or len(sources) != entry.expected_fact_count
            ):
                raise ValueError("historical fact count changed")
            product_facts = sorted(
                (_legacy_fact(row, product=True) for row in products),
                key=_legacy_fact_sort_key,
            )
            source_facts = sorted(
                (_legacy_fact(row, product=False) for row in sources),
                key=_legacy_fact_sort_key,
            )
            if (
                _canonical_json_sha256(product_facts)
                != entry.product_facts_sha256
                or _canonical_json_sha256(source_facts)
                != entry.source_facts_sha256
                or product_facts != source_facts
            ):
                raise ValueError("historical facts changed")
            mismatches: list[dict[str, str]] = []
            mismatch_keys: set[tuple[str, str, int, str]] = set()
            for fact in product_facts:
                expected_target = calendar.nth_trading_day_after(
                    fact["feature_date"],
                    int(fact["horizon"]),
                )
                if fact["target_date"] == expected_target:
                    continue
                mismatches.append(
                    {
                        "predict_date": fact["predict_date"],
                        "feature_date": fact["feature_date"],
                        "target_date": fact["target_date"],
                        "expected_target_date": expected_target,
                    }
                )
                mismatch_keys.add(
                    (
                        fact["scheme_id"],
                        fact["target_tenor"],
                        int(fact["horizon"]),
                        fact["target_date"],
                    )
                )
            if (
                len(mismatches) != entry.target_contract_mismatch_count
                or _canonical_json_sha256(mismatches)
                != entry.target_contract_mismatch_sha256
            ):
                raise ValueError("historical target-date exception changed")
            corrected_facts = sorted(
                (
                    {
                        "scheme_id": fact.scheme_id,
                        "target_tenor": fact.target_tenor,
                        "horizon": fact.horizon,
                        "predict_date": fact.predict_date,
                        "feature_date": fact.feature_date,
                        "target_date": fact.target_date,
                        "predicted_direction": fact.predicted_direction,
                        "actual_direction": fact.actual_direction,
                    }
                    for fact in corrected.corrected_facts
                ),
                key=_legacy_fact_sort_key,
            )
            if len(corrected_facts) != entry.expected_fact_count:
                raise ValueError("designated corrected fact count changed")
            for fact in corrected_facts:
                if (
                    fact["scheme_id"] != entry.designated_source_scheme_id
                    or fact["target_tenor"] != entry.target_tenor
                    or int(fact["horizon"]) != entry.horizon
                ):
                    raise ValueError("designated corrected fact scope changed")
                _validate_task_date_contract(
                    task_type="T+5",
                    horizon=entry.horizon,
                    predict_date=fact["predict_date"],
                    feature_date=fact["feature_date"],
                    target_date=fact["target_date"],
                    is_live=False,
                    calendar=calendar,
                    business_key=(
                        fact["scheme_id"],
                        fact["target_tenor"],
                        int(fact["horizon"]),
                        fact["target_date"],
                    ),
                )
            old_identity = sorted(
                _legacy_comparison_identity(fact)
                for fact in product_facts
            )
            corrected_identity = sorted(
                _legacy_comparison_identity(fact)
                for fact in corrected_facts
            )
            if old_identity != corrected_identity:
                raise ValueError(
                    "designated corrected facts do not match legacy signals"
                )
        except (DataConsistencyError, ValueError, TypeError) as exc:
            raise DataConsistencyError(
                "legacy migration evidence drift: "
                f"scheme_id={entry.prediction_scheme_id}: {exc}"
            ) from exc
        matches[run_id] = _LegacyMigrationMatch(
            entry=entry,
            backtest_run_id=run_id,
            mismatch_business_keys=frozenset(mismatch_keys),
        )
    return matches


def _resolve_legacy_backtest_lineage_matches(
    snapshot: DatabaseSnapshot,
    entries: frozenset[LegacyBacktestLineage],
) -> dict[int, _LegacyBacktestLineageMatch]:
    """仅在版本、run、summary 与两侧逐事实摘要全部命中时恢复旧血缘。"""
    if not entries:
        return {}
    runs = {int(row["reference_id"]): row for row in snapshot.backtest_runs}
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    sources_by_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshot.backtest_prediction_rows:
        sources_by_run[int(row["run_id"])].append(row)

    matches: dict[int, _LegacyBacktestLineageMatch] = {}
    for entry in entries:
        products = [
            row
            for row in snapshot.prediction_rows
            if str(row.get("scheme_id") or "") == entry.scheme_id
            and str(row.get("scheme_version") or "")
            == entry.scheme_version
            and str(row.get("target_tenor") or "") == entry.target_tenor
            and int(row.get("horizon") or 0) == entry.horizon
            and row.get("backtest_run_id") is not None
        ]
        if not products:
            continue
        try:
            run_ids = {int(row["backtest_run_id"]) for row in products}
            if len(run_ids) != 1:
                raise ValueError("expected one immutable backtest run")
            run_id = next(iter(run_ids))
            if run_id in matches:
                raise ValueError("duplicate immutable backtest run")
            run = runs.get(run_id)
            version = versions.get((entry.scheme_id, entry.scheme_version))
            sources = sources_by_run.get(run_id, [])
            if run is None or version is None:
                raise ValueError("run or version evidence is missing")
            if (
                str(run.get("scheme_id") or "") != entry.scheme_id
                or run.get("status") != "success"
                or run.get("benchmark_id") != entry.benchmark_id
                or run.get("code_hash") is not None
                or run.get("config_hash") is not None
                or run.get("input_artifact_hash") is not None
            ):
                raise ValueError("legacy run identity changed")
            if (
                version.get("runtime_type") != "blackbox_v2"
                or version.get("data_snapshot_id") != entry.input_snapshot_id
                or version.get("code_hash") != entry.code_hash
                or version.get("config_hash") != entry.config_hash
                or version.get("manifest_hash") != entry.manifest_hash
            ):
                raise ValueError("legacy exact version evidence changed")
            summary = _json_mapping(run.get("summary"), field="backtest summary")
            if (
                summary.get("scheme_version") != entry.scheme_version
                or summary.get("data_snapshot_id")
                != entry.input_snapshot_id
                or summary.get("manifest_hash") is not None
                or _canonical_json_sha256(summary)
                != entry.backtest_summary_sha256
            ):
                raise ValueError("legacy backtest summary changed")
            summary_counts = (
                summary.get("raw_row_count"),
                summary.get("row_count"),
                summary.get("persisted_prediction_count"),
                (
                    summary.get("evaluation_filter", {}).get(
                        "included_row_count"
                    )
                    if isinstance(summary.get("evaluation_filter"), Mapping)
                    else None
                ),
            )
            if summary_counts != (entry.expected_fact_count,) * 4:
                raise ValueError("legacy backtest summary count changed")
            if (
                len(products) != entry.expected_fact_count
                or len(sources) != entry.expected_fact_count
            ):
                raise ValueError("legacy backtest fact count changed")
            source_facts = sorted(
                (_legacy_fact(row, product=False) for row in sources),
                key=_legacy_fact_sort_key,
            )
            product_facts = sorted(
                (
                    {
                        **_legacy_fact(row, product=True),
                        "scheme_version": _required_text(
                            row.get("scheme_version"),
                            field="prediction exact",
                        ),
                    }
                    for row in products
                ),
                key=_legacy_fact_sort_key,
            )
            comparable_products = [
                {
                    key: value
                    for key, value in fact.items()
                    if key != "scheme_version"
                }
                for fact in product_facts
            ]
            if (
                _canonical_json_sha256(source_facts)
                != entry.backtest_facts_sha256
                or _canonical_json_sha256(product_facts)
                != entry.product_facts_sha256
                or source_facts != comparable_products
            ):
                raise ValueError("legacy backtest facts changed")
        except (DataConsistencyError, TypeError, ValueError) as exc:
            raise DataConsistencyError(
                "legacy backtest lineage evidence drift: "
                f"scheme_id={entry.scheme_id} exact={entry.scheme_version}: "
                f"{exc}"
            ) from exc
        matches[run_id] = _LegacyBacktestLineageMatch(
            entry=entry,
            backtest_run_id=run_id,
        )
    return matches


def _legacy_fact_sort_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["target_tenor"],
        fact["horizon"],
        fact["target_date"],
        fact["predict_date"],
    )


def _legacy_comparison_identity(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["target_tenor"],
        fact["horizon"],
        fact["predict_date"],
        fact["feature_date"],
        fact["predicted_direction"],
        fact["actual_direction"],
    )


def _legacy_migration_evidence(
    matches: Mapping[int, _LegacyMigrationMatch],
) -> list[dict[str, Any]]:
    return [
        {
            "prediction_scheme_id": match.entry.prediction_scheme_id,
            "designated_source_scheme_id": (
                match.entry.designated_source_scheme_id
            ),
            "designated_exact": match.entry.designated_exact,
            "fact_count": match.entry.expected_fact_count,
            "target_contract_mismatch_count": (
                match.entry.target_contract_mismatch_count
            ),
        }
        for _, match in sorted(matches.items())
    ]


def _legacy_backtest_lineage_evidence(
    matches: Mapping[int, _LegacyBacktestLineageMatch],
) -> list[dict[str, Any]]:
    return [
        {
            "scheme_id": match.entry.scheme_id,
            "scheme_version": match.entry.scheme_version,
            "backtest_run_id": match.backtest_run_id,
            "benchmark_id": match.entry.benchmark_id,
            "input_snapshot_id": match.entry.input_snapshot_id,
            "fact_count": match.entry.expected_fact_count,
        }
        for match in sorted(
            matches.values(), key=lambda item: item.backtest_run_id
        )
    ]


def validate_and_join_facts(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
    legacy_native_scheme_ids: frozenset[str] = frozenset(),
    historical_references: frozenset[
        HistoricalBacktestReference
    ] = frozenset(),
    legacy_migrations: frozenset[
        LegacyPredictionMigration
    ] = frozenset(),
    corrected_exact_evidence: frozenset[
        LegacyCorrectedExactEvidence
    ] = frozenset(),
) -> list[ConsistencyFact]:
    """独立校验产品事实并按任务规则关联已发布或 live Actual。"""
    calendar = _FrozenCalendar.from_rows(snapshot.calendar_rows)
    legacy_matches = _resolve_legacy_migration_matches(
        snapshot,
        legacy_migrations,
        corrected_exact_evidence,
    )
    registry_by_scope: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for row in snapshot.registry_rows:
        scope = (
            _required_text(row.get("base_scheme_id"), field="base_scheme_id"),
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _positive_int(row.get("horizon"), field="horizon"),
        )
        if scope in registry_by_scope:
            raise DataConsistencyError(f"duplicate active Registry scope: {scope!r}")
        task_type = _required_text(row.get("task_type"), field="task_type")
        runtime_type = _required_text(
            row.get("runtime_type"), field="runtime_type"
        )
        try:
            expected = runtime_task_contract(
                runtime_type=runtime_type,
                task_type=task_type,
                base_scheme_id=scope[0],
                legacy_native_scheme_ids=legacy_native_scheme_ids,
            )
        except ValueError as exc:
            raise DataConsistencyError(
                f"Registry task contract mismatch: scheme_id={row.get('scheme_id')!r}"
            ) from exc
        if expected.horizon != scope[2] or (
            row.get("frequency") is not None
            and str(row["frequency"]) != expected.frequency
        ):
            raise DataConsistencyError(
                f"Registry task contract mismatch: scheme_id={row.get('scheme_id')!r}"
            )
        registry_by_scope[scope] = row

    actuals: dict[tuple[str, str, str, str], int | None] = {}
    for row in snapshot.actual_rows:
        key = (
            _required_text(row.get("actual_kind"), field="actual kind"),
            _required_text(row.get("target_tenor"), field="actual target_tenor"),
            _iso_date(row.get("target_date"), field="actual target_date"),
            _required_text(row.get("target_rule"), field="actual target_rule"),
        )
        direction = _direction(
            row.get("actual_direction"),
            field="actual_direction",
            allow_none=True,
        )
        if key in actuals and actuals[key] != direction:
            raise DataConsistencyError(f"conflicting Actual facts: {key!r}")
        actuals[key] = direction

    live_runs = {
        int(row["reference_id"]): row for row in snapshot.live_runs
    }
    backtest_runs = {
        int(row["reference_id"]): row for row in snapshot.backtest_runs
    }
    versions = {
        (str(row["scheme_id"]), str(row["scheme_version"])): row
        for row in snapshot.version_rows
    }
    compatible_pairs = {
        (item.prediction_scheme_id, item.backtest_source_scheme_id)
        for item in historical_references
    }
    business_keys: set[tuple[str, str, int, str]] = set()
    facts: list[ConsistencyFact] = []
    for row in snapshot.prediction_rows:
        scheme_id = _required_text(row.get("scheme_id"), field="scheme_id")
        target_tenor = _required_text(
            row.get("target_tenor"), field="target_tenor"
        )
        horizon = _positive_int(row.get("horizon"), field="horizon")
        target_date = _iso_date(row.get("target_date"), field="target_date")
        business_key = (scheme_id, target_tenor, horizon, target_date)
        if business_key in business_keys:
            raise DataConsistencyError(
                "duplicate prediction business key (exact version is not part "
                f"of the key): {business_key!r}"
            )
        business_keys.add(business_key)

        registry = registry_by_scope.get((scheme_id, target_tenor, horizon))
        if registry is None:
            raise DataConsistencyError(
                f"prediction has no selected active Registry scope: {business_key!r}"
            )
        predict_date = _iso_date(row.get("predict_date"), field="predict_date")
        feature_date = _iso_date(row.get("feature_date"), field="feature_date")
        if not feature_date <= predict_date <= target_date or not feature_date < target_date:
            raise DataConsistencyError(
                f"prediction dates violate feature <= predict <= target: {business_key!r}"
            )
        predicted_direction = _direction(
            row.get("predicted_direction"),
            field="predicted_direction",
        )
        live_run_id = _optional_positive_int(row.get("run_id"), field="run_id")
        backtest_run_id = _optional_positive_int(
            row.get("backtest_run_id"), field="backtest_run_id"
        )
        published_actual = _direction(
            row.get("backtest_actual_direction"),
            field="backtest_actual_direction",
            allow_none=True,
        )
        if (live_run_id is None) == (backtest_run_id is None):
            raise DataConsistencyError(
                f"prediction run references must be mutually exclusive: {business_key!r}"
            )
        if live_run_id is not None:
            if live_run_id not in live_runs:
                raise DataConsistencyError(
                    f"prediction references missing live run: {business_key!r}"
                )
            live_run = live_runs[live_run_id]
            lineage_scheme_id = str(
                row.get("lineage_scheme_id") or scheme_id
            )
            if live_run.get("scheme_id") != lineage_scheme_id:
                raise DataConsistencyError(
                    f"prediction references another scheme's live run: {business_key!r}"
                )
            if live_run.get("status") not in {None, "success"}:
                raise DataConsistencyError(
                    f"prediction references non-success live run: {business_key!r}"
                )
            if published_actual is not None:
                raise DataConsistencyError(
                    f"live prediction carries backtest Actual: {business_key!r}"
                )
        else:
            if backtest_run_id not in backtest_runs:
                raise DataConsistencyError(
                    f"prediction references missing backtest run: {business_key!r}"
                )
            backtest_run = backtest_runs[backtest_run_id]
            source_scheme_id = str(backtest_run.get("scheme_id") or "")
            if source_scheme_id != scheme_id:
                if (scheme_id, source_scheme_id) not in compatible_pairs:
                    raise DataConsistencyError(
                        "prediction references another scheme's backtest run: "
                        f"{business_key!r}"
                    )
                source_scope_status = _source_registry_scope_status(
                    backtest_run,
                    target_tenor=target_tenor,
                    horizon=horizon,
                )
                source_summary = _json_mapping(
                    backtest_run.get("summary"), field="backtest summary"
                )
                source_exact = str(
                    source_summary.get("scheme_version") or ""
                )
                source_version = versions.get(
                    (source_scheme_id, source_exact)
                )
                if (
                    source_scope_status != "archived"
                    or source_version is None
                    or source_version.get("status") != "retired"
                    or backtest_run.get("status") != "success"
                    or not backtest_run.get("code_hash")
                    or not backtest_run.get("config_hash")
                    or not backtest_run.get("input_artifact_hash")
                ):
                    raise DataConsistencyError(
                        "historical cross-scheme reference lacks retired source evidence: "
                        f"{business_key!r}"
                    )
            elif backtest_run.get("status") not in {None, "success"}:
                raise DataConsistencyError(
                    f"prediction references non-success backtest run: {business_key!r}"
                )
            if published_actual is None:
                raise DataConsistencyError(
                    f"backtest prediction is missing immutable Actual: {business_key!r}"
                )
            if (
                str(registry["task_type"]) != "monthly"
                and predict_date != feature_date
            ):
                raise DataConsistencyError(
                    f"backtest prediction must have predict_date=feature_date: {business_key!r}"
                )

        legacy_match = (
            legacy_matches.get(backtest_run_id)
            if backtest_run_id is not None
            else None
        )
        _validate_task_date_contract(
            task_type=str(registry["task_type"]),
            horizon=horizon,
            predict_date=predict_date,
            feature_date=feature_date,
            target_date=target_date,
            is_live=live_run_id is not None,
            calendar=calendar,
            business_key=business_key,
            allow_legacy_target_date=(
                legacy_match is not None
                and business_key in legacy_match.mismatch_business_keys
            ),
        )

        if predict_date < HISTORY_START_DATE or predict_date > display_until:
            continue
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        actual_direction = (
            published_actual
            if published_actual is not None
            else actuals.get(
                (actual_kind, target_tenor, target_date, target_rule)
            )
        )
        facts.append(
            ConsistencyFact(
                scheme_id=str(registry["scheme_id"]),
                target_tenor=target_tenor,
                horizon=horizon,
                task_type=task_type,
                predict_date=predict_date,
                feature_date=feature_date,
                target_date=target_date,
                predicted_direction=predicted_direction,
                actual_direction=actual_direction,
            )
        )
    return sorted(
        facts,
        key=lambda item: (
            item.scheme_id,
            item.target_date,
            item.predict_date,
        ),
    )


def build_representation_facts(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
) -> list[ConsistencyFact]:
    """只按持久化产品事实与 Actual 独立重建公开表示。"""
    registry_by_scope: dict[
        tuple[str, str, int], Mapping[str, Any]
    ] = {}
    for row in snapshot.registry_rows:
        scope = (
            _required_text(row.get("base_scheme_id"), field="base_scheme_id"),
            _required_text(row.get("target_tenor"), field="target_tenor"),
            _positive_int(row.get("horizon"), field="horizon"),
        )
        if scope in registry_by_scope:
            raise DataConsistencyError(
                f"duplicate active Registry scope: {scope!r}"
            )
        _required_text(row.get("scheme_id"), field="registry scheme_id")
        _required_text(row.get("task_type"), field="task_type")
        registry_by_scope[scope] = row

    actuals: dict[tuple[str, str, str, str], int | None] = {}
    for row in snapshot.actual_rows:
        key = (
            _required_text(row.get("actual_kind"), field="actual kind"),
            _required_text(
                row.get("target_tenor"), field="actual target_tenor"
            ),
            _iso_date(row.get("target_date"), field="actual target_date"),
            _required_text(
                row.get("target_rule"), field="actual target_rule"
            ),
        )
        direction = _direction(
            row.get("actual_direction"),
            field="actual_direction",
            allow_none=True,
        )
        if key in actuals and actuals[key] != direction:
            raise DataConsistencyError(f"conflicting Actual facts: {key!r}")
        actuals[key] = direction

    business_keys: set[tuple[str, str, int, str]] = set()
    facts: list[ConsistencyFact] = []
    for row in snapshot.prediction_rows:
        scheme_id = _required_text(row.get("scheme_id"), field="scheme_id")
        target_tenor = _required_text(
            row.get("target_tenor"), field="target_tenor"
        )
        horizon = _positive_int(row.get("horizon"), field="horizon")
        target_date = _iso_date(row.get("target_date"), field="target_date")
        business_key = (scheme_id, target_tenor, horizon, target_date)
        if business_key in business_keys:
            raise DataConsistencyError(
                "duplicate prediction business key (exact version is not part "
                f"of the key): {business_key!r}"
            )
        business_keys.add(business_key)

        registry = registry_by_scope.get((scheme_id, target_tenor, horizon))
        if registry is None:
            raise DataConsistencyError(
                "prediction has no selected active Registry scope: "
                f"{business_key!r}"
            )
        predict_date = _iso_date(row.get("predict_date"), field="predict_date")
        feature_date = _iso_date(row.get("feature_date"), field="feature_date")
        predicted_direction = _direction(
            row.get("predicted_direction"),
            field="predicted_direction",
        )
        published_actual = _direction(
            row.get("backtest_actual_direction"),
            field="backtest_actual_direction",
            allow_none=True,
        )
        if predict_date < HISTORY_START_DATE or predict_date > display_until:
            continue
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        actual_direction = (
            published_actual
            if published_actual is not None
            else actuals.get(
                (actual_kind, target_tenor, target_date, target_rule)
            )
        )
        facts.append(
            ConsistencyFact(
                scheme_id=str(registry["scheme_id"]),
                target_tenor=target_tenor,
                horizon=horizon,
                task_type=task_type,
                predict_date=predict_date,
                feature_date=feature_date,
                target_date=target_date,
                predicted_direction=predicted_direction,
                actual_direction=actual_direction,
            )
        )
    return sorted(
        facts,
        key=lambda item: (
            item.scheme_id,
            item.target_date,
            item.predict_date,
        ),
    )


def _validate_task_date_contract(
    *,
    task_type: str,
    horizon: int,
    predict_date: str,
    feature_date: str,
    target_date: str,
    is_live: bool,
    calendar: _FrozenCalendar,
    business_key: tuple[str, str, int, str],
    allow_legacy_target_date: bool = False,
) -> None:
    """按冻结日历独立核对每个任务族的三日期业务语义。"""
    try:
        if task_type in {"T+1", "T+5"}:
            if is_live:
                if not calendar.is_trading_day(predict_date):
                    raise DataConsistencyError(
                        f"daily live predict_date must be a trading day, got {predict_date}"
                    )
                expected_feature = calendar.previous_trading_day(predict_date)
                if feature_date != expected_feature:
                    raise DataConsistencyError(
                        f"daily live feature_date must be previous trading day "
                        f"{expected_feature}, got {feature_date}"
                    )
            elif not calendar.is_trading_day(feature_date):
                raise DataConsistencyError(
                    f"daily backtest feature_date must be a trading day, got {feature_date}"
                )
            expected_target = calendar.nth_trading_day_after(
                feature_date,
                horizon,
            )
            if target_date != expected_target and not (
                task_type == "T+5" and allow_legacy_target_date
            ):
                raise DataConsistencyError(
                    f"daily target_date must be trading-day horizon "
                    f"{expected_target}, got {target_date}"
                )
            return

        if task_type in {"weekly_point", "weekly_average"}:
            if is_live:
                if date.fromisoformat(predict_date).weekday() != 5:
                    raise DataConsistencyError(
                        f"weekly live predict_date must be natural Saturday, got {predict_date}"
                    )
                expected_feature = calendar.previous_trading_day(predict_date)
                if feature_date != expected_feature:
                    raise DataConsistencyError(
                        f"weekly live feature_date must be previous trading day "
                        f"{expected_feature}, got {feature_date}"
                    )
            expected_target = calendar.next_week_last_trading_day(feature_date)
            if target_date != expected_target:
                raise DataConsistencyError(
                    f"weekly target_date must be next actual week end "
                    f"{expected_target}, got {target_date}"
                )
            return

        if task_type == "monthly":
            trigger = date.fromisoformat(predict_date)
            if trigger.day != 15:
                raise DataConsistencyError(
                    "monthly predict_date must be natural month 15, "
                    f"got {predict_date}"
                )
            feature_anchor = date(trigger.year, trigger.month, 15)
            target_anchor = _next_month_anchor(feature_anchor)
            for anchor, label in (
                (feature_anchor, "feature"),
                (target_anchor, "target"),
            ):
                if anchor.isoformat() not in calendar.covered_dates:
                    raise DataConsistencyError(
                        f"trade calendar does not cover monthly {label} anchor "
                        f"{anchor.isoformat()}"
                    )
            expected_feature = calendar.last_trading_day_on_or_before(
                feature_anchor.isoformat()
            )
            expected_target = calendar.last_trading_day_on_or_before(
                target_anchor.isoformat()
            )
            if not expected_feature.startswith(f"{feature_anchor:%Y-%m}"):
                raise DataConsistencyError("monthly feature anchor resolved outside its month")
            if not expected_target.startswith(f"{target_anchor:%Y-%m}"):
                raise DataConsistencyError("monthly target anchor resolved outside its month")
            if feature_date != expected_feature or target_date != expected_target:
                raise DataConsistencyError(
                    "monthly dates must use current/next natural-month 15 anchors: "
                    f"expected feature={expected_feature}, target={expected_target}; "
                    f"got feature={feature_date}, target={target_date}"
                )
            return

        if task_type in {
            "monthly_average",
            "quarterly_average",
            "annual_average",
        }:
            buckets = build_period_buckets(task_type, calendar.rows)
            anchors = {bucket.anchor_date for bucket in buckets}
            if feature_date not in anchors:
                raise DataConsistencyError(
                    f"{task_type} feature_date is not a complete bucket anchor: "
                    f"{feature_date}"
                )
            expected_target = (
                date.fromisoformat(feature_date) + timedelta(days=1)
            ).isoformat()
            if predict_date != feature_date or target_date != expected_target:
                raise DataConsistencyError(
                    f"{task_type} dates must use predict=feature=bucket anchor "
                    f"and target=feature+1 calendar day: expected target="
                    f"{expected_target}, got predict={predict_date}, "
                    f"feature={feature_date}, target={target_date}"
                )
            return
    except DataConsistencyError as exc:
        raise DataConsistencyError(
            f"prediction dates violate {task_type} business contract: "
            f"{business_key!r}: {exc}"
        ) from exc
    except ValueError as exc:
        raise DataConsistencyError(
            f"prediction dates cannot be validated for {task_type}: "
            f"{business_key!r}: {exc}"
        ) from exc
    raise DataConsistencyError(f"unsupported task_type: {task_type!r}")


def _next_month_anchor(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 15)
    return date(value.year, value.month + 1, 15)


def aggregate_display_facts(
    facts: Sequence[ConsistencyFact],
) -> dict[str, list[list[Any]]]:
    """不调用 Dashboard builder，独立形成公开月份计数。"""
    groups: dict[tuple[str, str, str], list[ConsistencyFact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.scheme_id, _display_month(fact), _source(fact.target_date))].append(
            fact
        )

    result: dict[str, list[list[Any]]] = defaultdict(list)
    for (scheme_id, month, source), group in sorted(groups.items()):
        verified = [item for item in group if item.actual_direction is not None]
        directional = [
            item for item in verified if item.predicted_direction in {-1, 1}
        ]
        predicted = [item.predicted_direction for item in verified]
        actual = [int(item.actual_direction) for item in verified]
        result[scheme_id].append(
            [
                month,
                source,
                len(verified),
                len(directional),
                sum(
                    item.predicted_direction == item.actual_direction
                    for item in directional
                ),
                predicted.count(1),
                predicted.count(-1),
                predicted.count(0),
                actual.count(1),
                actual.count(-1),
                actual.count(0),
                sum(
                    item.predicted_direction == item.actual_direction == 1
                    for item in directional
                ),
                sum(
                    item.predicted_direction == item.actual_direction == -1
                    for item in directional
                ),
            ]
        )
    return dict(result)


def _detail_rows_by_partition(
    facts: Sequence[ConsistencyFact],
) -> dict[tuple[str, str, str], list[list[Any]]]:
    result: dict[tuple[str, str, str], list[list[Any]]] = defaultdict(list)
    for fact in facts:
        source = _source(fact.target_date)
        result[(fact.scheme_id, _display_month(fact), source)].append(
            [
                source,
                fact.predict_date,
                fact.feature_date,
                fact.target_date,
                fact.predicted_direction,
                fact.actual_direction,
            ]
        )
    for rows in result.values():
        rows.sort(key=lambda row: (str(row[3]), str(row[1])))
    return dict(result)


def _compare_summary(
    payload: Mapping[str, Any],
    *,
    registry_ids: Sequence[str],
    expected: Mapping[str, list[list[Any]]],
) -> list[str]:
    errors: list[str] = []
    if payload.get("representation") != "summary":
        return ["Dashboard Summary representation is invalid"]
    if payload.get("monthly_row_fields") != list(MONTHLY_ROW_FIELDS):
        errors.append("Dashboard Summary monthly_row_fields mismatch")
    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        return errors + ["Dashboard Summary schemes must be a list"]
    for scheme_id in registry_ids:
        matches = [
            row
            for row in schemes
            if isinstance(row, Mapping) and row.get("scheme_id") == scheme_id
        ]
        if len(matches) != 1:
            errors.append(
                f"Dashboard Summary scheme count mismatch: scheme_id={scheme_id} "
                f"count={len(matches)}"
            )
            continue
        actual_rows = matches[0].get("monthly_rows")
        if actual_rows != expected.get(scheme_id, []):
            errors.append(
                f"Dashboard Summary monthly rows mismatch: scheme_id={scheme_id}"
            )
    return errors


def _compare_detail(
    payload: Mapping[str, Any],
    *,
    scheme_id: str,
    month: str,
    source: str,
    expected_rows: list[list[Any]],
    display_until: str,
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "representation": "detail",
        "scheme_id": scheme_id,
        "month": month,
        "source": source,
        "row_fields": list(DETAIL_ROW_FIELDS),
        "display_until": display_until,
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            errors.append(
                f"Dashboard Detail {field} mismatch: scheme_id={scheme_id} "
                f"month={month} source={source}"
            )
    rows = payload.get("rows")
    if rows != expected_rows:
        errors.append(
            f"Dashboard Detail rows mismatch: scheme_id={scheme_id} "
            f"month={month} source={source}"
        )
    return errors


def _selected_registry_ids(ctx: GateContext) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected = ctx.dashboard_scheme_ids or (ctx.scheme_id,)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in selected
        )
    ):
        raise ValueError(
            "data consistency gate requires unique non-empty base scheme ids"
        )
    registry_ids: list[str] = []
    for base_scheme_id in selected:
        config = load_scheme_config(
            ctx.project_root / "schemes" / base_scheme_id / "config.yaml"
        )
        if config.scheme_id != base_scheme_id:
            raise ValueError("scheme directory and canonical id mismatch")
        registry_ids.extend(
            registry_scheme_id(base_scheme_id, config.horizon, tenor)
            for tenor in config.tenors
        )
    return tuple(selected), tuple(registry_ids)


def _execute_budgeted(
    connection: Connection,
    statement: Any,
    params: Mapping[str, Any] | None = None,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
):
    """每条 SQL 共用 Gate 截止时刻，并同步收紧 MySQL 单语句上限。"""
    if deadline is not None:
        remaining = _remaining_budget(deadline, monotonic)
        if connection.dialect.name == "mysql":
            max_execution_ms = max(1, min(30_000, int(remaining * 1000)))
            connection.exec_driver_sql(
                f"SET SESSION MAX_EXECUTION_TIME={max_execution_ms}"
            )
    result = connection.execute(statement, dict(params or {}))
    if deadline is not None:
        _remaining_budget(deadline, monotonic)
    return result


def _chunks(values: Sequence[Any], size: int = 500) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _capture_snapshot(
    engine: Engine,
    registry_ids: Sequence[str],
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> DatabaseSnapshot:
    with _read_connection(
        engine,
        deadline=deadline,
        monotonic=monotonic,
    ) as connection:
        registry_rows = tuple(
            dict(row)
            for row in _execute_budgeted(
                connection,
                text(
                    "SELECT scheme_id, base_scheme_id, horizon, task_type, "
                    "runtime_type, frequency, target_tenor, status "
                    "FROM t_scheme_registry "
                    "WHERE scheme_id IN :registry_ids"
                ).bindparams(bindparam("registry_ids", expanding=True)),
                {"registry_ids": list(registry_ids)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
        by_id = {str(row["scheme_id"]): row for row in registry_rows}
        if set(by_id) != set(registry_ids) or any(
            row.get("status") != "active" for row in registry_rows
        ):
            raise DataConsistencyError(
                "selected active Registry identities are incomplete"
            )
        scopes = [
            (
                str(row["base_scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            for row in registry_rows
        ]
        prediction_rows = tuple(
            dict(row)
            for row in _read_predictions(
                connection,
                scopes,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        prediction_rows, replaced_live_run_ids = (
            _project_snapshot_history_replacements(
                connection,
                registry_rows,
                prediction_rows,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        actual_rows = tuple(
            dict(row)
            for row in _read_actuals(
                connection,
                registry_rows,
                prediction_rows,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        actual_watermarks = tuple(
            dict(row)
            for row in _read_actual_watermarks(
                connection,
                registry_rows,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        requested_live_ids = sorted(
            {
                int(row["run_id"])
                for row in prediction_rows
                if row.get("run_id") is not None
            }
        )
        authoritative_live_ids = _read_authoritative_live_run_ids(
            connection,
            sorted({str(row["base_scheme_id"]) for row in registry_rows}),
            deadline=deadline,
            monotonic=monotonic,
        )
        authoritative_live_ids = sorted(
            set(authoritative_live_ids) - replaced_live_run_ids
        )
        requested_live_ids = sorted(
            set(requested_live_ids) | set(authoritative_live_ids)
        )
        requested_backtest_ids = sorted(
            {
                int(row["backtest_run_id"])
                for row in prediction_rows
                if row.get("backtest_run_id") is not None
            }
        )
        live_runs = _read_references(
            connection,
            table="t_scheme_runs",
            column="run_id",
            values=requested_live_ids,
            deadline=deadline,
            monotonic=monotonic,
        )
        backtest_runs = _read_references(
            connection,
            table="t_backtest_runs",
            column="id",
            values=requested_backtest_ids,
            deadline=deadline,
            monotonic=monotonic,
        )
        backtest_prediction_rows = tuple(
            dict(row)
            for row in _read_backtest_predictions(
                connection,
                requested_backtest_ids,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        scheme_versions = {
                (
                    str(row.get("lineage_scheme_id") or row["scheme_id"]),
                    str(row.get("scheme_version") or ""),
                )
                for row in prediction_rows
            }
        for run in backtest_runs.values():
            summary = _json_mapping(
                run.get("summary"), field="backtest summary"
            )
            source_exact = str(summary.get("scheme_version") or "")
            if source_exact:
                scheme_versions.add((str(run["scheme_id"]), source_exact))
        version_rows = tuple(
            dict(row)
            for row in _read_versions(
                connection,
                sorted(scheme_versions),
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        artifact_ids = sorted(
            {
                str(row.get("input_artifact_id") or "")
                for row in live_runs.values()
                if row.get("input_artifact_id")
            }
        )
        input_artifact_rows = tuple(
            dict(row)
            for row in _read_input_artifacts(
                connection,
                artifact_ids,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        calendar_rows = tuple(
            dict(row)
            for row in _execute_budgeted(
                connection,
                text(
                    "SELECT tc.rdate, tc.trade_flag, wd.week_id "
                    "FROM t_trade_calendar tc "
                    "LEFT JOIN api_wind_date wd ON wd.rdate = tc.rdate "
                    "ORDER BY tc.rdate"
                ),
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    digest = _snapshot_digest(
        registry_rows,
        prediction_rows,
        actual_rows,
        tuple(live_runs.values()),
        tuple(backtest_runs.values()),
        calendar_rows,
        backtest_prediction_rows,
        version_rows,
        input_artifact_rows,
        actual_watermarks,
    )
    return DatabaseSnapshot(
        registry_rows=registry_rows,
        prediction_rows=prediction_rows,
        actual_rows=actual_rows,
        live_runs=tuple(
            dict(row) for _, row in sorted(live_runs.items())
        ),
        backtest_runs=tuple(
            dict(row) for _, row in sorted(backtest_runs.items())
        ),
        calendar_rows=calendar_rows,
        digest=digest,
        backtest_prediction_rows=backtest_prediction_rows,
        version_rows=version_rows,
        input_artifact_rows=input_artifact_rows,
        actual_watermarks=actual_watermarks,
    )


@contextmanager
def _read_connection(
    engine: Engine,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> Iterator[Connection]:
    if deadline is not None:
        _remaining_budget(deadline, monotonic)
    connection = engine.connect()
    transaction = None
    is_mysql = engine.dialect.name == "mysql"
    try:
        if is_mysql:
            if deadline is not None:
                _remaining_budget(deadline, monotonic)
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            connection.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
            if deadline is not None:
                _remaining_budget(deadline, monotonic)
        else:
            transaction = connection.begin()
        yield connection
    finally:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        elif is_mysql:
            connection.rollback()
        connection.close()


def _read_predictions(
    connection: Connection,
    scopes: Sequence[tuple[str, str, int]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for scope_chunk in _chunks(scopes):
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for index, (scheme_id, tenor, horizon) in enumerate(scope_chunk):
            clauses.append(
                f"(scheme_id = :scheme_{index} AND target_tenor = :tenor_{index} "
                f"AND horizon = :horizon_{index})"
            )
            params.update(
                {
                    f"scheme_{index}": scheme_id,
                    f"tenor_{index}": tenor,
                    f"horizon_{index}": horizon,
                }
            )
        where = " OR ".join(clauses) or "1 = 0"
        rows.extend(
            _execute_budgeted(
                connection,
                text(
                    "SELECT id, run_id, backtest_run_id, scheme_version, scheme_id, "
                    "target_tenor, horizon, predict_date, feature_date, target_date, "
                    "predicted_direction, backtest_actual_direction "
                    f"FROM t_scheme_predictions WHERE {where} "
                    "ORDER BY scheme_id, target_tenor, horizon, target_date, id"
                ),
                params,
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _project_snapshot_history_replacements(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    prediction_rows: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> tuple[tuple[dict[str, Any], ...], frozenset[int]]:
    """按同一精确合同投影已验证替代事实；目标机无来源时保持原样。"""
    active_scopes = {
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in registry_rows
    }
    entries = load_legacy_prediction_migrations(PROJECT_ROOT)
    corrected_by_identity = {
        (item.source_scheme_id, item.designated_exact): item
        for item in load_legacy_corrected_exact_evidence(PROJECT_ROOT)
    }
    projected_rows = [dict(row) for row in prediction_rows]
    replaced_live_run_ids: set[int] = set()
    for entry in entries:
        scope = (
            entry.prediction_scheme_id,
            entry.target_tenor,
            entry.horizon,
        )
        if scope not in active_scopes:
            continue
        corrected = corrected_by_identity.get(
            (entry.designated_source_scheme_id, entry.designated_exact)
        )
        if corrected is None:
            raise DataConsistencyError(
                "historical replacement corrected evidence is missing"
            )
        source_identity = _execute_budgeted(
            connection,
            text(
                "SELECT r.base_scheme_id, r.status AS registry_status, "
                "v.runtime_type, v.status AS version_status, v.code_hash, "
                "v.config_hash, v.manifest_hash "
                "FROM t_scheme_registry r "
                "JOIN t_scheme_versions v "
                "ON v.scheme_id = r.base_scheme_id "
                "AND v.scheme_version = :source_exact "
                "WHERE r.scheme_id = :source_registry_scheme_id"
            ),
            {
                "source_registry_scheme_id": corrected.source_registry_scheme_id,
                "source_exact": entry.designated_exact,
            },
            deadline=deadline,
            monotonic=monotonic,
        ).mappings().one_or_none()
        if source_identity is None:
            orphan_count = _execute_budgeted(
                connection,
                text(
                    "SELECT "
                    "(SELECT COUNT(*) FROM t_scheme_versions "
                    " WHERE scheme_id = :source_scheme_id "
                    " AND scheme_version = :source_exact) + "
                    "(SELECT COUNT(*) FROM t_scheme_predictions "
                    " WHERE scheme_id = :source_scheme_id "
                    " AND scheme_version = :source_exact)"
                ),
                {
                    "source_scheme_id": entry.designated_source_scheme_id,
                    "source_exact": entry.designated_exact,
                },
                deadline=deadline,
                monotonic=monotonic,
            ).scalar_one()
            if int(orphan_count or 0):
                raise DataConsistencyError(
                    "historical replacement source identity is incomplete"
                )
            continue
        if dict(source_identity) != {
            "base_scheme_id": entry.designated_source_scheme_id,
            "registry_status": "archived",
            "runtime_type": "blackbox_v2",
            "version_status": "retired",
            "code_hash": entry.designated_code_hash,
            "config_hash": entry.designated_config_hash,
            "manifest_hash": entry.designated_manifest_hash,
        }:
            raise DataConsistencyError(
                "historical replacement source identity changed"
            )
        source_rows = _read_predictions(
            connection,
            [
                (
                    entry.designated_source_scheme_id,
                    entry.target_tenor,
                    entry.horizon,
                )
            ],
            deadline=deadline,
            monotonic=monotonic,
        )
        source_rows = [
            row
            for row in source_rows
            if str(row.get("scheme_version") or "") == entry.designated_exact
        ]
        products = [
            row
            for row in projected_rows
            if (
                str(row["scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            == scope
        ]
        try:
            projection = resolve_prediction_history_projection(
                products,
                source_rows,
                entry=entry,
                corrected=corrected,
            )
        except PredictionHistoryReplacementError as exc:
            raise DataConsistencyError(
                f"historical replacement evidence drift: {exc}"
            ) from exc
        if projection is None:
            continue
        excluded = projection.excluded_product_ids
        replaced_live_run_ids.update(projection.deletable_live_run_ids)
        projected_rows = [
            row
            for row in projected_rows
            if int(row["id"]) not in excluded
        ]
        projected_rows.extend(projection.source_rows)
    projected_rows.sort(
        key=lambda row: (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
            int(row["id"]),
        )
    )
    return tuple(projected_rows), frozenset(replaced_live_run_ids)


def _read_actuals(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    prediction_rows: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    grouped_dates: dict[
        tuple[str, str, str, str, str, str, str], set[str]
    ] = defaultdict(set)
    for registry in registry_rows:
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        tenor = str(registry["target_tenor"])
        if task_type == "T+1":
            table, date_column, direction_column = (
                "t_scheme_actuals",
                "trade_date",
                "direction_1d",
            )
            rule_filter = ""
        elif task_type == "T+5":
            table, date_column, direction_column = (
                "t_scheme_actuals",
                "trade_date",
                "direction_5d",
            )
            rule_filter = ""
        elif task_type in {"weekly_point", "weekly_average"}:
            table, date_column, direction_column = (
                "t_scheme_weekly_actuals",
                "target_date",
                "direction_weekly",
            )
            rule_filter = " AND target_rule = :target_rule"
        elif task_type == "monthly":
            table, date_column, direction_column = (
                "t_scheme_monthly_actuals",
                "target_date",
                "direction_monthly",
            )
            rule_filter = " AND target_rule = :target_rule"
        else:
            table, date_column, direction_column = (
                "t_scheme_period_average_actuals",
                "target_date",
                "actual_direction",
            )
            rule_filter = " AND target_rule = :target_rule"
        scope = (
            str(registry["base_scheme_id"]),
            tenor,
            int(registry["horizon"]),
        )
        dates = {
            _iso_date(row.get("target_date"), field="target_date")
            for row in prediction_rows
            if row.get("backtest_actual_direction") is None
            and (
                str(row["scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
            )
            == scope
        }
        grouped_dates[
            (
                actual_kind,
                tenor,
                target_rule,
                table,
                date_column,
                direction_column,
                rule_filter,
            )
        ].update(dates)

    rows: list[Mapping[str, Any]] = []
    for (
        actual_kind,
        tenor,
        target_rule,
        table,
        date_column,
        direction_column,
        rule_filter,
    ), target_date_set in sorted(grouped_dates.items()):
        target_dates = sorted(target_date_set)
        for date_chunk in _chunks(target_dates):
            statement = text(
                f"SELECT :actual_kind AS actual_kind, tenor AS target_tenor, "
                f"{date_column} AS target_date, "
                f":selected_rule AS target_rule, "
                f"{direction_column} AS actual_direction "
                f"FROM {table} WHERE tenor = :tenor "
                f"AND {date_column} IN :target_dates{rule_filter}"
            ).bindparams(bindparam("target_dates", expanding=True))
            params = {
                "tenor": tenor,
                "target_dates": list(date_chunk),
                "selected_rule": target_rule,
                "target_rule": target_rule,
                "actual_kind": actual_kind,
            }
            rows.extend(
                _execute_budgeted(
                    connection,
                    statement,
                    params,
                    deadline=deadline,
                    monotonic=monotonic,
                ).mappings()
            )
    return rows


def _read_actual_watermarks(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    scopes: set[tuple[str, str, str, str, str, str]] = set()
    for registry in registry_rows:
        task_type = str(registry["task_type"])
        actual_kind, target_rule = _actual_selector(task_type)
        if task_type in {"T+1", "T+5"}:
            table = "t_scheme_actuals"
            date_column = "trade_date"
            rule_filter = ""
        elif task_type in {"weekly_point", "weekly_average"}:
            table = "t_scheme_weekly_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        elif task_type == "monthly":
            table = "t_scheme_monthly_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        else:
            table = "t_scheme_period_average_actuals"
            date_column = "target_date"
            rule_filter = " AND target_rule = :target_rule"
        scopes.add(
            (
                actual_kind,
                str(registry["target_tenor"]),
                target_rule,
                table,
                date_column,
                rule_filter,
            )
        )
    watermarks: list[Mapping[str, Any]] = []
    for (
        actual_kind,
        tenor,
        target_rule,
        table,
        date_column,
        rule_filter,
    ) in sorted(scopes):
        result = _execute_budgeted(
            connection,
            text(
                f"SELECT MAX({date_column}) AS max_target_date "
                f"FROM {table} WHERE tenor = :tenor{rule_filter}"
            ),
            {"tenor": tenor, "target_rule": target_rule},
            deadline=deadline,
            monotonic=monotonic,
        ).scalar_one_or_none()
        watermarks.append(
            {
                "actual_kind": actual_kind,
                "target_tenor": tenor,
                "target_rule": target_rule,
                "max_target_date": (
                    str(result)[:10] if result is not None else None
                ),
            }
        )
    return watermarks


def _read_references(
    connection: Connection,
    *,
    table: str,
    column: str,
    values: Sequence[int],
    deadline: float | None,
    monotonic: Callable[[], float],
) -> dict[int, dict[str, Any]]:
    if not values:
        return {}
    if table == "t_scheme_runs":
        selected = (
            f"{column}, scheme_id, scheme_version, runtime_type, status, "
            "prediction_phase, input_artifact_id, data_snapshot_id, "
            "predict_date, records_written"
        )
    elif table == "t_backtest_runs":
        selected = (
            f"{column}, benchmark_id, scheme_id, status, code_hash, config_hash, "
            "input_artifact_hash, summary"
        )
    else:
        raise ValueError("unsupported consistency reference table")
    statement = text(
        f"SELECT {selected} FROM {table} WHERE {column} IN :reference_ids"
    ).bindparams(bindparam("reference_ids", expanding=True))
    result: dict[int, dict[str, Any]] = {}
    for value_chunk in _chunks(values):
        for row in _execute_budgeted(
            connection,
            statement,
            {"reference_ids": list(value_chunk)},
            deadline=deadline,
            monotonic=monotonic,
        ).mappings():
            result[int(row[column])] = {
                **dict(row),
                "reference_id": int(row[column]),
            }
    if table != "t_backtest_runs" or not result:
        return result
    source_ids = sorted({str(row["scheme_id"]) for row in result.values()})
    registry_scopes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source_chunk in _chunks(source_ids):
        for registry_row in _execute_budgeted(
            connection,
            text(
                "SELECT base_scheme_id, target_tenor, horizon, status "
                "FROM t_scheme_registry "
                "WHERE base_scheme_id IN :scheme_ids"
            ).bindparams(bindparam("scheme_ids", expanding=True)),
            {"scheme_ids": list(source_chunk)},
            deadline=deadline,
            monotonic=monotonic,
        ).mappings():
            registry_scopes[str(registry_row["base_scheme_id"])].append(
                {
                    "target_tenor": str(registry_row["target_tenor"]),
                    "horizon": int(registry_row["horizon"]),
                    "status": str(registry_row["status"]),
                }
            )
    for row in result.values():
        scheme_id = str(row["scheme_id"])
        row["source_registry_scopes"] = sorted(
            registry_scopes.get(scheme_id, []),
            key=lambda item: (item["target_tenor"], item["horizon"]),
        )
    return result


def _read_authoritative_live_run_ids(
    connection: Connection,
    scheme_ids: Sequence[str],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[int]:
    run_ids: set[int] = set()
    statement = text(
        "SELECT run_id FROM t_scheme_runs "
        "WHERE scheme_id IN :scheme_ids AND status = 'success' "
        "AND prediction_phase IN :live_phases "
        "AND predict_date >= :live_start"
    ).bindparams(
        bindparam("scheme_ids", expanding=True),
        bindparam("live_phases", expanding=True),
    )
    for scheme_chunk in _chunks(scheme_ids):
        run_ids.update(
            int(value)
            for value in _execute_budgeted(
                connection,
                statement,
                {
                    "scheme_ids": list(scheme_chunk),
                    "live_phases": sorted(LIVE_PREDICTION_PHASES),
                    "live_start": LIVE_TARGET_START_DATE.isoformat(),
                },
                deadline=deadline,
                monotonic=monotonic,
            ).scalars()
        )
    return sorted(run_ids)


def _read_backtest_predictions(
    connection: Connection,
    run_ids: Sequence[int],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    statement = text(
        "SELECT run_id, scheme_id, target_tenor, horizon, predict_date, "
        "feature_date, target_date, label, predicted_direction "
        "FROM t_backtest_predictions WHERE run_id IN :run_ids "
        "ORDER BY run_id, target_tenor, horizon, target_date, predict_date, id"
    ).bindparams(bindparam("run_ids", expanding=True))
    for run_chunk in _chunks(run_ids):
        rows.extend(
            _execute_budgeted(
                connection,
                statement,
                {"run_ids": list(run_chunk)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _read_versions(
    connection: Connection,
    identities: Sequence[tuple[str, str]],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for identity_chunk in _chunks(identities):
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for index, (scheme_id, exact) in enumerate(identity_chunk):
            clauses.append(
                f"(scheme_id = :version_scheme_{index} "
                f"AND scheme_version = :version_exact_{index})"
            )
            params[f"version_scheme_{index}"] = scheme_id
            params[f"version_exact_{index}"] = exact
        if not clauses:
            continue
        rows.extend(
            _execute_budgeted(
                connection,
                text(
                    "SELECT scheme_id, scheme_version, runtime_type, "
                    "data_snapshot_id, code_hash, config_hash, "
                    "manifest_hash, status FROM t_scheme_versions WHERE "
                    + " OR ".join(clauses)
                ),
                params,
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _read_input_artifacts(
    connection: Connection,
    artifact_ids: Sequence[str],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    statement = text(
        "SELECT artifact_id, scheme_id, scheme_version, predict_date, "
        "content_hash, schema_hash FROM t_input_artifacts "
        "WHERE artifact_id IN :artifact_ids"
    ).bindparams(bindparam("artifact_ids", expanding=True))
    for artifact_chunk in _chunks(artifact_ids):
        rows.extend(
            _execute_budgeted(
                connection,
                statement,
                {"artifact_ids": list(artifact_chunk)},
                deadline=deadline,
                monotonic=monotonic,
            ).mappings()
        )
    return rows


def _snapshot_digest(*parts: Any) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple, set, frozenset)):
            normalized = [normalize(item) for item in value]
            return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
        if isinstance(value, (date,)):
            return value.isoformat()
        return value

    raw = json.dumps(
        normalize(parts),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _remaining_budget(
    deadline: float,
    monotonic: Callable[[], float],
) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise DataConsistencyTimeout(
            "data_consistency_timeout: total gate budget exhausted"
        )
    return remaining


def _fetch_json_object(
    fetcher,
    url: str,
    ctx: GateContext,
    *,
    deadline: float,
    monotonic: Callable[[], float],
):
    remaining = _remaining_budget(deadline, monotonic)
    try:
        response = fetcher(
            url,
            timeout_sec=min(remaining, 30),
            max_response_bytes=MAX_RESPONSE_BYTES,
            session_token=ctx.dashboard_session_token,
        )
    except ApiProbeError as exc:
        raise DataConsistencyError(
            f"Dashboard probe failed: {exc.error_summary}"
        ) from exc
    _remaining_budget(deadline, monotonic)
    if not isinstance(response, tuple) or len(response) not in {2, 3}:
        raise DataConsistencyError("Dashboard fetcher returned an invalid result")
    payload, status = response[:2]
    metadata = response[2] if len(response) == 3 else {}
    if int(status) != 200:
        raise DataConsistencyError(f"Dashboard probe returned HTTP {int(status)}")
    if not isinstance(payload, Mapping):
        raise DataConsistencyError("Dashboard probe payload must be an object")
    if not isinstance(metadata, Mapping):
        raise DataConsistencyError("Dashboard probe metadata must be an object")
    return payload, dict(metadata)


def _display_month(fact: ConsistencyFact) -> str:
    target = date.fromisoformat(fact.target_date)
    if fact.task_type != "monthly_average":
        return f"{target.year:04d}-{target.month:02d}"
    if target.month == 12:
        return f"{target.year + 1:04d}-01"
    return f"{target.year:04d}-{target.month + 1:02d}"


def _actual_selector(task_type: str) -> tuple[str, str]:
    expected = TASK_COMBINATIONS.get(task_type)
    if expected is None:
        raise DataConsistencyError(f"unsupported task_type: {task_type!r}")
    target_rule = expected[1]
    if task_type == "T+1":
        return "daily_1d", target_rule
    if task_type == "T+5":
        return "daily_5d", target_rule
    if task_type == "weekly_point":
        return "weekly", WEEKLY_TARGET_RULE
    if task_type == "weekly_average":
        return "weekly", WEEKLY_AVERAGE_TARGET_RULE
    if task_type == "monthly":
        return "monthly", MONTHLY_TARGET_RULE
    return "period_average", target_rule


def _clock_display_date(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise ValueError("data consistency clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("data consistency clock must return an aware datetime")
    return value.astimezone(SHANGHAI_TIMEZONE).date().isoformat()


def _source(value: str) -> str:
    return "live" if date.fromisoformat(value) >= LIVE_TARGET_START_DATE else "backtest"


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DataConsistencyError(f"{field} must be a non-empty canonical string")
    return value


def _iso_date(value: Any, *, field: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise DataConsistencyError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataConsistencyError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise DataConsistencyError(f"{field} must be a canonical ISO date")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DataConsistencyError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _direction(
    value: Any,
    *,
    field: str,
    allow_none: bool = False,
) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in {-1, 0, 1}:
        raise DataConsistencyError(f"{field} must be one of -1, 0, 1")
    return value


def _redact_gate_result(result: GateResult, secret: str | None) -> GateResult:
    def redact(value: Any) -> Any:
        if not secret:
            return value
        if isinstance(value, str):
            return value.replace(secret, "[redacted-session]")
        if isinstance(value, Mapping):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(redact(item) for item in value)
        return value

    return GateResult(
        gate_name=result.gate_name,
        status=result.status,
        evidence=[Evidence(item.key, redact(item.value)) for item in result.evidence],
        errors=[str(redact(error)) for error in result.errors],
        started_at=result.started_at,
        finished_at=result.finished_at,
    )
