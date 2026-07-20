from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Sequence

from backtests._base_runner import RunOutput, make_run_output
from shared.blackbox_v2.contracts import BlackboxMetadata
from shared.blackbox_v2.history import HistoricalCase, validate_historical_cases
from shared.blackbox_v2.snapshot import BlackboxSnapshot


DATA_SOURCE = "blackbox_v2_current_snapshot_as_of"


def run_blackbox_historical_backtest(
    *,
    metadata: BlackboxMetadata,
    script_path: str | Path,
    cases: Sequence[HistoricalCase],
    snapshot: BlackboxSnapshot,
    scheme_version: str,
    generation_id: str,
    benchmark_id: str,
    harness_run_id: str,
    run_delivery: Callable[..., Sequence[Any]],
    profile: Any,
    budget: Any | None = None,
) -> RunOutput:
    """用原始交付脚本执行真实历史 Request，并转换为平台标准输出。"""
    materialized = validate_historical_cases(cases, expected_count=len(cases))
    if not materialized:
        raise ValueError("Blackbox historical backtest requires at least one case")
    records = run_delivery(
        metadata=metadata,
        script_path=script_path,
        requests=[case.request for case in materialized],
        data_dir=snapshot.data_dir,
        data_snapshot_id=snapshot.snapshot_id,
        profile=profile,
        budget=budget,
    )
    if len(records) != len(materialized):
        raise ValueError(
            f"historical Result count mismatch: expected={len(materialized)}, got={len(records)}"
        )

    rows = []
    for index, (case, record) in enumerate(zip(materialized, records, strict=True)):
        request = case.request
        extra = dict(record.extra or {})
        expected = (
            metadata.scheme_id,
            metadata.target_tenor,
            metadata.horizon,
            request.request_id,
            request.predict_date,
            request.feature_date,
            request.target_date,
        )
        actual = (
            record.scheme_id,
            record.target_tenor,
            record.horizon,
            extra.get("request_id"),
            record.predict_date,
            record.feature_date,
            record.target_date,
        )
        if actual != expected:
            raise ValueError(
                f"historical Result order or echo mismatch at row {index}: "
                f"expected={expected}, got={actual}"
            )
        direction = record.predicted_direction
        if type(direction) is not int or direction not in {-1, 0, 1}:
            raise ValueError(f"invalid historical predicted_direction at row {index}: {direction!r}")
        rows.append(
            {
                "benchmark_id": benchmark_id,
                "scheme_id": metadata.scheme_id,
                "target_tenor": metadata.target_tenor,
                "horizon": metadata.horizon,
                "predict_date": request.predict_date,
                "feature_date": request.feature_date,
                "target_date": request.target_date,
                "label": case.label,
                "predicted_direction": direction,
                "model_pred": direction,
                "confidence": record.confidence,
                "source_row": {
                    **asdict(request),
                    "actual": dict(case.actual_extra),
                },
                "extra": {
                    **extra,
                    "request_id": request.request_id,
                    "runtime_type": "blackbox_v2",
                    "target_rule": metadata.target_rule,
                    "scheme_version": scheme_version,
                    "generation_id": generation_id,
                    "data_snapshot_id": snapshot.snapshot_id,
                    "replay_semantics": "current_snapshot_as_of_not_historical_vintage",
                },
            }
        )
    _require_unique_persisted_dates(rows)
    output = make_run_output(
        scheme_id=metadata.scheme_id,
        data_source=DATA_SOURCE,
        start_date=min(row["predict_date"] for row in rows),
        end_date=max(row["predict_date"] for row in rows),
        rows=rows,
        benchmark_id=benchmark_id,
    )
    output.summary.update(
        {
            "scheme_version": scheme_version,
            "generation_id": generation_id,
            "data_snapshot_id": snapshot.snapshot_id,
            "harness_run_id": harness_run_id,
            "batch_count": (
                len(materialized) + profile.max_batch_requests - 1
            ) // profile.max_batch_requests,
            "batch_sizes": [
                min(profile.max_batch_requests, len(materialized) - start)
                for start in range(0, len(materialized), profile.max_batch_requests)
            ],
            "max_batch_requests": profile.max_batch_requests,
        }
    )
    return output


def _require_unique_persisted_dates(rows: list[dict]) -> None:
    predict_dates = [str(row["predict_date"]) for row in rows]
    if len(predict_dates) != len(set(predict_dates)):
        raise ValueError("Blackbox persisted backtest contains duplicate predict_date")
