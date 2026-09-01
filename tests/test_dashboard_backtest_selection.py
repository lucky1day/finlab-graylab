"""Dashboard 回测候选选择保持两层 latest 语义并复用排名。"""

from __future__ import annotations

from collections import Counter

import backend.factor_lab_dashboard_semantics as semantics


def test_backtest_candidate_rank_is_evaluated_once_per_row(
    monkeypatch,
) -> None:
    registry_rows = [
        {
            "scheme_id": "demo__h1__5Y",
            "base_scheme_id": "demo",
            "runtime_type": "blackbox_v2",
            "status": "active",
        },
        {
            "scheme_id": "demo__h1__10Y",
            "base_scheme_id": "demo",
            "runtime_type": "blackbox_v2",
            "status": "active",
        },
    ]
    run_rows = [
        {
            "id": 1,
            "benchmark_id": "benchmark-a",
            "scheme_id": "demo",
            "data_source": "blackbox_v2_current_snapshot_as_of",
            "status": "success",
            "updated_at": "2026-08-01T12:00:00+08:00",
        },
        {
            "id": 2,
            "benchmark_id": "benchmark-a",
            "scheme_id": "demo",
            "data_source": "blackbox_v2_current_snapshot_as_of",
            "status": "success",
            "updated_at": "2026-08-02T12:00:00+08:00",
        },
        {
            "id": 3,
            "benchmark_id": "benchmark-b",
            "scheme_id": "demo",
            "data_source": "blackbox_v2_current_snapshot_as_of",
            "status": "success",
            "updated_at": "2026-08-03T12:00:00+08:00",
        },
    ]
    original_rank = semantics._backtest_run_rank
    rank_calls: Counter[int] = Counter()

    def _counted_rank(row):
        rank_calls[int(row["id"])] += 1
        return original_rank(row)

    monkeypatch.setattr(semantics, "_backtest_run_rank", _counted_rank)

    selected = semantics.choose_latest_backtest_runs(
        run_rows,
        registry_rows,
    )

    assert {key: row["id"] for key, row in selected.items()} == {
        "demo__h1__5Y": 3,
        "demo__h1__10Y": 3,
    }
    assert rank_calls == Counter({1: 1, 2: 1, 3: 1})
