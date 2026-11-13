"""算法进程的独立只读输入视图与受控清理。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxInputBundle,
    compose_blackbox_input_bundle,
)
from shared.data_bridge.raw_wind import MANIFEST_FILENAME, RAW_WIND_DATA_FILENAMES, RAW_WIND_TABLES, RawWindError, read_raw_wind_manifest
from shared.data_bridge.validation import LEGACY_FOUR_FILENAMES
from shared._input_artifacts.contract import BLACKBOX_RUNTIME_VIEW_ROOT
from shared._input_artifacts.files import (
    _make_tree_writable,
    _read_stable_regular_file,
    _stat_fingerprint,
    _validate_blackbox_snapshot,
)


_BLACKBOX_RUNTIME_VIEW_PREFIX = "blackbox-runtime-"


_BLACKBOX_DEBRIS_MARKER_SCHEMA = "blackbox-runtime-debris-v1"
_logger = logging.getLogger(__name__)


@dataclass
class BlackboxRuntimeView:
    """一次子进程可见的私有只读输入目录。"""

    bundle: BlackboxInputBundle
    data_dir: Path
    debris_path: Path | None = None
    _termination_uncertain: bool = False
    _closed: bool = False

    def mark_termination_uncertain(self) -> None:
        """标记子进程可能仍读取目录，退出时转入受控 debris。"""
        if self._closed:
            raise RuntimeError("runtime view is already closed")
        self._termination_uncertain = True


@contextmanager
def open_blackbox_runtime_view(
    bundle: BlackboxInputBundle,
    *,
    runtime_root: str | Path = BLACKBOX_RUNTIME_VIEW_ROOT,
):
    """物化一次私有、精确、只读的组合输入视图并负责清理。"""
    if not isinstance(bundle, BlackboxInputBundle):
        raise ValueError("bundle must be a BlackboxInputBundle")
    trusted_bundle = _validate_trusted_blackbox_input_bundle(bundle)
    root = Path(runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    active_root = root / "active"
    debris_root = root / "debris"
    _ensure_private_runtime_directory(active_root)
    _ensure_private_runtime_directory(debris_root)
    temporary = Path(
        tempfile.mkdtemp(prefix=_BLACKBOX_RUNTIME_VIEW_PREFIX, dir=active_root)
    )
    view = BlackboxRuntimeView(bundle=trusted_bundle, data_dir=temporary)
    try:
        _materialize_blackbox_runtime_view(view)
        yield view
    except BaseException as execution_error:
        try:
            _finalize_blackbox_runtime_view(view, runtime_root=root)
        except BaseException as cleanup_error:
            execution_error.add_note(
                f"Blackbox runtime view cleanup failed for {view.data_dir}: {cleanup_error}"
            )
        raise
    else:
        _finalize_blackbox_runtime_view(view, runtime_root=root)


def cleanup_blackbox_runtime_debris(
    debris_path: str | Path,
    *,
    runtime_root: str | Path = BLACKBOX_RUNTIME_VIEW_ROOT,
) -> None:
    """删除一个经过路径边界校验的受控 runtime debris 目录。"""
    root = Path(runtime_root).resolve()
    active_root = root / "active"
    debris_root = root / "debris"
    candidate = Path(debris_path)
    if (
        not candidate.is_absolute()
        or candidate.name in {"", ".", ".."}
        or not candidate.name.startswith(_BLACKBOX_RUNTIME_VIEW_PREFIX)
    ):
        raise ValueError("invalid Blackbox runtime debris path")
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_active_root = active_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Blackbox runtime debris path does not exist") from exc
    if resolved_parent != resolved_active_root or candidate.is_symlink():
        raise ValueError("Blackbox runtime debris path is outside controlled root")
    if not candidate.is_dir():
        raise ValueError("Blackbox runtime debris path is not a directory")
    marker_path = _blackbox_debris_marker_path(
        debris_root,
        candidate.name,
    )
    marker = _read_blackbox_debris_marker(marker_path)
    if marker.get("active_path") != str(candidate):
        raise ValueError("Blackbox runtime debris marker path mismatch")
    _remove_runtime_view(
        candidate,
        controlled_parent=active_root,
    )
    marker_path.unlink()


def _materialize_blackbox_runtime_view(view: BlackboxRuntimeView) -> None:
    bundle = view.bundle
    destination = view.data_dir
    sealed_fingerprints = bundle.base_snapshot.sealed_file_fingerprints
    standard_filenames = tuple(
        name for name in bundle.expected_filenames if name not in RAW_WIND_DATA_FILENAMES
    )
    if sealed_fingerprints is None:
        _validate_blackbox_snapshot(bundle.base_snapshot)
    elif not set(standard_filenames).issubset(sealed_fingerprints):
        raise ValueError("producer snapshot file seal is invalid")

    for filename in standard_filenames:
        source = bundle.base_snapshot.data_dir / filename
        target = destination / filename
        source_fingerprint = _copy_ready_snapshot_file(
            source,
            target,
            label=f"runtime view {filename}",
            expected_fingerprint=(
                sealed_fingerprints[filename]
                if sealed_fingerprints is not None
                else None
            ),
        )
        target_stat = target.lstat()
        if target_stat.st_nlink != 1:
            raise ValueError(f"runtime view {filename} must not be a hardlink")
        if (
            source_fingerprint[0] == target_stat.st_dev
            and source_fingerprint[1] == target_stat.st_ino
        ):
            raise ValueError(f"runtime view {filename} must be an independent file")
        try:
            source_after = source.lstat()
        except OSError as exc:
            raise ValueError(
                f"parent snapshot {filename} changed during materialization"
            ) from exc
        if (
            stat.S_ISLNK(source_after.st_mode)
            or not stat.S_ISREG(source_after.st_mode)
            or _stat_fingerprint(source_after)
            != source_fingerprint
        ):
            raise ValueError(
                f"parent snapshot {filename} changed during materialization"
            )

    if bundle.raw_wind is not None:
        _copy_raw_wind_files(bundle, destination)

    actual_filenames = tuple(
        sorted(path.name for path in destination.iterdir())
    )
    if actual_filenames != tuple(sorted(bundle.expected_filenames)):
        raise ValueError(
            "runtime view files do not match bundle expected_filenames"
        )
    for filename in bundle.expected_filenames:
        (destination / filename).chmod(0o444)
    destination.chmod(0o555)


def _copy_raw_wind_files(bundle: BlackboxInputBundle, destination: Path) -> None:
    """复制原始源表旁路文件：边复制边按 manifest 校验 sha256 与字节数，目标必须是独立的普通文件。"""
    raw = bundle.raw_wind
    try:
        manifest = read_raw_wind_manifest(raw.path)
    except RawWindError as exc:
        raise ValueError(f"raw wind artifact is invalid: {exc}") from exc
    if dict(manifest) != dict(raw.manifest) or manifest["artifact_id"] != raw.artifact_id:
        raise ValueError("raw wind artifact does not match the bundle composition")
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for table in RAW_WIND_TABLES:
        info = manifest["tables"][table.name]
        source, target = raw.path / info["file"], destination / info["file"]
        digest, size = hashlib.sha256(), 0
        source_fd = os.open(source, read_flags)
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError(f"raw wind file is not a regular file: {info['file']}")
            target_fd = os.open(target, write_flags, 0o600)
            try:
                while block := os.read(source_fd, 1 << 20):
                    digest.update(block)
                    size += len(block)
                    os.write(target_fd, block)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
        if digest.hexdigest() != info["sha256"] or size != info["bytes"]:
            raise ValueError(f"raw wind file does not match its manifest: {info['file']}")
        target_stat = target.lstat()
        if target_stat.st_nlink != 1 or (target_stat.st_dev, target_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
            raise ValueError(f"runtime view {info['file']} must be an independent file")
    # manifest 一并交给算法进程：读取方据此还原列类型并校验各文件哈希；内容必须与已校验的 manifest 完全一致
    manifest_payload = (raw.path / MANIFEST_FILENAME).read_bytes()
    if json.loads(manifest_payload) != dict(manifest):
        raise ValueError("raw wind manifest changed during materialization")
    manifest_fd = os.open(destination / MANIFEST_FILENAME, write_flags, 0o600)
    try:
        os.write(manifest_fd, manifest_payload)
        os.fsync(manifest_fd)
    finally:
        os.close(manifest_fd)


def _copy_ready_snapshot_file(
    source: Path,
    target: Path,
    *,
    label: str,
    expected_fingerprint: tuple[int, ...] | None,
) -> tuple[int, ...]:
    """稳定复制 producer 已封存的文件，不重复哈希或解析内容。"""
    read_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_descriptor = os.open(source, read_flags)
    except OSError as exc:
        raise ValueError(f"{label} source is unavailable") from exc
    try:
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            raise ValueError(f"{label} source must be a regular file")
        if (
            expected_fingerprint is not None
            and _stat_fingerprint(source_before) != expected_fingerprint
        ):
            raise ValueError(f"{label} source no longer matches producer seal")
        try:
            target_descriptor = os.open(target, write_flags, 0o600)
        except OSError as exc:
            raise ValueError(f"{label} could not be created safely") from exc
        try:
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    view = view[written:]
            target_state = os.fstat(target_descriptor)
        finally:
            os.close(target_descriptor)
        source_after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    source_fingerprint = _stat_fingerprint(source_after)
    if _stat_fingerprint(source_before) != source_fingerprint:
        raise ValueError(f"{label} source changed while being copied")
    source_path_state = source.lstat()
    if (
        stat.S_ISLNK(source_path_state.st_mode)
        or not stat.S_ISREG(source_path_state.st_mode)
        or _stat_fingerprint(source_path_state) != source_fingerprint
    ):
        raise ValueError(f"{label} source changed while being copied")
    if (
        not stat.S_ISREG(target_state.st_mode)
        or target_state.st_nlink != 1
        or target_state.st_size != source_after.st_size
    ):
        raise ValueError(f"{label} copy is incomplete")
    return source_fingerprint


def _validate_trusted_blackbox_input_bundle(
    bundle: BlackboxInputBundle,
) -> BlackboxInputBundle:
    """从受信字段重算 bundle，拒绝 replace/手工构造的派生字段。"""
    try:
        standard_filenames = tuple(
            name for name in bundle.expected_filenames if name not in RAW_WIND_DATA_FILENAMES
        )
        if standard_filenames == LEGACY_FOUR_FILENAMES:
            factor_input_mode = "legacy_v1"
        elif standard_filenames == SNAPSHOT_FILENAMES:
            factor_input_mode = "algorithm_managed"
        else:
            raise ValueError("input bundle file set is invalid")
        trusted = compose_blackbox_input_bundle(
            bundle.base_snapshot,
            factor_input_mode=factor_input_mode,
            input_profile=bundle.input_profile,
            raw_wind=bundle.raw_wind,
        )
        matches = (
            bundle.combined_snapshot_id == trusted.combined_snapshot_id
            and bundle.parent_snapshot_id == trusted.parent_snapshot_id
            and bundle.expected_filenames == trusted.expected_filenames
            and bundle.input_profile == trusted.input_profile
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Blackbox input bundle does not match trusted composition"
        ) from exc
    if not matches:
        raise ValueError(
            "Blackbox input bundle does not match trusted composition"
        )
    return trusted


def _finalize_blackbox_runtime_view(
    view: BlackboxRuntimeView,
    *,
    runtime_root: Path,
) -> None:
    try:
        if view._termination_uncertain:
            _register_runtime_view_debris(view, runtime_root=runtime_root)
        else:
            try:
                _remove_runtime_view(
                    view.data_dir,
                    controlled_parent=runtime_root / "active",
                )
            except BaseException as cleanup_error:
                try:
                    _register_runtime_view_debris(view, runtime_root=runtime_root)
                except BaseException as registration_error:
                    cleanup_error.add_note(
                        f"Blackbox runtime debris registration failed: {registration_error}"
                    )
                raise
            view._closed = True
    except BaseException as cleanup_error:
        _logger.warning(
            "Blackbox runtime view cleanup failed: path=%s error=%s notes=%s",
            view.data_dir, cleanup_error, getattr(cleanup_error, "__notes__", ()),
        )
        raise


def _register_runtime_view_debris(view: BlackboxRuntimeView, *, runtime_root: Path) -> None:
    """登记本次视图的既有受控清理凭证。"""
    if (
        view.data_dir.parent != runtime_root / "active"
        or not view.data_dir.name.startswith(_BLACKBOX_RUNTIME_VIEW_PREFIX)
        or not stat.S_ISDIR(view.data_dir.lstat().st_mode)
    ):
        raise ValueError("runtime view is outside controlled active root")
    marker_path = _blackbox_debris_marker_path(runtime_root / "debris", view.data_dir.name)
    marker = {
        "schema_version": _BLACKBOX_DEBRIS_MARKER_SCHEMA,
        "active_path": str(view.data_dir),
        "combined_snapshot_id": view.bundle.combined_snapshot_id,
    }
    payload = json.dumps(marker, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as error:
        try:
            marker_path.unlink(missing_ok=True)
        except OSError as unlink_error:
            error.add_note(f"partial runtime debris marker cleanup failed: {unlink_error}")
        raise
    view.debris_path = view.data_dir
    view._closed = True


def _remove_runtime_view(
    path: Path,
    *,
    controlled_parent: Path,
) -> None:
    try:
        parent = path.parent.resolve(strict=True)
        expected_parent = controlled_parent.resolve(strict=True)
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("runtime view cleanup path is invalid") from exc
    if (
        parent != expected_parent
        or not path.name.startswith(_BLACKBOX_RUNTIME_VIEW_PREFIX)
        or stat.S_ISLNK(mode)
        or not stat.S_ISDIR(mode)
    ):
        raise ValueError("runtime view cleanup path is outside controlled root")
    _make_tree_writable(path)
    shutil.rmtree(path)


def _ensure_private_runtime_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError("runtime control directory is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError("runtime control directory must be a non-symlink directory")
    path.chmod(0o700)


def _blackbox_debris_marker_path(
    debris_root: Path,
    view_name: str,
) -> Path:
    return debris_root / f"{view_name}.json"


def _read_blackbox_debris_marker(path: Path) -> dict[str, Any]:
    try:
        _, raw = _read_stable_regular_file(
            path,
            "Blackbox runtime debris marker",
        )
        marker = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Blackbox runtime debris marker is invalid") from exc
    if (
        not isinstance(marker, dict)
        or set(marker)
        != {"schema_version", "active_path", "combined_snapshot_id"}
        or marker.get("schema_version") != _BLACKBOX_DEBRIS_MARKER_SCHEMA
        or not isinstance(marker.get("active_path"), str)
        or not isinstance(marker.get("combined_snapshot_id"), str)
    ):
        raise ValueError("Blackbox runtime debris marker is invalid")
    canonical = (
        json.dumps(
            marker,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise ValueError("Blackbox runtime debris marker is not canonical")
    return marker
