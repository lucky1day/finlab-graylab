from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text


_SCHEMA = (
    "CREATE TABLE t_harness_runs ("
    "harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
    "stage TEXT, status TEXT, finished_at TEXT)",
    "CREATE TABLE t_harness_gate_results ("
    "harness_run_id TEXT, gate_name TEXT, status TEXT, summary_json TEXT)",
    "CREATE TABLE t_scheme_versions ("
    "scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, status TEXT, "
    "code_hash TEXT, config_hash TEXT, manifest_hash TEXT, "
    "approved_by TEXT, approved_at TEXT)",
    "CREATE TABLE t_scheme_registry ("
    "scheme_id TEXT, base_scheme_id TEXT, name TEXT, description TEXT, "
    "horizon INTEGER, task_type TEXT, runtime_type TEXT, tenors TEXT, "
    "frequency TEXT, target_tenor TEXT, schedule_cron TEXT, "
    "schedule_timezone TEXT, status TEXT, deployed_at TEXT)",
    "CREATE TABLE t_backtest_runs ("
    "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
    "data_source TEXT, status TEXT, updated_at TEXT)",
    "CREATE TABLE t_backtest_predictions ("
    "id INTEGER PRIMARY KEY, run_id INTEGER)",
)


def create_harness_control_plane_engine(database_path: Path | None = None):
    """创建 Native Harness 测试共用的最小 SQLite 控制面。"""
    url = (
        "sqlite:///:memory:"
        if database_path is None
        else f"sqlite:///{database_path}"
    )
    engine = create_engine(url)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
    return engine
