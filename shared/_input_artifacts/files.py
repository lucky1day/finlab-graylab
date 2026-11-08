"""不可变快照的安全读取、身份校验及文件清理原语。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Mapping,
)
import pandas as pd

from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxSnapshot,
)
from shared.data_bridge.validation import LEGACY_FOUR_FILENAMES


@dataclass(frozen=True)
class _StableSnapshotFile:
    path: Path
    fingerprint: tuple[int, ...]
    sha256: str
    content_bytes: bytes


def _validate_blackbox_snapshot(
    snapshot: BlackboxSnapshot,
    *,
    validate_csv_profiles: bool = True,
    validate_file_contents: bool = True,
) -> tuple[dict[str, Any], dict[str, _StableSnapshotFile]]:
    if validate_csv_profiles and not validate_file_contents:
        raise ValueError(
            "CSV profile validation requires snapshot content validation"
        )
    root = snapshot.root_dir
    if (
        snapshot.data_dir != root / "data"
        or snapshot.manifest_path != root / "manifest.json"
    ):
        raise ValueError("parent snapshot path geometry is invalid")
    _require_directory(root, "parent snapshot root")
    _require_directory(snapshot.data_dir, "parent snapshot data directory")
    try:
        _, manifest_raw = _read_stable_regular_file(
            snapshot.manifest_path,
            "parent snapshot manifest",
        )
        manifest = json.loads(manifest_raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("parent snapshot manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("parent snapshot manifest must be an object")
    schema_version = manifest.get("schema_version")
    if (
        not isinstance(schema_version, str)
        or not schema_version
        or schema_version != snapshot.schema_version
    ):
        raise ValueError("parent snapshot schema_version mismatch")
    entries = manifest.get("files")
    allowed_file_sets = {
        frozenset(SNAPSHOT_FILENAMES),
        frozenset(LEGACY_FOUR_FILENAMES),
    }
    if not isinstance(entries, dict) or frozenset(entries) not in allowed_file_sets:
        raise ValueError("parent snapshot manifest files are invalid")
    snapshot_filenames = tuple(entries)

    has_simple_identity = "data_snapshot_id" in manifest
    has_databridge_identity = "dataset_content_id" in manifest
    if has_simple_identity == has_databridge_identity:
        raise ValueError(
            "parent snapshot manifest must use exactly one identity shape"
        )
    if has_simple_identity:
        entry_fields = {"sha256", "row_count", "columns"}
        identity_kind = "simple"
    elif "dataset_content_id" in manifest:
        entry_fields = {
            "path",
            "sha256",
            "size_bytes",
            "row_count",
            "columns",
        }
        identity_kind = "databridge"
    else:
        raise ValueError("parent snapshot manifest identity is missing")

    source_states: dict[str, _StableSnapshotFile] = {}
    try:
        actual_filenames = {
            path.name
            for path in snapshot.data_dir.iterdir()
        }
    except OSError as exc:
        raise ValueError("parent snapshot data directory is unreadable") from exc
    if actual_filenames != set(snapshot_filenames):
        raise ValueError("parent snapshot data files do not match manifest")

    for filename in snapshot_filenames:
        entry = entries.get(filename)
        if not isinstance(entry, dict) or set(entry) != entry_fields:
            raise ValueError(
                f"parent snapshot manifest entry is invalid: {filename}"
            )
        sha256 = entry.get("sha256")
        row_count = entry.get("row_count")
        columns = entry.get("columns")
        if not _is_sha256(sha256):
            raise ValueError(f"{filename} manifest sha256 is invalid")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise ValueError(f"{filename} manifest row_count is invalid")
        if (
            not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, str) or not column
                for column in columns
            )
            or len(columns) != len(set(columns))
        ):
            raise ValueError(f"{filename} manifest columns are invalid")
        if identity_kind == "databridge":
            if entry.get("path") != f"data/{filename}":
                raise ValueError(f"{filename} manifest path is invalid")
            size_bytes = entry.get("size_bytes")
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 0
            ):
                raise ValueError(f"{filename} manifest size_bytes is invalid")

        path = snapshot.data_dir / filename
        if validate_file_contents:
            state, raw = _read_stable_regular_file(
                path,
                f"parent snapshot {filename}",
            )
            if state.sha256 != sha256:
                raise ValueError(f"{filename} sha256 does not match manifest")
            if (
                identity_kind == "databridge"
                and len(raw) != entry["size_bytes"]
            ):
                raise ValueError(f"{filename} size does not match manifest")
            if validate_csv_profiles:
                actual_columns, actual_rows = _csv_profile(raw, filename)
                if actual_columns != columns:
                    raise ValueError(f"{filename} columns do not match manifest")
                if actual_rows != row_count:
                    raise ValueError(f"{filename} row_count does not match manifest")
            source_states[filename] = state
        else:
            _require_regular_file(path, f"parent snapshot {filename}")
            if (
                identity_kind == "databridge"
                and path.lstat().st_size != entry["size_bytes"]
            ):
                raise ValueError(f"{filename} size does not match manifest")

    identity = {
        "schema_version": schema_version,
        "files": entries,
    }
    content_id = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    expected_snapshot_id = f"snapshot-{content_id[:24]}"
    if identity_kind == "simple":
        if manifest.get("data_snapshot_id") != expected_snapshot_id:
            raise ValueError("parent snapshot simple identity mismatch")
    elif manifest.get("dataset_content_id") != content_id:
        raise ValueError("parent snapshot DataBridge identity mismatch")
    if snapshot.snapshot_id != expected_snapshot_id:
        raise ValueError("parent snapshot object identity mismatch")
    return entries, source_states


def _read_stable_regular_file(
    path: Path,
    label: str,
) -> tuple[_StableSnapshotFile, bytes]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be a readable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    fingerprint = _stat_fingerprint(after)
    if _stat_fingerprint(before) != fingerprint:
        raise ValueError(f"{label} changed while being read")
    try:
        path_state = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} changed while being read") from exc
    if (
        stat.S_ISLNK(path_state.st_mode)
        or not stat.S_ISREG(path_state.st_mode)
        or _stat_fingerprint(path_state) != fingerprint
    ):
        raise ValueError(f"{label} changed while being read")
    return (
        _StableSnapshotFile(
            path=path,
            fingerprint=fingerprint,
            sha256=hashlib.sha256(raw).hexdigest(),
            content_bytes=raw,
        ),
        raw,
    )


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _csv_profile(raw: bytes, filename: str) -> tuple[list[str], int]:
    try:
        frame = pd.read_csv(io.BytesIO(raw))
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise ValueError(f"{filename} is not a valid CSV") from exc
    return [str(column) for column in frame.columns], int(len(frame))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a non-symlink directory")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular non-symlink file")


def _snapshot_file_fingerprints(
    snapshot: BlackboxSnapshot,
) -> Mapping[str, tuple[int, ...]]:
    fingerprints: dict[str, tuple[int, ...]] = {}
    filenames = tuple(
        sorted(path.name for path in snapshot.data_dir.iterdir())
    )
    if frozenset(filenames) not in {
        frozenset(SNAPSHOT_FILENAMES),
        frozenset(LEGACY_FOUR_FILENAMES),
    }:
        raise ValueError("producer snapshot file set is invalid")
    for filename in filenames:
        path = snapshot.data_dir / filename
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o222
        ):
            raise ValueError(
                f"producer snapshot file is not sealed: {filename}"
            )
        fingerprints[filename] = _stat_fingerprint(info)
    return MappingProxyType(fingerprints)


def _make_tree_writable(root: Path) -> None:
    try:
        root_mode = root.lstat().st_mode
    except OSError:
        return
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ValueError("cleanup root must be a non-symlink directory")
    for directory, directory_names, filenames in os.walk(
        root,
        topdown=False,
        followlinks=False,
    ):
        directory_path = Path(directory)
        for name in filenames + directory_names:
            path = directory_path / name
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    path.chmod(0o755)
                elif stat.S_ISREG(mode):
                    path.chmod(0o644)
            except OSError:
                pass
    try:
        root.chmod(0o755)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
