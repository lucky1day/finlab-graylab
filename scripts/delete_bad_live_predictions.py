from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env


AUDITED_BAD_RUNS = (
    ("weekly_5y_direct_0529", 57),
    ("weekly_7y_cross_d_overlay_0529", 56),
)


def delete_bad_live_predictions(engine: Engine, *, apply: bool = False) -> dict[str, Any]:
    """删除已审计确认的坏 live prediction 明细；保留 run/log 审计。"""
    rows = _matching_prediction_rows(engine)
    if apply and rows:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM t_scheme_predictions WHERE id = :id"),
                [{"id": int(row["id"])} for row in rows],
            )
    return {
        "applied": bool(apply),
        "deleted_prediction_rows": len(rows) if apply else 0,
        "matched_prediction_rows": len(rows),
        "rows": rows,
    }


def _matching_prediction_rows(engine: Engine) -> list[dict[str, Any]]:
    filters = " OR ".join(
        f"(scheme_id = :scheme_id_{index} AND run_id = :run_id_{index})"
        for index, _ in enumerate(AUDITED_BAD_RUNS)
    )
    params: dict[str, Any] = {}
    for index, (scheme_id, run_id) in enumerate(AUDITED_BAD_RUNS):
        params[f"scheme_id_{index}"] = scheme_id
        params[f"run_id_{index}"] = run_id
    sql = text(
        f"""
        SELECT id, run_id, scheme_id, predict_date, feature_date, target_date, prediction_phase
        FROM t_scheme_predictions
        WHERE {filters}
        ORDER BY scheme_id, run_id, id
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).mappings().all()]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Delete audited bad live prediction rows only.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印命中行；默认行为")
    mode.add_argument("--apply", action="store_true", help="真正执行 DELETE")
    args = parser.parse_args(argv)

    engine = create_engine_from_env()
    try:
        summary = delete_bad_live_predictions(engine, apply=args.apply)
        for row in summary["rows"]:
            print(
                f"matched id={row['id']} scheme_id={row['scheme_id']} run_id={row['run_id']} "
                f"predict_date={row['predict_date']} feature_date={row['feature_date']} "
                f"target_date={row['target_date']} prediction_phase={row['prediction_phase']}"
            )
        print(f"matched_prediction_rows={summary['matched_prediction_rows']}")
        print(f"deleted_prediction_rows={summary['deleted_prediction_rows']}")
        print(f"applied={str(summary['applied']).lower()}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
