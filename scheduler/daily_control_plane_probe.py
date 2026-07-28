"""日频控制面切换与隔离回放共用的只读静默探针。"""

from __future__ import annotations

import os
import plistlib
import pwd
import shlex
import stat
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from scheduler.daily_policy import (
    APPROVED_0629_LIVE_SOURCE_SCHEMES,
)
from shared.daily_coordinator_mode import (
    DAILY_COORDINATOR_MODE_ENV,
)
from shared.input_artifacts import LIVE_SOURCE_INPUT_MODE


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


@dataclass(frozen=True)
class ReplayProcessBoundaryObservation:
    """不含命令行的隔离 replay 进程边界证据。"""

    process_id: int
    process_group_id: int
    uid: int
    classification: str


@dataclass(frozen=True)
class ReplayProcessBoundaryReport:
    """当前 occurrence 的确定性 replay 进程边界报告。"""

    service_uid: int
    occurrence_id: int
    active_item_ids: tuple[int, ...]
    registered_leader_process_ids: tuple[int, ...]
    registered_process_group_ids: tuple[int, ...]
    allowed_processes: tuple[
        ReplayProcessBoundaryObservation,
        ...,
    ]
    blocked_processes: tuple[
        ReplayProcessBoundaryObservation,
        ...,
    ]

    @property
    def boundary_clear(self) -> bool:
        """仅在没有任何阻塞进程时返回真。"""
        return not self.blocked_processes


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
    *,
    allow_backend: bool = False,
) -> dict[str, int]:
    """联合账本和进程表验证静默，可仅忽略展示后端。"""
    from scheduler.repository import create_engine_from_env

    if not isinstance(business_date, date):
        raise TypeError("business_date must be a date")
    if not isinstance(allow_backend, bool):
        raise TypeError("allow_backend must be bool")
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
        if (
            _is_daily_platform_process(str(row["command"]))
            and not (
                allow_backend
                and _is_backend_service_process(
                    str(row["command"])
                )
            )
        )
    )
    report = {
        **database_report,
        "registered_process_alive_count": registered_alive,
        "project_process_count": project_process_count,
    }
    _validate_quiescence_report(report)
    return report


def probe_isolated_replay_process_boundary(
    engine: object,
    *,
    service_uid: int,
    occurrence_id: int,
    active_item_ids: Iterable[int],
    allow_backend: bool = False,
) -> ReplayProcessBoundaryReport:
    """只允许当前 operator 与隔离账本证明的 replay 进程组。"""
    from scheduler.repository import (
        read_current_replay_attempt_processes,
    )

    normalized_service_uid = _require_positive_int(
        service_uid,
        field="service_uid",
    )
    normalized_occurrence_id = _require_positive_int(
        occurrence_id,
        field="occurrence_id",
    )
    if not isinstance(allow_backend, bool):
        raise TypeError("allow_backend must be bool")
    normalized_item_ids = tuple(
        sorted(
            {
                _require_positive_int(
                    item_id,
                    field="active_item_id",
                )
                for item_id in active_item_ids
            }
        )
    )
    registered_rows = read_current_replay_attempt_processes(
        engine,
        occurrence_id=normalized_occurrence_id,
        active_item_ids=normalized_item_ids,
    )
    registered_pids, registered_pgids = (
        _normalize_registered_replay_processes(registered_rows)
    )
    approved_live_source_pids = (
        _approved_live_source_process_ids(registered_rows)
    )
    process_rows = _normalize_process_rows(
        _read_process_table(fail_on_malformed=True)
    )
    operator_pid = os.getpid()
    if (
        not _is_safe_process_identity(operator_pid)
        or sum(
            1
            for row in process_rows
            if row["pid"] == operator_pid
        )
        != 1
    ):
        raise RuntimeError(
            "operator process identity is not uniquely observable"
        )
    operator_process_group_id = int(
        next(
            row["pgid"]
            for row in process_rows
            if row["pid"] == operator_pid
        )
    )
    _validate_replay_operator_identity(
        operator_pid=operator_pid,
        operator_process_group_id=operator_process_group_id,
        registered_process_ids=registered_pids,
        registered_process_group_ids=registered_pgids,
    )
    replay_descendant_pids = _read_replay_descendant_pids(
        process_rows,
        root_process_ids={
            operator_pid,
            *registered_pids,
        },
    )
    approved_live_source_descendant_pids = (
        _read_replay_descendant_pids(
            process_rows,
            root_process_ids=set(approved_live_source_pids),
        )
    )
    allowed: list[ReplayProcessBoundaryObservation] = []
    blocked: list[ReplayProcessBoundaryObservation] = []
    for row in process_rows:
        pid = int(row["pid"])
        pgid = int(row["pgid"])
        uid = int(row["uid"])
        if pid == operator_pid:
            destination = (
                allowed
                if uid == normalized_service_uid
                else blocked
            )
            destination.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "operator"
                        if uid == normalized_service_uid
                        else "operator_uid_mismatch"
                    ),
                )
            )
            continue
        matches_leader = pid in registered_pids
        matches_group = pgid in registered_pgids
        if matches_leader or matches_group:
            if uid != normalized_service_uid:
                blocked.append(
                    _replay_process_observation(
                        row,
                        classification=(
                            "registered_identity_uid_mismatch"
                        ),
                    )
                )
                continue
            allowed.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "registered_leader"
                        if matches_leader
                        else "registered_process_group_member"
                    ),
                )
            )
            continue
        if pgid == operator_process_group_id:
            if uid != normalized_service_uid:
                blocked.append(
                    _replay_process_observation(
                        row,
                        classification=(
                            "operator_process_group_uid_mismatch"
                        ),
                    )
                )
                continue
            if pid in replay_descendant_pids:
                allowed.append(
                    _replay_process_observation(
                        row,
                        classification=(
                            "operator_process_group_member"
                        ),
                    )
                )
                continue
        if pid in approved_live_source_descendant_pids:
            destination = (
                allowed
                if uid == normalized_service_uid
                else blocked
            )
            destination.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "approved_live_source_descendant"
                        if uid == normalized_service_uid
                        else (
                            "approved_live_source_descendant_"
                            "uid_mismatch"
                        )
                    ),
                )
            )
            continue
        if pid in replay_descendant_pids:
            blocked.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "unregistered_replay_descendant"
                    ),
                )
            )
            continue
        if (
            allow_backend
            and _is_backend_service_process(str(row["command"]))
        ):
            destination = (
                allowed
                if uid == normalized_service_uid
                else blocked
            )
            destination.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "display_backend"
                        if uid == normalized_service_uid
                        else "display_backend_uid_mismatch"
                    ),
                )
            )
            continue
        if _is_daily_platform_process(str(row["command"])):
            blocked.append(
                _replay_process_observation(
                    row,
                    classification=(
                        "unregistered_daily_platform_process"
                    ),
                )
            )
    return ReplayProcessBoundaryReport(
        service_uid=normalized_service_uid,
        occurrence_id=normalized_occurrence_id,
        active_item_ids=normalized_item_ids,
        registered_leader_process_ids=registered_pids,
        registered_process_group_ids=registered_pgids,
        allowed_processes=tuple(allowed),
        blocked_processes=tuple(blocked),
    )


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


def _read_process_table(
    *,
    fail_on_malformed: bool = False,
) -> list[dict[str, object]]:
    command = [
        "/bin/ps",
        "-axo",
        "pid=,ppid=,pgid=,uid=,command=",
    ]
    try:
        sampler = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        sampler_pid = sampler.pid
        stdout, stderr = sampler.communicate()
    except Exception:
        raise RuntimeError(
            "OS process table sampling failed"
        ) from None
    if (
        not _is_safe_process_identity(sampler_pid)
        or not isinstance(sampler.returncode, int)
        or isinstance(sampler.returncode, bool)
        or sampler.returncode != 0
        or not isinstance(stdout, str)
        or not stdout.strip()
        or not isinstance(stderr, str)
        or stderr
    ):
        raise RuntimeError("OS process table sampling failed")
    rows: list[dict[str, object]] = []
    sampler_row_count = 0
    for line in stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            if fail_on_malformed:
                raise RuntimeError("OS process table is malformed")
            continue
        try:
            pid, ppid, pgid, uid = (
                int(value) for value in fields[:4]
            )
        except ValueError:
            if fail_on_malformed:
                raise RuntimeError(
                    "OS process table is malformed"
                ) from None
            continue
        if pid == sampler_pid:
            sampler_row_count += 1
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "pgid": pgid,
                "uid": uid,
                "command": fields[4],
            }
        )
    if sampler_row_count != 1:
        raise RuntimeError("OS process table sampling failed")
    return rows


def _validate_replay_operator_identity(
    *,
    operator_pid: int,
    operator_process_group_id: int,
    registered_process_ids: Iterable[int],
    registered_process_group_ids: Iterable[int],
) -> None:
    if (
        not _is_safe_process_identity(operator_pid)
        or not _is_safe_process_identity(
            operator_process_group_id
        )
    ):
        raise RuntimeError("operator process identity is invalid")
    operator_identity = {
        operator_pid,
        operator_process_group_id,
    }
    registered_identity = {
        *registered_process_ids,
        *registered_process_group_ids,
    }
    if operator_identity & registered_identity:
        raise RuntimeError(
            "operator and registered replay process "
            "identities collide"
        )


def _normalize_registered_replay_processes(
    rows: Iterable[object],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    registered_pids: set[int] = set()
    registered_pgids: set[int] = set()
    for row in rows:
        try:
            process_id = row.process_id
            process_group_id = row.process_group_id
        except AttributeError:
            raise RuntimeError(
                "registered replay process identity is invalid"
            ) from None
        if (
            not _is_safe_process_identity(process_id)
            or not _is_safe_process_identity(process_group_id)
            or process_id != process_group_id
            or process_id in registered_pids
            or process_group_id in registered_pgids
        ):
            raise RuntimeError(
                "registered replay process identity is invalid"
            )
        registered_pids.add(process_id)
        registered_pgids.add(process_group_id)
    return (
        tuple(sorted(registered_pids)),
        tuple(sorted(registered_pgids)),
    )


def _approved_live_source_process_ids(
    rows: Iterable[object],
) -> tuple[int, ...]:
    """只信任精确 allowlist 兼容项的已登记外层进程。"""
    approved: set[int] = set()
    for row in rows:
        try:
            process_id = row.process_id
            base_scheme_id = row.base_scheme_id
            input_compatibility = row.input_compatibility
        except AttributeError:
            raise RuntimeError(
                "registered replay process identity is invalid"
            ) from None
        if (
            not _is_safe_process_identity(process_id)
            or not isinstance(base_scheme_id, str)
            or not base_scheme_id
            or not isinstance(input_compatibility, str)
            or not input_compatibility
        ):
            raise RuntimeError(
                "registered replay process identity is invalid"
            )
        if (
            input_compatibility == LIVE_SOURCE_INPUT_MODE
            and base_scheme_id
            in APPROVED_0629_LIVE_SOURCE_SCHEMES
        ):
            approved.add(process_id)
    return tuple(sorted(approved))


def _normalize_process_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    normalized: list[Mapping[str, object]] = []
    process_ids: set[int] = set()
    for row in rows:
        try:
            pid = row["pid"]
            ppid = row["ppid"]
            pgid = row["pgid"]
            uid = row["uid"]
            command = row["command"]
        except (KeyError, TypeError):
            raise RuntimeError(
                "OS process table is malformed"
            ) from None
        if (
            not _is_positive_int(pid)
            or not isinstance(ppid, int)
            or isinstance(ppid, bool)
            or ppid < 0
            or not _is_positive_int(pgid)
            or not isinstance(uid, int)
            or isinstance(uid, bool)
            or uid < 0
            or not isinstance(command, str)
            or not command
            or pid in process_ids
        ):
            raise RuntimeError("OS process table is malformed")
        process_ids.add(pid)
        normalized.append(row)
    return tuple(
        sorted(
            normalized,
            key=lambda row: (
                int(row["pid"]),
                int(row["pgid"]),
                int(row["uid"]),
            ),
        )
    )


def _read_replay_descendant_pids(
    rows: Iterable[Mapping[str, object]],
    *,
    root_process_ids: set[int],
) -> frozenset[int]:
    parent_by_pid = {
        int(row["pid"]): int(row["ppid"])
        for row in rows
    }
    for process_id in parent_by_pid:
        visited: set[int] = set()
        current = process_id
        while current != 0:
            if current in visited:
                raise RuntimeError(
                    "OS process table parent relationships "
                    "are malformed"
                )
            visited.add(current)
            try:
                current = parent_by_pid[current]
            except KeyError:
                raise RuntimeError(
                    "OS process table parent relationships "
                    "are malformed"
                ) from None
    children_by_parent: dict[int, list[int]] = {}
    for process_id, parent_id in parent_by_pid.items():
        children_by_parent.setdefault(parent_id, []).append(
            process_id
        )
    descendants: set[int] = set()
    pending = sorted(
        process_id
        for process_id in root_process_ids
        if process_id in parent_by_pid
    )
    while pending:
        parent_id = pending.pop()
        for child_id in sorted(
            children_by_parent.get(parent_id, ())
        ):
            if child_id in descendants:
                continue
            descendants.add(child_id)
            pending.append(child_id)
    return frozenset(descendants)


def _replay_process_observation(
    row: Mapping[str, object],
    *,
    classification: str,
) -> ReplayProcessBoundaryObservation:
    return ReplayProcessBoundaryObservation(
        process_id=int(row["pid"]),
        process_group_id=int(row["pgid"]),
        uid=int(row["uid"]),
        classification=classification,
    )


def _require_positive_int(value: object, *, field: str) -> int:
    if not _is_positive_int(value):
        raise ValueError(f"{field} must be a positive integer")
    return value


def _is_positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _is_safe_process_identity(value: object) -> bool:
    return _is_positive_int(value) and value > 1


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


def _is_backend_service_process(command: str) -> bool:
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return False
    effective = _unwrap_backend_command(tokens)
    if not effective:
        return False
    executable = Path(effective[0]).name.casefold()
    arguments = tuple(
        token.casefold()
        for token in effective[1:]
    )
    if executable == "uvicorn":
        return bool(
            arguments
            and arguments[0] == "backend.main:app"
        )
    if not executable.startswith("python"):
        return False
    if (
        len(arguments) >= 2
        and Path(arguments[0]).name == "uvicorn"
        and arguments[1] == "backend.main:app"
    ):
        return True
    return bool(
        len(arguments) >= 2
        and arguments[0] == "-m"
        and (
            arguments[1] == "backend.main"
            or (
                arguments[1] == "uvicorn"
                and len(arguments) >= 3
                and arguments[2] == "backend.main:app"
            )
        )
    )


def _unwrap_backend_command(
    tokens: tuple[str, ...],
) -> tuple[str, ...]:
    if not tokens:
        return ()
    executable = Path(tokens[0]).name.casefold()
    if (
        executable.startswith("python")
        and len(tokens) >= 2
        and Path(tokens[1]).name.casefold() == "conda"
    ):
        return _unwrap_conda_run(tokens[1:])
    if executable == "conda":
        return _unwrap_conda_run(tokens)
    return tokens


def _unwrap_conda_run(
    tokens: tuple[str, ...],
) -> tuple[str, ...]:
    if len(tokens) < 3 or tokens[1].casefold() != "run":
        return ()
    index = 2
    no_value_options = {
        "--debug-wrapper-scripts",
        "--dev",
        "--live-stream",
        "--no-capture-output",
    }
    value_options = {
        "--cwd",
        "--name",
        "--prefix",
        "-n",
        "-p",
    }
    while index < len(tokens):
        token = tokens[index]
        normalized = token.casefold()
        if token == "--":
            index += 1
            break
        if normalized in no_value_options:
            index += 1
            continue
        if normalized in value_options:
            if index + 1 >= len(tokens):
                return ()
            index += 2
            continue
        if (
            normalized.startswith("--name=")
            or normalized.startswith("--prefix=")
            or normalized.startswith("--cwd=")
        ):
            index += 1
            continue
        if normalized.startswith("-"):
            return ()
        break
    return tokens[index:]


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
