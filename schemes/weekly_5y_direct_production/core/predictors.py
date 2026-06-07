from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


TARGET_COL = "TB5YWI3C"
REQUIRED_WEEKLY_COLUMNS = ("TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C")
PRODUCTION_PROFILE = "5y_direct_independent_rule_vote_weekly0529"
PRODUCTION_MODEL = "Direct_Production_Final"
TIE_LABEL = -1

RULES: tuple[tuple[dict[str, Any], float], ...] = (
    (
        {
            "name": "five_year_7y_10y_spread_momentum_2w",
            "kind": "spread_change",
            "col_a": "TB7YWI3C",
            "col_b": "TB0YWI3C",
            "lookback": 2,
            "sign": 1.0,
        },
        1.0,
    ),
    (
        {
            "name": "five_year_5y_10y_spread_reversal_4w",
            "kind": "spread_change",
            "col_a": "TB5YWI3C",
            "col_b": "TB0YWI3C",
            "lookback": 4,
            "sign": -1.0,
        },
        1.0,
    ),
    (
        {
            "name": "five_year_1y_momentum_4w",
            "kind": "momentum",
            "source_col": "TB1YWI3C",
            "lookback": 4,
            "sign": 1.0,
        },
        1.0,
    ),
)


@dataclass(frozen=True)
class Weekly5YPredictionResult:
    rdate: str
    frequency: str
    tenor: str
    pred_label: int
    prob_up: float
    week_id: int
    prediction_column: str
    probability_column: str
    source: str
    source_spec: str
    score_spec: str
    predictions: pd.DataFrame


def normalize_weekly_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """把公共周频输入 CSV 读回的 DataFrame 规范为算法可用格式。"""
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    require_columns(df, ["week_id", *REQUIRED_WEEKLY_COLUMNS])
    df = df[["week_id", *REQUIRED_WEEKLY_COLUMNS]].copy()
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    for col in REQUIRED_WEEKLY_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").reset_index(drop=True)


def require_columns(df: pd.DataFrame, cols: list[str] | tuple[str, ...]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"weekly 5Y input is missing required columns: {missing}")


def build_rule_signal(rule: dict[str, Any], weekly: pd.DataFrame) -> pd.DataFrame:
    """按原始 0529 方案的一条固定规则生成方向信号。"""
    name = str(rule["name"])
    kind = str(rule["kind"])
    lookback = int(rule["lookback"])
    sign = float(rule.get("sign", 1.0))
    if lookback <= 0:
        raise ValueError(f"rule lookback must be positive: {name}")

    if kind == "momentum":
        source_col = str(rule["source_col"])
        require_columns(weekly, [source_col])
        raw = pd.to_numeric(weekly[source_col], errors="coerce").ffill().pct_change(lookback, fill_method=None)
    elif kind == "spread_change":
        col_a = str(rule["col_a"])
        col_b = str(rule["col_b"])
        require_columns(weekly, [col_a, col_b])
        spread = pd.to_numeric(weekly[col_a], errors="coerce") - pd.to_numeric(weekly[col_b], errors="coerce")
        raw = spread.diff(lookback)
    else:
        raise ValueError(f"unsupported rule kind: {kind}")

    signal = np.sign(raw.to_numpy(dtype=float) * sign)
    valid = np.isin(signal, [-1.0, 1.0])
    return pd.DataFrame(
        {
            "week_id": weekly.loc[valid, "week_id"].astype(int).to_numpy(),
            f"{name}__prob_up": np.where(signal[valid] > 0, 0.55, 0.45),
            f"{name}__pred_label": signal[valid].astype(int),
        }
    )


def build_rule_vote(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """复现 5Y direct-production 三规则等权投票。"""
    weekly = normalize_weekly_frame(weekly_df)
    merged: pd.DataFrame | None = None
    rule_names: list[str] = []
    weights: list[float] = []

    for rule, weight in RULES:
        rule_names.append(str(rule["name"]))
        weights.append(float(weight))
        signal = build_rule_signal(rule, weekly)
        merged = signal if merged is None else merged.merge(signal, on="week_id", how="inner")

    if merged is None or merged.empty:
        raise RuntimeError("No valid 5Y rule-vote rows generated")

    weight_array = np.asarray(weights, dtype=float)
    pred_matrix = merged[[f"{name}__pred_label" for name in rule_names]].to_numpy(dtype=float)
    prob_matrix = merged[[f"{name}__prob_up" for name in rule_names]].to_numpy(dtype=float)
    vote = (pred_matrix * weight_array).sum(axis=1)
    weight_sum = float(np.abs(weight_array).sum()) or 1.0

    merged["rule_vote"] = vote
    merged["final_pred_label"] = np.where(vote > 0, 1, np.where(vote < 0, -1, TIE_LABEL)).astype(int)
    merged["final_prob_up"] = np.clip((prob_matrix * weight_array).sum(axis=1) / weight_sum, 0.0, 1.0)
    merged["winner_model"] = PRODUCTION_MODEL
    merged["selector_decision"] = "direct_independent_5y_fixed_rule_vote"
    merged["source_spec"] = ";".join(rule_names)
    merged["score_spec"] = ";".join(f"{name}:{weight:.4f}" for name, weight in zip(rule_names, weights))
    merged["selector_profile"] = PRODUCTION_PROFILE
    return merged.sort_values("week_id").reset_index(drop=True)


def predict_w5y(
    weekly_df: pd.DataFrame,
    rdate: str,
    target_week_id: int | None = None,
) -> Weekly5YPredictionResult:
    """基于公共周频输入生成 5Y 单周预测结果。"""
    predictions = build_rule_vote(weekly_df)
    if predictions.empty:
        raise ValueError("W5Y produced no predictions")

    ordered = predictions.sort_values("week_id").copy()
    if target_week_id is not None:
        target_rows = ordered[ordered["week_id"].astype(int).eq(int(target_week_id))]
        if target_rows.empty:
            latest_week_id = int(ordered["week_id"].max())
            raise ValueError(
                f"W5Y target week_id {target_week_id} not found in prediction output; "
                f"latest model output week_id is {latest_week_id}."
            )
        row = target_rows.iloc[-1]
    else:
        row = ordered.iloc[-1]

    return Weekly5YPredictionResult(
        rdate=rdate,
        frequency="W5Y",
        tenor="5Y",
        pred_label=int(row["final_pred_label"]),
        prob_up=float(row["final_prob_up"]) if pd.notna(row["final_prob_up"]) else float("nan"),
        week_id=int(row["week_id"]),
        prediction_column="final_pred_label",
        probability_column="final_prob_up",
        source="5y_direct_production_0529",
        source_spec=str(row["source_spec"]),
        score_spec=str(row["score_spec"]),
        predictions=predictions.reset_index(drop=True),
    )
