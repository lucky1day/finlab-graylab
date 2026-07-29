from __future__ import annotations

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
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.engine import Connection, Engine

from shared import calendar_service as _calendar_service
from shared import data_contract as _data_contract
from shared import data_service as _data_service


NATIVE_GENERATION_SCHEMA_VERSION = "native-generation-v1"
NATIVE_GENERATION_EXPORTER_VERSION = "native-generation-exporter-v1"
NATIVE_GENERATION_MANIFEST_VERSION = "native-generation-manifest-v2"
NATIVE_GENERATION_TYPE = "native_source"
NATIVE_GENERATION_FILENAMES = (
    "metadata.csv",
    "api_wind_daily.csv",
    "api_wind_derivative_daily.csv",
    "api_wind_weekly.csv",
    "api_wind_derivative_weekly.csv",
    "api_wind_monthly.csv",
    "api_wind_derivative_monthly.csv",
    "api_wind_date.csv",
    "t_trade_calendar.csv",
    "weekly_cutoff_index.csv",
    "monthly_cutoff_index.csv",
)
_FACTOR_FILENAMES = (
    "api_wind_daily.csv",
    "api_wind_derivative_daily.csv",
    "api_wind_weekly.csv",
    "api_wind_derivative_weekly.csv",
    "api_wind_monthly.csv",
    "api_wind_derivative_monthly.csv",
)
_READINESS_BASES = frozenset({"CLOCK_CONTRACT"})
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CLOCK_CONTRACT_CUTOFF = datetime_time(6, 30)
_SOURCE_EVIDENCE_VERSION = "native-source-watermark-v1"
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
        "source_contract_cutoff",
        "capture_not_after",
        "snapshot_started_at",
        "source_evidence",
        "source_evidence_sha256",
        "schema_version",
        "exporter_version",
        "created_at",
        "sealed_at",
        "cutoffs",
        "files",
    }
)
_FILE_ENTRY_FIELDS = frozenset(
    {
        "path",
        "sha256",
        "size_bytes",
        "row_count",
        "columns",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NATIVE_GENERATION_ID_PATTERN = re.compile(r"^native-[0-9a-f]{24}$")
DEFAULT_GENERATION_RETENTION_COUNT = 3
DEFAULT_GENERATION_MIN_FREE_BYTES = 2 * 1024**3
_EXACT_COLUMNS = {
    "api_wind_daily.csv": (
        "rdate",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_derivative_daily.csv": (
        "rdate",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_weekly.csv": (
        "rdate",
        "week_id",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_derivative_weekly.csv": (
        "rdate",
        "week_id",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_monthly.csv": (
        "rdate",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_derivative_monthly.csv": (
        "rdate",
        "month_id",
        "indicators_code",
        "indicators_value",
    ),
    "api_wind_date.csv": ("rdate", "week_id"),
    "t_trade_calendar.csv": ("rdate", "trade_flag"),
    "weekly_cutoff_index.csv": ("week_id", "available_date"),
    "monthly_cutoff_index.csv": ("month_id", "available_date"),
}


@dataclass(frozen=True)
class NativeGenerationContext:
    """一次已完整校验的 Native 不可变输入 generation。"""

    generation_id: str
    generation_type: str
    dataset_content_id: str
    root_dir: Path
    manifest_path: Path
    manifest_sha256: str
    business_date: str
    feature_date: str
    source_commit_token: str
    readiness_basis: str
    schema_version: str
    exporter_version: str
    created_at: str
    sealed_at: str
    _cutoffs_json: str = field(repr=False)
    _manifest_json: str = field(repr=False)
    _frames: Mapping[str, pd.DataFrame] = field(repr=False)

    @property
    def manifest(self) -> dict[str, Any]:
        """返回 manifest 的隔离副本。"""
        return json.loads(self._manifest_json)

    @property
    def files(self) -> dict[str, Path]:
        """返回逻辑文件名到只读文件路径的映射。"""
        return {
            filename: self.root_dir / filename
            for filename in NATIVE_GENERATION_FILENAMES
        }

    @property
    def cutoffs(self) -> dict[str, str]:
        """返回三频截止键的隔离副本。"""
        return json.loads(self._cutoffs_json)

    def frame(self, name: str) -> pd.DataFrame:
        """返回已校验 frozen bytes 对应 DataFrame 的深副本。"""
        filename = str(name).strip()
        if not filename.endswith(".csv"):
            filename = f"{filename}.csv"
        try:
            frame = self._frames[filename]
        except KeyError as exc:
            raise KeyError(f"unknown Native generation frame: {name}") from exc
        return frame.copy(deep=True)

    def dispose(self) -> None:
        """与 Engine 生命周期接口兼容；generation context 无需释放资源。"""
        return None


def create_native_generation(
    engine: Engine,
    *,
    business_date: str,
    feature_date: str,
    source_commit_token: str | None = None,
    output_root: str | Path,
    readiness_basis: str = "CLOCK_CONTRACT",
    schema_version: str = NATIVE_GENERATION_SCHEMA_VERSION,
    exporter_version: str = NATIVE_GENERATION_EXPORTER_VERSION,
    capture_not_after: datetime | None = None,
    source_contract_cutoff: datetime | None = None,
    max_total_bytes: int | None = None,
    min_free_bytes: int | None = None,
    _snapshot_clock: Callable[[], datetime] | None = None,
) -> NativeGenerationContext:
    """在单一 MySQL 一致性快照中导出并原子发布 Native generation。"""
    created_at = _utc_now_text()
    normalized_business_date = _canonical_date(business_date, "business_date")
    normalized_feature_date = _canonical_date(feature_date, "feature_date")
    if normalized_feature_date > normalized_business_date:
        raise ValueError("feature_date must not be after business_date")
    expected_source_token = (
        _required_text(
            source_commit_token,
            "source_commit_token",
        )
        if source_commit_token is not None
        else None
    )
    normalized_readiness = _required_text(readiness_basis, "readiness_basis")
    if normalized_readiness not in _READINESS_BASES:
        if normalized_readiness == "UPSTREAM_SEAL":
            raise ValueError(
                "UPSTREAM_SEAL is not supported by the Native DB exporter"
            )
        raise ValueError(
            f"readiness_basis must be one of {sorted(_READINESS_BASES)}"
        )
    normalized_contract_cutoff, normalized_capture_deadline = (
        _validate_clock_contract_parameters(
            business_date=normalized_business_date,
            source_contract_cutoff=source_contract_cutoff,
            capture_not_after=capture_not_after,
        )
    )
    if schema_version != NATIVE_GENERATION_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported Native generation schema_version: {schema_version}"
        )
    normalized_exporter = _required_text(exporter_version, "exporter_version")
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

    root = Path(output_root)
    _ensure_private_generation_root(root)
    frames, source_evidence, snapshot_started_at = _export_frozen_frames(
        engine,
        feature_date=normalized_feature_date,
        capture_not_after=normalized_capture_deadline,
        source_contract_cutoff=normalized_contract_cutoff,
        snapshot_clock=_snapshot_clock,
    )
    source_evidence_payload = _source_evidence_payload(
        source_evidence,
        expected_feature_date=normalized_feature_date,
    )
    source_evidence_sha256 = hashlib.sha256(
        _canonical_json_bytes(source_evidence_payload)
    ).hexdigest()
    normalized_source_token = _required_text(
        source_evidence.source_commit_token,
        "source_commit_evidence.source_commit_token",
    )
    if not hmac.compare_digest(
        normalized_source_token,
        source_evidence_sha256,
    ):
        raise RuntimeError(
            "source_commit_token does not match the canonical source evidence"
        )
    if (
        expected_source_token is not None
        and expected_source_token != normalized_source_token
    ):
        raise RuntimeError(
            "source_commit_token was captured outside the Native snapshot: "
            f"expected={expected_source_token} "
            f"snapshot={normalized_source_token}"
        )
    rendered = {
        filename: _render_csv(frames[filename])
        for filename in NATIVE_GENERATION_FILENAMES
    }
    cutoffs = _derive_cutoffs(
        frames,
        feature_date=normalized_feature_date,
    )
    file_entries = {
        filename: {
            "path": filename,
            "sha256": hashlib.sha256(rendered[filename]).hexdigest(),
            "size_bytes": len(rendered[filename]),
            "row_count": int(len(frames[filename])),
            "columns": [str(column) for column in frames[filename].columns],
        }
        for filename in NATIVE_GENERATION_FILENAMES
    }
    identity = {
        "schema_version": schema_version,
        "files": file_entries,
    }
    dataset_content_id = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    stable_provenance = _stable_generation_provenance(
        generation_type=NATIVE_GENERATION_TYPE,
        dataset_content_id=dataset_content_id,
        source_commit_token=normalized_source_token,
        business_date=normalized_business_date,
        feature_date=normalized_feature_date,
        readiness_basis=normalized_readiness,
        schema_version=schema_version,
        exporter_version=normalized_exporter,
    )
    generation_digest = hashlib.sha256(
        _canonical_json_bytes(stable_provenance)
    ).hexdigest()
    generation_id = f"native-{generation_digest[:24]}"
    contract_manifest_fields = {
        "source_contract_cutoff": _utc_timestamp_text(
            normalized_contract_cutoff
        ),
        "capture_not_after": _utc_timestamp_text(
            normalized_capture_deadline
        ),
        "snapshot_started_at": _utc_timestamp_text(snapshot_started_at),
        "source_evidence": source_evidence_payload,
        "source_evidence_sha256": source_evidence_sha256,
    }
    manifest_base = {
        "manifest_version": NATIVE_GENERATION_MANIFEST_VERSION,
        "generation_id": generation_id,
        "generation_type": NATIVE_GENERATION_TYPE,
        "dataset_content_id": dataset_content_id,
        "business_date": normalized_business_date,
        "feature_date": normalized_feature_date,
        "readiness_basis": normalized_readiness,
        "source_commit_token": normalized_source_token,
        **contract_manifest_fields,
        "schema_version": schema_version,
        "exporter_version": normalized_exporter,
        "created_at": created_at,
        "cutoffs": cutoffs,
        "files": file_entries,
    }
    destination = root / generation_id
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=root))
    try:
        for filename in NATIVE_GENERATION_FILENAMES:
            _write_fsynced(staging / filename, rendered[filename])
        sealed_at = _utc_now_text()
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

        candidate = open_native_generation(
            staging / "manifest.json",
            expected_generation_id=generation_id,
            expected_manifest_sha256=manifest_sha256,
            expected_business_date=normalized_business_date,
            expected_feature_date=normalized_feature_date,
        )
        if candidate.dataset_content_id != dataset_content_id:
            raise ValueError("Native generation candidate content ID drifted")

        if os.path.lexists(destination):
            existing = open_native_generation(
                destination / "manifest.json",
                expected_generation_id=generation_id,
                expected_business_date=normalized_business_date,
                expected_feature_date=normalized_feature_date,
            )
            _require_same_stable_provenance(existing, candidate)
            return existing

        _enforce_staged_native_generation_storage(
            root,
            max_total_bytes=storage_total_limit,
            min_free_bytes=storage_free_watermark,
        )
        _make_generation_read_only(staging)
        _fsync_directory(staging)
        try:
            _publish_sealed_generation(staging, destination)
        except OSError:
            if not os.path.lexists(destination):
                raise
            existing = open_native_generation(
                destination / "manifest.json",
                expected_generation_id=generation_id,
                expected_business_date=normalized_business_date,
                expected_feature_date=normalized_feature_date,
            )
            _require_same_stable_provenance(existing, candidate)
            return existing
        _fsync_directory(root)
        return open_native_generation(
            destination / "manifest.json",
            expected_generation_id=generation_id,
            expected_manifest_sha256=manifest_sha256,
            expected_business_date=normalized_business_date,
            expected_feature_date=normalized_feature_date,
        )
    finally:
        if staging.exists():
            _remove_tree(staging)


def find_published_native_generation(
    output_root: str | Path,
    *,
    business_date: str,
    feature_date: str,
) -> NativeGenerationContext | None:
    """只读扫描同日已完整发布的唯一 Native generation。

    随机命名的 ``.building-*`` 只属于未提交 staging，本入口忽略它们。
    任一 final-looking 目录非法、损坏或同日存在多代时均 fail-closed。
    """
    root = Path(output_root)
    _require_private_generation_root(root)
    expected_business_date = _canonical_date(
        business_date,
        "business_date",
    )
    expected_feature_date = _canonical_date(
        feature_date,
        "feature_date",
    )
    matches: list[NativeGenerationContext] = []
    for entry in sorted(root.iterdir(), key=lambda value: value.name):
        if not entry.name.startswith("native-"):
            continue
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"Native published generation rejects symlink: {entry.name}"
            )
        if not _NATIVE_GENERATION_ID_PATTERN.fullmatch(entry.name):
            raise ValueError(
                "Native published generation has invalid generation id: "
                f"{entry.name}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "Native published generation is not a directory: "
                f"{entry.name}"
            )
        context = open_native_generation(
            entry / "manifest.json",
            expected_generation_id=entry.name,
        )
        if (
            context.business_date == expected_business_date
            and context.feature_date == expected_feature_date
        ):
            matches.append(context)
    if len(matches) > 1:
        raise ValueError(
            "ambiguous published Native generations for "
            f"{expected_business_date}/{expected_feature_date}: "
            f"{sorted(item.generation_id for item in matches)}"
        )
    return matches[0] if matches else None


def cleanup_native_generation_debris(
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
                r"\.gc-native-[0-9a-f]{24}-[0-9a-f]{32}",
                entry.name,
            )
        )
        if valid_name is None:
            raise ValueError(
                f"Native generation debris has unsafe name: {entry.name}"
            )
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"Native generation debris must not be a symlink: {entry.name}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                f"Native generation debris must be a directory: {entry.name}"
            )
        candidates.append(entry)
    for entry in candidates:
        _remove_tree(entry)
    if candidates:
        _fsync_directory(root)
    return tuple(entry.name for entry in candidates)


def prune_native_generations(
    output_root: str | Path,
    *,
    bound_generation_ids: Collection[str],
    protected_generation_ids: Collection[str] = (),
    retain_count: int = DEFAULT_GENERATION_RETENTION_COUNT,
    min_free_bytes: int = DEFAULT_GENERATION_MIN_FREE_BYTES,
) -> tuple[str, ...]:
    """拒绝旧式、由调用方自行声明绑定集合的危险清理入口。"""
    del (
        output_root,
        bound_generation_ids,
        protected_generation_ids,
        retain_count,
        min_free_bytes,
    )
    raise RuntimeError(
        "Native generation caller-supplied retention is disabled; "
        "use DB-resolved INVALIDATED reclaim candidates"
    )


def delete_reclaimable_native_generation(
    output_root: str | Path,
    *,
    generation_id: str,
    manifest_sha256: str,
    business_date: str,
    feature_date: str,
) -> bool:
    """精确删除数据库已证明可回收的一个 INVALIDATED generation。

    本函数不判断数据库引用；调用方必须只传入
    ``scheduler.repository.resolve_reclaimable_generation_payloads()``
    返回的行。删除前会重新校验目录身份、manifest 摘要和全部文件。
    """
    root = Path(output_root)
    _require_private_generation_root(root)
    if not _NATIVE_GENERATION_ID_PATTERN.fullmatch(str(generation_id)):
        raise ValueError("Native reclaim candidate has invalid generation id")
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
            "Native reclaim candidate must be a real directory"
        )
    context = open_native_generation(
        source / "manifest.json",
        expected_generation_id=generation_id,
        expected_manifest_sha256=expected_sha256,
        expected_business_date=expected_business_date,
        expected_feature_date=expected_feature_date,
    )
    _delete_native_generation(
        root,
        context,
        scanned_info=scanned_info,
    )
    return True


def preflight_native_generation_storage(
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
                    "Native generation storage contains unsafe directory"
                )
            if current == root and _NATIVE_GENERATION_ID_PATTERN.fullmatch(
                name
            ):
                generation_count += 1
        for name in filenames:
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    "Native generation storage contains unsafe file"
                )
            total_bytes += int(info.st_size)
    prospective_total_bytes = total_bytes + reserved_bytes
    prospective_generation_count = (
        generation_count + reserved_generations
    )
    if prospective_total_bytes > total_limit:
        raise OSError(
            "Native generation storage quota exceeded: "
            f"used={total_bytes}, reserve={reserved_bytes}, "
            f"limit={total_limit}"
        )
    if prospective_generation_count > generation_limit:
        raise OSError(
            "Native generation count limit exceeded: "
            f"count={generation_count}, "
            f"reserve={reserved_generations}, "
            f"limit={generation_limit}"
        )
    free_bytes = int(shutil.disk_usage(root).free)
    if free_bytes < required_free:
        raise OSError(
            "Native generation storage below disk low watermark: "
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


def _enforce_staged_native_generation_storage(
    root: Path,
    *,
    max_total_bytes: int | None,
    min_free_bytes: int | None,
) -> None:
    """按已落盘 staging 的实际字节执行发布前硬配额检查。"""
    if max_total_bytes is None and min_free_bytes is None:
        return
    preflight_native_generation_storage(
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


def open_native_generation(
    manifest_path: str | Path,
    *,
    expected_generation_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_business_date: str | None = None,
    expected_feature_date: str | None = None,
) -> NativeGenerationContext:
    """重新读取并校验 manifest 与全部 CSV 后返回运行 context。"""
    path = Path(manifest_path)
    root = path.parent
    _require_real_directory(root)
    _require_private_generation_root(root.parent)
    if path.name != "manifest.json":
        raise ValueError("Native generation manifest path must end in manifest.json")
    manifest_bytes = _read_regular_file_bytes(path, label="manifest")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and not hmac.compare_digest(
            manifest_sha256,
            _required_sha256(
                expected_manifest_sha256,
                "expected_manifest_sha256",
            ),
        )
    ):
        raise ValueError("Native generation manifest sha256 mismatch")
    try:
        manifest = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Native generation manifest is not canonical UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Native generation manifest must be an object")
    if manifest_bytes != _canonical_json_bytes(manifest) + b"\n":
        raise ValueError("Native generation manifest is not canonical JSON")
    actual_manifest_fields = set(manifest)
    if actual_manifest_fields != _MANIFEST_FIELDS:
        raise ValueError(
            "Native generation manifest fields mismatch: "
            f"expected={sorted(_MANIFEST_FIELDS)}, "
            f"actual={sorted(actual_manifest_fields)}"
        )
    if manifest.get("manifest_version") != NATIVE_GENERATION_MANIFEST_VERSION:
        raise ValueError("Native generation manifest_version mismatch")
    if manifest.get("schema_version") != NATIVE_GENERATION_SCHEMA_VERSION:
        raise ValueError("Native generation schema_version mismatch")
    if manifest.get("generation_type") != NATIVE_GENERATION_TYPE:
        raise ValueError("Native generation generation_type mismatch")

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
    if feature_date > business_date:
        raise ValueError("Native generation feature_date is after business_date")
    readiness_basis = _required_text(
        manifest.get("readiness_basis"),
        "readiness_basis",
    )
    if readiness_basis not in _READINESS_BASES:
        raise ValueError("Native generation readiness_basis is invalid")
    source_contract_cutoff = _canonical_utc_timestamp(
        manifest.get("source_contract_cutoff"),
        "source_contract_cutoff",
    )
    capture_not_after = _canonical_utc_timestamp(
        manifest.get("capture_not_after"),
        "capture_not_after",
    )
    snapshot_started_at = _canonical_utc_timestamp(
        manifest.get("snapshot_started_at"),
        "snapshot_started_at",
    )
    _validate_manifest_clock_contract(
        business_date=business_date,
        source_contract_cutoff=source_contract_cutoff,
        capture_not_after=capture_not_after,
        snapshot_started_at=snapshot_started_at,
    )
    source_evidence = _validate_source_evidence_payload(
        manifest.get("source_evidence"),
        expected_feature_date=feature_date,
    )
    source_evidence_sha256 = _required_sha256(
        manifest.get("source_evidence_sha256"),
        "source_evidence_sha256",
    )
    actual_source_evidence_sha256 = hashlib.sha256(
        _canonical_json_bytes(source_evidence)
    ).hexdigest()
    if not hmac.compare_digest(
        source_evidence_sha256,
        actual_source_evidence_sha256,
    ):
        raise ValueError("Native generation source evidence sha256 mismatch")
    source_commit_token = _required_sha256(
        manifest.get("source_commit_token"),
        "source_commit_token",
    )
    if not hmac.compare_digest(
        source_commit_token,
        source_evidence_sha256,
    ):
        raise ValueError(
            "Native generation source_commit_token does not match "
            "source evidence"
        )
    _assert_source_evidence_payload_at_cutoff(
        source_evidence,
        cutoff_at=datetime.fromisoformat(
            snapshot_started_at.replace("Z", "+00:00")
        ),
        source_commit_token=source_commit_token,
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
        raise ValueError("Native generation sealed_at is before created_at")
    cutoffs = _validate_cutoffs(
        manifest.get("cutoffs"),
        feature_date=feature_date,
    )
    if expected_generation_id is not None and generation_id != expected_generation_id:
        raise ValueError("Native generation id mismatch")
    if (
        expected_business_date is not None
        and business_date
        != _canonical_date(expected_business_date, "expected_business_date")
    ):
        raise ValueError("Native generation business_date mismatch")
    if (
        expected_feature_date is not None
        and feature_date
        != _canonical_date(expected_feature_date, "expected_feature_date")
    ):
        raise ValueError("Native generation feature_date mismatch")

    entries = manifest.get("files")
    if not isinstance(entries, dict):
        raise ValueError("Native generation manifest files must be an object")
    expected_file_set = set(NATIVE_GENERATION_FILENAMES)
    actual_file_set = set(entries)
    if actual_file_set != expected_file_set:
        raise ValueError(
            "Native generation manifest file allowlist mismatch: "
            f"missing={sorted(expected_file_set - actual_file_set)}, "
            f"extra={sorted(actual_file_set - expected_file_set)}"
        )
    expected_directory_entries = expected_file_set | {"manifest.json"}
    actual_directory_entries = {item.name for item in root.iterdir()}
    missing_entries = expected_directory_entries - actual_directory_entries
    if missing_entries:
        raise ValueError(
            f"Native generation missing entries: {sorted(missing_entries)}"
        )
    unexpected_entries = actual_directory_entries - expected_directory_entries
    if unexpected_entries:
        raise ValueError(
            "Native generation unexpected entries: "
            f"{sorted(unexpected_entries)}"
        )

    frames: dict[str, pd.DataFrame] = {}
    for filename in NATIVE_GENERATION_FILENAMES:
        entry = entries.get(filename)
        if not isinstance(entry, dict):
            raise ValueError(f"Native generation manifest missing {filename}")
        _validate_file_entry(filename, entry)
        raw = _read_regular_file_bytes(
            root / filename,
            label=filename,
        )
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual_sha256, entry["sha256"]):
            raise ValueError(f"Native generation file hash mismatch: {filename}")
        if len(raw) != entry["size_bytes"]:
            raise ValueError(
                f"Native generation file size mismatch: {filename}"
            )
        frame = _parse_csv_bytes(filename, raw)
        if list(frame.columns) != entry.get("columns"):
            raise ValueError(f"Native generation file schema mismatch: {filename}")
        if len(frame) != entry.get("row_count"):
            raise ValueError(f"Native generation file row count mismatch: {filename}")
        _validate_open_frame(
            filename,
            frame,
            feature_date=feature_date,
        )
        frames[filename] = frame

    identity = {
        "schema_version": manifest.get("schema_version"),
        "files": entries,
    }
    dataset_content_id = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    if manifest.get("dataset_content_id") != dataset_content_id:
        raise ValueError("Native generation dataset content ID mismatch")
    stable_provenance = _stable_generation_provenance(
        generation_type=NATIVE_GENERATION_TYPE,
        dataset_content_id=dataset_content_id,
        source_commit_token=_required_text(
            source_commit_token,
            "source_commit_token",
        ),
        business_date=business_date,
        feature_date=feature_date,
        readiness_basis=readiness_basis,
        schema_version=NATIVE_GENERATION_SCHEMA_VERSION,
        exporter_version=_required_text(
            manifest.get("exporter_version"),
            "exporter_version",
        ),
    )
    generation_digest = hashlib.sha256(
        _canonical_json_bytes(stable_provenance)
    ).hexdigest()
    if generation_id != f"native-{generation_digest[:24]}":
        raise ValueError(
            "Native generation id does not match stable provenance"
        )
    actual_cutoffs = _derive_cutoffs(
        frames,
        feature_date=feature_date,
    )
    if cutoffs != actual_cutoffs:
        raise ValueError(
            "Native generation cutoffs do not match frozen content"
        )

    return NativeGenerationContext(
        generation_id=generation_id,
        generation_type=NATIVE_GENERATION_TYPE,
        dataset_content_id=dataset_content_id,
        root_dir=path.parent,
        manifest_path=path,
        manifest_sha256=manifest_sha256,
        business_date=business_date,
        feature_date=feature_date,
        source_commit_token=_required_text(
            source_commit_token,
            "source_commit_token",
        ),
        readiness_basis=readiness_basis,
        schema_version=_required_text(
            manifest.get("schema_version"),
            "schema_version",
        ),
        exporter_version=_required_text(
            manifest.get("exporter_version"),
            "exporter_version",
        ),
        created_at=created_at,
        sealed_at=sealed_at,
        _cutoffs_json=_canonical_json_bytes(cutoffs).decode("utf-8"),
        _manifest_json=_canonical_json_bytes(manifest).decode("utf-8"),
        _frames=frames,
    )


def _export_frozen_frames(
    engine: Engine,
    *,
    feature_date: str,
    capture_not_after: datetime | None = None,
    source_contract_cutoff: datetime | None = None,
    snapshot_clock: Callable[[], datetime] | None = None,
) -> tuple[
    dict[str, pd.DataFrame],
    _data_contract.SourceCommitEvidence,
    datetime,
]:
    normalized_deadline = _optional_aware_utc(
        capture_not_after,
        "capture_not_after",
    )
    normalized_cutoff = _optional_aware_utc(
        source_contract_cutoff,
        "source_contract_cutoff",
    )
    with engine.connect() as connection:
        try:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            connection.exec_driver_sql(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            )
            snapshot_started_at = _aware_utc_now(snapshot_clock)
            if (
                normalized_deadline is not None
                and snapshot_started_at >= normalized_deadline
            ):
                raise RuntimeError(
                    "Native snapshot capture window expired: "
                    f"started_at={snapshot_started_at.isoformat()} "
                    f"not_after={normalized_deadline.isoformat()}"
                )
            evidence = (
                _data_contract.capture_source_commit_evidence_from_connection(
                    connection,
                    feature_date=feature_date,
                )
            )
            if evidence.feature_date != feature_date:
                raise RuntimeError(
                    "source commit evidence feature_date drifted: "
                    f"{evidence.feature_date} != {feature_date}"
                )
            if normalized_cutoff is not None:
                _data_contract.assert_source_commit_evidence_at_cutoff(
                    evidence,
                    cutoff_at=snapshot_started_at,
                )
            frames = _read_source_frames(
                connection,
                feature_date=feature_date,
            )
        finally:
            connection.rollback()
    return frames, evidence, snapshot_started_at


def _read_source_frames(
    connection: Connection,
    *,
    feature_date: str,
) -> dict[str, pd.DataFrame]:
    calendar_frames = (
        _calendar_service.read_calendar_snapshot_from_connection(
            connection
        )
    )
    metadata = _data_service.read_factor_metadata_from_db(connection)
    _validate_metadata(metadata)
    daily_codes = _selected_codes(metadata, "daily")
    weekly_codes = _selected_codes(metadata, "weekly")
    monthly_codes = _selected_codes(metadata, "monthly")

    frames = {
        "metadata.csv": _canonicalize_frame("metadata.csv", metadata),
        "api_wind_daily.csv": _cutoff_factor_frame(
            "api_wind_daily.csv",
            _data_service.read_daily_long_from_db(
                daily_codes,
                "api_wind_daily",
                connection,
            ),
            feature_date,
        ),
        "api_wind_derivative_daily.csv": _cutoff_factor_frame(
            "api_wind_derivative_daily.csv",
            _data_service.read_daily_long_from_db(
                daily_codes,
                "api_wind_derivative_daily",
                connection,
            ),
            feature_date,
        ),
        "api_wind_weekly.csv": _cutoff_factor_frame(
            "api_wind_weekly.csv",
            _data_service.read_weekly_long_from_db(
                weekly_codes,
                "api_wind_weekly",
                connection,
            ),
            feature_date,
        ),
        "api_wind_derivative_weekly.csv": _cutoff_factor_frame(
            "api_wind_derivative_weekly.csv",
            _data_service.read_weekly_long_from_db(
                weekly_codes,
                "api_wind_derivative_weekly",
                connection,
            ),
            feature_date,
        ),
        "api_wind_monthly.csv": _cutoff_factor_frame(
            "api_wind_monthly.csv",
            _data_service.read_monthly_long_from_db(
                monthly_codes,
                "api_wind_monthly",
                connection,
            ),
            feature_date,
        ),
        "api_wind_derivative_monthly.csv": _cutoff_factor_frame(
            "api_wind_derivative_monthly.csv",
            _data_service.read_monthly_long_from_db(
                monthly_codes,
                "api_wind_derivative_monthly",
                connection,
                include_month_id=True,
            ),
            feature_date,
        ),
        "api_wind_date.csv": _canonicalize_frame(
            "api_wind_date.csv",
            calendar_frames["api_wind_date.csv"],
        ),
        "t_trade_calendar.csv": _canonicalize_frame(
            "t_trade_calendar.csv",
            calendar_frames["t_trade_calendar.csv"],
        ),
    }
    weekly_selected = _data_service.select_factor_metadata(
        metadata,
        "weekly",
    )
    weekly_schema = [
        "week_id",
        *weekly_selected["indicators_code"].astype(str).str.strip().tolist(),
    ]
    frames["weekly_cutoff_index.csv"] = _canonicalize_frame(
        "weekly_cutoff_index.csv",
        _data_service.build_weekly_cutoff_index_from_frames(
            weekly_schema,
            frames["api_wind_weekly.csv"],
            frames["api_wind_derivative_weekly.csv"],
            end_date=feature_date,
        ),
    )
    frames["monthly_cutoff_index.csv"] = _canonicalize_frame(
        "monthly_cutoff_index.csv",
        _data_service.build_monthly_cutoff_index_from_frames(
            metadata,
            frames["api_wind_monthly.csv"],
            frames["api_wind_derivative_monthly.csv"],
            end_date=feature_date,
        ),
    )
    return {
        filename: frames[filename]
        for filename in NATIVE_GENERATION_FILENAMES
    }


def _selected_codes(metadata: pd.DataFrame, frequency: str) -> list[str]:
    selected = _data_service.select_factor_metadata(metadata, frequency)
    return selected["indicators_code"].astype(str).str.strip().tolist()


def _cutoff_factor_frame(
    filename: str,
    frame: pd.DataFrame,
    feature_date: str,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{filename} source must be a DataFrame")
    expected_columns = list(_EXACT_COLUMNS[filename])
    if list(frame.columns) != expected_columns:
        raise ValueError(
            f"{filename} columns must be exactly {expected_columns}, "
            f"got {list(frame.columns)}"
        )
    parsed = pd.to_datetime(
        frame["rdate"],
        errors="coerce",
        format="mixed",
    ).dt.date
    if parsed.isna().any():
        raise ValueError(f"{filename} contains invalid rdate")
    normalized = frame.loc[
        parsed.le(date.fromisoformat(feature_date))
    ].copy()
    return _canonicalize_frame(filename, normalized)


def _canonicalize_frame(filename: str, frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{filename} source must be a DataFrame")
    result = frame.copy(deep=True)
    result.columns = [str(column).strip() for column in result.columns]
    if len(result.columns) != len(set(result.columns)):
        raise ValueError(f"{filename} contains duplicate columns")
    if filename == "metadata.csv":
        _validate_metadata(result)
    else:
        expected = list(_EXACT_COLUMNS[filename])
        if list(result.columns) != expected:
            raise ValueError(
                f"{filename} columns must be exactly {expected}, "
                f"got {list(result.columns)}"
            )

    for column in ("rdate", "available_date"):
        if column in result.columns:
            parsed = pd.to_datetime(
                result[column],
                errors="coerce",
                format="mixed",
            ).dt.date
            if parsed.isna().any():
                raise ValueError(f"{filename} contains invalid {column}")
            result[column] = [value.isoformat() for value in parsed]
    for column in ("week_id", "month_id"):
        if column in result.columns:
            result[column] = (
                result[column]
                .astype("string")
                .str.strip()
                .str.replace(r"\.0$", "", regex=True)
            )
    for column in ("indicators_code", "trade_flag"):
        if column in result.columns:
            result[column] = result[column].astype("string").str.strip()

    if not result.empty:
        result = result.sort_values(
            list(result.columns),
            kind="mergesort",
            na_position="last",
        )
    return result.reset_index(drop=True)


def _validate_metadata(metadata: pd.DataFrame) -> None:
    required = {"indicators_code", "frequency"}
    missing = required - set(metadata.columns)
    if missing:
        raise ValueError(
            f"metadata.csv missing required columns: {sorted(missing)}"
        )


def _render_csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(
        index=False,
        lineterminator="\n",
        na_rep="",
    ).encode("utf-8")


def _parse_csv_bytes(filename: str, raw: bytes) -> pd.DataFrame:
    try:
        raw.decode("utf-8")
        frame = pd.read_csv(
            io.BytesIO(raw),
            dtype={
                "rdate": "string",
                "available_date": "string",
                "week_id": "string",
                "month_id": "string",
                "indicators_code": "string",
                "trade_flag": "string",
            },
        )
    except (
        UnicodeDecodeError,
        pd.errors.EmptyDataError,
        pd.errors.ParserError,
    ) as exc:
        raise ValueError(f"{filename} is not valid UTF-8 CSV") from exc
    return frame


def _validate_file_entry(
    filename: str,
    entry: Mapping[str, Any],
) -> None:
    actual_fields = set(entry)
    if actual_fields != _FILE_ENTRY_FIELDS:
        raise ValueError(
            f"Native generation file entry schema mismatch: {filename}"
        )
    declared_path = entry.get("path")
    if not isinstance(declared_path, str):
        raise ValueError(f"Native generation unsafe file path: {filename}")
    posix_path = PurePosixPath(declared_path)
    windows_path = PureWindowsPath(declared_path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
        or declared_path != filename
    ):
        raise ValueError(
            f"Native generation unsafe file path: {declared_path!r}"
        )
    _required_sha256(entry.get("sha256"), f"{filename}.sha256")
    size_bytes = entry.get("size_bytes")
    row_count = entry.get("row_count")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ValueError(f"Native generation invalid size_bytes: {filename}")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
    ):
        raise ValueError(f"Native generation invalid row_count: {filename}")
    columns = entry.get("columns")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(
            isinstance(column, str) and column.strip() == column and column
            for column in columns
        )
        or len(columns) != len(set(columns))
    ):
        raise ValueError(f"Native generation invalid columns: {filename}")


def _validate_open_frame(
    filename: str,
    frame: pd.DataFrame,
    *,
    feature_date: str,
) -> None:
    if filename == "metadata.csv":
        _validate_metadata(frame)
    else:
        expected_columns = list(_EXACT_COLUMNS[filename])
        if list(frame.columns) != expected_columns:
            raise ValueError(
                f"Native generation file schema mismatch: {filename}"
            )

    date_columns = [
        column
        for column in ("rdate", "available_date")
        if column in frame.columns
    ]
    parsed_by_column: dict[str, pd.Series] = {}
    for column in date_columns:
        parsed = pd.to_datetime(
            frame[column],
            errors="coerce",
            format="%Y-%m-%d",
        ).dt.date
        if parsed.isna().any():
            raise ValueError(
                f"Native generation {filename} contains invalid {column}"
            )
        canonical = [
            value.isoformat()
            for value in parsed
        ]
        if frame[column].astype(str).tolist() != canonical:
            raise ValueError(
                f"Native generation {filename} contains noncanonical {column}"
            )
        parsed_by_column[column] = parsed

    if filename in _FACTOR_FILENAMES:
        parsed = parsed_by_column["rdate"]
        if any(value > date.fromisoformat(feature_date) for value in parsed):
            raise ValueError(
                f"Native generation {filename} exceeds feature_date"
            )
    if filename in {
        "weekly_cutoff_index.csv",
        "monthly_cutoff_index.csv",
    }:
        parsed = parsed_by_column["available_date"]
        if any(value > date.fromisoformat(feature_date) for value in parsed):
            raise ValueError(
                f"Native generation {filename} exceeds feature_date"
            )

    period_column = {
        "api_wind_weekly.csv": "week_id",
        "api_wind_derivative_weekly.csv": "week_id",
        "api_wind_derivative_monthly.csv": "month_id",
        "api_wind_date.csv": "week_id",
        "weekly_cutoff_index.csv": "week_id",
        "monthly_cutoff_index.csv": "month_id",
    }.get(filename)
    if period_column is not None:
        values = frame[period_column].astype(str)
        if not values.str.fullmatch(r"\d{6}", na=False).all():
            raise ValueError(
                f"Native generation {filename} contains invalid "
                f"{period_column}"
            )


def _derive_cutoffs(
    frames: Mapping[str, pd.DataFrame],
    *,
    feature_date: str,
) -> dict[str, str]:
    feature = date.fromisoformat(feature_date)
    daily_sources = pd.concat(
        [
            frames["api_wind_daily.csv"],
            frames["api_wind_derivative_daily.csv"],
        ],
        ignore_index=True,
    )
    daily_targets = daily_sources[
        daily_sources["indicators_code"]
        .astype(str)
        .isin(_data_contract.NATIVE_READINESS_DAILY_ANCHORS)
    ]
    if daily_targets.empty:
        raise ValueError(
            "Native generation cannot derive daily cutoff from target factors"
        )
    daily_dates = pd.to_datetime(
        daily_targets["rdate"],
        errors="coerce",
        format="%Y-%m-%d",
    ).dt.date
    if daily_dates.isna().any():
        raise ValueError("Native generation daily cutoff source is invalid")
    daily_cutoff = max(daily_dates)
    if daily_cutoff != feature:
        raise ValueError(
            "Native generation daily cutoff must equal feature_date"
        )
    anchor_values = pd.to_numeric(
        daily_targets["indicators_value"],
        errors="coerce",
    )
    present_anchors = set(
        daily_targets.loc[
            (daily_dates == feature) & anchor_values.notna(),
            "indicators_code",
        ].astype(str)
    )
    missing_anchors = sorted(
        set(_data_contract.NATIVE_READINESS_DAILY_ANCHORS)
        - present_anchors
    )
    if missing_anchors:
        raise ValueError(
            "Native generation missing required daily anchors at "
            f"feature_date: {', '.join(missing_anchors)}"
        )

    weekly_cutoff = _latest_period_cutoff(
        frames["weekly_cutoff_index.csv"],
        period_column="week_id",
        feature_date=feature,
    )
    monthly_cutoff = _latest_period_cutoff(
        frames["monthly_cutoff_index.csv"],
        period_column="month_id",
        feature_date=feature,
    )
    return {
        "daily": daily_cutoff.isoformat(),
        "weekly": weekly_cutoff,
        "monthly": monthly_cutoff,
    }


def _latest_period_cutoff(
    frame: pd.DataFrame,
    *,
    period_column: str,
    feature_date: date,
) -> str:
    if frame.empty:
        raise ValueError(
            f"Native generation cannot derive {period_column} cutoff"
        )
    available_dates = pd.to_datetime(
        frame["available_date"],
        errors="coerce",
        format="%Y-%m-%d",
    ).dt.date
    periods = frame[period_column].astype(str)
    if available_dates.isna().any() or not periods.str.fullmatch(
        r"\d{6}",
        na=False,
    ).all():
        raise ValueError(
            f"Native generation {period_column} cutoff index is invalid"
        )
    eligible = [
        (available_date, period)
        for available_date, period in zip(available_dates, periods)
        if available_date <= feature_date
    ]
    if not eligible:
        raise ValueError(
            f"Native generation has no {period_column} cutoff by feature_date"
        )
    return max(eligible)[1]


def _validate_cutoffs(
    value: object,
    *,
    feature_date: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "daily",
        "weekly",
        "monthly",
    }:
        raise ValueError(
            "Native generation cutoffs must contain exactly "
            "daily/weekly/monthly"
        )
    daily = _canonical_date(value.get("daily"), "cutoffs.daily")
    if daily != feature_date:
        raise ValueError(
            "Native generation cutoffs.daily must equal feature_date"
        )
    result = {"daily": daily}
    for frequency in ("weekly", "monthly"):
        period = value.get(frequency)
        if not isinstance(period, str) or not re.fullmatch(r"\d{6}", period):
            raise ValueError(
                f"Native generation cutoffs.{frequency} must be six digits"
            )
        result[frequency] = period
    return result


def _stable_generation_provenance(
    *,
    generation_type: str,
    dataset_content_id: str,
    source_commit_token: str,
    business_date: str,
    feature_date: str,
    readiness_basis: str,
    schema_version: str,
    exporter_version: str,
) -> dict[str, str]:
    return {
        "generation_type": generation_type,
        "dataset_content_id": dataset_content_id,
        "source_commit_token": source_commit_token,
        "business_date": business_date,
        "feature_date": feature_date,
        "readiness_basis": readiness_basis,
        "schema_version": schema_version,
        "exporter_version": exporter_version,
    }


def _require_same_stable_provenance(
    existing: NativeGenerationContext,
    candidate: NativeGenerationContext,
) -> None:
    fields = (
        "generation_id",
        "generation_type",
        "dataset_content_id",
        "source_commit_token",
        "business_date",
        "feature_date",
        "readiness_basis",
        "schema_version",
        "exporter_version",
        "cutoffs",
    )
    mismatches = {
        field_name: (
            getattr(existing, field_name),
            getattr(candidate, field_name),
        )
        for field_name in fields
        if getattr(existing, field_name) != getattr(candidate, field_name)
    }
    if mismatches:
        raise ValueError(
            "existing Native generation stable provenance mismatch: "
            f"{mismatches}"
        )


def _utc_now_text() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_aware_utc(
    value: datetime | None,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_clock_contract_parameters(
    *,
    business_date: str,
    source_contract_cutoff: datetime | None,
    capture_not_after: datetime | None,
) -> tuple[datetime, datetime]:
    if source_contract_cutoff is None or capture_not_after is None:
        raise ValueError(
            "CLOCK_CONTRACT requires source_contract_cutoff and "
            "capture_not_after"
        )
    cutoff = _optional_aware_utc(
        source_contract_cutoff,
        "source_contract_cutoff",
    )
    deadline = _optional_aware_utc(
        capture_not_after,
        "capture_not_after",
    )
    assert cutoff is not None
    assert deadline is not None
    local_cutoff = cutoff.astimezone(_SHANGHAI)
    local_deadline = deadline.astimezone(_SHANGHAI)
    if (
        local_cutoff.date().isoformat() != business_date
        or local_cutoff.time().replace(tzinfo=None)
        != _CLOCK_CONTRACT_CUTOFF
    ):
        raise ValueError(
            "CLOCK_CONTRACT source_contract_cutoff must be business-date "
            "06:30:00 Asia/Shanghai"
        )
    if local_deadline.date().isoformat() != business_date:
        raise ValueError(
            "CLOCK_CONTRACT capture_not_after must be on business-date "
            "Asia/Shanghai"
        )
    if cutoff >= deadline:
        raise ValueError(
            "CLOCK_CONTRACT cutoff must precede capture deadline"
        )
    return cutoff, deadline


def _validate_manifest_clock_contract(
    *,
    business_date: str,
    source_contract_cutoff: str,
    capture_not_after: str,
    snapshot_started_at: str,
) -> None:
    cutoff = datetime.fromisoformat(
        source_contract_cutoff.replace("Z", "+00:00")
    )
    deadline = datetime.fromisoformat(
        capture_not_after.replace("Z", "+00:00")
    )
    started = datetime.fromisoformat(
        snapshot_started_at.replace("Z", "+00:00")
    )
    expected_cutoff, expected_deadline = (
        _validate_clock_contract_parameters(
            business_date=business_date,
            source_contract_cutoff=cutoff,
            capture_not_after=deadline,
        )
    )
    if started < expected_cutoff or started >= expected_deadline:
        raise ValueError(
            "Native generation snapshot_started_at is outside the "
            "CLOCK_CONTRACT capture window"
        )


def _source_evidence_payload(
    evidence: _data_contract.SourceCommitEvidence,
    *,
    expected_feature_date: str,
) -> dict[str, Any]:
    if not isinstance(evidence, _data_contract.SourceCommitEvidence):
        raise TypeError("source evidence must be SourceCommitEvidence")
    payload = {
        "evidence_version": _SOURCE_EVIDENCE_VERSION,
        "feature_date": evidence.feature_date,
        "tables": [
            {
                "table_name": item.table_name,
                "row_count": item.row_count,
                "latest_create_time": item.latest_create_time,
                "latest_update_time": item.latest_update_time,
                "latest_business_key": item.latest_business_key,
            }
            for item in evidence.tables
        ],
    }
    return _validate_source_evidence_payload(
        payload,
        expected_feature_date=expected_feature_date,
    )


def _validate_source_evidence_payload(
    value: object,
    *,
    expected_feature_date: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"evidence_version", "feature_date", "tables"}
    ):
        raise ValueError("Native generation source evidence shape mismatch")
    if value.get("evidence_version") != _SOURCE_EVIDENCE_VERSION:
        raise ValueError("Native generation source evidence version mismatch")
    feature_date = _canonical_date(
        value.get("feature_date"),
        "source_evidence.feature_date",
    )
    if feature_date != expected_feature_date:
        raise ValueError(
            "Native generation source evidence feature_date mismatch"
        )
    raw_tables = value.get("tables")
    if not isinstance(raw_tables, list):
        raise ValueError("Native generation source evidence tables must be a list")
    expected_names = {
        *_data_contract.FACTOR_SOURCE_TABLES,
        _data_contract.METADATA_SOURCE_TABLE,
        *_data_contract.CALENDAR_SOURCE_TABLES,
    }
    normalized_tables: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    expected_fields = {
        "table_name",
        "row_count",
        "latest_create_time",
        "latest_update_time",
        "latest_business_key",
    }
    for raw in raw_tables:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ValueError(
                "Native generation source evidence table shape mismatch"
            )
        table_name = raw.get("table_name")
        if not isinstance(table_name, str) or table_name not in expected_names:
            raise ValueError(
                "Native generation source evidence table_name mismatch"
            )
        if table_name in seen_names:
            raise ValueError(
                "Native generation source evidence contains duplicate table"
            )
        seen_names.add(table_name)
        row_count = raw.get("row_count")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise ValueError(
                "Native generation source evidence row_count is invalid"
            )
        latest_create_time = _optional_source_evidence_timestamp(
            raw.get("latest_create_time"),
            "latest_create_time",
        )
        latest_update_time = _optional_source_evidence_timestamp(
            raw.get("latest_update_time"),
            "latest_update_time",
        )
        latest_business_key = raw.get("latest_business_key")
        if latest_business_key is not None and (
            not isinstance(latest_business_key, str)
            or not latest_business_key.strip()
        ):
            raise ValueError(
                "Native generation source evidence latest_business_key "
                "is invalid"
            )
        normalized_tables.append(
            {
                "table_name": table_name,
                "row_count": row_count,
                "latest_create_time": latest_create_time,
                "latest_update_time": latest_update_time,
                "latest_business_key": latest_business_key,
            }
        )
    if seen_names != expected_names:
        raise ValueError(
            "Native generation source evidence table set mismatch"
        )
    normalized_tables.sort(key=lambda item: str(item["table_name"]))
    return {
        "evidence_version": _SOURCE_EVIDENCE_VERSION,
        "feature_date": feature_date,
        "tables": normalized_tables,
    }


def _assert_source_evidence_payload_at_cutoff(
    payload: Mapping[str, Any],
    *,
    cutoff_at: datetime,
    source_commit_token: str,
) -> None:
    evidence = _data_contract.SourceCommitEvidence(
        feature_date=str(payload["feature_date"]),
        source_commit_token=source_commit_token,
        tables=tuple(
            _data_contract.SourceTableEvidence(
                table_name=str(item["table_name"]),
                row_count=int(item["row_count"]),
                latest_create_time=item["latest_create_time"],
                latest_update_time=item["latest_update_time"],
                latest_business_key=item["latest_business_key"],
            )
            for item in payload["tables"]
        ),
    )
    _data_contract.assert_source_commit_evidence_at_cutoff(
        evidence,
        cutoff_at=cutoff_at,
    )


def _optional_source_evidence_timestamp(
    value: object,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T"
        r"\d{2}:\d{2}:\d{2}\.\d{6}",
        value,
    ):
        raise ValueError(
            "Native generation source evidence "
            f"{field_name} must be a naive microsecond timestamp"
        )
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Native generation source evidence {field_name} is invalid"
        ) from exc
    return value


def _aware_utc_now(
    clock: Callable[[], datetime] | None,
) -> datetime:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("snapshot clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_utc_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T"
        r"\d{2}:\d{2}:\d{2}\.\d{6}Z",
        value,
    ):
        raise ValueError(
            f"Native generation {field_name} must be canonical UTC"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Native generation {field_name} must be canonical UTC"
        ) from exc
    canonical = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if value != canonical:
        raise ValueError(
            f"Native generation {field_name} must be canonical UTC"
        )
    return canonical


def _require_real_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Native generation directory is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Native generation directory must not be a symlink: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Native generation root is not a directory: {path}")


def _require_private_generation_root(path: Path) -> None:
    _require_real_directory(path)
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise ValueError("Native generation root has another owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            "Native generation root must be private (mode 0700 or stricter)"
        )


def _ensure_private_generation_root(path: Path) -> None:
    """只创建新私有根；既有非私有根必须由部署迁移显式修复。"""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_generation_root(path)


def _read_regular_file_bytes(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Native generation missing file: {label}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"Native generation file must not be a symlink: {label}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Native generation entry is not a regular file: {label}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Native generation cannot safely open file: {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ValueError(
                f"Native generation file changed while opening: {label}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)

    try:
        after = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            f"Native generation file changed while reading: {label}"
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError(f"Native generation file changed while reading: {label}")
    return b"".join(chunks)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"Native generation manifest contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
                f"Native generation fsync target is not regular: {path.name}"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_generation_read_only(root: Path) -> None:
    for path in root.iterdir():
        path.chmod(0o444)
        _fsync_regular_file(path)


_SEALED_RENAME_SUPPORT: dict[str, bool] = {}


def _sealed_rename_supported(parent: Path) -> bool:
    """探测该父目录所在权限体制是否允许重命名 ``0o555`` 目录。

    重命名目录需要更新其 ``..`` 项，POSIX 因此要求对被移动目录本身有写权限。
    macOS 上该要求可被继承 ACL 覆盖：父链带 ``delete``/``add_subdirectory``
    等 allow 条目时 ``0o555`` 目录照常可 rename，没有这类 ACL 的部署则必然
    ``EACCES``。两种体制都属正常部署，因此按父目录探测一次并缓存，而不是
    对真实 generation 试错——后者会产生第二次发布 rename，破坏"原子发布只
    重命名一次"的不变量。探针使用 ``.probe-`` 前缀，与发布命名不冲突。
    """
    key = str(parent)
    cached = _SEALED_RENAME_SUPPORT.get(key)
    if cached is not None:
        return cached
    probe_source = Path(tempfile.mkdtemp(prefix=".probe-", dir=parent))
    probe_target = parent / f"{probe_source.name}-moved"
    try:
        os.chmod(probe_source, 0o555)
        try:
            os.replace(probe_source, probe_target)
            supported = True
        except PermissionError:
            supported = False
    finally:
        for path in (probe_target, probe_source):
            if os.path.lexists(path):
                os.chmod(path, 0o700)
                os.rmdir(path)
    _SEALED_RENAME_SUPPORT[key] = supported
    return supported


def _publish_sealed_generation(staging: Path, destination: Path) -> None:
    """原子发布 generation，并保证发布后目录为 ``0o555``。

    平台允许重命名只读目录时走既有语义：``staging`` 在 ``os.replace``
    **之前**就封存，发布出去的一刻即是只读的，不存在可写窗口。

    平台不允许时（见 :func:`_sealed_rename_supported`）才改为发布后封存，
    且经**发布前已持有的 fd** 操作：fd 绑定 inode 而非路径，rename 后仍指向
    同一目录，重新封存不涉及路径重解析、也不会被 symlink 调包，这正是不用
    ``Path.chmod`` 的原因。该路径存在一个极短的"已发布但尚未封存"窗口，其间
    generation 只在平台私有、不可被 group/other 写的父目录下可见；封存后会
    经 ``fstat`` 复核，未达 ``0o555`` 即报错。

    两条路径都只对真实 generation 执行一次 ``os.replace``。
    """
    if _sealed_rename_supported(destination.parent):
        os.chmod(staging, 0o555, follow_symlinks=False)
        os.replace(staging, destination)
        return

    staging_fd = os.open(
        staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        os.replace(staging, destination)
        os.fchmod(staging_fd, 0o555)
        os.fsync(staging_fd)
        sealed_mode = stat.S_IMODE(os.fstat(staging_fd).st_mode)
        if sealed_mode != 0o555:
            raise RuntimeError(
                "generation was published but could not be sealed: "
                f"{destination} mode={sealed_mode:o}"
            )
    finally:
        os.close(staging_fd)


def _remove_tree(root: Path) -> None:
    if not os.path.lexists(root):
        return
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(
            f"Native generation cleanup root must be a real directory: {root}"
        )
    os.chmod(root, 0o755, follow_symlinks=False)
    for path in root.iterdir():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(
                "Native generation cleanup refuses non-regular entry: "
                f"{path.name}"
            )
        os.chmod(path, 0o644, follow_symlinks=False)
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
            or not _NATIVE_GENERATION_ID_PATTERN.fullmatch(value)
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


def _delete_native_generation(
    root: Path,
    context: NativeGenerationContext,
    *,
    scanned_info: os.stat_result,
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
            "Native generation retention candidate changed during collection: "
            f"{context.generation_id}"
        )
    open_native_generation(
        source / "manifest.json",
        expected_generation_id=context.generation_id,
        expected_manifest_sha256=context.manifest_sha256,
        expected_business_date=context.business_date,
        expected_feature_date=context.feature_date,
    )
    tombstone = root / (
        f".gc-{context.generation_id}-{uuid.uuid4().hex}"
    )
    if os.path.lexists(tombstone):
        raise ValueError("Native generation retention tombstone collision")
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
            "Native generation retention tombstone identity mismatch"
        )
    _remove_tree(tombstone)
    _fsync_directory(root)


def _canonical_date(value: object, field_name: str) -> str:
    if isinstance(value, datetime):
        normalized = value.date().isoformat()
    elif isinstance(value, date):
        normalized = value.isoformat()
    else:
        normalized = str(value)
    try:
        parsed = date.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc
    if normalized != parsed.isoformat():
        raise ValueError(f"{field_name} must use canonical YYYY-MM-DD")
    return normalized


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _required_sha256(value: object, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be lowercase sha256")
    return normalized
