from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKTEST_ARTIFACT_ROOT = PROJECT_ROOT / "backtest_artifacts"
RUNTIME_INPUT_ROOT = BACKTEST_ARTIFACT_ROOT / "runtime_inputs"
HISTORICAL_BACKTEST_ROOT = BACKTEST_ARTIFACT_ROOT / "backtests"


def safe_path_part(value: str) -> str:
    """Return a filesystem-safe path segment for scheme and benchmark ids."""
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))


def benchmark_artifact_root(benchmark_id: str) -> Path:
    """Return the canonical artifact root for one historical backtest benchmark."""
    return HISTORICAL_BACKTEST_ROOT / safe_path_part(benchmark_id)


def benchmark_input_root(benchmark_id: str) -> Path:
    """Return the runtime-style input artifact root scoped to a benchmark."""
    return benchmark_artifact_root(benchmark_id) / "runtime_inputs"


def benchmark_data_check_root(benchmark_id: str) -> Path:
    """Return the root for generated data-alignment and audit reports."""
    return benchmark_artifact_root(benchmark_id) / "data_checks"
