from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.blackbox_v2.gates import _verify_passed_all
from scheduler.discovery import load_scheme_config
from scheduler.executor import (
    BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
    _normalize_live_records,
    run_configured_scheme,
)
from scheduler.repository import (
    create_engine_from_env,
    replace_approved_blackbox_gray_prediction,
)
from shared.input_artifacts import read_blackbox_current_state


def replace_blackbox_gray_history_controlled(
    engine: Engine,
    *,
    project_root: str | Path,
    scheme_id: str,
    previous_scheme_version: str,
    expected_new_scheme_version: str,
    expected_harness_run_id: str,
    expected_count: int,
    algo_env: str,
    timeout_sec: int,
    apply: bool = False,
) -> dict[str, Any]:
    """预计算全部灰度信号，再逐目标原子替换退役版本数据。"""
    root = Path(project_root).resolve()
    cfg = load_scheme_config(root / "schemes" / scheme_id / "config.yaml")
    if cfg.scheme_version != expected_new_scheme_version:
        raise SystemExit(
            "new scheme_version mismatch: "
            f"config={cfg.scheme_version}, expected={expected_new_scheme_version}"
        )
    if cfg.status != "active" or cfg.version_status != "active":
        raise SystemExit("gray replacement config must stay active+active")
    passed = _verify_passed_all(engine, cfg)
    if passed.harness_run_id != expected_harness_run_id:
        raise SystemExit(
            "latest passed all-stage harness run mismatch: "
            f"latest={passed.harness_run_id}, expected={expected_harness_run_id}"
        )
    state = read_blackbox_current_state()
    generation_id = str(state.get("generation_id") or "").strip()
    refresh_date = str(state.get("refresh_date") or "").strip()
    if not generation_id or not refresh_date:
        raise SystemExit("DataBridge current state is missing generation/refresh_date")

    old_rows = _load_old_gray_rows(
        engine,
        scheme_id=scheme_id,
        previous_scheme_version=previous_scheme_version,
    )
    if len(old_rows) != int(expected_count):
        raise SystemExit(
            "old gray row count mismatch: "
            f"actual={len(old_rows)}, expected={expected_count}"
        )

    computed: list[dict[str, Any]] = []
    for old in old_rows:
        started = time.monotonic()
        records = run_configured_scheme(
            cfg,
            str(old["predict_date"]),
            engine=engine,
            algo_env=algo_env,
            timeout_sec=int(timeout_sec),
            blackbox_snapshot_mode=BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF,
            expected_generation_id=generation_id,
            expected_refresh_date=refresh_date,
        )
        normalized = _normalize_live_records(
            records,
            prediction_phase="gray_live",
        )
        if len(normalized) != 1:
            raise RuntimeError(
                "gray replacement requires exactly one record per predict_date"
            )
        record = normalized[0]
        expected_identity = (
            scheme_id,
            str(old["predict_date"]),
            str(old["feature_date"]),
            str(old["target_date"]),
        )
        actual_identity = (
            record.scheme_id,
            record.predict_date,
            record.feature_date,
            record.target_date,
        )
        if actual_identity != expected_identity:
            raise RuntimeError(
                "gray replacement result date identity mismatch: "
                f"expected={expected_identity}, actual={actual_identity}"
            )
        computed.append(
            {
                "old": old,
                "record": record,
                "duration_sec": time.monotonic() - started,
            }
        )

    direction_diffs = [
        {
            "target_date": item["old"]["target_date"],
            "old_direction": int(item["old"]["predicted_direction"]),
            "new_direction": int(item["record"].predicted_direction),
        }
        for item in computed
        if int(item["old"]["predicted_direction"])
        != int(item["record"].predicted_direction)
    ]
    summary: dict[str, Any] = {
        "applied": bool(apply),
        "scheme_id": scheme_id,
        "previous_scheme_version": previous_scheme_version,
        "new_scheme_version": cfg.scheme_version,
        "harness_run_id": passed.harness_run_id,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "computed_count": len(computed),
        "direction_diff_count": len(direction_diffs),
        "direction_diffs": direction_diffs,
        "targets": [
            {
                "previous_run_id": int(item["old"]["run_id"]),
                "predict_date": item["record"].predict_date,
                "feature_date": item["record"].feature_date,
                "target_date": item["record"].target_date,
                "old_direction": int(item["old"]["predicted_direction"]),
                "new_direction": int(item["record"].predicted_direction),
                "new_run_id": None,
            }
            for item in computed
        ],
    }
    if not apply:
        return summary

    for index, item in enumerate(computed):
        new_run_id = replace_approved_blackbox_gray_prediction(
            engine,
            cfg,
            previous_scheme_version=previous_scheme_version,
            previous_run_id=int(item["old"]["run_id"]),
            record=item["record"],
            harness_run_id=expected_harness_run_id,
            duration_sec=float(item["duration_sec"]),
        )
        summary["targets"][index]["new_run_id"] = new_run_id
    summary["remaining_old_gray_rows"] = _count_old_gray_rows(
        engine,
        scheme_id=scheme_id,
        previous_scheme_version=previous_scheme_version,
    )
    if summary["remaining_old_gray_rows"] != 0:
        raise RuntimeError(
            "gray replacement left old-version gray business rows"
        )
    return summary


def _load_old_gray_rows(
    engine: Engine,
    *,
    scheme_id: str,
    previous_scheme_version: str,
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT p.id AS prediction_id, p.run_id, p.predict_date,
                       p.feature_date, p.target_date, p.target_tenor,
                       p.horizon, p.predicted_direction
                FROM t_scheme_predictions AS p
                JOIN t_scheme_runs AS r ON r.run_id = p.run_id
                WHERE p.scheme_id = :scheme_id
                  AND p.scheme_version = :scheme_version
                  AND p.prediction_phase = 'gray_live'
                  AND r.scheme_id = p.scheme_id
                  AND r.scheme_version = p.scheme_version
                  AND r.prediction_phase = 'gray_live'
                  AND r.status = 'success'
                ORDER BY p.target_date, p.id
                """
            ),
            {
                "scheme_id": scheme_id,
                "scheme_version": previous_scheme_version,
            },
        ).mappings().all()
    return [
        {
            **dict(row),
            "predict_date": str(row["predict_date"])[:10],
            "feature_date": str(row["feature_date"])[:10],
            "target_date": str(row["target_date"])[:10],
        }
        for row in rows
    ]


def _count_old_gray_rows(
    engine: Engine,
    *,
    scheme_id: str,
    previous_scheme_version: str,
) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM t_scheme_predictions
                    WHERE scheme_id = :scheme_id
                      AND scheme_version = :scheme_version
                      AND prediction_phase = 'gray_live'
                    """
                ),
                {
                    "scheme_id": scheme_id,
                    "scheme_version": previous_scheme_version,
                },
            ).scalar_one()
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Atomically replace retired Blackbox gray-live history."
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--previous-scheme-version", required=True)
    parser.add_argument("--expected-new-scheme-version", required=True)
    parser.add_argument("--expected-harness-run-id", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--algo-env", default="forecast_env_blackbox_v1")
    parser.add_argument("--timeout-sec", default=1800, type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine_from_env()
    try:
        summary = replace_blackbox_gray_history_controlled(
            engine,
            project_root=args.project_root,
            scheme_id=args.scheme_id,
            previous_scheme_version=args.previous_scheme_version,
            expected_new_scheme_version=args.expected_new_scheme_version,
            expected_harness_run_id=args.expected_harness_run_id,
            expected_count=args.expected_count,
            algo_env=args.algo_env,
            timeout_sec=args.timeout_sec,
            apply=args.apply,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
