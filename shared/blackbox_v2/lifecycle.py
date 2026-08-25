from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from shared.runtime_paths import resolve_runtime_state_path
from shared.scheme_lifecycle_state import write_lifecycle_state


INCOMPLETE_PHASES = frozenset({"prepared", "config_written", "db_committed", "unresolved"})
TERMINAL_PHASES = frozenset({"verified", "compensated", "unresolved"})
_TRANSITIONS = {
    "prepared": frozenset({"config_written", "compensated", "unresolved"}),
    "config_written": frozenset({"db_committed", "compensated", "unresolved"}),
    "db_committed": frozenset({"verified", "compensated", "unresolved"}),
    "verified": frozenset(),
    "compensated": frozenset(),
    "unresolved": frozenset(),
}


@dataclass(frozen=True)
class LifecycleState:
    config_status: str
    # version_status is the exact database version state. Keep the separately
    # persisted config state explicit so independent verification covers both.
    version_status: str
    registry_status: str
    config_version_status: str | None = None

    def __post_init__(self) -> None:
        if self.config_version_status is None:
            object.__setattr__(self, "config_version_status", self.version_status)


@dataclass(frozen=True)
class LifecyclePhaseEvent:
    phase: str
    at: str


@dataclass(frozen=True)
class LifecycleJournal:
    operation_id: str
    action: str
    scheme_id: str
    scheme_version: str
    harness_run_id: str
    previous: LifecycleState
    target: LifecycleState
    phase: str
    operation_scope_sha256: str
    error: str | None = None
    reconciliation_of: str | None = None
    reconciled_by: str | None = None
    phase_events: tuple[LifecyclePhaseEvent, ...] = ()

    @classmethod
    def prepare(
        cls,
        *,
        action: str,
        scheme_id: str,
        scheme_version: str,
        harness_run_id: str,
        previous: LifecycleState,
        target: LifecycleState,
        operation_scope_sha256: str,
        reconciliation_of: str | None = None,
    ) -> "LifecycleJournal":
        return cls(
            operation_id=f"op_{uuid.uuid4().hex}",
            action=action,
            scheme_id=scheme_id,
            scheme_version=scheme_version,
            harness_run_id=harness_run_id,
            previous=previous,
            target=target,
            phase="prepared",
            operation_scope_sha256=operation_scope_sha256,
            reconciliation_of=reconciliation_of,
            phase_events=(LifecyclePhaseEvent("prepared", _utc_iso_timestamp()),),
        )

    def transition(self, phase: str, *, error: str | None = None) -> "LifecycleJournal":
        if phase not in _TRANSITIONS.get(self.phase, frozenset()):
            raise ValueError(f"invalid lifecycle phase transition: {self.phase} -> {phase}")
        return replace(
            self,
            phase=phase,
            error=error,
            phase_events=self.phase_events + (LifecyclePhaseEvent(phase, _utc_iso_timestamp()),),
        )


class LifecycleOperationError(RuntimeError):
    def __init__(self, message: str, *, journal_path: Path, compensated: bool) -> None:
        super().__init__(message)
        self.journal_path = journal_path
        self.compensated = compensated


class LifecycleLockTimeout(RuntimeError):
    """生命周期互斥锁未在限定时间内取得。"""


def lifecycle_root(project_root: str | Path, scheme_id: str) -> Path:
    root = resolve_runtime_state_path(
        relative_path="artifacts/blackbox-v2-lifecycle",
        development_default=(
            Path(project_root)
            / "backtest_artifacts"
            / "blackbox_v2_lifecycle"
        ),
    )
    return root / scheme_id


def write_journal(project_root: str | Path, journal: LifecycleJournal) -> Path:
    path = lifecycle_root(project_root, journal.scheme_id) / f"{journal.operation_id}.json"
    _atomic_write_bytes(
        path,
        (json.dumps(asdict(journal), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return path


def load_journal(path: str | Path) -> LifecycleJournal:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    journal = LifecycleJournal(
        operation_id=str(raw["operation_id"]),
        action=str(raw["action"]),
        scheme_id=str(raw["scheme_id"]),
        scheme_version=str(raw["scheme_version"]),
        harness_run_id=str(raw["harness_run_id"]),
        previous=LifecycleState(**raw["previous"]),
        target=LifecycleState(**raw["target"]),
        phase=str(raw["phase"]),
        operation_scope_sha256=str(
            raw.get("operation_scope_sha256") or raw["token_hash"]
        ),
        error=str(raw["error"]) if raw.get("error") is not None else None,
        reconciliation_of=(
            str(raw["reconciliation_of"])
            if raw.get("reconciliation_of") is not None
            else None
        ),
        reconciled_by=(
            str(raw["reconciled_by"]) if raw.get("reconciled_by") is not None else None
        ),
        phase_events=tuple(
            LifecyclePhaseEvent(phase=str(event["phase"]), at=str(event["at"]))
            for event in raw.get("phase_events", [])
        ),
    )
    if journal.phase not in _TRANSITIONS:
        raise ValueError(f"invalid lifecycle journal phase: {journal.phase}")
    return journal


def pending_journals(project_root: str | Path, scheme_id: str) -> list[tuple[Path, LifecycleJournal]]:
    root = lifecycle_root(project_root, scheme_id)
    if not root.is_dir():
        return []
    journals: list[tuple[Path, LifecycleJournal]] = []
    for path in sorted(root.glob("*.json")):
        try:
            journal = load_journal(path)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid lifecycle journal blocks {scheme_id}: {path}: {exc}") from exc
        journals.append((path, journal))

    return [
        (path, journal)
        for path, journal in journals
        if journal.reconciliation_of is None
        and journal.phase in INCOMPLETE_PHASES
        and journal.reconciled_by is None
        and not any(
            candidate.action == "lifecycle_reconcile"
            and candidate.phase == "verified"
            and candidate.reconciliation_of == journal.operation_id
            and candidate.scheme_version == journal.scheme_version
            and candidate.harness_run_id == journal.harness_run_id
            and candidate.target == journal.previous
            for _, candidate in journals
        )
    ]


def assert_lifecycle_clear(project_root: str | Path, scheme_id: str) -> None:
    pending = pending_journals(project_root, scheme_id)
    if not pending:
        return
    details = ", ".join(f"{journal.operation_id}:{journal.phase}" for _, journal in pending)
    unresolved = any(journal.phase == "unresolved" for _, journal in pending)
    label = "unresolved lifecycle journal" if unresolved else "incomplete lifecycle journal"
    raise RuntimeError(f"{label} blocks {scheme_id}: {details}")


def apply_lifecycle_state(
    project_root: str | Path,
    *,
    scheme_id: str,
    scheme_version: str,
    state: LifecycleState,
    harness_run_id: str | None = None,
) -> None:
    """把生效生命周期状态写入本机覆盖层。

    该状态是主机级运行状态，不写入 immutable release 内的 config.yaml——后者参与
    source_tree_sha256，写它会使 release 相对安装记录漂移。
    """
    write_lifecycle_state(
        project_root,
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        status=state.config_status,
        version_status=str(state.config_version_status),
        harness_run_id=harness_run_id,
    )


def perform_lifecycle_transition(
    *,
    project_root: str | Path,
    config_path: str | Path,
    action: str,
    scheme_id: str,
    scheme_version: str,
    harness_run_id: str,
    previous: LifecycleState,
    target: LifecycleState,
    compensation: LifecycleState,
    operation_scope_sha256: str,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
) -> tuple[LifecycleState, Path]:
    """执行跨配置/数据库生命周期切换，并在失败时收敛到安全状态。"""
    with lifecycle_operation_lock(project_root, scheme_id):
        return _perform_lifecycle_transition_unlocked(
            project_root=project_root,
            config_path=config_path,
            action=action,
            scheme_id=scheme_id,
            scheme_version=scheme_version,
            harness_run_id=harness_run_id,
            previous=previous,
            target=target,
            compensation=compensation,
            operation_scope_sha256=operation_scope_sha256,
            apply_database=apply_database,
            read_state=read_state,
        )


def _perform_lifecycle_transition_unlocked(
    *,
    project_root: str | Path,
    config_path: str | Path,
    action: str,
    scheme_id: str,
    scheme_version: str,
    harness_run_id: str,
    previous: LifecycleState,
    target: LifecycleState,
    compensation: LifecycleState,
    operation_scope_sha256: str,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
) -> tuple[LifecycleState, Path]:
    assert_lifecycle_clear(project_root, scheme_id)
    config_path = Path(config_path)
    actual_before = read_state()
    if actual_before != previous:
        raise RuntimeError(
            f"lifecycle read-only preflight mismatch: expected={previous}, actual={actual_before}"
        )
    journal = LifecycleJournal.prepare(
        action=action,
        scheme_id=scheme_id,
        scheme_version=scheme_version,
        harness_run_id=harness_run_id,
        previous=previous,
        target=target,
        operation_scope_sha256=operation_scope_sha256,
    )
    journal_path = write_journal(project_root, journal)
    try:
        apply_lifecycle_state(
            project_root,
            scheme_id=scheme_id,
            scheme_version=scheme_version,
            state=target,
            harness_run_id=harness_run_id,
        )
        next_journal = journal.transition("config_written")
        write_journal(project_root, next_journal)
        journal = next_journal
        apply_database(target)
        next_journal = journal.transition("db_committed")
        write_journal(project_root, next_journal)
        journal = next_journal
        actual = read_state()
        if actual != target:
            raise RuntimeError(f"lifecycle independent verification mismatch: expected={target}, actual={actual}")
        next_journal = journal.transition("verified")
        write_journal(project_root, next_journal)
        journal = next_journal
        return actual, journal_path
    except BaseException as exc:
        compensation_errors: list[str] = []
        try:
            apply_lifecycle_state(
                project_root,
                scheme_id=scheme_id,
                scheme_version=scheme_version,
                state=compensation,
                harness_run_id=harness_run_id,
            )
        except BaseException as rollback_exc:
            compensation_errors.append(f"config compensation failed: {rollback_exc}")
        try:
            apply_database(compensation)
        except BaseException as rollback_exc:
            compensation_errors.append(f"database compensation failed: {rollback_exc}")
        try:
            actual = read_state()
            if actual != compensation:
                compensation_errors.append(
                    f"compensation verification mismatch: expected={compensation}, actual={actual}"
                )
        except BaseException as rollback_exc:
            compensation_errors.append(f"compensation verification failed: {rollback_exc}")

        phase = "unresolved" if compensation_errors else "compensated"
        error = str(exc)
        if compensation_errors:
            error = f"{error}; " + "; ".join(compensation_errors)
        journal = _failure_transition(journal, phase, error)
        write_journal(project_root, journal)
        raise LifecycleOperationError(
            f"lifecycle {action} failed: {error}",
            journal_path=journal_path,
            compensated=phase == "compensated",
        ) from exc


def reconcile_journal(
    journal_path: str | Path,
    *,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
    operation_scope_sha256: str | None = None,
) -> LifecycleState:
    """只回退到 previous 安全状态；绝不把中断操作继续推进到 active。"""
    path = Path(journal_path)
    journal = load_journal(path)
    project_root = _project_root_from_journal_path(path, journal.scheme_id)
    with lifecycle_operation_lock(project_root, journal.scheme_id):
        return _reconcile_journal_unlocked(
            path,
            apply_database=apply_database,
            read_state=read_state,
            operation_scope_sha256=operation_scope_sha256,
        )


def _reconcile_journal_unlocked(
    path: Path,
    *,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
    operation_scope_sha256: str | None,
) -> LifecycleState:
    journal = load_journal(path)
    project_root = _project_root_from_journal_path(path, journal.scheme_id)
    pending = pending_journals(project_root, journal.scheme_id)
    if (
        len(pending) != 1
        or pending[0][1].operation_id != journal.operation_id
    ):
        raise RuntimeError(
            "journal is no longer the unique pending lifecycle journal"
        )
    return _reconcile_with_linked_journal(
        path,
        journal,
        apply_database=apply_database,
        read_state=read_state,
        operation_scope_sha256=operation_scope_sha256,
    )


def _reconcile_with_linked_journal(
    original_path: Path,
    original: LifecycleJournal,
    *,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
    operation_scope_sha256: str | None,
) -> LifecycleState:
    """以新 journal 对账 pending 操作，保留原 phase 及错误证据。"""
    actual_before = read_state()
    reconciliation = LifecycleJournal.prepare(
        action="lifecycle_reconcile",
        scheme_id=original.scheme_id,
        scheme_version=original.scheme_version,
        harness_run_id=original.harness_run_id,
        previous=actual_before,
        target=original.previous,
        operation_scope_sha256=(
            operation_scope_sha256 or original.operation_scope_sha256
        ),
        reconciliation_of=original.operation_id,
    )
    project_root = _project_root_from_journal_path(original_path, original.scheme_id)
    reconciliation_path = write_journal(project_root, reconciliation)
    try:
        apply_lifecycle_state(
            project_root,
            scheme_id=original.scheme_id,
            scheme_version=original.scheme_version,
            state=original.previous,
        )
        next_journal = reconciliation.transition("config_written")
        write_journal(project_root, next_journal)
        reconciliation = next_journal
        apply_database(original.previous)
        next_journal = reconciliation.transition("db_committed")
        write_journal(project_root, next_journal)
        reconciliation = next_journal
        actual_after = read_state()
        if actual_after != original.previous:
            raise RuntimeError(
                "reconciliation verification mismatch: "
                f"expected={original.previous}, actual={actual_after}"
            )
        next_journal = reconciliation.transition("verified")
        write_journal(project_root, next_journal)
        reconciliation = next_journal
        return original.previous
    except BaseException as exc:
        if reconciliation.phase not in TERMINAL_PHASES:
            failed = reconciliation.transition("unresolved", error=str(exc))
            _atomic_write_journal_path(reconciliation_path, failed)
        raise RuntimeError(f"unresolved lifecycle reconciliation failed: {exc}") from exc


@contextmanager
def lifecycle_operation_lock(
    project_root: str | Path,
    scheme_id: str,
    *,
    timeout_sec: float = 30.0,
    poll_interval_sec: float = 0.05,
):
    if timeout_sec < 0:
        raise ValueError("lifecycle lock timeout_sec must be non-negative")
    if poll_interval_sec <= 0:
        raise ValueError("lifecycle lock poll_interval_sec must be positive")
    root = lifecycle_root(project_root, scheme_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_sec
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LifecycleLockTimeout(
                            "timed out waiting for Blackbox lifecycle lock: "
                            f"scheme_id={scheme_id} timeout_sec={timeout_sec:g}"
                        ) from exc
                    time.sleep(min(poll_interval_sec, remaining))
            yield
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _project_root_from_journal_path(path: Path, scheme_id: str) -> Path:
    expected_tail = Path("backtest_artifacts") / "blackbox_v2_lifecycle" / scheme_id
    parent = path.parent.resolve()
    parts = expected_tail.parts
    if tuple(parent.parts[-len(parts):]) != parts:
        raise ValueError(f"journal path is outside the lifecycle root for {scheme_id}: {path}")
    return Path(*parent.parts[:-len(parts)])


def _failure_transition(journal: LifecycleJournal, phase: str, error: str) -> LifecycleJournal:
    if journal.phase in TERMINAL_PHASES:
        return journal
    if journal.phase == "unresolved":
        return replace(journal, error=error)
    return journal.transition(phase, error=error)


def _utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_journal_path(path: Path, journal: LifecycleJournal) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(asdict(journal), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
