from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from shared._native_source import _SourceRuntime, _source_python_command
from shared.source_runtime_database import (
    SourceRuntimeDatabaseConfig,
    assert_source_package_identity,
    assert_source_interpreter_supports_package,
    assert_source_runtime_payload_safe,
    load_source_runtime_database_config,
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
    with _SourceRuntime(
        evidence.source_package_path,
        evidence.source_package_hash,
        temporary_prefix="bfl_weekly_avg_source_",
        tree_sha256=source_package_tree_sha256,
        mismatch_message="weekly average copied source package hash differs from frozen source identity",
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
    python_command = _source_python_command("WEEKLY_AVERAGE_SOURCE_PYTHON")
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
