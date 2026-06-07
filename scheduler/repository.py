from __future__ import annotations

import json
from dataclasses import asdict
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

from scheduler.discovery import SchemeConfig
from shared.db_config import DatabaseConfig
from shared.models import ActualRecord, PredictionRecord, WeeklyActualRecord


def create_engine_from_env() -> Engine:
    """创建 SQLAlchemy Engine。"""
    cfg = DatabaseConfig.from_env()
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        query={"charset": cfg.charset},
    )
    return create_engine(url, future=True)


def sync_scheme_registry(engine: Engine, schemes: Iterable[SchemeConfig]) -> None:
    """将配置文件中的方案元数据同步到 t_scheme_registry。"""
    sql = text(
        """
        INSERT INTO t_scheme_registry
            (scheme_id, name, description, horizon, tenors, frequency, schedule_cron, schedule_timezone, status)
        VALUES
            (:scheme_id, :name, :description, :horizon, CAST(:tenors AS JSON), :frequency,
             :schedule_cron, :schedule_timezone, :status)
        ON DUPLICATE KEY UPDATE
            updated_at = IF(
                NOT (
                    name <=> VALUES(name)
                    AND description <=> VALUES(description)
                    AND horizon <=> VALUES(horizon)
                    AND CAST(tenors AS CHAR) <=> CAST(VALUES(tenors) AS CHAR)
                    AND frequency <=> VALUES(frequency)
                    AND schedule_cron <=> VALUES(schedule_cron)
                    AND schedule_timezone <=> VALUES(schedule_timezone)
                    AND status <=> VALUES(status)
                ),
                CURRENT_TIMESTAMP,
                updated_at
            ),
            name = VALUES(name),
            description = VALUES(description),
            horizon = VALUES(horizon),
            tenors = VALUES(tenors),
            frequency = VALUES(frequency),
            schedule_cron = VALUES(schedule_cron),
            schedule_timezone = VALUES(schedule_timezone),
            status = VALUES(status)
        """
    )
    rows = [
        {
            "scheme_id": cfg.scheme_id,
            "name": cfg.name,
            "description": cfg.description,
            "horizon": cfg.horizon,
            "tenors": json.dumps(cfg.tenors, ensure_ascii=False),
            "frequency": cfg.frequency,
            "schedule_cron": cfg.schedule.cron,
            "schedule_timezone": cfg.schedule.timezone,
            "status": cfg.status,
        }
        for cfg in schemes
    ]
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(sql, rows)


def upsert_predictions(engine: Engine, records: Iterable[PredictionRecord]) -> int:
    """UPSERT 预测记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_predictions
            (scheme_id, target_tenor, horizon, predict_date, target_date,
             predicted_direction, confidence, model_version, extra)
        VALUES
            (:scheme_id, :target_tenor, :horizon, :predict_date, :target_date,
             :predicted_direction, :confidence, :model_version, CAST(:extra AS JSON))
        ON DUPLICATE KEY UPDATE
            horizon = VALUES(horizon),
            target_date = VALUES(target_date),
            predicted_direction = VALUES(predicted_direction),
            confidence = VALUES(confidence),
            model_version = VALUES(model_version),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def upsert_actuals(engine: Engine, records: Iterable[ActualRecord]) -> int:
    """UPSERT 实际方向记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_actuals
            (tenor, trade_date, close_yield, direction_1d, direction_5d)
        VALUES
            (:tenor, :trade_date, :close_yield, :direction_1d, :direction_5d)
        ON DUPLICATE KEY UPDATE
            close_yield = VALUES(close_yield),
            direction_1d = VALUES(direction_1d),
            direction_5d = VALUES(direction_5d),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = [asdict(record) for record in records]
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def upsert_weekly_actuals(engine: Engine, records: Iterable[WeeklyActualRecord]) -> int:
    """UPSERT 周度实际方向记录。"""
    sql = text(
        """
        INSERT INTO t_scheme_weekly_actuals
            (tenor, feature_week_id, target_week_id, predict_date, feature_date, target_date,
             feature_yield, target_yield, direction_weekly, price_signal, target_rule, extra)
        VALUES
            (:tenor, :feature_week_id, :target_week_id, :predict_date, :feature_date, :target_date,
             :feature_yield, :target_yield, :direction_weekly, :price_signal, :target_rule, CAST(:extra AS JSON))
        ON DUPLICATE KEY UPDATE
            feature_week_id = VALUES(feature_week_id),
            target_week_id = VALUES(target_week_id),
            feature_date = VALUES(feature_date),
            target_date = VALUES(target_date),
            feature_yield = VALUES(feature_yield),
            target_yield = VALUES(target_yield),
            direction_weekly = VALUES(direction_weekly),
            price_signal = VALUES(price_signal),
            target_rule = VALUES(target_rule),
            extra = VALUES(extra),
            updated_at = CURRENT_TIMESTAMP
        """
    )
    rows = []
    for record in records:
        row = asdict(record)
        row["extra"] = json.dumps(record.extra or {}, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def write_run_log(
    engine: Engine,
    scheme_id: str,
    run_date: str,
    status: str,
    duration_sec: float | None = None,
    error_msg: str | None = None,
) -> None:
    """写入方案运行日志。"""
    sql = text(
        """
        INSERT INTO t_scheme_run_log (scheme_id, run_date, status, duration_sec, error_msg)
        VALUES (:scheme_id, :run_date, :status, :duration_sec, :error_msg)
        """
    )
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "scheme_id": scheme_id,
                "run_date": run_date,
                "status": status,
                "duration_sec": duration_sec,
                "error_msg": error_msg,
            },
        )
