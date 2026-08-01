from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
import threading
from datetime import date, datetime, time
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import (
    ThreadPoolExecutor as APSchedulerThreadPoolExecutor,
)
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from scheduler.blackbox_scheduler_admission import (
    DIRECT_SCHEDULED,
    LEGACY_AUTOMATIC,
    RESERVED_BLACKBOX_SCHEME_IDS,
    BlackboxSchedulerAdmissionError,
    BlackboxSchedulerAdmissionPolicy,
    ScheduledPredictionConfigurationError,
    ScheduledPredictionControlPlaneDenied,
    load_blackbox_scheduler_admission,
    require_scheduled_prediction_control_plane,
    uses_blackbox_scheduler_admission,
)
from scheduler.daily_direct_authority import (
    DailyDirectAuthorityError,
    build_daily_direct_cache_authorities,
)
from scheduler.daily_actuals_updater import update_actuals
from scheduler.monthly_actuals_updater import update_monthly_actuals
from scheduler.daily_policy import POLICY_V2_PATH
from scheduler.weekly_actuals_updater import update_weekly_actuals
from scheduler.calendar import is_trading_day
from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, SchemeRunResult, execute_scheme
from scheduler.repository import create_engine_from_env, sync_scheme_registry
from scheduler.v2_daily_gate import V2DailyGateBlocked, require_v2_daily_ready
from shared.data_bridge.client import DataBridgeClient, DataBridgeClientConfig
from shared.data_bridge.authority import (
    resolve_databridge_continuity_authority_from_engine,
)
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    check_current_dataset,
    run_full_refresh,
)
from shared.daily_coordinator_mode import (
    DAILY_COORDINATOR_MODE_ENV,
    bootstrap_deployment_daily_coordinator_mode,
)
from shared.daily_storage_preflight import (
    DailyStoragePreflightError,
    preflight_daily_storage,
)
from shared.source_runtime_database import (
    SOURCE_RUNTIME_SCHEME_IDS,
    SourceRuntimeDatabasePreflightError,
    load_source_runtime_database_config,
    preflight_source_runtime_database_access,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
STAGGER_MINUTES_ENV = "BOND_SCHEDULER_STAGGER_MINUTES"
PREDICTION_MAX_CONCURRENCY_ENV = "BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY"
STARTUP_CATCHUP_ENV = "BOND_SCHEDULER_STARTUP_CATCHUP"
DEFAULT_STAGGER_MINUTES = 2
DEFAULT_PREDICTION_MAX_CONCURRENCY = 1
DEFAULT_DATA_BRIDGE_REFRESH_START = "06:30"
DAILY_RECOVERY_NOT_BEFORE = time(6, 30)
DAILY_RECOVERY_CUTOFF = time(8, 30)
PLATFORM_CONFIGURATION_ERROR_PREFIX = "platform configuration error:"
logger = logging.getLogger(__name__)
_prediction_semaphore_lock = threading.Lock()
_prediction_semaphore_limit: int | None = None
_prediction_semaphore: threading.BoundedSemaphore | None = None


@dataclass(frozen=True)
class StaggeredPredictionJob:
    """实际注册到 scheduler 的预测任务。"""

    cfg: SchemeConfig
    base_cron: str
    effective_cron: str
    offset_minutes: int


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


def _sync_registry(schemes: list[SchemeConfig]) -> None:
    engine = create_engine_from_env()
    try:
        sync_scheme_registry(engine, schemes)
    finally:
        engine.dispose()


def _preflight_source_runtime_database(
    schemes: Sequence[SchemeConfig],
) -> None:
    """在任何 Registry/任务副作用前验证 source-readonly 绑定。"""
    if not any(
        cfg.scheme_id in SOURCE_RUNTIME_SCHEME_IDS
        for cfg in schemes
    ):
        return
    try:
        config = load_source_runtime_database_config()
    except RuntimeError:
        raise SourceRuntimeDatabasePreflightError(
            "SOURCE_DB_CONFIG_INVALID"
        ) from None
    preflight_source_runtime_database_access(config)


def _env_int(name: str, default: int, *, min_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer >= {min_value}, got {raw!r}") from exc
    if value < min_value:
        raise ValueError(f"{name} must be an integer >= {min_value}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _daily_coordinator_mode() -> str:
    return bootstrap_deployment_daily_coordinator_mode()


def _require_ledger_runtime_mode() -> None:
    """外层只拒绝 mode 漂移；确定性 authority 由 runtime 内层执行。"""
    if _daily_coordinator_mode() != "ledger":
        raise RuntimeError(
            "daily ledger entry requires ledger coordinator mode"
        )


def _configure_prediction_semaphore(max_concurrency: int) -> None:
    global _prediction_semaphore, _prediction_semaphore_limit
    with _prediction_semaphore_lock:
        if _prediction_semaphore is None or _prediction_semaphore_limit != max_concurrency:
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


def _translate_cron_day_of_week(value: str) -> str:
    mapping = {
        "0": "sun",
        "7": "sun",
        "1": "mon",
        "2": "tue",
        "3": "wed",
        "4": "thu",
        "5": "fri",
        "6": "sat",
    }
    translated = []
    for item in value.split(","):
        if "-" in item:
            start, end = item.split("-", 1)
            translated.append(f"{mapping.get(start, start)}-{mapping.get(end, end)}")
        else:
            translated.append(mapping.get(item, item))
    return ",".join(translated)


def _offset_cron_expr(cron_expr: str, offset_minutes: int) -> str:
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"unsupported cron expression: {cron_expr}")
    minute, hour, day, month, day_of_week = parts
    if not minute.isdigit() or not hour.isdigit():
        raise ValueError(f"cron expression must use numeric minute and hour: {cron_expr}")
    if offset_minutes < 0:
        raise ValueError(f"cron offset must be >= 0, got {offset_minutes}")
    minute_int = int(minute)
    hour_int = int(hour)
    if minute_int > 59 or hour_int > 23:
        raise ValueError(f"cron expression has invalid minute/hour: {cron_expr}")

    total_minutes = hour_int * 60 + minute_int + offset_minutes
    if total_minutes >= 24 * 60:
        raise ValueError(
            f"stagger offset crosses a natural day: cron={cron_expr} offset_minutes={offset_minutes}"
        )
    effective_hour, effective_minute = divmod(total_minutes, 60)
    return f"{effective_minute} {effective_hour} {day} {month} {day_of_week}"


def _cron_trigger(cron_expr: str, timezone: str) -> CronTrigger:
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"unsupported cron expression: {cron_expr}")
    minute, hour, day, month, day_of_week = parts
    day_of_week = _translate_cron_day_of_week(day_of_week)
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=ZoneInfo(timezone),
    )


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    for item in field.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_raw, end_raw = item.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            if start <= value <= end:
                return True
            continue
        if int(item) == value:
            return True
    return False


def _cron_day_of_week_matches(field: str, value: int) -> bool:
    if field == "*":
        return True

    def parse_day(raw: str) -> int:
        normalized = raw.strip().lower()
        aliases = {
            "mon": 0,
            "tue": 1,
            "wed": 2,
            "thu": 3,
            "fri": 4,
            "sat": 5,
            "sun": 6,
        }
        if normalized in aliases:
            return aliases[normalized]
        numeric = int(normalized)
        if numeric in {0, 7}:
            return 6
        return numeric - 1

    for item in field.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_raw, end_raw = item.split("-", 1)
            start = parse_day(start_raw)
            end = parse_day(end_raw)
            if start <= value <= end:
                return True
            continue
        if parse_day(item) == value:
            return True
    return False


def _scheduled_datetime_for_date(cron_expr: str, run_date: date, timezone: str) -> datetime | None:
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"unsupported cron expression: {cron_expr}")
    minute, hour, day, month, day_of_week = parts
    if not minute.isdigit() or not hour.isdigit():
        raise ValueError(f"cron expression must use numeric minute and hour: {cron_expr}")
    run_at = datetime.combine(run_date, time(int(hour), int(minute)), tzinfo=ZoneInfo(timezone))
    if not _cron_field_matches(day, run_at.day):
        return None
    if not _cron_field_matches(month, run_at.month):
        return None
    if not _cron_day_of_week_matches(day_of_week, run_at.weekday()):
        return None
    return run_at


def _staggered_prediction_jobs(
    schemes: Iterable[SchemeConfig],
    interval_minutes: int,
) -> list[StaggeredPredictionJob]:
    groups: dict[tuple[str, str], list[SchemeConfig]] = defaultdict(list)
    for cfg in schemes:
        if cfg.status != "active":
            continue
        groups[(cfg.schedule.cron, cfg.schedule.timezone)].append(cfg)

    jobs: list[StaggeredPredictionJob] = []
    for base_cron, timezone in sorted(groups):
        for index, cfg in enumerate(sorted(groups[(base_cron, timezone)], key=lambda item: item.scheme_id)):
            offset_minutes = index * interval_minutes
            jobs.append(
                StaggeredPredictionJob(
                    cfg=cfg,
                    base_cron=base_cron,
                    effective_cron=_offset_cron_expr(base_cron, offset_minutes),
                    offset_minutes=offset_minutes,
                )
            )
    return jobs


def _automatic_prediction_schemes(
    schemes: Iterable[SchemeConfig],
) -> list[SchemeConfig]:
    """保留 Native，并仅放行 formal 精确身份的 Blackbox。"""
    configs = list(schemes)
    controlled_configs = [
        config
        for config in configs
        if _uses_blackbox_scheduler_admission(config)
    ]
    if not controlled_configs:
        return configs
    try:
        policy = load_blackbox_scheduler_admission()
    except BlackboxSchedulerAdmissionError as exc:
        logger.critical(
            "Blackbox automatic scheduling denied for all discovered "
            "Blackbox schemes because admission is invalid: %s",
            exc,
        )
        return [
            config
            for config in configs
            if not _uses_blackbox_scheduler_admission(config)
        ]
    admitted: list[SchemeConfig] = []
    for config in configs:
        if policy.allows(
            config,
            plane=LEGACY_AUTOMATIC,
        ):
            admitted.append(config)
        else:
            _log_blackbox_identity_drift(config, policy)
    return admitted


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


def _direct_scheduled_denial(
    config: SchemeConfig,
    *,
    policy: BlackboxSchedulerAdmissionPolicy | None = None,
    policy_error: BlackboxSchedulerAdmissionError | None = None,
) -> str | None:
    """返回 direct-scheduled 拒绝原因；非受控 Native 不读取 policy。"""
    lifecycle_denial = _direct_scheduled_lifecycle_denial(
        config
    )
    if lifecycle_denial is not None:
        return lifecycle_denial
    if not _uses_blackbox_scheduler_admission(config):
        return None
    identity = (
        f"{config.scheme_id}@"
        f"{getattr(config, 'scheme_version', '')}"
    )
    if policy_error is not None:
        return (
            "direct_scheduled denied because Blackbox admission is "
            f"invalid: identity={identity} error={policy_error}"
        )
    if policy is None:
        try:
            policy = load_blackbox_scheduler_admission()
        except BlackboxSchedulerAdmissionError as exc:
            return (
                "direct_scheduled denied because Blackbox admission is "
                f"invalid: identity={identity} error={exc}"
            )
    if policy.allows(config, plane=DIRECT_SCHEDULED):
        return None
    mode = policy.mode(config) or "unlisted"
    return (
        "direct_scheduled denied by exact Blackbox admission: "
        f"identity={identity} mode={mode}"
    )


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
    require_scheduled_prediction_control_plane(
        config,
        plane=plane,
    )
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
    lifecycle_denial = _direct_scheduled_lifecycle_denial(
        config
    )
    if lifecycle_denial is not None:
        raise ScheduledPredictionConfigurationError(
            lifecycle_denial
        )
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


def _log_blackbox_identity_drift(
    config: object,
    policy: BlackboxSchedulerAdmissionPolicy,
) -> None:
    """对冻结身份的 runtime/version 漂移记录 critical 拒绝证据。"""
    scheme_id = str(getattr(config, "scheme_id", "")).strip()
    if scheme_id not in RESERVED_BLACKBOX_SCHEME_IDS:
        return
    runtime_type = getattr(
        config,
        "runtime_type",
        "native_adapter",
    )
    if runtime_type != "blackbox_v2":
        reason = (
            "runtime_type drift: expected=blackbox_v2 "
            f"actual={runtime_type}"
        )
    elif policy.mode(config) is None:
        reason = (
            "scheme_version drift: "
            f"actual={getattr(config, 'scheme_version', '')}"
        )
    else:
        return
    logger.critical(
        "Reserved Blackbox scheduler identity denied: "
        "scheme=%s %s",
        scheme_id,
        reason,
    )


def _startup_prediction_catchup_due_jobs(
    schemes: Iterable[SchemeConfig],
    *,
    now: datetime,
    interval_minutes: int,
) -> list[StaggeredPredictionJob]:
    """返回今天调度时间已过、适合启动时补跑的 active 预测任务。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ASIA_SHANGHAI)
    due_jobs: list[StaggeredPredictionJob] = []
    for job in _staggered_prediction_jobs(schemes, interval_minutes):
        scheduled_at = _scheduled_datetime_for_date(
            job.effective_cron,
            now.astimezone(ZoneInfo(job.cfg.schedule.timezone)).date(),
            job.cfg.schedule.timezone,
        )
        if scheduled_at is None:
            continue
        if scheduled_at <= now.astimezone(scheduled_at.tzinfo):
            due_jobs.append(job)
    return due_jobs


def _prediction_run_exists(engine, scheme_id: str, predict_date: str) -> bool:
    sql = text(
        """
        SELECT 1
        FROM t_scheme_runs
        WHERE scheme_id = :scheme_id
          AND predict_date = :predict_date
          AND prediction_phase = 'scheduled_live'
          AND status IN ('success', 'partial', 'failed', 'skipped')
        LIMIT 1
        """
    )
    with engine.begin() as conn:
        return conn.execute(sql, {"scheme_id": scheme_id, "predict_date": predict_date}).first() is not None


def run_startup_prediction_catchup(
    *,
    now: datetime | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
) -> list[SchemeRunResult]:
    """启动时补跑今天已错过且没有运行记录的预测任务。"""
    run_now = now or datetime.now(ASIA_SHANGHAI)
    if run_now.tzinfo is None:
        run_now = run_now.replace(tzinfo=ASIA_SHANGHAI)
    stagger_minutes = _env_int(STAGGER_MINUTES_ENV, DEFAULT_STAGGER_MINUTES, min_value=0)
    schemes = discover_schemes()
    _sync_registry(schemes)
    due_jobs = _startup_prediction_catchup_due_jobs(
        _automatic_prediction_schemes(schemes),
        now=run_now,
        interval_minutes=stagger_minutes,
    )
    if not due_jobs:
        logger.info("Startup prediction catchup: no due jobs")
        return []

    results: list[SchemeRunResult] = []
    engine = create_engine_from_env()
    try:
        for job in due_jobs:
            predict_date = run_now.astimezone(ZoneInfo(job.cfg.schedule.timezone)).date().isoformat()
            if _prediction_run_exists(engine, job.cfg.scheme_id, predict_date):
                logger.info(
                    "Startup prediction catchup skipped existing run: scheme=%s predict_date=%s",
                    job.cfg.scheme_id,
                    predict_date,
                )
                results.append(
                    SchemeRunResult(job.cfg.scheme_id, "skipped", 0, 0.0, "existing run")
                )
                continue
            logger.warning(
                "Startup prediction catchup running missed job: scheme=%s predict_date=%s scheduled_cron=%s",
                job.cfg.scheme_id,
                predict_date,
                job.effective_cron,
            )
            results.append(
                _run_prediction_config(
                    job.cfg,
                    predict_date,
                    algo_env=algo_env,
                    force=False,
                )
            )
    finally:
        engine.dispose()

    failures = [result for result in results if result.status in {"failed", "partial"}]
    if failures:
        summary = ", ".join(f"{result.scheme_id}={result.status}" for result in failures)
        raise RuntimeError(f"Startup prediction catchup failed: {summary}")
    return results


def run_data_bridge_refresh_job(
    run_date: str | date | None = None,
    *,
    enforce_deadline: bool = True,
):
    """全量重建并发布当天 DataBridge 三频文件。"""
    refresh_date = _normalize_run_date(run_date)
    expected_daily_date = _previous_trading_day(refresh_date)
    client = DataBridgeClient(DataBridgeClientConfig.from_env())
    config = DataBridgeRefreshConfig.from_env()
    engine = create_engine_from_env()
    try:
        continuity_authority = (
            resolve_databridge_continuity_authority_from_engine(
                config,
                feature_date=expected_daily_date,
                engine=engine,
            )
        )
    finally:
        engine.dispose()
    result = run_full_refresh(
        client=client,
        config=config,
        expected_daily_date=expected_daily_date,
        refresh_date=refresh_date,
        publish=True,
        deadline_at=config.deadline_at(refresh_date) if enforce_deadline else None,
        continuity_authority=continuity_authority,
    )
    logger.info(
        "DataBridge refresh finished: date=%s generation=%s rounds=%s duration_sec=%.1f",
        refresh_date,
        result.state.get("generation_id"),
        result.rounds_completed,
        result.duration_sec,
    )
    return result


def data_bridge_refresh_is_current(run_date: str | date | None = None) -> bool:
    refresh_date = _normalize_run_date(run_date)
    expected_daily_date = _previous_trading_day(refresh_date)
    check_current_dataset(
        DataBridgeRefreshConfig.from_env(),
        required_refresh_date=refresh_date,
        expected_daily_date=expected_daily_date,
    )
    return True


def run_startup_tasks(
    *,
    now: datetime | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
) -> None:
    """启动时先补齐三频 current，再补跑已到期预测。"""
    run_now = now or datetime.now(ASIA_SHANGHAI)
    if run_now.tzinfo is None:
        run_now = run_now.replace(tzinfo=ASIA_SHANGHAI)
    start_text = os.getenv("DATABRIDGE_REFRESH_START", DEFAULT_DATA_BRIDGE_REFRESH_START)
    try:
        start_at = time.fromisoformat(start_text)
    except ValueError as exc:
        raise ValueError(f"DATABRIDGE_REFRESH_START must use HH:MM, got {start_text!r}") from exc
    refresh_date = run_now.date().isoformat()
    if run_now.timetz().replace(tzinfo=None) >= start_at:
        try:
            needs_refresh = not data_bridge_refresh_is_current(refresh_date)
        except Exception as exc:
            logger.warning("Startup DataBridge current check requires refresh: %s", exc)
            needs_refresh = True
        try:
            if needs_refresh:
                run_data_bridge_refresh_job(refresh_date)
        except Exception:
            logger.exception("Startup DataBridge refresh failed; data_bridge_current schemes remain blocked")
    run_startup_prediction_catchup(now=run_now, algo_env=algo_env)


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
    if not force and _skips_non_trading_day(cfg) and not _is_trading_day(predict_date):
        logger.info("Skip %s on non-trading day %s", cfg.scheme_id, predict_date)
        return SchemeRunResult(cfg.scheme_id, "skipped", 0, 0.0, "non-trading day")
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
            return SchemeRunResult(cfg.scheme_id, "skipped", 0, 0.0, str(exc))
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
    """执行单个方案调度任务。"""
    predict_date = _normalize_run_date(run_date)
    try:
        cfg = require_direct_scheduled_prediction_config(
            scheme_id
        )
    except DirectScheduledPredictionDenied as exc:
        logger.error(
            "Direct scheduled prediction denied: scheme=%s error=%s",
            scheme_id,
            exc,
        )
        return _platform_configuration_failure(
            scheme_id,
            str(exc),
        )
    if (
        cfg.frequency == "daily"
        and _daily_coordinator_mode() != "legacy"
    ):
        raise RuntimeError(
            "direct daily prediction is disabled; use the ledger coordinator"
        )
    return _run_prediction_config(
        cfg,
        predict_date,
        algo_env=algo_env,
        force=force,
    )


def run_scheduled_prediction_job(
    scheme_id: str,
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> SchemeRunResult:
    """执行 APScheduler 预测任务，并向调度器暴露失败状态。"""
    predict_date = _normalize_run_date(run_date)
    scheduled_config = next(
        (
            config
            for config in discover_schemes()
            if config.scheme_id == scheme_id
        ),
        None,
    )
    if scheduled_config is None:
        logger.error("Scheme not found: %s", scheme_id)
        result = SchemeRunResult(
            scheme_id,
            "failed",
            0,
            0.0,
            f"{PLATFORM_CONFIGURATION_ERROR_PREFIX} "
            f"scheme not found: {scheme_id}",
        )
    else:
        result = None
    if (
        scheduled_config is not None
        and _uses_blackbox_scheduler_admission(
            scheduled_config
        )
    ):
        identity = (
            f"{scheduled_config.scheme_id}@"
            f"{getattr(scheduled_config, 'scheme_version', '')}"
        )
        try:
            policy = load_blackbox_scheduler_admission()
        except BlackboxSchedulerAdmissionError as exc:
            logger.critical(
                "Blackbox automatic scheduling denied because admission "
                "is invalid: identity=%s error=%s",
                identity,
                exc,
            )
            raise BlackboxSchedulerAdmissionError(
                "Blackbox automatic scheduling denied: "
                f"{identity}; invalid admission: {exc}"
            ) from exc
        if not policy.allows(
            scheduled_config,
            plane=LEGACY_AUTOMATIC,
        ):
            mode = policy.mode(scheduled_config) or "unlisted"
            _log_blackbox_identity_drift(
                scheduled_config,
                policy,
            )
            raise BlackboxSchedulerAdmissionError(
                "Blackbox automatic scheduling denied: "
                f"{identity} mode={mode}"
            )
    if (
        scheduled_config is not None
        and scheduled_config.frequency == "daily"
        and _daily_coordinator_mode() != "legacy"
    ):
        raise RuntimeError(
            "legacy scheduled daily prediction is disabled in ledger mode"
        )
    if scheduled_config is not None:
        result = _run_prediction_config(
            scheduled_config,
            predict_date,
            algo_env=algo_env,
            force=force,
        )
    assert result is not None
    if result.status in {"failed", "partial"}:
        detail = f": {result.error_msg}" if result.error_msg else ""
        raise RuntimeError(f"Scheduled prediction {result.status}: {scheme_id}{detail}")
    return result


def run_daily_coordinator_job(
    run_date: str | date | None = None,
    *,
    algo_env: str = DEFAULT_ALGO_ENV,
    trigger_origin: str = "apscheduler",
):
    """延迟导入 ledger runtime，确保旧回滚模式不初始化新写入路径。"""
    _require_ledger_runtime_mode()
    from scheduler.daily_runtime import run_daily_occurrence

    return run_daily_occurrence(
        run_date=run_date,
        algo_env=algo_env,
        trigger_origin=trigger_origin,
    )


def run_daily_recovery_tick_job(
    run_date: str | date | None = None,
    *,
    algo_env: str = DEFAULT_ALGO_ENV,
    now: datetime | None = None,
):
    """在恢复窗口内周期性重入同一协调器，弥补单次 job 异常。

    该入口不创建第二套队列；真正的单 owner、冻结 occurrence、attempt
    fence 与 08:30 截止仍由 ``run_daily_occurrence`` 强制执行。
    """
    checked_at = now or datetime.now(ASIA_SHANGHAI)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=ASIA_SHANGHAI)
    local_now = checked_at.astimezone(ASIA_SHANGHAI)
    predict_date = (
        local_now.date().isoformat()
        if run_date is None
        else _normalize_run_date(run_date)
    )
    inside_window = (
        local_now.date().isoformat() == predict_date
        and DAILY_RECOVERY_NOT_BEFORE
        <= local_now.time().replace(tzinfo=None)
        < DAILY_RECOVERY_CUTOFF
    )
    if inside_window:
        return run_daily_coordinator_job(
            run_date=predict_date,
            algo_env=algo_env,
            trigger_origin="startup_catchup",
        )
    after_cutoff_same_day = (
        local_now.date().isoformat() == predict_date
        and local_now.time().replace(tzinfo=None)
        >= DAILY_RECOVERY_CUTOFF
    )
    if after_cutoff_same_day:
        return run_daily_watchdog_job(
            "recovery_cutoff",
            run_date=predict_date,
        )
    else:
        return {
            "status": "outside_recovery_window",
            "predict_date": predict_date,
        }


def run_daily_watchdog_job(
    stage: str,
    run_date: str | date | None = None,
):
    """执行日批进度/guardrail/SLA/cutoff 控制动作。"""
    _require_ledger_runtime_mode()
    from scheduler.daily_runtime import run_daily_watchdog

    return run_daily_watchdog(stage=stage, run_date=run_date)


def run_daily_heartbeat_job(
    run_date: str | date | None = None,
):
    """独立刷新日批心跳，避免长任务占满预测执行池造成假失联。"""
    _require_ledger_runtime_mode()
    from scheduler.daily_runtime import run_scheduler_heartbeat

    return run_scheduler_heartbeat(run_date=run_date)


def run_daily_operator_recovery_job(
    scheme_id: str,
    run_date: str | date | None = None,
    *,
    algo_env: str = DEFAULT_ALGO_ENV,
):
    """将 admin 单方案恢复请求路由到同一 occurrence 协调器。"""
    _require_ledger_runtime_mode()
    from scheduler.daily_runtime import run_operator_recovery

    return run_operator_recovery(
        scheme_id=scheme_id,
        run_date=run_date,
        algo_env=algo_env,
    )


def run_all_prediction_jobs(
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> list[SchemeRunResult]:
    """执行全部 active 方案调度任务。"""
    predict_date = _normalize_run_date(run_date)
    schemes = [
        config
        for config in discover_schemes()
        if config.status == "active"
    ]
    ledger_mode = _daily_coordinator_mode() != "legacy"
    preliminary_denials: dict[int, str] = {}
    for config in schemes:
        lifecycle_denial = _direct_scheduled_lifecycle_denial(
            config
        )
        if lifecycle_denial is not None:
            preliminary_denials[id(config)] = lifecycle_denial
        elif ledger_mode and config.frequency == "daily":
            preliminary_denials[id(config)] = (
                "direct_scheduled daily prediction is disabled in ledger "
                f"mode: scheme_id={config.scheme_id}"
            )
    controlled = [
        config
        for config in schemes
        if id(config) not in preliminary_denials
        if _uses_blackbox_scheduler_admission(config)
    ]
    policy: BlackboxSchedulerAdmissionPolicy | None = None
    policy_error: BlackboxSchedulerAdmissionError | None = None
    if controlled:
        try:
            policy = load_blackbox_scheduler_admission()
        except BlackboxSchedulerAdmissionError as exc:
            policy_error = exc
    results: list[SchemeRunResult] = []
    for cfg in schemes:
        denial = preliminary_denials.get(id(cfg))
        if denial is None:
            denial = _direct_scheduled_denial(
                cfg,
                policy=policy,
                policy_error=policy_error,
            )
        if denial is not None:
            logger.error(
                "Direct scheduled prediction denied: scheme=%s error=%s",
                cfg.scheme_id,
                denial,
            )
            results.append(
                _platform_configuration_failure(
                    cfg.scheme_id,
                    denial,
                )
            )
            continue
        results.append(
            _run_prediction_config(
                cfg,
                predict_date,
                algo_env=algo_env,
                force=force,
            )
        )
    return results


def _prediction_exit_code(results: Sequence[SchemeRunResult]) -> int:
    if any(
        result.status == "failed"
        and (result.error_msg or "").startswith(PLATFORM_CONFIGURATION_ERROR_PREFIX)
        for result in results
    ):
        return 2
    if any(result.status in {"failed", "partial"} for result in results):
        return 1
    return 0


def _daily_runtime_exit_code(result: object) -> int:
    """把单 occurrence admin 结果映射为稳定退出码。"""
    status = str(getattr(result, "status", "unknown"))
    return 0 if status in {"complete", "non_trading_day"} else 1


def _emit_daily_runtime_result(result: object, exit_code: int) -> None:
    """输出 ledger admin 入口的 JSON-safe 单 occurrence 摘要。"""
    payload = {
        "event": "daily_occurrence_run",
        "business_date": getattr(result, "business_date", None),
        "occurrence_id": getattr(result, "occurrence_id", None),
        "status": getattr(result, "status", "unknown"),
        "dispatched_scheme_ids": list(
            getattr(result, "dispatched_scheme_ids", ()) or ()
        ),
        "exit_code": int(exit_code),
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _emit_prediction_summary(results: Sequence[SchemeRunResult], exit_code: int) -> None:
    counts = Counter(result.status for result in results)
    summary = {
        "event": "prediction_run_summary",
        "counts": {
            status: counts[status]
            for status in ("success", "failed", "partial", "skipped")
        },
        "exit_code": exit_code,
        "total": len(results),
    }
    print(json.dumps(summary, sort_keys=True))


def run_actuals_job(run_date: str | date | None = None, force: bool = False) -> None:
    """执行 actuals 刷新任务。"""
    target_date = _normalize_run_date(run_date)
    is_trading_day = _is_trading_day(target_date)
    if not force and not is_trading_day:
        daily_weekly_end_date = _previous_trading_day(target_date)
        logger.info(
            "Refresh daily/weekly actuals to previous trading day %s on non-trading day %s; "
            "monthly actuals still refresh to %s",
            daily_weekly_end_date,
            target_date,
            target_date,
        )
    else:
        daily_weekly_end_date = target_date
    daily_written = update_actuals(end_date=daily_weekly_end_date)
    weekly_written = update_weekly_actuals(end_date=daily_weekly_end_date)
    monthly_written = update_monthly_actuals(end_date=target_date)
    logger.info(
        "Actuals refresh finished: date=%s daily_weekly_end_date=%s daily_records=%s weekly_records=%s "
        "monthly_records=%s",
        target_date,
        daily_weekly_end_date,
        daily_written,
        weekly_written,
        monthly_written,
    )


def build_scheduler(algo_env: str = DEFAULT_ALGO_ENV) -> BlockingScheduler:
    """创建 APScheduler 实例。"""
    coordinator_mode = _daily_coordinator_mode()
    stagger_minutes = _env_int(STAGGER_MINUTES_ENV, DEFAULT_STAGGER_MINUTES, min_value=0)
    max_concurrency = _env_int(
        PREDICTION_MAX_CONCURRENCY_ENV,
        DEFAULT_PREDICTION_MAX_CONCURRENCY,
        min_value=1,
    )
    _configure_prediction_semaphore(max_concurrency)
    schemes = discover_schemes()
    _preflight_source_runtime_database(schemes)
    if coordinator_mode == "ledger":
        authority_engine = create_engine_from_env()
        try:
            build_daily_direct_cache_authorities(
                authority_engine,
                policy_path=POLICY_V2_PATH,
                discovered=schemes,
                algo_env=algo_env,
            )
        finally:
            authority_engine.dispose()
    else:
        _sync_registry(schemes)
    scheduler = BlockingScheduler(
        timezone=ASIA_SHANGHAI,
        executors={
            "default": APSchedulerThreadPoolExecutor(max_workers=4),
            "daily_control": APSchedulerThreadPoolExecutor(max_workers=1),
            "daily_heartbeat": APSchedulerThreadPoolExecutor(max_workers=1),
            "daily_soft_watchdog": APSchedulerThreadPoolExecutor(
                max_workers=2
            ),
            "daily_sla": APSchedulerThreadPoolExecutor(max_workers=1),
            "daily_cutoff": APSchedulerThreadPoolExecutor(max_workers=1),
            "daily_recovery": APSchedulerThreadPoolExecutor(max_workers=1),
            "recurring_predictions": APSchedulerThreadPoolExecutor(
                max_workers=2
            ),
        },
    )

    automatic_schemes = _automatic_prediction_schemes(schemes)
    prediction_schemes = (
        automatic_schemes
        if coordinator_mode == "legacy"
        else [
            cfg
            for cfg in automatic_schemes
            if cfg.frequency != "daily"
        ]
    )
    for job in _staggered_prediction_jobs(
        prediction_schemes,
        stagger_minutes,
    ):
        cfg = job.cfg
        scheduler.add_job(
            run_scheduled_prediction_job,
            trigger=_cron_trigger(job.effective_cron, cfg.schedule.timezone),
            args=[cfg.scheme_id],
            kwargs={"algo_env": algo_env},
            id=f"predict:{cfg.scheme_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
            executor=(
                "recurring_predictions"
                if coordinator_mode == "ledger"
                else "default"
            ),
        )
        logger.info(
            "Scheduled scheme %s at %s -> %s (offset=%smin, max_concurrency=%s)",
            cfg.scheme_id,
            job.base_cron,
            job.effective_cron,
            job.offset_minutes,
            max_concurrency,
        )

    if coordinator_mode == "ledger":
        scheduler.add_job(
            run_daily_heartbeat_job,
            trigger=IntervalTrigger(
                seconds=30,
                timezone=ASIA_SHANGHAI,
            ),
            id="daily:heartbeat",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
            executor="daily_heartbeat",
        )
        scheduler.add_job(
            run_daily_coordinator_job,
            trigger=CronTrigger(
                hour=6,
                minute=30,
                day_of_week="mon-fri",
                timezone=ASIA_SHANGHAI,
            ),
            kwargs={
                "algo_env": algo_env,
                "trigger_origin": "apscheduler",
            },
            id="daily:coordinator",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=7200,
            executor="daily_control",
        )
        scheduler.add_job(
            run_daily_recovery_tick_job,
            trigger=CronTrigger(
                hour="6-8",
                minute="1-59/2",
                day_of_week="mon-fri",
                timezone=ASIA_SHANGHAI,
            ),
            kwargs={"algo_env": algo_env},
            id="daily:recovery-loop",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            executor="daily_recovery",
        )
        control_jobs = (
            (
                "databridge-readiness",
                6,
                55,
                "databridge_readiness_guardrail",
            ),
            ("watchdog", 7, 0, "progress"),
            ("v2-guardrail", 7, 45, "v2_start_guardrail"),
            ("sla", 8, 0, "target_sla"),
            ("recovery-cutoff", 8, 30, "recovery_cutoff"),
        )
        control_executor = {
            "databridge-readiness": "daily_soft_watchdog",
            "watchdog": "daily_soft_watchdog",
            "v2-guardrail": "daily_soft_watchdog",
            "sla": "daily_sla",
            "recovery-cutoff": "daily_cutoff",
        }
        for job_name, hour, minute, stage in control_jobs:
            scheduler.add_job(
                run_daily_watchdog_job,
                trigger=CronTrigger(
                    hour=hour,
                    minute=minute,
                    day_of_week="mon-fri",
                    timezone=ASIA_SHANGHAI,
                ),
                args=[stage],
                id=f"daily:{job_name}:{hour:02d}{minute:02d}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=1800,
                executor=control_executor[job_name],
            )
        logger.info(
            "Scheduled ledger daily coordinator at 06:30, a two-minute "
            "recovery loop through 08:29, and isolated "
            "06:55/07:00/07:45/08:00/08:30 control jobs"
        )

    if _env_bool(STARTUP_CATCHUP_ENV, True):
        startup_function = (
            run_daily_coordinator_job
            if coordinator_mode == "ledger"
            else run_startup_tasks
        )
        startup_kwargs = (
            {
                "algo_env": algo_env,
                "trigger_origin": "startup_catchup",
            }
            if coordinator_mode == "ledger"
            else {"algo_env": algo_env}
        )
        startup_id = (
            "startup:daily-occurrence-catchup"
            if coordinator_mode == "ledger"
            else "startup:data-refresh-and-prediction-catchup"
        )
        scheduler.add_job(
            startup_function,
            trigger=DateTrigger(run_date=datetime.now(ASIA_SHANGHAI), timezone=ASIA_SHANGHAI),
            kwargs=startup_kwargs,
            id=startup_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            executor=(
                "daily_control"
                if coordinator_mode == "ledger"
                else "default"
            ),
        )
        logger.info(
            "Scheduled startup %s catchup",
            "daily occurrence"
            if coordinator_mode == "ledger"
            else "DataBridge refresh and prediction",
        )
    return scheduler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bond Factor Lab scheduler.")
    parser.add_argument("--algo-env", default=os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV))
    parser.add_argument("--run-once", choices=["predictions", "actuals", "data-refresh"], default=None)
    parser.add_argument("--date", default=None, help="Run date in YYYY-MM-DD format")
    parser.add_argument("--scheme-id", default=None, help="Limit --run-once predictions to one scheme")
    parser.add_argument("--force", action="store_true", help="Run even when the date is not a trading day")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        coordinator_mode = _daily_coordinator_mode()
        if args.run_once in {"predictions", "data-refresh"}:
            preflight_daily_storage()
        if args.run_once == "predictions":
            if coordinator_mode == "ledger":
                if args.force:
                    raise ValueError(
                        "--force is not supported by the ledger daily "
                        "coordinator"
                    )
                if args.scheme_id:
                    daily_result = run_daily_operator_recovery_job(
                        args.scheme_id,
                        run_date=args.date,
                        algo_env=args.algo_env,
                    )
                else:
                    daily_result = run_daily_coordinator_job(
                        run_date=args.date,
                        algo_env=args.algo_env,
                        trigger_origin="operator_recovery",
                    )
                exit_code = _daily_runtime_exit_code(daily_result)
                _emit_daily_runtime_result(daily_result, exit_code)
                return exit_code
            if args.scheme_id:
                results = [
                    run_prediction_job(
                        args.scheme_id,
                        run_date=args.date,
                        algo_env=args.algo_env,
                        force=args.force,
                    )
                ]
            else:
                results = run_all_prediction_jobs(
                    run_date=args.date,
                    algo_env=args.algo_env,
                    force=args.force,
                )
            exit_code = _prediction_exit_code(results)
            _emit_prediction_summary(results, exit_code)
            return exit_code
        if args.run_once == "actuals":
            run_actuals_job(run_date=args.date, force=args.force)
            return 0
        if args.run_once == "data-refresh":
            if coordinator_mode == "ledger":
                data_bridge_refresh_is_current(args.date)
            else:
                run_data_bridge_refresh_job(
                    run_date=args.date,
                    enforce_deadline=False,
                )
            return 0

        preflight_daily_storage()
        scheduler = build_scheduler(algo_env=args.algo_env)
    except (
        ValueError,
        DailyDirectAuthorityError,
        DailyStoragePreflightError,
        SourceRuntimeDatabasePreflightError,
    ) as exc:
        if args.run_once == "predictions":
            _emit_prediction_summary([], 2)
        logger.error("Scheduler argument or configuration error: %s", exc)
        return 2

    logger.info("Starting scheduler")
    scheduler.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
