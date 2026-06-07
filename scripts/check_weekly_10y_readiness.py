from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from scheduler.repository import create_engine_from_env


REQUIRED_WEEKLY_VALUE_COLUMNS = ("TB0YWI3C", "TB1YWI3C", "TB5YWI3C")


@dataclass(frozen=True)
class WeeklyPredictionContext:
    predict_date: str
    feature_week_id: int
    feature_date: str
    target_week_id: int
    target_date: str


@dataclass(frozen=True)
class Weekly10YReadinessResult:
    """周度 10Y live 前置只读检查结果。"""

    predict_date: str
    feature_week_id: int
    feature_date: str
    target_week_id: int
    target_date: str
    latest_complete_required_week_id: int | None
    latest_supported_predict_date: str
    ready: bool
    missing_required_values: list[dict[str, int | str]]

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_supported_predict_date(week_id: int | None) -> str:
    if week_id is None:
        return "unavailable"
    return (week_id_to_friday(int(week_id)) + timedelta(days=1)).strftime("%Y-%m-%d")


def _predict_date_after_feature_date(feature_date: str | None, fallback_week_id: int | None) -> str:
    if feature_date:
        return (_to_date(feature_date) + timedelta(days=1)).strftime("%Y-%m-%d")
    return _latest_supported_predict_date(fallback_week_id)


def _to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def first_monday_of_year(year: int) -> date:
    first_day = date(year, 1, 1)
    return first_day + timedelta(days=(7 - first_day.weekday()) % 7)


def date_to_week_id(value: str | date | datetime) -> int:
    current = _to_date(value)
    monday_of_week = current - timedelta(days=current.weekday())
    first_monday = first_monday_of_year(current.year)
    if monday_of_week < first_monday:
        return date_to_week_id(date(current.year - 1, 12, 31))
    week_number = ((monday_of_week - first_monday).days // 7) + 1
    return int(f"{current.year:04d}{week_number:02d}")


def week_id_to_monday(week_id: int | float | str) -> date:
    text = str(int(week_id))
    year = int(text[:4])
    week = int(text[4:])
    return first_monday_of_year(year) + timedelta(weeks=week - 1)


def week_id_to_friday(week_id: int | float | str) -> date:
    return week_id_to_monday(week_id) + timedelta(days=4)


def next_week_id(week_id: int | float | str) -> int:
    return date_to_week_id(week_id_to_monday(week_id) + timedelta(days=7))


def resolve_weekly_prediction_context(
    predict_date: str,
    source_feature_week_id: int | None = None,
) -> WeeklyPredictionContext:
    run_date = _to_date(predict_date)
    feature_week_id = int(source_feature_week_id) if source_feature_week_id is not None else date_to_week_id(run_date)
    target_week_id = next_week_id(feature_week_id)
    feature_date = run_date - timedelta(days=1) if source_feature_week_id is not None else week_id_to_friday(feature_week_id)
    target_date = feature_date + timedelta(days=7) if source_feature_week_id is not None else week_id_to_friday(target_week_id)
    return WeeklyPredictionContext(
        predict_date=run_date.strftime("%Y-%m-%d"),
        feature_week_id=feature_week_id,
        feature_date=feature_date.strftime("%Y-%m-%d"),
        target_week_id=target_week_id,
        target_date=target_date.strftime("%Y-%m-%d"),
    )


def _read_required_presence(engine: Engine) -> dict[int, set[str]]:
    stmt = text(
        """
        SELECT week_id, indicators_code
        FROM api_wind_derivative_weekly
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
        GROUP BY week_id, indicators_code
        ORDER BY week_id, indicators_code
        """
    ).bindparams(bindparam("codes", expanding=True))
    presence: dict[int, set[str]] = {}
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"codes": list(REQUIRED_WEEKLY_VALUE_COLUMNS)}).mappings().all()
    for row in rows:
        try:
            week_id = int(row["week_id"])
        except (TypeError, ValueError):
            continue
        code = str(row["indicators_code"])
        presence.setdefault(week_id, set()).add(code)
    return presence


def _read_source_week_id_for_date(engine: Engine, target_date: str, fallback_week_id: int) -> int:
    for table_name in ("api_wind_weekly", "api_wind_derivative_weekly", "api_wind_daily"):
        stmt = text(
            f"""
            SELECT week_id
            FROM {table_name}
            WHERE rdate = :target_date
              AND week_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        )
        try:
            with engine.connect() as conn:
                row = conn.execute(stmt, {"target_date": target_date}).mappings().first()
        except SQLAlchemyError:
            continue
        if row and row["week_id"] is not None:
            return int(row["week_id"])
    return fallback_week_id


def _read_source_feature_date_for_week_id(engine: Engine, week_id: int | None) -> str | None:
    if week_id is None:
        return None
    for table_name in ("api_wind_weekly", "api_wind_derivative_weekly", "api_wind_daily"):
        stmt = text(
            f"""
            SELECT MAX(rdate) AS feature_date
            FROM {table_name}
            WHERE week_id = :week_id
            """
        )
        try:
            with engine.connect() as conn:
                row = conn.execute(stmt, {"week_id": int(week_id)}).mappings().first()
        except SQLAlchemyError:
            continue
        if row and row["feature_date"] is not None:
            return str(row["feature_date"])
    return None


def assess_weekly_10y_readiness(engine: Engine, predict_date: str) -> Weekly10YReadinessResult:
    """只读确认周度 10Y 方案所需衍生周频特征是否已完整落库。"""
    initial_context = resolve_weekly_prediction_context(predict_date)
    source_feature_week_id = _read_source_week_id_for_date(
        engine,
        initial_context.feature_date,
        initial_context.feature_week_id,
    )
    context = resolve_weekly_prediction_context(predict_date, source_feature_week_id=source_feature_week_id)
    presence = _read_required_presence(engine)
    required = list(REQUIRED_WEEKLY_VALUE_COLUMNS)
    complete_weeks = [week for week, codes in presence.items() if all(code in codes for code in required)]
    latest_complete = max(complete_weeks) if complete_weeks else None
    latest_complete_feature_date = _read_source_feature_date_for_week_id(engine, latest_complete)
    feature_codes = presence.get(context.feature_week_id, set())
    missing = [
        {"week_id": context.feature_week_id, "indicators_code": code}
        for code in required
        if code not in feature_codes
    ]
    return Weekly10YReadinessResult(
        predict_date=context.predict_date,
        feature_week_id=context.feature_week_id,
        feature_date=context.feature_date,
        target_week_id=context.target_week_id,
        target_date=context.target_date,
        latest_complete_required_week_id=latest_complete,
        latest_supported_predict_date=_predict_date_after_feature_date(latest_complete_feature_date, latest_complete),
        ready=not missing,
        missing_required_values=missing,
    )


def format_readiness_result(result: Weekly10YReadinessResult) -> str:
    """输出运维可读的 key=value readiness 摘要。"""
    lines = [
        f"ready={'true' if result.ready else 'false'}",
        f"predict_date={result.predict_date}",
        f"feature_week_id={result.feature_week_id}",
        f"feature_date={result.feature_date}",
        f"target_week_id={result.target_week_id}",
        f"target_date={result.target_date}",
        f"latest_complete_required_week_id={result.latest_complete_required_week_id}",
        f"latest_supported_predict_date={result.latest_supported_predict_date}",
    ]
    if result.missing_required_values:
        lines.append("missing_required_values=" + json.dumps(result.missing_required_values, ensure_ascii=False))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only readiness check for weekly_10y_d_overlay live run.")
    parser.add_argument("--predict-date", required=True, help="Saturday predict date in YYYY-MM-DD format")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of key=value lines")
    args = parser.parse_args()

    engine = create_engine_from_env()
    try:
        result = assess_weekly_10y_readiness(engine, args.predict_date)
    finally:
        engine.dispose()
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_readiness_result(result))
    raise SystemExit(0 if result.ready else 2)


if __name__ == "__main__":
    main()
