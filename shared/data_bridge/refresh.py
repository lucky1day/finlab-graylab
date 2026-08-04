from __future__ import annotations

import csv
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from shared.data_bridge.validation import (
    DataBridgeValidationError,
    EXPECTED_FILENAMES,
    ValidatedDataBridgeDataset,
    read_dataset_directory,
    validate_dataset,
    validate_unique_csv_header,
    write_validated_dataset,
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
from shared.daily_coordinator_mode import (
    assert_daily_coordinator_epoch_payload_matches_current,
    require_current_daily_coordinator_identity,
)


class DataBridgeRefreshError(RuntimeError):
    """A full refresh could not produce a stable valid dataset."""


class DataBridgeCurrentReadError(DataBridgeRefreshError):
    """严格只读 current 检查失败。"""


class DataBridgeCurrentMissingError(DataBridgeCurrentReadError):
    """严格只读 current 所需的已发布状态缺失。"""


class DataBridgeCurrentInvalidError(DataBridgeCurrentReadError):
    """严格只读 current 的目录、状态或内容无效。"""


class DataBridgePublicationFenceError(DataBridgeRefreshError):
    """Occurrence-bound publication capability 已缺失或漂移。"""


CURRENT_PUBLICATION_MANIFEST = ".publication-manifest.json"
LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION = "data-bridge-current-v1"
CURRENT_PUBLICATION_MANIFEST_VERSION = "data-bridge-current-v2"
LEGACY_CURRENT_PUBLICATION_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "generation_id",
        "refresh_date",
        "schema_version",
        "business_digest",
        "files",
        "publication_capability",
    }
)
CURRENT_PUBLICATION_MANIFEST_FIELDS = frozenset(
    {
        *LEGACY_CURRENT_PUBLICATION_MANIFEST_FIELDS,
        "source_mode",
        "source_provenance",
    }
)
CURRENT_PUBLICATION_MANIFEST_FIELDS_BY_VERSION = {
    LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION: (
        LEGACY_CURRENT_PUBLICATION_MANIFEST_FIELDS
    ),
    CURRENT_PUBLICATION_MANIFEST_VERSION: (
        CURRENT_PUBLICATION_MANIFEST_FIELDS
    ),
}
LOCAL_MYSQL_SOURCE_MODE = "local_mysql"
LOCAL_MYSQL_PROVENANCE_VERSION = "data-bridge-local-mysql-v1"
FAILED_ATTEMPT_ERROR_CATEGORIES = frozenset(
    {
        "refresh_failed",
        "source_io_failed",
        "validation_failed",
    }
)


@dataclass(frozen=True)
class DailyCoordinatorPublicationCapability:
    """绑定单个 occurrence/date/exact epoch 的不可变发布能力。"""

    occurrence_id: int
    business_date: str
    epoch: int
    mode: str
    record_sha256: str
    occurrence_validator: (
        Callable[[], Mapping[str, object]] | None
    ) = field(default=None, repr=False, compare=False)

    @classmethod
    def from_occurrence(
        cls,
        *,
        occurrence_id: int,
        business_date: str,
        policy_json: Mapping[str, object],
        occurrence_validator: Callable[[], Mapping[str, object]],
    ) -> "DailyCoordinatorPublicationCapability":
        frozen = policy_json.get("daily_coordinator_epoch")
        if not isinstance(frozen, Mapping):
            raise DataBridgeRefreshError(
                "daily occurrence publication epoch is unavailable"
            )
        return cls(
            occurrence_id=int(occurrence_id),
            business_date=date.fromisoformat(
                business_date
            ).isoformat(),
            epoch=int(frozen.get("epoch", 0)),
            mode=str(frozen.get("mode", "")),
            record_sha256=str(frozen.get("record_sha256", "")),
            occurrence_validator=occurrence_validator,
        )

    def epoch_payload(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "mode": self.mode,
            "record_sha256": self.record_sha256,
        }


def _validate_publication_capability(
    capability: object,
) -> DailyCoordinatorPublicationCapability:
    if not isinstance(
        capability,
        DailyCoordinatorPublicationCapability,
    ):
        raise DataBridgePublicationFenceError(
            "DataBridge publication capability is missing"
        )
    if capability.occurrence_id <= 0:
        raise DataBridgePublicationFenceError(
            "DataBridge publication occurrence_id is invalid"
        )
    if not callable(capability.occurrence_validator):
        raise DataBridgePublicationFenceError(
            "DataBridge publication occurrence validator is missing"
        )
    try:
        normalized_date = date.fromisoformat(
            capability.business_date
        ).isoformat()
        occurrence = capability.occurrence_validator()
        if not isinstance(occurrence, Mapping):
            raise ValueError("occurrence validator returned no snapshot")
        snapshot_occurrence_id = int(
            occurrence.get("occurrence_id", 0)
        )
        snapshot_business_date = date.fromisoformat(
            str(occurrence.get("business_date", ""))
        ).isoformat()
        snapshot_epoch = occurrence.get("daily_coordinator_epoch")
        if not isinstance(snapshot_epoch, Mapping):
            raise ValueError("occurrence epoch is unavailable")
        normalized_snapshot_epoch = {
            "epoch": int(snapshot_epoch.get("epoch", 0)),
            "mode": str(snapshot_epoch.get("mode", "")),
            "record_sha256": str(
                snapshot_epoch.get("record_sha256", "")
            ),
        }
        if (
            snapshot_occurrence_id != capability.occurrence_id
            or snapshot_business_date != capability.business_date
            or normalized_snapshot_epoch != capability.epoch_payload()
        ):
            raise ValueError("occurrence publication identity drifted")
        assert_daily_coordinator_epoch_payload_matches_current(
            normalized_snapshot_epoch,
            label="DataBridge publication coordinator epoch",
        )
    except Exception as exc:
        raise DataBridgePublicationFenceError(
            "DataBridge publication occurrence snapshot or coordinator "
            "epoch drifted"
        ) from exc
    if normalized_date != capability.business_date:
        raise DataBridgePublicationFenceError(
            "DataBridge publication business_date is non-canonical"
        )
    if capability.mode != "ledger":
        raise DataBridgePublicationFenceError(
            "DataBridge publication capability must use ledger mode"
        )
    return capability


def _require_publish_authority(
    *,
    publish: bool,
    publication_capability: (
        DailyCoordinatorPublicationCapability | None
    ) = None,
) -> DailyCoordinatorPublicationCapability | None:
    """DataBridge refresh 必须绑定显式模式，ledger 仅限协调器调用域。"""
    del publish
    try:
        identity = require_current_daily_coordinator_identity()
    except Exception as exc:
        raise DataBridgePublicationFenceError(
            "DataBridge coordinator epoch identity is unavailable"
        ) from exc
    mode = identity.mode
    if mode != "ledger":
        return None
    capability = (
        publication_capability
    )
    if capability is None:
        raise DataBridgePublicationFenceError(
            "DataBridge refresh is reserved for an occurrence-bound daily "
            "coordinator capability in ledger mode"
        )
    return _validate_publication_capability(capability)


REQUIRED_SOURCE_TABLES = frozenset(
    {
        "api_wind_daily",
        "api_wind_derivative_daily",
        "api_wind_weekly",
        "api_wind_derivative_weekly",
        "api_wind_monthly",
        "api_wind_derivative_monthly",
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
    """为一次刷新构造标准三频候选目录的可替换数据源。"""

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
    daily_chunk_months: int = 3
    download_concurrency: int = 4
    max_rounds: int = 3
    round_timeout_sec: int = 900
    refresh_start: str = "06:30"
    refresh_deadline: str = "06:55"

    @classmethod
    def from_env(cls) -> "DataBridgeRefreshConfig":
        project_root = Path(__file__).resolve().parents[2]
        config = cls(
            data_root=project_root / "data" / "data_bridge",
            runtime_root=project_root / "backtest_artifacts" / "data_bridge_refresh",
            schema_path=project_root / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json",
            daily_chunk_months=int(os.getenv("DATABRIDGE_DAILY_CHUNK_MONTHS", "3")),
            download_concurrency=int(os.getenv("DATABRIDGE_DOWNLOAD_CONCURRENCY", "4")),
            refresh_start=os.getenv("DATABRIDGE_REFRESH_START", "06:30"),
            refresh_deadline=os.getenv("DATABRIDGE_REFRESH_DEADLINE", "06:55"),
        )
        if config.daily_chunk_months <= 0 or config.download_concurrency <= 0:
            raise ValueError("DataBridge chunk months and download concurrency must be positive")
        if config.download_concurrency > 4:
            raise ValueError("DATABRIDGE_DOWNLOAD_CONCURRENCY must be at most 4")
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


class DataBridgeRoundBuilder:
    def __init__(self, client, config: DataBridgeRefreshConfig) -> None:
        self.client = client
        self.config = config

    def build(
        self,
        round_id: str,
        *,
        end_date: str,
        expected_daily_date: str,
        previous_keys: Mapping[str, set[str] | frozenset[str]] | None,
        continuity_cutoffs: Mapping[str, object] | None = None,
    ) -> DownloadRound:
        started = time.monotonic()
        ranges = quarter_ranges(
            self.config.daily_start_date,
            end_date,
            months=self.config.daily_chunk_months,
        )
        with ThreadPoolExecutor(max_workers=self.config.download_concurrency) as executor:
            payloads = list(
                executor.map(
                    lambda item: self.client.export_csv(
                        "日",
                        start_date=item[0],
                        end_date=item[1],
                        allow_empty=True,
                    ),
                    ranges,
                )
            )
        daily = _merge_daily_payloads([payload for payload in payloads if payload is not None])
        weekly = _read_csv(self.client.export_csv("周"), "weekly_output.csv")
        monthly = _read_csv(self.client.export_csv("月"), "monthly_output.csv")
        dataset = validate_dataset(
            {
                "daily_output.csv": daily,
                "weekly_output.csv": weekly,
                "monthly_output.csv": monthly,
            },
            schema_path=self.config.schema_path,
            expected_daily_date=expected_daily_date,
            previous_keys=previous_keys,
            continuity_cutoffs=continuity_cutoffs,
        )
        elapsed = time.monotonic() - started
        if elapsed > self.config.round_timeout_sec:
            raise DataBridgeRefreshError(
                f"DataBridge full round exceeded {self.config.round_timeout_sec}s: {elapsed:.1f}s"
            )
        staging_root = self.config.runtime_root / "staging"
        staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination = staging_root / round_id
        if destination.exists():
            shutil.rmtree(destination)
        write_validated_dataset(dataset, destination)
        return DownloadRound(
            round_id=round_id,
            directory=destination,
            dataset=dataset,
            digest=dataset.business_digest,
        )


def quarter_ranges(start_date: str, end_date: str, *, months: int = 3) -> list[tuple[str, str]]:
    if months <= 0:
        raise ValueError("months must be positive")
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("start_date must not be after end_date")
    ranges: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        month_index = cursor.year * 12 + cursor.month - 1 + months
        next_start = date(month_index // 12, month_index % 12 + 1, 1)
        chunk_end = min(end, next_start - timedelta(days=1))
        ranges.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return ranges


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
    client=None,
    config: DataBridgeRefreshConfig,
    expected_daily_date: str,
    refresh_date: str,
    publish: bool,
    deadline_at: datetime | None = None,
    publication_capability: (
        DailyCoordinatorPublicationCapability | None
    ) = None,
    continuity_authority: object | None = None,
    round_builder: DataBridgeRoundSource | None = None,
    enforce_legacy_publication_fence: bool = True,
) -> RefreshResult:
    if (
        not enforce_legacy_publication_fence
        and publication_capability is not None
    ):
        raise DataBridgePublicationFenceError(
            "legacy publication capability cannot be mixed with the "
            "launchd-only refresh path"
        )
    if not enforce_legacy_publication_fence:
        _require_launchd_round_builder(round_builder)
    authority = (
        _require_publish_authority(
            publish=publish,
            publication_capability=publication_capability,
        )
        if enforce_legacy_publication_fence
        else None
    )
    if (
        authority is not None
        and authority.business_date != refresh_date
    ):
        raise DataBridgePublicationFenceError(
            "DataBridge publication capability business_date does not "
            "match refresh_date"
        )
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    with store.lock(exclusive=True, blocking=False):
        if authority is not None:
            _validate_publication_capability(authority)
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
                if client is None:
                    raise DataBridgeRefreshError(
                        "DataBridge refresh requires a round source"
                    )
                _validate_source_tables(client.get_tables())
                _ensure_source_ready(client, expected_daily_date)
                builder: DataBridgeRoundSource = DataBridgeRoundBuilder(
                    client,
                    config,
                )
            else:
                builder = round_builder
            previous_keys = (
                {
                    filename: profile.keys
                    for filename, profile
                    in previous_current.dataset.files.items()
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
            if authority is not None:
                _validate_publication_capability(authority)
            assert selected.dataset is not None
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
                state = store.publish(
                    selected.directory,
                    state,
                    publication_capability=authority,
                    enforce_legacy_publication_fence=(
                        enforce_legacy_publication_fence
                    ),
                )
            return RefreshResult(
                state=state,
                published=publish,
                rounds_completed=len(built_directories),
                duration_sec=time.monotonic() - started,
            )
        except BaseException as exc:
            if (
                publish
                and not isinstance(
                    exc,
                    DataBridgePublicationFenceError,
                )
            ):
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


def _validate_source_tables(payload: Mapping[str, object]) -> None:
    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, list):
        raise DataBridgeRefreshError("DataBridge tables response is missing tables")
    available = {
        str(item.get("name"))
        for item in raw_tables
        if isinstance(item, dict) and item.get("status") == "ok"
    }
    missing = sorted(REQUIRED_SOURCE_TABLES - available)
    if missing:
        raise DataBridgeRefreshError(f"DataBridge required tables are unavailable: {missing}")


def check_current_dataset(
    config: DataBridgeRefreshConfig,
    *,
    required_refresh_date: str | None = None,
    expected_daily_date: str | None = None,
    expected_generation_id: str | None = None,
    expected_business_digest: str | None = None,
    expected_publication_capability: (
        DailyCoordinatorPublicationCapability | None
    ) = None,
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
        if expected_publication_capability is not None:
            authority = _validate_publication_capability(
                expected_publication_capability
            )
            if state.get("publication_capability") != (
                _publication_capability_payload(authority)
            ):
                raise DataBridgePublicationFenceError(
                    "DataBridge current publication capability does not "
                    "match caller occurrence"
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


def _ensure_source_ready(client, expected_daily_date: str) -> None:
    payload = client.export_csv(
        "日",
        start_date=expected_daily_date,
        end_date=expected_daily_date,
    )
    frame = _read_csv(payload, "daily_output.csv")
    if "date" not in frame.columns:
        raise DataBridgeRefreshError("DataBridge readiness export is missing date")
    normalized = {
        pd.to_datetime(str(value), errors="raise").date().isoformat()
        for value in frame["date"].tolist()
    }
    if expected_daily_date not in normalized:
        raise DataBridgeRefreshError(
            f"DataBridge source is not ready for expected daily date {expected_daily_date}"
        )


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
            raise DataBridgeRefreshError(
                "DataBridge continuity authority current drift: "
                "expected current is missing"
            )
        return None
    if continuity_authority is None:
        raise DataBridgeRefreshError(
            "DataBridge continuity authority is missing for existing current"
        )
    if not isinstance(
        continuity_authority,
        DataBridgeContinuityAuthority,
    ):
        raise DataBridgeRefreshError(
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
        raise DataBridgeRefreshError(
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
        raise DataBridgeRefreshError(
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
        raise DataBridgeRefreshError(
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
        raise DataBridgeRefreshError(
            "DataBridge continuity authority current identity drift"
        )
    return cutoffs


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


def _publication_capability_payload(
    capability: DailyCoordinatorPublicationCapability,
) -> dict[str, object]:
    return {
        "occurrence_id": capability.occurrence_id,
        "business_date": capability.business_date,
        "daily_coordinator_epoch": capability.epoch_payload(),
    }


def _publication_identity_from_state(
    state: Mapping[str, object],
) -> dict[str, object]:
    """提取与 current 原子切换的稳定身份；排除后置 published_at。"""
    if not isinstance(state, Mapping):
        raise DataBridgeRefreshError(
            "DataBridge publication state must be an object"
        )
    identity: dict[str, object] = {
        "manifest_version": LEGACY_CURRENT_PUBLICATION_MANIFEST_VERSION,
        "generation_id": copy.deepcopy(state.get("generation_id")),
        "refresh_date": copy.deepcopy(state.get("refresh_date")),
        "schema_version": copy.deepcopy(state.get("schema_version")),
        "business_digest": copy.deepcopy(
            state.get("business_digest")
        ),
        "files": copy.deepcopy(state.get("files")),
        "publication_capability": (
            _normalize_stored_publication_capability(
                state.get("publication_capability")
            )
        ),
    }
    source_provenance = state.get("source_provenance")
    if source_provenance is None:
        return identity
    source_mode = state.get("source_mode")
    if source_mode != LOCAL_MYSQL_SOURCE_MODE:
        raise DataBridgeRefreshError(
            "DataBridge local source provenance requires local_mysql mode"
        )
    identity["manifest_version"] = CURRENT_PUBLICATION_MANIFEST_VERSION
    identity["source_mode"] = source_mode
    identity["source_provenance"] = _normalize_source_provenance(
        source_provenance
    )
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
    if identity.get("manifest_version") != CURRENT_PUBLICATION_MANIFEST_VERSION:
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


def _normalize_source_provenance(payload: object) -> dict[str, object]:
    """校验并规范化写入 v2 current identity 的 MySQL 水位证据。"""
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


def _normalize_stored_publication_capability(
    payload: object,
) -> dict[str, object] | None:
    if payload is None:
        return None
    if (
        not isinstance(payload, Mapping)
        or set(payload)
        != {
            "occurrence_id",
            "business_date",
            "daily_coordinator_epoch",
        }
    ):
        raise DataBridgeRefreshError(
            "DataBridge publication capability fields are invalid"
        )
    occurrence_id = payload.get("occurrence_id")
    business_date = payload.get("business_date")
    epoch = payload.get("daily_coordinator_epoch")
    if (
        not isinstance(occurrence_id, int)
        or isinstance(occurrence_id, bool)
        or occurrence_id <= 0
        or not isinstance(business_date, str)
        or date.fromisoformat(business_date).isoformat()
        != business_date
        or not isinstance(epoch, Mapping)
        or set(epoch) != {"epoch", "mode", "record_sha256"}
    ):
        raise DataBridgeRefreshError(
            "DataBridge publication capability identity is invalid"
        )
    normalized_epoch = {
        "epoch": epoch.get("epoch"),
        "mode": epoch.get("mode"),
        "record_sha256": epoch.get("record_sha256"),
    }
    if (
        not isinstance(normalized_epoch["epoch"], int)
        or isinstance(normalized_epoch["epoch"], bool)
        or normalized_epoch["epoch"] <= 0
        or normalized_epoch["mode"] != "ledger"
        or not isinstance(
            normalized_epoch["record_sha256"],
            str,
        )
        or re.fullmatch(
            r"[0-9a-f]{64}",
            normalized_epoch["record_sha256"],
        )
        is None
    ):
        raise DataBridgeRefreshError(
            "DataBridge publication capability epoch is invalid"
        )
    return {
        "occurrence_id": occurrence_id,
        "business_date": business_date,
        "daily_coordinator_epoch": normalized_epoch,
    }


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
            os.O_RDONLY | os.O_NOFOLLOW,
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
    if identity.get("manifest_version") == CURRENT_PUBLICATION_MANIFEST_VERSION:
        if identity.get("source_mode") != LOCAL_MYSQL_SOURCE_MODE:
            raise DataBridgeRefreshError(
                "DataBridge publication manifest local source mode is invalid"
            )
        _normalize_source_provenance(identity.get("source_provenance"))
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


def _merge_daily_payloads(payloads: list[bytes]) -> pd.DataFrame:
    if not payloads:
        raise DataBridgeRefreshError("daily export produced no chunks")
    frames = [_read_csv(payload, "daily_output.csv") for payload in payloads]
    columns = list(frames[0].columns)
    for frame in frames[1:]:
        if list(frame.columns) != columns:
            raise DataBridgeRefreshError("daily export chunk columns do not match")
    combined = pd.concat(frames, ignore_index=True)
    try:
        normalized = pd.to_datetime(combined["date"], errors="raise").dt.date.astype(str)
    except (KeyError, TypeError, ValueError) as exc:
        raise DataBridgeRefreshError("daily export contains an invalid date") from exc
    normalized_column = object()
    combined[normalized_column] = normalized
    for normalized_date, group in combined.groupby(normalized_column, sort=False):
        if len(group) <= 1:
            continue
        values = group.drop(columns=["date", normalized_column])
        if any(values[column].nunique(dropna=False) > 1 for column in values.columns):
            raise DataBridgeRefreshError(
                f"daily export has conflicting duplicate boundary row for {normalized_date}"
            )
    combined = combined.drop_duplicates(normalized_column, keep="last")
    combined = combined.sort_values(normalized_column).drop(columns=normalized_column)
    return combined.reset_index(drop=True)


def _read_csv(payload: bytes, filename: str) -> pd.DataFrame:
    try:
        text = payload.decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(text)))
        validate_unique_csv_header(filename, header)
        frame = pd.read_csv(
            io.StringIO(text),
            dtype="string",
            keep_default_na=False,
        )
    except StopIteration as exc:
        raise DataBridgeRefreshError(f"{filename} header must not be empty") from exc
    except DataBridgeValidationError as exc:
        raise DataBridgeRefreshError(str(exc)) from exc
    except (UnicodeError, csv.Error, pd.errors.ParserError) as exc:
        raise DataBridgeRefreshError(f"{filename} is not a valid UTF-8 CSV") from exc
    if frame.empty:
        raise DataBridgeRefreshError(f"{filename} is empty")
    return frame


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
                raise DataBridgeRefreshError(
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
            _ensure_private_directory(
                self.data_root,
                label="DataBridge data root",
            )
            _ensure_private_directory(
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
                raise DataBridgeRefreshError(
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
        fail_after_backup: bool = False,
        publication_capability: (
            DailyCoordinatorPublicationCapability | None
        ) = None,
        enforce_legacy_publication_fence: bool = True,
    ) -> dict[str, object]:
        if (
            not enforce_legacy_publication_fence
            and publication_capability is not None
        ):
            raise DataBridgePublicationFenceError(
                "legacy publication capability cannot be mixed with the "
                "launchd-only refresh path"
            )
        authority = (
            _require_publish_authority(
                publish=True,
                publication_capability=publication_capability,
            )
            if enforce_legacy_publication_fence
            else None
        )
        candidate = Path(candidate_dir)
        _validate_publish_candidate(candidate)
        with self.lock(exclusive=True):
            if authority is not None:
                _validate_publication_capability(authority)
            _validate_publish_candidate(candidate)
            self._recover_locked()
            next_dir = Path(tempfile.mkdtemp(prefix=".current-next-", dir=self.data_root))
            shutil.rmtree(next_dir)
            shutil.copytree(
                candidate,
                next_dir,
                copy_function=_copy_regular_file_no_follow,
            )
            _validate_publish_candidate(candidate)
            published_state = copy.deepcopy(state)
            if authority is not None:
                _validate_publication_capability(authority)
                published_state["publication_capability"] = (
                    _publication_capability_payload(authority)
                )
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
            state_replaced = False
            try:
                if authority is not None:
                    _validate_publication_capability(authority)
                if self.previous_dir.exists():
                    shutil.rmtree(self.previous_dir)
                if had_current:
                    os.replace(self.current_dir, self.previous_dir)
                if fail_after_backup:
                    raise RuntimeError("injected publication failure")
                if authority is not None:
                    _validate_publication_capability(authority)
                os.replace(next_dir, self.current_dir)
                published_state["published_at"] = (
                    _shanghai_now().isoformat(
                        timespec="seconds"
                    )
                )
                state_replaced = True
                if authority is not None:
                    _validate_publication_capability(authority)
                self._write_state_locked(published_state)
                _fsync_directory(self.data_root)
                _fsync_directory(self.runtime_root)
                if self.previous_dir.exists():
                    shutil.rmtree(self.previous_dir)
            except BaseException:
                if self.current_dir.exists():
                    shutil.rmtree(self.current_dir)
                if self.previous_dir.exists():
                    os.replace(self.previous_dir, self.current_dir)
                if next_dir.exists():
                    shutil.rmtree(next_dir)
                if state_replaced:
                    self._restore_state_locked(previous_state)
                _fsync_directory(self.data_root)
                _fsync_directory(self.runtime_root)
                raise
        return published_state

    def cleanup_crash_debris(self) -> tuple[str, ...]:
        """在外层 occurrence owner 下清理未发布 ``.current-next-*``。"""
        _ensure_private_directory(
            self.data_root,
            label="DataBridge data root",
        )
        _ensure_private_directory(
            self.runtime_root,
            label="DataBridge runtime root",
        )
        removed: list[str] = []
        with self.lock(exclusive=True):
            candidates = sorted(
                (
                    entry
                    for entry in self.data_root.iterdir()
                    if entry.name.startswith(".current-next-")
                ),
                key=lambda entry: entry.name,
            )
            for entry in candidates:
                if not re.fullmatch(
                    r"\.current-next-[A-Za-z0-9_-]+",
                    entry.name,
                ):
                    raise DataBridgeRefreshError(
                        "DataBridge crash debris has an unsafe name"
                    )
                _remove_private_debris_tree(
                    entry,
                    label="DataBridge current-next crash debris",
                )
                removed.append(entry.name)
            if removed:
                _fsync_directory(self.data_root)
        return tuple(removed)

    def load_state(self) -> dict[str, object]:
        with self.lock(exclusive=False):
            if not self.state_path.is_file():
                raise DataBridgeRefreshError("DataBridge refresh state does not exist")
            return json.loads(self.state_path.read_text(encoding="utf-8"))

    def recover(self, *, schema_path: str | Path) -> None:
        """按 state/current 完整发布身份恢复原子切换中断现场。"""
        with self.lock(exclusive=True):
            state: Mapping[str, object] | None = None
            state_identity: dict[str, object] | None = None
            if self.state_path.is_file():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    state_identity = _publication_identity_from_state(
                        state
                    )
                except (OSError, json.JSONDecodeError):
                    state_identity = None

            current_identity = _directory_publication_identity(
                self.current_dir,
                schema_path=Path(schema_path),
            )
            previous_identity = _directory_publication_identity(
                self.previous_dir,
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
                    shutil.rmtree(self.previous_dir)
                elif (
                    state_identity is not None
                    and previous_identity == state_identity
                ):
                    if self.current_dir.exists():
                        shutil.rmtree(self.current_dir)
                    os.replace(self.previous_dir, self.current_dir)
                else:
                    raise DataBridgeRefreshError(
                        "cannot recover DataBridge current/previous against published state"
                    )
            elif self.current_dir.exists() and (
                state_identity is None
                or current_identity != state_identity
            ):
                raise DataBridgeRefreshError(
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

    def _recover_locked(self) -> None:
        if not self.current_dir.exists() and self.previous_dir.exists():
            os.replace(self.previous_dir, self.current_dir)

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


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DataBridgeRefreshError(
                "DataBridge fsync target is not a regular file"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_private_directory(
    path: Path,
    *,
    label: str,
    error_type: type[DataBridgeRefreshError] = DataBridgeRefreshError,
    missing_error_type: (
        type[DataBridgeRefreshError] | None
    ) = None,
) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise (missing_error_type or error_type)(
            f"{label} is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise error_type(f"{label} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise error_type(f"{label} must be a directory")
    if info.st_uid != os.getuid():
        raise error_type(f"{label} has another owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise error_type(
            f"{label} must be private (mode 0700 or stricter)"
        )


def _open_strict_read_lock(path: Path):
    """原子、无跟随地打开既有 lock，不创建任何目录或文件。"""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise DataBridgeCurrentMissingError(
            "DataBridge refresh lock is missing"
        ) from exc
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh lock is invalid"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise DataBridgeCurrentInvalidError(
                "DataBridge refresh lock is unsafe"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _read_strict_current_state(path: Path) -> dict[str, object]:
    """无跟随读取既有 state，缺失与损坏在 shared lock 内分类。"""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError as exc:
        raise DataBridgeCurrentMissingError(
            "DataBridge refresh state does not exist"
        ) from exc
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise DataBridgeCurrentInvalidError(
                    "DataBridge refresh state is not a regular file"
                )
            raw = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    if len(raw) > 4 * 1024 * 1024:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is too large"
        )
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state is invalid"
        ) from exc
    if not isinstance(state, dict):
        raise DataBridgeCurrentInvalidError(
            "DataBridge refresh state must be an object"
        )
    return state


def _ensure_private_directory(path: Path, *, label: str) -> None:
    """只创建新私有根；既有非私有根必须由部署迁移显式修复。"""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_directory(path, label=label)


def _validate_publish_candidate(path: Path) -> None:
    try:
        root_info = path.lstat()
    except FileNotFoundError as exc:
        raise DataBridgeRefreshError(
            "candidate directory does not exist"
        ) from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise DataBridgeRefreshError(
            "candidate directory must not be a symlink"
        )
    if not stat.S_ISDIR(root_info.st_mode):
        raise DataBridgeRefreshError("candidate must be a directory")
    entries = list(path.iterdir())
    if {entry.name for entry in entries} != set(EXPECTED_FILENAMES):
        raise DataBridgeRefreshError(
            "candidate directory does not contain exactly three data files"
        )
    for entry in entries:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise DataBridgeRefreshError(
                f"candidate file must not be a symlink: {entry.name}"
            )
        if not stat.S_ISREG(info.st_mode):
            raise DataBridgeRefreshError(
                f"candidate file must be regular: {entry.name}"
            )


def _copy_regular_file_no_follow(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> str:
    """以 O_NOFOLLOW 复制候选文件，并拒绝复制期间身份漂移。"""
    source_path = Path(source)
    destination_path = Path(destination)
    before = source_path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DataBridgeRefreshError(
            f"candidate file must be regular and non-symlink: "
            f"{source_path.name}"
        )
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise DataBridgeRefreshError(
                f"candidate file changed while opening: {source_path.name}"
            )
        destination_descriptor = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    after = source_path.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise DataBridgeRefreshError(
            f"candidate file changed while copying: {source_path.name}"
        )
    return str(destination_path)


def _remove_private_debris_tree(path: Path, *, label: str) -> None:
    try:
        root_info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_info.st_mode):
        raise DataBridgeRefreshError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(root_info.st_mode):
        raise DataBridgeRefreshError(f"{label} must be a directory")
    for current_root, directory_names, filenames in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in (*directory_names, *filenames):
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise DataBridgeRefreshError(
                    f"{label} contains a symlink"
                )
            if not (
                stat.S_ISDIR(info.st_mode)
                or stat.S_ISREG(info.st_mode)
            ):
                raise DataBridgeRefreshError(
                    f"{label} contains a special file"
                )
    shutil.rmtree(path)


def _shanghai_now() -> datetime:
    """返回 DataBridge 契约时区的当前时刻，测试可替换。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))
