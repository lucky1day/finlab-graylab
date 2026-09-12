from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, URL

from shared.db_config import DatabaseConfig

if TYPE_CHECKING:
    from backtests._base_runner import RunOutput


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
    summary: dict[str, Any] | None = None,
    report_path: str | None = None,
    code_hash: str | None = None,
    config_hash: str | None = None,
    input_artifact_hash: str | None = None,
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
        "status": "running",
        "summary": json_dumps(summary or {}),
        "report_path": report_path,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "input_artifact_hash": input_artifact_hash,
        "run_mode": "persist",
    }
    with engine.begin() as conn:
        result = conn.execute(insert_sql, params)
        run_id = getattr(result, "lastrowid", None)
        if run_id is None:
            run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
        conn.execute(update_sql, {"run_id": int(run_id)})
    return int(run_id)


def persist_backtest_output_atomic(
    engine: Engine,
    output: "RunOutput",
    *,
    benchmark_id: str,
) -> int:
    """在单一事务中写入 Blackbox run、明细、月指标与成功摘要。"""
    rows = output.rows
    metrics = output.monthly_metrics
    if not rows:
        raise ValueError("atomic Blackbox backtest persistence requires prediction rows")
    if not metrics:
        raise ValueError("atomic Blackbox backtest persistence requires monthly metrics")
    if any(str(row.get("benchmark_id")) != benchmark_id for row in rows):
        raise ValueError("prediction benchmark_id does not match persistence scope")
    if any(str(row.get("benchmark_id")) != benchmark_id for row in metrics):
        raise ValueError("monthly metric benchmark_id does not match persistence scope")

    with engine.begin() as connection:
        run_id = _create_backtest_run_connection(
            connection,
            benchmark_id=benchmark_id,
            scheme_id=output.scheme_id,
            data_source=output.data_source,
            start_date=output.start_date,
            end_date=output.end_date,
            summary=output.summary,
            report_path=output.report_path,
            code_hash=output.summary.get("code_hash"),
            config_hash=output.summary.get("config_hash"),
            input_artifact_hash=output.summary.get("input_artifact_hash"),
        )
        inserted_predictions = _insert_backtest_predictions_connection(connection, run_id, rows)
        if inserted_predictions != len(rows):
            raise RuntimeError(
                f"atomic prediction insert mismatch: expected={len(rows)}, got={inserted_predictions}"
            )
        inserted_metrics = _insert_backtest_monthly_metrics_connection(connection, run_id, metrics)
        if inserted_metrics != len(metrics):
            raise RuntimeError(
                f"atomic metric insert mismatch: expected={len(metrics)}, got={inserted_metrics}"
            )
        summary = dict(output.summary)
        summary["run_id"] = run_id
        summary["persisted_prediction_count"] = inserted_predictions
        summary["persisted_monthly_metric_count"] = inserted_metrics
        _update_backtest_run_summary_connection(
            connection,
            run_id=run_id,
            summary=summary,
            report_path=output.report_path,
        )
    output.summary.update(summary)
    return run_id


def _create_backtest_run_connection(
    connection: Connection,
    *,
    benchmark_id: str,
    scheme_id: str,
    data_source: str,
    start_date: str,
    end_date: str,
    summary: dict[str, Any],
    report_path: str | None,
    code_hash: str | None,
    config_hash: str | None,
    input_artifact_hash: str | None,
) -> int:
    summary_expr = _json_expression(connection, "summary")
    result = connection.execute(
        text(
            f"""
            INSERT INTO t_backtest_runs
                (benchmark_id, scheme_id, data_source, start_date, end_date, status,
                 summary, report_path, code_hash, config_hash, input_artifact_hash, run_mode)
            VALUES
                (:benchmark_id, :scheme_id, :data_source, :start_date, :end_date, :status,
                 {summary_expr}, :report_path, :code_hash, :config_hash,
                 :input_artifact_hash, :run_mode)
            """
        ),
        {
            "benchmark_id": benchmark_id,
            "scheme_id": scheme_id,
            "data_source": data_source,
            "start_date": start_date,
            "end_date": end_date,
            "status": "running",
            "summary": json_dumps(summary),
            "report_path": report_path,
            "code_hash": code_hash,
            "config_hash": config_hash,
            "input_artifact_hash": input_artifact_hash,
            "run_mode": "persist",
        },
    )
    run_id = getattr(result, "lastrowid", None)
    if run_id is None:
        run_id = connection.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
    connection.execute(
        text(
            "UPDATE t_backtest_runs SET backtest_run_id=:run_id "
            "WHERE id=:run_id AND backtest_run_id IS NULL"
        ),
        {"run_id": int(run_id)},
    )
    return int(run_id)


def _insert_backtest_predictions_connection(
    connection: Connection,
    run_id: int,
    rows: Sequence[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    source_expr = _json_expression(connection, "source_row")
    extra_expr = _json_expression(connection, "extra")
    connection.execute(
        text(
            f"""
            INSERT INTO t_backtest_predictions
                (run_id, benchmark_id, scheme_id, target_tenor, horizon, predict_date,
                 feature_date, target_date, label, predicted_direction, model_pred,
                 source_row, extra)
            VALUES
                (:run_id, :benchmark_id, :scheme_id, :target_tenor, :horizon, :predict_date,
                 :feature_date, :target_date, :label, :predicted_direction, :model_pred,
                 {source_expr}, {extra_expr})
            """
        ),
        [_prediction_params(run_id, row) for row in rows],
    )
    return len(rows)


def _insert_backtest_monthly_metrics_connection(
    connection: Connection,
    run_id: int,
    rows: Sequence[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    actual_expr = _json_expression(connection, "actual_dist")
    predicted_expr = _json_expression(connection, "predicted_dist")
    connection.execute(
        text(
            f"""
            INSERT INTO t_backtest_monthly_metrics
                (run_id, benchmark_id, scheme_id, target_tenor, horizon, month,
                 sample_count, correct_count, accuracy, up_precision, up_recall,
                 down_precision, down_recall, actual_dist, predicted_dist)
            VALUES
                (:run_id, :benchmark_id, :scheme_id, :target_tenor, :horizon, :month,
                 :sample_count, :correct_count, :accuracy, :up_precision, :up_recall,
                 :down_precision, :down_recall, {actual_expr}, {predicted_expr})
            """
        ),
        [_metric_params(run_id, row) for row in rows],
    )
    return len(rows)


def _update_backtest_run_summary_connection(
    connection: Connection,
    *,
    run_id: int,
    summary: dict[str, Any],
    report_path: str | None,
) -> None:
    summary_expr = _json_expression(connection, "summary")
    result = connection.execute(
        text(
            f"""
            UPDATE t_backtest_runs
            SET status=:status, summary={summary_expr}, report_path=:report_path,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=:run_id
            """
        ),
        {
            "run_id": run_id,
            "status": "success",
            "summary": json_dumps(summary),
            "report_path": report_path,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError(f"atomic run summary update affected {result.rowcount} rows")


def _json_expression(connection: Connection, parameter: str) -> str:
    return f"CAST(:{parameter} AS JSON)" if connection.dialect.name == "mysql" else f":{parameter}"


def update_backtest_run_summary(
    engine: Engine,
    *,
    run_id: int,
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
                "status": "success",
                "summary": json_dumps(summary or {}),
                "report_path": report_path,
            },
        )


def replace_backtest_predictions(engine: Engine, run_id: int, rows: Iterable[dict[str, Any]]) -> int:
    """替换某个 run 的逐日预测明细。"""
    materialized = list(rows)
    delete_sql = text("DELETE FROM t_backtest_predictions WHERE run_id = :run_id")
    insert_sql = text(
        """
        INSERT INTO t_backtest_predictions
            (run_id, benchmark_id, scheme_id, target_tenor, horizon, predict_date,
             feature_date, target_date, label, predicted_direction, model_pred,
             source_row, extra)
        VALUES
            (:run_id, :benchmark_id, :scheme_id, :target_tenor, :horizon, :predict_date,
             :feature_date, :target_date, :label, :predicted_direction, :model_pred,
             CAST(:source_row AS JSON), CAST(:extra AS JSON))
        """
    )
    with engine.begin() as conn:
        conn.execute(delete_sql, {"run_id": run_id})
        if materialized:
            conn.execute(insert_sql, [_prediction_params(run_id, row) for row in materialized])
    return len(materialized)


def _prediction_params(run_id: int, row: dict[str, Any]) -> dict[str, Any]:
    params = {key: row.get(key) for key in (
        "benchmark_id", "scheme_id", "target_tenor", "horizon", "predict_date",
        "feature_date", "target_date", "label", "predicted_direction", "model_pred",
        "source_row", "extra",
    )}
    params["run_id"] = run_id
    params["source_row"] = json_dumps(params.get("source_row") or {})
    params["extra"] = json_dumps(params.get("extra") or {})
    params["label"] = _int_or_none(params.get("label"))
    params["predicted_direction"] = _int_or_none(params.get("predicted_direction"))
    params["model_pred"] = _int_or_none(params.get("model_pred"))
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
