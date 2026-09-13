from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from shared.data_bridge.validation import (
    EXPECTED_FILENAMES as SNAPSHOT_FILENAMES,
    FACTOR_CATALOG_COLUMNS,
    FACTOR_CATALOG_FREQUENCIES,
    FACTOR_VERSION_PATTERN,
    LEGACY_FOUR_FILENAMES,
    TIME_KEY_BY_FILE,
    validate_baseline_compatible_columns,
)


@dataclass(frozen=True)
class BlackboxSnapshot:
    snapshot_id: str
    root_dir: Path
    data_dir: Path
    manifest_path: Path
    schema_version: str
    generation_id: str | None = None
    refresh_date: str | None = None
    business_digest: str | None = None
    daily_cutoff_keys: tuple[str, ...] | None = None
    weekly_cutoff_keys: tuple[str, ...] | None = None
    monthly_cutoff_keys: tuple[str, ...] | None = None
    calendar_week_ids_by_date: Mapping[str, str] | None = None
    sealed_file_fingerprints: Mapping[str, tuple[int, ...]] | None = None


@dataclass(frozen=True)
class BlackboxInputBundle:
    """DataBridge generation 标准文件组成的一次执行输入。"""

    combined_snapshot_id: str
    parent_snapshot_id: str
    base_snapshot: BlackboxSnapshot
    expected_filenames: tuple[str, ...]


@dataclass(frozen=True)
class CutoffKeys:
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str


def compose_blackbox_input_bundle(
    base_snapshot: BlackboxSnapshot,
    *,
    factor_input_mode: str = "legacy_v1",
) -> BlackboxInputBundle:
    """将已含标准文件的 generation 快照交给运行视图。"""
    if not isinstance(base_snapshot, BlackboxSnapshot):
        raise ValueError("base_snapshot must be a BlackboxSnapshot")
    if factor_input_mode == "legacy_v1":
        expected_filenames = LEGACY_FOUR_FILENAMES
    elif factor_input_mode == "algorithm_managed":
        expected_filenames = SNAPSHOT_FILENAMES
    else:
        raise ValueError(f"unsupported factor_input_mode: {factor_input_mode}")
    return BlackboxInputBundle(
        combined_snapshot_id=base_snapshot.snapshot_id,
        parent_snapshot_id=base_snapshot.snapshot_id,
        base_snapshot=base_snapshot,
        expected_filenames=expected_filenames,
    )


def create_snapshot_from_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    output_root: Path,
    expected_columns: Mapping[str, list[str]],
    schema_version: str,
) -> BlackboxSnapshot:
    """将 DataBridge 标准文件固化为内容寻址、只读的数据快照。"""
    _validate_frame_set(frames, expected_columns)
    filenames = tuple(frames)
    rendered = {
        filename: frames[filename].to_csv(index=False, lineterminator="\n").encode("utf-8")
        for filename in filenames
    }
    file_entries = {
        filename: {
            "sha256": hashlib.sha256(rendered[filename]).hexdigest(),
            "row_count": int(len(frames[filename])),
            "columns": list(frames[filename].columns),
        }
        for filename in filenames
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
        daily_cutoff_keys=tuple(
            normalize_daily_key(value)
            for value in frames["daily_output.csv"]["date"].tolist()
        ),
        weekly_cutoff_keys=tuple(
            normalize_period_key(value, "week_id")
            for value in frames["weekly_output.csv"]["week_id"].tolist()
        ),
        monthly_cutoff_keys=tuple(
            normalize_period_key(value, "month_id")
            for value in frames["monthly_output.csv"]["month_id"].tolist()
        ),
        calendar_week_ids_by_date=MappingProxyType(
            {
                normalize_daily_key(row.rdate): normalize_period_key(
                    row.week_id,
                    "week_id",
                )
                for row in frames["api_wind_date.csv"].itertuples(
                    index=False
                )
            }
        ),
    )


def _validate_frame_set(
    frames: Mapping[str, pd.DataFrame],
    expected_columns: Mapping[str, list[str]],
) -> None:
    actual_files = set(frames)
    allowed_file_sets = {
        frozenset(SNAPSHOT_FILENAMES),
        frozenset(LEGACY_FOUR_FILENAMES),
    }
    if frozenset(actual_files) not in allowed_file_sets:
        raise ValueError("snapshot files must be the standard five or legacy four")
    if not actual_files.issubset(expected_columns):
        raise ValueError("snapshot schema does not define every input file")
    for filename in frames:
        frame = frames[filename]
        if frame.empty:
            raise ValueError(f"{filename} must not be empty")
        actual = list(frame.columns)
        if filename == "factor_catalog.csv":
            if actual != list(expected_columns[filename]):
                raise ValueError("factor_catalog.csv columns are invalid")
            _validate_catalog_frame(frame)
            continue
        validate_baseline_compatible_columns(
            filename,
            actual,
            list(expected_columns[filename]),
        )
        _validate_frame_content(filename, frame)


def _validate_catalog_frame(frame: pd.DataFrame) -> None:
    if list(frame.columns) != list(FACTOR_CATALOG_COLUMNS):
        raise ValueError("factor_catalog.csv columns are invalid")
    if frame.empty:
        raise ValueError("factor_catalog.csv must not be empty")
    normalized: dict[str, list[str]] = {}
    for column in FACTOR_CATALOG_COLUMNS:
        values = frame[column]
        if values.isna().any():
            raise ValueError(f"factor_catalog.csv {column} must not be null")
        texts = values.astype(str).tolist()
        if any(not value or value != value.strip() for value in texts):
            raise ValueError(f"factor_catalog.csv {column} is invalid")
        normalized[column] = texts
    codes = normalized["indicators_code"]
    if len(codes) != len(set(codes)):
        raise ValueError("factor_catalog.csv indicators_code must be unique")
    if any(
        value not in FACTOR_CATALOG_FREQUENCIES
        for value in normalized["frequency"]
    ):
        raise ValueError("factor_catalog.csv frequency is invalid")
    if any(
        FACTOR_VERSION_PATTERN.fullmatch(value) is None
        for value in normalized["factor_version"]
    ):
        raise ValueError("factor_catalog.csv factor_version is invalid")


def _validate_frame_content(filename: str, frame: pd.DataFrame) -> None:
    key_column = TIME_KEY_BY_FILE[filename]
    if key_column not in frame.columns:
        raise ValueError(f"{filename} is missing {key_column}")
    if key_column in {"date", "rdate"}:
        keys = [normalize_daily_key(value) for value in frame[key_column].tolist()]
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


def normalize_daily_key(value: object) -> str:
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
        for filename in rendered:
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
    for path in data_dir.iterdir():
        path.chmod(0o444)
    (destination / "manifest.json").chmod(0o444)
    data_dir.chmod(0o555)
    destination.chmod(0o555)


def normalize_period_key(value: object, field: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{field} must not be empty")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"{field} must be a six-digit platform key")
    return text
