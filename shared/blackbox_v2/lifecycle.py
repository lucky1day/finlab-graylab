from __future__ import annotations

import json
import os
import tempfile
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable


INCOMPLETE_PHASES = frozenset({"prepared", "config_written", "db_committed", "unresolved"})
TERMINAL_PHASES = frozenset({"verified", "compensated"})
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
    version_status: str
    registry_status: str


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
    token_hash: str
    error: str | None = None

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
        token_hash: str,
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
            token_hash=token_hash,
        )

    def transition(self, phase: str, *, error: str | None = None) -> "LifecycleJournal":
        if phase not in _TRANSITIONS.get(self.phase, frozenset()):
            raise ValueError(f"invalid lifecycle phase transition: {self.phase} -> {phase}")
        return replace(self, phase=phase, error=error)


class LifecycleOperationError(RuntimeError):
    def __init__(self, message: str, *, journal_path: Path, compensated: bool) -> None:
        super().__init__(message)
        self.journal_path = journal_path
        self.compensated = compensated


def lifecycle_root(project_root: str | Path, scheme_id: str) -> Path:
    return (
        Path(project_root)
        / "backtest_artifacts"
        / "blackbox_v2_lifecycle"
        / scheme_id
    )


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
        token_hash=str(raw["token_hash"]),
        error=str(raw["error"]) if raw.get("error") is not None else None,
    )
    if journal.phase not in _TRANSITIONS:
        raise ValueError(f"invalid lifecycle journal phase: {journal.phase}")
    return journal


def pending_journals(project_root: str | Path, scheme_id: str) -> list[tuple[Path, LifecycleJournal]]:
    root = lifecycle_root(project_root, scheme_id)
    if not root.is_dir():
        return []
    pending: list[tuple[Path, LifecycleJournal]] = []
    for path in sorted(root.glob("*.json")):
        try:
            journal = load_journal(path)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"invalid lifecycle journal blocks {scheme_id}: {path}: {exc}") from exc
        if journal.phase in INCOMPLETE_PHASES:
            pending.append((path, journal))
    return pending


def assert_lifecycle_clear(project_root: str | Path, scheme_id: str) -> None:
    pending = pending_journals(project_root, scheme_id)
    if not pending:
        return
    details = ", ".join(f"{journal.operation_id}:{journal.phase}" for _, journal in pending)
    unresolved = any(journal.phase == "unresolved" for _, journal in pending)
    label = "unresolved lifecycle journal" if unresolved else "incomplete lifecycle journal"
    raise RuntimeError(f"{label} blocks {scheme_id}: {details}")


def atomic_update_config(
    config_path: str | Path,
    state: LifecycleState,
    *,
    original_text: str | None = None,
) -> None:
    path = Path(config_path)
    text = original_text if original_text is not None else path.read_text(encoding="utf-8")
    text = _replace_top_level_scalar(text, "status", state.config_status)
    text = _replace_top_level_scalar(text, "version_status", state.version_status)
    _atomic_write_bytes(path, text.encode("utf-8"))


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
    token_hash: str,
    consume_authorization: Callable[[], None],
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
            token_hash=token_hash,
            consume_authorization=consume_authorization,
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
    token_hash: str,
    consume_authorization: Callable[[], None],
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
) -> tuple[LifecycleState, Path]:
    assert_lifecycle_clear(project_root, scheme_id)
    config_path = Path(config_path)
    original_text = config_path.read_text(encoding="utf-8")
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
        token_hash=token_hash,
    )
    journal_path = write_journal(project_root, journal)
    try:
        consume_authorization()
        atomic_update_config(config_path, target)
        journal = journal.transition("config_written")
        write_journal(project_root, journal)
        apply_database(target)
        journal = journal.transition("db_committed")
        write_journal(project_root, journal)
        actual = read_state()
        if actual != target:
            raise RuntimeError(f"lifecycle independent verification mismatch: expected={target}, actual={actual}")
        journal = journal.transition("verified")
        write_journal(project_root, journal)
        return actual, journal_path
    except BaseException as exc:
        compensation_errors: list[str] = []
        try:
            if compensation == previous:
                # Restore the exact original text for Shadow/normal Activation rollback.
                _atomic_write_bytes(config_path, original_text.encode("utf-8"))
            else:
                atomic_update_config(config_path, compensation)
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
    config_path: str | Path,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
) -> LifecycleState:
    """只回退到 previous 安全状态；绝不把中断操作继续推进到 active。"""
    path = Path(journal_path)
    journal = load_journal(path)
    project_root = _project_root_from_journal_path(path, journal.scheme_id)
    with lifecycle_operation_lock(project_root, journal.scheme_id):
        return _reconcile_journal_unlocked(
            path,
            config_path=config_path,
            apply_database=apply_database,
            read_state=read_state,
        )


def _reconcile_journal_unlocked(
    path: Path,
    *,
    config_path: str | Path,
    apply_database: Callable[[LifecycleState], None],
    read_state: Callable[[], LifecycleState],
) -> LifecycleState:
    journal = load_journal(path)
    if journal.phase in TERMINAL_PHASES:
        return journal.previous if journal.phase == "compensated" else journal.target
    errors: list[str] = []
    try:
        atomic_update_config(config_path, journal.previous)
    except BaseException as exc:
        errors.append(f"config reconciliation failed: {exc}")
    try:
        apply_database(journal.previous)
    except BaseException as exc:
        errors.append(f"database reconciliation failed: {exc}")
    try:
        actual = read_state()
        if actual != journal.previous:
            errors.append(
                f"reconciliation verification mismatch: expected={journal.previous}, actual={actual}"
            )
    except BaseException as exc:
        errors.append(f"reconciliation verification failed: {exc}")
    if errors:
        updated = replace(journal, phase="unresolved", error="; ".join(errors))
        _atomic_write_journal_path(path, updated)
        raise RuntimeError(updated.error)
    updated = replace(journal, phase="compensated", error=journal.error)
    _atomic_write_journal_path(path, updated)
    return journal.previous


@contextmanager
def lifecycle_operation_lock(project_root: str | Path, scheme_id: str):
    root = lifecycle_root(project_root, scheme_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
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


def _atomic_write_journal_path(path: Path, journal: LifecycleJournal) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(asdict(journal), ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _replace_top_level_scalar(text: str, key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{key}: {value}{newline}"
            return "".join(lines)
    suffix = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{suffix}{key}: {value}\n"


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
