from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .lgbm_predictor import PredictionResult


LEGACY_STATS_NAMES = ["均值", "方差", "中位数", "极差", "25分位数", "75分位数", "三阶矩", "偏度"]


def aggregate_shap_to_original_factors(
    shap_rows: Iterable[Mapping[str, object]],
    feature_origin_map: Mapping[str, list[str]],
) -> dict[str, dict[str, float]]:
    """Aggregate derived-feature SHAP values back to original indicator codes.

    A single-source feature contributes all of its SHAP value to that source.
    A multi-source spread or ratio feature is split evenly across its sources.
    """
    aggregate: dict[str, dict[str, float]] = defaultdict(lambda: {"shap_sum": 0.0, "abs_shap_sum": 0.0})
    for row in shap_rows:
        feature = str(row.get("feature", "")).strip()
        if not feature:
            continue
        shap_value = float(row.get("shap_value", 0.0) or 0.0)
        origins = [str(item).strip() for item in feature_origin_map.get(feature, [feature]) if str(item).strip()]
        if not origins:
            origins = [feature]
        share = shap_value / len(origins)
        abs_share = abs(shap_value) / len(origins)
        for code in origins:
            aggregate[code]["shap_sum"] += share
            aggregate[code]["abs_shap_sum"] += abs_share
    return {code: dict(values) for code, values in aggregate.items()}


def shap_aggregation_records(
    shap_rows: Iterable[Mapping[str, object]],
    feature_origin_map: Mapping[str, list[str]],
) -> list[dict[str, float | str]]:
    aggregated = aggregate_shap_to_original_factors(shap_rows, feature_origin_map)
    return [
        {"indicators_code": code, "shap_sum": values["shap_sum"], "abs_shap_sum": values["abs_shap_sum"]}
        for code, values in sorted(aggregated.items(), key=lambda item: item[1]["abs_shap_sum"], reverse=True)
    ]


def restore_legacy_factor_name(factor_name: str) -> str:
    if "_滞后" in factor_name:
        return factor_name.split("_滞后")[0]
    for stat in LEGACY_STATS_NAMES:
        if f"_{stat}" in factor_name:
            return factor_name.split(f"_{stat}")[0]
    return factor_name


def legacy_shap_records(
    rdate: str,
    frequency: str,
    shap_rows: Iterable[Mapping[str, object]],
    feature_origin_map: Mapping[str, list[str]] | None = None,
    up_or_down: str | None = None,
) -> list[dict[str, object]]:
    feature_origin_map = feature_origin_map or {}
    factor_max_shap: dict[str, dict[str, object]] = {}
    for row in shap_rows:
        feature = str(row.get("feature", "")).strip()
        if not feature:
            continue
        shap_value = float(row.get("shap_value", 0.0) or 0.0)
        restored = restore_legacy_factor_name(feature)
        origins = [str(item).strip() for item in feature_origin_map.get(feature, []) if str(item).strip()]
        if len(origins) == 1:
            restored = origins[0]
        indicators_code = restored
        record = {
            "rdate": rdate,
            "frequency": frequency,
            "feature": restored,
            "indicators_code": indicators_code,
            "shap_value": shap_value,
            "up_or_down": up_or_down,
        }
        if restored not in factor_max_shap or abs(shap_value) > abs(float(factor_max_shap[restored]["shap_value"])):
            factor_max_shap[restored] = record
    return sorted(factor_max_shap.values(), key=lambda item: abs(float(item["shap_value"])), reverse=True)


def _normalize_tree_shap_values(shap_values) -> np.ndarray:
    if isinstance(shap_values, list):
        val_array = shap_values[1] if len(shap_values) > 1 else shap_values[0]
    else:
        val_array = shap_values
    val_array = np.asarray(val_array)
    if val_array.ndim == 3:
        val_array = val_array[0, 1, :]
    elif val_array.ndim == 2:
        val_array = val_array[0]
    return val_array


def compute_shap_records_for_result(daily_df: pd.DataFrame, result: PredictionResult) -> list[dict[str, object]]:
    if result.model is None:
        return []

    import shap

    from .feature_engineering import build_feature_matrix

    daily = daily_df.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    features, origin_map = build_feature_matrix(daily, result.config)
    feature_dates = daily["date"].dt.strftime("%Y-%m-%d")
    matches = np.flatnonzero(feature_dates.eq(result.feature_date).to_numpy())
    if not len(matches):
        raise ValueError(f"feature_date {result.feature_date} not found in daily dataframe")
    idx = int(matches[-1])
    test_x = features.iloc[[idx]].copy()

    explainer = shap.TreeExplainer(result.model)
    shap_values = explainer.shap_values(test_x, check_additivity=False)
    val_array = _normalize_tree_shap_values(shap_values)
    shap_rows = [
        {"feature": feature, "shap_value": float(value)}
        for feature, value in zip(features.columns.tolist(), val_array)
    ]
    return legacy_shap_records(result.rdate, result.frequency, shap_rows, origin_map)
