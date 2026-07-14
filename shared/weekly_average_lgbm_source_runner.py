from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from shared.weekly_average_source_evidence import WeeklyAverageSourceEvidence


DETAIL_RELATIVE_PATH = Path("prediction") / "weekly_prediction_detail.csv"


def run_source_weekly_backtest(
    evidence: WeeklyAverageSourceEvidence,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """重跑原始周平均 LGBM 回测包并返回 prediction detail 行。"""
    return list(
        _run_source_weekly_backtest_cached(
            str(evidence.source_package_path),
            evidence.source_package_hash,
            evidence.runner_module,
            start_date,
            end_date,
        )
    )


def run_source_weekly_live(
    evidence: WeeklyAverageSourceEvidence,
    *,
    predict_date: str,
) -> list[dict[str, Any]]:
    """重跑原始周平均 LGBM live 包并返回 prediction detail 行。"""
    with _source_runtime(evidence.source_package_path) as source_root:
        weekly_root = source_root / "weekly_project"
        _run_python_module(
            evidence.live_runner_module,
            [predict_date, "--project-root", str(weekly_root), "--frequencies", "all", "--dry-run"],
            source_root=source_root,
            weekly_root=weekly_root,
        )
        return _read_detail_rows(weekly_root / "output")


@lru_cache(maxsize=8)
def _run_source_weekly_backtest_cached(
    source_package_path: str,
    source_package_hash: str,
    runner_module: str,
    start_date: str,
    end_date: str,
) -> tuple[dict[str, Any], ...]:
    del source_package_hash
    with _source_runtime(Path(source_package_path)) as source_root:
        weekly_root = source_root / "weekly_project"
        _run_python_module(
            runner_module,
            [start_date, end_date, "--project-root", str(weekly_root), "--dry-run"],
            source_root=source_root,
            weekly_root=weekly_root,
        )
        return tuple(_read_detail_rows(weekly_root / "output"))


class _source_runtime:
    def __init__(self, source_package_path: Path):
        self.source_package_path = source_package_path
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.source_root: Path | None = None

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix="bfl_weekly_avg_source_")
        self.source_root = Path(self.tempdir.name) / "forecast_project"
        shutil.copytree(self.source_package_path, self.source_root)
        _clear_quarantine(self.source_root)
        return self.source_root

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


def _run_python_module(
    module: str,
    args: list[str],
    *,
    source_root: Path,
    weekly_root: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(weekly_root / "src"),
            str(weekly_root),
            str(source_root),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["DRY_RUN"] = "1"
    command = [*_python_command(), "-m", module, *args]
    completed = subprocess.run(
        command,
        cwd=str(weekly_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "weekly average source runner failed: "
            f"cmd={' '.join(command)}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )


def _python_command() -> list[str]:
    explicit = os.environ.get("WEEKLY_AVERAGE_SOURCE_PYTHON")
    if explicit:
        return [explicit]
    local = Path.home() / "miniconda3" / "envs" / "forecast_env" / "bin" / "python"
    if local.exists():
        return [str(local)]
    return ["conda", "run", "--no-capture-output", "-n", "forecast_env", "python"]


def _read_detail_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date_dir in sorted(path for path in output_root.iterdir() if path.is_dir()):
        path = date_dir / DETAIL_RELATIVE_PATH
        if not path.exists():
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame.columns = [str(column).lstrip("\ufeff") for column in frame.columns]
        for _, row in frame.iterrows():
            payload = row.to_dict()
            payload["source_output_date"] = date_dir.name
            rows.append(payload)
    if not rows:
        raise RuntimeError(f"weekly average source runner produced no detail rows under {output_root}")
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
