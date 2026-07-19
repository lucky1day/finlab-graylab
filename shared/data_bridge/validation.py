from __future__ import annotations

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
)
TIME_KEY_BY_FILE = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
}


class DataBridgeValidationError(ValueError):
    """A candidate DataBridge dataset violates the frozen contract."""


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


def validate_dataset(
    frames: Mapping[str, pd.DataFrame],
    *,
    schema_path: str | Path,
    expected_daily_date: str | None = None,
    previous_keys: Mapping[str, set[str] | frozenset[str]] | None = None,
) -> ValidatedDataBridgeDataset:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    expected_files = schema.get("files")
    if not isinstance(expected_files, dict) or set(expected_files) != set(EXPECTED_FILENAMES):
        raise DataBridgeValidationError("DataBridge schema must define exactly three files")
    if set(frames) != set(EXPECTED_FILENAMES):
        raise DataBridgeValidationError("DataBridge dataset must contain exactly three files")

    validated_frames: dict[str, pd.DataFrame] = {}
    profiles: dict[str, DataBridgeFileProfile] = {}
    dataset_hash = hashlib.sha256()
    for filename in EXPECTED_FILENAMES:
        frame = frames[filename].copy()
        columns = list(frame.columns)
        expected_columns = list(expected_files[filename].get("columns", []))
        if columns != expected_columns:
            raise DataBridgeValidationError(
                f"{filename} columns do not match frozen schema: actual={columns!r}"
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
            missing = set(previous_keys[filename]) - set(key_set)
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


def read_dataset_directory(directory: str | Path) -> dict[str, pd.DataFrame]:
    root = Path(directory)
    entries = {path.name for path in root.iterdir()} if root.is_dir() else set()
    if entries != set(EXPECTED_FILENAMES):
        raise DataBridgeValidationError(
            f"DataBridge directory files must be exactly {list(EXPECTED_FILENAMES)}"
        )
    return {
        filename: pd.read_csv(root / filename, dtype="string", keep_default_na=False)
        for filename in EXPECTED_FILENAMES
    }


def _normalize_key(filename: str, value: object) -> str:
    if pd.isna(value) or not str(value).strip():
        raise DataBridgeValidationError(f"{TIME_KEY_BY_FILE[filename]} must not be empty")
    text = str(value).strip()
    if filename == "daily_output.csv":
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
