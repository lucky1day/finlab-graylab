from __future__ import annotations

import argparse
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.daily_actuals_updater import update_actuals
from scheduler.calendar import is_trading_day
from scheduler.discovery import SchemeConfig, discover_schemes
from scheduler.executor import DEFAULT_ALGO_ENV, execute_scheme
from scheduler.repository import create_engine_from_env, sync_scheme_registry


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


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
    if not force and not _is_trading_day(predict_date):
        logger.info("Skip %s on non-trading day %s", scheme_id, predict_date)
        return
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
    if not force and not _is_trading_day(target_date):
        logger.info("Skip actuals on non-trading day %s", target_date)
        return
    written = update_actuals(end_date=target_date)
    logger.info("Actuals refresh finished: date=%s records=%s", target_date, written)


def build_scheduler(algo_env: str = DEFAULT_ALGO_ENV) -> BlockingScheduler:
    """创建 APScheduler 实例。"""
    schemes = discover_schemes()
    _sync_registry(schemes)
    scheduler = BlockingScheduler(timezone=ASIA_SHANGHAI)

    for cfg in schemes:
        if cfg.status != "active":
            continue
        scheduler.add_job(
            run_prediction_job,
            trigger=_cron_trigger(cfg.schedule.cron, cfg.schedule.timezone),
            args=[cfg.scheme_id],
            kwargs={"algo_env": algo_env, "force": cfg.frequency == "weekly"},
            id=f"predict:{cfg.scheme_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        logger.info("Scheduled scheme %s at %s", cfg.scheme_id, cfg.schedule.cron)

    scheduler.add_job(
        run_actuals_job,
        trigger=CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=ASIA_SHANGHAI),
        id="actuals",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled actuals refresh at 16:00 Asia/Shanghai")
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
