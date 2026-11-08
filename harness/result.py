from __future__ import annotations

from collections.abc import Mapping
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


def redact_gate_result(result: GateResult, secret: str | None) -> GateResult:
    """在 Gate 返回边界递归清除会话回显，保留状态、时间和容器形状。"""
    return GateResult(
        gate_name=result.gate_name,
        status=result.status,
        evidence=[
            Evidence(item.key, _redact_secret(item.value, secret))
            for item in result.evidence
        ],
        errors=[str(_redact_secret(error, secret)) for error in result.errors],
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


def _redact_secret(value: Any, secret: str | None) -> Any:
    """返回去除 secret 的副本，不修改调用方的嵌套证据。"""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[redacted-session]")
    if isinstance(value, Mapping):
        return {
            _redact_secret(key, secret): _redact_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret(item, secret) for item in value)
    return value
