"""周期均值任务复用 Dashboard 的格子、actual 与指标主链路。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard import _collapse_actual_rows
from backend.factor_lab_dashboard_semantics import (
    DashboardDataError,
    live_actual_selector,
)
from shared.task_specs import PERIOD_AVERAGE_TASK_TYPES, TASK_COMBINATIONS


@pytest.mark.parametrize("task_type", sorted(PERIOD_AVERAGE_TASK_TYPES))
def test_period_average_task_selects_its_exact_actual_rule(task_type: str) -> None:
    assert live_actual_selector(task_type) == (
        "period_average",
        TASK_COMBINATIONS[task_type][1],
    )


def test_period_average_actuals_collapse_by_target_rule() -> None:
    monthly_rule = TASK_COMBINATIONS["monthly_average"][1]
    quarterly_rule = TASK_COMBINATIONS["quarterly_average"][1]
    facts, diagnostics = _collapse_actual_rows(
        [
            {
                "actual_kind": "period_average",
                "target_tenor": "10Y",
                "target_date": "2026-06-15",
                "target_rule": monthly_rule,
                "actual_direction": -1,
            },
            {
                "actual_kind": "period_average",
                "target_tenor": "10Y",
                "target_date": "2026-06-30",
                "target_rule": quarterly_rule,
                "actual_direction": 1,
            },
        ],
        active_actual_scopes={
            ("10Y", "period_average", monthly_rule),
            ("10Y", "period_average", quarterly_rule),
        },
    )

    assert facts[("period_average", monthly_rule)][
        ("10Y", "2026-06-15", monthly_rule)
    ] == -1
    assert facts[("period_average", quarterly_rule)][
        ("10Y", "2026-06-30", quarterly_rule)
    ] == 1
    assert diagnostics["actual_direction_conflicts"]["period_average"] == 0


def test_period_average_actual_conflict_fails_closed() -> None:
    target_rule = TASK_COMBINATIONS["annual_average"][1]
    rows = [
        {
            "actual_kind": "period_average",
            "target_tenor": "10Y",
            "target_date": "2026-02-15",
            "target_rule": target_rule,
            "actual_direction": direction,
        }
        for direction in (1, -1)
    ]
    with pytest.raises(DashboardDataError, match="conflicting directions"):
        _collapse_actual_rows(
            rows,
            active_actual_scopes={
                ("10Y", "period_average", target_rule),
            },
        )
