from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from scheduler.discovery import SCHEMES_ROOT, discover_schemes
from scheduler.repository import create_engine_from_env, delete_actuals_after_source_watermark, upsert_actuals
from shared.models import ActualRecord
from shared.tenor_mapping import TENOR_TO_INDICATOR, indicator_map_for_tenors, normalize_tenor


logger = logging.getLogger(__name__)


def active_scheme_tenors(schemes_root=SCHEMES_ROOT, frequency: str | None = None) -> list[str]:
    """读取 active 方案配置中的期限列表。"""
    selected: set[str] = set()
    for cfg in discover_schemes(schemes_root):
        if cfg.status != "active":
            continue
        if frequency and cfg.frequency != frequency:
            continue
        selected.update(normalize_tenor(tenor) for tenor in cfg.tenors)
    return sorted(selected)


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
    selected_tenors = [normalize_tenor(tenor) for tenor in (tenors or TENOR_TO_INDICATOR.keys())]
    code_to_tenor = indicator_map_for_tenors(selected_tenors)
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


def read_source_watermarks(
    engine: Engine,
    tenors: Iterable[str] | None = None,
    end_date: str | date | datetime | None = None,
) -> dict[str, str]:
    """读取每个期限在源表中的最新可用日期。"""
    selected_tenors = [normalize_tenor(tenor) for tenor in (tenors or TENOR_TO_INDICATOR.keys())]
    code_to_tenor = indicator_map_for_tenors(selected_tenors)
    if not code_to_tenor:
        return {}

    params = {
        "codes": list(code_to_tenor.keys()),
        "end_date": _normalize_date(end_date),
    }
    end_filter = "AND rdate <= :end_date" if params["end_date"] else ""
    sql = text(
        f"""
        SELECT indicators_code, MAX(rdate) AS max_date
        FROM api_wind_daily
        WHERE indicators_code IN :codes
          AND indicators_value IS NOT NULL
          {end_filter}
        GROUP BY indicators_code
        """
    ).bindparams(bindparam("codes", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return {
        code_to_tenor[str(row["indicators_code"])]: str(row["max_date"])
        for row in rows
        if row["max_date"] is not None
    }


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
        selected_tenors = list(tenors) if tenors is not None else active_scheme_tenors(frequency="daily")
        records = build_actual_records(engine, start_date=start_date, end_date=end_date, tenors=selected_tenors)
        written = upsert_actuals(engine, records)
        source_watermarks = read_source_watermarks(engine, tenors=selected_tenors, end_date=end_date)
        pruned = delete_actuals_after_source_watermark(
            engine,
            source_watermarks,
            end_date=_normalize_date(end_date),
        )
        if pruned:
            logger.warning("Pruned stale daily actuals beyond source watermark: records=%s", pruned)
        return written
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh t_scheme_actuals from api_wind_daily.")
    parser.add_argument("--start-date", default=None, help="Optional start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", default=None, help="Optional end date in YYYY-MM-DD format")
    parser.add_argument("--tenor", action="append", help="Limit to one tenor")
    args = parser.parse_args()

    written = update_actuals(start_date=args.start_date, end_date=args.end_date, tenors=args.tenor)
    print(f"actuals_written={written}")


if __name__ == "__main__":
    main()
