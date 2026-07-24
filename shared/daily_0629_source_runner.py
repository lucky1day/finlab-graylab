from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared.daily_0629_source_evidence import (
    Daily0629SourceEvidence,
    source_package_tree_sha256,
)


SELECTED_ROWS_RELATIVE_PATH = Path("prediction") / "daily_selected_prediction_rows.csv"
DETAIL_RELATIVE_PATH = Path("prediction") / "daily_prediction_detail.json"


def run_source_daily_live(
    evidence: Daily0629SourceEvidence,
    *,
    predict_date: str,
) -> list[dict[str, Any]]:
    """重跑原始日度 dry-run live runner，并返回 selected prediction 行。"""
    return list(
        _run_source_daily_live_cached(
            str(evidence.source_package_path),
            evidence.source_package_hash,
            evidence.live_runner_module,
            predict_date,
        )
    )


def run_source_daily_backtest_panel(
    evidence: Daily0629SourceEvidence,
    *,
    source_run_date: str,
) -> list[dict[str, Any]]:
    """重跑原始日度 dry-run backtest runner，并返回当前 scheme 的历史预测面板。"""
    rows = _run_source_daily_backtest_cached(
        str(evidence.source_package_path),
        evidence.source_package_hash,
        evidence.runner_module,
        source_run_date,
    )
    return [dict(row) for row in rows if str(row.get("final_select_id")) == evidence.final_select_id]


@lru_cache(maxsize=128)
def _run_source_daily_live_cached(
    source_package_path: str,
    source_package_hash: str,
    live_runner_module: str,
    predict_date: str,
) -> tuple[dict[str, Any], ...]:
    cache_path = _source_cache_path("live", source_package_hash, live_runner_module, predict_date)
    if cache_path is not None:
        cached = _read_source_cache(cache_path, source_package_hash, live_runner_module, predict_date)
        if cached is not None:
            return cached

    with _source_runtime(
        Path(source_package_path),
        source_package_hash,
    ) as source_root:
        _run_daily_live(source_root, predict_date)
        rows = tuple(_read_live_rows(source_root / "daily_project" / "output", predict_date))
    if cache_path is not None:
        _write_source_cache(cache_path, source_package_hash, live_runner_module, predict_date, rows)
    return rows


@lru_cache(maxsize=64)
def _run_source_daily_backtest_cached(
    source_package_path: str,
    source_package_hash: str,
    runner_module: str,
    source_run_date: str,
) -> tuple[dict[str, Any], ...]:
    cache_path = _source_cache_path("backtest", source_package_hash, runner_module, source_run_date)
    if cache_path is not None:
        cached = _read_source_cache(cache_path, source_package_hash, runner_module, source_run_date)
        if cached is not None:
            return cached

    with _source_runtime(
        Path(source_package_path),
        source_package_hash,
    ) as source_root:
        _run_daily_backtest(source_root, source_run_date)
        rows = tuple(_read_backtest_rows(source_root / "daily_project" / "output", source_run_date))
    if cache_path is not None:
        _write_source_cache(cache_path, source_package_hash, runner_module, source_run_date, rows)
    return rows


def _source_cache_path(source_type: str, source_package_hash: str, runner_module: str, date_value: str) -> Path | None:
    if os.environ.get("DAILY_0629_SOURCE_CACHE_DISABLE") == "1":
        return None
    raw_root = os.environ.get("DAILY_0629_SOURCE_CACHE_DIR")
    root = Path(raw_root) if raw_root else Path(__file__).resolve().parents[1] / "backtest_artifacts" / "daily_0629_source_cache"
    module_key = hashlib.sha256(runner_module.encode("utf-8")).hexdigest()[:16]
    return root / source_type / source_package_hash / module_key / f"{str(date_value)[:10]}.json"


def _read_source_cache(
    path: Path,
    source_package_hash: str,
    runner_module: str,
    date_value: str,
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
    if payload.get("date_value") != str(date_value)[:10]:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return tuple(dict(row) for row in rows)


def _write_source_cache(
    path: Path,
    source_package_hash: str,
    runner_module: str,
    date_value: str,
    rows: tuple[dict[str, Any], ...],
) -> None:
    payload = {
        "source_package_hash": source_package_hash,
        "runner_module": runner_module,
        "date_value": str(date_value)[:10],
        "rows": list(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


class _source_runtime:
    def __init__(
        self,
        source_package_path: Path,
        expected_source_package_sha256: str,
    ):
        self.source_package_path = source_package_path
        self.expected_source_package_sha256 = (
            expected_source_package_sha256
        )
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.source_root: Path | None = None

    def __enter__(self) -> Path:
        self.tempdir = tempfile.TemporaryDirectory(prefix="bfl_daily0629_source_")
        self.source_root = Path(self.tempdir.name) / "forecast_project"
        shutil.copytree(self.source_package_path, self.source_root)
        _clear_quarantine(self.source_root)
        copied_sha256 = source_package_tree_sha256(self.source_root)
        if copied_sha256 != self.expected_source_package_sha256:
            self.tempdir.cleanup()
            self.tempdir = None
            self.source_root = None
            raise RuntimeError(
                "daily 0629 copied source package hash differs from "
                "frozen source identity"
            )
        return self.source_root

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


def _run_daily_live(source_root: Path, predict_date: str) -> None:
    command = ["bash", "daily_project/src/run_daily.sh", "run", str(predict_date)[:10]]
    _run_source_command(command, source_root)


def _run_daily_backtest(source_root: Path, source_run_date: str) -> None:
    command = ["bash", "run_backtest_test.sh", str(source_run_date)[:10], str(source_run_date)[:10], "daily"]
    _run_source_command(command, source_root)


def _run_source_command(command: list[str], source_root: Path) -> None:
    env = os.environ.copy()
    python = _python_command()[0]
    env["PYTHON_BIN"] = python
    env["DRY_RUN"] = "1"
    env["DAILY_N_JOBS"] = env.get("DAILY_N_JOBS", "1")
    env["DAILY_BACKTEST_WORKERS"] = env.get("DAILY_BACKTEST_WORKERS", "1")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(source_root),
            str(source_root / "daily_project" / "src"),
            env.get("PYTHONPATH", ""),
        ]
    )
    completed = subprocess.run(
        command,
        cwd=str(source_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=int(os.environ.get("DAILY_0629_SOURCE_TIMEOUT_SEC", "1800")),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "daily 0629 source runner failed: "
            f"cmd={' '.join(command)}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )


def _python_command() -> list[str]:
    explicit = os.environ.get("DAILY_0629_SOURCE_PYTHON")
    if explicit:
        return [explicit]
    local = Path.home() / "miniconda3" / "envs" / "forecast_env" / "bin" / "python"
    if local.exists():
        return [str(local)]
    return ["conda", "run", "--no-capture-output", "-n", "forecast_env", "python"]


def _read_live_rows(output_root: Path, predict_date: str) -> list[dict[str, Any]]:
    output_date = str(predict_date)[:10]
    selected_path = output_root / output_date / SELECTED_ROWS_RELATIVE_PATH
    if not selected_path.exists():
        raise RuntimeError(f"daily 0629 source live runner produced no selected rows: {selected_path}")
    details = _read_detail_rows(output_root / output_date / DETAIL_RELATIVE_PATH)
    rows: list[dict[str, Any]] = []
    with selected_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            payload = dict(row)
            frequency = str(payload.get("frequency") or "")
            detail = details.get(frequency, {})
            source_row = detail.get("source_row") if isinstance(detail.get("source_row"), dict) else {}
            payload.update({key: value for key, value in detail.items() if key not in {"candidate_summary", "source_row"}})
            payload.update({key: value for key, value in source_row.items() if key not in payload})
            payload["source_output_date"] = output_date
            rows.append(payload)
    if not rows:
        raise RuntimeError(f"daily 0629 source live runner selected rows are empty: {selected_path}")
    return rows


def _read_detail_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _read_backtest_rows(output_root: Path, source_run_date: str) -> list[dict[str, Any]]:
    output_date = str(source_run_date)[:10]
    backtest_root = output_root / output_date / "backtest"
    if not backtest_root.exists():
        raise RuntimeError(f"daily 0629 source backtest runner produced no backtest dir: {backtest_root}")
    rows: list[dict[str, Any]] = []
    for model_dir in sorted(path for path in backtest_root.iterdir() if path.is_dir()):
        predictions_path = model_dir / "predictions.csv"
        summary_path = model_dir / "summary.json"
        if not predictions_path.exists():
            continue
        summary = _read_json(summary_path)
        final_select_id = str(summary.get("final_select_id") or model_dir.name.upper())
        with predictions_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                payload = dict(row)
                payload["source_output_date"] = output_date
                payload["source_model_dir"] = model_dir.name
                payload["final_select_id"] = final_select_id
                for key in (
                    "candidate_id",
                    "display_name",
                    "tenor",
                    "prediction_mode",
                    "target_col",
                    "selected_feature_count",
                    "feature_count",
                    "screen_rule",
                    "train_rule",
                ):
                    if key in summary:
                        payload[key] = summary.get(key)
                rows.append(payload)
    if not rows:
        raise RuntimeError(f"daily 0629 source backtest runner produced no prediction rows under {backtest_root}")
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clear_quarantine(path: Path) -> None:
    if os.uname().sysname != "Darwin":
        return
    for attr in ("com.apple.quarantine",):
        subprocess.run(
            ["xattr", "-dr", attr, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
