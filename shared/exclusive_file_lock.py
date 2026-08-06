"""机器级非阻塞排它文件锁。"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path


class ExclusiveFileLockUnavailable(RuntimeError):
    """锁已由另一个进程持有。"""


class ExclusiveFileLockPathChanged(RuntimeError):
    """锁目录在校验后发生替换。"""


class ExclusiveFileLock:
    """基于 ``fcntl.flock`` 的 inode-checked 非阻塞排它锁。

    锁文件是稳定 fence 文件，释放时仅解锁和关闭描述符，绝不 unlink。
    """

    def __init__(self, path: str | Path) -> None:
        self._fd: int | None = None
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("exclusive lock path must be absolute")
        if candidate.suffix != ".lock":
            raise ValueError("exclusive lock path must end with .lock")
        if candidate != candidate.resolve(strict=False):
            raise ValueError(
                "exclusive lock path must be normalized and symlink-free"
            )
        if not candidate.parent.is_dir():
            raise ValueError(
                "exclusive lock parent must be an existing directory"
            )
        parent_details = os.stat(candidate.parent, follow_symlinks=False)
        _validate_parent_security(parent_details)
        self._path = candidate
        self._parent_identity = (
            parent_details.st_dev,
            parent_details.st_ino,
        )

    @property
    def path(self) -> Path:
        """返回已校验的锁路径。"""
        return self._path

    @property
    def acquired(self) -> bool:
        """当前实例是否持有锁。"""
        return self._fd is not None

    def acquire(self) -> "ExclusiveFileLock":
        """非阻塞获取独占锁；已持有时立即抛出。"""
        if self._fd is not None:
            return self
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_flags = os.O_RDONLY
        parent_flags |= getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd: int | None = None
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
            parent_details = os.fstat(parent_fd)
            try:
                _validate_parent_security(parent_details)
            except ValueError as exc:
                raise ExclusiveFileLockPathChanged(
                    "exclusive lock parent became unsafe: "
                    f"{self._path.parent}"
                ) from exc
            if (
                parent_details.st_dev,
                parent_details.st_ino,
            ) != self._parent_identity:
                raise ExclusiveFileLockPathChanged(
                    f"exclusive lock parent changed: {self._path.parent}"
                )
            fd = os.open(
                self._path.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if parent_fd is not None:
                os.close(parent_fd)
            raise ExclusiveFileLockPathChanged(
                "cannot safely resolve exclusive lock parent: "
                f"{self._path}"
            ) from exc
        except BaseException:
            if parent_fd is not None:
                os.close(parent_fd)
            raise
        try:
            current_parent = os.stat(
                self._path.parent,
                follow_symlinks=False,
            )
            try:
                _validate_parent_security(current_parent)
            except ValueError as exc:
                raise ExclusiveFileLockPathChanged(
                    "exclusive lock parent became unsafe: "
                    f"{self._path.parent}"
                ) from exc
            if (
                current_parent.st_dev,
                current_parent.st_ino,
            ) != self._parent_identity:
                raise ExclusiveFileLockPathChanged(
                    f"exclusive lock parent changed: {self._path.parent}"
                )
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("exclusive lock must be a regular file")
            if details.st_uid != os.getuid():
                raise ValueError(
                    "exclusive lock must be owned by the service user"
                )
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise ExclusiveFileLockUnavailable(
                    f"exclusive lock is already held: {self._path}"
                ) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        self._fd = fd
        return self

    def release(self) -> None:
        """释放锁但保留稳定 fence 文件。"""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


def _validate_parent_security(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("exclusive lock parent must be a real directory")
    if details.st_uid != os.getuid():
        raise ValueError(
            "exclusive lock parent must be owned by the service user"
        )
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(
            "exclusive lock parent cannot be group/world writable"
        )
