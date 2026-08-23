"""任务类型、目标规则与业务频率的唯一共享规格。"""

from __future__ import annotations


TASK_COMBINATIONS: dict[str, tuple[int, str, str]] = {
    "T+1": (1, "target_date_yield_vs_feature_date_yield", "daily"),
    "T+5": (5, "target_date_yield_vs_feature_date_yield", "daily"),
    "weekly_point": (
        1,
        "target_week_end_yield_vs_feature_week_end_yield",
        "weekly",
    ),
    "weekly_average": (
        1,
        "target_week_average_yield_vs_feature_week_average_yield",
        "weekly",
    ),
    "monthly": (
        1,
        "target_month_observation_yield_vs_feature_month_observation_yield",
        "monthly",
    ),
    "monthly_average": (
        1,
        "target_month_average_yield_vs_feature_month_average_yield",
        "monthly",
    ),
    "quarterly_average": (
        1,
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    ),
    "annual_average": (
        1,
        "target_year_average_yield_vs_feature_year_average_yield",
        "annual",
    ),
}

PERIOD_AVERAGE_TASK_TYPES = frozenset(
    {"monthly_average", "quarterly_average", "annual_average"}
)
ALLOWED_TASK_TYPES = frozenset(TASK_COMBINATIONS)
ALLOWED_FREQUENCIES = frozenset(
    frequency for _horizon, _target_rule, frequency in TASK_COMBINATIONS.values()
)
ALLOWED_DATA_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})


def task_combination(task_type: str) -> tuple[int, str, str]:
    """返回精确任务规格；未知任务直接失败。"""
    try:
        return TASK_COMBINATIONS[str(task_type)]
    except KeyError as exc:
        raise ValueError(f"unsupported task_type: {task_type}") from exc
