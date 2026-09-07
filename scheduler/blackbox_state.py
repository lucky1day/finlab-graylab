"""Blackbox 私有派生状态：只管理完整性与发布，不解释算法依赖。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.exclusive_file_lock import ExclusiveFileLock


MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
_MAGIC = b"BFL-BLACKBOX-STATE-1\n"
_MAX_ENVELOPE_BYTES = MAX_STATE_BYTES + MAX_HEADER_BYTES + len(_MAGIC) + 40


@dataclass(frozen=True)
class StateBinding:
    """由平台调用者提供的 exact 身份；root 缺省时只使用本次私有状态。"""

    scheme_id: str
    scheme_version: str
    generation_id: str
    root: Path | None = None
    rebuild: bool = False
    code_hash: str | None = None
    manifest_hash: str | None = None
    config_path: Path | None = None

    def __post_init__(self) -> None:
        for label, value in (("scheme_id", self.scheme_id),
                             ("scheme_version", self.scheme_version),
                             ("generation_id", self.generation_id)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
                raise ValueError(f"invalid Blackbox state {label}")
        if type(self.rebuild) is not bool or (self.rebuild and self.root is None):
            raise ValueError("state rebuild requires an explicit persistent root")
        for value in (self.code_hash, self.manifest_hash):
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("invalid Blackbox state code/manifest hash")


def read_regular_bytes(path: Path, limit: int) -> bytes:
    """通过同一个 no-follow 文件句柄读取有界单链接常规文件。"""
    path = Path(os.path.abspath(path))
    aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
    for parent in path.parents:
        if parent.is_symlink() and aliases.get(parent) != parent.resolve(strict=True):
            raise ValueError("Blackbox state path traverses a symlink")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > limit):
            raise ValueError(f"invalid or oversized Blackbox state file: {path}")
        content = source.read(limit + 1)
        after = os.fstat(source.fileno())
        current = path.lstat()
        fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                    item.st_mtime_ns, item.st_ctime_ns, item.st_nlink)
        if (len(content) != before.st_size or fingerprint(before) != fingerprint(after)
                or fingerprint(before) != fingerprint(current)):
            raise ValueError("Blackbox state file changed during read")
        return content


def _encode(header: dict[str, Any], payload: bytes) -> bytes:
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode()
    if len(encoded) > MAX_HEADER_BYTES or not 0 < len(payload) <= MAX_STATE_BYTES:
        raise ValueError("Blackbox state envelope exceeds size limit")
    body = _MAGIC + len(encoded).to_bytes(8, "big") + encoded + payload
    return body + hashlib.sha256(body).digest()


def _decode(content: bytes) -> tuple[dict[str, Any], bytes]:
    if not content.startswith(_MAGIC) or len(content) < len(_MAGIC) + 41:
        raise ValueError("invalid Blackbox state envelope")
    body, checksum = content[:-32], content[-32:]
    if hashlib.sha256(body).digest() != checksum:
        raise ValueError("Blackbox state envelope checksum mismatch")
    start = len(_MAGIC) + 8
    length = int.from_bytes(content[len(_MAGIC):start], "big")
    if not 0 < length <= MAX_HEADER_BYTES or start + length >= len(body):
        raise ValueError("invalid Blackbox state header size")
    header = json.loads(body[start:start + length])
    payload = body[start + length:]
    if (not isinstance(header, dict)
            or set(header) != {"identity", "input", "payload_sha256"}
            or header["payload_sha256"] != hashlib.sha256(payload).hexdigest()
            or _encode(header, payload) != content):
        raise ValueError("invalid Blackbox state header or payload")
    return header, payload


def _private_directory(path: Path) -> tuple[int, int]:
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ValueError("Blackbox state directory must be absolute and symlink-free")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError("Blackbox state directory must be private and service-owned")
    return info.st_dev, info.st_ino


class StateSession:
    """一次执行持有方案锁；只有 Result 校验成功后调用 publish。"""

    def __init__(self, binding: StateBinding, *, work_dir: Path,
                 identity: dict[str, str], input_identity: dict[str, Any]) -> None:
        self.binding = binding
        self.identity = {**identity, "scheme_id": binding.scheme_id,
                         "scheme_version": binding.scheme_version}
        self.input_identity = {**input_identity, "generation_id": binding.generation_id}
        self.input_path: Path | None = None
        self.output_path = work_dir / "output" / "state.bin"
        self._work_dir = work_dir
        self._lock: ExclusiveFileLock | None = None
        self._destination: Path | None = None
        self._parent_identity: tuple[int, int] | None = None
        self._old: bytes | None = None
        self._input: bytes | None = None
        self.audit: dict[str, Any] = {}

    def __enter__(self) -> StateSession:
        try:
            if self.binding.root is not None:
                _private_directory(self.binding.root)
                parent = self.binding.root / self.binding.scheme_id
                self._parent_identity = _private_directory(parent)
                self._lock = ExclusiveFileLock(parent / "state.lock").acquire()
                self._destination = parent / f"{self.binding.scheme_version}.state"
                if os.path.lexists(self._destination):
                    self._old = read_regular_bytes(self._destination, _MAX_ENVELOPE_BYTES)
                if not self.binding.rebuild:
                    if self._old is None:
                        raise ValueError("Blackbox state missing; explicit rebuild required")
                    header, self._input = _decode(self._old)
                    if header["identity"] != self.identity:
                        raise ValueError("Blackbox state exact identity mismatch; rebuild required")
                    if (not isinstance(header["input"], dict)
                            or header["input"].get("schema") != self.input_identity.get("schema")):
                        raise ValueError("Blackbox state input schema mismatch")
                    self.input_path = self._work_dir / "state-input.bin"
                    with self.input_path.open("xb") as target:
                        target.write(self._input)
                    self.input_path.chmod(0o400)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def publish(self) -> dict[str, Any]:
        """验证旧状态未变并发布单文件封装；不写预测数据库。"""
        if self.input_path is not None:
            if read_regular_bytes(self.input_path, MAX_STATE_BYTES) != self._input:
                raise ValueError("Blackbox state input changed during execution")
        payload = read_regular_bytes(self.output_path, MAX_STATE_BYTES)
        header = {"identity": self.identity, "input": self.input_identity,
                  "payload_sha256": hashlib.sha256(payload).hexdigest()}
        content = _encode(header, payload)
        _decode(content)
        if self._destination is not None:
            self._publish_file(content)
        self.audit = {
            "state_schema": "blackbox-state-1",
            "state_input_sha256": hashlib.sha256(self._input).hexdigest() if self._input is not None else None,
            "state_output_sha256": header["payload_sha256"],
            "state_envelope_sha256": hashlib.sha256(content).hexdigest(),
            "state_bytes": len(payload),
            "state_scope": "persistent" if self._destination is not None else "private",
        }
        return dict(self.audit)

    def _publish_file(self, content: bytes) -> None:
        destination = self._destination
        assert destination is not None
        assert self._lock is not None
        self._lock.verify()
        parent = destination.parent
        if _private_directory(parent) != self._parent_identity:
            raise ValueError("Blackbox state directory changed during execution")
        previous = (read_regular_bytes(destination, _MAX_ENVELOPE_BYTES)
                    if os.path.lexists(destination) else None)
        if previous != self._old:
            raise ValueError("Blackbox canonical state changed during execution")
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary: Path | None = None
        try:
            info = os.fstat(directory_fd)
            if (info.st_dev, info.st_ino) != self._parent_identity:
                raise ValueError("Blackbox state publication directory changed")
            fd, name = tempfile.mkstemp(prefix=".state-", dir=parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            if read_regular_bytes(temporary, _MAX_ENVELOPE_BYTES) != content:
                raise ValueError("Blackbox staged state verification failed")
            if _private_directory(parent) != self._parent_identity:
                raise ValueError("Blackbox state publication directory changed")
            self._lock.verify()
            os.replace(temporary.name, destination.name,
                       src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = None
            os.fsync(directory_fd)
        finally:
            if temporary is not None:
                os.unlink(temporary.name, dir_fd=directory_fd)
            os.close(directory_fd)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock is not None:
            self._lock.release()
