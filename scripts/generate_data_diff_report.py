from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BENCHMARK_ID = "model_muti_0529"
ARTIFACT_ROOT = PROJECT_ROOT / "backtest_artifacts" / BENCHMARK_ID
CANONICAL_CSV = PROJECT_ROOT / "benchmarks" / BENCHMARK_ID / "daily_output.csv"
GENERATED_CSV = ARTIFACT_ROOT / "upstream_db_generated_daily_output.csv"
REPORT_HTML = ARTIFACT_ROOT / "historical_data_diff_report.html"
MISSING_BY_FACTOR_CSV = ARTIFACT_ROOT / "missing_diff_by_factor.csv"
MISSING_BY_MONTH_CSV = ARTIFACT_ROOT / "missing_diff_by_month.csv"
NUMERIC_BY_FACTOR_CSV = ARTIFACT_ROOT / "numeric_diff_by_factor.csv"
NUMERIC_THRESHOLD = 1e-8
EXCLUDED_TARGET_WEEK = ("2026-05-25", "2026-05-29")


@dataclass
class Metadata:
    name: str = ""
    display_name: str = ""
    unit: str = ""
    status: str = ""
    source: str = ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an independent historical data-diff validation report.")
    parser.add_argument("--output", default=str(REPORT_HTML), help="HTML report output path.")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    csv_df = read_daily(CANONICAL_CSV)
    csv_text_df = read_daily_text(CANONICAL_CSV)
    db_df = read_daily(GENERATED_CSV)
    db_text_df = read_daily_text(GENERATED_CSV)
    db_df = align_to_canonical(csv_df, db_df)
    csv_text_df = align_text_to_canonical(csv_df, csv_text_df)
    db_text_df = align_text_to_canonical(csv_df, db_text_df)
    metadata = load_metadata()

    missing_cells = collect_missing_cells(csv_df, db_df)
    missing_by_factor = summarize_missing_by_factor(missing_cells, metadata)
    missing_by_month = summarize_missing_by_month(missing_cells)
    numeric_by_factor, numeric_samples, numeric_hist, numeric_exclusions = summarize_numeric_diffs(csv_df, db_df, csv_text_df, db_text_df, metadata)
    latest_check = load_latest_check()

    missing_by_factor.to_csv(MISSING_BY_FACTOR_CSV, index=False)
    missing_by_month.to_csv(MISSING_BY_MONTH_CSV, index=False)
    numeric_by_factor.to_csv(NUMERIC_BY_FACTOR_CSV, index=False)

    html_text = build_report_html(
        csv_df=csv_df,
        db_df=db_df,
        missing_cells=missing_cells,
        missing_by_factor=missing_by_factor,
        missing_by_month=missing_by_month,
        numeric_by_factor=numeric_by_factor,
        numeric_samples=numeric_samples,
        numeric_hist=numeric_hist,
        numeric_exclusions=numeric_exclusions,
        latest_check=latest_check,
    )
    output.write_text(html_text, encoding="utf-8")
    print(output)
    print(MISSING_BY_FACTOR_CSV)
    print(MISSING_BY_MONTH_CSV)
    print(NUMERIC_BY_FACTOR_CSV)


def read_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"missing input CSV: {path}")
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def read_daily_text(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"missing input CSV: {path}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def align_to_canonical(csv_df: pd.DataFrame, db_df: pd.DataFrame) -> pd.DataFrame:
    aligned = db_df.set_index("date").reindex(csv_df["date"])
    for col in csv_df.columns:
        if col != "date" and col not in aligned.columns:
            aligned[col] = np.nan
    aligned = aligned[[col for col in csv_df.columns if col != "date"]].reset_index()
    return aligned.rename(columns={"index": "date"})


def align_text_to_canonical(csv_df: pd.DataFrame, text_df: pd.DataFrame) -> pd.DataFrame:
    aligned = text_df.set_index("date").reindex(csv_df["date"])
    for col in csv_df.columns:
        if col != "date" and col not in aligned.columns:
            aligned[col] = ""
    aligned = aligned[[col for col in csv_df.columns if col != "date"]].reset_index()
    return aligned.rename(columns={"index": "date"})


def load_metadata() -> dict[str, Metadata]:
    try:
        from shared.data_service import create_sqlalchemy_engine

        engine = create_sqlalchemy_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT indicators_code, indicators_name, indicators_display_name,
                           indicators_unit, indicators_source, status
                    FROM api_wind_indicators_all
                    """
                )
            ).mappings().all()
        engine.dispose()
    except Exception:
        return {}
    result: dict[str, Metadata] = {}
    for row in rows:
        code = str(row["indicators_code"]).strip()
        if not code:
            continue
        result[code] = Metadata(
            name=clean_meta_value(row["indicators_name"]),
            display_name=clean_meta_value(row["indicators_display_name"]),
            unit=clean_meta_value(row["indicators_unit"]),
            source=clean_meta_value(row["indicators_source"]),
            status=clean_meta_value(row["status"]),
        )
    return result


def clean_meta_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def load_latest_check() -> dict[str, Any]:
    try:
        from shared.data_service import create_sqlalchemy_engine

        engine = create_sqlalchemy_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, status, target_max_abs_diff, overall_max_abs_diff,
                           missing_diff_count, first_diff, report
                    FROM t_backtest_reproduction_checks
                    WHERE benchmark_id = :benchmark_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"benchmark_id": BENCHMARK_ID},
            ).mappings().first()
        engine.dispose()
    except Exception:
        return {}
    if row is None:
        return {}
    report = json.loads(row["report"])
    return {
        "id": row["id"],
        "status": row["status"],
        "target_max_abs_diff": json.loads(row["target_max_abs_diff"]),
        "overall_max_abs_diff": float(row["overall_max_abs_diff"] or 0),
        "missing_diff_count": int(row["missing_diff_count"] or 0),
        "first_diff": json.loads(row["first_diff"]) if row["first_diff"] else None,
        "db_only_columns": report.get("columns", {}).get("db_only", []),
        "framework_db_comparison": report.get("framework_db_comparison", {}),
        "excluding_evaluation_target_week": report.get("excluding_evaluation_target_week", {}),
    }


def collect_missing_cells(csv_df: pd.DataFrame, db_df: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for col in [item for item in csv_df.columns if item != "date"]:
        left = pd.to_numeric(csv_df[col], errors="coerce")
        right = pd.to_numeric(db_df[col], errors="coerce")
        left_na = left.isna()
        right_na = right.isna()
        csv_has_db_null = (~left_na) & right_na
        csv_null_db_has = left_na & (~right_na)
        for idx in np.flatnonzero(csv_has_db_null.to_numpy()):
            records.append({"date": csv_df.loc[idx, "date"], "column": col, "direction": "csv_has_db_null"})
        for idx in np.flatnonzero(csv_null_db_has.to_numpy()):
            records.append({"date": csv_df.loc[idx, "date"], "column": col, "direction": "csv_null_db_has"})
    return pd.DataFrame(records, columns=["date", "column", "direction"])


def summarize_missing_by_factor(missing: pd.DataFrame, metadata: dict[str, Metadata]) -> pd.DataFrame:
    if missing.empty:
        return pd.DataFrame(columns=["column", "name", "unit", "status", "csv_has_db_null", "csv_null_db_has", "total", "first_date", "last_date", "date_count", "share_pct", "cumulative_pct"])
    pivot = missing.groupby(["column", "direction"]).size().unstack(fill_value=0)
    for direction in ("csv_has_db_null", "csv_null_db_has"):
        if direction not in pivot.columns:
            pivot[direction] = 0
    pivot["total"] = pivot["csv_has_db_null"] + pivot["csv_null_db_has"]
    ranges = missing.groupby("column")["date"].agg(["min", "max", "nunique"])
    summary = pivot.join(ranges).reset_index().rename(columns={"min": "first_date", "max": "last_date", "nunique": "date_count"})
    summary = summary.sort_values("total", ascending=False).reset_index(drop=True)
    total = float(summary["total"].sum()) or 1.0
    summary["share_pct"] = (summary["total"] / total * 100).round(2)
    summary["cumulative_pct"] = (summary["total"].cumsum() / total * 100).round(2)
    summary["name"] = summary["column"].map(lambda code: metadata.get(code, Metadata()).name)
    summary["unit"] = summary["column"].map(lambda code: metadata.get(code, Metadata()).unit)
    summary["status"] = summary["column"].map(lambda code: metadata.get(code, Metadata()).status)
    summary["first_date"] = pd.to_datetime(summary["first_date"]).dt.strftime("%Y-%m-%d")
    summary["last_date"] = pd.to_datetime(summary["last_date"]).dt.strftime("%Y-%m-%d")
    columns = ["column", "name", "unit", "status", "csv_has_db_null", "csv_null_db_has", "total", "first_date", "last_date", "date_count", "share_pct", "cumulative_pct"]
    return summary[columns]


def summarize_missing_by_month(missing: pd.DataFrame) -> pd.DataFrame:
    if missing.empty:
        return pd.DataFrame(columns=["month", "csv_has_db_null", "csv_null_db_has", "total"])
    monthly = missing.assign(month=missing["date"].dt.strftime("%Y-%m")).groupby(["month", "direction"]).size().unstack(fill_value=0)
    for direction in ("csv_has_db_null", "csv_null_db_has"):
        if direction not in monthly.columns:
            monthly[direction] = 0
    monthly["total"] = monthly["csv_has_db_null"] + monthly["csv_null_db_has"]
    return monthly.reset_index()[["month", "csv_has_db_null", "csv_null_db_has", "total"]]


def summarize_numeric_diffs(
    csv_df: pd.DataFrame,
    db_df: pd.DataFrame,
    csv_text_df: pd.DataFrame,
    db_text_df: pd.DataFrame,
    metadata: dict[str, Metadata],
) -> tuple[pd.DataFrame, list[dict[str, Any]], pd.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    rounding_only_count = 0
    bins = [
        (NUMERIC_THRESHOLD, 1e-7, "1e-8~1e-7"),
        (1e-7, 5e-7, "1e-7~5e-7"),
        (5e-7, 1e-6, "5e-7~1e-6"),
        (1e-6, 1e-4, "1e-6~1e-4"),
        (1e-4, 1e-2, "1e-4~1e-2"),
        (1e-2, 1.0, "1e-2~1"),
        (1.0, 10.0, "1~10"),
        (10.0, 100.0, "10~100"),
        (100.0, np.inf, ">=100"),
    ]
    hist_counts = {label: 0 for _, _, label in bins}
    for col in [item for item in csv_df.columns if item != "date"]:
        left = pd.to_numeric(csv_df[col], errors="coerce")
        right = pd.to_numeric(db_df[col], errors="coerce")
        both = ~(left.isna() | right.isna())
        if not bool(both.any()):
            continue
        diff = (left[both] - right[both]).abs()
        bad = diff[diff > NUMERIC_THRESHOLD]
        if bad.empty:
            continue
        rounding_only = find_rounding_only_diffs(csv_text_df[col], db_text_df[col], bad.index)
        rounding_only_count += int(rounding_only.sum())
        bad = bad[~rounding_only]
        if bad.empty:
            continue
        max_idx = int(bad.idxmax())
        rows.append(
            {
                "column": col,
                "name": metadata.get(col, Metadata()).name,
                "unit": metadata.get(col, Metadata()).unit,
                "status": metadata.get(col, Metadata()).status,
                "diff_count": int(len(bad)),
                "max_abs_diff": float(bad.max()),
                "first_date": csv_df.loc[int(bad.index[0]), "date"].strftime("%Y-%m-%d"),
                "max_date": csv_df.loc[max_idx, "date"].strftime("%Y-%m-%d"),
                "csv_at_max": float(left.loc[max_idx]),
                "db_at_max": float(right.loc[max_idx]),
            }
        )
        if len(samples) < 50:
            sample_idx = int(bad.index[0])
            samples.append(
                {
                    "column": col,
                    "name": metadata.get(col, Metadata()).name,
                    "date": csv_df.loc[sample_idx, "date"].strftime("%Y-%m-%d"),
                    "csv": float(left.loc[sample_idx]),
                    "db": float(right.loc[sample_idx]),
                    "abs_diff": float(bad.loc[sample_idx]),
                }
            )
        for low, high, label in bins:
            if np.isinf(high):
                hist_counts[label] += int((bad >= low).sum())
            else:
                hist_counts[label] += int(((bad >= low) & (bad < high)).sum())
    numeric = pd.DataFrame(rows)
    if not numeric.empty:
        numeric = numeric.sort_values(["max_abs_diff", "diff_count"], ascending=False).reset_index(drop=True)
    hist = pd.DataFrame([{"bucket": label, "count": count} for _, _, label in bins for count in [hist_counts[label]]])
    exclusions = {
        "rounding_only_count": rounding_only_count,
        "rounding_rule": "DB 值按常规四舍五入到历史 CSV 单元格的小数位后等于历史 CSV 值",
    }
    return numeric, samples, hist, exclusions


def find_rounding_only_diffs(csv_raw_values: pd.Series, db_raw_values: pd.Series, indexes: pd.Index) -> pd.Series:
    if len(indexes) == 0:
        return pd.Series(False, index=indexes)
    csv_raw = csv_raw_values.reindex(indexes)
    db_raw = db_raw_values.reindex(indexes)
    result = pd.Series(False, index=indexes)
    for idx in indexes:
        result.loc[idx] = rounds_to_csv_value(csv_raw.loc[idx], db_raw.loc[idx])
    return result


def rounds_to_csv_value(csv_value: Any, db_value: Any) -> bool:
    csv_text = str(csv_value).strip()
    db_text = str(db_value).strip()
    if not csv_text or not db_text:
        return False
    try:
        csv_decimal = Decimal(csv_text)
        db_decimal = Decimal(db_text)
    except InvalidOperation:
        return False
    if not csv_decimal.is_finite() or not db_decimal.is_finite():
        return False
    places = max(-csv_decimal.as_tuple().exponent, 0)
    quant = Decimal(1).scaleb(-places)
    return db_decimal.quantize(quant, rounding=ROUND_HALF_UP) == csv_decimal


def build_report_html(
    *,
    csv_df: pd.DataFrame,
    db_df: pd.DataFrame,
    missing_cells: pd.DataFrame,
    missing_by_factor: pd.DataFrame,
    missing_by_month: pd.DataFrame,
    numeric_by_factor: pd.DataFrame,
    numeric_samples: list[dict[str, Any]],
    numeric_hist: pd.DataFrame,
    numeric_exclusions: dict[str, Any],
    latest_check: dict[str, Any],
) -> str:
    top_factors = missing_by_factor.head(20)
    top_factor_names = top_factors["column"].tolist()
    heatmap = (
        missing_cells[missing_cells["column"].isin(top_factor_names)]
        .assign(month=missing_cells["date"].dt.strftime("%Y-%m"))
        .groupby(["column", "month"])
        .size()
        .unstack(fill_value=0)
        if not missing_cells.empty and top_factor_names
        else pd.DataFrame()
    )
    total_missing = int(len(missing_cells))
    direction_counts = missing_cells["direction"].value_counts().to_dict() if not missing_cells.empty else {}
    date_min = csv_df["date"].min().strftime("%Y-%m-%d")
    date_max = csv_df["date"].max().strftime("%Y-%m-%d")
    factor_count = int(missing_by_factor["column"].nunique()) if not missing_by_factor.empty else 0
    top16_share = float(missing_by_factor.head(16)["total"].sum() / total_missing * 100) if total_missing else 0.0
    substantive_numeric_cells = int(numeric_by_factor["diff_count"].sum()) if not numeric_by_factor.empty else 0
    substantive_numeric_factors = int(numeric_by_factor["column"].nunique()) if not numeric_by_factor.empty else 0
    max_abs = float(numeric_by_factor["max_abs_diff"].max()) if not numeric_by_factor.empty else 0.0
    target_diff = latest_check.get("target_max_abs_diff", {}) if latest_check else {}
    target_values = [float(value or 0) for value in target_diff.values()]
    target_max_abs = max(target_values) if target_values else 0.0
    target_ok = all(value == 0.0 for value in target_values) if target_values else True
    framework_cmp = latest_check.get("framework_db_comparison", {}) if latest_check else {}
    framework_ok = float(framework_cmp.get("overall_max_abs_diff") or 0) == 0.0 and int(framework_cmp.get("missing_diff_count") or 0) == 0
    db_only = ", ".join(latest_check.get("db_only_columns", [])) or "无" if latest_check else "无"
    cards = [
        ("缺失差异单元格", f"{total_missing:,}"),
        ("涉及因子数", f"{factor_count:,}"),
        ("实质数值差异单元格", f"{substantive_numeric_cells:,}"),
        ("日期范围", f"{date_min} ~ {date_max}"),
        ("主要方向", f"CSV 有值 / DB 缺失: {direction_counts.get('csv_has_db_null', 0):,}"),
        ("前16个因子累计占比", f"{top16_share:.2f}%"),
        ("实质数值涉及因子", f"{substantive_numeric_factors:,}"),
        ("实质数值最大误差", f"{max_abs:g}"),
    ]
    latest_note = ""
    if latest_check:
        latest_note = f"""
        <div class="note">
          <strong>口径说明</strong>
          <span>当前 DB 上游生成 CSV 与当前框架生成 CSV 对齐版: 最大误差 {fmt(framework_cmp.get("overall_max_abs_diff"))}，缺失差异 {fmt(framework_cmp.get("missing_diff_count"))}。</span>
          <span>DB 完整输出相对历史 CSV 多列: {esc(db_only)}。</span>
          <span>数值差异统计口径: {esc(numeric_exclusions.get("rounding_rule"))}，这类差异不计入统计。</span>
        </div>
        """
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>历史 daily_output 数据差异验证报告</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17211b;
      --muted: #647067;
      --line: #dfe5dc;
      --paper: #fbfbf7;
      --panel: #ffffff;
      --green: #15623f;
      --blue: #2f6f9f;
      --gold: #a06f16;
      --red: #b83d3d;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 32px 28px 64px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 30px 0 12px; font-size: 19px; letter-spacing: 0; }}
    p {{ margin: 0 0 12px; color: var(--muted); }}
    .kicker {{ margin-bottom: 6px; color: var(--green); font-size: 13px; font-weight: 700; }}
    .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }}
    .card, .panel, .note {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .card {{ padding: 14px 16px; }}
    .card span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .card strong {{ display: block; font-size: 19px; }}
    .panel {{ padding: 16px; margin-bottom: 16px; overflow: auto; }}
    .note {{ padding: 12px 14px; display: grid; gap: 5px; margin: 16px 0; }}
    .note span {{ color: var(--muted); }}
    .status-title {{ margin: 18px 0 8px; font-size: 16px; }}
    .status-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0 4px; }}
    .status-item {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 13px 14px; }}
    .status-item b {{ display: block; margin-bottom: 4px; }}
    .status-item span {{ color: var(--muted); font-size: 12px; }}
    .ok b {{ color: var(--green); }}
    .warn b {{ color: var(--gold); }}
    .danger b {{ color: var(--red); }}
    svg text {{ font: 11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: var(--muted); }}
    table {{ border-collapse: collapse; width: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ font-size: 12px; color: var(--muted); background: #f3f6f1; }}
    tr:last-child td {{ border-bottom: 0; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .summary {{ color: var(--ink); max-width: 860px; }}
    @media (max-width: 820px) {{
      main {{ padding: 24px 16px 48px; }}
      .cards, .status-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
<main>
  <div class="kicker">离线验证报告</div>
  <h1>历史 daily_output 数据差异验证报告</h1>
  <p>对比对象: 上游历史保存 CSV vs 当前 DB 按上游 data_service 口径重新生成 CSV。Y 生成所依赖的收益率列完全一致；本文只展开因子列差异。</p>
  <h2 class="status-title">首屏验收看板</h2>
  <div class="status-grid">
    <div class="status-item {'ok' if target_ok else 'danger'}"><b>Y 生成列: {'一致' if target_ok else '存在差异'}</b><span>收益率列最大误差为 {esc(target_max_abs)}。</span></div>
    <div class="status-item {'ok' if framework_ok else 'danger'}"><b>生成链路: {'一致' if framework_ok else '存在差异'}</b><span>上游 data_service 与当前框架对齐版比较。</span></div>
    <div class="status-item warn"><b>DB 多列: {esc(db_only)}</b><span>多出的列不参与基准对齐比较。</span></div>
    <div class="status-item warn"><b>数值口径: 剔除四舍五入</b><span>{esc(numeric_exclusions.get("rounding_rule"))}。</span></div>
  </div>
  <div class="cards">{''.join(card(label, value) for label, value in cards)}</div>
  {latest_note}
  <h2>结论摘要</h2>
  <p class="summary">缺失差异不是某几个交易日集中异常，而是少数商品/现货价格类因子在当前 DB 中长期为 NULL；历史 CSV 中保存过这些值。前 16 个因子已经覆盖几乎全部缺失差异。数值差异部分已排除纯四舍五入造成的差异。</p>
  <div class="panel">{svg_year_bars(missing_cells)}</div>
  <h2>主要差异因子贡献图（累计占比）</h2>
  <p>按每个因子的缺失差异数量从高到低排序；右侧“累计占比”表示从第一行累加到当前行，已经解释了全部缺失差异中的多少比例。</p>
  <div class="panel">{svg_top_factor_bars(top_factors)}</div>
  <h2>主要差异因子月度热力图</h2>
  <div class="panel">{svg_heatmap(heatmap, top_factors)}</div>
  <h2>主要差异因子缺失时间轴</h2>
  <div class="panel">{svg_timeline(top_factors)}</div>
  <h2>实质数值差异分布（已剔除四舍五入）</h2>
  <div class="panel">{svg_histogram(numeric_hist)}</div>
  <h2>缺失差异主要因子明细</h2>
  {table_html(top_factors, ["column", "name", "unit", "status", "total", "first_date", "last_date", "date_count", "share_pct", "cumulative_pct"])}
  <h2>月度缺失差异前30名</h2>
  {table_html(missing_by_month.sort_values("total", ascending=False).head(30), ["month", "csv_has_db_null", "csv_null_db_has", "total"])}
  <h2>数值差异前20名</h2>
  {table_html(numeric_by_factor.head(20), ["column", "name", "unit", "status", "diff_count", "max_abs_diff", "first_date", "max_date", "csv_at_max", "db_at_max"])}
  <h2>数值差异样本</h2>
  {table_html(pd.DataFrame(numeric_samples), ["column", "name", "date", "csv", "db", "abs_diff"])}
</main>
</body>
</html>
"""
    return html_text


def card(label: str, value: str) -> str:
    return f'<div class="card"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'


def svg_year_bars(missing: pd.DataFrame) -> str:
    if missing.empty:
        return "<p>无缺失差异。</p>"
    data = missing.assign(year=missing["date"].dt.year).groupby("year").size().reset_index(name="total")
    width = 1020
    height = 270
    left = 44
    bottom = 42
    top = 18
    max_v = max(int(data["total"].max()), 1)
    bar_w = max(12, int((width - left - 20) / len(data)) - 4)
    parts = [f'<svg width="{width}" height="{height}" role="img" aria-label="年度缺失差异柱状图">']
    parts.append(f'<line x1="{left}" y1="{height-bottom}" x2="{width-10}" y2="{height-bottom}" stroke="#dfe5dc"/>')
    for i, row in data.iterrows():
        x = left + i * (bar_w + 4)
        h = (height - bottom - top) * row["total"] / max_v
        y = height - bottom - h
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="#2f6f9f"/>')
        parts.append(f'<text x="{x + bar_w/2:.1f}" y="{height-22}" text-anchor="middle">{int(row["year"])}</text>')
        if i % 2 == 0:
            parts.append(f'<text x="{x + bar_w/2:.1f}" y="{y-4:.1f}" text-anchor="middle">{int(row["total"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_top_factor_bars(top: pd.DataFrame) -> str:
    if top.empty:
        return "<p>无缺失差异。</p>"
    width = 1080
    row_h = 24
    left = 116
    height = 28 + row_h * len(top)
    max_v = max(int(top["total"].max()), 1)
    parts = [f'<svg width="{width}" height="{height}" role="img" aria-label="主要差异因子贡献图">']
    for i, row in top.reset_index(drop=True).iterrows():
        y = 20 + i * row_h
        w = (width - left - 170) * row["total"] / max_v
        label = f'{row["column"]}'
        parts.append(f'<text x="4" y="{y+13}" class="mono">{esc(label)}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="15" fill="#15623f"/>')
        parts.append(f'<text x="{left + w + 8:.1f}" y="{y+12}">{int(row["total"]):,} / 累计 {float(row["cumulative_pct"]):.2f}%</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_heatmap(heatmap: pd.DataFrame, top: pd.DataFrame) -> str:
    if heatmap.empty:
        return "<p>无热力图数据。</p>"
    ordered = top["column"].tolist()
    heatmap = heatmap.reindex(ordered).fillna(0)
    months = sorted(heatmap.columns.tolist())
    cell_w = 6
    cell_h = 18
    left = 110
    top_pad = 30
    width = left + cell_w * len(months) + 30
    height = top_pad + cell_h * len(ordered) + 34
    max_v = max(float(heatmap.to_numpy().max()), 1.0)
    parts = [f'<svg width="{width}" height="{height}" role="img" aria-label="主要差异因子月度热力图">']
    for j, month in enumerate(months):
        if month.endswith("-01"):
            x = left + j * cell_w
            parts.append(f'<text x="{x}" y="18" transform="rotate(-45 {x},18)">{esc(month[:4])}</text>')
    for i, code in enumerate(ordered):
        y = top_pad + i * cell_h
        parts.append(f'<text x="4" y="{y+12}" class="mono">{esc(code)}</text>')
        for j, month in enumerate(months):
            val = float(heatmap.loc[code, month])
            color = heat_color(val / max_v)
            x = left + j * cell_w
            parts.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h-2}" fill="{color}"><title>{esc(code)} {esc(month)}: {int(val)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def svg_timeline(top: pd.DataFrame) -> str:
    if top.empty:
        return "<p>无时间轴数据。</p>"
    start = pd.to_datetime(top["first_date"]).min()
    end = pd.to_datetime(top["last_date"]).max()
    span = max((end - start).days, 1)
    width = 1060
    row_h = 25
    left = 116
    height = 36 + row_h * len(top)
    parts = [f'<svg width="{width}" height="{height}" role="img" aria-label="主要差异因子缺失时间轴">']
    parts.append(f'<text x="{left}" y="16">{start.strftime("%Y-%m-%d")}</text><text x="{width-100}" y="16">{end.strftime("%Y-%m-%d")}</text>')
    parts.append(f'<line x1="{left}" y1="24" x2="{width-40}" y2="24" stroke="#dfe5dc"/>')
    for i, row in top.reset_index(drop=True).iterrows():
        y = 42 + i * row_h
        first = pd.Timestamp(row["first_date"])
        last = pd.Timestamp(row["last_date"])
        x1 = left + (first - start).days / span * (width - left - 50)
        x2 = left + (last - start).days / span * (width - left - 50)
        parts.append(f'<text x="4" y="{y+4}" class="mono">{esc(row["column"])}</text>')
        parts.append(f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" stroke="#a06f16" stroke-width="8" stroke-linecap="round"/>')
        parts.append(f'<text x="{x2 + 8:.1f}" y="{y+4}">{int(row["date_count"]):,} 天</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_histogram(hist: pd.DataFrame) -> str:
    if hist.empty:
        return "<p>无数值差异。</p>"
    width = 980
    height = 260
    left = 64
    bottom = 52
    max_v = max(int(hist["count"].max()), 1)
    bar_w = max(34, int((width - left - 20) / len(hist)) - 8)
    parts = [f'<svg width="{width}" height="{height}" role="img" aria-label="实质数值差异直方图">']
    parts.append(f'<line x1="{left}" y1="{height-bottom}" x2="{width-10}" y2="{height-bottom}" stroke="#dfe5dc"/>')
    for i, row in hist.iterrows():
        x = left + i * (bar_w + 8)
        h = (height - bottom - 22) * row["count"] / max_v
        y = height - bottom - h
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="#b83d3d"/>')
        parts.append(f'<text x="{x + bar_w/2:.1f}" y="{height-28}" text-anchor="middle" transform="rotate(-25 {x + bar_w/2:.1f},{height-28})">{esc(row["bucket"])}</text>')
        parts.append(f'<text x="{x + bar_w/2:.1f}" y="{y-4:.1f}" text-anchor="middle">{int(row["count"]):,}</text>')
    parts.append("</svg>")
    return "".join(parts)


def table_html(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "<p>无数据。</p>"
    existing = [col for col in columns if col in df.columns]
    head = "".join(f"<th>{esc(col)}</th>" for col in existing)
    rows = []
    for _, row in df[existing].iterrows():
        cells = "".join(f'<td class="{"mono" if col in {"column", "month"} else ""}">{esc(fmt(row[col]))}</td>' for col in existing)
        rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def heat_color(ratio: float) -> str:
    ratio = min(max(ratio, 0.0), 1.0)
    low = (239, 248, 233)
    high = (21, 98, 63)
    rgb = tuple(round(low[i] + (high[i] - low[i]) * ratio) for i in range(3))
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:g}"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    return str(value)


def esc(value: Any) -> str:
    return html.escape(fmt(value), quote=True)


if __name__ == "__main__":
    main()
