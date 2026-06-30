#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = "2026-06-10"
MONTHLY_DB_RDATE = "2026-04-15"


def read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, low_memory=False)


def path_exists_table(paths: dict[str, Path]) -> list[str]:
    lines = ["| item | path | exists |", "| --- | --- | ---: |"]
    for name, path in paths.items():
        lines.append(f"| {name} | `{path.relative_to(PROJECT_ROOT)}` | {path.exists()} |")
    return lines


def daily_section() -> list[str]:
    payload_path = PROJECT_ROOT / "daily_project" / "output" / RUN_DATE / "db_payload" / "daily_pre_market_forecast_payload.json"
    metadata_path = PROJECT_ROOT / "daily_project" / "check_data" / RUN_DATE / "metadata" / "daily_model_input_metadata.json"
    detail_path = PROJECT_ROOT / "daily_project" / "output" / RUN_DATE / "prediction" / "daily_prediction_detail.json"
    payload = read_json(payload_path)
    metadata = read_json(metadata_path)
    detail = read_json(detail_path)
    lines = [
        "## Daily",
        "",
        f"- input source: `{metadata.get('source', 'database')}`",
        f"- model input rows/cols: `{metadata['row_count']}` / `{metadata['column_count']}`",
        "",
        "| frequency | final id | result | signal | prediction date | candidate |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in payload:
        item = detail[row["frequency"]]
        lines.append(
            f"| {row['frequency']} | {row['final_select_id']} | {row['result']} | "
            f"{row['pred_label']} | {item['prediction_date']} | `{row['candidate_id']}` |"
        )
    return lines


def weekly_section() -> list[str]:
    detail_path = PROJECT_ROOT / "weekly_project" / "output" / RUN_DATE / "prediction" / "weekly_prediction_detail.json"
    metadata_path = PROJECT_ROOT / "weekly_project" / "check_data" / RUN_DATE / "metadata" / "weekly_model_input_metadata.json"
    payload_path = PROJECT_ROOT / "weekly_project" / "output" / RUN_DATE / "db_payload" / "weekly_db_payload.csv"
    detail = read_json(detail_path)
    metadata = read_json(metadata_path)
    payload = read_csv(payload_path)
    lines = [
        "## Weekly",
        "",
        f"- input source: `{metadata['source']}`",
        f"- requested/effective/latest week: `{metadata['requested_week_id']}` / `{metadata['effective_week_id']}` / `{metadata['latest_week_id']}`",
        f"- payload rows: `{len(payload)}`",
        "",
        "| frequency | result | signal | target col | prob_up |",
        "| --- | --- | ---: | --- | ---: |",
    ]
    for frequency, item in detail.items():
        lines.append(
            f"| {frequency} | {item['result']} | {item['pred_label']} | "
            f"`{item['close_col']}` | {item.get('prob_up')} |"
        )
    return lines


def monthly_section() -> list[str]:
    metadata_path = PROJECT_ROOT / "monthly_project" / "check_data" / MONTHLY_DB_RDATE / "metadata" / "run_metadata.json"
    payload_path = (
        PROJECT_ROOT
        / "monthly_project"
        / "output"
        / MONTHLY_DB_RDATE
        / "db_payload"
        / "t_pre_market_forecast_monthly_dry_run.csv"
    )
    metadata = read_json(metadata_path)
    payload = read_csv(payload_path)
    lines = [
        "## Monthly",
        "",
        f"- trigger/db rdate: `{metadata['trigger_date']}` / `{metadata['db_rdate']}`",
        f"- feature month: `{metadata['feature_month']}`",
        f"- requested/effective target month: `{metadata['requested_target_month']}` / `{metadata['effective_target_month']}`",
        f"- payload rows: `{len(payload)}`",
        "- known limit: historical `2026-05` target bucket is incomplete and is not formal accuracy.",
        "",
        "| frequency | final id | result | signal | model id |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for _, row in payload.iterrows():
        source_row = ast.literal_eval(row["source_row"]) if isinstance(row.get("source_row"), str) else {}
        lines.append(
            f"| {row['frequency']} | {source_row.get('final_select_id')} | {row['result']} | "
            f"{row['signal_label']} | `{row['model_id']}` |"
        )
    return lines


def build_report() -> str:
    required_paths = {
        "daily payload": PROJECT_ROOT / "daily_project" / "output" / RUN_DATE / "db_payload" / "daily_pre_market_forecast_payload.json",
        "weekly payload": PROJECT_ROOT / "weekly_project" / "output" / RUN_DATE / "db_payload" / "weekly_db_payload.csv",
        "monthly payload": PROJECT_ROOT / "monthly_project" / "output" / MONTHLY_DB_RDATE / "db_payload" / "t_pre_market_forecast_monthly.csv",
        "daily selected source": PROJECT_ROOT / "daily_project" / "src" / "daily" / "selected_models" / "1y13" / "run.py",
        "weekly source": PROJECT_ROOT / "weekly_project" / "src" / "weekly" / "run_weekly.py",
        "monthly selected source": PROJECT_ROOT / "monthly_project" / "src" / "monthly" / "selected_models" / "export_selected.py",
    }
    lines = [
        "# forecast_project_0616 Production-Format Migration Report",
        "",
        "## Scope",
        "",
        "- Migration target: `final_select/forecast_project_0616` local package only.",
        "- Standard: `db_data/迁移模版.md` production-format contract.",
        "- Production entrypoints default to `DRY_RUN=0` and write DB payloads; validation uses explicit `DRY_RUN=1`.",
        "- Current data contract: production runs build all model inputs from database tables; package-local CSV files are not deployment inputs.",
        "- Final selected models: daily `1Y13/5Y10/10Y04`, weekly `WEEKLY-1Y/5Y/10Y-LGBM-01`, monthly `1Y-06/5Y-06/10Y-01`.",
        "",
        "## Required Artifacts",
        "",
        *path_exists_table(required_paths),
        "",
        *daily_section(),
        "",
        *weekly_section(),
        "",
        *monthly_section(),
        "",
        "## Contract Notes",
        "",
        "- Root directory keeps only shared code, unified scripts, docs, and the three frequency projects.",
        "- Old reproduction roots `daily/`, `weekly/`, `monthly/`, `outputs/`, `source_snapshots/`, `configs/`, and root `data/` are intentionally removed.",
        "- Package-local historical data defaults are intentionally removed; any CSV consumed by candidate code must be generated from the database during the same run.",
        "- Daily T+1 labels use `target[t+1] / target[t] - 1` sign.",
        "- Weekly targets use `WI1C`: `TB1YWI1C/TB5YWI1C/TB0YWI1C`.",
        "- Direction mapping is yield up `1 -> 空`, yield down `-1 -> 多`, no trade/flat `0 -> 平`.",
        "- SHAP policy is documented in `docs/shap方案_9模型.md`: daily/weekly tree components export model contributions; monthly KNN/RF candidates generate cleaned pseudo SHAP rows for the same `t_shap` write path.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build current production-format migration report.")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs" / "production_migration_report.md")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_report(), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
