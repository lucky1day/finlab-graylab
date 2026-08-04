from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from shared import data_service as _data_service
from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT, RUNTIME_INPUT_ROOT, safe_path_part
from shared.blackbox_v2 import platform_inputs as _platform_inputs
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxInputBundle,
    BlackboxSnapshot,
    CutoffKeys,
    compose_blackbox_input_bundle,
    create_snapshot_from_frames,
    resolve_cutoffs,
)
from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    check_current_dataset,
)
from shared.native_input_generation import (
    NativeGenerationContext,
    open_native_generation,
)

DEFAULT_OUTPUT_ROOT = RUNTIME_INPUT_ROOT
BLACKBOX_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "snapshots"
BLACKBOX_RUNTIME_SNAPSHOT_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "runtime_snapshots"
BLACKBOX_RUNTIME_VIEW_ROOT = BACKTEST_ARTIFACT_ROOT / "blackbox_v2" / "runtime_views"
BLACKBOX_SCHEMA_PATH = Path(__file__).with_name("blackbox_v2") / "data_bridge_v1_schema.json"
DATA_BRIDGE_ROOT = Path(__file__).resolve().parents[1] / "data" / "data_bridge"
DATA_BRIDGE_REFRESH_RUNTIME_ROOT = BACKTEST_ARTIFACT_ROOT / "data_bridge_refresh"
DAILY_DATA_VERSION = "shared_data_service_daily.v1"
WEEKLY_DATA_VERSION = "shared_data_service_weekly.v1"
MONTHLY_DATA_VERSION = "shared_data_service_monthly.v1"
_FREQUENCY_FILE_PREFIXES = {
    "daily": "daily_output",
    "weekly": "weekly_output",
    "monthly": "monthly_output",
}
NATIVE_INPUT_MODE = "native_generation_v1"
NATIVE_INPUT_MODE_ENV = "BOND_NATIVE_INPUT_MODE"
NATIVE_MANIFEST_PATH_ENV = "BOND_NATIVE_GENERATION_MANIFEST"
NATIVE_GENERATION_ID_ENV = "BOND_NATIVE_GENERATION_ID"
NATIVE_MANIFEST_SHA256_ENV = "BOND_NATIVE_GENERATION_MANIFEST_SHA256"
NATIVE_BUSINESS_DATE_ENV = "BOND_NATIVE_GENERATION_BUSINESS_DATE"
NATIVE_FEATURE_DATE_ENV = "BOND_NATIVE_GENERATION_FEATURE_DATE"
LIVE_SOURCE_INPUT_MODE = "live_source_0629"
LIVE_SOURCE_INPUT_MODE_ENV = "BOND_NATIVE_LIVE_SOURCE_MODE"
LIVE_SOURCE_FENCE_GENERATION_ID_ENV = (
    "BOND_NATIVE_LIVE_SOURCE_FENCE_GENERATION_ID"
)
LIVE_SOURCE_FEATURE_DATE_ENV = "BOND_NATIVE_LIVE_SOURCE_FEATURE_DATE"
LIVE_SOURCE_PACKAGE_SHA256_ENV = (
    "BOND_NATIVE_LIVE_SOURCE_PACKAGE_SHA256"
)
SCHEDULE_EXECUTION_TOKEN_ENV = "BOND_SCHEDULE_EXECUTION_TOKEN"
_NATIVE_GENERATION_ENV_FIELDS = {
    NATIVE_INPUT_MODE_ENV,
    NATIVE_MANIFEST_PATH_ENV,
    NATIVE_GENERATION_ID_ENV,
    NATIVE_MANIFEST_SHA256_ENV,
    NATIVE_BUSINESS_DATE_ENV,
    NATIVE_FEATURE_DATE_ENV,
}
_BLACKBOX_RUNTIME_VIEW_PREFIX = "blackbox-runtime-"
_BLACKBOX_DEBRIS_MARKER_SCHEMA = "blackbox-runtime-debris-v1"


def create_input_engine(*, database_config: Any | None = None):
    """创建输入源；日批 Native 环境中只打开被冻结的 generation。"""
    configured = {
        name: os.environ.get(name)
        for name in _NATIVE_GENERATION_ENV_FIELDS
        if name in os.environ
    }
    if configured:
        if database_config is not None:
            raise ValueError(
                "frozen Native generation cannot also bind a live "
                "database config"
            )
        missing = sorted(_NATIVE_GENERATION_ENV_FIELDS - set(configured))
        empty = sorted(
            name
            for name, value in configured.items()
            if not isinstance(value, str) or not value.strip()
        )
        if missing or empty:
            raise ValueError(
                "partial Native input generation environment: "
                f"missing={missing}, empty={empty}"
            )
        if configured[NATIVE_INPUT_MODE_ENV] != NATIVE_INPUT_MODE:
            raise ValueError(
                "unsupported Native input generation mode: "
                f"{configured[NATIVE_INPUT_MODE_ENV]}"
            )
        manifest_path = Path(configured[NATIVE_MANIFEST_PATH_ENV])
        if not manifest_path.is_absolute():
            raise ValueError(
                "Native input generation manifest path must be absolute"
            )
        return open_native_generation(
            manifest_path,
            expected_generation_id=configured[NATIVE_GENERATION_ID_ENV],
            expected_manifest_sha256=configured[NATIVE_MANIFEST_SHA256_ENV],
            expected_business_date=configured[NATIVE_BUSINESS_DATE_ENV],
            expected_feature_date=configured[NATIVE_FEATURE_DATE_ENV],
        )
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
    generated_at: str
    data_version: str
    artifact_id: str
    content_hash: str
    schema_hash: str
    source_watermark: str | None
    row_count: int
    column_count: int
    columns: list[str]
    date_coverage: dict[str, Any]
    quality_flags: dict[str, Any]
    metadata: dict[str, Any]


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
    fingerprint: tuple[int, int, int, int, int, int]
    sha256: str


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
    base_files, source_states = _validate_blackbox_snapshot(
        bundle.base_snapshot
    )

    for filename in SNAPSHOT_FILENAMES:
        source = bundle.base_snapshot.data_dir / filename
        target = destination / filename
        expected_sha256 = base_files[filename].get("sha256")
        shutil.copyfile(source, target, follow_symlinks=False)
        _require_regular_file(target, f"runtime view {filename}")
        target_stat = target.stat()
        if target_stat.st_nlink != 1:
            raise ValueError(f"runtime view {filename} must not be a hardlink")
        if (
            source_states[filename].fingerprint[0] == target_stat.st_dev
            and source_states[filename].fingerprint[1] == target_stat.st_ino
        ):
            raise ValueError(f"runtime view {filename} must be an independent file")
        _verify_file_sha256(target, expected_sha256, filename)
        after_state, _ = _read_stable_regular_file(
            source,
            f"parent snapshot {filename}",
        )
        if after_state != source_states[filename]:
            raise ValueError(
                f"parent snapshot {filename} changed during materialization"
            )

    for artifact in bundle.platform_input_artifacts:
        target = destination / artifact.filename
        target.write_bytes(artifact.content_bytes)
        _require_regular_file(target, f"runtime view {artifact.filename}")
        if target.stat().st_nlink != 1:
            raise ValueError(
                f"runtime view {artifact.filename} must not be a hardlink"
            )
        _verify_file_sha256(target, artifact.sha256, artifact.filename)

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


def _validate_trusted_blackbox_input_bundle(
    bundle: BlackboxInputBundle,
) -> BlackboxInputBundle:
    """从受信字段重算 bundle，拒绝 replace/手工构造的派生字段。"""
    try:
        trusted = compose_blackbox_input_bundle(
            bundle.base_snapshot,
            platform_input_ids=bundle.platform_input_ids,
            platform_input_artifacts=bundle.platform_input_artifacts,
        )
        matches = (
            bundle.combined_snapshot_id == trusted.combined_snapshot_id
            and bundle.parent_snapshot_id == trusted.parent_snapshot_id
            and bundle.platform_input_ids == trusted.platform_input_ids
            and bundle.expected_filenames == trusted.expected_filenames
            and bundle.identity_manifest == trusted.identity_manifest
            and bundle.audit_manifest == trusted.audit_manifest
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
) -> tuple[dict[str, Any], dict[str, _StableSnapshotFile]]:
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
    if not isinstance(entries, dict) or set(entries) != set(
        SNAPSHOT_FILENAMES
    ):
        raise ValueError("parent snapshot manifest files must be exactly three")

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
    if actual_filenames != set(SNAPSHOT_FILENAMES):
        raise ValueError("parent snapshot data files must be exactly three")

    for filename in SNAPSHOT_FILENAMES:
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

        state, raw = _read_stable_regular_file(
            snapshot.data_dir / filename,
            f"parent snapshot {filename}",
        )
        if state.sha256 != sha256:
            raise ValueError(f"{filename} sha256 does not match manifest")
        if (
            identity_kind == "databridge"
            and len(raw) != entry["size_bytes"]
        ):
            raise ValueError(f"{filename} size does not match manifest")
        actual_columns, actual_rows = _csv_profile(raw, filename)
        if actual_columns != columns:
            raise ValueError(f"{filename} columns do not match manifest")
        if actual_rows != row_count:
            raise ValueError(f"{filename} row_count does not match manifest")
        source_states[filename] = state

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
        ),
        raw,
    )


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
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


def _verify_file_sha256(
    path: Path,
    expected_sha256: object,
    filename: str,
) -> None:
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
    ):
        raise ValueError(f"{filename} manifest sha256 is invalid")
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f"{filename} sha256 does not match manifest")


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


def build_blackbox_input_snapshot(
    *,
    snapshot_date: str,
    engine=None,
    output_root: str | Path = BLACKBOX_SNAPSHOT_ROOT,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    data_root: str | Path = DATA_BRIDGE_ROOT,
    refresh_runtime_root: str | Path = DATA_BRIDGE_REFRESH_RUNTIME_ROOT,
    require_fresh: bool = False,
) -> BlackboxSnapshot:
    """从平台当前三频文件生成一份内容寻址的 Blackbox V2 快照。"""
    schema_version, expected_columns = _load_blackbox_schema(schema_path)
    current = _load_current_data_bridge_dataset(
        snapshot_date=snapshot_date,
        schema_path=Path(schema_path),
        data_root=Path(data_root),
        refresh_runtime_root=Path(refresh_runtime_root),
        require_fresh=require_fresh,
    )
    snapshot = create_snapshot_from_frames(
        current.dataset.frames,
        output_root=Path(output_root),
        expected_columns=expected_columns,
        schema_version=schema_version,
    )
    generation_id = _required_current_state_text(current.state, "generation_id")
    refresh_date = _required_current_state_text(current.state, "refresh_date")
    return replace(
        snapshot,
        generation_id=generation_id,
        refresh_date=refresh_date,
    )


def capture_blackbox_platform_inputs_from_connection(
    platform_input_ids: Iterable[str],
    *,
    connection,
    weekly_cutoff_key: object,
    captured_at: str | None = None,
) -> tuple[_platform_inputs.FrozenPlatformInput, ...]:
    """从同一只读事务冻结 Blackbox 显式声明的平台输入。"""
    return _platform_inputs.capture_platform_inputs_from_connection(
        platform_input_ids,
        connection=connection,
        weekly_cutoff_key=weekly_cutoff_key,
        captured_at=captured_at,
    )


def capture_blackbox_platform_inputs_from_native_generation(
    platform_input_ids: Iterable[str],
    *,
    native_generation: NativeGenerationContext,
    weekly_cutoff_key: object,
    captured_at: str | None = None,
) -> tuple[_platform_inputs.FrozenPlatformInput, ...]:
    """从已校验的 Native generation 冻结 scheduled 平台输入。"""
    return _platform_inputs.capture_platform_inputs_from_native_generation(
        platform_input_ids,
        native_generation=native_generation,
        weekly_cutoff_key=weekly_cutoff_key,
        captured_at=captured_at,
    )


@contextmanager
def open_blackbox_input_snapshot(
    *,
    snapshot_date: str,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    data_root: str | Path = DATA_BRIDGE_ROOT,
    refresh_runtime_root: str | Path = DATA_BRIDGE_REFRESH_RUNTIME_ROOT,
    snapshot_runtime_root: str | Path = BLACKBOX_RUNTIME_SNAPSHOT_ROOT,
    require_fresh: bool = False,
):
    """复制当前三频文件供一次算法运行使用，并在运行后删除副本。"""
    runtime_root = Path(snapshot_runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="blackbox-input-", dir=runtime_root))
    try:
        snapshot = build_blackbox_input_snapshot(
            snapshot_date=snapshot_date,
            output_root=temporary,
            schema_path=schema_path,
            data_root=data_root,
            refresh_runtime_root=refresh_runtime_root,
            require_fresh=require_fresh,
        )
        yield snapshot
    finally:
        _make_tree_writable(temporary)
        shutil.rmtree(temporary, ignore_errors=True)


def read_blackbox_current_state(
    *,
    schema_path: str | Path = BLACKBOX_SCHEMA_PATH,
    data_root: str | Path = DATA_BRIDGE_ROOT,
    refresh_runtime_root: str | Path = DATA_BRIDGE_REFRESH_RUNTIME_ROOT,
) -> dict[str, Any]:
    """完整校验 DataBridge current 后返回灰度补齐所需发布状态。"""
    current = check_current_dataset(
        DataBridgeRefreshConfig(
            data_root=Path(data_root),
            runtime_root=Path(refresh_runtime_root),
            schema_path=Path(schema_path),
        )
    )
    return {
        "generation_id": _required_current_state_text(
            current.state,
            "generation_id",
        ),
        "refresh_date": _required_current_state_text(
            current.state,
            "refresh_date",
        ),
    }


def _load_current_data_bridge_dataset(
    *,
    snapshot_date: str,
    schema_path: Path,
    data_root: Path,
    refresh_runtime_root: Path,
    require_fresh: bool,
):
    current = check_current_dataset(
        DataBridgeRefreshConfig(
            data_root=data_root,
            runtime_root=refresh_runtime_root,
            schema_path=schema_path,
        ),
        required_refresh_date=snapshot_date if require_fresh else None,
    )
    return current


def _required_current_state_text(state: dict | Any, field: str) -> str:
    value = state.get(field) if hasattr(state, "get") else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"DataBridge current state {field} must be non-empty")
    return value


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
        )
        return resolve_cutoffs(
            snapshot,
            feature_date,
            weekly_as_of=weekly_as_of,
            monthly_as_of=monthly_as_of,
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
    allow_legacy_v1_period_fallback: bool = False,
) -> dict[str, CutoffKeys]:
    """使用已冻结 key 集合和 caller Connection 批量解析有效截止键。"""
    resolved = _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
        snapshot_keys,
        feature_dates=feature_dates,
        connection=connection,
        schema_path=schema_path,
        allow_legacy_v1_period_fallback=allow_legacy_v1_period_fallback,
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
    monthly_selected = _data_service.select_factor_metadata(
        metadata,
        "monthly",
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
        resolved[feature_date] = _ResolvedBlackboxInputCutoffs(
            cutoff_keys=CutoffKeys(
                daily_cutoff_key=daily_dates[daily_position - 1],
                weekly_cutoff_key=weekly.effective_key,
                monthly_cutoff_key=monthly.effective_key,
            ),
            source_weekly_cutoff_key=weekly.source_key,
            source_monthly_cutoff_key=monthly.source_key,
        )
    return resolved


def _load_snapshot_cutoff_keys(snapshot: BlackboxSnapshot) -> dict[str, Any]:
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
        values = [_normalize_period_key(value, key) for value in frame[key].tolist()]
        if values != sorted(set(values)):
            raise ValueError(f"{path.name} {key} cutoff keys must be unique and ascending")
        loaded[key] = set(values)
    return loaded


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
            _normalize_period_key(key, key_column),
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


def _normalize_period_key(value: object, field: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{field} must not be empty")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"{field} must be a six-digit platform key")
    return text


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
    execution_token = os.environ.get(SCHEDULE_EXECUTION_TOKEN_ENV)
    if execution_token is None:
        return Path(output_root) / safe_scheme_id / filename
    generation_id = (
        os.environ.get(NATIVE_GENERATION_ID_ENV)
        or os.environ.get(LIVE_SOURCE_FENCE_GENERATION_ID_ENV)
    )
    safe_token = _require_safe_artifact_identity(
        execution_token,
        field=SCHEDULE_EXECUTION_TOKEN_ENV,
    )
    safe_generation_id = _require_safe_artifact_identity(
        generation_id,
        field=NATIVE_GENERATION_ID_ENV,
    )
    return (
        Path(output_root)
        / safe_scheme_id
        / "_scheduled"
        / safe_generation_id
        / safe_token
        / filename
    )


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
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="daily",
        predict_date=predict_date,
        output_root=output_root,
    )
    generation = _native_generation_context(engine)
    if generation is None:
        df = _data_service.build_daily_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
        )
    else:
        _require_frozen_cutoff(
            end_date,
            generation=generation,
            field_name="end_date",
        )
        df = _data_service.build_daily_output_from_frames(
            generation.frame("metadata"),
            generation.frame("api_wind_daily"),
            generation.frame("api_wind_derivative_daily"),
            start_date=start_date,
            end_date=end_date,
        )
    _atomic_save_output(_data_service.save_daily_output, df, path)
    read_back = _read_daily_output_csv(path)
    profile = _dataframe_profile(read_back, coverage_field="date", required_columns=("date",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="daily",
        path=path,
        dataframe=read_back,
        source="shared_data_service_daily",
        generated_at=_utc_now(),
        data_version=DAILY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata=_with_generation_provenance(
            {
                "start_date": start_date,
                "end_date": end_date,
                "predict_date": predict_date,
            },
            generation,
        ),
    )


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
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="weekly",
        predict_date=predict_date,
        output_root=output_root,
    )
    generation = _native_generation_context(engine)
    if generation is None:
        df = _data_service.build_weekly_output_from_db(
            schema_columns=schema_columns,
            start_week=start_week,
            end_week=end_week,
            as_of_date=as_of_date,
            engine=engine,
        )
    else:
        if as_of_date is not None:
            _require_frozen_cutoff(
                as_of_date,
                generation=generation,
                field_name="as_of_date",
            )
        metadata = generation.frame("metadata")
        raw = generation.frame("api_wind_weekly")
        derivative = generation.frame("api_wind_derivative_weekly")
        if schema_columns is None:
            df = _data_service.build_weekly_output_from_metadata(
                metadata,
                raw,
                derivative,
                start_week=start_week,
                end_week=end_week,
                as_of_date=as_of_date,
            )
        else:
            df = _data_service.build_weekly_output_from_frames(
                schema_columns,
                raw,
                derivative,
                start_week=start_week,
                end_week=end_week,
                as_of_date=as_of_date,
            )
    _atomic_save_output(_data_service.save_weekly_output, df, path)
    read_back = pd.read_csv(path)
    if "week_id" in read_back.columns:
        read_back["week_id"] = pd.to_numeric(read_back["week_id"], errors="coerce").astype("Int64")
    for col in read_back.columns:
        if col != "week_id":
            read_back[col] = pd.to_numeric(read_back[col], errors="coerce")
    profile = _dataframe_profile(read_back, coverage_field="week_id", required_columns=("week_id",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="weekly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_weekly",
        generated_at=_utc_now(),
        data_version=WEEKLY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata=_with_generation_provenance(
            {
                "start_week": start_week,
                "end_week": end_week,
                "as_of_date": as_of_date,
                "predict_date": predict_date,
            },
            generation,
        ),
    )


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
    path = input_artifact_path(
        scheme_id=scheme_id,
        frequency="monthly",
        predict_date=predict_date,
        output_root=output_root,
    )
    generation = _native_generation_context(engine)
    if generation is None:
        df = _data_service.build_monthly_output_from_db(
            start_date=start_date,
            end_date=end_date,
            engine=engine,
        )
    else:
        _require_frozen_cutoff(
            end_date,
            generation=generation,
            field_name="end_date",
        )
        df = _data_service.build_monthly_output_from_frames(
            generation.frame("metadata"),
            generation.frame("api_wind_monthly"),
            generation.frame("api_wind_derivative_monthly"),
            start_date=start_date,
            end_date=end_date,
        )
    _atomic_save_output(_data_service.save_monthly_output, df, path)
    read_back = _read_monthly_output_csv(path)
    profile = _dataframe_profile(read_back, coverage_field="month_id", required_columns=("month_id",))
    content_hash = _file_sha256(path)
    schema_hash = _schema_hash(read_back)
    return InputArtifact(
        scheme_id=scheme_id,
        frequency="monthly",
        path=path,
        dataframe=read_back,
        source="shared_data_service_monthly",
        generated_at=_utc_now(),
        data_version=MONTHLY_DATA_VERSION,
        artifact_id=_artifact_id(scheme_id, predict_date, content_hash),
        content_hash=content_hash,
        schema_hash=schema_hash,
        source_watermark=_source_watermark(profile),
        row_count=profile["row_count"],
        column_count=profile["column_count"],
        columns=profile["columns"],
        date_coverage=profile["date_coverage"],
        quality_flags=profile["quality_flags"],
        metadata=_with_generation_provenance(
            {
                "start_date": start_date,
                "end_date": end_date,
                "predict_date": predict_date,
            },
            generation,
        ),
    )


def _native_generation_context(
    candidate: object,
) -> NativeGenerationContext | None:
    if isinstance(candidate, NativeGenerationContext):
        return candidate
    return None


def _require_safe_artifact_identity(
    value: object,
    *,
    field: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or safe_path_part(value) != value
    ):
        raise ValueError(f"{field} must be a safe non-empty path identity")
    return value


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


def _require_frozen_cutoff(
    value: object,
    *,
    generation: NativeGenerationContext,
    field_name: str,
) -> None:
    normalized = _normalize_feature_date(value)
    if normalized > generation.feature_date:
        raise ValueError(
            f"{field_name}={normalized} is after frozen "
            f"feature_date={generation.feature_date}"
        )


def _with_generation_provenance(
    metadata: dict[str, Any],
    generation: NativeGenerationContext | None,
) -> dict[str, Any]:
    result = dict(metadata)
    if generation is None:
        return result
    result.update(
        {
            "input_generation_id": generation.generation_id,
            "input_generation_type": generation.generation_type,
            "input_generation_manifest_sha256":
                generation.manifest_sha256,
            "input_generation_dataset_content_id":
                generation.dataset_content_id,
            "input_generation_business_date": generation.business_date,
            "input_generation_feature_date": generation.feature_date,
            "input_generation_source_commit_token":
                generation.source_commit_token,
            "input_generation_schema_version": generation.schema_version,
            "input_generation_exporter_version":
                generation.exporter_version,
        }
    )
    return result


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
    return schema_version, columns


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
