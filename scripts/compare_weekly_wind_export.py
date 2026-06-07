from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.data_service import create_sqlalchemy_engine

WEEKLY_FREQ_ALIASES = ["周", "weekly", "Weekly", "WEEKLY", "W", "w", "2"]


def read_sql(engine, sql: str, params: dict[str, object] | None = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def metadata_order_clause(engine) -> str:
    columns = read_sql(engine, "SHOW COLUMNS FROM api_wind_indicators_all")
    fields = set(columns["Field"].astype(str))
    return " ORDER BY id" if "id" in fields else ""


def get_weekly_metadata(engine, active_pre_forecast_only: bool = False) -> pd.DataFrame:
    placeholders = ", ".join([f":f{i}" for i in range(len(WEEKLY_FREQ_ALIASES))])
    params = {f"f{i}": value for i, value in enumerate(WEEKLY_FREQ_ALIASES)}
    filters = [f"frequency IN ({placeholders})"]
    if active_pre_forecast_only:
        filters.extend(["status = :status", "pre_forecast_flag = :pre_forecast_flag"])
        params.update({"status": "1", "pre_forecast_flag": "1"})
    sql = (
        "SELECT indicators_code, lag_length "
        "FROM api_wind_indicators_all "
        f"WHERE {' AND '.join(filters)}"
        f"{metadata_order_clause(engine)}"
    )
    df = read_sql(engine, sql, params)
    df["indicators_code"] = df["indicators_code"].astype(str).str.strip()
    df = df[df["indicators_code"].ne("")].copy()
    df["lag_length_num"] = pd.to_numeric(df["lag_length"], errors="coerce").fillna(0).astype(int)
    return df[["indicators_code", "lag_length_num"]]


def fetch_weekly_long(engine, codes: list[str], table_name: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["rdate", "week_id", "indicators_code", "indicators_value"])

    frames: list[pd.DataFrame] = []
    for start in range(0, len(codes), 800):
        chunk = codes[start : start + 800]
        placeholders = ", ".join([f":c{i}" for i in range(len(chunk))])
        params = {f"c{i}": value for i, value in enumerate(chunk)}
        sql = (
            "SELECT rdate, week_id, indicators_code, indicators_value "
            f"FROM {table_name} "
            f"WHERE indicators_code IN ({placeholders}) "
            "AND indicators_value IS NOT NULL"
        )
        frames.append(read_sql(engine, sql, params))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_weekly_long(
    raw: pd.DataFrame,
    derivative: pd.DataFrame,
    include_weekend_updates: bool = False,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for frame in (raw, derivative):
        df = frame.copy()
        if not df.empty:
            # wind_export.py uses pd.to_datetime(..., errors="coerce").  With current
            # pandas, mixed rdate string formats can otherwise be coerced to NaT
            # after the first inferred format. Use the equivalent mixed parser so
            # the validation reflects the intended export behavior.
            df["rdate"] = pd.to_datetime(df["rdate"], errors="coerce", format="mixed").dt.normalize()
            df["indicators_value"] = pd.to_numeric(df["indicators_value"], errors="coerce")
            df["week_id"] = df["week_id"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            df = df[df["week_id"].str.fullmatch(r"\d{6}", na=False)].copy()
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True) if not frames[1].empty else frames[0]
    if not combined.empty:
        if not include_weekend_updates:
            combined = combined[combined["rdate"].dt.weekday <= 4].copy()
        combined = combined.dropna(subset=["indicators_code", "indicators_value"])
    return combined


def build_weekly_export(df: pd.DataFrame, output_columns: list[str], lag_map: dict[str, int]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=output_columns)

    df = df[df["week_id"].astype(str).str.fullmatch(r"\d{6}", na=False)].copy()
    df = df.sort_values(["indicators_code", "week_id", "rdate"])
    df = df.drop_duplicates(["week_id", "indicators_code"], keep="last")

    all_weeks = sorted(df["week_id"].dropna().unique())
    wide = df.pivot(index="week_id", columns="indicators_code", values="indicators_value")
    wide = wide.reindex(all_weeks)

    parts = []
    for code in output_columns:
        series = wide[code] if code in wide.columns else pd.Series(dtype=float, index=all_weeks)
        lag = lag_map.get(code, 0)
        parts.append((series.shift(lag) if lag else series).rename(code))

    result = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=all_weeks)
    result = result[result.index.notna()]
    result = result[~result.index.astype(str).isin(["None", "nan", ""])]
    result = result[result.index >= "201001"]
    result.index.name = "week_id"
    return result


def load_comparable_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "week_id" not in df.columns:
        df = df.rename(columns={df.columns[0]: "week_id"})
    df["week_id"] = pd.to_numeric(df["week_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["week_id"]).copy()
    df["week_id"] = df["week_id"].astype(int)
    for col in df.columns:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("week_id").reset_index(drop=True)


def compare_csvs(generated_path: Path, desktop_path: Path, diff_path: Path) -> dict[str, object]:
    generated = load_comparable_csv(generated_path)
    desktop = load_comparable_csv(desktop_path)
    common_weeks = sorted(set(generated["week_id"]).intersection(set(desktop["week_id"])))
    desktop_cols = set(desktop.columns)
    common_cols = [col for col in generated.columns if col != "week_id" and col in desktop_cols]

    g = generated.set_index("week_id")
    d = desktop.set_index("week_id")
    rows: list[dict[str, object]] = []
    numeric_diff_count = 0
    missing_diff_count = 0
    max_abs_diff = 0.0

    for col in common_cols:
        gv = g.loc[common_weeks, col]
        dv = d.loc[common_weeks, col]
        gna = gv.isna()
        dna = dv.isna()
        missing_mask = gna ^ dna
        for week in missing_mask[missing_mask].index:
            missing_diff_count += 1
            rows.append(
                {
                    "差异类型": "缺失差异",
                    "周编号": int(week),
                    "因子代码": col,
                    "wind_export导出值": None if pd.isna(gv.loc[week]) else float(gv.loc[week]),
                    "桌面CSV值": None if pd.isna(dv.loc[week]) else float(dv.loc[week]),
                    "绝对差异": None,
                }
            )
        both = ~(gna | dna)
        if not both.any():
            continue
        delta = (gv[both] - dv[both]).abs()
        diff_mask = delta > 1e-12
        for week, abs_diff in delta[diff_mask].items():
            numeric_diff_count += 1
            max_abs_diff = max(max_abs_diff, float(abs_diff))
            rows.append(
                {
                    "差异类型": "数值差异",
                    "周编号": int(week),
                    "因子代码": col,
                    "wind_export导出值": float(gv.loc[week]),
                    "桌面CSV值": float(dv.loc[week]),
                    "绝对差异": float(abs_diff),
                }
            )

    diff_df = pd.DataFrame(
        rows,
        columns=["差异类型", "周编号", "因子代码", "wind_export导出值", "桌面CSV值", "绝对差异"],
    )
    diff_df.to_csv(diff_path, index=False, encoding="utf-8-sig")

    summary: dict[str, object] = {
        "wind_export_path": str(generated_path),
        "desktop_path": str(desktop_path),
        "wind_export_shape": [int(generated.shape[0]), int(generated.shape[1])],
        "desktop_shape": [int(desktop.shape[0]), int(desktop.shape[1])],
        "wind_export_week_min": int(generated["week_id"].min()) if not generated.empty else None,
        "wind_export_week_max": int(generated["week_id"].max()) if not generated.empty else None,
        "desktop_week_min": int(desktop["week_id"].min()) if not desktop.empty else None,
        "desktop_week_max": int(desktop["week_id"].max()) if not desktop.empty else None,
        "common_week_count": len(common_weeks),
        "wind_export_only_weeks": sorted(set(generated["week_id"]) - set(desktop["week_id"])),
        "desktop_only_weeks": sorted(set(desktop["week_id"]) - set(generated["week_id"])),
        "wind_export_column_count": int(generated.shape[1]),
        "desktop_column_count": int(desktop.shape[1]),
        "common_column_count": len(common_cols) + 1,
        "wind_export_only_columns": [col for col in generated.columns if col not in set(desktop.columns)],
        "desktop_only_columns": [col for col in desktop.columns if col not in set(generated.columns)],
        "numeric_diff_count": numeric_diff_count,
        "missing_diff_count": missing_diff_count,
        "diff_cell_count": numeric_diff_count + missing_diff_count,
        "diff_factor_count": len({str(row["因子代码"]) for row in rows}),
        "overall_max_abs_diff": max_abs_diff if rows else 0.0,
        "latest_common_week": int(max(common_weeks)) if common_weeks else None,
        "detail_report_path": str(diff_path),
    }
    if common_weeks:
        latest_week = max(common_weeks)
        summary["latest_common_week_diff_count"] = sum(1 for row in rows if row["周编号"] == int(latest_week))
        summary["latest_common_week_diffs_sample"] = [row for row in rows if row["周编号"] == int(latest_week)][:20]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare wind_export weekly DB export with a weekly_output CSV.")
    parser.add_argument("--desktop-csv", type=Path, default=Path("/Users/macstudio0/Desktop/weekly_output.csv"))
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    parser.add_argument("--output-prefix", default="weekly_output_wind_export")
    parser.add_argument("--active-pre-forecast-only", action="store_true")
    parser.add_argument("--include-weekend-updates", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    generated_path = args.out_dir / f"{args.output_prefix}_generated.csv"
    summary_path = args.out_dir / f"{args.output_prefix}_vs_desktop_summary.json"
    diff_path = args.out_dir / f"{args.output_prefix}_vs_desktop_diff.csv"

    engine = create_sqlalchemy_engine()
    try:
        metadata = get_weekly_metadata(engine, active_pre_forecast_only=args.active_pre_forecast_only)
        output_columns = metadata["indicators_code"].tolist()
        lag_map = dict(zip(metadata["indicators_code"], metadata["lag_length_num"]))
        raw = fetch_weekly_long(engine, output_columns, "api_wind_weekly")
        derivative = fetch_weekly_long(engine, output_columns, "api_wind_derivative_weekly")
        weekly_long = normalize_weekly_long(
            raw,
            derivative,
            include_weekend_updates=args.include_weekend_updates,
        )
        exported = build_weekly_export(weekly_long, output_columns, lag_map)
    finally:
        engine.dispose()

    exported.to_csv(generated_path, encoding="utf-8-sig")
    summary = compare_csvs(generated_path, args.desktop_csv, diff_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
