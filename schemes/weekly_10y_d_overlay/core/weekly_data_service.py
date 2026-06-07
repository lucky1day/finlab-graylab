from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from shared.data_service import create_sqlalchemy_engine


SCHEMA_PATH = Path(__file__).with_name("weekly_output0529_columns.json")
WEEKLY_RAW_TABLE = "api_wind_weekly"
WEEKLY_DERIVATIVE_TABLE = "api_wind_derivative_weekly"
DAILY_RAW_TABLE = "api_wind_daily"
WEEKLY_FREQ_ALIASES = ("周", "weekly", "Weekly", "WEEKLY", "W", "w", "2")
ACTIVE_PRE_FORECAST_FILTERS = {
    "status": "1",
    "pre_forecast_flag": "1",
}
WEEKLY_CLOSE_DAILY_CODE_MAP = {
    "TB1YWI3C": "TB1YWI0C",
    "TB5YWI3C": "TB5YWI0C",
    "TB0YWI3C": "TB0YWI0C",
}


def load_weekly_schema(schema_path: str | Path = SCHEMA_PATH) -> list[str]:
    """读取 weekly_output0529.csv 的列顺序 schema。"""
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"weekly schema file not found: {path}")
    return [str(col) for col in json.loads(path.read_text(encoding="utf-8"))]


def select_weekly_factor_metadata(metadata: pd.DataFrame) -> pd.DataFrame:
    """按 wind_export(1).py 口径筛选周频盘前预测因子。"""
    required = {"indicators_code", "frequency", "status", "pre_forecast_flag"}
    missing = required - set(metadata.columns)
    if missing:
        raise ValueError(f"weekly metadata missing required columns: {sorted(missing)}")

    result = metadata.copy()
    result.columns = [str(col).strip() for col in result.columns]
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result = result[result["indicators_code"].ne("")].copy()

    frequency_values = result["frequency"].fillna("").astype(str).str.strip()
    status_values = result["status"].fillna("").astype(str).str.strip()
    pre_forecast_values = result["pre_forecast_flag"].fillna("").astype(str).str.strip()
    result = result[
        frequency_values.isin(WEEKLY_FREQ_ALIASES)
        & status_values.eq(ACTIVE_PRE_FORECAST_FILTERS["status"])
        & pre_forecast_values.eq(ACTIVE_PRE_FORECAST_FILTERS["pre_forecast_flag"])
    ].copy()

    if "lag_length" in result.columns:
        lags = pd.to_numeric(result["lag_length"], errors="coerce").fillna(0).astype(int)
    else:
        lags = pd.Series(0, index=result.index)
    result["lag_length_num"] = lags
    return result[["indicators_code", "lag_length_num"]].reset_index(drop=True)


def _parse_rdate(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed").dt.normalize()
    except ValueError:
        return pd.to_datetime(series, errors="coerce").dt.normalize()


def _prepare_long_frame(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=["week_id", "indicators_code", "indicators_value"])

    df = pd.concat(non_empty, ignore_index=True)
    required = {"week_id", "indicators_code", "indicators_value"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"weekly long data missing required columns: {sorted(missing)}")

    result = df[list(required)].copy()
    result["week_id"] = pd.to_numeric(result["week_id"], errors="coerce")
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result["indicators_value"] = pd.to_numeric(result["indicators_value"], errors="coerce")
    return result.dropna(subset=["week_id", "indicators_code"])


def _prepare_wind_export_weekly_long_frame(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=["rdate", "week_id", "indicators_code", "indicators_value"])

    df = pd.concat(non_empty, ignore_index=True)
    required = {"rdate", "week_id", "indicators_code", "indicators_value"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"weekly wind_export long data missing required columns: {sorted(missing)}")

    result = df[list(required)].copy()
    result["rdate"] = _parse_rdate(result["rdate"])
    result["week_id"] = result["week_id"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    result = result[result["week_id"].str.fullmatch(r"\d{6}", na=False)].copy()
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result["indicators_value"] = pd.to_numeric(result["indicators_value"], errors="coerce")
    return result.dropna(subset=["week_id", "indicators_code", "indicators_value"])


def build_weekly_output_from_frames(
    schema_columns: Sequence[str],
    raw_weekly: pd.DataFrame,
    derivative_weekly: pd.DataFrame | None = None,
    daily_weekly_close_fallback: pd.DataFrame | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
) -> pd.DataFrame:
    """把周频长表数据 pivot 成原始脚本要求的 weekly_output0529 宽表。"""
    schema = [str(col) for col in schema_columns]
    if not schema or schema[0] != "week_id":
        raise ValueError("weekly schema must start with week_id")

    value_columns = schema[1:]
    df = _prepare_long_frame([raw_weekly, derivative_weekly if derivative_weekly is not None else pd.DataFrame()])
    if daily_weekly_close_fallback is not None and not daily_weekly_close_fallback.empty:
        fallback = _prepare_long_frame([daily_weekly_close_fallback])
        if not fallback.empty:
            existing_keys = {
                (int(row.week_id), str(row.indicators_code))
                for row in df[["week_id", "indicators_code"]].itertuples(index=False)
            }
            fallback = fallback[
                [
                    (int(row.week_id), str(row.indicators_code)) not in existing_keys
                    for row in fallback[["week_id", "indicators_code"]].itertuples(index=False)
                ]
            ].copy()
            if not fallback.empty:
                df = pd.concat([df, fallback], ignore_index=True)
    if start_week is not None:
        df = df[df["week_id"] >= int(start_week)]
    if end_week is not None:
        df = df[df["week_id"] <= int(end_week)]

    df = df[df["indicators_code"].isin(value_columns)].copy()
    if df.empty:
        return pd.DataFrame(columns=schema)

    df["_source_order"] = range(len(df))
    df = df.sort_values(["week_id", "indicators_code", "_source_order"])
    df = df.drop_duplicates(["week_id", "indicators_code"], keep="last")
    wide = df.pivot(index="week_id", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(columns=value_columns)
    wide = wide.sort_index()
    wide.index = wide.index.astype(int)
    return wide.reset_index()[schema]


def build_wind_export_weekly_output_from_frames(
    metadata: pd.DataFrame,
    raw_weekly: pd.DataFrame,
    derivative_weekly: pd.DataFrame | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """按 wind_export(1).py 周频导出口径生成算法输入宽表。"""
    selected = select_weekly_factor_metadata(metadata)
    output_columns = selected["indicators_code"].astype(str).tolist()
    lag_map = dict(zip(selected["indicators_code"], selected["lag_length_num"]))
    df = _prepare_wind_export_weekly_long_frame(
        [raw_weekly, derivative_weekly if derivative_weekly is not None else pd.DataFrame()]
    )

    if start_date is not None:
        df = df[df["rdate"] >= pd.Timestamp(start_date)].copy()
    if end_date is not None:
        df = df[df["rdate"] <= pd.Timestamp(end_date)].copy()
    if start_week is not None:
        df = df[df["week_id"] >= f"{int(start_week):06d}"].copy()
    if end_week is not None:
        df = df[df["week_id"] <= f"{int(end_week):06d}"].copy()

    df = df[df["indicators_code"].isin(output_columns)].copy()
    if df.empty:
        return pd.DataFrame(columns=["week_id"] + output_columns)

    df = df.sort_values(["indicators_code", "week_id", "rdate"])
    df = df.drop_duplicates(["week_id", "indicators_code"], keep="last")

    all_weeks = sorted(df["week_id"].dropna().unique())
    wide = df.pivot(index="week_id", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(all_weeks)

    parts = []
    for code in output_columns:
        series = wide[code] if code in wide.columns else pd.Series(dtype=float, index=all_weeks)
        lag = int(lag_map.get(code, 0) or 0)
        parts.append((series.shift(lag) if lag else series).rename(code))

    result = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=all_weeks)
    result = result[result.index.notna()]
    result = result[~result.index.astype(str).isin(["None", "nan", ""])]
    result = result[result.index.astype(str) >= "201001"]
    result.index.name = "week_id"
    output = result.reset_index()
    output["week_id"] = pd.to_numeric(output["week_id"], errors="coerce").astype("Int64")
    output = output.dropna(subset=["week_id"]).copy()
    output["week_id"] = output["week_id"].astype(int)
    return output[["week_id"] + output_columns]


def build_daily_weekly_close_fallback_from_frame(
    daily_frame: pd.DataFrame,
    weekly_code_map: dict[str, str] | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """按周频算法定义，从日频收益率现场生成 weekly_close 长表。"""
    mapping = weekly_code_map or WEEKLY_CLOSE_DAILY_CODE_MAP
    daily_to_weekly = {daily_code: weekly_code for weekly_code, daily_code in mapping.items()}
    if daily_frame is None or daily_frame.empty:
        return pd.DataFrame(columns=["week_id", "indicators_code", "indicators_value"])
    required = {"rdate", "week_id", "indicators_code", "indicators_value"}
    missing = required - set(daily_frame.columns)
    if missing:
        raise ValueError(f"daily close data missing required columns: {sorted(missing)}")

    df = daily_frame[list(required)].copy()
    df["rdate"] = pd.to_datetime(df["rdate"], errors="coerce")
    if end_date is not None:
        df = df[df["rdate"] <= pd.Timestamp(end_date)]
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce")
    df["indicators_code"] = df["indicators_code"].astype(str).str.strip()
    df["indicators_value"] = pd.to_numeric(df["indicators_value"], errors="coerce")
    df = df.dropna(subset=["rdate", "week_id", "indicators_code", "indicators_value"]).copy()
    df = df[df["indicators_code"].isin(daily_to_weekly)].copy()
    if df.empty:
        return pd.DataFrame(columns=["week_id", "indicators_code", "indicators_value"])

    df["_source_order"] = range(len(df))
    df = df.sort_values(["week_id", "indicators_code", "rdate", "_source_order"])
    df = df.drop_duplicates(["week_id", "indicators_code"], keep="last")
    df["indicators_code"] = df["indicators_code"].map(daily_to_weekly)
    df["week_id"] = df["week_id"].astype(int)
    return df[["week_id", "indicators_code", "indicators_value"]].reset_index(drop=True)


def read_weekly_long_from_db(codes: Iterable[str], table_name: str, engine=None) -> pd.DataFrame:
    """只读读取周频原始/衍生因子长表。"""
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    selected_codes = [str(code).strip() for code in codes if str(code).strip()]
    if not selected_codes:
        return pd.DataFrame(columns=["rdate", "week_id", "indicators_code", "indicators_value"])

    placeholders = ", ".join(["%s"] * len(selected_codes))
    sql = (
        "SELECT rdate, week_id, indicators_code, indicators_value "
        f"FROM {table_name} WHERE indicators_code IN ({placeholders}) "
        "AND indicators_value IS NOT NULL"
    )
    try:
        return pd.read_sql(sql, engine, params=tuple(selected_codes))
    finally:
        if own_engine:
            engine.dispose()


def _metadata_order_clause(engine) -> str:
    try:
        columns = pd.read_sql("SHOW COLUMNS FROM api_wind_indicators_all", engine)
    except Exception:
        return ""
    fields = set(columns["Field"].astype(str))
    return " ORDER BY id" if "id" in fields else ""


def read_factor_metadata_from_db(engine=None) -> pd.DataFrame:
    """只读读取因子元数据，用于 wind_export 周频宽表。"""
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        sql = (
            "SELECT indicators_code, frequency, lag_length, status, pre_forecast_flag "
            "FROM api_wind_indicators_all"
            f"{_metadata_order_clause(engine)}"
        )
        return pd.read_sql(sql, engine)
    finally:
        if own_engine:
            engine.dispose()


def read_source_week_id_for_date(target_date: str, engine=None) -> int | None:
    """按源表实际 rdate 反查 week_id，避免和周编号公式发生偏移。"""
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        for table_name in (WEEKLY_RAW_TABLE, WEEKLY_DERIVATIVE_TABLE, DAILY_RAW_TABLE):
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
                    row = conn.execute(stmt, {"target_date": str(target_date)}).mappings().first()
            except SQLAlchemyError:
                continue
            if row and row["week_id"] is not None:
                return int(row["week_id"])
        return None
    finally:
        if own_engine:
            engine.dispose()


def read_daily_weekly_close_fallback_from_db(
    weekly_code_map: dict[str, str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    end_date: str | None = None,
    engine=None,
) -> pd.DataFrame:
    """只读日频表，并按 weekly close 定义生成缺失周频输入。"""
    mapping = weekly_code_map or WEEKLY_CLOSE_DAILY_CODE_MAP
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    daily_codes = [str(code).strip() for code in mapping.values() if str(code).strip()]
    if not daily_codes:
        return pd.DataFrame(columns=["week_id", "indicators_code", "indicators_value"])

    placeholders = ", ".join(["%s"] * len(daily_codes))
    filters = [f"indicators_code IN ({placeholders})", "indicators_value IS NOT NULL"]
    params: list[object] = list(daily_codes)
    if start_week is not None:
        filters.append("week_id >= %s")
        params.append(int(start_week))
    if end_week is not None:
        filters.append("week_id <= %s")
        params.append(int(end_week))
    if end_date is not None:
        filters.append("rdate <= %s")
        params.append(str(end_date))
    sql = (
        "SELECT rdate, week_id, indicators_code, indicators_value "
        f"FROM {DAILY_RAW_TABLE} WHERE {' AND '.join(filters)} "
        "ORDER BY week_id, indicators_code, rdate"
    )
    try:
        daily = pd.read_sql(sql, engine, params=tuple(params))
        return build_daily_weekly_close_fallback_from_frame(daily, mapping, end_date=end_date)
    finally:
        if own_engine:
            engine.dispose()


def build_weekly_output_from_db(
    schema_columns: Sequence[str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    end_date: str | None = None,
    include_daily_weekly_close_fallback: bool = False,
    engine=None,
) -> pd.DataFrame:
    """从 DB 只读生成 weekly_output 宽表。"""
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        if schema_columns is None:
            metadata = read_factor_metadata_from_db(engine)
            selected = select_weekly_factor_metadata(metadata)
            codes = selected["indicators_code"].astype(str).tolist()
            raw = read_weekly_long_from_db(codes, WEEKLY_RAW_TABLE, engine)
            derivative = read_weekly_long_from_db(codes, WEEKLY_DERIVATIVE_TABLE, engine)
            return build_wind_export_weekly_output_from_frames(
                metadata,
                raw,
                derivative,
                start_week=start_week,
                end_week=end_week,
                end_date=end_date,
            )

        schema = list(schema_columns)
        codes = schema[1:]
        raw = read_weekly_long_from_db(codes, WEEKLY_RAW_TABLE, engine)
        derivative = read_weekly_long_from_db(codes, WEEKLY_DERIVATIVE_TABLE, engine)
        daily_fallback = (
            read_daily_weekly_close_fallback_from_db(
                start_week=start_week,
                end_week=end_week,
                end_date=end_date,
                engine=engine,
            )
            if include_daily_weekly_close_fallback
            else None
        )
        return build_weekly_output_from_frames(
            schema,
            raw,
            derivative,
            daily_weekly_close_fallback=daily_fallback,
            start_week=start_week,
            end_week=end_week,
        )
    finally:
        if own_engine:
            engine.dispose()
