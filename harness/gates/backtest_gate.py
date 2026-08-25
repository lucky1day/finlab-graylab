from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from harness.operation import (
    operation_scope_sha256,
    verify_direct_operation,
)
from harness.context import GateContext
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.probes.table_guard import PROTECTED_TABLES, diff_snapshots, snapshot_table_counts
from harness.result import Evidence, GateResult, GateStatus
from shared.scheme_config_loader import load_yaml_mapping

BACKTEST_WRITE_ALLOWED_TABLES = (
    "t_backtest_runs",
    "t_backtest_predictions",
    "t_backtest_reproduction_checks",
)


class BacktestGate(Gate):
    name = "backtest"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config_path = ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        config = load_yaml_mapping(config_path)
        runner = _runner_from_config(config)
        runner_args = _runner_args_from_config(config)
        if not runner:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.SKIPPED,
                evidence=[Evidence("reason", "config.backtest.runner is not configured")],
                errors=[],
                started_at=started_at,
                finished_at=finished_at,
            )

        operation = None
        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        try:
            before = snapshot_table_counts(engine, PROTECTED_TABLES)
            if ctx.persist_backtest:
                from scheduler.discovery import load_scheme_config

                cfg = load_scheme_config(config_path)
                operation, operation_errors = verify_direct_operation(
                    ctx.operation,
                    scheme_id=ctx.scheme_id,
                    action="backtest_persist",
                    predict_date=ctx.predict_date,
                    scheme_version=cfg.scheme_version,
                    backtest_start_date=ctx.backtest_start_date,
                )
                if operation is None or operation_errors:
                    finished_at = utc_now()
                    return GateResult(
                        gate_name=self.name,
                        status=GateStatus.BLOCKED,
                        evidence=[Evidence("runner", runner), Evidence("persisted", True)],
                        errors=operation_errors,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                current = run_backtest_runner(
                    runner,
                    ctx.project_root,
                    ctx.timeout_sec,
                    persist=True,
                    algo_env=ctx.algo_env,
                    runner_args=runner_args,
                )
            else:
                current = run_backtest_no_persist(
                    runner,
                    ctx.project_root,
                    ctx.timeout_sec,
                    algo_env=ctx.algo_env,
                    runner_args=runner_args,
                )
        finally:
            after = snapshot_table_counts(engine, PROTECTED_TABLES)
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        errors: list[str] = []
        if current.get("status") != "success":
            errors.append(
                "backtest runner status must be success, "
                f"got {current.get('status')!r}"
            )
        table_deltas = diff_snapshots(before, after)
        errors.extend(_validate_backtest_table_deltas(table_deltas, persist=ctx.persist_backtest))

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            evidence=[
                Evidence("runner", runner),
                Evidence("runner_args", runner_args),
                Evidence("persisted", ctx.persist_backtest),
                Evidence("operator", operation.issued_by if operation else None),
                Evidence(
                    "operation_scope_sha256",
                    operation_scope_sha256(operation) if operation else None,
                ),
                Evidence("protected_table_deltas", table_deltas),
                Evidence("status", current.get("status")),
                Evidence("row_count", _row_count(current)),
                Evidence("monthly_count", _monthly_count(current)),
                Evidence("sample_count", _sample_count(current)),
                Evidence("summary", current.get("summary")),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )


def run_backtest_no_persist(
    runner: str,
    project_root: Path,
    timeout_sec: int,
    *,
    algo_env: str = "forecast_env",
    runner_args: list[str] | None = None,
) -> dict[str, Any]:
    return run_backtest_runner(
        runner,
        project_root,
        timeout_sec,
        persist=False,
        algo_env=algo_env,
        runner_args=runner_args,
    )


def run_backtest_runner(
    runner: str,
    project_root: Path,
    timeout_sec: int,
    persist: bool,
    *,
    algo_env: str = "forecast_env",
    runner_args: list[str] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    cmd = ["conda", "run", "-n", algo_env, "python", "-m", runner]
    cmd.extend(runner_args or [])
    if not persist:
        cmd.append("--no-persist")
    completed = subprocess.run(
        cmd,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(_tail(output))
    return _parse_json_object(output)


def _validate_backtest_table_deltas(deltas: dict[str, int], *, persist: bool) -> list[str]:
    errors: list[str] = []
    for table, delta in deltas.items():
        if persist and table in BACKTEST_WRITE_ALLOWED_TABLES:
            continue
        if delta != 0:
            mode = "persist" if persist else "no-persist"
            errors.append(f"{table} delta must remain 0 for backtest {mode}, got {delta}")
    return errors


def _runner_from_config(config: dict[str, Any]) -> str | None:
    backtest = config.get("backtest")
    if not isinstance(backtest, dict):
        return None
    runner = backtest.get("runner")
    return str(runner) if runner else None


def _runner_args_from_config(config: dict[str, Any]) -> list[str]:
    backtest = config.get("backtest")
    if not isinstance(backtest, dict):
        return []
    raw_args = backtest.get("runner_args")
    if raw_args is None:
        return []
    if not isinstance(raw_args, list) or not all(isinstance(item, str) and item for item in raw_args):
        raise ValueError("backtest.runner_args must be a list of non-empty strings")
    if "--no-persist" in raw_args:
        raise ValueError("backtest.runner_args must not include --no-persist")
    return list(raw_args)


def _parse_json_object(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"backtest runner did not emit JSON object: {_tail(output)}")


def _tail(output: str) -> str:
    return output[-4000:] if len(output) > 4000 else output


def _row_count(payload: dict[str, Any]) -> int | None:
    if "row_count" in payload:
        return int(payload["row_count"])
    return None


def _monthly_count(payload: dict[str, Any]) -> int | None:
    if "monthly_count" in payload:
        return int(payload["monthly_count"])
    return None


def _sample_count(payload: dict[str, Any]) -> int:
    if "row_count" in payload:
        return int(payload["row_count"])
    runs = payload.get("runs", [])
    if isinstance(runs, list):
        return sum(int(run.get("rows") or 0) for run in runs if isinstance(run, dict))
    return 0
