from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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
    detail: str | None = None


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    status: GateStatus
    passed: bool
    evidence: list[Evidence]
    errors: list[str]
    started_at: str
    finished_at: str
    report_path: Path | None = None


@dataclass(frozen=True)
class OnboardReport:
    scheme_id: str
    predict_date: str
    stage_requested: str
    results: list[GateResult]
    overall_passed: bool
    report_dir: Path
