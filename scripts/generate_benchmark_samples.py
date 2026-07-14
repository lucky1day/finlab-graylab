"""生成 benchmark 样本文件供 CompareGate 校验。"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import text

# 使用项目自身的 DB 配置（会从 .env 加载）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler.repository import create_engine_from_env


SCHEMES_ROOT = Path(__file__).resolve().parents[1] / "schemes"
DEFAULT_DATA_SOURCE = "framework_db_aligned"


def _write_predictions(rows: Sequence[Any], path: Path) -> int:
    """写入预测样本 CSV。"""
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["predict_date", "tenor", "direction", "confidence"])
        for row in rows:
            writer.writerow([
                str(row[0]),
                str(row[1]),
                int(row[2]) if row[2] is not None else "",
                str(row[3]) if row[3] is not None else "",
            ])
    return len(rows)


def _write_summary(rows: Sequence[Any], path: Path) -> tuple[int, int]:
    """写入回测月度指标摘要 JSON，返回 (tenor_count, month_count)。"""
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        tenor = str(row[0])
        month = str(row[1])
        if tenor not in summary:
            summary[tenor] = {}
        summary[tenor][month] = {
            "sample_count": int(row[2]),
            "correct_count": int(row[3]),
            "accuracy": float(row[4]) if row[4] is not None else None,
            "up_precision": float(row[5]) if row[5] is not None else None,
            "up_recall": float(row[6]) if row[6] is not None else None,
            "down_precision": float(row[7]) if row[7] is not None else None,
            "down_recall": float(row[8]) if row[8] is not None else None,
        }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(summary), sum(len(v) for v in summary.values())


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate CompareGate benchmark samples from one backtest run.")
    parser.add_argument("--scheme-id", help="Scheme id to export, e.g. daily_5y_2_v28.")
    parser.add_argument("--benchmark-id", help="Benchmark id for canonical latest selection.")
    parser.add_argument("--data-source", default=DEFAULT_DATA_SOURCE, help="Backtest data source.")
    parser.add_argument("--run-id", type=int, help="Explicit t_backtest_runs.id/backtest_run_id to export.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum prediction sample rows to export.")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.run_id is None and not (args.scheme_id and args.benchmark_id):
        parser.error("provide either --run-id or both --scheme-id and --benchmark-id")
    return args


def _run_row_by_id(conn: Any, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, backtest_run_id, benchmark_id, scheme_id, data_source,
                   start_date, end_date, status, created_at, updated_at
            FROM t_backtest_runs
            WHERE id = :run_id OR backtest_run_id = :run_id
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    return dict(row) if row else None


def _canonical_latest_run(
    conn: Any,
    *,
    scheme_id: str,
    benchmark_id: str,
    data_source: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, backtest_run_id, benchmark_id, scheme_id, data_source,
                   start_date, end_date, status, created_at, updated_at
            FROM v_latest_backtest_run
            WHERE scheme_id = :scheme_id
              AND benchmark_id = :benchmark_id
              AND data_source = :data_source
            LIMIT 1
            """
        ),
        {
            "scheme_id": scheme_id,
            "benchmark_id": benchmark_id,
            "data_source": data_source,
        },
    ).mappings().first()
    return dict(row) if row else None


def _select_run(conn: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.run_id is not None:
        run = _run_row_by_id(conn, args.run_id)
        if run is None:
            raise SystemExit(f"backtest run not found: {args.run_id}")
        if args.scheme_id and run["scheme_id"] != args.scheme_id:
            raise SystemExit(
                f"--scheme-id {args.scheme_id!r} does not match run {args.run_id} scheme_id {run['scheme_id']!r}"
            )
        if args.benchmark_id and run["benchmark_id"] != args.benchmark_id:
            raise SystemExit(
                f"--benchmark-id {args.benchmark_id!r} does not match run {args.run_id} benchmark_id {run['benchmark_id']!r}"
            )
        if args.data_source and run["data_source"] != args.data_source:
            raise SystemExit(
                f"--data-source {args.data_source!r} does not match run {args.run_id} data_source {run['data_source']!r}"
            )
        if run["status"] != "success":
            raise SystemExit(f"backtest run {args.run_id} is not success: {run['status']}")
        return run

    run = _canonical_latest_run(
        conn,
        scheme_id=args.scheme_id,
        benchmark_id=args.benchmark_id,
        data_source=args.data_source,
    )
    if run is None:
        raise SystemExit(
            "canonical latest backtest run not found for "
            f"scheme_id={args.scheme_id}, benchmark_id={args.benchmark_id}, data_source={args.data_source}"
        )
    return run


def _fetch_predictions(conn: Any, run_id: int, limit: int) -> list[Any]:
    policy_filter = _non_policy_prediction_predicate(conn)
    return list(
        conn.execute(
            text(
                f"""
                SELECT predict_date, target_tenor AS tenor,
                       predicted_direction AS direction, confidence
                FROM t_backtest_predictions
                WHERE run_id = :run_id
                  AND {policy_filter}
                ORDER BY predict_date, target_tenor
                LIMIT :limit
                """
            ),
            {"run_id": run_id, "limit": limit},
        ).fetchall()
    )


def _fetch_metrics(conn: Any, run_id: int) -> list[Any]:
    policy_filter = _non_policy_prediction_predicate(conn)
    rows = [
        dict(row)
        for row in conn.execute(
            text(
                f"""
                SELECT target_tenor, predict_date, target_date, label, predicted_direction
                FROM t_backtest_predictions
                WHERE run_id = :run_id
                  AND {policy_filter}
                ORDER BY target_tenor, target_date, predict_date
                """
            ),
            {"run_id": run_id},
        ).mappings().all()
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        month = str(row.get("target_date") or row.get("predict_date") or "")[:7]
        tenor = str(row.get("target_tenor") or "")
        if month and tenor:
            grouped[(tenor, month)].append(row)
    return [
        _metric_row(tenor, month, items)
        for (tenor, month), items in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


def _non_policy_prediction_predicate(conn: Any) -> str:
    """返回排除平台政策生成行的数据库 JSON 谓词。"""
    dialect = str(conn.dialect.name)
    if dialect == "sqlite":
        return "json_type(extra, '$.signal_policy_applied') IS NOT 'true'"
    if dialect == "mysql":
        return (
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(extra, '$.signal_policy_applied')), 'false') "
            "<> 'true'"
        )
    raise RuntimeError(f"unsupported database dialect for benchmark export: {dialect}")


def _metric_row(tenor: str, month: str, rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    valid = [
        row
        for row in rows
        if _int_or_none(row.get("label")) is not None
        and _int_or_none(row.get("predicted_direction")) is not None
    ]
    metric_rows = [row for row in valid if _int_or_none(row.get("predicted_direction")) in (-1, 1)]
    correct = sum(
        1
        for row in metric_rows
        if _int_or_none(row.get("label")) == _int_or_none(row.get("predicted_direction"))
    )
    pred_up = sum(1 for row in metric_rows if _int_or_none(row.get("predicted_direction")) == 1)
    pred_down = sum(1 for row in metric_rows if _int_or_none(row.get("predicted_direction")) == -1)
    actual_up = sum(1 for row in metric_rows if _int_or_none(row.get("label")) == 1)
    actual_down = sum(1 for row in metric_rows if _int_or_none(row.get("label")) == -1)
    up_tp = sum(
        1
        for row in metric_rows
        if _int_or_none(row.get("predicted_direction")) == 1 and _int_or_none(row.get("label")) == 1
    )
    down_tp = sum(
        1
        for row in metric_rows
        if _int_or_none(row.get("predicted_direction")) == -1 and _int_or_none(row.get("label")) == -1
    )
    return (
        tenor,
        month,
        len(valid),
        correct,
        _ratio(correct, len(metric_rows)),
        _ratio(up_tp, pred_up),
        _ratio(up_tp, actual_up),
        _ratio(down_tp, pred_down),
        _ratio(down_tp, actual_down),
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def generate(
    *,
    run_id: int | None = None,
    scheme_id: str | None = None,
    benchmark_id: str | None = None,
    data_source: str = DEFAULT_DATA_SOURCE,
    limit: int = 200,
) -> dict[str, Any]:
    """从唯一 backtest run 提取 current/original benchmark 文件。"""
    engine = create_engine_from_env()
    try:
        args = argparse.Namespace(
            run_id=run_id,
            scheme_id=scheme_id,
            benchmark_id=benchmark_id,
            data_source=data_source,
            limit=limit,
        )
        if args.run_id is None and not (args.scheme_id and args.benchmark_id):
            raise SystemExit("provide either run_id or both scheme_id and benchmark_id")

        with engine.connect() as conn:
            run = _select_run(conn, args)
            selected_run_id = int(run["id"])
            pred_rows = _fetch_predictions(conn, selected_run_id, args.limit)
            metric_rows = _fetch_metrics(conn, selected_run_id)

        bench_dir = SCHEMES_ROOT / str(run["scheme_id"]) / "benchmarks"
        bench_dir.mkdir(parents=True, exist_ok=True)
        for prefix in ("original", "current"):
            _write_predictions(pred_rows, bench_dir / f"{prefix}_predictions_sample.csv")
            _write_summary(metric_rows, bench_dir / f"{prefix}_backtest_summary.json")

        return {
            "run": run,
            "prediction_count": len(pred_rows),
            "monthly_count": len(metric_rows),
            "bench_dir": str(bench_dir),
        }
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    result = generate(
        run_id=args.run_id,
        scheme_id=args.scheme_id,
        benchmark_id=args.benchmark_id,
        data_source=args.data_source,
        limit=args.limit,
    )
    run = result["run"]
    print(
        "selected "
        f"run_id={run['id']} "
        f"benchmark_id={run['benchmark_id']} "
        f"scheme_id={run['scheme_id']} "
        f"data_source={run['data_source']} "
        f"start_date={run['start_date']} "
        f"end_date={run['end_date']} "
        f"prediction_count={result['prediction_count']} "
        f"monthly_count={result['monthly_count']}"
    )
    print(f"wrote benchmark files to {result['bench_dir']}")


if __name__ == "__main__":
    main()
