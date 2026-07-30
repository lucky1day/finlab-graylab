from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.blackbox_v2.gates import _verify_passed_all
from scheduler.discovery import load_scheme_config
from scheduler.repository import (
    create_engine_from_env,
    replace_active_blackbox_version,
)


def replace_active_blackbox_version_controlled(
    engine: Engine,
    *,
    project_root: str | Path,
    scheme_id: str,
    previous_scheme_version: str,
    expected_new_scheme_version: str,
    expected_harness_run_id: str,
    approved_by: str,
    apply: bool = False,
) -> dict[str, Any]:
    """核对 all-stage 证据，并按 exact identity 切换 active Blackbox 版本。"""
    root = Path(project_root).resolve()
    cfg = load_scheme_config(root / "schemes" / scheme_id / "config.yaml")
    if cfg.scheme_version != expected_new_scheme_version:
        raise SystemExit(
            "new scheme_version mismatch: "
            f"config={cfg.scheme_version}, expected={expected_new_scheme_version}"
        )
    if cfg.status != "active" or cfg.version_status != "active":
        raise SystemExit(
            "replacement config must stay active+active: "
            f"got={cfg.status}+{cfg.version_status}"
        )

    passed = _verify_passed_all(engine, cfg)
    if passed.harness_run_id != expected_harness_run_id:
        raise SystemExit(
            "latest passed all-stage harness run mismatch: "
            f"latest={passed.harness_run_id}, expected={expected_harness_run_id}"
        )
    environment_fingerprint = str(
        passed.environment_fingerprint or ""
    ).strip()
    data_snapshot_id = str(passed.data_snapshot_id or "").strip()
    if not environment_fingerprint or not data_snapshot_id:
        raise SystemExit(
            "latest passed all-stage run is missing environment/data snapshot evidence"
        )
    before = _replacement_database_snapshot(engine, scheme_id)
    summary: dict[str, Any] = {
        "applied": bool(apply),
        "scheme_id": scheme_id,
        "previous_scheme_version": previous_scheme_version,
        "new_scheme_version": cfg.scheme_version,
        "harness_run_id": passed.harness_run_id,
        "generation_id": passed.generation_id,
        "data_snapshot_id": data_snapshot_id,
        "environment_fingerprint": environment_fingerprint,
        "approved_by": approved_by,
        "before": before,
        "after": None,
    }
    if not apply:
        return summary

    enriched_cfg = replace(
        cfg,
        environment_fingerprint=environment_fingerprint,
        data_snapshot_id=data_snapshot_id,
    )
    state = replace_active_blackbox_version(
        engine,
        enriched_cfg,
        previous_scheme_version=previous_scheme_version,
        expected_harness_run_id=expected_harness_run_id,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc),
    )
    summary["state"] = asdict(state)
    summary["after"] = _replacement_database_snapshot(engine, scheme_id)
    return summary


def _replacement_database_snapshot(
    engine: Engine,
    scheme_id: str,
) -> dict[str, list[dict[str, Any]]]:
    with engine.connect() as conn:
        versions = conn.execute(
            text(
                """
                SELECT scheme_version, runtime_type, algorithm_version, status,
                       code_hash, config_hash, manifest_hash, approved_by,
                       approved_at
                FROM t_scheme_versions
                WHERE scheme_id = :scheme_id
                ORDER BY created_at, id
                """
            ),
            {"scheme_id": scheme_id},
        ).mappings().all()
        registry = conn.execute(
            text(
                """
                SELECT scheme_id, base_scheme_id, runtime_type, task_type,
                       target_tenor, horizon, status
                FROM t_scheme_registry
                WHERE base_scheme_id = :scheme_id
                ORDER BY scheme_id
                """
            ),
            {"scheme_id": scheme_id},
        ).mappings().all()
    return {
        "versions": [dict(row) for row in versions],
        "registry": [dict(row) for row in registry],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replace one active Blackbox exact version and retire its predecessor."
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--previous-scheme-version", required=True)
    parser.add_argument("--expected-new-scheme-version", required=True)
    parser.add_argument("--expected-harness-run-id", required=True)
    parser.add_argument("--approved-by", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine_from_env()
    try:
        summary = replace_active_blackbox_version_controlled(
            engine,
            project_root=args.project_root,
            scheme_id=args.scheme_id,
            previous_scheme_version=args.previous_scheme_version,
            expected_new_scheme_version=args.expected_new_scheme_version,
            expected_harness_run_id=args.expected_harness_run_id,
            approved_by=args.approved_by,
            apply=args.apply,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
