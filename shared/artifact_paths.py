from __future__ import annotations

from pathlib import Path

from shared.runtime_paths import resolve_runtime_state_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKTEST_ARTIFACT_ROOT = resolve_runtime_state_path(
    relative_path="artifacts",
    development_default=PROJECT_ROOT / "backtest_artifacts",
)
RUNTIME_INPUT_ROOT = BACKTEST_ARTIFACT_ROOT / "runtime_inputs"


def safe_path_part(value: str) -> str:
    """Return a filesystem-safe path segment for scheme and benchmark ids."""
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))
