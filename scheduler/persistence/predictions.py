"""预测运行、正常提交与补缺事务的内部持久化实现。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence, cast
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from scheduler.discovery import SchemeConfig, load_scheme_config
from scheduler.persistence.connections import dialect_name as _dialect_name
from shared.models import PredictionRecord
from shared.one_shot_control_plane import SCHEDULED_ONE_SHOT_CONTROL_PLANES
from shared.prediction_context import LIVE_PREDICTION_PHASES
from shared.scheme_config_schema import ALLOWED_RUNTIME_TYPES
from scheduler.persistence.prediction_rows import (
    _PredictionBusinessKey,
    _insert_run_predictions_conn,
)
from scheduler.persistence.lifecycle import (
    _expected_registry_identity,
    _read_blackbox_execution_approval_conn,
    _read_scheme_registry_rows_conn,
    _read_scheme_version_conn,
    _registry_identity_error,
    registry_scheme_id,
)


PREDICTION_KEYS_ALREADY_EXIST = "prediction_keys_already_exist"

PARTIAL_PREDICTION_KEY_CONFLICT = "partial_prediction_key_conflict"


def _require_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_sha256(value: str, field: str) -> str:
    normalized = _require_nonempty(value, field).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field} must be a 64-character SHA256 hex digest")
    return normalized


def _select_mapping_one_or_none(
    conn: Connection,
    sql: str,
    params: Mapping[str, object],
) -> Mapping[str, object] | None:
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    return (
        conn.execute(text(sql + lock), dict(params))
        .mappings()
        .one_or_none()
    )


def _read_scheme_run_conn(
    conn: Connection,
    *,
    run_id: int,
) -> Mapping[str, object] | None:
    """读取普通 run；遗留 schedule linkage 不属于新的运行契约。"""
    return _select_mapping_one_or_none(
        conn,
        """
        SELECT run_id, scheme_id, scheme_version, runtime_type, run_type,
               prediction_phase, predict_date, status, records_expected,
               records_returned, records_written, data_snapshot_id,
               started_at, finished_at
        FROM t_scheme_runs
        WHERE run_id = :run_id
        """,
        {"run_id": int(run_id)},
    )


def _require_rowcount(result: object, operation: str) -> None:
    actual = int(getattr(result, "rowcount", 0) or 0)
    if actual != 1:
        raise RuntimeError(
            f"{operation} affected {actual} rows, expected 1"
        )


def _stored_iso_date(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise RuntimeError(f"invalid stored {field}: {value!r}") from exc
    raise RuntimeError(f"invalid stored {field}: {value!r}")


def _lock_existing_prediction_business_keys(
    conn: Connection,
    keys: Sequence[_PredictionBusinessKey],
) -> frozenset[_PredictionBusinessKey]:
    """用一次锁定查询读取已存在的预测业务键。"""
    unique_keys = tuple(dict.fromkeys(keys))
    if len(unique_keys) != len(keys):
        raise RuntimeError(
            "prediction business-key lock input contains duplicate keys"
        )
    if not unique_keys:
        return frozenset()

    placeholders = []
    params: dict[str, object] = {}
    for index, key in enumerate(unique_keys):
        placeholders.append(
            f"(:scheme_id_{index}, :target_tenor_{index}, "
            f":horizon_{index}, :target_date_{index})"
        )
        params.update(
            {
                f"scheme_id_{index}": key[0],
                f"target_tenor_{index}": key[1],
                f"horizon_{index}": key[2],
                f"target_date_{index}": key[3],
            }
        )
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    rows = (
        conn.execute(
            text(
                "SELECT scheme_id, target_tenor, horizon, target_date "
                "FROM t_scheme_predictions "
                "WHERE (scheme_id, target_tenor, horizon, target_date) IN ("
                + ", ".join(placeholders)
                + ")"
                + lock
            ),
            params,
        )
        .mappings()
        .all()
    )

    requested = frozenset(unique_keys)
    existing: set[_PredictionBusinessKey] = set()
    for row in rows:
        try:
            key = (
                str(row["scheme_id"]),
                str(row["target_tenor"]),
                int(row["horizon"]),
                _stored_iso_date(row, "target_date"),
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                "prediction business-key lock readback is malformed"
            ) from exc
        if key not in requested:
            raise RuntimeError(
                "prediction business-key lock readback returned an unexpected key"
            )
        if key in existing:
            raise RuntimeError(
                "prediction business-key lock readback returned a duplicate key"
            )
        existing.add(key)
    return frozenset(existing)


def create_scheme_run(
    engine: Engine,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    runtime_type: str = "native_adapter",
    prediction_phase: str | None = None,
    records_expected: int | None = None,
    scheduled_control_plane: str | None = None,
) -> int:
    """创建正常预测运行；自然写入只接受已安装的一次性控制面。"""
    if (
        prediction_phase is not None
        and prediction_phase not in LIVE_PREDICTION_PHASES
    ):
        raise ValueError(
            "prediction_phase must be one of "
            f"{sorted(LIVE_PREDICTION_PHASES)}, got {prediction_phase}"
        )
    if (
        scheduled_control_plane is not None
        and scheduled_control_plane not in SCHEDULED_ONE_SHOT_CONTROL_PLANES
    ):
        raise ValueError(
            "scheduled_control_plane must be an installed one-shot control plane"
        )
    if scheduled_control_plane is not None:
        if prediction_phase != "scheduled_live":
            raise ValueError(
                "scheduled_control_plane requires scheduled_live"
            )
    if (
        prediction_phase == "scheduled_live"
        and scheduled_control_plane not in SCHEDULED_ONE_SHOT_CONTROL_PLANES
    ):
        raise RuntimeError(
            "scheduled_live requires an installed one-shot control plane"
        )
    with engine.begin() as conn:
        return _create_scheme_run_conn(
            conn,
            scheme_id=scheme_id,
            predict_date=predict_date,
            scheme_version=scheme_version,
            runtime_type=runtime_type,
            prediction_phase=prediction_phase,
            records_expected=records_expected,
        )


def _create_scheme_run_conn(
    conn: Connection,
    *,
    scheme_id: str,
    predict_date: str,
    scheme_version: str | None = None,
    runtime_type: str = "native_adapter",
    prediction_phase: str | None = None,
    records_expected: int | None = None,
) -> int:
    """在调用方事务内创建不带遗留 schedule linkage 的运行。"""
    result = conn.execute(
        text(
            """
            INSERT INTO t_scheme_runs
                (scheme_id, scheme_version, runtime_type, run_type,
                 prediction_phase, predict_date, status, records_expected)
            VALUES
                (:scheme_id, :scheme_version, :runtime_type, :run_type,
                 :prediction_phase, :predict_date, :status, :records_expected)
            """
        ),
        {
            "scheme_id": scheme_id,
            "scheme_version": scheme_version,
            "runtime_type": runtime_type,
            "run_type": "active",
            "prediction_phase": prediction_phase,
            "predict_date": predict_date,
            "status": "running",
            "records_expected": records_expected,
        },
    )
    run_id = getattr(result, "lastrowid", None)
    if run_id is None:
        run_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
    return int(run_id)


def attach_run_data_snapshot(engine: Engine, *, run_id: int, data_snapshot_id: str) -> None:
    """将 Blackbox V2 输入快照关联到既有运行审计行。"""
    if not str(data_snapshot_id).strip():
        raise ValueError("data_snapshot_id must be non-empty")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_scheme_runs
                SET data_snapshot_id = :data_snapshot_id
                WHERE run_id = :run_id
                """
            ),
            {"run_id": int(run_id), "data_snapshot_id": str(data_snapshot_id)},
        )


def _finish_scheme_run_conn(
    conn: Connection,
    *,
    run_id: int,
    status: str,
    records_returned: int | None = None,
    records_written: int | None = None,
    error_message: str | None = None,
) -> None:
    """在调用方事务中标记预测运行结束。"""
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
    result = conn.execute(
        sql,
        {
            "run_id": run_id,
            "status": status,
            "records_returned": records_returned,
            "records_written": records_written,
            "error_message": error_message,
        },
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        raise RuntimeError(
            "scheme run update affected "
            f"{int(getattr(result, 'rowcount', 0) or 0)} rows for run_id={run_id}"
        )


def _prediction_write_decision_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> tuple[str, int, str | None]:
    """锁定已验证的实盘业务键并决定整组提交语义。"""
    keys = sorted(
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in prediction_rows
    )
    existing_keys = _lock_existing_prediction_business_keys(conn, keys)
    existing = [key for key in keys if key in existing_keys]
    missing = [key for key in keys if key not in existing_keys]

    if not existing:
        return "success", len(keys), None
    if not missing:
        return "skipped", 0, PREDICTION_KEYS_ALREADY_EXIST

    def format_keys(values: list[tuple[str, str, int, str]]) -> str:
        return "[" + ", ".join(
            f"{scheme_id}/{target_tenor}/h{horizon}/{target_date}"
            for scheme_id, target_tenor, horizon, target_date in values
        ) + "]"

    return (
        "failed",
        0,
        f"{PARTIAL_PREDICTION_KEY_CONFLICT}: "
        f"existing={format_keys(existing)}; missing={format_keys(missing)}",
    )


def _validate_running_run_identity(
    run: Mapping[str, object] | None,
    *,
    expected_identity: Mapping[str, object],
    error_prefix: str,
) -> str:
    """纯校验 running run 身份，并返回其 scheduled_live phase。"""
    errors = []
    if run is None:
        errors.append("run missing")
    else:
        for field, expected_value in expected_identity.items():
            actual_value = run.get(field)
            if field == "predict_date":
                actual_value = _stored_iso_date(run, field)
            if field == "records_expected" and actual_value is not None:
                try:
                    actual_value = int(actual_value)
                except (TypeError, ValueError):
                    pass
            if actual_value != expected_value:
                errors.append(
                    f"{field}: expected={expected_value!r}, "
                    f"got={actual_value!r}"
                )
    if errors:
        raise RuntimeError(f"{error_prefix}: " + "; ".join(errors))
    assert run is not None
    run_phase = str(run.get("prediction_phase") or "")
    if run_phase != "scheduled_live":
        raise RuntimeError(
            f"{error_prefix}: ordinary completion requires scheduled_live, "
            f"got prediction_phase={run.get('prediction_phase')!r}"
        )
    return run_phase


def _validate_completion_records(
    records: Sequence[PredictionRecord],
    *,
    records_returned: int,
    expected_targets: set[tuple[str, int]],
    scheme_id: str,
    scheme_version: str,
    predict_date: str,
    prediction_phase: str,
    runtime_label: str,
) -> None:
    """纯校验 completion records 的计数、业务身份与目标集合。"""
    if records_returned != len(records):
        raise RuntimeError(
            f"{runtime_label} completion records_returned mismatch: "
            f"returned={records_returned}, records={len(records)}"
        )
    returned_targets = Counter(
        (str(record.target_tenor), int(record.horizon))
        for record in records
    )
    duplicate_targets = sorted(
        (target_tenor, horizon, count)
        for (target_tenor, horizon), count in returned_targets.items()
        if count != 1
    )
    errors = []
    if set(returned_targets) != expected_targets or duplicate_targets:
        errors.append(
            "target set does not match active Registry: "
            f"expected={sorted(expected_targets)}, "
            f"returned={sorted(returned_targets)}, "
            f"duplicates={duplicate_targets}"
        )
    for record in records:
        if record.scheme_id != scheme_id:
            errors.append(f"record scheme_id={record.scheme_id!r}")
        if record.scheme_version not in (None, scheme_version):
            errors.append(f"record scheme_version={record.scheme_version!r}")
        if str(record.predict_date) != predict_date:
            errors.append(f"record predict_date={record.predict_date!r}")
        record_phase = record.prediction_phase or (
            record.extra or {}
        ).get("prediction_phase")
        if record_phase != prediction_phase:
            errors.append(f"record prediction_phase={record_phase!r}")
    if errors:
        raise RuntimeError(
            f"{runtime_label} completion records revalidation failed: "
            + "; ".join(errors)
        )


def complete_approved_blackbox_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    scheme_version: str | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> tuple[str, int, str | None]:
    """原子提交 Blackbox prediction、终态 run 与运行日志。"""
    normalized_run_date = _require_iso_date(run_date, "run_date")
    expected_tenors, _expected_registry_ids = _expected_registry_identity(cfg)
    expected_targets = {
        (target_tenor, int(cfg.horizon))
        for target_tenor in expected_tenors
    }
    with _approved_blackbox_write_transaction(
        engine,
        cfg,
        records,
        scheme_version=scheme_version,
    ) as (conn, record_list, exact_scheme_version):
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        expected_run_identity = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": exact_scheme_version,
            "runtime_type": "blackbox_v2",
            "run_type": "active",
            "predict_date": normalized_run_date,
            "status": "running",
            "records_expected": len(expected_targets),
        }
        run_phase = _validate_running_run_identity(
            run,
            expected_identity=expected_run_identity,
            error_prefix=(
                "Blackbox completion running run identity revalidation failed"
            ),
        )
        _validate_completion_records(
            record_list,
            records_returned=records_returned,
            expected_targets=expected_targets,
            scheme_id=cfg.scheme_id,
            scheme_version=exact_scheme_version,
            predict_date=normalized_run_date,
            prediction_phase=run_phase,
            runtime_label="Blackbox",
        )
        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != records_returned:
                raise RuntimeError(
                    "Blackbox completion records_written mismatch: "
                    f"returned={records_returned}, written={records_written}"
                )
        _finish_scheme_run_conn(
            conn,
            run_id=run_id,
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            cfg.scheme_id,
            normalized_run_date,
            status,
            duration_sec,
            error_message,
            run_id,
        )
        return status, records_written, error_message


def complete_active_native_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    scheme_version: str | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> tuple[str, int, str | None]:
    """原子提交 Native prediction、终态 run 与运行日志。"""
    if getattr(cfg, "runtime_type", None) != "native_adapter":
        raise ValueError(
            "active Native completion requires runtime_type=native_adapter"
        )
    if getattr(cfg, "status", None) != "active":
        raise ValueError("active Native completion requires active config")
    exact_scheme_version = getattr(cfg, "scheme_version", None)
    if (
        not isinstance(exact_scheme_version, str)
        or not exact_scheme_version.strip()
    ):
        raise ValueError(
            "active Native completion requires non-empty scheme_version"
        )
    if scheme_version != exact_scheme_version:
        raise ValueError(
            "Native completion scheme_version does not match config: "
            f"{scheme_version!r} != {exact_scheme_version!r}"
        )
    expected_tenors, expected_registry_ids = _expected_registry_identity(cfg)
    expected_targets = {
        (target_tenor, int(cfg.horizon))
        for target_tenor in expected_tenors
    }
    record_list = list(records)
    normalized_run_date = _require_iso_date(run_date, "run_date")

    with engine.begin() as conn:
        version_row = _read_scheme_version_conn(
            conn,
            cfg,
            for_update=True,
        )
        if (
            version_row is None
            or version_row.get("scheme_id") != cfg.scheme_id
            or version_row.get("scheme_version") != exact_scheme_version
            or version_row.get("runtime_type") != "native_adapter"
            or version_row.get("status") != "active"
        ):
            raise RuntimeError(
                "exact active Native version revalidation failed: "
                f"scheme_id={cfg.scheme_id} "
                f"scheme_version={exact_scheme_version}"
            )
        registry_rows = _read_scheme_registry_rows_conn(
            conn,
            cfg,
            expected_registry_ids,
            for_update=True,
        )
        registry_error = _registry_identity_error(
            cfg,
            expected_tenors,
            expected_registry_ids,
            registry_rows,
            expected_status="active",
            expected_runtime_type="native_adapter",
        )
        if registry_error is not None:
            raise RuntimeError(
                f"Native completion Registry revalidation failed: {registry_error}"
            )
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        expected_run_identity = {
            "scheme_id": cfg.scheme_id,
            "scheme_version": exact_scheme_version,
            "runtime_type": "native_adapter",
            "run_type": "active",
            "predict_date": normalized_run_date,
            "status": "running",
            "records_expected": len(expected_targets),
        }
        run_phase = _validate_running_run_identity(
            run,
            expected_identity=expected_run_identity,
            error_prefix="running run identity revalidation failed",
        )
        _validate_completion_records(
            record_list,
            records_returned=records_returned,
            expected_targets=expected_targets,
            scheme_id=cfg.scheme_id,
            scheme_version=exact_scheme_version,
            predict_date=normalized_run_date,
            prediction_phase=run_phase,
            runtime_label="Native",
        )

        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != len(expected_targets):
                raise RuntimeError(
                    "Native completion records_written mismatch: "
                    f"expected={len(expected_targets)}, "
                    f"written={records_written}"
                )
        _finish_scheme_run_conn(
            conn,
            run_id=int(run_id),
            status=status,
            records_returned=records_returned,
            records_written=records_written,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            cfg.scheme_id,
            normalized_run_date,
            status,
            duration_sec,
            error_message,
            int(run_id),
        )
        return status, records_written, error_message


def complete_gray_gap_run(
    engine: Engine,
    cfg: SchemeConfig,
    *,
    run_id: int,
    records: Iterable[PredictionRecord],
    expected_target_keys: list[Mapping[str, object]],
    source_authority: Mapping[str, object] | None,
    records_returned: int,
    run_date: str,
    duration_sec: float,
) -> int:
    """原子提交一个调度日的 ``gray_live`` 历史信号缺口。"""
    written = complete_gray_gap_runs_atomic(
        engine,
        [
            {
                "cfg": cfg,
                "run_id": run_id,
                "records": list(records),
                "expected_target_keys": expected_target_keys,
                "source_authority": source_authority,
                "records_returned": records_returned,
                "run_date": run_date,
                "duration_sec": duration_sec,
            }
        ],
    )
    return written[int(run_id)]


def complete_gray_gap_runs_atomic(
    engine: Engine,
    items: Iterable[Mapping[str, object]],
) -> dict[int, int]:
    """在一个事务中提交多个调度日的 gray-live run 与 prediction。"""
    prepared: list[dict[str, object]] = []
    all_business_keys: set[tuple[object, ...]] = set()
    for item in items:
        cfg = cast(SchemeConfig, item["cfg"])
        run_id = int(item["run_id"])
        records = list(cast(Iterable[PredictionRecord], item["records"]))
        records_returned = int(item["records_returned"])
        expected_targets = _normalize_gray_gap_target_keys(
            cfg,
            list(
                cast(
                    Iterable[Mapping[str, object]],
                    item["expected_target_keys"],
                )
            ),
        )
        if records_returned != len(records):
            raise RuntimeError(
                "gray gap records_returned mismatch: "
                f"returned={records_returned}, records={len(records)}"
            )
        run_date = _require_iso_date(item["run_date"], "run_date")
        duration_sec = float(item["duration_sec"])
        if not math.isfinite(duration_sec) or duration_sec < 0:
            raise ValueError("duration_sec must be a finite non-negative number")
        execution_dates = _gray_gap_execution_dates(expected_targets)
        if run_date != execution_dates["predict_date"]:
            raise ValueError("run_date must equal gray gap execution predict_date")
        normalized_authority = _normalize_gray_gap_source_authority(
            cast(Mapping[str, object] | None, item.get("source_authority")),
            runtime_type=str(cfg.runtime_type),
            feature_date=execution_dates["feature_date"],
            predict_date=execution_dates["predict_date"],
        )
        _validate_gray_gap_records(
            cfg,
            records=records,
            expected_targets=expected_targets,
        )
        enriched_records = _enrich_gray_gap_records(
            cfg,
            records=records,
            source_authority=normalized_authority,
        )
        snapshot_id = _blackbox_gray_gap_snapshot_id(
            cfg,
            records=enriched_records,
        )
        for target in expected_targets:
            key = (
                target["base_scheme_id"],
                target["target_tenor"],
                target["horizon"],
                target["target_date"],
            )
            if key in all_business_keys:
                raise RuntimeError("gray gap range contains duplicate business keys")
            all_business_keys.add(key)
        prepared.append(
            {
                "cfg": cfg,
                "run_id": run_id,
                "records_returned": records_returned,
                "run_date": run_date,
                "duration_sec": duration_sec,
                "expected_targets": expected_targets,
                "records": enriched_records,
                "snapshot_id": snapshot_id,
            }
        )
    if not prepared:
        raise ValueError("gray gap atomic completion requires at least one run")
    run_ids = [int(item["run_id"]) for item in prepared]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("gray gap atomic completion contains duplicate run ids")

    written_by_run: dict[int, int] = {}
    with engine.begin() as conn:
        validated_configs: set[tuple[str, str]] = set()
        all_targets: list[Mapping[str, object]] = []
        for item in prepared:
            cfg = cast(SchemeConfig, item["cfg"])
            run_id = int(item["run_id"])
            targets = cast(list[Mapping[str, object]], item["expected_targets"])
            run = _read_scheme_run_conn(conn, run_id=run_id)
            _validate_gray_gap_run(
                cfg,
                run_id=run_id,
                run=run,
                expected_count=len(targets),
                predict_date=str(item["run_date"]),
            )
            config_identity = (str(cfg.scheme_id), str(cfg.scheme_version))
            if config_identity not in validated_configs:
                _validate_gray_gap_active_version(conn, cfg)
                _validate_gray_gap_active_registry(conn, cfg)
                validated_configs.add(config_identity)
            snapshot_id = item["snapshot_id"]
            if snapshot_id is not None:
                _bind_blackbox_gray_gap_snapshot_conn(
                    conn,
                    run_id=run_id,
                    run=run,
                    data_snapshot_id=str(snapshot_id),
                )
            all_targets.extend(targets)

        _assert_gray_gap_business_keys_absent(conn, all_targets)

        for item in prepared:
            cfg = cast(SchemeConfig, item["cfg"])
            run_id = int(item["run_id"])
            targets = cast(list[Mapping[str, object]], item["expected_targets"])
            prediction_rows = _prepare_run_prediction_rows(
                run_id,
                cast(Iterable[PredictionRecord], item["records"]),
                scheme_version=str(cfg.scheme_version),
            )
            records_written = _insert_run_predictions_conn(conn, prediction_rows)
            if records_written != len(targets):
                raise RuntimeError(
                    "gray gap records_written mismatch: "
                    f"expected={len(targets)}, written={records_written}"
                )
            finished = conn.execute(
                text(
                    """
                    UPDATE t_scheme_runs
                    SET status = 'success',
                        finished_at = CURRENT_TIMESTAMP,
                        records_returned = :records_returned,
                        records_written = :records_written,
                        error_message = NULL
                    WHERE run_id = :run_id
                      AND status = 'running'
                      AND run_type = 'active'
                      AND prediction_phase = 'gray_live'
                    """
                ),
                {
                    "run_id": run_id,
                    "records_returned": int(item["records_returned"]),
                    "records_written": records_written,
                },
            )
            _require_rowcount(finished, "gray gap run finish")
            _write_run_log_conn(
                conn,
                str(cfg.scheme_id),
                str(item["run_date"]),
                "success",
                float(item["duration_sec"]),
                None,
                run_id,
            )
            written_by_run[run_id] = records_written
    return written_by_run


def _blackbox_gray_gap_snapshot_id(
    cfg: SchemeConfig,
    *,
    records: Iterable[PredictionRecord],
) -> str | None:
    """提取并核验 Blackbox gray-gap records 的唯一运行输入快照。"""
    if str(cfg.runtime_type) != "blackbox_v2":
        return None
    snapshot_ids: set[str] = set()
    for record in records:
        snapshot_ids.add(
            _blackbox_gray_gap_record_snapshot_id(
                cfg,
                scheme_id=record.scheme_id,
                predict_date=record.predict_date,
                feature_date=record.feature_date,
                target_date=record.target_date,
                extra=record.extra,
            )
        )
    if len(snapshot_ids) != 1:
        raise RuntimeError(
            "Blackbox gray gap records must carry exactly one non-empty "
            "data_snapshot_id"
        )
    return next(iter(snapshot_ids))


def _bind_blackbox_gray_gap_snapshot_conn(
    conn: Connection,
    *,
    run_id: int,
    run: Mapping[str, object] | None,
    data_snapshot_id: str,
) -> None:
    """在同一 completion 事务中将已核验 snapshot 固定到 running run。"""
    if run is None:
        raise RuntimeError("Blackbox gray gap run is missing before snapshot bind")
    if run.get("data_snapshot_id") is not None:
        raise RuntimeError(
            "Blackbox gray gap run already has data_snapshot_id; refusing overwrite"
        )
    result = conn.execute(
        text(
            """
            UPDATE t_scheme_runs
            SET data_snapshot_id = :data_snapshot_id
            WHERE run_id = :run_id
              AND data_snapshot_id IS NULL
              AND scheme_id = :scheme_id
              AND scheme_version = :scheme_version
              AND runtime_type = 'blackbox_v2'
              AND status = 'running'
              AND run_type = 'active'
              AND prediction_phase = 'gray_live'
            """
        ),
        {
            "run_id": int(run_id),
            "data_snapshot_id": data_snapshot_id,
            "scheme_id": str(run["scheme_id"]),
            "scheme_version": str(run["scheme_version"]),
        },
    )
    _require_rowcount(result, "Blackbox gray gap run data snapshot bind")


def _blackbox_gray_gap_record_snapshot_id(
    cfg: SchemeConfig,
    *,
    scheme_id: object,
    predict_date: object,
    feature_date: object,
    target_date: object,
    extra: object,
) -> str:
    """验证单条 Blackbox gray-gap record 的 request/snapshot provenance。"""
    if str(scheme_id) != str(cfg.scheme_id):
        raise RuntimeError(
            "Blackbox gray gap record scheme_id provenance does not match config"
        )
    if not isinstance(extra, Mapping):
        raise RuntimeError(
            "Blackbox gray gap record provenance requires extra object"
        )
    try:
        snapshot_id = _require_nonempty(
            extra.get("data_snapshot_id"),
            "Blackbox gray gap record data_snapshot_id",
        )
        normalized_feature_date = _require_iso_date(
            feature_date or extra.get("feature_date"),
            "Blackbox gray gap record feature_date",
        )
        normalized_predict_date = _require_iso_date(
            predict_date,
            "Blackbox gray gap record predict_date",
        )
        normalized_target_date = _require_iso_date(
            target_date,
            "Blackbox gray gap record target_date",
        )
        request_id = _require_nonempty(
            extra.get("request_id"),
            "Blackbox gray gap record request_id",
        )
    except ValueError as exc:
        raise RuntimeError(
            "Blackbox gray gap record provenance is invalid"
        ) from exc
    canonical_request_id = (
        f"{cfg.scheme_id}:{normalized_predict_date}:{normalized_feature_date}:"
        f"{normalized_target_date}"
    )
    if request_id != canonical_request_id:
        raise RuntimeError(
            "Blackbox gray gap record request_id provenance is not canonical"
        )
    return snapshot_id


_GRAY_GAP_TARGET_FIELDS = frozenset(
    {
        "registry_scheme_id",
        "base_scheme_id",
        "target_tenor",
        "horizon",
        "task_type",
        "predict_date",
        "feature_date",
        "target_date",
        "prediction_phase",
    }
)

_GRAY_GAP_DATABRIDGE_AUTHORITY_FIELDS = frozenset(
    {
        "authority_type",
        "generation_id",
        "manifest_sha256",
        "refresh_date",
        "cutoff_date",
        "replay_mode",
        "vintage_disclaimer",
    }
)

_GRAY_GAP_VINTAGE_DISCLAIMER = (
    "current_snapshot_as_of_not_historical_vintage"
)

_RETIRED_GRAY_GAP_EXTRA_FIELDS = frozenset(
    {
        "signal_gap_plan_sha256",
        "execution_group_identity",
        "execution_group_identity_sha256",
        "backfilled_at",
        "backfilled_by",
    }
)

_GRAY_GAP_FIXED_ATOMIC_TARGETS = {
    "t1_daily": frozenset({"5Y", "10Y"}),
    "t5_daily": frozenset({"3Y", "5Y", "7Y", "10Y"}),
}


def _require_lower_sha256(value: object) -> str:
    field = "source_authority.manifest_sha256"
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a lowercase SHA256 digest")
    normalized = _require_sha256(value, field)
    if normalized != value:
        raise ValueError(f"{field} must be a lowercase SHA256 digest")
    return normalized


def _require_iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if normalized != value:
        raise ValueError(f"{field} must be an ISO date")
    return normalized


def _normalize_gray_gap_target_keys(
    cfg: SchemeConfig,
    expected_target_keys: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(expected_target_keys, list) or not expected_target_keys:
        raise ValueError(
            "expected_target_keys must be a non-empty list of mappings"
        )
    if (
        getattr(cfg, "runtime_type", None) != "blackbox_v2"
        and (
            getattr(cfg, "status", None) != "active"
            or getattr(cfg, "version_status", None) != "active"
        )
    ):
        raise RuntimeError(
            "gray gap config must be active with active version_status"
        )
    base_scheme_id = _require_nonempty(
        getattr(cfg, "scheme_id", None),
        "cfg.scheme_id",
    )
    _require_nonempty(
        getattr(cfg, "scheme_version", None),
        "cfg.scheme_version",
    )
    runtime_type = getattr(cfg, "runtime_type", None)
    if runtime_type not in ALLOWED_RUNTIME_TYPES:
        raise ValueError(
            "gray gap config runtime_type must be native_adapter or "
            "blackbox_v2"
        )
    cfg_horizon = getattr(cfg, "horizon", None)
    if isinstance(cfg_horizon, bool) or not isinstance(cfg_horizon, int):
        raise ValueError("cfg.horizon must be an integer")
    cfg_task_type = _require_nonempty(
        getattr(cfg, "task_type", None),
        "cfg.task_type",
    )
    _require_nonempty(
        getattr(cfg, "frequency", None),
        "cfg.frequency",
    )
    raw_tenors = getattr(cfg, "tenors", None)
    if not isinstance(raw_tenors, list) or not raw_tenors:
        raise ValueError("cfg.tenors must be a non-empty list")
    cfg_tenors = [
        _require_nonempty(value, "cfg.tenors item")
        for value in raw_tenors
    ]
    if len(cfg_tenors) != len(set(cfg_tenors)):
        raise ValueError("cfg.tenors must not contain duplicates")
    fixed_targets = _GRAY_GAP_FIXED_ATOMIC_TARGETS.get(base_scheme_id)
    if (
        fixed_targets is not None
        and frozenset(cfg_tenors) != fixed_targets
    ):
        raise RuntimeError(
            f"{base_scheme_id} fixed atomic target multiset mismatch: "
            f"expected={sorted(fixed_targets)}, actual={sorted(cfg_tenors)}"
        )

    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(expected_target_keys):
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"expected_target_keys[{index}] must be a mapping"
            )
        if set(raw) != _GRAY_GAP_TARGET_FIELDS:
            raise ValueError(
                f"expected_target_keys[{index}] must contain exact fields "
                f"{sorted(_GRAY_GAP_TARGET_FIELDS)}"
            )
        horizon = raw["horizon"]
        if isinstance(horizon, bool) or not isinstance(horizon, int):
            raise ValueError(
                f"expected_target_keys[{index}].horizon must be an integer"
            )
        target_tenor = _require_nonempty(
            raw["target_tenor"],
            f"expected_target_keys[{index}].target_tenor",
        )
        row = {
            "registry_scheme_id": _require_nonempty(
                raw["registry_scheme_id"],
                f"expected_target_keys[{index}].registry_scheme_id",
            ),
            "base_scheme_id": _require_nonempty(
                raw["base_scheme_id"],
                f"expected_target_keys[{index}].base_scheme_id",
            ),
            "target_tenor": target_tenor,
            "horizon": horizon,
            "task_type": _require_nonempty(
                raw["task_type"],
                f"expected_target_keys[{index}].task_type",
            ),
            "predict_date": _require_iso_date(
                raw["predict_date"],
                f"expected_target_keys[{index}].predict_date",
            ),
            "feature_date": _require_iso_date(
                raw["feature_date"],
                f"expected_target_keys[{index}].feature_date",
            ),
            "target_date": _require_iso_date(
                raw["target_date"],
                f"expected_target_keys[{index}].target_date",
            ),
            "prediction_phase": _require_nonempty(
                raw["prediction_phase"],
                f"expected_target_keys[{index}].prediction_phase",
            ),
        }
        expected_registry_id = registry_scheme_id(
            base_scheme_id,
            cfg_horizon,
            target_tenor,
        )
        exact_identity = {
            "registry_scheme_id": expected_registry_id,
            "base_scheme_id": base_scheme_id,
            "horizon": cfg_horizon,
            "task_type": cfg_task_type,
            "prediction_phase": "gray_live",
        }
        mismatches = [
            field
            for field, expected in exact_identity.items()
            if row[field] != expected
        ]
        if mismatches:
            raise RuntimeError(
                "expected target identity does not match exact config: "
                + ",".join(mismatches)
            )
        normalized.append(row)

    actual_tenors = Counter(str(row["target_tenor"]) for row in normalized)
    unknown_tenors = sorted(set(actual_tenors) - set(cfg_tenors))
    if unknown_tenors:
        raise RuntimeError(
            "expected target subset does not match exact config: "
            f"unknown={unknown_tenors}, configured={sorted(cfg_tenors)}"
        )
    identities = [
        tuple(row[field] for field in sorted(_GRAY_GAP_TARGET_FIELDS))
        for row in normalized
    ]
    if len(identities) != len(set(identities)):
        raise RuntimeError("expected target multiset contains duplicates")
    _gray_gap_execution_dates(normalized)
    return sorted(
        normalized,
        key=lambda row: (
            str(row["registry_scheme_id"]),
            str(row["target_tenor"]),
        ),
    )


def _gray_gap_execution_dates(
    expected_targets: list[Mapping[str, object]],
) -> dict[str, str]:
    fields = ("predict_date", "feature_date", "target_date")
    values = {
        field: {str(row[field]) for row in expected_targets}
        for field in fields
    }
    inconsistent = [
        field for field, observed in values.items() if len(observed) != 1
    ]
    if inconsistent:
        raise RuntimeError(
            "gray gap execution group dates are inconsistent: "
            + ",".join(inconsistent)
        )
    return {
        field: next(iter(values[field]))
        for field in fields
    }


def _normalize_gray_gap_source_authority(
    source_authority: Mapping[str, object] | None,
    *,
    runtime_type: str,
    feature_date: str,
    predict_date: str,
) -> dict[str, object] | None:
    if runtime_type == "native_adapter":
        if source_authority is not None:
            raise ValueError("Native source_authority must be None")
        return None
    if runtime_type != "blackbox_v2":
        raise ValueError(
            f"unsupported gray gap runtime_type={runtime_type}"
        )
    if not isinstance(source_authority, Mapping):
        raise ValueError("Blackbox DataBridge source_authority is required")
    authority_type = source_authority.get("authority_type")
    if authority_type != "databridge_current_generation":
        raise ValueError(
            "Blackbox source_authority must be a DataBridge authority"
        )
    expected_fields = _GRAY_GAP_DATABRIDGE_AUTHORITY_FIELDS
    if set(source_authority) != expected_fields:
        raise ValueError(
            "source_authority must contain exact fields "
            f"{sorted(expected_fields)}"
        )
    normalized = dict(source_authority)
    normalized["manifest_sha256"] = _require_lower_sha256(
        source_authority["manifest_sha256"],
    )
    cutoff_date = _require_iso_date(
        source_authority["cutoff_date"],
        "source_authority.cutoff_date",
    )
    if cutoff_date != feature_date:
        raise ValueError(
            "source_authority cutoff_date must equal execution feature_date"
        )
    if (
        source_authority["vintage_disclaimer"]
        != _GRAY_GAP_VINTAGE_DISCLAIMER
    ):
        raise ValueError("source_authority vintage_disclaimer is invalid")
    normalized["generation_id"] = _require_nonempty(
        source_authority["generation_id"],
        "source_authority.generation_id",
    )
    refresh_date = _require_iso_date(
        source_authority["refresh_date"],
        "source_authority.refresh_date",
    )
    if refresh_date < predict_date:
        raise ValueError(
            "source_authority refresh_date must be on or after historical "
            "predict_date"
        )
    if source_authority["replay_mode"] != "historical_as_of_replay":
        raise ValueError("source_authority replay_mode is invalid")
    return normalized


def _validate_gray_gap_records(
    cfg: SchemeConfig,
    *,
    records: list[PredictionRecord],
    expected_targets: list[Mapping[str, object]],
) -> None:
    expected = Counter(
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["predict_date"]),
            str(row["feature_date"]),
            str(row["target_date"]),
            str(row["prediction_phase"]),
        )
        for row in expected_targets
    )
    actual: Counter[tuple[object, ...]] = Counter()
    for record in records:
        phase = record.prediction_phase or (
            (record.extra or {}).get("prediction_phase")
            if isinstance(record.extra, Mapping)
            else None
        )
        feature_date = record.feature_date or (
            (record.extra or {}).get("feature_date")
            if isinstance(record.extra, Mapping)
            else None
        )
        try:
            identity = (
                str(record.scheme_id),
                str(record.target_tenor),
                int(record.horizon),
                _require_iso_date(record.predict_date, "record.predict_date"),
                _require_iso_date(feature_date, "record.feature_date"),
                _require_iso_date(record.target_date, "record.target_date"),
                str(phase),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "gray gap record identity mismatch"
            ) from exc
        if record.run_id is not None:
            raise RuntimeError(
                "gray gap records must not carry a pre-bound run_id"
            )
        if (
            record.scheme_version is not None
            and record.scheme_version != str(cfg.scheme_version)
        ):
            raise RuntimeError(
                "gray gap record scheme_version identity mismatch"
            )
        actual[identity] += 1
    if actual != expected:
        raise RuntimeError(
            "gray gap record target multiset or identity mismatch: "
            f"expected={dict(expected)}, actual={dict(actual)}"
        )


def _enrich_gray_gap_records(
    cfg: SchemeConfig,
    *,
    records: list[PredictionRecord],
    source_authority: Mapping[str, object] | None,
) -> list[PredictionRecord]:
    common_extra: dict[str, object] = {
        "backfill_mode": "signal_gap_fill",
    }
    if source_authority is not None:
        common_extra.update(
            {
                "source_authority": dict(source_authority),
                "replay_semantics": _GRAY_GAP_VINTAGE_DISCLAIMER,
                "source_generation_id": source_authority["generation_id"],
                "source_generation_manifest_sha256":
                    source_authority["manifest_sha256"],
                "source_refresh_date": source_authority["refresh_date"],
                "source_cutoff_date": source_authority["cutoff_date"],
            }
        )
    return [
        replace(
            record,
            prediction_phase="gray_live",
            scheme_version=str(cfg.scheme_version),
            extra={
                **{
                    key: value
                    for key, value in dict(record.extra or {}).items()
                    if key not in _RETIRED_GRAY_GAP_EXTRA_FIELDS
                },
                **common_extra,
            },
        )
        for record in records
    ]


def _validate_gray_gap_run(
    cfg: SchemeConfig,
    *,
    run_id: int,
    run: Mapping[str, object] | None,
    expected_count: int,
    predict_date: str,
) -> None:
    if run is None:
        raise RuntimeError(f"gray gap scheme run not found: {run_id}")
    expected_fields = {
        "scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": str(cfg.runtime_type),
        "run_type": "active",
        "prediction_phase": "gray_live",
        "predict_date": predict_date,
        "status": "running",
    }
    identity_matches = all(
        str(run.get(field)) == expected
        for field, expected in expected_fields.items()
    )
    if not identity_matches:
        raise RuntimeError(
            "gray gap completion requires a gray_live running run with exact "
            f"config identity: run_id={run_id}"
        )
    if int(run.get("records_expected") or 0) != expected_count:
        raise RuntimeError(
            "gray gap run records_expected mismatch: "
            f"expected={expected_count}, actual={run.get('records_expected')}"
        )


def _validate_gray_gap_active_version(
    conn: Connection,
    cfg: SchemeConfig,
) -> None:
    version = _read_scheme_version_conn(conn, cfg, for_update=True)
    expected = {
        "scheme_id": str(cfg.scheme_id),
        "scheme_version": str(cfg.scheme_version),
        "runtime_type": str(cfg.runtime_type),
        "status": "active",
    }
    if version is None or any(
        str(version.get(field) or "") != value
        for field, value in expected.items()
    ):
        raise RuntimeError(
            "gray gap completion requires the exact active version"
        )


def _validate_gray_gap_active_registry(
    conn: Connection,
    cfg: SchemeConfig,
) -> None:
    lock = "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"
    rows = list(
        (
            conn.execute(
                text(
                    """
                    SELECT scheme_id, base_scheme_id, runtime_type, frequency,
                           task_type, target_tenor, horizon
                    FROM t_scheme_registry
                    WHERE base_scheme_id = :base_scheme_id
                      AND status = 'active'
                    ORDER BY scheme_id
                    """
                    + lock
                ),
                {"base_scheme_id": str(cfg.scheme_id)},
            )
            .mappings()
            .all()
        )
    )
    actual = Counter(
        (
            str(row["scheme_id"]),
            str(row["base_scheme_id"]),
            str(row["runtime_type"]),
            str(row["frequency"]),
            str(row["task_type"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
        )
        for row in rows
    )
    expected = Counter(
        (
            registry_scheme_id(
                str(cfg.scheme_id),
                int(cfg.horizon),
                str(target_tenor),
            ),
            str(cfg.scheme_id),
            str(cfg.runtime_type),
            str(cfg.frequency),
            str(cfg.task_type),
            str(target_tenor),
            int(cfg.horizon),
        )
        for target_tenor in cfg.tenors
    )
    if actual != expected:
        raise RuntimeError(
            "gray gap active Registry target multiset mismatch: "
            f"expected={dict(expected)}, actual={dict(actual)}"
        )


def _assert_gray_gap_business_keys_absent(
    conn: Connection,
    expected_targets: list[Mapping[str, object]],
) -> None:
    sorted_rows = sorted(
        expected_targets,
        key=lambda item: (
            str(item["base_scheme_id"]),
            str(item["target_tenor"]),
            int(item["horizon"]),
            str(item["target_date"]),
        ),
    )
    keys = tuple(
        (
            str(row["base_scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in sorted_rows
    )
    existing = _lock_existing_prediction_business_keys(conn, keys)
    for row, key in zip(sorted_rows, keys):
        if key in existing:
            raise RuntimeError(
                "gray gap business key already exists; entire group "
                "rejected: "
                f"{row['base_scheme_id']}/{row['target_tenor']}/"
                f"h{row['horizon']}/{row['target_date']}"
            )


def fail_scheme_run_atomic(
    engine: Engine,
    *,
    run_id: int,
    scheme_id: str,
    run_date: str,
    duration_sec: float,
    records_returned: int | None,
    error_message: str,
) -> None:
    """以独立事务同时写入 failed run 状态与失败日志。"""
    normalized_run_date = _require_iso_date(run_date, "run_date")
    with engine.begin() as conn:
        run = _read_scheme_run_conn(
            conn,
            run_id=int(run_id),
        )
        identity_errors = []
        if run is None:
            identity_errors.append("run missing")
        else:
            if run.get("scheme_id") != scheme_id:
                identity_errors.append(
                    "scheme_id: "
                    f"expected={scheme_id!r}, got={run.get('scheme_id')!r}"
                )
            actual_run_date = _stored_iso_date(run, "predict_date")
            if actual_run_date != normalized_run_date:
                identity_errors.append(
                    "predict_date: "
                    f"expected={normalized_run_date!r}, got={actual_run_date!r}"
                )
            if run.get("status") != "running":
                identity_errors.append(
                    "status: expected='running', "
                    f"got={run.get('status')!r}"
                )
        if identity_errors:
            raise RuntimeError(
                "failure run identity revalidation failed: "
                + "; ".join(identity_errors)
            )
        _finish_scheme_run_conn(
            conn,
            run_id=run_id,
            status="failed",
            records_returned=records_returned,
            records_written=0,
            error_message=error_message,
        )
        _write_run_log_conn(
            conn,
            scheme_id,
            normalized_run_date,
            "failed",
            duration_sec,
            error_message,
            run_id,
        )


@contextmanager
def _approved_blackbox_write_transaction(
    engine: Engine,
    cfg: SchemeConfig,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> Iterator[tuple[Connection, list[PredictionRecord], str]]:
    """统一 Blackbox 最终写入的生命周期锁、配置与 DB 复核边界。"""
    if getattr(cfg, "runtime_type", None) != "blackbox_v2":
        raise ValueError("approved Blackbox insert requires runtime_type=blackbox_v2")
    if scheme_version is not None and scheme_version != cfg.scheme_version:
        raise ValueError(
            f"prediction scheme_version={scheme_version} does not match config {cfg.scheme_version}"
        )
    scheme_path_value = getattr(cfg, "path", None)
    if scheme_path_value is None:
        raise RuntimeError("Blackbox canonical config path is required for final write")
    scheme_path = Path(scheme_path_value)
    if scheme_path.name != cfg.scheme_id or scheme_path.parent.name != "schemes":
        raise RuntimeError(
            "Blackbox canonical config path is invalid for final write: "
            f"scheme_id={cfg.scheme_id} path={scheme_path}"
        )
    config_path = scheme_path / "config.yaml"
    exact_scheme_version = cfg.scheme_version
    record_list = list(records)
    mismatched_record_versions = sorted(
        {
            str(record.scheme_version)
            for record in record_list
            if record.scheme_version is not None and record.scheme_version != exact_scheme_version
        }
    )
    if mismatched_record_versions:
        raise ValueError(
            "prediction records contain scheme versions that do not match approved config "
            f"{exact_scheme_version}: {mismatched_record_versions}"
        )

    current_cfg = load_scheme_config(config_path)
    if current_cfg.scheme_version != cfg.scheme_version:
        raise RuntimeError(
            "Blackbox canonical config changed before final write: "
            f"expected={cfg.scheme_version}, current={current_cfg.scheme_version}"
        )
    cfg = current_cfg
    with engine.begin() as conn:
        approval = _read_blackbox_execution_approval_conn(conn, cfg, for_update=True)
        if not approval.executable:
            raise RuntimeError(
                f"Blackbox V2 version is not production-approved: {approval.reason}"
            )
        yield conn, record_list, exact_scheme_version


def _prepare_run_prediction_rows(
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> list[dict[str, object]]:
    """将 prediction records 规范化并验证为最终 SQL rows。"""
    rows: list[dict[str, object]] = []
    for record in records:
        row = asdict(record)
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(
                "feature_date is required for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                "anchor_date must equal feature_date for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        phase = record.prediction_phase or extra.get("prediction_phase")
        if phase not in LIVE_PREDICTION_PHASES:
            raise ValueError(
                f"prediction_phase must be one of {sorted(LIVE_PREDICTION_PHASES)} "
                f"for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        extra["feature_date"] = str(feature_date)
        extra.pop("prediction_phase", None)
        row["run_id"] = record.run_id if record.run_id is not None else run_id
        if int(row["run_id"]) != int(run_id):
            raise RuntimeError(
                "prediction record run_id must match the committing run: "
                f"{row['run_id']} != {run_id}"
            )
        row["scheme_version"] = (
            record.scheme_version
            if record.scheme_version is not None
            else scheme_version
        )
        row["backtest_run_id"] = None
        row["backtest_actual_direction"] = None
        if not str(row["scheme_version"] or "").strip():
            raise ValueError(
                "scheme_version is required for live prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        row["feature_date"] = str(feature_date)
        row.pop("prediction_phase", None)
        row["extra"] = json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    return rows


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
    with engine.begin() as conn:
        _write_run_log_conn(
            conn,
            scheme_id,
            run_date,
            status,
            duration_sec,
            error_msg,
            run_id,
        )


def _write_run_log_conn(
    conn: Connection,
    scheme_id: str,
    run_date: str,
    status: str,
    duration_sec: float | None = None,
    error_msg: str | None = None,
    run_id: int | None = None,
) -> None:
    """在调用方事务中写入方案运行日志。"""
    sql = text(
        """
        INSERT INTO t_scheme_run_log (run_id, scheme_id, run_date, status, duration_sec, error_msg)
        VALUES (:run_id, :scheme_id, :run_date, :status, :duration_sec, :error_msg)
        """
    )
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


def prediction_day_backup_digest(rows: Sequence[Mapping[str, object]]) -> str:
    """计算原记录备份摘要，供明确授权的当日重算操作进行并发围栏。"""
    ordered = sorted((dict(row) for row in rows), key=lambda row: int(row['id']))
    return hashlib.sha256(json.dumps(
        ordered, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


def replace_authorized_prediction_day(
    engine: Engine, *, predict_date: str,
    expected_backup_digest: str, expected_count: int,
    authorization: str, items: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """将已暂存并核验的当日预测整批替换；事实和新run在同一事务提交。"""
    from zoneinfo import ZoneInfo

    if (authorization != 'replace-todays-published-predictions'
            or predict_date != datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            or type(expected_count) is not int or expected_count <= 0
            or len(expected_backup_digest) != 64 or not items):
        raise ValueError('explicit today-only prediction replacement authorization required')
    prepared = []
    for item in items:
        cfg = cast(SchemeConfig, item['cfg'])
        current = load_scheme_config(cfg.path / 'config.yaml')
        if current.scheme_version != cfg.scheme_version:
            raise RuntimeError('canonical exact changed before replacement')
        records = list(cast(Iterable[PredictionRecord], item['records']))
        if any(type(r.predicted_direction) is not int or r.predicted_direction not in {-1, 0, 1}
               or r.run_id is not None for r in records):
            raise ValueError('replacement requires valid directions and fresh run identity')
        tenors, _ = _expected_registry_identity(cfg)
        _validate_completion_records(
            records, records_returned=len(records),
            expected_targets={(tenor, int(cfg.horizon)) for tenor in tenors},
            scheme_id=cfg.scheme_id, scheme_version=cfg.scheme_version,
            predict_date=predict_date, prediction_phase='gray_live',
            runtime_label='authorized replacement',
        )
        prepared.append((cfg, records, float(item['duration_sec'])))
    def key(row):
        return tuple(str(row[name]) for name in (
            'scheme_id', 'scheme_version', 'target_tenor', 'horizon',
            'predict_date', 'feature_date', 'target_date',
        ))
    new_keys = [key(asdict(record)) for _, records, _ in prepared for record in records]
    if len(new_keys) != expected_count or len(set(new_keys)) != expected_count:
        raise ValueError('replacement must contain the complete unique backed-up scope')
    run_ids = {}
    with engine.begin() as conn:
        old_rows = [dict(row) for row in conn.execute(text(
            'SELECT * FROM t_scheme_predictions WHERE predict_date=:day ORDER BY id FOR UPDATE'
        ), {'day': predict_date}).mappings()]
        if (len(old_rows) != expected_count
                or any(row.get('backtest_run_id') is not None for row in old_rows)
                or prediction_day_backup_digest(old_rows) != expected_backup_digest
                or {key(row) for row in old_rows} != set(new_keys)):
            raise RuntimeError('prediction scope changed since backup; zero facts replaced')
        for cfg, records, duration in prepared:
            _validate_gray_gap_active_version(conn, cfg)
            _validate_gray_gap_active_registry(conn, cfg)
            if cfg.runtime_type == 'blackbox_v2':
                approval = _read_blackbox_execution_approval_conn(conn, cfg, for_update=True)
                if not approval.executable:
                    raise RuntimeError('Blackbox replacement not production-approved')
            if not math.isfinite(duration) or duration < 0:
                raise ValueError('invalid replacement duration')
        deleted = conn.execute(text(
            'DELETE FROM t_scheme_predictions WHERE predict_date=:day'
        ), {'day': predict_date})
        if deleted.rowcount != expected_count:
            raise RuntimeError('replacement delete count mismatch')
        for cfg, records, duration in prepared:
            run_id = _create_scheme_run_conn(
                conn, scheme_id=cfg.scheme_id, scheme_version=cfg.scheme_version,
                runtime_type=cfg.runtime_type, prediction_phase='gray_live',
                predict_date=predict_date, records_expected=len(records),
            )
            snapshot_ids = {str((r.extra or {}).get('data_snapshot_id') or '') for r in records}
            if cfg.runtime_type == 'blackbox_v2':
                if len(snapshot_ids) != 1 or not next(iter(snapshot_ids)):
                    raise RuntimeError('replacement snapshot identity required')
                conn.execute(text('UPDATE t_scheme_runs SET data_snapshot_id=:snapshot WHERE run_id=:run_id'),
                             {'snapshot': next(iter(snapshot_ids)), 'run_id': run_id})
            rows = _prepare_run_prediction_rows(run_id, records, scheme_version=cfg.scheme_version)
            count = _insert_run_predictions_conn(conn, rows)
            if count != len(records):
                raise RuntimeError('replacement insert count mismatch')
            _finish_scheme_run_conn(conn, run_id=run_id, status='success',
                                    records_returned=count, records_written=count)
            _write_run_log_conn(conn, cfg.scheme_id, predict_date, 'success', duration,
                                'user-authorized same-day recompute after source recovery', run_id)
            run_ids[cfg.scheme_id] = run_id
    return run_ids
