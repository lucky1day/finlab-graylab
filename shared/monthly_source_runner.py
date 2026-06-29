from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared.monthly_source_evidence import MonthlySourceEvidence


PREDICTION_RELATIVE_PATH = Path("prediction") / "monthly_selected_predictions.csv"


def run_source_monthly_live(
    evidence: MonthlySourceEvidence,
    *,
    predict_date: str,
) -> list[dict[str, Any]]:
    """重跑原始月度 dry-run 包并返回 selected prediction 行。"""
    return list(
        _run_source_monthly_live_cached(
            str(evidence.source_package_path),
            evidence.source_package_hash,
            evidence.runner_module,
            predict_date,
        )
    )


@lru_cache(maxsize=8)
def _run_source_monthly_live_cached(
    source_package_path: str,
    source_package_hash: str,
    runner_module: str,
    predict_date: str,
) -> tuple[dict[str, Any], ...]:
    del source_package_hash
    with _source_runtime(Path(source_package_path)) as source_root:
        monthly_root = source_root / "monthly_project"
        _run_monthly_module(
            runner_module,
            predict_date,
            source_root=source_root,
            monthly_root=monthly_root,
        )
        return tuple(_read_prediction_rows(monthly_root / "output"))


class _source_runtime:
    def __init__(self, source_package_path: Path):
        self.source_package_path = source_package_path
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.source_root: Path | None = None

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix="bfl_monthly_source_")
        self.source_root = Path(self.tempdir.name) / "forecast_project"
        shutil.copytree(self.source_package_path, self.source_root)
        _clear_quarantine(self.source_root)
        return self.source_root

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


def _run_monthly_module(
    module: str,
    predict_date: str,
    *,
    source_root: Path,
    monthly_root: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(source_root),
            str(monthly_root),
            str(monthly_root / "src"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["DRY_RUN"] = "1"
    script = (
        f"from {module} import run_monthly_pipeline\n"
        f"run_monthly_pipeline({predict_date!r}, dry_run=True)\n"
    )
    command = [*_python_command(), "-c", script]
    completed = subprocess.run(
        command,
        cwd=str(monthly_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "monthly source runner failed: "
            f"cmd={' '.join(command)}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )


def _python_command() -> list[str]:
    explicit = os.environ.get("MONTHLY_SOURCE_PYTHON")
    if explicit:
        return [explicit]
    local = Path.home() / "miniconda3" / "envs" / "forecast_env" / "bin" / "python"
    if local.exists():
        return [str(local)]
    return ["conda", "run", "--no-capture-output", "-n", "forecast_env", "python"]


def _read_prediction_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        path = date_dir / PREDICTION_RELATIVE_PATH
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                payload = dict(row)
                payload["source_output_date"] = date_dir.name
                rows.append(payload)
    if not rows:
        raise RuntimeError(f"monthly source runner produced no prediction rows under {output_root}")
    return rows


def _clear_quarantine(path: Path) -> None:
    if os.uname().sysname != "Darwin":
        return
    subprocess.run(
        ["xattr", "-dr", "com.apple.quarantine", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
