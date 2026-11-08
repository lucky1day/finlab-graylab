"""迁移文件与 release manifest 的连续性、摘要和历史核验。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
from migrations._contracts import (
    MigrationHistoryError,
    MigrationSQLParseError,
    PreparedMigration,
)
from migrations._sql import (
    split_sql_statements,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

RELEASE_MIGRATION_MANIFEST_PATH = MIGRATIONS_DIR / "release_manifest.json"

RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION = 1


def prepare_migration_files(
    paths: Iterable[Path],
) -> list[PreparedMigration]:
    """一次性解析 migration 清单并验证连续版本与内容摘要。"""
    prepared: list[PreparedMigration] = []
    seen_versions: set[int] = set()
    seen_names: set[str] = set()
    for raw_path in sorted(paths, key=lambda candidate: candidate.name):
        path = Path(raw_path)
        match = re.fullmatch(r"(\d{3})_[A-Za-z0-9_.-]+\.sql", path.name)
        if match is None:
            raise MigrationHistoryError(
                f"invalid migration filename: {path.name}"
            )
        version = int(match.group(1))
        if version in seen_versions or path.name in seen_names:
            raise MigrationHistoryError(
                f"duplicate migration version or filename: {path.name}"
            )
        raw_sql = path.read_bytes()
        try:
            statements = split_sql_statements(
                raw_sql.decode("utf-8")
            )
        except (UnicodeDecodeError, MigrationSQLParseError) as exc:
            raise MigrationSQLParseError(
                f"{path.name}: {exc}"
            ) from exc
        if not statements:
            raise MigrationSQLParseError(
                f"{path.name}: migration contains no statements"
            )
        prepared.append(
            PreparedMigration(
                version=version,
                path=path,
                sha256=hashlib.sha256(raw_sql).hexdigest(),
                statements=tuple(statements),
            )
        )
        seen_versions.add(version)
        seen_names.add(path.name)

    versions = [migration.version for migration in prepared]
    if versions and versions != list(range(1, max(versions) + 1)):
        raise MigrationHistoryError(
            "migration files must form a contiguous prefix starting at 001; "
            f"found {versions}"
        )
    return prepared


def validate_release_migration_manifest(
    paths: Iterable[Path],
    *,
    manifest_path: Path | None = None,
) -> list[PreparedMigration]:
    """验证 release SQL 集合与受版本控制的 checksum 清单完全一致。"""
    path = Path(manifest_path or RELEASE_MIGRATION_MANIFEST_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationHistoryError(
            f"release migration manifest cannot be read: {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "migrations",
    }:
        raise MigrationHistoryError(
            "release migration manifest must contain exactly "
            "schema_version and migrations"
        )
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != RELEASE_MIGRATION_MANIFEST_SCHEMA_VERSION
    ):
        raise MigrationHistoryError(
            "release migration manifest schema_version mismatch: "
            f"{schema_version!r}"
        )
    raw_entries = payload["migrations"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise MigrationHistoryError(
            "release migration manifest migrations must be a nonempty list"
        )

    expected: list[tuple[int, str, str]] = []
    for offset, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "version",
            "filename",
            "sha256",
        }:
            raise MigrationHistoryError(
                "release migration manifest entry must contain exactly "
                f"version, filename and sha256: offset={offset}"
            )
        version = raw_entry["version"]
        filename = raw_entry["filename"]
        sha256 = raw_entry["sha256"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            or not isinstance(filename, str)
            or re.fullmatch(
                rf"{version:03d}_[A-Za-z0-9_.-]+\.sql",
                filename,
            )
            is None
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise MigrationHistoryError(
                "release migration manifest entry is invalid: "
                f"offset={offset}"
            )
        expected.append((version, filename, sha256))

    expected_versions = list(range(1, len(expected) + 1))
    if [version for version, _filename, _sha256 in expected] != (
        expected_versions
    ):
        raise MigrationHistoryError(
            "release migration manifest versions must be the exact "
            f"contiguous 001..{len(expected):03d} prefix"
        )
    expected_names = [filename for _version, filename, _sha256 in expected]
    raw_paths = sorted(
        (Path(raw_path) for raw_path in paths),
        key=lambda migration_path: migration_path.name,
    )
    observed_names = [migration_path.name for migration_path in raw_paths]
    if observed_names != expected_names:
        missing = sorted(set(expected_names) - set(observed_names))
        unknown = sorted(set(observed_names) - set(expected_names))
        raise MigrationHistoryError(
            "release migration manifest file set mismatch: "
            f"missing={missing} unknown={unknown}"
        )

    prepared = prepare_migration_files(raw_paths)
    observed = [
        (migration.version, migration.path.name, migration.sha256)
        for migration in prepared
    ]
    if observed != expected:
        drift = [
            {
                "version": expected_row[0],
                "filename": expected_row[1],
                "expected_sha256": expected_row[2],
                "observed_sha256": observed_row[2],
            }
            for expected_row, observed_row in zip(expected, observed)
            if expected_row != observed_row
        ]
        raise MigrationHistoryError(
            f"release migration manifest checksum drift: {drift}"
        )
    return prepared


def select_pending_migrations(
    manifest: Iterable[PreparedMigration],
    history_rows: Iterable[Mapping[str, object]],
) -> list[PreparedMigration]:
    """验证历史 checksum/连续性并只返回真正 pending 的后缀。"""
    migrations = list(manifest)
    by_version = {migration.version: migration for migration in migrations}
    history = sorted(
        history_rows,
        key=lambda row: int(row["version"]),
    )
    seen_versions: set[int] = set()
    applied_versions: list[int] = []
    for row in history:
        version = int(row["version"])
        if version in seen_versions:
            raise MigrationHistoryError(
                f"duplicate migration history version: {version:03d}"
            )
        seen_versions.add(version)
        migration = by_version.get(version)
        if migration is None:
            raise MigrationHistoryError(
                f"unknown migration history version: {version:03d}"
            )
        filename = str(row.get("filename") or "")
        if filename != migration.path.name:
            raise MigrationHistoryError(
                "migration history filename drift for "
                f"{version:03d}: {filename!r}"
            )
        checksum = str(row.get("sha256") or "").lower()
        if checksum != migration.sha256:
            raise MigrationHistoryError(
                "migration checksum drift for "
                f"{migration.path.name}: expected {migration.sha256}, "
                f"found {checksum}"
            )
        state = str(row.get("state") or "").upper()
        if state == "APPLYING":
            raise MigrationHistoryError(
                "migration history contains APPLYING row; implicit-commit "
                f"state requires inspection before retry: {filename}"
            )
        if state != "APPLIED":
            raise MigrationHistoryError(
                f"invalid migration history state {state!r}: {filename}"
            )
        applied_versions.append(version)

    expected_prefix = list(range(1, len(applied_versions) + 1))
    if applied_versions != expected_prefix:
        raise MigrationHistoryError(
            "applied migration history must be a contiguous prefix; "
            f"found {applied_versions}"
        )
    return migrations[len(applied_versions) :]
