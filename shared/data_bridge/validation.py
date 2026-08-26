from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

import pandas as pd


EXPECTED_FILENAMES = (
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
    "api_wind_date.csv",
)
LEGACY_THREE_FILENAMES = EXPECTED_FILENAMES[:3]
TIME_KEY_BY_FILE = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
    "api_wind_date.csv": "rdate",
}


class DataBridgeValidationError(ValueError):
    """A candidate DataBridge dataset violates the compatibility contract."""


@dataclass(frozen=True)
class DataBridgeFileProfile:
    filename: str
    rows: int
    columns: int
    min_key: str
    max_key: str
    sha256: str
    business_hash: str
    keys: frozenset[str]


@dataclass(frozen=True)
class ValidatedDataBridgeDataset:
    schema_version: str
    frames: Mapping[str, pd.DataFrame]
    files: Mapping[str, DataBridgeFileProfile]
    business_digest: str


def validate_baseline_compatible_columns(
    filename: str,
    actual_columns: list[str],
    baseline_columns: list[str],
) -> None:
    """校验实际表头兼容最低字段基线，同时允许新增业务列。"""
    key_column = TIME_KEY_BY_FILE.get(filename)
    if key_column is None:
        raise DataBridgeValidationError(f"unsupported DataBridge filename: {filename}")
    validate_unique_csv_header(filename, actual_columns)
    if not actual_columns or actual_columns[0] != key_column:
        raise DataBridgeValidationError(f"{filename} must start with {key_column}")
    if not baseline_columns or baseline_columns[0] != key_column:
        raise DataBridgeValidationError(
            f"{filename} baseline must start with {key_column}"
        )
    if len(baseline_columns) != len(set(baseline_columns)):
        raise DataBridgeValidationError(f"{filename} baseline contains duplicate columns")
    missing = [column for column in baseline_columns if column not in actual_columns]
    if missing:
        raise DataBridgeValidationError(
            f"{filename} missing baseline columns: {missing[:10]}"
        )
    positions = [actual_columns.index(column) for column in baseline_columns]
    if any(left >= right for left, right in zip(positions, positions[1:])):
        raise DataBridgeValidationError(
            f"{filename} baseline column order changed"
        )


def validate_unique_csv_header(filename: str, columns: list[str]) -> None:
    """在 pandas 自动改写重复字段前校验原始 CSV 表头。"""
    if not columns:
        raise DataBridgeValidationError(f"{filename} header must not be empty")
    if len(columns) != len(set(columns)):
        raise DataBridgeValidationError(f"{filename} contains duplicate columns")


def validate_dataset(
    frames: Mapping[str, pd.DataFrame],
    *,
    schema_path: str | Path,
    expected_daily_date: str | None = None,
    previous_keys: Mapping[str, set[str] | frozenset[str]] | None = None,
    continuity_cutoffs: Mapping[str, object] | None = None,
) -> ValidatedDataBridgeDataset:
    return _validate_dataset(
        frames,
        schema_path=schema_path,
        filenames=EXPECTED_FILENAMES,
        expected_daily_date=expected_daily_date,
        previous_keys=previous_keys,
        continuity_cutoffs=continuity_cutoffs,
    )


def validate_legacy_three_file_dataset(
    frames: Mapping[str, pd.DataFrame],
    *,
    schema_path: str | Path,
) -> ValidatedDataBridgeDataset:
    """仅用于把已验证的旧三文件 current 平滑升级为四文件 generation。"""
    return _validate_dataset(
        frames,
        schema_path=schema_path,
        filenames=LEGACY_THREE_FILENAMES,
    )


def _validate_dataset(
    frames: Mapping[str, pd.DataFrame],
    *,
    schema_path: str | Path,
    filenames: tuple[str, ...],
    expected_daily_date: str | None = None,
    previous_keys: Mapping[str, set[str] | frozenset[str]] | None = None,
    continuity_cutoffs: Mapping[str, object] | None = None,
) -> ValidatedDataBridgeDataset:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    expected_files = schema.get("files")
    if not isinstance(expected_files, dict) or set(expected_files) != set(EXPECTED_FILENAMES):
        raise DataBridgeValidationError("DataBridge schema must define exactly four files")
    if set(frames) != set(filenames):
        raise DataBridgeValidationError(
            f"DataBridge dataset must contain exactly {len(filenames)} files"
        )
    normalized_continuity_cutoffs: dict[str, str] | None = None
    if continuity_cutoffs is not None:
        if previous_keys is None:
            raise DataBridgeValidationError(
                "DataBridge continuity cutoffs require previous keys"
            )
        if (
            set(continuity_cutoffs) != set(previous_keys)
            or not set(previous_keys).issubset(filenames)
        ):
            raise DataBridgeValidationError(
                "DataBridge continuity cutoffs must match the previous files"
            )
        normalized_continuity_cutoffs = {}
        for filename in continuity_cutoffs:
            cutoff = _normalize_key(
                filename,
                continuity_cutoffs[filename],
            )
            normalized_previous = {
                _normalize_key(filename, value)
                for value in previous_keys[filename]
            }
            if cutoff not in normalized_previous:
                raise DataBridgeValidationError(
                    f"{filename} continuity cutoff is absent from previous keys"
                )
            normalized_continuity_cutoffs[filename] = cutoff

    validated_frames: dict[str, pd.DataFrame] = {}
    profiles: dict[str, DataBridgeFileProfile] = {}
    dataset_hash = hashlib.sha256()
    for filename in filenames:
        frame = frames[filename].copy()
        columns = list(frame.columns)
        baseline_columns = list(expected_files[filename].get("columns", []))
        validate_baseline_compatible_columns(
            filename,
            columns,
            baseline_columns,
        )
        if frame.empty:
            raise DataBridgeValidationError(f"{filename} must not be empty")
        key_column = TIME_KEY_BY_FILE[filename]
        normalized_keys = [_normalize_key(filename, value) for value in frame[key_column].tolist()]
        if len(normalized_keys) != len(set(normalized_keys)):
            raise DataBridgeValidationError(f"{filename} {key_column} must be unique")
        order = sorted(range(len(frame)), key=normalized_keys.__getitem__)
        frame = frame.iloc[order].reset_index(drop=True)
        normalized_keys = [normalized_keys[index] for index in order]

        canonical_rows: list[str] = []
        for row_index, row in frame.iterrows():
            tokens = [normalized_keys[row_index]]
            for column in columns[1:]:
                try:
                    tokens.append(_numeric_token(row[column]))
                except DataBridgeValidationError as exc:
                    raise DataBridgeValidationError(
                        f"{filename} {column} must contain only finite numeric or empty values; "
                        f"row={row_index + 2}"
                    ) from exc
            canonical_rows.append("\x1f".join(tokens))

        key_set = frozenset(normalized_keys)
        if previous_keys and filename in previous_keys:
            required_previous = {
                _normalize_key(filename, value)
                for value in previous_keys[filename]
            }
            if (
                normalized_continuity_cutoffs is not None
                and filename != "api_wind_date.csv"
            ):
                cutoff = normalized_continuity_cutoffs[filename]
                required_previous = {
                    key
                    for key in required_previous
                    if key <= cutoff
                }
            missing = required_previous - set(key_set)
            if missing:
                raise DataBridgeValidationError(
                    f"{filename} historical keys disappeared: {sorted(missing)[:10]}"
                )
        if filename == "daily_output.csv" and expected_daily_date:
            expected = date.fromisoformat(expected_daily_date).isoformat()
            if normalized_keys[-1] < expected:
                raise DataBridgeValidationError(
                    f"daily_output.csv latest date {normalized_keys[-1]} is earlier than expected {expected}"
                )
            if normalized_keys[-1] > expected:
                raise DataBridgeValidationError(
                    "daily_output.csv latest date "
                    f"{normalized_keys[-1]} is later than feature cutoff {expected}"
                )
        if filename == "api_wind_date.csv":
            if columns != ["rdate", "week_id"]:
                raise DataBridgeValidationError(
                    "api_wind_date.csv columns must be exactly rdate,week_id"
                )
            if expected_daily_date:
                expected = date.fromisoformat(expected_daily_date).isoformat()
                if expected not in key_set:
                    raise DataBridgeValidationError(
                        "api_wind_date.csv does not cover expected daily date "
                        f"{expected}"
                    )
            week_ids = [
                _normalize_key("weekly_output.csv", value)
                for value in frame["week_id"].tolist()
            ]
            frame["week_id"] = week_ids
            frame["rdate"] = normalized_keys

        business_hash = hashlib.sha256("\n".join(canonical_rows).encode("utf-8")).hexdigest()
        rendered = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        file_hash = hashlib.sha256(rendered).hexdigest()
        dataset_hash.update(filename.encode("ascii"))
        dataset_hash.update("\0".join(columns).encode("utf-8"))
        dataset_hash.update(bytes.fromhex(business_hash))
        validated_frames[filename] = frame
        profiles[filename] = DataBridgeFileProfile(
            filename=filename,
            rows=len(frame),
            columns=len(columns),
            min_key=normalized_keys[0],
            max_key=normalized_keys[-1],
            sha256=file_hash,
            business_hash=business_hash,
            keys=key_set,
        )

    return ValidatedDataBridgeDataset(
        schema_version=str(schema.get("schema_version", "")),
        frames=validated_frames,
        files=profiles,
        business_digest=dataset_hash.hexdigest(),
    )


def write_validated_dataset(dataset: ValidatedDataBridgeDataset, directory: str | Path) -> Path:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=False)
    for filename in EXPECTED_FILENAMES:
        dataset.frames[filename].to_csv(
            destination / filename,
            index=False,
            lineterminator="\n",
        )
    return destination


def read_dataset_directory(
    directory: str | Path,
    *,
    allowed_sidecar_filenames: frozenset[str] = frozenset(),
) -> dict[str, pd.DataFrame]:
    return _read_dataset_directory(
        directory,
        filenames=EXPECTED_FILENAMES,
        allowed_sidecar_filenames=allowed_sidecar_filenames,
    )


def read_legacy_three_file_directory(
    directory: str | Path,
    *,
    allowed_sidecar_filenames: frozenset[str] = frozenset(),
) -> dict[str, pd.DataFrame]:
    """读取旧三文件 current；只供一次性 generation 升级校验。"""
    return _read_dataset_directory(
        directory,
        filenames=LEGACY_THREE_FILENAMES,
        allowed_sidecar_filenames=allowed_sidecar_filenames,
    )


def _read_dataset_directory(
    directory: str | Path,
    *,
    filenames: tuple[str, ...],
    allowed_sidecar_filenames: frozenset[str],
) -> dict[str, pd.DataFrame]:
    root = Path(directory)
    entries = {path.name for path in root.iterdir()} if root.is_dir() else set()
    expected_entries = set(filenames) | set(
        allowed_sidecar_filenames
    )
    if entries != expected_entries:
        raise DataBridgeValidationError(
            "DataBridge directory files must be exactly "
            f"{sorted(expected_entries)}"
        )
    frames: dict[str, pd.DataFrame] = {}
    for filename in filenames:
        path = root / filename
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle))
        except StopIteration as exc:
            raise DataBridgeValidationError(f"{filename} header must not be empty") from exc
        except (OSError, UnicodeError, csv.Error) as exc:
            raise DataBridgeValidationError(f"{filename} header is invalid") from exc
        validate_unique_csv_header(filename, header)
        frames[filename] = pd.read_csv(
            path,
            dtype="string",
            keep_default_na=False,
        )
    return frames


def _normalize_key(filename: str, value: object) -> str:
    if pd.isna(value) or not str(value).strip():
        raise DataBridgeValidationError(f"{TIME_KEY_BY_FILE[filename]} must not be empty")
    text = str(value).strip()
    if filename in {"daily_output.csv", "api_wind_date.csv"}:
        try:
            parsed = pd.to_datetime(text, errors="raise")
        except (TypeError, ValueError) as exc:
            raise DataBridgeValidationError("date must be parseable") from exc
        if isinstance(parsed, pd.DatetimeIndex):
            parsed = parsed[0]
        return parsed.date().isoformat()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise DataBridgeValidationError(f"{TIME_KEY_BY_FILE[filename]} must be a six-digit key")
    return text


def _numeric_token(value: object) -> str:
    if pd.isna(value) or not str(value).strip():
        return "<NULL>"
    try:
        numeric = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DataBridgeValidationError("not numeric") from exc
    if not numeric.is_finite():
        raise DataBridgeValidationError("not finite")
    if numeric == 0:
        return "0"
    return format(numeric.normalize(), "f")
