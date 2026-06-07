from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DAILY_SRC = Path(__file__).resolve().parents[1]
DAILY_PROJECT_ROOT = DAILY_SRC.parent
FORECAST_ROOT = DAILY_PROJECT_ROOT.parent
DATA_DAILY = DAILY_PROJECT_ROOT / "data" / "daily"
OUTPUT_DAILY = DAILY_PROJECT_ROOT / "output" / "daily"
LOGS_DAILY = DAILY_PROJECT_ROOT / "logs" / "daily"
MODEL_STORE = DATA_DAILY / "model_store"


@dataclass(frozen=True)
class TenorConfig:
    tenor: str
    frequency: str
    style: str
    close_col: str
    mid_col: str | None = None
    long_col: str | None = None
    short_col: str | None = None
    window: int = 126
    num_leaves: int = 3
    min_child_samples: int = 20
    learning_rate: float = 0.03
    n_estimators: int = 140
    epsilon: float = 0.0
    threshold: float = 0.0
    n_jobs: int = 1
    vote_scheme: str = "legacy"


TENOR_CONFIGS: dict[str, TenorConfig] = {
    "D1Y": TenorConfig(
        tenor="1Y",
        frequency="D1Y",
        style="10y",
        close_col="TB1YWI0C",
        mid_col="TB0YWI0C",
        short_col="TB5YWI0C",
        min_child_samples=20,
        vote_scheme="legacy",
    ),
    "D5Y": TenorConfig(
        tenor="5Y",
        frequency="D5Y",
        style="5y",
        close_col="TB5YWI0C",
        long_col="TB0YWI0C",
        short_col="TB1YWI0C",
        min_child_samples=40,
        epsilon=0.04,
    ),
    "D10Y": TenorConfig(
        tenor="10Y",
        frequency="D10Y",
        style="10y",
        close_col="TB0YWI0C",
        mid_col="TB5YWI0C",
        short_col="TB1YWI0C",
        min_child_samples=20,
        vote_scheme="legacy",
    ),
}


RESULT_MAPPING = {0: "平", 1: "空", -1: "多"}
