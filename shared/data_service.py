from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd


FORECAST_ROOT = Path(__file__).resolve().parent
DAILY_OUTPUT_PATH = FORECAST_ROOT / "daily_project" / "data" / "daily" / "daily_output.csv"
WEEKLY_OUTPUT_PATH = FORECAST_ROOT / "weekly_project" / "data" / "weekly" / "weekly_output0606.csv"
MONTHLY_OUTPUT_PATH = FORECAST_ROOT / "monthly_project" / "data" / "monthly" / "monthly_output.csv"

DAILY_TARGETS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")
FREQUENCY_ALIASES = {
    "daily": {"日", "daily", "Daily", "DAILY", "D", "d", "1"},
    "weekly": {"周", "weekly", "Weekly", "WEEKLY", "W", "w", "2"},
    "monthly": {"月", "monthly", "Monthly", "MONTHLY", "M", "m", "3"},
}
DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES = (
    "M0041340",
    "M0041341",
    "M0041342",
)
DATA_BRIDGE_V1_RAW_INDICATORS_SOURCE = "raw"


@dataclass(frozen=True)
class DatabaseConfig:
    user: str
    password: str
    host: str
    port: int = 3306
    database: str = "bond_db"
    charset: str = "utf8mb4"


def normalize_frequency(frequency: str) -> str:
    value = str(frequency).strip()
    for normalized, aliases in FREQUENCY_ALIASES.items():
        if value in aliases:
            return normalized
    return value.lower()


def _frequency_mask(series: pd.Series, frequency: str) -> pd.Series:
    normalized = normalize_frequency(frequency)
    aliases = FREQUENCY_ALIASES.get(normalized, {frequency})
    values = series.fillna("").astype(str).str.strip()
    return values.isin(aliases)


def _truthy_predict_model(value: object, key: str) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, dict):
        return int(value.get(key, 0) or 0) == 1
    text = str(value).strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return int(parsed.get(key, 0) or 0) == 1


def _truthy_column_mask(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().isin({"1", "1.0", "True", "true"})


def select_factor_metadata(
    metadata: pd.DataFrame,
    frequency: str = "daily",
    tenor_filter: Optional[set[str]] = None,
    predict_model_only: bool = False,
    status_only: bool = True,
    pre_forecast_only: bool = True,
) -> pd.DataFrame:
    """Select active factors used to build model input/output frames."""
    if "indicators_code" not in metadata.columns:
        raise ValueError("metadata must contain indicators_code")
    if "frequency" not in metadata.columns:
        raise ValueError("metadata must contain frequency")

    result = metadata.copy()
    result.columns = [str(col).strip() for col in result.columns]
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result = result[result["indicators_code"].ne("")]
    result = result[_frequency_mask(result["frequency"], frequency)]

    normalized = normalize_frequency(frequency)
    if predict_model_only and "predict_model" in result.columns:
        result = result[result["predict_model"].apply(lambda value: _truthy_predict_model(value, normalized))]

    if status_only and "status" in result.columns:
        result = result[_truthy_column_mask(result["status"])]

    if pre_forecast_only and "pre_forecast_flag" in result.columns:
        result = result[_truthy_column_mask(result["pre_forecast_flag"])]

    if tenor_filter and "bond_tenor" in result.columns:
        allowed = {str(item).strip() for item in tenor_filter}
        tenor_values = result["bond_tenor"].fillna("").astype(str).str.strip()
        result = result[tenor_values.eq("") | tenor_values.isin(allowed)]

    return result.reset_index(drop=True)


def _is_raw_metadata_source(value: object) -> bool:
    return (
        str(value).strip().lower()
        == DATA_BRIDGE_V1_RAW_INDICATORS_SOURCE
    )


def select_monthly_factor_metadata(
    metadata: pd.DataFrame,
    *,
    include_databridge_additions: bool = False,
) -> pd.DataFrame:
    """选择月频元数据；DataBridge 可显式补入批准的原始因子。"""
    selected = select_factor_metadata(metadata, "monthly")
    if not include_databridge_additions:
        return selected

    eligible = select_factor_metadata(
        metadata,
        "monthly",
        pre_forecast_only=False,
    )
    if "status" not in eligible.columns:
        raise ValueError(
            "monthly DataBridge additive metadata missing status for "
            + ", ".join(DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES)
        )
    if "indicators_source" not in eligible.columns:
        raise ValueError("monthly DataBridge additive metadata missing indicators_source")

    additions: list[pd.DataFrame] = []
    selected_codes = selected["indicators_code"].astype(str).str.strip()
    eligible_codes = eligible["indicators_code"].astype(str).str.strip()
    for code in DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES:
        candidates = eligible.loc[
            eligible_codes.eq(code)
            & eligible["indicators_source"].apply(_is_raw_metadata_source)
        ]
        selected_matches = selected.loc[selected_codes.eq(code)]
        if len(candidates) != 1:
            raise ValueError(
                "monthly DataBridge additive metadata must contain exactly one "
                f"active raw monthly row for {code}"
            )
        if selected_matches.empty:
            additions.append(candidates)
            continue
        if (
            len(selected_matches) != 1
            or not selected_matches["indicators_source"].apply(
                _is_raw_metadata_source
            ).all()
        ):
            raise ValueError(
                "monthly DataBridge additive metadata must select exactly one "
                f"raw monthly row for {code}"
            )

    if not additions:
        return selected
    return pd.concat([selected, *additions], ignore_index=True)


def _metadata_output_columns_and_lags(metadata: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    selected = metadata.copy()
    selected["indicators_code"] = selected["indicators_code"].astype(str).str.strip()
    output_columns = selected["indicators_code"].tolist()
    if "lag_length" in selected.columns:
        lags = pd.to_numeric(selected["lag_length"], errors="coerce").fillna(0).astype(int)
    else:
        lags = pd.Series(0, index=selected.index)
    return output_columns, dict(zip(output_columns, lags))


def _metadata_source_map(metadata: pd.DataFrame) -> dict[str, str]:
    if "indicators_source" not in metadata.columns:
        return {}
    result: dict[str, str] = {}
    for _, row in metadata.iterrows():
        code = str(row.get("indicators_code", "") or "").strip()
        source = str(row.get("indicators_source", "") or "").strip().lower()
        if code:
            result[code] = "derivative" if "derivative" in source or "衍生" in source else "raw"
    return result


def _prepare_long_frame(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if frame is not None and not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=["rdate", "indicators_code", "indicators_value"])
    df = pd.concat(non_empty, ignore_index=True)
    required = {"rdate", "indicators_code", "indicators_value"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"long data missing required columns: {sorted(missing)}")
    df = df[list(required)].copy()
    df["rdate"] = pd.to_datetime(df["rdate"], errors="coerce").dt.normalize()
    df["indicators_code"] = df["indicators_code"].astype(str).str.strip()
    df["indicators_value"] = pd.to_numeric(df["indicators_value"], errors="coerce")
    return df.dropna(subset=["rdate", "indicators_code", "indicators_value"])


def build_daily_output_from_frames(
    metadata: pd.DataFrame,
    raw_daily: pd.DataFrame,
    derivative_daily: Optional[pd.DataFrame] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tenor_filter: Optional[set[str]] = None,
) -> pd.DataFrame:
    selected = select_factor_metadata(metadata, "daily", tenor_filter=tenor_filter)
    output_columns, lag_map = _metadata_output_columns_and_lags(selected)
    df = _prepare_long_frame([raw_daily, derivative_daily if derivative_daily is not None else pd.DataFrame()])

    if start_date:
        df = df[df["rdate"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["rdate"] <= pd.to_datetime(end_date)]

    y_df = df[df["indicators_code"].isin(DAILY_TARGETS)]
    trading = pd.Index(sorted(y_df["rdate"].dropna().unique()), name="date")
    if len(trading) == 0:
        return pd.DataFrame(columns=["date"] + output_columns)

    work = df[df["indicators_code"].isin(output_columns)].copy()
    work = work[work["rdate"] >= trading.min()].copy()
    if work.empty:
        empty = pd.DataFrame(index=trading, columns=output_columns, dtype=float)
        return empty.reset_index()

    t_vals = trading.to_numpy(dtype="datetime64[ns]")
    s_dates = work["rdate"].to_numpy(dtype="datetime64[ns]")
    pos = np.searchsorted(t_vals, s_dates, side="left")
    valid = pos < len(t_vals)
    work = work.iloc[valid].copy()
    work["target_date"] = t_vals[pos[valid]]
    work = work.sort_values(["indicators_code", "target_date", "rdate"])
    work = work.drop_duplicates(["target_date", "indicators_code"], keep="last")

    wide = work.pivot(index="target_date", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(trading)

    parts = []
    for code in output_columns:
        series = wide[code] if code in wide.columns else pd.Series(dtype=float, index=trading)
        lag = int(lag_map.get(code, 0) or 0)
        parts.append((series.shift(lag) if lag else series).rename(code))
    result = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=trading)
    return result.reset_index()


def _load_root_db_config() -> DatabaseConfig:
    try:
        from db_config import DB_CONFIG  # type: ignore
    except Exception:
        from shared.db_config import DatabaseConfig as EnvDatabaseConfig

        env_config = EnvDatabaseConfig.from_env()
        return DatabaseConfig(
            user=env_config.user,
            password=env_config.password,
            host=env_config.host,
            port=env_config.port,
            database=env_config.database,
            charset=env_config.charset,
        )
    data = dict(DB_CONFIG)
    missing = [key for key in ("user", "password", "host", "database") if not str(data.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"database config missing required keys: {missing}")
    return DatabaseConfig(
        user=str(data.get("user")),
        password=str(data.get("password")),
        host=str(data.get("host")),
        port=int(data.get("port", 3306)),
        database=str(data.get("database")),
        charset=str(data.get("charset", "utf8mb4")),
    )


def create_sqlalchemy_engine(db_config: Optional[DatabaseConfig] = None):
    if os.getenv("BOND_NATIVE_INPUT_MODE") == "native_generation_v1":
        raise RuntimeError(
            "frozen Native generation mode forbids live database access"
        )
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL

    cfg = db_config or _load_root_db_config()
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        query={"charset": cfg.charset},
    )
    return create_engine(url)


def read_factor_metadata_from_db(engine=None) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        return pd.read_sql("SELECT * FROM api_wind_indicators_all", engine)
    finally:
        if own_engine:
            engine.dispose()


def _read_long_from_db(
    indicator_codes: Iterable[str],
    table_name: str,
    columns: Sequence[str],
    engine=None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    codes = [str(code).strip() for code in indicator_codes if str(code).strip()]
    if not codes:
        return pd.DataFrame(columns=list(columns))
    placeholders = ", ".join(["%s"] * len(codes))
    select_cols = ", ".join(columns)
    sql = (
        f"SELECT {select_cols} "
        f"FROM {table_name} WHERE indicators_code IN ({placeholders}) "
        "AND indicators_value IS NOT NULL"
    )
    params: tuple[object, ...] = tuple(codes)
    if end_date is not None:
        sql += " AND rdate <= %s"
        params = (*params, str(end_date))
    try:
        return pd.read_sql(sql, engine, params=params)
    finally:
        if own_engine:
            engine.dispose()


def read_daily_long_from_db(
    indicator_codes: Iterable[str],
    table_name: str,
    engine=None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    return _read_long_from_db(
        indicator_codes,
        table_name,
        ["rdate", "indicators_code", "indicators_value"],
        engine,
        end_date=end_date,
    )


def build_daily_output_from_db(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tenor_filter: Optional[set[str]] = None,
    engine=None,
) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        metadata = read_factor_metadata_from_db(engine)
        selected = select_factor_metadata(metadata, "daily", tenor_filter=tenor_filter)
        codes = selected["indicators_code"].astype(str).str.strip().tolist()
        raw = read_daily_long_from_db(
            codes,
            "api_wind_daily",
            engine,
            end_date=end_date,
        )
        derivative = read_daily_long_from_db(
            codes,
            "api_wind_derivative_daily",
            engine,
            end_date=end_date,
        )
        return build_daily_output_from_frames(selected, raw, derivative, start_date=start_date, end_date=end_date)
    finally:
        if own_engine:
            engine.dispose()


def _with_source_order(frames: Sequence[pd.DataFrame]) -> list[pd.DataFrame]:
    prepared: list[pd.DataFrame] = []
    for source_priority, frame in enumerate(frames):
        if frame is None or frame.empty:
            continue
        item = frame.copy()
        item["_source_priority"] = source_priority
        item["_source_order"] = range(len(item))
        prepared.append(item)
    return prepared


def _prepare_weekly_long_frame(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    prepared = _with_source_order(frames)
    if not prepared:
        return pd.DataFrame(
            columns=["rdate", "week_id", "indicators_code", "indicators_value", "_source_priority", "_source_order"]
        )
    df = pd.concat(prepared, ignore_index=True)
    required = {"week_id", "indicators_code", "indicators_value", "_source_priority", "_source_order"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"weekly long data missing required columns: {sorted(missing)}")
    if "rdate" not in df.columns:
        df["rdate"] = pd.NaT
    result = df[["rdate", "week_id", "indicators_code", "indicators_value", "_source_priority", "_source_order"]].copy()
    result["rdate"] = pd.to_datetime(result["rdate"], errors="coerce", format="mixed").dt.normalize()
    result["week_id"] = result["week_id"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    result = result[result["week_id"].str.fullmatch(r"\d{6}", na=False)].copy()
    result["week_id"] = result["week_id"].astype(int)
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result["indicators_value"] = pd.to_numeric(result["indicators_value"], errors="coerce")
    return result.dropna(subset=["week_id", "indicators_code", "indicators_value"])


def build_weekly_output_from_frames(
    schema_columns: Sequence[str],
    raw_weekly: pd.DataFrame,
    derivative_weekly: Optional[pd.DataFrame] = None,
    start_week: Optional[int] = None,
    end_week: Optional[int] = None,
    as_of_date: Optional[str] = None,
    lag_map: Optional[dict[str, int]] = None,
) -> pd.DataFrame:
    schema = [str(col).strip() for col in schema_columns]
    if not schema or schema[0] != "week_id":
        raise ValueError("weekly schema must start with week_id")
    value_columns = schema[1:]
    df = _prepare_weekly_long_frame([raw_weekly, derivative_weekly if derivative_weekly is not None else pd.DataFrame()])
    if as_of_date is not None:
        cutoff = pd.to_datetime(as_of_date).normalize()
        df = df[df["rdate"].notna() & df["rdate"].le(cutoff)]
    if start_week is not None:
        df = df[df["week_id"] >= int(start_week)]
    if end_week is not None:
        df = df[df["week_id"] <= int(end_week)]
    df = df[df["indicators_code"].isin(value_columns)].copy()
    if df.empty:
        return pd.DataFrame(columns=schema)
    df = df.sort_values(["week_id", "indicators_code", "_source_priority", "rdate", "_source_order"])
    df = df.drop_duplicates(["week_id", "indicators_code"], keep="last")
    wide = df.pivot(index="week_id", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(columns=value_columns)
    wide = wide.sort_index()
    wide.index = wide.index.astype(int)

    if lag_map:
        parts = []
        for code in value_columns:
            series = wide[code] if code in wide.columns else pd.Series(dtype=float, index=wide.index)
            lag = int(lag_map.get(code, 0) or 0)
            parts.append((series.shift(lag) if lag else series).rename(code))
        wide = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=wide.index)
    return wide.reset_index()[schema]


def build_weekly_cutoff_index_from_frames(
    schema_columns: Sequence[str],
    raw_weekly: pd.DataFrame,
    derivative_weekly: Optional[pd.DataFrame] = None,
    *,
    end_date: str,
) -> pd.DataFrame:
    """构建周频 key 首次可用日期索引，供批量 as-of 截止解析。"""
    schema = [str(column).strip() for column in schema_columns]
    if not schema or schema[0] != "week_id":
        raise ValueError("weekly schema must start with week_id")
    cutoff = pd.to_datetime(end_date).normalize()
    prepared = _prepare_weekly_long_frame(
        [raw_weekly, derivative_weekly if derivative_weekly is not None else pd.DataFrame()]
    )
    prepared = prepared[
        prepared["rdate"].notna()
        & prepared["rdate"].le(cutoff)
        & prepared["indicators_code"].isin(schema[1:])
    ].copy()
    if prepared.empty:
        return pd.DataFrame(columns=["week_id", "available_date"])

    index = (
        prepared.groupby("week_id", as_index=False)["rdate"]
        .min()
        .rename(columns={"rdate": "available_date"})
        .sort_values(["available_date", "week_id"])
        .reset_index(drop=True)
    )
    authoritative = build_weekly_output_from_frames(
        schema,
        raw_weekly,
        derivative_weekly,
        as_of_date=end_date,
    )
    if set(index["week_id"].astype(int)) != set(authoritative["week_id"].astype(int)):
        raise ValueError("weekly cutoff index does not match authoritative output builder")
    return index


def build_weekly_output_from_metadata(
    metadata: pd.DataFrame,
    raw_weekly: pd.DataFrame,
    derivative_weekly: Optional[pd.DataFrame] = None,
    start_week: Optional[int] = None,
    end_week: Optional[int] = None,
    as_of_date: Optional[str] = None,
) -> pd.DataFrame:
    selected = select_factor_metadata(metadata, "weekly")
    output_columns, lag_map = _metadata_output_columns_and_lags(selected)
    return build_weekly_output_from_frames(
        ["week_id"] + output_columns,
        raw_weekly,
        derivative_weekly,
        start_week=start_week,
        end_week=end_week,
        as_of_date=as_of_date,
        lag_map=lag_map,
    )


def read_weekly_long_from_db(
    indicator_codes: Iterable[str],
    table_name: str,
    engine=None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    return _read_long_from_db(
        indicator_codes,
        table_name,
        ["rdate", "week_id", "indicators_code", "indicators_value"],
        engine,
        end_date=end_date,
    )


def build_weekly_output_from_db(
    schema_columns: Optional[Sequence[str]] = None,
    start_week: Optional[int] = None,
    end_week: Optional[int] = None,
    as_of_date: Optional[str] = None,
    engine=None,
) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        if schema_columns is None:
            metadata = read_factor_metadata_from_db(engine)
            selected = select_factor_metadata(metadata, "weekly")
            codes = selected["indicators_code"].astype(str).str.strip().tolist()
            raw = read_weekly_long_from_db(
                codes,
                "api_wind_weekly",
                engine,
                end_date=as_of_date,
            )
            derivative = read_weekly_long_from_db(
                codes,
                "api_wind_derivative_weekly",
                engine,
                end_date=as_of_date,
            )
            return build_weekly_output_from_metadata(
                selected,
                raw,
                derivative,
                start_week=start_week,
                end_week=end_week,
                as_of_date=as_of_date,
            )

        schema = list(schema_columns)
        codes = schema[1:]
        raw = read_weekly_long_from_db(
            codes,
            "api_wind_weekly",
            engine,
            end_date=as_of_date,
        )
        derivative = read_weekly_long_from_db(
            codes,
            "api_wind_derivative_weekly",
            engine,
            end_date=as_of_date,
        )
        return build_weekly_output_from_frames(
            schema,
            raw,
            derivative,
            start_week=start_week,
            end_week=end_week,
            as_of_date=as_of_date,
        )
    finally:
        if own_engine:
            engine.dispose()


def _business_month_id(rdate: object) -> str:
    if pd.isna(rdate):
        return ""
    date = pd.to_datetime(rdate)
    if date.day > 15:
        return (date + pd.offsets.MonthBegin(1)).strftime("%Y%m")
    return date.strftime("%Y%m")


def _prepare_monthly_long_frame(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    prepared = _with_source_order(frames)
    if not prepared:
        return pd.DataFrame(
            columns=["rdate", "month_id", "indicators_code", "indicators_value", "_source_priority", "_source_order"]
        )
    df = pd.concat(prepared, ignore_index=True)
    required = {"rdate", "indicators_code", "indicators_value", "_source_priority", "_source_order"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"monthly long data missing required columns: {sorted(missing)}")
    if "month_id" not in df.columns:
        df["month_id"] = ""
    result = df[["rdate", "month_id", "indicators_code", "indicators_value", "_source_priority", "_source_order"]].copy()
    result["rdate"] = pd.to_datetime(result["rdate"], errors="coerce").dt.normalize()
    cleaned_month = result["month_id"].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    missing_month = cleaned_month.str.lower().isin({"", "nan", "nat", "none", "<na>"})
    result["month_id"] = cleaned_month.where(~missing_month, result["rdate"].map(_business_month_id))
    result = result[result["month_id"].astype(str).str.fullmatch(r"\d{6}", na=False)].copy()
    result["month_id"] = result["month_id"].astype(str)
    result["indicators_code"] = result["indicators_code"].astype(str).str.strip()
    result["indicators_value"] = pd.to_numeric(result["indicators_value"], errors="coerce")
    return result.dropna(subset=["rdate", "indicators_code", "indicators_value"])


def build_monthly_output_from_frames(
    metadata: pd.DataFrame,
    raw_monthly: pd.DataFrame,
    derivative_monthly: Optional[pd.DataFrame] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_databridge_additions: bool = False,
) -> pd.DataFrame:
    selected = select_monthly_factor_metadata(
        metadata,
        include_databridge_additions=include_databridge_additions,
    )
    output_columns, lag_map = _metadata_output_columns_and_lags(selected)
    source_map = _metadata_source_map(selected)
    df = _prepare_monthly_long_frame([raw_monthly, derivative_monthly if derivative_monthly is not None else pd.DataFrame()])
    if start_date:
        df = df[df["rdate"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["rdate"] <= pd.to_datetime(end_date)]
    df = df[df["indicators_code"].isin(output_columns)].copy()
    if df.empty:
        return pd.DataFrame(columns=["month_id"] + output_columns)

    df = df.sort_values(["month_id", "indicators_code", "_source_priority", "rdate", "_source_order"])
    df = df.drop_duplicates(["month_id", "indicators_code"], keep="last")
    all_months = sorted(df["month_id"].dropna().unique())
    wide = df.pivot(index="month_id", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(all_months)

    parts = []
    for code in output_columns:
        series = wide[code] if code in wide.columns else pd.Series(dtype=float, index=all_months)
        lag = 0 if source_map.get(code) == "derivative" else int(lag_map.get(code, 0) or 0)
        parts.append((series.shift(lag) if lag else series).rename(code))
    result = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=all_months)
    result.index.name = "month_id"
    return result.reset_index()[["month_id"] + output_columns]


def build_monthly_cutoff_index_from_frames(
    metadata: pd.DataFrame,
    raw_monthly: pd.DataFrame,
    derivative_monthly: Optional[pd.DataFrame] = None,
    *,
    end_date: str,
    include_databridge_additions: bool = False,
) -> pd.DataFrame:
    """构建月频 key 首次可用日期索引，保留月中归属与元数据筛选语义。"""
    selected = select_monthly_factor_metadata(
        metadata,
        include_databridge_additions=include_databridge_additions,
    )
    output_columns, _ = _metadata_output_columns_and_lags(selected)
    cutoff = pd.to_datetime(end_date).normalize()
    prepared = _prepare_monthly_long_frame(
        [raw_monthly, derivative_monthly if derivative_monthly is not None else pd.DataFrame()]
    )
    prepared = prepared[
        prepared["rdate"].le(cutoff)
        & prepared["indicators_code"].isin(output_columns)
    ].copy()
    if prepared.empty:
        return pd.DataFrame(columns=["month_id", "available_date"])

    index = (
        prepared.groupby("month_id", as_index=False)["rdate"]
        .min()
        .rename(columns={"rdate": "available_date"})
        .sort_values(["available_date", "month_id"])
        .reset_index(drop=True)
    )
    authoritative = build_monthly_output_from_frames(
        metadata,
        raw_monthly,
        derivative_monthly,
        end_date=end_date,
        include_databridge_additions=include_databridge_additions,
    )
    if set(index["month_id"].astype(str)) != set(authoritative["month_id"].astype(str)):
        raise ValueError("monthly cutoff index does not match authoritative output builder")
    return index


def read_monthly_long_from_db(
    indicator_codes: Iterable[str],
    table_name: str,
    engine=None,
    include_month_id: bool = False,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    columns = ["rdate", "indicators_code", "indicators_value"]
    if include_month_id:
        columns = ["rdate", "month_id", "indicators_code", "indicators_value"]
    return _read_long_from_db(
        indicator_codes,
        table_name,
        columns,
        engine,
        end_date=end_date,
    )


def build_monthly_output_from_db(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    engine=None,
    include_databridge_additions: bool = False,
) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        metadata = read_factor_metadata_from_db(engine)
        selected = select_monthly_factor_metadata(
            metadata,
            include_databridge_additions=include_databridge_additions,
        )
        codes = selected["indicators_code"].astype(str).str.strip().tolist()
        raw = read_monthly_long_from_db(
            codes,
            "api_wind_monthly",
            engine,
            end_date=end_date,
        )
        derivative = read_monthly_long_from_db(
            codes,
            "api_wind_derivative_monthly",
            engine,
            include_month_id=True,
            end_date=end_date,
        )
        return build_monthly_output_from_frames(
            metadata,
            raw,
            derivative,
            start_date=start_date,
            end_date=end_date,
            include_databridge_additions=include_databridge_additions,
        )
    finally:
        if own_engine:
            engine.dispose()


def _save_output(df: pd.DataFrame, path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def save_daily_output(df: pd.DataFrame, path: str | Path | None = None) -> Path:
    return _save_output(df, path or DAILY_OUTPUT_PATH)


def save_weekly_output(df: pd.DataFrame, path: str | Path | None = None) -> Path:
    return _save_output(df, path or WEEKLY_OUTPUT_PATH)


def save_monthly_output(df: pd.DataFrame, path: str | Path | None = None) -> Path:
    return _save_output(df, path or MONTHLY_OUTPUT_PATH)
