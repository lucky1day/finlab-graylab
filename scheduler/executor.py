from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.repository import (
    create_scheme_run,
    create_engine_from_env,
    finish_scheme_run,
    insert_run_predictions,
    sync_scheme_registry,
    write_run_log,
)
from shared.calendar_service import get_calendar
from shared.models import PredictionRecord
from shared.prediction_context import build_daily_live_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALGO_ENV = "forecast_env"
VALID_PREDICTION_PHASES = {"gray_live", "scheduled_live"}
TIMEOUT_OUTPUT_DRAIN_SEC = 1


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
        feature_date=str(item["feature_date"]) if item.get("feature_date") is not None else None,
        prediction_phase=str(item["prediction_phase"]) if item.get("prediction_phase") is not None else None,
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
    completed = _run_process_group(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        timeout=timeout_sec,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"scheme runner returned non-list payload for {scheme_id}")
    return [_record_from_payload(item) for item in payload]


def _run_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """启动独立进程组，timeout 时清理 conda wrapper 及其子进程。"""
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        stdout, stderr = _drain_timed_out_process_output(process)
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from exc
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """先 SIGTERM，若进程未退出再 SIGKILL 整个进程组。"""
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            return


def _drain_timed_out_process_output(process: subprocess.Popen[str]) -> tuple[str, str]:
    """timeout 清理后短暂收集输出，避免子进程持管道导致无界等待。"""
    try:
        return process.communicate(timeout=TIMEOUT_OUTPUT_DRAIN_SEC)
    except subprocess.TimeoutExpired as exc:
        _close_process_pipes(process)
        return _timeout_payload_to_text(exc.output), _timeout_payload_to_text(exc.stderr)


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _timeout_payload_to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def execute_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    timeout_sec: int = 600,
    prediction_phase: str = "scheduled_live",
) -> SchemeRunResult:
    """执行单个方案并写入预测表和运行日志。

    执行前校验：
    1. config.yaml status == 'active'（本地配置）
    2. t_scheme_registry.status == 'active'（DB 注册状态）
    3. t_scheme_versions 中当前版本状态为 'active' 或 'shadow'（激活审核状态）
    """
    if prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}")
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
    active_targets = _active_registry_targets(engine, cfg.scheme_id)

    run_id: int | None = None
    try:
        run_id = create_scheme_run(
            engine,
            scheme_id=cfg.scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            run_type="active",
            prediction_phase=prediction_phase,
        )
        effective_timeout_sec = _effective_timeout_sec(cfg, timeout_sec)
        records = run_scheme_subprocess(
            cfg.scheme_id,
            predict_date,
            algo_env=algo_env,
            timeout_sec=effective_timeout_sec,
        )
        records = _normalize_live_records(records, prediction_phase=prediction_phase)
        _validate_live_record_dates(records, cfg=cfg, predict_date=predict_date, engine=engine)
        _validate_records_against_active_registry(records, cfg=cfg, active_targets=active_targets)
        written = insert_run_predictions(engine, run_id, records, scheme_version=scheme_version)
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


def _effective_timeout_sec(cfg: SchemeConfig, default_timeout_sec: int) -> int:
    """读取方案级执行 timeout；未配置时保持全局默认。"""
    schedule = getattr(cfg, "schedule", None)
    configured = getattr(schedule, "timeout_sec", None)
    if configured is None:
        configured = getattr(cfg, "execution_timeout_sec", None)
    timeout = int(configured) if configured is not None else int(default_timeout_sec)
    if timeout <= 0:
        raise ValueError(f"scheme {cfg.scheme_id} timeout_sec must be positive, got {timeout}")
    return timeout


def execute_all(
    predict_date: str,
    algo_env: str = DEFAULT_ALGO_ENV,
    include_paused: bool = False,
    prediction_phase: str = "scheduled_live",
) -> list[SchemeRunResult]:
    """同步 registry 后执行方案。"""
    schemes = discover_schemes()
    engine = create_engine_from_env()
    try:
        sync_scheme_registry(engine, schemes)
    finally:
        engine.dispose()

    runnable = schemes if include_paused else [cfg for cfg in schemes if cfg.status == "active"]
    return [
        execute_scheme(cfg, predict_date, algo_env=algo_env, prediction_phase=prediction_phase)
        for cfg in runnable
    ]


def _normalize_live_records(records: list[PredictionRecord], *, prediction_phase: str) -> list[PredictionRecord]:
    """补齐平台级 feature_date / prediction_phase，一处统一控制灰度和正式实盘语义。"""
    normalized: list[PredictionRecord] = []
    for record in records:
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(f"record {record.scheme_id}/{record.target_tenor} missing feature_date")
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                f"record {record.scheme_id}/{record.target_tenor} anchor_date={anchor_date} "
                f"does not equal feature_date={feature_date}"
            )
        extra["feature_date"] = str(feature_date)
        extra["prediction_phase"] = prediction_phase
        normalized.append(
            replace(
                record,
                feature_date=str(feature_date),
                prediction_phase=prediction_phase,
                extra=extra,
            )
        )
    return normalized


def _active_registry_targets(engine, scheme_id: str) -> set[tuple[str, int]]:
    """读取 base 方案当前 active 的业务 target 集合。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT target_tenor, horizon
                FROM t_scheme_registry
                WHERE base_scheme_id = :scheme_id
                  AND status = 'active'
                """
            ),
            {"scheme_id": scheme_id},
        ).mappings().all()
    return {(str(row["target_tenor"]), int(row["horizon"])) for row in rows}


def _validate_live_record_dates(
    records: list[PredictionRecord],
    *,
    cfg: SchemeConfig,
    predict_date: str,
    engine,
) -> None:
    """校验实盘记录没有复用旧输入窗口或旧 target。"""
    if getattr(cfg, "frequency", None) != "daily":
        return
    calendar = get_calendar(engine=engine)
    expected_by_horizon: dict[int, object] = {}
    run_predict_date = str(predict_date)[:10]
    for record in records:
        horizon = int(record.horizon)
        expected = expected_by_horizon.get(horizon)
        if expected is None:
            expected = build_daily_live_context(calendar, run_predict_date, horizon=horizon)
            expected_by_horizon[horizon] = expected
        record_predict_date = str(record.predict_date)[:10]
        record_feature_date = str(record.feature_date)[:10] if record.feature_date is not None else None
        record_target_date = str(record.target_date)[:10]
        problems = []
        if record_predict_date != run_predict_date:
            problems.append(f"expected predict_date={run_predict_date}, got {record_predict_date}")
        if record_feature_date != expected.feature_date:
            problems.append(f"expected feature_date={expected.feature_date}, got {record_feature_date}")
        if record_target_date != expected.target_date:
            problems.append(f"expected target_date={expected.target_date}, got {record_target_date}")
        if problems:
            raise ValueError(
                f"daily live record {record.scheme_id}/{record.target_tenor}/h{horizon} has invalid dates: "
                + "; ".join(problems)
            )


def _validate_records_against_active_registry(
    records: list[PredictionRecord],
    *,
    cfg: SchemeConfig,
    active_targets: set[tuple[str, int]],
) -> None:
    """确保 live 写库记录全部对应 active registry 业务方案行。"""
    config_horizon = int(getattr(cfg, "horizon"))
    for record in records:
        if record.scheme_id != cfg.scheme_id:
            raise ValueError(
                f"record scheme_id={record.scheme_id} does not equal config scheme_id={cfg.scheme_id}"
            )
        if int(record.horizon) != config_horizon:
            raise ValueError(
                f"record {record.scheme_id}/{record.target_tenor} horizon={record.horizon} "
                f"does not equal config horizon={config_horizon}"
            )
        target = (str(record.target_tenor), int(record.horizon))
        if target not in active_targets:
            raise ValueError(
                f"record {record.scheme_id}/{record.target_tenor}/h{record.horizon} "
                "is not active in t_scheme_registry"
            )


def _verify_scheme_activation(engine, scheme_id: str, scheme_version: str | None) -> tuple[bool, str]:
    """校验方案在 DB 注册与版本激活状态，返回 (通过, 原因)。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        # 1. 校验 t_scheme_registry 中该 base 方案至少有一个 active 业务方案行。
        active_rows = conn.execute(
            text(
                """
                SELECT COUNT(*) AS active_count
                FROM t_scheme_registry
                WHERE base_scheme_id = :scheme_id
                  AND status = 'active'
                """
            ),
            {"scheme_id": scheme_id},
        ).scalar_one()
        if int(active_rows or 0) == 0:
            return False, f"scheme {scheme_id} not found in t_scheme_registry"

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
    parser.add_argument(
        "--prediction-phase",
        choices=sorted(VALID_PREDICTION_PHASES),
        default="scheduled_live",
        help="Live prediction phase; gray backfill must pass gray_live explicitly.",
    )
    args = parser.parse_args()

    if args.scheme_id:
        schemes = {cfg.scheme_id: cfg for cfg in discover_schemes()}
        result = execute_scheme(
            schemes[args.scheme_id],
            args.predict_date,
            algo_env=args.algo_env,
            prediction_phase=args.prediction_phase,
        )
        print(result)
        return

    for result in execute_all(
        args.predict_date,
        algo_env=args.algo_env,
        include_paused=args.include_paused,
        prediction_phase=args.prediction_phase,
    ):
        print(result)


if __name__ == "__main__":
    main()
