"""子进程组清理的 fail-closed 结果与异常。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, ContextManager


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


class ProcessStartGuardPoisonedError(ProcessGroupTerminationError):
    """同一 occurrence 已存在清理未确认的进程，禁止再启动。"""

    def __init__(
        self,
        *,
        poison_error: ProcessGroupTerminationError,
    ) -> None:
        self.poison_error = poison_error
        super().__init__(
            termination=poison_error.termination,
            context=(
                "process-start guard is poisoned by unconfirmed "
                f"cleanup: {poison_error}"
            ),
        )


class ProcessStartGuard:
    """串行化启动登记窗口，并永久传播清理未确认的 poison。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._poison_error: ProcessGroupTerminationError | None = None

    def __enter__(self) -> ProcessStartGuard:
        self._lock.acquire()
        if self._poison_error is not None:
            poison_error = self._poison_error
            self._lock.release()
            raise ProcessStartGuardPoisonedError(
                poison_error=poison_error,
            ) from poison_error
        return self

    def __exit__(
        self,
        _exc_type,
        exc: BaseException | None,
        _traceback,
    ) -> bool:
        try:
            if (
                isinstance(exc, ProcessGroupTerminationError)
                and not exc.termination.confirmed_gone
            ):
                self._poison_error = exc
        finally:
            self._lock.release()
        return False

    def poison(self, error: ProcessGroupTerminationError) -> None:
        """运行阶段清理未确认时，永久拒绝本批次后续启动。"""
        with self._lock:
            if self._poison_error is None:
                self._poison_error = error


def require_process_start_guard(
    guard: object | None,
) -> ProcessStartGuard | None:
    """生产入口只接受 canonical guard，禁止普通 Lock 误绕过 poison。"""
    if guard is None:
        return None
    if type(guard) is not ProcessStartGuard:
        raise TypeError(
            "process_start_guard must be a canonical ProcessStartGuard"
        )
    return guard


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


def start_process_group(
    command: list[str],
    *,
    process_start_guard: ContextManager[object] | None,
    process_started: Callable[[int, int], None] | None = None,
    process_fence: Callable[[], None] | None = None,
    cancellation_event: threading.Event | None = None,
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], int]:
    """在启动 guard 内启动独立进程组并完成登记；登记失败时确认整个组已退出。

    Native 与 Blackbox 两个执行器共用本函数，保证启动、取消与清理语义一致。
    无法确认清理时抛出 ``ProcessRegistrationCleanupError``，供调用方 poison 批次。
    """
    guard_context = (
        process_start_guard
        if process_start_guard is not None
        else nullcontext()
    )
    process_start_completed = False
    unconfirmed_cleanup_error: ProcessRegistrationCleanupError | None = None
    try:
        with guard_context:
            if cancellation_event is not None and cancellation_event.is_set():
                raise RuntimeError("Native process start was cancelled")
            if process_fence is not None:
                process_fence()
            process = subprocess.Popen(
                command,
                start_new_session=True,
                **popen_kwargs,
            )
            process_group_id: int | None = None
            try:
                process_group_id = capture_new_session_process_group(
                    process
                )
                if process_started is not None:
                    process_started(process.pid, process_group_id)
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()
                ):
                    raise RuntimeError("Native process was cancelled")
                if process_fence is not None:
                    process_fence()
            except BaseException as registration_error:
                termination = terminate_process_group(
                    process,
                    process_group_id=process_group_id,
                )
                if not termination.confirmed_gone:
                    unconfirmed_cleanup_error = (
                        ProcessRegistrationCleanupError(
                            registration_error=registration_error,
                            termination=termination,
                        )
                    )
                    raise unconfirmed_cleanup_error from registration_error
                raise
            process_start_completed = True
    except BaseException as guard_exit_error:
        if (
            unconfirmed_cleanup_error is not None
            and guard_exit_error is not unconfirmed_cleanup_error
        ):
            raise unconfirmed_cleanup_error from guard_exit_error
        if process_start_completed:
            termination = terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
            if not termination.confirmed_gone:
                raise ProcessRegistrationCleanupError(
                    registration_error=guard_exit_error,
                    termination=termination,
                ) from guard_exit_error
        raise
    if not process_start_completed:
        if unconfirmed_cleanup_error is not None:
            raise unconfirmed_cleanup_error
        raise RuntimeError(
            "process_start_guard suppressed a process-start failure"
        )
    if process_group_id is None:
        raise AssertionError("process group was not captured")
    return process, process_group_id


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
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        _reap_process_leader(process)
        if not _process_group_exists(process_group_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


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
    except OSError:
        # 无法证明不存在时必须 fail closed。
        return True
    return True
