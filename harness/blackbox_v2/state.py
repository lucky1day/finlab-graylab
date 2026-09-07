"""显式重建 Blackbox 派生状态，不创建 run 或 prediction 事实。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from scheduler.discovery import load_scheme_config
from scheduler.executor import DEFAULT_ALGO_ENV, run_blackbox_scheme_subprocess
from shared.input_artifacts import create_input_engine


def rebuild_blackbox_state(*, project_root: Path, scheme_id: str, predict_date: str,
                           expected_scheme_version: str, approved_by: str) -> dict:
    """在显式版本与操作者范围内从源输入重建；算法结果只用于合同验证。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_]{0,199}", scheme_id):
        raise ValueError("invalid state rebuild scheme_id")
    if date.fromisoformat(predict_date).isoformat() != predict_date:
        raise ValueError("state rebuild predict_date must be an ISO date")
    if not approved_by.strip() or len(approved_by) > 200:
        raise ValueError("state rebuild requires approved_by")
    cfg = load_scheme_config(project_root / "schemes" / scheme_id / "config.yaml")
    if (cfg.runtime_type != "blackbox_v2" or not cfg.incremental_state
            or cfg.scheme_version != expected_scheme_version):
        raise ValueError("state rebuild requires matching incremental Blackbox exact version")
    engine = create_input_engine()
    try:
        records = run_blackbox_scheme_subprocess(
            cfg, predict_date, engine=engine, algo_env=DEFAULT_ALGO_ENV,
            timeout_sec=1800, rebuild_state=True,
        )
    finally:
        engine.dispose()
    record = records[0]
    return {
        "operation": "rebuild-blackbox-state", "approved_by": approved_by,
        "scheme_id": scheme_id, "scheme_version": cfg.scheme_version,
        "predict_date": predict_date, "feature_date": record.feature_date,
        "prediction_written": False,
        "state": {key: value for key, value in record.extra.items() if key.startswith("state_")},
    }
