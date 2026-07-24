"""子进程组清理的 fail-closed 结果与异常。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessGroupTerminationResult:
    """一次进程组终止尝试的可审计结果。"""

    process_id: int
    process_group_id: int | None
    term_sent: bool
    kill_sent: bool
    confirmed_gone: bool
    failure_reason: str | None = None


class ProcessGroupTerminationError(RuntimeError):
    """SIGTERM/SIGKILL 后仍无法确认整个进程组消失。"""

    def __init__(
        self,
        *,
        termination: ProcessGroupTerminationResult,
        context: str,
    ) -> None:
        if termination.confirmed_gone:
            raise ValueError(
                "confirmed process-group cleanup cannot raise an error"
            )
        self.termination = termination
        self.context = str(context)
        super().__init__(
            f"{self.context}; process group cleanup was not confirmed: "
            f"pid={termination.process_id}, "
            f"pgid={termination.process_group_id}, "
            f"term_sent={termination.term_sent}, "
            f"kill_sent={termination.kill_sent}, "
            f"reason={termination.failure_reason or 'unknown'}"
        )


class ProcessRegistrationCleanupError(ProcessGroupTerminationError):
    """进程启动登记失败，且对应进程组清理未获确认。"""

    def __init__(
        self,
        *,
        registration_error: BaseException,
        termination: ProcessGroupTerminationResult,
    ) -> None:
        self.registration_error = registration_error
        super().__init__(
            termination=termination,
            context=(
                "process registration callback failed: "
                f"{type(registration_error).__name__}: "
                f"{registration_error}"
            ),
        )


def capture_new_session_process_group(
    process: subprocess.Popen[str],
) -> int:
    """在 leader 可能退出前固定 ``start_new_session`` 的进程组 ID。"""
    process_id = int(process.pid)
    try:
        return int(os.getpgid(process_id))
    except OSError:
        # ``start_new_session=True`` 令新进程成为 session/group leader，
        # 因此即便极短进程已退出，初始 PGID 仍确定等于其 PID。
        return process_id


def terminate_process_group(
    process: subprocess.Popen[str],
    *,
    process_group_id: int | None = None,
    term_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> ProcessGroupTerminationResult:
    """终止独立进程组，并只在整个组确实消失时返回成功。"""
    process_id = int(process.pid)
    pgid = process_group_id
    if pgid is None:
        try:
            pgid = os.getpgid(process_id)
        except ProcessLookupError:
            return ProcessGroupTerminationResult(
                process_id=process_id,
                process_group_id=None,
                term_sent=False,
                kill_sent=False,
                confirmed_gone=True,
            )
        except OSError as exc:
            return ProcessGroupTerminationResult(
                process_id=process_id,
                process_group_id=None,
                term_sent=False,
                kill_sent=False,
                confirmed_gone=False,
                failure_reason=f"could not resolve process group: {exc}",
            )
    pgid = int(pgid)
    if pgid <= 1 or pgid == os.getpgrp():
        return ProcessGroupTerminationResult(
            process_id=process_id,
            process_group_id=pgid,
            term_sent=False,
            kill_sent=False,
            confirmed_gone=False,
            failure_reason="refused unsafe process group id",
        )

    term_sent, term_error = _send_group_signal(pgid, signal.SIGTERM)
    if term_error == "gone":
        return ProcessGroupTerminationResult(
            process_id=process_id,
            process_group_id=pgid,
            term_sent=False,
            kill_sent=False,
            confirmed_gone=True,
        )
    if _wait_for_process_group_exit(
        process,
        pgid,
        timeout=term_timeout,
    ):
        return ProcessGroupTerminationResult(
            process_id=process_id,
            process_group_id=pgid,
            term_sent=term_sent,
            kill_sent=False,
            confirmed_gone=True,
        )

    kill_sent, kill_error = _send_group_signal(pgid, signal.SIGKILL)
    if kill_error == "gone":
        return ProcessGroupTerminationResult(
            process_id=process_id,
            process_group_id=pgid,
            term_sent=term_sent,
            kill_sent=False,
            confirmed_gone=True,
        )
    if _wait_for_process_group_exit(
        process,
        pgid,
        timeout=kill_timeout,
    ):
        return ProcessGroupTerminationResult(
            process_id=process_id,
            process_group_id=pgid,
            term_sent=term_sent,
            kill_sent=kill_sent,
            confirmed_gone=True,
        )

    errors = [
        error
        for error in (term_error, kill_error)
        if error not in {None, "gone"}
    ]
    errors.append("process group still exists after SIGKILL")
    return ProcessGroupTerminationResult(
        process_id=process_id,
        process_group_id=pgid,
        term_sent=term_sent,
        kill_sent=kill_sent,
        confirmed_gone=False,
        failure_reason="; ".join(errors),
    )


def _send_group_signal(
    process_group_id: int,
    signum: int,
) -> tuple[bool, str | None]:
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        return False, "gone"
    except OSError as exc:
        return False, f"{signal.Signals(signum).name} failed: {exc}"
    return True, None


def _wait_for_process_group_exit(
    process: subprocess.Popen[str],
    process_group_id: int,
    *,
    timeout: float,
    poll_interval: float = 0.05,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        _reap_process_leader(process)
        if not _process_group_exists(process_group_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(0.001, poll_interval), remaining))


def _reap_process_leader(process: subprocess.Popen[str]) -> None:
    try:
        process.poll()
    except (AttributeError, OSError, ValueError):
        return


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # 无法证明不存在时必须 fail closed。
        return True
    return True
