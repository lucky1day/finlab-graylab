"""日频控制面切换与隔离回放共用的只读静默探针。"""

from __future__ import annotations

import os
import plistlib
import pwd
import stat
import subprocess
from datetime import date
from pathlib import Path
from typing import Mapping

from shared.daily_coordinator_mode import (
    DAILY_COORDINATOR_MODE_ENV,
)


LAUNCHAGENT_LABELS = (
    "com.bond-factor-lab.backend",
    "com.bond-factor-lab.scheduler",
    "com.bond-factor-lab.v2-preflight",
)
QUIESCENCE_FIELDS = frozenset(
    {
        "active_occurrence_count",
        "nonterminal_item_count",
        "running_ledger_run_count",
        "cleanup_pending_count",
        "running_legacy_scheduled_live_run_count",
        "registered_process_alive_count",
        "project_process_count",
    }
)
_MAX_PLIST_BYTES = 1024 * 1024


def probe_launchagent_service_states(
    service_uid: int,
) -> dict[str, bool]:
    """返回三个 BFL LaunchAgent 是否仍被 launchd 加载。"""
    states: dict[str, bool] = {}
    for label in LAUNCHAGENT_LABELS:
        completed = subprocess.run(
            [
                "launchctl",
                "print",
                f"gui/{service_uid}/{label}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        states[label] = completed.returncode == 0
    return states


def read_installed_launchagent_modes(
    service_uid: int,
) -> dict[str, str]:
    """安全读取服务用户实际安装的三份 plist mode。"""
    try:
        service_home = Path(pwd.getpwuid(service_uid).pw_dir)
    except (KeyError, OSError) as exc:
        raise RuntimeError(
            f"service UID has no local account: {service_uid}"
        ) from exc
    launchagents = service_home / "Library" / "LaunchAgents"
    modes: dict[str, str] = {}
    for label in LAUNCHAGENT_LABELS:
        path = launchagents / f"{label}.plist"
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.fspath(path), flags)
        except OSError as exc:
            raise RuntimeError(
                f"installed plist is missing or unsafe: {path}"
            ) from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise RuntimeError(
                    f"installed plist is not regular: {path}"
                )
            raw = _read_bounded(
                descriptor,
                limit=_MAX_PLIST_BYTES,
            )
        finally:
            os.close(descriptor)
        try:
            payload = plistlib.loads(raw)
        except Exception as exc:
            raise RuntimeError(
                f"installed plist is invalid: {path}"
            ) from exc
        environment = payload.get("EnvironmentVariables")
        if not isinstance(environment, dict):
            raise RuntimeError(
                f"installed plist has no EnvironmentVariables: {path}"
            )
        mode = environment.get(DAILY_COORDINATOR_MODE_ENV)
        if mode not in {"legacy", "ledger"}:
            raise RuntimeError(
                f"installed plist mode is invalid: {path}"
            )
        modes[label] = str(mode)
    return modes


def probe_daily_transition_quiescence(
    service_uid: int,
    business_date: date,
) -> dict[str, int]:
    """联合生产账本和进程表验证全日期静默。"""
    from scheduler.repository import create_engine_from_env

    if not isinstance(business_date, date):
        raise TypeError("business_date must be a date")
    engine = create_engine_from_env()
    try:
        database_report, registered_rows = (
            _read_database_quiescence(engine)
        )
    finally:
        engine.dispose()
    processes = _read_process_table()
    service_processes = [
        row
        for row in processes
        if row["uid"] == service_uid and row["pid"] != os.getpid()
    ]
    registered_pids = {
        int(row["process_id"])
        for row in registered_rows
        if row["process_id"] is not None
    }
    registered_pgids = {
        int(row["process_group_id"])
        for row in registered_rows
        if row["process_group_id"] is not None
    }
    registered_alive = sum(
        1
        for row in service_processes
        if (
            row["pid"] in registered_pids
            or row["pgid"] in registered_pgids
        )
    )
    project_process_count = sum(
        1
        for row in service_processes
        if _is_daily_platform_process(str(row["command"]))
    )
    report = {
        **database_report,
        "registered_process_alive_count": registered_alive,
        "project_process_count": project_process_count,
    }
    _validate_quiescence_report(report)
    return report


def _read_database_quiescence(
    engine: object,
) -> tuple[dict[str, int], tuple[Mapping[str, object], ...]]:
    """全局统计所有日期；切换或回放不允许遗留执行与孤儿。"""
    from sqlalchemy import text

    with engine.connect() as connection:
        active_occurrence_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_schedule_occurrences
                WHERE completion_state IN ('PENDING', 'RUNNING')
                """
            )
        ).scalar_one()
        nonterminal_item_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_schedule_items
                WHERE state IN (
                    'PENDING',
                    'RUNNING',
                    'RETRY_WAIT',
                    'ABANDONED'
                )
                """
            )
        ).scalar_one()
        running_ledger_run_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_scheme_runs
                WHERE schedule_item_id IS NOT NULL
                  AND status = 'running'
                """
            )
        ).scalar_one()
        cleanup_pending_count = connection.execute(
            text(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM t_schedule_items AS item
                        WHERE item.failure_code =
                            'ABANDONED_FENCE_PENDING_CLEANUP'
                           OR EXISTS (
                                SELECT 1
                                FROM t_scheme_runs AS linked_run
                                WHERE linked_run.schedule_item_id =
                                        item.item_id
                                  AND linked_run.failure_code =
                                    'ABANDONED_FENCE_PENDING_CLEANUP'
                           )
                    )
                    +
                    (
                        SELECT COUNT(*)
                        FROM t_scheme_runs AS orphan_run
                        WHERE orphan_run.failure_code =
                                'ABANDONED_FENCE_PENDING_CLEANUP'
                          AND (
                                orphan_run.schedule_item_id IS NULL
                                OR NOT EXISTS (
                                    SELECT 1
                                    FROM t_schedule_items AS owner_item
                                    WHERE owner_item.item_id =
                                        orphan_run.schedule_item_id
                                )
                          )
                    )
                """
            )
        ).scalar_one()
        running_legacy_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM t_scheme_runs
                WHERE prediction_phase = 'scheduled_live'
                  AND schedule_item_id IS NULL
                  AND status = 'running'
                """
            )
        ).scalar_one()
        registered_rows = connection.execute(
            text(
                """
                SELECT process_id, process_group_id
                FROM t_scheme_runs
                WHERE (
                    status = 'running'
                    OR failure_code =
                      'ABANDONED_FENCE_PENDING_CLEANUP'
                )
                  AND (
                    process_id IS NOT NULL
                    OR process_group_id IS NOT NULL
                  )
                """
            )
        ).mappings().all()
    return (
        {
            "active_occurrence_count":
                int(active_occurrence_count),
            "nonterminal_item_count": int(nonterminal_item_count),
            "running_ledger_run_count":
                int(running_ledger_run_count),
            "cleanup_pending_count":
                int(cleanup_pending_count),
            "running_legacy_scheduled_live_run_count":
                int(running_legacy_count),
        },
        tuple(dict(row) for row in registered_rows),
    )


def _validate_quiescence_report(
    report: Mapping[str, object],
) -> None:
    if set(report) != QUIESCENCE_FIELDS:
        raise RuntimeError(
            "daily coordinator quiescence probe must return exact fields"
        )
    for field, value in report.items():
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise RuntimeError(
                f"daily coordinator quiescence field is invalid: {field}"
            )


def _read_process_table() -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            "ps",
            "-axo",
            "pid=,pgid=,uid=,command=",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            pid, pgid, uid = (
                int(value) for value in fields[:3]
            )
        except ValueError:
            continue
        rows.append(
            {
                "pid": pid,
                "pgid": pgid,
                "uid": uid,
                "command": fields[3],
            }
        )
    return rows


def _is_daily_platform_process(command: str) -> bool:
    normalized = command.casefold()
    tokens = (
        "bond-factor-lab",
        "scheduler.main",
        "scheduler.scheme_runner",
        "backend.main",
        "scheduler.v2_daily_preflight",
        "/schemes/",
    )
    return any(token in normalized for token in tokens)


def _read_bounded(descriptor: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise RuntimeError("installed plist is too large")
        chunks.append(chunk)
