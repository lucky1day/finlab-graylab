from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine


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


def upsert_backtest_run(
    engine: Engine,
    *,
    benchmark_id: str,
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    status: str,
    summary: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> int:
    """创建或更新一次历史复现 run，并返回 run_id。"""
    sql = text(
        """
        INSERT INTO t_backtest_runs
            (benchmark_id, scheme_id, data_source, start_date, end_date, status, summary, report_path)
        VALUES
            (:benchmark_id, :scheme_id, :data_source, :start_date, :end_date, :status,
             CAST(:summary AS JSON), :report_path)
        ON DUPLICATE KEY UPDATE
            status = VALUES(status),
            summary = VALUES(summary),
            report_path = VALUES(report_path),
            updated_at = CURRENT_TIMESTAMP,
            id = LAST_INSERT_ID(id)
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
    }
    with engine.begin() as conn:
        conn.execute(sql, params)
        return int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one())


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


def replace_backtest_monthly_metrics(engine: Engine, run_id: int, rows: Iterable[dict[str, Any]]) -> int:
    """替换某个 run 的月度指标。"""
    materialized = list(rows)
    delete_sql = text("DELETE FROM t_backtest_monthly_metrics WHERE run_id = :run_id")
    insert_sql = text(
        """
        INSERT INTO t_backtest_monthly_metrics
            (run_id, benchmark_id, scheme_id, target_tenor, horizon, month,
             sample_count, correct_count, accuracy, up_precision, up_recall,
             down_precision, down_recall, actual_dist, predicted_dist)
        VALUES
            (:run_id, :benchmark_id, :scheme_id, :target_tenor, :horizon, :month,
             :sample_count, :correct_count, :accuracy, :up_precision, :up_recall,
             :down_precision, :down_recall, CAST(:actual_dist AS JSON), CAST(:predicted_dist AS JSON))
        """
    )
    with engine.begin() as conn:
        conn.execute(delete_sql, {"run_id": run_id})
        if materialized:
            conn.execute(insert_sql, [_metric_params(run_id, row) for row in materialized])
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
