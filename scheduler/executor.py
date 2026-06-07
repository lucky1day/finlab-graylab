from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.repository import (
    create_engine_from_env,
    sync_scheme_registry,
    upsert_predictions,
    write_run_log,
)
from shared.models import PredictionRecord


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALGO_ENV = "forecast_env"


@dataclass(frozen=True)
class SchemeRunResult:
    """方案执行结果。"""

    scheme_id: str
    status: str
    records_written: int = 0
    duration_sec: float | None = None
    error_msg: str | None = None


def _record_from_payload(item: dict) -> PredictionRecord:
    return PredictionRecord(
        scheme_id=str(item["scheme_id"]),
        target_tenor=str(item["target_tenor"]),
        horizon=int(item["horizon"]),
        predict_date=str(item["predict_date"]),
        target_date=str(item["target_date"]),
        predicted_direction=int(item["predicted_direction"]),
        confidence=float(item["confidence"]) if item.get("confidence") is not None else None,
        model_version=str(item["model_version"]) if item.get("model_version") is not None else None,
        extra=item.get("extra") or None,
    )


def run_scheme_subprocess(
    scheme_id: str,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
) -> list[PredictionRecord]:
    """通过 conda 子进程在算法环境中运行方案。"""
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    cmd = [
        "conda",
        "run",
        "-n",
        algo_env,
        "python",
        "-m",
        "scheduler.scheme_runner",
        "--scheme-id",
        scheme_id,
        "--predict-date",
        predict_date,
    ]
    completed = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"scheme runner returned non-list payload for {scheme_id}")
    return [_record_from_payload(item) for item in payload]


def execute_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
) -> SchemeRunResult:
    """执行单个方案并写入预测表和运行日志。"""
    engine = create_engine_from_env()
    started = time.monotonic()
    if cfg.status != "active":
        duration = time.monotonic() - started
        write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, f"status={cfg.status}")
        engine.dispose()
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, f"status={cfg.status}")

    try:
        records = run_scheme_subprocess(cfg.scheme_id, predict_date, algo_env=algo_env, timeout_sec=timeout_sec)
        written = upsert_predictions(engine, records)
        duration = time.monotonic() - started
        status = "success" if written == len(records) else "partial"
        error_msg = None if status == "success" else f"written={written}, returned={len(records)}"
        write_run_log(engine, cfg.scheme_id, predict_date, status, duration, error_msg)
        return SchemeRunResult(cfg.scheme_id, status, written, duration, error_msg)
    except Exception as exc:
        duration = time.monotonic() - started
        error_msg = str(exc)
        write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, error_msg)
        return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, error_msg)
    finally:
        engine.dispose()


def execute_all(
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    include_paused: bool = False,
) -> list[SchemeRunResult]:
    """同步 registry 后执行方案。"""
    schemes = discover_schemes()
    engine = create_engine_from_env()
    try:
        sync_scheme_registry(engine, schemes)
    finally:
        engine.dispose()

    runnable = schemes if include_paused else [cfg for cfg in schemes if cfg.status == "active"]
    return [execute_scheme(cfg, predict_date, algo_env=algo_env) for cfg in runnable]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Execute Bond Factor Lab schemes.")
    parser.add_argument("predict_date", help="Prediction date in YYYY-MM-DD format")
    parser.add_argument("--scheme-id", default=None, help="Only execute one scheme")
    parser.add_argument("--algo-env", default=os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV))
    parser.add_argument("--include-paused", action="store_true", help="Run/skip paused schemes and log skipped rows")
    args = parser.parse_args()

    if args.scheme_id:
        schemes = {cfg.scheme_id: cfg for cfg in discover_schemes()}
        result = execute_scheme(schemes[args.scheme_id], args.predict_date, algo_env=args.algo_env)
        print(result)
        return

    for result in execute_all(args.predict_date, algo_env=args.algo_env, include_paused=args.include_paused):
        print(result)


if __name__ == "__main__":
    main()
