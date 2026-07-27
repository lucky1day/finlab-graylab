from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from shared.data_bridge.refresh import (
    DataBridgeRefreshConfig,
    DataBridgeRefreshError,
    check_current_dataset,
)
from shared.data_bridge.validation import DataBridgeValidationError
from shared.input_artifacts import (
    _resolve_blackbox_input_cutoffs_bulk_from_keys,
)


AUTHORITY_SCHEMA_VERSION = "stable-databridge-current-authority-v1"


class DataBridgeCurrentAuthorityError(RuntimeError):
    """稳定 DataBridge current authority 无法建立。"""


class DataBridgeCurrentMissingError(DataBridgeCurrentAuthorityError):
    """DataBridge current 尚未发布。"""


class DataBridgeCurrentInvalidError(DataBridgeCurrentAuthorityError):
    """DataBridge current 或其稳定身份无效。"""


@dataclass(frozen=True, slots=True)
class StablePublicationCapability:
    occurrence_id: int
    business_date: str
    epoch: int
    mode: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class StableDataBridgeFileIdentity:
    filename: str
    rows: int
    columns: int
    min_key: str
    max_key: str
    sha256: str
    business_hash: str


@dataclass(frozen=True, slots=True)
class StableDataBridgeCutoff:
    feature_date: str
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str


@dataclass(frozen=True, slots=True)
class StableDataBridgeCurrentAuthority:
    authority_schema_version: str
    generation_id: str
    refresh_date: str
    schema_version: str
    business_digest: str
    publication_capability: StablePublicationCapability | None
    files: tuple[StableDataBridgeFileIdentity, ...]
    cutoffs: tuple[StableDataBridgeCutoff, ...]
    stable_identity_sha256: str


def resolve_stable_databridge_current_authority(
    config: DataBridgeRefreshConfig,
    *,
    feature_dates: Iterable[str],
    connection: Any,
) -> StableDataBridgeCurrentAuthority:
    """校验 current，并用 caller Connection 冻结稳定发布与截止身份。"""
    if connection is None:
        raise ValueError("connection is required")
    normalized_dates = tuple(sorted({
        date.fromisoformat(str(value)[:10]).isoformat()
        for value in feature_dates
    }))
    try:
        current = check_current_dataset(config)
    except (
        DataBridgeRefreshError,
        DataBridgeValidationError,
        OSError,
        ValueError,
    ) as exc:
        if not (config.runtime_root / "state.json").is_file():
            raise DataBridgeCurrentMissingError(
                "DataBridge current is unavailable"
            ) from exc
        raise DataBridgeCurrentInvalidError(
            "DataBridge current is invalid"
        ) from exc

    generation_id = _required_text(
        current.state,
        "generation_id",
    )
    refresh_date = _required_date(
        current.state,
        "refresh_date",
    )
    publication_capability = _stable_publication_capability(
        current.state.get("publication_capability")
    )
    files = tuple(
        StableDataBridgeFileIdentity(
            filename=profile.filename,
            rows=profile.rows,
            columns=profile.columns,
            min_key=profile.min_key,
            max_key=profile.max_key,
            sha256=profile.sha256,
            business_hash=profile.business_hash,
        )
        for _, profile in sorted(current.dataset.files.items())
    )
    cutoff_keys = {
        "date": sorted(
            current.dataset.files["daily_output.csv"].keys
        ),
        "week_id": set(
            current.dataset.files["weekly_output.csv"].keys
        ),
        "month_id": set(
            current.dataset.files["monthly_output.csv"].keys
        ),
    }
    try:
        resolved = _resolve_blackbox_input_cutoffs_bulk_from_keys(
            cutoff_keys,
            feature_dates=normalized_dates,
            connection=connection,
            schema_path=config.schema_path,
        )
    except ValueError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge current cutoff authority is invalid"
        ) from exc
    if set(resolved) != set(normalized_dates):
        raise DataBridgeCurrentInvalidError(
            "DataBridge current cutoff authority is incomplete"
        )
    cutoffs = tuple(
        StableDataBridgeCutoff(
            feature_date=feature_date,
            daily_cutoff_key=resolved[feature_date].daily_cutoff_key,
            weekly_cutoff_key=resolved[feature_date].weekly_cutoff_key,
            monthly_cutoff_key=resolved[feature_date].monthly_cutoff_key,
        )
        for feature_date in normalized_dates
    )
    payload = {
        "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "schema_version": current.dataset.schema_version,
        "business_digest": current.dataset.business_digest,
        "publication_capability": (
            _capability_payload(publication_capability)
        ),
        "files": [
            {
                "filename": item.filename,
                "rows": item.rows,
                "columns": item.columns,
                "min_key": item.min_key,
                "max_key": item.max_key,
                "sha256": item.sha256,
                "business_hash": item.business_hash,
            }
            for item in files
        ],
        "cutoffs": [
            {
                "feature_date": item.feature_date,
                "daily_cutoff_key": item.daily_cutoff_key,
                "weekly_cutoff_key": item.weekly_cutoff_key,
                "monthly_cutoff_key": item.monthly_cutoff_key,
            }
            for item in cutoffs
        ],
    }
    stable_identity_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return StableDataBridgeCurrentAuthority(
        authority_schema_version=AUTHORITY_SCHEMA_VERSION,
        generation_id=generation_id,
        refresh_date=refresh_date,
        schema_version=current.dataset.schema_version,
        business_digest=current.dataset.business_digest,
        publication_capability=publication_capability,
        files=files,
        cutoffs=cutoffs,
        stable_identity_sha256=stable_identity_sha256,
    )


def _stable_publication_capability(
    payload: object,
) -> StablePublicationCapability | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise DataBridgeCurrentInvalidError(
            "DataBridge publication capability is invalid"
        )
    epoch = payload.get("daily_coordinator_epoch")
    if not isinstance(epoch, Mapping):
        raise DataBridgeCurrentInvalidError(
            "DataBridge publication epoch is invalid"
        )
    try:
        return StablePublicationCapability(
            occurrence_id=int(payload["occurrence_id"]),
            business_date=date.fromisoformat(
                str(payload["business_date"])
            ).isoformat(),
            epoch=int(epoch["epoch"]),
            mode=str(epoch["mode"]),
            record_sha256=str(epoch["record_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge publication capability is invalid"
        ) from exc


def _required_text(
    state: Mapping[str, object],
    field: str,
) -> str:
    value = state.get(field)
    if not isinstance(value, str) or not value:
        raise DataBridgeCurrentInvalidError(
            f"DataBridge current {field} is invalid"
        )
    return value


def _required_date(
    state: Mapping[str, object],
    field: str,
) -> str:
    try:
        return date.fromisoformat(
            _required_text(state, field)
        ).isoformat()
    except ValueError as exc:
        raise DataBridgeCurrentInvalidError(
            f"DataBridge current {field} is invalid"
        ) from exc


def _capability_payload(
    capability: StablePublicationCapability | None,
) -> Mapping[str, object] | None:
    if capability is None:
        return None
    return {
        "occurrence_id": capability.occurrence_id,
        "business_date": capability.business_date,
        "daily_coordinator_epoch": {
            "epoch": capability.epoch,
            "mode": capability.mode,
            "record_sha256": capability.record_sha256,
        },
    }
