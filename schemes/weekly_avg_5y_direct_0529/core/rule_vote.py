from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ---- 锁定规则配置（0529 原版，不做改动） ----
# 三个独立因子投票，每个权重 1.0，平局预测下行 (-1)

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


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {missing}")


def build_rule_signal(df: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    """计算单条规则的方向信号。

    Args:
        df: 含 week_id 和所需收益率列的 DataFrame，按 week_id 升序。
        rule: 规则定义，包含 name/kind/lookback/sign 及对应数据列。

    Returns:
        DataFrame，列 [week_id, {name}__prob_up, {name}__pred_label]，
        仅包含有效信号行（pred_label ∈ {-1, 1}）。
    """
    name = str(rule["name"])
    kind = str(rule["kind"])
    lookback = int(rule["lookback"])
    sign = float(rule.get("sign", 1.0))
    if lookback <= 0:
        raise ValueError(f"规则 lookback 必须 >0: {name}")

    _require_columns(df, ["week_id"])

    if kind == "momentum":
        source_col = str(rule["source_col"])
        _require_columns(df, [source_col])
        raw = pd.to_numeric(df[source_col], errors="coerce").ffill().pct_change(lookback, fill_method=None)
    elif kind == "spread_change":
        col_a = str(rule["col_a"])
        col_b = str(rule["col_b"])
        _require_columns(df, [col_a, col_b])
        spread = pd.to_numeric(df[col_a], errors="coerce") - pd.to_numeric(df[col_b], errors="coerce")
        raw = spread.diff(lookback)
    else:
        raise ValueError(f"不支持的规则类型: {kind}")

    signal = np.sign(raw.to_numpy(dtype=float) * sign)
    valid_mask = np.isin(signal, [-1.0, 1.0])
    return pd.DataFrame(
        {
            "week_id": df.loc[valid_mask, "week_id"].astype(int).to_numpy(),
            f"{name}__prob_up": np.where(signal[valid_mask] > 0, 0.55, 0.45),
            f"{name}__pred_label": signal[valid_mask].astype(int),
        }
    )


def build_rule_vote(
    df: pd.DataFrame,
    rules: tuple[tuple[dict[str, Any], float], ...] | None = None,
    tie_label: int = -1,
) -> pd.DataFrame:
    """多规则加权投票，输出最终预测方向与置信度。

    Args:
        df: 周频宽表，必须含 week_id + 所有规则引用的收益率列。
        rules: 规则列表；默认使用模块级 RULES。
        tie_label: 票数和为 0 时的预测方向（默认 -1=预测下行）。

    Returns:
        DataFrame，包含每个规则的信号列 + 汇总列（rule_vote,
        final_pred_label, final_prob_up, winner_model, source_spec, score_spec）。
        仅包含所有规则均产出有效信号的 week_id（inner join）。
    """
    if rules is None:
        rules = RULES

    merged: pd.DataFrame | None = None
    rule_names: list[str] = []
    weights: list[float] = []

    for rule_def, weight in rules:
        name = str(rule_def["name"])
        rule_names.append(name)
        weights.append(float(weight))
        signal_df = build_rule_signal(df, rule_def)
        merged = signal_df if merged is None else merged.merge(signal_df, on="week_id", how="inner")

    if merged is None or merged.empty:
        raise RuntimeError("没有产生任何有效规则投票行")

    weight_array = np.asarray(weights, dtype=float)
    pred_cols = [f"{name}__pred_label" for name in rule_names]
    prob_cols = [f"{name}__prob_up" for name in rule_names]
    pred_matrix = merged[pred_cols].to_numpy(dtype=float)
    prob_matrix = merged[prob_cols].to_numpy(dtype=float)

    vote = pred_matrix @ weight_array
    weight_sum = float(np.abs(weight_array).sum()) or 1.0

    merged["rule_vote"] = vote
    merged["final_pred_label"] = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label)).astype(int)
    merged["final_prob_up"] = np.clip((prob_matrix @ weight_array) / weight_sum, 0.0, 1.0)
    merged["winner_model"] = "Direct_Production_Final"
    merged["source_spec"] = ";".join(rule_names)
    merged["score_spec"] = ";".join(f"{name}:{weight:.4f}" for name, weight in zip(rule_names, weights))
    return merged
