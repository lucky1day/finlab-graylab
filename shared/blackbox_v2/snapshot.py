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
from typing import Mapping

import pandas as pd

from shared.data_bridge.validation import validate_baseline_compatible_columns


SNAPSHOT_FILENAMES = (
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
)
TIME_KEY_BY_FILE = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
}


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
class CutoffKeys:
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str


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
        keys = [_normalize_period_key(value, key_column) for value in frame[key_column].tolist()]
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
    values = [_normalize_period_key(value, key_column) for value in as_of[key_column].tolist()]
    cutoff = values[-1]

    snapshot_frame = pd.read_csv(snapshot_path, dtype={key_column: "string"})
    if key_column not in snapshot_frame.columns:
        raise ValueError(f"{snapshot_path.name} is missing {key_column} cutoff column")
    available = {
        _normalize_period_key(value, key_column)
        for value in snapshot_frame[key_column].tolist()
    }
    if cutoff not in available:
        raise ValueError(
            f"platform {key_column} cutoff {cutoff} does not exist in {snapshot_path.name}"
        )
    return cutoff


def _normalize_period_key(value: object, field: str) -> str:
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
