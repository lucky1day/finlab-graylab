from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from shared.calendar_service import is_trading_day_row
from shared.period_average_buckets import build_period_buckets
from shared.week_calendar_normalizer import normalize_week_calendar_rows
from harness.gates.consistency.common import DataConsistencyError, _iso_date


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
