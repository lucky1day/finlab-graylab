from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.repository import (
    attach_run_data_snapshot,
    create_scheme_run,
    create_engine_from_env,
    finish_scheme_run,
    insert_approved_blackbox_predictions,
    insert_run_predictions,
    read_blackbox_execution_approval,
    sync_scheme_registry,
    write_run_log,
)
from shared.calendar_service import get_calendar
from shared.blackbox_v2.contracts import load_metadata
from shared.blackbox_v2.requests import build_live_request
from shared.input_artifacts import open_blackbox_input_snapshot, resolve_blackbox_input_cutoffs
from shared.models import PredictionRecord
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALGO_ENV = "forecast_env"
VALID_PREDICTION_PHASES = {"gray_live", "scheduled_live"}
TIMEOUT_OUTPUT_DRAIN_SEC = 1
logger = logging.getLogger(__name__)


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
        "--no-capture-output",
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


def run_configured_scheme(
    cfg: SchemeConfig,
    predict_date: str,
    *,
    engine,
    algo_env: str,
    timeout_sec: int,
) -> list[PredictionRecord]:
    """按显式 runtime_type 选择算法执行驱动。"""
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type == "native_adapter":
        return run_scheme_subprocess(
            cfg.scheme_id,
            predict_date,
            algo_env=algo_env,
            timeout_sec=timeout_sec,
        )
    if runtime_type == "blackbox_v2":
        if getattr(cfg, "input_source", None) != "data_bridge_current":
            raise ValueError(f"Blackbox V2 input_source must be data_bridge_current: {cfg.scheme_id}")
        return run_blackbox_scheme_subprocess(
            cfg,
            predict_date,
            engine=engine,
            algo_env=algo_env,
            timeout_sec=timeout_sec,
        )
    raise ValueError(f"unsupported runtime_type for {cfg.scheme_id}: {runtime_type}")


def run_blackbox_scheme_subprocess(
    cfg: SchemeConfig,
    predict_date: str,
    *,
    engine,
    algo_env: str,
    timeout_sec: int,
) -> list[PredictionRecord]:
    """生成平台输入并通过 Blackbox V2 CLI 执行一个实盘 Request。"""
    from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE, run_blackbox_predict

    if cfg.delivery_script is None or cfg.delivery_metadata is None:
        raise ValueError(f"Blackbox V2 delivery paths missing for {cfg.scheme_id}")
    metadata = load_metadata(cfg.delivery_metadata)
    with open_blackbox_input_snapshot(snapshot_date=predict_date, require_fresh=True) as snapshot:
        calendar = get_calendar(engine)
        if metadata.frequency == "daily":
            feature_date = build_daily_live_context(
                calendar,
                predict_date,
                horizon=metadata.horizon,
            ).feature_date
        elif metadata.frequency == "weekly":
            feature_date = build_weekly_live_context(calendar, predict_date).feature_date
        else:
            feature_date = build_monthly_live_context(calendar, predict_date).feature_date
        cutoffs = resolve_blackbox_input_cutoffs(
            snapshot,
            feature_date=feature_date,
            engine=engine,
        )
        request = build_live_request(
            metadata,
            predict_date=predict_date,
            calendar=calendar,
            cutoffs=cutoffs,
        )
        blackbox_env = DEFAULT_RUNTIME_PROFILE.conda_env if algo_env == DEFAULT_ALGO_ENV else algo_env
        profile = replace(
            DEFAULT_RUNTIME_PROFILE,
            conda_env=blackbox_env,
            predict_timeout_sec=timeout_sec,
        )
        record = run_blackbox_predict(
            metadata=metadata,
            script_path=cfg.delivery_script,
            request=request,
            data_dir=snapshot.data_dir,
            data_snapshot_id=snapshot.snapshot_id,
            profile=profile,
        )
    return [record]


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
    2. Native 维持既有 Registry 与 active/shadow 版本校验
    3. Blackbox 要求 exact active 版本、批准人与 composite Registry 身份全部一致
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
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type == "blackbox_v2":
        config_version_status = getattr(cfg, "version_status", None)
        if config_version_status != "active":
            reason = (
                "Blackbox V2 config version_status is "
                f"{config_version_status}, expected active"
            )
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
        try:
            from shared.blackbox_v2.lifecycle import assert_lifecycle_clear

            cfg_path = getattr(cfg, "path", None)
            project_root = Path(cfg_path).parents[1] if cfg_path is not None else Path(__file__).resolve().parents[1]
            assert_lifecycle_clear(project_root, cfg.scheme_id)
        except RuntimeError as exc:
            reason = str(exc)
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
        approval = read_blackbox_execution_approval(engine, cfg)
        if not approval.executable:
            reason = f"Blackbox V2 version is not production-approved: {approval.reason}"
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "failed", 0, duration, reason)
    else:
        ok, reason = _verify_scheme_activation(engine, cfg.scheme_id, scheme_version)
        if not ok:
            duration = time.monotonic() - started
            write_run_log(engine, cfg.scheme_id, predict_date, "skipped", duration, reason)
            engine.dispose()
            return SchemeRunResult(cfg.scheme_id, "skipped", 0, duration, reason)
    active_targets = _active_registry_targets(engine, cfg.scheme_id)

    run_id: int | None = None
    records_returned: int | None = None
    records_written = 0
    try:
        run_id = create_scheme_run(
            engine,
            scheme_id=cfg.scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            run_type="active",
            prediction_phase=prediction_phase,
            records_expected=len(active_targets),
        )
        if not active_targets:
            raise ValueError(
                f"active registry targets empty for scheme {cfg.scheme_id}: "
                "missing=[], extra=[], duplicates=[]"
            )
        effective_timeout_sec = _effective_timeout_sec(cfg, timeout_sec)
        records = run_configured_scheme(
            cfg,
            predict_date,
            engine=engine,
            algo_env=algo_env,
            timeout_sec=effective_timeout_sec,
        )
        records_returned = len(records)
        if getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2":
            snapshot_ids = {
                str((record.extra or {}).get("data_snapshot_id"))
                for record in records
                if (record.extra or {}).get("data_snapshot_id")
            }
            if len(snapshot_ids) != 1:
                raise ValueError(
                    f"Blackbox V2 run must return exactly one data_snapshot_id, got {sorted(snapshot_ids)}"
                )
            attach_run_data_snapshot(
                engine,
                run_id=run_id,
                data_snapshot_id=next(iter(snapshot_ids)),
            )
        records = _normalize_live_records(records, prediction_phase=prediction_phase)
        _validate_live_record_dates(records, cfg=cfg, predict_date=predict_date, engine=engine)
        _validate_records_against_active_registry(records, cfg=cfg, active_targets=active_targets)
        if runtime_type == "blackbox_v2":
            records_written = insert_approved_blackbox_predictions(
                engine,
                cfg,
                run_id,
                records,
                scheme_version=scheme_version,
            )
        else:
            current_active_targets = _active_registry_targets(engine, cfg.scheme_id)
            if current_active_targets != active_targets:
                raise ValueError(
                    "active registry targets changed during run: "
                    f"initial={sorted(active_targets)}, current={sorted(current_active_targets)}"
                )
            records_written = insert_run_predictions(
                engine,
                run_id,
                records,
                scheme_version=scheme_version,
            )
        duration = time.monotonic() - started
        expected = len(active_targets)
        status = "success" if expected == records_returned == records_written else "partial"
        error_msg = (
            None
            if status == "success"
            else f"expected={expected}, returned={records_returned}, written={records_written}"
        )
        finish_scheme_run(
            engine,
            run_id=run_id,
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_msg,
        )
        try:
            write_run_log(engine, cfg.scheme_id, predict_date, status, duration, error_msg, run_id=run_id)
        except Exception:
            logger.exception("failed to write run log for completed scheme run_id=%s", run_id)
        return SchemeRunResult(cfg.scheme_id, status, records_written, duration, error_msg, run_id)
    except Exception as exc:
        duration = time.monotonic() - started
        error_msg = str(exc)
        if run_id is not None:
            try:
                finish_scheme_run(
                    engine,
                    run_id=run_id,
                    status="failed",
                    records_returned=records_returned,
                    records_written=records_written,
                    error_message=error_msg,
                )
            except Exception as audit_exc:
                logger.exception("failed to finish failed scheme run_id=%s", run_id)
                error_msg = _append_audit_error(error_msg, "finish_scheme_run", audit_exc)
        try:
            write_run_log(engine, cfg.scheme_id, predict_date, "failed", duration, error_msg, run_id=run_id)
        except Exception as audit_exc:
            logger.exception("failed to write run log for failed scheme run_id=%s", run_id)
            error_msg = _append_audit_error(error_msg, "write_run_log", audit_exc)
        return SchemeRunResult(cfg.scheme_id, "failed", records_written, duration, error_msg, run_id)
    finally:
        engine.dispose()


def _append_audit_error(error_msg: str, operation: str, exc: Exception) -> str:
    """保留原始错误并追加 best-effort 审计失败信息。"""
    return f"{error_msg}; {operation} audit failed: {exc}"


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
    """确保 live 返回 target multiset 与 active registry target set 一致。"""
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

    returned_targets = Counter((str(record.target_tenor), int(record.horizon)) for record in records)
    returned_target_set = set(returned_targets)
    missing = sorted(active_targets - returned_target_set)
    extra = sorted(returned_target_set - active_targets)
    duplicates = sorted(
        (target_tenor, horizon, count)
        for (target_tenor, horizon), count in returned_targets.items()
        if count > 1
    )
    if missing or extra or duplicates:
        raise ValueError(
            f"live target mismatch: missing={missing}, extra={extra}, duplicates={duplicates}"
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
