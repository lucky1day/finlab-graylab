from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from shared.db_config import DatabaseConfig

DAILY_TARGETS = ("TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
FREQUENCY_ALIASES = {
    "daily": {"日", "daily", "Daily", "DAILY", "D", "d", "1"},
    "weekly": {"周", "weekly", "Weekly", "WEEKLY", "W", "w", "2"},
    "monthly": {"月", "monthly", "Monthly", "MONTHLY", "M", "m", "3"},
}

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
        import json

        parsed = json.loads(text)
    except Exception:
        return False
    return int(parsed.get(key, 0) or 0) == 1


def select_factor_metadata(
    metadata: pd.DataFrame,
    frequency: str = "daily",
    tenor_filter: Optional[set[str]] = None,
    predict_model_only: bool = False,
    status_only: bool = False,
) -> pd.DataFrame:
    """Select factor rows used to build model input.

    The normal daily-output reproduction uses all rows with daily frequency.
    When exact reproduction needs a restricted factor universe, the optional
    tenor filter is applied only through api_wind_indicators_all.bond_tenor.
    """
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
        result = result[result["status"].astype(str).str.strip().isin({"1", "1.0", "True", "true"})]

    if tenor_filter:
        if "bond_tenor" in result.columns:
            allowed = {str(item).strip() for item in tenor_filter}
            tenor_values = result["bond_tenor"].fillna("").astype(str).str.strip()
            result = result[tenor_values.eq("") | tenor_values.isin(allowed)]

    return result.reset_index(drop=True)


def _metadata_output_columns_and_lags(metadata: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    selected = metadata.copy()
    selected["indicators_code"] = selected["indicators_code"].astype(str).str.strip()
    output_columns = selected["indicators_code"].tolist()
    if "lag_length" in selected.columns:
        lags = pd.to_numeric(selected["lag_length"], errors="coerce").fillna(0).astype(int)
    else:
        lags = pd.Series(0, index=selected.index)
    return output_columns, dict(zip(output_columns, lags))


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
    target_columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    selected = select_factor_metadata(metadata, "daily", tenor_filter=tenor_filter)
    output_columns, lag_map = _metadata_output_columns_and_lags(selected)
    df = _prepare_long_frame([raw_daily, derivative_daily if derivative_daily is not None else pd.DataFrame()])

    if start_date:
        df = df[df["rdate"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["rdate"] <= pd.to_datetime(end_date)]

    target_codes = tuple(target_columns or DAILY_TARGETS)
    y_df = df[df["indicators_code"].isin(target_codes)]
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


def create_sqlalchemy_engine(db_config: Optional[DatabaseConfig] = None):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL

    cfg = db_config or DatabaseConfig.from_env()
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


def read_daily_long_from_db(indicator_codes: Iterable[str], table_name: str, engine=None) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    codes = [str(code).strip() for code in indicator_codes if str(code).strip()]
    if not codes:
        return pd.DataFrame(columns=["rdate", "indicators_code", "indicators_value"])
    placeholders = ", ".join(["%s"] * len(codes))
    sql = (
        "SELECT rdate, indicators_code, indicators_value "
        f"FROM {table_name} WHERE indicators_code IN ({placeholders}) "
        "AND indicators_value IS NOT NULL"
    )
    try:
        return pd.read_sql(sql, engine, params=tuple(codes))
    finally:
        if own_engine:
            engine.dispose()


def build_daily_output_from_db(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tenor_filter: Optional[set[str]] = None,
    target_columns: Optional[Sequence[str]] = None,
    engine=None,
) -> pd.DataFrame:
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        metadata = read_factor_metadata_from_db(engine)
        selected = select_factor_metadata(metadata, "daily", tenor_filter=tenor_filter)
        codes = selected["indicators_code"].astype(str).str.strip().tolist()
        raw = read_daily_long_from_db(codes, "api_wind_daily", engine)
        derivative = read_daily_long_from_db(codes, "api_wind_derivative_daily", engine)
        return build_daily_output_from_frames(
            selected,
            raw,
            derivative,
            start_date=start_date,
            end_date=end_date,
            target_columns=target_columns,
        )
    finally:
        if own_engine:
            engine.dispose()


def save_daily_output(df: pd.DataFrame, path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    return output_path
