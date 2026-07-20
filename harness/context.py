from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scheduler.discovery import SchemeConfig


@dataclass(frozen=True)
class GateContext:
    scheme_id: str
    predict_date: str
    project_root: Path
    report_dir: Path
    config: "SchemeConfig | None" = None
    algo_env: str = "forecast_env"
    engine_factory: Callable[[], Any] | None = None
    authorization: Any | None = None
    prediction_phase: str | None = None
    persist_backtest: bool = False
    backtest_sample_size: int = 100
    expected_empty_schema: str | None = None
    timeout_sec: int = 600
    api_base_url: str = "http://127.0.0.1:8100"
    api_instance_nonce: str | None = None
