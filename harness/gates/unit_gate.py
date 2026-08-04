from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


_NATIVE_PREACTIVATION_EXCLUDED_MODULES = frozenset(
    {
        "tests.test_daily_gray_launchd_policy",
        "tests.test_daily_policy",
        "tests.test_daily_runtime",
    }
)


class UnitGate(Gate):
    name = "unit"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        native_preactivation = (
            ctx.scheme_id == "t1_daily"
            and getattr(ctx.config, "runtime_type", None) == "native_adapter"
            and getattr(ctx.config, "status", None) == "paused"
        )
        modules = _select_test_modules(
            ctx.project_root,
            ctx.scheme_id,
            native_preactivation=native_preactivation,
        )
        errors: list[str] = []
        if not modules:
            errors.append(f"no unittest modules selected for scheme_id={ctx.scheme_id}")
            return _result(started_at, modules, None, errors)

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", *modules],
            cwd=ctx.project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=ctx.timeout_sec,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        tests_run = _parse_tests_run(output)
        if completed.returncode != 0:
            errors.append(_tail(output))
        return _result(started_at, modules, completed.returncode, errors, tests_run, output)


def _select_test_modules(
    project_root: Path,
    scheme_id: str,
    *,
    native_preactivation: bool = False,
) -> list[str]:
    """选择方案测试；仅 t1_daily 预激活期排除已退役日频控制面断言。"""
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        return []
    modules: list[str] = []
    for path in sorted(tests_dir.glob("test*.py")):
        if path.stem.startswith("test_harness_"):
            continue
        text = path.read_text(encoding="utf-8")
        if scheme_id in text or scheme_id in path.stem:
            module = f"tests.{path.stem}"
            if (
                native_preactivation
                and module in _NATIVE_PREACTIVATION_EXCLUDED_MODULES
            ):
                continue
            modules.append(module)
    return modules


def _parse_tests_run(output: str) -> int:
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    return int(match.group(1)) if match else 0


def _tail(output: str, limit: int = 4000) -> str:
    return output[-limit:] if len(output) > limit else output


def _result(
    started_at: str,
    modules: list[str],
    returncode: int | None,
    errors: list[str],
    tests_run: int = 0,
    output: str = "",
) -> GateResult:
    finished_at = utc_now()
    status = GateStatus.PASSED if not errors else GateStatus.FAILED
    return GateResult(
        gate_name=UnitGate.name,
        status=status,
        passed=status == GateStatus.PASSED,
        evidence=[
            Evidence("test_modules", modules),
            Evidence("tests_run", tests_run),
            Evidence("tests_passed", status == GateStatus.PASSED),
            Evidence("returncode", returncode),
            Evidence("output_tail", _tail(output) if output else ""),
        ],
        errors=errors,
        started_at=started_at,
        finished_at=finished_at,
    )
