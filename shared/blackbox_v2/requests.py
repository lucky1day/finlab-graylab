from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from shared.blackbox_v2.contracts import (
    REQUEST_FIELDS,
    BlackboxMetadata,
    BlackboxRequest,
    request_from_mapping,
)
from shared.blackbox_v2.snapshot import CutoffKeys
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_period_average_live_context,
    build_weekly_live_context,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES


def build_live_request(
    metadata: BlackboxMetadata,
    *,
    predict_date: str,
    calendar,
    cutoffs: CutoffKeys,
) -> BlackboxRequest:
    """按平台统一日期语义生成单次实盘 Request。"""
    context = resolve_live_context(
        metadata,
        predict_date=predict_date,
        calendar=calendar,
    )
    if (
        metadata.task_type in PERIOD_AVERAGE_TASK_TYPES
        and cutoffs.daily_cutoff_key != context.feature_date
    ):
        raise ValueError(
            "period-average daily_cutoff_key must equal feature_date: "
            f"{cutoffs.daily_cutoff_key} != {context.feature_date}"
        )
    return build_request(
        scheme_id=metadata.scheme_id,
        predict_date=predict_date,
        feature_date=context.feature_date,
        target_date=context.target_date,
        cutoffs=cutoffs,
    )


def resolve_live_context(
    metadata: BlackboxMetadata,
    *,
    predict_date: str,
    calendar,
):
    """按显式 task type/frequency 解析唯一 live 日期上下文。"""
    if metadata.task_type in PERIOD_AVERAGE_TASK_TYPES:
        return build_period_average_live_context(
            calendar,
            predict_date,
            task_type=metadata.task_type,
        )
    if metadata.frequency == "daily":
        context = build_daily_live_context(calendar, predict_date, horizon=metadata.horizon)
    elif metadata.frequency == "weekly":
        context = build_weekly_live_context(calendar, predict_date)
    elif metadata.frequency == "monthly":
        context = build_monthly_live_context(calendar, predict_date)
    else:
        raise ValueError(f"unsupported Blackbox V2 frequency: {metadata.frequency}")
    return context


def build_request(
    *,
    scheme_id: str,
    predict_date: str,
    feature_date: str,
    target_date: str,
    cutoffs: CutoffKeys,
) -> BlackboxRequest:
    """由平台已计算日期和截止键构造并严格校验 Request。"""
    request_id = f"{scheme_id}:{predict_date}:{feature_date}:{target_date}"
    return request_from_mapping(
        {
            "request_id": request_id,
            "predict_date": predict_date,
            "feature_date": feature_date,
            "target_date": target_date,
            "daily_cutoff_key": cutoffs.daily_cutoff_key,
            "weekly_cutoff_key": cutoffs.weekly_cutoff_key,
            "monthly_cutoff_key": cutoffs.monthly_cutoff_key,
        }
    )


def write_request(request: BlackboxRequest, path: str | Path) -> Path:
    destination = Path(path)
    payload = json.dumps(asdict(request), ensure_ascii=True, indent=2) + "\n"
    _atomic_write_text(destination, payload)
    return destination


def write_requests(requests: Sequence[BlackboxRequest], path: str | Path) -> Path:
    if not requests:
        raise ValueError("Request batch must contain at least one row")
    request_ids = [request.request_id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Request batch contains duplicate request_id values")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(REQUEST_FIELDS), lineterminator="\n")
            writer.writeheader()
            for request in requests:
                writer.writerow(asdict(request))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _atomic_write_text(destination: Path, payload: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
