from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pandas as pd

from shared.data_service import create_sqlalchemy_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_DATA_SERVICE_PATH = PROJECT_ROOT / "schemes" / "_original_source" / "data_service.py"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "backtest_artifacts" / "input_artifacts"


def _load_original_data_service(path: Path = ORIGINAL_DATA_SERVICE_PATH) -> ModuleType:
    """动态加载原始日频 data_service.py，保持原始文件只读。"""
    spec = importlib.util.spec_from_file_location("bfl_original_daily_data_service_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load original daily data_service: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_original_daily_targets() -> tuple[str, ...]:
    """返回原始 data_service.py 的日频交易日锚定列。"""
    module = _load_original_data_service()
    return tuple(str(item) for item in module.DAILY_TARGETS)


def daily_output_artifact_path(scheme_id: str, predict_date: str) -> Path:
    """生成运行期 daily_output CSV 路径。"""
    safe_scheme_id = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in scheme_id)
    safe_predict_date = predict_date.replace("/", "-").replace(":", "-")
    return DEFAULT_OUTPUT_ROOT / safe_scheme_id / f"daily_output_{safe_predict_date}.csv"


def read_daily_output_csv(path: str | Path) -> pd.DataFrame:
    """读取 data_service 写出的 CSV，并标准化 date / 数值列。"""
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"daily_output CSV missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_original_daily_output_from_db(
    *,
    start_date: str,
    end_date: str,
    engine=None,
    output_path: Optional[str | Path] = None,
) -> pd.DataFrame:
    """按原始 data_service.py 从 DB 生成 daily_output CSV，再读回给算法。

    该函数只编排 SELECT 与本地文件生成；不会写入任何数据库表。
    """
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    path = Path(output_path) if output_path is not None else daily_output_artifact_path("daily", end_date)
    module = _load_original_data_service()
    try:
        df = module.build_daily_output_from_db(start_date=start_date, end_date=end_date, engine=engine)
        module.save_daily_output(df, path)
        return read_daily_output_csv(path)
    finally:
        if own_engine:
            engine.dispose()
