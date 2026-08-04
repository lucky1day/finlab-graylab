from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from harness.context import GateContext
from harness.result import GateResult, GateStatus


# Approved harness control-plane write boundary.
# This module may write only t_harness_runs and t_harness_gate_results; it must
# never write prediction, backtest, registry, source, or other business tables.


class LegacyNativeAdmissionAttestationPersistenceError(RuntimeError):
    """单用途 legacy Native 身份收据无法原子落库。"""


def new_harness_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hr_{stamp}_{uuid.uuid4().hex[:12]}"


def persist_harness_run_start(
    ctx: GateContext,
    *,
    harness_run_id: str,
    stage: str,
    started_at: str,
) -> bool:
    """记录 harness run 开始；DB 不可用时返回 False，不阻断 gate。"""

    def operation(engine) -> None:
        cfg = ctx.config
        sql = text(
            """
            INSERT INTO t_harness_runs
                (harness_run_id, scheme_id, scheme_version, stage, status, started_at,
                 triggered_by, project_root, git_commit, code_hash, config_hash, report_uri)
            VALUES
                (:harness_run_id, :scheme_id, :scheme_version, :stage, 'running', :started_at,
                 :triggered_by, :project_root, :git_commit, :code_hash, :config_hash, :report_uri)
            ON DUPLICATE KEY UPDATE
                stage = VALUES(stage),
                status = VALUES(status),
                started_at = VALUES(started_at),
                project_root = VALUES(project_root),
                code_hash = VALUES(code_hash),
                config_hash = VALUES(config_hash),
                report_uri = VALUES(report_uri)
            """
        )
        with engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "harness_run_id": harness_run_id,
                    "scheme_id": ctx.scheme_id,
                    "scheme_version": _ctx_scheme_version(ctx),
                    "stage": stage,
                    "started_at": _mysql_datetime(started_at),
                    "triggered_by": "harness",
                    "project_root": str(ctx.project_root),
                    "git_commit": None,
                    "code_hash": getattr(cfg, "code_hash", None),
                    "config_hash": getattr(cfg, "config_hash", None),
                    "report_uri": str(ctx.report_dir),
                },
            )

    return _with_engine(ctx, operation)


def persist_harness_gate_result(ctx: GateContext, harness_run_id: str, result: GateResult) -> bool:
    """记录单个 gate 结果；DB 不可用时返回 False，不阻断 gate。"""

    def operation(engine) -> None:
        sql = text(
            """
            INSERT INTO t_harness_gate_results
                (harness_run_id, gate_name, status, started_at, finished_at, summary_json, report_uri)
            VALUES
                (:harness_run_id, :gate_name, :status, :started_at, :finished_at,
                 CAST(:summary_json AS JSON), :report_uri)
            """
        )
        with engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "harness_run_id": harness_run_id,
                    "gate_name": result.gate_name,
                    "status": result.status.value,
                    "started_at": _mysql_datetime(result.started_at),
                    "finished_at": _mysql_datetime(result.finished_at),
                    "summary_json": json.dumps(_result_summary(result), ensure_ascii=False),
                    "report_uri": str(result.report_path) if result.report_path is not None else None,
                },
            )

    return _with_engine(ctx, operation)


def persist_harness_run_finish(
    ctx: GateContext,
    *,
    harness_run_id: str,
    status: str,
    finished_at: str,
    report_uri: str | None,
) -> bool:
    """记录 harness run 结束；DB 不可用时返回 False，不阻断 gate。"""

    def operation(engine) -> None:
        sql = text(
            """
            UPDATE t_harness_runs
            SET status = :status,
                finished_at = :finished_at,
                report_uri = :report_uri
            WHERE harness_run_id = :harness_run_id
            """
        )
        with engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "harness_run_id": harness_run_id,
                    "status": status,
                    "finished_at": _mysql_datetime(finished_at),
                    "report_uri": report_uri,
                },
            )

    return _with_engine(ctx, operation)


def persist_legacy_native_admission_attestation(
    ctx: GateContext,
    *,
    harness_run_id: str,
    stage: str,
    gate_name: str,
    triggered_by: str,
    evidence_key: str,
    evidence_value: dict,
    started_at: str,
    finished_at: str,
) -> None:
    """原子写入一个不含当前版本/hash 的 operator attestation receipt。

    该专用入口刻意不复用普通 ``persist_harness_run_start``，避免把当前
    code/config/version 元数据写入历史准入证明。
    """
    if ctx.engine_factory is None:
        raise LegacyNativeAdmissionAttestationPersistenceError(
            "legacy Native admission attestation requires a database engine"
        )

    engine = None
    try:
        engine = ctx.engine_factory()
        if engine is None:
            raise LegacyNativeAdmissionAttestationPersistenceError(
                "legacy Native admission attestation cannot connect to database"
            )
        summary_json = json.dumps(
            {
                "passed": True,
                "evidence": [
                    {
                        "key": evidence_key,
                        "value": _jsonable(evidence_value),
                        "detail": None,
                    }
                ],
                "errors": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with engine.begin() as conn:
            existing_run = conn.execute(
                text(
                    "SELECT harness_run_id FROM t_harness_runs "
                    "WHERE harness_run_id = :harness_run_id"
                ),
                {"harness_run_id": harness_run_id},
            ).first()
            existing_gate = conn.execute(
                text(
                    "SELECT harness_run_id FROM t_harness_gate_results "
                    "WHERE harness_run_id = :harness_run_id"
                ),
                {"harness_run_id": harness_run_id},
            ).first()
            if existing_run is not None or existing_gate is not None:
                raise LegacyNativeAdmissionAttestationPersistenceError(
                    "legacy Native admission attestation receipt already exists"
                )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_runs
                        (harness_run_id, scheme_id, scheme_version, stage, status,
                         started_at, finished_at, triggered_by, project_root,
                         git_commit, code_hash, config_hash, report_uri)
                    VALUES
                        (:harness_run_id, :scheme_id, NULL, :stage, 'passed',
                         :started_at, :finished_at, :triggered_by, :project_root,
                         NULL, NULL, NULL, NULL)
                    """
                ),
                {
                    "harness_run_id": harness_run_id,
                    "scheme_id": ctx.scheme_id,
                    "stage": stage,
                    "started_at": _mysql_datetime(started_at),
                    "finished_at": _mysql_datetime(finished_at),
                    "triggered_by": triggered_by,
                    "project_root": str(ctx.project_root),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_gate_results
                        (harness_run_id, gate_name, status, started_at, finished_at,
                         summary_json, report_uri)
                    VALUES
                        (:harness_run_id, :gate_name, 'passed', :started_at,
                         :finished_at, :summary_json, NULL)
                    """
                ),
                {
                    "harness_run_id": harness_run_id,
                    "gate_name": gate_name,
                    "started_at": _mysql_datetime(started_at),
                    "finished_at": _mysql_datetime(finished_at),
                    "summary_json": summary_json,
                },
            )
    except LegacyNativeAdmissionAttestationPersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - token is already consumed; fail closed.
        raise LegacyNativeAdmissionAttestationPersistenceError(
            "legacy Native admission attestation persistence failed"
        ) from exc


def _with_engine(ctx: GateContext, operation: Callable[[object], None]) -> bool:
    if ctx.engine_factory is None:
        return False
    engine = None
    try:
        engine = ctx.engine_factory()
        operation(engine)
        return True
    except Exception:
        return False
    finally:
        if engine is not None and hasattr(engine, "dispose"):
            engine.dispose()


def _ctx_scheme_version(ctx: GateContext) -> str | None:
    if ctx.config is not None and getattr(ctx.config, "scheme_version", None):
        return ctx.config.scheme_version
    if ctx.authorization is not None and getattr(ctx.authorization, "scheme_version", None):
        return ctx.authorization.scheme_version
    config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
    scheme_dir = config_path.parent
    if config_path.exists() and scheme_dir.exists():
        from shared.versioning import compute_code_hash, compute_config_hash, compute_scheme_version

        return compute_scheme_version(
            compute_code_hash(scheme_dir),
            compute_config_hash(config_path),
        )
    return None


def _result_summary(result: GateResult) -> dict:
    return {
        "passed": result.passed,
        "evidence": [
            {"key": item.key, "value": _jsonable(item.value), "detail": item.detail}
            for item in result.evidence
        ],
        "errors": result.errors,
    }


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, GateStatus):
        return value.value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _mysql_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
