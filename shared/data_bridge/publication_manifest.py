"""DataBridge publication manifest fields, identity and serialization; re-exported by refresh.py."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from shared.data_bridge.errors import DataBridgeRefreshError
from shared.data_bridge.filesystem import _fsync_directory
from shared.data_bridge.validation import (
    EXPECTED_FILENAMES,
    ValidatedDataBridgeDataset,
    read_dataset_directory,
    validate_dataset,
)
from shared.data_contract import (
    CALENDAR_SOURCE_TABLES,
    FACTOR_SOURCE_TABLES,
    METADATA_SOURCE_TABLE,
    SourceCommitEvidence,
    SourceTableEvidence,
    source_commit_evidence_payload,
    source_commit_evidence_sha256,
)


CURRENT_PUBLICATION_MANIFEST = ".publication-manifest.json"
CURRENT_PUBLICATION_MANIFEST_VERSION = "data-bridge-current-v3"
CURRENT_PUBLICATION_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "generation_id",
        "refresh_date",
        "schema_version",
        "business_digest",
        "files",
        "source_mode",
        "source_provenance",
    }
)
CURRENT_PUBLICATION_MANIFEST_FIELDS_BY_VERSION = {
    CURRENT_PUBLICATION_MANIFEST_VERSION: CURRENT_PUBLICATION_MANIFEST_FIELDS,
}
LOCAL_MYSQL_SOURCE_MODE = "local_mysql"
LOCAL_MYSQL_PROVENANCE_VERSION = "data-bridge-local-mysql-v1"


def _publication_identity_from_state(
    state: Mapping[str, object],
) -> dict[str, object]:
    """提取与 current 原子切换的稳定身份；排除后置 published_at。"""
    if not isinstance(state, Mapping):
        raise DataBridgeRefreshError(
            "DataBridge publication state must be an object"
        )
    version = state.get("manifest_version")
    if version is None:
        version = state.get("publication_manifest_version")
    if not isinstance(version, str):
        raise DataBridgeRefreshError(
            "DataBridge publication manifest version is invalid"
        )
    identity: dict[str, object] = {
        "manifest_version": version,
        "generation_id": copy.deepcopy(state.get("generation_id")),
        "refresh_date": copy.deepcopy(state.get("refresh_date")),
        "schema_version": copy.deepcopy(state.get("schema_version")),
        "business_digest": copy.deepcopy(
            state.get("business_digest")
        ),
        "files": copy.deepcopy(state.get("files")),
    }
    source_provenance = state.get("source_provenance")
    source_mode = state.get("source_mode")
    if version != CURRENT_PUBLICATION_MANIFEST_VERSION:
        raise DataBridgeRefreshError(
            "DataBridge publication manifest version is unsupported"
        )
    if source_mode is None:
        source_mode = "full_export"
    if not isinstance(source_mode, str) or not source_mode:
        raise DataBridgeRefreshError("DataBridge source_mode is invalid")
    if source_mode == LOCAL_MYSQL_SOURCE_MODE:
        normalized_provenance: dict[str, object] | None = (
            _normalize_source_provenance(source_provenance)
        )
    elif source_provenance is not None:
        raise DataBridgeRefreshError(
            "DataBridge non-local source must not carry local provenance"
        )
    else:
        normalized_provenance = None
    identity["source_mode"] = source_mode
    identity["source_provenance"] = normalized_provenance
    return identity


def require_local_mysql_source_provenance(
    state: Mapping[str, object],
    *,
    expected_daily_date: str | None = None,
) -> dict[str, object]:
    """返回已封存的本机 MySQL 来源证据；旧 current 一律拒绝。"""
    if not isinstance(state, Mapping):
        raise DataBridgeRefreshError(
            "DataBridge local source provenance state is invalid"
        )
    identity = _publication_identity_from_state(state)
    if (
        identity.get("manifest_version")
        != CURRENT_PUBLICATION_MANIFEST_VERSION
        or identity.get("source_mode") != LOCAL_MYSQL_SOURCE_MODE
    ):
        raise DataBridgeRefreshError(
            "DataBridge current lacks sealed local MySQL source provenance"
        )
    provenance = identity.get("source_provenance")
    if not isinstance(provenance, dict):
        raise DataBridgeRefreshError(
            "DataBridge local source provenance is invalid"
        )
    _require_provenance_feature_date(
        provenance,
        expected_daily_date=expected_daily_date,
    )
    return provenance


def _require_provenance_feature_date(
    provenance: Mapping[str, object],
    *,
    expected_daily_date: str | None,
) -> None:
    """本机 MySQL 水位必须与调用方 feature cutoff 完全一致。"""
    if expected_daily_date is None:
        return
    normalized_expected_date = _canonical_date_value(
        expected_daily_date,
        label="DataBridge expected_daily_date",
    )
    if provenance.get("feature_date") != normalized_expected_date:
        raise DataBridgeRefreshError(
            "DataBridge local source provenance feature_date must equal "
            "expected_daily_date"
        )


def _normalize_source_provenance(payload: object) -> dict[str, object]:
    """校验并规范化写入 current identity 的 MySQL 水位证据。"""
    if not isinstance(payload, Mapping):
        raise DataBridgeRefreshError(
            "DataBridge source_provenance must be an object"
        )
    required_fields = {
        "provenance_version",
        "feature_date",
        "source_rdate_cutoff",
        "snapshot_started_at",
        "source_commit_token",
        "source_evidence_sha256",
        "source_evidence",
    }
    if set(payload) != required_fields:
        raise DataBridgeRefreshError(
            "DataBridge source_provenance fields are invalid"
        )
    if payload.get("provenance_version") != LOCAL_MYSQL_PROVENANCE_VERSION:
        raise DataBridgeRefreshError(
            "DataBridge source_provenance version is invalid"
        )
    feature_date = _canonical_date_value(
        payload.get("feature_date"),
        label="DataBridge source_provenance.feature_date",
    )
    source_rdate_cutoff = _canonical_date_value(
        payload.get("source_rdate_cutoff"),
        label="DataBridge source_provenance.source_rdate_cutoff",
    )
    if source_rdate_cutoff != feature_date:
        raise DataBridgeRefreshError(
            "DataBridge source_provenance cutoff must equal feature_date"
        )
    snapshot_started_at = _canonical_aware_timestamp_value(
        payload.get("snapshot_started_at"),
        label="DataBridge source_provenance.snapshot_started_at",
    )
    source_commit_token = _canonical_sha256_value(
        payload.get("source_commit_token"),
        label="DataBridge source_provenance.source_commit_token",
    )
    source_evidence_sha256 = _canonical_sha256_value(
        payload.get("source_evidence_sha256"),
        label="DataBridge source_provenance.source_evidence_sha256",
    )
    if source_evidence_sha256 != source_commit_token:
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence hash mismatch"
        )
    raw_evidence = payload.get("source_evidence")
    if not isinstance(raw_evidence, Mapping) or set(raw_evidence) != {
        "evidence_version",
        "feature_date",
        "tables",
    }:
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence is invalid"
        )
    if raw_evidence.get("evidence_version") != "native-source-watermark-v1":
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence version is invalid"
        )
    evidence_feature_date = _canonical_date_value(
        raw_evidence.get("feature_date"),
        label="DataBridge source provenance evidence feature_date",
    )
    if evidence_feature_date != feature_date:
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence feature_date mismatch"
        )
    raw_tables = raw_evidence.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence tables are invalid"
        )
    tables: list[SourceTableEvidence] = []
    expected_table_fields = {
        "table_name",
        "row_count",
        "latest_create_time",
        "latest_update_time",
        "latest_business_key",
    }
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping) or set(raw_table) != expected_table_fields:
            raise DataBridgeRefreshError(
                "DataBridge source provenance evidence table is invalid"
            )
        table_name = raw_table.get("table_name")
        row_count = raw_table.get("row_count")
        if (
            not isinstance(table_name, str)
            or not table_name
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
        ):
            raise DataBridgeRefreshError(
                "DataBridge source provenance evidence table values are invalid"
            )
        values: dict[str, str | None] = {}
        for field_name in (
            "latest_create_time",
            "latest_update_time",
            "latest_business_key",
        ):
            value = raw_table.get(field_name)
            if value is not None and not isinstance(value, str):
                raise DataBridgeRefreshError(
                    "DataBridge source provenance evidence table values are invalid"
                )
            values[field_name] = value
        tables.append(
            SourceTableEvidence(
                table_name=table_name,
                row_count=row_count,
                latest_create_time=values["latest_create_time"],
                latest_update_time=values["latest_update_time"],
                latest_business_key=values["latest_business_key"],
            )
        )
    if [item.table_name for item in tables] != sorted(
        item.table_name for item in tables
    ):
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence tables are not canonical"
        )
    required_table_names = set(FACTOR_SOURCE_TABLES) | {
        METADATA_SOURCE_TABLE
    } | set(CALENDAR_SOURCE_TABLES)
    if tuple(item.table_name for item in tables) != tuple(
        sorted(required_table_names)
    ):
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence tables are incomplete"
        )
    evidence = SourceCommitEvidence(
        feature_date=feature_date,
        source_commit_token=source_commit_token,
        tables=tuple(tables),
    )
    canonical_evidence = source_commit_evidence_payload(evidence)
    if dict(raw_evidence) != canonical_evidence:
        raise DataBridgeRefreshError(
            "DataBridge source provenance evidence is not canonical"
        )
    if source_commit_evidence_sha256(evidence) != source_commit_token:
        raise DataBridgeRefreshError(
            "DataBridge source provenance token does not match evidence"
        )
    return {
        "provenance_version": LOCAL_MYSQL_PROVENANCE_VERSION,
        "feature_date": feature_date,
        "source_rdate_cutoff": source_rdate_cutoff,
        "snapshot_started_at": snapshot_started_at,
        "source_commit_token": source_commit_token,
        "source_evidence_sha256": source_evidence_sha256,
        "source_evidence": canonical_evidence,
    }


def _canonical_date_value(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise DataBridgeRefreshError(f"{label} is invalid")
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DataBridgeRefreshError(f"{label} is invalid") from exc
    if value != normalized:
        raise DataBridgeRefreshError(f"{label} is non-canonical")
    return normalized


def _canonical_aware_timestamp_value(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise DataBridgeRefreshError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataBridgeRefreshError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataBridgeRefreshError(f"{label} is timezone-naive")
    normalized = parsed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(
        timespec="seconds"
    )
    if value != normalized:
        raise DataBridgeRefreshError(f"{label} is non-canonical")
    return normalized


def _canonical_sha256_value(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DataBridgeRefreshError(f"{label} is invalid")
    return value


def data_bridge_publication_identity_sha256(
    state: Mapping[str, object],
) -> str:
    """返回 current 原子发布身份的稳定摘要。"""
    identity = _publication_identity_from_state(state)
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_publication_manifest_bytes(
    manifest: Mapping[str, object],
) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_publication_manifest(
    directory: Path,
    *,
    state: Mapping[str, object],
) -> None:
    marker_path = directory / CURRENT_PUBLICATION_MANIFEST
    if os.path.lexists(marker_path):
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest already exists"
        )
    payload = _canonical_publication_manifest_bytes(
        _publication_identity_from_state(state)
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".publication-manifest-",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o444)
        os.replace(temporary, marker_path)
        _fsync_directory(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_publication_manifest(
    directory: Path,
) -> dict[str, object]:
    marker_path = directory / CURRENT_PUBLICATION_MANIFEST
    try:
        descriptor = os.open(
            marker_path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest is missing"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            marker_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(marker_stat.st_mode):
                raise DataBridgeRefreshError(
                    "DataBridge current publication manifest is not a "
                    "regular file"
                )
            if stat.S_IMODE(marker_stat.st_mode) & 0o222:
                raise DataBridgeRefreshError(
                    "DataBridge current publication manifest must be "
                    "read-only"
                )
            raw = handle.read(1024 * 1024 + 1)
    except OSError as exc:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest is invalid"
        ) from exc
    if len(raw) > 1024 * 1024:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest is too large"
        )
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest is invalid"
        ) from exc
    if not isinstance(marker, dict):
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest must be an object"
        )
    manifest_version = marker.get("manifest_version")
    expected_fields = (
        CURRENT_PUBLICATION_MANIFEST_FIELDS_BY_VERSION.get(
            manifest_version
        )
        if isinstance(manifest_version, str)
        else None
    )
    if expected_fields is None or set(marker) != expected_fields:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest fields are invalid"
        )
    if marker != _publication_identity_from_state(marker):
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest identity is invalid"
        )
    if raw != _canonical_publication_manifest_bytes(marker):
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest is not canonical JSON"
        )
    return marker


def _validate_identity_against_dataset(
    identity: Mapping[str, object],
    *,
    dataset: ValidatedDataBridgeDataset,
) -> None:
    manifest_version = identity.get("manifest_version")
    if manifest_version == CURRENT_PUBLICATION_MANIFEST_VERSION:
        source_mode = identity.get("source_mode")
        source_provenance = identity.get("source_provenance")
        if not isinstance(source_mode, str) or not source_mode:
            raise DataBridgeRefreshError(
                "DataBridge publication manifest source mode is invalid"
            )
        if source_mode == LOCAL_MYSQL_SOURCE_MODE:
            _normalize_source_provenance(source_provenance)
        elif source_provenance is not None:
            raise DataBridgeRefreshError(
                "DataBridge publication manifest non-local source provenance "
                "is invalid"
            )
    generation_id = identity.get("generation_id")
    refresh_date = identity.get("refresh_date")
    business_digest = identity.get("business_digest")
    if (
        not isinstance(generation_id, str)
        or not generation_id
        or not isinstance(refresh_date, str)
        or date.fromisoformat(refresh_date).isoformat() != refresh_date
        or not isinstance(business_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", business_digest) is None
    ):
        raise DataBridgeRefreshError(
            "DataBridge publication manifest generation identity is invalid"
        )
    if identity.get("schema_version") != dataset.schema_version:
        raise DataBridgeRefreshError(
            "DataBridge publication manifest schema does not match files"
        )
    if identity.get("business_digest") != dataset.business_digest:
        raise DataBridgeRefreshError(
            "DataBridge publication manifest digest does not match files"
        )
    files = identity.get("files")
    if not isinstance(files, Mapping):
        raise DataBridgeRefreshError(
            "DataBridge publication manifest files profile is missing"
        )
    if set(files) != set(EXPECTED_FILENAMES):
        raise DataBridgeRefreshError(
            "DataBridge publication manifest files profile is incomplete"
        )
    for filename, profile in dataset.files.items():
        stored = files.get(filename)
        if (
            not isinstance(stored, Mapping)
            or stored.get("sha256") != profile.sha256
        ):
            raise DataBridgeRefreshError(
                "DataBridge publication manifest hash mismatch for "
                f"{filename}"
            )


def _directory_publication_identity(
    directory: Path,
    *,
    schema_path: Path,
) -> dict[str, object] | None:
    if not directory.is_dir():
        return None
    try:
        identity = _read_publication_manifest(directory)
        dataset = validate_dataset(
            read_dataset_directory(
                directory,
                allowed_sidecar_filenames=frozenset(
                    {CURRENT_PUBLICATION_MANIFEST}
                ),
            ),
            schema_path=schema_path,
        )
        _validate_identity_against_dataset(
            identity,
            dataset=dataset,
        )
        return identity
    except (DataBridgeRefreshError, OSError, ValueError):
        return None
