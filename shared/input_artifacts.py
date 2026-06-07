from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared import data_service as daily_data_service
from shared.artifact_paths import RUNTIME_INPUT_ROOT, safe_path_part
from schemes.weekly_10y_d_overlay.core import weekly_data_service

DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
DAILY_INPUT_TARGET_COLUMNS = ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")


@dataclass(frozen=True)
class InputArtifact:
    """预测算法输入文件及读回后的 DataFrame。"""

    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    generated_at: str
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
    df = daily_data_service.build_daily_output_from_db(
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        target_columns=DAILY_INPUT_TARGET_COLUMNS,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    daily_data_service.save_daily_output(df, path)
    read_back = _read_daily_output_csv(path)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_daily_data_service",
        generated_at=_utc_now(),
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
    end_date: str | None = None,
    include_daily_weekly_close_fallback: bool = False,
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
    df = weekly_data_service.build_weekly_output_from_db(
        schema_columns=schema_columns,
        start_week=start_week,
        end_week=end_week,
        end_date=end_date,
        include_daily_weekly_close_fallback=include_daily_weekly_close_fallback,
        engine=engine,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    read_back = pd.read_csv(path)
    if "week_id" in read_back.columns:
        read_back["week_id"] = pd.to_numeric(read_back["week_id"], errors="coerce").astype("Int64")
    for col in read_back.columns:
        if col != "week_id":
            read_back[col] = pd.to_numeric(read_back[col], errors="coerce")
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="wind_export_weekly_data_service",
        generated_at=_utc_now(),
        metadata={
            "start_week": start_week,
            "end_week": end_week,
            "end_date": end_date,
            "predict_date": predict_date,
            "include_daily_weekly_close_fallback": include_daily_weekly_close_fallback,
        },
    )

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
