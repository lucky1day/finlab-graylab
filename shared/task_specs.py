"""任务类型、目标规则与业务频率的唯一共享规格。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Collection


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

NATIVE_V1_TASK_HORIZONS: dict[str, int] = {
    "T+1": 1,
    "T+5": 5,
    "weekly_point": 6,
    "weekly_average": 6,
    "monthly": 30,
}
NATIVE_V1_SCHEME_TASKS: dict[str, str] = {
    "daily_10y_lgbm_10y04_0629": "T+1",
    "daily_1y_xgb_1y13_0629": "T+1",
    "daily_5y_lgbm_5y10_0629": "T+1",
    "monthly_10y_rf_top5_0629": "monthly",
    "monthly_1y_rf_top30_0629": "monthly",
    "monthly_5y_knn_top20_0629": "monthly",
    "weekly_avg_10y_lgbm_0529": "weekly_average",
    "weekly_avg_1y_lgbm_0529": "weekly_average",
    "weekly_avg_5y_lgbm_0529": "weekly_average",
}


@dataclass(frozen=True, slots=True)
class RuntimeTaskContract:
    """一个运行时下任务类型的固定事实合同。"""

    horizon: int
    target_rule: str
    frequency: str


def load_legacy_native_scheme_ids(project_root: str | Path) -> frozenset[str]:
    """从版本化入库策略读取允许维护的固定 Native 身份。"""
    path = Path(project_root) / "deploy" / "onboarding_policy_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("policy_version") != "1.0":
        raise ValueError("onboarding policy schema is invalid")
    values = raw.get("legacy_native_scheme_ids")
    if (
        not isinstance(values, list)
        or not values
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            for value in values
        )
        or len(values) != len(set(values))
    ):
        raise ValueError("onboarding policy legacy Native ids are invalid")
    return frozenset(values)


def runtime_task_contract(
    *,
    runtime_type: str,
    task_type: str,
    base_scheme_id: str,
    legacy_native_scheme_ids: Collection[str],
) -> RuntimeTaskContract:
    """按运行时和登记身份返回唯一任务合同，未知组合 fail-closed。"""
    blackbox = TASK_COMBINATIONS.get(task_type)
    if runtime_type == "blackbox_v2":
        if blackbox is None:
            raise ValueError("unsupported Blackbox task type")
        return RuntimeTaskContract(*blackbox)
    if runtime_type != "native_adapter":
        raise ValueError("unsupported runtime type")
    if base_scheme_id not in legacy_native_scheme_ids:
        raise ValueError("Native scheme is not registered for maintenance")
    if NATIVE_V1_SCHEME_TASKS.get(base_scheme_id) != task_type:
        raise ValueError("Native scheme task contract does not match its fixed version")
    native_horizon = NATIVE_V1_TASK_HORIZONS.get(task_type)
    if blackbox is None or native_horizon is None:
        raise ValueError("unsupported Native task type")
    return RuntimeTaskContract(
        horizon=native_horizon,
        target_rule=blackbox[1],
        frequency=blackbox[2],
    )

PERIOD_AVERAGE_TASK_TYPES = frozenset(
    {"monthly_average", "quarterly_average", "annual_average"}
)
WEEKLY_TASK_TYPES = frozenset({"weekly_point", "weekly_average"})
PREDICTION_CADENCES = frozenset(
    {"daily", "weekly", "monthly", "period_average"}
)
ALLOWED_TASK_TYPES = frozenset(TASK_COMBINATIONS)
ALLOWED_FREQUENCIES = frozenset(
    frequency for _horizon, _target_rule, frequency in TASK_COMBINATIONS.values()
)
ALLOWED_DATA_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})
