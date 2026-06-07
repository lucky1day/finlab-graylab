from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from scheduler.repository import create_engine_from_env, upsert_actuals
from shared.models import ActualRecord


TENOR_TO_INDICATOR = {
    "1Y": "TB1YWI0C",
    "3Y": "TB3YWI0C",
    "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C",
    "10Y": "TB0YWI0C",
}


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _to_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def read_yield_rows(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> list[dict]:
    """读取实际收益率序列。"""
    selected_tenors = list(tenors or TENOR_TO_INDICATOR.keys())
    code_to_tenor = {TENOR_TO_INDICATOR[tenor]: tenor for tenor in selected_tenors if tenor in TENOR_TO_INDICATOR}
    if not code_to_tenor:
        return []

    params = {
        "codes": list(code_to_tenor.keys()),
        "end_date": _normalize_date(end_date),
    }
    end_filter = "AND rdate <= :end_date" if params["end_date"] else ""
    sql = text(
        f"""
        SELECT rdate, indicators_code, indicators_value
        FROM api_wind_daily
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
          {end_filter}
        ORDER BY indicators_code, rdate
        """
    ).bindparams(bindparam("codes", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [
        {
            "trade_date": str(row["rdate"]),
            "tenor": code_to_tenor[str(row["indicators_code"])],
            "close_yield": _to_float(row["indicators_value"]),
        }
        for row in rows
    ]


def build_actual_records(
    engine: Engine,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> list[ActualRecord]:
    """构建实际方向记录，方向为目标日相对前 1/5 个交易日的收益率变化。"""
    start = _normalize_date(start_date)
    rows = read_yield_rows(engine, tenors=tenors, end_date=end_date)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["tenor"]].append(row)

    records: list[ActualRecord] = []
    for tenor, items in grouped.items():
        for idx, item in enumerate(items):
            trade_date = item["trade_date"]
            if start and trade_date < start:
                continue
            close_yield = item["close_yield"]
            prev_1 = items[idx - 1]["close_yield"] if idx >= 1 else None
            prev_5 = items[idx - 5]["close_yield"] if idx >= 5 else None
            records.append(
                ActualRecord(
                    tenor=tenor,
                    trade_date=trade_date,
                    close_yield=close_yield,
                    direction_1d=_sign(close_yield - prev_1) if prev_1 is not None else None,
                    direction_5d=_sign(close_yield - prev_5) if prev_5 is not None else None,
                )
            )
    return records


def update_actuals(
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    tenors: Iterable[str] | None = None,
) -> int:
    """从行情表刷新 t_scheme_actuals。"""
    engine = create_engine_from_env()
    try:
        records = build_actual_records(engine, start_date=start_date, end_date=end_date, tenors=tenors)
        return upsert_actuals(engine, records)
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh t_scheme_actuals from api_wind_daily.")
    parser.add_argument("--start-date", default=None, help="Optional start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", default=None, help="Optional end date in YYYY-MM-DD format")
    parser.add_argument("--tenor", action="append", choices=sorted(TENOR_TO_INDICATOR), help="Limit to one tenor")
    args = parser.parse_args()

    written = update_actuals(start_date=args.start_date, end_date=args.end_date, tenors=args.tenor)
    print(f"actuals_written={written}")


if __name__ == "__main__":
    main()
