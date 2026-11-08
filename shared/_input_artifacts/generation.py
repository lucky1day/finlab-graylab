"""DataBridge producer 的 generation 快照发布与 ready 读取。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Mapping,
)
import pandas as pd

from shared import data_service as _data_service
from shared.blackbox_v2.snapshot import (
    SNAPSHOT_FILENAMES,
    BlackboxSnapshot,
    create_snapshot_from_frames,
    normalize_period_key,
)
from shared.data_bridge.authority import normalize_blackbox_gray_replay_source_identity
from shared.data_bridge.validation import ValidatedDataBridgeDataset
from shared.data_bridge.validation import LEGACY_FOUR_FILENAMES
from shared._input_artifacts.contract import (
    BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
    BLACKBOX_GENERATION_SNAPSHOT_ROOT,
    BLACKBOX_SCHEMA_CONTRACT_SHA256,
    BLACKBOX_SCHEMA_PATH,
    BLACKBOX_SCHEMA_VERSION,
    LEGACY_BLACKBOX_GENERATION_SNAPSHOT_CACHE_VERSION,
    LEGACY_BLACKBOX_SCHEMA_CONTRACT_SHA256,
    _load_blackbox_schema,
)
from shared._input_artifacts.files import (
    _fsync_directory,
    _is_sha256,
    _make_tree_writable,
    _read_stable_regular_file,
    _snapshot_file_fingerprints,
    _validate_blackbox_snapshot,
)


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


@contextmanager
def _generation_snapshot_cache_lock(root: Path, *, shared: bool = False):
    lock_path = root / "generation-cache.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    locked = False
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
        locked = True
        yield
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
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
