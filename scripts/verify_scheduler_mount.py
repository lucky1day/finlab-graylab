from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import text

from scripts.postonboard_common import (
    PROJECT_ROOT,
    default_output_dir,
    exit_code,
    finish,
    load_scheme_config,
    make_payload,
    write_json,
)


OUTPUT_FILE = "scheduler_mount_verification.json"
SCHEDULER_LABEL = "com.bond-factor-lab.scheduler"
LOG_PATHS = (
    Path("/Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.scheduler.log"),
    Path("/Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.scheduler.err"),
    Path("/Users/macstudio0/Library/Logs/com.bond-factor-lab.scheduler.log"),
    Path("/Users/macstudio0/Library/Logs/com.bond-factor-lab.scheduler.err"),
    Path("/tmp/bond-factor-lab-scheduler.log"),
    Path("/tmp/bond-factor-lab-scheduler.err"),
)


def verify_scheduler_mount(
    scheme_id: str,
    output_dir: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> tuple[dict[str, Any], int]:
    """检查 scheduler 进程、registry 与方案 cron 是否一致。"""
    try:
        config = load_scheme_config(scheme_id, project_root)
    except Exception as exc:
        payload = make_payload("fail", {"scheme_id": scheme_id}, [str(exc)])
        return payload, exit_code(payload["status"])

    config_status = str(config.get("status") or "").strip()
    schedule = config.get("schedule") if isinstance(config.get("schedule"), dict) else {}
    config_cron = str(schedule.get("cron") or "").strip()
    scheduler_running, launchctl_detail = _scheduler_running()
    registry_status = None
    registry_cron = None
    try:
        registry_status, registry_cron = _registry_row(scheme_id)
    except Exception as exc:
        launchctl_detail = f"{launchctl_detail}\nregistry unavailable: {exc}".strip()

    log_text = _recent_scheduler_log()
    evidence, errors, status = evaluate_scheduler_mount(
        scheme_id=scheme_id,
        config_status=config_status,
        config_cron=config_cron,
        registry_status=registry_status,
        registry_cron=registry_cron,
        scheduler_running=scheduler_running,
        log_text=log_text,
    )
    evidence["launchctl_detail"] = launchctl_detail
    resolved_output_dir = Path(output_dir) if output_dir else default_output_dir(scheme_id, project_root)
    output_path = write_json(resolved_output_dir / OUTPUT_FILE, evidence)
    evidence["output_path"] = str(output_path)
    payload = make_payload(status, evidence, errors)
    return payload, exit_code(payload["status"])


def evaluate_scheduler_mount(
    scheme_id: str,
    config_status: str,
    config_cron: str,
    registry_status: str | None,
    registry_cron: str | None,
    scheduler_running: bool,
    log_text: str,
) -> tuple[dict[str, Any], list[str], str]:
    cron_in_log = f"Scheduled scheme {scheme_id} at {config_cron}" in log_text
    registry_matches = registry_status == config_status and registry_cron == config_cron
    cron_registered = bool(config_status == "active" and config_cron and (registry_matches or cron_in_log))
    evidence = {
        "scheme_id": scheme_id,
        "config_status": config_status,
        "config_cron": config_cron,
        "registry_status": registry_status,
        "registry_cron": registry_cron,
        "scheduler_running": scheduler_running,
        "cron_registered": cron_registered,
        "cron_in_log": cron_in_log,
    }
    errors: list[str] = []
    if not config_status:
        errors.append("config status is empty")
    if config_status != "active":
        errors.append(f"config status is not active: {config_status}")
    if not config_cron:
        errors.append("config schedule.cron is empty")
    if registry_status is None or registry_cron is None:
        errors.append("scheme registry row is missing or incomplete")
    elif not registry_matches:
        errors.append(
            f"registry mismatch: status={registry_status}, cron={registry_cron}; "
            f"config status={config_status}, cron={config_cron}"
        )
    if not cron_registered:
        errors.append("cron registration not confirmed by registry or scheduler log")

    if not scheduler_running:
        return evidence, errors or ["scheduler is not running"], "blocked"
    status = "fail" if errors else "pass"
    return evidence, errors, status


def _scheduler_running() -> tuple[bool, str]:
    domain = f"gui/{os.getuid()}/{SCHEDULER_LABEL}"
    completed = subprocess.run(["launchctl", "print", domain], capture_output=True, text=True, check=False)
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return False, output
    return "state = running" in output or "state = waiting" in output, output[:4000]


def _registry_row(scheme_id: str) -> tuple[str | None, str | None]:
    from shared.data_service import create_sqlalchemy_engine

    engine = create_sqlalchemy_engine()
    try:
        sql = text(
            """
            SELECT status, schedule_cron
            FROM t_scheme_registry
            WHERE scheme_id = :scheme_id
            LIMIT 1
            """
        )
        with engine.connect() as conn:
            row = conn.execute(sql, {"scheme_id": scheme_id}).mappings().first()
        if row is None:
            return None, None
        return str(row["status"]) if row["status"] is not None else None, str(row["schedule_cron"]) if row["schedule_cron"] is not None else None
    finally:
        engine.dispose()


def _recent_scheduler_log(max_chars: int = 20000) -> str:
    chunks: list[str] = []
    for path in LOG_PATHS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks.append(text[-max_chars:])
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a scheme is mounted in the launchd scheduler.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    payload, _ = verify_scheduler_mount(args.scheme_id, output_dir=args.output_dir)
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
