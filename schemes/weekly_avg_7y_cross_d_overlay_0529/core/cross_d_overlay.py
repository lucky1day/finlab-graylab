from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


TARGET_COL = "TB7YWI3C"
REQUIRED_COLUMNS = ("week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C")

CROSS_D_CONFIG: dict[str, Any] = {
    "source": "5Y_D",
    "allowed_direction": "down",
    "main_low_conf": 0.05,
    "aux_min_conf": 0.0,
    "rate_threshold": 2.0,
    "require_aux_overlay": False,
}


@dataclass(frozen=True)
class RuleSpec:
    """0529 规则定义。"""

    name: str
    kind: str
    lookback: int
    sign: float = 1.0
    source_col: str = ""
    col_a: str = ""
    col_b: str = ""
    weight: float = 1.0


SEVEN_Y_BASE_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="seven_year_1y_3y_spread_reversal_3w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=3,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_1y_5y_spread_momentum_3w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=3,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_1y_5y_spread_momentum_6w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=6,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_5y_7y_spread_momentum_1w",
        kind="spread_change",
        col_a="TB5YWI3C",
        col_b="TB7YWI3C",
        lookback=1,
        sign=1.0,
    ),
)

SEVEN_Y_REBOUND_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="seven_year_overlay_3y_momentum_1w",
        kind="momentum",
        source_col="TB3YWI3C",
        lookback=1,
        sign=1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_3y_ratio_reversal_8w",
        kind="ratio_momentum",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=8,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_3y_spread_reversal_8w",
        kind="spread_change",
        col_a="TB1YWI3C",
        col_b="TB3YWI3C",
        lookback=8,
        sign=-1.0,
    ),
    RuleSpec(
        name="seven_year_overlay_1y_5y_ratio_momentum_1w",
        kind="ratio_momentum",
        col_a="TB1YWI3C",
        col_b="TB5YWI3C",
        lookback=1,
        sign=1.0,
    ),
)

FIVE_Y_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name="five_year_7y_10y_spread_momentum_2w",
        kind="spread_change",
        col_a="TB7YWI3C",
        col_b="TB0YWI3C",
        lookback=2,
        sign=1.0,
    ),
    RuleSpec(
        name="five_year_5y_10y_spread_reversal_4w",
        kind="spread_change",
        col_a="TB5YWI3C",
        col_b="TB0YWI3C",
        lookback=4,
        sign=-1.0,
    ),
    RuleSpec(
        name="five_year_1y_momentum_4w",
        kind="momentum",
        source_col="TB1YWI3C",
        lookback=4,
        sign=1.0,
    ),
)


def build_cross_d_overlay(weekly: pd.DataFrame) -> pd.DataFrame:
    """构建 7Y Cross-D 最终预测序列。

    Args:
        weekly: 周频宽表，必须包含 week_id 与 REQUIRED_COLUMNS 中的收益率列。

    Returns:
        DataFrame，按 week_id 升序，含 7Y 主模型、5Y 辅助信号与最终 Cross-D 输出。
    """
    normalized = _normalize_weekly(weekly)
    seven_y = build_7y_main(normalized)
    five_y = build_5y_aux(normalized)
    return apply_cross_d(seven_y, five_y, normalized)


def build_7y_main(weekly: pd.DataFrame) -> pd.DataFrame:
    """7Y 主规则投票 + 低利率反弹叠加。"""
    out = fixed_vote(weekly, SEVEN_Y_BASE_RULES, TARGET_COL, "seven_y_base", tie_label=-1)
    for rule in SEVEN_Y_REBOUND_RULES:
        add_rule_signal(out, weekly, rule)

    overlay_names = [rule.name for rule in SEVEN_Y_REBOUND_RULES]
    overlay_cols = [f"{name}__signal" for name in overlay_names]
    overlay_valid = out[overlay_cols].isin([-1.0, 1.0]).all(axis=1).to_numpy()
    overlay_matrix = out[overlay_cols].to_numpy(dtype=float)
    agree_count = np.where(overlay_valid, np.sum(overlay_matrix == 1.0, axis=1), np.nan)
    overlay_vote = np.where(overlay_valid, np.nansum(overlay_matrix, axis=1), np.nan)
    regime_valid = pd.to_numeric(weekly[TARGET_COL], errors="coerce").le(1.85).to_numpy()

    base_pred = out["seven_y_base_pred_label"].to_numpy(dtype=float).copy()
    base_prob = out["seven_y_base_prob_up"].to_numpy(dtype=float).copy()
    base_vote = out["seven_y_base_vote"].to_numpy(dtype=float)
    base_valid = np.isin(base_pred, [-1.0, 1.0])

    label_overlay_candidate = (
        base_valid
        & overlay_valid
        & (base_pred == -1.0)
        & (base_vote <= -2.0)
        & (agree_count >= 3)
    )
    label_overlay_applied = label_overlay_candidate & regime_valid

    pred = base_pred.copy()
    prob = base_prob.copy()
    pred[label_overlay_applied] = 1.0
    adjusted_prob = np.clip(0.62 + 0.03 * np.maximum(agree_count - 3, 0), 0.0, 1.0)
    prob[label_overlay_applied] = np.maximum(prob[label_overlay_applied], adjusted_prob[label_overlay_applied])

    out["actual_label"] = out["seven_y_base_actual_label"]
    out["future_return"] = out["seven_y_base_future_return"]
    out["label_overlay_vote"] = overlay_vote
    out["label_overlay_agree_count"] = agree_count
    out["label_overlay_regime_valid"] = regime_valid
    out["label_overlay_candidate"] = label_overlay_candidate
    out["label_overlay_applied"] = label_overlay_applied
    out["main_pred_label"] = pred
    out["main_prob_up"] = prob
    return out[out["main_pred_label"].isin([-1.0, 1.0])].copy()


def build_5y_aux(weekly: pd.DataFrame) -> pd.DataFrame:
    """构建 5Y D 辅助信号。"""
    out = fixed_vote(weekly, FIVE_Y_RULES, "TB5YWI3C", "five_y", tie_label=-1)
    out = out[out["five_y_pred_label"].isin([-1.0, 1.0])].copy()
    out["d5_d_pred_label"] = out["five_y_pred_label"].astype(int)
    out["d5_d_prob_up"] = out["five_y_prob_up"].astype(float)
    out["d5_conf"] = (out["d5_d_prob_up"] - 0.5).abs()
    out["d5_d_model2_overlay"] = False
    out["d5_d_signal_source"] = "fixed_5y_vote"
    return out[
        [
            "week_id",
            "d5_d_pred_label",
            "d5_d_prob_up",
            "d5_conf",
            "d5_d_model2_overlay",
            "d5_d_signal_source",
        ]
    ].copy()


def apply_cross_d(seven_y: pd.DataFrame, five_y: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """将 5Y 下行确认窄口径覆盖到 7Y 主信号。"""
    rates = weekly[list(REQUIRED_COLUMNS)].copy()
    out = seven_y.merge(five_y, on="week_id", how="inner")
    out = out.drop(columns=[col for col in REQUIRED_COLUMNS if col in out.columns and col != "week_id"])
    out = out.merge(rates, on="week_id", how="left")

    aux_pred = out["d5_d_pred_label"].astype(int)
    aux_prob = out["d5_d_prob_up"].astype(float)
    aux_conf = out["d5_conf"].astype(float)
    main_pred = out["main_pred_label"].astype(int)
    main_conf = (out["main_prob_up"].astype(float) - 0.5).abs()

    overlay = (
        aux_pred.ne(main_pred)
        & (main_conf <= float(CROSS_D_CONFIG["main_low_conf"]))
        & (aux_conf >= float(CROSS_D_CONFIG["aux_min_conf"]))
        & aux_pred.eq(-1)
        & (out["TB7YWI3C"].astype(float) <= float(CROSS_D_CONFIG["rate_threshold"]))
    )

    out["cross_d_overlay"] = overlay.astype(bool)
    out["cross_d_pred_label"] = np.where(overlay, aux_pred, main_pred).astype(int)
    out["cross_d_prob_up"] = np.where(overlay, aux_prob, out["main_prob_up"]).astype(float)
    out["cross_d_signal_source"] = np.where(overlay, "5y_d_down_overlay", "7y_rule_vote_main")

    valid_actual = out["actual_label"].notna()
    out["cross_d_is_correct"] = (
        valid_actual & out["cross_d_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    )
    out["main_is_correct"] = (
        valid_actual & out["main_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    )
    return out.sort_values("week_id").reset_index(drop=True)


def fixed_vote(
    weekly: pd.DataFrame,
    rules: tuple[RuleSpec, ...],
    target_col: str,
    prefix: str,
    tie_label: int = -1,
) -> pd.DataFrame:
    """固定规则加权投票。"""
    _require_columns(weekly, ["week_id", target_col])
    out = weekly[["week_id", target_col]].copy()
    future_return, actual_label = make_labels(weekly, target_col)
    out[f"{prefix}_future_return"] = future_return
    out[f"{prefix}_actual_label"] = actual_label

    for rule in rules:
        add_rule_signal(out, weekly, rule)

    signal_cols = [f"{rule.name}__signal" for rule in rules]
    valid = out[signal_cols].isin([-1.0, 1.0]).all(axis=1)
    weights = np.asarray([rule.weight for rule in rules], dtype=float)
    matrix = out[signal_cols].to_numpy(dtype=float)
    vote = matrix @ weights
    pred = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label)).astype(float)
    pred[~valid.to_numpy()] = np.nan

    prob_matrix = np.where(matrix > 0, 0.55, 0.45)
    prob_up = (prob_matrix @ weights) / max(float(np.abs(weights).sum()), 1.0)
    prob_up[~valid.to_numpy()] = np.nan

    out[f"{prefix}_vote"] = vote
    out[f"{prefix}_pred_label"] = pred
    out[f"{prefix}_prob_up"] = prob_up
    return out


def make_labels(df: pd.DataFrame, target_col: str) -> tuple[pd.Series, pd.Series]:
    """计算下一周收益率变化方向，仅供回测评估使用。"""
    close = pd.to_numeric(df[target_col], errors="coerce")
    future_return = close.shift(-1).div(close).sub(1.0)
    label = pd.Series(
        np.select([future_return > 0, future_return < 0], [1, -1], default=0).astype(float),
        index=df.index,
    )
    label[future_return.isna()] = np.nan
    return future_return, label


def add_rule_signal(out: pd.DataFrame, weekly: pd.DataFrame, rule: RuleSpec) -> None:
    """向输出表追加单条规则信号。"""
    raw = rule_raw(rule, weekly)
    signed_raw = raw * rule.sign
    out[f"{rule.name}__raw"] = raw
    out[f"{rule.name}__signed_raw"] = signed_raw
    out[f"{rule.name}__signal"] = np.sign(signed_raw.to_numpy(dtype=float))


def rule_raw(rule: RuleSpec, weekly: pd.DataFrame) -> pd.Series:
    """计算规则原始动量或利差变化。"""
    if rule.kind == "momentum":
        _require_columns(weekly, [rule.source_col])
        return pd.to_numeric(weekly[rule.source_col], errors="coerce").pct_change(
            rule.lookback,
            fill_method=None,
        )
    if rule.kind == "spread_change":
        _require_columns(weekly, [rule.col_a, rule.col_b])
        spread = pd.to_numeric(weekly[rule.col_a], errors="coerce") - pd.to_numeric(
            weekly[rule.col_b],
            errors="coerce",
        )
        return spread.diff(rule.lookback)
    if rule.kind == "ratio_momentum":
        _require_columns(weekly, [rule.col_a, rule.col_b])
        ratio = pd.to_numeric(weekly[rule.col_a], errors="coerce") / pd.to_numeric(
            weekly[rule.col_b],
            errors="coerce",
        )
        return ratio.pct_change(rule.lookback, fill_method=None)
    raise ValueError(f"不支持的规则类型: {rule.kind}")


def _normalize_weekly(weekly: pd.DataFrame) -> pd.DataFrame:
    df = weekly.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    _require_columns(df, list(REQUIRED_COLUMNS))
    df = df[list(REQUIRED_COLUMNS)].copy()
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    for col in REQUIRED_COLUMNS:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").drop_duplicates("week_id", keep="last").reset_index(drop=True)


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {missing}")
