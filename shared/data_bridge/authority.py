from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from shared.data_bridge.refresh import (
    LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION,
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
    DataBridgeCurrentReadError,
    DataBridgeContinuityAuthority,
    DataBridgeRefreshConfig,
    DataBridgeRefreshError,
    DataBridgeStore,
    check_current_dataset,
    data_bridge_continuity_authority_sha256,
    data_bridge_publication_identity_sha256,
)
from shared.data_bridge.validation import DataBridgeValidationError


AUTHORITY_SCHEMA_VERSION = "stable-databridge-current-authority-v2"
_GRAY_REPLAY_FILENAMES = frozenset(
    {
        "daily_output.csv",
        "weekly_output.csv",
        "monthly_output.csv",
    }
)
_GRAY_REPLAY_SOURCE_IDENTITY_FIELDS = frozenset(
    {
        "generation_id",
        "refresh_date",
        "schema_version",
        "business_digest",
        "stable_identity_sha256",
        "files",
    }
)
_GRAY_REPLAY_SOURCE_FILE_FIELDS = frozenset(
    {
        "filename",
        "rows",
        "columns",
        "min_key",
        "max_key",
        "sha256",
        "business_hash",
    }
)


DataBridgeCurrentAuthorityError = DataBridgeCurrentReadError


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
    source_weekly_cutoff_key: str | None = None
    source_monthly_cutoff_key: str | None = None


@dataclass(frozen=True, slots=True)
class StableDataBridgeCurrentAuthority:
    authority_schema_version: str
    generation_id: str
    refresh_date: str
    schema_version: str
    business_digest: str
    files: tuple[StableDataBridgeFileIdentity, ...]
    cutoffs: tuple[StableDataBridgeCutoff, ...]
    publication_identity_sha256: str
    stable_identity_sha256: str


def blackbox_gray_replay_source_identity(
    authority: StableDataBridgeCurrentAuthority,
) -> dict[str, Any]:
    """序列化 Blackbox gray replay 唯一允许的 current source identity。"""
    if not isinstance(authority, StableDataBridgeCurrentAuthority):
        raise ValueError(
            "gray replay authority must be StableDataBridgeCurrentAuthority"
        )
    if authority.authority_schema_version != AUTHORITY_SCHEMA_VERSION:
        raise ValueError("gray replay authority must use current authority v2")
    return _normalize_blackbox_gray_replay_source_identity(
        {
            "generation_id": authority.generation_id,
            "refresh_date": authority.refresh_date,
            "schema_version": authority.schema_version,
            "business_digest": authority.business_digest,
            "stable_identity_sha256": authority.stable_identity_sha256,
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
                for item in authority.files
            ],
        }
    )


def _normalize_blackbox_gray_replay_source_identity(
    source_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """供 authority serializer 与 replay session 共用的字段校验。"""
    if not isinstance(source_identity, Mapping):
        raise ValueError("gray replay source identity must be an object")
    raw = dict(source_identity)
    if set(raw) != _GRAY_REPLAY_SOURCE_IDENTITY_FIELDS:
        raise ValueError("gray replay source identity fields are invalid")
    generation_id = raw["generation_id"]
    schema_version = raw["schema_version"]
    if (
        not isinstance(generation_id, str)
        or not generation_id.strip()
        or not isinstance(schema_version, str)
        or not schema_version.strip()
        or not _is_sha256(raw["business_digest"])
        or not _is_sha256(raw["stable_identity_sha256"])
    ):
        raise ValueError("gray replay source identity is invalid")
    refresh_date = _canonical_gray_replay_key(
        raw["refresh_date"],
        filename="daily_output.csv",
        field="source_identity.refresh_date",
    )
    raw_files = raw["files"]
    if not isinstance(raw_files, list):
        raise ValueError("gray replay source identity files are invalid")
    normalized_files: list[dict[str, Any]] = []
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise ValueError("gray replay source identity files are invalid")
        file_identity = dict(item)
        if set(file_identity) != _GRAY_REPLAY_SOURCE_FILE_FIELDS:
            raise ValueError("gray replay source identity files are invalid")
        filename = file_identity["filename"]
        rows = file_identity["rows"]
        columns = file_identity["columns"]
        if (
            filename not in _GRAY_REPLAY_FILENAMES
            or isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows < 1
            or isinstance(columns, bool)
            or not isinstance(columns, int)
            or columns < 1
            or not _is_sha256(file_identity["sha256"])
            or not _is_sha256(file_identity["business_hash"])
        ):
            raise ValueError("gray replay source identity files are invalid")
        min_key = _canonical_gray_replay_key(
            file_identity["min_key"],
            filename=filename,
            field=f"source_identity.{filename}.min_key",
        )
        max_key = _canonical_gray_replay_key(
            file_identity["max_key"],
            filename=filename,
            field=f"source_identity.{filename}.max_key",
        )
        if min_key > max_key:
            raise ValueError("gray replay source identity files are invalid")
        normalized_files.append(
            {
                "filename": filename,
                "rows": rows,
                "columns": columns,
                "min_key": min_key,
                "max_key": max_key,
                "sha256": file_identity["sha256"],
                "business_hash": file_identity["business_hash"],
            }
        )
    if tuple(item["filename"] for item in normalized_files) != tuple(
        sorted(_GRAY_REPLAY_FILENAMES)
    ):
        raise ValueError("gray replay source identity files must be sorted")
    return {
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "schema_version": schema_version,
        "business_digest": raw["business_digest"],
        "stable_identity_sha256": raw["stable_identity_sha256"],
        "files": normalized_files,
    }


def _canonical_gray_replay_key(
    value: Any,
    *,
    filename: str,
    field: str,
) -> str:
    if filename == "daily_output.csv":
        if not isinstance(value, str):
            raise ValueError(f"{field} must use YYYY-MM-DD")
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError(f"{field} must use YYYY-MM-DD") from exc
        if normalized != value:
            raise ValueError(f"{field} must use YYYY-MM-DD")
        return normalized
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise ValueError(f"{field} must be a six-digit platform key")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def resolve_databridge_continuity_authority(
    config: DataBridgeRefreshConfig,
    *,
    feature_date: str,
    connection: Any,
    allow_legacy_v1_period_fallback: bool = False,
) -> DataBridgeContinuityAuthority | None:
    """从现有 current 和 caller 只读连接解析下一轮连续性截止键。

    首次发布没有 current 时不需要连续性比较；已经存在但无效的 current
    必须继续 fail-closed。
    """
    normalized_feature_date = date.fromisoformat(
        str(feature_date)[:10]
    ).isoformat()
    kwargs: dict[str, object] = {}
    if allow_legacy_v1_period_fallback:
        kwargs["allow_legacy_v1_period_fallback"] = True
    try:
        authority = resolve_stable_databridge_current_authority(
            config,
            feature_dates=(normalized_feature_date,),
            connection=connection,
            **kwargs,
        )
    except DataBridgeCurrentMissingError:
        return None
    if (
        len(authority.cutoffs) != 1
        or authority.cutoffs[0].feature_date
        != normalized_feature_date
    ):
        raise DataBridgeCurrentInvalidError(
            "DataBridge current continuity cutoff authority is incomplete"
        )
    cutoff = authority.cutoffs[0]
    required_weekly_key = _frozen_source_period_key(
        cutoff.source_weekly_cutoff_key,
        effective_key=cutoff.weekly_cutoff_key,
        label="weekly",
    )
    required_monthly_key = _frozen_source_period_key(
        cutoff.source_monthly_cutoff_key,
        effective_key=cutoff.monthly_cutoff_key,
        label="monthly",
    )
    stable_identity_sha256 = (
        data_bridge_continuity_authority_sha256(
            generation_id=authority.generation_id,
            business_digest=authority.business_digest,
            publication_identity_sha256=(
                authority.publication_identity_sha256
            ),
            daily_cutoff_key=cutoff.daily_cutoff_key,
            weekly_cutoff_key=cutoff.weekly_cutoff_key,
            monthly_cutoff_key=cutoff.monthly_cutoff_key,
            required_weekly_key=required_weekly_key,
            required_monthly_key=required_monthly_key,
        )
    )
    return DataBridgeContinuityAuthority(
        generation_id=authority.generation_id,
        business_digest=authority.business_digest,
        publication_identity_sha256=(
            authority.publication_identity_sha256
        ),
        stable_identity_sha256=stable_identity_sha256,
        daily_cutoff_key=cutoff.daily_cutoff_key,
        weekly_cutoff_key=cutoff.weekly_cutoff_key,
        monthly_cutoff_key=cutoff.monthly_cutoff_key,
        required_weekly_key=required_weekly_key,
        required_monthly_key=required_monthly_key,
    )


def _frozen_source_period_key(
    source_key: object,
    *,
    effective_key: str,
    label: str,
) -> str | None:
    """仅在 fallback 实际选用 predecessor 时冻结同一 snapshot 的 exact key。"""
    if source_key is None:
        return None
    if (
        not isinstance(source_key, str)
        or len(source_key) != 6
        or not source_key.isdigit()
    ):
        raise DataBridgeCurrentInvalidError(
            f"DataBridge current source {label} cutoff is invalid"
        )
    if source_key == effective_key:
        return None
    return source_key


def resolve_databridge_continuity_authority_from_engine(
    config: DataBridgeRefreshConfig,
    *,
    feature_date: str,
    engine: Any,
    allow_legacy_v1_period_fallback: bool = False,
) -> DataBridgeContinuityAuthority | None:
    """在单个 RR consistent snapshot 只读事务中解析 current authority。"""
    store = DataBridgeStore(
        data_root=config.data_root,
        runtime_root=config.runtime_root,
    )
    store.recover(schema_path=config.schema_path)
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        try:
            kwargs: dict[str, object] = {}
            if allow_legacy_v1_period_fallback:
                kwargs["allow_legacy_v1_period_fallback"] = True
            return resolve_databridge_continuity_authority(
                config,
                feature_date=feature_date,
                connection=connection,
                **kwargs,
            )
        finally:
            connection.rollback()


def resolve_stable_databridge_current_authority(
    config: DataBridgeRefreshConfig,
    *,
    feature_dates: Iterable[str],
    connection: Any,
    allow_legacy_v1_period_fallback: bool = False,
) -> StableDataBridgeCurrentAuthority:
    """校验 current，并用 caller Connection 冻结稳定发布与截止身份。"""
    from shared.input_artifacts import (
        _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys,
    )

    if connection is None:
        raise ValueError("connection is required")
    normalized_dates = tuple(sorted({
        date.fromisoformat(str(value)[:10]).isoformat()
        for value in feature_dates
    }))
    try:
        current = check_current_dataset(
            config,
            strict_read_only=True,
        )
    except (
        DataBridgeCurrentMissingError,
        DataBridgeCurrentInvalidError,
    ):
        raise
    except (
        DataBridgeRefreshError,
        DataBridgeValidationError,
        OSError,
        ValueError,
    ) as exc:
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
    publication_manifest = current.publication_manifest
    legacy_v1_period_fallback = (
        allow_legacy_v1_period_fallback
        and isinstance(publication_manifest, Mapping)
        and publication_manifest.get("manifest_version")
        == LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION
    )
    try:
        resolved = _resolve_blackbox_input_cutoffs_with_source_keys_bulk_from_keys(
            cutoff_keys,
            feature_dates=normalized_dates,
            connection=connection,
            schema_path=config.schema_path,
            allow_legacy_v1_period_fallback=(
                legacy_v1_period_fallback
            ),
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
            daily_cutoff_key=(
                resolved[feature_date].cutoff_keys.daily_cutoff_key
            ),
            weekly_cutoff_key=(
                resolved[feature_date].cutoff_keys.weekly_cutoff_key
            ),
            monthly_cutoff_key=(
                resolved[feature_date].cutoff_keys.monthly_cutoff_key
            ),
            source_weekly_cutoff_key=(
                resolved[feature_date].source_weekly_cutoff_key
            ),
            source_monthly_cutoff_key=(
                resolved[feature_date].source_monthly_cutoff_key
            ),
        )
        for feature_date in normalized_dates
    )
    publication_identity_sha256 = (
        data_bridge_publication_identity_sha256(current.state)
    )
    payload = {
        "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "schema_version": current.dataset.schema_version,
        "business_digest": current.dataset.business_digest,
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
                "source_weekly_cutoff_key": (
                    item.source_weekly_cutoff_key
                ),
                "source_monthly_cutoff_key": (
                    item.source_monthly_cutoff_key
                ),
            }
            for item in cutoffs
        ],
        "publication_identity_sha256": publication_identity_sha256,
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
        files=files,
        cutoffs=cutoffs,
        publication_identity_sha256=publication_identity_sha256,
        stable_identity_sha256=stable_identity_sha256,
    )


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
