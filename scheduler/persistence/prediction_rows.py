"""预测事实行序列化与首次发布回测转换；复用调用者事务。"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable, Mapping
from sqlalchemy import text
from sqlalchemy.engine import Connection
from scheduler.discovery import SchemeConfig
from scheduler.persistence.connections import dialect_name as _dialect_name
from shared.blackbox_v2.contracts import REQUEST_FIELDS, request_from_mapping


_PredictionBusinessKey = tuple[str, str, int, str]


_BLACKBOX_BACKTEST_START_DATE = "2025-01-01"


_BLACKBOX_LIVE_TARGET_START_DATE = "2026-06-01"


def _mysql_utc_datetime(value: datetime | None) -> datetime | None:
    """将批准时刻统一为 MySQL DATETIME(0) 使用的无时区 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _prepare_blackbox_backtest_fact_rows_conn(
    conn: Connection,
    cfg: SchemeConfig,
    *,
    backtest_run_id: int,
    backtest_benchmark_id: str,
    backtest_data_snapshot_id: str,
    backtest_generation_id: str,
    backtest_runtime_profile: str,
    backtest_environment_fingerprint: str,
    backtest_code_hash: str,
    backtest_config_hash: str,
    backtest_manifest_hash: str,
    backtest_validator_policy_digest: str,
    for_update: bool = True,
    require_source_request: bool = False,
) -> list[dict[str, object]]:
    """锁定并把首次激活的 exact-version 回测转换为产品事实 rows。"""
    lock_clause = (
        " FOR UPDATE"
        if for_update and _dialect_name(conn) != "sqlite"
        else ""
    )
    run = conn.execute(
        text(
            "SELECT id, benchmark_id, scheme_id, data_source, status, run_mode, "
            "code_hash, config_hash, input_artifact_hash, summary "
            "FROM t_backtest_runs WHERE id = :backtest_run_id"
            + lock_clause
        ),
        {"backtest_run_id": backtest_run_id},
    ).mappings().one_or_none()
    if run is None:
        raise ValueError("Blackbox activation backtest run does not exist")
    summary = _json_mapping(run.get("summary"))
    expected = {
        "scheme_id": cfg.scheme_id,
        "data_source": "blackbox_v2_current_snapshot_as_of",
        "status": "success",
        "run_mode": "persist",
        "benchmark_id": backtest_benchmark_id,
        "code_hash": backtest_code_hash,
        "config_hash": backtest_config_hash,
        "input_artifact_hash": backtest_data_snapshot_id,
        "scheme_version": cfg.scheme_version,
        "manifest_hash": backtest_manifest_hash,
        "script_validator_policy_digest": backtest_validator_policy_digest,
        "data_snapshot_id": backtest_data_snapshot_id,
        "generation_id": backtest_generation_id,
        "runtime_profile": backtest_runtime_profile,
        "environment_fingerprint": backtest_environment_fingerprint,
    }
    actual = {
        "scheme_id": str(run.get("scheme_id") or ""),
        "data_source": str(run.get("data_source") or ""),
        "status": str(run.get("status") or ""),
        "run_mode": str(run.get("run_mode") or ""),
        "benchmark_id": str(run.get("benchmark_id") or ""),
        "code_hash": str(run.get("code_hash") or ""),
        "config_hash": str(run.get("config_hash") or ""),
        "input_artifact_hash": str(run.get("input_artifact_hash") or ""),
        "scheme_version": str(summary.get("scheme_version") or ""),
        "manifest_hash": str(summary.get("manifest_hash") or ""),
        "script_validator_policy_digest": str(
            summary.get("script_validator_policy_digest") or ""
        ),
        "data_snapshot_id": str(summary.get("data_snapshot_id") or ""),
        "generation_id": str(summary.get("generation_id") or ""),
        "runtime_profile": str(summary.get("runtime_profile") or ""),
        "environment_fingerprint": str(
            summary.get("environment_fingerprint") or ""
        ),
    }
    if actual != expected:
        raise ValueError(
            "Blackbox activation backtest identity mismatch: "
            f"expected={expected!r} actual={actual!r}"
        )
    source_request_column = ", source_row" if require_source_request else ""
    source_rows = conn.execute(
        text(
            "SELECT scheme_id, target_tenor, horizon, predict_date, feature_date, "
            "target_date, label, predicted_direction, extra"
            + source_request_column
            + " "
            "FROM t_backtest_predictions "
            "WHERE run_id = :backtest_run_id "
            "ORDER BY target_tenor, horizon, target_date, predict_date, id"
            + lock_clause
        ),
        {"backtest_run_id": backtest_run_id},
    ).mappings().all()
    if not source_rows:
        raise ValueError("Blackbox activation backtest contains no predictions")
    if require_source_request:
        predict_dates = [str(row.get("predict_date")) for row in source_rows]
        target_dates = [str(row.get("target_date")) for row in source_rows]
        expected_coverage = {
            "backtest_start_date": _BLACKBOX_BACKTEST_START_DATE,
            "target_date_before": _BLACKBOX_LIVE_TARGET_START_DATE,
            "request_count": len(source_rows),
            "actual_predict_date_min": min(predict_dates),
            "actual_predict_date_max": max(predict_dates),
            "actual_target_date_min": min(target_dates),
            "actual_target_date_max": max(target_dates),
        }
        actual_coverage = {
            "backtest_start_date": str(summary.get("backtest_start_date") or ""),
            "target_date_before": str(summary.get("target_date_before") or ""),
            "request_count": summary.get("request_count"),
            "actual_predict_date_min": str(
                summary.get("actual_predict_date_min") or ""
            ),
            "actual_predict_date_max": str(
                summary.get("actual_predict_date_max") or ""
            ),
            "actual_target_date_min": str(
                summary.get("actual_target_date_min") or ""
            ),
            "actual_target_date_max": str(
                summary.get("actual_target_date_max") or ""
            ),
        }
        if actual_coverage != expected_coverage:
            raise ValueError(
                "Native successor backtest coverage summary mismatch: "
                f"expected={expected_coverage!r} actual={actual_coverage!r}"
            )
    expected_tenors = set(str(value) for value in cfg.tenors)
    rows: list[dict[str, object]] = []
    seen: set[_PredictionBusinessKey] = set()
    for source in source_rows:
        if str(source.get("scheme_id") or "") != cfg.scheme_id:
            raise ValueError("Blackbox activation backtest scheme identity mismatch")
        tenor = str(source.get("target_tenor") or "")
        horizon = int(source.get("horizon") or 0)
        predict_date = source.get("predict_date")
        feature_date = source.get("feature_date")
        target_date = source.get("target_date")
        direction = source.get("predicted_direction")
        actual_direction = source.get("label")
        if tenor not in expected_tenors or horizon != int(cfg.horizon):
            raise ValueError("Blackbox activation backtest target scope mismatch")
        if predict_date is None or feature_date is None or target_date is None:
            raise ValueError("Blackbox activation backtest date lineage is incomplete")
        if direction not in {-1, 0, 1}:
            raise ValueError("Blackbox activation backtest direction is invalid")
        if actual_direction not in {-1, 0, 1}:
            raise ValueError("Blackbox activation backtest actual direction is invalid")
        key = (cfg.scheme_id, tenor, horizon, str(target_date))
        if key in seen:
            raise ValueError("Blackbox activation backtest has duplicate business key")
        seen.add(key)
        extra = _json_mapping(source.get("extra"))
        extra.pop("prediction_phase", None)
        row: dict[str, object] = {
            "run_id": None,
            "backtest_run_id": backtest_run_id,
            "scheme_version": cfg.scheme_version,
            "scheme_id": cfg.scheme_id,
            "target_tenor": tenor,
            "horizon": horizon,
            "predict_date": str(predict_date),
            "feature_date": str(feature_date),
            "target_date": str(target_date),
            "predicted_direction": int(direction),
            "backtest_actual_direction": int(actual_direction),
            "model_version": cfg.scheme_version,
            "extra": json.dumps(extra, ensure_ascii=False),
        }
        if require_source_request:
            raw_source_row = _json_mapping(source.get("source_row"))
            if set(raw_source_row) != {*REQUEST_FIELDS, "actual"}:
                raise ValueError(
                    "Blackbox activation backtest source_row fields are invalid"
                )
            source_request = request_from_mapping(
                {field: raw_source_row.get(field) for field in REQUEST_FIELDS}
            )
            expected_request_id = (
                f"{cfg.scheme_id}:{predict_date}:{feature_date}:{target_date}"
            )
            if (
                source_request.request_id != expected_request_id
                or source_request.predict_date != str(predict_date)
                or source_request.feature_date != str(feature_date)
                or source_request.target_date != str(target_date)
            ):
                raise ValueError(
                    "Blackbox activation backtest source_row Request identity mismatch"
                )
            row["_source_request"] = asdict(source_request)
        rows.append(row)
    return rows


def _json_mapping(value: object) -> dict[str, object]:
    """把 MySQL JSON 或 SQLite 文本收敛为对象。"""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, Mapping):
            return dict(loaded)
    raise ValueError("JSON value must be an object")


def _insert_run_predictions_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> int:
    """在调用方事务中用唯一产品事实 INSERT 写入已验证 rows。"""
    sqlite = _dialect_name(conn) == "sqlite"
    extra_expression = ":extra" if sqlite else "CAST(:extra AS JSON)"
    statement = """
        INSERT INTO t_scheme_predictions
            (run_id, backtest_run_id, scheme_version, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
             predicted_direction, backtest_actual_direction, model_version, extra)
        VALUES
            (:run_id, :backtest_run_id, :scheme_version, :scheme_id, :target_tenor, :horizon, :predict_date, :feature_date, :target_date,
             :predicted_direction, :backtest_actual_direction, :model_version, {extra_expression})
        """.format(extra_expression=extra_expression)
    rows = [
        {key: value for key, value in row.items() if key != "_source_request"}
        for row in prediction_rows
    ]
    if not rows:
        return 0
    conn.execute(text(statement), rows)
    return len(rows)
