from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env


VALID_PHASES = {"gray_live", "scheduled_live"}

PHASE_BY_SCHEME_RUN: dict[tuple[str, int], str] = {
    **{("daily_5y_2_v28", run_id): "gray_live" for run_id in range(39, 52)},
    ("daily_5y_2_v28", 52): "scheduled_live",
    ("weekly_5y_direct_0529", 31): "gray_live",
    ("weekly_5y_direct_0529", 32): "gray_live",
    ("weekly_7y_cross_d_overlay_0529", 35): "gray_live",
    ("weekly_7y_cross_d_overlay_0529", 36): "gray_live",
    ("weekly_10y_d_overlay_0529", 37): "gray_live",
    ("weekly_10y_d_overlay_0529", 38): "gray_live",
    **{("t1_daily", run_id): "gray_live" for run_id in range(22, 30)},
    ("t1_daily", 34): "scheduled_live",
    ("t1_daily", 53): "scheduled_live",
    **{("t5_daily", run_id): "gray_live" for run_id in (2, 4, 6, 8, 11, 16, 17, 18, 19, 20, 21)},
    ("t5_daily", 33): "scheduled_live",
    ("t5_daily", 54): "scheduled_live",
}


@dataclass(frozen=True)
class PredictionSemanticsUpdate:
    prediction_id: int
    scheme_id: str
    run_id: int
    feature_date: str
    prediction_phase: str


def collect_updates(engine: Engine) -> tuple[list[PredictionSemanticsUpdate], list[str]]:
    """收集待回填行；不修改数据库。"""
    sql = text(
        """
        SELECT id, run_id, scheme_id, feature_date, prediction_phase, extra
        FROM t_scheme_predictions
        ORDER BY scheme_id, run_id, id
        """
    )
    updates: list[PredictionSemanticsUpdate] = []
    errors: list[str] = []
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()

    for row in rows:
        prediction_id = int(row["id"])
        scheme_id = str(row["scheme_id"])
        run_id = int(row["run_id"] or 0)
        extra = _json_object(row["extra"])
        existing_feature_date = _clean_text(row["feature_date"])
        extra_feature_date = _clean_text(extra.get("feature_date"))
        feature_date = existing_feature_date or extra_feature_date
        if not feature_date:
            errors.append(f"prediction id={prediction_id} run_id={run_id} missing feature_date")
            continue
        if existing_feature_date and extra_feature_date and existing_feature_date != extra_feature_date:
            errors.append(
                f"prediction id={prediction_id} run_id={run_id} feature_date column={existing_feature_date} "
                f"does not match extra.feature_date={extra_feature_date}"
            )
            continue
        anchor_date = _clean_text(extra.get("anchor_date"))
        if anchor_date and anchor_date != feature_date:
            errors.append(
                f"prediction id={prediction_id} run_id={run_id} anchor_date={anchor_date} "
                f"does not match feature_date={feature_date}"
            )
            continue

        existing_phase = _clean_text(row["prediction_phase"])
        mapped_phase = PHASE_BY_SCHEME_RUN.get((scheme_id, run_id))
        if existing_phase and existing_phase not in VALID_PHASES:
            errors.append(f"prediction id={prediction_id} run_id={run_id} has invalid prediction_phase={existing_phase}")
            continue
        if existing_phase and mapped_phase and existing_phase != mapped_phase:
            errors.append(
                f"prediction id={prediction_id} run_id={run_id} phase column={existing_phase} "
                f"does not match explicit mapping={mapped_phase}"
            )
            continue
        phase = existing_phase or mapped_phase
        if not phase:
            errors.append(f"prediction id={prediction_id} run_id={run_id} has no explicit prediction_phase mapping")
            continue

        if existing_feature_date != feature_date or existing_phase != phase:
            updates.append(
                PredictionSemanticsUpdate(
                    prediction_id=prediction_id,
                    scheme_id=scheme_id,
                    run_id=run_id,
                    feature_date=feature_date,
                    prediction_phase=phase,
                )
            )
    return updates, errors


def apply_updates(engine: Engine, updates: list[PredictionSemanticsUpdate]) -> None:
    """应用回填；只更新预测语义字段。"""
    if not updates:
        return
    pred_sql = text(
        """
        UPDATE t_scheme_predictions
        SET feature_date = :feature_date,
            prediction_phase = :prediction_phase
        WHERE id = :prediction_id
        """
    )
    run_sql = text(
        """
        UPDATE t_scheme_runs
        SET prediction_phase = :prediction_phase
        WHERE run_id = :run_id
        """
    )
    rows = [
        {
            "prediction_id": update.prediction_id,
            "run_id": update.run_id,
            "feature_date": update.feature_date,
            "prediction_phase": update.prediction_phase,
        }
        for update in updates
    ]
    run_rows = {
        update.run_id: {"run_id": update.run_id, "prediction_phase": update.prediction_phase}
        for update in updates
    }
    with engine.begin() as conn:
        conn.execute(pred_sql, rows)
        conn.execute(run_sql, list(run_rows.values()))


def missing_semantics_counts(engine: Engine) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT scheme_id, COUNT(*) AS bad_rows
        FROM t_scheme_predictions
        WHERE feature_date IS NULL OR prediction_phase IS NULL
        GROUP BY scheme_id
        ORDER BY scheme_id
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sql).mappings().all()]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    text_value = str(value).strip()
    if not text_value:
        return {}
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill feature_date and prediction_phase for live predictions.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只预览 UPDATE；默认行为")
    mode.add_argument("--apply", action="store_true", help="真正执行 UPDATE")
    args = parser.parse_args()

    engine = create_engine_from_env()
    try:
        updates, errors = collect_updates(engine)
        print(f"updates={len(updates)}")
        for update in updates:
            print(
                f"will_update id={update.prediction_id} scheme_id={update.scheme_id} run_id={update.run_id} "
                f"feature_date={update.feature_date} prediction_phase={update.prediction_phase}"
            )
        if errors:
            for error in errors:
                print(f"ERROR {error}")
            raise SystemExit(1)
        if args.apply:
            apply_updates(engine, updates)
            missing = missing_semantics_counts(engine)
            if missing:
                for row in missing:
                    print(f"ERROR scheme_id={row['scheme_id']} bad_rows={row['bad_rows']}")
                raise SystemExit(1)
            print("applied=true")
        else:
            print("applied=false")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
