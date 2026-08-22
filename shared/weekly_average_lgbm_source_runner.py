from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from shared.source_runtime_database import (
    SourceRuntimeDatabaseConfig,
    assert_source_package_identity,
    assert_source_interpreter_supports_package,
    assert_source_package_tree_safe,
    assert_source_runtime_payload_safe,
    install_source_runtime_database_config,
    load_source_runtime_database_config,
    prepare_private_source_runtime_tree,
    redact_source_runtime_text,
    run_source_subprocess,
    source_subprocess_environment,
    source_timeout_seconds,
)
from shared.weekly_average_source_evidence import (
    WeeklyAverageSourceEvidence,
    source_package_tree_sha256,
)


DETAIL_RELATIVE_PATH = Path("prediction") / "weekly_prediction_detail.csv"


def run_source_weekly_backtest(
    evidence: WeeklyAverageSourceEvidence,
    *,
    start_date: str,
    end_date: str,
    database_config: SourceRuntimeDatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """重跑原始周平均 LGBM 回测包并返回 prediction detail 行。"""
    config = database_config or load_source_runtime_database_config()
    return list(
        _run_source_weekly_backtest_cached(
            str(evidence.source_package_path),
            evidence.source_package_hash,
            evidence.runner_module,
            start_date,
            end_date,
            config,
        )
    )


def run_source_weekly_live(
    evidence: WeeklyAverageSourceEvidence,
    *,
    predict_date: str,
    database_config: SourceRuntimeDatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """重跑原始周平均 LGBM live 包并返回 prediction detail 行。"""
    config = database_config or load_source_runtime_database_config()
    assert_source_package_identity(
        evidence.source_package_path,
        evidence.source_package_hash,
        tree_sha256=source_package_tree_sha256,
        label="weekly average",
    )
    with _source_runtime(
        evidence.source_package_path,
        evidence.source_package_hash,
        database_config=config,
    ) as source_root:
        weekly_root = source_root / "weekly_project"
        _run_python_module(
            evidence.live_runner_module,
            [predict_date, "--project-root", str(weekly_root), "--frequencies", "all", "--dry-run"],
            source_root=source_root,
            weekly_root=weekly_root,
            database_config=config,
        )
        rows = _read_detail_rows(weekly_root / "output")
    assert_source_runtime_payload_safe(rows, config)
    return rows


def _run_source_weekly_backtest_cached(
    source_package_path: str,
    source_package_hash: str,
    runner_module: str,
    start_date: str,
    end_date: str,
    database_config: SourceRuntimeDatabaseConfig,
) -> tuple[dict[str, Any], ...]:
    assert_source_package_identity(
        Path(source_package_path),
        source_package_hash,
        tree_sha256=source_package_tree_sha256,
        label="weekly average",
    )
    with _source_runtime(
        Path(source_package_path),
        source_package_hash,
        database_config=database_config,
    ) as source_root:
        weekly_root = source_root / "weekly_project"
        _run_python_module(
            runner_module,
            [start_date, end_date, "--project-root", str(weekly_root), "--dry-run"],
            source_root=source_root,
            weekly_root=weekly_root,
            database_config=database_config,
        )
        rows = tuple(_read_detail_rows(weekly_root / "output"))
    assert_source_runtime_payload_safe(rows, database_config)
    return rows


class _source_runtime:
    def __init__(
        self,
        source_package_path: Path,
        expected_source_package_sha256: str,
        *,
        database_config: SourceRuntimeDatabaseConfig | None = None,
    ):
        self.source_package_path = source_package_path
        self.expected_source_package_sha256 = (
            expected_source_package_sha256
        )
        self.database_config = database_config
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.source_root: Path | None = None

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix="bfl_weekly_avg_source_")
        self.source_root = Path(self.tempdir.name) / "forecast_project"
        try:
            assert_source_package_tree_safe(
                self.source_package_path
            )
            shutil.copytree(self.source_package_path, self.source_root)
            prepare_private_source_runtime_tree(self.source_root)
            _clear_quarantine(self.source_root)
            copied_sha256 = source_package_tree_sha256(
                self.source_root
            )
            if copied_sha256 != self.expected_source_package_sha256:
                raise RuntimeError(
                    "weekly average copied source package hash differs "
                    "from frozen source identity"
                )
            config = (
                self.database_config
                or load_source_runtime_database_config()
            )
            install_source_runtime_database_config(
                self.source_root,
                config,
            )
        except BaseException:
            self.tempdir.cleanup()
            self.tempdir = None
            self.source_root = None
            raise
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
    database_config: SourceRuntimeDatabaseConfig | None = None,
) -> None:
    config = (
        database_config
        or load_source_runtime_database_config()
    )
    env = source_subprocess_environment()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(weekly_root / "src"),
            str(weekly_root),
            str(source_root),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["DRY_RUN"] = "1"
    python_command = _python_command()
    assert_source_interpreter_supports_package(
        python_command,
        source_root,
        label="weekly average lgbm",
    )
    command = [*python_command, "-m", module, *args]
    timeout_sec = source_timeout_seconds(
        "WEEKLY_AVERAGE_SOURCE_TIMEOUT_SEC"
    )
    try:
        completed = run_source_subprocess(
            command,
            cwd=str(weekly_root),
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "weekly average source runner timed out: "
            f"timeout_sec={timeout_sec}\n"
            f"stdout={redact_source_runtime_text(exc.stdout or '', config)}\n"
            f"stderr={redact_source_runtime_text(exc.stderr or '', config)}"
        ) from None
    if completed.returncode != 0:
        raise RuntimeError(
            "weekly average source runner failed: "
            f"cmd={' '.join(command)}\n"
            f"stdout={redact_source_runtime_text(completed.stdout, config)}\n"
            f"stderr={redact_source_runtime_text(completed.stderr, config)}"
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
