"""DataBridge private-directory, fsync and no-follow file helpers; re-exported by refresh.py."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from shared.data_bridge.errors import (
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeRefreshError,
)
from shared.data_bridge.validation import EXPECTED_FILENAMES


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DataBridgeRefreshError(
                "DataBridge fsync target is not a regular file"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_private_directory(
    path: Path,
    *,
    label: str,
    error_type: type[DataBridgeRefreshError] = DataBridgeRefreshError,
    missing_error_type: (
        type[DataBridgeRefreshError] | None
    ) = None,
) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise (missing_error_type or error_type)(
            f"{label} is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise error_type(f"{label} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise error_type(f"{label} must be a directory")
    if info.st_uid != os.getuid():
        raise error_type(f"{label} has another owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise error_type(
            f"{label} must be private (mode 0700 or stricter)"
        )


def _open_strict_read_lock(path: Path):
    """原子、无跟随地打开既有 lock，不创建任何目录或文件。"""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise DataBridgeCurrentMissingError(
            "DataBridge refresh lock is missing"
        ) from exc
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh lock is invalid"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise DataBridgeCurrentInvalidError(
                "DataBridge refresh lock is unsafe"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _read_strict_current_state(path: Path) -> dict[str, object]:
    """无跟随读取既有 state，缺失与损坏在 shared lock 内分类。"""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise DataBridgeCurrentMissingError(
            "DataBridge refresh state does not exist"
        ) from exc
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise DataBridgeCurrentInvalidError(
                    "DataBridge refresh state is not a regular file"
                )
            raw = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    if len(raw) > 4 * 1024 * 1024:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is too large"
        )
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    if not isinstance(state, dict):
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state must be an object"
        )
    return state


def ensure_private_directory(path: Path, *, label: str) -> None:
    """只创建新私有根；既有非私有根必须由部署迁移显式修复。"""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_directory(path, label=label)


def _validate_publish_candidate(path: Path) -> None:
    try:
        root_info = path.lstat()
    except FileNotFoundError as exc:
        raise DataBridgeRefreshError(
            "candidate directory does not exist"
        ) from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise DataBridgeRefreshError(
            "candidate directory must not be a symlink"
        )
    if not stat.S_ISDIR(root_info.st_mode):
        raise DataBridgeRefreshError("candidate must be a directory")
    entries = list(path.iterdir())
    if {entry.name for entry in entries} != set(EXPECTED_FILENAMES):
        raise DataBridgeRefreshError(
            "candidate directory does not contain exactly five data files"
        )
    for entry in entries:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise DataBridgeRefreshError(
                f"candidate file must not be a symlink: {entry.name}"
            )
        if not stat.S_ISREG(info.st_mode):
            raise DataBridgeRefreshError(
                f"candidate file must be regular: {entry.name}"
            )


def _copy_regular_file_no_follow(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> str:
    """以 O_NOFOLLOW 复制候选文件，并拒绝复制期间身份漂移。"""
    source_path = Path(source)
    destination_path = Path(destination)
    before = source_path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DataBridgeRefreshError(
            f"candidate file must be regular and non-symlink: "
            f"{source_path.name}"
        )
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise DataBridgeRefreshError(
                f"candidate file changed while opening: {source_path.name}"
            )
        destination_descriptor = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    after = source_path.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise DataBridgeRefreshError(
            f"candidate file changed while copying: {source_path.name}"
        )
    return str(destination_path)
