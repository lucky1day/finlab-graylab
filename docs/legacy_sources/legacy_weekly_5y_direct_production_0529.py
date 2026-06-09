#!/usr/bin/env python3
"""
5Y fully independent direct-production weekly signal, 0529 version.

Run directly:
    python production_10y_d_overlay_0529/weekly_5y_direct_production_0529.py

This file intentionally does not import the multi-tenor production scripts.
It reads weekly_output0529.csv, applies the locked 5Y three-rule vote, and
writes production-window weekly predictions plus monthly accuracy artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


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

TARGET_COL = "TB5YWI3C"
START_DATE = pd.Timestamp("2025-07-01")
END_DATE = pd.Timestamp("2026-05-31")
MONTH_SOURCE = "week_date"
ANNUAL_START_WEEK = 202001
ANNUAL_END_WEEK = 202616

PRODUCTION_PROFILE = "5y_direct_independent_rule_vote_weekly0529"
PRODUCTION_MODEL = "Direct_Production_Final"

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
TIE_LABEL = -1


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def safe_rate(num: int, den: int) -> float:
    return num / den if den else np.nan


def week_id_to_friday(week_id: int | float) -> pd.Timestamp:
    week_text = str(int(week_id))
    if len(week_text) < 6:
        return pd.NaT
    if len(week_text) == 8:
        parsed = pd.to_datetime(week_text, format="%Y%m%d", errors="coerce")
        if pd.notna(parsed):
            return parsed
    year = int(week_text[:4])
    week = int(week_text[4:])
    try:
        return pd.Timestamp.fromisocalendar(year, week, 5)
    except ValueError:
        return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=(week - 1) * 7 + 4)


def require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in {INPUT_PATH}: {missing}")


def read_weekly() -> pd.DataFrame:
    df = pd.read_csv(INPUT_PATH, encoding="utf-8-sig")
    df.columns = [col.strip().lstrip("\ufeff") for col in df.columns]
    require_columns(df, ["week_id", "TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"])
    df = df[["week_id", "TB1YWI3C", "TB5YWI3C", "TB7YWI3C", "TB0YWI3C"]].copy()
    df = df.sort_values("week_id").reset_index(drop=True)
    numeric_cols = [col for col in df.columns if col != "week_id"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df["week_id"] = df["week_id"].astype(int)
    df["week_date"] = df["week_id"].map(week_id_to_friday)
    return df


def add_target_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = pd.to_numeric(out[TARGET_COL], errors="coerce")
    out["future_return"] = close.shift(-1).div(close).sub(1.0)
    out["actual_label"] = np.select(
        [out["future_return"] > 0.0, out["future_return"] < 0.0],
        [1, -1],
        default=0,
    ).astype(float)
    out.loc[out["future_return"].isna(), "actual_label"] = np.nan
    return out


def build_rule_signal(rule: dict[str, Any], weekly: pd.DataFrame) -> pd.DataFrame:
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


def build_rule_vote(weekly: pd.DataFrame) -> pd.DataFrame:
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
    return merged


def build_predictions() -> pd.DataFrame:
    weekly = add_target_labels(read_weekly())
    vote = build_rule_vote(weekly)
    meta_cols = ["week_id", "week_date", TARGET_COL, "actual_label", "future_return"]
    out = vote.merge(weekly[meta_cols], on="week_id", how="left")
    out = out[out["actual_label"].notna()].copy()
    out["actual_label"] = out["actual_label"].astype(int)
    out["is_correct"] = out["final_pred_label"].astype(int).eq(out["actual_label"].astype(int))
    out["winner_score"] = np.where(
        out["final_pred_label"].eq(1),
        out["final_prob_up"].astype(float),
        1.0 - out["final_prob_up"].astype(float),
    )
    out["month_date"] = pd.to_datetime(out[MONTH_SOURCE])
    out["month"] = out["month_date"].dt.to_period("M").astype(str)
    out["selector_profile"] = PRODUCTION_PROFILE
    return out.sort_values("week_id").reset_index(drop=True)


def metrics(df: pd.DataFrame) -> dict[str, Any]:
    pred = df["final_pred_label"].astype(int)
    actual = df["actual_label"].astype(int)
    correct = pred.eq(actual)
    pred_up = int(pred.eq(1).sum())
    pred_down = int(pred.eq(-1).sum())
    true_up = int(actual.eq(1).sum())
    true_down = int(actual.eq(-1).sum())
    true_flat = int(actual.eq(0).sum())
    up_correct = int((pred.eq(1) & actual.eq(1)).sum())
    down_correct = int((pred.eq(-1) & actual.eq(-1)).sum())
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
    }


def monthly_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for month, group in df.groupby("month", sort=True):
        row = {"month": month}
        row.update(metrics(group))
        rows.append(row)
    return pd.DataFrame(rows)


def annual_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    work = df.copy()
    work["year"] = (work["week_id"].astype(int) // 100).astype(int)
    for year, group in work.groupby("year", sort=True):
        row = {"year": int(year)}
        row.update(metrics(group))
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(
    monthly: pd.DataFrame,
    overall: dict[str, Any],
    annual: pd.DataFrame,
    annual_overall: dict[str, Any],
) -> Path:
    report_path = OUTPUT_DIR / "5Y_DIRECT_PRODUCTION_0529_MONTHLY_2025_07_2026_05.md"
    rule_config = {
        "target_col": TARGET_COL,
        "rules": [{"weight": weight, **rule} for rule, weight in RULES],
        "tie_label": TIE_LABEL,
    }
    lines = [
        "# 5Y Direct Production 0529 Independent Result",
        "",
        "## Fixed Protocol",
        "",
        f"- Input: `{INPUT_PATH}`",
        f"- Output directory: `{OUTPUT_DIR}`",
        f"- Month source: `{MONTH_SOURCE}`",
        f"- Production window: `{START_DATE.date()}` to `{END_DATE.date()}`",
        f"- Annual accuracy window: `{ANNUAL_START_WEEK}` to `{ANNUAL_END_WEEK}`; year grouped by `week_id // 100`",
        f"- Rule config: `{json.dumps(rule_config, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Overall",
        "",
        "| Samples | Correct | Accuracy | Up Precision | Down Precision | Up Recall | Down Recall | Pred Up | Pred Down |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {overall['total']} | {overall['correct']} | {pct(overall['direction_accuracy'])} | "
            f"{pct(overall['up_precision'])} | {pct(overall['down_precision'])} | "
            f"{pct(overall['up_recall'])} | {pct(overall['down_recall'])} | "
            f"{overall['pred_up']} | {overall['pred_down']} |"
        ),
        "",
        "## Monthly Accuracy",
        "",
        "| Month | Samples | Correct | Accuracy | True Up | True Down | True Flat | Pred Up | Pred Down | Up Precision | Down Precision | Up Recall | Down Recall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in monthly.iterrows():
        lines.append(
            f"| {row['month']} | {int(row['total'])} | {int(row['correct'])} | {pct(row['direction_accuracy'])} | "
            f"{int(row['true_up'])} | {int(row['true_down'])} | {int(row['true_flat'])} | "
            f"{int(row['pred_up'])} | {int(row['pred_down'])} | {pct(row['up_precision'])} | "
            f"{pct(row['down_precision'])} | {pct(row['up_recall'])} | {pct(row['down_recall'])} |"
        )
    lines.extend(
        [
            "",
            "## Annual Accuracy",
            "",
            "| Year | Samples | Correct | Accuracy | True Up | True Down | True Flat | Pred Up | Pred Down | Up Precision | Down Precision | Up Recall | Down Recall |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in annual.iterrows():
        lines.append(
            f"| {int(row['year'])} | {int(row['total'])} | {int(row['correct'])} | {pct(row['direction_accuracy'])} | "
            f"{int(row['true_up'])} | {int(row['true_down'])} | {int(row['true_flat'])} | "
            f"{int(row['pred_up'])} | {int(row['pred_down'])} | {pct(row['up_precision'])} | "
            f"{pct(row['down_precision'])} | {pct(row['up_recall'])} | {pct(row['down_recall'])} |"
        )
    lines.extend(
        [
            "",
            "## Annual Window Overall",
            "",
            "| Samples | Correct | Accuracy | Up Precision | Down Precision | Up Recall | Down Recall | Pred Up | Pred Down |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            (
                f"| {annual_overall['total']} | {annual_overall['correct']} | {pct(annual_overall['direction_accuracy'])} | "
                f"{pct(annual_overall['up_precision'])} | {pct(annual_overall['down_precision'])} | "
                f"{pct(annual_overall['up_recall'])} | {pct(annual_overall['down_recall'])} | "
                f"{annual_overall['pred_up']} | {annual_overall['pred_down']} |"
            ),
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_predictions = build_predictions()
    production = all_predictions[
        (all_predictions["month_date"] >= START_DATE) & (all_predictions["month_date"] <= END_DATE)
    ].copy()
    annual_window = all_predictions[
        (all_predictions["week_id"] >= ANNUAL_START_WEEK) & (all_predictions["week_id"] <= ANNUAL_END_WEEK)
    ].copy()
    monthly = monthly_metrics(production)
    overall = metrics(production)
    annual = annual_metrics(annual_window)
    annual_overall = metrics(annual_window)

    weekly_path = OUTPUT_DIR / "5y_direct_production_0529_weekly_predictions_2025_07_2026_05.csv"
    monthly_path = OUTPUT_DIR / "5y_direct_production_0529_monthly_accuracy_2025_07_2026_05.csv"
    annual_path = OUTPUT_DIR / "5y_direct_production_0529_annual_accuracy_2020_2026.csv"
    all_path = OUTPUT_DIR / "5y_direct_production_0529_all_valid_predictions.csv"
    report_path = write_report(monthly, overall, annual, annual_overall)

    keep_cols = [
        "week_id",
        "week_date",
        "month",
        TARGET_COL,
        "actual_label",
        "future_return",
        "rule_vote",
        "final_prob_up",
        "final_pred_label",
        "winner_score",
        "winner_model",
        "selector_decision",
        "source_spec",
        "score_spec",
        "is_correct",
        "selector_profile",
    ]
    for rule, _ in RULES:
        name = str(rule["name"])
        keep_cols.extend([f"{name}__pred_label", f"{name}__prob_up"])
    keep_cols = [col for col in keep_cols if col in all_predictions.columns]

    production[keep_cols].to_csv(weekly_path, index=False, encoding="utf-8-sig")
    all_predictions[keep_cols].to_csv(all_path, index=False, encoding="utf-8-sig")
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    annual.to_csv(annual_path, index=False, encoding="utf-8-sig")

    print(f"Saved weekly predictions: {weekly_path}")
    print(f"Saved all valid predictions: {all_path}")
    print(f"Saved monthly accuracy: {monthly_path}")
    print(f"Saved annual accuracy: {annual_path}")
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
            ]
        ].to_string(index=False)
    )
    print()
    print(
        annual[
            [
                "year",
                "total",
                "correct",
                "direction_accuracy",
                "up_precision",
                "down_precision",
                "up_recall",
                "down_recall",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
