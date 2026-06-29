from __future__ import annotations

import json
from dataclasses import asdict
from typing import Iterable

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, URL

from scheduler.discovery import SchemeConfig
from shared.db_config import DatabaseConfig
from shared.input_artifacts import InputArtifact
from shared.models import ActualRecord, PredictionRecord, WeeklyActualRecord


VALID_PREDICTION_PHASES = {"gray_live", "scheduled_live"}


def registry_scheme_id(base_scheme_id: str, horizon: int, target_tenor: str) -> str:
    """生成前端/业务层唯一方案 ID。"""
    return f"{base_scheme_id}__h{int(horizon)}__{target_tenor}"


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
    scheme_list = list(schemes)
    sql = text(
        """
        INSERT INTO t_scheme_registry
            (scheme_id, base_scheme_id, name, description, horizon, task_type, tenors, frequency, target_tenor,
             schedule_cron, schedule_timezone, status, deployed_at)
        VALUES
            (:scheme_id, :base_scheme_id, :name, :description, :horizon, :task_type, CAST(:tenors AS JSON), :frequency,
             :target_tenor, :schedule_cron, :schedule_timezone, :status,
             IF(:status = 'active', CURRENT_DATE, NULL))
        ON DUPLICATE KEY UPDATE
            updated_at = IF(
                NOT (
                    base_scheme_id <=> VALUES(base_scheme_id)
                    AND
                    name <=> VALUES(name)
                    AND description <=> VALUES(description)
                    AND horizon <=> VALUES(horizon)
                    AND task_type <=> VALUES(task_type)
                    AND CAST(tenors AS CHAR) <=> CAST(VALUES(tenors) AS CHAR)
                    AND frequency <=> VALUES(frequency)
                    AND target_tenor <=> VALUES(target_tenor)
                    AND schedule_cron <=> VALUES(schedule_cron)
                    AND schedule_timezone <=> VALUES(schedule_timezone)
                    AND status <=> VALUES(status)
                ),
                CURRENT_TIMESTAMP,
                updated_at
            ),
            base_scheme_id = VALUES(base_scheme_id),
            name = VALUES(name),
            description = VALUES(description),
            horizon = VALUES(horizon),
            task_type = VALUES(task_type),
            tenors = VALUES(tenors),
            frequency = VALUES(frequency),
            target_tenor = VALUES(target_tenor),
            schedule_cron = VALUES(schedule_cron),
            schedule_timezone = VALUES(schedule_timezone),
            status = VALUES(status),
            deployed_at = IF(deployed_at IS NULL AND VALUES(status) = 'active', CURRENT_DATE, deployed_at)
        """
    )
    rows = []
    active_registry_ids_by_base: dict[str, list[str]] = {}
    for cfg in scheme_list:
        registry_ids: list[str] = []
        for target_tenor in cfg.tenors:
            row_scheme_id = registry_scheme_id(cfg.scheme_id, cfg.horizon, target_tenor)
            registry_ids.append(row_scheme_id)
            rows.append(
                {
                    "scheme_id": row_scheme_id,
                    "base_scheme_id": cfg.scheme_id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "horizon": cfg.horizon,
                    "task_type": cfg.task_type,
                    "tenors": json.dumps([target_tenor], ensure_ascii=False),
                    "frequency": cfg.frequency,
                    "target_tenor": target_tenor,
                    "schedule_cron": cfg.schedule.cron,
                    "schedule_timezone": cfg.schedule.timezone,
                    "status": cfg.status,
                }
            )
        active_registry_ids_by_base[cfg.scheme_id] = registry_ids
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(sql, rows)
        for base_scheme_id, registry_ids in active_registry_ids_by_base.items():
            placeholders = ", ".join(f":scheme_id_{index}" for index, _ in enumerate(registry_ids))
            params = {"base_scheme_id": base_scheme_id}
            for index, scheme_id in enumerate(registry_ids):
                params[f"scheme_id_{index}"] = scheme_id
            conn.execute(
                text(
                    f"""
                    UPDATE t_scheme_registry
                    SET status = 'archived',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE base_scheme_id = :base_scheme_id
                      AND scheme_id NOT IN ({placeholders})
                      AND status <> 'archived'
                    """
                ),
                params,
            )
    for cfg in scheme_list:
        if getattr(cfg, "scheme_version", None):
            upsert_scheme_version(engine, cfg)


def upsert_scheme_version(engine: Engine, cfg: SchemeConfig) -> str:
    """将发现到的方案版本写入 t_scheme_versions，保持幂等。"""
    version_statuses = {"draft", "validated", "shadow", "active", "paused", "retired"}
    status = cfg.status if cfg.status in version_statuses else "draft"
    sql = text(
        """
        INSERT INTO t_scheme_versions
            (scheme_id, scheme_version, code_hash, config_hash, manifest_hash, git_commit, status, created_by)
        VALUES
            (:scheme_id, :scheme_version, :code_hash, :config_hash, :manifest_hash, :git_commit, :status, :created_by)
        ON DUPLICATE KEY UPDATE
            code_hash = VALUES(code_hash),
            config_hash = VALUES(config_hash),
            manifest_hash = VALUES(manifest_hash),
            git_commit = VALUES(git_commit),
            status = VALUES(status)
        """
    )
    params = {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "code_hash": cfg.code_hash,
        "config_hash": cfg.config_hash,
        "manifest_hash": cfg.manifest_hash,
        "git_commit": None,
        "status": status,
        "created_by": "scheduler.discovery",
    }
    with engine.begin() as conn:
        conn.execute(sql, params)
    return cfg.scheme_version


def create_scheme_run(
    engine: Engine,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    run_type: str = "active",
    prediction_phase: str | None = None,
    status: str = "running",
    harness_run_id: str | None = None,
    input_artifact_id: str | None = None,
    records_expected: int | None = None,
) -> int:
    """创建一次不可变预测运行记录，返回 run_id。"""
    if prediction_phase is not None and prediction_phase not in VALID_PREDICTION_PHASES:
        raise ValueError(f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)}, got {prediction_phase}")
    sql = text(
        """
        INSERT INTO t_scheme_runs
            (scheme_id, scheme_version, run_type, prediction_phase, predict_date, status,
             harness_run_id, input_artifact_id, records_expected)
        VALUES
            (:scheme_id, :scheme_version, :run_type, :prediction_phase, :predict_date, :status,
             :harness_run_id, :input_artifact_id, :records_expected)
        """
    )
    params = {
        "scheme_id": scheme_id,
        "scheme_version": scheme_version,
        "run_type": run_type,
        "prediction_phase": prediction_phase,
        "predict_date": predict_date,
        "status": status,
        "harness_run_id": harness_run_id,
        "input_artifact_id": input_artifact_id,
        "records_expected": records_expected,
    }
    with engine.begin() as conn:
        result = conn.execute(sql, params)
        run_id = getattr(result, "lastrowid", None)
        if run_id is None:
            run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
    return int(run_id)


def finish_scheme_run(
    engine: Engine,
    *,
    run_id: int,
    status: str,
    records_returned: int | None = None,
    records_written: int | None = None,
    error_message: str | None = None,
) -> None:
    """标记预测运行结束。"""
    sql = text(
        """
        UPDATE t_scheme_runs
        SET status = :status,
            finished_at = CURRENT_TIMESTAMP,
            records_returned = :records_returned,
            records_written = :records_written,
            error_message = :error_message
        WHERE run_id = :run_id
        """
    )
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "run_id": run_id,
                "status": status,
                "records_returned": records_returned,
                "records_written": records_written,
                "error_message": error_message,
            },
        )


def insert_run_predictions(
    engine: Engine,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None = None,
) -> int:
    """UPSERT 预测记录，按 UK (scheme_id, target_tenor, horizon, target_date) 覆盖。"""
    sql = text(
        """
        INSERT INTO t_scheme_predictions
            (run_id, scheme_version, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
             prediction_phase, predicted_direction, confidence, model_version, extra)
        VALUES
            (:run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :prediction_phase, :predicted_direction, :confidence, :model_version, CAST(:extra AS JSON))
        ON DUPLICATE KEY UPDATE
            run_id = VALUES(run_id),
            scheme_version = VALUES(scheme_version),
            predict_date = VALUES(predict_date),
            feature_date = VALUES(feature_date),
            prediction_phase = VALUES(prediction_phase),
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
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(f"feature_date is required for prediction record {record.scheme_id}/{record.target_tenor}")
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                f"anchor_date must equal feature_date for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        phase = record.prediction_phase or extra.get("prediction_phase")
        if phase not in VALID_PREDICTION_PHASES:
            raise ValueError(
                f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)} "
                f"for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        extra["feature_date"] = str(feature_date)
        extra["prediction_phase"] = str(phase)
        row["run_id"] = record.run_id if record.run_id is not None else run_id
        row["scheme_version"] = record.scheme_version if record.scheme_version is not None else scheme_version
        row["feature_date"] = str(feature_date)
        row["prediction_phase"] = str(phase)
        row["extra"] = json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def upsert_input_artifact(engine: Engine, artifact: InputArtifact) -> str:
    """UPSERT 输入产物指纹，返回稳定 artifact_id。"""
    predict_date = artifact.metadata.get("predict_date")
    if not predict_date:
        raise ValueError("InputArtifact.metadata must include predict_date")

    coverage = artifact.date_coverage or {}
    if coverage.get("field") == "date":
        min_date = coverage.get("start")
        max_date = coverage.get("end")
    else:
        min_date = None
        max_date = None

    sql = text(
        """
        INSERT INTO t_input_artifacts
            (artifact_id, scheme_id, scheme_version, predict_date, frequency,
             data_version, artifact_uri, content_hash, schema_hash, source_watermark,
             row_count, min_date, max_date)
        VALUES
            (:artifact_id, :scheme_id, :scheme_version, :predict_date, :frequency,
             :data_version, :artifact_uri, :content_hash, :schema_hash, :source_watermark,
             :row_count, :min_date, :max_date)
        ON DUPLICATE KEY UPDATE
            scheme_version = VALUES(scheme_version),
            data_version = VALUES(data_version),
            artifact_uri = VALUES(artifact_uri),
            schema_hash = VALUES(schema_hash),
            source_watermark = VALUES(source_watermark),
            row_count = VALUES(row_count),
            min_date = VALUES(min_date),
            max_date = VALUES(max_date)
        """
    )
    params = {
        "artifact_id": artifact.artifact_id,
        "scheme_id": artifact.scheme_id,
        "scheme_version": artifact.metadata.get("scheme_version"),
        "predict_date": str(predict_date),
        "frequency": artifact.frequency,
        "data_version": artifact.data_version,
        "artifact_uri": str(artifact.path),
        "content_hash": artifact.content_hash,
        "schema_hash": artifact.schema_hash,
        "source_watermark": artifact.source_watermark,
        "row_count": artifact.row_count,
        "min_date": min_date,
        "max_date": max_date,
    }
    with engine.begin() as conn:
        conn.execute(sql, params)
    return artifact.artifact_id


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
        _assert_weekly_actuals_target_rule_unique_key(conn)
        conn.execute(sql, rows)
    return len(rows)


def _assert_weekly_actuals_target_rule_unique_key(conn) -> None:
    """确认周度 actual 唯一键包含 target_rule，避免 point/average 互相覆盖。"""
    expected = ("tenor", "predict_date", "target_rule")
    legacy = ("tenor", "predict_date")
    indexes = inspect(conn).get_indexes("t_scheme_weekly_actuals")
    unique_columns = [
        tuple(index.get("column_names") or ())
        for index in indexes
        if bool(index.get("unique"))
    ]
    if expected not in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals missing unique key "
            "uk_weekly_actual_predict_rule(tenor,predict_date,target_rule); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )
    if legacy in unique_columns:
        raise RuntimeError(
            "t_scheme_weekly_actuals still has legacy unique key on (tenor,predict_date); "
            "run migrations/014_weekly_average_actuals.sql before weekly actual writes"
        )


def write_run_log(
    engine: Engine,
    scheme_id: str,
    run_date: str,
    status: str,
    duration_sec: float | None = None,
    error_msg: str | None = None,
    run_id: int | None = None,
) -> None:
    """写入方案运行日志。"""
    sql = text(
        """
        INSERT INTO t_scheme_run_log (run_id, scheme_id, run_date, status, duration_sec, error_msg)
        VALUES (:run_id, :scheme_id, :run_date, :status, :duration_sec, :error_msg)
        """
    )
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "run_id": run_id,
                "scheme_id": scheme_id,
                "run_date": run_date,
                "status": status,
                "duration_sec": duration_sec,
                "error_msg": error_msg,
            },
        )
