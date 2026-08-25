from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backtests.repository import clean_json
from shared.prediction_context import WEEKLY_TARGET_RULE, next_calendar_week_id
from shared.signal_policy import no_signal_as_flat
from shared.tenor_mapping import TENOR_TO_INDICATOR


@dataclass(frozen=True)
class WeeklyBacktestSpec:
    """周频回测行构造的公共业务参数。"""

    benchmark_id: str
    scheme_id: str
    target_tenor: str
    horizon_days: int
    target_column: str
    model_version: str
    predict_start_date: str
    live_target_start_date: str
    target_rule: str = WEEKLY_TARGET_RULE
    precheck_predict_start: bool = False
    precheck_target: bool = False
    no_signal_policy: Literal["skip", "flat"] = "skip"
    no_signal_source_component: str | None = None

    def __post_init__(self) -> None:
        if self.no_signal_policy not in {"skip", "flat"}:
            raise ValueError(f"invalid no_signal_policy: {self.no_signal_policy!r}")
        if self.no_signal_policy == "flat" and not (
            isinstance(self.no_signal_source_component, str)
            and self.no_signal_source_component.strip()
        ):
            raise ValueError("no_signal_source_component is required when no_signal_policy='flat'")


@dataclass(frozen=True)
class WeeklyPredictionPoint:
    """单个 feature_week_id 的算法输出。"""

    source_row: Mapping[str, Any]
    predicted_direction: int | None
    confidence: float | None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WeeklyAverageLabel:
    """周平均 actual 标签，按目标周均值相对特征周均值计算。"""

    feature_week_id: int
    target_week_id: int
    feature_date: str
    target_date: str
    feature_yield: float
    target_yield: float
    future_return: float | None
    label: int | None


def index_weekly_core_output_rows(
    frame: pd.DataFrame,
    *,
    source_component: str,
) -> dict[int, dict[str, Any]]:
    """严格校验周频 core 批量输出，并按 week_id 建立索引。"""
    if frame.empty:
        raise RuntimeError(f"{source_component} core 未产生任何输出，禁止批量补平")
    if "week_id" not in frame.columns:
        raise ValueError(f"{source_component} core 输出缺少 week_id")

    normalized = frame.copy()

    def normalize_week_id(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{source_component} core 输出 week_id 必须为严格整数: {value!r}")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{source_component} core 输出 week_id 必须为严格整数: {value!r}"
            ) from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{source_component} core 输出 week_id 必须为严格整数: {value!r}")
        return int(numeric)

    normalized["week_id"] = normalized["week_id"].map(normalize_week_id)
    by_week: dict[int, dict[str, Any]] = {}
    for _, row in normalized.sort_values("week_id").iterrows():
        by_week[int(row["week_id"])] = row.to_dict()
    if not by_week:
        raise RuntimeError(f"{source_component} core 未产生任何合法输出，禁止批量补平")
    return by_week


def read_weekly_average_label_rows(
    engine: Engine,
    *,
    target_tenor: str,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """读取周平均 label 所需的日频收益率明细。"""
    indicator_code = TENOR_TO_INDICATOR[str(target_tenor)]
    end_filter = "AND rdate <= :end_date" if end_date else ""
    stmt = text(
        f"""
        SELECT rdate, indicators_value
        FROM api_wind_daily
        WHERE indicators_code = :indicator_code
          AND indicators_value IS NOT NULL
          {end_filter}
        ORDER BY rdate
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            stmt,
            {"indicator_code": indicator_code, "end_date": end_date},
        ).mappings().all()
    return [
        {
            "tenor": target_tenor,
            "trade_date": str(row["rdate"]),
            "close_yield": _float_or_none(row["indicators_value"]),
        }
        for row in rows
        if _float_or_none(row["indicators_value"]) is not None
    ]


def build_weekly_average_label_map_from_rows(
    rows: list[dict[str, Any]],
    *,
    calendar: Any,
    target_tenor: str,
    live_target_start_date: str,
) -> dict[int, WeeklyAverageLabel]:
    """按日频收益率均值构造 feature_week_id -> 周平均标签。"""
    grouped: dict[int, dict[str, Any]] = defaultdict(lambda: {"items": [], "last": None})
    max_trade_date: str | None = None
    for raw in rows:
        if str(raw.get("tenor")) != str(target_tenor):
            continue
        trade_date = str(raw["trade_date"])
        week_id = calendar.week_id_for_date(trade_date)
        close_yield = _float_or_none(raw.get("close_yield"))
        if week_id is None or close_yield is None:
            continue
        item = {"trade_date": trade_date, "close_yield": close_yield}
        bucket = grouped[int(week_id)]
        bucket["items"].append(item)
        if bucket["last"] is None or trade_date > bucket["last"]["trade_date"]:
            bucket["last"] = item
        if max_trade_date is None or trade_date > max_trade_date:
            max_trade_date = trade_date

    labels: dict[int, WeeklyAverageLabel] = {}
    for feature_week_id in sorted(grouped):
        try:
            target_week_id = next_calendar_week_id(calendar, int(feature_week_id))
        except ValueError:
            continue
        target_bucket = grouped.get(target_week_id)
        if target_bucket is None:
            continue
        target_date = calendar.week_id_to_last_trading_day(target_week_id)
        if target_date >= live_target_start_date:
            continue
        if max_trade_date is None or max_trade_date < target_date:
            continue
        feature_bucket = grouped[feature_week_id]
        feature_yield = _mean_close_yield(feature_bucket["items"])
        target_yield = _mean_close_yield(target_bucket["items"])
        future_return = _weekly_future_return_from_values(feature_yield, target_yield)
        labels[int(feature_week_id)] = WeeklyAverageLabel(
            feature_week_id=int(feature_week_id),
            target_week_id=int(target_week_id),
            feature_date=str(feature_bucket["last"]["trade_date"]),
            target_date=str(target_date),
            feature_yield=feature_yield,
            target_yield=target_yield,
            future_return=future_return,
            label=_label_from_future_return(future_return) if future_return is not None else None,
        )
    return labels


def build_weekly_backtest_rows(
    weekly_df: pd.DataFrame,
    *,
    calendar: Any,
    spec: WeeklyBacktestSpec,
    artifact_path: Path,
    artifact_source: str,
    normalize_frame: Callable[[pd.DataFrame], pd.DataFrame],
    predict_for_feature: Callable[[pd.DataFrame, int], WeeklyPredictionPoint | None],
    weekly_frame_for_feature: Callable[[int, str], pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """按 feature_week_id 逐周构造 PIT 回测行。

    回测发出日固定为 feature_date；target week 统一来自 DB 日历的下一实际周。
    """
    weekly = normalize_frame(weekly_df)
    if weekly.empty:
        return []

    rows: list[dict[str, Any]] = []
    feature_weeks = [int(value) for value in weekly["week_id"].tolist()]
    weekly_by_id = {int(row["week_id"]): row for _, row in weekly.iterrows()}

    for feature_week_id in feature_weeks:
        if feature_week_id not in weekly_by_id:
            continue
        feature_date = calendar.week_id_to_last_trading_day(feature_week_id)
        predict_date = feature_date
        if spec.precheck_predict_start and predict_date < spec.predict_start_date:
            continue

        target = None
        if spec.precheck_target:
            target = _resolve_target(calendar, weekly_by_id, feature_week_id, spec.live_target_start_date)
            if target is None:
                continue

        history_source = weekly_frame_for_feature(feature_week_id, feature_date) if weekly_frame_for_feature else weekly
        history = normalize_frame(history_source)
        history = history[history["week_id"].le(feature_week_id)].copy()
        history_by_id = {int(row["week_id"]): row for _, row in history.iterrows()}
        feature_row = history_by_id.get(feature_week_id)
        if feature_row is None:
            continue

        point = predict_for_feature(history, feature_week_id)
        if point is None and spec.no_signal_policy == "skip":
            continue

        if target is None:
            target = _resolve_target(calendar, weekly_by_id, feature_week_id, spec.live_target_start_date)
            if target is None:
                continue

        if predict_date < spec.predict_start_date:
            continue

        target_week_id, target_date, target_row = target
        future_return = _weekly_future_return(feature_row, target_row, spec.target_column)
        label = _label_from_future_return(future_return) if future_return is not None else None
        if point is None:
            if label is None:
                continue
            policy_outcome = no_signal_as_flat(str(spec.no_signal_source_component))
            point = WeeklyPredictionPoint(
                source_row={},
                predicted_direction=policy_outcome.predicted_direction,
                confidence=policy_outcome.confidence,
                extra=policy_outcome.extra,
            )
        source_row = clean_json({**feature_row.to_dict(), **dict(point.source_row)})
        extra = {
            "frequency": "weekly",
            "model_version": spec.model_version,
            "target_rule": spec.target_rule,
            "feature_week_id": feature_week_id,
            "target_week_id": target_week_id,
            "feature_date": feature_date,
            "target_date": target_date,
            "future_return": future_return,
            "input_artifact_path": str(artifact_path),
            "input_artifact_source": artifact_source,
        }
        extra.update(dict(point.extra))

        rows.append(
            {
                "benchmark_id": spec.benchmark_id,
                "scheme_id": spec.scheme_id,
                "target_tenor": spec.target_tenor,
                "horizon": spec.horizon_days,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": target_date,
                "label": label,
                "predicted_direction": point.predicted_direction,
                "model_pred": point.predicted_direction,
                "confidence": point.confidence,
                "source_row": source_row,
                "extra": extra,
            }
        )

    return rows


def policy_generated_flat_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总平台无信号政策生成的周频平记录。"""
    policy_rows = [
        row
        for row in rows
        if (row.get("extra") or {}).get("signal_policy_applied") is True
    ]
    feature_keys = sorted(
        {
            feature_week_id
            for row in policy_rows
            if (feature_week_id := _int_or_none((row.get("extra") or {}).get("feature_week_id")))
            is not None
        }
    )
    return {
        "policy_generated_flat_count": len(policy_rows),
        "policy_generated_flat_feature_keys": feature_keys,
    }


def compact_weekly_benchmark_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 CompareGate 使用的严格周频 benchmark 行。"""
    compact: list[dict[str, Any]] = []
    for row in rows:
        if (row.get("extra") or {}).get("signal_policy_applied") is True:
            continue
        direction = _int_or_none(row.get("predicted_direction"))
        label = _int_or_none(row.get("label"))
        extra = row.get("extra") or {}
        compact.append(
            {
                "feature_date": str(row["feature_date"]),
                "target_date": str(row["target_date"]),
                "target_tenor": str(row["target_tenor"]),
                "horizon": _int_or_none(row.get("horizon")),
                "direction": direction,
                "confidence": _float_or_none(row.get("confidence")),
                "label": label,
                "is_correct": direction == label if direction is not None and label is not None else None,
                "feature_week_id": _int_or_none(extra.get("feature_week_id")),
                "target_week_id": _int_or_none(extra.get("target_week_id")),
            }
        )
        compact[-1].update(_compact_extra_fields(extra, compact[-1]))
    return compact


def _compact_extra_fields(extra: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    """保留 CompareGate 需要的 extra 审计字段，避免把大路径/收益率诊断写入 benchmark CSV。"""
    skipped = {
        "input_artifact_path",
        "input_artifact_source",
        "feature_yield",
        "target_yield",
        "future_return",
    }
    out: dict[str, Any] = {}
    for key in sorted(extra):
        if key in skipped or key in existing:
            continue
        value = extra.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def _resolve_target(
    calendar: Any,
    weekly_by_id: dict[int, pd.Series],
    feature_week_id: int,
    live_target_start_date: str,
) -> tuple[int, str, pd.Series] | None:
    try:
        target_week_id = next_calendar_week_id(calendar, feature_week_id)
    except ValueError:
        return None
    target_row = weekly_by_id.get(target_week_id)
    if target_row is None:
        return None
    target_date = calendar.week_id_to_last_trading_day(target_week_id)
    if target_date >= live_target_start_date:
        return None
    return target_week_id, target_date, target_row


def _weekly_future_return(feature_row: pd.Series, target_row: pd.Series, target_column: str) -> float | None:
    current_close = _float_or_none(feature_row.get(target_column))
    target_close = _float_or_none(target_row.get(target_column))
    return _weekly_future_return_from_values(current_close, target_close)


def _weekly_future_return_from_values(current_close: float | None, target_close: float | None) -> float | None:
    if current_close is None or target_close is None or current_close == 0:
        return None
    return (target_close - current_close) / current_close


def _mean_close_yield(items: list[dict[str, Any]]) -> float:
    return sum(float(item["close_yield"]) for item in items) / len(items)


def _label_from_future_return(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result
