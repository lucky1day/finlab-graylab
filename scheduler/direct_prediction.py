"""后端手动触发复用的单方案预测辅助。

本模块不注册 cron、不启动常驻 scheduler，也不承担 Registry 同步、DataBridge 发布或
startup catch-up。正式 ``scheduled_live`` 自然写入仍由对应 launchd one-shot runner
负责；这里仅保留 backend 直调所需的精确准入与执行闭包。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import logging
import os
import threading
from zoneinfo import ZoneInfo

from sqlalchemy import text

from scheduler.blackbox_scheduler_admission import (
    DIRECT_SCHEDULED,
    ScheduledPredictionConfigurationError,
    ScheduledPredictionControlPlaneDenied,
    require_scheduled_prediction_control_plane,
    uses_blackbox_scheduler_admission,
)
from scheduler.calendar import is_trading_day
from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, SchemeRunResult, execute_scheme
from scheduler.repository import create_engine_from_env
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.data_bridge.refresh import DataBridgeRefreshConfig


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
PREDICTION_MAX_CONCURRENCY_ENV = "BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY"
DEFAULT_PREDICTION_MAX_CONCURRENCY = 1
PLATFORM_CONFIGURATION_ERROR_PREFIX = "platform configuration error:"
logger = logging.getLogger(__name__)
_prediction_semaphore_lock = threading.Lock()
_prediction_semaphore_limit: int | None = None
_prediction_semaphore: threading.BoundedSemaphore | None = None


class DirectScheduledPredictionDenied(RuntimeError):
    """直接 ``scheduled_live`` 入口未通过精确控制面准入。"""


def _today() -> str:
    return datetime.now(ASIA_SHANGHAI).date().isoformat()


def _normalize_run_date(value: str | date | None) -> str:
    if value is None:
        return _today()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _env_int(name: str, default: int, *, min_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer >= {min_value}, got {raw!r}"
        ) from exc
    if value < min_value:
        raise ValueError(f"{name} must be an integer >= {min_value}, got {value}")
    return value


def _configure_prediction_semaphore(max_concurrency: int) -> None:
    global _prediction_semaphore, _prediction_semaphore_limit
    with _prediction_semaphore_lock:
        if (
            _prediction_semaphore is None
            or _prediction_semaphore_limit != max_concurrency
        ):
            _prediction_semaphore = threading.BoundedSemaphore(max_concurrency)
            _prediction_semaphore_limit = max_concurrency


def _current_prediction_semaphore() -> threading.BoundedSemaphore:
    max_concurrency = _env_int(
        PREDICTION_MAX_CONCURRENCY_ENV,
        DEFAULT_PREDICTION_MAX_CONCURRENCY,
        min_value=1,
    )
    _configure_prediction_semaphore(max_concurrency)
    assert _prediction_semaphore is not None
    return _prediction_semaphore


@contextmanager
def _prediction_slot():
    semaphore = _current_prediction_semaphore()
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


def _is_trading_day(run_date: str) -> bool:
    engine = create_engine_from_env()
    try:
        return is_trading_day(engine, run_date)
    finally:
        engine.dispose()


def _previous_trading_day(run_date: str) -> str:
    engine = create_engine_from_env()
    try:
        sql = text(
            """
            SELECT MAX(rdate)
            FROM t_trade_calendar
            WHERE trade_flag = '1'
              AND rdate < :rdate
            """
        )
        with engine.connect() as conn:
            value = conn.execute(sql, {"rdate": run_date}).scalar()
        if value is None:
            raise ValueError(f"no previous trading day before {run_date}")
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]
    finally:
        engine.dispose()


def _uses_blackbox_scheduler_admission(config: object) -> bool:
    """Blackbox runtime 和冻结 base ID 都属于 admission 控制域。"""
    return uses_blackbox_scheduler_admission(config)


def _direct_scheduled_lifecycle_denial(
    config: SchemeConfig,
) -> str | None:
    """拒绝 inactive 方案或 Blackbox 版本，不读取 admission。"""
    if config.status != "active":
        return (
            "direct_scheduled denied because scheme is not active: "
            f"scheme_id={config.scheme_id} status={config.status}"
        )
    if (
        _uses_blackbox_scheduler_admission(config)
        and getattr(config, "version_status", None) != "active"
    ):
        return (
            "direct_scheduled denied because Blackbox version is not "
            f"active: identity={config.scheme_id}@"
            f"{getattr(config, 'scheme_version', '')} "
            f"version_status={getattr(config, 'version_status', None)}"
        )
    return None


def require_direct_scheduled_prediction_config(
    scheme_id: str,
) -> SchemeConfig:
    """解析并校验直接 ``scheduled_live`` 单方案入口。"""
    try:
        return require_scheduled_prediction_config(
            scheme_id,
            plane=DIRECT_SCHEDULED,
        )
    except (
        ScheduledPredictionConfigurationError,
        ScheduledPredictionControlPlaneDenied,
    ) as exc:
        raise DirectScheduledPredictionDenied(str(exc)) from exc


def require_scheduled_prediction_config(
    scheme_id: str,
    *,
    plane: str,
) -> SchemeConfig:
    """按精确身份校验指定 scheduled 控制面并返回 canonical config。"""
    config = resolve_scheduled_prediction_config(scheme_id)
    require_scheduled_prediction_control_plane(config, plane=plane)
    return config


def resolve_scheduled_prediction_config(
    scheme_id: str,
) -> SchemeConfig:
    """发现 active canonical config，并验证版本生命周期。"""
    config = next(
        (
            candidate
            for candidate in discover_schemes()
            if candidate.scheme_id == scheme_id
        ),
        None,
    )
    if config is None:
        raise ScheduledPredictionConfigurationError(
            f"scheme not found: {scheme_id}"
        )
    lifecycle_denial = _direct_scheduled_lifecycle_denial(config)
    if lifecycle_denial is not None:
        raise ScheduledPredictionConfigurationError(lifecycle_denial)
    return config


def _platform_configuration_failure(
    scheme_id: str,
    detail: str,
) -> SchemeRunResult:
    return SchemeRunResult(
        scheme_id,
        "failed",
        0,
        0.0,
        f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} {detail}",
    )


def _skips_non_trading_day(cfg: SchemeConfig) -> bool:
    return cfg.frequency not in {"weekly", "monthly"}


def _run_prediction_config(
    cfg: SchemeConfig,
    predict_date: str,
    *,
    algo_env: str,
    force: bool,
) -> SchemeRunResult:
    """使用已发现的方案配置执行单次预测。"""
    if (
        not force
        and _skips_non_trading_day(cfg)
        and not _is_trading_day(predict_date)
    ):
        logger.info("Skip %s on non-trading day %s", cfg.scheme_id, predict_date)
        return SchemeRunResult(
            cfg.scheme_id,
            "skipped",
            0,
            0.0,
            "non-trading day",
        )
    if (
        getattr(cfg, "runtime_type", "native_adapter") == "blackbox_v2"
        and getattr(cfg, "input_source", None) == "data_bridge_current"
    ):
        try:
            require_v2_daily_ready(
                DataBridgeRefreshConfig.from_env(),
                predict_date,
                _previous_trading_day(predict_date),
            )
        except V2DailyGateBlocked as exc:
            logger.error(
                "V2 scheduled prediction blocked: scheme=%s date=%s error=%s",
                cfg.scheme_id,
                predict_date,
                exc,
            )
            return SchemeRunResult(
                cfg.scheme_id,
                "skipped",
                0,
                0.0,
                str(exc),
            )
    with _prediction_slot():
        result = execute_scheme(cfg, predict_date, algo_env=algo_env)
    logger.info("Scheme run finished: %s", result)
    return result


def run_prediction_job(
    scheme_id: str,
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> SchemeRunResult:
    """执行 backend 手动触发的单方案预测。"""
    predict_date = _normalize_run_date(run_date)
    try:
        cfg = require_direct_scheduled_prediction_config(scheme_id)
    except DirectScheduledPredictionDenied as exc:
        logger.error(
            "Direct scheduled prediction denied: scheme=%s error=%s",
            scheme_id,
            exc,
        )
        return _platform_configuration_failure(scheme_id, str(exc))
    return _run_prediction_config(
        cfg,
        predict_date,
        algo_env=algo_env,
        force=force,
    )
