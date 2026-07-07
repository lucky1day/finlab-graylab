from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

from shared.db_config import DatabaseConfig


def clean_json(value: Any) -> Any:
    """把 numpy/pandas 类型、日期和 NaN 转成 MySQL JSON 可接受的值。"""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if hasattr(value, "item"):
        try:
            return clean_json(value.item())
        except Exception:
            pass
    return str(value)


def json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(clean_json(value), ensure_ascii=False)


def create_engine_from_env() -> Engine:
    """创建回测写库/读库 Engine。"""
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


def create_backtest_run(
    engine: Engine,
    *,
    benchmark_id: str,
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    status: str = "running",
    summary: dict[str, Any] | None = None,
    report_path: str | None = None,
    code_hash: str | None = None,
    config_hash: str | None = None,
    input_artifact_hash: str | None = None,
    run_mode: str = "persist",
) -> int:
    """追加一次不可变历史复现 run，并返回 backtest_run_id。"""
    insert_sql = text(
        """
        INSERT INTO t_backtest_runs
            (benchmark_id, scheme_id, data_source, start_date, end_date, status,
             summary, report_path, code_hash, config_hash, input_artifact_hash, run_mode)
        VALUES
            (:benchmark_id, :scheme_id, :data_source, :start_date, :end_date, :status,
             CAST(:summary AS JSON), :report_path, :code_hash, :config_hash,
             :input_artifact_hash, :run_mode)
        """
    )
    update_sql = text(
        """
        UPDATE t_backtest_runs
        SET backtest_run_id = :run_id
        WHERE id = :run_id
          AND backtest_run_id IS NULL
        """
    )
    params = {
        "benchmark_id": benchmark_id,
        "scheme_id": scheme_id,
        "data_source": data_source,
        "start_date": start_date,
        "end_date": end_date,
        "status": status,
        "summary": json_dumps(summary or {}),
        "report_path": report_path,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "input_artifact_hash": input_artifact_hash,
        "run_mode": run_mode,
    }
    with engine.begin() as conn:
        result = conn.execute(insert_sql, params)
        run_id = getattr(result, "lastrowid", None)
        if run_id is None:
            run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
        conn.execute(update_sql, {"run_id": int(run_id)})
    return int(run_id)


def update_backtest_run_summary(
    engine: Engine,
    *,
    run_id: int,
    status: str,
    summary: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> None:
    """更新当前不可变 run 的状态和 summary。"""
    sql = text(
        """
        UPDATE t_backtest_runs
        SET status = :status,
            summary = CAST(:summary AS JSON),
            report_path = :report_path,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :run_id
        """
    )
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "run_id": run_id,
                "status": status,
                "summary": json_dumps(summary or {}),
                "report_path": report_path,
            },
        )


def latest_backtest_run_id(
    engine: Engine,
    *,
    benchmark_id: str,
    scheme_id: str,
    data_source: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int | None:
    """读取 canonical latest success backtest_run_id。"""
    filters = [
        "benchmark_id = :benchmark_id",
        "scheme_id = :scheme_id",
        "data_source = :data_source",
    ]
    params: dict[str, Any] = {
        "benchmark_id": benchmark_id,
        "scheme_id": scheme_id,
        "data_source": data_source,
    }
    if start_date is not None:
        filters.append("start_date = :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        filters.append("end_date = :end_date")
        params["end_date"] = end_date
    sql = text(
        f"""
        SELECT backtest_run_id
        FROM v_latest_backtest_run
        WHERE {" AND ".join(filters)}
        ORDER BY id DESC
        LIMIT 1
        """
    )
    with engine.begin() as conn:
        value = conn.execute(sql, params).scalar_one_or_none()
    return int(value) if value is not None else None


def replace_backtest_predictions(engine: Engine, run_id: int, rows: Iterable[dict[str, Any]]) -> int:
    """替换某个 run 的逐日预测明细。"""
    materialized = list(rows)
    delete_sql = text("DELETE FROM t_backtest_predictions WHERE run_id = :run_id")
    insert_sql = text(
        """
        INSERT INTO t_backtest_predictions
            (run_id, benchmark_id, scheme_id, target_tenor, horizon, predict_date,
             feature_date, target_date, label, predicted_direction, model_pred,
             confidence, source_row, extra)
        VALUES
            (:run_id, :benchmark_id, :scheme_id, :target_tenor, :horizon, :predict_date,
             :feature_date, :target_date, :label, :predicted_direction, :model_pred,
             :confidence, CAST(:source_row AS JSON), CAST(:extra AS JSON))
        """
    )
    with engine.begin() as conn:
        conn.execute(delete_sql, {"run_id": run_id})
        if materialized:
            conn.execute(insert_sql, [_prediction_params(run_id, row) for row in materialized])
    return len(materialized)


def insert_reproduction_check(engine: Engine, row: dict[str, Any]) -> int:
    """写入一次数据复现检查结果。"""
    sql = text(
        """
        INSERT INTO t_backtest_reproduction_checks
            (benchmark_id, check_name, status, source_path, row_count_csv, row_count_db,
             col_count_csv, col_count_db, date_min_csv, date_max_csv, date_min_db, date_max_db,
             csv_only_columns, db_only_columns, target_max_abs_diff, overall_max_abs_diff,
             missing_diff_count, first_diff, report)
        VALUES
            (:benchmark_id, :check_name, :status, :source_path, :row_count_csv, :row_count_db,
             :col_count_csv, :col_count_db, :date_min_csv, :date_max_csv, :date_min_db, :date_max_db,
             CAST(:csv_only_columns AS JSON), CAST(:db_only_columns AS JSON),
             CAST(:target_max_abs_diff AS JSON), :overall_max_abs_diff,
             :missing_diff_count, CAST(:first_diff AS JSON), CAST(:report AS JSON))
        """
    )
    params = dict(row)
    for key in ("csv_only_columns", "db_only_columns", "target_max_abs_diff", "first_diff", "report"):
        params[key] = json_dumps(params.get(key))
    with engine.begin() as conn:
        result = conn.execute(sql, params)
        return int(result.lastrowid or 0)


def _prediction_params(run_id: int, row: dict[str, Any]) -> dict[str, Any]:
    params = dict(row)
    params["run_id"] = run_id
    params["source_row"] = json_dumps(params.get("source_row") or {})
    params["extra"] = json_dumps(params.get("extra") or {})
    params["label"] = _int_or_none(params.get("label"))
    params["predicted_direction"] = _int_or_none(params.get("predicted_direction"))
    params["model_pred"] = _int_or_none(params.get("model_pred"))
    params["confidence"] = _float_or_none(params.get("confidence"))
    return params


def _metric_params(run_id: int, row: dict[str, Any]) -> dict[str, Any]:
    params = dict(row)
    params["run_id"] = run_id
    params["actual_dist"] = json_dumps(params.get("actual_dist") or {})
    params["predicted_dist"] = json_dumps(params.get("predicted_dist") or {})
    for key in ("accuracy", "up_precision", "up_recall", "down_precision", "down_recall"):
        params[key] = _float_or_none(params.get(key))
    return params


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result
