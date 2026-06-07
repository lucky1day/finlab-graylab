from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from schemes.weekly_10y_d_overlay.core.weekly_data_service import build_weekly_output_from_db
from shared.original_daily_data_service import build_original_daily_output_from_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "backtest_artifacts" / "input_artifacts"


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
    safe_scheme_id = _safe_path_part(scheme_id)
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
    """通过原始 data_service 生成日频输入 CSV，再读回给算法。"""
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    df = build_original_daily_output_from_db(
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        output_path=path,
    )
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=df,
        source="original_daily_data_service",
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
    df = build_weekly_output_from_db(
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


def _safe_path_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
