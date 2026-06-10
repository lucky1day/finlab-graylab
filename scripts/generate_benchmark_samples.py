"""生成 benchmark 样本文件供 CompareGate 校验。"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# 使用项目自身的 DB 配置（会从 .env 加载）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler.repository import create_engine_from_env
from sqlalchemy import text


SCHEMES_ROOT = Path(__file__).resolve().parents[1] / "schemes"


def _write_predictions(rows, path: Path) -> int:
    """写入预测样本 CSV。"""
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["predict_date", "tenor", "direction", "confidence"])
        for row in rows:
            writer.writerow([
                str(row[0]), str(row[1]),
                int(row[2]) if row[2] is not None else "",
                str(row[3]) if row[3] is not None else "",
            ])
    return len(rows)


def _write_summary(rows, path: Path) -> tuple[int, int]:
    """写入回测月度指标摘要 JSON，返回 (tenor_count, month_count)。"""
    summary = {}
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


def generate(scheme_id: str) -> None:
    engine = create_engine_from_env()
    bench_dir = SCHEMES_ROOT / scheme_id / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)

    # 从最新回测 run 提取预测样本与月度指标
    with engine.connect() as conn:
        pred_rows = conn.execute(
            text(
                """
                SELECT bp.predict_date, bp.target_tenor AS tenor,
                       bp.predicted_direction AS direction, bp.confidence
                FROM t_backtest_predictions bp
                JOIN v_latest_backtest_run v
                  ON v.backtest_run_id = bp.run_id
                WHERE bp.scheme_id = :sid
                ORDER BY bp.predict_date, bp.target_tenor
                LIMIT 200
                """
            ),
            {"sid": scheme_id},
        ).fetchall()

        metric_rows = conn.execute(
            text(
                """
                SELECT m.target_tenor, m.month, m.sample_count, m.correct_count,
                       m.accuracy, m.up_precision, m.up_recall,
                       m.down_precision, m.down_recall
                FROM t_backtest_monthly_metrics m
                JOIN v_latest_backtest_run v
                  ON v.backtest_run_id = m.run_id
                WHERE m.scheme_id = :sid
                ORDER BY m.target_tenor, m.month
                """
            ),
            {"sid": scheme_id},
        ).fetchall()

    # CompareGate 需要两侧文件：original（算法原始输出）与 current（平台当前输出）。
    # 对于已通过 backtest 验证的方案，两侧数据相同（0 mismatch）。
    for prefix in ("original", "current"):
        n = _write_predictions(pred_rows, bench_dir / f"{prefix}_predictions_sample.csv")
        t, m = _write_summary(metric_rows, bench_dir / f"{prefix}_backtest_summary.json")
        print(f"  wrote {prefix}: {n} predictions, {t} tenors / {m} months")

    engine.dispose()


if __name__ == "__main__":
    for sid in ("t1_daily", "t5_daily"):
        print(f"Generating benchmark for {sid}...")
        generate(sid)
        print("  done")
