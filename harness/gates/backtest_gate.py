from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.authorization import (
    mark_token_used,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)
from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.probes.table_guard import PROTECTED_TABLES, diff_snapshots, snapshot_table_counts
from harness.result import Evidence, GateResult, GateStatus


IGNORE_PATHS = frozenset({"$.elapsed_sec"})
BACKTEST_WRITE_ALLOWED_TABLES = (
    "t_backtest_runs",
    "t_backtest_predictions",
    "t_backtest_monthly_metrics",
    "t_backtest_reproduction_checks",
)


class BacktestGate(Gate):
    name = "backtest"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_config_raw(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        runner = _runner_from_config(config)
        if not runner:
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.SKIPPED,
                passed=True,
                evidence=[Evidence("reason", "config.backtest.runner is not configured")],
                errors=[],
                started_at=started_at,
                finished_at=finished_at,
            )

        audit_path: Path | None = None
        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        try:
            before = snapshot_table_counts(engine, PROTECTED_TABLES)
            if ctx.persist_backtest:
                auth, auth_errors = verify_authorization(
                    ctx.authorization,
                    scheme_id=ctx.scheme_id,
                    action="backtest_persist",
                    predict_date=ctx.predict_date,
                    used_store_path=used_tokens_path(ctx.project_root),
                )
                if auth_errors:
                    finished_at = utc_now()
                    return GateResult(
                        gate_name=self.name,
                        status=GateStatus.BLOCKED,
                        passed=False,
                        evidence=[Evidence("runner", runner), Evidence("persisted", True)],
                        errors=auth_errors,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                audit_path = write_authorization_audit(auth, ctx.report_dir / "backtest_authorization")
                mark_token_used(auth, used_tokens_path(ctx.project_root))
                current = run_backtest_runner(runner, ctx.project_root, ctx.timeout_sec, persist=True)
            else:
                current = run_backtest_no_persist(runner, ctx.project_root, ctx.timeout_sec)
        finally:
            after = snapshot_table_counts(engine, PROTECTED_TABLES)
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        baseline_path = ctx.project_root / "reports" / "refactor_baseline" / ctx.scheme_id / "backtest_no_persist.json"
        errors: list[str] = []
        diff_count: int | None = None
        first_diffs: list[str] = []
        baseline_bootstrapped = False
        if not ctx.persist_backtest:
            if not baseline_path.exists():
                # 基线缺失：将本次 no-persist 输出写为新基线（自举），并判定通过。
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                baseline_path.write_text(
                    json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                baseline_bootstrapped = True
            else:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                diffs = compare_json(baseline, current, ignore_paths=IGNORE_PATHS)
                diff_count = len(diffs)
                first_diffs = diffs[:20]
                if diffs:
                    errors.append(f"backtest output differs from baseline: diff_count={len(diffs)}")
        table_deltas = diff_snapshots(before, after)
        errors.extend(_validate_backtest_table_deltas(table_deltas, persist=ctx.persist_backtest))

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("runner", runner),
                Evidence("persisted", ctx.persist_backtest),
                Evidence("authorization_audit_path", str(audit_path) if audit_path else None),
                Evidence("baseline_path", str(baseline_path)),
                Evidence("baseline_bootstrapped", baseline_bootstrapped),
                Evidence("protected_table_counts_before", before),
                Evidence("protected_table_counts_after", after),
                Evidence("protected_table_deltas", table_deltas),
                Evidence("diff_count", diff_count),
                Evidence("first_diffs", first_diffs),
                Evidence("status", current.get("status")),
                Evidence("row_count", _row_count(current)),
                Evidence("monthly_count", _monthly_count(current)),
                Evidence("sample_count", _sample_count(current)),
                Evidence("summary", current.get("summary")),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=audit_path,
        )


def run_backtest_no_persist(runner: str, project_root: Path, timeout_sec: int) -> dict[str, Any]:
    return run_backtest_runner(runner, project_root, timeout_sec, persist=False)


def run_backtest_runner(runner: str, project_root: Path, timeout_sec: int, persist: bool) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    cmd = [sys.executable, "-m", runner]
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


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _validate_backtest_table_deltas(deltas: dict[str, int], *, persist: bool) -> list[str]:
    errors: list[str] = []
    for table, delta in deltas.items():
        if persist and table in BACKTEST_WRITE_ALLOWED_TABLES:
            continue
        if delta != 0:
            mode = "persist" if persist else "no-persist"
            errors.append(f"{table} delta must remain 0 for backtest {mode}, got {delta}")
    return errors


def compare_json(expected: Any, actual: Any, *, ignore_paths: frozenset[str] = IGNORE_PATHS) -> list[str]:
    diffs: list[str] = []
    _compare(expected, actual, "$", ignore_paths, diffs)
    return diffs


def _compare(expected: Any, actual: Any, path: str, ignore_paths: frozenset[str], diffs: list[str]) -> None:
    if path in ignore_paths:
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}"
            if child_path in ignore_paths:
                continue
            if key not in expected:
                diffs.append(f"{child_path}: unexpected key")
            elif key not in actual:
                diffs.append(f"{child_path}: missing key")
            else:
                _compare(expected[key], actual[key], child_path, ignore_paths, diffs)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append(f"{path}: length {len(expected)} != {len(actual)}")
            return
        for index, (left, right) in enumerate(zip(expected, actual)):
            _compare(left, right, f"{path}[{index}]", ignore_paths, diffs)
        return
    if expected != actual:
        diffs.append(f"{path}: {expected!r} != {actual!r}")


def _runner_from_config(config: dict[str, Any]) -> str | None:
    backtest = config.get("backtest")
    if not isinstance(backtest, dict):
        return None
    runner = backtest.get("runner")
    return str(runner) if runner else None


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


def _tail(output: str, limit: int = 4000) -> str:
    return output[-limit:] if len(output) > limit else output


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
