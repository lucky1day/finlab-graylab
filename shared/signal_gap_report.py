"""只读计算 active 方案的 live 信号缺口。

Blackbox 从当前精确 active version 的 ``approved_at`` 日历日之后开始，
Native 从 Registry ``created_at`` 日历日之后开始。同日没有可审计的调度
时钟，故不计为已到期；本模块从不读取 ``deployed_at``、DataBridge 或文件。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any, Literal, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from shared.actual_facts import build_week_calendar
from shared.calendar_service import read_calendar_snapshot_from_connection
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)


ACCEPTED_LIVE_PHASES = ("gray_live", "scheduled_live")
FailureCategory = Literal[
    "activation_unavailable",
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
    base_scheme_id: str
    target_tenor: str
    horizon: int
    state: Literal["missing", "not_due", "present", "unavailable"]
    predict_date: str | None
    open_missing_count: int
    latest_missing_predict_date: str | None
    failure_category: FailureCategory | None


@dataclass(frozen=True, slots=True)
class SignalGapReport:
    start_date: str
    end_date: str
    targets: tuple[SignalTarget, ...]
    expected: tuple[SignalCase, ...]
    present: tuple[SignalCase, ...]
    missing: tuple[SignalCase, ...]


def load_signal_gap_report(
    engine: Engine,
    *,
    start_date: str,
    end_date: str,
) -> SignalGapReport:
    """用单个 Repeatable Read、read-only 一致性快照读取报告。"""

    start, end = _range(start_date, end_date)
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            return read_signal_gap_report(connection, start_date=start, end_date=end)
        finally:
            connection.rollback()


def read_signal_gap_report(
    connection: Connection,
    *,
    start_date: str,
    end_date: str,
) -> SignalGapReport:
    """仅使用 caller 的连接；caller 可将其放入已有只读快照。"""

    start, end = _range(start_date, end_date)
    targets = _targets(connection)
    calendar = _SnapshotCalendar(connection)
    expected = _expected(targets, calendar, start, end)
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
            "scheme_id, scheme_version, predict_date, prediction_phase, status",
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
    """每个 active Registry 一行，保留范围内所有未修复缺口计数。"""

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
        if unresolved:
            latest = due[-1]
            latest_missing = unresolved[-1]
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    target.base_scheme_id,
                    target.target_tenor,
                    target.horizon,
                    "missing",
                    latest.predict_date,
                    len(unresolved),
                    latest_missing.predict_date,
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
                    target.base_scheme_id,
                    target.target_tenor,
                    target.horizon,
                    state,
                    latest.predict_date,
                    0,
                    None,
                    None,
                )
            )
        elif target.failure_category:
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    target.base_scheme_id,
                    target.target_tenor,
                    target.horizon,
                    "unavailable",
                    None,
                    0,
                    None,
                    target.failure_category,
                )
            )
        else:
            result.append(
                LatestDueSignalStatus(
                    target.registry_scheme_id,
                    target.base_scheme_id,
                    target.target_tenor,
                    target.horizon,
                    "not_due",
                    None,
                    0,
                    None,
                    None,
                )
            )
    return tuple(sorted(result, key=lambda item: item.registry_scheme_id))


def serialize_signal_gap_report(report: SignalGapReport) -> dict[str, object]:
    """输出稳定、无原始异常文本的 JSON 结构。"""

    return {
        "schema_version": "signal-gap-report-v1",
        "availability_semantics": "strictly_after_onboarding_calendar_date",
        "start_date": report.start_date,
        "end_date": report.end_date,
        "summary": {
            "expected": len(report.expected),
            "present": len(report.present),
            "missing": len(report.missing),
        },
        "expected": [asdict(item) for item in report.expected],
        "present": [asdict(item) for item in report.present],
        "missing": [asdict(item) for item in report.missing],
        "latest_due": [asdict(item) for item in latest_due_signal_statuses(report)],
    }


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
                (str(item["scheme_version"]), _timestamp_date(item["approved_at"]))
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
            "phases": ACCEPTED_LIVE_PHASES,
        },
    ).mappings().all()
    return tuple(dict(item) for item in rows)


def _expected(
    targets: Sequence[SignalTarget],
    calendar: "_SnapshotCalendar",
    start_date: str,
    end_date: str,
) -> tuple[SignalCase, ...]:
    result: list[SignalCase] = []
    for target in targets:
        if target.available_after is None or target.failure_category:
            continue
        for predict_date in calendar.predict_dates(target.frequency, start_date, end_date):
            if predict_date <= target.available_after:
                continue
            try:
                if target.frequency == "daily":
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
                raise SignalGapReportError("calendar_context_unavailable") from None
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
    return tuple(sorted(result, key=_case_sort_key))


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

    def __init__(self, connection: Connection) -> None:
        snapshot = read_calendar_snapshot_from_connection(connection)
        flags = {
            _date(row["rdate"]): str(row["trade_flag"]).strip()
            for row in snapshot["t_trade_calendar.csv"].to_dict("records")
        }
        self.trading_days = tuple(sorted(day for day, flag in flags.items() if flag == "1"))
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

    def previous_trading_day(self, value: str) -> str:
        candidates = [day for day in self.trading_days if day < _date(value)]
        if not candidates:
            raise ValueError("no previous trading day")
        return candidates[-1]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        return [day for day in self.trading_days if day > _date(value)][:count]

    def nth_trading_day_after(self, value: str, n: int) -> str:
        days = self.next_trading_days(value, n)
        if len(days) != n:
            raise ValueError("not enough following trading days")
        return days[-1]

    def week_id_for_date(self, value: str) -> int | None:
        return self.week.week_id_for_date(value)

    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return self.week.week_id_to_last_trading_day(week_id)

    def predict_dates(self, frequency: str, start_date: str, end_date: str) -> tuple[str, ...]:
        if frequency == "daily":
            values = self.trading_days
        elif frequency == "weekly":
            values = self.week.week_predict_date.values()
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


def _date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        raise ValueError("invalid date") from None
