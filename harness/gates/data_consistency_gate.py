from __future__ import annotations

import hashlib
import json
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.dashboard_gate import ApiProbeError, fetch_json
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import load_scheme_config
from scheduler.repository import registry_scheme_id
from shared.calendar_service import is_trading_day_row
from shared.period_average_buckets import build_period_buckets
from shared.task_specs import TASK_COMBINATIONS
from shared.week_calendar_normalizer import normalize_week_calendar_rows


HISTORY_START_DATE = "2025-01-01"
LIVE_TARGET_START_DATE = date(2026, 6, 1)
MAX_RESPONSE_BYTES = 1_500_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
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
    live_runs: tuple[tuple[int, str], ...]
    backtest_runs: tuple[tuple[int, str], ...]
    calendar_rows: tuple[dict[str, Any], ...]
    digest: str


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
        base_url = _validated_origin(ctx.api_base_url)
        summary_url = f"{base_url}/api/factor-lab/dashboard"
        display_until = _clock_display_date(self._clock)
        engine = ctx.engine_factory()
        final_snapshot: DatabaseSnapshot | None = None
        final_snapshot_failed = False
        operation_error: Exception | None = None
        summary: Mapping[str, Any] | None = None
        summary_metadata: Mapping[str, Any] = {}
        facts: list[ConsistencyFact] = []
        errors: list[str] = []
        detail_metadata: list[dict[str, Any]] = []
        try:
            _remaining_budget(deadline, self._monotonic)
            snapshot = _capture_snapshot(engine, registry_ids)
            _remaining_budget(deadline, self._monotonic)
            try:
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
                facts = validate_and_join_facts(
                    snapshot,
                    display_until=display_until,
                )
                expected_monthly = aggregate_display_facts(facts)
                _remaining_budget(deadline, self._monotonic)
                errors = _compare_summary(
                    summary,
                    registry_ids=registry_ids,
                    expected=expected_monthly,
                )
                if summary_display_until != display_until:
                    errors.append(
                        "Dashboard Summary display_until does not match the "
                        "independent Shanghai business date"
                    )

                expected_details = _detail_rows_by_partition(facts)
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
                    errors.extend(
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
            except Exception as exc:
                operation_error = exc
            finally:
                try:
                    _remaining_budget(deadline, self._monotonic)
                    final_snapshot = _capture_snapshot(engine, registry_ids)
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
            Evidence("joined_fact_count", len(facts)),
            Evidence(
                "summary_snapshot_id",
                summary.get("snapshot_id") if summary is not None else None,
            ),
            Evidence("summary_request_id", summary_metadata.get("request_id")),
            Evidence("summary_fetched_at", summary_metadata.get("fetched_at")),
            Evidence("detail_requests", detail_metadata),
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
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED if not errors else GateStatus.FAILED,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=utc_now(),
        )


def validate_and_join_facts(
    snapshot: DatabaseSnapshot,
    *,
    display_until: str,
) -> list[ConsistencyFact]:
    """独立校验产品事实并按任务规则关联已发布或 live Actual。"""
    calendar = _FrozenCalendar.from_rows(snapshot.calendar_rows)
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
        expected = TASK_COMBINATIONS.get(task_type)
        if expected is None or expected[0] != scope[2]:
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

    live_runs = dict(snapshot.live_runs)
    backtest_runs = dict(snapshot.backtest_runs)
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
            if live_runs[live_run_id] != scheme_id:
                raise DataConsistencyError(
                    f"prediction references another scheme's live run: {business_key!r}"
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
            if backtest_runs[backtest_run_id] != scheme_id:
                raise DataConsistencyError(
                    "prediction references another scheme's backtest run: "
                    f"{business_key!r}"
                )
            if published_actual is None:
                raise DataConsistencyError(
                    f"backtest prediction is missing immutable Actual: {business_key!r}"
                )
            if predict_date != feature_date:
                raise DataConsistencyError(
                    f"backtest prediction must have predict_date=feature_date: {business_key!r}"
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
            if target_date != expected_target:
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
            trigger = date.fromisoformat(predict_date if is_live else feature_date)
            if is_live and trigger.day != 15:
                raise DataConsistencyError(
                    f"monthly live predict_date must be natural month 15, got {predict_date}"
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


def _capture_snapshot(engine: Engine, registry_ids: Sequence[str]) -> DatabaseSnapshot:
    with _read_connection(engine) as connection:
        registry_rows = tuple(
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT scheme_id, base_scheme_id, horizon, task_type, "
                    "target_tenor, status FROM t_scheme_registry "
                    "WHERE scheme_id IN :registry_ids"
                ).bindparams(bindparam("registry_ids", expanding=True)),
                {"registry_ids": list(registry_ids)},
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
            dict(row) for row in _read_predictions(connection, scopes)
        )
        actual_rows = tuple(
            dict(row)
            for row in _read_actuals(connection, registry_rows, prediction_rows)
        )
        requested_live_ids = sorted(
            {
                int(row["run_id"])
                for row in prediction_rows
                if row.get("run_id") is not None
            }
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
        )
        backtest_runs = _read_references(
            connection,
            table="t_backtest_runs",
            column="id",
            values=requested_backtest_ids,
        )
        calendar_rows = tuple(
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT tc.rdate, tc.trade_flag, wd.week_id "
                    "FROM t_trade_calendar tc "
                    "LEFT JOIN api_wind_date wd ON wd.rdate = tc.rdate "
                    "ORDER BY tc.rdate"
                )
            ).mappings()
        )
    digest = _snapshot_digest(
        registry_rows,
        prediction_rows,
        actual_rows,
        live_runs,
        backtest_runs,
        calendar_rows,
    )
    return DatabaseSnapshot(
        registry_rows=registry_rows,
        prediction_rows=prediction_rows,
        actual_rows=actual_rows,
        live_runs=tuple(sorted(live_runs.items())),
        backtest_runs=tuple(sorted(backtest_runs.items())),
        calendar_rows=calendar_rows,
        digest=digest,
    )


@contextmanager
def _read_connection(engine: Engine) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = None
    is_mysql = engine.dialect.name == "mysql"
    try:
        if is_mysql:
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            connection.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
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
):
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (scheme_id, tenor, horizon) in enumerate(scopes):
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
    return connection.execute(
        text(
            "SELECT id, run_id, backtest_run_id, scheme_version, scheme_id, "
            "target_tenor, horizon, predict_date, feature_date, target_date, "
            "predicted_direction, backtest_actual_direction "
            f"FROM t_scheme_predictions WHERE {where} "
            "ORDER BY scheme_id, target_tenor, horizon, target_date, id"
        ),
        params,
    ).mappings()


def _read_actuals(
    connection: Connection,
    registry_rows: Sequence[Mapping[str, Any]],
    prediction_rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    target_dates = sorted(
        {_iso_date(row.get("target_date"), field="target_date") for row in prediction_rows}
    )
    if not target_dates:
        return []
    rows: list[Mapping[str, Any]] = []
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
        statement = text(
            f"SELECT :actual_kind AS actual_kind, tenor AS target_tenor, "
            f"{date_column} AS target_date, "
            f":selected_rule AS target_rule, {direction_column} AS actual_direction "
            f"FROM {table} WHERE tenor = :tenor "
            f"AND {date_column} IN :target_dates{rule_filter}"
        ).bindparams(bindparam("target_dates", expanding=True))
        params = {
            "tenor": tenor,
            "target_dates": target_dates,
            "selected_rule": target_rule,
            "target_rule": target_rule,
            "actual_kind": actual_kind,
        }
        rows.extend(connection.execute(statement, params).mappings())
    return rows


def _read_references(
    connection: Connection,
    *,
    table: str,
    column: str,
    values: Sequence[int],
) -> dict[int, str]:
    if not values:
        return {}
    statement = text(
        f"SELECT {column}, scheme_id FROM {table} "
        f"WHERE {column} IN :reference_ids"
    ).bindparams(bindparam("reference_ids", expanding=True))
    return {
        int(row[column]): str(row["scheme_id"])
        for row in connection.execute(
            statement, {"reference_ids": list(values)}
        ).mappings()
    }


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


def _validated_origin(value: str) -> str:
    base_url = str(value).strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("data consistency api_base_url must be an HTTP(S) origin")
    return base_url


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
    if task_type in {"weekly_point", "weekly_average"}:
        return "weekly", target_rule
    if task_type == "monthly":
        return "monthly", target_rule
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
