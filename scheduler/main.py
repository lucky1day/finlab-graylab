from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
import threading
from datetime import date, datetime, time
from typing import Iterable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import text

from scheduler.daily_actuals_updater import update_actuals
from scheduler.monthly_actuals_updater import update_monthly_actuals
from scheduler.weekly_actuals_updater import update_weekly_actuals
from scheduler.calendar import is_trading_day
from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, execute_scheme
from scheduler.repository import create_engine_from_env, sync_scheme_registry


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTUALS_REFRESH_TIMES = ((8, 30), (19, 0), (23, 45))
STAGGER_MINUTES_ENV = "BOND_SCHEDULER_STAGGER_MINUTES"
PREDICTION_MAX_CONCURRENCY_ENV = "BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY"
STARTUP_CATCHUP_ENV = "BOND_SCHEDULER_STARTUP_CATCHUP"
DEFAULT_STAGGER_MINUTES = 2
DEFAULT_PREDICTION_MAX_CONCURRENCY = 1
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


def _format_actuals_refresh_times() -> str:
    return ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in ACTUALS_REFRESH_TIMES)


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
) -> None:
    """启动时补跑今天已错过且没有运行记录的预测任务。"""
    run_now = now or datetime.now(ASIA_SHANGHAI)
    if run_now.tzinfo is None:
        run_now = run_now.replace(tzinfo=ASIA_SHANGHAI)
    stagger_minutes = _env_int(STAGGER_MINUTES_ENV, DEFAULT_STAGGER_MINUTES, min_value=0)
    schemes = discover_schemes()
    _sync_registry(schemes)
    due_jobs = _startup_prediction_catchup_due_jobs(
        schemes,
        now=run_now,
        interval_minutes=stagger_minutes,
    )
    if not due_jobs:
        logger.info("Startup prediction catchup: no due jobs")
        return

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
                continue
            logger.warning(
                "Startup prediction catchup running missed job: scheme=%s predict_date=%s scheduled_cron=%s",
                job.cfg.scheme_id,
                predict_date,
                job.effective_cron,
            )
            run_prediction_job(job.cfg.scheme_id, run_date=predict_date, algo_env=algo_env)
    finally:
        engine.dispose()


def _skips_non_trading_day(cfg: SchemeConfig) -> bool:
    return cfg.frequency not in {"weekly", "monthly"}


def run_prediction_job(
    scheme_id: str,
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> None:
    """执行单个方案调度任务。"""
    predict_date = _normalize_run_date(run_date)
    schemes = discover_schemes()
    _sync_registry(schemes)
    configs = {cfg.scheme_id: cfg for cfg in schemes}
    cfg = configs.get(scheme_id)
    if cfg is None:
        logger.error("Scheme not found: %s", scheme_id)
        return
    if not force and _skips_non_trading_day(cfg) and not _is_trading_day(predict_date):
        logger.info("Skip %s on non-trading day %s", scheme_id, predict_date)
        return
    with _prediction_slot():
        result = execute_scheme(cfg, predict_date, algo_env=algo_env)
    logger.info("Scheme run finished: %s", result)


def run_all_prediction_jobs(
    run_date: str | date | None = None,
    algo_env: str = DEFAULT_ALGO_ENV,
    force: bool = False,
) -> None:
    """执行全部 active 方案调度任务。"""
    schemes = discover_schemes()
    _sync_registry(schemes)
    for cfg in schemes:
        if cfg.status != "active":
            continue
        run_prediction_job(cfg.scheme_id, run_date=run_date, algo_env=algo_env, force=force)


def run_actuals_job(run_date: str | date | None = None, force: bool = False) -> None:
    """执行 actuals 刷新任务。"""
    target_date = _normalize_run_date(run_date)
    is_trading_day = _is_trading_day(target_date)
    if not force and not is_trading_day:
        logger.info("Skip daily/weekly actuals on non-trading day %s; monthly actuals still refresh", target_date)
        daily_written = 0
        weekly_written = 0
    else:
        daily_written = update_actuals(end_date=target_date)
        weekly_written = update_weekly_actuals(end_date=target_date)
    monthly_written = update_monthly_actuals(end_date=target_date)
    logger.info(
        "Actuals refresh finished: date=%s daily_records=%s weekly_records=%s monthly_records=%s",
        target_date,
        daily_written,
        weekly_written,
        monthly_written,
    )


def build_scheduler(algo_env: str = DEFAULT_ALGO_ENV) -> BlockingScheduler:
    """创建 APScheduler 实例。"""
    stagger_minutes = _env_int(STAGGER_MINUTES_ENV, DEFAULT_STAGGER_MINUTES, min_value=0)
    max_concurrency = _env_int(
        PREDICTION_MAX_CONCURRENCY_ENV,
        DEFAULT_PREDICTION_MAX_CONCURRENCY,
        min_value=1,
    )
    _configure_prediction_semaphore(max_concurrency)
    schemes = discover_schemes()
    _sync_registry(schemes)
    scheduler = BlockingScheduler(timezone=ASIA_SHANGHAI)

    for job in _staggered_prediction_jobs(schemes, stagger_minutes):
        cfg = job.cfg
        scheduler.add_job(
            run_prediction_job,
            trigger=_cron_trigger(job.effective_cron, cfg.schedule.timezone),
            args=[cfg.scheme_id],
            kwargs={"algo_env": algo_env},
            id=f"predict:{cfg.scheme_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        logger.info(
            "Scheduled scheme %s at %s -> %s (offset=%smin, max_concurrency=%s)",
            cfg.scheme_id,
            job.base_cron,
            job.effective_cron,
            job.offset_minutes,
            max_concurrency,
        )

    for hour, minute in ACTUALS_REFRESH_TIMES:
        scheduler.add_job(
            run_actuals_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=ASIA_SHANGHAI),
            id=f"actuals:{hour:02d}{minute:02d}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    logger.info("Scheduled actuals refresh at %s Asia/Shanghai", _format_actuals_refresh_times())
    if _env_bool(STARTUP_CATCHUP_ENV, True):
        scheduler.add_job(
            run_startup_prediction_catchup,
            trigger=DateTrigger(run_date=datetime.now(ASIA_SHANGHAI), timezone=ASIA_SHANGHAI),
            kwargs={"algo_env": algo_env},
            id="startup:prediction-catchup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("Scheduled startup prediction catchup")
    return scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Bond Factor Lab scheduler.")
    parser.add_argument("--algo-env", default=os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV))
    parser.add_argument("--run-once", choices=["predictions", "actuals"], default=None)
    parser.add_argument("--date", default=None, help="Run date in YYYY-MM-DD format")
    parser.add_argument("--scheme-id", default=None, help="Limit --run-once predictions to one scheme")
    parser.add_argument("--force", action="store_true", help="Run even when the date is not a trading day")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.run_once == "predictions":
        if args.scheme_id:
            run_prediction_job(args.scheme_id, run_date=args.date, algo_env=args.algo_env, force=args.force)
        else:
            run_all_prediction_jobs(run_date=args.date, algo_env=args.algo_env, force=args.force)
        return
    if args.run_once == "actuals":
        run_actuals_job(run_date=args.date, force=args.force)
        return

    scheduler = build_scheduler(algo_env=args.algo_env)
    logger.info("Starting scheduler")
    scheduler.start()


if __name__ == "__main__":
    main()
