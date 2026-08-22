from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from harness.context import GateContext
from harness.result import GateResult, GateStatus
from shared.service_instance import resolve_code_commit


# Approved harness control-plane write boundary.
# This module may write only t_harness_runs and t_harness_gate_results; it must
# never write prediction, backtest, registry, source, or other business tables.


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
                    "git_commit": _release_commit(ctx),
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


def _release_commit(ctx: GateContext) -> str | None:
    """本次运行所属的 release commit；无法确定时返回 None。"""
    try:
        return resolve_code_commit(ctx.project_root)
    except Exception:
        return None


def find_passed_gate_run(
    ctx: GateContext,
    *,
    gate_name: str,
    scheme_version: str,
) -> str | None:
    """查找同一方案、同一精确版本、同一 release commit 下已通过的 gate run。

    命中返回该 `harness_run_id`。任何不确定——数据库不可用、commit 未知、
    版本为空、查询失败——一律返回 None，由调用方执行完整检查。
    """
    commit = _release_commit(ctx)
    if not commit or not str(scheme_version or "").strip():
        return None
    if ctx.engine_factory is None:
        return None
    engine = None
    try:
        engine = ctx.engine_factory()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT r.harness_run_id
                    FROM t_harness_runs AS r
                    JOIN t_harness_gate_results AS g
                      ON g.harness_run_id = r.harness_run_id
                    WHERE r.scheme_id = :scheme_id
                      AND r.scheme_version = :scheme_version
                      AND r.git_commit = :git_commit
                      AND g.gate_name = :gate_name
                      AND g.status = 'passed'
                    ORDER BY g.finished_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "scheme_id": ctx.scheme_id,
                    "scheme_version": str(scheme_version).strip(),
                    "git_commit": commit,
                    "gate_name": gate_name,
                },
            ).first()
    except Exception:
        return None
    finally:
        if engine is not None and hasattr(engine, "dispose"):
            engine.dispose()
    return str(row[0]) if row else None


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
