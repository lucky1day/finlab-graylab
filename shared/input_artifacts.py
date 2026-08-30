from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from shared import data_service as _data_service
from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT, RUNTIME_INPUT_ROOT, safe_path_part
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxInputBundle,
    BlackboxSnapshot,
    CutoffKeys,
    compose_blackbox_input_bundle,
    create_snapshot_from_frames,
    normalize_daily_key,
    normalize_period_key,
)
from shared.data_bridge.authority import (
    normalize_blackbox_gray_replay_source_identity,
)
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.data_bridge.validation import ValidatedDataBridgeDataset
from shared.data_bridge.validation import LEGACY_FOUR_FILENAMES
from shared.runtime_paths import resolve_runtime_state_path

DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
BLACKBOX_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "snapshots"
BLACKBOX_GENERATION_SNAPSHOT_ROOT = (
    BLACKBOX_SNAPSHOT_ROOT / "generation_cache"
)
BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION = "blackbox-generation-snapshot-v3"
LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION = (
    "blackbox-generation-snapshot-v2"
)
BLACKBOX_RUNTIME_VIEW_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "runtime_views"
BLACKBOX_SCHEMA_PATH = Path(__file__).with_name("blackbox_v2") / "data_bridge_v1_schema.json"
BLACKBOX_SCHEMA_VERSION = "data-bridge-v1"
BLACKBOX_SCHEMA_CONTRACT_SHA256 = (
    "130c1acbb1cd13d49155334d3bba57f43896bf26d1ec7983c188eb93a7d0cf28"
)
LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256 = (
    "addf732eb35071f89493073d22f4bf6ef79a55d7ceaf554dd9cf2c41e4dc6db3"
)
DAILY_DATA_VERSION = "shared_data_service_daily.v1"
WEEKLY_DATA_VERSION = "shared_data_service_weekly.v1"
MONTHLY_DATA_VERSION = "shared_data_service_monthly.v1"
_FREQUENCY_FILE_PREFIXES = {
    "daily": "daily_output",
    "weekly": "weekly_output",
    "monthly": "monthly_output",
}
EPHEMERAL_NATIVE_INPUT_ROOT_ENV = "BOND_NATIVE_EPHEMERAL_INPUT_ROOT"
NATIVE_INPUT_AUDIT_ROOT_ENV = "BFL_NATIVE_INPUT_AUDIT_ROOT"
_BLACKBOX_RUNTIME_VIEW_PREFIX = "blackbox-runtime-"
_BLACKBOX_DEBRIS_MARKER_SCHEMA = "blackbox-runtime-debris-v1"


def create_input_engine(*, database_config: Any | None = None):
    """创建当前权威数据库输入源。"""
    if database_config is None:
        return _data_service.create_sqlalchemy_engine()
    return _data_service.create_sqlalchemy_engine(
        db_config=database_config,
    )


@dataclass(frozen=True)
class InputArtifact:
    """预测算法输入文件及读回后的 DataFrame。"""

    scheme_id: str
    frequency: str
    path: Path
    dataframe: pd.DataFrame
    source: str
    data_version: str


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


@dataclass(frozen=True)
class _StableSnapshotFile:
    path: Path
    fingerprint: tuple[int, ...]
    sha256: str
    content_bytes: bytes


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
    except BaseException:
        try:
            _remove_runtime_view(
                temporary,
                controlled_parent=active_root,
            )
        except Exception:
            pass
        view._closed = True
        raise

    try:
        yield view
    except BaseException:
        try:
            _finalize_blackbox_runtime_view(view, runtime_root=root)
        except Exception:
            pass
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
    if sealed_fingerprints is None:
        _validate_blackbox_snapshot(bundle.base_snapshot)
    elif not set(bundle.expected_filenames).issubset(sealed_fingerprints):
        raise ValueError("producer snapshot file seal is invalid")

    for filename in bundle.expected_filenames:
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


def _copy_ready_snapshot_file(
    source: Path,
    target: Path,
    *,
    label: str,
    expected_fingerprint: tuple[int, ...] | None,
) -> tuple[int, ...]:
    """稳定复制 producer 已封存的文件，不重复哈希或解析内容。"""
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
        if bundle.expected_filenames == LEGACY_FOUR_FILENAMES:
            factor_input_mode = "legacy_v1"
        elif bundle.expected_filenames == SNAPSHOT_FILENAMES:
            factor_input_mode = "algorithm_managed"
        else:
            raise ValueError("input bundle file set is invalid")
        trusted = compose_blackbox_input_bundle(
            bundle.base_snapshot,
            factor_input_mode=factor_input_mode,
        )
        matches = (
            bundle.combined_snapshot_id == trusted.combined_snapshot_id
            and bundle.parent_snapshot_id == trusted.parent_snapshot_id
            and bundle.expected_filenames == trusted.expected_filenames
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


def _finalize_blackbox_runtime_view(
    view: BlackboxRuntimeView,
    *,
    runtime_root: Path,
) -> None:
    try:
        if view._termination_uncertain:
            active_root = runtime_root / "active"
            debris_root = runtime_root / "debris"
            if view.data_dir.parent != active_root:
                raise ValueError(
                    "uncertain runtime view is outside controlled active root"
                )
            marker_path = _blackbox_debris_marker_path(
                debris_root,
                view.data_dir.name,
            )
            marker = {
                "schema_version": _BLACKBOX_DEBRIS_MARKER_SCHEMA,
                "active_path": str(view.data_dir),
                "combined_snapshot_id": view.bundle.combined_snapshot_id,
            }
            with marker_path.open("x", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        marker,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            marker_path.chmod(0o400)
            view.debris_path = view.data_dir
        else:
            _remove_runtime_view(
                view.data_dir,
                controlled_parent=runtime_root / "active",
            )
    finally:
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
    except OSError as exc:
        if not path.exists():
            return
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


def prepare_blackbox_generation_snapshot(
    *,
    state: Mapping[str, Any],
    dataset: ValidatedDataBridgeDataset,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    cache_root: str | Path = BLACKBOX_GENERATION_SNAPSHOT_ROOT,
) -> BlackboxSnapshot:
    """由 DataBridge producer 用本次已验证结果构建一次只读快照。"""
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Blackbox generation snapshot cache must be a real directory")
    with _generation_snapshot_cache_lock(root):
        current_identity = _generation_snapshot_identity(
            state,
            schema_path=Path(schema_path),
        )
        _validate_generation_dataset_identity(dataset, current_identity)
        cached = _read_generation_snapshot_cache(
            root,
            current_identity,
            factor_input_mode="algorithm_managed",
        )
        if cached is not None:
            _read_generation_snapshot_cache(
                root,
                current_identity,
                factor_input_mode="legacy_v1",
            )
            _write_ready_generation_identity(root, current_identity)
            return cached
        _require_blackbox_databridge_monthly_additions(dataset.frames)
        schema_version, expected_columns = _load_blackbox_schema(schema_path)
        generation_root = (
            root
            / "snapshots"
            / _generation_snapshot_cache_key(current_identity)
        )
        if generation_root.exists():
            _make_tree_writable(generation_root)
            shutil.rmtree(generation_root)
        generation_root.mkdir(parents=True)
        try:
            snapshot = create_snapshot_from_frames(
                dataset.frames,
                output_root=generation_root,
                expected_columns=expected_columns,
                schema_version=schema_version,
            )
            snapshot = replace(
                snapshot,
                generation_id=str(current_identity["generation_id"]),
                refresh_date=str(current_identity["refresh_date"]),
                business_digest=str(current_identity["business_digest"]),
                sealed_file_fingerprints=_snapshot_file_fingerprints(snapshot),
            )
            legacy_snapshot = _build_legacy_blackbox_snapshot(
                snapshot,
                dataset=dataset,
                generation_root=generation_root,
                expected_columns=expected_columns,
                schema_version=schema_version,
                current_identity=current_identity,
            )
            _write_generation_snapshot_cache(
                root,
                current_identity,
                snapshot,
                legacy_snapshot,
            )
        except BaseException:
            _make_tree_writable(generation_root)
            shutil.rmtree(generation_root)
            raise
        _write_ready_generation_identity(root, current_identity)
        return snapshot


def invalidate_ready_blackbox_snapshot(
    *,
    cache_root: str | Path = BLACKBOX_GENERATION_SNAPSHOT_ROOT,
) -> None:
    """DataBridge publish 开始前撤销旧 ready 指针。"""
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Blackbox generation snapshot cache must be a real directory")
    with _generation_snapshot_cache_lock(root):
        ready_path = root / "ready-generation.json"
        if ready_path.is_symlink():
            raise ValueError("Blackbox ready generation receipt must not be a symlink")
        ready_path.unlink(missing_ok=True)
        _fsync_directory(root)


def get_ready_blackbox_snapshot(
    *,
    snapshot_date: str,
    cache_root: str | Path = BLACKBOX_GENERATION_SNAPSHOT_ROOT,
    require_fresh: bool = False,
    expected_source_identity: Mapping[str, Any] | None = None,
    factor_input_mode: str = "legacy_v1",
) -> BlackboxSnapshot:
    """读取 producer 已发布的不可变快照；缺失时不代建、不修复。"""
    root = Path(cache_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Blackbox ready snapshot root is unavailable")
    with _generation_snapshot_cache_lock(root, shared=True):
        current_identity = _read_ready_generation_identity(root)
        if expected_source_identity is not None:
            _validate_ready_snapshot_source_identity(
                current_identity,
                expected_source_identity,
            )
        snapshot = _read_generation_snapshot_cache(
            root,
            current_identity,
            factor_input_mode=factor_input_mode,
        )
        if snapshot is None:
            raise ValueError(
                "DataBridge generation has no ready Blackbox snapshot"
            )
        if require_fresh and snapshot.refresh_date != snapshot_date:
            raise ValueError(
                "DataBridge refresh_date must match snapshot_date: "
                f"{snapshot.refresh_date} != {snapshot_date}"
            )
        return snapshot


def _build_legacy_blackbox_snapshot(
    full_snapshot: BlackboxSnapshot,
    *,
    dataset: ValidatedDataBridgeDataset,
    generation_root: Path,
    expected_columns: Mapping[str, list[str]],
    schema_version: str,
    current_identity: Mapping[str, Any],
) -> BlackboxSnapshot:
    """全 V1 时复用完整快照；出现新版本后只构建一次四文件视图。"""
    catalog = dataset.frames["factor_catalog.csv"]
    wide_files = LEGACY_FOUR_FILENAMES[:3]
    can_reuse_full = (
        set(catalog["factor_version"]) == {"V1.0"}
        and all(
            list(dataset.frames[filename].columns)
            == expected_columns[filename]
            for filename in wide_files
        )
    )
    if can_reuse_full:
        return full_snapshot
    legacy_frames = {
        filename: (
            dataset.frames[filename].loc[
                :, expected_columns[filename]
            ].copy()
            if filename in wide_files
            else dataset.frames[filename].copy()
        )
        for filename in LEGACY_FOUR_FILENAMES
    }
    legacy = create_snapshot_from_frames(
        legacy_frames,
        output_root=generation_root,
        expected_columns=expected_columns,
        schema_version=schema_version,
    )
    return replace(
        legacy,
        generation_id=str(current_identity["generation_id"]),
        refresh_date=str(current_identity["refresh_date"]),
        business_digest=str(current_identity["business_digest"]),
        sealed_file_fingerprints=_snapshot_file_fingerprints(legacy),
    )


def _validate_ready_snapshot_source_identity(
    ready_identity: Mapping[str, Any],
    expected_source_identity: Mapping[str, Any],
) -> None:
    """只比较 producer receipt，不重新读取或哈希输入 CSV。"""
    expected = normalize_blackbox_gray_replay_source_identity(
        expected_source_identity
    )
    raw_files = ready_identity.get("files")
    if not isinstance(raw_files, Mapping):
        raise ValueError("DataBridge ready file identity is invalid")
    ready = {
        "generation_id": ready_identity.get("generation_id"),
        "refresh_date": ready_identity.get("refresh_date"),
        "schema_version": ready_identity.get("schema_version"),
        "business_digest": ready_identity.get("business_digest"),
        "files": [
            {"filename": filename, **dict(raw_files[filename])}
            for filename in sorted(raw_files)
            if isinstance(raw_files.get(filename), Mapping)
        ],
    }
    comparable_expected = {
        key: expected[key]
        for key in (
            "generation_id",
            "refresh_date",
            "schema_version",
            "business_digest",
            "files",
        )
    }
    if ready != comparable_expected:
        raise ValueError(
            "DataBridge ready snapshot does not match planned authority"
        )


def _generation_snapshot_identity(
    state: Mapping[str, Any],
    *,
    schema_path: Path,
) -> dict[str, Any]:
    schema_contract_sha256 = hashlib.sha256(
        schema_path.read_bytes()
    ).hexdigest()
    if schema_contract_sha256 != BLACKBOX_SCHEMA_CONTRACT_SHA256:
        raise ValueError("Blackbox DataBridge schema contract does not match this release")
    identity: dict[str, Any] = {
        "cache_schema_version": BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
        "schema_contract_sha256": schema_contract_sha256,
        "generation_id": state.get("generation_id"),
        "refresh_date": state.get("refresh_date"),
        "business_digest": state.get("business_digest"),
        "schema_version": state.get("schema_version"),
        "files": json.loads(
            json.dumps(state.get("files"), ensure_ascii=True, sort_keys=True)
        ),
    }
    _validate_generation_snapshot_identity(identity)
    return identity


def _generation_snapshot_cache_key(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(identity),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_generation_dataset_identity(
    dataset: ValidatedDataBridgeDataset,
    identity: Mapping[str, Any],
) -> None:
    if (
        dataset.schema_version != identity.get("schema_version")
        or dataset.business_digest != identity.get("business_digest")
    ):
        raise ValueError(
            "validated DataBridge dataset does not match published identity"
        )
    identity_files = identity.get("files")
    if not isinstance(identity_files, Mapping):
        raise ValueError("published DataBridge file identity is invalid")
    mismatches = []
    for filename in SNAPSHOT_FILENAMES:
        profile = dataset.files.get(filename)
        stored = identity_files.get(filename)
        if (
            profile is None
            or not isinstance(stored, Mapping)
            or stored.get("sha256") != profile.sha256
            or stored.get("business_hash") != profile.business_hash
            or stored.get("rows") != profile.rows
            or stored.get("columns") != profile.columns
            or stored.get("min_key") != profile.min_key
            or stored.get("max_key") != profile.max_key
        ):
            mismatches.append(filename)
    if mismatches:
        raise ValueError(
            "validated DataBridge files do not match published identity: "
            f"{mismatches}"
        )


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


@contextmanager
def _generation_snapshot_cache_lock(root: Path, *, shared: bool = False):
    lock_path = root / "generation-cache.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(
                "Blackbox generation snapshot lock must be a private regular file"
            )
        if info.st_uid != os.getuid():
            raise ValueError(
                "Blackbox generation snapshot lock must be owned by the current user"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(
            descriptor,
            fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
        )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_generation_snapshot_cache(
    root: Path,
    identity: Mapping[str, Any],
    *,
    factor_input_mode: str,
) -> BlackboxSnapshot | None:
    receipt_path = root / "receipts" / (
        _generation_snapshot_cache_key(identity) + ".json"
    )
    if not receipt_path.exists():
        return None
    try:
        _, receipt_raw = _read_stable_regular_file(
            receipt_path,
            "Blackbox generation snapshot receipt",
        )
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Blackbox generation snapshot receipt is invalid") from exc
    if not isinstance(receipt, dict):
        raise ValueError("Blackbox generation snapshot receipt identity mismatch")
    legacy_receipt = set(receipt) == {
        "identity",
        "snapshot_id",
        "cutoff_keys",
        "sealed_file_fingerprints",
    }
    current_receipt = set(receipt) == {
        "identity",
        "snapshot_id",
        "legacy_snapshot_id",
        "cutoff_keys",
        "sealed_file_fingerprints",
        "legacy_sealed_file_fingerprints",
    }
    if (
        not (legacy_receipt or current_receipt)
        or receipt.get("identity") != dict(identity)
    ):
        raise ValueError("Blackbox generation snapshot receipt identity mismatch")
    identity_contract = (
        identity.get("cache_schema_version"),
        identity.get("schema_contract_sha256"),
    )
    if (
        legacy_receipt
        and identity_contract
        != (
            LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
            LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
        )
    ) or (
        current_receipt
        and identity_contract
        != (
            BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
            BLACKBOX_SCHEMA_CONTRACT_SHA256,
        )
    ):
        raise ValueError(
            "Blackbox generation snapshot receipt contract mismatch"
        )
    if factor_input_mode == "algorithm_managed":
        if legacy_receipt:
            raise ValueError(
                "algorithm-managed input requires a five-file generation"
            )
        snapshot_id = receipt.get("snapshot_id")
        sealed_value = receipt.get("sealed_file_fingerprints")
        expected_filenames = SNAPSHOT_FILENAMES
    elif factor_input_mode == "legacy_v1":
        snapshot_id = (
            receipt.get("legacy_snapshot_id")
            if current_receipt
            else receipt.get("snapshot_id")
        )
        sealed_value = (
            receipt.get("legacy_sealed_file_fingerprints")
            if current_receipt
            else receipt.get("sealed_file_fingerprints")
        )
        expected_filenames = LEGACY_FOUR_FILENAMES
    else:
        raise ValueError(f"unsupported factor_input_mode: {factor_input_mode}")
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r"snapshot-[0-9a-f]{24}", snapshot_id) is None
    ):
        raise ValueError("Blackbox generation snapshot receipt identity mismatch")
    snapshots_root = (
        root
        / "snapshots"
        / _generation_snapshot_cache_key(identity)
    ).resolve(strict=True)
    snapshot_root = snapshots_root / snapshot_id
    if snapshot_root.parent.resolve(strict=True) != snapshots_root:
        raise ValueError("Blackbox generation snapshot path is outside cache")
    cached_cutoff_fields = _cached_snapshot_cutoff_fields(
        receipt.get("cutoff_keys")
    )
    sealed_file_fingerprints = _cached_snapshot_file_fingerprints(
        sealed_value,
        allowed_filenames=(
            (SNAPSHOT_FILENAMES,)
            if factor_input_mode == "algorithm_managed"
            else (SNAPSHOT_FILENAMES, LEGACY_FOUR_FILENAMES)
        ),
    )
    snapshot = BlackboxSnapshot(
        snapshot_id=snapshot_id,
        root_dir=snapshot_root,
        data_dir=snapshot_root / "data",
        manifest_path=snapshot_root / "manifest.json",
        schema_version=str(identity["schema_version"]),
        generation_id=str(identity["generation_id"]),
        refresh_date=str(identity["refresh_date"]),
        business_digest=str(identity["business_digest"]),
        sealed_file_fingerprints=sealed_file_fingerprints,
    )
    snapshot_files, source_states = _validate_blackbox_snapshot(
        snapshot,
        validate_csv_profiles=False,
        validate_file_contents=False,
    )
    if source_states:
        raise ValueError(
            "Blackbox generation snapshot metadata validation read file contents"
        )
    current_fingerprints = _snapshot_file_fingerprints(snapshot)
    if dict(current_fingerprints) != dict(sealed_file_fingerprints):
        raise ValueError(
            "Blackbox generation snapshot no longer matches producer seal"
        )
    identity_files = identity["files"]
    snapshot_filenames = tuple(snapshot_files)
    if not set(expected_filenames).issubset(snapshot_filenames):
        raise ValueError("Blackbox generation snapshot input mode mismatch")
    if factor_input_mode == "legacy_v1":
        _, frozen_columns = _load_blackbox_schema(BLACKBOX_SCHEMA_PATH)
        changed_columns = [
            filename
            for filename in LEGACY_FOUR_FILENAMES
            if snapshot_files[filename].get("columns")
            != frozen_columns[filename]
        ]
        if changed_columns:
            raise ValueError(
                "legacy Blackbox snapshot does not match frozen V1 columns: "
                f"{changed_columns}"
            )
    profile_mismatches = []
    if snapshot_id == receipt.get("snapshot_id"):
        profile_mismatches = [
            filename
            for filename in snapshot_filenames
            if not isinstance(identity_files.get(filename), Mapping)
            or identity_files[filename].get("sha256")
            != snapshot_files[filename].get("sha256")
            or identity_files[filename].get("rows")
            != snapshot_files[filename].get("row_count")
            or identity_files[filename].get("columns")
            != len(snapshot_files[filename].get("columns", ()))
        ]
    if profile_mismatches:
        raise ValueError(
            "Blackbox generation snapshot does not match DataBridge identity: "
            f"{profile_mismatches}"
        )
    return replace(snapshot, **cached_cutoff_fields)


def _write_generation_snapshot_cache(
    root: Path,
    identity: Mapping[str, Any],
    snapshot: BlackboxSnapshot,
    legacy_snapshot: BlackboxSnapshot,
) -> None:
    sealed_file_fingerprints = snapshot.sealed_file_fingerprints
    if sealed_file_fingerprints is None:
        raise ValueError("producer snapshot file seal is missing")
    legacy_sealed_file_fingerprints = legacy_snapshot.sealed_file_fingerprints
    if legacy_sealed_file_fingerprints is None:
        raise ValueError("legacy snapshot file seal is missing")
    receipts = root / "receipts"
    receipts.mkdir(exist_ok=True)
    receipt_path = receipts / (
        _generation_snapshot_cache_key(identity) + ".json"
    )
    payload = (
        json.dumps(
            {
                "identity": dict(identity),
                "snapshot_id": snapshot.snapshot_id,
                "legacy_snapshot_id": legacy_snapshot.snapshot_id,
                "sealed_file_fingerprints": {
                    filename: list(fingerprint)
                    for filename, fingerprint in sorted(
                        sealed_file_fingerprints.items()
                    )
                },
                "legacy_sealed_file_fingerprints": {
                    filename: list(fingerprint)
                    for filename, fingerprint in sorted(
                        legacy_sealed_file_fingerprints.items()
                    )
                },
                "cutoff_keys": {
                    "date": list(snapshot.daily_cutoff_keys or ()),
                    "week_id": list(snapshot.weekly_cutoff_keys or ()),
                    "month_id": list(snapshot.monthly_cutoff_keys or ()),
                    "calendar_week_ids_by_date": [
                        list(item)
                        for item in (
                            snapshot.calendar_week_ids_by_date or {}
                        ).items()
                    ],
                },
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".generation-receipt-",
        dir=receipts,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, receipt_path)
        _fsync_directory(receipts)
    finally:
        temporary.unlink(missing_ok=True)


def _write_ready_generation_identity(
    root: Path,
    identity: Mapping[str, Any],
) -> None:
    payload = (
        json.dumps(
            dict(identity),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ready-generation-",
        dir=root,
    )
    temporary = Path(temporary_name)
    ready_path = root / "ready-generation.json"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, ready_path)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _read_ready_generation_identity(root: Path) -> dict[str, Any]:
    try:
        _, raw = _read_stable_regular_file(
            root / "ready-generation.json",
            "Blackbox ready generation receipt",
        )
        identity = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Blackbox ready generation receipt is unavailable or invalid"
        ) from exc
    if not isinstance(identity, dict):
        raise ValueError("Blackbox ready generation receipt is invalid")
    canonical = (
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise ValueError("Blackbox ready generation receipt is invalid")
    _validate_generation_snapshot_identity(identity)
    return identity


def _validate_generation_snapshot_identity(identity: Mapping[str, Any]) -> None:
    expected_fields = {
        "cache_schema_version",
        "schema_contract_sha256",
        "generation_id",
        "refresh_date",
        "business_digest",
        "schema_version",
        "files",
    }
    if set(identity) != expected_fields:
        raise ValueError("Blackbox ready generation receipt fields are invalid")
    cache_contract = (
        identity.get("cache_schema_version"),
        identity.get("schema_contract_sha256"),
    )
    current_contract = (
        BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
        BLACKBOX_SCHEMA_CONTRACT_SHA256,
    )
    legacy_contract = (
        LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
        LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
    )
    if (
        cache_contract not in {current_contract, legacy_contract}
        or identity.get("schema_version") != BLACKBOX_SCHEMA_VERSION
    ):
        raise ValueError("Blackbox ready generation receipt contract mismatch")
    generation_id = identity.get("generation_id")
    refresh_date = identity.get("refresh_date")
    business_digest = identity.get("business_digest")
    if (
        not isinstance(generation_id, str)
        or not generation_id.strip()
        or not isinstance(refresh_date, str)
        or date.fromisoformat(refresh_date).isoformat() != refresh_date
        or not _is_sha256(business_digest)
    ):
        raise ValueError("Blackbox ready generation identity is invalid")
    files = identity.get("files")
    expected_filenames = (
        SNAPSHOT_FILENAMES
        if cache_contract == current_contract
        else LEGACY_FOUR_FILENAMES
    )
    if not isinstance(files, Mapping) or set(files) != set(expected_filenames):
        raise ValueError("Blackbox ready generation file identity is invalid")
    expected_profile_fields = {
        "sha256",
        "business_hash",
        "rows",
        "columns",
        "min_key",
        "max_key",
    }
    for filename in expected_filenames:
        profile = files.get(filename)
        if (
            not isinstance(profile, Mapping)
            or set(profile) != expected_profile_fields
            or not _is_sha256(profile.get("sha256"))
            or not _is_sha256(profile.get("business_hash"))
            or type(profile.get("rows")) is not int
            or int(profile["rows"]) < 0
            or type(profile.get("columns")) is not int
            or int(profile["columns"]) <= 0
            or not isinstance(profile.get("min_key"), str)
            or not isinstance(profile.get("max_key"), str)
        ):
            raise ValueError(
                f"Blackbox ready generation file identity is invalid: {filename}"
            )


def _cached_snapshot_file_fingerprints(
    value: object,
    *,
    allowed_filenames: tuple[tuple[str, ...], ...],
) -> Mapping[str, tuple[int, ...]]:
    allowed_sets = {frozenset(item) for item in allowed_filenames}
    if not isinstance(value, Mapping) or frozenset(value) not in allowed_sets:
        raise ValueError("Blackbox generation snapshot file seal is invalid")
    fingerprints: dict[str, tuple[int, ...]] = {}
    for filename in value:
        raw = value.get(filename)
        if (
            not isinstance(raw, list)
            or len(raw) != 7
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in raw
            )
        ):
            raise ValueError(
                "Blackbox generation snapshot file seal is invalid"
            )
        fingerprints[filename] = tuple(raw)
    return MappingProxyType(fingerprints)


def _cached_snapshot_cutoff_fields(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or set(value) != {
        "date",
        "week_id",
        "month_id",
        "calendar_week_ids_by_date",
    }:
        raise ValueError("Blackbox generation snapshot cutoff cache is invalid")
    normalized: dict[str, tuple[str, ...]] = {}
    for key in ("date", "week_id", "month_id"):
        raw = value.get(key)
        if not isinstance(raw, list) or not raw:
            raise ValueError("Blackbox generation snapshot cutoff cache is invalid")
        try:
            if key == "date":
                values = tuple(date.fromisoformat(str(item)).isoformat() for item in raw)
            else:
                values = tuple(normalize_period_key(item, key) for item in raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Blackbox generation snapshot cutoff cache is invalid"
            ) from exc
        if list(values) != sorted(set(values)):
            raise ValueError("Blackbox generation snapshot cutoff cache is invalid")
        normalized[key] = values
    raw_calendar = value.get("calendar_week_ids_by_date")
    if not isinstance(raw_calendar, list) or not raw_calendar:
        raise ValueError("Blackbox generation snapshot calendar cache is invalid")
    try:
        calendar = tuple(
            (
                date.fromisoformat(str(item[0])).isoformat(),
                normalize_period_key(item[1], "week_id"),
            )
            for item in raw_calendar
            if isinstance(item, list) and len(item) == 2
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Blackbox generation snapshot calendar cache is invalid"
        ) from exc
    if len(calendar) != len(raw_calendar) or list(calendar) != sorted(
        set(calendar)
    ):
        raise ValueError("Blackbox generation snapshot calendar cache is invalid")
    return {
        "daily_cutoff_keys": normalized["date"],
        "weekly_cutoff_keys": normalized["week_id"],
        "monthly_cutoff_keys": normalized["month_id"],
        "calendar_week_ids_by_date": MappingProxyType(dict(calendar)),
    }


def _require_blackbox_databridge_monthly_additions(
    frames: Mapping[str, pd.DataFrame],
) -> None:
    monthly = frames.get("monthly_output.csv")
    if monthly is None:
        raise ValueError("Blackbox DataBridge monthly output is missing")
    missing = [
        code
        for code in _data_service.DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES
        if code not in monthly.columns
    ]
    if missing:
        raise ValueError(
            "Blackbox DataBridge monthly output is missing required columns: "
            f"{missing}"
        )


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


def resolve_blackbox_input_cutoffs(
    snapshot: BlackboxSnapshot,
    *,
    feature_date: str,
    engine=None,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> CutoffKeys:
    """从数据桥 as-of 输出解析截止键，不在算法层推导周/月标识。"""
    _, expected_columns = _load_blackbox_schema(schema_path)
    own_engine = engine is None
    engine = engine or _data_service.create_sqlalchemy_engine()
    try:
        weekly_as_of = _data_service.build_weekly_output_from_db(
            schema_columns=expected_columns["weekly_output.csv"],
            as_of_date=feature_date,
            engine=engine,
        )
        monthly_as_of = _data_service.build_monthly_output_from_db(
            end_date=feature_date,
            engine=engine,
            include_databridge_additions=True,
        )
        snapshot_keys = _load_snapshot_cutoff_keys(snapshot)
        normalized_feature_date = _normalize_feature_date(feature_date)
        daily_position = bisect_right(
            snapshot_keys["date"],
            normalized_feature_date,
        )
        if daily_position == 0:
            raise ValueError(
                "daily snapshot has no row on or before "
                f"{normalized_feature_date}"
            )
        daily_cutoff_key = snapshot_keys["date"][daily_position - 1]
        weekly_cutoff_key = _cached_authoritative_cutoff(
                weekly_as_of,
                key_column="week_id",
                available_keys=snapshot_keys["week_id"],
                filename="weekly_output.csv",
            )
        _require_snapshot_calendar_week_id(
            snapshot_keys,
            daily_cutoff_key=daily_cutoff_key,
            weekly_cutoff_key=weekly_cutoff_key,
        )
        return CutoffKeys(
            daily_cutoff_key=daily_cutoff_key,
            weekly_cutoff_key=weekly_cutoff_key,
            monthly_cutoff_key=_cached_authoritative_cutoff(
                monthly_as_of,
                key_column="month_id",
                available_keys=snapshot_keys["month_id"],
                filename="monthly_output.csv",
            ),
        )
    finally:
        if own_engine:
            engine.dispose()


def resolve_blackbox_input_cutoffs_bulk(
    snapshot: BlackboxSnapshot,
    *,
    feature_dates: Iterable[str],
    engine=None,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> dict[str, CutoffKeys]:
    """一次读取平台来源与快照，为多个 feature_date 解析三频截止键。"""
    normalized_dates = tuple(dict.fromkeys(
        _normalize_feature_date(value) for value in feature_dates
    ))
    if not normalized_dates:
        return {}
    snapshot_keys = _load_snapshot_cutoff_keys(snapshot)
    own_engine = engine is None
    engine = engine or _data_service.create_sqlalchemy_engine()
    try:
        return _resolve_blackbox_input_cutoffs_bulk_from_keys(
            snapshot_keys,
            feature_dates=normalized_dates,
            connection=engine,
            schema_path=schema_path,
        )
    finally:
        if own_engine:
            engine.dispose()


def _resolve_blackbox_input_cutoffs_bulk_from_keys(
    snapshot_keys: Mapping[str, Any],
    *,
    feature_dates: Iterable[str],
    connection: Any,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
) -> dict[str, CutoffKeys]:
    """使用已冻结 key 集合和 caller Connection 批量解析有效截止键。"""
    resolved = _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
        snapshot_keys,
        feature_dates=feature_dates,
        connection=connection,
        schema_path=schema_path,
    )
    return {
        feature_date: item.cutoff_keys
        for feature_date, item in resolved.items()
    }


@dataclass(frozen=True, slots=True)
class _ResolvedPeriodCutoff:
    """同一 source snapshot 内的 exact key 与实际连续性 key。"""

    source_key: str
    effective_key: str


@dataclass(frozen=True, slots=True)
class _ResolvedBlackboxInputCutoffs:
    """保留 authority 所需 source period key，外部仍只接收 CutoffKeys。"""

    cutoff_keys: CutoffKeys
    source_weekly_cutoff_key: str
    source_monthly_cutoff_key: str


def _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
    snapshot_keys: Mapping[str, Any],
    *,
    feature_dates: Iterable[str],
    connection: Any,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    allow_legacy_v1_period_fallback: bool = False,
) -> dict[str, _ResolvedBlackboxInputCutoffs]:
    """解析有效 cutoff，并保留 fallback 前的 source weekly/monthly key。"""
    normalized_dates = list(dict.fromkeys(
        _normalize_feature_date(value) for value in feature_dates
    ))
    if not normalized_dates:
        return {}

    _, expected_columns = _load_blackbox_schema(schema_path)
    metadata = _data_service.read_factor_metadata_from_db(connection)
    weekly_codes = expected_columns["weekly_output.csv"][1:]
    weekly_raw = _data_service.read_weekly_long_from_db(
        weekly_codes,
        "api_wind_weekly",
        connection,
    )
    weekly_derivative = _data_service.read_weekly_long_from_db(
        weekly_codes,
        "api_wind_derivative_weekly",
        connection,
    )
    monthly_selected = _data_service.select_monthly_factor_metadata(
        metadata,
        include_databridge_additions=True,
    )
    monthly_codes = (
        monthly_selected["indicators_code"]
        .astype(str)
        .str.strip()
        .tolist()
    )
    monthly_raw = _data_service.read_monthly_long_from_db(
        monthly_codes,
        "api_wind_monthly",
        connection,
    )
    monthly_derivative = _data_service.read_monthly_long_from_db(
        monthly_codes,
        "api_wind_derivative_monthly",
        connection,
        include_month_id=True,
    )
    max_date = max(normalized_dates)
    weekly_index = _data_service.build_weekly_cutoff_index_from_frames(
        expected_columns["weekly_output.csv"],
        weekly_raw,
        weekly_derivative,
        end_date=max_date,
    )
    monthly_index = _data_service.build_monthly_cutoff_index_from_frames(
        metadata,
        monthly_raw,
        monthly_derivative,
        end_date=max_date,
        include_databridge_additions=True,
    )
    daily_dates = snapshot_keys["date"]
    weekly_by_date = _resolve_period_cutoffs_bulk(
        normalized_dates,
        weekly_index,
        key_column="week_id",
        available_keys=snapshot_keys["week_id"],
        filename="weekly_output.csv",
        allow_legacy_v1_period_fallback=(
            allow_legacy_v1_period_fallback
        ),
    )
    monthly_by_date = _resolve_period_cutoffs_bulk(
        normalized_dates,
        monthly_index,
        key_column="month_id",
        available_keys=snapshot_keys["month_id"],
        filename="monthly_output.csv",
        allow_legacy_v1_period_fallback=(
            allow_legacy_v1_period_fallback
        ),
    )
    resolved: dict[str, _ResolvedBlackboxInputCutoffs] = {}
    for feature_date in normalized_dates:
        daily_position = bisect_right(daily_dates, feature_date)
        if daily_position == 0:
            raise ValueError(f"daily snapshot has no row on or before {feature_date}")
        weekly = weekly_by_date[feature_date]
        monthly = monthly_by_date[feature_date]
        daily_cutoff_key = daily_dates[daily_position - 1]
        if "calendar_week_id_by_date" in snapshot_keys:
            _require_snapshot_calendar_week_id(
                snapshot_keys,
                daily_cutoff_key=daily_cutoff_key,
                weekly_cutoff_key=weekly.effective_key,
            )
        resolved[feature_date] = _ResolvedBlackboxInputCutoffs(
            cutoff_keys=CutoffKeys(
                daily_cutoff_key=daily_cutoff_key,
                weekly_cutoff_key=weekly.effective_key,
                monthly_cutoff_key=monthly.effective_key,
            ),
            source_weekly_cutoff_key=weekly.source_key,
            source_monthly_cutoff_key=monthly.source_key,
        )
    return resolved


def _require_snapshot_calendar_week_id(
    snapshot_keys: Mapping[str, Any],
    *,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
) -> None:
    calendar = snapshot_keys.get("calendar_week_id_by_date")
    if not isinstance(calendar, Mapping):
        raise ValueError("snapshot calendar index is missing")
    frozen_week_id = calendar.get(daily_cutoff_key)
    if frozen_week_id != weekly_cutoff_key:
        raise ValueError(
            "Request weekly_cutoff_key does not match frozen calendar: "
            f"{daily_cutoff_key} -> {frozen_week_id}, got {weekly_cutoff_key}"
        )


def validate_blackbox_request_calendar(
    snapshot: BlackboxSnapshot,
    *,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
) -> None:
    """以 generation 缓存索引校验 Request 周历映射。"""
    calendar = snapshot.calendar_week_ids_by_date
    if isinstance(calendar, Mapping):
        frozen_week_id = calendar.get(daily_cutoff_key)
        if frozen_week_id != weekly_cutoff_key:
            raise ValueError(
                "Request weekly_cutoff_key does not match frozen calendar: "
                f"{daily_cutoff_key} -> {frozen_week_id}, "
                f"got {weekly_cutoff_key}"
            )
        return
    _require_snapshot_calendar_week_id(
        _load_snapshot_cutoff_keys(snapshot),
        daily_cutoff_key=daily_cutoff_key,
        weekly_cutoff_key=weekly_cutoff_key,
    )


def _load_snapshot_cutoff_keys(snapshot: BlackboxSnapshot) -> dict[str, Any]:
    cached = (
        snapshot.daily_cutoff_keys,
        snapshot.weekly_cutoff_keys,
        snapshot.monthly_cutoff_keys,
        snapshot.calendar_week_ids_by_date,
    )
    if all(value is not None for value in cached):
        return {
            "date": list(snapshot.daily_cutoff_keys or ()),
            "week_id": set(snapshot.weekly_cutoff_keys or ()),
            "month_id": set(snapshot.monthly_cutoff_keys or ()),
            "calendar_week_id_by_date": dict(
                snapshot.calendar_week_ids_by_date or {}
            ),
        }
    paths = {
        "date": snapshot.data_dir / "daily_output.csv",
        "week_id": snapshot.data_dir / "weekly_output.csv",
        "month_id": snapshot.data_dir / "monthly_output.csv",
    }
    loaded: dict[str, Any] = {}
    for key, path in paths.items():
        try:
            frame = pd.read_csv(path, usecols=[key], dtype={key: "string"})
        except ValueError as exc:
            raise ValueError(f"{path.name} is missing {key} cutoff column") from exc
        if key == "date":
            parsed = pd.to_datetime(frame[key], errors="coerce").dt.date
            if parsed.isna().any():
                raise ValueError("daily_output.csv contains an invalid date cutoff key")
            values = [value.isoformat() for value in parsed]
            if values != sorted(set(values)):
                raise ValueError("daily_output.csv date cutoff keys must be unique and ascending")
            loaded[key] = values
            continue
        values = [normalize_period_key(value, key) for value in frame[key].tolist()]
        if values != sorted(set(values)):
            raise ValueError(f"{path.name} {key} cutoff keys must be unique and ascending")
        loaded[key] = set(values)
    calendar_path = snapshot.data_dir / "api_wind_date.csv"
    try:
        calendar = pd.read_csv(
            calendar_path,
            usecols=["rdate", "week_id"],
            dtype="string",
            keep_default_na=False,
        )
    except ValueError as exc:
        raise ValueError(
            "api_wind_date.csv is missing calendar columns"
        ) from exc
    calendar_pairs = [
        (
            normalize_daily_key(row.rdate),
            normalize_period_key(row.week_id, "week_id"),
        )
        for row in calendar.itertuples(index=False)
    ]
    if not calendar_pairs or calendar_pairs != sorted(set(calendar_pairs)):
        raise ValueError(
            "api_wind_date.csv calendar keys must be unique and ascending"
        )
    loaded["calendar_week_id_by_date"] = dict(calendar_pairs)
    return loaded


def _cached_authoritative_cutoff(
    as_of: pd.DataFrame,
    *,
    key_column: str,
    available_keys: set[str],
    filename: str,
) -> str:
    if key_column not in as_of.columns or as_of.empty:
        raise ValueError(f"platform as-of data has no {key_column}")
    cutoff = normalize_period_key(as_of[key_column].tolist()[-1], key_column)
    if cutoff not in available_keys:
        raise ValueError(
            f"platform {key_column} cutoff {cutoff} does not exist in {filename}"
        )
    return cutoff


def _resolve_period_cutoffs_bulk(
    feature_dates: list[str],
    index: pd.DataFrame,
    *,
    key_column: str,
    available_keys: set[str],
    filename: str,
    allow_legacy_v1_period_fallback: bool = False,
) -> dict[str, _ResolvedPeriodCutoff]:
    required = {key_column, "available_date"}
    if index.empty or not required.issubset(index.columns):
        raise ValueError(f"platform as-of data has no {key_column}")
    events = sorted(
        (
            _normalize_feature_date(value),
            normalize_period_key(key, key_column),
        )
        for value, key in zip(index["available_date"], index[key_column])
    )
    result: dict[str, _ResolvedPeriodCutoff] = {}
    latest: str | None = None
    position = 0
    for feature_date in sorted(feature_dates):
        while position < len(events) and events[position][0] <= feature_date:
            key = events[position][1]
            latest = key if latest is None or key > latest else latest
            position += 1
        if latest is None:
            raise ValueError(f"platform as-of data has no {key_column} for {feature_date}")
        selected = latest
        if selected not in available_keys:
            if allow_legacy_v1_period_fallback:
                prior_keys = [
                    key for key in available_keys if key <= selected
                ]
                if prior_keys:
                    selected = max(prior_keys)
            if selected not in available_keys:
                raise ValueError(
                    f"platform {key_column} cutoff {latest} does not exist in {filename}"
                )
        result[feature_date] = _ResolvedPeriodCutoff(
            source_key=latest,
            effective_key=selected,
        )
    return result


def _normalize_feature_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("feature_date must use YYYY-MM-DD") from exc


def input_artifact_path(
    *,
    scheme_id: str,
    frequency: str,
    predict_date: str,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """生成统一的输入文件路径。"""
    safe_scheme_id = safe_path_part(scheme_id)
    safe_predict_date = str(predict_date).replace("/", "-").replace(":", "-")
    prefix = _FREQUENCY_FILE_PREFIXES.get(str(frequency))
    if prefix is None:
        raise ValueError(f"unsupported frequency for input artifact path: {frequency}")
    filename = f"{prefix}_{safe_predict_date}.csv"
    ephemeral_root = os.environ.get(EPHEMERAL_NATIVE_INPUT_ROOT_ENV)
    if ephemeral_root is not None:
        root = Path(ephemeral_root)
        if not root.is_absolute():
            raise ValueError(
                f"{EPHEMERAL_NATIVE_INPUT_ROOT_ENV} must be absolute"
            )
        return root / "views" / safe_scheme_id / filename
    return Path(output_root) / safe_scheme_id / filename


def build_daily_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    start_date: str,
    end_date: str,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """通过日频 data_service 生成输入 CSV，再读回给算法。"""
    schema_columns = _legacy_native_schema_columns("daily_output.csv")
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "start_date": start_date,
        "end_date": end_date,
        "predict_date": predict_date,
        "schema_columns": schema_columns,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        data_version=DAILY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_daily_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            schema_columns=schema_columns,
        ),
        save=_data_service.save_daily_output,
        read=_read_daily_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_data_service_daily",
        data_version=DAILY_DATA_VERSION,
    )
    _write_native_input_audit_receipt(
        artifact,
        metadata=metadata,
        coverage_field="date",
        required_columns=("date",),
    )
    return artifact


def build_weekly_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    schema_columns: list[str] | None = None,
    start_week: int | None = None,
    end_week: int | None = None,
    as_of_date: str | None = None,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """生成周频输入 CSV，再读回给算法。"""
    effective_schema_columns = (
        list(schema_columns)
        if schema_columns is not None
        else _legacy_native_schema_columns("weekly_output.csv")
    )
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "schema_columns": effective_schema_columns,
        "start_week": start_week,
        "end_week": end_week,
        "as_of_date": as_of_date,
        "predict_date": predict_date,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        data_version=WEEKLY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_weekly_output_from_db(
            schema_columns=effective_schema_columns,
            start_week=start_week,
            end_week=end_week,
            as_of_date=as_of_date,
            engine=engine,
            preserve_metadata_lags=schema_columns is None,
        ),
        save=_data_service.save_weekly_output,
        read=_read_weekly_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_weekly",
        data_version=WEEKLY_DATA_VERSION,
    )
    _write_native_input_audit_receipt(
        artifact,
        metadata=metadata,
        coverage_field="week_id",
        required_columns=("week_id",),
    )
    return artifact


def build_monthly_input_artifact(
    *,
    scheme_id: str,
    predict_date: str,
    start_date: str,
    end_date: str,
    engine=None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> InputArtifact:
    """通过月频 data_service 生成输入 CSV，再读回给算法。"""
    schema_columns = _legacy_native_schema_columns("monthly_output.csv")
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        output_root=output_root,
    )
    metadata = {
        "start_date": start_date,
        "end_date": end_date,
        "predict_date": predict_date,
        "schema_columns": schema_columns,
    }
    read_back = _build_or_reuse_native_csv(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        data_version=MONTHLY_DATA_VERSION,
        metadata=metadata,
        view_path=path,
        build=lambda: _data_service.build_monthly_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            schema_columns=schema_columns,
        ),
        save=_data_service.save_monthly_output,
        read=_read_monthly_output_csv,
    )
    artifact = InputArtifact(
        scheme_id=scheme_id,
        frequency="monthly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_monthly",
        data_version=MONTHLY_DATA_VERSION,
    )
    _write_native_input_audit_receipt(
        artifact,
        metadata=metadata,
        coverage_field="month_id",
        required_columns=("month_id",),
    )
    return artifact


def _legacy_native_schema_columns(filename: str) -> list[str]:
    _, columns = _load_blackbox_schema(BLACKBOX_SCHEMA_PATH)
    selected = list(columns[filename])
    if filename == "monthly_output.csv":
        additions = set(_data_service.DATA_BRIDGE_V1_ADDITIVE_MONTHLY_CODES)
        selected = [
            column
            for column in selected
            if column == "month_id" or column not in additions
        ]
    return selected


def _build_or_reuse_native_csv(
    *,
    scheme_id: str,
    frequency: str,
    predict_date: str,
    data_version: str,
    metadata: Mapping[str, Any],
    view_path: Path,
    build: Callable[[], pd.DataFrame],
    save: Callable[[pd.DataFrame, str | Path], None],
    read: Callable[[str | Path], pd.DataFrame],
) -> pd.DataFrame:
    """同一临时作业内按完整 builder 参数复用只读 CSV。"""
    configured = str(os.environ.get(EPHEMERAL_NATIVE_INPUT_ROOT_ENV) or "").strip()
    if not configured:
        frame = build()
        _atomic_save_output(save, frame, view_path)
        return read(view_path)

    root = Path(configured)
    if not root.is_absolute():
        raise ValueError(f"{EPHEMERAL_NATIVE_INPUT_ROOT_ENV} must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    source_scope = (
        f"isolated:{scheme_id}"
        if str(os.environ.get("BFL_SOURCE_DB_CONFIG_PATH") or "").strip()
        else "service-db"
    )
    identity_payload = {
        "frequency": frequency,
        "predict_date": predict_date,
        "data_version": data_version,
        "source_scope": source_scope,
        "metadata": dict(metadata),
    }
    identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    objects = root / "objects"
    locks = root / "locks"
    objects.mkdir(mode=0o700, parents=True, exist_ok=True)
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    object_path = objects / f"{identity}.csv"
    lock_path = locks / f"{identity}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if not object_path.exists():
                frame = build()
                _atomic_save_output(save, frame, object_path)
                object_path.chmod(0o444)
            _link_native_input_view(object_path, view_path)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return read(view_path)


def _link_native_input_view(object_path: Path, view_path: Path) -> None:
    """为单方案建立同一只读 inode 的路径，不复制输入内容。"""
    view_path.parent.mkdir(parents=True, exist_ok=True)
    if view_path.exists():
        if os.path.samefile(object_path, view_path):
            return
        raise OSError(f"native input view already exists: {view_path}")
    os.link(object_path, view_path)


def _write_native_input_audit_receipt(
    artifact: InputArtifact,
    *,
    metadata: Mapping[str, Any],
    coverage_field: str,
    required_columns: tuple[str, ...],
) -> None:
    """仅在 Harness dry-run 请求时记录本次真实 builder 的输入身份。"""
    configured = str(os.environ.get(NATIVE_INPUT_AUDIT_ROOT_ENV) or "").strip()
    if not configured:
        return
    profile = _dataframe_profile(
        artifact.dataframe,
        coverage_field=coverage_field,
        required_columns=required_columns,
    )
    content_hash = _file_sha256(artifact.path)
    root = Path(configured)
    if not root.is_absolute():
        raise ValueError(f"{NATIVE_INPUT_AUDIT_ROOT_ENV} must be absolute")
    resolved_root = root.resolve(strict=True)
    root_details = root.lstat()
    if (
        root.is_symlink()
        or Path(os.path.abspath(root)) != resolved_root
        or stat.S_ISLNK(root_details.st_mode)
        or not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid != os.getuid()
    ):
        raise OSError("native input audit root is unsafe")

    artifact_details = artifact.path.lstat()
    if (
        stat.S_ISLNK(artifact_details.st_mode)
        or not stat.S_ISREG(artifact_details.st_mode)
    ):
        raise OSError("native input artifact is not a regular file")
    payload = {
        "scheme_id": artifact.scheme_id,
        "frequency": artifact.frequency,
        "path": str(artifact.path),
        "source": artifact.source,
        "data_version": artifact.data_version,
        "content_hash": content_hash,
        "schema_hash": _schema_hash(artifact.dataframe),
        "row_count": profile["row_count"],
        "column_count": profile["column_count"],
        "columns": list(profile["columns"]),
        "date_coverage": dict(profile["date_coverage"]),
        "quality_flags": dict(profile["quality_flags"]),
        "metadata": dict(metadata),
        "file_size": artifact_details.st_size,
        "modified_ns": artifact_details.st_mtime_ns,
    }
    receipt_path = resolved_root / f"{artifact.frequency}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{artifact.frequency}.",
        suffix=".tmp",
        dir=resolved_root,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt_path)
        _fsync_directory(resolved_root)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_save_output(save_output, frame: pd.DataFrame, path: Path) -> None:
    """同目录写临时文件，fsync 后原子替换，失败时保留旧完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        save_output(frame, temporary)
        if temporary.is_symlink() or not temporary.is_file():
            raise OSError(
                f"input artifact writer did not create a regular file: "
                f"{temporary}"
            )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
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


def _dataframe_profile(
    df: pd.DataFrame,
    *,
    coverage_field: str,
    required_columns: tuple[str, ...],
) -> dict[str, Any]:
    columns = [str(col) for col in df.columns]
    missing_required = [col for col in required_columns if col not in columns]
    date_coverage: dict[str, Any] = {"field": coverage_field, "start": None, "end": None}
    null_coverage_rows = 0
    duplicate_coverage_values = 0

    if coverage_field in df.columns:
        values = df[coverage_field]
        null_coverage_rows = int(values.isna().sum())
        non_null = values.dropna()
        duplicate_coverage_values = int(non_null.duplicated().sum())
        start, end = _coverage_bounds(non_null, coverage_field)
        date_coverage["start"] = start
        date_coverage["end"] = end

    return {
        "row_count": int(len(df)),
        "column_count": int(len(columns)),
        "columns": columns,
        "date_coverage": date_coverage,
        "quality_flags": {
            "missing_required_columns": missing_required,
            "empty_frame": bool(df.empty),
            "null_coverage_rows": null_coverage_rows,
            "duplicate_coverage_values": duplicate_coverage_values,
        },
    }


def _coverage_bounds(values: pd.Series, coverage_field: str) -> tuple[Any, Any]:
    if values.empty:
        return None, None
    if coverage_field == "date":
        parsed = pd.to_datetime(values, errors="coerce").dropna()
        if parsed.empty:
            return None, None
        return parsed.min().strftime("%Y-%m-%d"), parsed.max().strftime("%Y-%m-%d")
    if coverage_field == "week_id":
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return None, None
        return int(numeric.min()), int(numeric.max())
    if coverage_field == "month_id":
        cleaned = values.dropna().astype(str).str.strip()
        cleaned = cleaned[cleaned.str.fullmatch(r"\d{6}", na=False)]
        if cleaned.empty:
            return None, None
        return str(cleaned.min()), str(cleaned.max())
    return values.min(), values.max()


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _schema_hash(df: pd.DataFrame) -> str:
    schema = sorted((str(column), str(df[column].dtype)) for column in df.columns)
    payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_id(scheme_id: str, predict_date: str, content_hash: str) -> str:
    payload = json.dumps(
        {
            "scheme_id": scheme_id,
            "predict_date": predict_date,
            "content_hash": content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_watermark(profile: dict[str, Any]) -> str | None:
    end = profile["date_coverage"].get("end")
    if end is None:
        return None
    return str(end)


def _read_daily_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"daily input artifact missing date column: {path}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_weekly_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "week_id" in df.columns:
        df["week_id"] = pd.to_numeric(
            df["week_id"], errors="coerce"
        ).astype("Int64")
    for col in df.columns:
        if col != "week_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_monthly_output_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df.columns = [str(col).strip().lstrip("\ufeff") for col in df.columns]
    if "month_id" not in df.columns:
        raise ValueError(f"monthly input artifact missing month_id column: {path}")
    df["month_id"] = df["month_id"].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df = df[df["month_id"].str.fullmatch(r"\d{6}", na=False)].copy()
    df = df.sort_values("month_id").reset_index(drop=True)
    for col in df.columns:
        if col != "month_id":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_blackbox_schema(path: str | Path) -> tuple[str, dict[str, list[str]]]:
    schema_path = Path(path)
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Blackbox V2 data schema {schema_path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "files"}:
        raise ValueError("Blackbox V2 data schema must contain only schema_version and files")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("Blackbox V2 data schema_version must be a non-empty string")
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(SNAPSHOT_FILENAMES):
        raise ValueError(f"Blackbox V2 data schema files must be exactly {list(SNAPSHOT_FILENAMES)}")
    columns: dict[str, list[str]] = {}
    for filename in SNAPSHOT_FILENAMES:
        file_schema = files.get(filename)
        if not isinstance(file_schema, dict) or set(file_schema) != {"columns"}:
            raise ValueError(f"{filename} schema must contain only columns")
        values = file_schema.get("columns")
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"{filename} columns must be a non-empty string list")
        if len(values) != len(set(values)):
            raise ValueError(f"{filename} schema contains duplicate columns")
        columns[filename] = list(values)
    if columns["daily_output.csv"][0] != "date":
        raise ValueError("daily_output.csv schema must start with date")
    if columns["weekly_output.csv"][0] != "week_id":
        raise ValueError("weekly_output.csv schema must start with week_id")
    if columns["monthly_output.csv"][0] != "month_id":
        raise ValueError("monthly_output.csv schema must start with month_id")
    if columns["factor_catalog.csv"] != [
        "indicators_code",
        "frequency",
        "factor_version",
    ]:
        raise ValueError("factor_catalog.csv schema columns are invalid")
    return schema_version, columns


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
