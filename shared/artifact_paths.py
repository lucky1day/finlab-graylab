from __future__ import annotations

from pathlib import Path

from shared.runtime_paths import resolve_runtime_state_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKTEST_ARTIFACT_ROOT = resolve_runtime_state_path(
    relative_path="artifacts",
    development_default=PROJECT_ROOT / "backtest_artifacts",
)
RUNTIME_INPUT_ROOT = BACKTEST_ARTIFACT_ROOT / "runtime_inputs"
HISTORICAL_BACKTEST_ROOT = BACKTEST_ARTIFACT_ROOT / "backtests"
SOURCE_EVIDENCE_ROOT = PROJECT_ROOT / "source_evidence"
BENCHMARK_SOURCE_EVIDENCE_ROOT = SOURCE_EVIDENCE_ROOT / "benchmark_batches"


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


def benchmark_source_evidence_root(benchmark_id: str) -> Path:
    """Return the read-only external source evidence root for a benchmark batch."""
    return BENCHMARK_SOURCE_EVIDENCE_ROOT / safe_path_part(benchmark_id)
