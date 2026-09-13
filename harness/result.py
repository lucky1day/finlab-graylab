from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Evidence:
    key: str
    value: Any


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    status: GateStatus
    evidence: list[Evidence]
    errors: list[str]
    started_at: str
    finished_at: str

    @property
    def passed(self) -> bool:
        """PASSED 与 SKIPPED 都是非阻塞结果。"""
        return self.status in {GateStatus.PASSED, GateStatus.SKIPPED}
