from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from shared.weekly_calendar import week_id_to_friday, week_id_to_monday


TARGET_COL = "TB7YWI3C"
REQUIRED_WEEKLY_COLUMNS = ("TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C")
SOURCE_NAME = "7y_cross_d_overlay_0529"
PRODUCTION_PROFILE = "7y_cross_d_overlay_weekly0529"
TARGET_RULE = "next_week_last_trading_day_vs_current_week_last_trading_day"
TIE_LABEL = -1


SEVEN_Y_CROSS_CONFIG: dict[str, Any] = {
    "source": "5Y_D",
    "allowed_direction": "down",
    "main_low_conf": 0.05,
    "aux_min_conf": 0.0,
    "rate_threshold": 2.0,
    "require_aux_overlay": False,
}


@dataclass(frozen=True)
class RuleSpec:
    name: str
    kind: str
    lookback: int
    sign: float = 1.0
    source_col: str = ""
    col_a: str = ""
    col_b: str = ""
    weight: float = 1.0


@dataclass(frozen=True)
class Weekly7YPredictionResult:
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

FIVE_Y_AUX_RULES: tuple[RuleSpec, ...] = (
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


def normalize_weekly_frame(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """把公共周频输入 CSV 读回的 DataFrame 规范为 7Y 算法输入。"""
    df = weekly_df.copy()
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    require_columns(df, ["week_id", *REQUIRED_WEEKLY_COLUMNS])
    keep_columns = ["week_id", *REQUIRED_WEEKLY_COLUMNS]
    df = df[keep_columns].copy()
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    for col in REQUIRED_WEEKLY_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("week_id").reset_index(drop=True)
    df["date"] = df["week_id"].map(week_id_to_monday)
    df["week_date"] = df["week_id"].map(week_id_to_friday)
    return df


def require_columns(df: pd.DataFrame, cols: list[str] | tuple[str, ...]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"weekly 7Y input is missing required columns: {missing}")


def make_labels(df: pd.DataFrame, target_col: str) -> tuple[pd.Series, pd.Series]:
    close = pd.to_numeric(df[target_col], errors="coerce")
    future_return = close.shift(-1).div(close).sub(1.0)
    label = pd.Series(
        np.select([future_return > 0, future_return < 0], [1, -1], default=0).astype(float),
        index=df.index,
    )
    label[future_return.isna()] = np.nan
    return future_return, label


def direction_text(label: float | int) -> str:
    if pd.isna(label):
        return ""
    value = int(label)
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def rule_raw(rule: RuleSpec, weekly: pd.DataFrame) -> pd.Series:
    if rule.kind == "momentum":
        return pd.to_numeric(weekly[rule.source_col], errors="coerce").pct_change(rule.lookback, fill_method=None)
    if rule.kind == "spread_change":
        spread = pd.to_numeric(weekly[rule.col_a], errors="coerce") - pd.to_numeric(weekly[rule.col_b], errors="coerce")
        return spread.diff(rule.lookback)
    if rule.kind == "ratio_momentum":
        ratio = pd.to_numeric(weekly[rule.col_a], errors="coerce") / pd.to_numeric(weekly[rule.col_b], errors="coerce")
        return ratio.pct_change(rule.lookback, fill_method=None)
    raise ValueError(f"unsupported rule kind: {rule.kind}")


def add_rule_signal(out: pd.DataFrame, weekly: pd.DataFrame, rule: RuleSpec) -> None:
    raw = rule_raw(rule, weekly)
    signed_raw = raw * rule.sign
    out[f"{rule.name}__raw"] = raw
    out[f"{rule.name}__signed_raw"] = signed_raw
    out[f"{rule.name}__signal"] = np.sign(signed_raw.to_numpy(dtype=float))


def fixed_vote(
    weekly: pd.DataFrame,
    rules: tuple[RuleSpec, ...],
    target_col: str,
    prefix: str,
    tie_label: int = TIE_LABEL,
) -> pd.DataFrame:
    out = weekly[["week_id", "date", "week_date", target_col]].copy()
    future_return, actual_label = make_labels(weekly, target_col)
    out[f"{prefix}_future_return"] = future_return
    out[f"{prefix}_actual_label"] = actual_label

    for rule in rules:
        add_rule_signal(out, weekly, rule)

    signal_cols = [f"{rule.name}__signal" for rule in rules]
    valid = out[signal_cols].isin([-1.0, 1.0]).all(axis=1)
    weights = np.asarray([rule.weight for rule in rules], dtype=float)
    matrix = out[signal_cols].to_numpy(dtype=float)
    valid_mask = valid.to_numpy()
    vote = np.full(len(out), np.nan, dtype=float)
    prob_up = np.full(len(out), np.nan, dtype=float)
    if valid_mask.any():
        valid_matrix = matrix[valid_mask]
        vote[valid_mask] = (valid_matrix * weights).sum(axis=1)
        prob_matrix = np.where(valid_matrix > 0, 0.55, 0.45)
        prob_up[valid_mask] = (prob_matrix * weights).sum(axis=1) / max(float(np.abs(weights).sum()), 1.0)

    pred = np.where(vote > 0, 1, np.where(vote < 0, -1, tie_label)).astype(float)
    pred[~valid_mask] = np.nan

    out[f"{prefix}_vote"] = vote
    out[f"{prefix}_pred_label"] = pred
    out[f"{prefix}_prob_up"] = prob_up
    return out


def build_7y_main(weekly: pd.DataFrame) -> pd.DataFrame:
    """复现原始 7Y fixed rule vote + low-rate rebound overlay。"""
    out = fixed_vote(weekly, SEVEN_Y_BASE_RULES, TARGET_COL, "seven_y_base", tie_label=TIE_LABEL)
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
    out["main_direction"] = pd.Series(pred).map(direction_text).to_numpy()
    out["actual_direction"] = out["actual_label"].map(direction_text)
    return out[out["main_pred_label"].isin([-1.0, 1.0])].copy()


def build_5y_aux(weekly: pd.DataFrame) -> pd.DataFrame:
    """复现原始 cross-D 里使用的固定 5Y 辅助投票。"""
    out = fixed_vote(weekly, FIVE_Y_AUX_RULES, "TB5YWI3C", "five_y", tie_label=TIE_LABEL)
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
    """把 5Y down confirmation overlay 应用到 7Y 主信号。"""
    rates = weekly[["week_id", *REQUIRED_WEEKLY_COLUMNS]].copy()
    out = seven_y.merge(five_y, on="week_id", how="inner")
    out = out.drop(columns=[col for col in REQUIRED_WEEKLY_COLUMNS if col in out.columns])
    out = out.merge(rates, on="week_id", how="left")

    aux_pred = out["d5_d_pred_label"].astype(int)
    aux_prob = out["d5_d_prob_up"].astype(float)
    aux_conf = out["d5_conf"].astype(float)
    main_pred = out["main_pred_label"].astype(int)
    main_conf = (out["main_prob_up"].astype(float) - 0.5).abs()

    overlay = (
        aux_pred.ne(main_pred)
        & (main_conf <= float(SEVEN_Y_CROSS_CONFIG["main_low_conf"]))
        & (aux_conf >= float(SEVEN_Y_CROSS_CONFIG["aux_min_conf"]))
        & aux_pred.eq(-1)
        & (out["TB7YWI3C"].astype(float) <= float(SEVEN_Y_CROSS_CONFIG["rate_threshold"]))
    )

    out["cross_d_overlay"] = overlay.astype(bool)
    out["cross_d_pred_label"] = np.where(overlay, aux_pred, main_pred).astype(int)
    out["cross_d_prob_up"] = np.where(overlay, aux_prob, out["main_prob_up"]).astype(float)
    out["cross_d_signal_source"] = np.where(overlay, "5y_d_down_overlay", "7y_rule_vote_main")
    valid_actual = out["actual_label"].notna()
    out["cross_d_is_correct"] = valid_actual & out["cross_d_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    out["main_is_correct"] = valid_actual & out["main_pred_label"].astype(int).eq(out["actual_label"].fillna(0).astype(int))
    out["month_date"] = pd.to_datetime(out["week_date"])
    out["month"] = out["month_date"].dt.to_period("M").astype(str)
    out["source_spec"] = ";".join(rule.name for rule in (*SEVEN_Y_BASE_RULES, *SEVEN_Y_REBOUND_RULES, *FIVE_Y_AUX_RULES))
    out["score_spec"] = (
        "7y_base_rules=4;7y_rebound_rules=4;5y_aux_rules=3;"
        f"cross_d_config={SEVEN_Y_CROSS_CONFIG}"
    )
    out["selector_profile"] = PRODUCTION_PROFILE
    return out.sort_values("month_date").reset_index(drop=True)


def build_cross_d_predictions(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """生成 weekly_7y_cross_d_overlay 全量周频预测 DataFrame。"""
    weekly = normalize_weekly_frame(weekly_df)
    seven_y_main = build_7y_main(weekly)
    five_y_aux = build_5y_aux(weekly)
    return apply_cross_d(seven_y_main, five_y_aux, weekly)


def predict_w7y(
    weekly_df: pd.DataFrame,
    rdate: str,
    target_week_id: int | None = None,
) -> Weekly7YPredictionResult:
    """基于公共周频输入生成 7Y 单周 cross-D overlay 预测结果。"""
    predictions = build_cross_d_predictions(weekly_df)
    if predictions.empty:
        raise ValueError("W7Y produced no predictions")

    ordered = predictions.sort_values("week_id").copy()
    if target_week_id is not None:
        target_rows = ordered[ordered["week_id"].astype(int).eq(int(target_week_id))]
        if target_rows.empty:
            latest_week_id = int(ordered["week_id"].max())
            raise ValueError(
                f"W7Y target week_id {target_week_id} not found in prediction output; "
                f"latest model output week_id is {latest_week_id}."
            )
        row = target_rows.iloc[-1]
    else:
        row = ordered.iloc[-1]

    return Weekly7YPredictionResult(
        rdate=rdate,
        frequency="W7Y",
        tenor="7Y",
        pred_label=int(row["cross_d_pred_label"]),
        prob_up=float(row["cross_d_prob_up"]) if pd.notna(row["cross_d_prob_up"]) else float("nan"),
        week_id=int(row["week_id"]),
        prediction_column="cross_d_pred_label",
        probability_column="cross_d_prob_up",
        source=SOURCE_NAME,
        source_spec=str(row["source_spec"]),
        score_spec=str(row["score_spec"]),
        predictions=predictions.reset_index(drop=True),
    )
