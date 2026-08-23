from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from shared.actual_facts import (
    build_daily_actual_records_from_rows,
    build_monthly_actual_records_from_rows,
    build_weekly_actual_records_from_rows,
    read_trade_calendar_rows,
    read_week_calendar_rows,
    read_yield_rows,
)
from shared.blackbox_v2.contracts import (
    TASK_COMBINATIONS,
    BlackboxMetadata,
    BlackboxRequest,
)
from shared.blackbox_v2.requests import build_request
from shared.blackbox_v2.snapshot import BlackboxSnapshot
from shared.calendar_service import get_calendar, is_trading_day_row
from shared.input_artifacts import resolve_blackbox_input_cutoffs_bulk
from shared.prediction_context import (
    MONTHLY_TARGET_RULE,
    WEEKLY_AVERAGE_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.period_average_buckets import (
    build_period_buckets,
    complete_bucket_average,
    target_pointer,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES
from shared.week_calendar_normalizer import normalize_week_calendar_rows


CURRENT_SNAPSHOT_REPLAY = "current_snapshot_as_of_not_historical_vintage"
PLATFORM_ACTUAL_RULE_BY_TASK = {
    "weekly_point": WEEKLY_TARGET_RULE,
    "weekly_average": WEEKLY_AVERAGE_TARGET_RULE,
    "monthly": MONTHLY_TARGET_RULE,
    **{
        task_type: TASK_COMBINATIONS[task_type][1]
        for task_type in PERIOD_AVERAGE_TASK_TYPES
    },
}


@dataclass(frozen=True)
class HistoricalCase:
    request: BlackboxRequest
    label: int
    actual_extra: dict[str, Any]


@dataclass(frozen=True)
class _Candidate:
    predict_date: str
    feature_date: str
    target_date: str
    label: int
    actual_extra: dict[str, Any]


def build_historical_cases(
    metadata: BlackboxMetadata,
    snapshot: BlackboxSnapshot,
    engine,
    *,
    limit: int | None,
    target_date_before: str,
    predict_date_from: str = "2025-01-01",
) -> list[HistoricalCase]:
    """按平台日期与 actual 事实生成当前快照 as-of 历史 Request。"""
    _validate_metadata_contract(metadata)
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise ValueError("historical case limit must be a positive integer")
    target_date_before = _iso_date(target_date_before, "target_date_before")
    predict_date_from = _iso_date(predict_date_from, "predict_date_from")
    if target_date_before <= predict_date_from:
        raise ValueError("target_date_before must be after predict_date_from")

    yield_rows = read_yield_rows(engine, tenors=[metadata.target_tenor], end_date=target_date_before)
    trade_calendar_rows = read_trade_calendar_rows(engine)
    if metadata.task_type in {"T+1", "T+5"}:
        candidates = _daily_candidates(metadata, yield_rows, trade_calendar_rows)
    elif metadata.task_type in {"weekly_point", "weekly_average"}:
        candidates = _weekly_candidates(metadata, yield_rows, read_week_calendar_rows(engine))
    elif metadata.task_type == "monthly":
        candidates = _monthly_candidates(metadata, yield_rows, trade_calendar_rows, engine)
    elif metadata.task_type in PERIOD_AVERAGE_TASK_TYPES:
        candidates = _period_average_candidates(
            metadata,
            yield_rows,
            trade_calendar_rows,
        )
    else:
        raise ValueError(f"unsupported Blackbox historical task_type: {metadata.task_type}")
    if metadata.task_type == "monthly":
        _require_actual_source_coverage(
            yield_rows,
            trade_calendar_rows,
            predict_date_from=predict_date_from,
            target_date_before=target_date_before,
        )

    eligible = [
        item
        for item in candidates
        if item.predict_date >= predict_date_from
        and _candidate_complete_date(item) < target_date_before
    ]
    eligible.sort(key=lambda item: (item.predict_date, item.target_date))
    if limit is not None and len(eligible) < limit:
        raise ValueError(
            f"historical backtest requires exactly {limit} unique cases before "
            f"{target_date_before}, found {len(eligible)}"
        )

    selected = eligible if limit is None else eligible[-limit:]
    if not selected:
        raise ValueError(
            "historical backtest requires at least one unique case in interval "
            f"[{predict_date_from}, {target_date_before})"
        )
    if metadata.task_type in {"weekly_point", "weekly_average"}:
        _validate_selected_weekly_candidates(selected)
    elif metadata.task_type in {"T+1", "T+5"}:
        _require_actual_source_coverage(
            yield_rows,
            trade_calendar_rows,
            predict_date_from=selected[0].feature_date,
            target_date_before=target_date_before,
            source_gap_is_horizon_violation=True,
        )
    cutoffs_by_feature_date = resolve_blackbox_input_cutoffs_bulk(
        snapshot,
        feature_dates=[candidate.feature_date for candidate in selected],
        engine=engine,
    )
    cases: list[HistoricalCase] = []
    for candidate in selected:
        cutoffs = cutoffs_by_feature_date[candidate.feature_date]
        request = build_request(
            scheme_id=metadata.scheme_id,
            predict_date=candidate.predict_date,
            feature_date=candidate.feature_date,
            target_date=candidate.target_date,
            cutoffs=cutoffs,
        )
        cases.append(
            HistoricalCase(
                request=request,
                label=_direction(candidate.label),
                actual_extra={
                    **candidate.actual_extra,
                    "replay_semantics": CURRENT_SNAPSHOT_REPLAY,
                    "data_snapshot_id": snapshot.snapshot_id,
                },
            )
        )
    validate_historical_cases(cases, expected_count=len(selected))
    return cases


def validate_historical_cases(cases: Iterable[HistoricalCase], *, expected_count: int) -> list[HistoricalCase]:
    materialized = list(cases)
    if len(materialized) != expected_count:
        raise ValueError(f"historical case count must be exactly {expected_count}, got {len(materialized)}")
    fields = {
        "request_id": [item.request.request_id for item in materialized],
        "predict_date": [item.request.predict_date for item in materialized],
        "target_date": [item.request.target_date for item in materialized],
    }
    for field, values in fields.items():
        if len(values) != len(set(values)):
            raise ValueError(f"historical cases contain duplicate {field}")
    if any(item.label not in {-1, 0, 1} for item in materialized):
        raise ValueError("historical labels must be -1, 0 or 1")
    return materialized


def _validate_metadata_contract(metadata: BlackboxMetadata) -> None:
    task_type = metadata.task_type
    if not isinstance(task_type, str) or task_type not in TASK_COMBINATIONS:
        raise ValueError(
            "Blackbox historical metadata contract has unsupported "
            f"task_type={task_type!r}"
        )
    expected_horizon, expected_rule, _ = TASK_COMBINATIONS[task_type]
    if type(metadata.horizon) is not int or (
        metadata.horizon,
        metadata.target_rule,
    ) != (expected_horizon, expected_rule):
        raise ValueError(
            "Blackbox historical metadata contract requires fixed "
            "task_type/horizon/target_rule combination: "
            f"task_type={task_type!r}, "
            f"expected_horizon={expected_horizon!r}, "
            f"expected_target_rule={expected_rule!r}, "
            f"got_horizon={metadata.horizon!r}, "
            f"got_target_rule={metadata.target_rule!r}"
        )


def _daily_candidates(
    metadata: BlackboxMetadata,
    yield_rows: list[dict],
    calendar_rows: list[dict],
) -> list[_Candidate]:
    records = build_daily_actual_records_from_rows(yield_rows, strict_duplicates=True)
    actual_by_target = _unique_facts(
        ((record.tenor, record.trade_date), record) for record in records
    )
    trading_days = sorted(
        str(row["rdate"])[:10]
        for row in calendar_rows
        if is_trading_day_row(str(row["rdate"])[:10], row.get("trade_flag"))
    )
    calendar_index = {value: index for index, value in enumerate(trading_days)}
    source_rows = sorted(
        (row for row in yield_rows if str(row["tenor"]) == metadata.target_tenor),
        key=lambda row: str(row["trade_date"]),
    )
    source_dates = [str(row["trade_date"])[:10] for row in source_rows]
    source_index = {value: index for index, value in enumerate(source_dates)}
    source_yield = {str(row["trade_date"])[:10]: float(row["close_yield"]) for row in source_rows}
    direction_field = "direction_1d" if metadata.task_type == "T+1" else "direction_5d"
    candidates: list[_Candidate] = []
    for target_date in trading_days:
        target_index = calendar_index[target_date]
        if target_index < metadata.horizon:
            continue
        feature_date = trading_days[target_index - metadata.horizon]
        fact = actual_by_target.get((metadata.target_tenor, target_date))
        if fact is None or getattr(fact, direction_field) is None:
            continue
        if target_date not in source_index or source_index[target_date] < metadata.horizon:
            continue
        if source_dates[source_index[target_date] - metadata.horizon] != feature_date:
            continue
        candidates.append(
            _Candidate(
                predict_date=feature_date,
                feature_date=feature_date,
                target_date=target_date,
                label=getattr(fact, direction_field),
                actual_extra={
                    "actual_fact_key": [metadata.target_tenor, target_date],
                    "direction_field": direction_field,
                    "calendar_horizon": metadata.horizon,
                    "feature_yield": source_yield[feature_date],
                    "target_yield": source_yield[target_date],
                },
            )
        )
    return candidates


def _weekly_candidates(
    metadata: BlackboxMetadata,
    yield_rows: list[dict],
    calendar_rows: list[dict],
) -> list[_Candidate]:
    platform_rule = PLATFORM_ACTUAL_RULE_BY_TASK[metadata.task_type]
    records = [
        record
        for record in build_weekly_actual_records_from_rows(
            yield_rows,
            calendar_rows,
            strict_duplicates=True,
        )
        if record.target_rule == platform_rule and record.tenor == metadata.target_tenor
    ]
    facts = _unique_facts(
        ((record.tenor, record.target_date, record.target_rule), record) for record in records
    )
    expected_week_ends = _weekday_trading_week_ends(calendar_rows)
    candidates: list[_Candidate] = []
    for fact in facts.values():
        expected_feature_end = expected_week_ends.get(int(fact.feature_week_id))
        expected_target_end = expected_week_ends.get(int(fact.target_week_id))
        candidates.append(
            _Candidate(
                predict_date=fact.feature_date,
                feature_date=fact.feature_date,
                target_date=fact.target_date,
                label=fact.direction_weekly,
                actual_extra={
                    "actual_fact_key": [fact.tenor, fact.target_date, fact.target_rule],
                    "platform_actual_rule": platform_rule,
                    "feature_week_id": str(fact.feature_week_id),
                    "target_week_id": str(fact.target_week_id),
                    "feature_week_end": fact.feature_date,
                    "target_week_end": fact.target_date,
                    "expected_feature_week_end": expected_feature_end,
                    "expected_target_week_end": expected_target_end,
                    "feature_yield": fact.feature_yield,
                    "target_yield": fact.target_yield,
                    "actual_extra": dict(fact.extra or {}),
                },
            )
        )
    return candidates


def _validate_selected_weekly_candidates(candidates: list[_Candidate]) -> None:
    """只校验本次实际入选窗口，避免无关旧数据缺口阻断最近样本。"""
    for candidate in candidates:
        expected_feature = candidate.actual_extra.get("expected_feature_week_end")
        expected_target = candidate.actual_extra.get("expected_target_week_end")
        if expected_feature is None or expected_target is None:
            raise ValueError(
                "weekly actual week is missing a weekday trading observation: "
                f"feature_week_id={candidate.actual_extra.get('feature_week_id')}, "
                f"target_week_id={candidate.actual_extra.get('target_week_id')}"
            )
        if candidate.feature_date != expected_feature or candidate.target_date != expected_target:
            raise ValueError(
                "weekly actual does not use exact calendar week ends: "
                f"feature={candidate.feature_date}/{expected_feature}, "
                f"target={candidate.target_date}/{expected_target}"
            )


def _weekday_trading_week_ends(calendar_rows: list[dict]) -> dict[int, str]:
    """返回每周最后一个工作日债券观测日，忽略通用日历中的调休周末。"""
    result: dict[int, str] = {}
    for row in normalize_week_calendar_rows(calendar_rows):
        rdate = str(row["rdate"])[:10]
        if row.get("week_id") is None or not is_trading_day_row(
            rdate, row.get("trade_flag")
        ):
            continue
        week_id = int(str(row["week_id"]).replace(".0", ""))
        if week_id not in result or rdate > result[week_id]:
            result[week_id] = rdate
    return result


def _monthly_candidates(
    metadata: BlackboxMetadata,
    yield_rows: list[dict],
    calendar_rows: list[dict],
    engine,
) -> list[_Candidate]:
    platform_rule = PLATFORM_ACTUAL_RULE_BY_TASK[metadata.task_type]
    records = build_monthly_actual_records_from_rows(
        yield_rows,
        calendar_rows,
        strict_duplicates=True,
    )
    facts = _unique_facts(
        ((record.tenor, record.target_date, record.target_rule), record)
        for record in records
        if record.tenor == metadata.target_tenor and record.target_rule == platform_rule
    )
    calendar = get_calendar(engine)
    candidates: list[_Candidate] = []
    for fact in facts.values():
        feature_anchor = f"{fact.feature_month_id}-15"
        target_anchor = f"{fact.target_month_id}-15"
        expected_feature = feature_anchor if calendar.is_trading_day(feature_anchor) else calendar.previous_trading_day(feature_anchor)
        expected_target = target_anchor if calendar.is_trading_day(target_anchor) else calendar.previous_trading_day(target_anchor)
        if fact.predict_date != feature_anchor or fact.feature_date != expected_feature or fact.target_date != expected_target:
            raise ValueError(
                "monthly actual does not match natural-15 calendar semantics: "
                f"predict={fact.predict_date}, feature={fact.feature_date}, target={fact.target_date}"
            )
        candidates.append(
            _Candidate(
                predict_date=feature_anchor,
                feature_date=expected_feature,
                target_date=expected_target,
                label=fact.direction_monthly,
                actual_extra={
                    "actual_fact_key": [fact.tenor, fact.target_date, fact.target_rule],
                    "platform_actual_rule": platform_rule,
                    "feature_month_id": fact.feature_month_id,
                    "target_month_id": fact.target_month_id,
                    "feature_yield": fact.feature_yield,
                    "target_yield": fact.target_yield,
                    "actual_extra": dict(fact.extra or {}),
                },
            )
        )
    return candidates


def _period_average_candidates(
    metadata: BlackboxMetadata,
    yield_rows: list[dict],
    calendar_rows: list[dict],
) -> list[_Candidate]:
    """按两个连续完整业务桶生成周期均值历史案例。"""
    source_rows = [
        row for row in yield_rows if str(row.get("tenor")) == metadata.target_tenor
    ]
    if not source_rows:
        return []
    source_dates = sorted(str(row["trade_date"])[:10] for row in source_rows)
    source_start, source_end = source_dates[0], source_dates[-1]
    buckets = build_period_buckets(metadata.task_type, calendar_rows)
    candidates: list[_Candidate] = []
    for feature_bucket, target_bucket in zip(buckets, buckets[1:]):
        if (
            feature_bucket.start_date < source_start
            or target_bucket.anchor_date > source_end
        ):
            continue
        feature_rows = [
            row
            for row in source_rows
            if feature_bucket.start_date
            <= str(row["trade_date"])[:10]
            <= feature_bucket.end_date
        ]
        target_rows = [
            row
            for row in source_rows
            if target_bucket.start_date
            <= str(row["trade_date"])[:10]
            <= target_bucket.end_date
        ]
        feature_yield = complete_bucket_average(feature_bucket, feature_rows)
        target_yield = complete_bucket_average(target_bucket, target_rows)
        direction = (
            1
            if target_yield > feature_yield
            else -1 if target_yield < feature_yield else 0
        )
        target_date = target_pointer(feature_bucket.anchor_date)
        candidates.append(
            _Candidate(
                predict_date=feature_bucket.anchor_date,
                feature_date=feature_bucket.anchor_date,
                target_date=target_date,
                label=direction,
                actual_extra={
                    "actual_fact_key": [
                        metadata.target_tenor,
                        target_date,
                        metadata.target_rule,
                    ],
                    "platform_actual_rule": metadata.target_rule,
                    "feature_bucket": _bucket_evidence(feature_bucket),
                    "target_bucket": _bucket_evidence(target_bucket),
                    "feature_yield": feature_yield,
                    "target_yield": target_yield,
                    "actual_target_anchor": target_bucket.anchor_date,
                },
            )
        )
    return candidates


def _bucket_evidence(bucket) -> dict[str, Any]:
    return {
        "label": bucket.label,
        "start_date": bucket.start_date,
        "end_date": bucket.end_date,
        "anchor_date": bucket.anchor_date,
        "sample_count": len(bucket.trading_days),
    }


def _candidate_complete_date(candidate: _Candidate) -> str:
    return str(
        candidate.actual_extra.get("actual_target_anchor")
        or candidate.target_date
    )[:10]


def _unique_facts(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate actual fact for key={key}")
        result[key] = value
    return result


def _direction(value: Any) -> int:
    if type(value) is not int or value not in {-1, 0, 1}:
        raise ValueError(f"invalid actual direction: {value!r}")
    return value


def _require_actual_source_coverage(
    yield_rows: list[dict],
    calendar_rows: list[dict],
    *,
    predict_date_from: str,
    target_date_before: str,
    source_gap_is_horizon_violation: bool = False,
) -> None:
    source_dates = {str(row["trade_date"])[:10] for row in yield_rows}
    if not source_dates:
        raise ValueError("missing platform actual source facts")
    coverage_start = max(predict_date_from, min(source_dates))
    expected_dates = {
        str(row["rdate"])[:10]
        for row in calendar_rows
        if is_trading_day_row(str(row["rdate"])[:10], row.get("trade_flag"))
        and coverage_start <= str(row["rdate"])[:10] < target_date_before
    }
    missing = sorted(expected_dates - source_dates)
    if missing:
        prefix = (
            "source gap violates calendar horizon: "
            if source_gap_is_horizon_violation
            else ""
        )
        raise ValueError(
            f"{prefix}missing platform actual source facts for trading dates: "
            f"{missing[:10]}"
        )


def _iso_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
