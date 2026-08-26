from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
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
    """记录 harness run 开始；DB 不可用时返回 False，由编排层阻断。"""

    def operation(engine) -> None:
        sql = text(
            """
            INSERT INTO t_harness_runs
                (harness_run_id, scheme_id, scheme_version, stage, status, started_at,
                 git_commit)
            VALUES
                (:harness_run_id, :scheme_id, :scheme_version, :stage, 'running', :started_at,
                 :git_commit)
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
                    "git_commit": _release_commit(),
                },
            )

    return _with_engine(ctx, operation)


def persist_harness_run_complete(
    ctx: GateContext,
    *,
    harness_run_id: str,
    status: str,
    finished_at: str,
    results: list[GateResult],
) -> bool:
    """在一个事务中批量保存 Gate 结果并完成 Harness run。"""

    def operation(engine) -> None:
        gate_sql = text(
            """
            INSERT INTO t_harness_gate_results
                (harness_run_id, gate_name, status, started_at, finished_at, summary_json)
            VALUES
                (:harness_run_id, :gate_name, :status, :started_at, :finished_at,
                 CAST(:summary_json AS JSON))
            """
        )
        with engine.begin() as conn:
            if results:
                conn.execute(
                    gate_sql,
                    [
                        {
                            "harness_run_id": harness_run_id,
                            "gate_name": result.gate_name,
                            "status": result.status.value,
                            "started_at": _mysql_datetime(result.started_at),
                            "finished_at": _mysql_datetime(result.finished_at),
                            "summary_json": json.dumps(
                                _result_summary(result),
                                ensure_ascii=False,
                            ),
                        }
                        for result in results
                    ],
                )
            update = conn.execute(
                text(
                    """
                    UPDATE t_harness_runs
                    SET status = :status,
                        finished_at = :finished_at
                    WHERE harness_run_id = :harness_run_id
                    """
                ),
                {
                    "harness_run_id": harness_run_id,
                    "status": status,
                    "finished_at": _mysql_datetime(finished_at),
                },
            )
            if update.rowcount != 1:
                raise RuntimeError(
                    "harness run completion must update exactly one row"
                )

    if _with_engine(ctx, operation):
        return True
    return _read_back_completed_run(
        ctx,
        harness_run_id=harness_run_id,
        status=status,
        results=results,
    )


def persist_harness_run_finish(
    ctx: GateContext,
    *,
    harness_run_id: str,
    status: str,
    finished_at: str,
) -> bool:
    """记录 harness run 结束；DB 不可用时返回 False，由编排层阻断。"""

    def operation(engine) -> None:
        sql = text(
            """
            UPDATE t_harness_runs
            SET status = :status,
                finished_at = :finished_at
            WHERE harness_run_id = :harness_run_id
            """
        )
        with engine.begin() as conn:
            result = conn.execute(
                sql,
                {
                    "harness_run_id": harness_run_id,
                    "status": status,
                    "finished_at": _mysql_datetime(finished_at),
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "harness run finish must update exactly one row"
                )

    return _with_engine(ctx, operation)


def _read_back_completed_run(
    ctx: GateContext,
    *,
    harness_run_id: str,
    status: str,
    results: list[GateResult],
) -> bool:
    """commit ACK 不确定时用新连接核对精确最终状态。"""
    expected = {
        result.gate_name: {
            "status": result.status.value,
            "summary": _result_summary(result),
        }
        for result in results
    }
    if len(expected) != len(results):
        return False

    for _attempt in range(3):
        state: dict[str, object] = {}

        def operation(engine) -> None:
            with engine.begin() as conn:
                run_row = conn.execute(
                    text(
                        "SELECT status FROM t_harness_runs "
                        "WHERE harness_run_id = :harness_run_id"
                    ),
                    {"harness_run_id": harness_run_id},
                ).mappings().one_or_none()
                gate_rows = conn.execute(
                    text(
                        "SELECT gate_name, status, summary_json "
                        "FROM t_harness_gate_results "
                        "WHERE harness_run_id = :harness_run_id"
                    ),
                    {"harness_run_id": harness_run_id},
                ).mappings().all()
            state["run_row"] = run_row
            state["gate_rows"] = gate_rows

        if not _with_engine(ctx, operation):
            continue
        run_row = state.get("run_row")
        gate_rows = state.get("gate_rows")
        if not isinstance(run_row, Mapping) or not isinstance(gate_rows, list):
            continue
        if str(run_row.get("status") or "") != status:
            continue
        actual: dict[str, dict[str, object]] = {}
        duplicate = False
        for row in gate_rows:
            if not isinstance(row, Mapping):
                duplicate = True
                break
            gate_name = str(row.get("gate_name") or "")
            if not gate_name or gate_name in actual:
                duplicate = True
                break
            actual[gate_name] = {
                "status": str(row.get("status") or ""),
                "summary": _decoded_json(row.get("summary_json")),
            }
        if not duplicate and actual == expected:
            return True
    return False


def _release_commit() -> str | None:
    """本次运行所属的 release commit；无法确定时返回 None。"""
    try:
        return resolve_code_commit()
    except Exception:
        return None


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
    if ctx.operation is not None and getattr(ctx.operation, "scheme_version", None):
        return ctx.operation.scheme_version
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
            {"key": item.key, "value": _jsonable(item.value)}
            for item in result.evidence
        ],
        "errors": result.errors,
    }


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, GateStatus):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _decoded_json(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _mysql_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
