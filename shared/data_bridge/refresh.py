from __future__ import annotations

import csv
import fcntl
import io
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from shared.data_bridge.validation import (
    DataBridgeValidationError,
    EXPECTED_FILENAMES,
    ValidatedDataBridgeDataset,
    read_dataset_directory,
    validate_dataset,
    validate_unique_csv_header,
    write_validated_dataset,
)


class DataBridgeRefreshError(RuntimeError):
    """A full refresh could not produce a stable valid dataset."""


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
    refresh_start: str = "06:00"
    refresh_deadline: str = "07:00"

    @classmethod
    def from_env(cls) -> "DataBridgeRefreshConfig":
        project_root = Path(__file__).resolve().parents[2]
        config = cls(
            data_root=project_root / "data" / "data_bridge",
            runtime_root=project_root / "backtest_artifacts" / "data_bridge_refresh",
            schema_path=project_root / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json",
            daily_chunk_months=int(os.getenv("DATABRIDGE_DAILY_CHUNK_MONTHS", "3")),
            download_concurrency=int(os.getenv("DATABRIDGE_DOWNLOAD_CONCURRENCY", "4")),
            refresh_start=os.getenv("DATABRIDGE_REFRESH_START", "06:00"),
            refresh_deadline=os.getenv("DATABRIDGE_REFRESH_DEADLINE", "07:00"),
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
        )
        elapsed = time.monotonic() - started
        if elapsed > self.config.round_timeout_sec:
            raise DataBridgeRefreshError(
                f"DataBridge full round exceeded {self.config.round_timeout_sec}s: {elapsed:.1f}s"
            )
        staging_root = self.config.runtime_root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
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
    previous_digest: str | None = None
    for _ in range(max_rounds):
        try:
            current = next(rounds)
        except StopIteration as exc:
            raise DataBridgeRefreshError("not enough download rounds to establish stability") from exc
        if previous_digest is not None and previous_digest == current.digest:
            return current
        previous_digest = current.digest
    raise DataBridgeRefreshError(
        f"DataBridge did not produce matching consecutive rounds within {max_rounds} rounds"
    )


def run_full_refresh(
    *,
    client,
    config: DataBridgeRefreshConfig,
    expected_daily_date: str,
    refresh_date: str,
    publish: bool,
    deadline_at: datetime | None = None,
) -> RefreshResult:
    started = time.monotonic()
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    built_directories: list[Path] = []
    try:
        store.recover(schema_path=config.schema_path)
        _ensure_before_deadline(deadline_at)
        _validate_source_tables(client.get_tables())
        _ensure_source_ready(client, expected_daily_date)
        previous_keys = _load_previous_keys(store, config.schema_path)
        builder = DataBridgeRoundBuilder(client, config)

        def rounds() -> Iterator[DownloadRound]:
            for index in range(config.max_rounds):
                _ensure_before_deadline(deadline_at)
                item = builder.build(
                    f"round-{index + 1}",
                    end_date=refresh_date,
                    expected_daily_date=expected_daily_date,
                    previous_keys=previous_keys,
                )
                built_directories.append(item.directory)
                _ensure_before_deadline(deadline_at)
                yield item
                item = None

        selected = select_stable_round(rounds(), max_rounds=config.max_rounds)
        assert selected.dataset is not None
        for directory in built_directories:
            if directory != selected.directory and directory.exists():
                shutil.rmtree(directory)
        state = _build_state(
            selected.dataset,
            refresh_date,
            len(built_directories),
            duration_sec=time.monotonic() - started,
        )
        if publish:
            store.publish(selected.directory, state)
        return RefreshResult(
            state=state,
            published=publish,
            rounds_completed=len(built_directories),
            duration_sec=time.monotonic() - started,
        )
    except BaseException as exc:
        if publish:
            try:
                store.record_failed_attempt(
                    refresh_date=refresh_date,
                    error=str(exc)[:1000],
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
) -> CurrentDataset:
    """在共享锁内验证 current 三文件与发布状态完全一致。"""
    store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
    with store.lock(exclusive=False):
        if not store.state_path.is_file():
            raise DataBridgeRefreshError("DataBridge refresh state does not exist")
        try:
            state = json.loads(store.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataBridgeRefreshError("DataBridge refresh state is invalid") from exc
        dataset = validate_dataset(
            read_dataset_directory(store.current_dir),
            schema_path=config.schema_path,
            expected_daily_date=expected_daily_date,
        )
        if state.get("schema_version") != dataset.schema_version:
            raise DataBridgeRefreshError("DataBridge state schema_version does not match current files")
        if state.get("business_digest") != dataset.business_digest:
            raise DataBridgeRefreshError("DataBridge state digest does not match current files")
        if required_refresh_date is not None and state.get("refresh_date") != required_refresh_date:
            raise DataBridgeRefreshError(
                f"DataBridge refresh_date must be {required_refresh_date}, got {state.get('refresh_date')}"
            )
        state_files = state.get("files")
        if not isinstance(state_files, dict):
            raise DataBridgeRefreshError("DataBridge state files profile is missing")
        for filename, profile in dataset.files.items():
            item = state_files.get(filename)
            if not isinstance(item, dict) or item.get("sha256") != profile.sha256:
                raise DataBridgeRefreshError(f"DataBridge state hash mismatch for {filename}")
        return CurrentDataset(state=state, dataset=dataset)


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


def _load_previous_keys(
    store: "DataBridgeStore",
    schema_path: Path,
) -> dict[str, frozenset[str]] | None:
    if not store.current_dir.is_dir():
        return None
    with store.lock(exclusive=False):
        dataset = validate_dataset(
            read_dataset_directory(store.current_dir),
            schema_path=schema_path,
        )
    return {filename: profile.keys for filename, profile in dataset.files.items()}


def _build_state(
    dataset: ValidatedDataBridgeDataset,
    refresh_date: str,
    stability_rounds: int,
    *,
    duration_sec: float,
) -> dict[str, object]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    generation_id = f"full-{refresh_date.replace('-', '')}-{now.strftime('%H%M%S')}-{dataset.business_digest[:12]}"
    return {
        "schema_version": dataset.schema_version,
        "generation_id": generation_id,
        "refresh_date": refresh_date,
        "refreshed_at": now.isoformat(timespec="seconds"),
        "source_mode": "full_export",
        "stability_rounds": stability_rounds,
        "business_digest": dataset.business_digest,
        "last_attempt": {
            "status": "success",
            "refresh_date": refresh_date,
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

    @contextmanager
    def lock(self, *, exclusive: bool) -> Iterator[None]:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def publish(
        self,
        candidate_dir: str | Path,
        state: dict[str, object],
        *,
        fail_after_backup: bool = False,
    ) -> None:
        candidate = Path(candidate_dir)
        entries = {path.name for path in candidate.iterdir()} if candidate.is_dir() else set()
        if entries != set(EXPECTED_FILENAMES):
            raise DataBridgeRefreshError("candidate directory does not contain exactly three data files")
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with self.lock(exclusive=True):
            self._recover_locked()
            next_dir = Path(tempfile.mkdtemp(prefix=".current-next-", dir=self.data_root))
            shutil.rmtree(next_dir)
            shutil.copytree(candidate, next_dir)
            for path in next_dir.iterdir():
                path.chmod(0o444)
            had_current = self.current_dir.exists()
            try:
                if self.previous_dir.exists():
                    shutil.rmtree(self.previous_dir)
                if had_current:
                    os.replace(self.current_dir, self.previous_dir)
                if fail_after_backup:
                    raise RuntimeError("injected publication failure")
                os.replace(next_dir, self.current_dir)
                self._write_state_locked(state)
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
                _fsync_directory(self.data_root)
                _fsync_directory(self.runtime_root)
                raise

    def load_state(self) -> dict[str, object]:
        with self.lock(exclusive=False):
            if not self.state_path.is_file():
                raise DataBridgeRefreshError("DataBridge refresh state does not exist")
            return json.loads(self.state_path.read_text(encoding="utf-8"))

    def recover(self, *, schema_path: str | Path) -> None:
        """按 state 摘要恢复原子发布中断现场，并清理未发布 staging。"""
        with self.lock(exclusive=True):
            state_digest: str | None = None
            if self.state_path.is_file():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if state.get("business_digest"):
                        state_digest = str(state["business_digest"])
                except (OSError, json.JSONDecodeError):
                    state_digest = None

            current_digest = self._directory_digest(self.current_dir, Path(schema_path))
            previous_digest = self._directory_digest(self.previous_dir, Path(schema_path))
            if self.previous_dir.exists():
                if current_digest is not None and current_digest == state_digest:
                    shutil.rmtree(self.previous_dir)
                elif previous_digest is not None and (
                    state_digest is None or previous_digest == state_digest
                ):
                    if self.current_dir.exists():
                        shutil.rmtree(self.current_dir)
                    os.replace(self.previous_dir, self.current_dir)
                else:
                    raise DataBridgeRefreshError(
                        "cannot recover DataBridge current/previous against published state"
                    )

            staging = self.runtime_root / "staging"
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _directory_digest(directory: Path, schema_path: Path) -> str | None:
        if not directory.is_dir():
            return None
        try:
            return validate_dataset(
                read_dataset_directory(directory),
                schema_path=schema_path,
            ).business_digest
        except (OSError, ValueError):
            return None

    def record_failed_attempt(
        self,
        *,
        refresh_date: str,
        error: str,
        duration_sec: float,
    ) -> None:
        """记录失败原因，但不替换最后成功的 current generation。"""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
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
                "error": error,
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


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
