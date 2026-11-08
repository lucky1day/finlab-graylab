"""APPLYING 恢复的纯状态判定与摘要，不执行迁移。"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping
from migrations.recovery_orchestration import RecoveryOrchestrationSpec, SPEC_025
from migrations.platform_confidence_025 import classify_prediction_schema
from migrations.auth_022 import classify_auth_schema
from migrations.auth_profile_023 import classify_auth_profile_schema
from migrations.scheme_prediction_fact_024 import classify_scheme_prediction_fact_state
from migrations._contracts import (
    AUTH_MIGRATION_FILENAME,
    AUTH_MIGRATION_SHA256,
    AUTH_MIGRATION_VERSION,
    AUTH_PROFILE_MIGRATION_FILENAME,
    AUTH_PROFILE_MIGRATION_SHA256,
    AUTH_PROFILE_MIGRATION_VERSION,
    DAILY_LEDGER_MIGRATION_FILENAME,
    DAILY_LEDGER_MIGRATION_SHA256,
    DAILY_LEDGER_MIGRATION_VERSION,
    MigrationHistoryError,
    MigrationPreflightError,
    PreparedMigration,
    REGISTRY_OWNER_MIGRATION_FILENAME,
    REGISTRY_OWNER_MIGRATION_SHA256,
    REGISTRY_OWNER_MIGRATION_VERSION,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256,
    SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION,
    SCHEME_PREDICTION_FACT_MIGRATION_FILENAME,
    SCHEME_PREDICTION_FACT_MIGRATION_SHA256,
    SCHEME_PREDICTION_FACT_MIGRATION_VERSION,
    SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME,
    SERVING_POINTER_RETIREMENT_MIGRATION_SHA256,
    SERVING_POINTER_RETIREMENT_MIGRATION_VERSION,
)
from migrations._manifest import (
    select_pending_migrations,
)
from migrations._schema import (
    _classify_registry_owner_recovery_state,
    _classify_schedule_run_started_at_shape,
    _classify_serving_pointer_retirement_state,
    _registry_owner_authority,
    validate_daily_ledger_schema_fingerprint,
    validate_daily_ledger_upgrade_state,
)


def _canonical_migration_state_value(value: object) -> object:
    """把 inspect 状态转为跨进程稳定的 JSON 值。"""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_migration_state_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (set, frozenset)):
        return sorted(
            _canonical_migration_state_value(item)
            for item in value
        )
    if isinstance(value, (list, tuple)):
        return [
            _canonical_migration_state_value(item)
            for item in value
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _applying_017_state_digest(state: Mapping[str, object]) -> str:
    """计算 inspect canonical safety state 的 SHA-256。"""
    payload = json.dumps(
        _canonical_migration_state_value(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_applying_017_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..016 APPLIED + 017 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= DAILY_LEDGER_MIGRATION_VERSION
    ]
    if [migration.version for migration in migrations] != list(
        range(1, DAILY_LEDGER_MIGRATION_VERSION + 1)
    ):
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..017"
        )
    target = migrations[-1]
    if (
        target.path.name != DAILY_LEDGER_MIGRATION_FILENAME
        or target.sha256 != DAILY_LEDGER_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 017 recovery identity/checksum is not the "
            "reviewed daily ledger migration"
        )

    rows = sorted(history, key=lambda row: int(row["version"]))
    versions = [int(row["version"]) for row in rows]
    if versions != list(range(1, DAILY_LEDGER_MIGRATION_VERSION + 1)):
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            f"001..017 prefix; found {versions}"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == DAILY_LEDGER_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 017 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 017 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_017_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    upgrade_state: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 017 三态检查及 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= DAILY_LEDGER_MIGRATION_VERSION
        ],
        "history": history_rows,
        "upgrade_state": dict(upgrade_state),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_017_history(
            migrations,
            history_rows,
        )
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        validate_daily_ledger_upgrade_state(upgrade_state)
        fingerprint = upgrade_state.get("fingerprint", {})
        assert isinstance(fingerprint, Mapping)
        try:
            validate_daily_ledger_schema_fingerprint(
                fingerprint,
                allow_missing=False,
            )
        except MigrationPreflightError:
            classification = "COMPATIBLE_PARTIAL"
        else:
            classification = "COMPLETE"
    except (
        AssertionError,
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _validate_applying_018_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..017 APPLIED + 018 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
    ]
    expected_versions = list(
        range(1, SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION + 1)
    )
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..018"
        )
    target = migrations[-1]
    if (
        target.path.name != SCHEDULE_RUN_STARTED_AT_MIGRATION_FILENAME
        or target.sha256 != SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 018 recovery identity/checksum is not the "
            "reviewed schedule-run timestamp migration"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            "001..018 prefix"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 018 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0)
        for row in rows
    ]
    fresh_history = [0] * 18
    legacy_bootstrap_history = [1] * 16 + [0, 0]
    if tuple(bootstrap_flags) not in {
        tuple(fresh_history),
        tuple(legacy_bootstrap_history),
    }:
        raise MigrationHistoryError(
            "migration 018 APPLYING history has an invalid baseline "
            f"bootstrap pattern: {bootstrap_flags}"
        )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 018 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_018_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    started_at_shape: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 018 两态检查及 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version
            <= SCHEDULE_RUN_STARTED_AT_MIGRATION_VERSION
        ],
        "history": history_rows,
        "started_at_shape": dict(started_at_shape),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_018_history(
            migrations,
            history_rows,
        )
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = _classify_schedule_run_started_at_shape(
            started_at_shape
        )
    except (
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _validate_applying_021_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..020 APPLIED + 021 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= REGISTRY_OWNER_MIGRATION_VERSION
    ]
    expected_versions = list(range(1, REGISTRY_OWNER_MIGRATION_VERSION + 1))
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files 001..021"
        )
    target = migrations[-1]
    if (
        target.path.name != REGISTRY_OWNER_MIGRATION_FILENAME
        or target.sha256 != REGISTRY_OWNER_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 021 recovery identity/checksum is not reviewed"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact 001..021 prefix"
        )
    by_version = {item.version: item for item in migrations}
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if (
            str(row.get("filename") or "") != migration.path.name
            or str(row.get("sha256") or "").lower() != migration.sha256
        ):
            raise MigrationHistoryError(
                f"migration history identity drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == REGISTRY_OWNER_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 021 may be APPLYING"
            )
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0) for row in rows
    ]
    if tuple(bootstrap_flags) not in {
        tuple([0] * REGISTRY_OWNER_MIGRATION_VERSION),
        tuple([1] * 16 + [0] * 5),
    }:
        raise MigrationHistoryError(
            "migration 021 APPLYING history has an invalid bootstrap pattern"
        )
    return target


def build_applying_021_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    registry_state: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 021 状态及 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= REGISTRY_OWNER_MIGRATION_VERSION
        ],
        "history": history_rows,
        "registry_state": dict(registry_state),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        if not str(database_identity.get("database_name") or "").strip():
            raise MigrationHistoryError("database name is required")
        if not str(database_identity.get("server_uuid") or "").strip():
            raise MigrationHistoryError("MySQL server UUID is required")
        target = _validate_applying_021_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = _classify_registry_owner_recovery_state(
            registry_state,
            authority=_registry_owner_authority(target),
        )
    except (
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _recovery_target(
    manifest: Iterable[PreparedMigration],
    spec: RecoveryOrchestrationSpec,
) -> PreparedMigration:
    """从受校验 manifest 中取得唯一受审恢复目标。"""
    candidates = [
        migration
        for migration in manifest
        if migration.version == spec.version
    ]
    if len(candidates) != 1:
        raise MigrationHistoryError(spec.target_count_error)
    target = candidates[0]
    if target.path.name != spec.filename or target.sha256 != spec.sha256:
        raise MigrationHistoryError(spec.target_identity_error)
    return target


def _validate_applying_019_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..018 APPLIED + 019 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
    ]
    expected_versions = list(
        range(1, SERVING_POINTER_RETIREMENT_MIGRATION_VERSION + 1)
    )
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files "
            "001..019"
        )
    target = migrations[-1]
    if (
        target.path.name != SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME
        or target.sha256 != SERVING_POINTER_RETIREMENT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 019 recovery identity/checksum is not the "
            "reviewed serving-pointer retirement migration"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact contiguous "
            "001..019 prefix"
        )
    by_version = {
        migration.version: migration for migration in migrations
    }
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if str(row.get("filename") or "") != migration.path.name:
            raise MigrationHistoryError(
                f"migration history filename drift for {version:03d}"
            )
        if str(row.get("sha256") or "").lower() != migration.sha256:
            raise MigrationHistoryError(
                f"migration history checksum drift for {version:03d}"
            )
        expected_state = (
            "APPLYING"
            if version == SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
            else "APPLIED"
        )
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError(
                "only migration 019 may be APPLYING; "
                f"version {version:03d} is {row.get('state')!r}"
            )
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0) for row in rows
    ]
    fresh_history = [0] * SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
    legacy_bootstrap_history = [1] * 16 + [0, 0, 0]
    if tuple(bootstrap_flags) not in {
        tuple(fresh_history),
        tuple(legacy_bootstrap_history),
    }:
        raise MigrationHistoryError(
            "migration 019 APPLYING history has an invalid baseline "
            f"bootstrap pattern: {bootstrap_flags}"
        )
    if int(rows[-1].get("baseline_bootstrap") or 0) != 0:
        raise MigrationHistoryError(
            "migration 019 APPLYING row cannot be a baseline bootstrap"
        )
    return target


def build_applying_019_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    pointer_state: Mapping[str, object],
) -> dict[str, object]:
    """构造只读 APPLYING 019 source/target/unsafe 检查及 digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= SERVING_POINTER_RETIREMENT_MIGRATION_VERSION
        ],
        "history": history_rows,
        "pointer_state": dict(pointer_state),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        database_name = str(
            database_identity.get("database_name") or ""
        ).strip()
        server_uuid = str(
            database_identity.get("server_uuid") or ""
        ).strip()
        if not database_name or not server_uuid:
            raise MigrationHistoryError(
                "database name and MySQL server UUID are required"
            )
        target = _validate_applying_019_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = _classify_serving_pointer_retirement_state(
            pointer_state
        )
    except (
        KeyError,
        MigrationHistoryError,
        MigrationPreflightError,
        TypeError,
        ValueError,
    ) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _validate_applying_022_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..021 APPLIED + 022 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= AUTH_MIGRATION_VERSION
    ]
    expected_versions = list(range(1, AUTH_MIGRATION_VERSION + 1))
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files 001..022"
        )
    target = migrations[-1]
    if (
        target.path.name != AUTH_MIGRATION_FILENAME
        or target.sha256 != AUTH_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 022 recovery identity/checksum is not reviewed"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact 001..022 prefix"
        )
    by_version = {migration.version: migration for migration in migrations}
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if (
            str(row.get("filename") or "") != migration.path.name
            or str(row.get("sha256") or "").lower() != migration.sha256
        ):
            raise MigrationHistoryError(
                f"migration history identity drift for {version:03d}"
            )
        expected_state = "APPLYING" if version == 22 else "APPLIED"
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError("only migration 022 may be APPLYING")
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0) for row in rows
    ]
    if tuple(bootstrap_flags) not in {
        tuple([0] * AUTH_MIGRATION_VERSION),
        tuple([1] * 16 + [0] * 6),
    }:
        raise MigrationHistoryError(
            "migration 022 APPLYING history has an invalid bootstrap pattern"
        )
    return target


def build_applying_022_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    auth_schema: Mapping[str, object],
) -> dict[str, object]:
    """构造认证表 partial-apply 分类与 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= AUTH_MIGRATION_VERSION
        ],
        "history": history_rows,
        "auth_schema": dict(auth_schema),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        if not str(database_identity.get("database_name") or "").strip():
            raise MigrationHistoryError("database name is required")
        if not str(database_identity.get("server_uuid") or "").strip():
            raise MigrationHistoryError("MySQL server UUID is required")
        target = _validate_applying_022_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = classify_auth_schema(auth_schema)
        if classification == "UNSAFE":
            reason = "authentication schema definition drift"
    except (KeyError, MigrationHistoryError, TypeError, ValueError) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _validate_applying_023_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..022 APPLIED + 023 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= AUTH_PROFILE_MIGRATION_VERSION
    ]
    expected_versions = list(range(1, AUTH_PROFILE_MIGRATION_VERSION + 1))
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files 001..023"
        )
    target = migrations[-1]
    if (
        target.path.name != AUTH_PROFILE_MIGRATION_FILENAME
        or target.sha256 != AUTH_PROFILE_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 023 recovery identity/checksum is not reviewed"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact 001..023 prefix"
        )
    by_version = {migration.version: migration for migration in migrations}
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if (
            str(row.get("filename") or "") != migration.path.name
            or str(row.get("sha256") or "").lower() != migration.sha256
        ):
            raise MigrationHistoryError(
                f"migration history identity drift for {version:03d}"
            )
        expected_state = "APPLYING" if version == 23 else "APPLIED"
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError("only migration 023 may be APPLYING")
    bootstrap_flags = [
        int(row.get("baseline_bootstrap") or 0) for row in rows
    ]
    if tuple(bootstrap_flags) not in {
        tuple([0] * AUTH_PROFILE_MIGRATION_VERSION),
        tuple([1] * 16 + [0] * 7),
    }:
        raise MigrationHistoryError(
            "migration 023 APPLYING history has an invalid bootstrap pattern"
        )
    return target


def build_applying_023_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    auth_profile_schema: Mapping[str, object],
) -> dict[str, object]:
    """构造用户资料 migration 状态与 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= AUTH_PROFILE_MIGRATION_VERSION
        ],
        "history": history_rows,
        "auth_profile_schema": dict(auth_profile_schema),
    }
    digest = _applying_017_state_digest(canonical_state)
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        if not str(database_identity.get("database_name") or "").strip():
            raise MigrationHistoryError("database name is required")
        if not str(database_identity.get("server_uuid") or "").strip():
            raise MigrationHistoryError("MySQL server UUID is required")
        target = _validate_applying_023_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = classify_auth_profile_schema(auth_profile_schema)
        if classification == "UNSAFE":
            reason = "authentication profile schema definition drift"
    except (KeyError, MigrationHistoryError, TypeError, ValueError) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": digest,
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def _validate_applying_024_history(
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
) -> PreparedMigration:
    """只接受 001..023 APPLIED + 024 APPLYING 的精确连续历史。"""
    migrations = [
        migration
        for migration in manifest
        if migration.version <= SCHEME_PREDICTION_FACT_MIGRATION_VERSION
    ]
    expected_versions = list(
        range(1, SCHEME_PREDICTION_FACT_MIGRATION_VERSION + 1)
    )
    if [migration.version for migration in migrations] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery requires contiguous migration files 001..024"
        )
    target = migrations[-1]
    if (
        target.path.name != SCHEME_PREDICTION_FACT_MIGRATION_FILENAME
        or target.sha256 != SCHEME_PREDICTION_FACT_MIGRATION_SHA256
    ):
        raise MigrationHistoryError(
            "migration 024 recovery identity/checksum is not reviewed"
        )
    rows = sorted(history, key=lambda row: int(row["version"]))
    if [int(row["version"]) for row in rows] != expected_versions:
        raise MigrationHistoryError(
            "APPLYING recovery history must be the exact 001..024 prefix"
        )
    by_version = {migration.version: migration for migration in migrations}
    for row in rows:
        version = int(row["version"])
        migration = by_version[version]
        if (
            str(row.get("filename") or "") != migration.path.name
            or str(row.get("sha256") or "").lower() != migration.sha256
        ):
            raise MigrationHistoryError(
                f"migration history identity drift for {version:03d}"
            )
        expected_state = "APPLYING" if version == 24 else "APPLIED"
        if str(row.get("state") or "").upper() != expected_state:
            raise MigrationHistoryError("only migration 024 may be APPLYING")
    bootstrap_flags = tuple(
        int(row.get("baseline_bootstrap") or 0) for row in rows
    )
    if bootstrap_flags not in {
        tuple([0] * 24),
        tuple([1] * 16 + [0] * 8),
    }:
        raise MigrationHistoryError(
            "migration 024 APPLYING history has an invalid bootstrap pattern"
        )
    return target


def build_applying_024_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    fact_state: Mapping[str, object],
) -> dict[str, object]:
    """构造单一产品事实源 migration 状态与 canonical digest。"""
    migrations = list(manifest)
    history_rows = sorted(
        (dict(row) for row in history),
        key=lambda row: int(row["version"]),
    )
    canonical_state = {
        "database_identity": dict(database_identity),
        "manifest": [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
            }
            for migration in migrations
            if migration.version <= SCHEME_PREDICTION_FACT_MIGRATION_VERSION
        ],
        "history": history_rows,
        "scheme_prediction_fact_state": dict(fact_state),
    }
    classification = "UNSAFE"
    reason: str | None = None
    migration_identity: dict[str, object] | None = None
    try:
        if not str(database_identity.get("database_name") or "").strip():
            raise MigrationHistoryError("database name is required")
        if not str(database_identity.get("server_uuid") or "").strip():
            raise MigrationHistoryError("MySQL server UUID is required")
        target = _validate_applying_024_history(migrations, history_rows)
        migration_identity = {
            "version": target.version,
            "filename": target.path.name,
            "sha256": target.sha256,
        }
        classification = classify_scheme_prediction_fact_state(fact_state)
        if classification == "UNSAFE":
            reason = "scheme prediction fact schema/data drift"
    except (KeyError, MigrationHistoryError, TypeError, ValueError) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": _applying_017_state_digest(canonical_state),
        "database_identity": dict(database_identity),
        "migration": migration_identity,
        "reason": reason,
    }


def build_applying_025_inspection(
    *,
    manifest: Iterable[PreparedMigration],
    history: Iterable[Mapping[str, object]],
    database_identity: Mapping[str, object],
    prediction_schema: Mapping[str, object],
) -> dict[str, object]:
    """绑定 025 精确历史、目标库与两表结构，不绑定会正常增长的业务数据。"""
    migrations = [m for m in manifest if m.version <= SPEC_025.version]
    rows = sorted((dict(row) for row in history), key=lambda row: int(row["version"]))
    canonical = {
        "database_identity": dict(database_identity),
        "manifest": [(m.version, m.path.name, m.sha256) for m in migrations],
        "history": rows,
        "prediction_schema": dict(prediction_schema),
    }
    classification, reason, identity = "UNSAFE", None, None
    try:
        if not all(database_identity.get(key) for key in ("database_name", "server_uuid")):
            raise MigrationHistoryError("database name and server UUID are required")
        if [m.version for m in migrations] != list(range(1, 26)):
            raise MigrationHistoryError("recovery requires migration files 001..025")
        if [int(row["version"]) for row in rows] != list(range(1, 26)):
            raise MigrationHistoryError("recovery history must be the exact 001..025 prefix")
        target = _recovery_target(migrations, SPEC_025)
        select_pending_migrations(migrations[:24], rows[:24])
        last = rows[-1]
        if (last.get("state"), last.get("filename"), last.get("sha256")) != (
            "APPLYING", target.path.name, target.sha256
        ):
            raise MigrationHistoryError("migration 025 APPLYING identity/checksum drift")
        if tuple(int(row.get("baseline_bootstrap") or 0) for row in rows) not in {
            (0,) * 25, (1,) * 16 + (0,) * 9
        }:
            raise MigrationHistoryError("migration 025 bootstrap pattern drift")
        identity = {"version": 25, "filename": target.path.name, "sha256": target.sha256}
        classification = classify_prediction_schema(prediction_schema)
        if classification == "UNSAFE":
            reason = "prediction schema drift outside the two authorized confidence columns"
    except (KeyError, MigrationHistoryError, TypeError, ValueError) as exc:
        reason = str(exc)
    return {
        "classification": classification,
        "state_digest": _applying_017_state_digest(canonical),
        "database_identity": dict(database_identity),
        "migration": identity,
        "reason": reason,
    }
