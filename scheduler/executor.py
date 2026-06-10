from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.repository import (
    create_scheme_run,
    create_engine_from_env,
    finish_scheme_run,
    insert_run_predictions,
    sync_scheme_registry,
    update_serving_pointer,
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
    run_id: int | None = None


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
        run_id=int(item["run_id"]) if item.get("run_id") is not None else None,
        scheme_version=str(item["scheme_version"]) if item.get("scheme_version") is not None else None,
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
    """执行单个方案并写入预测表和运行日志。

    执行前校验：
    1. config.yaml status == 'active'（本地配置）
    2. t_scheme_registry.status == 'active'（DB 注册状态）
    3. t_scheme_versions 中当前版本状态为 'active' 或 'shadow'（激活审核状态）
    """
    engine = create_engine_from_env()
    started = time.monotonic()
    if cfg.status != "active":
        duration = time.monotonic() - started
        write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, f"status={cfg.status}")
        engine.dispose()
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, f"status={cfg.status}")

    scheme_version = getattr(cfg, "scheme_version", None)
    ok, reason = _verify_scheme_activation(engine, cfg.scheme_id, scheme_version)
    if not ok:
        duration = time.monotonic() - started
        write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, reason)
        engine.dispose()
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, reason)

    run_id: int | None = None
    try:
        run_id = create_scheme_run(
            engine,
            scheme_id=cfg.scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            run_type="active",
        )
        records = run_scheme_subprocess(cfg.scheme_id, predict_date, algo_env=algo_env, timeout_sec=timeout_sec)
        written = insert_run_predictions(engine, run_id, records, scheme_version=scheme_version)
        for record in records:
            update_serving_pointer(
                engine,
                scheme_id=record.scheme_id,
                target_tenor=record.target_tenor,
                predict_date=record.predict_date,
                run_id=run_id,
                status="approved",
            )
        duration = time.monotonic() - started
        status = "success" if written == len(records) else "partial"
        error_msg = None if status == "success" else f"written={written}, returned={len(records)}"
        finish_scheme_run(
            engine,
            run_id=run_id,
            status=status,
            records_returned=len(records),
            records_written=written,
            error_message=error_msg,
        )
        write_run_log(engine, cfg.scheme_id, predict_date, status, duration, error_msg, run_id=run_id)
        return SchemeRunResult(cfg.scheme_id, status, written, duration, error_msg, run_id)
    except Exception as exc:
        duration = time.monotonic() - started
        error_msg = str(exc)
        if run_id is not None:
            finish_scheme_run(
                engine,
                run_id=run_id,
                status="failed",
                records_returned=None,
                records_written=0,
                error_message=error_msg,
            )
        write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, error_msg, run_id=run_id)
        return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, error_msg, run_id)
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


def _verify_scheme_activation(engine, scheme_id: str, scheme_version: str | None) -> tuple[bool, str]:
    """校验方案在 DB 注册与版本激活状态，返回 (通过, 原因)。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        # 1. 校验 t_scheme_registry 中该方案为 active
        reg_row = conn.execute(
            text("SELECT status FROM t_scheme_registry WHERE scheme_id = :scheme_id"),
            {"scheme_id": scheme_id},
        ).one_or_none()
        if reg_row is None:
            return False, f"scheme {scheme_id} not found in t_scheme_registry"
        if reg_row[0] != "active":
            return False, f"scheme {scheme_id} registry status={reg_row[0]}, must be active"

        # 2. 校验 t_scheme_versions 中当前版本为 active/shadow
        if scheme_version:
            ver_row = conn.execute(
                text(
                    "SELECT status FROM t_scheme_versions "
                    "WHERE scheme_id = :scheme_id AND scheme_version = :scheme_version "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"scheme_id": scheme_id, "scheme_version": scheme_version},
            ).one_or_none()
            if ver_row is None:
                return False, (
                    f"scheme {scheme_id} version {scheme_version} not found in t_scheme_versions; "
                    "run 'python -m harness activate --scheme-id {id}' first"
                )
            if ver_row[0] not in ("active", "shadow"):
                return False, (
                    f"scheme {scheme_id} version {scheme_version} status={ver_row[0]}, "
                    "must be active or shadow"
                )

    return True, "ok"


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
