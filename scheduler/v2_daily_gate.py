"""Blackbox V2 scheduler 日级就绪凭证。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal, Mapping, Sequence

from shared.data_bridge.refresh import DataBridgeRefreshConfig, check_current_dataset


SCHEMA_VERSION = "v2-scheduler-gate-v1"
GateStatus = Literal["checking", "ready", "blocked"]


class V2DailyGateBlocked(RuntimeError):
    """当天 Blackbox V2 scheduler 未获得 DataBridge 就绪授权。"""


def gate_record_path(config: DataBridgeRefreshConfig, run_date: str) -> Path:
    """返回指定生产日的 V2 日级凭证路径。"""
    normalized = date.fromisoformat(run_date).isoformat()
    return config.runtime_root / "v2_scheduler_gate" / f"{normalized}.json"


def write_gate_record(
    config: DataBridgeRefreshConfig,
    *,
    run_date: str,
    status: GateStatus | str,
    checked_at: str,
    generation_id: str | None,
    refresh_date: str | None,
    expected_daily_date: str,
    business_digest: str | None,
    checks: Sequence[Mapping[str, object]],
    restart: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """以原子替换写入当天 V2 Gate 审计凭证。"""
    if status not in {"checking", "ready", "blocked"}:
        raise ValueError(f"unsupported V2 daily gate status: {status}")
    normalized_run_date = date.fromisoformat(run_date).isoformat()
    normalized_expected_date = date.fromisoformat(expected_daily_date).isoformat()
    normalized_refresh_date = (
        date.fromisoformat(refresh_date).isoformat() if refresh_date is not None else None
    )
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_date": normalized_run_date,
        "status": status,
        "checked_at": str(checked_at),
        "generation_id": generation_id,
        "refresh_date": normalized_refresh_date,
        "expected_daily_date": normalized_expected_date,
        "business_digest": business_digest,
        "checks": [dict(item) for item in checks],
        "restart": dict(restart or {}),
    }
    path = gate_record_path(config, normalized_run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v2-gate-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return record


def load_gate_record(config: DataBridgeRefreshConfig, run_date: str) -> dict[str, object]:
    """读取指定生产日的 V2 Gate；缺失或损坏均 fail-closed。"""
    path = gate_record_path(config, run_date)
    if not path.is_file():
        raise V2DailyGateBlocked(f"V2 daily gate is missing for {run_date}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2DailyGateBlocked(f"V2 daily gate is invalid for {run_date}: {exc}") from exc
    if not isinstance(payload, dict):
        raise V2DailyGateBlocked(f"V2 daily gate is invalid for {run_date}")
    return payload


def require_v2_daily_ready(
    config: DataBridgeRefreshConfig,
    run_date: str,
    expected_daily_date: str,
) -> dict[str, object]:
    """校验当天凭证仍与当前 DataBridge generation 完全一致。"""
    record = load_gate_record(config, run_date)
    if record.get("schema_version") != SCHEMA_VERSION:
        raise V2DailyGateBlocked("V2 daily gate schema mismatch")
    if record.get("run_date") != run_date or record.get("status") != "ready":
        raise V2DailyGateBlocked(f"V2 daily gate is not ready for {run_date}")
    if record.get("expected_daily_date") != expected_daily_date:
        raise V2DailyGateBlocked("V2 daily gate expected_daily_date mismatch")
    try:
        current = check_current_dataset(
            config,
            required_refresh_date=run_date,
            expected_daily_date=expected_daily_date,
        )
    except Exception as exc:
        raise V2DailyGateBlocked(f"V2 daily current dataset validation failed: {exc}") from exc
    for field in ("generation_id", "refresh_date", "business_digest"):
        if record.get(field) != current.state.get(field):
            raise V2DailyGateBlocked(f"V2 daily gate {field} mismatch")
    return record


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
