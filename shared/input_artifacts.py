from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared import data_service
from shared.artifact_paths import RUNTIME_INPUT_ROOT, safe_path_part

DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
DAILY_DATA_VERSION = "shared_data_service_daily.v1"
WEEKLY_DATA_VERSION = "shared_data_service_weekly.v1"


@dataclass(frozen=True)
class InputArtifact:
    """预测算法输入文件及读回后的 DataFrame。"""

    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    generated_at: str
    data_version: str
    row_count: int
    column_count: int
    columns: list[str]
    date_coverage: dict[str, Any]
    quality_flags: dict[str, Any]
    metadata: dict[str, Any]


def input_artifact_path(
    *,
    scheme_id: str,
    frequency: str,
    predict_date: str,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """生成统一的输入文件路径。"""
    safe_scheme_id = safe_path_part(scheme_id)
    safe_predict_date = str(predict_date).replace("/", "-").replace(":", "-")
    prefix = "weekly_output" if frequency == "weekly" else "daily_output"
    return Path(output_root) / safe_scheme_id / f"{prefix}_{safe_predict_date}.csv"


def build_daily_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    start_date: str,
    end_date: str,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """通过日频 data_service 生成输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = data_service.build_daily_output_from_db(
        start_date=start_date,
        end_date=end_date,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    data_service.save_daily_output(df, path)
    read_back = _read_daily_output_csv(path)
    profile = _dataframe_profile(read_back, coverage_field="date", required_columns=("date",))
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_data_service_daily",
        generated_at=_utc_now(),
        data_version=DAILY_DATA_VERSION,
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata={
            "start_date": start_date,
            "end_date": end_date,
            "predict_date": predict_date,
        },
    )


def build_weekly_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    schema_columns: list[str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """生成周频输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = data_service.build_weekly_output_from_db(
        schema_columns=schema_columns,
        start_week=start_week,
        end_week=end_week,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    data_service.save_weekly_output(df, path)
    read_back = pd.read_csv(path)
    if "week_id" in read_back.columns:
        read_back["week_id"] = pd.to_numeric(read_back["week_id"], errors="coerce").astype("Int64")
    for col in read_back.columns:
        if col != "week_id":
            read_back[col] = pd.to_numeric(read_back[col], errors="coerce")
    profile = _dataframe_profile(read_back, coverage_field="week_id", required_columns=("week_id",))
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_weekly",
        generated_at=_utc_now(),
        data_version=WEEKLY_DATA_VERSION,
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata={
            "start_week": start_week,
            "end_week": end_week,
            "predict_date": predict_date,
        },
    )


def _dataframe_profile(
    df: pd.DataFrame,
    *,
    coverage_field: str,
    required_columns: tuple[str, ...],
) -> dict[str, Any]:
    columns = [str(col) for col in df.columns]
    missing_required = [col for col in required_columns if col not in columns]
    date_coverage: dict[str, Any] = {"field": coverage_field, "start": None, "end": None}
    null_coverage_rows = 0
    duplicate_coverage_values = 0

    if coverage_field in df.columns:
        values = df[coverage_field]
        null_coverage_rows = int(values.isna().sum())
        non_null = values.dropna()
        duplicate_coverage_values = int(non_null.duplicated().sum())
        start, end = _coverage_bounds(non_null, coverage_field)
        date_coverage["start"] = start
        date_coverage["end"] = end

    return {
        "row_count": int(len(df)),
        "column_count": int(len(columns)),
        "columns": columns,
        "date_coverage": date_coverage,
        "quality_flags": {
            "missing_required_columns": missing_required,
            "empty_frame": bool(df.empty),
            "null_coverage_rows": null_coverage_rows,
            "duplicate_coverage_values": duplicate_coverage_values,
        },
    }


def _coverage_bounds(values: pd.Series, coverage_field: str) -> tuple[Any, Any]:
    if values.empty:
        return None, None
    if coverage_field == "date":
        parsed = pd.to_datetime(values, errors="coerce").dropna()
        if parsed.empty:
            return None, None
        return parsed.min().strftime("%Y-%m-%d"), parsed.max().strftime("%Y-%m-%d")
    if coverage_field == "week_id":
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return None, None
        return int(numeric.min()), int(numeric.max())
    return values.min(), values.max()

def _read_daily_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"daily input artifact missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
