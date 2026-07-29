"""Blackbox V2 当日 DataBridge 不可变输入 generation。"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from shared.blackbox_v2.snapshot import (
    BlackboxSnapshot,
    CutoffKeys,
    SNAPSHOT_FILENAMES,
)
from shared.data_bridge.refresh import CurrentDataset
from shared.data_bridge.validation import validate_dataset
from shared.native_input_generation import (
    NativeGenerationContext,
    open_native_generation,
    # 发布语义与 Native generation 一致，复用同一实现避免两处漂移。
    _publish_sealed_generation,
)


DATABRIDGE_GENERATION_TYPE = "databridge_v1"
DATABRIDGE_GENERATION_SCHEMA_VERSION = "databridge-generation-v1"
DATABRIDGE_GENERATION_EXPORTER_VERSION = "databridge-generation-exporter-v1"
DATABRIDGE_GENERATION_MANIFEST_VERSION = "databridge-generation-manifest-v1"
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "generation_id",
        "generation_type",
        "dataset_content_id",
        "business_date",
        "feature_date",
        "readiness_basis",
        "source_commit_token",
        "schema_version",
        "exporter_version",
        "created_at",
        "sealed_at",
        "upstream_generation_id",
        "upstream_business_digest",
        "native_generation_id",
        "native_manifest_sha256",
        "refresh_started_at",
        "refreshed_at",
        "published_at",
        "source_mode",
        "stability_rounds",
        "cutoffs",
        "files",
    }
)
_FILE_ENTRY_FIELDS = frozenset(
    {"path", "sha256", "size_bytes", "row_count", "columns"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABRIDGE_GENERATION_ID_PATTERN = re.compile(
    r"^databridge-[0-9a-f]{24}$"
)
DEFAULT_GENERATION_RETENTION_COUNT = 3
DEFAULT_GENERATION_MIN_FREE_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class DataBridgeGenerationContext:
    """已完整校验、可供四个 V2 共享的当日输入。"""

    generation_id: str
    generation_type: str
    dataset_content_id: str
    root_dir: Path
    data_dir: Path
    manifest_path: Path
    manifest_sha256: str
    business_date: str
    feature_date: str
    readiness_basis: str
    source_commit_token: str
    schema_version: str
    exporter_version: str
    created_at: str
    sealed_at: str
    upstream_generation_id: str
    upstream_business_digest: str
    native_generation_id: str
    native_manifest_sha256: str
    refresh_started_at: str
    refreshed_at: str
    published_at: str
    source_mode: str
    stability_rounds: int
    cutoffs: CutoffKeys
    snapshot: BlackboxSnapshot


def create_databridge_generation(
    current: CurrentDataset,
    *,
    native_generation: NativeGenerationContext,
    business_date: str,
    feature_date: str,
    output_root: str | Path,
    schema_path: str | Path,
    max_total_bytes: int | None = None,
    min_free_bytes: int | None = None,
) -> DataBridgeGenerationContext:
    """将稳定轮次与冻结 cutoff 合并为内容寻址、原子发布的 generation。"""
    normalized_business_date = _canonical_date(
        business_date,
        "business_date",
    )
    normalized_feature_date = _canonical_date(
        feature_date,
        "feature_date",
    )
    if normalized_feature_date >= normalized_business_date:
        raise ValueError(
            "DataBridge generation feature_date must be before business_date"
        )
    storage_total_limit = (
        None
        if max_total_bytes is None
        else _required_nonnegative_int(
            max_total_bytes,
            "max_total_bytes",
        )
    )
    storage_free_watermark = (
        None
        if min_free_bytes is None
        else _required_nonnegative_int(
            min_free_bytes,
            "min_free_bytes",
        )
    )
    if not isinstance(native_generation, NativeGenerationContext):
        raise TypeError(
            "native_generation must be a validated NativeGenerationContext"
        )
    if native_generation.business_date != normalized_business_date:
        raise ValueError(
            "Native generation business_date does not match DataBridge "
            f"business_date: {native_generation.business_date} != "
            f"{normalized_business_date}"
        )
    if native_generation.feature_date != normalized_feature_date:
        raise ValueError(
            "Native generation feature_date does not match DataBridge "
            f"feature_date: {native_generation.feature_date} != "
            f"{normalized_feature_date}"
        )
    native_generation = open_native_generation(
        native_generation.manifest_path,
        expected_generation_id=native_generation.generation_id,
        expected_manifest_sha256=native_generation.manifest_sha256,
        expected_business_date=normalized_business_date,
        expected_feature_date=normalized_feature_date,
    )

    state = current.state
    upstream_generation_id = _required_text(
        state.get("generation_id"),
        "upstream generation_id",
    )
    refresh_date = _canonical_date(
        state.get("refresh_date"),
        "DataBridge refresh_date",
    )
    if refresh_date != normalized_business_date:
        raise ValueError(
            "DataBridge refresh_date does not match business_date: "
            f"{refresh_date} != {normalized_business_date}"
        )
    refresh_started = _required_aware_timestamp(
        state.get("refresh_started_at"),
        "DataBridge refresh_started_at",
    )
    refreshed = _required_aware_timestamp(
        state.get("refreshed_at"),
        "DataBridge refreshed_at",
    )
    published = _required_aware_timestamp(
        state.get("published_at"),
        "DataBridge published_at",
    )
    refresh_started_local = refresh_started.astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    refreshed_local = refreshed.astimezone(ZoneInfo("Asia/Shanghai"))
    published_local = published.astimezone(ZoneInfo("Asia/Shanghai"))
    if (
        any(
            item.date().isoformat() != normalized_business_date
            for item in (
                refresh_started_local,
                refreshed_local,
                published_local,
            )
        )
        or refresh_started_local.time().replace(tzinfo=None) < time(6, 30)
    ):
        raise ValueError(
            "DataBridge generation requires a fresh full refresh "
            "started after 06:30 on the business date"
        )
    if not refresh_started <= refreshed <= published:
        raise ValueError("DataBridge refresh timeline is not monotonic")
    source_mode = _required_exact(
        state.get("source_mode"),
        "full_export",
        "DataBridge source_mode",
    )
    stability_rounds = state.get("stability_rounds")
    if (
        isinstance(stability_rounds, bool)
        or not isinstance(stability_rounds, int)
        or stability_rounds < 2
    ):
        raise ValueError(
            "DataBridge stability_rounds must be an integer at least 2"
        )
    last_attempt = state.get("last_attempt")
    if not isinstance(last_attempt, Mapping):
        raise ValueError("DataBridge last_attempt evidence is missing")
    _required_exact(
        last_attempt.get("status"),
        "success",
        "DataBridge last_attempt.status",
    )
    if (
        _canonical_date(
            last_attempt.get("refresh_date"),
            "DataBridge last_attempt.refresh_date",
        )
        != normalized_business_date
        or _required_aware_timestamp(
            last_attempt.get("started_at"),
            "DataBridge last_attempt.started_at",
        )
        != refresh_started
        or _required_aware_timestamp(
            last_attempt.get("finished_at"),
            "DataBridge last_attempt.finished_at",
        )
        != refreshed
        or last_attempt.get("error") is not None
    ):
        raise ValueError("DataBridge last_attempt evidence mismatch")
    refresh_started_at = _aware_timestamp_to_utc_text(refresh_started)
    refreshed_at = _aware_timestamp_to_utc_text(refreshed)
    published_at = _aware_timestamp_to_utc_text(published)
    validated = validate_dataset(
        current.dataset.frames,
        schema_path=schema_path,
        expected_daily_date=normalized_feature_date,
    )
    upstream_business_digest = _required_sha256(
        state.get("business_digest"),
        "upstream business_digest",
    )
    if validated.business_digest != upstream_business_digest:
        raise ValueError(
            "DataBridge current business digest changed before generation seal"
        )

    rendered = {
        filename: validated.frames[filename]
        .to_csv(index=False, lineterminator="\n")
        .encode("utf-8")
        for filename in SNAPSHOT_FILENAMES
    }
    file_entries = {
        filename: {
            "path": f"data/{filename}",
            "sha256": hashlib.sha256(rendered[filename]).hexdigest(),
            "size_bytes": len(rendered[filename]),
            "row_count": int(len(validated.frames[filename])),
            "columns": [
                str(column)
                for column in validated.frames[filename].columns
            ],
        }
        for filename in SNAPSHOT_FILENAMES
    }
    identity = {
        "schema_version": validated.schema_version,
        "files": file_entries,
    }
    dataset_content_id = hashlib.sha256(
        _canonical_json_bytes(identity)
    ).hexdigest()
    cutoffs = _derive_cutoffs(
        validated.frames,
        native_generation=native_generation,
        feature_date=normalized_feature_date,
    )
    stable_provenance = {
        "generation_type": DATABRIDGE_GENERATION_TYPE,
        "dataset_content_id": dataset_content_id,
        "business_date": normalized_business_date,
        "feature_date": normalized_feature_date,
        "readiness_basis": "UPSTREAM_SEAL",
        "source_commit_token": upstream_generation_id,
        "schema_version": validated.schema_version,
        "exporter_version": DATABRIDGE_GENERATION_EXPORTER_VERSION,
        "upstream_generation_id": upstream_generation_id,
        "upstream_business_digest": upstream_business_digest,
        "native_generation_id": native_generation.generation_id,
        "native_manifest_sha256": native_generation.manifest_sha256,
        "refresh_started_at": refresh_started_at,
        "refreshed_at": refreshed_at,
        "published_at": published_at,
        "source_mode": source_mode,
        "stability_rounds": stability_rounds,
        "cutoffs": _cutoffs_mapping(cutoffs),
    }
    generation_digest = hashlib.sha256(
        _canonical_json_bytes(stable_provenance)
    ).hexdigest()
    generation_id = f"databridge-{generation_digest[:24]}"
    created_at = _utc_now()
    manifest_base = {
        "manifest_version": DATABRIDGE_GENERATION_MANIFEST_VERSION,
        "generation_id": generation_id,
        **stable_provenance,
        "created_at": created_at,
        "files": file_entries,
    }

    root = Path(output_root)
    _ensure_private_generation_root(root)
    destination = root / generation_id
    if os.path.lexists(destination):
        return open_databridge_generation(
            destination / "manifest.json",
            expected_generation_id=generation_id,
            expected_business_date=normalized_business_date,
            expected_feature_date=normalized_feature_date,
            schema_path=schema_path,
        )

    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=root))
    try:
        data_dir = staging / "data"
        data_dir.mkdir()
        for filename in SNAPSHOT_FILENAMES:
            _write_fsynced(data_dir / filename, rendered[filename])
        _fsync_directory(data_dir)
        sealed_at = _utc_now()
        if sealed_at < created_at:
            raise ValueError("sealed_at must not be earlier than created_at")
        manifest = {
            **manifest_base,
            "sealed_at": sealed_at,
        }
        manifest_bytes = _canonical_json_bytes(manifest) + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        _write_fsynced(staging / "manifest.json", manifest_bytes)
        _fsync_directory(staging)
        candidate = open_databridge_generation(
            staging / "manifest.json",
            expected_generation_id=generation_id,
            expected_manifest_sha256=manifest_sha256,
            expected_business_date=normalized_business_date,
            expected_feature_date=normalized_feature_date,
            schema_path=schema_path,
        )
        if candidate.dataset_content_id != dataset_content_id:
            raise ValueError("DataBridge generation content identity drifted")

        _enforce_staged_databridge_generation_storage(
            root,
            max_total_bytes=storage_total_limit,
            min_free_bytes=storage_free_watermark,
        )
        _make_read_only(staging)
        _fsync_directory(data_dir)
        _fsync_directory(staging)
        try:
            _publish_sealed_generation(staging, destination)
        except OSError:
            if not os.path.lexists(destination):
                raise
            return open_databridge_generation(
                destination / "manifest.json",
                expected_generation_id=generation_id,
                expected_business_date=normalized_business_date,
                expected_feature_date=normalized_feature_date,
                schema_path=schema_path,
            )

        _fsync_directory(root)
        return open_databridge_generation(
            destination / "manifest.json",
            expected_generation_id=generation_id,
            expected_manifest_sha256=manifest_sha256,
            expected_business_date=normalized_business_date,
            expected_feature_date=normalized_feature_date,
            schema_path=schema_path,
        )
    finally:
        if staging.exists():
            _remove_tree(staging)


def find_published_databridge_generation(
    output_root: str | Path,
    *,
    business_date: str,
    feature_date: str,
    native_generation: NativeGenerationContext,
    schema_path: str | Path,
) -> DataBridgeGenerationContext | None:
    """只读扫描同日且绑定指定 Native 的唯一 DataBridge generation。

    ``.building-*`` staging 不属于已发布输入，会被忽略。任一
    final-looking 目录非法、损坏、Native 关系漂移或同日存在多代时均
    fail-closed。
    """
    if not isinstance(native_generation, NativeGenerationContext):
        raise TypeError(
            "native_generation must be a validated NativeGenerationContext"
        )
    expected_business_date = _canonical_date(
        business_date,
        "business_date",
    )
    expected_feature_date = _canonical_date(
        feature_date,
        "feature_date",
    )
    verified_native = open_native_generation(
        native_generation.manifest_path,
        expected_generation_id=native_generation.generation_id,
        expected_manifest_sha256=native_generation.manifest_sha256,
        expected_business_date=expected_business_date,
        expected_feature_date=expected_feature_date,
    )
    root = Path(output_root)
    _require_private_generation_root(root)
    matches: list[DataBridgeGenerationContext] = []
    for entry in sorted(root.iterdir(), key=lambda value: value.name):
        if not entry.name.startswith("databridge-"):
            continue
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                "DataBridge published generation rejects symlink: "
                f"{entry.name}"
            )
        if not _DATABRIDGE_GENERATION_ID_PATTERN.fullmatch(entry.name):
            raise ValueError(
                "DataBridge published generation has invalid generation id: "
                f"{entry.name}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "DataBridge published generation is not a directory: "
                f"{entry.name}"
            )
        context = open_databridge_generation(
            entry / "manifest.json",
            expected_generation_id=entry.name,
            schema_path=schema_path,
        )
        if (
            context.business_date != expected_business_date
            or context.feature_date != expected_feature_date
        ):
            continue
        if (
            context.native_generation_id != verified_native.generation_id
            or context.native_manifest_sha256
            != verified_native.manifest_sha256
        ):
            raise ValueError(
                "DataBridge published generation Native relation mismatch: "
                f"{context.generation_id}"
            )
        native_cutoffs = verified_native.cutoffs
        if (
            context.cutoffs.daily_cutoff_key != native_cutoffs["daily"]
            or context.cutoffs.weekly_cutoff_key != native_cutoffs["weekly"]
            or context.cutoffs.monthly_cutoff_key
            != native_cutoffs["monthly"]
        ):
            raise ValueError(
                "DataBridge published generation Native cutoff relation "
                f"mismatch: {context.generation_id}"
            )
        matches.append(context)
    if len(matches) > 1:
        raise ValueError(
            "ambiguous published DataBridge generations for "
            f"{expected_business_date}/{expected_feature_date}: "
            f"{sorted(item.generation_id for item in matches)}"
        )
    return matches[0] if matches else None


def cleanup_databridge_generation_debris(
    output_root: str | Path,
) -> tuple[str, ...]:
    """在外层 occurrence owner 下清理未发布 staging/GC tombstone。"""
    root = Path(output_root)
    _require_private_generation_root(root)
    candidates: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda value: value.name):
        is_building = entry.name.startswith(".building-")
        is_tombstone = entry.name.startswith(".gc-")
        if not is_building and not is_tombstone:
            continue
        valid_name = (
            re.fullmatch(r"\.building-[A-Za-z0-9_-]+", entry.name)
            if is_building
            else re.fullmatch(
                r"\.gc-databridge-[0-9a-f]{24}-[0-9a-f]{32}",
                entry.name,
            )
        )
        if valid_name is None:
            raise ValueError(
                "DataBridge generation debris has unsafe name: "
                f"{entry.name}"
            )
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                "DataBridge generation debris must not be a symlink: "
                f"{entry.name}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "DataBridge generation debris must be a directory: "
                f"{entry.name}"
            )
        _validate_debris_tree(entry)
        candidates.append(entry)
    for entry in candidates:
        _remove_tree(entry)
    if candidates:
        _fsync_directory(root)
    return tuple(entry.name for entry in candidates)


def prune_databridge_generations(
    output_root: str | Path,
    *,
    schema_path: str | Path,
    bound_generation_ids: Collection[str],
    protected_generation_ids: Collection[str] = (),
    retain_count: int = DEFAULT_GENERATION_RETENTION_COUNT,
    min_free_bytes: int = DEFAULT_GENERATION_MIN_FREE_BYTES,
) -> tuple[str, ...]:
    """拒绝旧式、由调用方自行声明绑定集合的危险清理入口。"""
    del (
        output_root,
        schema_path,
        bound_generation_ids,
        protected_generation_ids,
        retain_count,
        min_free_bytes,
    )
    raise RuntimeError(
        "DataBridge generation caller-supplied retention is disabled; "
        "use DB-resolved INVALIDATED reclaim candidates"
    )


def delete_reclaimable_databridge_generation(
    output_root: str | Path,
    *,
    schema_path: str | Path,
    generation_id: str,
    manifest_sha256: str,
    business_date: str,
    feature_date: str,
) -> bool:
    """精确删除数据库已证明可回收的一个 INVALIDATED generation。"""
    root = Path(output_root)
    _require_private_generation_root(root)
    if not _DATABRIDGE_GENERATION_ID_PATTERN.fullmatch(str(generation_id)):
        raise ValueError(
            "DataBridge reclaim candidate has invalid generation id"
        )
    expected_sha256 = _required_sha256(
        manifest_sha256,
        "manifest_sha256",
    )
    expected_business_date = _canonical_date(
        business_date,
        "business_date",
    )
    expected_feature_date = _canonical_date(
        feature_date,
        "feature_date",
    )
    source = root / generation_id
    if not os.path.lexists(source):
        return False
    scanned_info = source.lstat()
    if stat.S_ISLNK(scanned_info.st_mode) or not stat.S_ISDIR(
        scanned_info.st_mode
    ):
        raise ValueError(
            "DataBridge reclaim candidate must be a real directory"
        )
    context = open_databridge_generation(
        source / "manifest.json",
        expected_generation_id=generation_id,
        expected_manifest_sha256=expected_sha256,
        expected_business_date=expected_business_date,
        expected_feature_date=expected_feature_date,
        schema_path=schema_path,
    )
    _delete_databridge_generation(
        root,
        context,
        scanned_info=scanned_info,
        schema_path=schema_path,
    )
    return True


def preflight_databridge_generation_storage(
    output_root: str | Path,
    *,
    min_free_bytes: int,
    max_total_bytes: int,
    max_generation_count: int,
    reserve_bytes: int = 0,
    reserve_generation_count: int = 0,
) -> Mapping[str, int]:
    """校验私有存储根、文件类型、总配额和磁盘低水位。"""
    root = Path(output_root)
    _require_private_generation_root(root)
    required_free = _required_nonnegative_int(
        min_free_bytes,
        "min_free_bytes",
    )
    total_limit = _required_nonnegative_int(
        max_total_bytes,
        "max_total_bytes",
    )
    generation_limit = _required_nonnegative_int(
        max_generation_count,
        "max_generation_count",
    )
    reserved_bytes = _required_nonnegative_int(
        reserve_bytes,
        "reserve_bytes",
    )
    reserved_generations = _required_nonnegative_int(
        reserve_generation_count,
        "reserve_generation_count",
    )
    total_bytes = 0
    generation_count = 0
    for current_root, directory_names, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in directory_names:
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(
                    "DataBridge generation storage contains unsafe directory"
                )
            if current == root and _DATABRIDGE_GENERATION_ID_PATTERN.fullmatch(
                name
            ):
                generation_count += 1
        for name in filenames:
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    "DataBridge generation storage contains unsafe file"
                )
            total_bytes += int(info.st_size)
    prospective_total_bytes = total_bytes + reserved_bytes
    prospective_generation_count = (
        generation_count + reserved_generations
    )
    if prospective_total_bytes > total_limit:
        raise OSError(
            "DataBridge generation storage quota exceeded: "
            f"used={total_bytes}, reserve={reserved_bytes}, "
            f"limit={total_limit}"
        )
    if prospective_generation_count > generation_limit:
        raise OSError(
            "DataBridge generation count limit exceeded: "
            f"count={generation_count}, "
            f"reserve={reserved_generations}, "
            f"limit={generation_limit}"
        )
    free_bytes = int(shutil.disk_usage(root).free)
    if free_bytes < required_free:
        raise OSError(
            "DataBridge generation storage below disk low watermark: "
            f"free={free_bytes}, required={required_free}"
        )
    return {
        "total_bytes": total_bytes,
        "generation_count": generation_count,
        "prospective_total_bytes": prospective_total_bytes,
        "prospective_generation_count":
            prospective_generation_count,
        "free_bytes": free_bytes,
    }


def _enforce_staged_databridge_generation_storage(
    root: Path,
    *,
    max_total_bytes: int | None,
    min_free_bytes: int | None,
) -> None:
    """按已落盘 staging 的实际字节执行发布前硬配额检查。"""
    if max_total_bytes is None and min_free_bytes is None:
        return
    preflight_databridge_generation_storage(
        root,
        min_free_bytes=(
            0 if min_free_bytes is None else min_free_bytes
        ),
        max_total_bytes=(
            (1 << 63) - 1
            if max_total_bytes is None
            else max_total_bytes
        ),
        max_generation_count=(1 << 63) - 1,
    )


def open_databridge_generation(
    manifest_path: str | Path,
    *,
    expected_generation_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_business_date: str | None = None,
    expected_feature_date: str | None = None,
    schema_path: str | Path,
) -> DataBridgeGenerationContext:
    """重新打开并 rehash manifest 与三文件，拒绝替换、额外文件和漂移。"""
    path = Path(manifest_path)
    if path.name != "manifest.json":
        raise ValueError(
            "DataBridge generation manifest path must end in manifest.json"
        )
    root = path.parent
    _require_directory(root, "root")
    _require_private_generation_root(root.parent)
    data_dir = root / "data"
    _require_directory(data_dir, "data")
    if {entry.name for entry in root.iterdir()} != {"manifest.json", "data"}:
        raise ValueError("DataBridge generation root entries mismatch")
    if {entry.name for entry in data_dir.iterdir()} != set(
        SNAPSHOT_FILENAMES
    ):
        raise ValueError("DataBridge generation data entries mismatch")

    manifest_bytes = _read_regular_file(path, "manifest")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and not hmac.compare_digest(
            manifest_sha256,
            _required_sha256(
                expected_manifest_sha256,
                "expected manifest sha256",
            ),
        )
    ):
        raise ValueError("DataBridge generation manifest hash mismatch")
    try:
        manifest = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "DataBridge generation manifest is invalid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _MANIFEST_FIELDS
        or manifest_bytes != _canonical_json_bytes(manifest) + b"\n"
    ):
        raise ValueError(
            "DataBridge generation manifest shape/canonical form mismatch"
        )
    if (
        manifest.get("manifest_version")
        != DATABRIDGE_GENERATION_MANIFEST_VERSION
        or manifest.get("generation_type") != DATABRIDGE_GENERATION_TYPE
    ):
        raise ValueError("DataBridge generation manifest version/type mismatch")

    generation_id = _required_text(
        manifest.get("generation_id"),
        "generation_id",
    )
    business_date = _canonical_date(
        manifest.get("business_date"),
        "business_date",
    )
    feature_date = _canonical_date(
        manifest.get("feature_date"),
        "feature_date",
    )
    if feature_date >= business_date:
        raise ValueError(
            "DataBridge generation feature_date must be before business_date"
        )
    if (
        expected_generation_id is not None
        and generation_id != expected_generation_id
    ):
        raise ValueError("DataBridge generation id mismatch")
    if (
        expected_business_date is not None
        and business_date
        != _canonical_date(
            expected_business_date,
            "expected business_date",
        )
    ):
        raise ValueError("DataBridge generation business_date mismatch")
    if (
        expected_feature_date is not None
        and feature_date
        != _canonical_date(
            expected_feature_date,
            "expected feature_date",
        )
    ):
        raise ValueError("DataBridge generation feature_date mismatch")

    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != set(
        SNAPSHOT_FILENAMES
    ):
        raise ValueError("DataBridge generation file manifest mismatch")
    frames: dict[str, pd.DataFrame] = {}
    for filename in SNAPSHOT_FILENAMES:
        entry = entries.get(filename)
        if not isinstance(entry, dict) or set(entry) != _FILE_ENTRY_FIELDS:
            raise ValueError(
                f"DataBridge generation file entry mismatch: {filename}"
            )
        if entry.get("path") != f"data/{filename}":
            raise ValueError(
                f"DataBridge generation file path mismatch: {filename}"
            )
        raw = _read_regular_file(data_dir / filename, filename)
        if not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            _required_sha256(entry.get("sha256"), f"{filename} sha256"),
        ):
            raise ValueError(
                f"DataBridge generation file hash mismatch: {filename}"
            )
        if len(raw) != int(entry.get("size_bytes", -1)):
            raise ValueError(
                f"DataBridge generation file size mismatch: {filename}"
            )
        frame = _parse_csv(raw, filename)
        if list(frame.columns) != entry.get("columns"):
            raise ValueError(
                f"DataBridge generation file schema mismatch: {filename}"
            )
        if len(frame) != int(entry.get("row_count", -1)):
            raise ValueError(
                f"DataBridge generation row count mismatch: {filename}"
            )
        frames[filename] = frame

    validated = validate_dataset(
        frames,
        schema_path=schema_path,
        expected_daily_date=feature_date,
    )
    identity = {
        "schema_version": validated.schema_version,
        "files": entries,
    }
    dataset_content_id = hashlib.sha256(
        _canonical_json_bytes(identity)
    ).hexdigest()
    if manifest.get("dataset_content_id") != dataset_content_id:
        raise ValueError(
            "DataBridge generation dataset content identity mismatch"
        )
    if manifest.get("schema_version") != validated.schema_version:
        raise ValueError("DataBridge generation schema version mismatch")
    upstream_digest = _required_sha256(
        manifest.get("upstream_business_digest"),
        "upstream business digest",
    )
    if upstream_digest != validated.business_digest:
        raise ValueError(
            "DataBridge generation upstream business digest mismatch"
        )
    cutoffs = _parse_cutoffs(manifest.get("cutoffs"))
    _validate_cutoffs_exist(frames, cutoffs, feature_date=feature_date)
    stable_provenance = {
        "generation_type": DATABRIDGE_GENERATION_TYPE,
        "dataset_content_id": dataset_content_id,
        "business_date": business_date,
        "feature_date": feature_date,
        "readiness_basis": _required_exact(
            manifest.get("readiness_basis"),
            "UPSTREAM_SEAL",
            "readiness_basis",
        ),
        "source_commit_token": _required_text(
            manifest.get("source_commit_token"),
            "source_commit_token",
        ),
        "schema_version": validated.schema_version,
        "exporter_version": _required_exact(
            manifest.get("exporter_version"),
            DATABRIDGE_GENERATION_EXPORTER_VERSION,
            "exporter_version",
        ),
        "upstream_generation_id": _required_text(
            manifest.get("upstream_generation_id"),
            "upstream_generation_id",
        ),
        "upstream_business_digest": upstream_digest,
        "native_generation_id": _required_text(
            manifest.get("native_generation_id"),
            "native_generation_id",
        ),
        "native_manifest_sha256": _required_sha256(
            manifest.get("native_manifest_sha256"),
            "native_manifest_sha256",
        ),
        "refresh_started_at": _canonical_utc_timestamp(
            manifest.get("refresh_started_at"),
            "refresh_started_at",
        ),
        "refreshed_at": _canonical_utc_timestamp(
            manifest.get("refreshed_at"),
            "refreshed_at",
        ),
        "published_at": _canonical_utc_timestamp(
            manifest.get("published_at"),
            "published_at",
        ),
        "source_mode": _required_exact(
            manifest.get("source_mode"),
            "full_export",
            "source_mode",
        ),
        "stability_rounds": _required_stability_rounds(
            manifest.get("stability_rounds")
        ),
        "cutoffs": _cutoffs_mapping(cutoffs),
    }
    if (
        stable_provenance["source_commit_token"]
        != stable_provenance["upstream_generation_id"]
    ):
        raise ValueError(
            "DataBridge generation source commit token mismatch"
        )
    expected_id = "databridge-" + hashlib.sha256(
        _canonical_json_bytes(stable_provenance)
    ).hexdigest()[:24]
    if generation_id != expected_id:
        raise ValueError(
            "DataBridge generation id does not match stable provenance"
        )
    created_at = _canonical_utc_timestamp(
        manifest.get("created_at"),
        "created_at",
    )
    sealed_at = _canonical_utc_timestamp(
        manifest.get("sealed_at"),
        "sealed_at",
    )
    if sealed_at < created_at:
        raise ValueError("DataBridge generation sealed_at is before created_at")
    refresh_started_at = str(stable_provenance["refresh_started_at"])
    refreshed_at = str(stable_provenance["refreshed_at"])
    published_at = str(stable_provenance["published_at"])
    _validate_frozen_refresh_timeline(
        business_date=business_date,
        refresh_started_at=refresh_started_at,
        refreshed_at=refreshed_at,
        published_at=published_at,
        created_at=created_at,
        sealed_at=sealed_at,
    )

    snapshot_id = f"snapshot-{dataset_content_id[:24]}"
    snapshot = BlackboxSnapshot(
        snapshot_id=snapshot_id,
        root_dir=root,
        data_dir=data_dir,
        manifest_path=path,
        schema_version=validated.schema_version,
        generation_id=generation_id,
        refresh_date=business_date,
    )
    return DataBridgeGenerationContext(
        generation_id=generation_id,
        generation_type=DATABRIDGE_GENERATION_TYPE,
        dataset_content_id=dataset_content_id,
        root_dir=root,
        data_dir=data_dir,
        manifest_path=path,
        manifest_sha256=manifest_sha256,
        business_date=business_date,
        feature_date=feature_date,
        readiness_basis="UPSTREAM_SEAL",
        source_commit_token=str(stable_provenance["source_commit_token"]),
        schema_version=validated.schema_version,
        exporter_version=DATABRIDGE_GENERATION_EXPORTER_VERSION,
        created_at=created_at,
        sealed_at=sealed_at,
        upstream_generation_id=str(
            stable_provenance["upstream_generation_id"]
        ),
        upstream_business_digest=upstream_digest,
        native_generation_id=str(
            stable_provenance["native_generation_id"]
        ),
        native_manifest_sha256=str(
            stable_provenance["native_manifest_sha256"]
        ),
        refresh_started_at=refresh_started_at,
        refreshed_at=refreshed_at,
        published_at=published_at,
        source_mode=str(stable_provenance["source_mode"]),
        stability_rounds=int(stable_provenance["stability_rounds"]),
        cutoffs=cutoffs,
        snapshot=snapshot,
    )


def _derive_cutoffs(
    frames: Mapping[str, pd.DataFrame],
    *,
    native_generation: NativeGenerationContext,
    feature_date: str,
) -> CutoffKeys:
    feature = date.fromisoformat(feature_date)
    daily_values = pd.to_datetime(
        frames["daily_output.csv"]["date"],
        errors="coerce",
    ).dt.date
    eligible_daily = daily_values[
        daily_values.notna() & daily_values.le(feature)
    ]
    if eligible_daily.empty:
        raise ValueError(
            f"DataBridge daily output has no key on/before {feature_date}"
        )
    daily_cutoff = max(eligible_daily).isoformat()
    if daily_cutoff != feature_date:
        raise ValueError(
            "DataBridge daily cutoff must equal feature_date"
        )
    cutoffs = CutoffKeys(
        daily_cutoff_key=daily_cutoff,
        weekly_cutoff_key=_period_cutoff(
            native_generation.frame("weekly_cutoff_index"),
            key_column="week_id",
            feature_date=feature_date,
        ),
        monthly_cutoff_key=_period_cutoff(
            native_generation.frame("monthly_cutoff_index"),
            key_column="month_id",
            feature_date=feature_date,
        ),
    )
    _validate_cutoffs_exist(frames, cutoffs, feature_date=feature_date)
    return cutoffs


def _period_cutoff(
    index: pd.DataFrame,
    *,
    key_column: str,
    feature_date: str,
) -> str:
    required = {key_column, "available_date"}
    if index.empty or not required.issubset(index.columns):
        raise ValueError(
            f"Native frozen cutoff index has no {key_column}"
        )
    available_dates = pd.to_datetime(
        index["available_date"],
        errors="coerce",
    ).dt.date
    eligible = index[
        available_dates.notna()
        & available_dates.le(date.fromisoformat(feature_date))
    ]
    if eligible.empty:
        raise ValueError(
            f"Native frozen cutoff index has no {key_column} "
            f"for {feature_date}"
        )
    values = [_period_key(value, key_column) for value in eligible[key_column]]
    return max(values)


def _validate_cutoffs_exist(
    frames: Mapping[str, pd.DataFrame],
    cutoffs: CutoffKeys,
    *,
    feature_date: str,
) -> None:
    daily_keys = {
        pd.to_datetime(value, errors="raise").date().isoformat()
        for value in frames["daily_output.csv"]["date"]
    }
    weekly_keys = {
        _period_key(value, "week_id")
        for value in frames["weekly_output.csv"]["week_id"]
    }
    monthly_keys = {
        _period_key(value, "month_id")
        for value in frames["monthly_output.csv"]["month_id"]
    }
    if cutoffs.daily_cutoff_key not in daily_keys:
        raise ValueError("DataBridge daily cutoff is absent from generation")
    if cutoffs.daily_cutoff_key != feature_date:
        raise ValueError(
            "DataBridge daily cutoff must equal feature_date"
        )
    if cutoffs.weekly_cutoff_key not in weekly_keys:
        raise ValueError("DataBridge weekly cutoff is absent from generation")
    if cutoffs.monthly_cutoff_key not in monthly_keys:
        raise ValueError("DataBridge monthly cutoff is absent from generation")


def _parse_cutoffs(value: object) -> CutoffKeys:
    if not isinstance(value, dict) or set(value) != {
        "daily_cutoff_key",
        "weekly_cutoff_key",
        "monthly_cutoff_key",
    }:
        raise ValueError("DataBridge generation cutoffs shape mismatch")
    daily = _canonical_date(value.get("daily_cutoff_key"), "daily cutoff")
    return CutoffKeys(
        daily_cutoff_key=daily,
        weekly_cutoff_key=_period_key(
            value.get("weekly_cutoff_key"),
            "weekly cutoff",
        ),
        monthly_cutoff_key=_period_key(
            value.get("monthly_cutoff_key"),
            "monthly cutoff",
        ),
    )


def _cutoffs_mapping(cutoffs: CutoffKeys) -> dict[str, str]:
    return {
        "daily_cutoff_key": cutoffs.daily_cutoff_key,
        "weekly_cutoff_key": cutoffs.weekly_cutoff_key,
        "monthly_cutoff_key": cutoffs.monthly_cutoff_key,
    }


def _parse_csv(raw: bytes, filename: str) -> pd.DataFrame:
    try:
        text = raw.decode("utf-8")
        header = next(csv.reader(io.StringIO(text)))
        if not header or len(header) != len(set(header)):
            raise ValueError("duplicate/empty header")
        return pd.read_csv(
            io.StringIO(text),
            dtype="string",
            keep_default_na=False,
        )
    except (UnicodeError, csv.Error, pd.errors.ParserError) as exc:
        raise ValueError(
            f"DataBridge generation CSV is invalid: {filename}"
        ) from exc


def _write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(
                "DataBridge generation fsync target must be regular"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(root: Path) -> None:
    data = root / "data"
    for path in data.iterdir():
        path.chmod(0o444)
        _fsync_regular_file(path)
    data.chmod(0o555)
    manifest = root / "manifest.json"
    manifest.chmod(0o444)
    _fsync_regular_file(manifest)




def _remove_tree(root: Path) -> None:
    if not os.path.lexists(root):
        return
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(
            "DataBridge generation cleanup root must be a real directory"
        )
    os.chmod(root, 0o755, follow_symlinks=False)
    data = root / "data"
    if os.path.lexists(data):
        data_info = data.lstat()
        if stat.S_ISLNK(data_info.st_mode) or not stat.S_ISDIR(
            data_info.st_mode
        ):
            raise ValueError(
                "DataBridge generation cleanup data must be a real directory"
            )
        os.chmod(data, 0o755, follow_symlinks=False)
        for path in data.iterdir():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    "DataBridge generation cleanup refuses non-regular "
                    f"entry: {path.name}"
                )
            os.chmod(path, 0o644, follow_symlinks=False)
    manifest = root / "manifest.json"
    if os.path.lexists(manifest):
        manifest_info = manifest.lstat()
        if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(
            manifest_info.st_mode
        ):
            raise ValueError(
                "DataBridge generation cleanup manifest must be regular"
            )
        os.chmod(manifest, 0o644, follow_symlinks=False)
    shutil.rmtree(root)


def _validated_generation_id_set(
    values: Collection[str],
    field_name: str,
) -> set[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise TypeError(f"{field_name} must be a collection of generation ids")
    result: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not _DATABRIDGE_GENERATION_ID_PATTERN.fullmatch(value)
        ):
            raise ValueError(
                f"{field_name} contains an invalid generation id: {value!r}"
            )
        result.add(value)
    return result


def _required_nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _delete_databridge_generation(
    root: Path,
    context: DataBridgeGenerationContext,
    *,
    scanned_info: os.stat_result,
    schema_path: str | Path,
) -> None:
    source = root / context.generation_id
    current_info = source.lstat()
    if (
        stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or current_info.st_dev != scanned_info.st_dev
        or current_info.st_ino != scanned_info.st_ino
    ):
        raise ValueError(
            "DataBridge generation retention candidate changed during "
            f"collection: {context.generation_id}"
        )
    open_databridge_generation(
        source / "manifest.json",
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
        schema_path=schema_path,
    )
    tombstone = root / (
        f".gc-{context.generation_id}-{uuid.uuid4().hex}"
    )
    if os.path.lexists(tombstone):
        raise ValueError("DataBridge generation retention tombstone collision")
    try:
        os.rename(source, tombstone)
    except PermissionError:
        # 已发布 generation 目录是 0o555。有继承 ACL 的部署可直接 rename；
        # 没有时 POSIX 要求对被移动目录本身有写权限，此时才解开写位重试。
        # 上方已确认它是 dev/ino 匹配的真目录，`_remove_tree` 随后也会做
        # 同样的放宽。
        os.chmod(source, 0o755, follow_symlinks=False)
        os.rename(source, tombstone)
    _fsync_directory(root)
    tombstone_info = tombstone.lstat()
    if (
        stat.S_ISLNK(tombstone_info.st_mode)
        or not stat.S_ISDIR(tombstone_info.st_mode)
        or tombstone_info.st_dev != current_info.st_dev
        or tombstone_info.st_ino != current_info.st_ino
    ):
        raise ValueError(
            "DataBridge generation retention tombstone identity mismatch"
        )
    _remove_tree(tombstone)
    _fsync_directory(root)


def _require_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            f"DataBridge generation {label} directory is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(
            f"DataBridge generation {label} must be a real directory"
        )


def _require_private_generation_root(path: Path) -> None:
    _require_directory(path, "output root")
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise ValueError("DataBridge generation root has another owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            "DataBridge generation root must be private "
            "(mode 0700 or stricter)"
        )


def _ensure_private_generation_root(path: Path) -> None:
    """只创建新私有根；既有非私有根必须由部署迁移显式修复。"""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_generation_root(path)


def _validate_debris_tree(path: Path) -> None:
    for current_root, directory_names, filenames in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in (*directory_names, *filenames):
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(
                    "DataBridge generation debris contains a symlink"
                )
            if not (
                stat.S_ISDIR(info.st_mode)
                or stat.S_ISREG(info.st_mode)
            ):
                raise ValueError(
                    "DataBridge generation debris contains a special file"
                )


def _read_regular_file(path: Path, label: str) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            f"DataBridge generation file is missing: {label}"
        ) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(
            f"DataBridge generation file must be regular: {label}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise ValueError(
                f"DataBridge generation file changed while opening: {label}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError(
            f"DataBridge generation file changed while reading: {label}"
        )
    return b"".join(chunks)


def _canonical_date(value: object, field: str) -> str:
    try:
        normalized = date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
    if str(value) != normalized:
        raise ValueError(f"{field} must use canonical YYYY-MM-DD")
    return normalized


def _period_key(value: object, field: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{field} must not be empty")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"{field} must be a six-digit key")
    return text


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _required_exact(value: object, expected: str, field: str) -> str:
    actual = _required_text(value, field)
    if actual != expected:
        raise ValueError(f"{field} must equal {expected}")
    return actual


def _required_sha256(value: object, field: str) -> str:
    text = _required_text(value, field)
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return text


def _required_stability_rounds(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 2
    ):
        raise ValueError(
            "stability_rounds must be an integer at least 2"
        )
    return value


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_utc_timestamp(value: object, field: str) -> str:
    text = _required_text(value, field)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        text,
    ):
        raise ValueError(f"{field} must be canonical UTC")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    canonical = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if canonical != text:
        raise ValueError(f"{field} must be canonical UTC")
    return text


def _required_aware_timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _aware_timestamp_to_utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_frozen_refresh_timeline(
    *,
    business_date: str,
    refresh_started_at: str,
    refreshed_at: str,
    published_at: str,
    created_at: str,
    sealed_at: str,
) -> None:
    parsed = [
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        for value in (
            refresh_started_at,
            refreshed_at,
            published_at,
            created_at,
            sealed_at,
        )
    ]
    if parsed != sorted(parsed):
        raise ValueError(
            "DataBridge frozen refresh/generation timeline is not monotonic"
        )
    shanghai = ZoneInfo("Asia/Shanghai")
    local_started, local_refreshed, local_published = (
        value.astimezone(shanghai)
        for value in parsed[:3]
    )
    if (
        any(
            item.date().isoformat() != business_date
            for item in (
                local_started,
                local_refreshed,
                local_published,
            )
        )
        or local_started.time().replace(tzinfo=None) < time(6, 30)
    ):
        raise ValueError(
            "DataBridge frozen refresh was not started after 06:30 "
            "on the business date"
        )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"DataBridge generation manifest duplicate key: {key}"
            )
        result[key] = value
    return result
