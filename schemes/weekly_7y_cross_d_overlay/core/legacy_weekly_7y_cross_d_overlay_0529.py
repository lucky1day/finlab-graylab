#!/usr/bin/env python3
"""
7Y fully standalone production-style Cross-D overlay script, 0529 version.

Run directly:
    python production_10y_d_overlay_0529/weekly_7y_cross_d_overlay_0529.py

This file is intentionally self-contained. It does not import project model
modules and does not read exploration outputs. The only input is weekly data.

Production logic:
- Main 7Y signal: fixed 7Y rule vote plus the fixed low-rate 7Y rebound
  overlay.
- Auxiliary 5Y signal: fixed 5Y production vote. In the 0529 Dev/OOS search,
  selected 5Y_D had no Model2 overlay, so it is equivalent to this fixed 5Y
  main vote.
- Cross overlay: allow only a narrow 5Y-confirmed down overlay onto 7Y.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import PerformanceWarning


warnings.filterwarnings("ignore", category=PerformanceWarning)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

def resolve_input_path() -> Path:
    candidates = (
        PROJECT_ROOT / "weekly_output0529.csv",
        SCRIPT_DIR / "weekly_output0529.csv",
        SCRIPT_DIR / "weekly_output0521.csv",
    )
    for path in candidates:
        if path.exists():
            return path
    tried = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"weekly data file not found; tried: {tried}")


INPUT_PATH = resolve_input_path()
OUTPUT_DIR = SCRIPT_DIR / "outputs"

START_DATE = pd.Timestamp("2025-07-01")
END_DATE = pd.Timestamp("2026-05-31")

# Match the 10Y production script: week_id 202618 has week_date 2026-05-01,
# so it belongs to the 2026-05 reporting month.
MONTH_SOURCE = "week_date"

TARGET_COL = "TB7YWI3C"

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


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def safe_rate(num: int, den: int) -> float:
    return num / den if den else np.nan


def read_weekly() -> pd.DataFrame:
    df = pd.read_csv(INPUT_PATH, encoding="utf-8-sig")
    df.columns = [col.strip().lstrip("\ufeff") for col in df.columns]
    if "week_id" not in df.columns:
        raise ValueError(f"weekly data must contain week_id: {INPUT_PATH}")
    required = {"TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"missing weekly columns in {INPUT_PATH}: {missing}")
    df = df.sort_values("week_id").reset_index(drop=True)
    for col in df.columns:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["week_id"] = df["week_id"].astype(int)
    df["date"] = df["week_id"].apply(week_id_to_monday)
    df["week_date"] = df["week_id"].apply(week_id_to_friday)
    return df


def week_id_to_monday(week_id: int | float) -> pd.Timestamp:
    week_text = str(int(week_id))
    year = int(week_text[:4])
    week = int(week_text[4:])
    try:
        return pd.Timestamp.fromisocalendar(year, week, 1)
    except ValueError:
        return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=(week - 1) * 7)


def week_id_to_friday(week_id: int | float) -> pd.Timestamp:
    week_text = str(int(week_id))
    year = int(week_text[:4])
    week = int(week_text[4:])
    try:
        return pd.Timestamp.fromisocalendar(year, week, 5)
    except ValueError:
        return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=(week - 1) * 7 + 4)


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
        return pd.to_numeric(weekly[rule.source_col], errors="coerce").pct_change(
            rule.lookback,
            fill_method=None,
        )
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
    tie_label: int = -1,
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
    out["main_direction"] = pd.Series(pred).map(direction_text).to_numpy()
    out["actual_direction"] = out["actual_label"].map(direction_text)
    return out[out["main_pred_label"].isin([-1.0, 1.0])].copy()


def build_5y_aux(weekly: pd.DataFrame) -> pd.DataFrame:
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
    rates = weekly[["week_id", "TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]].copy()
    out = seven_y.merge(five_y, on="week_id", how="inner")
    out = out.drop(
        columns=[col for col in ["TB1YWI3C", "TB3YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"] if col in out.columns]
    )
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
    out["month_date"] = pd.to_datetime(out[MONTH_SOURCE])
    out["month"] = out["month_date"].dt.to_period("M").astype(str)
    return out.sort_values("month_date").reset_index(drop=True)


def metrics(df: pd.DataFrame, pred_col: str, overlay_col: str | None = None) -> dict[str, Any]:
    pred = df[pred_col].astype(int)
    actual = df["actual_label"].astype(int)
    correct = pred.eq(actual)
    pred_up = int(pred.eq(1).sum())
    pred_down = int(pred.eq(-1).sum())
    true_up = int(actual.eq(1).sum())
    true_down = int(actual.eq(-1).sum())
    true_flat = int(actual.eq(0).sum())
    up_correct = int((pred.eq(1) & actual.eq(1)).sum())
    down_correct = int((pred.eq(-1) & actual.eq(-1)).sum())
    if overlay_col is None:
        overlay_total = 0
        overlay_correct = 0
    else:
        overlay = df[overlay_col].astype(bool)
        overlay_total = int(overlay.sum())
        overlay_correct = int((overlay & correct).sum())
    return {
        "total": int(len(df)),
        "correct": int(correct.sum()),
        "direction_accuracy": float(correct.mean()) if len(df) else np.nan,
        "true_up": true_up,
        "true_down": true_down,
        "true_flat": true_flat,
        "pred_up": pred_up,
        "pred_down": pred_down,
        "up_precision": safe_rate(up_correct, pred_up),
        "down_precision": safe_rate(down_correct, pred_down),
        "up_recall": safe_rate(up_correct, true_up),
        "down_recall": safe_rate(down_correct, true_down),
        "overlay_total": overlay_total,
        "overlay_correct": overlay_correct,
        "overlay_accuracy": safe_rate(overlay_correct, overlay_total),
    }


def monthly_metrics(df: pd.DataFrame, pred_col: str, overlay_col: str | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for month, group in df.groupby("month", sort=True):
        row = {"month": month}
        row.update(metrics(group, pred_col, overlay_col))
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(monthly: pd.DataFrame, overall: dict[str, Any], baseline: dict[str, Any]) -> Path:
    report_path = OUTPUT_DIR / "7Y_CROSS_D_OVERLAY_0529_MONTHLY_2025_07_2026_05.md"
    lines = [
        "# 7Y Cross-D Overlay 0529 Fully Standalone Production Result",
        "",
        "## Fixed Protocol",
        "",
        f"- Input: `{INPUT_PATH}`",
        f"- Output dir: `{OUTPUT_DIR}`",
        f"- Monthly bucket: `{MONTH_SOURCE}`",
        f"- Report window: `{START_DATE.date()}` to `{END_DATE.date()}`",
        f"- Cross-D config: `{json.dumps(SEVEN_Y_CROSS_CONFIG, ensure_ascii=False, sort_keys=True)}`",
        "- Script dependency: only pandas/numpy plus weekly CSV; no exploration output CSV is read.",
        "",
        "## Summary",
        "",
        "| Scheme | Samples | Correct | Accuracy | Up Precision | Down Precision | Up Recall | Down Recall | Overlay | Overlay Accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| 7Y main | {baseline['total']} | {baseline['correct']} | {pct(baseline['direction_accuracy'])} | "
            f"{pct(baseline['up_precision'])} | {pct(baseline['down_precision'])} | "
            f"{pct(baseline['up_recall'])} | {pct(baseline['down_recall'])} | 0 | N/A |"
        ),
        (
            f"| Cross-D final | {overall['total']} | {overall['correct']} | {pct(overall['direction_accuracy'])} | "
            f"{pct(overall['up_precision'])} | {pct(overall['down_precision'])} | "
            f"{pct(overall['up_recall'])} | {pct(overall['down_recall'])} | "
            f"{overall['overlay_total']} | {pct(overall['overlay_accuracy'])} |"
        ),
        "",
        "## Monthly Accuracy",
        "",
        "| Month | Samples | Correct | Accuracy | True Up | True Down | True Flat | Pred Up | Pred Down | Up Precision | Down Precision | Up Recall | Down Recall | Overlay | Overlay Accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in monthly.iterrows():
        lines.append(
            f"| {row['month']} | {int(row['total'])} | {int(row['correct'])} | {pct(row['direction_accuracy'])} | "
            f"{int(row['true_up'])} | {int(row['true_down'])} | {int(row['true_flat'])} | "
            f"{int(row['pred_up'])} | {int(row['pred_down'])} | {pct(row['up_precision'])} | "
            f"{pct(row['down_precision'])} | {pct(row['up_recall'])} | {pct(row['down_recall'])} | "
            f"{int(row['overlay_total'])} | {pct(row['overlay_accuracy'])} |"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    weekly = read_weekly()
    seven_y_main = build_7y_main(weekly)
    five_y_aux = build_5y_aux(weekly)
    predictions = apply_cross_d(seven_y_main, five_y_aux, weekly)
    predictions = predictions[
        (predictions["month_date"] >= START_DATE)
        & (predictions["month_date"] <= END_DATE)
        & predictions["actual_label"].notna()
    ].copy()

    monthly = monthly_metrics(predictions, "cross_d_pred_label", "cross_d_overlay")
    overall = metrics(predictions, "cross_d_pred_label", "cross_d_overlay")
    baseline = metrics(predictions, "main_pred_label")

    weekly_path = OUTPUT_DIR / "7y_cross_d_overlay_0529_weekly_predictions_2025_07_2026_05.csv"
    monthly_path = OUTPUT_DIR / "7y_cross_d_overlay_0529_monthly_accuracy_2025_07_2026_05.csv"
    report_path = write_report(monthly, overall, baseline)

    keep_cols = [
        "week_id",
        "date",
        "week_date",
        "month",
        "TB1YWI3C",
        "TB3YWI3C",
        "TB5YWI3C",
        "TB7YWI3C",
        "TB0YWI3C",
        "actual_label",
        "future_return",
        "main_pred_label",
        "main_prob_up",
        "main_is_correct",
        "label_overlay_candidate",
        "label_overlay_applied",
        "d5_d_pred_label",
        "d5_d_prob_up",
        "d5_conf",
        "cross_d_overlay",
        "cross_d_signal_source",
        "cross_d_pred_label",
        "cross_d_prob_up",
        "cross_d_is_correct",
    ]
    predictions[[col for col in keep_cols if col in predictions.columns]].to_csv(
        weekly_path,
        index=False,
        encoding="utf-8-sig",
    )
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")

    print(f"Saved weekly predictions: {weekly_path}")
    print(f"Saved monthly accuracy: {monthly_path}")
    print(f"Saved report: {report_path}")
    print()
    print(
        monthly[
            [
                "month",
                "total",
                "correct",
                "direction_accuracy",
                "up_precision",
                "down_precision",
                "up_recall",
                "down_recall",
                "overlay_total",
                "overlay_accuracy",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
