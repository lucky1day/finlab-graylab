from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from shared.data_bridge.validation import (
    DataBridgeValidationError,
    LEGACY_FOUR_FILENAMES,
    ValidatedDataBridgeDataset,
    read_dataset_directory,
    validate_dataset,
)
from shared.runtime_paths import resolve_runtime_state_path
from shared.data_bridge.errors import (
    DataBridgeRefreshError,
    DataBridgeNonRetryableRefreshError,
    DataBridgeContinuityIdentityError,
    DataBridgeCurrentReadError,
    DataBridgeCurrentMissingError,
    DataBridgeCurrentInvalidError,
    DataBridgeStoreLockError,
    DataBridgeRecoveryIdentityError,
)
from shared.data_bridge.filesystem import (
    _fsync_directory,
    _fsync_regular_file,
    _require_private_directory,
    _open_strict_read_lock,
    _read_strict_current_state,
    ensure_private_directory,
    _validate_publish_candidate,
    _copy_regular_file_no_follow,
)
from shared.data_bridge.publication_manifest import (
    CURRENT_PUBLICATION_MANIFEST,
    CURRENT_PUBLICATION_MANIFEST_VERSION,
    CURRENT_PUBLICATION_MANIFEST_FIELDS,
    CURRENT_PUBLICATION_MANIFEST_FIELDS_BY_VERSION,
    LOCAL_MYSQL_SOURCE_MODE,
    LOCAL_MYSQL_PROVENANCE_VERSION,
    _publication_identity_from_state,
    require_local_mysql_source_provenance,
    _require_provenance_feature_date,
    _normalize_source_provenance,
    _canonical_date_value,
    _canonical_aware_timestamp_value,
    _canonical_sha256_value,
    data_bridge_publication_identity_sha256,
    _canonical_publication_manifest_bytes,
    _write_publication_manifest,
    _read_publication_manifest,
    _validate_identity_against_dataset,
    _directory_publication_identity,
)


_logger = logging.getLogger(__name__)

__all__ = [
    "DataBridgeRefreshError",
    "DataBridgeNonRetryableRefreshError",
    "DataBridgeContinuityIdentityError",
    "DataBridgeCurrentReadError",
    "DataBridgeCurrentMissingError",
    "DataBridgeCurrentInvalidError",
    "DataBridgeStoreLockError",
    "DataBridgeRecoveryIdentityError",
    "CURRENT_PUBLICATION_MANIFEST",
    "CURRENT_PUBLICATION_MANIFEST_VERSION",
    "CURRENT_PUBLICATION_MANIFEST_FIELDS",
    "CURRENT_PUBLICATION_MANIFEST_FIELDS_BY_VERSION",
    "LOCAL_MYSQL_SOURCE_MODE",
    "LOCAL_MYSQL_PROVENANCE_VERSION",
    "FAILED_ATTEMPT_ERROR_CATEGORIES",
    "DownloadRound",
    "DataBridgeRoundSource",
    "DataBridgeRefreshConfig",
    "RefreshResult",
    "CurrentDataset",
    "DataBridgeContinuityAuthority",
    "data_bridge_continuity_authority_sha256",
    "select_stable_round",
    "run_full_refresh",
    "check_current_dataset",
    "require_local_mysql_source_provenance",
    "data_bridge_publication_identity_sha256",
    "DataBridgeStore",
    "ensure_private_directory",
    "_canonical_aware_timestamp_value",
    "_canonical_sha256_value",
    "_canonical_publication_manifest_bytes",
]


FAILED_ATTEMPT_ERROR_CATEGORIES = frozenset(
    {
        "refresh_failed",
        "source_io_failed",
        "validation_failed",
    }
)


@dataclass(frozen=True)
class DownloadRound:
    round_id: str
    directory: Path
    dataset: ValidatedDataBridgeDataset | None
    digest: str
    source_mode: str = "full_export"
    source_provenance: Mapping[str, object] | None = None


class DataBridgeRoundSource(Protocol):
    """为一次刷新构造标准五文件候选目录的可替换数据源。"""

    def build(
        self,
        round_id: str,
        *,
        end_date: str,
        expected_daily_date: str,
        previous_keys: Mapping[str, set[str] | frozenset[str]] | None,
        continuity_cutoffs: Mapping[str, object] | None = None,
    ) -> DownloadRound:
        """在一次稳定性 round 中构造并验证候选数据集。"""


@dataclass(frozen=True)
class DataBridgeRefreshConfig:
    data_root: Path
    runtime_root: Path
    schema_path: Path
    daily_start_date: str = "2010-01-01"
    max_rounds: int = 3
    round_timeout_sec: int = 900
    refresh_start: str = "06:30"
    refresh_deadline: str = "07:00"

    @classmethod
    def from_env(
        cls,
        *,
        refresh_start: str | None = None,
        refresh_deadline: str | None = None,
    ) -> "DataBridgeRefreshConfig":
        """读取主机配置，并允许调用方显式指定本次刷新窗口。"""
        project_root = Path(__file__).resolve().parents[2]
        config = cls(
            data_root=resolve_runtime_state_path(
                relative_path="data-bridge/data",
                development_default=(
                    project_root / "data" / "data_bridge"
                ),
                override_env="DATABRIDGE_DATA_ROOT",
            ),
            runtime_root=resolve_runtime_state_path(
                relative_path="data-bridge/refresh",
                development_default=(
                    project_root
                    / "backtest_artifacts"
                    / "data_bridge_refresh"
                ),
                override_env="DATABRIDGE_RUNTIME_ROOT",
            ),
            schema_path=project_root / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json",
            refresh_start=(
                str(refresh_start)
                if refresh_start is not None
                else os.getenv("DATABRIDGE_REFRESH_START", "06:30")
            ),
            refresh_deadline=(
                str(refresh_deadline)
                if refresh_deadline is not None
                else os.getenv("DATABRIDGE_REFRESH_DEADLINE", "07:00")
            ),
        )
        config._parse_clock(config.refresh_start, "DATABRIDGE_REFRESH_START")
        config._parse_clock(config.refresh_deadline, "DATABRIDGE_REFRESH_DEADLINE")
        return config

    def deadline_at(self, refresh_date: str) -> datetime:
        clock = self._parse_clock(self.refresh_deadline, "DATABRIDGE_REFRESH_DEADLINE")
        return datetime.combine(date.fromisoformat(refresh_date), clock, tzinfo=ZoneInfo("Asia/Shanghai"))

    @staticmethod
    def _parse_clock(value: str, name: str):
        try:
            return datetime.strptime(value, "%H:%M").time()
        except ValueError as exc:
            raise ValueError(f"{name} must use HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class RefreshResult:
    state: Mapping[str, object]
    dataset: ValidatedDataBridgeDataset
    published: bool
    rounds_completed: int
    duration_sec: float


@dataclass(frozen=True)
class CurrentDataset:
    state: Mapping[str, object]
    dataset: ValidatedDataBridgeDataset
    publication_manifest: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DataBridgeContinuityAuthority:
    """绑定一次 refresh 前已校验 current 与其有效连续性截止键。"""

    generation_id: str
    business_digest: str
    publication_identity_sha256: str
    stable_identity_sha256: str
    daily_cutoff_key: str
    weekly_cutoff_key: str
    monthly_cutoff_key: str
    required_weekly_key: str | None = None
    required_monthly_key: str | None = None

    @property
    def continuity_cutoffs(self) -> Mapping[str, str]:
        return {
            "daily_output.csv": self.daily_cutoff_key,
            "weekly_output.csv": self.weekly_cutoff_key,
            "monthly_output.csv": self.monthly_cutoff_key,
            "api_wind_date.csv": self.daily_cutoff_key,
        }


def data_bridge_continuity_authority_sha256(
    *,
    generation_id: str,
    business_digest: str,
    publication_identity_sha256: str,
    daily_cutoff_key: str,
    weekly_cutoff_key: str,
    monthly_cutoff_key: str,
    required_weekly_key: str | None = None,
    required_monthly_key: str | None = None,
) -> str:
    """绑定 exact current identity 与全部三频 cutoff 的规范摘要。"""
    _validate_optional_period_key(
        required_weekly_key,
        label="DataBridge required_weekly_key",
    )
    _validate_optional_period_key(
        required_monthly_key,
        label="DataBridge required_monthly_key",
    )
    payload = {
        "authority_schema_version": (
            "data-bridge-continuity-authority-v1"
        ),
        "generation_id": generation_id,
        "business_digest": business_digest,
        "publication_identity_sha256": (
            publication_identity_sha256
        ),
        "continuity_cutoffs": {
            "daily_output.csv": daily_cutoff_key,
            "weekly_output.csv": weekly_cutoff_key,
            "monthly_output.csv": monthly_cutoff_key,
        },
        "frozen_exact_period_keys": {
            "weekly_output.csv": required_weekly_key,
            "monthly_output.csv": required_monthly_key,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_optional_period_key(
    value: object,
    *,
    label: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError(f"{label} must be a six-digit platform key or None")


def select_stable_round(rounds: Iterator[DownloadRound], *, max_rounds: int = 3) -> DownloadRound:
    if max_rounds < 2:
        raise ValueError("max_rounds must be at least 2")
    previous_identity: tuple[str, str, str | None] | None = None
    for _ in range(max_rounds):
        try:
            current = next(rounds)
        except StopIteration as exc:
            raise DataBridgeRefreshError("not enough download rounds to establish stability") from exc
        current_identity = _round_stability_identity(current)
        if (
            previous_identity is not None
            and previous_identity == current_identity
        ):
            return current
        previous_identity = current_identity
    raise DataBridgeRefreshError(
        f"DataBridge did not produce matching consecutive rounds within {max_rounds} rounds"
    )


def _round_stability_identity(
    round_result: DownloadRound,
) -> tuple[str, str, str | None]:
    """本机 MySQL round 还必须绑定同一源水位，避免静默漂移。"""
    if round_result.source_mode != LOCAL_MYSQL_SOURCE_MODE:
        return (round_result.digest, round_result.source_mode, None)
    provenance = _normalize_source_provenance(
        round_result.source_provenance
    )
    return (
        round_result.digest,
        round_result.source_mode,
        str(provenance["source_commit_token"]),
    )


def run_full_refresh(
    *,
    config: DataBridgeRefreshConfig,
    expected_daily_date: str,
    refresh_date: str,
    publish: bool,
    deadline_at: datetime | None = None,
    continuity_authority: object | None = None,
    round_builder: DataBridgeRoundSource | None = None,
    require_launchd_round_builder: bool = False,
) -> RefreshResult:
    if require_launchd_round_builder:
        _require_launchd_round_builder(round_builder)
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    with store.lock(exclusive=True, blocking=False):
        refresh_started_at = _shanghai_now()
        started = time.monotonic()
        built_directories: list[Path] = []
        try:
            store.recover(schema_path=config.schema_path)
            previous_current = _load_previous_current_locked(
                store,
                schema_path=config.schema_path,
            )
            continuity_cutoffs = _validate_continuity_authority(
                previous_current,
                continuity_authority=continuity_authority,
            )
            _ensure_before_deadline(deadline_at)
            if round_builder is None:
                raise DataBridgeRefreshError(
                    "DataBridge refresh requires a round source"
                )
            builder = round_builder
            previous_keys = (
                {
                    filename: previous_current.dataset.files[filename].keys
                    for filename in LEGACY_FOUR_FILENAMES
                    if filename in previous_current.dataset.files
                }
                if previous_current is not None
                else None
            )

            def rounds() -> Iterator[DownloadRound]:
                for index in range(config.max_rounds):
                    _ensure_before_deadline(deadline_at)
                    item = builder.build(
                        f"round-{index + 1}",
                        end_date=expected_daily_date,
                        expected_daily_date=expected_daily_date,
                        previous_keys=previous_keys,
                        continuity_cutoffs=continuity_cutoffs,
                    )
                    built_directories.append(item.directory)
                    _ensure_before_deadline(deadline_at)
                    yield item
                    item = None

            selected = select_stable_round(
                rounds(),
                max_rounds=config.max_rounds,
            )
            assert selected.dataset is not None
            _assert_factor_catalog_continuity(
                previous_current,
                selected.dataset,
            )
            _assert_frozen_exact_period_keys(
                selected.dataset,
                continuity_authority=continuity_authority,
            )
            for directory in built_directories:
                if (
                    directory != selected.directory
                    and directory.exists()
                ):
                    shutil.rmtree(directory)
            state = _build_state(
                selected.dataset,
                refresh_date,
                len(built_directories),
                refresh_started_at=refresh_started_at,
                duration_sec=time.monotonic() - started,
                source_mode=selected.source_mode,
                source_provenance=selected.source_provenance,
                expected_daily_date=expected_daily_date,
            )
            if publish:
                _ensure_before_deadline(deadline_at)
                state = store.publish(
                    selected.directory,
                    state,
                    deadline_at=deadline_at,
                )
            return RefreshResult(
                state=state,
                dataset=selected.dataset,
                published=publish,
                rounds_completed=len(built_directories),
                duration_sec=time.monotonic() - started,
            )
        except BaseException as exc:
            if publish:
                try:
                    store.record_failed_attempt(
                        refresh_date=refresh_date,
                        error=_failed_attempt_error_category(exc),
                        duration_sec=time.monotonic() - started,
                    )
                except Exception:
                    pass
            raise
        finally:
            staging_root = config.runtime_root / "staging"
            if staging_root.exists():
                shutil.rmtree(staging_root)


def _ensure_before_deadline(deadline_at: datetime | None) -> None:
    if deadline_at is None:
        return
    deadline = deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if datetime.now(ZoneInfo("Asia/Shanghai")) >= deadline:
        raise DataBridgeRefreshError(f"DataBridge refresh deadline has passed: {deadline.isoformat()}")


def check_current_dataset(
    config: DataBridgeRefreshConfig,
    *,
    required_refresh_date: str | None = None,
    expected_daily_date: str | None = None,
    expected_generation_id: str | None = None,
    expected_business_digest: str | None = None,
    strict_read_only: bool = False,
    require_source_provenance: bool = False,
) -> CurrentDataset:
    """在共享锁内验证 current 文件、状态及调用方拥有的发布身份。"""
    if require_source_provenance:
        if expected_daily_date is None:
            raise DataBridgeRefreshError(
                "DataBridge local source provenance requires "
                "expected_daily_date"
            )
        expected_daily_date = _canonical_date_value(
            expected_daily_date,
            label="DataBridge expected_daily_date",
        )
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    with store.current_read(
        strict_read_only=strict_read_only,
    ) as state:
        current = _validate_current_dataset_locked(
            store,
            state=state,
            schema_path=config.schema_path,
            expected_daily_date=expected_daily_date,
        )
        dataset = current.dataset
        if require_source_provenance:
            require_local_mysql_source_provenance(
                current.state,
                expected_daily_date=expected_daily_date,
            )
        if (
            expected_generation_id is not None
            and state.get("generation_id") != expected_generation_id
        ):
            raise DataBridgeRefreshError(
                "DataBridge current generation_id was replaced after refresh: "
                f"expected {expected_generation_id}, "
                f"got {state.get('generation_id')}"
            )
        if (
            expected_business_digest is not None
            and dataset.business_digest != expected_business_digest
        ):
            raise DataBridgeRefreshError(
                "DataBridge current business_digest was replaced after refresh: "
                f"expected {expected_business_digest}, "
                f"got {dataset.business_digest}"
            )
        if (
            required_refresh_date is not None
            and state.get("refresh_date") != required_refresh_date
        ):
            raise DataBridgeRefreshError(
                f"DataBridge refresh_date must be {required_refresh_date}, "
                f"got {state.get('refresh_date')}"
            )
        return current


def _load_previous_current_locked(
    store: "DataBridgeStore",
    schema_path: Path,
) -> CurrentDataset | None:
    if not store.current_dir.is_dir():
        if store.state_path.exists():
            try:
                state = json.loads(
                    store.state_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise DataBridgeRefreshError(
                    "DataBridge current is missing while state is invalid"
                ) from exc
            if _is_no_current_failure_audit_state(state):
                return None
            raise DataBridgeRefreshError(
                "DataBridge current is missing while state still exists"
            )
        return None
    with store.current_read() as state:
        return _validate_current_dataset_locked(
            store,
            state=state,
            schema_path=schema_path,
        )


def _validate_current_dataset_locked(
    store: "DataBridgeStore",
    *,
    state: Mapping[str, object],
    schema_path: Path,
    expected_daily_date: str | None = None,
) -> CurrentDataset:
    marker = _read_publication_manifest(store.current_dir)
    state_identity = _publication_identity_from_state(state)
    if marker != state_identity:
        raise DataBridgeRefreshError(
            "DataBridge current publication manifest identity does not "
            "match state"
        )
    dataset = validate_dataset(
        read_dataset_directory(
            store.current_dir,
            allowed_sidecar_filenames=frozenset(
                {CURRENT_PUBLICATION_MANIFEST}
            ),
        ),
        schema_path=schema_path,
        expected_daily_date=expected_daily_date,
    )
    if state.get("schema_version") != dataset.schema_version:
        raise DataBridgeRefreshError(
            "DataBridge state schema_version does not match current files"
        )
    if state.get("business_digest") != dataset.business_digest:
        raise DataBridgeRefreshError(
            "DataBridge state digest does not match current files"
        )
    _validate_identity_against_dataset(
        marker,
        dataset=dataset,
    )
    state_files = state.get("files")
    if not isinstance(state_files, dict):
        raise DataBridgeRefreshError(
            "DataBridge state files profile is missing"
        )
    for filename, profile in dataset.files.items():
        item = state_files.get(filename)
        if (
            not isinstance(item, dict)
            or item.get("sha256") != profile.sha256
        ):
            raise DataBridgeRefreshError(
                f"DataBridge state hash mismatch for {filename}"
            )
    return CurrentDataset(
        state=state,
        dataset=dataset,
        publication_manifest=marker,
    )


def _validate_continuity_authority(
    current: CurrentDataset | None,
    *,
    continuity_authority: object | None,
) -> Mapping[str, object] | None:
    if current is None:
        if continuity_authority is not None:
            raise DataBridgeContinuityIdentityError(
                "DataBridge continuity authority current drift: "
                "expected current is missing"
            )
        return None
    if continuity_authority is None:
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority is missing for existing current"
        )
    if not isinstance(
        continuity_authority,
        DataBridgeContinuityAuthority,
    ):
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority is invalid"
        )
    expected_generation_id = getattr(
        continuity_authority,
        "generation_id",
        None,
    )
    expected_business_digest = getattr(
        continuity_authority,
        "business_digest",
        None,
    )
    expected_publication_identity = getattr(
        continuity_authority,
        "publication_identity_sha256",
        None,
    )
    stable_identity = getattr(
        continuity_authority,
        "stable_identity_sha256",
        None,
    )
    cutoffs = getattr(
        continuity_authority,
        "continuity_cutoffs",
        None,
    )
    required_weekly_key = getattr(
        continuity_authority,
        "required_weekly_key",
        None,
    )
    required_monthly_key = getattr(
        continuity_authority,
        "required_monthly_key",
        None,
    )
    if (
        not isinstance(expected_generation_id, str)
        or not expected_generation_id
        or not isinstance(expected_business_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_business_digest)
        is None
        or not isinstance(expected_publication_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_publication_identity)
        is None
        or not isinstance(stable_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", stable_identity) is None
        or not isinstance(cutoffs, Mapping)
    ):
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority is invalid"
        )
    try:
        _validate_optional_period_key(
            required_weekly_key,
            label="DataBridge continuity authority required_weekly_key",
        )
        _validate_optional_period_key(
            required_monthly_key,
            label="DataBridge continuity authority required_monthly_key",
        )
    except ValueError as exc:
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority is invalid"
        ) from exc
    recalculated_stable_identity = (
        data_bridge_continuity_authority_sha256(
            generation_id=expected_generation_id,
            business_digest=expected_business_digest,
            publication_identity_sha256=(
                expected_publication_identity
            ),
            daily_cutoff_key=(
                continuity_authority.daily_cutoff_key
            ),
            weekly_cutoff_key=(
                continuity_authority.weekly_cutoff_key
            ),
            monthly_cutoff_key=(
                continuity_authority.monthly_cutoff_key
            ),
            required_weekly_key=required_weekly_key,
            required_monthly_key=required_monthly_key,
        )
    )
    if stable_identity != recalculated_stable_identity:
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority stable digest mismatch"
        )
    actual_publication_identity = (
        data_bridge_publication_identity_sha256(current.state)
    )
    if (
        current.state.get("generation_id") != expected_generation_id
        or current.dataset.business_digest
        != expected_business_digest
        or actual_publication_identity
        != expected_publication_identity
    ):
        raise DataBridgeContinuityIdentityError(
            "DataBridge continuity authority current identity drift"
        )
    return {
        filename: cutoffs[filename]
        for filename in LEGACY_FOUR_FILENAMES
    }


def _assert_frozen_exact_period_keys(
    dataset: ValidatedDataBridgeDataset,
    *,
    continuity_authority: DataBridgeContinuityAuthority | None,
) -> None:
    """拒绝在有效 fallback cutoff 下丢失同一 authority 冻结的 exact period key。"""
    if continuity_authority is None:
        return
    for filename, required_key in (
        (
            "weekly_output.csv",
            continuity_authority.required_weekly_key,
        ),
        (
            "monthly_output.csv",
            continuity_authority.required_monthly_key,
        ),
    ):
        if (
            required_key is not None
            and required_key not in dataset.files[filename].keys
        ):
            raise DataBridgeRefreshError(
                "DataBridge selected candidate is missing frozen exact "
                f"{filename} key {required_key}"
            )


def _assert_factor_catalog_continuity(
    previous: CurrentDataset | None,
    candidate: ValidatedDataBridgeDataset,
) -> None:
    """已发布版本成员封版。"""
    if previous is None:
        return
    previous_catalog = previous.dataset.frames["factor_catalog.csv"]
    candidate_catalog = candidate.frames.get("factor_catalog.csv")
    if candidate_catalog is None:
        raise DataBridgeRefreshError(
            "DataBridge candidate factor catalog is missing"
        )
    previous_versions = dict(
        previous_catalog[["indicators_code", "factor_version"]].itertuples(
            index=False,
            name=None,
        )
    )
    candidate_versions = dict(
        candidate_catalog[["indicators_code", "factor_version"]].itertuples(
            index=False,
            name=None,
        )
    )
    changed = sorted(
        code
        for code, version in previous_versions.items()
        if candidate_versions.get(code) != version
    )
    if changed:
        raise DataBridgeRefreshError(
            "published factor_version assignments changed: "
            f"{changed[:10]}"
        )
    for version in sorted(set(previous_versions.values())):
        previous_codes = {
            code
            for code, assigned in previous_versions.items()
            if assigned == version
        }
        candidate_codes = {
            code
            for code, assigned in candidate_versions.items()
            if assigned == version
        }
        if previous_codes != candidate_codes:
            raise DataBridgeRefreshError(
                "published factor version membership changed: "
                f"{version}"
            )


def _build_state(
    dataset: ValidatedDataBridgeDataset,
    refresh_date: str,
    stability_rounds: int,
    *,
    refresh_started_at: datetime,
    duration_sec: float,
    source_mode: str = "full_export",
    source_provenance: Mapping[str, object] | None = None,
    expected_daily_date: str | None = None,
) -> dict[str, object]:
    now = _shanghai_now()
    if (
        refresh_started_at.tzinfo is None
        or refresh_started_at.utcoffset() is None
    ):
        raise ValueError("refresh_started_at must be timezone-aware")
    refresh_started_at = refresh_started_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    if now < refresh_started_at:
        raise ValueError("DataBridge refresh completion precedes start")
    if not isinstance(source_mode, str) or not source_mode:
        raise ValueError("DataBridge source_mode must be a non-empty string")
    normalized_source_provenance: dict[str, object] | None = None
    if source_mode == LOCAL_MYSQL_SOURCE_MODE:
        normalized_source_provenance = _normalize_source_provenance(
            source_provenance
        )
        _require_provenance_feature_date(
            normalized_source_provenance,
            expected_daily_date=expected_daily_date,
        )
    elif source_provenance is not None:
        raise ValueError(
            "DataBridge non-local source must not carry local provenance"
        )
    generation_id = f"full-{refresh_date.replace('-', '')}-{now.strftime('%H%M%S')}-{dataset.business_digest[:12]}"
    return {
        "publication_manifest_version": (
            CURRENT_PUBLICATION_MANIFEST_VERSION
        ),
        "schema_version": dataset.schema_version,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "refresh_started_at": refresh_started_at.isoformat(
            timespec="seconds"
        ),
        "refreshed_at": now.isoformat(timespec="seconds"),
        "published_at": None,
        "source_mode": source_mode,
        "source_provenance": copy.deepcopy(
            normalized_source_provenance
        ),
        "stability_rounds": stability_rounds,
        "business_digest": dataset.business_digest,
        "last_attempt": {
            "status": "success",
            "refresh_date": refresh_date,
            "started_at": refresh_started_at.isoformat(
                timespec="seconds"
            ),
            "finished_at": now.isoformat(timespec="seconds"),
            "duration_sec": round(duration_sec, 3),
            "error": None,
        },
        "files": {
            filename: {
                "sha256": profile.sha256,
                "business_hash": profile.business_hash,
                "rows": profile.rows,
                "columns": profile.columns,
                "min_key": profile.min_key,
                "max_key": profile.max_key,
            }
            for filename, profile in dataset.files.items()
        },
    }


def _require_launchd_round_builder(
    round_builder: DataBridgeRoundSource | None,
) -> None:
    """无 legacy fence 的本机路径只接受受控 MySQL exporter。"""
    from shared.data_bridge.mysql_exporter import (
        MySqlDataBridgeRoundBuilder,
    )

    if type(round_builder) is not MySqlDataBridgeRoundBuilder:
        raise DataBridgeRefreshError(
            "launchd-only DataBridge refresh requires "
            "MySqlDataBridgeRoundBuilder"
        )


def _failed_attempt_error_category(exc: BaseException) -> str:
    """将运行异常压缩为可审计而不含敏感细节的固定类别。"""
    if isinstance(exc, (OSError, SQLAlchemyError)):
        return "source_io_failed"
    if isinstance(exc, DataBridgeValidationError):
        return "validation_failed"
    return "refresh_failed"


def _safe_failed_attempt_error(error: object) -> str:
    if (
        isinstance(error, str)
        and error in FAILED_ATTEMPT_ERROR_CATEGORIES
    ):
        return error
    return "refresh_failed"


def _is_no_current_failure_audit_state(state: object) -> bool:
    """仅把 record_failed_attempt 写出的空基线审计 state 识别为无 current。"""
    if not isinstance(state, Mapping) or set(state) != {"last_attempt"}:
        return False
    attempt = state.get("last_attempt")
    if not isinstance(attempt, Mapping):
        return False
    if set(attempt) != {
        "status",
        "refresh_date",
        "finished_at",
        "duration_sec",
        "error",
    }:
        return False
    duration = attempt.get("duration_sec")
    return (
        attempt.get("status") == "failed"
        and isinstance(attempt.get("refresh_date"), str)
        and bool(attempt["refresh_date"])
        and isinstance(attempt.get("finished_at"), str)
        and bool(attempt["finished_at"])
        and not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and duration >= 0
        and isinstance(attempt.get("error"), str)
        and attempt["error"] in FAILED_ATTEMPT_ERROR_CATEGORIES
    )


class DataBridgeStore:
    def __init__(self, *, data_root: str | Path, runtime_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.runtime_root = Path(runtime_root)
        self.current_dir = self.data_root / "current"
        self.previous_dir = self.runtime_root / "previous"
        self.state_path = self.runtime_root / "state.json"
        self.lock_path = self.runtime_root / "refresh.lock"
        self._thread_lock_state = threading.local()

    @contextmanager
    def current_read(
        self,
        *,
        strict_read_only: bool = False,
    ) -> Iterator[Mapping[str, object]]:
        """在一个 shared lock 范围内读取 state 并映射 current 错误。"""
        with self.lock(
            exclusive=False,
            strict_read_only=strict_read_only,
        ):
            try:
                if strict_read_only:
                    state = _read_strict_current_state(self.state_path)
                else:
                    if not self.state_path.is_file():
                        raise DataBridgeRefreshError(
                            "DataBridge refresh state does not exist"
                        )
                    try:
                        state = json.loads(
                            self.state_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as exc:
                        raise DataBridgeRefreshError(
                            "DataBridge refresh state is invalid"
                        ) from exc
                if (
                    strict_read_only
                    and not self.current_dir.is_dir()
                    and _is_no_current_failure_audit_state(state)
                ):
                    raise DataBridgeCurrentMissingError(
                        "DataBridge current has no published generation"
                    )
                yield state
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
                if strict_read_only:
                    raise DataBridgeCurrentInvalidError(
                        "DataBridge current is invalid"
                    ) from exc
                raise

    @contextmanager
    def lock(
        self,
        *,
        exclusive: bool,
        blocking: bool = True,
        strict_read_only: bool = False,
    ) -> Iterator[None]:
        if strict_read_only and exclusive:
            raise ValueError(
                "strict DataBridge read lock cannot be exclusive"
            )
        depth = int(getattr(self._thread_lock_state, "depth", 0))
        held_exclusive = bool(
            getattr(self._thread_lock_state, "exclusive", False)
        )
        if depth:
            if exclusive and not held_exclusive:
                raise DataBridgeStoreLockError(
                    "cannot upgrade an active shared DataBridge lock"
                )
            self._thread_lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._thread_lock_state.depth -= 1
            return

        if strict_read_only:
            _require_private_directory(
                self.data_root,
                label="DataBridge data root",
                error_type=DataBridgeCurrentInvalidError,
                missing_error_type=DataBridgeCurrentMissingError,
            )
            _require_private_directory(
                self.runtime_root,
                label="DataBridge runtime root",
                error_type=DataBridgeCurrentInvalidError,
                missing_error_type=DataBridgeCurrentMissingError,
            )
            handle = _open_strict_read_lock(self.lock_path)
        else:
            ensure_private_directory(
                self.data_root,
                label="DataBridge data root",
            )
            ensure_private_directory(
                self.runtime_root,
                label="DataBridge runtime root",
            )
            handle = self.lock_path.open("a+b")
        with handle:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise DataBridgeStoreLockError(
                    "DataBridge refresh is busy in another process"
                ) from exc
            self._thread_lock_state.depth = 1
            self._thread_lock_state.exclusive = exclusive
            try:
                yield
            finally:
                self._thread_lock_state.depth = 0
                self._thread_lock_state.exclusive = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def publish(
        self,
        candidate_dir: str | Path,
        state: dict[str, object],
        *,
        deadline_at: datetime | None = None,
        fail_after_backup: bool = False,
    ) -> dict[str, object]:
        candidate = Path(candidate_dir)
        _validate_publish_candidate(candidate)
        with self.lock(exclusive=True):
            _validate_publish_candidate(candidate)
            if not self.current_dir.exists() and self.previous_dir.exists():
                raise DataBridgeRecoveryIdentityError(
                    "DataBridge previous publication requires verified recovery"
                )
            next_dir = Path(tempfile.mkdtemp(prefix=".current-next-", dir=self.data_root))
            shutil.rmtree(next_dir)
            shutil.copytree(
                candidate,
                next_dir,
                copy_function=_copy_regular_file_no_follow,
            )
            _validate_publish_candidate(candidate)
            published_state = copy.deepcopy(state)
            published_state.pop("publication_capability", None)
            published_state["publication_manifest_version"] = (
                CURRENT_PUBLICATION_MANIFEST_VERSION
            )
            published_state.setdefault("source_mode", "full_export")
            published_state.setdefault("source_provenance", None)
            _write_publication_manifest(
                next_dir,
                state=published_state,
            )
            for path in next_dir.iterdir():
                path.chmod(0o444)
                _fsync_regular_file(path)
            _fsync_directory(next_dir)
            had_current = self.current_dir.exists()
            previous_state = (
                self.state_path.read_bytes()
                if self.state_path.is_file()
                else None
            )
            current_backed_up = False
            candidate_activated = False
            state_replaced = False
            try:
                _ensure_before_deadline(deadline_at)
                if self.previous_dir.exists():
                    shutil.rmtree(self.previous_dir)
                if had_current:
                    os.replace(self.current_dir, self.previous_dir)
                    current_backed_up = True
                if fail_after_backup:
                    raise RuntimeError("injected publication failure")
                _ensure_before_deadline(deadline_at)
                os.replace(next_dir, self.current_dir)
                candidate_activated = True
                published_state["published_at"] = (
                    _shanghai_now().isoformat(
                        timespec="seconds"
                    )
                )
                state_replaced = True
                self._write_state_locked(published_state)
                _fsync_directory(self.data_root)
                _fsync_directory(self.runtime_root)
            except BaseException:
                if candidate_activated and self.current_dir.exists():
                    shutil.rmtree(self.current_dir)
                if current_backed_up and self.previous_dir.exists():
                    os.replace(self.previous_dir, self.current_dir)
                if next_dir.exists():
                    shutil.rmtree(next_dir)
                if state_replaced:
                    self._restore_state_locked(previous_state)
                _fsync_directory(self.data_root)
                _fsync_directory(self.runtime_root)
                raise
            # 新 current 和 state 已同步。删除旧备份失败不再回滚已提交发布。
            self._discard_previous_after_commit()
        return published_state

    def _discard_previous_after_commit(self) -> None:
        if not self.previous_dir.exists():
            return
        try:
            shutil.rmtree(self.previous_dir)
            _fsync_directory(self.runtime_root)
        except OSError as exc:
            _logger.warning(
                "DataBridge previous publication cleanup failed: path=%s errno=%s",
                self.previous_dir,
                exc.errno,
            )

    def recover(self, *, schema_path: str | Path) -> None:
        """按 state/current 完整发布身份恢复原子切换中断现场。"""
        with self.lock(exclusive=True):
            state: Mapping[str, object] | None = None
            state_identity: dict[str, object] | None = None
            if self.state_path.is_file():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    # 无 current 的失败审计 state 不带发布版本，不构成发布身份。
                    state_identity = (
                        None
                        if _is_no_current_failure_audit_state(state)
                        else _publication_identity_from_state(state)
                    )
                except (OSError, json.JSONDecodeError):
                    state_identity = None

            current_identity = _directory_publication_identity(
                self.current_dir,
                schema_path=Path(schema_path),
            )
            marker_path = (
                self.current_dir / CURRENT_PUBLICATION_MANIFEST
            )
            if (
                not self.previous_dir.exists()
                and self.current_dir.is_dir()
                and current_identity is None
                and state is not None
                and state_identity is not None
                and not os.path.lexists(marker_path)
            ):
                try:
                    legacy_dataset = validate_dataset(
                        read_dataset_directory(self.current_dir),
                        schema_path=Path(schema_path),
                    )
                except (OSError, ValueError):
                    legacy_dataset = None
                if legacy_dataset is not None:
                    _validate_identity_against_dataset(
                        state_identity,
                        dataset=legacy_dataset,
                    )
                    _write_publication_manifest(
                        self.current_dir,
                        state=state,
                    )
                    current_identity = (
                        _directory_publication_identity(
                            self.current_dir,
                            schema_path=Path(schema_path),
                        )
                    )
            if self.previous_dir.exists():
                if (
                    state_identity is not None
                    and current_identity == state_identity
                ):
                    self._discard_previous_after_commit()
                elif (
                    state_identity is not None
                    and _directory_publication_identity(
                        self.previous_dir,
                        schema_path=Path(schema_path),
                    ) == state_identity
                ):
                    if self.current_dir.exists():
                        shutil.rmtree(self.current_dir)
                    os.replace(self.previous_dir, self.current_dir)
                    _fsync_directory(self.data_root)
                    _fsync_directory(self.runtime_root)
                else:
                    raise DataBridgeRecoveryIdentityError(
                        "cannot recover DataBridge current/previous against published state"
                    )
            elif self.current_dir.exists() and (
                state_identity is None
                or current_identity != state_identity
            ):
                raise DataBridgeRecoveryIdentityError(
                    "DataBridge current publication identity drifted from "
                    "state"
                )

            staging = self.runtime_root / "staging"
            if staging.exists():
                shutil.rmtree(staging)

    def record_failed_attempt(
        self,
        *,
        refresh_date: str,
        error: str,
        duration_sec: float,
    ) -> None:
        """记录失败原因，但不替换最后成功的 current generation。"""
        now = _shanghai_now()
        with self.lock(exclusive=True):
            if self.state_path.is_file():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
            else:
                state = {}
            state["last_attempt"] = {
                "status": "failed",
                "refresh_date": refresh_date,
                "finished_at": now.isoformat(timespec="seconds"),
                "duration_sec": round(duration_sec, 3),
                "error": _safe_failed_attempt_error(error),
            }
            self._write_state_locked(state)

    def _write_state_locked(self, state: dict[str, object]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".state-", dir=self.runtime_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _restore_state_locked(self, previous: bytes | None) -> None:
        """发布后置失败时恢复 publication 前的 state 字节。"""
        if previous is None:
            self.state_path.unlink(missing_ok=True)
            _fsync_directory(self.runtime_root)
            return
        fd, temporary_name = tempfile.mkstemp(
            prefix=".state-restore-",
            dir=self.runtime_root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            _fsync_directory(self.runtime_root)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _shanghai_now() -> datetime:
    """返回 DataBridge 契约时区的当前时刻，测试可替换。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))
