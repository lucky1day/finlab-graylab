from __future__ import annotations

from datetime import date, timedelta

from backtests._base_runner import RunOutput, make_run_output
from shared.blackbox_v2.contracts import BlackboxRequest
from shared.blackbox_v2.history import HistoricalCase


def historical_cases(count: int) -> list[HistoricalCase]:
    """构造 Blackbox 持久化 gate 使用的历史样本。"""
    start = date(2025, 1, 1)
    result = []
    for index in range(count):
        feature = (start + timedelta(days=index * 2)).isoformat()
        target = (start + timedelta(days=index * 2 + 1)).isoformat()
        result.append(
            HistoricalCase(
                request=BlackboxRequest(
                    request_id=f"trial_history:{feature}:{feature}:{target}",
                    predict_date=feature,
                    feature_date=feature,
                    target_date=target,
                    daily_cutoff_key=feature,
                    weekly_cutoff_key=f"2025{index % 52 + 1:02d}",
                    monthly_cutoff_key=f"2025{index % 12 + 1:02d}",
                ),
                label=1 if index % 2 == 0 else -1,
                actual_extra={
                    "actual_fact_key": ["10Y", target],
                    "replay_semantics": (
                        "current_snapshot_as_of_not_historical_vintage"
                    ),
                },
            )
        )
    return result


def backtest_output(count: int = 100) -> RunOutput:
    """构造与持久化表增量断言一致的回测输出。"""
    rows = []
    start = date(2025, 1, 1)
    for index in range(count):
        predict_date = (start + timedelta(days=index)).isoformat()
        target_date = (start + timedelta(days=index + 1)).isoformat()
        direction = 1 if index % 2 == 0 else -1
        rows.append(
            {
                "benchmark_id": "bbv2-history",
                "scheme_id": "trial_history",
                "target_tenor": "10Y",
                "horizon": 1,
                "predict_date": predict_date,
                "feature_date": predict_date,
                "target_date": target_date,
                "label": direction,
                "predicted_direction": direction,
                "model_pred": direction,
                "confidence": None,
                "source_row": {"request_id": f"request-{index}"},
                "extra": {"runtime_type": "blackbox_v2"},
            }
        )
    return make_run_output(
        scheme_id="trial_history",
        data_source="blackbox_v2_current_snapshot_as_of",
        start_date=rows[0]["predict_date"],
        end_date=rows[-1]["predict_date"],
        rows=rows,
        benchmark_id="bbv2-history",
    )
