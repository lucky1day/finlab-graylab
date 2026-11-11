"""Release 迁移清单：基线加增量迁移的解析、摘要校验与 pending 选择。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from migrations._contracts import (
    MigrationHistoryError,
    MigrationSQLParseError,
    PreparedMigration,
    RERUNNABLE_MARKER,
)
from migrations._sql import split_sql_statements


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
RELEASE_MIGRATION_MANIFEST_PATH = MIGRATIONS_DIR / "release_manifest.json"
RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION = 2

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MIGRATION_FILENAME = re.compile(r"(\d{3})_[A-Za-z0-9_.-]+\.sql")


@dataclass(frozen=True)
class HistoryEntry:
    """已退役迁移文件在历史表中的精确身份。"""

    version: int
    filename: str
    sha256: str


@dataclass(frozen=True)
class MigrationBaseline:
    """等价于 ``through_version`` 之前全部历史迁移的建库基线。"""

    through_version: int
    path: Path
    sha256: str
    statements: tuple[str, ...]
    history: tuple[HistoryEntry, ...]
    table_names: frozenset[str]


@dataclass(frozen=True)
class MigrationPlan:
    """一个 release 的完整迁移计划。"""

    baseline: MigrationBaseline
    migrations: tuple[PreparedMigration, ...]


def _parse_sql(path: Path, raw_sql: bytes) -> tuple[str, ...]:
    try:
        statements = split_sql_statements(raw_sql.decode("utf-8"))
    except (UnicodeDecodeError, MigrationSQLParseError) as exc:
        raise MigrationSQLParseError(f"{path.name}: {exc}") from exc
    if not statements:
        raise MigrationSQLParseError(
            f"{path.name}: migration contains no statements"
        )
    return tuple(statements)


def prepare_migration_files(
    paths: Iterable[Path],
    *,
    first_version: int,
) -> list[PreparedMigration]:
    """解析增量迁移，要求版本从 ``first_version`` 起连续且文件名唯一。"""
    prepared: list[PreparedMigration] = []
    for raw_path in sorted(paths, key=lambda candidate: Path(candidate).name):
        path = Path(raw_path)
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationHistoryError(
                f"invalid migration filename: {path.name}"
            )
        raw_sql = path.read_bytes()
        prepared.append(
            PreparedMigration(
                version=int(match.group(1)),
                path=path,
                sha256=hashlib.sha256(raw_sql).hexdigest(),
                statements=_parse_sql(path, raw_sql),
                rerunnable=raw_sql.decode("utf-8").startswith(
                    RERUNNABLE_MARKER + "\n"
                ),
            )
        )
    versions = [migration.version for migration in prepared]
    expected = list(range(first_version, first_version + len(prepared)))
    if versions != expected:
        raise MigrationHistoryError(
            "migration files must be contiguous from "
            f"{first_version:03d}; found {versions}"
        )
    return prepared


def _require_entry(raw: object, *, label: str) -> HistoryEntry:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "filename",
        "sha256",
    }:
        raise MigrationHistoryError(
            f"{label} must contain exactly version, filename and sha256"
        )
    version, filename, sha256 = raw["version"], raw["filename"], raw["sha256"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version <= 0
        or not isinstance(filename, str)
        or re.fullmatch(rf"{version:03d}_[A-Za-z0-9_.-]+\.sql", filename)
        is None
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise MigrationHistoryError(f"{label} is invalid: {dict(raw)}")
    return HistoryEntry(version, filename, sha256)


def _baseline_table_names(statements: Iterable[str]) -> frozenset[str]:
    names = set()
    for statement in statements:
        match = re.match(
            r"\s*CREATE\s+TABLE\s+`?([A-Za-z0-9_]+)`?",
            statement,
            re.IGNORECASE,
        )
        if match:
            names.add(match.group(1).lower())
    return frozenset(names)


def validate_release_migration_manifest(
    paths: Iterable[Path],
    *,
    manifest_path: Path | None = None,
) -> MigrationPlan:
    """校验基线与增量迁移文件同受版本控制的摘要清单逐字节一致。"""
    path = Path(manifest_path or RELEASE_MIGRATION_MANIFEST_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationHistoryError(
            f"release migration manifest cannot be read: {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "baseline",
        "migrations",
    }:
        raise MigrationHistoryError(
            "release migration manifest must contain exactly "
            "schema_version, baseline and migrations"
        )
    if payload["schema_version"] != RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION:
        raise MigrationHistoryError(
            "release migration manifest schema_version mismatch: "
            f"{payload['schema_version']!r}"
        )

    raw_baseline = payload["baseline"]
    if not isinstance(raw_baseline, Mapping) or set(raw_baseline) != {
        "through_version",
        "path",
        "sha256",
        "history",
    }:
        raise MigrationHistoryError("release migration baseline is invalid")
    through_version = raw_baseline["through_version"]
    raw_history = raw_baseline["history"]
    if (
        not isinstance(through_version, int)
        or isinstance(through_version, bool)
        or not isinstance(raw_history, list)
    ):
        raise MigrationHistoryError("release migration baseline is invalid")
    history = tuple(
        _require_entry(item, label=f"baseline history entry {offset}")
        for offset, item in enumerate(raw_history)
    )
    if [entry.version for entry in history] != list(
        range(1, through_version + 1)
    ):
        raise MigrationHistoryError(
            "baseline history must cover 001.."
            f"{through_version:03d} exactly"
        )
    baseline_path = path.parent / str(raw_baseline["path"])
    try:
        baseline_sql = baseline_path.read_bytes()
    except OSError as exc:
        raise MigrationHistoryError(
            f"migration baseline cannot be read: {baseline_path}"
        ) from exc
    baseline_sha256 = hashlib.sha256(baseline_sql).hexdigest()
    if baseline_sha256 != raw_baseline["sha256"]:
        raise MigrationHistoryError(
            "migration baseline checksum drift: "
            f"expected {raw_baseline['sha256']}, found {baseline_sha256}"
        )
    baseline_statements = _parse_sql(baseline_path, baseline_sql)
    baseline = MigrationBaseline(
        through_version=through_version,
        path=baseline_path,
        sha256=baseline_sha256,
        statements=baseline_statements,
        history=history,
        table_names=_baseline_table_names(baseline_statements),
    )

    raw_entries = payload["migrations"]
    if not isinstance(raw_entries, list):
        raise MigrationHistoryError(
            "release migration manifest migrations must be a list"
        )
    expected = [
        _require_entry(item, label=f"migration entry {offset}")
        for offset, item in enumerate(raw_entries)
    ]
    raw_paths = sorted((Path(raw) for raw in paths), key=lambda item: item.name)
    observed_names = [item.name for item in raw_paths]
    expected_names = [entry.filename for entry in expected]
    if observed_names != expected_names:
        raise MigrationHistoryError(
            "release migration manifest file set mismatch: "
            f"missing={sorted(set(expected_names) - set(observed_names))} "
            f"unknown={sorted(set(observed_names) - set(expected_names))}"
        )
    prepared = prepare_migration_files(
        raw_paths,
        first_version=through_version + 1,
    )
    drift = [
        {
            "filename": entry.filename,
            "expected_sha256": entry.sha256,
            "observed_sha256": migration.sha256,
        }
        for entry, migration in zip(expected, prepared)
        if (entry.version, entry.sha256) != (migration.version, migration.sha256)
    ]
    if drift:
        raise MigrationHistoryError(
            f"release migration manifest checksum drift: {drift}"
        )
    return MigrationPlan(baseline=baseline, migrations=tuple(prepared))


def select_pending_migrations(
    plan: MigrationPlan,
    history_rows: Iterable[Mapping[str, object]],
) -> list[PreparedMigration]:
    """校验历史与基线/增量身份逐行一致，只返回尚未执行的增量后缀。"""
    expected: dict[int, tuple[str, str]] = {
        entry.version: (entry.filename, entry.sha256)
        for entry in plan.baseline.history
    }
    expected.update(
        {
            migration.version: (migration.path.name, migration.sha256)
            for migration in plan.migrations
        }
    )
    versions: list[int] = []
    for row in sorted(history_rows, key=lambda item: int(item["version"])):
        version = int(row["version"])
        identity = expected.get(version)
        if identity is None:
            raise MigrationHistoryError(
                f"unknown migration history version: {version:03d}"
            )
        filename = str(row.get("filename") or "")
        checksum = str(row.get("sha256") or "").lower()
        if (filename, checksum) != identity:
            raise MigrationHistoryError(
                f"migration history identity drift for {version:03d}: "
                f"expected {identity}, found {(filename, checksum)}"
            )
        state = str(row.get("state") or "").upper()
        if state == "APPLYING":
            raise MigrationHistoryError(
                "migration history contains APPLYING row; inspect and "
                f"recover before applying: {filename}"
            )
        if state != "APPLIED":
            raise MigrationHistoryError(
                f"invalid migration history state {state!r}: {filename}"
            )
        versions.append(version)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationHistoryError(
            f"applied migration history must be a contiguous prefix; found {versions}"
        )
    if len(versions) < plan.baseline.through_version:
        raise MigrationHistoryError(
            "database history stops before the schema baseline "
            f"({len(versions):03d} < {plan.baseline.through_version:03d}); "
            "upgrade it with an archived release that still ships the "
            "individual migrations first"
        )
    applied = set(versions)
    return [
        migration for migration in plan.migrations
        if migration.version not in applied
    ]
