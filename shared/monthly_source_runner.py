from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from shared.monthly_source_evidence import (
    MonthlySourceEvidence,
    source_package_tree_sha256,
)
from shared._native_source import _SourceRuntime, _source_python_command
from shared.source_runtime_database import (
    SourceRuntimeDatabaseConfig,
    assert_source_package_identity,
    assert_source_interpreter_supports_package,
    assert_source_runtime_payload_safe,
    load_source_runtime_database_config,
    redact_source_runtime_text,
    run_source_subprocess,
    source_immutable_input_token,
    source_subprocess_environment,
    source_timeout_seconds,
    write_private_json_atomic,
)
from shared.runtime_paths import resolve_runtime_state_path


PREDICTION_RELATIVE_PATH = Path("prediction") / "monthly_selected_predictions.csv"


def run_source_monthly_live(
    evidence: MonthlySourceEvidence,
    *,
    predict_date: str,
    database_config: SourceRuntimeDatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """重跑原始月度 dry-run 包并返回 selected prediction 行。"""
    config = database_config or load_source_runtime_database_config()
    return list(
        _run_source_monthly_live_cached(
            str(evidence.source_package_path),
            evidence.source_package_hash,
            evidence.runner_module,
            predict_date,
            config,
        )
    )


def _run_source_monthly_live_cached(
    source_package_path: str,
    source_package_hash: str,
    runner_module: str,
    predict_date: str,
    database_config: SourceRuntimeDatabaseConfig,
) -> tuple[dict[str, Any], ...]:
    assert_source_package_identity(
        Path(source_package_path),
        source_package_hash,
        tree_sha256=source_package_tree_sha256,
        label="monthly",
    )
    input_token = source_immutable_input_token()
    cache_path = _source_cache_path(
        source_package_hash,
        runner_module,
        predict_date,
        database_config.cache_identity,
        input_token=input_token,
    )
    if cache_path is not None:
        cached = _read_source_cache(
            cache_path,
            source_package_hash,
            runner_module,
            predict_date,
            database_config.cache_identity,
            input_token,
        )
        if cached is not None:
            assert_source_runtime_payload_safe(
                cached,
                database_config,
            )
            return cached

    with _SourceRuntime(
        Path(source_package_path),
        source_package_hash,
        temporary_prefix="bfl_monthly_source_",
        tree_sha256=source_package_tree_sha256,
        mismatch_message="monthly copied source package hash differs from frozen source identity",
        database_config=database_config,
    ) as source_root:
        monthly_root = source_root / "monthly_project"
        _run_monthly_module(
            runner_module,
            predict_date,
            source_root=source_root,
            monthly_root=monthly_root,
            database_config=database_config,
        )
        rows = tuple(_read_prediction_rows(monthly_root / "output"))
    assert_source_runtime_payload_safe(rows, database_config)
    if cache_path is not None:
        _write_source_cache(
            cache_path,
            source_package_hash,
            runner_module,
            predict_date,
            database_config.cache_identity,
            input_token,
            rows,
        )
    return rows


def _source_cache_path(
    source_package_hash: str,
    runner_module: str,
    predict_date: str,
    database_identity: str,
    *,
    input_token: str | None = None,
) -> Path | None:
    if os.environ.get("MONTHLY_SOURCE_CACHE_DISABLE") == "1":
        return None
    token = input_token or source_immutable_input_token()
    if token is None:
        return None
    root = resolve_runtime_state_path(
        relative_path="cache/monthly",
        development_default=(
            Path(__file__).resolve().parents[1]
            / "backtest_artifacts"
            / "monthly_source_cache"
        ),
        override_env="MONTHLY_SOURCE_CACHE_DIR",
    )
    module_key = hashlib.sha256(runner_module.encode("utf-8")).hexdigest()[:16]
    return (
        root
        / source_package_hash
        / database_identity
        / token
        / module_key
        / f"{str(predict_date)[:10]}.json"
    )


def _read_source_cache(
    path: Path,
    source_package_hash: str,
    runner_module: str,
    predict_date: str,
    database_identity: str,
    input_token: str | None,
) -> tuple[dict[str, Any], ...] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("source_package_hash") != source_package_hash:
        return None
    if payload.get("runner_module") != runner_module:
        return None
    if payload.get("predict_date") != str(predict_date)[:10]:
        return None
    if payload.get("database_identity") != database_identity:
        return None
    if payload.get("input_token") != input_token:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return tuple(dict(row) for row in rows)


def _write_source_cache(
    path: Path,
    source_package_hash: str,
    runner_module: str,
    predict_date: str,
    database_identity: str,
    input_token: str | None,
    rows: tuple[dict[str, Any], ...],
) -> None:
    payload = {
        "source_package_hash": source_package_hash,
        "runner_module": runner_module,
        "predict_date": str(predict_date)[:10],
        "database_identity": database_identity,
        "input_token": input_token,
        "rows": list(rows),
    }
    write_private_json_atomic(path, payload)


def _run_monthly_module(
    module: str,
    predict_date: str,
    *,
    source_root: Path,
    monthly_root: Path,
    database_config: SourceRuntimeDatabaseConfig | None = None,
) -> None:
    config = (
        database_config
        or load_source_runtime_database_config()
    )
    env = source_subprocess_environment()
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
    python_command = _source_python_command("MONTHLY_SOURCE_PYTHON")
    assert_source_interpreter_supports_package(
        python_command,
        source_root,
        label="monthly 0629",
    )
    command = [*python_command, "-c", script]
    timeout_sec = source_timeout_seconds(
        "MONTHLY_SOURCE_TIMEOUT_SEC"
    )
    try:
        completed = run_source_subprocess(
            command,
            cwd=str(monthly_root),
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "monthly source runner timed out: "
            f"timeout_sec={timeout_sec}\n"
            f"stdout={redact_source_runtime_text(exc.stdout or '', config)}\n"
            f"stderr={redact_source_runtime_text(exc.stderr or '', config)}"
        ) from None
    if completed.returncode != 0:
        raise RuntimeError(
            "monthly source runner failed: "
            f"cmd={' '.join(command)}\n"
            f"stdout={redact_source_runtime_text(completed.stdout, config)}\n"
            f"stderr={redact_source_runtime_text(completed.stderr, config)}"
        )


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
