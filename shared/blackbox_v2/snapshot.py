from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from shared.blackbox_v2.platform_input_registry import PLATFORM_INPUT_REGISTRY
from shared.blackbox_v2.platform_inputs import FrozenPlatformInput
from shared.data_bridge.validation import (
    EXPECTED_FILENAMES as SNAPSHOT_FILENAMES,
    TIME_KEY_BY_FILE,
    validate_baseline_compatible_columns,
)
COMBINED_INPUT_IDENTITY_SCHEMA_VERSION = "blackbox-combined-input-v1"
_COMBINED_IDENTITY_FIELDS = frozenset(
    {
        "identity_schema_version",
        "parent_snapshot_id",
        "platform_inputs",
    }
)
_PLATFORM_INPUT_IDENTITY_FIELDS = frozenset(
    {
        "artifact_id",
        "provider_version",
        "filename",
        "sha256",
        "size_bytes",
        "row_count",
        "columns",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BlackboxSnapshot:
    snapshot_id: str
    root_dir: Path
    data_dir: Path
    manifest_path: Path
    schema_version: str
    generation_id: str | None = None
    refresh_date: str | None = None


@dataclass(frozen=True)
class BlackboxInputBundle:
    """三频父快照与显式平台制品组成的一次执行输入身份。"""

    combined_snapshot_id: str
    parent_snapshot_id: str
    base_snapshot: BlackboxSnapshot
    platform_input_ids: tuple[str, ...]
    platform_input_artifacts: tuple[FrozenPlatformInput, ...]
    expected_filenames: tuple[str, ...]
    _identity_manifest_json: str
    _audit_manifest_json: str

    @property
    def identity_manifest(self) -> dict[str, Any]:
        """返回不会改变 bundle 内部身份的深防御性副本。"""
        return _load_bundle_manifest(
            self._identity_manifest_json,
            "identity_manifest",
        )

    @property
    def audit_manifest(self) -> dict[str, Any]:
        """返回不会改变 bundle 内部审计证据的深防御性副本。"""
        return _load_bundle_manifest(
            self._audit_manifest_json,
            "audit_manifest",
        )


@dataclass(frozen=True)
class CutoffKeys:
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str


def compose_blackbox_input_bundle(
    base_snapshot: BlackboxSnapshot,
    *,
    platform_input_ids: Iterable[str] | None = None,
    platform_input_artifacts: Iterable[FrozenPlatformInput] = (),
) -> BlackboxInputBundle:
    """校验并组合父快照与平台制品，不改写父快照。"""
    if not isinstance(base_snapshot, BlackboxSnapshot):
        raise ValueError("base_snapshot must be a BlackboxSnapshot")

    artifacts = tuple(platform_input_artifacts)
    if any(
        not isinstance(artifact, FrozenPlatformInput)
        for artifact in artifacts
    ):
        raise ValueError(
            "platform_input_artifacts must contain FrozenPlatformInput values"
        )
    artifact_ids = tuple(artifact.artifact_id for artifact in artifacts)
    if artifact_ids:
        normalized_artifact_ids = PLATFORM_INPUT_REGISTRY.normalize_ids(
            artifact_ids
        )
    else:
        normalized_artifact_ids = ()

    if platform_input_ids is None:
        normalized_requested_ids = normalized_artifact_ids
    else:
        requested_ids = tuple(platform_input_ids)
        if requested_ids:
            normalized_requested_ids = PLATFORM_INPUT_REGISTRY.normalize_ids(
                requested_ids
            )
        else:
            normalized_requested_ids = ()
    if normalized_requested_ids != normalized_artifact_ids:
        raise ValueError(
            "platform_input_ids must match platform_input_artifacts"
        )

    artifacts_by_id = {
        artifact.artifact_id: artifact
        for artifact in artifacts
    }
    if len(artifacts_by_id) != len(artifacts):
        raise ValueError("platform_input_artifacts contains duplicate IDs")
    sorted_artifacts = tuple(
        artifacts_by_id[artifact_id]
        for artifact_id in normalized_requested_ids
    )

    expected_filenames = SNAPSHOT_FILENAMES + tuple(
        artifact.filename for artifact in sorted_artifacts
    )
    if len(expected_filenames) != len(set(expected_filenames)):
        raise ValueError(
            "platform input filename conflicts with another runtime input"
        )

    identity = {
        "identity_schema_version": COMBINED_INPUT_IDENTITY_SCHEMA_VERSION,
        "parent_snapshot_id": base_snapshot.snapshot_id,
        "platform_inputs": [
            artifact.identity_manifest
            for artifact in sorted_artifacts
        ],
    }
    combined_snapshot_id = compute_blackbox_combined_snapshot_id(
        identity
    )

    audit = {
        "identity": identity,
        "base_snapshot": {
            "snapshot_id": base_snapshot.snapshot_id,
            "schema_version": base_snapshot.schema_version,
            "generation_id": base_snapshot.generation_id,
            "refresh_date": base_snapshot.refresh_date,
        },
        "platform_inputs": [
            artifact.audit_manifest
            for artifact in sorted_artifacts
        ],
    }
    return BlackboxInputBundle(
        combined_snapshot_id=combined_snapshot_id,
        parent_snapshot_id=base_snapshot.snapshot_id,
        base_snapshot=base_snapshot,
        platform_input_ids=normalized_requested_ids,
        platform_input_artifacts=sorted_artifacts,
        expected_filenames=expected_filenames,
        _identity_manifest_json=_canonical_json(identity),
        _audit_manifest_json=_canonical_json(audit),
    )


def compute_blackbox_combined_snapshot_id(
    identity_manifest: Mapping[str, Any],
) -> str:
    """严格校验组合身份 manifest，并以统一 canonical 规则计算 ID。"""
    if not isinstance(identity_manifest, Mapping):
        raise ValueError("combined input identity manifest must be an object")
    identity = dict(identity_manifest)
    if set(identity) != _COMBINED_IDENTITY_FIELDS:
        raise ValueError(
            "combined input identity manifest fields are invalid"
        )
    if (
        identity["identity_schema_version"]
        != COMBINED_INPUT_IDENTITY_SCHEMA_VERSION
    ):
        raise ValueError(
            "combined input identity schema version is invalid"
        )
    parent_snapshot_id = identity["parent_snapshot_id"]
    if (
        not isinstance(parent_snapshot_id, str)
        or not parent_snapshot_id
    ):
        raise ValueError(
            "combined input identity parent_snapshot_id is invalid"
        )
    platform_inputs = identity["platform_inputs"]
    if not isinstance(platform_inputs, list):
        raise ValueError(
            "combined input identity platform_inputs must be a list"
        )

    artifact_ids: list[str] = []
    for raw_artifact in platform_inputs:
        if not isinstance(raw_artifact, Mapping):
            raise ValueError(
                "combined input identity platform input must be an object"
            )
        artifact = dict(raw_artifact)
        if set(artifact) != _PLATFORM_INPUT_IDENTITY_FIELDS:
            raise ValueError(
                "combined input identity platform input fields are invalid"
            )
        artifact_id = artifact["artifact_id"]
        if not isinstance(artifact_id, str):
            raise ValueError(
                "combined input identity artifact_id is invalid"
            )
        spec = PLATFORM_INPUT_REGISTRY.get(artifact_id)
        if artifact["provider_version"] != spec.provider_version:
            raise ValueError(
                "combined input identity provider_version mismatch"
            )
        if artifact["filename"] != spec.filename:
            raise ValueError(
                "combined input identity filename mismatch"
            )
        if artifact["columns"] != list(spec.columns):
            raise ValueError(
                "combined input identity columns mismatch"
            )
        if (
            not isinstance(artifact["sha256"], str)
            or not _SHA256_PATTERN.fullmatch(artifact["sha256"])
        ):
            raise ValueError(
                "combined input identity sha256 is invalid"
            )
        for field in ("size_bytes", "row_count"):
            value = artifact[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(
                    f"combined input identity {field} is invalid"
                )
        artifact_ids.append(artifact_id)

    if artifact_ids:
        normalized_ids = PLATFORM_INPUT_REGISTRY.normalize_ids(
            tuple(artifact_ids)
        )
        if tuple(artifact_ids) != normalized_ids:
            raise ValueError(
                "combined input identity platform inputs must be sorted"
            )
        identity_bytes = _canonical_json(identity).encode("utf-8")
        return (
            f"snapshot-{hashlib.sha256(identity_bytes).hexdigest()[:24]}"
        )
    return parent_snapshot_id


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_bundle_manifest(raw: str, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"bundle {field} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"bundle {field} must be an object")
    return value


def create_snapshot_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    output_root: Path,
    expected_columns: Mapping[str, list[str]],
    schema_version: str,
) -> BlackboxSnapshot:
    """将数据桥三频结果固化为内容寻址、只读的数据快照。"""
    _validate_frame_set(frames, expected_columns)
    rendered = {
        filename: frames[filename].to_csv(index=False, lineterminator="\n").encode("utf-8")
        for filename in SNAPSHOT_FILENAMES
    }
    file_entries = {
        filename: {
            "sha256": hashlib.sha256(rendered[filename]).hexdigest(),
            "row_count": int(len(frames[filename])),
            "columns": list(frames[filename].columns),
        }
        for filename in SNAPSHOT_FILENAMES
    }
    identity = {
        "schema_version": schema_version,
        "files": file_entries,
    }
    identity_bytes = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_id = f"snapshot-{hashlib.sha256(identity_bytes).hexdigest()[:24]}"

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / snapshot_id
    if not destination.exists():
        _write_snapshot(destination, rendered, identity, snapshot_id, output_root)

    manifest_path = destination / "manifest.json"
    _verify_existing_snapshot(manifest_path, identity, snapshot_id)
    _make_snapshot_read_only(destination)
    return BlackboxSnapshot(
        snapshot_id=snapshot_id,
        root_dir=destination,
        data_dir=destination / "data",
        manifest_path=manifest_path,
        schema_version=schema_version,
    )


def resolve_cutoffs(
    snapshot: BlackboxSnapshot,
    feature_date: str,
    *,
    weekly_as_of: pd.DataFrame,
    monthly_as_of: pd.DataFrame,
) -> CutoffKeys:
    """解析三频截止键；周/月键必须由平台权威 as-of 结果提供。"""
    feature = _parse_iso_date(feature_date, "feature_date")
    daily = pd.read_csv(snapshot.data_dir / "daily_output.csv", dtype={"date": "string"})
    if "date" not in daily.columns:
        raise ValueError("daily_output.csv is missing date cutoff column")
    daily_dates = pd.to_datetime(daily["date"], errors="coerce").dt.date
    eligible_daily = daily_dates[daily_dates <= feature].dropna()
    if eligible_daily.empty:
        raise ValueError(f"daily snapshot has no row on or before {feature_date}")
    daily_key = max(eligible_daily).isoformat()

    weekly_key = _authoritative_cutoff(
        weekly_as_of,
        "week_id",
        snapshot.data_dir / "weekly_output.csv",
    )
    monthly_key = _authoritative_cutoff(
        monthly_as_of,
        "month_id",
        snapshot.data_dir / "monthly_output.csv",
    )
    return CutoffKeys(
        daily_cutoff_key=daily_key,
        weekly_cutoff_key=weekly_key,
        monthly_cutoff_key=monthly_key,
    )


def _validate_frame_set(
    frames: Mapping[str, pd.DataFrame],
    expected_columns: Mapping[str, list[str]],
) -> None:
    expected_files = set(SNAPSHOT_FILENAMES)
    if set(frames) != expected_files:
        raise ValueError(f"snapshot files must be exactly {sorted(expected_files)}")
    if set(expected_columns) != expected_files:
        raise ValueError(f"schema files must be exactly {sorted(expected_files)}")
    for filename in SNAPSHOT_FILENAMES:
        frame = frames[filename]
        if frame.empty:
            raise ValueError(f"{filename} must not be empty")
        actual = list(frame.columns)
        validate_baseline_compatible_columns(
            filename,
            actual,
            list(expected_columns[filename]),
        )
        _validate_frame_content(filename, frame)


def _validate_frame_content(filename: str, frame: pd.DataFrame) -> None:
    key_column = TIME_KEY_BY_FILE[filename]
    if key_column not in frame.columns:
        raise ValueError(f"{filename} is missing {key_column}")
    if key_column == "date":
        keys = [_normalize_daily_key(value) for value in frame[key_column].tolist()]
    else:
        keys = [normalize_period_key(value, key_column) for value in frame[key_column].tolist()]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{filename} {key_column} must be unique")
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise ValueError(f"{filename} {key_column} must be strictly ascending")

    for column in frame.columns:
        if column == key_column:
            continue
        invalid = []
        for row_number, value in enumerate(frame[column].tolist(), start=2):
            if pd.isna(value) or (isinstance(value, str) and not value.strip()):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                invalid.append(row_number)
                continue
            if not math.isfinite(numeric):
                invalid.append(row_number)
        if invalid:
            raise ValueError(
                f"{filename} {column} must contain only numeric or null values; "
                f"invalid rows={invalid[:10]}"
            )


def _normalize_daily_key(value: object) -> str:
    if pd.isna(value):
        raise ValueError("date must use YYYY-MM-DD and must not be empty")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        parsed = pd.to_datetime(text, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("date must use YYYY-MM-DD or DataBridge timestamp format") from exc
    return parsed.date().isoformat()


def _write_snapshot(
    destination: Path,
    rendered: Mapping[str, bytes],
    identity: dict[str, object],
    snapshot_id: str,
    output_root: Path,
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=output_root))
    try:
        data_dir = staging / "data"
        data_dir.mkdir()
        for filename in SNAPSHOT_FILENAMES:
            path = data_dir / filename
            path.write_bytes(rendered[filename])
            path.chmod(0o444)
        manifest = {
            "data_snapshot_id": snapshot_id,
            **identity,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o444)
        try:
            os.replace(staging, destination)
        except OSError:
            if not destination.exists():
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_existing_snapshot(
    manifest_path: Path,
    identity: dict[str, object],
    snapshot_id: str,
) -> None:
    if not manifest_path.is_file():
        raise ValueError(f"snapshot {snapshot_id} has no manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("data_snapshot_id") != snapshot_id:
        raise ValueError(f"snapshot manifest id mismatch for {snapshot_id}")
    if manifest.get("schema_version") != identity["schema_version"]:
        raise ValueError(f"snapshot schema mismatch for {snapshot_id}")
    if manifest.get("files") != identity["files"]:
        raise ValueError(f"snapshot content mismatch for {snapshot_id}")


def _make_snapshot_read_only(destination: Path) -> None:
    data_dir = destination / "data"
    for filename in SNAPSHOT_FILENAMES:
        (data_dir / filename).chmod(0o444)
    (destination / "manifest.json").chmod(0o444)
    data_dir.chmod(0o555)
    destination.chmod(0o555)


def _authoritative_cutoff(
    as_of: pd.DataFrame,
    key_column: str,
    snapshot_path: Path,
) -> str:
    if key_column not in as_of.columns or as_of.empty:
        raise ValueError(f"platform as-of data has no {key_column}")
    values = [normalize_period_key(value, key_column) for value in as_of[key_column].tolist()]
    cutoff = values[-1]

    snapshot_frame = pd.read_csv(snapshot_path, dtype={key_column: "string"})
    if key_column not in snapshot_frame.columns:
        raise ValueError(f"{snapshot_path.name} is missing {key_column} cutoff column")
    available = {
        normalize_period_key(value, key_column)
        for value in snapshot_frame[key_column].tolist()
    }
    if cutoff not in available:
        raise ValueError(
            f"platform {key_column} cutoff {cutoff} does not exist in {snapshot_path.name}"
        )
    return cutoff


def normalize_period_key(value: object, field: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{field} must not be empty")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"{field} must be a six-digit platform key")
    return text


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
