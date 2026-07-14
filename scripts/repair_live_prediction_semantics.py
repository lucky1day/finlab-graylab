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
T1_GRAY_RUNS = set(range(22, 30))
AUDITED_DELETE_ONLY_WEEKLY_RUNS = {
    ("weekly_5y_direct_0529", 31),
    ("weekly_5y_direct_0529", 57),
    ("weekly_7y_cross_d_overlay_0529", 56),
}


@dataclass(frozen=True)
class LivePredictionRepair:
    prediction_id: int
    run_id: int
    scheme_id: str
    predict_date: str
    feature_date: str
    prediction_phase: str
    extra: dict[str, Any]


def collect_repairs(engine: Engine) -> tuple[list[LivePredictionRepair], list[str]]:
    """收集审计确认坏行的定点修复；不修改数据库。"""
    rows = _bad_live_rows(engine)
    repairs: list[LivePredictionRepair] = []
    errors: list[str] = []
    for row in rows:
        scheme_id = str(row["scheme_id"])
        run_id = int(row["run_id"] or 0)
        prediction_id = int(row["id"])
        predict_date = _clean_date(row["predict_date"])
        extra = _json_object(row["extra"])

        if scheme_id == "t1_daily" and run_id in T1_GRAY_RUNS:
            feature_date = _previous_trading_day(engine, predict_date)
            phase = "gray_live"
            repaired_extra = dict(extra)
            repaired_extra["feature_date"] = feature_date
            if "anchor_date" in repaired_extra:
                repaired_extra["anchor_date"] = feature_date
            repaired_extra["prediction_phase"] = phase
            repairs.append(
                LivePredictionRepair(prediction_id, run_id, scheme_id, predict_date, feature_date, phase, repaired_extra)
            )
            continue

        if (scheme_id, run_id) in AUDITED_DELETE_ONLY_WEEKLY_RUNS:
            errors.append(
                f"audited bad weekly live row id={prediction_id} scheme_id={scheme_id} run_id={run_id}; "
                "use scripts/delete_bad_live_predictions.py instead of repairing it"
            )
            continue

        errors.append(
            f"unhandled bad live row id={prediction_id} scheme_id={scheme_id} run_id={run_id} "
            f"predict_date={predict_date}"
        )
    return repairs, errors


def apply_repairs(engine: Engine, repairs: list[LivePredictionRepair]) -> None:
    """应用定点修复；不改 target_date、方向、置信度。"""
    if not repairs:
        return
    pred_sql = text(
        """
        UPDATE t_scheme_predictions
        SET feature_date = :feature_date,
            prediction_phase = :prediction_phase,
            extra = :extra
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
    pred_rows = [
        {
            "prediction_id": repair.prediction_id,
            "feature_date": repair.feature_date,
            "prediction_phase": repair.prediction_phase,
            "extra": json.dumps(repair.extra, ensure_ascii=False, sort_keys=True),
        }
        for repair in repairs
    ]
    run_rows = {
        repair.run_id: {"run_id": repair.run_id, "prediction_phase": repair.prediction_phase}
        for repair in repairs
    }
    with engine.begin() as conn:
        conn.execute(pred_sql, pred_rows)
        conn.execute(run_sql, list(run_rows.values()))


def bad_live_semantics_counts(engine: Engine) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT scheme_id, COUNT(*) AS bad_rows
        FROM t_scheme_predictions
        WHERE feature_date IS NULL
           OR prediction_phase IS NULL
           OR feature_date >= predict_date
        GROUP BY scheme_id
        ORDER BY scheme_id
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sql).mappings().all()]


def _bad_live_rows(engine: Engine) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT id, run_id, scheme_id, predict_date, feature_date, target_date, prediction_phase, extra
        FROM t_scheme_predictions
        WHERE feature_date IS NULL
           OR prediction_phase IS NULL
           OR feature_date >= predict_date
        ORDER BY scheme_id, run_id, id
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sql).mappings().all()]


def _previous_trading_day(engine: Engine, predict_date: str) -> str:
    sql = text(
        """
        SELECT MAX(rdate)
        FROM t_trade_calendar
        WHERE trade_flag = '1' AND rdate < :predict_date
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"predict_date": predict_date}).scalar()
    feature_date = _clean_date(row)
    if not feature_date:
        raise ValueError(f"no previous trading day before predict_date={predict_date}")
    return feature_date


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


def _clean_date(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()[:10]


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair audited live prediction date semantics.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只预览修复；默认行为")
    mode.add_argument("--apply", action="store_true", help="真正执行 UPDATE")
    args = parser.parse_args()

    engine = create_engine_from_env()
    try:
        repairs, errors = collect_repairs(engine)
        print(f"repairs={len(repairs)}")
        for repair in repairs:
            print(
                f"will_repair id={repair.prediction_id} scheme_id={repair.scheme_id} run_id={repair.run_id} "
                f"predict_date={repair.predict_date} feature_date={repair.feature_date} "
                f"prediction_phase={repair.prediction_phase}"
            )
        if errors:
            for error in errors:
                print(f"ERROR {error}")
            raise SystemExit(1)
        if args.apply:
            apply_repairs(engine, repairs)
            bad_counts = bad_live_semantics_counts(engine)
            if bad_counts:
                for row in bad_counts:
                    print(f"ERROR scheme_id={row['scheme_id']} bad_rows={row['bad_rows']}")
                raise SystemExit(1)
            print("applied=true")
        else:
            print("applied=false")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
