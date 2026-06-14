from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import text

from scripts.postonboard_common import (
    PROJECT_ROOT,
    default_output_dir,
    exit_code,
    finish,
    make_payload,
    write_json,
)


OUTPUT_FILE = "frontend_db_verification.json"


def verify_frontend_db(
    scheme_id: str,
    run_id: int | None = None,
    api_base_url: str = "http://127.0.0.1:8100",
    output_dir: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> tuple[dict[str, Any], int]:
    """逐格比对前端 factor-lab API 与 t_backtest_predictions 明细动态聚合。"""
    try:
        from shared.data_service import create_sqlalchemy_engine

        engine = create_sqlalchemy_engine()
    except Exception as exc:
        payload = make_payload("blocked", {"scheme_id": scheme_id}, [f"database engine unavailable: {exc}"])
        return payload, exit_code(payload["status"])

    try:
        resolved_run_id = run_id or _latest_run_id(engine, scheme_id)
        if resolved_run_id is None:
            payload = make_payload("fail", {"scheme_id": scheme_id}, ["no framework_db_aligned backtest run found"])
            return payload, exit_code(payload["status"])
        api_payload = _fetch_factor_lab(api_base_url)
        db_rows = _fetch_prediction_details(engine, resolved_run_id)
        evidence = compare_frontend_db_cells(api_payload, db_rows, scheme_id=scheme_id, run_id=resolved_run_id)
        resolved_output_dir = Path(output_dir) if output_dir else default_output_dir(scheme_id, project_root)
        output_path = write_json(resolved_output_dir / OUTPUT_FILE, evidence)
        evidence["output_path"] = str(output_path)
        status = "pass" if evidence["mismatch_count"] == 0 else "fail"
        errors = [] if status == "pass" else [f"{evidence['mismatch_count']} frontend/DB cells mismatched"]
        payload = make_payload(status, evidence, errors)
        return payload, exit_code(payload["status"])
    except urllib.error.URLError as exc:
        evidence = {"scheme_id": scheme_id, "run_id": run_id, "api_base_url": api_base_url}
        payload = make_payload("blocked", evidence, [f"API unavailable: {exc}"])
        return payload, exit_code(payload["status"])
    finally:
        engine.dispose()


def compare_frontend_db_cells(
    api_payload: dict[str, Any],
    db_rows: list[dict[str, Any]],
    scheme_id: str,
    run_id: int,
) -> dict[str, Any]:
    frontend = _frontend_cells(api_payload, scheme_id, run_id)
    database = _db_cells(db_rows)
    keys = sorted(set(frontend) | set(database), key=lambda item: (_tenor_sort_key(item[0]), item[1]))
    mismatches: list[dict[str, Any]] = []
    for key in keys:
        left = frontend.get(key)
        right = database.get(key)
        if left is None or right is None:
            mismatches.append(
                {
                    "target_tenor": key[0],
                    "month": key[1],
                    "kind": "missing_cell",
                    "frontend": left,
                    "database": right,
                }
            )
            continue
        cell_mismatches = _compare_cell(left, right)
        if cell_mismatches:
            mismatches.append(
                {
                    "target_tenor": key[0],
                    "month": key[1],
                    "kind": "value_mismatch",
                    "fields": cell_mismatches,
                    "frontend": left,
                    "database": right,
                }
            )

    tenors = sorted({key[0] for key in keys}, key=_tenor_sort_key)
    frontend_only = sorted(set(frontend) - set(database), key=lambda item: (_tenor_sort_key(item[0]), item[1]))
    database_only = sorted(set(database) - set(frontend), key=lambda item: (_tenor_sort_key(item[0]), item[1]))
    return {
        "run_id": run_id,
        "scheme_id": scheme_id,
        "tenors_checked": tenors,
        "total_cells_checked": len(keys),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "frontend_only_cells": len(frontend_only),
        "database_only_cells": len(database_only),
    }


def _latest_run_id(engine: Any, scheme_id: str) -> int | None:
    sql = text(
        """
        SELECT id
        FROM t_backtest_runs
        WHERE scheme_id = :scheme_id
          AND data_source = 'framework_db_aligned'
          AND status = 'success'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        value = conn.execute(sql, {"scheme_id": scheme_id}).scalar()
    return int(value) if value is not None else None


def _fetch_factor_lab(api_base_url: str) -> dict[str, Any]:
    base = api_base_url.rstrip("/")
    query = urllib.parse.urlencode({"data_source": "framework_db_aligned"})
    url = f"{base}/api/backtests/factor-lab?{query}"
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read().decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("factor-lab API did not return a JSON object")
    return payload


def _fetch_prediction_details(engine: Any, run_id: int) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT target_tenor, predict_date, target_date, label, predicted_direction
        FROM t_backtest_predictions
        WHERE run_id = :run_id
        ORDER BY target_tenor, target_date, predict_date
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"run_id": run_id}).mappings().all()
    return [dict(row) for row in rows]


def _frontend_cells(api_payload: dict[str, Any], scheme_id: str, run_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    schemes = api_payload.get("schemes") if isinstance(api_payload.get("schemes"), list) else []
    for scheme in schemes:
        if not isinstance(scheme, dict):
            continue
        api_scheme_id = str(scheme.get("scheme_id") or "").strip()
        api_base_scheme_id = str(scheme.get("base_scheme_id") or "").strip()
        if scheme_id not in {api_scheme_id, api_base_scheme_id} or int(scheme.get("run_id") or -1) != int(run_id):
            continue
        tenor = str(scheme.get("tenor") or scheme.get("target_tenor") or "").strip()
        for item in scheme.get("monthly_metrics") or []:
            if not isinstance(item, dict):
                continue
            month = str(item.get("month") or "").strip()
            if not tenor or not month:
                continue
            cells[(tenor, month)] = {
                "samples": _int_or_none(item.get("samples", item.get("total"))),
                "correct": _int_or_none(item.get("correct")),
                "accuracy": _round_percent(item.get("accuracy")),
            }
    return cells


def _db_cells(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        tenor = str(row.get("target_tenor") or "").strip()
        month = str(row.get("target_date") or row.get("predict_date") or "").strip()[:7]
        if not tenor or not month:
            continue
        grouped.setdefault((tenor, month), []).append(row)
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in grouped.items():
        valid = [
            row
            for row in items
            if _int_or_none(row.get("label")) is not None
            and _int_or_none(row.get("predicted_direction")) is not None
        ]
        metric_rows = [row for row in valid if _int_or_none(row.get("predicted_direction")) in (-1, 1)]
        correct = sum(
            1
            for row in metric_rows
            if _int_or_none(row.get("label")) == _int_or_none(row.get("predicted_direction"))
        )
        cells[key] = {
            "samples": len(valid),
            "correct": correct,
            "accuracy": _percent(correct, len(metric_rows)),
        }
    return cells


def _compare_cell(frontend: dict[str, Any], database: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key in ("samples", "correct", "accuracy"):
        if frontend.get(key) != database.get(key):
            mismatches.append({"field": key, "frontend": frontend.get(key), "database": database.get(key)})
    return mismatches


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 1)


def _round_percent(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _tenor_sort_key(value: str) -> tuple[int, str]:
    text_value = str(value)
    digits = "".join(char for char in text_value if char.isdigit())
    return (int(digits) if digits else 999, text_value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify factor-lab frontend monthly cells against DB metrics.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    payload, _ = verify_frontend_db(
        args.scheme_id,
        run_id=args.run_id,
        api_base_url=args.api_base_url,
        output_dir=args.output_dir,
    )
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
