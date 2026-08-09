"""Blackbox V2 平台输入制品的规范化与权威来源捕获。"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import numbers
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

import pandas as pd

from shared.blackbox_v2.platform_input_registry import (
    PLATFORM_INPUT_REGISTRY,
    PlatformInputSpec,
)
from shared.calendar_service import read_calendar_snapshot_from_connection

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


_ISO_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_WEEK_ID_PATTERN = re.compile(r"^[0-9]{6}(?:\.0)?$")


@dataclass(frozen=True)
class FrozenPlatformInput:
    """一份已经规范化、可参与组合输入身份的平台制品。"""

    artifact_id: str
    provider_version: str
    filename: str
    columns: tuple[str, ...]
    content_bytes: bytes
    sha256: str
    size_bytes: int
    row_count: int
    audit_provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        spec = PLATFORM_INPUT_REGISTRY.get(self.artifact_id)
        if self.provider_version != spec.provider_version:
            raise ValueError(
                "platform input provider_version does not match registry spec"
            )
        if self.filename != spec.filename:
            raise ValueError(
                "platform input filename does not match registry spec"
            )
        if self.columns != spec.columns:
            raise ValueError(
                "platform input columns do not match registry spec"
            )
        if not isinstance(self.content_bytes, bytes):
            raise ValueError("platform input content_bytes must be bytes")
        actual_sha256 = hashlib.sha256(self.content_bytes).hexdigest()
        if self.sha256 != actual_sha256:
            raise ValueError(
                "platform input sha256 does not match content_bytes"
            )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes != len(self.content_bytes)
        ):
            raise ValueError(
                "platform input size_bytes does not match content_bytes"
            )
        if (
            isinstance(self.row_count, bool)
            or not isinstance(self.row_count, int)
            or self.row_count < 0
        ):
            raise ValueError(
                "platform input row_count must be a non-negative int"
            )
        _validate_canonical_csv(
            self.content_bytes,
            spec=spec,
            row_count=self.row_count,
        )
        if not isinstance(self.audit_provenance, Mapping):
            raise ValueError("audit_provenance must be a mapping")
        provenance = dict(self.audit_provenance)
        if any(not isinstance(key, str) for key in provenance):
            raise ValueError("audit_provenance keys must be strings")
        if any(
            value is not None and not isinstance(value, str)
            for value in provenance.values()
        ):
            raise ValueError(
                "audit_provenance values must be strings or null"
            )
        object.__setattr__(
            self,
            "audit_provenance",
            MappingProxyType(provenance),
        )

    @property
    def identity_manifest(self) -> dict[str, Any]:
        """返回不含捕获来源与时间的稳定内容身份字段。"""
        return {
            "artifact_id": self.artifact_id,
            "provider_version": self.provider_version,
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "row_count": self.row_count,
            "columns": list(self.columns),
        }

    @property
    def audit_manifest(self) -> dict[str, Any]:
        """返回内容身份与非身份 provenance 的隔离副本。"""
        return {
            "identity": self.identity_manifest,
            "provenance": dict(self.audit_provenance),
        }


@dataclass(frozen=True)
class PlatformInputProvider:
    """一个平台注册制品的规范化与权威冻结来源。"""

    spec: PlatformInputSpec
    normalize_frame: Callable[[pd.DataFrame, PlatformInputSpec, object], pd.DataFrame]
    normalize_content: Callable[[pd.DataFrame, PlatformInputSpec], pd.DataFrame]
    calendar_snapshot_filename: str


def freeze_platform_input(
    artifact_id: str,
    frame: pd.DataFrame,
    *,
    weekly_cutoff_key: object,
    audit_provenance: Mapping[str, Any] | None = None,
) -> FrozenPlatformInput:
    """按 provider 契约校验并冻结一份平台输入制品。"""
    provider = _provider(artifact_id)
    spec = provider.spec
    normalized = provider.normalize_frame(
        frame,
        spec,
        weekly_cutoff_key,
    )
    content = normalized.to_csv(
        index=False,
        lineterminator="\n",
    ).encode("utf-8")
    provenance = {} if audit_provenance is None else audit_provenance
    return FrozenPlatformInput(
        artifact_id=spec.artifact_id,
        provider_version=spec.provider_version,
        filename=spec.filename,
        columns=spec.columns,
        content_bytes=content,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        row_count=int(len(normalized)),
        audit_provenance=provenance,
    )


def capture_platform_inputs_from_connection(
    platform_input_ids: Iterable[str],
    *,
    connection: Connection,
    weekly_cutoff_key: object,
    captured_at: str | None = None,
) -> tuple[FrozenPlatformInput, ...]:
    """从调用方只读事务捕获 Harness 所需的平台输入。"""
    artifact_ids = _normalize_selection(platform_input_ids)
    if not artifact_ids:
        return ()
    calendar_frames = read_calendar_snapshot_from_connection(connection)
    provenance = _provenance(
        source_kind="harness_database",
        generation_id=None,
        manifest_sha256=None,
        captured_at=captured_at,
    )
    return tuple(
        freeze_platform_input(
            artifact_id,
            _frame_from_calendar_snapshot(
                calendar_frames,
                _provider(artifact_id),
            ),
            weekly_cutoff_key=weekly_cutoff_key,
            audit_provenance=provenance,
        )
        for artifact_id in artifact_ids
    )


def _normalize_api_wind_date(
    frame: pd.DataFrame,
    spec: PlatformInputSpec,
    weekly_cutoff_key: object,
) -> pd.DataFrame:
    normalized = _normalize_api_wind_date_content(frame, spec)
    cutoff = _normalize_week_id(weekly_cutoff_key, "weekly_cutoff_key")
    if cutoff not in set(normalized["week_id"].tolist()):
        raise ValueError(
            f"{spec.filename} does not cover weekly_cutoff_key={cutoff}"
        )
    return normalized


def _normalize_api_wind_date_content(
    frame: pd.DataFrame,
    spec: PlatformInputSpec,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{spec.filename} source must be a DataFrame")
    actual_columns = tuple(frame.columns)
    if actual_columns != spec.columns:
        raise ValueError(
            f"{spec.filename} columns must be exactly {list(spec.columns)}; "
            f"got {list(actual_columns)}"
        )
    if frame.empty:
        raise ValueError(f"{spec.filename} must not be empty")

    dates = [_normalize_rdate(value) for value in frame["rdate"].tolist()]
    if len(dates) != len(set(dates)):
        raise ValueError(f"{spec.filename} rdate must be unique")
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise ValueError(f"{spec.filename} rdate must be strictly ascending")

    week_ids = [
        _normalize_week_id(value, "week_id")
        for value in frame["week_id"].tolist()
    ]
    return pd.DataFrame(
        {"rdate": dates, "week_id": week_ids},
        columns=list(spec.columns),
    )


def _validate_canonical_csv(
    content: bytes,
    *,
    spec: PlatformInputSpec,
    row_count: int,
) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("platform input CSV must use UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise ValueError("platform input CSV must use LF and end with LF")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError("platform input content is not valid CSV") from exc
    if not rows or rows[0] != list(spec.columns):
        raise ValueError(
            "platform input CSV header does not match registered columns"
        )
    data_rows = rows[1:]
    if len(data_rows) != row_count:
        raise ValueError(
            "platform input row_count does not match canonical CSV rows"
        )
    if any(len(row) != len(spec.columns) for row in data_rows):
        raise ValueError(
            "platform input CSV data rows must match registered columns"
        )
    frame = pd.DataFrame(data_rows, columns=list(spec.columns))
    normalized = _provider(spec.artifact_id).normalize_content(frame, spec)
    canonical = normalized.to_csv(
        index=False,
        lineterminator="\n",
    ).encode("utf-8")
    if content != canonical:
        raise ValueError(
            "platform input content_bytes are not provider-canonical CSV"
        )


def _normalize_rdate(value: object) -> str:
    if pd.isna(value):
        raise ValueError("api_wind_date.csv rdate must not be empty")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not _ISO_DATE_PATTERN.fullmatch(text):
        raise ValueError("api_wind_date.csv rdate must use YYYY-MM-DD")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            "api_wind_date.csv rdate must be a valid YYYY-MM-DD date"
        ) from exc


def _normalize_week_id(value: object, field: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise ValueError(f"{field} must not be empty")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a six-digit platform key")
    if isinstance(value, numbers.Integral):
        text = str(int(value))
    elif isinstance(value, numbers.Real):
        numeric = float(value)
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{field} must be a six-digit platform key")
        text = str(int(numeric))
    else:
        text = str(value).strip()
        if not _WEEK_ID_PATTERN.fullmatch(text):
            raise ValueError(f"{field} must be a six-digit platform key")
        if text.endswith(".0"):
            text = text[:-2]
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"{field} must be a six-digit platform key")
    return text


def _normalize_selection(
    platform_input_ids: Iterable[str],
) -> tuple[str, ...]:
    if isinstance(platform_input_ids, (str, bytes)):
        raise ValueError("platform input selection must be an iterable of IDs")
    values = tuple(platform_input_ids)
    if not values:
        return ()
    return PLATFORM_INPUT_REGISTRY.normalize_ids(values)


def _frame_from_calendar_snapshot(
    frames: Mapping[str, pd.DataFrame],
    provider: PlatformInputProvider,
) -> pd.DataFrame:
    try:
        return frames[provider.calendar_snapshot_filename]
    except KeyError as exc:
        raise ValueError(
            "calendar snapshot is missing required file "
            f"{provider.calendar_snapshot_filename}"
        ) from exc


def _provider(artifact_id: str) -> PlatformInputProvider:
    PLATFORM_INPUT_REGISTRY.get(artifact_id)
    try:
        return PLATFORM_INPUT_PROVIDERS[artifact_id]
    except KeyError as exc:
        raise ValueError(
            f"no platform input provider implementation for {artifact_id!r}"
        ) from exc


def _provenance(
    *,
    source_kind: str,
    generation_id: str | None,
    manifest_sha256: str | None,
    captured_at: str | None,
) -> dict[str, str | None]:
    capture_time = captured_at
    if capture_time is None:
        capture_time = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if not isinstance(capture_time, str) or not capture_time.strip():
        raise ValueError("captured_at must be a non-empty string")
    return {
        "source_kind": source_kind,
        "generation_id": generation_id,
        "manifest_sha256": manifest_sha256,
        "captured_at": capture_time,
    }


PLATFORM_INPUT_PROVIDERS: Mapping[str, PlatformInputProvider] = MappingProxyType(
    {
        "api-wind-date-v1": PlatformInputProvider(
            spec=PLATFORM_INPUT_REGISTRY.get("api-wind-date-v1"),
            normalize_frame=_normalize_api_wind_date,
            normalize_content=_normalize_api_wind_date_content,
            calendar_snapshot_filename="api_wind_date.csv",
        ),
    }
)
