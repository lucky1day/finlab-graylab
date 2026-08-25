from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable

from harness.context import GateContext
from harness.result import Evidence, GateResult, GateStatus


class Gate(ABC):
    name: str

    @abstractmethod
    def run(self, ctx: GateContext) -> GateResult:
        """运行 gate 并返回统一结果。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_default_engine():
    """创建未由测试或调用方显式注入的 Harness 数据库 Engine。"""
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def guarded_result(gate_name: str, runner: Callable[[str], GateResult]) -> GateResult:
    started_at = utc_now()
    try:
        return runner(started_at)
    except Exception as exc:
        finished_at = utc_now()
        return GateResult(
            gate_name=gate_name,
            status=GateStatus.FAILED,
            evidence=[Evidence("exception_type", type(exc).__name__)],
            errors=[str(exc)],
            started_at=started_at,
            finished_at=finished_at,
        )
