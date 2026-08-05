"""从当前 Mac、DB、worktree 与环境重算容量准入 candidate。

这是启动、operator admission 和 occurrence 冻结前的高成本校验，不得放入
30 秒 heartbeat。模块只读 DB/文件/命令输出，不写库、不修改 admission。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from sqlalchemy import text

from scheduler.capacity_attestation import (
    COMPONENT_DIGEST_FIELDS,
    ValidatedCapacityCandidate,
    build_capacity_candidate,
    build_capacity_target,
    canonical_artifact_set_sha256,
    canonical_json_bytes,
    content_sha256,
)
from scheduler.daily_policy import DailySchedulerPolicy, load_daily_policy
from scheduler.discovery import (
    SchemeConfig,
    discover_schemes,
)
from shared.daily_coordinator_mode import daily_control_plane_artifacts


CommandRunner = Callable[[tuple[str, ...]], bytes]

SERVICE_ENV_NAME = "bond_factor_lab_service"
NATIVE_ENV_NAME = "forecast_env"
LEDGER_SCHEMA_VERSION = "daily-ledger-schema-v018"
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024

SCHEDULER_RELEASE_ROOTS = (
    "scheduler",
    "shared",
    "backend",
)
SCHEDULER_RELEASE_REQUIRED_FILES = frozenset(
    {
        "scheduler/capacity_attestation.py",
        "scheduler/capacity_candidate_runtime.py",
        "scheduler/daily_policy.py",
        "scheduler/discovery.py",
        "scheduler/daily_runtime.py",
        "scheduler/repository.py",
        "shared/input_artifacts.py",
        "backend/main.py",
    }
)
MIGRATION_FILES = (
    "001_init.sql",
    "002_backtests.sql",
    "003_target_registry.sql",
    "004_weekly_actuals.sql",
    "005_lifecycle.sql",
    "006_predictions_runid_uk.sql",
    "007_backtest_immutable.sql",
    "008_predictions_target_uk.sql",
    "009_backtest_latest_view.sql",
    "010_prediction_semantics.sql",
    "011_registry_per_tenor.sql",
    "012_registry_task_type.sql",
    "013_add_1y_target_registry.sql",
    "014_weekly_average_actuals.sql",
    "015_monthly_actuals.sql",
    "016_dual_runtime.sql",
    "017_daily_schedule_ledger.sql",
    "018_schedule_run_started_at_nullable.sql",
    "019_retire_scheme_serving_pointer.sql",
)
RUNTIME_PROFILE_PATH = "deploy/blackbox_v2/runtime_profile_v1.json"
NATIVE_EXPORTER_FILES = (
    "shared/native_input_generation.py",
    "shared/input_artifacts.py",
    "shared/data_service.py",
)
DATABRIDGE_EXPORTER_FILES = (
    "shared/databridge_input_generation.py",
    "shared/data_bridge/refresh.py",
    "shared/data_bridge/validation.py",
    "shared/input_artifacts.py",
    "shared/blackbox_v2/data_bridge_v1_schema.json",
)
CACHE_ADAPTER_SHARED_FILES = (
    "shared/liwei_0616_phase_a_cache.py",
    "shared/liwei_0616_cache_contract.py",
)

_REGISTRY_VERSION_FIELDS = frozenset(
    {
        "registry_scheme_id",
        "base_scheme_id",
        "registry_runtime_type",
        "frequency",
        "task_type",
        "target_tenor",
        "horizon",
        "registry_status",
        "scheme_version",
        "version_runtime_type",
        "code_hash",
        "config_hash",
        "manifest_hash",
        "version_status",
        "algorithm_version",
        "contract_version",
        "runtime_profile",
        "environment_fingerprint",
        "data_snapshot_id",
    }
)
_CRITICAL_TABLES = (
    "t_input_generations",
    "t_schedule_occurrences",
    "t_schedule_items",
    "t_schedule_item_targets",
    "t_scheme_runs",
)
_REQUIRED_LEDGER_COLUMNS = {
    "t_input_generations": {
        "generation_id",
        "state",
        "manifest_sha256",
    },
    "t_schedule_occurrences": {
        "occurrence_id",
        "policy_sha256",
        "registry_digest",
        "sla_outcome",
    },
    "t_schedule_items": {
        "item_id",
        "current_run_id",
        "attempt_no",
        "state",
        "input_generation_id",
    },
    "t_schedule_item_targets": {
        "target_id",
        "registry_scheme_id",
        "accepted_run_id",
        "status",
    },
    "t_scheme_runs": {
        "run_id",
        "schedule_item_id",
        "attempt_no",
        "status",
        "failure_code",
        "started_at",
    },
}
_REQUIRED_LEDGER_INDEXES = {
    ("t_schedule_occurrences", "uk_schedule_occurrence"),
    ("t_schedule_items", "uk_schedule_item"),
    ("t_schedule_item_targets", "uk_schedule_target_registry"),
    ("t_schedule_item_targets", "uk_schedule_target_item"),
    ("t_scheme_runs", "uk_scheme_runs_schedule_attempt"),
    ("t_scheme_runs", "uk_scheme_runs_execution_token"),
}
_REQUIRED_LEDGER_FOREIGN_KEYS = {
    ("t_schedule_items", "fk_schedule_item_occurrence"),
    ("t_schedule_item_targets", "fk_schedule_target_item"),
    ("t_scheme_runs", "fk_scheme_run_schedule_item"),
}
_REQUIRED_LEDGER_CHECKS = {
    (
        "t_schedule_occurrences",
        "ck_schedule_occurrence_feature_date",
    ),
    (
        "t_schedule_occurrences",
        "ck_schedule_occurrence_failure_code",
    ),
    ("t_schedule_items", "ck_schedule_item_failure_code"),
    ("t_scheme_runs", "ck_scheme_run_schedule_failure_code"),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CapacityCandidateRuntimeError(RuntimeError):
    """live candidate 无法被完整、确定地重算。"""


def build_current_capacity_candidate(
    engine: object,
    *,
    project_root: str | Path,
    policy_path: str | Path,
    discovered: Iterable[SchemeConfig] | None = None,
    command_runner: CommandRunner | None = None,
    native_env: str = NATIVE_ENV_NAME,
) -> ValidatedCapacityCandidate:
    """重算当前 live candidate，并返回 canonical payload/fingerprint。

    调用成本包含 strict discovery、全量 source file-set、三套 conda
    explicit/JSON manifest、active Registry/version 及 information_schema，
    不适用于心跳。
    """

    if native_env != NATIVE_ENV_NAME:
        raise CapacityCandidateRuntimeError(
            "ledger Native algorithm environment must be "
            f"{NATIVE_ENV_NAME!r}; found {native_env!r}"
        )
    root = _validated_project_root(Path(project_root))
    policy_file = _project_file(
        root,
        Path(policy_path),
        "daily policy",
    )
    runner = command_runner or _default_command_runner

    policy_bytes = _read_source_file(policy_file, "daily policy")
    _parse_json_object(policy_bytes, "daily policy")
    current_active_daily = tuple(
        config
        for config in discover_schemes(
            root / "schemes",
            strict=True,
        )
        if config.status == "active" and config.frequency == "daily"
    )
    policy = load_daily_policy(
        policy_file,
        discovered=current_active_daily,
    )
    current_discovered = tuple(
        config
        for config in current_active_daily
        if config.scheme_id in policy.schemes
    )
    if discovered is not None:
        supplied = tuple(
            config
            for config in discovered
            if config.status == "active"
            and config.frequency == "daily"
            and config.scheme_id in policy.schemes
        )
        _require_same_discovery(supplied, current_discovered)
    if _read_source_file(policy_file, "daily policy") != policy_bytes:
        raise CapacityCandidateRuntimeError(
            "daily policy changed while candidate was being built"
        )

    profile_path = root / RUNTIME_PROFILE_PATH
    profile_bytes = _read_source_file(
        profile_path,
        "Blackbox runtime profile",
    )
    profile = _load_and_validate_runtime_profile(
        profile_path,
        profile_bytes,
    )
    _validate_profile_bindings(
        current_discovered,
        profile_name=profile.name,
        conda_env=profile.conda_env,
        data_schema_version=profile.data_schema_version,
        contract_version=profile.contract_version,
    )

    db_snapshot = _read_database_snapshot(engine)
    (
        target_manifest,
        scheme_bundle_artifacts,
    ) = _build_target_manifest(
        root,
        current_discovered,
        policy,
        db_snapshot["registry_versions"],
    )

    machine = _read_machine_identity(runner)
    if profile.memory_limit_bytes > machine["memory_bytes"]:
        raise CapacityCandidateRuntimeError(
            "Blackbox runtime profile memory exceeds this Mac"
        )
    environment_artifacts = _read_environment_manifests(
        runner,
        blackbox_env=profile.conda_env,
        native_env=native_env,
    )

    component_artifacts: dict[str, Mapping[str, str]] = {
        "scheduler_release_sha256":
            _collect_scheduler_release_artifacts(root),
        "scheme_bundle_sha256": scheme_bundle_artifacts,
        "service_environment_sha256":
            environment_artifacts["service"],
        "algorithm_environment_sha256":
            environment_artifacts["algorithm"],
        "runtime_profile_sha256": {
            RUNTIME_PROFILE_PATH: content_sha256(profile_bytes)
        },
        "migration_set_sha256": _collect_migration_artifacts(root),
        "native_exporter_sha256": _collect_fixed_artifacts(
            root,
            NATIVE_EXPORTER_FILES,
            "Native exporter",
        ),
        "databridge_exporter_sha256": _collect_fixed_artifacts(
            root,
            DATABRIDGE_EXPORTER_FILES,
            "DataBridge exporter",
        ),
        "database_identity_sha256": db_snapshot["identity_artifacts"],
        "control_plane_identity_sha256":
            daily_control_plane_artifacts(),
    }
    if set(component_artifacts) != set(COMPONENT_DIGEST_FIELDS):
        raise CapacityCandidateRuntimeError(
            "capacity component artifact set is incomplete"
        )

    return build_capacity_candidate(
        machine_id=machine["machine_id"],
        hardware_model=machine["hardware_model"],
        os_build=machine["os_build"],
        memory_bytes=machine["memory_bytes"],
        policy_version=policy.version,
        policy_bytes=policy_bytes,
        target_manifest=target_manifest,
        component_artifacts=component_artifacts,
    )


def _read_database_snapshot(engine: object) -> dict[str, object]:
    dialect = getattr(getattr(engine, "dialect", None), "name", None)
    if dialect != "mysql":
        raise CapacityCandidateRuntimeError(
            "capacity candidate requires MySQL"
        )
    try:
        with engine.begin() as connection:
            registry_versions = _mapping_rows(
                connection.execute(
                    text(
                        """
                        SELECT
                            r.scheme_id AS registry_scheme_id,
                            r.base_scheme_id AS base_scheme_id,
                            r.runtime_type AS registry_runtime_type,
                            r.frequency AS frequency,
                            r.task_type AS task_type,
                            r.target_tenor AS target_tenor,
                            r.horizon AS horizon,
                            r.status AS registry_status,
                            v.scheme_version AS scheme_version,
                            v.runtime_type AS version_runtime_type,
                            v.code_hash AS code_hash,
                            v.config_hash AS config_hash,
                            v.manifest_hash AS manifest_hash,
                            v.status AS version_status,
                            v.algorithm_version AS algorithm_version,
                            v.contract_version AS contract_version,
                            v.runtime_profile AS runtime_profile,
                            v.environment_fingerprint
                                AS environment_fingerprint,
                            v.data_snapshot_id AS data_snapshot_id
                        FROM t_scheme_registry r
                        JOIN t_scheme_versions v
                          ON v.scheme_id = r.base_scheme_id
                         AND v.status = 'active'
                        WHERE r.status = 'active'
                          AND r.frequency = 'daily'
                        ORDER BY r.scheme_id, v.scheme_version
                        """
                    )
                )
            )
            identity = dict(
                connection.execute(
                    text(
                        """
                        SELECT
                            @@server_uuid AS server_uuid,
                            DATABASE() AS database_schema,
                            VERSION() AS server_version,
                            @@version_comment AS version_comment
                        """
                    )
                )
                .mappings()
                .one()
            )
            columns = _mapping_rows(
                connection.execute(
                    text(
                        """
                        SELECT
                            TABLE_NAME AS table_name,
                            COLUMN_NAME AS column_name,
                            ORDINAL_POSITION AS ordinal_position,
                            COLUMN_TYPE AS column_type,
                            IS_NULLABLE AS is_nullable,
                            COLUMN_DEFAULT AS column_default,
                            EXTRA AS extra
                        FROM information_schema.columns
                        WHERE table_schema = DATABASE()
                          AND table_name IN (
                            't_input_generations',
                            't_schedule_occurrences',
                            't_schedule_items',
                            't_schedule_item_targets',
                            't_scheme_runs'
                          )
                        ORDER BY table_name, ordinal_position
                        """
                    )
                )
            )
            indexes = _mapping_rows(
                connection.execute(
                    text(
                        """
                        SELECT
                            TABLE_NAME AS table_name,
                            INDEX_NAME AS index_name,
                            NON_UNIQUE AS non_unique,
                            SEQ_IN_INDEX AS seq_in_index,
                            COLUMN_NAME AS column_name,
                            COLLATION AS collation,
                            INDEX_TYPE AS index_type
                        FROM information_schema.statistics
                        WHERE table_schema = DATABASE()
                          AND table_name IN (
                            't_input_generations',
                            't_schedule_occurrences',
                            't_schedule_items',
                            't_schedule_item_targets',
                            't_scheme_runs'
                          )
                        ORDER BY table_name, index_name, seq_in_index
                        """
                    )
                )
            )
            foreign_keys = _mapping_rows(
                connection.execute(
                    text(
                        """
                        SELECT
                            TABLE_NAME AS table_name,
                            CONSTRAINT_NAME AS constraint_name,
                            COLUMN_NAME AS column_name,
                            REFERENCED_TABLE_NAME AS referenced_table_name,
                            REFERENCED_COLUMN_NAME AS referenced_column_name,
                            ORDINAL_POSITION AS ordinal_position
                        FROM information_schema.key_column_usage
                        WHERE constraint_schema = DATABASE()
                          AND referenced_table_name IS NOT NULL
                          AND table_name IN (
                            't_input_generations',
                            't_schedule_occurrences',
                            't_schedule_items',
                            't_schedule_item_targets',
                            't_scheme_runs'
                          )
                        ORDER BY table_name, constraint_name, ordinal_position
                        """
                    )
                )
            )
            checks = _mapping_rows(
                connection.execute(
                    text(
                        """
                        SELECT
                            tc.TABLE_NAME AS table_name,
                            tc.CONSTRAINT_NAME AS constraint_name,
                            cc.CHECK_CLAUSE AS check_clause
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.check_constraints cc
                          ON cc.constraint_schema = tc.constraint_schema
                         AND cc.constraint_name = tc.constraint_name
                        WHERE tc.constraint_schema = DATABASE()
                          AND tc.constraint_type = 'CHECK'
                          AND tc.table_name IN (
                            't_input_generations',
                            't_schedule_occurrences',
                            't_schedule_items',
                            't_schedule_item_targets',
                            't_scheme_runs'
                          )
                        ORDER BY tc.table_name, tc.constraint_name
                        """
                    )
                )
            )
    except CapacityCandidateRuntimeError:
        raise
    except Exception as exc:
        raise CapacityCandidateRuntimeError(
            "capacity candidate DB snapshot failed"
        ) from exc

    identity_artifacts = _validate_database_identity(
        identity,
        columns=columns,
        indexes=indexes,
        foreign_keys=foreign_keys,
        checks=checks,
    )
    return {
        "registry_versions": registry_versions,
        "identity_artifacts": identity_artifacts,
    }


def _build_target_manifest(
    root: Path,
    configs: Sequence[SchemeConfig],
    policy: DailySchedulerPolicy,
    raw_rows: object,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    if not isinstance(raw_rows, list):
        raise CapacityCandidateRuntimeError(
            "active Registry/version query did not return rows"
        )
    config_by_id = {config.scheme_id: config for config in configs}
    if len(config_by_id) != len(configs):
        raise CapacityCandidateRuntimeError(
            "strict discovery contains duplicate daily schemes"
        )
    if set(policy.schemes) != set(config_by_id):
        raise CapacityCandidateRuntimeError(
            "policy/discovery active scheme set drift"
        )

    scheme_files: dict[
        str,
        tuple[dict[str, str], dict[str, str], dict[str, str]],
    ] = {}
    for scheme_id, config in config_by_id.items():
        scheme_files[scheme_id] = _scheme_artifacts(root, config)

    target_rows: list[dict[str, object]] = []
    bundle: dict[str, str] = {}
    registry_ids: set[str] = set()
    targets_by_base: dict[str, set[str]] = {}
    version_by_base: dict[str, bytes] = {}
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            raise CapacityCandidateRuntimeError(
                f"active Registry/version row {index} is not an object"
            )
        missing = _REGISTRY_VERSION_FIELDS - set(raw)
        if missing:
            raise CapacityCandidateRuntimeError(
                "active Registry/version row is missing fields: "
                + ",".join(sorted(missing))
            )
        registry_id = _text_value(
            raw.get("registry_scheme_id"),
            "Registry scheme_id",
        )
        base_id = _text_value(
            raw.get("base_scheme_id"),
            "Registry base_scheme_id",
        )
        if registry_id in registry_ids:
            raise CapacityCandidateRuntimeError(
                "multiple active versions or duplicate Registry target: "
                + registry_id
            )
        registry_ids.add(registry_id)
        config = config_by_id.get(base_id)
        if config is None:
            raise CapacityCandidateRuntimeError(
                f"Registry contains unknown active daily scheme: {base_id}"
            )
        item_policy = policy.schemes[base_id]
        tenor = _text_value(
            raw.get("target_tenor"),
            f"{registry_id}.target_tenor",
        )
        horizon = _positive_int_value(
            raw.get("horizon"),
            f"{registry_id}.horizon",
        )
        expected_registry_id = f"{base_id}__h{horizon}__{tenor}"
        if registry_id != expected_registry_id:
            raise CapacityCandidateRuntimeError(
                "Registry composite identity mismatch: "
                f"expected={expected_registry_id} actual={registry_id}"
            )
        expected_registry = {
            "registry_runtime_type": config.runtime_type,
            "frequency": "daily",
            "task_type": config.task_type,
            "registry_status": "active",
        }
        for field, expected in expected_registry.items():
            if raw.get(field) != expected:
                raise CapacityCandidateRuntimeError(
                    f"Registry {field} drift for {registry_id}"
                )
        if horizon != config.horizon or horizon != item_policy.horizon:
            raise CapacityCandidateRuntimeError(
                f"Registry horizon drift for {registry_id}"
            )
        if tenor not in config.tenors or tenor not in item_policy.target_tenors:
            raise CapacityCandidateRuntimeError(
                f"Registry target_tenor drift for {registry_id}"
            )

        _validate_active_version(raw, config)
        version_payload = canonical_json_bytes(
            {
                field: _json_scalar(raw.get(field), field)
                for field in sorted(
                    _REGISTRY_VERSION_FIELDS
                    - {
                        "registry_scheme_id",
                        "target_tenor",
                    }
                )
            }
        )
        previous_version = version_by_base.setdefault(
            base_id,
            version_payload,
        )
        if previous_version != version_payload:
            raise CapacityCandidateRuntimeError(
                f"inconsistent active version rows for {base_id}"
            )
        code_artifacts, config_artifacts, all_artifacts = scheme_files[
            base_id
        ]
        (
            cache_adapter_sha256,
            cache_core_sha256,
        ) = _cache_component_digests(
            root,
            config=config,
            cache_spec_fingerprint=item_policy.cache_spec_fingerprint,
            code_artifacts=code_artifacts,
        )
        target_rows.append(
            build_capacity_target(
                registry_scheme_id=registry_id,
                base_scheme_id=base_id,
                runtime_type=config.runtime_type,
                target_tenor=tenor,
                horizon=config.horizon,
                task_type=config.task_type,
                scheme_version=config.scheme_version,
                code_artifacts=code_artifacts,
                config_artifacts=config_artifacts,
                cache_group=item_policy.cache_group,
                cache_spec_fingerprint=(
                    item_policy.cache_spec_fingerprint
                ),
                cache_adapter_sha256=cache_adapter_sha256,
                cache_core_sha256=cache_core_sha256,
            )
        )
        targets_by_base.setdefault(base_id, set()).add(tenor)
        _merge_artifacts(bundle, all_artifacts, "scheme bundle")

    if set(targets_by_base) != set(config_by_id):
        raise CapacityCandidateRuntimeError(
            "Registry active daily base scheme set differs from discovery"
        )
    for base_id, config in config_by_id.items():
        if targets_by_base[base_id] != set(config.tenors):
            raise CapacityCandidateRuntimeError(
                f"Registry target set drift for {base_id}"
            )
        version_payload = version_by_base.get(base_id)
        if version_payload is None:
            raise CapacityCandidateRuntimeError(
                f"active t_scheme_versions row missing for {base_id}"
            )
        bundle[f"database-active-version/{base_id}.json"] = (
            content_sha256(version_payload)
        )
    target_rows.sort(key=lambda row: str(row["registry_scheme_id"]))
    return target_rows, bundle


def _cache_component_digests(
    root: Path,
    *,
    config: SchemeConfig,
    cache_spec_fingerprint: str | None,
    code_artifacts: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """为 cache-qualified consumer 生成独立 adapter/core 文件集摘要。"""
    if cache_spec_fingerprint is None:
        return None, None
    scheme_prefix = f"schemes/{config.scheme_id}/"
    adapter_identities = {
        f"{scheme_prefix}predict.py",
        f"{scheme_prefix}inference.py",
    }
    missing_adapter = adapter_identities - set(code_artifacts)
    if missing_adapter:
        raise CapacityCandidateRuntimeError(
            f"cache adapter files missing for {config.scheme_id}: "
            + ",".join(sorted(missing_adapter))
        )
    adapter_artifacts = {
        identity: code_artifacts[identity]
        for identity in sorted(adapter_identities)
    }
    _merge_artifacts(
        adapter_artifacts,
        _collect_fixed_artifacts(
            root,
            CACHE_ADAPTER_SHARED_FILES,
            "cache adapter",
        ),
        "cache adapter",
    )
    core_artifacts = {
        identity: digest
        for identity, digest in code_artifacts.items()
        if identity.startswith(f"{scheme_prefix}core/")
    }
    if not core_artifacts:
        raise CapacityCandidateRuntimeError(
            f"cache core files missing for {config.scheme_id}"
        )
    return (
        canonical_artifact_set_sha256(
            f"cache-adapter/{config.scheme_id}/{config.scheme_version}",
            adapter_artifacts,
        ),
        canonical_artifact_set_sha256(
            f"cache-core/{config.scheme_id}/{config.scheme_version}",
            core_artifacts,
        ),
    )


def _validate_active_version(
    row: Mapping[str, object],
    config: SchemeConfig,
) -> None:
    expected = {
        "scheme_version": config.scheme_version,
        "version_runtime_type": config.runtime_type,
        "code_hash": config.code_hash,
        "config_hash": config.config_hash,
        "manifest_hash": config.manifest_hash,
        "version_status": "active",
        "algorithm_version": config.algorithm_version,
        "contract_version": config.contract_version,
        "runtime_profile": config.runtime_profile,
    }
    for field, value in expected.items():
        actual = row.get(field)
        if actual != value:
            raise CapacityCandidateRuntimeError(
                f"active version {field} drift for {config.scheme_id}"
            )
    environment_fingerprint = row.get("environment_fingerprint")
    if environment_fingerprint is not None:
        _sha256_value(
            environment_fingerprint,
            f"{config.scheme_id}.environment_fingerprint",
        )
    data_snapshot_id = row.get("data_snapshot_id")
    if data_snapshot_id is not None:
        _text_value(
            data_snapshot_id,
            f"{config.scheme_id}.data_snapshot_id",
        )


def _scheme_artifacts(
    root: Path,
    config: SchemeConfig,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    expected_dir = root / "schemes" / config.scheme_id
    if config.path.resolve() != expected_dir.resolve():
        raise CapacityCandidateRuntimeError(
            f"discovery path escapes project for {config.scheme_id}"
        )
    config_path = expected_dir / "config.yaml"
    code_paths: list[Path]
    config_paths: list[Path] = [config_path]
    if config.runtime_type == "native_adapter":
        predict_path = expected_dir / "predict.py"
        if not predict_path.is_file():
            raise CapacityCandidateRuntimeError(
                f"Native predict.py is missing for {config.scheme_id}"
            )
        # Native 的真实 import 面不只限于 predict.py/core；当前存量方案还有
        # root-level inference.py。保守纳入方案目录中的全部 Python source，
        # 避免未被旧版 compute_code_hash 覆盖的执行代码绕过候选绑定。
        code_paths = _scan_files(expected_dir, suffixes={".py"})
        if predict_path not in code_paths:
            raise CapacityCandidateRuntimeError(
                f"Native predict.py is not in code set for {config.scheme_id}"
            )
        manifests = [
            expected_dir / name
            for name in ("manifest.yaml", "manifest.yml", "manifest.json")
            if (expected_dir / name).exists()
        ]
        if len(manifests) > 1:
            raise CapacityCandidateRuntimeError(
                f"multiple Native manifests for {config.scheme_id}"
            )
        config_paths.extend(manifests)
    elif config.runtime_type == "blackbox_v2":
        if config.delivery_script is None or config.delivery_metadata is None:
            raise CapacityCandidateRuntimeError(
                f"Blackbox delivery files missing for {config.scheme_id}"
            )
        code_paths = [config.delivery_script]
        config_paths.append(config.delivery_metadata)
    else:
        raise CapacityCandidateRuntimeError(
            f"unsupported runtime_type for {config.scheme_id}"
        )
    code_artifacts = _artifact_map(root, code_paths, "scheme code")
    config_artifacts = _artifact_map(
        root,
        config_paths,
        "scheme config",
    )
    all_artifacts = dict(code_artifacts)
    _merge_artifacts(all_artifacts, config_artifacts, "scheme files")
    return code_artifacts, config_artifacts, all_artifacts


def _collect_scheduler_release_artifacts(root: Path) -> dict[str, str]:
    paths: list[Path] = []
    for relative_root in SCHEDULER_RELEASE_ROOTS:
        source_root = root / relative_root
        if not source_root.is_dir():
            raise CapacityCandidateRuntimeError(
                f"scheduler release root is missing: {relative_root}"
            )
        paths.extend(_scan_files(source_root, suffixes={".py"}))
    artifacts = _artifact_map(root, paths, "scheduler release")
    missing = SCHEDULER_RELEASE_REQUIRED_FILES - set(artifacts)
    if missing:
        raise CapacityCandidateRuntimeError(
            "scheduler release required files missing: "
            + ",".join(sorted(missing))
        )
    return artifacts


def _collect_migration_artifacts(root: Path) -> dict[str, str]:
    migration_root = root / "migrations"
    actual = {
        path.name
        for path in _scan_files(migration_root, suffixes={".sql"})
    }
    expected = set(MIGRATION_FILES)
    if actual != expected:
        raise CapacityCandidateRuntimeError(
            "migration set must be exactly 001..019: "
            f"missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )
    return _collect_fixed_artifacts(
        root,
        tuple(f"migrations/{name}" for name in MIGRATION_FILES),
        "migration set",
    )


def _collect_fixed_artifacts(
    root: Path,
    relative_paths: Sequence[str],
    label: str,
) -> dict[str, str]:
    return _artifact_map(
        root,
        [root / relative for relative in relative_paths],
        label,
    )


def _read_environment_manifests(
    runner: CommandRunner,
    *,
    blackbox_env: str,
    native_env: str,
) -> dict[str, dict[str, str]]:
    conda = _resolve_conda_executable()
    service_prefix = Path(sys.prefix).resolve()
    if service_prefix.name != SERVICE_ENV_NAME:
        raise CapacityCandidateRuntimeError(
            "service environment is not bond_factor_lab_service"
        )
    service = _run_conda_explicit(
        runner,
        (
            str(conda),
            "list",
            "--explicit",
            "--prefix",
            str(service_prefix),
        ),
        label=SERVICE_ENV_NAME,
    )
    service_packages = _run_conda_json(
        runner,
        (
            str(conda),
            "list",
            "--json",
            "--prefix",
            str(service_prefix),
        ),
        label=SERVICE_ENV_NAME,
    )
    native = _run_conda_explicit(
        runner,
        (
            str(conda),
            "list",
            "--explicit",
            "--name",
            native_env,
        ),
        label=native_env,
    )
    native_packages = _run_conda_json(
        runner,
        (
            str(conda),
            "list",
            "--json",
            "--name",
            native_env,
        ),
        label=native_env,
    )
    blackbox = _run_conda_explicit(
        runner,
        (
            str(conda),
            "list",
            "--explicit",
            "--name",
            blackbox_env,
        ),
        label=blackbox_env,
    )
    blackbox_packages = _run_conda_json(
        runner,
        (
            str(conda),
            "list",
            "--json",
            "--name",
            blackbox_env,
        ),
        label=blackbox_env,
    )
    return {
        "service": {
            f"conda-explicit/{SERVICE_ENV_NAME}.txt":
                content_sha256(service),
            f"conda-list-json/{SERVICE_ENV_NAME}.json":
                content_sha256(service_packages),
        },
        "algorithm": {
            f"conda-explicit/{native_env}.txt":
                content_sha256(native),
            f"conda-list-json/{native_env}.json":
                content_sha256(native_packages),
            f"conda-explicit/{blackbox_env}.txt":
                content_sha256(blackbox),
            f"conda-list-json/{blackbox_env}.json":
                content_sha256(blackbox_packages),
        },
    }


def _run_conda_explicit(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    label: str,
) -> bytes:
    output = _run_command(runner, command, f"{label} conda manifest")
    try:
        decoded = output.decode("utf-8")
    except UnicodeError as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} conda explicit manifest is not UTF-8"
        ) from exc
    lines = [line.strip() for line in decoded.splitlines()]
    if "@EXPLICIT" not in lines:
        raise CapacityCandidateRuntimeError(
            f"{label} conda manifest must contain @EXPLICIT"
        )
    explicit_index = lines.index("@EXPLICIT")
    packages = [
        line
        for line in lines[explicit_index + 1 :]
        if line and not line.startswith("#")
    ]
    if not packages:
        raise CapacityCandidateRuntimeError(
            f"{label} conda explicit manifest is empty"
        )
    return output


def _run_conda_json(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    label: str,
) -> bytes:
    """返回不受 conda 输出顺序影响的完整包清单 canonical bytes。"""
    output = _run_command(runner, command, f"{label} conda package list")
    try:
        decoded = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} conda JSON package list is invalid"
        ) from exc
    if not isinstance(decoded, list) or not decoded:
        raise CapacityCandidateRuntimeError(
            f"{label} conda JSON package list must be a non-empty array"
        )
    packages: list[dict[str, object]] = []
    for index, raw_package in enumerate(decoded):
        if not isinstance(raw_package, Mapping):
            raise CapacityCandidateRuntimeError(
                f"{label} conda JSON package {index} is not an object"
            )
        package = dict(raw_package)
        for required_field in (
            "name",
            "version",
            "build_string",
            "channel",
            "platform",
        ):
            value = package.get(required_field)
            if not isinstance(value, str) or not value.strip():
                raise CapacityCandidateRuntimeError(
                    f"{label} conda JSON package {index} has invalid "
                    f"{required_field}"
                )
        packages.append(package)
    packages.sort(key=canonical_json_bytes)
    return canonical_json_bytes(packages)


def _read_machine_identity(
    runner: CommandRunner,
) -> dict[str, object]:
    ioreg = _run_command(
        runner,
        (
            "/usr/sbin/ioreg",
            "-rd1",
            "-c",
            "IOPlatformExpertDevice",
        ),
        "IOPlatformUUID",
    )
    match = re.search(
        rb'"IOPlatformUUID"\s*=\s*"([^"]+)"',
        ioreg,
    )
    if match is None:
        raise CapacityCandidateRuntimeError(
            "IOPlatformUUID is unavailable"
        )
    try:
        machine_id = match.group(1).decode("ascii").strip()
    except UnicodeError as exc:
        raise CapacityCandidateRuntimeError(
            "IOPlatformUUID is invalid"
        ) from exc
    if _UUID_RE.fullmatch(machine_id) is None:
        raise CapacityCandidateRuntimeError(
            "IOPlatformUUID is invalid"
        )
    hardware_model = _command_text(
        runner,
        ("/usr/sbin/sysctl", "-n", "hw.model"),
        "hw.model",
    )
    memory_text = _command_text(
        runner,
        ("/usr/sbin/sysctl", "-n", "hw.memsize"),
        "hw.memsize",
    )
    os_build = _command_text(
        runner,
        ("/usr/bin/sw_vers", "-buildVersion"),
        "macOS build",
    )
    try:
        memory_bytes = int(memory_text)
    except ValueError as exc:
        raise CapacityCandidateRuntimeError(
            "hw.memsize is not an integer"
        ) from exc
    if memory_bytes <= 0:
        raise CapacityCandidateRuntimeError(
            "hw.memsize must be positive"
        )
    return {
        "machine_id": _nonempty(machine_id, "IOPlatformUUID"),
        "hardware_model": hardware_model,
        "memory_bytes": memory_bytes,
        "os_build": os_build,
    }


def _validate_database_identity(
    identity: Mapping[str, object],
    *,
    columns: list[dict[str, object]],
    indexes: list[dict[str, object]],
    foreign_keys: list[dict[str, object]],
    checks: list[dict[str, object]],
) -> dict[str, str]:
    server_uuid = _text_value(identity.get("server_uuid"), "server_uuid")
    if _UUID_RE.fullmatch(server_uuid) is None:
        raise CapacityCandidateRuntimeError(
            "MySQL server_uuid is invalid"
        )
    database_schema = _text_value(
        identity.get("database_schema"),
        "database schema",
    )
    if database_schema != "bond_db":
        raise CapacityCandidateRuntimeError(
            "capacity candidate must use bond_db"
        )
    server_version = _text_value(
        identity.get("server_version"),
        "MySQL version",
    )
    version_comment = _text_value(
        identity.get("version_comment"),
        "MySQL version comment",
    )
    if "mariadb" in f"{server_version} {version_comment}".lower():
        raise CapacityCandidateRuntimeError(
            "capacity candidate requires Oracle MySQL"
        )
    version_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", server_version)
    if version_match is None or tuple(
        int(part) for part in version_match.groups()
    ) < (8, 0, 16):
        raise CapacityCandidateRuntimeError(
            "capacity candidate requires MySQL >= 8.0.16"
        )
    _validate_ledger_schema(
        columns=columns,
        indexes=indexes,
        foreign_keys=foreign_keys,
        checks=checks,
    )
    schema_definition = canonical_json_bytes(
        {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
            "checks": checks,
        }
    )
    return {
        "mysql/server_uuid.txt":
            content_sha256(server_uuid.lower().encode("ascii")),
        "mysql/database_schema.txt":
            content_sha256(database_schema.encode("utf-8")),
        "mysql/server_version.txt":
            content_sha256(server_version.encode("utf-8")),
        "mysql/version_comment.txt":
            content_sha256(version_comment.encode("utf-8")),
        "mysql/ledger_schema_version.txt":
            content_sha256(LEDGER_SCHEMA_VERSION.encode("ascii")),
        "mysql/ledger_schema_definition.json":
            content_sha256(schema_definition),
    }


def _validate_ledger_schema(
    *,
    columns: list[dict[str, object]],
    indexes: list[dict[str, object]],
    foreign_keys: list[dict[str, object]],
    checks: list[dict[str, object]],
) -> None:
    columns_by_table: dict[str, set[str]] = {}
    for row in columns:
        table = _text_value(row.get("table_name"), "schema table_name")
        column = _text_value(row.get("column_name"), "schema column_name")
        columns_by_table.setdefault(table, set()).add(column)
    for table, expected in _REQUIRED_LEDGER_COLUMNS.items():
        missing = expected - columns_by_table.get(table, set())
        if missing:
            raise CapacityCandidateRuntimeError(
                f"ledger schema {table} missing columns: "
                + ",".join(sorted(missing))
            )
    started_at_rows = [
        row
        for row in columns
        if row.get("table_name") == "t_scheme_runs"
        and row.get("column_name") == "started_at"
    ]
    if len(started_at_rows) != 1:
        raise CapacityCandidateRuntimeError(
            "ledger schema t_scheme_runs.started_at definition is missing "
            "or duplicated"
        )
    started_at = started_at_rows[0]
    observed_started_at = {
        "column_type": str(
            started_at.get("column_type") or ""
        ).lower(),
        "is_nullable": str(
            started_at.get("is_nullable") or ""
        ).lower(),
        "column_default": str(
            started_at.get("column_default") or ""
        ).lower(),
        "extra": str(started_at.get("extra") or "").lower(),
    }
    expected_started_at = {
        "column_type": "datetime(6)",
        "is_nullable": "yes",
        "column_default": "current_timestamp(6)",
        "extra": "default_generated",
    }
    if observed_started_at != expected_started_at:
        raise CapacityCandidateRuntimeError(
            "ledger schema t_scheme_runs.started_at definition drift: "
            f"{observed_started_at}"
        )
    actual_indexes = {
        (
            _text_value(row.get("table_name"), "index table_name"),
            _text_value(row.get("index_name"), "index_name"),
        )
        for row in indexes
    }
    actual_foreign_keys = {
        (
            _text_value(row.get("table_name"), "FK table_name"),
            _text_value(row.get("constraint_name"), "FK constraint_name"),
        )
        for row in foreign_keys
    }
    actual_checks = {
        (
            _text_value(row.get("table_name"), "CHECK table_name"),
            _text_value(
                row.get("constraint_name"),
                "CHECK constraint_name",
            ),
        )
        for row in checks
    }
    required_sets = (
        ("indexes", _REQUIRED_LEDGER_INDEXES, actual_indexes),
        (
            "foreign keys",
            _REQUIRED_LEDGER_FOREIGN_KEYS,
            actual_foreign_keys,
        ),
        ("checks", _REQUIRED_LEDGER_CHECKS, actual_checks),
    )
    for label, required, actual in required_sets:
        missing = required - actual
        if missing:
            raise CapacityCandidateRuntimeError(
                f"ledger schema missing {label}: {sorted(missing)}"
            )


def _load_and_validate_runtime_profile(
    path: Path,
    raw_bytes: bytes,
) -> object:
    _parse_json_object(raw_bytes, "Blackbox runtime profile")
    try:
        from scheduler.blackbox_v2_runner import _load_runtime_profile

        profile = _load_runtime_profile(path)
    except Exception as exc:
        raise CapacityCandidateRuntimeError(
            "Blackbox runtime profile is invalid"
        ) from exc
    if _read_source_file(path, "Blackbox runtime profile") != raw_bytes:
        raise CapacityCandidateRuntimeError(
            "Blackbox runtime profile changed while being read"
        )
    return profile


def _validate_profile_bindings(
    configs: Sequence[SchemeConfig],
    *,
    profile_name: str,
    conda_env: str,
    data_schema_version: str,
    contract_version: str,
) -> None:
    blackbox = [
        config
        for config in configs
        if config.runtime_type == "blackbox_v2"
    ]
    if not blackbox:
        raise CapacityCandidateRuntimeError(
            "active daily Blackbox V2 set is empty"
        )
    for config in blackbox:
        expected = {
            "runtime_profile": profile_name,
            "data_schema_version": data_schema_version,
            "contract_version": contract_version,
        }
        for field, value in expected.items():
            if getattr(config, field) != value:
                raise CapacityCandidateRuntimeError(
                    f"{config.scheme_id} {field} differs from runtime profile"
                )
    _nonempty(conda_env, "Blackbox conda_env")


def _require_same_discovery(
    supplied: Sequence[SchemeConfig],
    current: Sequence[SchemeConfig],
) -> None:
    def identity(config: SchemeConfig) -> tuple[object, ...]:
        return (
            config.scheme_id,
            config.runtime_type,
            config.task_type,
            config.horizon,
            tuple(config.tenors),
            config.scheme_version,
            config.code_hash,
            config.config_hash,
            config.manifest_hash,
            config.runtime_profile,
            config.data_schema_version,
            config.contract_version,
            config.path.resolve(),
        )

    supplied_rows = sorted(identity(config) for config in supplied)
    current_rows = sorted(identity(config) for config in current)
    if supplied_rows != current_rows:
        raise CapacityCandidateRuntimeError(
            "supplied discovery differs from strict live discovery"
        )


def _artifact_map(
    root: Path,
    paths: Iterable[Path],
    label: str,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for path in sorted(set(paths)):
        relative = _relative_project_path(root, path, label)
        if relative in artifacts:
            raise CapacityCandidateRuntimeError(
                f"{label} contains duplicate path: {relative}"
            )
        artifacts[relative] = content_sha256(
            _read_source_file(path, f"{label} {relative}")
        )
    if not artifacts:
        raise CapacityCandidateRuntimeError(
            f"{label} artifact set is empty"
        )
    return artifacts


def _scan_files(root: Path, *, suffixes: set[str]) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise CapacityCandidateRuntimeError(
            f"artifact root is missing or unsafe: {root}"
        )
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CapacityCandidateRuntimeError(
                f"artifact tree contains a symlink: {path}"
            )
        if path.is_file() and path.suffix in suffixes:
            paths.append(path)
    return paths


def _read_source_file(path: Path, label: str) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if nofollow is None or cloexec is None:
        raise CapacityCandidateRuntimeError(
            f"{label} cannot be read safely"
        )
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | nofollow | cloexec,
        )
    except (OSError, ValueError) as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} is missing or unsafe"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CapacityCandidateRuntimeError(
                f"{label} is not a regular file"
            )
        if before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CapacityCandidateRuntimeError(
                f"{label} is group/world writable"
            )
        if before.st_size < 0 or before.st_size > MAX_SOURCE_FILE_BYTES:
            raise CapacityCandidateRuntimeError(
                f"{label} exceeds the size limit"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(
                    1024 * 1024,
                    MAX_SOURCE_FILE_BYTES + 1 - total,
                ),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_FILE_BYTES:
                raise CapacityCandidateRuntimeError(
                    f"{label} exceeds the size limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_mode",
            "st_uid",
        )
        if tuple(getattr(before, field) for field in identity_fields) != tuple(
            getattr(after, field) for field in identity_fields
        ):
            raise CapacityCandidateRuntimeError(
                f"{label} changed while being read"
            )
        if total != before.st_size:
            raise CapacityCandidateRuntimeError(
                f"{label} size changed while being read"
            )
        return b"".join(chunks)
    except CapacityCandidateRuntimeError:
        raise
    except OSError as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} could not be read"
        ) from exc
    finally:
        os.close(descriptor)


def _default_command_runner(command: tuple[str, ...]) -> bytes:
    if not command or not Path(command[0]).is_absolute():
        raise CapacityCandidateRuntimeError(
            "capacity command must use an absolute executable"
        )
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin",
                "LANG": "C",
                "LC_ALL": "C",
            },
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityCandidateRuntimeError(
            f"capacity command failed to run: {command[0]}"
        ) from exc
    if completed.returncode != 0:
        raise CapacityCandidateRuntimeError(
            f"capacity command failed: {command[0]}"
        )
    if (
        not completed.stdout
        or len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise CapacityCandidateRuntimeError(
            f"capacity command output is empty or oversized: {command[0]}"
        )
    return completed.stdout


def _run_command(
    runner: CommandRunner,
    command: tuple[str, ...],
    label: str,
) -> bytes:
    try:
        output = runner(command)
    except CapacityCandidateRuntimeError:
        raise
    except Exception as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} command failed"
        ) from exc
    if not isinstance(output, bytes):
        raise CapacityCandidateRuntimeError(
            f"{label} command must return bytes"
        )
    if not output or len(output) > MAX_COMMAND_OUTPUT_BYTES:
        raise CapacityCandidateRuntimeError(
            f"{label} command output is empty or oversized"
        )
    return output


def _command_text(
    runner: CommandRunner,
    command: tuple[str, ...],
    label: str,
) -> str:
    raw = _run_command(runner, command, label)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} command output is not UTF-8"
        ) from exc
    return _nonempty(value, label)


def _resolve_conda_executable() -> Path:
    executable = Path(sys.executable).resolve()
    candidates = [
        ancestor / "bin" / "conda"
        for ancestor in executable.parents
    ]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    raise CapacityCandidateRuntimeError(
        "conda executable could not be resolved from service Python"
    )


def _validated_project_root(root: Path) -> Path:
    if not root.is_absolute():
        raise CapacityCandidateRuntimeError(
            "project_root must be absolute"
        )
    if root.is_symlink() or not root.is_dir():
        raise CapacityCandidateRuntimeError(
            "project_root is missing or unsafe"
        )
    resolved = root.resolve()
    if resolved != root:
        raise CapacityCandidateRuntimeError(
            "project_root cannot contain symlink indirection"
        )
    return resolved


def _project_file(root: Path, path: Path, label: str) -> Path:
    candidate = path if path.is_absolute() else root / path
    _relative_project_path(root, candidate, label)
    return candidate


def _relative_project_path(
    root: Path,
    path: Path,
    label: str,
) -> str:
    current = path.absolute()
    while current != root:
        if current.is_symlink():
            raise CapacityCandidateRuntimeError(
                f"{label} path contains symlink indirection"
            )
        parent = current.parent
        if parent == current:
            raise CapacityCandidateRuntimeError(
                f"{label} path escapes project_root"
            )
        current = parent
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} path escapes project_root"
        ) from exc
    if not relative or relative.startswith("../"):
        raise CapacityCandidateRuntimeError(
            f"{label} path is invalid"
        )
    return relative


def _parse_json_object(raw: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CapacityCandidateRuntimeError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapacityCandidateRuntimeError(
            f"{label} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CapacityCandidateRuntimeError(
            f"{label} root must be an object"
        )
    return value


def _mapping_rows(result: object) -> list[dict[str, object]]:
    rows = result.mappings().all()
    if not isinstance(rows, list):
        rows = list(rows)
    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise CapacityCandidateRuntimeError(
                "database query returned a non-mapping row"
            )
        normalized.append(
            {
                str(key): _json_scalar(value, str(key))
                for key, value in row.items()
            }
        )
    return normalized


def _json_scalar(value: object, field: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeError as exc:
            raise CapacityCandidateRuntimeError(
                f"database field {field} is not UTF-8"
            ) from exc
    raise CapacityCandidateRuntimeError(
        f"database field {field} is not JSON-safe"
    )


def _merge_artifacts(
    target: dict[str, str],
    source: Mapping[str, str],
    label: str,
) -> None:
    overlap = set(target) & set(source)
    if overlap:
        conflicts = [
            key
            for key in sorted(overlap)
            if target[key] != source[key]
        ]
        if conflicts:
            raise CapacityCandidateRuntimeError(
                f"{label} has conflicting artifact paths: {conflicts}"
            )
    target.update(source)


def _text_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapacityCandidateRuntimeError(
            f"{field} must be non-empty text"
        )
    if value != value.strip():
        raise CapacityCandidateRuntimeError(
            f"{field} must be canonical text"
        )
    return value


def _sha256_value(value: object, field: str) -> str:
    normalized = _text_value(value, field)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapacityCandidateRuntimeError(
            f"{field} must be lowercase SHA-256"
        )
    return normalized


def _positive_int_value(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityCandidateRuntimeError(
            f"{field} must be a positive integer"
        )
    return value


def _nonempty(value: str, field: str) -> str:
    if not value:
        raise CapacityCandidateRuntimeError(
            f"{field} must be non-empty"
        )
    return value
