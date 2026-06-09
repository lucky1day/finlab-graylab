from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from shared.data_service import create_sqlalchemy_engine


API_BASE = "http://127.0.0.1:8100"


def main() -> None:
    checks = [
        check_canonical_csv,
        check_data_alignment,
        check_t5_reproduction,
        check_t1_reproduction,
        check_api_endpoints,
        check_frontend_entry,
    ]
    for check in checks:
        check()
        print(f"ok {check.__name__}")


def check_canonical_csv() -> None:
    canonical = PROJECT_ROOT / "benchmarks" / "model_muti_0529" / "daily_output.csv"
    assert canonical.exists(), f"canonical daily output missing: {canonical}"
    assert not canonical.is_symlink(), f"canonical daily output must be a real tracked file: {canonical}"


def check_data_alignment() -> None:
    engine = create_sqlalchemy_engine()
    try:
        row = engine.connect().execute(
            text(
                """
                SELECT status, row_count_csv, row_count_db, col_count_csv, col_count_db,
                       target_max_abs_diff, report
                FROM t_backtest_reproduction_checks
                WHERE benchmark_id = 'model_muti_0529'
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).mappings().one()
    finally:
        engine.dispose()
    assert row["row_count_csv"] == 3843
    assert row["row_count_db"] == 3843
    assert row["col_count_csv"] == 877
    assert row["col_count_db"] == 877
    target_diff = json.loads(row["target_max_abs_diff"])
    assert target_diff
    assert all(float(value) <= 1e-10 for value in target_diff.values())
    report = json.loads(row["report"])
    assert report["date_match"] is True
    assert report["columns"]["column_order_match"] is True
    assert report["generation"]["primary"] in {
        "upstream_data_service",
        "original_data_service_file",
        "shared_daily_data_service",
        "shared_data_service_daily",
    }
    assert report["generation"]["upstream_daily_targets"] == ["TB1YWI0C", "TB5YWI0C", "TB0YWI0C"]
    assert report["framework_db_comparison"]["date_match"] is True
    assert report["framework_db_comparison"]["column_order_match"] is True
    assert report["framework_db_comparison"]["overall_max_abs_diff"] == 0.0
    assert report["framework_db_comparison"]["missing_diff_count"] == 0
    effective = report["excluding_evaluation_target_week"]
    assert effective["csv"]["rows"] == 3839
    assert effective["numeric_diff"]["overall_max_abs_diff"] < 1e-6
    assert report["evaluation_exclusion"]["excluded_row_count"] == 4
    if row["status"] != "success":
        assert report["numeric_diff"]["above_threshold_sample"] or report["numeric_diff"]["missing_diff_count"] > 0


def check_t5_reproduction() -> None:
    runs = _runs_for("t5_daily")
    expected_sources = {"baseline_original_csv", "framework_original_csv", "framework_db_aligned"}
    assert expected_sources.issubset({run["data_source"] for run in runs})
    for run in runs:
        if run["data_source"] not in expected_sources:
            continue
        summary = run["summary"]
        assert summary["row_count"] == 1312
        assert summary["raw_row_count"] == 1328
        assert summary["excluded_row_count"] == 16
        if run["data_source"] != "baseline_original_csv":
            assert summary["comparison"]["mismatch_count"] == 0
        for item in summary["report_reproduction"].values():
            assert item["all_match"] is True
            assert all(item["sample_count_match"].values())


def check_t1_reproduction() -> None:
    runs = _runs_for("t1_daily")
    expected_sources = {"baseline_original_csv", "framework_original_csv", "framework_db_aligned"}
    assert expected_sources.issubset({run["data_source"] for run in runs})
    for run in runs:
        if run["data_source"] not in expected_sources:
            continue
        summary = run["summary"]
        assert summary["row_count"] == 999
        assert summary["raw_row_count"] == 1011
        assert summary["excluded_row_count"] == 12
        if run["data_source"] != "baseline_original_csv":
            assert summary["comparison"]["mismatch_count"] == 0


def check_api_endpoints() -> None:
    targets = _json("/api/targets")
    assert targets["target_labels"]["3Y"] == "3Y国债活跃"
    assert targets["target_labels"]["10Y"] == "10Y国债活跃"
    runs = _json("/api/backtests/runs")["runs"]
    assert len(runs) >= 6
    framework_t5 = next(run for run in runs if run["scheme_id"] == "t5_daily" and run["data_source"] == "framework_db_aligned")
    run_id = framework_t5["id"]
    assert _json(f"/api/backtests/runs/{run_id}")["id"] == run_id
    assert _json(f"/api/backtests/runs/{run_id}/metrics")["items"]
    assert _json(f"/api/backtests/runs/{run_id}/diffs")["comparison"]["mismatch_count"] == 0
    assert _json("/api/backtests/data-checks?limit=1")["checks"]
    factor_lab = _json("/api/backtests/factor-lab")
    assert factor_lab["data_source"] == "framework_db_aligned"
    assert factor_lab["target_labels"]["3Y"] == "3Y国债活跃"
    assert len(factor_lab["schemes"]) >= 6
    by_id = {item["id"]: item for item in factor_lab["schemes"]}
    assert "t1_daily:1Y:framework_db_aligned" not in by_id
    assert by_id["t5_daily:3Y:framework_db_aligned"]["summary"]["overall"] == 69.5
    assert by_id["t5_daily:3Y:framework_db_aligned"]["summary"]["up_precision"] == 61.4
    assert by_id["t5_daily:3Y:framework_db_aligned"]["summary"]["down_recall"] == 68.5
    assert by_id["t1_daily:10Y:framework_db_aligned"]["summary"]["overall"] == 60.7
    assert by_id["t1_daily:10Y:framework_db_aligned"]["summary"]["up_precision"] == 57.8
    assert by_id["t1_daily:10Y:framework_db_aligned"]["summary"]["down_recall"] == 43.1


def check_frontend_entry() -> None:
    html = urllib.request.urlopen(f"{API_BASE}/?verify=reproduction", timeout=5).read().decode("utf-8")
    assert "因子实验室" in html
    assert "factorTaskMatrixBody" in html
    assert "Y标的" in html
    assert "factorHistoryView" not in html


def _runs_for(scheme_id: str) -> list[dict]:
    engine = create_sqlalchemy_engine()
    try:
        rows = engine.connect().execute(
            text(
                """
                SELECT data_source, summary
                FROM t_backtest_runs
                WHERE benchmark_id = 'model_muti_0529' AND scheme_id = :scheme_id
                """
            ),
            {"scheme_id": scheme_id},
        ).mappings().all()
    finally:
        engine.dispose()
    return [{"data_source": row["data_source"], "summary": json.loads(row["summary"])} for row in rows]


def _json(path: str) -> dict:
    return json.loads(urllib.request.urlopen(f"{API_BASE}{path}", timeout=10).read().decode("utf-8"))


if __name__ == "__main__":
    main()
