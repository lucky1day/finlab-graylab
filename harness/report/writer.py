from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from harness.context import GateContext
from harness.result import GateResult, GateStatus, OnboardReport


def write_gate_result(ctx: GateContext, result: GateResult, filename: str | None = None) -> Path:
    """写出单个 GateResult JSON。"""
    ctx.report_dir.mkdir(parents=True, exist_ok=True)
    output = ctx.report_dir / (filename or f"{result.gate_name}.json")
    output.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def write_onboard_report(report: OnboardReport, filename: str = "onboard_report.json") -> Path:
    """写出 OnboardReport JSON。"""
    report.report_dir.mkdir(parents=True, exist_ok=True)
    output = report.report_dir / filename
    output.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, GateStatus):
        return value.value
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
