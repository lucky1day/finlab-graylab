"""只读计算 active 方案的 live 信号缺口。

Blackbox 从当前精确 active version 的 ``approved_at``（UTC 存储、映射到
Asia/Shanghai 业务日）之后开始，Native 从 Registry ``created_at`` 日历日
之后开始。同日没有可审计的调度时钟，故不计为已到期；本模块从不读取
``deployed_at``、DataBridge 或文件。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from shared.actual_facts import build_week_calendar
from shared.calendar_service import (
    is_trading_day_row,
    read_calendar_snapshot_from_connection,
)
from shared.prediction_context import (
    LIVE_PREDICTION_PHASES,
    build_daily_live_context,
    build_monthly_live_context,
    build_period_average_live_context,
    build_weekly_live_context,
    is_weekly_signal_date,
)
from shared.period_average_buckets import period_anchor_dates
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


_SHANGHAI = ZoneInfo("Asia/Shanghai")
FailureCategory = Literal[
    "activation_unavailable",
    "calendar_context_unavailable",
    "data_bridge_ready_timeout",
    "no_run",
    "prediction_ambiguous",
    "prediction_missing",
    "run_failed",
    "run_pending",
    "run_skipped",
]


class SignalGapReportError(RuntimeError):
    """报告输入、控制面或日历不满足只读计算契约。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SignalTarget:
    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    frequency: str
    task_type: str
    target_tenor: str
    horizon: int
    available_after: str | None
    scheme_version: str | None
    failure_category: FailureCategory | None = None

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.base_scheme_id, self.target_tenor, self.horizon


@dataclass(frozen=True, slots=True)
class SignalCase:
    registry_scheme_id: str
    base_scheme_id: str
    runtime_type: str
    frequency: str
    task_type: str
    target_tenor: str
    horizon: int
    scheme_version: str | None
    predict_date: str
    feature_date: str
    target_date: str
    prediction_phase: str | None = None
    failure_category: FailureCategory | None = None

    @property
    def key(self) -> tuple[str, str, int, str, str, str]:
        return (
            self.base_scheme_id,
            self.target_tenor,
            self.horizon,
            self.predict_date,
            self.feature_date,
            self.target_date,
        )


@dataclass(frozen=True, slots=True)
class LatestDueSignalStatus:
    registry_scheme_id: str
    state: Literal["missing", "not_due", "present"]
    failure_category: FailureCategory | None


@dataclass(frozen=True, slots=True)
class SignalGapReport:
    start_date: str
    end_date: str
    targets: tuple[SignalTarget, ...]
    expected: tuple[SignalCase, ...]
    present: tuple[SignalCase, ...]
    missing: tuple[SignalCase, ...]


def read_signal_gap_report(
    connection: Connection,
    *,
    start_date: str,
    end_date: str,
) -> SignalGapReport:
    """仅使用 caller 的连接；caller 可将其放入已有只读快照。"""

    start, end = _range(start_date, end_date)
    targets = _targets(connection)
    calendar = _SnapshotCalendar(
        connection,
        history_start_date=start,
    )
    expected, targets = _expected(targets, calendar, start, end)
    predictions = _live_rows(
        connection,
        table="t_scheme_predictions",
        fields=(
            "scheme_id, scheme_version, target_tenor, horizon, predict_date, "
            "feature_date, target_date, prediction_phase",
        ),
        base_ids=tuple(sorted({item.base_scheme_id for item in targets})),
        start_date=start,
        end_date=end,
    )
    runs = _live_rows(
        connection,
        table="t_scheme_runs",
        fields=(
            "scheme_id, scheme_version, predict_date, prediction_phase, status, "
            "error_message",
        ),
        base_ids=tuple(sorted({item.base_scheme_id for item in targets})),
        start_date=start,
        end_date=end,
    )
    present, missing = _classify(expected, targets, predictions, runs)
    return SignalGapReport(start, end, targets, expected, present, missing)


def read_latest_due_signal_statuses(
    connection: Connection,
    *,
    start_date: str,
    as_of_date: str,
) -> tuple[LatestDueSignalStatus, ...]:
    """在同一连接内汇总完整可报告区间，旧缺口不会被后来信号掩盖。"""

    return latest_due_signal_statuses(
        read_signal_gap_report(
            connection,
            start_date=start_date,
            end_date=as_of_date,
        )
    )


def latest_due_signal_statuses(
    report: SignalGapReport,
) -> tuple[LatestDueSignalStatus, ...]:
    """每个 active Registry 返回一条 Dashboard 信号状态。"""

    expected: dict[str, list[SignalCase]] = {}
    missing: dict[str, list[SignalCase]] = {}
    present = {(item.registry_scheme_id, item.predict_date) for item in report.present}
    for item in report.expected:
        expected.setdefault(item.registry_scheme_id, []).append(item)
    for item in report.missing:
        missing.setdefault(item.registry_scheme_id, []).append(item)
    result: list[LatestDueSignalStatus] = []
    for target in report.targets:
        due = sorted(expected.get(target.registry_scheme_id, ()), key=_case_sort_key)
        unresolved = sorted(missing.get(target.registry_scheme_id, ()), key=_case_sort_key)
        if target.failure_category:
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    "missing",
                    target.failure_category,
                )
            )
        elif unresolved:
            latest_missing = unresolved[-1]
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    "missing",
                    latest_missing.failure_category,
                )
            )
        elif due:
            latest = due[-1]
            state: Literal["present", "missing"] = (
                "present"
                if (latest.registry_scheme_id, latest.predict_date) in present
                else "missing"
            )
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    state,
                    None,
                )
            )
        else:
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    "not_due",
                    None,
                )
            )
    return tuple(sorted(result, key=lambda item: item.registry_scheme_id))


def _targets(connection: Connection) -> tuple[SignalTarget, ...]:
    rows = connection.execute(
        text(
            """
            SELECT r.scheme_id, r.base_scheme_id, r.runtime_type, r.frequency,
                   r.task_type, r.target_tenor, r.horizon, r.created_at,
                   v.scheme_version, v.approved_at
            FROM t_scheme_registry r
            LEFT JOIN t_scheme_versions v
              ON v.scheme_id = r.base_scheme_id
             AND v.status = 'active'
             AND v.runtime_type = 'blackbox_v2'
            WHERE r.status = 'active'
            ORDER BY r.scheme_id, v.scheme_version
            """
        )
    ).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["scheme_id"]), []).append(dict(row))
    targets: list[SignalTarget] = []
    identities: set[tuple[str, str, int]] = set()
    for registry_id, group in sorted(grouped.items()):
        row = group[0]
        runtime = str(row["runtime_type"])
        version: str | None = None
        availability: str | None = None
        failure: FailureCategory | None = None
        if runtime == "blackbox_v2":
            versions = {
                (
                    str(item["scheme_version"]),
                    _blackbox_approved_calendar_date(item["approved_at"]),
                )
                for item in group
                if item["scheme_version"] is not None
            }
            if len(versions) == 1:
                version, availability = next(iter(versions))
                if availability is None:
                    failure = "activation_unavailable"
            else:
                failure = "activation_unavailable"
        elif runtime == "native_adapter":
            availability = _timestamp_date(row["created_at"])
            if availability is None:
                failure = "activation_unavailable"
        else:
            failure = "activation_unavailable"
        target = SignalTarget(
            registry_id,
            str(row["base_scheme_id"]),
            runtime,
            str(row["frequency"]),
            str(row["task_type"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            availability,
            version,
            failure,
        )
        if target.identity in identities:
            raise SignalGapReportError("active_target_identity_ambiguous")
        identities.add(target.identity)
        targets.append(target)
    return tuple(sorted(targets, key=lambda item: item.registry_scheme_id))


def _live_rows(
    connection: Connection,
    *,
    table: Literal["t_scheme_predictions", "t_scheme_runs"],
    fields: tuple[str],
    base_ids: Sequence[str],
    start_date: str,
    end_date: str,
) -> tuple[dict[str, Any], ...]:
    if not base_ids:
        return ()
    rows = connection.execute(
        text(
            f"""
            SELECT {fields[0]}
            FROM {table}
            WHERE scheme_id IN :base_ids
              AND predict_date BETWEEN :start_date AND :end_date
              AND prediction_phase IN :phases
            """
        ).bindparams(
            bindparam("base_ids", expanding=True),
            bindparam("phases", expanding=True),
        ),
        {
            "base_ids": list(base_ids),
            "start_date": start_date,
            "end_date": end_date,
            "phases": tuple(sorted(LIVE_PREDICTION_PHASES)),
        },
    ).mappings().all()
    return tuple(dict(item) for item in rows)


def _expected(
    targets: Sequence[SignalTarget],
    calendar: "_SnapshotCalendar",
    start_date: str,
    end_date: str,
) -> tuple[tuple[SignalCase, ...], tuple[SignalTarget, ...]]:
    """返回预期 case 与（可能被标记为日历不可用的）target。"""
    result: list[SignalCase] = []
    resolved: list[SignalTarget] = []
    predict_dates_by_cadence: dict[tuple[str, str], tuple[str, ...]] = {}
    contexts: dict[tuple[str, str, int, str], object | None] = {}
    for target in targets:
        resolved.append(target)
        if target.available_after is None or target.failure_category:
            continue
        cadence_key = (
            "task_type",
            target.task_type,
        ) if target.task_type in PERIOD_AVERAGE_TASK_TYPES else (
            "frequency",
            target.frequency,
        )
        predict_dates = predict_dates_by_cadence.get(cadence_key)
        if predict_dates is None:
            predict_dates = (
                calendar.period_predict_dates(
                    target.task_type,
                    start_date,
                    end_date,
                )
                if target.task_type in PERIOD_AVERAGE_TASK_TYPES
                else calendar.predict_dates(
                    target.frequency,
                    start_date,
                    end_date,
                )
            )
            predict_dates_by_cadence[cadence_key] = predict_dates
        for predict_date in predict_dates:
            if predict_date <= target.available_after:
                continue
            context_key = (
                target.task_type,
                target.frequency,
                target.horizon,
                predict_date,
            )
            if context_key not in contexts:
                try:
                    if target.task_type in PERIOD_AVERAGE_TASK_TYPES:
                        context = build_period_average_live_context(
                            calendar,
                            predict_date,
                            task_type=target.task_type,
                        )
                    elif target.frequency == "daily":
                        context = build_daily_live_context(
                            calendar, predict_date, horizon=target.horizon
                        )
                    elif target.frequency == "weekly":
                        context = build_weekly_live_context(calendar, predict_date)
                    elif target.frequency == "monthly":
                        context = build_monthly_live_context(calendar, predict_date)
                    else:
                        raise SignalGapReportError("unsupported_signal_cadence")
                except SignalGapReportError:
                    raise
                except (KeyError, ValueError):
                    context = None
                contexts[context_key] = context
            context = contexts[context_key]
            if context is None:
                # 单个 target 的日历问题不得让整份报告不可用（dashboard 会因此
                # 整页 503），但也不能静默降级成"未到期"——无法区分"日历还没
                # 延长"与"周历数据缺行"。把该 target 标记为日历不可用后跳过，
                # 由 latest_due_signal_statuses 以 missing + failure_category
                # 呈现，保持可见且 fail-closed。
                resolved[-1] = replace(
                    target, failure_category="calendar_context_unavailable"
                )
                break
            result.append(
                SignalCase(
                    target.registry_scheme_id,
                    target.base_scheme_id,
                    target.runtime_type,
                    target.frequency,
                    target.task_type,
                    target.target_tenor,
                    target.horizon,
                    target.scheme_version,
                    predict_date,
                    str(context.feature_date),
                    str(context.target_date),
                )
            )
    return tuple(sorted(result, key=_case_sort_key)), tuple(resolved)


def _classify(
    expected: Sequence[SignalCase],
    targets: Sequence[SignalTarget],
    prediction_rows: Sequence[dict[str, Any]],
    run_rows: Sequence[dict[str, Any]],
) -> tuple[tuple[SignalCase, ...], tuple[SignalCase, ...]]:
    by_identity = {target.identity: target for target in targets}
    predictions: dict[tuple[str, str, int, str, str, str], list[dict[str, Any]]] = {}
    for row in prediction_rows:
        target = by_identity.get(
            (str(row["scheme_id"]), str(row["target_tenor"]), int(row["horizon"]))
        )
        if target is None or not _version_matches(row, target):
            continue
        try:
            key = (
                target.base_scheme_id,
                target.target_tenor,
                target.horizon,
                _date(row["predict_date"]),
                _date(row["feature_date"]),
                _date(row["target_date"]),
            )
        except ValueError:
            continue
        predictions.setdefault(key, []).append(row)
    by_base: dict[str, list[SignalTarget]] = {}
    for target in targets:
        by_base.setdefault(target.base_scheme_id, []).append(target)
    runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in run_rows:
        possible = by_base.get(str(row["scheme_id"]), ())
        if not any(_version_matches(row, target) for target in possible):
            continue
        try:
            runs.setdefault((str(row["scheme_id"]), _date(row["predict_date"])), []).append(row)
        except ValueError:
            continue
    present: list[SignalCase] = []
    missing: list[SignalCase] = []
    for case in expected:
        rows = predictions.get(case.key, ())
        if len(rows) == 1:
            present.append(replace(case, prediction_phase=str(rows[0]["prediction_phase"])))
        else:
            category: FailureCategory = (
                "prediction_ambiguous"
                if len(rows) > 1
                else _failure_category(runs.get((case.base_scheme_id, case.predict_date), ()))
            )
            missing.append(replace(case, failure_category=category))
    return tuple(sorted(present, key=_case_sort_key)), tuple(sorted(missing, key=_case_sort_key))


def _failure_category(rows: Sequence[dict[str, Any]]) -> FailureCategory:
    statuses = {str(row.get("status") or "") for row in rows}
    if any(
        str(row.get("status") or "") == "failed"
        and str(row.get("error_message") or "") == "data_bridge_ready_timeout"
        for row in rows
    ):
        return "data_bridge_ready_timeout"
    if statuses & {"success", "partial"}:
        return "prediction_missing"
    if "failed" in statuses:
        return "run_failed"
    if "running" in statuses:
        return "run_pending"
    if "skipped" in statuses:
        return "run_skipped"
    return "no_run"


def _version_matches(row: dict[str, Any], target: SignalTarget) -> bool:
    return target.runtime_type != "blackbox_v2" or (
        str(row.get("scheme_version") or "")
        == str(target.scheme_version or "")
    )


class _SnapshotCalendar:
    """同一连接快照内使用 shared calendar/context helpers 的最小适配器。"""

    def __init__(self, connection: Connection, *, history_start_date: str) -> None:
        history_start = date.fromisoformat(history_start_date)
        calendar_start = date(history_start.year - 1, 1, 1).isoformat()
        snapshot = read_calendar_snapshot_from_connection(
            connection,
            rdate_from=calendar_start,
        )
        flags = {
            _date(row["rdate"]): str(row["trade_flag"]).strip()
            for row in snapshot["t_trade_calendar.csv"].to_dict("records")
        }
        self.calendar_set = frozenset(flags)
        self._period_rows = tuple(
            {"rdate": day, "trade_flag": flag}
            for day, flag in sorted(flags.items())
        )
        self.trading_days = tuple(
            sorted(
                day for day, flag in flags.items() if is_trading_day_row(day, flag)
            )
        )
        if not self.trading_days:
            raise SignalGapReportError("trade_calendar_unavailable")
        self.trading_set = frozenset(self.trading_days)
        self.week = build_week_calendar(
            {
                "rdate": _date(row["rdate"]),
                "week_id": row["week_id"],
                "trade_flag": flags.get(_date(row["rdate"]), "0"),
            }
            for row in snapshot["api_wind_date.csv"].to_dict("records")
        )

    def is_trading_day(self, value: str) -> bool:
        return _date(value) in self.trading_set

    def covers(self, value: str) -> bool:
        return _date(value) in self.calendar_set

    def previous_trading_day(self, value: str) -> str:
        index = bisect_left(self.trading_days, _date(value))
        if index == 0:
            raise ValueError("no previous trading day")
        return self.trading_days[index - 1]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        index = bisect_right(self.trading_days, _date(value))
        return list(self.trading_days[index : index + count])

    def nth_trading_day_after(self, value: str, n: int) -> str:
        days = self.next_trading_days(value, n)
        if len(days) != n:
            raise ValueError("not enough following trading days")
        return days[-1]

    def week_id_for_date(self, value: str) -> int | None:
        return self.week.week_id_for_date(value)

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self.week.week_id_to_last_trading_day(week_id)

    def period_calendar_rows(self) -> tuple[dict[str, object], ...]:
        return self._period_rows

    def period_predict_dates(
        self,
        task_type: str,
        start_date: str,
        end_date: str,
    ) -> tuple[str, ...]:
        return period_anchor_dates(
            task_type,
            self._period_rows,
            start_date=start_date,
            end_date=end_date,
        )

    def predict_dates(self, frequency: str, start_date: str, end_date: str) -> tuple[str, ...]:
        if frequency == "daily":
            values = self.trading_days
        elif frequency == "weekly":
            values = tuple(
                value
                for value in _weekly_live_dates(start_date, end_date)
                if is_weekly_signal_date(self, value)
            )
        elif frequency == "monthly":
            values = _monthly_dates(start_date, end_date)
        else:
            raise SignalGapReportError("unsupported_signal_cadence")
        return tuple(
            sorted(
                {
                    _date(value)
                    for value in values
                    if start_date <= _date(value) <= end_date
                }
            )
        )


def _monthly_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    current = date.fromisoformat(start_date).replace(day=15)
    if current.isoformat() < start_date:
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 15)
    result: list[str] = []
    while current.isoformat() <= end_date:
        result.append(current.isoformat())
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 15)
    return tuple(result)


def _weekly_live_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    """返回与 weekly launchd 入口一致的自然周六信号日。"""
    current = date.fromisoformat(start_date)
    current += timedelta(days=(5 - current.weekday()) % 7)
    result: list[str] = []
    while current.isoformat() <= end_date:
        result.append(current.isoformat())
        current += timedelta(days=7)
    return tuple(result)


def _case_sort_key(case: SignalCase) -> tuple[str, str, str]:
    return case.predict_date, case.registry_scheme_id, case.target_date


def _range(start_date: str, end_date: str) -> tuple[str, str]:
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise SignalGapReportError("invalid_date_range")
    return start, end


def _timestamp_date(value: object) -> str | None:
    try:
        return _date(value)
    except ValueError:
        return None


def _blackbox_approved_calendar_date(value: object) -> str | None:
    """将 version 生命周期中无时区 UTC 的 ``approved_at`` 映射为业务日。"""

    try:
        if isinstance(value, datetime):
            timestamp = value
        elif isinstance(value, date):
            return value.isoformat()
        else:
            timestamp = datetime.fromisoformat(str(value or "").strip())
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(_SHANGHAI).date().isoformat()
    except ValueError:
        return None


def _date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        raise ValueError("invalid date") from None
